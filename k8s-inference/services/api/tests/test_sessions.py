"""Live sessions at the API (sessions.py, docs/API.md "Sessions"): the routes are 404 while the fleet has the feature off;
creation admits on the key; the gateway hook binds a claim once from either transport, injects the worker identity and
refuses client-supplied identity headers; the worker routes verify the pod server-side (faked here) and only the
attached worker watches; the database transitions are in test_database.py."""

import copy

import pytest
from fastapi import HTTPException

import app as api
import auth
import config as cfg
import kube
import render_endpoint
import sessions
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401

P2 = {"Authorization": "Bearer sk-other"}


def _auth(monkeypatch, values):
    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)


def _principal(owner, **limits):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **limits}


class FakeSessions:
    """sessions.py in memory: the API's view (create/get/list/cancel/bind/attach/release), the rules in the database."""

    def __init__(self):
        self.rows, self.calls = {}, []

    def create(self, key_hash, tenant, owner, model, cluster_, region, timeout_s, gpus):
        sid = f"ses-{len(self.rows) + 1:016x}"
        self.calls.append(("create", key_hash, tenant, owner, model, cluster_, region, timeout_s, gpus))
        self.rows[sid] = {
            "id": sid,
            "tenant": tenant,
            "owner": owner,
            "model": model,
            "cluster": cluster_,
            "region": region,
            "state": "claimed",
            "connection": None,
            "pod_uid": None,
            "cancel_requested_at": None,
            "cancel_reason": None,
            "deadline_at": None,
            "token": f"tok-{sid}",
        }
        return {**self.public(self.rows[sid]), "claim_token": f"tok-{sid}"}

    def get(self, sid):
        return dict(self.rows[sid]) if sid in self.rows else None

    def list_owner(self, tenant, owner, limit=100):
        return [self.public(r) for r in self.rows.values() if r["tenant"] == tenant and r["owner"] == owner][:limit]

    def public(self, s):
        return {
            "id": s["id"],
            "model": s["model"],
            "region": s["region"],
            "status": s["state"],
            "cancel_requested": s["cancel_requested_at"] is not None,
            "attached": s["pod_uid"] is not None,
        }

    def cancel(self, sid, reason):
        self.rows[sid]["cancel_requested_at"] = "now"
        self.rows[sid]["cancel_reason"] = reason
        if self.rows[sid]["state"] == "claimed":
            self.rows[sid]["state"] = "ended"
        return dict(self.rows[sid])

    def bind(self, token, model):
        for r in self.rows.values():
            if r["token"] == token and r["state"] == "claimed" and r["model"] == model:
                r["state"], r["connection"] = "bound", "conn-1"
                return {"id": r["id"], "tenant": r["tenant"], "key_hash": "h", "connection": "conn-1"}
        raise HTTPException(401, "session claim not accepted")

    def attach(self, sid, connection, worker, worker_session_id):
        r = self.rows[sid]
        if r["state"] != "bound" or r["connection"] != connection:
            raise HTTPException(409, f"session is {r['state']}")
        r["state"], r["pod_uid"] = "attached", worker["uid"]
        return dict(r)

    def release(self, sid, worker, outcome, gpu_seconds):
        r = self.rows[sid]
        if r["state"] != "attached" or r["pod_uid"] != worker["uid"]:
            raise HTTPException(409, f"session is {r['state']}")
        r["state"], r["outcome"], r["gpu_seconds"] = "ended", outcome, gpu_seconds
        return dict(r)


@pytest.fixture
def live(client, admin_keys, fake_keys, monkeypatch):
    c, cl = client
    _auth(monkeypatch, {"sk-good": _principal("p1"), "sk-other": _principal("p2")})
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    fs = FakeSessions()
    for name in ("create", "get", "list_owner", "public", "cancel", "bind", "attach", "release"):
        monkeypatch.setattr(sessions, name, getattr(fs, name))
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    monkeypatch.setattr(api, "_endpoint_model", lambda eid: {"name": "llm-example", "namespace": "models"})
    return c, cl, fs


def test_sessions_are_off_unless_the_fleet_enables_them(client, admin_keys, fake_keys, monkeypatch):
    c, _ = client
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", False)
    assert c.post("/v1/models/llm-example:session", headers=H, json={}).status_code == 404
    assert c.get("/v1/sessions", headers=H).status_code == 404
    # the hook ignores a claim while the feature is off: the request is authorized as any other (a key is needed)
    r = c.post("/internal/authorize/llm-example/v1/audio", headers={"x-serverless2-session-claim": "tok"})
    assert r.status_code == 401


def test_create_admits_on_the_key_and_the_hook_binds_the_claim_once(live):
    c, _cl, fs = live
    r = c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600})
    assert r.status_code == 202, r.text
    s = r.json()
    assert s["status"] == "claimed" and s["claim_token"].startswith("tok-") and s["region"] == "eu-north1"
    assert fs.calls[0][2] == "demo" and fs.calls[0][4] == "llm-example" and fs.calls[0][6:] == ("eu-north1", 600, 1)
    assert c.post("/v1/models/hello-run:session", headers=H, json={}).status_code == 400  # a run class
    # the owner's view; another principal sees nothing
    assert c.get(f"/v1/sessions/{s['id']}", headers=H).json()["status"] == "claimed"
    assert c.get(f"/v1/sessions/{s['id']}", headers=P2).status_code == 404
    assert [x["id"] for x in c.get("/v1/sessions", headers=H).json()] == [s["id"]]
    # the hook: the claim binds once (WebSocket subprotocol), the worker identity is injected, no key needed
    hdr = {"sec-websocket-protocol": f"s2-session.{s['claim_token']}, ndjson"}
    r = c.post("/internal/authorize/llm-example/v1/audio", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.headers["x-serverless2-session"] == s["id"] and r.headers["x-serverless2-connection"] == "conn-1"
    assert r.headers["x-serverless2-tenant"] == "demo"
    assert c.post("/internal/authorize/llm-example/v1/audio", headers=hdr).status_code == 401  # bound already
    # the plain HTTP transport (NDJSON POST) binds the same way
    s2 = c.post("/v1/models/llm-example:session", headers=H, json={}).json()
    r = c.post("/internal/authorize/llm-example/v1/tts", headers={"x-serverless2-session-claim": s2["claim_token"]})
    assert r.status_code == 200 and r.headers["x-serverless2-session"] == s2["id"]
    # a claim for another model is refused
    s3 = c.post("/v1/models/llm-example:session", headers=H, json={}).json()
    assert (
        c.post(
            "/internal/authorize/http-example/v1/x", headers={"x-serverless2-session-claim": s3["claim_token"]}
        ).status_code
        == 401
    )
    # a client that supplies the injected headers itself is refused outright
    for h in ("x-serverless2-session", "x-serverless2-connection"):
        assert c.post("/internal/authorize/llm-example/v1/audio", headers={**H, h: "forged"}).status_code == 403
    # cancel: a request on the handle
    r = c.post(f"/v1/sessions/{s['id']}:cancel", headers=H)
    assert r.status_code == 200 and r.json()["cancel_requested"] is True
    assert c.post(f"/v1/sessions/{s['id']}:cancel", headers=P2).status_code == 404


def test_worker_routes_verify_the_pod_and_only_the_attached_worker_watches(live, monkeypatch):
    c, _cl, fs = live
    s = c.post("/v1/models/llm-example:session", headers=H, json={}).json()
    c.post("/internal/authorize/llm-example/v1/audio", headers={"x-serverless2-session-claim": s["claim_token"]})
    seen = []

    def verify(token, session, endpoint_model):
        seen.append(token)
        if token == "bad":
            raise HTTPException(401, "worker token not accepted")
        return {"pod": "p", "namespace": "models", "uid": "uid-" + token, "region": session["region"]}

    monkeypatch.setattr(sessions, "verify_worker", verify)
    W = {"Authorization": "Bearer tokA"}
    body = {"connection": "conn-1", "worker_session_id": "w-1"}
    assert (
        c.post(f"/internal/sessions/{s['id']}:attach", json=body, headers={"Authorization": "Bearer bad"}).status_code
        == 401
    )
    assert c.post(f"/internal/sessions/{s['id']}:attach", json=body).status_code == 401  # no token at all
    r = c.post(f"/internal/sessions/{s['id']}:attach", json={**body, "connection": "wrong"}, headers=W)
    assert r.status_code == 409
    r = c.post(f"/internal/sessions/{s['id']}:attach", json=body, headers=W)
    assert r.status_code == 200 and r.json()["status"] == "attached" and r.json()["connection"] == "conn-1"
    assert c.get(f"/internal/sessions/{s['id']}", headers=W).json()["cancel_requested"] is False
    assert (
        c.get(f"/internal/sessions/{s['id']}", headers={"Authorization": "Bearer tokB"}).status_code == 403
    )  # another pod
    c.post(f"/v1/sessions/{s['id']}:cancel", headers=H)
    assert c.get(f"/internal/sessions/{s['id']}", headers=W).json()["cancel_requested"] is True
    rel = {"connection": "conn-1", "outcome": "cancelled", "gpu_seconds": None}
    assert (
        c.post(f"/internal/sessions/{s['id']}:release", json=rel, headers={"Authorization": "Bearer tokB"}).status_code
        == 409
    )
    assert (
        c.post(f"/internal/sessions/{s['id']}:release", json={**rel, "connection": "other"}, headers=W).status_code
        == 409
    )
    r = c.post(f"/internal/sessions/{s['id']}:release", json=rel, headers=W)
    assert r.status_code == 200 and r.json()["status"] == "ended"
    assert (
        c.post(f"/internal/sessions/{s['id']}:release", json={**rel, "outcome": "nope"}, headers=W).status_code == 422
    )
    assert "forged" not in str(seen)


def test_security_policy_carries_the_session_headers():
    doc = render_endpoint._securitypolicy({"name": "stt", "namespace": "models", "modelId": "stt"})
    ext = doc["spec"]["extAuth"]
    assert {"x-serverless2-session-claim", "x-serverless2-session", "x-serverless2-connection"} <= set(
        ext["headersToExtAuth"]
    )
    assert {"x-serverless2-session", "x-serverless2-connection"} <= set(ext["http"]["headersToBackend"])
