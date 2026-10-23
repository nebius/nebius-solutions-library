"""Migration regressions: direct S2 access, not only the Scientific gateway."""

import copy

import pytest
from fastapi import HTTPException

import app as api
import attempts
import auth
import db
import jobs
import keys
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_database import database  # noqa: F401


def principal_info(owner, **limits):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **limits}


def auth_as(monkeypatch, values):
    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(attempts, "enabled", lambda: False)


def test_same_principal_multiple_keys_and_other_principal_direct_paths(client, admin_keys, fake_keys, monkeypatch):
    c, cluster = client
    auth_as(
        monkeypatch,
        {"sk-good": principal_info("p1"), "sk-same": principal_info("p1"), "sk-other": principal_info("p2")},
    )
    own, other = {"Authorization": "Bearer sk-same"}, {"Authorization": "Bearer sk-other"}
    body = {"mode": "run", "input": {"message": "synthetic"}}
    first = c.post("/v1/models/hello-run:invoke", headers={**H, "Idempotency-Key": "same-intent"}, json=body)
    assert first.status_code == 202, first.text
    oid = first.json()["id"]
    assert c.get(f"/v1/operations/{oid}", headers=own).status_code == 200
    # Another principal's equal idempotency string is independent, never a replay.
    second = c.post("/v1/models/hello-run:invoke", headers={**other, "Idempotency-Key": "same-intent"}, json=body)
    assert second.status_code == 202 and second.json()["id"] != oid
    assert [r["id"] for r in c.get("/v1/operations", headers=own).json()] == [oid]
    assert [r["id"] for r in c.get("/v1/operations", headers=other).json()] == [second.json()["id"]]
    for suffix in ("", "/result", "/logs", "/metrics"):
        assert c.get(f"/v1/operations/{oid}{suffix}", headers=other).status_code == 404
    for action in ("cancel", "resume", "export"):
        assert c.post(f"/v1/operations/{oid}:{action}", headers=other, json={}).status_code == 404
    job = cluster.jobs[("tenant-demo", oid)]
    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    resumed = c.post(f"/v1/operations/{oid}:resume", headers=own, json={})
    assert resumed.status_code == 202, resumed.text
    successor = resumed.json()["id"]
    assert c.get(f"/v1/operations/{successor}", headers=H).status_code == 200
    assert c.get(f"/v1/operations/{successor}", headers=other).status_code == 404
    # The durable owner is not tied to a live Job or to the submitting key.
    del cluster.jobs[("tenant-demo", oid)]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda _: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    rows = [
        {
            "operation": oid,
            "root": oid,
            "tenant": "demo",
            "pod": oid + "-a",
            "epoch": 1,
            "status": "succeeded",
            "export_state": "succeeded",
            "revision": 1,
            "region": "eu-north1",
        }
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda _: rows)
    assert c.get(f"/v1/operations/{oid}", headers=own).status_code == 200
    assert c.get(f"/v1/operations/{oid}", headers=other).status_code == 404


def test_missing_historical_provenance_is_not_tenant_ownership(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1")})
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(
        attempts, "for_operation", lambda _: [{"operation": "op-old", "root": "op-old", "tenant": "demo"}]
    )
    assert c.get("/v1/operations/op-old", headers=H).status_code == 404


def test_operation_model_grant_is_checked_on_reads(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    state = principal_info("p1")
    auth_as(monkeypatch, {"sk-good": state})
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}})
    oid = r.json()["id"]
    state["models"] = ["another-model"]
    assert c.get(f"/v1/operations/{oid}", headers=H).status_code == 404
    assert c.get("/v1/operations", headers=H).json() == []


def test_bounded_continuation_is_admitted_on_the_run_ledger(client, admin_keys, fake_keys, monkeypatch):
    """A bounded key's resume joins the run's reservation as a member after the current head (keys.prepare_run_member,
    docs/API.md "Keys"); it is not refused and not admitted as a second run."""
    c, cluster = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", max_concurrent=1)})
    created = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}})
    assert created.status_code == 202
    oid = created.json()["id"]
    assert [m["operation"] for m in fake_keys.members] == [oid]  # the root member, admitted with the run
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    result = c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    assert result.status_code == 202, result.text
    successor = result.json()["id"]
    prepared = [x for x in fake_keys.calls if x[0] == "prepare"]
    assert prepared == [
        ("prepare", oid, successor, oid, 0, None, False)
    ]  # root, member, predecessor = head, GPUs, bound, compute
    assert len([x for x in fake_keys.calls if x[0] == "admit"]) == 1  # no second run admission


@pytest.mark.parametrize(
    "execution", [{"placement": {"fallback_wait_s": 0}}, {"recovery": {"on_infrastructure_loss": "recover"}}]
)
def test_bounded_auto_continuation_is_accepted(client, admin_keys, fake_keys, monkeypatch, execution):
    """A bounded key may ask for automatic fallback or recovery: the successor is admitted on the run's ledger when the
    platform creates it (reconciliation.continue_run), so the submission is accepted."""
    import execution as ex

    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", budget_requests=10)})
    monkeypatch.setattr(ex, "tenant_config", lambda *a, **k: {"identities": {}, "execution_profiles": {}})
    result = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}, "execution": execution})
    assert result.status_code == 202, result.text
    assert [x[0] for x in fake_keys.calls] == ["admit"]


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_finite_gpu_endpoint_budget_is_explicitly_unsupported(client, admin_keys, fake_keys, monkeypatch, mode):
    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", budget_gpu_seconds=1)})
    r = c.post("/v1/models/llm-example:invoke", headers=H, json={"mode": mode, "input": {}})
    assert r.status_code == 501 and "GPU-second" in r.json()["detail"]
    assert not fake_keys.calls
    assert c.get("/internal/authorize/llm-example", headers=H).status_code == 501


def test_imported_usd_endpoint_budget_is_not_silently_unaccounted(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", max_budget=1)})
    r = c.post("/v1/models/llm-example:invoke", headers=H, json={"mode": "sync", "input": {}})
    assert r.status_code == 501 and "USD" in r.json()["detail"]
    assert not fake_keys.calls


def test_direct_gateway_cannot_bypass_concurrency_and_unconfigured_hmac(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", max_concurrent=1)})
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "")
    forged = api._admitted_token(keys.key_hash("sk-good"))
    assert c.get("/internal/authorize/llm-example", headers={**H, api.ADMITTED_HEADER: forged}).status_code == 501
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    fake_keys.admit(keys.key_hash("sk-good"), "sync-own", "demo")  # the API's own admission the token is bound to
    fake_keys.calls.clear()
    token = api._admitted_token(keys.key_hash("sk-good"), "llm-example", "sync-own")
    assert (
        c.get(
            "/internal/authorize/llm-example",
            headers={**H, api.ADMITTED_HEADER: token, api.OPERATION_HEADER: "sync-own"},
        ).status_code
        == 200
    )
    assert not fake_keys.calls


def test_sync_unlimited_gpu_key_holds_slot_and_releases_on_failure(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1", max_concurrent=1)})

    async def fail(*args):
        assert [x[0] for x in fake_keys.calls] == ["admit"]
        raise HTTPException(502, "synthetic upstream failure")

    monkeypatch.setattr(api, "_sync", fail)
    assert c.post("/v1/models/llm-example:invoke", headers=H, json={"mode": "sync"}).status_code == 502
    assert [x[0] for x in fake_keys.calls] == ["admit", "release"]
    assert fake_keys.calls[-1][-1] is None  # unknown device usage is not a measured zero


@pytest.mark.parametrize("exists", [False, True])
def test_create_failure_refunds_only_confirmed_missing_new_job(client, admin_keys, fake_keys, monkeypatch, exists):
    c, cluster = client
    monkeypatch.setattr(attempts, "enabled", lambda: False)

    def fail(ns, job, region, secret):
        if exists:
            cluster.create_namespaced_job(ns, job)
        raise HTTPException(502, "synthetic create response lost")

    monkeypatch.setattr(jobs, "create", fail)
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}})
    assert r.status_code == 502
    assert bool([x for x in fake_keys.calls if x[0] == "abandon"]) is (not exists)


def test_durable_owner_survives_key_revocation_and_refund_does_not_charge(database):
    db.migrate()
    value, _ = keys.create("demo", "owner-proof", budget=10)
    h = keys.key_hash(value)
    keys.own_operation("op-owned", "demo", "opaque-owner", "hello-run")
    keys.own_operation("op-owned", "demo", "opaque-owner", "hello-run")
    with pytest.raises(keys.KeyLimit, match="another owner"):
        keys.own_operation("op-owned", "demo", "other-owner", "hello-run")
    keys.admit(h, "op-owned", "demo")
    assert not keys.abandon("op-owned", "0" * 64)
    assert keys.abandon("op-owned", h)
    assert keys.info(h)["spent_requests"] == 0
    keys.revoke("demo", "owner-proof")
    assert keys.operation_owner("op-owned") == ("demo", "opaque-owner", "hello-run")


def test_admission_cannot_replay_another_keys_reservation(database):
    db.migrate()
    a, _ = keys.create("demo", "a")
    b, _ = keys.create("demo", "b")
    keys.admit(keys.key_hash(a), "op-one", "demo")
    with pytest.raises(keys.KeyLimit, match="another key"):
        keys.admit(keys.key_hash(b), "op-one", "demo")


def test_create_lock_reuses_pool_connection_for_admission_and_owner(database):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db.migrate()
    value, _ = keys.create("demo", "lock-proof")
    keys.own_operation("op-lock", "demo", "owner", "hello-run")
    barrier = threading.Barrier(8)

    def operation(index):
        name = "op-lock-" + str(index)
        with keys.operation_lock(name):
            barrier.wait(timeout=5)  # all eight test-pool connections are held
            keys.admit(keys.key_hash(value), name, "demo")
            assert keys.operation_owner("op-lock") == ("demo", "owner", "hello-run")
            return keys.abandon(name, keys.key_hash(value))

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(operation, range(8)))


def test_durable_owner_with_real_attempt_row_after_job_deletion(database, client, monkeypatch):
    db.migrate()
    c, cluster = client
    auth_as(monkeypatch, {"sk-good": principal_info("p1"), "sk-other": principal_info("p2")})
    row = {"operation": "op-durable", "status": "succeeded", "export": "succeeded", "gpus": 0}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    attempts.record("eu-north1", "tenant-demo", "op-durable-a", row)
    owner = auth.Principal("sk-good", "demo", principal_info("p1")).owner
    keys.own_operation("op-durable", "demo", owner, "hello-run")
    assert c.get("/v1/operations/op-durable", headers=H).status_code == 200
    assert c.get("/v1/operations/op-durable", headers={"Authorization": "Bearer sk-other"}).status_code == 404
