"""Endpoint children (docs/JOBS.md "Endpoint children"): a live attempt of a run with `child_endpoints` asks for a
bounded async endpoint child of its root. Against PostgreSQL (migration 21, the key ledger) with the fake cluster:
the parent projection (no bearer anywhere), admission and deterministic identity, replay/readback, changed input,
overlap, the per-root bound, allowlist, stale pod, cancelled parent, closed root, the ledger accounting (one request,
GPU bound on the root), the child's operation-scoped credential accepted at the gateway hook and refused when forged
or dead, and the sweep (ended, cancel cascade)."""

import json

import pytest
from fastapi import HTTPException

import app as api
import catalog
import children
import db
import keys
import kube
import models as models_mod
from config import LABEL
from test_api import FLEET, H, client, cluster, pod  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

NS = "tenant-demo"
RUN_CLASS = {
    "id": "lerobot-worker",
    "kind": "job",
    "image": "example.org/lerobot:1",
    "command": "python worker.py",
    "gpu": {"count": 0},
    "cpu": "2",
    "memory": "4Gi",
    "child_endpoints": ["cosmos-nano"],
    "child_max_units": 3,
}
PLAIN_CLASS = {**RUN_CLASS, "id": "plain-worker"}
del PLAIN_CLASS["child_endpoints"]
del PLAIN_CLASS["child_max_units"]
ENDPOINT = {
    "id": "cosmos-nano",
    "kind": "endpoint",
    "image": "example.org/cosmos:1",
    "port": 8000,
    "gpu": {"count": 1, "classes": ["h100"]},
    "timeout_s": 900,
}


def _entry(spec):
    kube.fleet = lambda: FLEET
    return catalog.normalise(models_mod.to_entry(spec, managed_by="api"))


@pytest.fixture
def rig(database, client, monkeypatch):
    c, fake = client
    db.migrate()
    h = keys.key_hash("sk-good")
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": h,
                "alias": "demo-user",
                "max_concurrent": 4,
                "budget_requests": 10,
                "budget_gpu_seconds": None,  # a finite GPU-second budget refuses endpoint calls (501): see below
                "rate_limit": None,
                "scopes": [
                    "invoke:sync",
                    "invoke:async",
                    "run:submit",
                    "run:control",
                    "read:operations",
                    "read:results",
                ],
            }
        ],
        "demo",
        "admin",
    )
    entries = {e["name"]: e for e in (_entry(RUN_CLASS), _entry(PLAIN_CLASS), _entry(ENDPOINT))}
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    monkeypatch.setattr(catalog, "get", lambda name: entries.get(name))
    monkeypatch.setattr(
        catalog,
        "require",
        lambda name: entries[name] if name in entries else (_ for _ in ()).throw(HTTPException(404, name)),
    )
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    monkeypatch.setattr(api, "FLEET_MANAGER", False)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"hub": "eu-north1"}.get(cid, cid))
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    import attempts

    monkeypatch.setattr(attempts, "enabled", lambda: False)
    r = c.post("/v1/models/lerobot-worker:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    p = pod(op, "Running")
    p["metadata"]["uid"] = "uid-parent-1"
    fake.pods.append(p)
    identity = {"region": "eu-north1", "namespace": NS, "pod": p["metadata"]["name"], "uid": "uid-parent-1"}
    monkeypatch.setattr(children, "verify_parent_token", lambda token: dict(identity))
    monkeypatch.setattr(children, "_namespaces", lambda: [NS])
    return c, fake, op, identity


T = {"Authorization": "Bearer parent-pod-token"}


def _post(c, op, unit, inp=None, model="cosmos-nano", **extra):
    return c.post(
        f"/internal/operations/{op}/children",
        json={"unit_id": unit, "model": model, "input": inp or {"prompt": "x"}, **extra},
        headers=T,
    )


def test_parent_render_projects_its_own_identity_and_no_bearer(rig):
    c, fake, op, _ = rig
    job = fake.jobs[(NS, op)]
    spec = job["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["S2_OPERATION"] == op and env["S2_PARENT_TOKEN_FILE"] == "/var/run/secrets/serverless2/token"
    assert env["S2_CHILD_ENDPOINTS"] == "cosmos-nano" and env["S2_CHILD_MAX_UNITS"] == "3" and env["S2_API_URL"]
    vol = next(v for v in spec["volumes"] if v["name"] == "s2-parent")
    assert vol["projected"]["sources"][0]["serviceAccountToken"]["path"] == "token"
    assert {"name": "s2-parent", "mountPath": "/var/run/secrets/serverless2", "readOnly": True} in main["volumeMounts"]
    assert job["metadata"]["annotations"][f"{LABEL}/child-endpoints"] == "cosmos-nano"
    blob = json.dumps(job)
    assert "sk-good" not in blob and "Bearer" not in blob and (NS, f"{op}-auth") not in fake.secrets  # no key, nowhere
    # a class without child_endpoints renders as before
    r = c.post("/v1/models/plain-worker:invoke", json={"input": {}}, headers=H)
    plain = fake.jobs[(NS, r.json()["id"])]
    assert "s2-parent" not in [v["name"] for v in plain["spec"]["template"]["spec"]["volumes"]]
    assert f"{LABEL}/child-endpoints" not in plain["metadata"]["annotations"]


def test_child_admission_identity_replay_and_credential(rig):
    c, fake, op, identity = rig
    r = _post(c, op, "u1", {"prompt": "x", "n": 1})
    assert r.status_code == 201, r.text
    body = r.json()
    child = body["child"]
    assert (
        child == children.child_id(op, "u1") and body["state"] == "active" and body["status"] in ("QUEUED", "RUNNING")
    )
    assert body["request_sha256"] == children.request_hash({"prompt": "x", "n": 1}) and body["output_prefix"].endswith(
        f"/operations/{child}"
    )
    cjob = fake.jobs[(NS, child)]
    ann = cjob["metadata"]["annotations"]
    assert ann[f"{LABEL}/child-of"] == op and ann[f"{LABEL}/child-root"] == op and ann[f"{LABEL}/child-unit"] == "u1"
    assert ann[f"{LABEL}/key"] == keys.key_hash("sk-good")
    assert ann.get(f"{LABEL}/owner") == fake.jobs[(NS, op)]["metadata"]["annotations"].get(f"{LABEL}/owner")
    secret = fake.secrets[(NS, f"{child}-auth")]
    auth = secret["stringData"]["authorization"]
    assert auth.startswith(f"Bearer {api.ADMITTED_BEARER_PREFIX}") and secret["stringData"][
        "admitted"
    ] == api._admitted_token(keys.key_hash("sk-good"), "cosmos-nano", child)
    assert "sk-good" not in json.dumps(cjob) and "sk-good" not in json.dumps(secret)  # the key never travels
    # the ledger: a run member of the root, kind child, one request on the root's reservation, no GPU bound (an
    # endpoint call's GPU time is not metered per call; the key's policy above refuses finite GPU budgets)
    members = {m["operation"]: m for m in keys.run_members(op)}
    assert child in members, (members, keys.member_of(child), keys.reservation_key(op))
    assert members[child]["kind"] == "child" and members[child]["stage"] == "u1" and members[child]["gpu_bound"] == 0.0
    with db.pool().connection() as conn:
        res = conn.execute("select requests, gpu_seconds from key_reservations where operation = %s", (op,)).fetchone()
    assert res[0] == 2 and float(res[1]) == 0.0  # the root's request + one for the child
    # replay (same unit, same request): the existing child, nothing new
    r2 = _post(c, op, "u1", {"n": 1, "prompt": "x"})
    assert r2.status_code == 200 and r2.json()["child"] == child
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-c-")]) == 1
    # the same unit with a different request: refused
    r3 = _post(c, op, "u1", {"prompt": "y"})
    assert r3.status_code == 409 and "different request" in r3.text
    # readback
    g = c.get(f"/internal/operations/{op}/children/u1", headers=T)
    assert g.status_code == 200 and g.json()["child"] == child and g.json()["request_sha256"] == body["request_sha256"]
    assert c.get(f"/internal/operations/{op}/children/none", headers=T).status_code == 404
    # one active child per root
    r4 = _post(c, op, "u2")
    assert r4.status_code == 409 and "still active" in r4.text


def test_fences_allowlist_stale_pod_cancelled_and_bound(rig, monkeypatch):
    c, fake, op, identity = rig
    assert _post(c, op, "u1", model="lerobot-worker").status_code == 403  # not an allowlisted endpoint
    assert _post(c, op, "u1", model="unknown-model").status_code == 403
    assert _post(c, op, "bad unit!").status_code == 400
    assert (
        c.post(
            f"/internal/operations/{op}/children", json={"unit_id": "u1", "model": "cosmos-nano", "input": {}}
        ).status_code
        == 401
    )
    # a run class without child endpoints
    plain = c.post("/v1/models/plain-worker:invoke", json={"input": {}}, headers=H).json()["id"]
    pp = pod(plain, "Running")
    pp["metadata"]["uid"] = "uid-plain"
    fake.pods.append(pp)
    monkeypatch.setattr(
        children,
        "verify_parent_token",
        lambda token: {"region": "eu-north1", "namespace": NS, "pod": pp["metadata"]["name"], "uid": "uid-plain"},
    )
    assert _post(c, plain, "u1").status_code == 403
    # a stale attempt (another pod UID claims to be the live attempt)
    monkeypatch.setattr(children, "verify_parent_token", lambda token: {**identity, "uid": "uid-old"})
    r = _post(c, op, "u1")
    assert r.status_code == 409 and "not a live attempt" in r.text
    monkeypatch.setattr(children, "verify_parent_token", lambda token: dict(identity))
    # the bound: three units, the fourth refused; a child must end before the next starts
    for i in (1, 2, 3):
        r = _post(c, op, f"u{i}", {"i": i})
        assert r.status_code == 201, r.text
        cj = fake.jobs[(NS, r.json()["child"])]
        cj["status"] = {
            "conditions": [{"type": "Complete", "status": "True"}],
            "completionTime": "2026-10-14T10:00:00Z",
        }
        assert f"{r.json()['child']}:ended" in children.sweep()
    r = _post(c, op, "u4", {"i": 4})
    assert r.status_code == 409 and "3 children already" in r.text
    # a cancelled parent: nothing attaches
    fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/cancelled"] = "true"
    assert _post(c, op, "u9").status_code == 409
    # a closed root (its reservation settled): the ledger refuses
    fake.jobs[(NS, op)]["metadata"]["annotations"].pop(f"{LABEL}/cancelled")
    monkeypatch.setattr(
        children,
        "_live_parent",
        lambda identity, parent: (fake.jobs[(NS, op)], "eu-north1", "op-closed-root", catalog.get("lerobot-worker")),
    )
    with pytest.raises(HTTPException) as e:
        children.create(identity, op, {"unit_id": "z1", "model": "cosmos-nano", "input": {}})
    assert e.value.status_code == 409 and "closed" in e.value.detail


def test_credential_is_accepted_at_the_hook_only_while_the_child_lives(rig, monkeypatch):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    secret = fake.secrets[(NS, f"{child}-auth")]["stringData"]
    caller = pod(child, "Running")
    caller["metadata"]["labels"][f"{LABEL}/mode"] = "async"
    fake.pods.append(caller)
    api._admission_cache.clear()
    hdr = {
        "Authorization": secret["authorization"],
        "x-serverless2-admitted": secret["admitted"],
        "x-serverless2-operation": child,
        "x-serverless2-attempt": caller["metadata"]["name"],
    }
    monkeypatch.setattr(api, "_endpoint_model", lambda eid: catalog.get("cosmos-nano"))
    import leases

    prepared = []
    monkeypatch.setattr(
        leases,
        "prepare",
        lambda operation, attempt, region, model, caller_gone=None: prepared.append((operation, attempt)),
    )
    r = c.get("/internal/authorize/cosmos-nano/v1/health/ready", headers=hdr)
    assert r.status_code == 200 and r.headers["x-serverless2-operation"] == child, r.text
    r = c.post("/internal/authorize/cosmos-nano/generate", headers=hdr)
    assert (
        r.status_code == 200
        and r.headers["x-serverless2-attempt"] == caller["metadata"]["name"]
        and prepared == [(child, caller["metadata"]["name"])]
    )
    # the same credential for another model, or forged, or without the operation: refused
    assert c.post("/internal/authorize/lerobot-worker/generate", headers=hdr).status_code in (401, 403, 404)
    assert (
        c.post(
            "/internal/authorize/cosmos-nano/generate",
            headers={**hdr, "Authorization": f"Bearer {api.ADMITTED_BEARER_PREFIX}forged"},
        ).status_code
        == 401
    )
    assert (
        c.post("/internal/authorize/cosmos-nano/generate", headers={**hdr, "x-serverless2-operation": ""}).status_code
        == 401
    )
    # the root cancelled: the cascade ends the child and the credential dies with its reservation
    r = c.post(f"/v1/operations/{op}:cancel", headers=H)
    assert r.status_code == 200, r.text
    assert children.readback(NS, op, "u1")["state"] == "ended"
    api._admission_cache.clear()
    assert c.post("/internal/authorize/cosmos-nano/generate", headers=hdr).status_code != 200  # no live caller
    keys.release(child, 0.0)  # billing settles the cancelled child's member, then the root: no reservation left
    keys.release(op, 0.0)
    assert keys.reservation_key(op) is None and keys.member_of(child)["state"] == "settled"
    api._admission_cache.clear()
    assert c.post("/internal/authorize/cosmos-nano/generate", headers=hdr).status_code == 401


def test_sweep_cancels_children_of_a_cancelled_root_durably(rig):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    assert children.sweep() == []  # active, root alive: nothing
    fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/cancelled"] = "true"
    out = children.sweep()
    assert out == [f"{child}:cancelled"]
    assert (NS, child) not in fake.jobs or fake.jobs[(NS, child)]["metadata"]["annotations"].get(f"{LABEL}/cancelled")
    assert children.readback(NS, op, "u1")["state"] == "ended" and children.sweep() == []


def test_a_key_with_a_finite_gpu_budget_gets_the_endpoint_call_policy(rig):
    """The child is an endpoint call of the root's key: the same 501 as a direct `:invoke` of the endpoint (shared
    endpoint GPU time is not metered per call), decided before any row, member or Job exists."""
    c, fake, op, _ = rig
    with db.pool().connection() as conn:
        conn.execute("update keys set budget_gpu_seconds = 100000 where hash = %s", (keys.key_hash("sk-good"),))
    r = _post(c, op, "u1")
    assert r.status_code == 501 and "GPU-second" in r.text, r.text
    assert (
        children.readback(NS, op, "u1") is None
        and keys.run_members(op) == []
        or all(m["kind"] != "child" for m in keys.run_members(op))
    )
    assert (NS, children.child_id(op, "u1")) not in fake.jobs
