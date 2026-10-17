"""The fleet database (Nebius Managed PostgreSQL, database `platform`): model definitions with their history,
API keys and the GPU charge ledger (services/api/keys.py).

Only the control API has `DATABASE_URL` (the platform stage puts it in Secret api/database); it is the single
writer. Regional APIs have no database: they serve the read-only copies the control API writes into their
cluster (services/api/models.py). The schema is the baseline below plus the versioned migrations after it, applied at startup (`migrate`).
"""

import json
import os
import threading
from contextlib import contextmanager

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "")

BASELINE_VERSION = 30  # the schema below, as the thirty incremental migrations of releases up to 0.11.131 left it
BASELINE = """
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
-- the durable desired-state queue of the model reconciler (reconciliation.py)
create table if not exists model_changes (
    id text primary key, version int not null, entry jsonb, previous jsonb not null,
    error text, updated_at timestamptz not null default now()
);
-- the attempt lifecycle of runs (attempts.py): one row per pod of an operation
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
    checkpoint_generation int,
    role        text not null default 'run',
    cluster     text,
    mode        text,
    model       text,
    io_mode     text,
    gang_rank   int,
    gang_restart int,
    pod_uid     text,
    capture     jsonb,
    resume_checkpoint_ref text,
    resume_source_epoch int,
    resume_source text,
    primary key (operation, pod)
);
create index if not exists attempts_root on attempts (root);
create index if not exists attempts_gang on attempts (operation, gang_restart);
-- the cancel and successor marks of an operation (the completed_* columns are unused: retained from an earlier schema)
create table if not exists operation_marks (
    operation   text primary key,
    cancelled_at timestamptz,
    revision    bigserial,
    successor   text,
    completed_at timestamptz,
    completed_pod text,
    completed_pod_uid text,
    completed_epoch int,
    completed_result_ref text,
    completed_job_uid text,
    completion_reason text
);
-- API keys and the GPU charge ledger (keys.py): only the sha256 of a key value is stored
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
    scopes      jsonb not null default '[]',
    max_concurrent integer,
    budget_requests numeric,
    budget_gpu_seconds numeric,
    spent_requests numeric not null default 0,
    spent_gpu_seconds numeric not null default 0,
    rate_limit  jsonb,
    unique (tenant, alias)
);
create table if not exists charges (
    operation   text primary key,
    key_hash    text not null,
    gpu_seconds double precision not null,
    usd         numeric not null,
    at          timestamptz not null default now()
);
-- a key's reservations (slot, request, GPU seconds) until the attempt is accounted; key_calls counts the rate window
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
-- private operation ownership outlives Jobs, reservations and key revocation
create table if not exists operation_owners (
    operation text primary key,
    tenant text not null,
    owner text not null,
    model text not null,
    created_at timestamptz not null default now()
);
create index if not exists operation_owners_tenant_owner on operation_owners (tenant, owner);
-- LiteLLM records of revoked or replaced key values whose deletion is still owed (keys.retire_mirrors)
create table if not exists retired_mirrors (
    hash       text primary key,
    retired_at timestamptz not null default now()
);
-- the members of a logical run under the root's one reservation (keys.prepare_run_member / settle_run_member);
-- retire_marked_at is unused: retained from an earlier schema
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
    settled_at  timestamptz,
    kind        text not null default 'continuation',
    stage       text,
    note        text,
    retire_marked_at timestamptz
);
create index if not exists key_run_members_root on key_run_members (root, created_at);
create index if not exists key_run_members_key_state on key_run_members (key_hash, state);
-- stage graphs (docs/JOBS.md "Stage composition"): what a root declared at submission, and the stage -> model binding
create table if not exists run_graphs (
    root         text primary key,
    tenant       text not null,
    owner        text not null,
    stages       jsonb not null,
    deadline_at  timestamptz,
    sealed_at    timestamptz not null default now(),
    cancelled_at timestamptz,
    closed_at    timestamptz,
    reason       text,
    models       jsonb not null default '{}'::jsonb
);
-- live sessions (docs/API.md "Sessions"): one admitted stream = one claim, one connection, one worker pod
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
    settled_at          timestamptz,
    ready_seen_at       timestamptz,
    attach_deadline_at  timestamptz,
    idempotency_key     text,
    request_hash        text
);
create index if not exists sessions_open on sessions (state) where state <> 'ended';
create index if not exists sessions_owner on sessions (tenant, owner, created_at desc);
create unique index if not exists sessions_idempotency on sessions (tenant, owner, idempotency_key)
    where idempotency_key is not null;
-- native execution leases (docs/API.md "Native execution leases") and their durable terminal fence
create table if not exists native_executions (
    operation           text not null,
    attempt             text not null,
    state               text not null default 'prepared',
    pod                 text,
    pod_namespace       text,
    pod_uid             text,
    worker_request_id   text,
    region              text not null,
    model               text not null,
    prepared_at         timestamptz not null default now(),
    attached_at         timestamptz,
    released_at         timestamptz,
    outcome             text,
    gpu_seconds         numeric,
    lane_seconds        numeric,
    cancel_requested_at timestamptz,
    cancel_reason       text,
    settled_at          timestamptz,
    restart_count       integer,
    lost_reason         text,
    primary key (operation, attempt)
);
create index if not exists native_executions_attached on native_executions (state) where state = 'attached';
create table if not exists native_fences (
    operation text primary key,
    closed_at timestamptz not null default now(),
    reason    text
);
-- the admission record of a run (docs/JOBS.md "Resume"): what a resume needs past the Job's TTL
create table if not exists run_admissions (
    operation     text primary key,
    tenant        text not null,
    region        text,
    labels        jsonb not null default '{}'::jsonb,
    annotations   jsonb not null default '{}'::jsonb,
    timeout_s     integer,
    output_prefix text,
    managed_by    text,
    created_at    timestamptz not null default now()
);
-- endpoint children of a run (docs/JOBS.md "Endpoint children"): durable intents, one open child per root
create table if not exists endpoint_children (
    root             text not null,
    unit_id          text not null,
    child            text not null unique,
    parent           text not null,
    parent_pod       text not null,
    parent_pod_uid   text not null,
    model            text not null,
    request_sha256   text not null,
    state            text not null default 'active',
    created_at       timestamptz not null default now(),
    ended_at         timestamptz,
    namespace        text,
    region           text,
    timeout_s        integer,
    request          jsonb,
    cancel_requested_at timestamptz,
    reason           text,
    transport        jsonb,
    model_image      text,
    binding_ref      text,
    delivery         text not null default 'platform',
    incarnation      int not null default 1,
    previous_child   text,
    primary key (root, unit_id)
);
create unique index if not exists endpoint_children_one_open on endpoint_children (root) where state in ('pending', 'active');
-- fleet-wide settings the fleet operator writes and every API replica reads (pause.py)
create table if not exists fleet_settings (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);
"""
# Migrations above the baseline, numbered from BASELINE_VERSION + 1 in list order: one SQL block each, applied once
# and recorded in schema_version. A new one is appended here; the baseline is never edited.
MIGRATIONS: list[str] = [
    # 31: one shape of a model definition (services/api/models.py `definition`): a row is its spec and the placement
    # frozen at its write (`deployments`); the chart-era `entry` projection goes. A pending change holds the row shape.
    """
    create function pg_temp.s2_placement(d jsonb) returns jsonb language sql immutable as $$
        select coalesce((select jsonb_object_agg(e.key, case when e.value ? 'ibDevices'
            then (e.value - 'ibDevices') || jsonb_build_object('ib_devices', e.value -> 'ibDevices') else e.value end)
            from jsonb_each(case when jsonb_typeof(d) = 'object' then d else '{}'::jsonb end) as e), '{}'::jsonb)
    $$;
    alter table models add column if not exists deployments jsonb not null default '{}'::jsonb;
    update models set deployments = pg_temp.s2_placement(entry -> 'deployments');
    alter table models drop column if exists entry;
    alter table models_history drop column if exists entry;
    update model_changes set
        entry = case when entry is null then null else jsonb_build_object(
            'spec', entry -> 'spec', 'managed_by', coalesce(entry -> 'managed_by', '"api"'::jsonb),
            'version', to_jsonb(version), 'deployments', pg_temp.s2_placement(entry -> 'deployments')) end,
        previous = case when previous ? 'spec' then jsonb_build_object(
            'spec', previous -> 'spec', 'managed_by', coalesce(previous -> 'managed_by', '"api"'::jsonb),
            'deployments', pg_temp.s2_placement(previous -> 'deployments')) else '{}'::jsonb end;
    drop function pg_temp.s2_placement(jsonb);
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
                _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, kwargs={"autocommit": True}, open=True)
    return _pool


def migrate() -> int:
    """Bring the database to this release's schema, under an advisory lock (API replicas start together); returns
    the schema version. A fresh database gets the baseline and is recorded at BASELINE_VERSION; a database at the
    baseline or above gets the migrations it misses; a database below the baseline (an earlier release's increments
    still pending) is refused: it is upgraded through the previous release first."""
    with pool().connection() as c:
        with c.transaction():
            c.execute("select pg_advisory_xact_lock(1229001)")
            c.execute(
                "create table if not exists schema_version (version int primary key, applied_at timestamptz not null default now())"
            )
            done = {r[0] for r in c.execute("select version from schema_version").fetchall()}
            if not done:
                c.execute(BASELINE)
                c.execute("insert into schema_version (version) values (%s)", (BASELINE_VERSION,))
            elif max(done) < BASELINE_VERSION:
                raise RuntimeError(
                    f"database schema version {max(done)} is below this release's baseline {BASELINE_VERSION}: "
                    "run the previous release once (it applies the remaining increments), then this one"
                )
            for i, sql in enumerate(MIGRATIONS, start=BASELINE_VERSION + 1):
                if i in done:
                    continue
                c.execute(sql)
                c.execute("insert into schema_version (version) values (%s)", (i,))
        return BASELINE_VERSION + len(MIGRATIONS)


COLS = "id, kind, spec, deployments, version, managed_by, created_by, created_at, updated_by, updated_at"
ROW = ("spec", "deployments", "managed_by", "version")  # what a pending change keeps of a row (models.load)


def _row(r) -> dict:
    d = dict(zip(COLS.split(", "), r))
    return {**d, **{k: d[k].isoformat() if d[k] else None for k in ("created_at", "updated_at")}}


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
        raise HTTPException(409, "model version changed; reload before saving")
    version = c.execute("select coalesce(max(version), 0) + 1 from models_history where id = %s", (mid,)).fetchone()[0]
    pending = c.execute("select previous from model_changes where id = %s", (mid,)).fetchone()
    previous = dict(pending[0]) if pending else {}
    if old:
        previous = {
            **{k: old[k] for k in ROW},
            "deployments": {**previous.get("deployments", {}), **(old["deployments"] or {})},
        }
        # A job class has no endpoint resources; retain the last endpoint spec for cleanup.
        if old["kind"] == "job" and pending:
            previous = pending[0]
    return version, previous


def _change(c, mid: str, version: int, row: dict | None, previous: dict):
    c.execute(
        """insert into model_changes (id, version, entry, previous) values (%s, %s, %s, %s)
        on conflict (id) do update set version = excluded.version, entry = excluded.entry,
        previous = excluded.previous, error = null, updated_at = now()""",
        (mid, version, json.dumps(row) if row is not None else None, json.dumps(previous)),
    )


def upsert_model(
    definition: dict,
    spec: dict,
    by: str | None,
    managed_by: str = "api",
    expected: int | None = None,
    create_only: bool = False,
) -> dict:
    """Insert or replace a model (`definition`: models.definition of `spec`, whose placement the row freezes); every
    write lands in models_history as well."""
    mid, kind = definition["id"], spec.get("kind", "endpoint")
    with pool().connection() as c, c.transaction():
        version, previous = _generation(c, mid, expected)
        exists = _get(c, mid) is not None
        if create_only and exists:
            raise HTTPException(409, f"model {mid} exists (PUT replaces it)")
        c.execute(
            """insert into models (id, kind, spec, deployments, version, managed_by, created_by, updated_by, updated_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, now())
               on conflict (id) do update set kind = excluded.kind, spec = excluded.spec, deployments = excluded.deployments,
                 version = excluded.version, managed_by = excluded.managed_by, updated_by = excluded.updated_by, updated_at = now()""",
            (mid, kind, json.dumps(spec), json.dumps(definition["deployments"]), version, managed_by, by, by),
        )
        c.execute(
            "insert into models_history (id, version, action, spec, by) values (%s, %s, %s, %s, %s)",
            (mid, version, "update" if exists else "create", json.dumps(spec), by),
        )
        row = _get(c, mid)  # the same connection: the row is not committed yet
        _change(c, mid, version, {k: row[k] for k in ROW}, previous)
        return row


def delete_model(mid: str, by: str | None, expected: int | None = None) -> bool:
    with pool().connection() as c, c.transaction():
        version, previous = _generation(c, mid, expected)
        r = c.execute("select version, spec from models where id = %s for update", (mid,)).fetchone()
        if not r:
            return False
        c.execute(
            "insert into models_history (id, version, action, spec, by) values (%s, %s, 'delete', %s, %s)",
            (mid, version, json.dumps(r[1]), by),
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
