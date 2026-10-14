"""The application finalizer (docs/JOBS.md "Finalization by the application"): a model-owned command re-delivers from
the kept work volume after the compute ended with a 'delivery pending' exit code; through the existing :export path,
CPU only, an explicit role, the finalized compute attempt named in its environment across retries, one active finalizer
per root, bounded, cancellable from the root's handle, never turning a failed computation into a success."""

import json

import pytest

import app as appmod
import attempts
import catalog
import execution
import kube
import models as models_mod
from config import LABEL
from test_api import FLEET, H, client, cluster, pod  # noqa: F401

NS = "tenant-demo"
SPEC = {
    "id": "md",
    "kind": "job",
    "image": "registry.example/md:1",
    "command": "simulate",
    "gpu": {"count": 1, "classes": ["h100"]},
    "env": {"MD_MODE": "fast"},
    "finalize": {
        "command": "/opt/scientific-ai/bin/python -m scientific_ai.runner --finish-delivery",
        "on_exit_codes": [75],
        "timeout_s": 900,
        "cpu": "1",
        "memory": "2Gi",
    },
}


@pytest.fixture(autouse=True)
def _fresh_catalog():
    yield
    catalog._cache.update({"mtime": None, "models": {}})


def _model(spec=SPEC):
    return catalog.normalise({**models_mod.to_entry(spec, managed_by="api"), "version": 2})


@pytest.fixture
def failed_run(client, monkeypatch):
    """An application-I/O run of `md` that ended with exit 75 (compute done, delivery pending), its volume kept."""
    c, fake = client
    m = _model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "md" else None)
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    r = c.post(
        "/v1/models/md:invoke",
        json={"input": {}, "region": "eu-north1", "execution": {"io_mode": "application"}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    fake.jobs[(NS, op)]["status"] = {"conditions": [{"type": "Failed", "status": "True", "message": "exit 75"}]}
    rows = [
        {
            "operation": op,
            "pod": f"{op}-abc",
            "root": op,
            "tenant": "demo",
            "status": "failed",
            "exit_code": 75,
            "epoch": 1,
            "revision": 3,
            "gpus": 1,
            "gpu_class": "h100",
            "image_digest": "registry.example/md@sha256:0123",
            "started_at": "2026-10-12T19:00:00Z",
            "ended_at": "2026-10-12T19:30:00Z",
            "region": "eu-north1",
            "role": "run",
        }
    ]
    marks: dict = {}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [x for x in rows if x["root"] == root])
    monkeypatch.setattr(attempts, "_marks", lambda operation: marks.get(operation))
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: marks.__setitem__(operation, (1, "now", None)))
    return c, fake, op, rows, marks


def test_model_declares_the_finalizer_and_validation_holds(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    m = _model()
    assert m["job"]["finalize"] == {
        "command": SPEC["finalize"]["command"],
        "onExitCodes": [75],
        "timeoutS": 900,
        "cpu": "1",
        "memory": "2Gi",
    }
    assert "finalize" not in _model({k: v for k, v in SPEC.items() if k != "finalize"})["job"]
    from fastapi import HTTPException

    for bad in (
        {"on_exit_codes": [75]},
        {"command": "x", "on_exit_codes": []},
        {"command": "x", "on_exit_codes": [0]},
        {"command": "x", "timeout_s": 10},
    ):
        with pytest.raises(HTTPException) as e:
            _model({**SPEC, "finalize": bad})
        assert e.value.status_code == 400


def test_export_runs_the_model_finalizer_on_the_kept_volume(failed_run):
    c, fake, op, rows, marks = failed_run
    assert c.get(f"/v1/operations/{op}", headers=H).json()["status"] == "FAILED"
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = r.json()["id"]
    assert x1 == f"{op}-x1"
    j = fake.jobs[(NS, x1)]
    spec = j["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    assert main["image"] == "registry.example/md@sha256:0123"  # the finalized attempt's digest
    assert main["command"] == ["/bin/sh", "-c", SPEC["finalize"]["command"]] and "args" not in main
    assert main["resources"] == {
        "requests": {"cpu": "1", "memory": "2Gi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }  # CPU only
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert (
        env["ATTEMPT_ROLE"] == "finalize"
        and env["FINALIZE_OF"] == f"{op}-abc"
        and env["FINALIZE_EPOCH"] == "1"
        and env["FINALIZE_EXIT_CODE"] == "75"
        and env["WORK_DIR"] == "/work"
    )
    assert env["MD_MODE"] == "fast"  # the model's own environment stays
    assert next(v for v in spec["volumes"] if v["name"] == "work")["persistentVolumeClaim"]["claimName"] == f"{op}-work"
    assert "nodeSelector" not in spec and "affinity" not in spec
    # the compute attempt's bridge files are removed before main starts: no stale pointer, no old result marker
    assert [i["name"] for i in spec["initContainers"]] == ["bridge-reset"]
    reset = spec["initContainers"][0]["command"][-1]
    assert (
        all(
            f"/work/{f}" in reset
            for f in ("CHECKPOINT.json", "CHECKPOINT.ack.json", "RESULT.json", "attempts/.ack.json")
        )
        and "DELIVERY" not in reset
    )
    assert "nvidia.com/gpu" not in json.dumps(spec["initContainers"])
    assert j["spec"]["activeDeadlineSeconds"] == 900
    assert (
        j["metadata"]["annotations"][f"{LABEL}/attempt-role"] == "finalize"
        and j["metadata"]["annotations"][f"{LABEL}/finalize-of"] == f"{op}-abc"
    )
    assert (
        j["metadata"]["annotations"][f"{LABEL}/export-of"] == op
        and j["spec"]["template"]["metadata"]["labels"][f"{LABEL}/attempt-role"] == "finalize"
    )
    assert f"{LABEL}/gpu-class" not in j["metadata"]["labels"]
    uploader_env = {
        e["name"]: e.get("value") for e in next(x for x in spec["containers"] if x["name"] == "uploader")["env"]
    }
    assert uploader_env["GPUS"] == "0" and uploader_env["UPLOAD_MODE"] == "record"
    # while it runs: the root's handle shows FINALIZING with the finalizer's attempt, role and epoch; a second call is idempotent
    rows.append(
        {
            "operation": x1,
            "pod": f"{x1}-fin",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 2,
            "revision": 4,
            "gpus": 0,
            "role": "finalize",
            "started_at": "2026-10-12T19:40:00Z",
            "region": "eu-north1",
        }
    )
    rows[-1]["cluster"] = "north"  # the finalizer's record carries the canonical cluster id
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "FINALIZING"
        and got["finalizing_by"] == x1
        and got["attempt_id"] == f"{x1}-fin"
        and got["epoch"] == 2
        and got["attempt_role"] == "finalize"
    )
    assert got["cluster"] == "north" and got["region"] == "eu-north1"
    assert got["finalize_attempts"] == 1 and [a["attempt_role"] for a in got["attempts"]] == ["run", "finalize"]
    again = c.post(f"/v1/operations/{op}:export", headers=H)
    assert (
        again.status_code == 200
        and again.json()["id"] == x1
        and len([k for k in fake.jobs if k[1].startswith(f"{op}-x")]) == 1
    )
    assert c.post(f"/v1/operations/{x1}:export", headers=H).json()["id"] == x1  # the retry's own handle, too
    # delivered: the finalizer's record carries the result, the root is SUCCEEDED through it
    fake.jobs[(NS, x1)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows[-1].update(
        {
            "status": "succeeded",
            "export_state": "n/a",
            "result_ref": "app://md/result/1",
            "ended_at": "2026-10-12T19:45:00Z",
        }
    )
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "SUCCEEDED"
        and got["exported_by"] == x1
        and got["result_ref"] == "app://md/result/1"
        and "finalizing_by" not in got
    )


def test_finalizer_retries_keep_the_compute_attempt_and_are_bounded(failed_run):
    c, fake, op, rows, marks = failed_run
    for n in (1, 2, 3):
        r = c.post(f"/v1/operations/{op}:export", headers=H)
        assert r.status_code == 202, (n, r.text)
        x = fake.jobs[(NS, f"{op}-x{n}")]
        env = {
            e["name"]: e.get("value")
            for e in next(c_ for c_ in x["spec"]["template"]["spec"]["containers"] if c_["name"] == "main")["env"]
        }
        assert env["FINALIZE_OF"] == f"{op}-abc" and env["FINALIZE_EPOCH"] == "1"  # always the compute attempt
        # this finalizer failed again with 'delivery pending': the root is FAILED again, a further retry allowed
        x["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
        rows.append(
            {
                "operation": f"{op}-x{n}",
                "pod": f"{op}-x{n}-p",
                "root": op,
                "tenant": "demo",
                "status": "failed",
                "exit_code": 75,
                "epoch": n + 1,
                "revision": 10 + n,
                "gpus": 0,
                "role": "finalize",
                "started_at": "2026-10-12T20:00:00Z",
                "ended_at": "2026-10-12T20:05:00Z",
                "region": "eu-north1",
            }
        )
        got = c.get(f"/v1/operations/{op}", headers=H).json()
        assert got["status"] == "FAILED" and got["finalize_attempts"] == n
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 409 and "retries used" in r.text
    # a retry through a finalizer's own handle also names the compute attempt, never the finalizer
    r = c.post(f"/v1/operations/{op}-x2:export", headers=H)
    assert r.status_code == 409 and "retries used" in r.text


def test_never_finalizes_a_failed_computation(failed_run, monkeypatch):
    c, fake, op, rows, marks = failed_run
    rows[0]["exit_code"] = 1  # the compute itself failed
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 409 and "exit codes [75]" in r.text and (NS, f"{op}-x1") not in fake.jobs
    rows[0]["exit_code"] = 75
    plain = _model({k: v for k, v in SPEC.items() if k != "finalize"})
    monkeypatch.setattr(catalog, "get", lambda name: plain if name == "md" else None)  # no finalizer declared
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 409 and "job.finalize" in r.text


def test_cancel_on_the_root_stops_the_active_finalizer(failed_run):
    c, fake, op, rows, marks = failed_run
    x1 = c.post(f"/v1/operations/{op}:export", headers=H).json()["id"]
    rows.append(
        {
            "operation": x1,
            "pod": f"{x1}-fin",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 2,
            "revision": 4,
            "gpus": 0,
            "role": "finalize",
            "started_at": "2026-10-12T19:40:00Z",
            "region": "eu-north1",
        }
    )
    fake.pods.append(pod(x1, "Running", started="2026-10-12T19:40:00Z"))
    fake.pods[-1]["metadata"]["name"] = f"{x1}-fin"
    fake.jobs[(NS, x1)]["status"] = {"startTime": "2026-10-12T19:40:00Z"}  # started: cancelled in place, not deleted
    r = c.post(f"/v1/operations/{op}:cancel", headers=H)
    assert r.status_code == 200 and r.json()["id"] == x1 and r.json()["status"] == "CANCELLED"
    assert x1 in marks and op not in marks  # the finalizer is marked, the root is not
    assert fake.jobs[(NS, x1)]["metadata"]["annotations"].get(f"{LABEL}/cancelled")
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "FAILED" and "finalizing_by" not in got and got["finalize_attempts"] == 1
    )  # the root keeps its state and volume


def test_record_role_comes_from_the_job_not_the_pod(failed_run, monkeypatch):
    c, fake, op, rows, marks = failed_run
    x1 = c.post(f"/v1/operations/{op}:export", headers=H).json()["id"]
    seen = {}
    monkeypatch.setattr(attempts, "verify_runner_token", lambda token: ("eu-north1", NS, f"{x1}-fin"))
    monkeypatch.setattr(attempts, "record", lambda region, ns, pod_, body: seen.update(body) or {"ok": True})
    r = c.post(
        "/internal/attempts",
        json={"operation": x1, "status": "started", "gpus": 0, "role": "run"},
        headers={"Authorization": "Bearer t"},
    )
    assert r.status_code == 200 and seen["role"] == "finalize" and seen["root"] == op  # the pod's claim is overwritten
    monkeypatch.setattr(attempts, "verify_runner_token", lambda token: ("eu-north1", NS, f"{op}-abc"))
    c.post(
        "/internal/attempts",
        json={"operation": op, "status": "started", "gpus": 1, "role": "finalize"},
        headers={"Authorization": "Bearer t"},
    )
    assert seen["role"] == "run"


def test_a_stale_result_marker_is_never_this_attempts_success(failed_run):
    """Only the finalizer's own uploader record (main exit 0 -> `succeeded` with the result it wrote) closes the root; the
    compute attempt's old RESULT.json cannot: it is removed before main runs, and a finalizer record without a result
    (interrupted, failed, or started) never counts as delivery."""
    c, fake, op, rows, marks = failed_run
    x1 = c.post(f"/v1/operations/{op}:export", headers=H).json()["id"]
    rows[0]["result_ref"] = "app://md/result/stale"  # the compute attempt's old marker on record
    for st in ("started", "interrupted", "failed"):
        rows[1:] = [
            {
                "operation": x1,
                "pod": f"{x1}-fin",
                "root": op,
                "tenant": "demo",
                "status": st,
                "epoch": 2,
                "revision": 5,
                "gpus": 0,
                "role": "finalize",
                "started_at": "2026-10-12T19:40:00Z",
                "ended_at": None if st == "started" else "2026-10-12T19:41:00Z",
                "region": "eu-north1",
                "export_state": "n/a",
            }
        ]
        fake.jobs[(NS, x1)]["status"] = (
            {} if st == "started" else {"conditions": [{"type": "Failed", "status": "True"}]}
        )
        got = c.get(f"/v1/operations/{op}", headers=H).json()
        assert got["status"] in ("FINALIZING", "FAILED") and "exported_by" not in got, st
    rows[1:] = [
        {
            "operation": x1,
            "pod": f"{x1}-fin",
            "root": op,
            "tenant": "demo",
            "status": "succeeded",
            "epoch": 2,
            "revision": 6,
            "gpus": 0,
            "role": "finalize",
            "started_at": "2026-10-12T19:40:00Z",
            "ended_at": "2026-10-12T19:41:00Z",
            "region": "eu-north1",
            "export_state": "n/a",
            "result_ref": "app://md/result/new",
        }
    ]
    fake.jobs[(NS, x1)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["status"] == "SUCCEEDED" and got["exported_by"] == x1 and got["result_ref"] == "app://md/result/new"


def test_delivery_pending_fails_the_compute_job_at_once(failed_run):
    """A declared 'delivery pending' exit code is a FailJob rule on the compute Job: the GPU computation is not restarted by
    the Job's backoff for a delivery problem; retries are the finalizer's, bounded by :export. The ordinary policy
    (disruptions and SIGKILL/SIGTERM ignored) is unchanged, and a model without a finalizer keeps it as it was."""
    c, fake, op, rows, marks = failed_run
    rules = fake.jobs[(NS, op)]["spec"]["podFailurePolicy"]["rules"]
    assert rules[0] == {"action": "FailJob", "onExitCodes": {"containerName": "main", "operator": "In", "values": [75]}}
    assert [r_["action"] for r_ in rules[1:]] == ["Ignore", "Ignore"] and fake.jobs[(NS, op)]["spec"][
        "backoffLimit"
    ] >= 1
    x1 = c.post(f"/v1/operations/{op}:export", headers=H).json()["id"]
    assert fake.jobs[(NS, x1)]["spec"]["podFailurePolicy"]["rules"][0]["onExitCodes"]["values"] == [
        75
    ]  # the finalizer too: no restarts, :export bounds it
    plain = _model({k: v for k, v in SPEC.items() if k != "finalize"})
    import pytest as pt

    import catalog as cat

    with pt.MonkeyPatch.context() as mp:
        mp.setattr(cat, "get", lambda name: plain if name == "md" else None)
        r = c.post("/v1/models/md:invoke", json={"input": {}, "region": "eu-north1"}, headers=H)
    assert [r_["action"] for r_ in fake.jobs[(NS, r.json()["id"])]["spec"]["podFailurePolicy"]["rules"]] == [
        "Ignore",
        "Ignore",
    ]


def test_finalizer_of_a_fleet_run_is_pinned_to_the_admitting_cluster(client, monkeypatch):
    """A fleet run's retry on its kept volume cannot be dispatched elsewhere: the Job carries the admitting cluster's id as
    its region pin and as its only candidate, so the dispatcher reuses the volume in that cluster and never creates a
    fresh one in another cluster of the same GPU class."""
    c, fake = client
    m = _model({**SPEC, "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]}})
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {
                "south-rtx": {
                    "region": "south",
                    "pool": "rtx6000-spot-1x",
                    "gpu_class": "rtx-pro-6000",
                    "usd_per_gpu_hour": 1.0,
                },
                "north-l40s": {"region": "north", "pool": "l40s-spot-1x", "gpu_class": "l40s", "usd_per_gpu_hour": 0.8},
            },
            "clusters": {"north": {"region": "eu-north1"}, "south": {"region": "eu-south1"}},
        },
    )
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"north": "eu-north1", "south": "eu-south1"}.get(cid, cid))
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "md" else None)
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    m["regions"] = ["south", "north"]  # the new fleet names regions by cluster id
    r = c.post(
        "/v1/models/md:invoke",
        json={
            "input": {},
            "execution": {"io_mode": "application", "placement": {"allowed_clusters": ["south", "north"]}},
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[(NS, op)]
    assert f"{LABEL}/region" not in job["metadata"]["labels"]  # a fleet run: no pin at submission
    uid = job["metadata"].get("uid", "")
    monkeypatch.setattr(
        kube, "workload_clusters", lambda ns: {uid: "south"}
    )  # admitted by south, the volume lives there
    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    fake.pvcs[(NS, f"{op}-work")] = {"metadata": {"name": f"{op}-work"}}
    rows = [
        {
            "operation": op,
            "pod": f"{op}-abc",
            "root": op,
            "tenant": "demo",
            "status": "failed",
            "exit_code": 75,
            "epoch": 1,
            "revision": 3,
            "gpus": 1,
            "gpu_class": "rtx-pro-6000",
            "started_at": "2026-10-12T19:00:00Z",
            "ended_at": "2026-10-12T19:30:00Z",
            "region": "eu-south1",
            "cluster": "south",
            "role": "run",
        }
    ]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = fake.jobs[(NS, f"{op}-x1")]
    assert x1["metadata"]["labels"][f"{LABEL}/region"] == "south"  # pinned by cluster id
    assert x1["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/regions"] == "south"  # the only candidate
    assert (
        next(v for v in x1["spec"]["template"]["spec"]["volumes"] if v["name"] == "work")["persistentVolumeClaim"][
            "claimName"
        ]
        == f"{op}-work"
    )
    assert x1["spec"].get("managedBy") == "kueue.x-k8s.io/multikueue"  # still a manager Job
    assert "nvidia.com/gpu" not in json.dumps(
        next(c_ for c_ in x1["spec"]["template"]["spec"]["containers"] if c_["name"] == "main")["resources"]
    )


def test_discovery_lists_the_finalizer(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    f = c.get("/v1/fleet", headers=H).json()["execution"]["finalize"]
    assert (
        f["roles"] == ["run", "export", "finalize"]
        and f["max_retries"] == 3
        and f["trigger"] == "POST /v1/operations/{id}:export"
    )
