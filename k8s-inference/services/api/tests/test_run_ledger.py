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


def test_a_definite_refusal_rolls_back_and_an_ambiguous_failure_never_does(client, admin_keys, fake_keys, monkeypatch):
    """A 4xx from the API server is definite: the member goes. A 5xx or a transport error is ambiguous (the server may
    still accept the timed-out create): the member is KEPT whatever an immediate read says; the error reaches the
    caller, who replays."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    monkeypatch.setattr(
        jobs,
        "create",
        lambda ns, job, region="eu-north1", secret=None: (_ for _ in ()).throw(
            api.HTTPException(502, "kubernetes (eu-north1): 503 Service Unavailable")
        ),
    )
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 502
    x1 = f"{oid}-x1"
    assert ("rollback", x1) not in fake_keys.calls and fake_keys.member_of(x1)["state"] == "active"  # kept
    assert ("tenant-demo", x1) not in cluster.jobs
    monkeypatch.setattr(
        jobs,
        "create",
        lambda ns, job, region="eu-north1", secret=None: (_ for _ in ()).throw(
            api.HTTPException(404, "tenant namespace is not onboarded")
        ),
    )
    fake_keys.members = [m for m in fake_keys.members if m["operation"] != x1]  # the kept member, retired elsewhere
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 404 and ("rollback", x1) in fake_keys.calls and fake_keys.member_of(x1) is None


def test_a_phantom_head_is_marked_then_retired_past_the_grace_and_a_head_that_ran_is_kept(
    client, admin_keys, fake_keys, monkeypatch
):
    """A lineage head whose Job exists nowhere, with no attempt record and an active member: the first sighting MARKS
    it and refuses (409, the mark named); only an absence persisting past RETIRE_GRACE_S after the mark rolls it back,
    and the lineage continues from the real head. A head that ran and expired (record) is kept."""
    from datetime import datetime, timedelta, timezone

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    phantom = f"{oid}-x1"
    fake_keys.members.append(
        {
            "operation": phantom,
            "root": oid,
            "predecessor": oid,
            "state": "active",
            "kind": "continuation",
            "stage": None,
            "note": None,
        }
    )
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 409 and "retired automatically" in r.text, r.text  # phase one: marked, nothing rolled back
    assert ("mark", phantom) in fake_keys.calls and ("rollback", phantom) not in fake_keys.calls
    assert fake_keys.member_of(phantom)["retire_marked_at"] is not None
    r = c.post(f"/v1/operations/{oid}:export", headers=H)  # within the grace: still refused, still not rolled back
    assert r.status_code == 409 and ("rollback", phantom) not in fake_keys.calls
    fake_keys.member_of(phantom)  # the grace elapsed (the mark is old): the absence persisted
    next(m for m in fake_keys.members if m["operation"] == phantom)["retire_marked_at"] = datetime.now(
        timezone.utc
    ) - timedelta(seconds=api.RETIRE_GRACE_S + 1)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202, r.text
    assert ("rollback", phantom) in fake_keys.calls  # phase two: retired, confirmed absent past the grace
    new = r.json()["id"]
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [(oid, new, oid)]  # after the real head
    assert fake_keys.member_of(new)["retire_marked_at"] is None and ("confirm", new) in fake_keys.calls
    # a head that ran and whose Job expired: on record, never marked, the stale source refused with the head named
    del cluster.jobs[("tenant-demo", new)]
    rows.append({**_record(new, f"{new}-b", export="failed"), "operation": new})
    marks = len([x for x in fake_keys.calls if x[0] == "mark"])
    rollbacks = len([x for x in fake_keys.calls if x[0] == "rollback"])
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 409 and "continue" in r.text
    assert len([x for x in fake_keys.calls if x[0] == "mark"]) == marks  # on record: never marked
    assert len([x for x in fake_keys.calls if x[0] == "rollback"]) == rollbacks  # never retired


def test_barrier_a_second_replica_cannot_retire_an_admission_whose_create_is_in_flight(
    client, admin_keys, fake_keys, monkeypatch
):
    """Root's race (COORDINATION 12:18Z): replica A is paused between its admission and its create; replica B asks for
    the same continuation and sees an active member with no Job and no record. B must not retire it: it marks and
    refuses; A's create then lands, A confirms its member (the mark goes) and returns 202; exactly one Job, one member,
    nothing rolled back, nothing unaccounted."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create
    seen: dict = {}

    def paused_then_create(ns, job, region="eu-north1", secret=None):
        # the barrier: A has admitted (its member is active) and has not created yet; B's request runs here
        name = job["metadata"]["name"]
        assert fake_keys.member_of(name)["state"] == "active" and ("tenant-demo", name) not in cluster.jobs
        monkeypatch.setattr(jobs, "create", real)  # B's own create (if it got that far) would be real
        b = c.post(f"/v1/operations/{oid}:export", headers=H)
        seen["b"] = (b.status_code, b.text)
        assert ("rollback", name) not in fake_keys.calls, "B retired A's in-flight admission"
        assert fake_keys.member_of(name)["retire_marked_at"] is not None  # B marked it, nothing more
        return real(ns, job, region, secret)  # A's create is released

    monkeypatch.setattr(jobs, "create", paused_then_create)
    a = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert a.status_code == 202, a.text
    x1 = a.json()["id"]
    assert seen["b"][0] == 409 and "retired automatically" in seen["b"][1], seen["b"]
    assert [k for k in cluster.jobs if k[1].startswith(f"{oid}-x")] == [("tenant-demo", x1)]  # one Job
    assert [m["operation"] for m in fake_keys.members if m["operation"].startswith(f"{oid}-x")] == [x1]  # one member
    assert fake_keys.member_of(x1)["retire_marked_at"] is None and ("confirm", x1) in fake_keys.calls
    assert not [x for x in fake_keys.calls if x[0] == "rollback"]
    # B, asking again now that A's Job exists: the mark would be cleared by the sighting, the lineage continued from x1
    cluster.jobs[("tenant-demo", x1)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    rows.append({**_record(x1, f"{x1}-b", export="failed"), "operation": x1})
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202 and [x[1:4] for x in fake_keys.calls if x[0] == "prepare"][-1] == (
        oid,
        r.json()["id"],
        x1,
    )


def test_a_create_that_lands_after_its_member_was_retired_is_taken_back(client, admin_keys, fake_keys, monkeypatch):
    """The other interleaving: A's create lands only after B's absence outlasted the grace and B rolled the member
    back. A's confirmation finds no member: the Job A created is removed and A answers 409 (replay); nothing runs
    unaccounted."""
    import jobs

    c, cluster = client
    oid, rows = _exportable(c, cluster, monkeypatch)
    real = jobs.create

    def create_then_retired(ns, job, region="eu-north1", secret=None):
        out = real(ns, job, region, secret)
        fake_keys.members = [m for m in fake_keys.members if m["operation"] != job["metadata"]["name"]]  # B's rollback
        return out

    monkeypatch.setattr(jobs, "create", create_then_retired)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 409 and "retired during its creation" in r.text, r.text
    x1 = f"{oid}-x1"
    assert ("tenant-demo", x1) not in cluster.jobs and x1 in cluster.deleted  # taken back
    assert fake_keys.member_of(x1) is None
