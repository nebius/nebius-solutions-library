"""Imported keys and per-key limits (keys.py migration 8, docs/API.md "Importing keys"): the pure validation and
classification, the scope checks on every direct path, the one-logical-call rule between the API and the gateway
hook, and the reservation plumbing. The database rules themselves are in test_database.py."""

import hashlib
import hmac

import app as appmod
import auth
import keys
from test_api import (  # noqa: F401 - fixtures and constants
    ADMIN,
    ADMIN_INFO,
    KEY_INFO,
    H,
    admin_keys,
    client,
    cluster,
    fake_keys,
)

HASH = "a" * 64
REC = {
    "hash": HASH,
    "alias": "legacy-1",
    "tenant": "demo",
    "principal_id": "p-1",
    "models": ["llm-example"],
    "scopes": ["invoke:sync", "read:catalog"],
    "budget_requests": 50,
    "rate_limit": {"requests": 50, "window_s": 86400},
}


def test_validate_records_normalises_and_refuses():
    rows, errors = keys.validate_records([REC], "demo")
    assert (
        not errors and rows[0]["alias"] == "demo-legacy-1" and rows[0]["role"] == "user" and rows[0]["blocked"] is False
    )
    assert rows[0]["rate_limit"] == {"requests": 50, "window_s": 86400}  # the legacy units, never converted
    assert rows[0]["budget"] is None and rows[0]["budget_gpu_seconds"] is None and rows[0]["budget_requests"] == 50.0
    assert rows[0]["metadata"]["legacy"] == {"principal_id": "p-1", "tenant_id": None}
    bad = [
        {**REC, "hash": "xyz"},
        {**REC, "tenant": "other"},
        {**REC, "role": "admin"},
        {**REC, "principal_id": ""},
        {**REC, "models": "all"},
        {**REC, "rate_limit": {"requests": 50}},
        {**REC, "expires": "yesterday"},
        {**REC, "budget_gpu_seconds": -1},
    ]
    rows, errors = keys.validate_records(bad, "demo")
    assert not rows and len(errors) == 8 and all(e["errors"] for e in errors)
    rows, errors = keys.validate_records([REC, {**REC, "alias": "legacy-2"}], "demo")  # duplicate hash in one batch
    assert any("duplicate hash" in x for e in errors for x in e["errors"])
    # an unknown scope never widens: the key imports blocked, the scope recorded
    rows, errors = keys.validate_records([{**REC, "scopes": ["invoke:sync", "mcp.invoke"]}], "demo")
    assert not errors and rows[0]["blocked"] is True and rows[0]["unknown_scopes"] == ["mcp.invoke"]
    assert rows[0]["metadata"]["legacy"]["unenforced_scopes"] == ["mcp.invoke"]
    # grants for models not deployed yet are kept as they are
    rows, _ = keys.validate_records([{**REC, "models": ["future-model"]}], "demo")
    assert rows[0]["models"] == ["future-model"]


def test_equivalent_is_byte_for_byte_on_the_imported_fields():
    row = keys.validate_records([REC], "demo")[0][0]
    assert keys.equivalent(dict(row), row)
    assert not keys.equivalent({**row, "budget_requests": 51}, row)
    assert not keys.equivalent({**row, "scopes": ["invoke:sync"]}, row)
    assert not keys.equivalent(
        {**row, "metadata": {**row["metadata"], "legacy": {"principal_id": "p-2", "tenant_id": None}}}, row
    )


def test_scopes_bound_every_direct_path(client, fake_keys, monkeypatch):
    c, _ = client
    scoped = {**KEY_INFO, "scopes": ["read:catalog"]}

    async def fake_info(key):
        return dict(scoped)

    monkeypatch.setattr(auth, "key_info", fake_info)
    assert c.get("/v1/models", headers=H).status_code == 200  # in scope
    assert c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}}).status_code == 403
    assert (
        "run:submit"
        in c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}}).json()["detail"]
    )
    assert c.get("/v1/operations", headers=H).status_code == 403
    assert c.post("/v1/operations/op-x:cancel", headers=H).status_code == 403
    assert c.post("/v1/artifacts/uploads", headers=H, json={"filename": "a"}).status_code == 403
    assert c.get("/internal/authorize/llm-example", headers=H).status_code == 403  # invoke:sync
    me = c.get("/v1/keys/me", headers=H).json()
    assert me["scopes"] == ["read:catalog"] and me["principal_id"] is None and me["budget_requests"] is None
    legacy = {
        **KEY_INFO,
        "scopes": [],
        "metadata": {**KEY_INFO["metadata"], "legacy": {"principal_id": "p-9", "tenant_id": "t-1"}},
        "budget_requests": 10,
        "spent_requests": 10,
    }

    async def fake_legacy(key):
        return dict(legacy)

    monkeypatch.setattr(auth, "key_info", fake_legacy)
    me = c.get("/v1/keys/me", headers=H).json()
    assert me["principal_id"] == "p-9" and me["legacy"]["tenant_id"] == "t-1" and me["status"] == "exhausted"
    assert (
        c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}}).status_code == 402
    )  # request budget spent


def test_run_admission_reserves_and_the_gateway_counts_one_logical_call(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    budgeted = {**KEY_INFO, "budget_gpu_seconds": 7200.0}

    async def fake_info(key):
        return dict(budgeted)

    monkeypatch.setattr(auth, "key_info", fake_info)
    monkeypatch.setattr(appmod.cfg, "ROUTER_KEY", "sk-router")
    # a key with a GPU-second budget must bound a GPU run; a CPU class reserves nothing
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}})
    assert r.status_code in (200, 202), r.text
    assert [x for x in fake_keys.calls if x[0] == "admit"][-1][2] == 0.0
    body = {"mode": "run", "input": {"image": "example/image", "command": "true", "gpus": 2}}
    r = c.post("/v1/models/container-run:invoke", headers=H, json=body)
    assert r.status_code == 400 and "timeout_s" in r.json()["detail"]
    r = c.post("/v1/models/container-run:invoke", headers=H, json={**body, "timeout_s": 600})
    assert r.status_code in (200, 202), r.text
    admits = [x for x in fake_keys.calls if x[0] == "admit"]
    assert admits[-1][2] == 600.0 * 2 and admits[-1][3] is True  # GPUs x timeout
    assert (
        appmod._job_gpus(
            {
                "spec": {
                    "parallelism": 2,
                    "template": {"spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "4"}}}]}},
                }
            }
        )
        == 8
    )
    # the gateway hook counts a direct call once, and not at all when the API's proxied call carries the token
    budgeted["budget_gpu_seconds"] = None  # shared endpoint GPU time is not per-request measured
    fake_keys.calls.clear()
    assert c.get("/internal/authorize/llm-example", headers=H).status_code == 200
    assert [x[0] for x in fake_keys.calls] == ["admit", "release"] and fake_keys.calls[0][1].startswith("gw-")
    fake_keys.calls.clear()
    # the API's own admitted call: the token binds the key, the model and the admission the API holds (a `sync-…`
    # reservation on the key); the hook finds the reservation and counts nothing
    fake_keys.admit(keys.key_hash("sk-good"), "sync-test", "demo")
    fake_keys.calls.clear()
    token = hmac.new(
        b"sk-router", f"{keys.key_hash('sk-good')}:llm-example:sync-test".encode(), hashlib.sha256
    ).hexdigest()
    admitted = {**H, appmod.ADMITTED_HEADER: token, appmod.OPERATION_HEADER: "sync-test"}
    assert c.get("/internal/authorize/llm-example", headers=admitted).status_code == 200
    assert fake_keys.calls == []
    legacy = hmac.new(b"sk-router", keys.key_hash("sk-good").encode(), hashlib.sha256).hexdigest()
    assert c.get("/internal/authorize/llm-example", headers={**H, appmod.ADMITTED_HEADER: legacy}).status_code == 200
    assert [x[0] for x in fake_keys.calls] == ["admit", "release"]  # the key-only token of old releases: a direct call
    fake_keys.calls.clear()
    assert c.get("/internal/authorize/llm-example", headers={**H, appmod.ADMITTED_HEADER: "forged"}).status_code == 200
    assert [x[0] for x in fake_keys.calls] == ["admit", "release"]  # a wrong token counts


def test_import_route_is_admin_and_tenant_scoped(client, fake_keys, monkeypatch):
    c, _ = client
    assert c.post("/v1/keys:import", headers=H, json={"keys": [REC]}).status_code == 403  # a user key

    async def fake_admin(key):
        return dict(ADMIN_INFO)

    monkeypatch.setattr(auth, "key_info", fake_admin)
    r = c.post("/v1/keys:import", headers=H, json={"keys": [REC], "dry_run": True})
    assert (
        r.status_code == 200
        and r.json()["dry_run"] is True
        and ("import", "demo", ADMIN_INFO["key_alias"], True, 1) in fake_keys.calls
    )


def test_stale_reservations_are_released_when_the_job_is_gone(client, fake_keys, monkeypatch):
    import db

    monkeypatch.setattr(db, "enabled", lambda: True)
    fake_keys.stale = [("op-gone", "demo"), ("op-live", "demo")]
    from fastapi import HTTPException

    import jobs

    def find(ns, name):
        if name == "op-gone":
            raise HTTPException(404, "gone")
        return {"metadata": {"name": name}}, "eu-north1"

    monkeypatch.setattr(jobs, "find", find)
    assert appmod.release_stale_reservations() == ["op-gone"]
    assert ("release", "op-gone", None) in fake_keys.calls and not any(
        x[1] == "op-live" for x in fake_keys.calls if x[0] == "release"
    )
