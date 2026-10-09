"""The fleet database (Nebius Managed PostgreSQL, database `platform`): model definitions with their history.

Only the control API has `DATABASE_URL` (the platform stage puts it in Secret api/database); it is the single
writer. Regional APIs have no database: they serve the read-only copies the control API writes into their
cluster (services/api/models.py). Schema changes are versioned below and applied at startup (`migrate`).
"""
import json, os, threading
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
        c.execute("create table if not exists schema_version (version int primary key, applied_at timestamptz not null default now())")
        done = {r[0] for r in c.execute("select version from schema_version").fetchall()}
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i in done:
                continue
            with c.transaction():
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


def upsert_model(entry: dict, spec: dict, by: str | None, managed_by: str = "api") -> dict:
    """Insert or replace a model; every write lands in models_history as well."""
    mid, kind = entry["id"], spec.get("kind", "endpoint")
    with pool().connection() as c, c.transaction():
        cur = c.execute("select version from models where id = %s for update", (mid,)).fetchone()
        version = (cur[0] + 1) if cur else 1
        c.execute(
            """insert into models (id, kind, spec, entry, version, managed_by, created_by, updated_by, updated_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, now())
               on conflict (id) do update set kind = excluded.kind, spec = excluded.spec, entry = excluded.entry,
                 version = excluded.version, managed_by = excluded.managed_by, updated_by = excluded.updated_by, updated_at = now()""",
            (mid, kind, json.dumps(spec), json.dumps(entry), version, managed_by, by, by))
        c.execute("insert into models_history (id, version, action, spec, entry, by) values (%s, %s, %s, %s, %s, %s)",
                  (mid, version, "create" if version == 1 else "update", json.dumps(spec), json.dumps(entry), by))
        return _get(c, mid)   # the same connection: the row is not committed yet


def delete_model(mid: str, by: str | None) -> bool:
    with pool().connection() as c, c.transaction():
        r = c.execute("select version, spec, entry from models where id = %s for update", (mid,)).fetchone()
        if not r:
            return False
        c.execute("insert into models_history (id, version, action, spec, entry, by) values (%s, %s, 'delete', %s, %s, %s)",
                  (mid, r[0] + 1, json.dumps(r[1]), json.dumps(r[2]), by))
        c.execute("delete from models where id = %s", (mid,))
        return True


def history(mid: str, limit: int = 50) -> list[dict]:
    with pool().connection() as c:
        rows = c.execute("select version, action, by, at, spec from models_history where id = %s order by version desc, at desc limit %s", (mid, limit)).fetchall()
        return [{"version": r[0], "action": r[1], "by": r[2], "at": r[3].isoformat(), "spec": r[4]} for r in rows]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
