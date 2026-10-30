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


def test_claim_is_a_compare_and_swap_and_in_flight_ownerless_bindings_continue(
    client, admin_keys, fake_keys, monkeypatch
):
    """Two operations claiming one binding at once: one wins, the other sees 409. A binding written before owners
    existed and bound to a running operation continues under that operation's owner (its continuations deliver), while
    another principal still cannot touch it."""
    c, cluster = client
    _two(monkeypatch)
    monkeypatch.setattr(ex, "tenant_config", lambda *a, **k: {"identities": {}, "execution_profiles": {}})
    # an ownerless binding bound to p1's running operation (the state of the day before the fence)
    r = c.put("/v1/bindings/legacy", json={"data": {"input.json": "private"}}, headers=P1)
    body = {"mode": "run", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "legacy"}}
    oid = c.post("/v1/models/hello-run:invoke", json=body, headers=P1).json()["id"]
    sec = cluster.secrets[("tenant-demo", "binding-legacy")]
    del sec["metadata"]["annotations"][ex.OWNER_ANNOTATION]
    assert sec["metadata"]["annotations"]["serverless2.nebius/operation"] == oid
    # p2 cannot see, replace or revoke it; p1 can (the durable owner of the bound operation is p1)
    assert (
        c.get("/v1/bindings", headers=P2).json() == []
        and c.put("/v1/bindings/legacy", json={"data": {"input.json": "x"}}, headers=P2).status_code == 404
    )
    assert [b["ref"] for b in c.get("/v1/bindings", headers=P1).json()] == ["legacy"]
    # p1's continuation delivers the binding again and the owner is adopted on the way
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    r = c.post(f"/v1/operations/{oid}:resume", headers=P1, json={})
    assert r.status_code == 202, r.text
    assert cluster.secrets[("tenant-demo", f"{r.json()['id']}-private")]["data"] == {"input.json": "private"}
    adopted = cluster.secrets[("tenant-demo", "binding-legacy")]["metadata"][
        "annotations"
    ]  # the claim replaced the Secret
    assert adopted[ex.OWNER_ANNOTATION] == c.get("/v1/bindings", headers=P1).json()[0]["owner"]
    # the claim races: a concurrent writer changed the Secret between read and replace
    c.put("/v1/bindings/raced", json={"data": {"input.json": "private"}}, headers=P1)
    raced = cluster.secrets[("tenant-demo", "binding-raced")]
    raced["metadata"]["resourceVersion"] = "7"
    real_replace = cluster.replace_namespaced_secret

    def conflict_once(name, ns, body):
        if name == "binding-raced" and not getattr(conflict_once, "done", False):
            conflict_once.done = True
            raced["metadata"]["annotations"]["serverless2.nebius/operation"] = "op-other"
            from kubernetes.client.rest import ApiException

            raise ApiException(status=409, reason="Conflict")
        return real_replace(name, ns, body)

    monkeypatch.setattr(cluster, "replace_namespaced_secret", conflict_once)
    r = c.post(
        "/v1/models/hello-run:invoke",
        json={**body, "execution": {**body["execution"], "private_input_ref": "raced"}},
        headers={**P1, "Idempotency-Key": "race"},
    )
    assert r.status_code == 409 and "claimed concurrently" in r.json()["detail"]


def test_copies_follow_their_binding_identity_through_delete_and_recreate(client, admin_keys, fake_keys, monkeypatch):
    """A binding deleted and re-created under the same ref (by another principal) is a different binding: the old one's
    late refresh or revocation never reaches the new one's copies, a revocation of a binding that changed meanwhile is
    refused, and copies carry the identity they came from."""
    c, cluster = client
    _two(monkeypatch)
    monkeypatch.setattr(ex, "tenant_config", lambda *a, **k: {"identities": {}, "execution_profiles": {}})
    c.put("/v1/bindings/shared", json={"data": {"input.json": "p1"}}, headers=P1)
    body = {"mode": "run", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "shared"}}
    op1 = c.post("/v1/models/hello-run:invoke", json=body, headers=P1).json()["id"]
    old = dict(cluster.secrets[("tenant-demo", "binding-shared")])
    old_uid = old["metadata"]["uid"]
    assert (
        cluster.secrets[("tenant-demo", f"{op1}-private")]["metadata"]["annotations"][ex.BINDING_UID_ANNOTATION]
        == old_uid
    )
    assert c.delete("/v1/bindings/shared", headers=P1).status_code == 204
    assert ("tenant-demo", f"{op1}-private") not in cluster.secrets
    # p2 re-creates the ref and consumes it
    c.put("/v1/bindings/shared", json={"data": {"input.json": "p2"}}, headers=P2)
    op2 = c.post("/v1/models/hello-run:invoke", json=body, headers={**P2, "Idempotency-Key": "p2"}).json()["id"]
    copy2 = cluster.secrets[("tenant-demo", f"{op2}-private")]
    new_uid = cluster.secrets[("tenant-demo", "binding-shared")]["metadata"]["uid"]
    assert copy2["metadata"]["annotations"][ex.BINDING_UID_ANNOTATION] == new_uid != old_uid
    # a late refresh and a late revocation from p1's old binding: p2's copy is untouched
    ex._refresh_copies("tenant-demo", "shared", {"input.json": "late"}, old)
    assert cluster.secrets[("tenant-demo", f"{op2}-private")]["data"] == {"input.json": "p2"}
    ex._refresh_copies("tenant-demo", "shared", {"revoked": ""}, old, delete=True)
    assert cluster.secrets[("tenant-demo", f"{op2}-private")]["data"] == {"input.json": "p2"}
    assert c.delete("/v1/bindings/shared", headers=P1).status_code == 404  # p2's now
    # p2's own refresh reaches its copy, keeps the copy's identity and bumps the revision
    rv = copy2["metadata"]["resourceVersion"]
    assert c.put("/v1/bindings/shared", json={"data": {"input.json": "p2-rotated"}}, headers=P2).status_code == 200
    copy2 = cluster.secrets[("tenant-demo", f"{op2}-private")]
    assert copy2["data"] == {"input.json": "p2-rotated"} and copy2["metadata"]["resourceVersion"] != rv
    assert (
        copy2["metadata"]["annotations"][ex.BINDING_UID_ANNOTATION] == new_uid
        and copy2["metadata"]["labels"]["serverless2.nebius/operation"] == op2
    )
    # a revocation of a binding that changed between the read and the delete is refused (uid/resourceVersion preconditions)
    real_delete = cluster.delete_namespaced_secret

    def bump_then_delete(name, ns, body=None):
        if name == "binding-shared":
            cluster.secrets[(ns, name)]["metadata"]["resourceVersion"] = "999"
        return real_delete(name, ns, body)

    monkeypatch.setattr(cluster, "delete_namespaced_secret", bump_then_delete)
    r = c.delete("/v1/bindings/shared", headers=P2)
    assert r.status_code == 409 and ("tenant-demo", "binding-shared") in cluster.secrets
