"""Real transactions, using an ephemeral PostgreSQL service (never a cloud fleet).

Set TEST_DATABASE_URL to run these tests. CI provides PostgreSQL; each test uses its own schema.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

import attempts
import billing
import db
import jobs
import keys


@pytest.fixture
def database(monkeypatch):
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is needed for PostgreSQL integration tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(psycopg.sql.SQL("create schema {}").format(psycopg.sql.Identifier(schema)))
    url = make_conninfo(base, options="-c search_path=" + schema)
    try:
        with ConnectionPool(url, min_size=1, max_size=8, kwargs={"autocommit": True}) as pool:
            monkeypatch.setattr(db, "DATABASE_URL", url)
            monkeypatch.setattr(db, "_pool", pool)
            monkeypatch.setattr(keys, "LITELLM_MASTER_KEY", "")  # no mirror: LiteLLM is not part of these tests
            yield url
    finally:
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(psycopg.sql.SQL("drop schema {} cascade").format(psycopg.sql.Identifier(schema)))


def entry(mid="test-model", regions=("hub",)):
    return {"id": mid, "mode": "sync", "runtime": {"image": "example:1"}, "deployments": {r: {} for r in regions}}


def test_two_api_replicas_migrate_concurrently(database):
    with ThreadPoolExecutor(2) as ex:
        assert list(ex.map(lambda _: db.migrate(), range(2))) == [len(db.MIGRATIONS)] * 2
    with psycopg.connect(database) as c:
        assert c.execute("select count(*) from schema_version").fetchone()[0] == len(db.MIGRATIONS)


def test_delete_and_recreate_keeps_monotonic_history(database):
    db.migrate()
    assert db.upsert_model(entry(), {}, "operator", create_only=True)["version"] == 1
    assert db.delete_model("test-model", "operator")
    assert db.upsert_model(entry(), {}, "operator", create_only=True)["version"] == 3
    assert [(h["version"], h["action"]) for h in db.history("test-model")] == [
        (3, "create"),
        (2, "delete"),
        (1, "create"),
    ]


def test_optimistic_writes_reject_stale_version_without_history(database):
    db.migrate()
    db.upsert_model(entry(), {}, "operator")
    db.upsert_model(entry(), {}, "operator", expected=1)
    with pytest.raises(HTTPException, match="409"):
        db.upsert_model(entry(), {}, "operator", expected=1)
    with pytest.raises(HTTPException, match="409"):
        db.delete_model("test-model", "operator", expected=1)
    assert db.get_model("test-model")["version"] == 2
    assert len(db.history("test-model")) == 2


def test_concurrent_creates_commit_exactly_once(database):
    db.migrate()

    def create(_):
        try:
            return db.upsert_model(entry(), {}, "operator", create_only=True)["version"]
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(2) as ex:
        assert sorted(ex.map(create, range(2))) == [1, 409]
    assert len(db.history("test-model")) == 1


def test_pending_updates_keep_regions_and_new_generation(database):
    db.migrate()
    db.upsert_model(entry(regions=("hub",)), {}, "operator")
    db.upsert_model(entry(regions=("eu-south1",)), {}, "operator")
    db.upsert_model(entry(regions=("us-central1",)), {}, "operator")
    change = db.pending_changes()[0]
    assert set(change["previous"]["deployments"]) == {"hub", "eu-south1"}
    db.finish_change("test-model", 2)
    assert db.pending_changes()[0]["version"] == 3
    db.finish_change("test-model", 3, "temporary failure")
    assert db.pending_changes()[0]["error"] == "temporary failure"
    db.finish_change("test-model", 3)
    assert not db.pending_changes()


def test_reconciler_has_one_database_owner(database):
    db.migrate()
    with db.reconciler() as acquired:
        assert acquired
        with db.reconciler() as second:
            assert not second
    with db.reconciler() as next_owner:
        assert next_owner


def test_keys_create_update_revoke_and_tenant_isolation(database):
    db.migrate()
    value, i = keys.create(
        "demo",
        "demo-ci",
        5,
        ["llm-example"],
        7,
        metadata={"allowed_passthrough_routes": ["/models/llm/chat"]},
        by="demo-admin",
    )
    assert value.startswith("sk-") and len(value) == 43 and i["key_name"] == "sk-..." + value[-4:]
    assert (
        keys.info(keys.key_hash(value)) == i
        and i["max_budget"] == 5.0
        and i["spend"] == 0
        and i["models"] == ["llm-example"]
    )
    assert (
        i["metadata"] == {"tenant": "demo", "role": "user", "allowed_passthrough_routes": ["/models/llm/chat"]}
        and i["expires"]
    )
    with pytest.raises(HTTPException, match="409"):
        keys.create("demo", "demo-ci", 1)  # alias unique per tenant ...
    keys.create("other", "demo-ci", 1)  # ... not across tenants
    assert [k["key_alias"] for k in keys.list_tenant("demo")] == ["demo-ci"]
    u = keys.update("demo", "demo-ci", budget=50, expires_days=0)
    assert u["max_budget"] == 50 and u["expires"] is None and u["models"] == ["llm-example"]  # models untouched
    assert keys.update("other", "nope", budget=1) is None
    assert keys.block("demo", "demo-ci")["blocked"] and not keys.block("demo", "demo-ci", False)["blocked"]
    assert keys.revoke("demo", "demo-ci") and not keys.revoke("demo", "demo-ci")
    assert keys.info(keys.key_hash(value)) is None and keys.list_tenant("other")[0]["key_alias"] == "demo-ci"


def test_charge_retry_after_annotation_failure_is_idempotent(database):
    db.migrate()
    value, _ = keys.create("demo", "demo-k", 10)
    h = keys.key_hash(value)
    assert billing.record_spend(h, 1.5, "hub/job-uid", 3600) == 1.5
    assert billing.record_spend(h, 1.5, "hub/job-uid", 3600) == 1.5
    assert keys.info(h)["spend"] == 1.5
    with psycopg.connect(database) as c:
        assert c.execute("select count(*) from charges").fetchone()[0] == 1
        assert c.execute("select key_hash, gpu_seconds from charges where operation = 'hub/job-uid'").fetchone() == (
            h,
            3600,
        )


def test_concurrent_charges_and_admin_edits_do_not_lose_spend(database):
    db.migrate()
    value, _ = keys.create("demo", "demo-k", 10)
    h = keys.key_hash(value)

    def charge(i):
        if i == 0:
            keys.update("demo", "demo-k", budget=20)  # an admin edit in the middle: never touches spend
        else:
            billing.record_spend(h, 1, "hub/job-" + str(i), 100)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(charge, range(12)))
    i = keys.info(h)
    assert i["spend"] == 11 and i["max_budget"] == 20


def test_charge_of_a_revoked_key_stays_in_the_ledger(database):
    """The run happened: the receipt is kept under the key's hash and the run counts as billed (no endless retry)."""
    db.migrate()
    value, _ = keys.create("demo", "demo-k", 10)
    h = keys.key_hash(value)
    keys.revoke("demo", "demo-k")
    assert billing.record_spend(h, 1, "hub/retry", 100) is None
    with psycopg.connect(database) as c:
        assert c.execute("select key_hash from charges where operation='hub/retry'").fetchone() == (h,)


def test_receipt_cannot_be_reassigned_to_another_key(database):
    db.migrate()
    a, _ = keys.create("demo", "demo-a", 10)
    b, _ = keys.create("demo", "demo-b", 10)
    billing.record_spend(keys.key_hash(a), 1, "hub/uid", 100)
    with pytest.raises(ValueError):
        billing.record_spend(keys.key_hash(b), 1, "hub/uid", 100)


# ---- imported keys and per-key limits (migration 8) ----
LEGACY = {
    "hash": "b" * 64,
    "alias": "legacy",
    "tenant": "demo",
    "principal_id": "p-1",
    "models": [],
    "scopes": ["invoke:sync", "run:submit"],
    "budget_requests": 2,
    "budget_gpu_seconds": 1000,
    "max_concurrent": 1,
    "rate_limit": {"requests": 2, "window_s": 3600},
}


def test_import_is_idempotent_all_or_nothing_and_never_overwrites(database):
    db.migrate()
    from fastapi import HTTPException

    assert keys.import_records([LEGACY], "demo", "admin", dry_run=True)["inserted"] == ["demo-legacy"]
    assert keys.info("b" * 64) is None  # a dry run wrote nothing
    r = keys.import_records([LEGACY], "demo", "admin")
    assert r["inserted"] == ["demo-legacy"] and r["not_mirrored"] == ["demo-legacy"]
    i = keys.info("b" * 64)
    assert (
        i["scopes"] == ["invoke:sync", "run:submit"]
        and i["budget_requests"] == 2.0
        and i["rate_limit"] == {"requests": 2, "window_s": 3600}
    )
    assert (
        i["metadata"]["role"] == "user"
        and i["metadata"]["legacy"]["principal_id"] == "p-1"
        and i["created_by"] == "import:admin"
    )
    assert keys.import_records([LEGACY], "demo", "admin")["unchanged"] == ["demo-legacy"]  # byte for byte: unchanged
    with pytest.raises(HTTPException) as e:  # same key, other limits: a conflict
        keys.import_records(
            [{**LEGACY, "budget_requests": 3}, {**LEGACY, "hash": "c" * 64, "alias": "new"}], "demo", "admin"
        )
    assert e.value.status_code == 409 and e.value.detail["conflicts"][0]["alias"] == "demo-legacy"
    assert keys.info("c" * 64) is None  # ... and the batch wrote nothing
    with pytest.raises(HTTPException) as e:  # the hash under another alias
        keys.import_records([{**LEGACY, "alias": "other"}], "demo", "admin")
    assert e.value.status_code == 409 and "exists as demo/demo-legacy" in e.value.detail["conflicts"][0]["reason"]
    with pytest.raises(HTTPException) as e:  # the alias with another hash
        keys.import_records([{**LEGACY, "hash": "d" * 64}], "demo", "admin")
    assert e.value.status_code == 409 and "another hash" in e.value.detail["conflicts"][0]["reason"]
    with pytest.raises(HTTPException) as e:  # invalid records: 400, nothing written
        keys.import_records([{**LEGACY, "hash": "e" * 64, "alias": "x", "tenant": "other"}], "demo", "admin")
    assert e.value.status_code == 400
    r = keys.import_records(
        [{**LEGACY, "hash": "f" * 64, "alias": "blocked", "scopes": ["invoke:sync", "mcp.invoke"]}], "demo", "admin"
    )
    assert (
        r["blocked_for_unknown_scope"] == [{"alias": "demo-blocked", "scopes": ["mcp.invoke"]}]
        and keys.info("f" * 64)["blocked"] is True
    )


def test_admission_holds_every_limit_in_the_key_units(database):
    db.migrate()
    keys.import_records([LEGACY], "demo", "admin")
    h = "b" * 64
    assert keys.admit(h, "op-1", "demo", gpu_seconds=600)["active"] == 1
    assert keys.admit(h, "op-1", "demo", gpu_seconds=600)["replay"] is True  # the same idempotent operation counts once
    with pytest.raises(keys.KeyLimit) as e:  # max_concurrent 1
        keys.admit(h, "op-2", "demo")
    assert e.value.status == 429 and "concurrency" in e.value.detail
    assert keys.release("op-1", 450) is True and keys.release("op-1", 450) is False  # measured, idempotent
    i = keys.info(h)
    assert i["spent_requests"] == 1.0 and i["spent_gpu_seconds"] == 450.0
    with pytest.raises(keys.KeyLimit) as e:  # 450 spent + 600 > 1000 GPU seconds
        keys.admit(h, "op-3", "demo", gpu_seconds=600)
    assert e.value.status == 402 and "GPU-second" in e.value.detail
    assert keys.admit(h, "op-3", "demo", gpu_seconds=500)["active"] == 1
    keys.release("op-3", None)  # cancelled: nothing measured, the request spent
    with pytest.raises(keys.KeyLimit) as e:  # request budget 2: spent
        keys.admit(h, "op-4", "demo")
    assert e.value.status == 402 and "request budget" in e.value.detail
    assert keys.admit(h, "op-4", "demo", counts_request=False)["active"] == 1  # a non-request reservation still fits
    keys.release("op-4", 0)
    # the rate window: a key with a larger request budget hits its window
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": "9" * 64,
                "alias": "rate",
                "budget_requests": None,
                "max_concurrent": None,
                "budget_gpu_seconds": None,
            }
        ],
        "demo",
        "admin",
    )
    for n in range(2):
        keys.release(f"r-{n}", 0) if keys.admit("9" * 64, f"r-{n}", "demo") else None
    with pytest.raises(keys.KeyLimit) as e:
        keys.admit("9" * 64, "r-3", "demo")
    assert e.value.status == 429 and "2 requests per 3600 s" in e.value.detail
    assert (
        keys.stale_reservations(0) == []
        and keys.admit("9" * 64, "r-5", "demo", counts_request=False)
        and keys.stale_reservations(0) == [("r-5", "demo")]
    )


def test_two_replicas_cannot_both_take_the_last_slot(database):
    db.migrate()
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": "7" * 64,
                "alias": "race",
                "max_concurrent": 1,
                "budget_requests": None,
                "budget_gpu_seconds": None,
                "rate_limit": None,
            }
        ],
        "demo",
        "admin",
    )
    import threading

    results = []

    def take(n):
        try:
            keys.admit("7" * 64, f"race-{n}", "demo")
            results.append("ok")
        except keys.KeyLimit:
            results.append("full")

    ts = [threading.Thread(target=take, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["full"] * 5 + ["ok"]


def test_bootstrap_rotation_revokes_the_previous_mirror_and_retries_it(database, monkeypatch):
    """[P1] A Terraform key alias with a new value: the previous value's LiteLLM record is deleted; while LiteLLM is
    unavailable the deletion stays owed (retired_mirrors) and the next sweep completes it."""
    db.migrate()
    calls, answer = [], {"code": 200, "text": ""}
    monkeypatch.setattr(keys, "LITELLM_MASTER_KEY", "m")
    monkeypatch.setattr(
        keys, "_litellm_call", lambda method, path, body: calls.append((path, body)) or (answer["code"], answer["text"])
    )
    old, new = "sk-" + "o" * 40, "sk-" + "n" * 40
    i = keys.bootstrap(old, "demo-admin", "demo", "admin")
    assert i["token"] == keys.key_hash(old) and [p for p, _ in calls] == ["/key/generate"]
    calls.clear()
    answer["code"] = 0  # LiteLLM unreachable during the rotation
    assert keys.bootstrap(new, "demo-admin", "demo", "admin")["token"] == keys.key_hash(new)
    assert keys.info(keys.key_hash(old)) is None and keys.info(keys.key_hash(new))["key_alias"] == "demo-admin"
    assert ("/key/delete", {"keys": [keys.key_hash(old)]}) in calls
    with db.pool().connection() as c:
        assert [r[0] for r in c.execute("select hash from retired_mirrors").fetchall()] == [keys.key_hash(old)]
    calls.clear()
    answer["code"] = 400
    answer["text"] = '{"error": "key not found"}'  # LiteLLM back; the record is already gone
    assert keys.retire_mirrors() == [keys.key_hash(old)] and calls == [("/key/delete", {"keys": [keys.key_hash(old)]})]
    with db.pool().connection() as c:
        assert c.execute("select count(*) from retired_mirrors").fetchone()[0] == 0
    assert keys.retire_mirrors() == []
    # the same value again is not a rotation: nothing retired
    calls.clear()
    answer["code"] = 200
    keys.bootstrap(new, "demo-admin", "demo", "admin")
    assert [p for p, _ in calls] == ["/key/generate"]
    # a revocation is owed the same way
    answer["code"] = 0
    assert keys.revoke("demo", "demo-admin")
    answer["code"] = 200
    assert keys.retire_mirrors() == [keys.key_hash(new)]


def test_rotation_back_to_a_previous_value_does_not_revoke_the_active_mirror(database, monkeypatch):
    """A rollback during a router outage must cancel the old pending deletion before restoring that value."""
    db.migrate()
    calls, answer = [], {"code": 200}
    monkeypatch.setattr(keys, "LITELLM_MASTER_KEY", "m")
    monkeypatch.setattr(
        keys, "_litellm_call", lambda method, path, body: calls.append((path, body)) or (answer["code"], "")
    )
    old, new = "sk-" + "a" * 40, "sk-" + "b" * 40
    keys.bootstrap(old, "demo-admin", "demo", "admin")
    answer["code"] = 0
    keys.bootstrap(new, "demo-admin", "demo", "admin")
    answer["code"] = 200
    calls.clear()
    keys.bootstrap(old, "demo-admin", "demo", "admin")
    assert keys.info(keys.key_hash(old)) is not None
    assert calls[0][0] == "/key/generate"
    assert calls[1:] == [("/key/delete", {"keys": [keys.key_hash(new)]})]
    with db.pool().connection() as c:
        assert c.execute("select count(*) from retired_mirrors").fetchone()[0] == 0


def _bounded(h: str, **limits):
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": h,
                "alias": "run-" + h[:4],
                "max_concurrent": 1,
                "budget_requests": 5,
                "budget_gpu_seconds": 1000,
                "rate_limit": None,
                **limits,
            }
        ],
        "demo",
        "admin",
    )


def _reservation(root="root"):
    with db.pool().connection() as c:
        return c.execute(
            "select key_hash, gpu_seconds, requests from key_reservations where operation = %s", (root,)
        ).fetchone()


def test_run_ledger_one_root_slot_members_measured_and_unknown(database):
    """One reservation per run: the root takes the slot and the request once; continuations join after the head with
    their own GPU bound; measured settlement becomes spend, an unmeasured GPU attempt stays `unknown` against the
    budget; the slot closes when no member is active and a later manual continuation re-acquires it."""
    db.migrate()
    h = "c" * 64
    _bounded(h)
    assert keys.admit(h, "root", "demo", gpu_seconds=600, run_member=True)["active"] == 1
    assert keys.run_members("root")[0]["gpu_bound"] == 600.0 and keys.run_head("root") == "root"
    with pytest.raises(keys.KeyLimit) as e:  # the head rule: a continuation follows the head, nothing else
        keys.prepare_run_member("root", "root-r1", "root-x9", 1, 100)
    assert e.value.status == 409 and "head" in e.value.detail
    with pytest.raises(keys.KeyLimit) as e:  # 600 reserved + 500 > 1000 GPU seconds
        keys.prepare_run_member("root", "root-r1", "root", 1, 500)
    assert e.value.status == 402
    with pytest.raises(keys.KeyLimit) as e:  # a GPU continuation on a GPU-second budget needs a bound
        keys.prepare_run_member("root", "root-r1", "root", 1, None)
    assert e.value.status == 400
    m = keys.prepare_run_member("root", "root-r1", "root", 1, 300)
    assert m["gpu_bound"] == 300.0 and m["key_hash"] == h
    assert keys.prepare_run_member("root", "root-r1", "root", 1, 300)["replay"] is True  # idempotent member
    assert _reservation() == (h, 900, 1) and keys.run_head("root") == "root-r1"
    assert keys.info(h)["spent_requests"] == 0  # the request is spent when the run closes, once
    # the root's attempt settles measured: spend; the slot stays while the successor is active
    assert keys.release("root", 450) is True
    i = keys.info(h)
    assert i["spent_gpu_seconds"] == 450.0 and _reservation() == (h, 300, 1)
    # the successor ends without a trustworthy measurement: unknown, its bound stays against the budget, the slot goes
    s = keys.settle_run_member("root-r1", None)
    assert s["state"] == "unknown" and s["closed"] is True and _reservation() is None
    assert keys.run_usage(h) == {"active_operations": 0, "reserved_gpu_seconds": 0.0, "unknown_gpu_seconds": 300.0}
    assert keys.info(h)["spent_requests"] == 1
    with pytest.raises(keys.KeyLimit) as e:  # 450 spent + 300 unknown + 300 > 1000
        keys.admit(h, "other", "demo", gpu_seconds=300, run_member=True)
    assert e.value.status == 402
    # a manual continuation of the closed run re-acquires the slot: no second request, concurrency re-checked
    keys.admit(h, "busy", "demo", run_member=True)
    with pytest.raises(keys.KeyLimit) as e:
        keys.prepare_run_member("root", "root-r2", "root-r1", 1, 100)
    assert e.value.status == 429
    keys.release("busy", 0)
    m = keys.prepare_run_member("root", "root-r2", "root-r1", 1, 100, transfer_only=True)
    assert m["gpu_bound"] == 0.0 and _reservation() == (h, 0, 0)
    assert keys.settle_run_member("root-r2", 0.0)["closed"] is True
    assert keys.info(h)["spent_requests"] == 2  # root and busy: one request each; the continuation none
    # the operator settles the unknown attempt later
    assert keys.settle_unknown("root-r1", 120) is True and keys.settle_unknown("root-r1", 1) is False
    assert keys.info(h)["spent_gpu_seconds"] == 570.0 and keys.run_usage(h)["unknown_gpu_seconds"] == 0.0


def test_run_ledger_rechecks_the_original_key_and_rolls_back_a_lost_create(database):
    db.migrate()
    h = "d" * 64
    _bounded(h, budget_gpu_seconds=None)
    keys.admit(h, "root", "demo", run_member=True)
    keys.block("demo", "demo-run-dddd")
    with pytest.raises(keys.KeyLimit) as e:
        keys.prepare_run_member("root", "root-r1", "root", 1, 100)
    assert e.value.status == 403
    keys.block("demo", "demo-run-dddd", False)
    keys.prepare_run_member("root", "root-r1", "root", 1, 100)
    assert keys.rollback_run_member("root-r1") is True and keys.run_head("root") == "root"
    assert keys.rollback_run_member("root") is False  # the root is never rolled back here
    assert _reservation() is not None
    assert keys.release("root", 0) is True and _reservation() is None
    # a key that is not the run's cannot continue it: the ledger admits on the root's key only
    with db.pool().connection() as c:
        assert c.execute("select key_hash from key_run_members where operation = 'root'").fetchone()[0] == h


def test_late_record_of_a_replaced_attempt_closes_accounting_without_promoting(database):
    """Handoff failure 1: after epoch 2 started, epoch 1's own interruption still records its end (for accounting),
    but no pointer or result of it is kept and a late start or success stays refused."""
    db.migrate()
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "root-a",
        {"operation": "root", "status": "started", "gpus": 1, "started_at": "2026-10-13T08:00:00Z"},
    )
    attempts.mark_successor("root", "root-r1")
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "root-r1-a",
        {"operation": "root-r1", "root": "root", "status": "started", "gpus": 1, "started_at": "2026-10-13T08:00:30Z"},
    )
    closed = attempts.record(
        "eu-north1",
        "tenant-demo",
        "root-a",
        {
            "operation": "root",
            "status": "interrupted",
            "gpus": 1,
            "ended_at": "2026-10-13T08:00:25Z",
            "checkpoint_ref": "app://must-not-promote",
            "result_ref": "app://must-not-promote",
        },
    )
    assert (
        closed["ended_at"]
        and not closed["result_ref"]
        and not closed["checkpoint_ref"]
        and closed["status"] == "interrupted"
    )
    with pytest.raises(HTTPException, match="409"):
        attempts.record(
            "eu-north1",
            "tenant-demo",
            "root-a",
            {"operation": "root", "status": "succeeded", "gpus": 1, "ended_at": "2026-10-13T08:00:26Z"},
        )


def test_durable_rows_keep_mode_model_and_io_mode(database):
    """An operation whose Job is gone keeps its provenance (migration 12): an application-I/O endpoint call stays an
    async application call (its result is its reference, never the platform bucket) and its model still applies."""
    db.migrate()
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-call-a",
        {
            "operation": "op-call",
            "status": "started",
            "gpus": 0,
            "mode": "async",
            "model": "llm-example",
            "io_mode": "application",
        },
    )
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-call-a",
        {
            "operation": "op-call",
            "status": "succeeded",
            "gpus": 0,
            "export": "succeeded",
            "result_ref": "app://r/1",
            "ended_at": "2026-10-13T20:00:00Z",
            "io_mode": "application",
        },
    )
    rows = attempts.for_operation("op-call")
    assert rows[-1]["mode"] == "async" and rows[-1]["model"] == "llm-example" and rows[-1]["io_mode"] == "application"
    op = attempts.synthesize("op-call", "demo")
    assert op["mode"] == "async" and op["model"] == "llm-example" and op["execution"] == {"io_mode": "application"}
    assert op["status"] == "SUCCEEDED" and op["result_ref"] == "app://r/1"
    attempts.record(
        "eu-north1",
        "tenant-demo",
        "op-run-a",
        {
            "operation": "op-run",
            "status": "failed",
            "gpus": 1,
            "started_at": "2026-10-13T19:00:00Z",
            "ended_at": "2026-10-13T19:10:00Z",
        },
    )
    plain = attempts.synthesize("op-run", "demo")
    assert plain["mode"] == "run" and plain["execution"] is None and plain["model"] is None


def test_stage_graph_members_bypass_the_head_rule_and_hold_the_run_open(database):
    """Stage composition on the ledger (keys.py migration 13): a root's declared stages are sealed at admission; a
    stage member joins without the head rule (siblings coexist), one operation per declared stage, only declared
    stages; the run's slot and request stay until every declared stage settled (the root's own settlement does not
    close it); after it closed or was cancelled nothing attaches; a cancelled graph closes an idle run at once."""
    db.migrate()
    h = "e" * 64
    _bounded(h, budget_gpu_seconds=None)
    keys.admit(h, "root", "demo", run_member=True)
    keys.declare_graph("root", "demo", "owner-1", {"a": [], "b": ["a"]})
    keys.declare_graph("root", "demo", "owner-1", {"zzz": []})  # a replay of the root: the first declaration stands
    g = keys.graph_of("root")
    assert g["stages"] == {"a": [], "b": ["a"]} and g["owner"] == "owner-1" and g["cancelled_at"] is None
    assert [x["root"] for x in keys.open_graphs()] == ["root"]
    with pytest.raises(keys.KeyLimit) as e:
        keys.prepare_run_member("root", "root-s-x", None, 0, None, kind="stage", stage="x")
    assert e.value.status == 400 and "not declared" in e.value.detail
    with pytest.raises(keys.KeyLimit) as e:  # a graph is needed for a stage
        keys.prepare_run_member("other", "other-s-a", None, 0, None, kind="stage", stage="a")
    assert e.value.status == 409
    a = keys.prepare_run_member("root", "root-s-a", None, 1, 100, kind="stage", stage="a")
    assert a["replay"] is False and a["gpu_bound"] == 0.0  # no GPU-second budget on this key: no bound
    assert keys.prepare_run_member("root", "root-s-a", None, 1, 100, kind="stage", stage="a")["replay"] is True
    with pytest.raises(keys.KeyLimit) as e:  # one operation per declared stage
        keys.prepare_run_member("root", "root-s-a2", None, 1, 100, kind="stage", stage="a")
    assert e.value.status == 409 and "already has an operation" in e.value.detail
    b = keys.prepare_run_member("root", "root-s-b", None, 1, 100, kind="stage", stage="b")  # a sibling: no head rule
    assert b["replay"] is False
    members = {m["operation"]: m for m in keys.run_members("root")}
    assert members["root-s-a"]["kind"] == "stage" and members["root-s-a"]["stage"] == "a"
    assert members["root"]["kind"] == "continuation" and members["root"]["stage"] is None
    # the root settles: the run stays open (its declared stages have not settled), the request is not spent
    assert keys.release("root", 30) is True
    assert _reservation() is not None and keys.info(h)["spent_requests"] == 0
    assert keys.settle_run_member("root-s-a", 10.0)["closed"] is False
    keys.note_member("root-s-a", "ran")
    assert next(m for m in keys.run_members("root") if m["operation"] == "root-s-a")["note"] == "ran"
    assert keys.settle_run_member("root-s-b", 0.0)["closed"] is True  # the last declared stage: the run closes
    assert _reservation() is None and keys.info(h)["spent_requests"] == 1
    g = keys.graph_of("root")
    assert g["closed_at"] is not None and keys.open_graphs() == []
    with pytest.raises(keys.KeyLimit) as e:  # a declared stage is submitted once: its lineage exists
        keys.prepare_run_member("root", "root-s-b2", None, 1, 100, kind="stage", stage="b")
    assert e.value.status == 409 and "already has an operation" in e.value.detail
    # lineages: a stage's continuation follows that stage's head (never the root's, never a sibling's) and is admitted
    # on the root's key even after the graph closed (the slot is re-acquired, as for any continuation of a closed run)
    assert keys.member_of("root-s-a") == {
        "root": "root",
        "kind": "stage",
        "stage": "a",
        "state": "settled",
        "predecessor": None,
    }
    assert (
        keys.stage_head("root", "a") == "root-s-a" and keys.run_head("root") == "root"
    )  # stages are not the root's head
    with pytest.raises(keys.KeyLimit) as e:  # a sibling's head is not this stage's
        keys.prepare_run_member("root", "root-s-a-r1", "root-s-b", 1, 100, kind="stage", stage="a")
    assert e.value.status == 409 and "head of stage 'a'" in e.value.detail
    with pytest.raises(keys.KeyLimit) as e:  # a stage member is never the root lineage's head
        keys.prepare_run_member("root", "root-r1", "root-s-b", 1, 100)
    assert e.value.status == 409 and "run head" in e.value.detail
    m = keys.prepare_run_member("root", "root-s-a-r1", "root-s-a", 1, 100, kind="stage", stage="a")
    assert m["replay"] is False and keys.stage_head("root", "a") == "root-s-a-r1" and _reservation() is not None
    with pytest.raises(keys.KeyLimit) as e:  # the lineage moved: the old head is not continued twice
        keys.prepare_run_member("root", "root-s-a-r2", "root-s-a", 1, 100, kind="stage", stage="a")
    assert e.value.status == 409
    assert keys.prepare_run_member("root", "root-r1", "root", 1, 100)["replay"] is False  # the root's own lineage
    assert keys.run_head("root") == "root-r1" and keys.stage_head("root", "b") == "root-s-b"
    assert keys.settle_run_member("root-s-a-r1", 5.0)["closed"] is False  # root-r1 still active
    assert keys.settle_run_member("root-r1", 5.0)["closed"] is True and keys.info(h)["spent_requests"] == 1
    # a new declared stage cannot attach to a closed graph (declared but never submitted, then the graph closed)
    # a cancelled graph: an idle run closes at once, the first reason stands, nothing attaches
    keys.admit(h, "root2", "demo", run_member=True)
    keys.declare_graph("root2", "demo", "owner-1", {"a": [], "c": []})
    keys.prepare_run_member("root2", "root2-s-a", None, 0, None, kind="stage", stage="a")
    assert keys.release("root2", 0) is True and _reservation("root2") is not None  # open: stage c never ran
    assert keys.settle_run_member("root2-s-a", 0.0)["closed"] is False
    assert keys.cancel_graph("root2", "cancelled by the caller") is True
    with pytest.raises(keys.KeyLimit) as e:  # a cancelled graph admits nothing, not even a stage's continuation
        keys.prepare_run_member("root2", "root2-s-a-r1", "root2-s-a", 0, None, kind="stage", stage="a")
    assert e.value.status == 409 and "cancelled" in e.value.detail
    assert keys.cancel_graph("root2", "later") is True and keys.graph_of("root2")["reason"] == "cancelled by the caller"
    assert _reservation("root2") is None and keys.info(h)["spent_requests"] == 2
    with pytest.raises(keys.KeyLimit) as e:  # a declared, never submitted stage: nothing attaches any more
        keys.prepare_run_member("root2", "root2-s-c", None, 0, None, kind="stage", stage="c")
    assert e.value.status == 409 and "cancelled" in e.value.detail
    assert keys.prepare_run_member("root2", "root2-s-a", None, 0, None, kind="stage", stage="a")["replay"] is True
    assert keys.cancel_graph("nope", "x") is False


def test_sessions_bind_once_attach_release_once_and_sweep(database, monkeypatch):
    """Live sessions on the ledger (keys.py migration 14, docs/API.md "Sessions"): admission as a run's root; the claim
    binds exactly once across concurrent presentations; attach only for the bound connection, once, idempotent for the
    same pod; release by the attached pod only, measured or unknown, settled once; cancel of a claimed session ends it
    at zero; the sweep expires unbound claims at zero, ends unattached bindings unknown (fenced), requests cancel at the
    deadline, ends sessions whose pod is gone unknown, and settles what a crash left."""
    import config as cfg
    import sessions

    db.migrate()
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    monkeypatch.setattr(cfg, "SESSION_CLAIM_TTL_S", 120)
    monkeypatch.setattr(cfg, "SESSION_ATTACH_WAIT_S", 60)
    h = "f" * 64
    _bounded(h, budget_gpu_seconds=10000, max_concurrent=1)
    s = sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 2)
    sid, token = s["id"], s["claim_token"]
    assert s["status"] == "claimed" and s["usage"] is None and keys.run_members(sid)[0]["gpu_bound"] == 1200.0
    with pytest.raises(keys.KeyLimit) as e:  # the slot is held
        sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 2)
    assert e.value.status == 429
    with pytest.raises(HTTPException) as e:
        sessions.bind(token, "other-model")
    assert e.value.status_code == 401
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _try(lambda: sessions.bind(token, "stt")), range(4)))
    bound = [r for r in results if isinstance(r, dict)]
    assert len(bound) == 1 and all(getattr(r, "status_code", None) == 401 for r in results if r not in bound)
    connection = bound[0]["connection"]
    me = {"pod": "stt-predictor-abc", "namespace": "models", "uid": "uid-1", "region": "eu-north1"}
    other = {"pod": "stt-predictor-xyz", "namespace": "models", "uid": "uid-2", "region": "eu-north1"}
    with pytest.raises(HTTPException) as e:
        sessions.attach(sid, "not-the-connection", me, "w-1")
    assert e.value.status_code == 409
    a = sessions.attach(sid, connection, me, "w-1")
    assert a["state"] == "attached" and a["pod_uid"] == "uid-1"
    assert sessions.attach(sid, connection, me, "w-1")["state"] == "attached"  # the same worker's retry (lost ACK)
    with pytest.raises(HTTPException) as e:  # a second worker never takes it
        sessions.attach(sid, connection, other, "w-2")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:  # only the attached pod releases
        sessions.release(sid, other, "done", 1.0)
    assert e.value.status_code == 409
    sessions.cancel(sid, "cancelled by the owner")  # a request: still attached, slot held
    assert sessions.get(sid)["state"] == "attached" and sessions.get(sid)["cancel_requested_at"] is not None
    assert _reservation(sid) is not None
    r = sessions.release(sid, me, "cancelled", None)  # unmetered: unknown, the bound stays against the budget
    assert r["state"] == "ended" and r["usage"] == "unknown" and r["settled_at"] is not None
    assert sessions.release(sid, me, "cancelled", None)["settled_at"] == r["settled_at"]  # idempotent, settled once
    assert _reservation(sid) is None and keys.run_usage(h)["unknown_gpu_seconds"] == 1200.0
    assert keys.info(h)["spent_requests"] == 1
    with pytest.raises(HTTPException) as e:  # fenced: nothing attaches to an ended session
        sessions.attach(sid, connection, me, "w-1")
    assert e.value.status_code == 409
    # a claimed session cancelled before any connection: ended at zero, the slot free
    s2 = sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 0)
    assert sessions.cancel(s2["id"], "cancelled by the owner")["outcome"] == "cancelled"
    assert _reservation(s2["id"]) is None and sessions.get(s2["id"])["usage"] == "none"
    with pytest.raises(HTTPException):
        sessions.bind(s2["claim_token"], "stt")
    # the sweep: an expired claim, an unattached binding, a deadline, a lost worker, an unsettled end
    s3 = sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 0)
    with db.pool().connection() as c:
        c.execute("update sessions set expires_at = now() - interval '1 second' where id = %s", (s3["id"],))
    assert f"{s3['id']}:expired" in sessions.sweep() and sessions.get(s3["id"])["usage"] == "none"
    s4 = sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 1)
    sessions.bind(s4["claim_token"], "stt")
    with db.pool().connection() as c:  # api 0.11.83: the attach wait runs from the first observed Ready replica
        c.execute(
            "update sessions set ready_seen_at = now() - interval '2 minutes', attach_deadline_at = now() - interval '1 minute' where id = %s",
            (s4["id"],),
        )
    assert f"{s4['id']}:unattached" in sessions.sweep()
    g = sessions.get(s4["id"])
    assert g["state"] == "ended" and g["usage"] == "unknown" and g["settled_at"] is not None
    with pytest.raises(HTTPException):  # a late attach is fenced for good
        sessions.attach(s4["id"], g["connection"], me, "w")
    s5 = sessions.create(h, "demo", "owner-1", "stt", "north", "eu-north1", 600, 1)
    b5 = sessions.bind(s5["claim_token"], "stt")
    sessions.attach(s5["id"], b5["connection"], me, "w-5")
    monkeypatch.setattr(sessions, "_worker_gone", lambda s: False)
    with db.pool().connection() as c:
        c.execute("update sessions set deadline_at = now() - interval '1 second' where id = %s", (s5["id"],))
    assert f"{s5['id']}:cancel-requested" in sessions.sweep()
    assert sessions.get(s5["id"])["state"] == "attached" and sessions.get(s5["id"])["cancel_reason"] == "deadline"
    monkeypatch.setattr(sessions, "_worker_gone", lambda s: s["pod_uid"] == "uid-1")  # the exact pod is gone
    assert f"{s5['id']}:worker-lost" in sessions.sweep()
    assert sessions.get(s5["id"])["usage"] == "unknown" and _reservation(s5["id"]) is None
    with db.pool().connection() as c:  # a crash between the end and the settlement: the sweep settles once
        c.execute("update sessions set settled_at = null where id = %s", (s5["id"],))
    assert f"{s5['id']}:settled" in sessions.sweep() and sessions.sweep() == []


def _try(fn):
    try:
        return fn()
    except HTTPException as e:
        return e


def test_graph_declaration_keeps_the_stage_model_bindings(database):
    """Migration 15: the models a graph bound its stages to (docs/JOBS.md "Delegated stages") are sealed with the
    graph, idempotent for a replay, empty for a graph declared without bindings."""
    db.migrate()
    h = "a" * 64
    _bounded(h, budget_gpu_seconds=None)
    keys.admit(h, "root-m", "demo", run_member=True)
    keys.declare_graph("root-m", "demo", "owner-1", {"gpu": [], "collect": ["gpu"]}, None, {"gpu": "fold-gpu"})
    keys.declare_graph("root-m", "demo", "owner-1", {"gpu": []}, None, {"gpu": "other"})  # a replay: the first stands
    assert keys.graph_of("root-m")["models"] == {"gpu": "fold-gpu"}
    keys.declare_graph("root-n", "demo", "owner-1", {"a": []})
    assert keys.graph_of("root-n")["models"] == {}


def test_native_execution_lease_transitions_and_join(database, monkeypatch):
    """Native execution leases (migration 16; docs/API.md "Native execution leases"), the exact request-lifetime gaps:
    one live execution per operation (a retry attempt's POST refused while it lives; the same attempt replays; a new
    attempt gets its own row, the old evidence kept); attach fenced atomically against a closed row (the caller over,
    the join decided), a cancel before attach, a released reservation, a stale attempt, another pod; same-tuple
    idempotency for dropped attach/release responses; release only by the attached pod with its request id; the
    join never first-one-wins (attached -> deferred; released/lost -> settled, evidence summed, unknown stays
    unknown); second-owner fairness (the key's slot stays taken while attached); API restart changes nothing (rows)."""
    import leases

    db.migrate()
    h = "f" * 64
    _bounded(h, budget_gpu_seconds=None, max_concurrent=1, budget_requests=100)
    op, a1, a2 = "op-native-1", "op-native-1-abc", "op-native-1-def"
    keys.admit(h, op, "demo", run_member=True)  # the endpoint call's reservation (its slot on the key)
    me = {"pod": "cosmos-predictor-x", "namespace": "models", "uid": "uid-x"}
    other = {"pod": "cosmos-predictor-y", "namespace": "models", "uid": "uid-y"}
    with pytest.raises(HTTPException) as e:  # before the POST: nothing to attach to
        leases.attach(op, a1, me, "req-1")
    assert e.value.status_code == 409
    e1 = leases.prepare(op, a1, "eu-north1", "cosmos")
    assert e1["state"] == "prepared" and e1["attempt"] == a1
    assert leases.prepare(op, a1, "eu-north1", "cosmos")["attempt"] == a1  # the same attempt's POST again
    with pytest.raises(HTTPException) as e:  # a retry pod's POST while the first is prepared: refused at the hook
        leases.prepare(op, a2, "eu-north1", "cosmos")
    assert e.value.status_code == 409 and "waits" in e.value.detail
    with pytest.raises(HTTPException) as e:  # the hook read the first caller pod: alive -> still refused
        leases.prepare(op, a2, "eu-north1", "cosmos", caller_gone=lambda old: False)
    assert e.value.status_code == 409 and "waits" in e.value.detail
    # the first caller pod is gone (preempted before its worker attached): its prepared row is closed as replaced
    # and the replacement pod's POST prepares its own; the earlier evidence stays on record
    e2 = leases.prepare(op, a2, "eu-north1", "cosmos", caller_gone=lambda old: old == a1)
    assert e2["state"] == "prepared" and e2["attempt"] == a2
    assert [(h["attempt"], h["state"], h.get("outcome")) for h in leases.history(op)] == [
        (a1, "closed", "caller replaced"),
        (a2, "prepared", None),
    ]
    # back to the first attempt for the rest of the scenario: its own row is terminal now, so it posts no new one
    with pytest.raises(HTTPException) as e:
        leases.attach(op, a1, me, "req-1")
    assert e.value.status_code == 409
    a1, a2 = a2, "op-native-1-ghi"
    with pytest.raises(HTTPException) as e:  # a stale attempt cannot attach
        leases.attach(op, a2, me, "req-x")
    assert e.value.status_code == 409 and "stale" in e.value.detail
    att = leases.attach(op, a1, me, "req-1")
    assert att["state"] == "attached" and att["pod_uid"] == "uid-x"
    assert leases.attach(op, a1, me, "req-1")["state"] == "attached"  # dropped attach response: same tuple
    for who, rid in ((other, "req-1"), (me, "req-2")):  # another pod, or another request id: no second attach
        with pytest.raises(HTTPException):
            leases.attach(op, a1, who, rid)
    with pytest.raises(HTTPException) as e:  # still no overlapping attempt while attached
        leases.prepare(op, a2, "eu-north1", "cosmos")
    assert e.value.status_code == 409
    # second-owner fairness: the key's slot is the attached execution's until the join
    with pytest.raises(keys.KeyLimit) as e:
        keys.admit(h, "op-other", "demo", run_member=True)
    assert e.value.status == 429
    # the join while attached: the caller may be over, the member is not settled, whatever the caller did
    assert leases.settle_decision(op, required=True) == (
        False,
        None,
        "native execution attached: the worker has not settled",
    )
    leases.request_cancel(op, "cancelled by the owner")
    assert leases.get(op)["cancel_requested_at"] is not None and leases.get(op)["state"] == "attached"
    with pytest.raises(HTTPException):  # only the attached pod, with its request id, releases
        leases.release(op, a1, other, "req-1", "done", 1.0)
    with pytest.raises(HTTPException):
        leases.release(op, a1, me, "req-9", "done", 1.0)
    with pytest.raises(HTTPException):  # a stale same-pod release names another attempt: refused
        leases.release(op, a2, me, "req-1", "done", 1.0)
    rel = leases.release(op, a1, me, "req-1", "cancelled", None)  # cooperative abort, no measurement: unknown
    assert rel["state"] == "released" and rel["gpu_seconds"] is None and rel["lane_seconds"] is not None
    assert leases.release(op, a1, me, "req-1", "cancelled", None)["state"] == "released"  # dropped response
    # the caller is not over yet (its retry pod posts again): the worker settled, so the retry attempt gets its own
    # row; the first attempt's evidence stays
    e2 = leases.prepare(op, a2, "eu-north1", "cosmos")
    assert e2["state"] == "prepared" and e2["attempt"] == a2 and e2["pod_uid"] is None
    assert [x["attempt"] for x in leases.history(op)] == ["op-native-1-abc", a1, a2]  # the replaced row stays
    leases.attach(op, a2, me, "req-2")
    assert leases.release(op, a2, me, "req-2", "done", 12.5)["gpu_seconds"] == 12.5
    # the caller finished: the join sums the evidence; one unknown attempt makes the whole unknown
    assert leases.settle_decision(op, required=False) == (True, None, "released, usage unknown")
    assert leases.settle_decision(op, required=True) == (True, None, "released, usage unknown")  # idempotent
    assert keys.release(op, None) is True  # the join settles the member unknown; the slot is free again
    leases.mark_settled(op)
    assert all(x["settled_at"] is not None for x in leases.history(op))
    assert keys.admit(h, "op-other", "demo", run_member=True)["operation"] == "op-other"
    keys.release("op-other", 0.0)
    # evidence summed when every attempt measured
    opm = "op-native-m"
    keys.admit(h, opm, "demo", run_member=True)
    leases.prepare(opm, f"{opm}-a", "eu-north1", "cosmos")
    leases.attach(opm, f"{opm}-a", me, "r")
    leases.release(opm, f"{opm}-a", me, "r", "error", 10.0)
    leases.prepare(opm, f"{opm}-b", "eu-north1", "cosmos")
    leases.attach(opm, f"{opm}-b", me, "r2")
    leases.release(opm, f"{opm}-b", me, "r2", "done", 5.5)
    assert leases.settle_decision(opm, required=True) == (True, 15.5, "released (error, done)")
    keys.release(opm, 15.5)
    # caller terminal + delayed attach: the join closes the prepared row atomically, the late attach is refused
    opc = "op-native-c"
    keys.admit(h, opc, "demo", run_member=True)
    leases.prepare(opc, f"{opc}-a", "eu-north1", "cosmos")
    assert leases.settle_decision(opc, required=True) == (True, None, "unattached")
    assert leases.get(opc)["state"] == "closed"
    with pytest.raises(HTTPException) as e:
        leases.attach(opc, f"{opc}-a", me, "late")
    assert e.value.status_code == 409 and "fenced" in e.value.detail
    # the gap between the join's decision and the ledger release: the reservation is still live here, yet a prepare
    # for a different (or a never-seen) attempt and its attach are refused by the durable fence, before and after the
    # release, and after a restart (the fence is a row)
    assert keys.reservation_key(opc) == h
    for attempt in (f"{opc}-b", f"{opc}-zzz"):
        with pytest.raises(HTTPException) as e:
            leases.prepare(opc, attempt, "eu-north1", "cosmos")
        assert e.value.status_code == 409 and "over" in e.value.detail
        with pytest.raises(HTTPException) as e:
            leases.attach(opc, attempt, me, "late")
        assert e.value.status_code == 409
    keys.release(opc, None)
    with pytest.raises(HTTPException):
        leases.prepare(opc, f"{opc}-c", "eu-north1", "cosmos")
    assert leases.settle_decision(opc, required=True) == (True, None, "unattached")  # the join's retry: unchanged
    # a never-prepared operation whose caller finished: the join fences it; a later first POST cannot open anything
    opn = "op-native-n"
    keys.admit(h, opn, "demo", run_member=True)
    assert leases.settle_decision(opn, required=False) == (True, 0.0, "no native execution")
    with pytest.raises(HTTPException) as e:
        leases.prepare(opn, f"{opn}-a", "eu-north1", "cosmos")
    assert e.value.status_code == 409
    keys.release(opn, 0.0)
    # cancel before attach: refused
    opk = "op-native-k"
    keys.admit(h, opk, "demo", run_member=True)
    leases.prepare(opk, f"{opk}-a", "eu-north1", "cosmos")
    leases.request_cancel(opk, "cancelled by the owner")
    with pytest.raises(HTTPException) as e:
        leases.attach(opk, f"{opk}-a", me, "r")
    assert e.value.status_code == 409 and "cancelled before" in e.value.detail
    assert leases.settle_decision(opk, required=False) == (True, 0.0, "no native execution")
    keys.release(opk, 0.0)
    # a released reservation (settled by other means): attach refused
    opr = "op-native-r"
    keys.admit(h, opr, "demo", run_member=True)
    leases.prepare(opr, f"{opr}-a", "eu-north1", "cosmos")
    keys.release(opr, 0.0)
    with pytest.raises(HTTPException) as e:
        leases.attach(opr, f"{opr}-a", me, "r")
    assert e.value.status_code == 409 and "reservation is released" in e.value.detail
    # lost: the sweep ends an attached execution only when the pod by exact UID is gone or finished
    op2 = "op-native-2"
    keys.admit(h, op2, "demo", run_member=True)
    leases.prepare(op2, f"{op2}-a", "eu-north1", "cosmos")
    leases.attach(op2, f"{op2}-a", me, "r")
    monkeypatch.setattr(leases, "_worker_gone", lambda e: (_ for _ in ()).throw(RuntimeError("api unavailable")))
    assert leases.sweep() == [] and leases.get(op2)["state"] == "attached"  # a failed lookup is not a loss
    monkeypatch.setattr(leases, "_worker_gone", lambda e: False)
    assert leases.sweep() == [] and leases.get(op2)["state"] == "attached"
    monkeypatch.setattr(
        leases, "_worker_gone", lambda e: "worker container restarted (0 -> 1)" if e["pod_uid"] == "uid-x" else None
    )
    assert leases.sweep() == [f"{op2}:lost"]
    assert leases.settle_decision(op2, required=False) == (True, None, "worker lost")
    keys.release(op2, None)
    # required mode, nothing ever prepared: unknown; optional: the caller's own
    assert leases.settle_decision("op-never", required=True) == (True, None, "unattached")
    assert leases.settle_decision("op-never", required=False) == (True, 0.0, "no native execution")


def test_admission_records_survive_and_rebuild_the_job(database):
    import admissions

    db.migrate()

    job = {
        "metadata": {
            "name": "op-adm-1",
            "labels": {
                "serverless2.nebius/mode": "run",
                "serverless2.nebius/tenant": "demo",
                "serverless2.nebius/model": "m",
            },
            "annotations": {"serverless2.nebius/input": '{"x": 1}', "serverless2.nebius/owner": "p1"},
        },
        "spec": {
            "activeDeadlineSeconds": 1200,
            "template": {
                "spec": {
                    "containers": [
                        {"name": "uploader", "env": [{"name": "OUTPUT_PREFIX", "value": "s3://b/operations/op-adm-1"}]}
                    ]
                }
            },
        },
    }
    admissions.record(job, "eu-north1", jobs.output_prefix(job))
    admissions.record(job, "eu-north1", jobs.output_prefix(job))  # idempotent
    rec = admissions.get("op-adm-1")
    assert rec["tenant"] == "demo" and rec["region"] == "eu-north1" and rec["timeout_s"] == 1200
    assert (
        rec["output_prefix"] == "s3://b/operations/op-adm-1" and rec["annotations"]["serverless2.nebius/owner"] == "p1"
    )
    again = admissions.as_job(rec)
    assert again["_expired"] and again["metadata"]["labels"]["serverless2.nebius/model"] == "m"
    assert jobs.output_prefix(again) == "s3://b/operations/op-adm-1" and again["spec"]["activeDeadlineSeconds"] == 1200
    assert admissions.get("op-nope") is None
    # an endpoint call (mode async) leaves no record
    admissions.record(
        {**job, "metadata": {**job["metadata"], "name": "op-adm-2", "labels": {"serverless2.nebius/mode": "async"}}},
        "eu-north1",
        None,
    )
    assert admissions.get("op-adm-2") is None
