"""Idempotent session creation at the API (api 0.11.104; docs/API.md "Sessions"; the rules in test_sessions_db.py):
`Idempotency-Key` on `POST /v1/models/{model}:session`, owner-scoped, over the semantic request; the replay recovers a
lost answer with a usable claim, the hook binds exactly once, a bound session replays without a claim, another request
under the key is 409, a peer's same key is its own session and the owner's session is invisible to it. Against
PostgreSQL with the fake cluster; skipped without TEST_DATABASE_URL."""

import copy

import auth
import config as cfg
import db
import keys
import kube
from test_api import KEY_INFO, H, client, cluster  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

P2 = {"Authorization": "Bearer sk-other"}
SCOPES = ["invoke:sync", "run:submit", "read:operations"]


def _rig(database, client, monkeypatch):
    c, _ = client
    db.migrate()
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": keys.key_hash(k),
                "alias": alias,
                "principal_id": pid,
                "max_concurrent": 4,
                "budget_requests": 20,
                "rate_limit": None,
                "budget_gpu_seconds": 100000,
                "scopes": SCOPES,
            }
            for k, alias, pid in (("sk-good", "p1", "p-1"), ("sk-other", "p2", "p-2"))
        ],
        "demo",
        "admin",
    )
    infos = {
        f"sk-{k}": {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": pid}}}
        for k, pid in (("good", "p-1"), ("other", "p-2"))
    }
    c.infos = infos  # the principals as the auth layer sees them (a test may move their spend)

    async def info(key):
        if key not in infos:
            from fastapi import HTTPException

            raise HTTPException(401, "invalid API key")
        return copy.deepcopy(infos[key])

    monkeypatch.setattr(auth, "key_info", info)  # two principals of the tenant; the ledger rows are the records above
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    return c


def test_the_owner_recovers_a_lost_create_binds_once_and_a_peer_sees_nothing(database, client, monkeypatch):
    c = _rig(database, client, monkeypatch)
    hk = {**H, "Idempotency-Key": "speech-start-1"}
    r1 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r1.status_code == 202, r1.text
    s1 = r1.json()
    assert s1["replay"] is False and s1["claim_token"]
    # the first answer is discarded; the same request again (bytes may differ, the effective request is the same)
    r2 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600, "region": "eu-north1"})
    assert r2.status_code == 202, r2.text
    s2 = r2.json()
    assert s2["id"] == s1["id"] and s2["replay"] is True and s2["claim_token"] != s1["claim_token"]
    assert (s2["expires_at"], s2["deadline_at"]) == (s1["expires_at"], s1["deadline_at"])
    assert [x["id"] for x in c.get("/v1/sessions", headers=H).json()] == [s1["id"]]
    # another request under the same key: a conflict
    r3 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 300})
    assert r3.status_code == 409 and "Idempotency-Key" in r3.text
    # a peer with the same key: its own session (owner-scoped); the owner's session is 404 to it
    rp = c.post("/v1/models/llm-example:session", headers={**P2, "Idempotency-Key": "speech-start-1"}, json={})
    assert rp.status_code == 202, rp.text
    assert rp.json()["id"] != s1["id"] and rp.json()["replay"] is False
    assert c.get(f"/v1/sessions/{s1['id']}", headers=P2).status_code == 404
    assert c.post(f"/v1/sessions/{s1['id']}:cancel", headers=P2).status_code == 404
    # the hook binds the replayed claim once; the discarded first token binds no more
    old = {"x-serverless2-session-claim": s1["claim_token"]}
    new = {"x-serverless2-session-claim": s2["claim_token"]}
    assert c.post("/internal/authorize/llm-example/v1/audio", headers=old).status_code == 401
    rb = c.post("/internal/authorize/llm-example/v1/audio", headers=new)
    assert rb.status_code == 200, rb.text
    assert rb.headers["x-serverless2-session"] == s1["id"]
    assert c.post("/internal/authorize/llm-example/v1/audio", headers=new).status_code == 401  # bound already
    # bound: the replay is the same id without a claim (no second connection, no audio replayed), nothing extended
    r4 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r4.status_code == 202, r4.text
    s4 = r4.json()
    assert s4["id"] == s1["id"] and s4["status"] == "bound" and s4["replay"] is True and "claim_token" not in s4
    assert (s4["expires_at"], s4["deadline_at"]) == (s1["expires_at"], s1["deadline_at"])
    # without a key: a new session each time; a malformed key is refused before anything is admitted
    a = c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600}).json()
    b = c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600}).json()
    assert a["id"] != b["id"] and a["replay"] is False
    bad = c.post("/v1/models/llm-example:session", headers={**H, "Idempotency-Key": "x" * 201}, json={})
    assert bad.status_code == 400
    assert c.post("/v1/models/llm-example:session", headers={**H, "Idempotency-Key": "  "}, json={}).status_code == 400


def test_a_replay_takes_no_new_request_from_the_budget_but_keeps_scope_and_model_checks(database, client, monkeypatch):
    """A key with `budget_requests: 1`: the first create spends it (admission); the answer is lost; after the auth
    cache refreshed (spent 1 of 1) the same request under the same key must recover the session, not answer 402;
    a changed request under the key is 409, still not 402; a NEW session (another key, or none) is 402 as before."""
    c = _rig(database, client, monkeypatch)
    c.infos["sk-good"].update({"budget_requests": 1, "spent_requests": 0})
    hk = {**H, "Idempotency-Key": "speech-start-budget"}
    r1 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r1.status_code == 202, r1.text
    c.infos["sk-good"]["spent_requests"] = 1  # the request budget is spent by that admission (cache refreshed)
    r2 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r2.status_code == 202, r2.text
    assert r2.json()["id"] == r1.json()["id"] and r2.json()["replay"] is True and r2.json()["claim_token"]
    r3 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 300})
    assert r3.status_code == 409 and "Idempotency-Key" in r3.text
    # a new session is a new request: the spent budget refuses it, with or without a key
    assert (
        c.post("/v1/models/llm-example:session", headers={**H, "Idempotency-Key": "k-new"}, json={}).status_code == 402
    )
    assert c.post("/v1/models/llm-example:session", headers=H, json={}).status_code == 402
    # the replay still needs the scope and the model grant: a key without the scope, or on a model it is not granted
    c.infos["sk-good"]["scopes"] = ["read:operations"]
    r = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r.status_code == 403, r.text
    c.infos["sk-good"]["scopes"] = KEY_INFO.get("scopes") or []
    c.infos["sk-good"]["models"] = ["http-example"]
    r = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r.status_code in (403, 404), r.text
