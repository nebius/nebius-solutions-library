"""A bounded, retryable pause of NEW admissions (docs/API.md "Submission pause", api 0.11.124): the fleet operator
pauses submissions for some tenants or all; while paused, :invoke (every mode, stages included), :resume and :export
of a paused tenant answer 503 with Retry-After and create nothing. Everything else is untouched: reads,
:operation-id, :cancel, in-flight Jobs, finalizers, exports already running, callbacks, results, storage grants,
keys and budgets. The state is one row of the fleet_settings table (migration 30), read by every replica with a
short cache, so it survives restarts and a rolling rollout; without the database nothing is ever paused."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException

import db
from resilience import iso

KEY = "submissions"
CACHE_S = 2.0
RETRY_AFTER_DEFAULT_S = 30
RETRY_AFTER_MAX_S = 3600
MAX_S = 6 * 3600  # a pause is bounded: at most six hours, then it lifts itself (an operator who forgot it)
_cache: dict = {"until": 0.0, "value": None}


def _read() -> dict | None:
    with db.pool().connection() as c:
        row = c.execute("select value from fleet_settings where key = %s", (KEY,)).fetchone()
    return row[0] if row else None


def _write(value: dict | None) -> None:
    with db.pool().connection() as c:
        if value is None:
            c.execute("delete from fleet_settings where key = %s", (KEY,))
        else:
            c.execute(
                "insert into fleet_settings (key, value, updated_at) values (%s, %s::jsonb, now()) "
                "on conflict (key) do update set value = excluded.value, updated_at = now()",
                (KEY, json.dumps(value)),
            )
    _cache["until"] = 0.0


def state() -> dict:
    """{paused, tenants (None = every tenant), reason, since, until, by, retry_after_s}; never paused without the
    database or past `until`."""
    if not db.enabled():
        return {"paused": False}
    if _cache["until"] > time.time():
        value = _cache["value"]
    else:
        try:
            value = _read()
        except Exception:  # noqa: BLE001 - an unreadable setting pauses nobody (the pause is a convenience, never a lock)
            value = _cache["value"]
        _cache.update({"until": time.time() + CACHE_S, "value": value})
    if not value or not value.get("paused"):
        return {"paused": False}
    if value.get("until") and value["until"] <= iso(datetime.now(timezone.utc)):
        return {"paused": False, "expired": value}
    return dict(value)


def paused_for(tenant: str) -> dict | None:
    st = state()
    if not st.get("paused"):
        return None
    tenants = st.get("tenants")
    if tenants is None or tenant in tenants:
        return st
    return None


ADMISSION_HEADER = "X-Serverless2-Admission"  # `refused`: the header the 429 admission refusal carries too (app.py)
REASON_HEADER = "X-Serverless2-Admission-Reason"  # `paused`: this refusal is the pause, not a key limit
CODE = "ADMISSION_PAUSED"  # the detail's prefix: a client distinguishes it from a 429 limit without guessing


def check(tenant: str, kind: str = "submit") -> None:
    """Raise 503 + Retry-After + `X-Admission-State: not-admitted` for a submission of a paused tenant (the one
    check every submission route makes). `kind="export"` (a transfer-only retry of a finished run) is refused only
    when the pause says `exports: true`: an application draining old work keeps its exports."""
    st = paused_for(tenant)
    if not st or (kind == "export" and not st.get("exports")):
        return
    raise HTTPException(
        503,
        f"{CODE}: submissions paused: {st.get('reason') or 'fleet maintenance'} (until {st.get('until')}); nothing "
        "was created, retry after the pause",
        headers={
            "Retry-After": str(st.get("retry_after_s") or RETRY_AFTER_DEFAULT_S),
            ADMISSION_HEADER: "refused",
            REASON_HEADER: "paused",  # the 429 limit refusal carries no reason header: a caller tells them apart here
        },
    )


def pause(
    tenants: list[str] | None,
    reason: str,
    retry_after_s: int | None,
    max_s: int | None,
    by: str,
    exports: bool = False,
) -> dict:
    if not db.enabled():
        raise HTTPException(503, "the submission pause needs the database")
    retry_after = int(retry_after_s or RETRY_AFTER_DEFAULT_S)
    if not (1 <= retry_after <= RETRY_AFTER_MAX_S):
        raise HTTPException(400, f"retry_after_s: 1..{RETRY_AFTER_MAX_S}")
    bound = int(max_s or MAX_S)
    if not (1 <= bound <= MAX_S):
        raise HTTPException(400, f"max_s: 1..{MAX_S}")
    now = time.time()
    value = {
        "paused": True,
        "tenants": sorted(set(tenants)) if tenants else None,
        "reason": (reason or "")[:300],
        "retry_after_s": retry_after,
        "exports": bool(exports),
        "since": iso(datetime.now(timezone.utc)),
        "until": iso(datetime.fromtimestamp(now + bound, timezone.utc)),
        "by": by,
    }
    _write(value)
    return value


def resume(by: str) -> dict:
    if not db.enabled():
        raise HTTPException(503, "the submission pause needs the database")
    _write(None)
    return {"paused": False, "resumed_at": iso(datetime.now(timezone.utc)), "by": by}
