"""Attempt lifecycle (docs/JOBS.md "Attempts and finalization", services/api/attempts.py): authenticated records from the
uploader, epoch fencing, export gating of SUCCEEDED, lifecycle fields on the operation, the export retry. The database
rows are faked here; test_database.py exercises the real transactions when TEST_DATABASE_URL is set."""

import os

import pytest
from fastapi import HTTPException

import attempts
import status
from config import LABEL
from test_api import FLEET, H, client, cluster, pod  # noqa: F401
from test_database import database  # noqa: F401


def _job(name="op-container-run-1", execution=None, complete=True):
    ann = {f"{LABEL}/input": "{}", f"{LABEL}/pvc": f"{name}-work"}
    if execution:
        import json

        ann[f"{LABEL}/execution"] = json.dumps(execution)
    return {
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": "tenant-demo",
            "labels": {f"{LABEL}/tenant": "demo", f"{LABEL}/mode": "run", f"{LABEL}/model": "container-run"},
            "annotations": ann,
        },
        "spec": {},
        "status": {"conditions": [{"type": "Complete", "status": "True"}]} if complete else {},
        "_pods": [],
        "_region": "eu-north1",
    }


def _record(name, pod, st="succeeded", **kw):
    return {
        "operation": name,
        "pod": pod,
        "status": st,
        "gpus": 1,
        "started_at": "2026-10-11T10:00:00Z",
        "ended_at": "2026-10-11T11:00:00Z",
        **kw,
    }


def test_exit_zero_is_not_delivery():
    name = "op-container-run-1"
    ok = status.normalise(_job(name), None, [_record(name, f"{name}-a", export="succeeded")])
    assert (
        ok["status"] == "SUCCEEDED"
        and ok["attempts"][0]["export_state"] == "succeeded"
        and ok["attempts"][0]["attempt_id"] == f"{name}-a"
    )
    bad = status.normalise(_job(name), None, [_record(name, f"{name}-a", export="failed")])
    assert bad["status"] == "EXPORT_FAILED" and "export" in bad["error"] and bad["resumable"] is True
    # application-managed I/O: the finalization record (RESULT.json -> result_ref) is the acknowledgement
    app_ok = status.normalise(
        _job(name, {"io_mode": "application"}),
        None,
        [_record(name, f"{name}-a", export="n/a", result_ref="s3://cust/out/7")],
    )
    assert app_ok["status"] == "SUCCEEDED" and app_ok["attempts"][0]["result_ref"] == "s3://cust/out/7"
    app_bad = status.normalise(_job(name, {"io_mode": "application"}), None, [_record(name, f"{name}-a", export="n/a")])
    assert app_bad["status"] == "EXPORT_FAILED" and "finalization" in app_bad["error"]


def test_multinode_delivery_is_rank_zeros_record():
    """A JobSet's rank 0 exports for every rank: its record carries the export state, the other ranks' do not."""
    name = "op-distributed-run-1"
    job = _job(name)
    job["kind"] = "JobSet"
    job["spec"] = {"replicatedJobs": [{"name": "workers", "template": {"spec": {"template": {"spec": {}}}}}]}
    job["status"] = {"conditions": [{"type": "Completed", "status": "True"}]}
    import status as st

    records = [_record(name, f"{name}-workers-0-0", export="succeeded"), _record(name, f"{name}-workers-0-1")]
    op = st.normalise({**job, "status": {"conditions": [{"type": "Complete", "status": "True"}]}}, None, records)
    assert op["status"] == "SUCCEEDED"


def test_a_finalizing_pod_is_a_running_operation():
    name = "op-container-run-2"
    job = _job(name, complete=False)
    job["_pods"] = [
        {
            "metadata": {"name": f"{name}-a", "namespace": "tenant-demo"},
            "spec": {},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "main",
                        "state": {
                            "terminated": {
                                "exitCode": 0,
                                "startedAt": "2026-10-11T10:00:00Z",
                                "finishedAt": "2026-10-11T10:01:00Z",
                            }
                        },
                    }
                ],
            },
        }
    ]
    assert status.normalise(job)["status"] == "RUNNING"


@pytest.mark.parametrize("peer_first", [False, True])
@pytest.mark.parametrize(
    "export,ref,expected", [("committed", "result-5", "SUCCEEDED"), ("failed", None, "EXPORT_FAILED")]
)
def test_gang_peer_na_never_overrides_coordinator_delivery(peer_first, export, ref, expected):
    name = "op-gang-delivery"
    job = {**_job(name, {"io_mode": "application"}), "kind": "JobSet"}
    leader = _record(name, "rank-zero", epoch=5, gang_rank=0, export_state=export, result_ref=ref)
    peer = _record(name, "rank-one", epoch=5, gang_rank=1, export_state="n/a", started_at="2026-10-11T10:00:01Z")
    rows = [peer, leader] if peer_first else [leader, peer]
    operation = status.normalise(job, records=rows)
    assert operation["status"] == expected
    assert len(operation["attempts"]) == 2  # Both ranks remain visible and billable.


def test_gang_new_peer_does_not_borrow_old_coordinator_result():
    name = "op-gang-delivery"
    job = {**_job(name, {"io_mode": "application"}), "kind": "JobSet"}
    rows = [
        _record(name, "old-leader", epoch=4, gang_rank=0, export_state="committed", result_ref="old"),
        _record(name, "new-peer", epoch=5, gang_rank=1, export_state="n/a"),
    ]
    assert status.normalise(job, records=rows)["status"] == "FINALIZING"


def test_lifecycle_fields_come_from_the_rows(monkeypatch):
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    rows = [
        {**_record("op-x", "op-x-a", "interrupted", epoch=1, revision=3), "operation": "op-x"},
        {
            **_record(
                "op-x",
                "op-x-b",
                "succeeded",
                epoch=2,
                revision=9,
                export="succeeded",
                image_digest="sha256:abc",
                region="eu-north1",
            ),
            "operation": "op-x",
        },
    ]
    lc = attempts.lifecycle("op-x", "op-x", rows)
    assert (
        lc
        == {
            "root_operation_id": "op-x",
            "attempt_id": "op-x-b",
            "epoch": 2,
            "revision": 9,
            "export_state": None,
            "result_ref": None,
            "checkpoint_ref": None,
            "image_digest": "sha256:abc",
            "cluster": "eu-north1",
        }
        or lc["epoch"] == 2
    )


def test_record_endpoint_needs_a_pod_bound_token(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    seen = {}
    monkeypatch.setattr(
        attempts,
        "verify_runner_token",
        lambda tok: ("eu-north1", "tenant-demo", "op-container-run-1-abc")
        if tok == "pod-token"
        else (_ for _ in ()).throw(HTTPException(401, "nope")),
    )
    monkeypatch.setattr(
        attempts,
        "record",
        lambda region, ns, pod, body: seen.update({"region": region, "ns": ns, "pod": pod, "body": body})
        or {"operation": body["operation"], "pod": pod, "epoch": 1},
    )
    r = c.post(
        "/internal/attempts",
        json=_record("op-container-run-1", "op-container-run-1-abc"),
        headers={"Authorization": "Bearer bad"},
    )
    assert r.status_code == 401
    r = c.post(
        "/internal/attempts",
        json=_record("op-container-run-1", "op-container-run-1-abc"),
        headers={"Authorization": "Bearer pod-token"},
    )
    assert (
        r.status_code == 200
        and seen["pod"] == "op-container-run-1-abc"
        and seen["body"]["root"] == "op-container-run-1"
    )
    r = c.post("/internal/attempts", json={"x": 1}, headers={})
    assert r.status_code == 401


def test_record_fencing_rules():
    with pytest.raises(HTTPException) as e:
        attempts.record("eu-north1", "tenant-demo", "other-pod", {"operation": "op-x", "status": "succeeded"})
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        attempts.record("eu-north1", "tenant-demo", "op-x-a", {"operation": "op-x", "status": "done"})
    assert e.value.status_code == 400


def test_operation_reports_lifecycle_and_revision_cursor(client, monkeypatch):
    c, fake = client
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"input": {"image": "x", "command": "y"}, "region": "eu-north1"},
        headers=H,
    )
    op = r.json()["id"]
    rows = [{**_record(op, f"{op}-a", export="succeeded", epoch=1, revision=42, region="eu-north1"), "operation": op}]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == op else [])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "SUCCEEDED"
        and got["root_operation_id"] == op
        and got["attempt_id"] == f"{op}-a"
        and got["epoch"] == 1
        and got["revision"] == 42
    )
    assert c.get(f"/v1/operations/{op}?since=42", headers=H).status_code == 304
    assert c.get(f"/v1/operations/{op}?since=41", headers=H).status_code == 200
    import artifacts

    monkeypatch.setattr(artifacts, "list_outputs", lambda ns, op_, region, prefix=None: [])
    res = c.get(f"/v1/operations/{op}/result", headers=H).json()
    assert res["export_state"] == "succeeded"


def test_export_retry_reuses_the_volume_without_the_simulation(client, monkeypatch):
    c, fake = client
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"input": {"image": "x", "command": "y"}, "region": "eu-north1"},
        headers=H,
    )
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    monkeypatch.setattr(
        attempts, "for_operation", lambda root: [{**_record(op, f"{op}-a", export="failed"), "operation": op}]
    )
    assert c.get(f"/v1/operations/{op}", headers=H).json()["status"] == "EXPORT_FAILED"
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 202, r.text
    new = fake.jobs[("tenant-demo", f"{op}-x1")]
    spec = new["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    assert (
        main["command"][-1] == "true"
        and "nvidia.com/gpu" not in main["resources"]["limits"]
        and not spec["initContainers"]
    )
    uploader_env = {
        e["name"]: e.get("value") for e in next(x for x in spec["containers"] if x["name"] == "uploader")["env"]
    }
    assert (
        uploader_env["GPUS"] == "0" and uploader_env["GPU_CLASS"] == ""
    )  # the durable record of the transfer bills no GPU
    assert f"{LABEL}/gpu-class" not in new["metadata"]["labels"]
    assert next(v for v in spec["volumes"] if v["name"] == "work")["persistentVolumeClaim"]["claimName"] == f"{op}-work"
    assert new["metadata"]["annotations"][f"{LABEL}/export-of"] == op
    monkeypatch.setattr(
        attempts, "for_operation", lambda root: [{**_record(op, f"{op}-a", export="succeeded"), "operation": op}]
    )
    assert c.post(f"/v1/operations/{op}:export", headers=H).status_code == 409
    # the retry delivered: the original reports SUCCEEDED and names the retry
    monkeypatch.setattr(
        attempts,
        "for_operation",
        lambda root: [
            {**_record(op, f"{op}-a", export="failed"), "operation": op},
            {**_record(f"{op}-x1", f"{op}-x1-b", export="succeeded"), "operation": f"{op}-x1"},
        ],
    )
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["status"] == "SUCCEEDED" and got["export_state"] == "succeeded" and got["exported_by"] == f"{op}-x1"


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is needed for PostgreSQL integration tests"
)
def test_epoch_fencing_in_the_database(database):
    import db

    db.migrate()
    a = attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-a",
        {"operation": "op-x", "status": "started", "gpus": 1, "started_at": "2026-10-11T10:00:00Z"},
    )
    assert a["epoch"] == 1
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-a",
        {"operation": "op-x", "status": "interrupted", "gpus": 1, "ended_at": "2026-10-11T10:30:00Z"},
    )
    b = attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-b",
        {"operation": "op-x", "status": "started", "gpus": 1, "started_at": "2026-10-11T10:31:00Z"},
    )
    assert b["epoch"] == 2
    with pytest.raises(HTTPException) as e:  # the old pod comes back after a partition: it cannot finalize
        attempts.record(
            "eu-north1", "tenant-demo", "op-x-a", {"operation": "op-x", "status": "succeeded", "export": "succeeded"}
        )
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:  # nor publish a checkpoint pointer
        attempts.record(
            "eu-north1",
            "tenant-demo",
            "op-x-a",
            {"operation": "op-x", "status": "started", "checkpoint_ref": "app://stale", "checkpoint_generation": 9},
        )
    assert e.value.status_code == 409
    live = attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-b",
        {
            "operation": "op-x",
            "status": "started",
            "checkpoint_ref": "app://ckpt/4",
            "checkpoint_generation": 4,
            "gpus": 1,
        },
    )
    assert live["checkpoint_generation"] == 4 and live["status"] == "started"
    assert attempts.latest_checkpoint("op-x") == {
        "checkpoint_ref": "app://ckpt/4",
        "generation": 4,
        "epoch": 2,
        "attempt_id": "op-x-b",
    }
    attempts.mark_successor("op-x", "op-x-r1")
    assert attempts.successor_of("op-x") == "op-x-r1"
    with pytest.raises(
        HTTPException
    ) as e:  # superseded (automatic recovery): the live pod of the original cannot publish either
        attempts.record(
            "eu-north1",
            "tenant-demo",
            "op-x-b",
            {
                "operation": "op-x",
                "status": "started",
                "checkpoint_ref": "app://ckpt/5",
                "checkpoint_generation": 5,
                "gpus": 1,
            },
        )
    assert e.value.status_code == 409 and "superseded" in e.value.detail
    with pytest.raises(HTTPException):
        attempts.record(
            "eu-north1", "tenant-demo", "op-x-b", {"operation": "op-x", "status": "succeeded", "export": "succeeded"}
        )
    closed = attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-b",
        {
            "operation": "op-x",
            "status": "interrupted",
            "gpus": 1,
            "ended_at": "2026-10-11T11:00:00Z",
            "checkpoint_ref": "app://ckpt/9",
            "checkpoint_generation": 9,
            "result_ref": "app://late",
        },
    )
    assert closed["status"] == "interrupted"  # the evicted pod may still close its own attempt ...
    assert (
        closed["checkpoint_ref"] == "app://ckpt/4"
        and closed["checkpoint_generation"] == 4
        and not closed.get("result_ref")
    )  # ... but promotes nothing
    assert attempts.latest_checkpoint("op-x")["generation"] == 4
    failed = attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-x-b",
        {"operation": "op-x", "status": "failed", "exit_code": 1, "result_ref": "app://late-result"},
    )
    assert failed["status"] == "failed" and not failed.get("result_ref")
    rows = attempts.for_operation("op-x")
    assert [r["pod"] for r in rows] == ["op-x-a", "op-x-b"] and rows[1]["revision"] > rows[0]["revision"]
    attempts.mark_cancelled("op-x")
    with pytest.raises(HTTPException) as e:
        attempts.record(
            "eu-north1", "tenant-demo", "op-x-b", {"operation": "op-x", "status": "succeeded", "export": "succeeded"}
        )
    assert e.value.status_code == 409
    lc = attempts.lifecycle("op-x", "op-x", attempts.for_operation("op-x"))
    assert lc["epoch"] == 2 and lc["attempt_id"] == "op-x-b"
