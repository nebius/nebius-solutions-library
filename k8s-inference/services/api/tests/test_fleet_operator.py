"""The fleet operator surface (api 0.11.110; docs/API.md "Fleet operators"): a key with `metadata.role=fleet` (minted
only by the fleet's own bootstrap) lists the fleet's tenants (the fleet document's and those with keys on record, with
counts, spend and what their keys hold), lists, creates, edits, blocks, revokes and imports keys of an explicit tenant
through the tenant's own functions, and inspects a tenant's usage. Ordinary tenant admins stay tenant-scoped (403 on the
fleet surface); the fleet operator is not a tenant admin (403 on the tenant surface) and runs nothing. Against
PostgreSQL with the fake cluster; skipped without TEST_DATABASE_URL."""

import copy

import auth
import db
import keys
import kube
from test_api import ADMIN_INFO, KEY_INFO, H, client, cluster  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

FLEET_KEY = {"Authorization": "Bearer sk-fleet"}
ADMIN_KEY = {"Authorization": "Bearer sk-admin"}
FLEET_INFO = {
    **KEY_INFO,
    "key_alias": "platform-operator",
    "models": [],
    "scopes": ["read:catalog"],
    "max_budget": None,
    "metadata": {"tenant": "platform", "role": "fleet"},
}


def _rig(database, client, monkeypatch):
    c, fake = client
    db.migrate()
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": "a" * 64,
                "alias": "lab-1",
                "principal_id": "p-1",
                "max_concurrent": 2,
                "budget_requests": 5,
                "budget_gpu_seconds": 7200,
                "rate_limit": None,
            },
            {**LEGACY, "hash": "b" * 64, "alias": "lab-2", "principal_id": "p-2", "rate_limit": None},
        ],
        "demo",
        "admin",
    )
    keys.import_records(
        [{**LEGACY, "tenant": "acme", "hash": "c" * 64, "alias": "qa-1", "principal_id": "p-9", "rate_limit": None}],
        "acme",
        "admin",
    )
    keys.block("acme", "acme-qa-1")
    keys.admit("a" * 64, "op-live-1", "demo", gpu_seconds=600.0, run_member=True)
    infos = {"sk-fleet": FLEET_INFO, "sk-admin": ADMIN_INFO, "sk-good": KEY_INFO}

    async def info(key):
        if key not in infos:
            from fastapi import HTTPException

            raise HTTPException(401, "invalid API key")
        return copy.deepcopy(infos[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    fleet = dict(kube.fleet())
    fleet["tenants"] = {
        "demo": {"gpu_quota": 16, "fair_share_weight": 2, "dedicated_pools": [], "assets": ["weights-x"]},
        "quiet": {"gpu_quota": None, "fair_share_weight": 1, "dedicated_pools": ["north-h100"], "assets": []},
    }
    monkeypatch.setattr(kube, "fleet", lambda: fleet)
    return c, fake


def test_a_fleet_operator_lists_tenants_with_their_keys_and_usage(database, client, monkeypatch):
    c, _ = _rig(database, client, monkeypatch)
    r = c.get("/v1/fleet/tenants", headers=FLEET_KEY)
    assert r.status_code == 200, r.text
    by = {t["tenant"]: t for t in r.json()}
    # the fleet document's tenants and every tenant with keys on record; the platform's own keys are not a tenant
    assert set(by) == {"demo", "quiet", "acme"} and "platform" not in by
    demo = by["demo"]
    assert demo["in_fleet_document"] is True and demo["gpu_quota"] == 16 and demo["assets"] == ["weights-x"]
    assert demo["namespace"] == "tenant-demo" and demo["keys"] == 2 and demo["admins"] == 0 and demo["blocked"] == 0
    assert demo["usage"] == {"active_operations": 1, "reserved_gpu_seconds": 600.0, "unknown_gpu_seconds": 0.0}
    acme = by["acme"]
    assert acme["in_fleet_document"] is False and acme["gpu_quota"] is None and acme["assets"] is None
    assert acme["keys"] == 1 and acme["blocked"] == 1 and acme["usage"]["active_operations"] == 0
    quiet = by["quiet"]
    assert quiet["in_fleet_document"] is True and quiet["keys"] == 0 and quiet["dedicated_pools"] == ["north-h100"]
    one = c.get("/v1/fleet/tenants/demo", headers=FLEET_KEY)
    assert one.status_code == 200 and one.json() == demo
    assert c.get("/v1/fleet/tenants/nobody", headers=FLEET_KEY).status_code == 404
    assert c.get("/v1/fleet/tenants/platform", headers=FLEET_KEY).status_code == 404
    assert c.get("/v1/fleet/tenants/Bad_Name", headers=FLEET_KEY).status_code == 404
    # never a hash or a value anywhere in the listing
    text = r.text
    assert "a" * 64 not in text and "c" * 64 not in text and "sk-" not in text


def test_a_fleet_operator_manages_a_tenants_keys_through_the_tenants_own_functions(database, client, monkeypatch):
    c, _ = _rig(database, client, monkeypatch)
    listed = c.get("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY)
    assert listed.status_code == 200, listed.text
    assert sorted(k["alias"] for k in listed.json()) == ["demo-lab-1", "demo-lab-2"]
    lab1 = next(k for k in listed.json() if k["alias"] == "demo-lab-1")
    assert lab1["tenant"] == "demo" and lab1["usage"]["active_operations"] == 1 and lab1["max_concurrent"] == 2
    assert "a" * 64 not in listed.text and "hash" not in lab1 and "metadata" not in lab1
    # create: a user key of that tenant, no pass-through routes, the value once; the audit names the fleet operator
    made = c.post(
        "/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "ci", "budget": 5, "models": ["llm-example"]}
    )
    assert made.status_code == 201, made.text
    assert made.json()["alias"] == "demo-ci" and made.json()["key"].startswith("sk-") and made.json()["role"] == "user"
    row = next(k for k in keys.list_tenant("demo") if k["key_alias"] == "demo-ci")
    assert row["created_by"] == "fleet:platform-operator" and row["metadata"]["allowed_passthrough_routes"] == []
    # the grant and the budget: edit; block / unblock; revoke
    edited = c.patch(
        "/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"models": ["http-example"], "budget": 50}
    )
    assert edited.status_code == 200 and edited.json()["models"] == ["http-example"] and edited.json()["budget"] == 50
    assert c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={}).status_code == 400
    blocked = c.post("/v1/fleet/tenants/demo/keys/demo-ci:block", headers=FLEET_KEY)
    assert blocked.status_code == 200 and blocked.json()["status"] == "blocked" and blocked.json()["blocked"] is True
    unblocked = c.post("/v1/fleet/tenants/demo/keys/demo-ci:block?blocked=false", headers=FLEET_KEY)
    assert unblocked.status_code == 200 and unblocked.json()["status"] == "active"
    assert c.delete("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY).status_code == 204
    assert c.delete("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY).status_code == 404
    assert (
        c.patch("/v1/fleet/tenants/acme/keys/demo-lab-1", headers=FLEET_KEY, json={"budget": 1}).status_code == 404
    )  # wrong tenant
    # import into another tenant, with that tenant's own rules (unknown scopes block, as always)
    rep = c.post(
        "/v1/fleet/tenants/acme/keys:import",
        headers=FLEET_KEY,
        json={
            "keys": [
                {
                    **LEGACY,
                    "tenant": "acme",
                    "hash": "d" * 64,
                    "alias": "qa-2",
                    "principal_id": "p-8",
                    "rate_limit": None,
                    "scopes": ["telepathy:read"],
                }
            ]
        },
    )
    assert rep.status_code == 200, rep.text
    assert [x["alias"] for x in rep.json()["blocked_for_unknown_scope"]] == ["acme-qa-2"]
    assert c.get("/v1/fleet/tenants/acme", headers=FLEET_KEY).json()["keys"] == 2
    # the tenant admin's own listing is unchanged by all of this (tenant-scoped, the same projection)
    assert sorted(k["alias"] for k in c.get("/v1/keys", headers=ADMIN_KEY).json()) == ["demo-lab-1", "demo-lab-2"]


def test_the_fleet_surface_is_the_fleet_operators_only_and_the_fleet_operator_is_no_tenant_admin(
    database, client, monkeypatch
):
    c, _ = _rig(database, client, monkeypatch)
    # a tenant admin: 403 on every fleet route (no broadening of tenant-admin authority)
    for method, path, body in (
        ("get", "/v1/fleet/tenants", None),
        ("get", "/v1/fleet/tenants/demo", None),
        ("get", "/v1/fleet/tenants/demo/keys", None),
        ("post", "/v1/fleet/tenants/demo/keys", {"alias": "x"}),
        ("patch", "/v1/fleet/tenants/demo/keys/demo-lab-1", {"budget": 1}),
        ("post", "/v1/fleet/tenants/demo/keys/demo-lab-1:block", None),
        ("delete", "/v1/fleet/tenants/demo/keys/demo-lab-1", None),
        ("post", "/v1/fleet/tenants/demo/keys:import", {"keys": []}),
    ):
        for headers in (ADMIN_KEY, H):
            r = getattr(c, method)(path, headers=headers, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, path, headers, r.text)
    # the fleet operator: no tenant-admin route, no model work (its key carries read:catalog only, tenant platform)
    assert c.get("/v1/keys", headers=FLEET_KEY).status_code == 403
    assert c.post("/v1/keys", headers=FLEET_KEY, json={"alias": "x"}).status_code == 403
    assert c.post("/v1/models/llm-example:invoke", headers=FLEET_KEY, json={"input": {}}).status_code == 403
    assert (
        c.post("/v1/models/hello-run:invoke", headers=FLEET_KEY, json={"mode": "run", "input": {}}).status_code == 403
    )
    assert c.get("/v1/operations", headers=FLEET_KEY).status_code == 403
    # its own view says what it is
    me = c.get("/v1/keys/me", headers=FLEET_KEY).json()
    assert me["role"] == "fleet" and me["tenant"] == "platform"
    # the ledger never minted it as a tenant key: only the bootstrap path accepts the role
    rows, errors = keys.validate_records(
        [{**LEGACY, "hash": "e" * 64, "alias": "x", "principal_id": "p", "role": "fleet"}], "demo"
    )
    assert not rows and errors and "role must be user" in str(errors)
