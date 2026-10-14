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


def test_a_failed_continuation_create_rolls_its_member_back(client, admin_keys, fake_keys, monkeypatch):
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
    assert "prepare" in kinds and kinds[-1] == "rollback"
    assert [m["operation"] for m in fake_keys.members] == [oid]  # the member is gone, the root stays
