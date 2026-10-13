"""The fleet database (Nebius Managed PostgreSQL, database `platform`): model definitions with their history,
API keys and the GPU charge ledger (services/api/keys.py).

Only the control API has `DATABASE_URL` (the platform stage puts it in Secret api/database); it is the single
writer. Regional APIs have no database: they serve the read-only copies the control API writes into their
cluster (services/api/models.py). Schema changes are versioned below and applied at startup (`migrate`).
"""

import json
import os
import threading
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
    # 3: the attempt lifecycle of runs (services/api/attempts.py)
    """
create table if not exists attempts (
    operation   text not null,
    pod         text not null,
    root        text not null,
    tenant      text not null,
    namespace   text not null,
    epoch       int not null,
    region      text,
    node        text,
    pool        text,
    gpu_class   text,
    gpus        int not null default 0,
    image       text,
    image_digest text,
    status      text not null,
    exit_code   int,
    started_at  timestamptz,
    ended_at    timestamptz,
    export_state text,
    result_ref  text,
    checkpoint_ref text,
    revision    bigserial,
    updated_at  timestamptz not null default now(),
    primary key (operation, pod)
);
create index if not exists attempts_root on attempts (root);
create table if not exists operation_marks (
    operation   text primary key,
    cancelled_at timestamptz,
    revision    bigserial
);
""",
    # 4: live checkpoint pointers and successor operations (capacity fallback keeps the original handle)
    """
    alter table attempts add column if not exists checkpoint_generation int;
    alter table operation_marks add column if not exists successor text;
    """,
    # 5: API keys and the GPU charge ledger (services/api/keys.py): the fleet database is the source of truth for a
    # key's tenant, role, budget, spend, models, expiry and block flag; only the sha256 of a key value is stored
    """
    create table if not exists keys (
        hash        text primary key,
        alias       text not null,
        tenant      text not null,
        role        text not null default 'user',
        models      jsonb not null default '[]',
        budget      numeric,
        spend       numeric not null default 0,
        expires     timestamptz,
        blocked     boolean not null default false,
        preview     text,
        metadata    jsonb not null default '{}',
        created_at  timestamptz not null default now(),
        created_by  text,
        updated_at  timestamptz not null default now(),
        unique (tenant, alias)
    );
    create table if not exists charges (
        operation   text primary key,
        key_hash    text not null,
        gpu_seconds double precision not null,
        usd         numeric not null,
        at          timestamptz not null default now()
    );
    """,
    # 6: the attempt's role (run, export, finalize): the platform's statement from the Job it rendered
    """
    alter table attempts add column if not exists role text not null default 'run';
    """,
    # 7: the canonical cluster id of an attempt (the fleet's configured id, as placement and identities key it);
    # `region` stays the Nebius region (two ids may share one region)
    """
    alter table attempts add column if not exists cluster text;
    """,
    # 8: per-key scopes and limits beyond the USD budget (docs/API.md "Keys"): a request-count budget, a GPU-second
    # budget, a concurrency limit and a rate limit in the key's own units; reservations hold a slot and the GPU seconds
    # a run may take until its attempt is accounted; key_calls counts requests per key for the rate window.
    """
    alter table keys add column if not exists scopes jsonb not null default '[]';
    alter table keys add column if not exists max_concurrent integer;
    alter table keys add column if not exists budget_requests numeric;
    alter table keys add column if not exists budget_gpu_seconds numeric;
    alter table keys add column if not exists spent_requests numeric not null default 0;
    alter table keys add column if not exists spent_gpu_seconds numeric not null default 0;
    alter table keys add column if not exists rate_limit jsonb;
    create table if not exists key_reservations (
        operation   text primary key,
        key_hash    text not null,
        tenant      text not null,
        gpu_seconds numeric not null default 0,
        requests    integer not null default 1,
        created_at  timestamptz not null default now()
    );
    create index if not exists key_reservations_key on key_reservations (key_hash);
    create table if not exists key_calls (
        key_hash text not null,
        at       timestamptz not null default now()
    );
    create index if not exists key_calls_key_at on key_calls (key_hash, at);
    """,
    # 9: private operation ownership outlives Jobs, reservations, and key revocation.
    # Namespace/storage sharing does not grant access to another principal's operations.
    """
    create table if not exists operation_owners (
        operation text primary key,
        tenant text not null,
        owner text not null,
        model text not null,
        created_at timestamptz not null default now()
    );
    create index if not exists operation_owners_tenant_owner on operation_owners (tenant, owner);
    """,
    # 10: LiteLLM records of revoked or replaced key values whose deletion is still owed (keys.retire_mirrors)
    """
    create table if not exists retired_mirrors (
        hash       text primary key,
        retired_at timestamptz not null default now()
    );
    """,
    # 11: the members of a logical run (the root and every continuation: resume, export retry, finalizer, fallback,
    # recovery) under the root's one reservation (keys.prepare_run_member / settle_run_member); a member's GPU bound
    # stays reserved while it is active, becomes spend when measured, and stays on record as `unknown` when nothing
    # trustworthy was measured (never zero by assumption)
    """
    create table if not exists key_run_members (
        operation   text primary key,
        root        text not null,
        predecessor text,
        key_hash    text not null,
        tenant      text not null,
        gpu_bound   numeric not null default 0,
        gpu_seconds numeric,
        state       text not null default 'active',
        created_at  timestamptz not null default now(),
        settled_at  timestamptz
    );
    create index if not exists key_run_members_root on key_run_members (root, created_at);
    create index if not exists key_run_members_key_state on key_run_members (key_hash, state);
    """,
    # 12: the provenance an operation keeps after its Job is gone (attempts.synthesize): its mode (run | async), its
    # model and its I/O mode, so the durable result path stays the operation's (application-owned output is never
    # read from the platform bucket) and the key's model allow-list still applies
    """
    alter table attempts add column if not exists mode text;
    alter table attempts add column if not exists model text;
    alter table attempts add column if not exists io_mode text;
    """,
    # 13: stage graphs (docs/JOBS.md "Stage composition"): the stages a root declared at submission (sealed: nothing
    # attaches later), and members of kind `stage` (a declared stage's operation, outside the continuation head rule)
    """
    create table if not exists run_graphs (
        root         text primary key,
        tenant       text not null,
        owner        text not null,
        stages       jsonb not null,
        deadline_at  timestamptz,
        sealed_at    timestamptz not null default now(),
        cancelled_at timestamptz,
        closed_at    timestamptz,
        reason       text
    );
    alter table key_run_members add column if not exists kind text not null default 'continuation';
    alter table key_run_members add column if not exists stage text;
    alter table key_run_members add column if not exists note text;
    """,
    # 14: live sessions (docs/API.md "Sessions"): one admitted stream = one claim, one connection, one worker pod
    """
    create table if not exists sessions (
        id                  text primary key,
        tenant              text not null,
        owner               text not null,
        key_hash            text not null,
        model               text not null,
        cluster             text,
        region              text not null,
        state               text not null default 'claimed',
        claim_hash          text not null,
        connection          text,
        pod                 text,
        pod_namespace       text,
        pod_uid             text,
        worker_session_id   text,
        gpu_bound           numeric not null default 0,
        created_at          timestamptz not null default now(),
        expires_at          timestamptz not null,
        deadline_at         timestamptz not null,
        bound_at            timestamptz,
        attached_at         timestamptz,
        cancel_requested_at timestamptz,
        cancel_reason       text,
        ended_at            timestamptz,
        outcome             text,
        gpu_seconds         numeric,
        usage               text,
        settled_at          timestamptz
    );
    create index if not exists sessions_open on sessions (state) where state <> 'ended';
    create index if not exists sessions_owner on sessions (tenant, owner, created_at desc);
    """,
    # 15: the model each declared stage is bound to (docs/JOBS.md "Delegated stages"): the operator's delegate list on
    # the root's run class, intersected with this, lets the root's owner submit a stage on a model its key is not granted
    """
    alter table run_graphs add column if not exists models jsonb not null default '{}'::jsonb;
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
            c.execute(
                "create table if not exists schema_version (version int primary key, applied_at timestamptz not null default now())"
            )
            done = {r[0] for r in c.execute("select version from schema_version").fetchall()}
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i in done:
                    continue
                c.execute(sql)
                c.execute("insert into schema_version (version) values (%s)", (i,))
        return len(MIGRATIONS)


def _row(r) -> dict:
    return {
        "id": r[0],
        "kind": r[1],
        "spec": r[2],
        "entry": r[3],
        "version": r[4],
        "managed_by": r[5],
        "created_by": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
        "updated_by": r[8],
        "updated_at": r[9].isoformat() if r[9] else None,
    }


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
        previous = {
            **old["entry"],
            "deployments": {**previous.get("deployments", {}), **old["entry"].get("deployments", {})},
        }
        # A job class has no endpoint resources; retain the last endpoint spec for cleanup.
        if old["entry"].get("mode") == "run" and pending:
            previous = pending[0]
    return version, previous


def _change(c, mid: str, version: int, entry: dict | None, previous: dict):
    c.execute(
        """insert into model_changes (id, version, entry, previous) values (%s, %s, %s, %s)
        on conflict (id) do update set version = excluded.version, entry = excluded.entry,
        previous = excluded.previous, error = null, updated_at = now()""",
        (mid, version, json.dumps(entry) if entry is not None else None, json.dumps(previous)),
    )


def upsert_model(
    entry: dict,
    spec: dict,
    by: str | None,
    managed_by: str = "api",
    expected: int | None = None,
    create_only: bool = False,
) -> dict:
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
            (mid, kind, json.dumps(spec), json.dumps(entry), version, managed_by, by, by),
        )
        c.execute(
            "insert into models_history (id, version, action, spec, entry, by) values (%s, %s, %s, %s, %s, %s)",
            (mid, version, "update" if exists else "create", json.dumps(spec), json.dumps(entry), by),
        )
        _change(c, mid, version, entry, previous)
        return _get(c, mid)  # the same connection: the row is not committed yet


def delete_model(mid: str, by: str | None, expected: int | None = None) -> bool:
    with pool().connection() as c, c.transaction():
        version, previous = _generation(c, mid, expected)
        r = c.execute("select version, spec, entry from models where id = %s for update", (mid,)).fetchone()
        if not r:
            return False
        c.execute(
            "insert into models_history (id, version, action, spec, entry, by) values (%s, %s, 'delete', %s, %s, %s)",
            (mid, version, json.dumps(r[1]), json.dumps(r[2]), by),
        )
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


def pending_changes() -> list[dict]:
    with pool().connection() as c:
        return [
            dict(zip(("id", "version", "entry", "previous", "error"), r))
            for r in c.execute(
                "select id, version, entry, previous, error from model_changes order by updated_at"
            ).fetchall()
        ]


def finish_change(mid: str, version: int, error: str | None = None):
    with pool().connection() as c:
        if error:
            c.execute("update model_changes set error = %s where id = %s and version = %s", (error, mid, version))
        else:
            c.execute("delete from model_changes where id = %s and version = %s", (mid, version))


def history(mid: str, limit: int = 50) -> list[dict]:
    with pool().connection() as c:
        rows = c.execute(
            "select version, action, by, at, spec from models_history where id = %s order by version desc, at desc limit %s",
            (mid, limit),
        ).fetchall()
        return [{"version": r[0], "action": r[1], "by": r[2], "at": r[3].isoformat(), "spec": r[4]} for r in rows]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
