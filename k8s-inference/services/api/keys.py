"""API keys and the GPU charge ledger in the fleet database (docs/API.md "Keys").

The `keys` table is the source of truth for a key's tenant, role, budget, spend, model allow-list, expiry and
block flag; only the sha256 of a key's value is stored (the value is shown once, at creation). LiteLLM keeps a
mirror of every tenant key (created with the same value, updated and deleted best effort) so the same key works
on its OpenAI route; the API never reads the mirror back. `charges` is the idempotent GPU ledger billing.py
writes: one row per finished run, the key's spend incremented in the same transaction.

Roles: `user`, `admin` (key management, model definitions) and `router`: the per-fleet key LiteLLM's
deployments call the gateway with (ROUTER_KEY, Secret api/router-key), accepted by the gateway's authorize
hook only, every model, no budget, never billed.

Per-key limits beyond the USD budget (migration 8): `scopes` (what a key may do; empty = everything), `max_concurrent`
(operations in flight), `budget_requests` and `budget_gpu_seconds` (remaining counts in their own units, never
converted), `rate_limit` ({requests, window_s}). `admit` reserves a slot, a request and the GPU seconds a run may take
in one transaction per key (`for update` serialises the API replicas); `release` accounts the measured value when the
attempt is billed. Imported keys (`import_records`, `POST /v1/keys:import`, `python -m keys import`) arrive pre-hashed:
no plaintext, no rotation, nothing overwritten, conflicts refused, unknown scopes block the key.

CLI: `python -m keys bootstrap` (KEY = the value, KEY_SPEC = JSON {alias, tenant, role, budget, models, routes})
creates or updates one key straight in the database: the platform stage runs it once per tenant key of
terraform.tfvars, before any admin key exists that could mint through the API. `python -m keys import <file.json>`
imports pre-hashed keys ({"tenant": ..., "dry_run": bool, "keys": [...]}) for an operator with database access.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import string
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import db
from config import LITELLM_MASTER_KEY, LITELLM_URL

log = logging.getLogger("keys")
ROUTER = {"tenant": "platform", "alias": "router", "role": "router"}
COLS = (
    "hash, alias, tenant, role, models, budget, spend, expires, blocked, preview, metadata, created_at, created_by, updated_at, "
    "scopes, max_concurrent, budget_requests, budget_gpu_seconds, spent_requests, spent_gpu_seconds, rate_limit"
)
# what a key may do; an empty list on a key means everything (keys created before scopes existed, Terraform's keys)
SCOPES = (
    "invoke:sync",
    "invoke:async",
    "run:submit",
    "run:control",
    "read:catalog",
    "read:operations",
    "read:results",
    "read:logs",
    "read:metrics",
    "artifacts:write",
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
METADATA_MAX_BYTES = 8192
_ALNUM = string.ascii_letters + string.digits
_creation_connection = ContextVar("key_creation_connection", default=None)


@contextmanager
def _connection():
    # A create lock already holds a pool connection. Reuse it for its short key
    # transactions instead of exhausting the pool with nested borrows.
    current = _creation_connection.get()
    if current is not None:
        yield current
    else:
        with db.pool().connection() as connection:
            yield connection


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_value() -> str:
    """`sk-` + 40 alphanumerics: the shape LiteLLM and the Terraform-generated keys have (safe in a WebSocket subprotocol)."""
    return "sk-" + "".join(secrets.choice(_ALNUM) for _ in range(40))


def preview(value: str) -> str:
    return "sk-..." + value[-4:]


def _iso(t) -> str | None:
    return t.isoformat() if t else None


def _when(s) -> datetime | None:
    """An ISO timestamp from LiteLLM (or None) as an aware datetime; psycopg needs a datetime for timestamptz."""
    if not s:
        return None
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _info(r) -> dict:
    """A key as the API's Principal carries it: the shape LiteLLM's /key/info returned, kept so that nothing above
    auth.py changed when the keys moved into the fleet database."""
    (
        h,
        alias,
        tenant,
        role,
        models,
        budget,
        spend,
        expires,
        blocked,
        prev,
        meta,
        created,
        by,
        _updated,
        scopes,
        max_c,
        b_req,
        b_gpu,
        s_req,
        s_gpu,
        rate,
    ) = r
    num = lambda x: float(x) if x is not None else None  # noqa: E731
    return {
        "token": h,
        "key_name": prev,
        "key_alias": alias,
        "max_budget": num(budget),
        "spend": float(spend or 0),
        "models": list(models or []),
        "expires": _iso(expires),
        "blocked": bool(blocked),
        "created_at": _iso(created),
        "created_by": by,
        "metadata": {"tenant": tenant, "role": role, **(meta or {})},
        "scopes": list(scopes or []),
        "max_concurrent": max_c,
        "budget_requests": num(b_req),
        "budget_gpu_seconds": num(b_gpu),
        "spent_requests": float(s_req or 0),
        "spent_gpu_seconds": float(s_gpu or 0),
        "rate_limit": rate,
    }


def info(h: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(f"select {COLS} from keys where hash = %s", (h,)).fetchone()
        return _info(r) if r else None


def list_tenant(tenant: str) -> list[dict]:
    with db.pool().connection() as c:
        return [
            _info(r)
            for r in c.execute(f"select {COLS} from keys where tenant = %s order by alias", (tenant,)).fetchall()
        ]


def _expiry(days: int | None) -> datetime | None:
    """days from now; 0 = never."""
    return datetime.now(timezone.utc) + timedelta(days=days) if days else None


def _conflict(e, alias: str):
    import psycopg

    if isinstance(e, psycopg.errors.UniqueViolation):
        from fastapi import HTTPException

        raise HTTPException(409, f"key {alias} exists") from None
    raise e


def create(
    tenant: str,
    alias: str,
    budget: float | None = None,
    models: list[str] | None = None,
    expires_days: int | None = None,
    role: str = "user",
    metadata: dict | None = None,
    by: str | None = None,
    value: str | None = None,
) -> tuple[str, dict]:
    """A new key of the tenant; returns (value, info). The value is random unless given (Terraform's bootstrap)."""
    value = value or new_value()
    with db.pool().connection() as c:
        try:
            with c.transaction():
                r = c.execute(
                    f"""insert into keys (hash, alias, tenant, role, models, budget, expires, preview, metadata, created_by)
                                  values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning {COLS}""",
                    (
                        key_hash(value),
                        alias,
                        tenant,
                        role,
                        json.dumps(list(models or [])),
                        budget,
                        _expiry(expires_days),
                        preview(value),
                        json.dumps(metadata or {}),
                        by,
                    ),
                ).fetchone()
        except Exception as e:  # noqa: BLE001 - a duplicate alias is the caller's 409, anything else propagates
            _conflict(e, alias)
    i = _info(r)
    mirror_create(value, i)
    return value, i


def update(
    tenant: str,
    alias: str,
    budget: float | None = None,
    models: list[str] | None = None,
    expires_days: int | None = None,
) -> dict | None:
    """Budget, allowed models, expiry (days from now; 0 = never); fields left None stay. Spend never changes here."""
    sets, args = [], []
    if budget is not None:
        sets.append("budget = %s")
        args.append(budget)
    if models is not None:
        sets.append("models = %s")
        args.append(json.dumps(models))
    if expires_days is not None:
        sets.append("expires = %s")
        args.append(_expiry(expires_days))
    with db.pool().connection() as c, c.transaction():
        r = c.execute(
            f"update keys set {', '.join(sets + ['updated_at = now()'])} where tenant = %s and alias = %s returning {COLS}",
            (*args, tenant, alias),
        ).fetchone()
    if r is None:
        return None
    i = _info(r)
    mirror_update(i)
    return i


def block(tenant: str, alias: str, blocked: bool = True) -> dict | None:
    with db.pool().connection() as c, c.transaction():
        r = c.execute(
            f"update keys set blocked = %s, updated_at = now() where tenant = %s and alias = %s returning {COLS}",
            (blocked, tenant, alias),
        ).fetchone()
    if r is None:
        return None
    i = _info(r)
    mirror_update(i)
    return i


def revoke(tenant: str, alias: str) -> bool:
    with db.pool().connection() as c, c.transaction():
        r = c.execute("delete from keys where tenant = %s and alias = %s returning hash", (tenant, alias)).fetchone()
        if r is not None:
            _retire(c, r[0])
    if r is None:
        return False
    retire_mirrors()
    return True


def _retire(c, h: str) -> None:
    """Remember, in the revoking transaction, that a hash's LiteLLM mirror must go: the mirror call happens after
    the commit and may fail (LiteLLM down); the row keeps the revocation retryable until the mirror is gone."""
    c.execute("insert into retired_mirrors (hash) values (%s) on conflict do nothing", (h,))


def retire_mirrors() -> list[str]:
    """Delete the LiteLLM records of every revoked or replaced key value still on the retired list; a record that
    stays (LiteLLM unreachable or failing) is retried by the next call (the control API's reconcile loop, the next
    bootstrap). Returns the hashes whose mirrors are gone now."""
    if not db.enabled():
        return []
    with db.pool().connection() as c:
        pending = [r[0] for r in c.execute("select hash from retired_mirrors order by retired_at").fetchall()]
    gone = []
    for h in pending:
        # Serialize deletion with a bootstrap that restores this value. A captured pending list may
        # already be stale; hold its row through the router call so restoration mirrors the value last.
        with db.pool().connection() as c, c.transaction():
            retired = c.execute("select hash from retired_mirrors where hash = %s for update", (h,)).fetchone()
            if retired is None:
                continue
            if c.execute("select 1 from keys where hash = %s", (h,)).fetchone():
                c.execute("delete from retired_mirrors where hash = %s", (h,))
                continue
            if mirror_delete(h):
                c.execute("delete from retired_mirrors where hash = %s", (h,))
                gone.append(h)
    return gone


def add_spend(h: str, usd: float, operation: str, gpu_s: float) -> float | None:
    """Charge one finished run to a key: the receipt (`charges`, unique per operation) and the key's spend commit
    together, so a retry after a failed Job annotation never charges twice and concurrent charges never lose an
    increment. A charge of a key that was revoked meanwhile stays in the ledger (its hash names the key) and the
    run counts as billed; returns the key's spend, None when the key is gone."""
    with db.pool().connection() as c, c.transaction():
        inserted = c.execute(
            """insert into charges (operation, key_hash, gpu_seconds, usd) values (%s, %s, %s, %s)
                                on conflict do nothing returning operation""",
            (operation, h, gpu_s, usd),
        ).fetchone()
        if inserted:
            row = c.execute(
                "update keys set spend = spend + %s, updated_at = now() where hash = %s returning spend", (usd, h)
            ).fetchone()
            if row is None:
                log.warning("charge %s: key %s... no longer exists; recorded in the ledger only", operation, h[:8])
                return None
            return float(row[0])
        receipt = c.execute("select key_hash from charges where operation = %s", (operation,)).fetchone()
        if receipt[0] != h:
            raise ValueError("operation already belongs to another billing key")
        row = c.execute("select spend from keys where hash = %s", (h,)).fetchone()
        return float(row[0]) if row else None


def ensure_router(value: str) -> None:
    """The per-fleet router key (ROUTER_KEY): upserted at every API start, so a rotated Secret takes effect with
    a restart and the previous value stops working."""
    h = key_hash(value)
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "delete from keys where tenant = %s and alias = %s and hash <> %s", (ROUTER["tenant"], ROUTER["alias"], h)
        )
        c.execute(
            """insert into keys (hash, alias, tenant, role, preview, created_by) values (%s, %s, %s, %s, %s, 'platform')
                     on conflict (hash) do update set role = excluded.role, blocked = false, budget = null, expires = null, models = '[]', updated_at = now()""",
            (h, ROUTER["alias"], ROUTER["tenant"], ROUTER["role"], preview(value)),
        )


def bootstrap(
    value: str,
    alias: str,
    tenant: str,
    role: str = "user",
    budget: float | None = None,
    models: list[str] | None = None,
    routes: list[str] | None = None,
) -> dict:
    """A Terraform-defined key: exists with this value and these settings when this returns. Spend and creation
    time of an existing key stay; a key of the same alias with another value (a `-replace`d random_password)
    is replaced in place, its spend carried over. Idempotent; mirrored into LiteLLM."""
    h, meta = key_hash(value), json.dumps({"allowed_passthrough_routes": list(routes or [])})
    with db.pool().connection() as c, c.transaction():
        old = c.execute("select hash from keys where tenant = %s and alias = %s for update", (tenant, alias)).fetchone()
        r = c.execute(
            f"""update keys set hash = %s, preview = %s, role = %s, budget = %s, models = %s, metadata = %s, blocked = false, updated_at = now()
                          where tenant = %s and alias = %s returning {COLS}""",
            (h, preview(value), role, budget, json.dumps(list(models or [])), meta, tenant, alias),
        ).fetchone()
        if r is None:
            r = c.execute(
                f"""insert into keys (hash, alias, tenant, role, models, budget, preview, metadata, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, 'terraform')
                              returning {COLS}""",
                (h, alias, tenant, role, json.dumps(list(models or [])), budget, preview(value), meta),
            ).fetchone()
        if old and old[0] != h:
            _retire(
                c, old[0]
            )  # the replaced value's LiteLLM record is an entry point of its own: revoked with the value
        c.execute("delete from retired_mirrors where hash = %s", (h,))  # a rollback may restore a retired value
    i = _info(r)
    mirror_create(value, i)
    retire_mirrors()
    return i


# ---- import of pre-hashed keys (docs/API.md "Importing keys") -------------------------------------------
def _num(v, name, errors, integer=False):
    if v is None:
        return None
    try:
        out = int(v) if integer else float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} is not a number")
        return None
    if out < 0 or (integer and out < 1 and name == "max_concurrent"):
        errors.append(f"{name} must be positive")
        return None
    return out


def validate_records(records: list, tenant: str) -> tuple[list[dict], list[dict]]:
    """Pure: (rows, errors). A row is the normalised key as it would be stored; errors name the record index and
    every reason. A record with scopes S2 does not know is a row with `blocked = True` and `unknown_scopes` (it
    imports, but cannot act until an admin narrows it: never a silent widening). Roles are `user` only."""
    rows, errors = [], []
    if not isinstance(records, list):
        return [], [{"index": None, "errors": ["keys must be a list"]}]
    for i, r in enumerate(records):
        e = []
        if not isinstance(r, dict):
            errors.append({"index": i, "errors": ["record must be an object"]})
            continue
        h = str(r.get("hash") or "")
        if not HASH_RE.match(h):
            e.append("hash must be 64 lowercase hex characters (sha256 of the exact bearer value)")
        alias = str(r.get("alias") or "")
        if alias and not alias.startswith(f"{tenant}-"):
            alias = f"{tenant}-{alias}"
        if not ALIAS_RE.match(alias):
            e.append("alias must be 1-120 characters of letters, digits, dot, dash, underscore")
        if r.get("tenant") != tenant:
            e.append(f"tenant must be {tenant}")
        if r.get("role") not in (None, "user"):
            e.append("imported keys are user keys (role must be user or absent)")
        if not r.get("principal_id"):
            e.append("principal_id is required (the stable legacy principal)")
        models = r.get("models")
        if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
            e.append("models must be a list of model ids ([] = every model)")
            models = []
        scopes = r.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(x, str) and x for x in scopes):
            e.append("scopes must be a list of scope names")
            scopes = []
        unknown = sorted(set(scopes) - set(SCOPES))
        budget = _num(r.get("budget_usd"), "budget_usd", e)
        b_req = _num(r.get("budget_requests"), "budget_requests", e)
        b_gpu = _num(r.get("budget_gpu_seconds"), "budget_gpu_seconds", e)
        max_c = _num(r.get("max_concurrent"), "max_concurrent", e, integer=True)
        rate = r.get("rate_limit")
        if rate is not None:
            if (
                not isinstance(rate, dict)
                or _num(rate.get("requests"), "rate_limit.requests", e, True) in (None, 0)
                or _num(rate.get("window_s"), "rate_limit.window_s", e, True) in (None, 0)
            ):
                e.append("rate_limit must be {requests >= 1, window_s >= 1} (the legacy units, never converted)")
                rate = None
            else:
                rate = {"requests": int(rate["requests"]), "window_s": int(rate["window_s"])}
        expires = created = None
        try:
            expires = _when(r.get("expires"))
        except ValueError:
            e.append("expires must be RFC 3339")
        try:
            created = _when(r.get("created_at"))
        except ValueError:
            e.append("created_at must be RFC 3339")
        meta = r.get("metadata") or {}
        if not isinstance(meta, dict):
            e.append("metadata must be an object")
            meta = {}
        legacy = {**meta, "principal_id": r.get("principal_id"), "tenant_id": r.get("legacy_tenant_id")}
        if unknown:
            legacy["unenforced_scopes"] = unknown
        metadata = {"allowed_passthrough_routes": list(r.get("routes") or []), "legacy": legacy}
        if len(json.dumps(metadata)) > METADATA_MAX_BYTES:
            e.append(f"metadata exceeds {METADATA_MAX_BYTES} bytes")
        if e:
            errors.append({"index": i, "hash": h[:8], "errors": e})
            continue
        rows.append(
            {
                "hash": h,
                "alias": alias,
                "tenant": tenant,
                "role": "user",
                "models": sorted(models),
                "scopes": sorted(scopes),
                "budget": budget,
                "budget_requests": b_req,
                "budget_gpu_seconds": b_gpu,
                "max_concurrent": max_c,
                "rate_limit": rate,
                "expires": expires,
                "blocked": bool(r.get("blocked")) or bool(unknown),
                "preview": r.get("preview"),
                "metadata": metadata,
                "created_at": created,
                "unknown_scopes": unknown,
            }
        )
    seen_h, seen_a = set(), set()
    for row in rows:
        if row["hash"] in seen_h:
            errors.append({"index": None, "hash": row["hash"][:8], "errors": ["duplicate hash in the batch"]})
        if row["alias"] in seen_a:
            errors.append(
                {"index": None, "hash": row["hash"][:8], "errors": [f"duplicate alias {row['alias']} in the batch"]}
            )
        seen_h.add(row["hash"])
        seen_a.add(row["alias"])
    return rows, errors


def _stored_view(r) -> dict:
    """The stored row in the shape validate_records produces, for the byte-for-byte comparison."""
    i = _info(r)
    meta = i["metadata"]
    return {
        "hash": i["token"],
        "alias": i["key_alias"],
        "tenant": meta["tenant"],
        "role": meta["role"],
        "models": sorted(i["models"]),
        "scopes": sorted(i["scopes"]),
        "budget": i["max_budget"],
        "budget_requests": i["budget_requests"],
        "budget_gpu_seconds": i["budget_gpu_seconds"],
        "max_concurrent": i["max_concurrent"],
        "rate_limit": i["rate_limit"],
        "expires": _when(i["expires"]),
        "blocked": i["blocked"],
        "preview": i["key_name"],
        "metadata": {
            "allowed_passthrough_routes": list(meta.get("allowed_passthrough_routes") or []),
            "legacy": meta.get("legacy") or {},
        },
    }


def equivalent(stored: dict, row: dict) -> bool:
    keys_ = (
        "alias",
        "tenant",
        "role",
        "models",
        "scopes",
        "budget",
        "budget_requests",
        "budget_gpu_seconds",
        "max_concurrent",
        "rate_limit",
        "expires",
        "blocked",
        "preview",
        "metadata",
    )
    return all(stored.get(k) == row.get(k) for k in keys_)


class _DryRun(Exception):
    pass


def import_records(records: list, tenant: str, by: str | None, dry_run: bool = False) -> dict:
    """Idempotent, transactional, all-or-nothing. Every record is classified against the table under row locks:
    new hash and new alias -> inserted; the same hash under the same alias with every field equal -> unchanged;
    anything else (the hash under another alias or tenant, the alias with another hash, the same key with changed
    limits, grants or principal) -> conflict, and the whole batch is refused with the list. `dry_run` reports the
    classification and writes nothing. Returns the report; raises HTTPException 400 (invalid records) or 409 (conflicts)."""
    from fastapi import HTTPException

    rows, errors = validate_records(records, tenant)
    if errors:
        raise HTTPException(400, {"message": "invalid records; nothing imported", "errors": errors})
    report = {
        "dry_run": dry_run,
        "inserted": [],
        "unchanged": [],
        "conflicts": [],
        "blocked_for_unknown_scope": [],
        "not_mirrored": [],
    }
    try:
        with db.pool().connection() as c, c.transaction():
            for row in rows:
                by_hash = c.execute(f"select {COLS} from keys where hash = %s for update", (row["hash"],)).fetchone()
                by_alias = c.execute(
                    f"select {COLS} from keys where tenant = %s and alias = %s for update", (tenant, row["alias"])
                ).fetchone()
                if by_hash is None and by_alias is None:
                    c.execute(
                        """insert into keys (hash, alias, tenant, role, models, budget, expires, blocked, preview, metadata, created_at, created_by,
                                                   scopes, max_concurrent, budget_requests, budget_gpu_seconds, rate_limit)
                                 values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s, %s, %s)""",
                        (
                            row["hash"],
                            row["alias"],
                            tenant,
                            "user",
                            json.dumps(row["models"]),
                            row["budget"],
                            row["expires"],
                            row["blocked"],
                            row["preview"],
                            json.dumps(row["metadata"]),
                            row["created_at"],
                            f"import:{by or 'operator'}",
                            json.dumps(row["scopes"]),
                            row["max_concurrent"],
                            row["budget_requests"],
                            row["budget_gpu_seconds"],
                            json.dumps(row["rate_limit"]) if row["rate_limit"] else None,
                        ),
                    )
                    report["inserted"].append(row["alias"])
                    report["not_mirrored"].append(row["alias"])  # no plaintext: LiteLLM's route cannot know this key
                    if row["unknown_scopes"]:
                        report["blocked_for_unknown_scope"].append(
                            {"alias": row["alias"], "scopes": row["unknown_scopes"]}
                        )
                elif by_hash is not None and by_alias is not None and by_hash[0] == by_alias[0]:
                    if equivalent(_stored_view(by_hash), row):
                        report["unchanged"].append(row["alias"])
                    else:
                        report["conflicts"].append(
                            {
                                "alias": row["alias"],
                                "reason": "the key exists with other limits, grants or principal; change it with PATCH /v1/keys/<alias>, an import never overwrites",
                            }
                        )
                elif by_hash is not None:
                    report["conflicts"].append(
                        {"alias": row["alias"], "reason": f"the hash exists as {by_hash[2]}/{by_hash[1]}"}
                    )
                else:
                    report["conflicts"].append({"alias": row["alias"], "reason": "the alias exists with another hash"})
            if report["conflicts"]:
                raise HTTPException(409, {"message": "conflicts; nothing imported", **report})
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    return report


# ---- admission: a slot, a request and the GPU seconds a run may take, reserved per key in one transaction ----
class KeyLimit(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def admit(
    h: str, operation: str, tenant: str, gpu_seconds: float = 0.0, counts_request: bool = True, run_member: bool = False
) -> dict:
    """Reserve what `operation` will take from the key: one concurrency slot and (unless `counts_request` is false)
    one request against the request budget and the rate window, plus `gpu_seconds` against the GPU-second budget.
    The key row is locked, so two API replicas never admit past a limit together; a reservation that already exists
    (a replay with the same idempotency key) counts once. Raises KeyLimit(402 | 429)."""
    with _connection() as c, c.transaction():
        k = c.execute(
            "select max_concurrent, budget_requests, budget_gpu_seconds, spent_requests, spent_gpu_seconds, rate_limit from keys where hash = %s for update",
            (h,),
        ).fetchone()
        if k is None:
            raise KeyLimit(401, "invalid API key")
        existing = c.execute(
            "select key_hash, tenant from key_reservations where operation = %s", (operation,)
        ).fetchone()
        if existing and existing != (h, tenant):
            raise KeyLimit(409, "operation is reserved by another key")
        if existing:
            return {"operation": operation, "replay": True}
        max_c, b_req, b_gpu, s_req, s_gpu, rate = k
        active, r_req, r_gpu = c.execute(
            "select count(*), coalesce(sum(requests), 0), coalesce(sum(gpu_seconds), 0) from key_reservations where key_hash = %s",
            (h,),
        ).fetchone()
        r_gpu = float(r_gpu) + _unknown_gpu(
            c, h
        )  # attempts that ran without a trustworthy measurement keep their bound
        if max_c is not None and active >= max_c:
            raise KeyLimit(429, f"key concurrency limit reached ({active}/{max_c} operations in flight)")
        req = 1 if counts_request else 0
        if b_req is not None and req and float(s_req) + float(r_req) + req > float(b_req):
            raise KeyLimit(402, "key request budget exhausted")
        if b_gpu is not None and float(s_gpu) + float(r_gpu) + gpu_seconds > float(b_gpu):
            raise KeyLimit(
                402,
                f"key GPU-second budget exhausted ({max(0.0, float(b_gpu) - float(s_gpu) - float(r_gpu)):.0f} s left, {gpu_seconds:.0f} s needed)",
            )
        if rate and req:
            n = c.execute(
                "select count(*) from key_calls where key_hash = %s and at > now() - make_interval(secs => %s)",
                (h, rate["window_s"]),
            ).fetchone()[0]
            if n >= rate["requests"]:
                raise KeyLimit(429, f"key rate limit reached ({rate['requests']} requests per {rate['window_s']} s)")
            c.execute(
                "delete from key_calls where key_hash = %s and at <= now() - make_interval(secs => %s)",
                (h, rate["window_s"]),
            )
            c.execute("insert into key_calls (key_hash) values (%s)", (h,))
        c.execute(
            "insert into key_reservations (operation, key_hash, tenant, gpu_seconds, requests) values (%s, %s, %s, %s, %s)",
            (operation, h, tenant, gpu_seconds, req),
        )
        if run_member:
            # the root of a logical run (docs/API.md "Keys"): its continuations join this reservation as members
            c.execute(
                "insert into key_run_members (operation, root, predecessor, key_hash, tenant, gpu_bound) values (%s, %s, null, %s, %s, %s) on conflict do nothing",
                (operation, operation, h, tenant, gpu_seconds),
            )
        return {"operation": operation, "replay": False, "active": int(active) + 1}


def abandon(operation: str, h: str) -> bool:
    """Refund a new reservation only after the caller proves no Job was created.

    Admission attempts still count against the rate window; no executed request/GPU
    spend is invented. The matching key prevents a failed replay releasing another key.
    """
    with _connection() as c, c.transaction():
        return (
            c.execute(
                "delete from key_reservations where operation = %s and key_hash = %s returning operation",
                (operation, h),
            ).fetchone()
            is not None
        )


@contextmanager
def operation_lock(operation: str):
    """Serialize create/replay/refund across API replicas; no second scheduler."""
    with db.pool().connection() as c:
        c.execute("select pg_advisory_lock(hashtextextended(%s, 1))", (operation,))
        token = _creation_connection.set(c)
        try:
            yield
        finally:
            try:
                c.execute("select pg_advisory_unlock(hashtextextended(%s, 1))", (operation,))
            finally:
                _creation_connection.reset(token)


def own_operation(operation: str, tenant: str, owner: str, model: str) -> None:
    """Record immutable ownership before creating the Job; safe under concurrent replay."""
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "insert into operation_owners (operation, tenant, owner, model) values (%s, %s, %s, %s) on conflict do nothing",
            (operation, tenant, owner, model),
        )
        row = c.execute(
            "select tenant, owner, model from operation_owners where operation = %s", (operation,)
        ).fetchone()
        if row != (tenant, owner, model):
            raise KeyLimit(409, "operation belongs to another owner or model")


def operation_owner(operation: str) -> tuple[str, str, str] | None:
    with _connection() as c:
        return c.execute(
            "select tenant, owner, model from operation_owners where operation = %s", (operation,)
        ).fetchone()


def release(operation: str, gpu_seconds: float | None = None) -> bool:
    """The operation is done: its slot goes, its request count and the measured GPU seconds (`gpu_seconds`; None =
    nothing measured: a cancelled, failed-at-admission or CPU-only attempt) become spent. Idempotent. A member of a
    logical run settles through the run's ledger (settle_run_member): its root's slot goes only with the run."""
    with _connection() as c, c.transaction():
        if c.execute("select 1 from key_run_members where operation = %s", (operation,)).fetchone():
            return bool(settle_run_member(operation, gpu_seconds).get("settled"))
        r = c.execute(
            "delete from key_reservations where operation = %s returning key_hash, requests", (operation,)
        ).fetchone()
        if r is None:
            return False
        c.execute(
            "update keys set spent_requests = spent_requests + %s, spent_gpu_seconds = spent_gpu_seconds + %s, updated_at = now() where hash = %s",
            (r[1], float(gpu_seconds or 0), r[0]),
        )
        return True


# ---- the ledger of a logical run: one reservation per root, members per continuation (docs/API.md "Keys") ------
def _unknown_gpu(c, h: str) -> float:
    return float(
        c.execute(
            "select coalesce(sum(gpu_bound), 0) from key_run_members where key_hash = %s and state = 'unknown'", (h,)
        ).fetchone()[0]
    )


def run_member(operation: str) -> dict | None:
    with _connection() as c:
        r = c.execute(
            "select operation, root, predecessor, key_hash, tenant, gpu_bound, gpu_seconds, state from key_run_members where operation = %s",
            (operation,),
        ).fetchone()
    if r is None:
        return None
    return {
        "operation": r[0],
        "root": r[1],
        "predecessor": r[2],
        "key_hash": r[3],
        "tenant": r[4],
        "gpu_bound": float(r[5]),
        "gpu_seconds": float(r[6]) if r[6] is not None else None,
        "state": r[7],
    }


def reservation_key(operation: str) -> str | None:
    """The key hash holding a live reservation for `operation` (its slot: admitted, not released); None otherwise. The
    gateway hook's proof that an admitted operation is active."""
    with _connection() as c:
        r = c.execute("select key_hash from key_reservations where operation = %s", (operation,)).fetchone()
    return r[0] if r else None


def run_members(root: str) -> list[dict]:
    """The members of a run, oldest first (empty: no ledger for this root)."""
    with _connection() as c:
        rows = c.execute(
            "select operation, predecessor, gpu_bound, gpu_seconds, state, kind, stage, note from key_run_members where root = %s order by created_at, operation",
            (root,),
        ).fetchall()
    return [
        {
            "operation": r[0],
            "predecessor": r[1],
            "gpu_bound": float(r[2]),
            "gpu_seconds": float(r[3]) if r[3] is not None else None,
            "state": r[4],
            "kind": r[5],
            "stage": r[6],
            "note": r[7],
        }
        for r in rows
    ]


# ---- stage graphs (docs/JOBS.md "Stage composition") ----------------------------------------------------------
def declare_graph(
    root: str, tenant: str, owner: str, stages: dict, deadline_at=None, models: dict | None = None
) -> None:
    """Seal the stages of a root at its submission (idempotent for a replay of the same root); `models` binds declared
    stages to the model they will run on (docs/JOBS.md "Delegated stages")."""
    with _connection() as c, c.transaction():
        c.execute(
            "insert into run_graphs (root, tenant, owner, stages, deadline_at, models) values (%s, %s, %s, %s, %s, %s) on conflict (root) do nothing",
            (root, tenant, owner, json.dumps(stages), deadline_at, json.dumps(models or {})),
        )


_GRAPH_COLUMNS = "root, stages, deadline_at, cancelled_at, closed_at, reason, tenant, owner, sealed_at, models"


def _graph(r) -> dict:
    return {
        "root": r[0],
        "stages": r[1],
        "deadline_at": r[2],
        "cancelled_at": r[3],
        "closed_at": r[4],
        "reason": r[5],
        "tenant": r[6],
        "owner": r[7],
        "sealed_at": r[8],
        "models": r[9] or {},
    }


def graph_of(root: str) -> dict | None:
    with _connection() as c:
        r = c.execute(f"select {_GRAPH_COLUMNS} from run_graphs where root = %s", (root,)).fetchone()
    return _graph(r) if r else None


def open_graphs() -> list[dict]:
    """The graphs still waiting for a stage (neither cancelled nor closed), oldest first."""
    with _connection() as c:
        rows = c.execute(
            f"select {_GRAPH_COLUMNS} from run_graphs where cancelled_at is null and closed_at is null order by sealed_at"
        ).fetchall()
    return [_graph(r) for r in rows]


def cancel_graph(root: str, reason: str) -> bool:
    """The root was cancelled, or its graph can never complete (deadline, root failed): no stage attaches or is
    admitted any more; the run closes once nothing is active. Idempotent (the first reason stands)."""
    with _connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 2))", (root,))
        r = c.execute(
            "update run_graphs set cancelled_at = coalesce(cancelled_at, now()), reason = coalesce(reason, %s) where root = %s returning root",
            (reason, root),
        ).fetchone()
        if r is not None:
            _close_run_if_idle(c, root)
        return r is not None


def note_member(operation: str, note: str) -> None:
    """Why a member ended the way it did (a stage cancelled for a failed dependency), for the root's graph view."""
    with _connection() as c, c.transaction():
        c.execute("update key_run_members set note = %s where operation = %s", (note, operation))


def _lineage_head(c, root: str, kind: str, stage: str | None) -> str | None:
    r = c.execute(
        "select operation from key_run_members where root = %s and kind = %s and stage is not distinct from %s "
        "order by created_at desc, operation desc limit 1",
        (root, kind, stage),
    ).fetchone()
    return r[0] if r else None


def run_head(root: str) -> str | None:
    """The member a continuation of the root may follow: the newest member of the root's own lineage (the root and
    its resumes/exports/recoveries; a graph's stages are their own lineages, stage_head); None when the run has no
    ledger (an unbounded key admitted before the ledger existed, or no database)."""
    with _connection() as c:
        return _lineage_head(c, root, "continuation", None)


def child_head(root: str, unit: str) -> str | None:
    """The newest member of one endpoint child's lineage (the child and its transfer-only export retries)."""
    with _connection() as c:
        return _lineage_head(c, root, "child", unit)


def stage_head(root: str, stage: str) -> str | None:
    """The newest member of one stage's lineage (the stage's operation and its continuations); None before the stage
    was submitted."""
    with _connection() as c:
        return _lineage_head(c, root, "stage", stage)


def member_of(operation: str) -> dict | None:
    """The ledger row of an operation: its root, kind (`continuation` | `stage` | `child`), stage (the unit for a
    child) and state; None when it is no member (no ledger)."""
    with _connection() as c:
        r = c.execute(
            "select root, kind, stage, state, predecessor, created_at from key_run_members where operation = %s",
            (operation,),
        ).fetchone()
    return (
        {
            "root": r[0],
            "kind": r[1],
            "stage": r[2],
            "state": r[3],
            "predecessor": r[4],
            "created_at": r[5],
        }
        if r
        else None
    )


def prepare_run_member(
    root: str,
    operation: str,
    predecessor: str | None,
    gpus: int,
    timeout_s: int | None,
    transfer_only: bool = False,
    kind: str = "continuation",
    stage: str | None = None,
) -> dict:
    """Admit a continuation of the run `root` (resume, export retry, finalizer, fallback, recovery) as member
    `operation` on the root's original key, before its Job exists: the key must still be valid (not blocked, not
    expired); `predecessor` must be the run's current head (one chain, one head; a transfer-only member may follow a
    terminal head); the member's GPU bound (gpus x timeout for a key with a GPU-second budget, else 0) must fit the
    budget next to everything reserved and unknown; the root's slot is reused while the run is open and re-acquired
    (concurrency re-checked, no second request, no rate call) when the run had closed. Idempotent for the same
    member. Raises KeyLimit(400 | 401 | 402 | 403 | 409 | 429)."""
    with _connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 2))", (root,))
        own = c.execute(
            "select key_hash, tenant from key_run_members where operation = %s and root = %s", (root, root)
        ).fetchone()
        if own is None:
            raise KeyLimit(409, f"run {root} has no reservation ledger; it cannot be continued on a bounded key")
        h, tenant = own
        if c.execute("select 1 from key_run_members where operation = %s", (operation,)).fetchone():
            return {"operation": operation, "replay": True}
        k = c.execute(
            "select max_concurrent, budget_requests, budget_gpu_seconds, spent_requests, spent_gpu_seconds, blocked, expires from keys where hash = %s for update",
            (h,),
        ).fetchone()
        if k is None:
            raise KeyLimit(401, "the run's key no longer exists")
        max_c, _b_req, b_gpu, _s_req, s_gpu, blocked, expires = k
        if blocked:
            raise KeyLimit(403, "the run's key is blocked")
        if expires is not None and expires < datetime.now(timezone.utc):
            raise KeyLimit(401, "the run's key has expired")
        if kind == "stage":
            # a declared stage of the root's graph (docs/JOBS.md "Stage composition"), or a continuation of one: each
            # stage is its own lineage inside the root's run (one head per stage; siblings coexist; the stage's
            # resume/export/recovery members follow that stage's head); the graph must exist and name the stage; a
            # cancelled graph admits nothing, a closed one no new stage (continuations of its stages still run)
            g = c.execute(
                "select stages, cancelled_at, closed_at from run_graphs where root = %s for update", (root,)
            ).fetchone()
            if g is None:
                raise KeyLimit(409, f"run {root} declared no stage graph")
            if stage not in (g[0] or {}):
                raise KeyLimit(400, f"stage {stage!r} is not declared by {root} ({sorted(g[0] or {})})")
            if g[1] is not None:
                raise KeyLimit(409, f"the graph of {root} is cancelled; no stage attaches to it")
            head = _lineage_head(c, root, "stage", stage)
            if predecessor is None:
                if head is not None:
                    raise KeyLimit(409, f"stage {stage!r} of {root} already has an operation ({head})")
                if g[2] is not None:
                    raise KeyLimit(409, f"the graph of {root} is closed; no stage attaches to it")
            elif predecessor != head:
                raise KeyLimit(
                    409, f"only the current head of stage {stage!r} may be continued ({head}, not {predecessor})"
                )
        elif kind == "child" and predecessor is not None:
            # a continuation of an endpoint child (its transfer-only export retry, docs/JOBS.md "Endpoint children"):
            # the child's own lineage inside the root's run (one head per unit), no second request (nothing is
            # inferred again), the slot re-acquired like any continuation when the run had closed
            head = _lineage_head(c, root, "child", stage)
            if predecessor != head:
                raise KeyLimit(
                    409, f"only the current head of child unit {stage!r} may be continued ({head}, not {predecessor})"
                )
        elif kind == "child":
            # an endpoint child of an active run (docs/JOBS.md "Endpoint children"): attaches to an OPEN root only (the
            # root's reservation still exists: nothing settled it), never to a head; counts one request against the
            # key's request budget and rate window (one inference each)
            if c.execute("select 1 from key_reservations where operation = %s", (root,)).fetchone() is None:
                raise KeyLimit(409, f"run {root} is closed (its reservation is settled); no child attaches to it")
            _b_req, s_req, rate = k[1], k[3], _rate_of(c, h)
            r_req = c.execute(
                "select coalesce(sum(requests), 0) from key_reservations where key_hash = %s", (h,)
            ).fetchone()[0]
            if _b_req is not None and float(s_req) + float(r_req) + 1 > float(_b_req):
                raise KeyLimit(402, "key request budget exhausted for the child")
            if rate:
                n = c.execute(
                    "select count(*) from key_calls where key_hash = %s and at > now() - make_interval(secs => %s)",
                    (h, rate["window_s"]),
                ).fetchone()[0]
                if n >= rate["requests"]:
                    raise KeyLimit(
                        429, f"key rate limit reached ({rate['requests']} requests per {rate['window_s']} s)"
                    )
                c.execute("insert into key_calls (key_hash) values (%s)", (h,))
            c.execute("update key_reservations set requests = requests + 1 where operation = %s", (root,))
        else:
            head = _lineage_head(c, root, "continuation", None)
            if predecessor != head:
                raise KeyLimit(409, f"only the current run head may be continued ({head}, not {predecessor})")
        bound = 0.0
        if b_gpu is not None and gpus > 0 and not transfer_only:
            if not timeout_s:
                raise KeyLimit(400, "this key has a GPU-second budget: the continuation needs a bounded timeout")
            bound = float(gpus * int(timeout_s))
        reserved = c.execute(
            "select coalesce(sum(gpu_seconds), 0) from key_reservations where key_hash = %s", (h,)
        ).fetchone()[0]
        if b_gpu is not None and bound and float(s_gpu) + float(reserved) + _unknown_gpu(c, h) + bound > float(b_gpu):
            raise KeyLimit(402, "key GPU-second budget exhausted for the continuation")
        res = c.execute("select gpu_seconds from key_reservations where operation = %s for update", (root,)).fetchone()
        if res is None:  # the run had closed (every member settled): the continuation re-acquires the slot
            active = c.execute("select count(*) from key_reservations where key_hash = %s", (h,)).fetchone()[0]
            if max_c is not None and active >= max_c:
                raise KeyLimit(429, f"key concurrency limit reached ({active}/{max_c} operations in flight)")
            c.execute(
                "insert into key_reservations (operation, key_hash, tenant, gpu_seconds, requests) values (%s, %s, %s, %s, 0)",
                (root, h, tenant, bound),
            )
        else:
            c.execute("update key_reservations set gpu_seconds = gpu_seconds + %s where operation = %s", (bound, root))
        c.execute(
            "insert into key_run_members (operation, root, predecessor, key_hash, tenant, gpu_bound, kind, stage) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (operation, root, predecessor, h, tenant, bound, kind, stage),
        )
        return {"operation": operation, "replay": False, "gpu_bound": bound, "key_hash": h}


def _rate_of(c, h: str) -> dict | None:
    r = c.execute("select rate_limit from keys where hash = %s", (h,)).fetchone()
    return (r[0] if r and isinstance(r[0], dict) else None) or None


def _close_run_if_idle(c, root: str) -> bool:
    """No active member left, and every stage the root declared has a settled member (a graph keeps its slot and
    request until all declared stages settled or the graph was cancelled, docs/JOBS.md "Stage composition"): the
    run's slot goes and its request count is spent. Returns whether it closed now."""
    if c.execute("select 1 from key_run_members where root = %s and state = 'active' limit 1", (root,)).fetchone():
        return False
    g = c.execute("select stages, cancelled_at from run_graphs where root = %s", (root,)).fetchone()
    if g and g[1] is None:
        settled = {
            r[0]
            for r in c.execute(
                "select stage from key_run_members where root = %s and kind = 'stage' and state <> 'active'", (root,)
            ).fetchall()
        }
        if not set(g[0] or {}) <= settled:
            return False
    if g:
        c.execute("update run_graphs set closed_at = coalesce(closed_at, now()) where root = %s", (root,))
    r = c.execute("delete from key_reservations where operation = %s returning key_hash, requests", (root,)).fetchone()
    if r is None:
        return False
    c.execute("update keys set spent_requests = spent_requests + %s, updated_at = now() where hash = %s", (r[1], r[0]))
    return True


def settle_run_member(operation: str, gpu_seconds: float | None) -> dict:
    """A member's attempt is over: `gpu_seconds` measured (spent; its bound leaves the reservation) or None (nothing
    trustworthy was measured: the member is `unknown` and its bound stays counted against the budget, outside the
    slot). The run's slot closes when no member is active. Idempotent."""
    with _connection() as c, c.transaction():
        m = c.execute(
            "select root, key_hash, gpu_bound, state from key_run_members where operation = %s for update", (operation,)
        ).fetchone()
        if m is None:
            return {"settled": False, "reason": "no member"}
        root, h, bound, state = m
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 2))", (root,))
        if state != "active":
            return {"settled": False, "state": state, "closed": _close_run_if_idle(c, root)}
        if gpu_seconds is None:
            c.execute(
                "update key_run_members set state = 'unknown', settled_at = now() where operation = %s", (operation,)
            )
        else:
            c.execute(
                "update key_run_members set state = 'settled', gpu_seconds = %s, settled_at = now() where operation = %s",
                (float(gpu_seconds), operation),
            )
            c.execute(
                "update keys set spent_gpu_seconds = spent_gpu_seconds + %s, updated_at = now() where hash = %s",
                (float(gpu_seconds), h),
            )
        c.execute(
            "update key_reservations set gpu_seconds = greatest(gpu_seconds - %s, 0) where operation = %s",
            (bound, root),
        )
        return {
            "settled": True,
            "state": "unknown" if gpu_seconds is None else "settled",
            "closed": _close_run_if_idle(c, root),
        }


def settle_unknown(operation: str, gpu_seconds: float) -> bool:
    """An operator's later measurement of an `unknown` member (a lost worker's attempt): it becomes spend. False
    when the member is not unknown."""
    with _connection() as c, c.transaction():
        m = c.execute(
            "select key_hash from key_run_members where operation = %s and state = 'unknown' for update", (operation,)
        ).fetchone()
        if m is None:
            return False
        c.execute(
            "update key_run_members set state = 'settled', gpu_seconds = %s, settled_at = now() where operation = %s",
            (float(gpu_seconds), operation),
        )
        c.execute(
            "update keys set spent_gpu_seconds = spent_gpu_seconds + %s, updated_at = now() where hash = %s",
            (float(gpu_seconds), m[0]),
        )
        return True


def rollback_run_member(operation: str) -> bool:
    """The continuation's (or endpoint child's) Job was never created (a confirmed absence after a failed create): the
    member goes and its bound leaves the reservation (a child's request too); the run closes when nothing else is
    active. Never for a member whose Job may exist."""
    with _connection() as c, c.transaction():
        m = c.execute(
            "select root, gpu_bound, kind from key_run_members where operation = %s and state = 'active' and (predecessor is not null or kind = 'child') for update",
            (operation,),
        ).fetchone()
        if m is None:
            return False
        root, bound, kind = m
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 2))", (root,))
        c.execute("delete from key_run_members where operation = %s", (operation,))
        c.execute(
            "update key_reservations set gpu_seconds = greatest(gpu_seconds - %s, 0) where operation = %s",
            (bound, root),
        )
        if kind == "child":  # the child's one request was counted on the root's reservation at its preparation
            c.execute("update key_reservations set requests = greatest(requests - 1, 0) where operation = %s", (root,))
        _close_run_if_idle(c, root)
        return True


def run_usage(h: str) -> dict:
    """What a key holds beyond its spend: reserved GPU seconds (bounds of active work), unknown GPU seconds (attempts
    without a trustworthy measurement) and the operations in flight."""
    with _connection() as c:
        active, reserved = c.execute(
            "select count(*), coalesce(sum(gpu_seconds), 0) from key_reservations where key_hash = %s", (h,)
        ).fetchone()
        unknown = _unknown_gpu(c, h)
    return {"active_operations": int(active), "reserved_gpu_seconds": float(reserved), "unknown_gpu_seconds": unknown}


def stale_reservations(older_than_s: int) -> list[tuple[str, str]]:
    """(operation, tenant) of reservations older than `older_than_s`: the reconciler releases those whose Job is gone."""
    with db.pool().connection() as c:
        return [
            (r[0], r[1])
            for r in c.execute(
                "select operation, tenant from key_reservations where created_at < now() - make_interval(secs => %s)",
                (older_than_s,),
            ).fetchall()
        ]


# ---- the LiteLLM mirror (best effort; the fleet database is the source of truth) ----------------------
def _litellm(method: str, path: str, body: dict) -> int:
    """Status code of a LiteLLM admin call, 0 when LiteLLM is unreachable; never raises."""
    return _litellm_call(method, path, body)[0]


def _litellm_call(method: str, path: str, body: dict) -> tuple[int, str]:
    if not LITELLM_MASTER_KEY:
        return 0, ""
    import httpx

    try:
        with httpx.Client(timeout=10) as c:
            r = c.request(
                method, LITELLM_URL + path, json=body, headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
            )
    except Exception as e:  # noqa: BLE001
        log.warning("litellm mirror %s: %s", path, e)
        return 0, ""
    if r.status_code >= 300:
        log.warning("litellm mirror %s: %s %s", path, r.status_code, r.text[:160])
    return r.status_code, r.text


def _mirror_body(i: dict) -> dict:
    body = {"max_budget": i["max_budget"], "models": i["models"], "metadata": i["metadata"], "blocked": i["blocked"]}
    if i.get("expires"):
        left = (datetime.fromisoformat(i["expires"]) - datetime.now(timezone.utc)).total_seconds()
        body["duration"] = f"{max(int(left), 1)}s"
    return body


def mirror_create(value: str, i: dict) -> None:
    """LiteLLM accepts a provided key value: the same key works on its OpenAI route."""
    code = _litellm("POST", "/key/generate", {"key": value, "key_alias": i["key_alias"], **_mirror_body(i)})
    if code == 400:  # exists already (a re-run of the bootstrap, a restored database): bring it in line
        mirror_update(i)


def mirror_update(i: dict) -> None:
    body = _mirror_body(i)
    if "duration" not in body:
        body["duration"] = None
    _litellm("POST", "/key/update", {"key": i["token"], **body})


def mirror_delete(h: str) -> bool:
    """True when LiteLLM no longer has the key (deleted now, or not there); False when the call failed and the
    deletion must be retried. Without a mirror (no LITELLM_MASTER_KEY) there is nothing to delete."""
    if not LITELLM_MASTER_KEY:
        return True
    code, text = _litellm_call("POST", "/key/delete", {"keys": [h]})
    return code == 200 or code == 404 or (code == 400 and "not found" in text.lower())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    if sys.argv[1:2] == ["import"]:
        doc = json.load(open(sys.argv[2]))
        db.migrate()
        print(json.dumps(import_records(doc["keys"], doc["tenant"], "cli", bool(doc.get("dry_run"))), indent=1))
        sys.exit(0)
    if sys.argv[1:2] != ["bootstrap"]:
        sys.exit("usage: python -m keys bootstrap   (env KEY, KEY_SPEC) | python -m keys import <file.json>")
    spec, value = json.loads(os.environ["KEY_SPEC"]), os.environ["KEY"]
    for attempt in range(60):  # the managed database may still be coming up
        try:
            db.migrate()
            break
        except Exception as e:  # noqa: BLE001
            log.warning("database not ready (%s), attempt %d", e, attempt + 1)
            time.sleep(10)
    else:
        sys.exit("database unavailable")
    i = bootstrap(
        value,
        spec["alias"],
        spec["tenant"],
        spec.get("role") or "user",
        spec.get("budget"),
        spec.get("models"),
        spec.get("routes"),
    )
    log.info(
        "key %s of tenant %s ready (role %s, budget %s)",
        i["key_alias"],
        spec["tenant"],
        i["metadata"]["role"],
        i["max_budget"],
    )
