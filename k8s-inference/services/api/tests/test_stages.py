"""Stage composition (stages.py, docs/JOBS.md "Stage composition"): a root declares its stages, each stage is submitted
as a run that waits without a Kueue queue, the gate admits it when its dependencies delivered (results bound into the
pod, its own binding delivered only then), a failed dependency cancels it, the root's cancel fans out, the deadline
closes the graph, the root is terminal only with its stages, and a stage that ran is never run again. In-memory
cluster and ledger; the database rules of the ledger are in test_database.py."""

import copy
import json

import pytest

import attempts
import auth
import execution
import reconciliation
import stages
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_attempts import _record

NS = "tenant-demo"
GRAPH = {"stages": {"gpu-0": [], "gpu-1": [], "collect": ["gpu-0", "gpu-1"]}}
P2 = {"Authorization": "Bearer sk-other"}


def _auth(monkeypatch, values):
    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)


def _principal(owner, **limits):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **limits}


@pytest.fixture
def world(client, admin_keys, fake_keys, monkeypatch):
    """A root with the graph CPU -> (gpu-0, gpu-1) -> collect; durable records in memory."""
    c, cl = client
    _auth(monkeypatch, {"sk-good": _principal("p1"), "sk-other": _principal("p2")})
    rows: list = []
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [r for r in rows if r["operation"].startswith(root)])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "successor_of", lambda operation: None)
    monkeypatch.setattr(attempts, "synthesize", lambda operation, tenant: None)
    cancelled: list = []
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: cancelled.append(operation))
    import app as appmod

    monkeypatch.setattr(appmod.artifacts, "list_outputs", lambda ns, id, region, prefix=None: [])
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}, "execution": {"graph": GRAPH}})
    assert r.status_code == 202, r.text
    root = r.json()["id"]
    assert fake_keys.graphs[root]["stages"] == GRAPH["stages"]
    assert fake_keys.graphs[root]["owner"] == cl.jobs[(NS, root)]["metadata"]["annotations"]["serverless2.nebius/owner"]
    return c, cl, fake_keys, rows, cancelled, root


def _submit(c, root, sid, headers=H, **extra):
    return c.post(
        "/v1/models/hello-run:invoke",
        headers=headers,
        json={"input": {}, "execution": {"stage": {"root": root, "id": sid}, **extra}},
    )


def _succeed(cl, rows, name, result_ref="s3://b/out"):
    cl.jobs[(NS, name)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows.append(
        {**_record(name, f"{name}-a", export="succeeded", result_ref=result_ref), "tenant": "demo", "root": name}
    )


def _fail(cl, rows, name):
    cl.jobs[(NS, name)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    rows.append({**_record(name, f"{name}-a", st="failed", exit_code=1), "tenant": "demo", "root": name})


def _main_env(job):
    main = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    return {e["name"]: e for e in main.get("env") or []}


def test_graph_validation():
    ok = execution.validate_graph({"a": [], "b": ["a", "root"]}, 3600)
    assert ok == {"stages": {"a": [], "b": ["a", "root"]}, "deadline_s": 3600}
    wide = {f"shard-{i}": [] for i in range(128)} | {"collect": [f"shard-{i}" for i in range(128)]}
    assert len(execution.validate_graph(wide, 14 * 86400)["stages"]) == 129  # ESMFold-class: 128 shards + collector
    for stages_, why in (
        ({"a": ["b"], "b": ["a"]}, "cycle"),
        ({"a": ["x"]}, "not declared"),
        ({"a": ["a"]}, "itself"),
        ({"root": []}, "root"),
        ({"a": ["b", "b"], "b": []}, "duplicates"),
        ({f"s{i}": [] for i in range(257)}, "256"),
        ({}, "1 to"),
    ):
        with pytest.raises(Exception) as e:
            execution.validate_graph(stages_, None)
        assert why in str(e.value.detail), why


def test_stage_waits_without_queue_and_runs_after_its_dependencies(world):
    """The whole graph: stages submitted before their dependencies ran wait suspended and unqueued; the gate admits
    gpu-0 and gpu-1 once the root delivered (results bound into the pod), collect once both delivered; the root is
    RUNNING (phase stages) until collect delivered, then SUCCEEDED with every stage's result; the gate is idempotent."""
    c, cl, fk, rows, _cancelled, root = world
    names = {}
    for sid in ("collect", "gpu-0", "gpu-1"):  # any order: collect first
        r = _submit(c, root, sid)
        assert r.status_code == 202, r.text
        names[sid] = r.json()["id"]
        assert names[sid] == f"{root}-s-{sid}"
        job = cl.jobs[(NS, names[sid])]
        assert job["spec"]["suspend"] is True
        assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == stages.WAITING_QUEUE  # never a real queue
        assert job["metadata"]["labels"][stages.WAITING] == "true"
        assert job["metadata"]["labels"][stages.STAGE_ROOT] == root
        assert _main_env(job)["DEPENDENCY_RESULTS"]["valueFrom"]["fieldRef"]["fieldPath"].endswith(
            f"['{stages.RESULTS}']"
        )
        assert r.json()["status"] == "QUEUED" and r.json()["phase"] == "waiting_for_dependencies"
    prepared = [x for x in fk.calls if x[0] == "prepare"]
    assert [(x[1], x[2], x[3], x[7], x[8]) for x in prepared] == [
        (root, names["collect"], None, "stage", "collect"),
        (root, names["gpu-0"], None, "stage", "gpu-0"),
        (root, names["gpu-1"], None, "stage", "gpu-1"),
    ]  # on the root's ledger, no head rule, each its declared stage
    assert cl.jobs[(NS, names["gpu-0"])]["metadata"]["annotations"][stages.DEPENDS_ON] == "root"
    assert cl.jobs[(NS, names["collect"])]["metadata"]["annotations"][stages.DEPENDS_ON] == "gpu-0,gpu-1"
    # a replay of a waiting stage is the Job's replay: no second member, no second Job
    again = _submit(c, root, "gpu-0")
    assert again.status_code == 200 and again.json()["id"] == names["gpu-0"]
    assert len([x for x in fk.calls if x[0] == "prepare"]) == 3
    # the root is not done: nothing is admitted; the handle shows the graph pending
    assert reconciliation.gate_stages() == []
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["graph"]["status"] == "PENDING" and op["status"] == "QUEUED"
    assert {s["id"]: s["status"] for s in op["graph"]["stages"]} == {
        "gpu-0": "QUEUED",
        "gpu-1": "QUEUED",
        "collect": "QUEUED",
    }
    assert "waiting for root" in cl.jobs[(NS, names["gpu-0"])]["metadata"]["annotations"][stages.WAIT_REASON]
    # the root delivers: the two GPU stages are handed to Kueue with the root's result; collect keeps waiting
    _succeed(cl, rows, root, "s3://b/root")
    out = reconciliation.gate_stages()
    assert sorted(out) == sorted([f"{names['gpu-0']}:admitted", f"{names['gpu-1']}:admitted"])
    for sid in ("gpu-0", "gpu-1"):
        job = cl.jobs[(NS, names[sid])]
        assert (
            job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]
            == job["metadata"]["labels"]["serverless2.nebius/profile"]
        )
        assert stages.WAITING not in job["metadata"]["labels"]
        bound = json.loads(job["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])
        assert bound == {
            "root": {
                "operation": root,
                "producer": root,
                "result_ref": "s3://b/root",
                "attempt_id": f"{root}-a",
                "epoch": None,
            }
        }
        assert job["spec"]["suspend"] is True  # Kueue unsuspends when it admits
    assert stages.is_waiting(cl.jobs[(NS, names["collect"])])
    assert reconciliation.gate_stages() == []  # idempotent: nothing changes on the next pass
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["status"] == "RUNNING" and op["phase"] == "stages" and "collect" in op["reason"]
    assert c.get(f"/v1/operations/{root}/result", headers=H).status_code == 409  # not terminal yet
    # both GPU stages deliver: collect runs with both results
    _succeed(cl, rows, names["gpu-0"], "s3://b/g0")
    _succeed(cl, rows, names["gpu-1"], "s3://b/g1")
    assert reconciliation.gate_stages() == [f"{names['collect']}:admitted"]
    bound = json.loads(cl.jobs[(NS, names["collect"])]["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])
    assert {k: v["result_ref"] for k, v in bound.items()} == {"gpu-0": "s3://b/g0", "gpu-1": "s3://b/g1"}
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["status"] == "RUNNING" and op["graph"]["status"] == "PENDING"
    # collect delivers: the root is terminal with every stage's result
    _succeed(cl, rows, names["collect"], "s3://b/final")
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["status"] == "SUCCEEDED" and op["graph"]["status"] == "SUCCEEDED"
    assert {s["id"]: s["result_ref"] for s in op["graph"]["stages"]} == {
        "gpu-0": "s3://b/g0",
        "gpu-1": "s3://b/g1",
        "collect": "s3://b/final",
    }
    assert c.get(f"/v1/operations/{root}/result", headers=H).status_code == 200
    st = c.get(f"/v1/operations/{names['collect']}", headers=H).json()
    assert st["stage"] == {"root": root, "id": "collect", "depends_on": ["gpu-0", "gpu-1"], "reason": None}


def test_a_settled_stage_is_never_run_again(world, monkeypatch):
    """A stage whose member settled (it ran) answers a resubmission with its durable state, even after its Job is
    gone; no new Job, no new member (crash replay, repeated notifications)."""
    c, cl, fk, rows, _cancelled, root = world
    name = _submit(c, root, "gpu-0").json()["id"]
    _succeed(cl, rows, root)
    reconciliation.gate_stages()
    _succeed(cl, rows, name, "s3://b/g0")
    fk.settle_run_member(name, 10.0)  # billing settled it
    again = _submit(c, root, "gpu-0")
    assert again.status_code == 200 and again.json()["id"] == name and again.json()["status"] == "SUCCEEDED"
    del cl.jobs[(NS, name)]  # the Job expired: the durable rows answer
    syn = {"id": name, "status": "SUCCEEDED", "result_ref": "s3://b/g0", "mode": "run", "region": "eu-north1"}
    monkeypatch.setattr(attempts, "synthesize", lambda operation, tenant: dict(syn) if operation == name else None)
    again = _submit(c, root, "gpu-0")
    assert again.status_code == 200 and again.json()["id"] == name
    assert (NS, name) not in cl.jobs and len([x for x in fk.calls if x[0] == "prepare"]) == 1


def test_failed_dependency_holds_its_dependents_until_resumed_or_cancelled(world):
    """A FAILED dependency is resumable, so its dependents wait (reason on the handle) instead of dying; its resume
    is a member of the same stage lineage on the root's key, the successor carries the stage identity and the bound
    results, and once it delivers the dependents run with the successor's result. A CANCELLED dependency cancels
    them once."""
    c, cl, fk, rows, cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    g1 = _submit(c, root, "gpu-1").json()["id"]
    col = _submit(c, root, "collect").json()["id"]
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    _succeed(cl, rows, g0, "s3://b/g0")
    _fail(cl, rows, g1)
    assert reconciliation.gate_stages() == []  # collect waits: gpu-1 may be resumed
    reason = cl.jobs[(NS, col)]["metadata"]["annotations"][stages.WAIT_REASON]
    assert "gpu-1 FAILED" in reason and ":resume" in reason
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["status"] == "RUNNING" and op["graph"]["status"] == "PENDING"
    assert {s["id"]: s["status"] for s in op["graph"]["stages"]} == {
        "gpu-0": "SUCCEEDED",
        "gpu-1": "FAILED",
        "collect": "QUEUED",
    }
    # the resume: admitted on the graph root as the next member of stage gpu-1's lineage (sibling gpu-0 untouched)
    r = c.post(f"/v1/operations/{g1}:resume", headers=H, json={})
    assert r.status_code == 202, r.text
    succ = r.json()["id"]
    assert succ == f"{g1}-r1"
    prepared = [x for x in fk.calls if x[0] == "prepare" and x[2] == succ]
    assert [(x[1], x[3], x[7], x[8]) for x in prepared] == [(root, g1, "stage", "gpu-1")]
    sj = cl.jobs[(NS, succ)]
    assert sj["metadata"]["labels"][stages.STAGE_ROOT] == root and stages.WAITING not in sj["metadata"]["labels"]
    assert sj["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]  # not gated again: its dependencies delivered already
    assert sj["metadata"]["annotations"][stages.STAGE_ID] == "gpu-1"
    assert (
        json.loads(sj["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])["root"]["result_ref"]
        == "s3://b/root"
    )
    assert "DEPENDENCY_RESULTS" in _main_env(sj)
    # a resubmission of the stage answers with its lineage head, never a new Job
    again = _submit(c, root, "gpu-1")
    assert again.status_code == 200 and again.json()["id"] == succ
    _succeed(cl, rows, succ, "s3://b/g1-again")
    assert reconciliation.gate_stages() == [f"{col}:admitted"]
    bound = json.loads(cl.jobs[(NS, col)]["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])
    assert bound["gpu-1"] == {
        "operation": succ,
        "producer": succ,
        "result_ref": "s3://b/g1-again",
        "attempt_id": f"{succ}-a",
        "epoch": None,
    }
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    g1_view = next(s for s in op["graph"]["stages"] if s["id"] == "gpu-1")
    assert g1_view["status"] == "SUCCEEDED" and g1_view["operation"] == succ and g1_view["continuations"] == 1
    # a cancelled dependency cancels what depends on it, once
    other = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}, "execution": {"graph": GRAPH}})
    root2 = other.json()["id"]
    a = _submit(c, root2, "gpu-0").json()["id"]
    b = _submit(c, root2, "collect").json()["id"]
    assert c.post(f"/v1/operations/{a}:cancel", headers=H).status_code == 200
    _succeed(cl, rows, root2)
    assert reconciliation.gate_stages() == [f"{b}:cancelled"]
    assert (NS, b) not in cl.jobs and b in cancelled and ("settle", b, 0.0) in fk.calls
    assert "gpu-0" in next(m for m in fk.members if m["operation"] == b)["note"]
    assert reconciliation.gate_stages() == []


def test_stage_export_retry_follows_the_stage_lineage(world):
    """An export retry of a stage is a transfer-only member of that stage's lineage on the root's key (predecessor =
    the stage), while a sibling is active."""
    c, cl, fk, rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    g1 = _submit(c, root, "gpu-1").json()["id"]
    _succeed(cl, rows, root)
    reconciliation.gate_stages()
    cl.jobs[(NS, g1)]["status"] = {"startTime": "2026-10-13T20:00:00Z"}  # the sibling runs
    cl.jobs[(NS, g0)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows.append({**_record(g0, f"{g0}-a", export="failed"), "tenant": "demo", "root": g0})
    r = c.post(f"/v1/operations/{g0}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = r.json()["id"]
    prepared = [x for x in fk.calls if x[0] == "prepare" and x[2] == x1]
    assert [(x[1], x[3], x[6], x[7], x[8]) for x in prepared] == [(root, g0, True, "stage", "gpu-0")]
    assert cl.jobs[(NS, x1)]["metadata"]["labels"][stages.STAGE_ROOT] == root
    assert len([m for m in fk.members if m["root"] == root and m.get("stage") == "gpu-1"]) == 1  # untouched


def test_success_without_a_result_is_not_runnable(world):
    c, cl, fk, rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    cl.jobs[(NS, root)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows.append({**_record(root, f"{root}-a", export="succeeded"), "result_ref": None, "tenant": "demo", "root": root})
    assert reconciliation.gate_stages() == [f"{g0}:cancelled"]
    assert "without a result_ref" in next(m for m in fk.members if m["operation"] == g0)["note"]


def test_root_cancel_fans_out_and_nothing_attaches_afterwards(world):
    c, cl, fk, rows, cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    _succeed(cl, rows, root)
    reconciliation.gate_stages()  # gpu-0 handed to Kueue
    cl.jobs[(NS, g0)]["status"] = {"startTime": "2026-10-13T20:00:00Z"}  # running now
    g1 = _submit(c, root, "gpu-1").json()["id"]  # still waiting
    r = c.post(f"/v1/operations/{root}:cancel", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert (NS, g1) not in cl.jobs and ("settle", g1, 0.0) in fk.calls  # waiting: deleted, settled at zero
    assert cl.jobs[(NS, g0)]["metadata"]["annotations"]["serverless2.nebius/cancelled"] == "true"  # running: stopped
    assert g0 in cancelled and g1 in cancelled
    assert fk.graphs[root]["cancelled_at"] and "cancelled by the caller" in fk.graphs[root]["reason"]
    assert r.json()["graph"]["status"] == "CANCELLED"
    late = _submit(c, root, "collect")
    assert late.status_code == 409 and "no stage attaches" in late.text
    assert reconciliation.gate_stages() == []  # closed graphs are not visited


def test_late_attach_undeclared_other_owner_and_idempotency_key_are_refused(world):
    c, cl, fk, _rows, _cancelled, root = world
    assert _submit(c, root, "nope").status_code == 400
    assert _submit(c, root, "gpu-0", headers=P2).status_code == 403
    assert _submit(c, "op-hello-run-00000000", "gpu-0").status_code == 404
    r = c.post(
        "/v1/models/hello-run:invoke",
        headers={**H, "Idempotency-Key": "k"},
        json={"input": {}, "execution": {"stage": {"root": root, "id": "gpu-0"}}},
    )
    assert r.status_code == 400 and "Idempotency-Key" in r.text
    r = c.post(
        "/v1/models/hello-run:invoke",
        headers=H,
        json={"input": {}, "execution": {"stage": {"root": root, "id": "gpu-0"}, "graph": GRAPH}},
    )
    assert r.status_code == 400 and "not both" in r.text
    assert (
        c.post(
            "/v1/models/hello-run:invoke",
            headers=H,
            json={"input": {}, "execution": {"graph": {"stages": {"a": ["z"]}}}},
        ).status_code
        == 400
    )
    # a stage is never queued twice: one Job per declared stage
    assert len([x for x in fk.calls if x[0] == "prepare"]) == 0


def test_graph_deadline_cancels_what_waits_and_closes_the_graph(world):
    from datetime import datetime, timedelta, timezone

    c, cl, fk, _rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    fk.graphs[root]["deadline_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    out = reconciliation.gate_stages()
    assert out == [f"{g0}:cancelled", f"{root}:closed"]
    assert (NS, g0) not in cl.jobs
    assert "never submitted: collect, gpu-1" in fk.graphs[root]["reason"]
    op = c.get(f"/v1/operations/{root}", headers=H).json()
    assert op["graph"]["status"] == "FAILED" and op["graph"]["cancelled"] is True
    assert {s["id"]: s["status"] for s in op["graph"]["stages"]} == {
        "gpu-0": "CANCELLED",
        "gpu-1": "NOT_SUBMITTED",
        "collect": "NOT_SUBMITTED",
    }


def test_a_stage_binding_is_consumed_only_when_the_stage_is_admitted(world):
    """A stage with a private binding: nothing is delivered or claimed while it waits (the binding may not even exist
    yet); when its dependencies delivered, the gate claims and copies the binding, then hands the stage to Kueue."""
    c, cl, fk, rows, _cancelled, root = world
    r = _submit(c, root, "gpu-0", private_input_ref="late")
    assert r.status_code == 202, r.text
    g0 = r.json()["id"]
    assert not [k for k in cl.secrets if k[1] == f"{g0}-private"]  # nothing delivered at submission
    _succeed(cl, rows, root)
    assert reconciliation.gate_stages() == []  # ready, but the binding is not there: keeps waiting with the reason
    assert "not admitted" in cl.jobs[(NS, g0)]["metadata"]["annotations"][stages.WAIT_REASON]
    assert stages.is_waiting(cl.jobs[(NS, g0)])
    assert c.put("/v1/bindings/late", json={"data": {"input_url": "https://x/in"}}, headers=H).status_code == 200
    assert reconciliation.gate_stages() == [f"{g0}:admitted"]
    binding = cl.secrets[(NS, execution.BINDING_PREFIX + "late")]
    assert binding["metadata"]["annotations"]["serverless2.nebius/operation"] == g0
    assert (NS, f"{g0}-private") in cl.secrets
    assert not stages.is_waiting(cl.jobs[(NS, g0)])


def test_no_database_no_stages(client, monkeypatch):
    c, _ = client
    r = c.post(
        "/v1/models/hello-run:invoke",
        headers=H,
        json={"input": {}, "execution": {"stage": {"root": "op-x", "id": "a"}}},
    )
    assert r.status_code == 409 and "ledger" in r.text


def test_second_continuation_keeps_the_stage_identity_and_results(world):
    """resume -> resume and resume -> export: every successor carries the stage root (label and annotation), the stage
    id, the dependencies and the bound results, because each inherits from the previous member's annotations."""
    c, cl, fk, rows, _cancelled, root = world
    g1 = _submit(c, root, "gpu-1").json()["id"]
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    _fail(cl, rows, g1)
    r1 = c.post(f"/v1/operations/{g1}:resume", headers=H, json={}).json()["id"]
    _fail(cl, rows, r1)
    r = c.post(f"/v1/operations/{r1}:resume", headers=H, json={})
    assert r.status_code == 202, r.text
    r2 = r.json()["id"]
    assert r2 == f"{g1}-r2"
    for name in (r1, r2):
        j = cl.jobs[(NS, name)]
        assert j["metadata"]["labels"][stages.STAGE_ROOT] == root
        assert j["metadata"]["annotations"][stages.STAGE_ROOT] == root
        assert j["metadata"]["annotations"][stages.STAGE_ID] == "gpu-1"
        assert j["metadata"]["annotations"][stages.DEPENDS_ON] == "root"
        assert json.loads(j["metadata"]["annotations"][stages.RESULTS])["root"]["result_ref"] == "s3://b/root"
        assert json.loads(j["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])["root"]["result_ref"] == (
            "s3://b/root"
        )
        assert "DEPENDENCY_RESULTS" in _main_env(j)
    assert [x[3] for x in fk.calls if x[0] == "prepare" and x[2] in (r1, r2)] == [g1, r1]  # each after the previous
    # an export retry of the second resume keeps it all, too
    cl.jobs[(NS, r2)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows.append({**_record(r2, f"{r2}-a", export="failed"), "tenant": "demo", "root": g1})
    x = c.post(f"/v1/operations/{r2}:export", headers=H)
    assert x.status_code == 202, x.text
    xj = cl.jobs[(NS, x.json()["id"])]
    assert (
        xj["metadata"]["annotations"][stages.STAGE_ROOT] == root
        and xj["metadata"]["annotations"][stages.STAGE_ID] == "gpu-1"
    )
    assert [x[3] for x in fk.calls if x[0] == "prepare" and x[2] == xj["metadata"]["name"]] == [r2]


def test_resume_or_export_of_an_older_member_is_refused_with_the_head_named(world):
    """A public resume/export renders from the Job the caller names; the ledger admits after the lineage's head. An
    older member that a newer compute successor continued is refused (409 naming the head), whether the successor is
    active or delivered; the head itself continues; an export retry of the named member is still followed on it."""
    c, cl, fk, rows, _cancelled, root = world
    g1 = _submit(c, root, "gpu-1").json()["id"]
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    _fail(cl, rows, g1)
    r1 = c.post(f"/v1/operations/{g1}:resume", headers=H, json={}).json()["id"]
    cl.jobs[(NS, r1)]["status"] = {"startTime": "2026-10-13T20:00:00Z"}  # the successor runs
    stale = c.post(f"/v1/operations/{g1}:resume", headers=H, json={})
    assert stale.status_code == 409 and r1 in stale.json()["detail"], stale.text
    stale = c.post(f"/v1/operations/{g1}:export", headers=H)
    assert stale.status_code == 409 and r1 in stale.json()["detail"], stale.text
    _succeed(cl, rows, r1, "s3://b/g1")  # delivered: still not the old member's to resume
    stale = c.post(f"/v1/operations/{g1}:resume", headers=H, json={})
    assert stale.status_code == 409 and r1 in stale.json()["detail"]
    assert len([x for x in fk.calls if x[0] == "prepare" and x[1] == root]) == 2  # g1, r1: nothing else admitted
    assert len([k for k in cl.jobs if k[1].startswith(g1)]) == 2
    # the head's own export retry, then a resume of the head: allowed (the x-member leaves the compute where it is)
    rows[-1] = {**_record(r1, f"{r1}-a", export="failed"), "tenant": "demo", "root": g1}
    x = c.post(f"/v1/operations/{r1}:export", headers=H)
    assert x.status_code == 202, x.text
    x1 = x.json()["id"]
    assert x1 == f"{g1}-x1" and cl.jobs[(NS, x1)]["metadata"]["annotations"]["serverless2.nebius/export-of"] == r1
    assert c.post(f"/v1/operations/{r1}:export", headers=H).status_code in (200, 202)  # followed on the member
    cl.jobs[(NS, x1)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    rows.append({**_record(x1, f"{x1}-a", export="failed"), "tenant": "demo", "root": g1})
    again = c.post(f"/v1/operations/{r1}:resume", headers=H, json={})  # the head is r1's own export retry: r1 continues
    assert again.status_code in (202, 409) and (again.status_code == 202 or "x1" not in again.json()["detail"])


def test_completed_graph_stage_replays_its_head_and_nothing_new_attaches(world):
    """After the graph closed (every stage settled) a resubmission of a stage that ran answers 200 with its lineage
    head; a declared stage that never was submitted gets 409 and no Job; a cancelled graph refuses every stage."""
    c, cl, fk, rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    _succeed(cl, rows, g0, "s3://b/g0")
    fk.settle_run_member(g0, 10.0)
    fk.graphs[root]["closed_at"] = "now"  # the ledger closed the run (database rule in test_database.py)
    again = _submit(c, root, "gpu-0")
    assert again.status_code == 200 and again.json()["id"] == g0 and again.json()["status"] == "SUCCEEDED"
    late = _submit(c, root, "gpu-1")
    assert late.status_code == 409 and "closed" in late.json()["detail"]
    assert (NS, f"{root}-s-gpu-1") not in cl.jobs and len([x for x in fk.calls if x[0] == "prepare"]) == 1
    fk.graphs[root]["cancelled_at"] = "now"
    assert _submit(c, root, "gpu-0").status_code == 409 and _submit(c, root, "gpu-1").status_code == 409


def test_waiting_stage_names_a_queue_that_never_exists(world):
    """Kueue's LocalQueueDefaulting injects `default` into a Job without a queue label when the namespace has a
    LocalQueue `default` (API51: a waiting stage was admitted, unsuspended and ran without its bindings). A waiting
    stage therefore always carries the queue label, naming a queue no tenant chart renders; the gate replaces it."""
    c, cl, fk, rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    labels = cl.jobs[(NS, g0)]["metadata"]["labels"]
    assert labels["kueue.x-k8s.io/queue-name"] == "serverless2-stage-waiting" == stages.WAITING_QUEUE
    assert stages.WAITING_QUEUE != "default"
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    assert labels["kueue.x-k8s.io/queue-name"] == labels["serverless2.nebius/profile"] != stages.WAITING_QUEUE


def test_dependency_delivered_by_a_finalizer_names_the_delivering_attempt(world):
    """A dependency whose compute attempt failed delivery and whose CPU finalizer (`<op>-x1`) delivered: the bound
    result names the delivering operation, attempt and epoch (never the compute attempt next to the finalizer's
    receipt), and `producer` the lineage member; the root's graph view shows `delivered_by`."""
    c, cl, fk, rows, _cancelled, root = world
    g0 = _submit(c, root, "gpu-0").json()["id"]
    col = _submit(c, root, "collect").json()["id"]
    g1 = _submit(c, root, "gpu-1").json()["id"]
    _succeed(cl, rows, root, "s3://b/root")
    reconciliation.gate_stages()
    _succeed(cl, rows, g1, "s3://b/g1")
    # gpu-0: compute done, delivery failed (exit 75 -> FAILED, export failed), then its finalizer delivered
    cl.jobs[(NS, g0)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    rows.append({**_record(g0, f"{g0}-a", st="failed", exit_code=75, export="failed"), "tenant": "demo", "root": g0})
    assert reconciliation.gate_stages() == []  # waits: FAILED, resumable / finalizable
    rows.append(
        {
            **_record(f"{g0}-x1", f"{g0}-x1-b", export="succeeded", result_ref="s3://b/g0-final", epoch=2),
            "tenant": "demo",
            "root": g0,
            "role": "finalize",
        }
    )
    assert reconciliation.gate_stages() == [f"{col}:admitted"]
    bound = json.loads(cl.jobs[(NS, col)]["spec"]["template"]["metadata"]["annotations"][stages.RESULTS])
    assert bound["gpu-0"] == {
        "operation": f"{g0}-x1",
        "producer": g0,
        "result_ref": "s3://b/g0-final",
        "attempt_id": f"{g0}-x1-b",
        "epoch": 2,
    }
    assert bound["gpu-1"]["operation"] == g1 and bound["gpu-1"]["attempt_id"] == f"{g1}-a"
    view = {s["id"]: s for s in c.get(f"/v1/operations/{root}", headers=H).json()["graph"]["stages"]}
    assert view["gpu-0"]["status"] == "SUCCEEDED" and view["gpu-0"]["delivered_by"] == f"{g0}-x1"
    assert view["gpu-0"]["attempt_id"] == f"{g0}-x1-b" and view["gpu-0"]["epoch"] == 2
    assert view["gpu-1"]["delivered_by"] is None
    own = c.get(f"/v1/operations/{g0}", headers=H).json()
    assert (
        own["exported_by"] == f"{g0}-x1"
        and own["exported_by_attempt_id"] == f"{g0}-x1-b"
        and own["exported_by_epoch"] == 2
    )


def test_delegated_stage_needs_operator_delegation_declaration_owner_and_live_root(
    client, admin_keys, fake_keys, monkeypatch
):
    """A stage on a model the key is not granted runs only as a delegated stage (docs/JOBS.md "Delegated stages"):
    the root's run class lists the model in `delegates` (operator-owned), the graph declared the stage on exactly that
    model, the caller is the root's owner on the root's key, the root is active. A declaration alone confers nothing;
    a direct :invoke of the model stays 403."""
    c, cl = client
    import catalog

    base = catalog.get("hello-run")
    delegating = {**base, "delegates": ["container-run"]}
    plain = catalog.get("container-run")
    monkeypatch.setattr(
        catalog, "get", lambda mid: delegating if mid == "hello-run" else plain if mid == "container-run" else None
    )
    monkeypatch.setattr(catalog, "require", lambda mid: catalog.get(mid) or (_ for _ in ()).throw(Exception(mid)))
    import app as appmod

    monkeypatch.setattr(appmod, "_model", lambda mid: catalog.get(mid))
    granted_root_only = {
        **KEY_INFO,
        "max_budget": None,
        "models": ["hello-run"],
        "metadata": {"tenant": "demo", "legacy": {"principal_id": "p1"}},
    }
    other = {**granted_root_only, "metadata": {"tenant": "demo", "legacy": {"principal_id": "p2"}}}
    _auth(monkeypatch, {"sk-good": granted_root_only, "sk-other": other})
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    graph = {"stages": {"gpu-0": {"depends_on": [], "model": "container-run"}, "plain": []}}
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}, "execution": {"graph": graph}})
    assert r.status_code == 202, r.text
    root = r.json()["id"]
    assert fake_keys.graphs[root]["models"] == {"gpu-0": "container-run"}
    inp = {"image": "img", "command": "echo"}
    # a direct run of the ungranted model: 403, declaration or not
    assert c.post("/v1/models/container-run:invoke", headers=H, json={"input": inp}).status_code == 403
    # the delegated stage: allowed, on the root's key, as a member of the root's run
    r = c.post(
        "/v1/models/container-run:invoke",
        headers=H,
        json={"input": inp, "execution": {"stage": {"root": root, "id": "gpu-0"}}},
    )
    assert r.status_code == 202, r.text
    sid = r.json()["id"]
    assert [x[1:3] for x in fake_keys.calls if x[0] == "prepare"] == [(root, sid)]
    assert c.get(f"/v1/operations/{sid}", headers=H).status_code == 200  # the owner reads its delegated stage
    assert c.get(f"/v1/operations/{sid}", headers=P2).status_code == 404
    # a stage declared without a model binding: the key's own grants decide (403 here)
    r = c.post(
        "/v1/models/container-run:invoke",
        headers=H,
        json={"input": inp, "execution": {"stage": {"root": root, "id": "plain"}}},
    )
    assert r.status_code == 403 and "not declared on it" in r.text
    # another owner, a model the root class does not delegate to, a closed graph: all refused
    assert (
        c.post(
            "/v1/models/container-run:invoke",
            headers=P2,
            json={"input": inp, "execution": {"stage": {"root": root, "id": "gpu-0"}}},
        ).status_code
        == 403
    )
    delegating["delegates"] = []
    r = c.post(
        "/v1/models/container-run:invoke",
        headers=H,
        json={"input": inp, "execution": {"stage": {"root": root, "id": "gpu-0"}}},
    )
    assert r.status_code == 403 and "does not delegate" in r.text
    delegating["delegates"] = ["container-run"]
    fake_keys.release(root, 0.0)  # the root not active on the key any more
    r = c.post(
        "/v1/models/container-run:invoke",
        headers=H,
        json={"input": inp, "execution": {"stage": {"root": root, "id": "gpu-0"}}},
    )
    assert r.status_code == 403 and "not active" in r.text
    # the declaration is validated at the root: a stage may only be bound to a run class that exists
    bad = {"stages": {"x": {"depends_on": [], "model": "no-such-model"}}}
    assert (
        c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}, "execution": {"graph": bad}}).status_code
        == 400
    )
