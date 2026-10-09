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
            monkeypatch.setenv("BILLING_DATABASE_URL", url)
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


def native_key(database, token="token-hash"):
    # Pinned LiteLLM 1.104.0 schema: token is the PK; spend/total_spend are double precision.
    # This exercises the narrow schema contract the GPU ledger uses, without a proxy dependency.
    with psycopg.connect(database) as c:
        c.execute('create table "LiteLLM_VerificationToken" (token text primary key, spend double precision not null default 0, total_spend double precision not null default 0)')
        c.execute('insert into "LiteLLM_VerificationToken" (token) values (%s)', (token,))


def test_charge_retry_after_annotation_failure_is_idempotent(database):
    native_key(database)
    assert billing.record_spend("token-hash", 1.5, "hub/job-uid", 3600) == 1.5
    assert billing.record_spend("token-hash", 1.5, "hub/job-uid", 3600) == 1.5
    with psycopg.connect(database) as c:
        assert c.execute('select spend, total_spend from "LiteLLM_VerificationToken"').fetchone() == (1.5, 1.5)
        assert c.execute("select count(*) from serverless_gpu_charges").fetchone()[0] == 1


def test_concurrent_charges_and_native_proxy_increment_do_not_lose_spend(database):
    native_key(database)
    def charge(i):
        if i == 0:
            with psycopg.connect(database) as c:
                c.execute('update "LiteLLM_VerificationToken" set spend=spend+2, total_spend=total_spend+2')
        else:
            billing.record_spend("token-hash", 1, "hub/job-" + str(i), 100)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(charge, range(12)))
    with psycopg.connect(database) as c:
        assert c.execute('select spend, total_spend from "LiteLLM_VerificationToken"').fetchone() == (13, 13)


def test_missing_key_rolls_back_receipt_and_can_retry(database):
    native_key(database)
    billing.record_spend("token-hash", 0, "initial", 0)
    with pytest.raises(ValueError):
        billing.record_spend("missing-key", 1, "hub/retry", 100)
    with psycopg.connect(database) as c:
        assert c.execute("select count(*) from serverless_gpu_charges where operation='hub/retry'").fetchone()[0] == 0
        c.execute('insert into "LiteLLM_VerificationToken" (token) values (\'missing-key\')')
    assert billing.record_spend("missing-key", 1, "hub/retry", 100) == 1


def test_receipt_cannot_be_reassigned_to_another_key(database):
    native_key(database)
    billing.record_spend("token-hash", 1, "hub/uid", 100)
    with pytest.raises(ValueError):
        billing.record_spend("other-key", 1, "hub/uid", 100)
