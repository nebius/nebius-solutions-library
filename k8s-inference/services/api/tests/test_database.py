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
