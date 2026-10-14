"""The ledger of a logical run (keys.py migration 11, docs/API.md "Keys"): one reservation per root, every
continuation a member after the current head, measured settlement, unknown GPU time kept, never-started cancels at
zero, late records of replaced attempts closing their accounting, same-owner replays on the original key. The
in-memory keys double records the calls; the database rules are in test_database.py."""

import copy

import pytest

import app as api
import attempts
import auth
import billing
import keys
from test_api import ADMIN, KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_attempts import _record


def _auth(monkeypatch, values):
    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(attempts, "enabled", lambda: False)


def _principal(owner, **limits):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **limits}


def test_second_export_retry_follows_the_current_head_not_the_compute_job(client, admin_keys, fake_keys, monkeypatch):
    """An export retry is a transfer-only member after the current head; a second retry follows the first retry (the
    head), while the compute Job keeps its retained volume as the source (handoff failure 3)."""
    c, cluster = client
    _auth(monkeypatch, {"sk-good": _principal("p1", max_concurrent=1)})
    oid = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}}).json()["id"]
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [{**_record(oid, f"{oid}-a", export="failed"), "operation": oid}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    first = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert first.status_code == 202, first.text
    x1 = first.json()["id"]
    cluster.jobs[("tenant-demo", x1)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    rows.append({**_record(x1, f"{x1}-b", export="failed"), "operation": x1})
    second = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert second.status_code == 202, second.text
    x2 = second.json()["id"]
    prepared = [x[1:4] for x in fake_keys.calls if x[0] == "prepare"]
    assert prepared == [(oid, x1, oid), (oid, x2, x1)]  # (root, member, predecessor): the head moves, the root stays
    assert all(x[6] is True for x in fake_keys.calls if x[0] == "prepare")  # transfer-only: no GPU bound
    spec = cluster.jobs[("tenant-demo", x2)]["spec"]["template"]["spec"]
    assert (
        next(v for v in spec["volumes"] if v["name"] == "work")["persistentVolumeClaim"]["claimName"] == f"{oid}-work"
    )


def test_queued_cancel_settles_the_member_at_zero(client, admin_keys, fake_keys, monkeypatch):
    """A run cancelled before it started is deleted and its member settled at zero (never-started is the evidence);
    the run's slot closes with it (handoff failure 2)."""
    c, cluster = client
    _auth(monkeypatch, {"sk-good": _principal("p1", max_concurrent=1)})
    oid = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}}).json()["id"]
    r = c.post(f"/v1/operations/{oid}:cancel", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert ("tenant-demo", oid) not in cluster.jobs
    assert ("settle", oid, 0.0) in fake_keys.calls


def test_same_owner_replay_keeps_the_original_billing_key(client, admin_keys, fake_keys, monkeypatch):
    """The same request under the same Idempotency-Key from another key of the same principal is a replay of the
    existing operation: no second admission, the Job keeps the original key's hash (handoff failure 5)."""
    c, cluster = client
    _auth(monkeypatch, {"sk-good": _principal("p1"), "sk-same": _principal("p1"), "sk-other": _principal("p2")})
    body = {"mode": "run", "input": {"message": "synthetic"}}
    first = c.post("/v1/models/hello-run:invoke", headers={**H, "Idempotency-Key": "same-intent"}, json=body)
    assert first.status_code == 202
    oid = first.json()["id"]
    original = cluster.jobs[("tenant-demo", oid)]["metadata"]["annotations"]["serverless2.nebius/key"]
    assert original == keys.key_hash("sk-good")
    replay = c.post(
        "/v1/models/hello-run:invoke",
        headers={"Authorization": "Bearer sk-same", "Idempotency-Key": "same-intent"},
        json=body,
    )
    assert replay.status_code == 200 and replay.json()["id"] == oid
    assert cluster.jobs[("tenant-demo", oid)]["metadata"]["annotations"]["serverless2.nebius/key"] == original
    assert len([x for x in fake_keys.calls if x[0] == "admit"]) == 1
    other = c.post(
        "/v1/models/hello-run:invoke",
        headers={"Authorization": "Bearer sk-other", "Idempotency-Key": "same-intent"},
        json=body,
    )
    assert other.status_code == 202 and other.json()["id"] != oid  # another principal: its own operation


def test_keys_me_reports_what_the_key_holds(client, fake_keys, monkeypatch):
    c, _ = client
    me = c.get("/v1/keys/me", headers=H).json()
    assert me["usage"] == {"active_operations": 0, "reserved_gpu_seconds": 0.0, "unknown_gpu_seconds": 0.0}


def test_incomplete_gpu_measurement_is_unknown_not_zero():
    """A GPU attempt without a valid start and end measures nothing: None, never 0.0 (handoff failure 4); a CPU-only
    run measures zero; a complete GPU attempt measures its seconds."""
    gpu_job = {
        "metadata": {"name": "root"},
        "spec": {"template": {"spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}},
    }
    assert (
        billing.measured_usage(gpu_job, [{"operation": "root", "pod": "root-a", "gpus": 1, "status": "failed"}]) is None
    )
    assert billing.measured_usage(gpu_job, []) is None
    complete = {**_record("root", "root-a"), "gpus": 2}
    assert billing.measured_usage(gpu_job, [complete]) == 7200.0
    assert (
        billing.measured_usage(
            gpu_job,
            [
                complete,
                {
                    "operation": "root",
                    "pod": "root-b",
                    "gpus": 1,
                    "status": "interrupted",
                    "started_at": "2026-10-11T10:00:00Z",
                },
            ],
        )
        is None
    )
    cpu_job = {"metadata": {"name": "cpu"}, "spec": {"template": {"spec": {"containers": [{"resources": {}}]}}}}
    assert (
        billing.measured_usage(cpu_job, [{"operation": "cpu", "pod": "cpu-a", "gpus": 0, "status": "succeeded"}]) == 0.0
    )


def test_job_deadline_reads_jobs_and_jobsets():
    assert api._job_deadline({"spec": {"activeDeadlineSeconds": 120}}) == 120
    assert api._job_deadline({"spec": {}}) is None
    js = {
        "kind": "JobSet",
        "spec": {
            "replicatedJobs": [{"template": {"spec": {"activeDeadlineSeconds": 900}}}, {"template": {"spec": {}}}]
        },
    }
    assert api._job_deadline(js) == 900


def test_an_ambiguous_continuation_create_failure_keeps_its_member(client, admin_keys, fake_keys, monkeypatch):
    """The cluster failed the create with no definite answer (a transport-class error): the API server may still be
    accepting it, so the member is kept (never rolled back on an immediate absence); the error reaches the caller,
    whose replay is admitted as a replay of the member, and the two-phase retirement settles a member that never
    gets its Job (test_a_phantom_head_is_marked_then_retired…)."""
    c, cluster = client
    _auth(monkeypatch, {"sk-good": _principal("p1", max_concurrent=1)})
    oid = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}}).json()["id"]
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}

    def boom(ns, body):
        raise RuntimeError("cluster unavailable")

    monkeypatch.setattr(cluster, "create_namespaced_job", boom)
    with pytest.raises(RuntimeError):  # the test client re-raises the server's error
        c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    kinds = [x[0] for x in fake_keys.calls]
    assert "prepare" in kinds and "rollback" not in kinds
    assert [m["operation"] for m in fake_keys.members] == [oid, f"{oid}-r1"]  # the member stays next to the root


def _exportable(c, cluster, monkeypatch):
    """A finished run with one failed-export attempt on record: `:export` renders its transfer-only retry."""
    _auth(monkeypatch, {"sk-good": _principal("p1", max_concurrent=1)})
    oid = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}}).json()["id"]
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [{**_record(oid, f"{oid}-a", export="failed"), "operation": oid}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    return oid, rows


def test_a_continuation_created_with_a_lost_answer_keeps_its_member_and_is_returned(
    client, admin_keys, fake_keys, monkeypatch
):
    """jobs.create accepted the Job but the answer was lost (a transport error after the create): the member is NOT
    rolled back (the Job exists and must stay accounted); the call returns the created continuation (202)."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create

    def created_but_lost(ns, job, region="eu-north1", secret=None):
        real(ns, job, region, secret)
        raise OSError("connection reset after the create")

    monkeypatch.setattr(jobs, "create", created_but_lost)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = r.json()["id"]
    assert ("tenant-demo", x1) in cluster.jobs
    assert ("rollback", x1) not in fake_keys.calls and fake_keys.member_of(x1)["state"] == "active"
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [(oid, x1, oid)]


def test_no_failure_after_admission_rolls_the_member_back(client, admin_keys, fake_keys, monkeypatch):
    """A 5xx or a transport error after admission may still be accepted by the API server; a 4xx proves only that THIS
    caller's create was refused. Either way the member stays a pending admission (never rolled back); the error reaches
    the caller, whose replay of the same request is admitted as a replay of the member and creates the Job."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create
    x1 = f"{oid}-x1"
    for code in (502, 404):
        monkeypatch.setattr(
            jobs,
            "create",
            lambda ns, job, region="eu-north1", secret=None, code=code: (_ for _ in ()).throw(
                api.HTTPException(code, "kubernetes refused" if code == 404 else "kubernetes (eu-north1): 503")
            ),
        )
        r = c.post(f"/v1/operations/{oid}:export", headers=H)
        assert r.status_code == code
        assert not [x for x in fake_keys.calls if x[0] == "rollback"] and fake_keys.member_of(x1)["state"] == "active"
        assert ("tenant-demo", x1) not in cluster.jobs
    monkeypatch.setattr(jobs, "create", real)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)  # the same intent: admitted as a replay, created now
    assert r.status_code == 202 and r.json()["id"] == x1 and ("tenant-demo", x1) in cluster.jobs
    assert [m["operation"] for m in fake_keys.members if m["operation"].startswith(f"{oid}-x")] == [x1]


def test_barrier_a_definite_refusal_of_one_replica_never_deletes_the_member_another_replica_backed(
    client, admin_keys, fake_keys, monkeypatch
):
    """Root's final same-intent barrier: A and B both observe the prior head (no pending admission), A pauses right
    after its admission; B's whole request runs (its admission is a replay of the shared member) and creates the valid
    Job; A's create is then refused definitively (a 4xx of its own). A's member is the shared one and B's Job stands
    on it: nothing is rolled back, A answers with its error, one Job, one member."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create
    seen: dict = {}

    def paused_then_refused(ns, job, region="eu-north1", secret=None):
        name = job["metadata"]["name"]
        assert fake_keys.member_of(name)["state"] == "active" and ("tenant-demo", name) not in cluster.jobs
        monkeypatch.setattr(jobs, "create", real)
        b = c.post(f"/v1/operations/{oid}:export", headers=H)  # B: replay of the shared member, creates the Job
        seen["b"] = (b.status_code, b.json())
        assert ("tenant-demo", name) in cluster.jobs
        raise api.HTTPException(403, "forbidden: this replica's create was refused")  # A's own definite refusal

    monkeypatch.setattr(jobs, "create", paused_then_refused)
    a = c.post(f"/v1/operations/{oid}:export", headers=H)
    x1 = f"{oid}-x1"
    assert seen["b"][0] == 202 and seen["b"][1]["id"] == x1, seen["b"]
    assert a.status_code in (200, 202, 403), a.text  # A: its own refusal, or B's Job found and returned
    assert not [x for x in fake_keys.calls if x[0] == "rollback"]  # the shared member is never deleted
    assert fake_keys.member_of(x1)["state"] == "active" and ("tenant-demo", x1) in cluster.jobs
    assert [m["operation"] for m in fake_keys.members if m["operation"].startswith(f"{oid}-x")] == [x1]
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [(oid, x1, oid), (oid, x1, oid)]


def test_a_pending_admission_is_never_retired_and_only_the_same_intent_replays_it(
    client, admin_keys, fake_keys, monkeypatch
):
    """A lineage head that was admitted but has no Job and no attempt record stays PENDING: nothing retires it (no
    rollback, ever); a replay of the same request renders the same operation name, is admitted as a replay of that
    member and creates its Job (one member, one Job, the member untouched); a head that ran and expired (on record)
    is kept and the stale source is refused as before."""
    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    pending = f"{oid}-x1"  # the name the export retry of `oid` renders (no -x Job exists): the same intent
    fake_keys.members.append(
        {
            "operation": pending,
            "root": oid,
            "predecessor": oid,
            "state": "active",
            "kind": "continuation",
            "stage": None,
            "note": None,
        }
    )
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202 and r.json()["id"] == pending, r.text
    assert ("tenant-demo", pending) in cluster.jobs and not [x for x in fake_keys.calls if x[0] == "rollback"]
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [(oid, pending, oid)]  # a replay of the member
    assert [m["operation"] for m in fake_keys.members if m["operation"].startswith(f"{oid}-x")] == [pending]
    # a head that ran and whose Job expired: on record, the stale source is refused with the head named, nothing touched
    del cluster.jobs[("tenant-demo", pending)]
    rows.append({**_record(pending, f"{pending}-b", export="failed"), "operation": pending})
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 409 and "continue" in r.text and not [x for x in fake_keys.calls if x[0] == "rollback"]


def test_barrier_a_second_replica_cannot_retire_an_admission_whose_create_is_in_flight(
    client, admin_keys, fake_keys, monkeypatch
):
    """Root's race (COORDINATION 12:18Z and the 91 review): replica A is paused between its admission and its create;
    replica B asks for the same continuation and sees an active member with no Job and no record. B retires nothing:
    its request is the same intent, so it is admitted as a replay of A's member and creates the Job; A's create then
    meets AlreadyExists (the same Job) and answers with it. One Job, one member, no rollback, nothing unaccounted,
    whatever the order of the two creates and whatever either replica reads in between."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create
    seen: dict = {}

    def paused_then_create(ns, job, region="eu-north1", secret=None):
        # the barrier: A has admitted (its member is active) and has not created yet; B's whole request runs here
        name = job["metadata"]["name"]
        assert fake_keys.member_of(name)["state"] == "active" and ("tenant-demo", name) not in cluster.jobs
        monkeypatch.setattr(jobs, "create", real)
        b = c.post(f"/v1/operations/{oid}:export", headers=H)
        seen["b"] = (b.status_code, b.json())
        assert not [x for x in fake_keys.calls if x[0] == "rollback"], "B retired A's in-flight admission"
        assert ("tenant-demo", name) in cluster.jobs  # B created the same operation under A's member
        return real(ns, job, region, secret)  # A's create is released: AlreadyExists, the same Job

    monkeypatch.setattr(jobs, "create", paused_then_create)
    a = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert a.status_code in (200, 202), a.text
    x1 = a.json()["id"]
    assert seen["b"][0] == 202 and seen["b"][1]["id"] == x1, seen["b"]
    assert [k for k in cluster.jobs if k[1].startswith(f"{oid}-x")] == [("tenant-demo", x1)]  # one Job
    assert [m["operation"] for m in fake_keys.members if m["operation"].startswith(f"{oid}-x")] == [x1]  # one member
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [
        (oid, x1, oid),
        (oid, x1, oid),
    ]  # A, then B's replay
    assert not [x for x in fake_keys.calls if x[0] == "rollback"]


def test_another_intent_is_refused_while_an_admission_is_pending(client, admin_keys, fake_keys, monkeypatch):
    """While a lineage has a pending admission, a continuation that would render ANOTHER operation (a resume instead of
    the pending export retry) is refused, naming the pending admission; nothing is admitted, nothing retired."""
    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    pending = f"{oid}-x1"
    fake_keys.members.append(
        {
            "operation": pending,
            "root": oid,
            "predecessor": oid,
            "state": "active",
            "kind": "continuation",
            "stage": None,
            "note": None,
        }
    )
    r = c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    assert r.status_code == 409 and "pending admission" in r.text and pending in r.text, r.text
    assert [m["operation"] for m in fake_keys.members] == [oid, pending] and not [
        x for x in fake_keys.calls if x[0] == "rollback"
    ]
    assert not [k for k in cluster.jobs if k[1].startswith(f"{oid}-r")]
