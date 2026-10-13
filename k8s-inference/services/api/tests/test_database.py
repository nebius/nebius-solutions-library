"""Real transactions, using an ephemeral PostgreSQL service (never a cloud fleet).

Set TEST_DATABASE_URL to run these tests. CI provides PostgreSQL; each test uses its own schema.
"""
import os
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool
import pytest
from fastapi import HTTPException

import db
import billing
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
            monkeypatch.setattr(keys, "LITELLM_MASTER_KEY", "")    # no mirror: LiteLLM is not part of these tests
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
    assert [(h["version"], h["action"]) for h in db.history("test-model")] == [(3, "create"), (2, "delete"), (1, "create")]


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
    value, i = keys.create("demo", "demo-ci", 5, ["llm-example"], 7, metadata={"allowed_passthrough_routes": ["/models/llm/chat"]}, by="demo-admin")
    assert value.startswith("sk-") and len(value) == 43 and i["key_name"] == "sk-..." + value[-4:]
    assert keys.info(keys.key_hash(value)) == i and i["max_budget"] == 5.0 and i["spend"] == 0 and i["models"] == ["llm-example"]
    assert i["metadata"] == {"tenant": "demo", "role": "user", "allowed_passthrough_routes": ["/models/llm/chat"]} and i["expires"]
    with pytest.raises(HTTPException, match="409"):
        keys.create("demo", "demo-ci", 1)                                             # alias unique per tenant ...
    keys.create("other", "demo-ci", 1)                                                # ... not across tenants
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
        assert c.execute("select key_hash, gpu_seconds from charges where operation = 'hub/job-uid'").fetchone() == (h, 3600)


def test_concurrent_charges_and_admin_edits_do_not_lose_spend(database):
    db.migrate()
    value, _ = keys.create("demo", "demo-k", 10)
    h = keys.key_hash(value)
    def charge(i):
        if i == 0:
            keys.update("demo", "demo-k", budget=20)            # an admin edit in the middle: never touches spend
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


