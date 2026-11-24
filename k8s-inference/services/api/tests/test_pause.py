"""The bounded, retryable submission pause (api 0.11.124, docs/API.md "Submission pause"): the fleet operator pauses
new admissions of some or all tenants; paused submissions answer 503 + Retry-After + X-Serverless2-Admission: refused
and create nothing; reads, cancels, exports (unless asked) and everything in flight are untouched; the pause lifts
itself at `until`; without the database nothing is paused."""

import copy

import auth
import db
import jobs
import pause
from test_api import KEY_INFO, H, client, cluster  # noqa: F401 - pytest fixture registration
from test_database import database  # noqa: F401

FLEET = {"Authorization": "Bearer sk-fleet"}
BODY = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}


def _rig(monkeypatch):
    import app as appmod

    fleet_info = {**KEY_INFO, "metadata": {**(KEY_INFO.get("metadata") or {}), "tenant": "platform", "role": "fleet"}}

    async def info(key):
        return copy.deepcopy(fleet_info if key == "sk-fleet" else KEY_INFO)

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(appmod, "_fleet_only", lambda: None)
    db.migrate()
    import keys
    from test_database import LEGACY

    # with the database on, admission needs the key on record: the test key, generously budgeted
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": keys.key_hash("sk-good"),
                "alias": "good",
                "principal_id": "p-good",
                "scopes": [],
                "budget_requests": 1000,
                "budget_gpu_seconds": 10**7,
                "max_concurrent": 100,
                "rate_limit": None,
            }
        ],
        "demo",
        "admin",
    )
    pause._cache.update({"until": 0.0, "value": None})


def test_pause_refuses_new_submissions_explicitly_and_leaves_everything_else(database, client, monkeypatch):
    c, fake = client
    _rig(monkeypatch)
    assert c.get("/v1/fleet/submissions", headers=FLEET).json() == {"paused": False}
    assert c.post("/v1/fleet/submissions:pause", json={"reason": "x"}, headers=H).status_code == 403  # tenant key
    before = c.post("/v1/models/batch-example:invoke", json=BODY, headers={**H, "Idempotency-Key": "pz-0"})
    assert before.status_code == 202
    r = c.post(
        "/v1/fleet/submissions:pause",
        json={"tenants": ["demo"], "reason": "native-id cutover", "retry_after_s": 45, "max_s": 600},
        headers=FLEET,
    )
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["paused"] and st["tenants"] == ["demo"] and st["retry_after_s"] == 45 and st["exports"] is False
    assert st["since"] <= st["until"] and st["by"]
    jobs_before = dict(fake.jobs)
    denied = c.post("/v1/models/batch-example:invoke", json=BODY, headers={**H, "Idempotency-Key": "pz-1"})
    assert denied.status_code == 503, denied.text
    assert denied.headers["Retry-After"] == "45" and denied.headers["X-Serverless2-Admission"] == "refused"
    assert denied.headers["X-Serverless2-Admission-Reason"] == "paused"
    assert denied.json()["detail"].startswith("ADMISSION_PAUSED: ") and "nothing was created" in denied.json()["detail"]
    assert fake.jobs == jobs_before  # nothing created
    assert c.post("/v1/models/batch-example:operation-id", headers={**H, "Idempotency-Key": "pz-1"}).status_code == 200
    op = before.json()["id"]
    assert c.get(f"/v1/operations/{op}", headers=H).status_code == 200  # reads untouched
    assert c.get("/v1/operations", headers=H).status_code == 200
    assert c.post(f"/v1/operations/{op}:cancel", headers=H).status_code == 200  # cancel untouched
    assert c.post(f"/v1/operations/{op}:resume", headers={**H, "Idempotency-Key": "pz-r"}).status_code == 503
    # :export of a finished run keeps working during the pause (an application draining its old work)
    ex = c.post(f"/v1/operations/{op}:export", headers=H)
    assert ex.status_code != 503, ex.text
    # another tenant is not paused by a pause that names `demo`
    assert pause.paused_for("acme") is None and pause.paused_for("demo")["tenants"] == ["demo"]
    assert c.get("/v1/fleet/submissions", headers=FLEET).json()["paused"] is True
    # exports: true refuses :export too; resume lifts everything
    r = c.post("/v1/fleet/submissions:pause", json={"reason": "drain done", "exports": True}, headers=FLEET)
    assert r.json()["tenants"] is None and r.json()["exports"] is True  # every tenant
    assert c.post(f"/v1/operations/{op}:export", headers=H).status_code == 503
    r = c.post("/v1/fleet/submissions:resume", headers=FLEET)
    assert r.status_code == 200 and r.json()["paused"] is False and r.json()["resumed_at"]
    assert c.get("/v1/fleet/submissions", headers=FLEET).json() == {"paused": False}
    assert (
        c.post("/v1/models/batch-example:invoke", json=BODY, headers={**H, "Idempotency-Key": "pz-2"}).status_code
        == 202
    )


def test_pause_is_bounded_and_needs_the_database(database, client, monkeypatch):
    c, _ = client
    _rig(monkeypatch)
    r = c.post("/v1/fleet/submissions:pause", json={"reason": "short", "max_s": 1}, headers=FLEET)
    assert r.status_code == 200
    value = pause._read()
    value["until"] = "2000-01-01T00:00:00Z"  # as if the bound had passed
    pause._write(value)
    st = c.get("/v1/fleet/submissions", headers=FLEET).json()
    assert st["paused"] is False and st["expired"]["reason"] == "short"
    assert (
        c.post("/v1/models/batch-example:invoke", json=BODY, headers={**H, "Idempotency-Key": "pz-3"}).status_code
        == 202
    )
    assert (
        c.post("/v1/fleet/submissions:pause", json={"reason": "x", "max_s": 7 * 3600}, headers=FLEET).status_code == 422
    )
    assert c.post("/v1/fleet/submissions:pause", json={"reason": "x", "bogus": 1}, headers=FLEET).status_code == 422
    pause.resume("t")
    monkeypatch.setattr(db, "enabled", lambda: False)
    pause._cache.update({"until": 0.0, "value": {"paused": True, "tenants": None}})
    assert pause.state() == {"paused": False}  # never paused without the database
    assert c.post("/v1/fleet/submissions:pause", json={"reason": "x"}, headers=FLEET).status_code == 503


def test_an_unreadable_setting_pauses_nobody(monkeypatch):
    monkeypatch.setattr(db, "enabled", lambda: True)
    pause._cache.update({"until": 0.0, "value": None})

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(pause, "_read", boom)
    assert pause.state() == {"paused": False} and pause.paused_for("demo") is None
    pause.check("demo")  # no exception
    monkeypatch.setattr(jobs, "OPS_LIST_CACHE_S", 0.0)
