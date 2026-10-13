"""Bindings belong to the principal that created them (docs/API.md "Bindings"): within one tenant another principal
cannot see, replace, revoke or consume them, and a replacement is a compare-and-swap. Two principals of tenant demo
share the namespace and the bucket; the fence is the owner, not the tenant."""

import copy
import json

import attempts
import auth
import execution as ex
from test_api import ADMIN, KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401

P1, P2 = {"Authorization": "Bearer sk-p1"}, {"Authorization": "Bearer sk-p2"}


def _principal(owner, **extra):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **extra}


def _two(monkeypatch):
    values = {
        "sk-p1": _principal("p1"),
        "sk-p2": _principal("p2"),
        "sk-admin": {**KEY_INFO, "metadata": {"tenant": "demo", "role": "admin"}},
    }

    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(attempts, "enabled", lambda: False)


def test_another_principal_cannot_see_replace_revoke_or_consume_a_binding(client, monkeypatch):
    c, cluster = client
    _two(monkeypatch)
    r = c.put("/v1/bindings/mine", json={"data": {"response_url": "https://objects.example/p1"}}, headers=P1)
    assert r.status_code == 200 and r.json()["owner"] and r.json()["ref"] == "mine"
    owner = r.json()["owner"]
    assert cluster.secrets[("tenant-demo", "binding-mine")]["metadata"]["annotations"][ex.OWNER_ANNOTATION] == owner
    # p2: not listed, not replaceable, not revocable, not consumable; p2's own ref of the same name is p1's, not p2's
    assert [b["ref"] for b in c.get("/v1/bindings", headers=P2).json()] == []
    assert [b["ref"] for b in c.get("/v1/bindings", headers=P1).json()] == ["mine"]
    r = c.put("/v1/bindings/mine", json={"data": {"response_url": "https://attacker.example/"}}, headers=P2)
    assert r.status_code == 404
    assert cluster.secrets[("tenant-demo", "binding-mine")]["data"]["response_url"] == "https://objects.example/p1"
    assert c.delete("/v1/bindings/mine", headers=P2).status_code == 404
    assert ("tenant-demo", "binding-mine") in cluster.secrets
    body = {"mode": "run", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "mine"}}
    monkeypatch.setattr(ex, "tenant_config", lambda *a, **k: {"identities": {}, "execution_profiles": {}})
    r = c.post("/v1/models/hello-run:invoke", json=body, headers=P2)
    assert r.status_code == 404 and "no such binding" in r.json()["detail"]
    # the owner consumes it; the copy of a running attempt follows the owner's replacement, never p2's
    r = c.post("/v1/models/hello-run:invoke", json=body, headers=P1)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    assert cluster.secrets[("tenant-demo", f"{oid}-private")]["data"]["response_url"] == "https://objects.example/p1"
    assert (
        c.put("/v1/bindings/mine", json={"data": {"response_url": "https://attacker.example/"}}, headers=P2).status_code
        == 404
    )
    assert (
        c.put(
            "/v1/bindings/mine", json={"data": {"response_url": "https://objects.example/p1-rotated"}}, headers=P1
        ).status_code
        == 200
    )
    assert (
        cluster.secrets[("tenant-demo", f"{oid}-private")]["data"]["response_url"]
        == "https://objects.example/p1-rotated"
    )
    assert c.delete("/v1/bindings/mine", headers=P2).status_code == 404
    # an admin sees every binding with its owner and may revoke one
    listed = c.get("/v1/bindings", headers=ADMIN).json()
    assert [(b["ref"], b["owner"]) for b in listed] == [("mine", owner)]
    assert c.delete("/v1/bindings/mine", headers=ADMIN).status_code == 204


def test_replacement_is_a_compare_and_swap_and_owners_never_change(client, monkeypatch):
    c, cluster = client
    _two(monkeypatch)
    r = c.put("/v1/bindings/cas", json={"data": {"k": "1"}}, headers=P1)
    owner = r.json()["owner"]
    cluster.secrets[("tenant-demo", "binding-cas")]["metadata"]["resourceVersion"] = "41"
    r = c.put("/v1/bindings/cas", json={"data": {"k": "2"}}, headers={**P1, "If-Match": "40"})
    assert r.status_code == 409 and cluster.secrets[("tenant-demo", "binding-cas")]["data"]["k"] == "1"
    r = c.put("/v1/bindings/cas", json={"data": {"k": "2"}}, headers={**P1, "If-Match": "41"})
    assert r.status_code == 200 and cluster.secrets[("tenant-demo", "binding-cas")]["data"]["k"] == "2"
    assert cluster.secrets[("tenant-demo", "binding-cas")]["metadata"]["annotations"][ex.OWNER_ANNOTATION] == owner
    # an admin's replacement keeps the owner; a binding without an owner is nobody's to an ordinary caller
    assert c.put("/v1/bindings/cas", json={"data": {"k": "3"}}, headers=ADMIN).status_code == 200
    assert cluster.secrets[("tenant-demo", "binding-cas")]["metadata"]["annotations"][ex.OWNER_ANNOTATION] == owner
    del cluster.secrets[("tenant-demo", "binding-cas")]["metadata"]["annotations"][ex.OWNER_ANNOTATION]
    assert c.put("/v1/bindings/cas", json={"data": {"k": "4"}}, headers=P1).status_code == 404
    assert c.get("/v1/bindings", headers=P1).json() == []
    body = {"mode": "run", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "cas"}}
    monkeypatch.setattr(ex, "tenant_config", lambda *a, **k: {"identities": {}, "execution_profiles": {}})
    assert c.post("/v1/models/hello-run:invoke", json=body, headers=P1).status_code == 404
    assert json.loads(json.dumps(c.get("/v1/bindings", headers=ADMIN).json()))[0]["owner"] is None
