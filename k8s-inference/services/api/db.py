"""The fleet database (Nebius Managed PostgreSQL, database `platform`): model definitions with their history.

Only the control API has `DATABASE_URL` (the platform stage puts it in Secret api/database); it is the single
writer. Regional APIs have no database: they serve the read-only copies the control API writes into their
cluster (services/api/models.py). Schema changes are versioned below and applied at startup (`migrate`).
"""
import json, os, threading
from contextlib import contextmanager
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "")

MIGRATIONS = [
    # 1: model definitions and their history
    """
    create table if not exists schema_version (version int primary key, applied_at timestamptz not null default now());
    create table if not exists models (
        id          text primary key,
        kind        text not null,
        spec        jsonb not null,
        entry       jsonb not null,
        version     int not null default 1,
        managed_by  text not null default 'api',
        created_by  text,
        created_at  timestamptz not null default now(),
        updated_by  text,
        updated_at  timestamptz not null default now()
    );
    create table if not exists models_history (
        id          text not null,
        version     int not null,
        action      text not null,
        spec        jsonb,
        entry       jsonb,
        by          text,
        at          timestamptz not null default now(),
        primary key (id, version, action)
    );
    """,
    # 2: a durable desired-state queue. New writes retain all deployments needing cleanup.
    """
    create table if not exists model_changes (
        id text primary key, version int not null, entry jsonb, previous jsonb not null,
        error text, updated_at timestamptz not null default now()
    );
    """,
]

_pool = None
_lock = threading.Lock()


def enabled() -> bool:
    return bool(DATABASE_URL)


def pool():
    """A small connection pool, created on first use (psycopg 3)."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, kwargs={"autocommit": True}, open=True)
    return _pool


def migrate() -> int:
    """Apply the migrations that are not applied yet; returns the schema version."""
    with pool().connection() as c:
        with c.transaction():
            c.execute("select pg_advisory_xact_lock(1229001)")
            c.execute("create table if not exists schema_version (version int primary key, applied_at timestamptz not null default now())")
            done = {r[0] for r in c.execute("select version from schema_version").fetchall()}
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i in done:
                    continue
                c.execute(sql)
                c.execute("insert into schema_version (version) values (%s)", (i,))
        return len(MIGRATIONS)


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "spec": r[2], "entry": r[3], "version": r[4], "managed_by": r[5],
            "created_by": r[6], "created_at": r[7].isoformat() if r[7] else None, "updated_by": r[8], "updated_at": r[9].isoformat() if r[9] else None}


COLS = "id, kind, spec, entry, version, managed_by, created_by, created_at, updated_by, updated_at"


def list_models() -> dict[str, dict]:
    with pool().connection() as c:
        return {r[0]: _row(r) for r in c.execute(f"select {COLS} from models order by id").fetchall()}


def _get(c, mid: str) -> dict | None:
    r = c.execute(f"select {COLS} from models where id = %s", (mid,)).fetchone()
    return _row(r) if r else None


def get_model(mid: str) -> dict | None:
    with pool().connection() as c:
        return _get(c, mid)


def _generation(c, mid: str, expected: int | None = None) -> tuple[int, dict]:
    c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (mid,))
    old = _get(c, mid)
    if expected is not None and (old or {}).get("version") != expected:
        from fastapi import HTTPException
        raise HTTPException(409, "model version changed; reload before saving")
    version = c.execute("select coalesce(max(version), 0) + 1 from models_history where id = %s", (mid,)).fetchone()[0]
    pending = c.execute("select previous from model_changes where id = %s", (mid,)).fetchone()
    previous = dict(pending[0]) if pending else {}
    if old:
        previous = {**old["entry"], "deployments": {**previous.get("deployments", {}), **old["entry"].get("deployments", {})}}
        # A job class has no endpoint resources; retain the last endpoint spec for cleanup.
        if old["entry"].get("mode") == "run" and pending:
            previous = pending[0]
    return version, previous


def _change(c, mid: str, version: int, entry: dict | None, previous: dict):
    c.execute("""insert into model_changes (id, version, entry, previous) values (%s, %s, %s, %s)
        on conflict (id) do update set version = excluded.version, entry = excluded.entry,
        previous = excluded.previous, error = null, updated_at = now()""",
        (mid, version, json.dumps(entry) if entry is not None else None, json.dumps(previous)))


def upsert_model(entry: dict, spec: dict, by: str | None, managed_by: str = "api", expected: int | None = None, create_only: bool = False) -> dict:
    """Insert or replace a model; every write lands in models_history as well."""
    mid, kind = entry["id"], spec.get("kind", "endpoint")
    with pool().connection() as c, c.transaction():
        version, previous = _generation(c, mid, expected)
        exists = _get(c, mid) is not None
        if create_only and exists:
            from fastapi import HTTPException
            raise HTTPException(409, f"model {mid} exists (PUT replaces it)")
        c.execute(
            """insert into models (id, kind, spec, entry, version, managed_by, created_by, updated_by, updated_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, now())
               on conflict (id) do update set kind = excluded.kind, spec = excluded.spec, entry = excluded.entry,
                 version = excluded.version, managed_by = excluded.managed_by, updated_by = excluded.updated_by, updated_at = now()""",
            (mid, kind, json.dumps(spec), json.dumps(entry), version, managed_by, by, by))
        c.execute("insert into models_history (id, version, action, spec, entry, by) values (%s, %s, %s, %s, %s, %s)",
                  (mid, version, "update" if exists else "create", json.dumps(spec), json.dumps(entry), by))
        _change(c, mid, version, entry, previous)
        return _get(c, mid)   # the same connection: the row is not committed yet


def delete_model(mid: str, by: str | None, expected: int | None = None) -> bool:
    with pool().connection() as c, c.transaction():
        version, previous = _generation(c, mid, expected)
        r = c.execute("select version, spec, entry from models where id = %s for update", (mid,)).fetchone()
        if not r:
            return False
        c.execute("insert into models_history (id, version, action, spec, entry, by) values (%s, %s, 'delete', %s, %s, %s)",
                  (mid, version, json.dumps(r[1]), json.dumps(r[2]), by))
        c.execute("delete from models where id = %s", (mid,))
        _change(c, mid, version, None, previous)
        return True


@contextmanager
def reconciler():
    """One reconciler across API replicas; writes stay short database transactions."""
    with pool().connection() as c:
        acquired = c.execute("select pg_try_advisory_lock(1229002)").fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired:
                c.execute("select pg_advisory_unlock(1229002)")


def requeue_all() -> int:
    """Queue a no-op change for every model without a pending one: the reconciler re-renders each endpoint and run
    class once (server-side apply, idempotent), so objects this release renders and an older one did not (the
    per-endpoint SecurityPolicy, for one) exist after an upgrade without an admin re-saving every model.
    Called once at API startup; returns the number of rows queued."""
    with pool().connection() as c, c.transaction():
        rows = c.execute("""select m.id, m.version, m.entry from models m
                            where not exists (select 1 from model_changes ch where ch.id = m.id)""").fetchall()
        for mid, version, entry in rows:
            _change(c, mid, version, entry, entry)
        return len(rows)


def pending_changes() -> list[dict]:
    with pool().connection() as c:
        return [dict(zip(("id", "version", "entry", "previous", "error"), r))
                for r in c.execute("select id, version, entry, previous, error from model_changes order by updated_at").fetchall()]


def finish_change(mid: str, version: int, error: str | None = None):
    with pool().connection() as c:
        if error:
            c.execute("update model_changes set error = %s where id = %s and version = %s", (error, mid, version))
        else:
            c.execute("delete from model_changes where id = %s and version = %s", (mid, version))


def history(mid: str, limit: int = 50) -> list[dict]:
    with pool().connection() as c:
        rows = c.execute("select version, action, by, at, spec from models_history where id = %s order by version desc, at desc limit %s", (mid, limit)).fetchall()
        return [{"version": r[0], "action": r[1], "by": r[2], "at": r[3].isoformat(), "spec": r[4]} for r in rows]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
