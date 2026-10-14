"""Live sessions against PostgreSQL (docs/API.md "Sessions"): the rules that live in SQL and were covered only through
an in-memory fake until api 0.11.82: the compare-and-swap bind across replicas, attach fencing, release idempotency,
cancel of a never-bound claim, settlement exactly once on the key's ledger, and the sweep's transitions (expired claim,
unattached binding, deadline cancel request, lost worker, deferred settlement). Isolated schema per test; skipped
without TEST_DATABASE_URL."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import config as cfg
import db
import keys
import sessions
from test_database import LEGACY, database  # noqa: F401

KEY = "c" * 64
W1 = {"pod": "speech-predictor-00001-abc", "namespace": "models", "uid": "uid-w1", "region": "eu-north1"}
W2 = {"pod": "speech-predictor-00001-def", "namespace": "models", "uid": "uid-w2", "region": "eu-north1"}


@pytest.fixture
def live(database, monkeypatch):
    db.migrate()
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": KEY,
                "alias": "speech-qa",
                "max_concurrent": 20,
                "budget_requests": 50,
                "rate_limit": None,
                "budget_gpu_seconds": 100000,
            }
        ],
        "demo",
        "admin",
    )
    return database


def _create(timeout_s=600, gpus=1):
    return sessions.create(KEY, "demo", "p-1", "speech", "hub", "eu-north1", timeout_s, gpus)


def _reservation(sid):
    with db.pool().connection() as c:
        return c.execute("select gpu_seconds, requests from key_reservations where operation = %s", (sid,)).fetchone()


def _sql(q, *args):
    with db.pool().connection() as c, c.transaction():
        c.execute(q, args)


def test_create_admits_on_the_ledger_and_the_claim_binds_exactly_once_across_replicas(live):
    s = _create(timeout_s=600, gpus=2)
    assert s["status"] == "claimed" and s["claim_token"] and _reservation(s["id"])[0] == 1200.0  # 2 GPUs x 600 s
    with db.pool().connection() as c:
        stored = c.execute("select claim_hash from sessions where id = %s", (s["id"],)).fetchone()[0]
    assert stored != s["claim_token"] and "claim_hash" not in sessions.get(s["id"])  # the hash, never the token
    token = s["claim_token"]
    with ThreadPoolExecutor(max_workers=4) as ex:  # four replicas present the same claim at once
        results = list(ex.map(lambda _: _try(sessions.bind, token, "speech"), range(4)))
    ok = [r for r in results if isinstance(r, dict)]
    assert len(ok) == 1 and all(getattr(r, "status_code", None) == 401 for r in results if r is not ok[0])
    assert ok[0]["connection"] and sessions.get(s["id"])["state"] == "bound"
    # a claim for another model, an expired claim, a cancelled claim: 401
    s2 = _create()
    assert _try(sessions.bind, s2["claim_token"], "other-model").status_code == 401
    _sql("update sessions set expires_at = now() - interval '1 second' where id = %s", s2["id"])
    assert _try(sessions.bind, s2["claim_token"], "speech").status_code == 401
    s3 = _create()
    sessions.cancel(s3["id"], "owner")
    assert _try(sessions.bind, s3["claim_token"], "speech").status_code == 401
    assert sessions.get(s3["id"])["state"] == "ended" and sessions.get(s3["id"])["usage"] == "none"
    assert _reservation(s3["id"]) is None  # cancelled before any connection: settled at zero, the slot is back


def _try(fn, *a):
    try:
        return fn(*a)
    except HTTPException as e:
        return e


def test_attach_is_fenced_to_the_bound_connection_and_one_worker(live):
    s = _create()
    b = sessions.bind(s["claim_token"], "speech")
    assert _try(sessions.attach, s["id"], "another-connection", W1, "ws-1").status_code == 409
    a = sessions.attach(s["id"], b["connection"], W1, "ws-1")
    assert a["state"] == "attached" and a["pod_uid"] == "uid-w1" and a["worker_session_id"] == "ws-1"
    assert sessions.attach(s["id"], b["connection"], W1, "ws-1")["state"] == "attached"  # the same worker's retry
    assert _try(sessions.attach, s["id"], b["connection"], W2, "ws-2").status_code == 409  # a second worker
    assert _try(sessions.attach, "ses-missing", b["connection"], W1, None).status_code == 404
    assert sessions.open_on("speech") == 1 and sessions.open_on("other") == 0
    # a bound session past its deadline cannot be attached (fenced for good)
    s4 = _create()
    b4 = sessions.bind(s4["claim_token"], "speech")
    _sql("update sessions set deadline_at = now() - interval '1 second' where id = %s", s4["id"])
    assert _try(sessions.attach, s4["id"], b4["connection"], W1, None).status_code == 409


def test_release_is_the_attached_workers_only_and_settles_once(live):
    s = _create(timeout_s=600, gpus=1)
    b = sessions.bind(s["claim_token"], "speech")
    sessions.attach(s["id"], b["connection"], W1, "ws-1")
    assert _try(sessions.release, s["id"], W2, "done", 10.0).status_code == 409  # another pod
    assert _try(sessions.release, s["id"], W1, "finished", 10.0).status_code == 400  # not an outcome
    r = sessions.release(s["id"], W1, "done", 42.5)
    assert r["state"] == "ended" and r["usage"] == "measured" and r["gpu_seconds"] == 42.5 and r["settled_at"]
    assert _reservation(s["id"]) is None  # the reservation became spend
    assert keys.info(KEY)["spent_gpu_seconds"] == 42.5
    again = sessions.release(s["id"], W1, "done", 42.5)  # the same pod's retry: settled once
    assert again["settled_at"] == r["settled_at"] and keys.info(KEY)["spent_gpu_seconds"] == 42.5
    # unmetered: usage unknown, the bound stays against the budget (never a measured zero)
    s2 = _create(timeout_s=600, gpus=1)
    b2 = sessions.bind(s2["claim_token"], "speech")
    sessions.attach(s2["id"], b2["connection"], W2, None)
    r2 = sessions.release(s2["id"], W2, "disconnected", None)
    assert r2["usage"] == "unknown" and r2["gpu_seconds"] is None and r2["settled_at"]
    assert keys.info(KEY)["spent_gpu_seconds"] == 42.5 and keys.run_usage(KEY)["unknown_gpu_seconds"] == 600.0


def test_sweep_expires_unattached_deadlines_lost_workers_and_deferred_settlement(live, monkeypatch):
    monkeypatch.setattr(sessions, "_worker_gone", lambda s: s["pod_uid"] == "uid-gone")
    expired = _create()
    _sql("update sessions set expires_at = now() - interval '1 second' where id = %s", expired["id"])
    unattached = _create()
    sessions.bind(unattached["claim_token"], "speech")
    # a Ready replica was observed and the attach wait has passed (api 0.11.83: the wait runs from readiness)
    _sql(
        "update sessions set ready_seen_at = now() - interval '2 hours', attach_deadline_at = now() - interval '1 hour' where id = %s",
        unattached["id"],
    )
    late = _create(timeout_s=60)
    bl = sessions.bind(late["claim_token"], "speech")
    sessions.attach(late["id"], bl["connection"], W1, None)
    _sql("update sessions set deadline_at = now() - interval '1 second' where id = %s", late["id"])
    lost = _create()
    bg = sessions.bind(lost["claim_token"], "speech")
    sessions.attach(lost["id"], bg["connection"], {**W2, "uid": "uid-gone"}, None)
    fine = _create()
    bf = sessions.bind(fine["claim_token"], "speech")
    sessions.attach(fine["id"], bf["connection"], W2, None)
    out = sessions.sweep()
    assert f"{expired['id']}:expired" in out and f"{unattached['id']}:unattached" in out
    assert f"{late['id']}:cancel-requested" in out and f"{lost['id']}:worker-lost" in out
    assert not [x for x in out if x.startswith(fine["id"])]
    assert sessions.get(expired["id"])["usage"] == "none" and _reservation(expired["id"]) is None
    assert sessions.get(unattached["id"])["usage"] == "unknown" and sessions.get(unattached["id"])["settled_at"]
    assert sessions.get(late["id"])["state"] == "attached" and sessions.get(late["id"])["cancel_reason"] == "deadline"
    assert sessions.get(lost["id"])["outcome"] == "worker_lost" and sessions.get(lost["id"])["usage"] == "unknown"
    assert sessions.get(fine["id"])["state"] == "attached"
    assert sessions.sweep() == []  # idempotent: nothing left to do
    # a crash between the end and the settlement: the sweep settles it on the next pass, once
    r = sessions.release(fine["id"], W2, "done", 5.0)
    assert r["settled_at"]
    _sql("update sessions set settled_at = null where id = %s", fine["id"])
    spent = keys.info(KEY)["spent_gpu_seconds"]
    assert sessions.sweep() == [f"{fine['id']}:settled"]
    assert sessions.get(fine["id"])["settled_at"] and keys.info(KEY)["spent_gpu_seconds"] == spent  # released once


# ---- cold attachment (api 0.11.83, migration 20) --------------------------------------------------------------------
MODEL = {"name": "speech", "k8s_name": "speech", "namespace": "models", "nodes": 1, "regions": ["hub", "south"]}


@pytest.fixture
def cold(live, monkeypatch):
    """The endpoint's readiness per region is scripted; every status read is counted."""
    import catalog

    state = {"ready": {"eu-north1": False, "eu-south1": False}, "reads": [], "fail": set()}
    monkeypatch.setattr(catalog, "get", lambda name: MODEL if name == "speech" else None)

    def status(name, ns, region="eu-north1", nodes=1):
        state["reads"].append(region)
        if region in state["fail"]:
            raise RuntimeError("status unavailable")
        return {"status": "ready" if state["ready"].get(region) else "scaled-to-zero"}

    import kube

    monkeypatch.setattr(kube, "endpoint_status", status)
    monkeypatch.setattr(sessions, "_worker_gone", lambda s: False)  # the attached pod lives (no cluster here)
    monkeypatch.setattr(cfg, "SESSION_ATTACH_WAIT_S", 60)
    return state


def _bound(timeout_s=600, region="eu-north1"):
    s = sessions.create(KEY, "demo", "p-1", "speech", "hub", region, timeout_s, 1)
    b = sessions.bind(s["claim_token"], "speech")
    return s["id"], b["connection"]


def _age(sid, **cols):
    """Move timestamps into the past (seconds ago) to play time forward."""
    for col, ago in cols.items():
        _sql(f"update sessions set {col} = now() - %s * interval '1 second' where id = %s", ago, sid)


def test_cold_start_longer_than_the_wait_then_ready_and_attach(cold):
    sid, conn = _bound()
    _age(sid, bound_at=300)  # bound five minutes ago, the endpoint still starting: the old rule would have ended it
    assert sessions.sweep() == []
    s = sessions.get(sid)
    assert s["state"] == "bound" and s["ready_seen_at"] is None and sessions.public(s)["phase"] == "starting"
    assert sessions.public(s)["gpu_seconds"] is None and "claim_token" not in sessions.public(s)
    cold["ready"]["eu-north1"] = True
    assert sessions.sweep() == [f"{sid}:ready-seen"]
    s = sessions.get(sid)
    assert s["ready_seen_at"] and s["attach_deadline_at"] and sessions.public(s)["phase"] == "waiting_for_worker"
    assert (s["attach_deadline_at"] - s["ready_seen_at"]).total_seconds() == pytest.approx(60, abs=1)
    a = sessions.attach(sid, conn, W1, "ws-1")
    assert a["state"] == "attached" and sessions.public(a)["phase"] == "attached"
    assert sessions.sweep() == []


def test_ready_but_no_worker_expires_at_the_attach_deadline_not_before(cold):
    sid, _ = _bound()
    cold["ready"]["eu-north1"] = True
    assert sessions.sweep() == [f"{sid}:ready-seen"]
    assert sessions.sweep() == []  # within the wait: nothing
    _age(sid, attach_deadline_at=1)
    assert sessions.sweep() == [f"{sid}:unattached"]
    s = sessions.get(sid)
    assert s["outcome"] == "unattached" and s["usage"] == "unknown" and s["gpu_seconds"] is None and s["settled_at"]


def test_never_ready_ends_at_the_admitted_deadline_and_never_later(cold):
    sid, _ = _bound(timeout_s=600)
    _age(sid, bound_at=3000)  # long past any attach wait, still starting: held until the admitted deadline only
    assert sessions.sweep() == []
    _age(sid, deadline_at=1)
    assert sessions.sweep() == [f"{sid}:unattached"]
    s = sessions.get(sid)
    assert s["ready_seen_at"] is None and s["usage"] == "unknown" and s["gpu_seconds"] is None
    assert _reservation(sid) is None  # the admission's reservation is released as unknown; nothing extra was ever held
    # unknown status is unknown: no readiness recorded, nothing ended
    sid2, _ = _bound()
    cold["fail"].add("eu-north1")
    assert sessions.sweep() == [] and sessions.get(sid2)["ready_seen_at"] is None


def test_a_readiness_flap_never_extends_the_attach_deadline(cold):
    sid, _ = _bound()
    cold["ready"]["eu-north1"] = True
    assert sessions.sweep() == [f"{sid}:ready-seen"]
    first = sessions.get(sid)["attach_deadline_at"]
    cold["ready"]["eu-north1"] = False
    assert sessions.sweep() == []
    cold["ready"]["eu-north1"] = True
    assert sessions.sweep() == []  # ready again: recorded once already
    assert sessions.get(sid)["attach_deadline_at"] == first
    # the attach deadline never exceeds the admitted deadline
    short, _ = _bound(timeout_s=60)
    assert sessions.sweep() == [f"{short}:ready-seen"]
    s = sessions.get(short)
    assert s["attach_deadline_at"] <= s["deadline_at"]


def test_regions_are_judged_separately_and_status_is_read_once_per_region_per_sweep(cold):
    north, _ = _bound(region="eu-north1")
    north2, _ = _bound(region="eu-north1")
    south, _ = _bound(region="eu-south1")
    cold["ready"]["eu-south1"] = True
    cold["reads"].clear()
    out = sessions.sweep()
    assert out == [f"{south}:ready-seen"]
    assert sorted(cold["reads"]) == ["eu-north1", "eu-south1"]  # one read per (model, region), not per session
    assert sessions.get(north)["ready_seen_at"] is None and sessions.get(north2)["ready_seen_at"] is None


def test_concurrent_sweeps_record_readiness_once_and_end_once(cold):
    sid, _ = _bound()
    cold["ready"]["eu-north1"] = True
    with ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(lambda _: sessions.sweep(), range(4)))
    assert sum(o.count(f"{sid}:ready-seen") for o in outs) == 1
    _age(sid, attach_deadline_at=1)
    with ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(lambda _: sessions.sweep(), range(4)))
    assert sum(o.count(f"{sid}:unattached") for o in outs) == 1
    assert sessions.get(sid)["state"] == "ended"


# ---- idempotent creation (api 0.11.104, migration 27) --------------------------------------------------------------
def _idem(key, owner="p-1", timeout_s=600, model="speech"):
    return sessions.create(KEY, "demo", owner, model, "hub", "eu-north1", timeout_s, 1, idempotency_key=key)


def _rows(where="true", *args):
    with db.pool().connection() as c:
        return c.execute(f"select count(*) from sessions where {where}", args).fetchone()[0]


def test_a_lost_create_answer_is_recovered_with_an_unused_claim_and_one_admission(live):
    a = _idem("k1")
    assert a["replay"] is False and a["claim_token"]
    # the first answer never reached the client; the same request again: the same session, one row, one reservation,
    # a usable claim that replaces the earlier token; nothing extended
    b = _idem("k1")
    assert b["id"] == a["id"] and b["replay"] is True and b["claim_token"] and b["claim_token"] != a["claim_token"]
    assert (b["expires_at"], b["deadline_at"], b["created_at"]) == (a["expires_at"], a["deadline_at"], a["created_at"])
    assert _rows("owner = %s", "p-1") == 1 and _reservation(a["id"]) is not None
    assert _rows("id like 'ses-%%'") == 1
    # bind once: the replaced token binds no more; the replayed one binds exactly once
    with pytest.raises(HTTPException) as e:
        sessions.bind(a["claim_token"], "speech")
    assert e.value.status_code == 401
    bound = sessions.bind(b["claim_token"], "speech")
    assert bound["id"] == a["id"]
    with pytest.raises(HTTPException):
        sessions.bind(b["claim_token"], "speech")
    # bound: the replay is the same id, no claim (replay denied), nothing re-issued, nothing moved
    c = _idem("k1")
    assert c["id"] == a["id"] and c["status"] == "bound" and c["replay"] is True and "claim_token" not in c
    assert (c["expires_at"], c["deadline_at"]) == (a["expires_at"], a["deadline_at"])
    # attached and ended: still the same id without a claim; the ledger settles once
    sessions.attach(a["id"], bound["connection"], W1, "w-1")
    d = _idem("k1")
    assert d["id"] == a["id"] and d["status"] == "attached" and "claim_token" not in d
    sessions.release(a["id"], W1, "done", 12.0)
    e2 = _idem("k1")
    assert e2["id"] == a["id"] and e2["status"] == "ended" and "claim_token" not in e2 and e2["gpu_seconds"] == 12.0
    assert _rows("owner = %s", "p-1") == 1 and _reservation(a["id"]) is None  # settled: the reservation is spent
    # the same key with another request (timeout, model or deployment) is a conflict, not a session
    for kw in ({"timeout_s": 900}, {"model": "other-speech"}):
        with pytest.raises(HTTPException) as x:
            _idem("k1", **kw)
        assert x.value.status_code == 409 and "Idempotency-Key" in x.value.detail
    with pytest.raises(HTTPException) as x:
        sessions.create(KEY, "demo", "p-1", "speech", "south", "eu-south1", 600, 1, idempotency_key="k1")
    assert x.value.status_code == 409
    # owner-scoped: a peer's same key is its own session; without a key every create is a new session
    f = _idem("k1", owner="p-2")
    assert f["id"] != a["id"] and f["replay"] is False and f["claim_token"]
    assert _create()["id"] != _create()["id"]


def test_concurrent_creates_under_one_key_make_one_session_one_admission_and_one_binding(live):
    with ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(lambda _: _idem("race"), range(8)))
    ids = {o["id"] for o in outs}
    assert len(ids) == 1 and sum(1 for o in outs if o["replay"] is False) == 1
    sid = ids.pop()
    assert _rows("true") == 1 and _reservation(sid) is not None
    with db.pool().connection() as c:
        # the losers released their own admission: exactly one reservation of a session exists
        assert c.execute("select count(*) from key_reservations where operation like 'ses-%'").fetchone()[0] == 1
    # every answer carried a claim; exactly one of them (the last replacement) binds, the others are refused
    tokens = [o["claim_token"] for o in outs]
    assert len(set(tokens)) == len(tokens)
    bound = 0
    for t in tokens:
        try:
            sessions.bind(t, "speech")
            bound += 1
        except HTTPException as e:
            assert e.status_code == 401
    assert bound == 1


def test_an_expired_or_cancelled_claim_replays_without_a_claim_and_is_never_extended(live):
    a = _idem("k2")
    _sql("update sessions set expires_at = now() - interval '1 second' where id = %s", a["id"])
    b = _idem("k2")  # the claim TTL is over: no claim is re-issued, the TTL is not moved
    assert b["id"] == a["id"] and "claim_token" not in b and b["status"] == "claimed"
    with db.pool().connection() as c:
        assert c.execute("select expires_at < now() from sessions where id = %s", (a["id"],)).fetchone()[0]
    assert f"{a['id']}:expired" in sessions.sweep()
    c2 = _idem("k2")
    assert c2["id"] == a["id"] and c2["status"] == "ended" and c2["outcome"] == "expired" and "claim_token" not in c2
    # a cancelled claim: ended, replayed as such
    x = _idem("k3")
    sessions.cancel(x["id"], "owner")
    y = _idem("k3")
    assert y["id"] == x["id"] and y["status"] == "ended" and y["cancel_requested"] and "claim_token" not in y
    assert _rows("idempotency_key in ('k2', 'k3')") == 2
