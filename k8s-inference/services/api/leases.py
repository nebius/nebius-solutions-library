"""Native execution leases (docs/API.md "Native execution leases"): the worker side of one admitted endpoint call,
so that the operation's native reservation (its key slot, request and GPU bound) outlives the CPU caller and a cancel
until the worker settled or its loss is confirmed. One row per (operation, attempt) in `native_executions`, on the
existing run ledger (the operation's member; `keys.release` is called once, at the join); no second queue, no
scheduler. Every row is kept: the evidence of each attempt (lane occupancy, measured or unknown GPU time) is summed
at settlement, never overwritten.

Lifecycle (every transition a single conditional UPDATE under the operation's advisory lock; state in the database,
API restarts change nothing):

- `prepare` (the gateway hook, on the verified inference POST of an admitted caller attempt): one live execution per
  operation. A POST of another attempt while one is prepared or attached is refused at the hook (409): a caller's
  retry pod cannot overlap the worker's work on the first request. The same attempt's POST replays its row; after a
  terminal row the next attempt gets its own row.
- `attach` (the worker pod, pod-authenticated like a session's worker, atomically, before it enqueues GPU work):
  `prepared -> attached` for the attempt the hook prepared, only while the operation's reservation is live and no
  cancel was requested and the row is not closed; a stale attempt, another pod, a closed row (the caller is over and
  the join decided), a cancel before attach or a released reservation is 409 and the worker fails closed. Idempotent
  for the same (operation, attempt, pod UID, worker_request_id).
- `release` (the attached pod, after its own settlement: natural completion, or the stop ACK of a drain):
  `attached -> released` for that attempt, pod and request id only, with the outcome, the wall-clock lane occupancy
  (attached_at -> now, always) and the GPU seconds the worker measured (null = unknown; never zero). Idempotent for
  the same tuple.
- `lost` (the reconciler): the attached pod's exact UID is gone or terminated. Never from readiness, a timeout, or an
  API lookup that failed: those keep the execution attached and the slot held.
- `closed` (the join): the caller is over and the live row was never attached: the join closes it in the same
  transaction that decides the settlement, so a delayed worker can no longer attach and enqueue GPU work after the
  reservation went.
- cancel: a request (`cancel_requested_at`) the attached worker reads; it releases nothing; before attach it refuses
  the attach.

Settlement is a JOIN, never first-one-wins: the operation's member settles when the caller is terminal (the caller
Job finished: billing.bill_once) AND no execution of the operation is attached. The worker's release before the
caller finished frees nothing; the caller's end before the worker released frees nothing. The settled value sums every
terminal row's measured GPU seconds, or is unknown when any attached-then-ended row has none (or the worker was lost).
Required mode (`native_lease: required` on the model): a caller that ended with no attached execution settles unknown
(never zero) and the handle says `unattached`."""

import logging

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import db
import keys
import kube
from resilience import retry

log = logging.getLogger("api")
STATES = ("prepared", "attached", "released", "lost", "closed")
LIVE = ("prepared", "attached")
OUTCOMES = ("done", "cancelled", "error", "disconnected")
COLUMNS = (
    "operation, attempt, state, pod, pod_namespace, pod_uid, worker_request_id, region, model, prepared_at, "
    "attached_at, released_at, outcome, gpu_seconds, lane_seconds, cancel_requested_at, cancel_reason, settled_at, "
    "restart_count, lost_reason"
)


def enabled() -> bool:
    return db.enabled()


def _row(r) -> dict:
    return dict(zip([c.strip() for c in COLUMNS.split(",")], r))


def _ts(t) -> str | None:
    if not t:
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(t, "strftime") else str(t)


def _lock(c, operation: str) -> None:
    c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 3))", (operation,))


def _fenced(c, operation: str) -> bool:
    """The operation's native executions are over for good (the join decided; durable across the ledger release and
    any restart): no prepare, no attach after it."""
    return c.execute("select 1 from native_fences where operation = %s", (operation,)).fetchone() is not None


def public(e: dict | None, history: list[dict] | None = None) -> dict | None:
    """The handle's `native` block: the current execution and every earlier attempt's evidence; no pod names."""
    if e is None:
        return None

    def one(x: dict) -> dict:
        return {
            "state": x["state"],
            "attempt": x["attempt"],
            "worker_request_id": x["worker_request_id"],
            "prepared_at": _ts(x["prepared_at"]),
            "attached_at": _ts(x["attached_at"]),
            "released_at": _ts(x["released_at"]),
            "outcome": x["outcome"],
            "lane_seconds": float(x["lane_seconds"]) if x["lane_seconds"] is not None else None,
            "gpu_seconds": float(x["gpu_seconds"]) if x["gpu_seconds"] is not None else None,
            "cancel_requested": x["cancel_requested_at"] is not None,
            "settled": x["settled_at"] is not None,
            "lost_reason": x.get("lost_reason"),
        }

    out = one(e)
    if history:
        out["attempts"] = [one(x) for x in history]
    return out


def get(operation: str) -> dict | None:
    """The operation's current execution: the live row, else the newest terminal one."""
    with db.pool().connection() as c:
        r = c.execute(
            f"select {COLUMNS} from native_executions where operation = %s "
            "order by (state in ('prepared', 'attached')) desc, prepared_at desc limit 1",
            (operation,),
        ).fetchone()
    return _row(r) if r else None


def history(operation: str) -> list[dict]:
    with db.pool().connection() as c:
        rows = c.execute(
            f"select {COLUMNS} from native_executions where operation = %s order by prepared_at", (operation,)
        ).fetchall()
    return [_row(r) for r in rows]


# ---- transitions ------------------------------------------------------------------------------------------------
def prepare(operation: str, attempt: str, region: str, model: str, caller_gone=None) -> dict:
    """The hook, on the verified inference POST of `attempt`: the one live execution of the operation. Another
    attempt's POST while one is prepared or attached is refused (409); the same attempt replays its row; after a
    terminal row the new attempt gets its own row (the earlier evidence is kept). `caller_gone(attempt)`: the hook's
    exact read of an earlier caller pod (gone or finished, by name in the caller's namespace): a row that earlier
    caller only *prepared* (the worker never attached) is closed so its replacement pod (a preempted caller's) can
    post; an attached row is never freed this way (the worker settles it, or the reconciler confirms its loss)."""
    with db.pool().connection() as c, c.transaction():
        _lock(c, operation)
        if _fenced(c, operation):
            raise HTTPException(
                409, f"operation {operation}: its native executions are over (the caller finished and the join decided)"
            )
        live = c.execute(
            "select attempt, state from native_executions where operation = %s and state in ('prepared', 'attached')",
            (operation,),
        ).fetchone()
        if live and live[0] != attempt and live[1] == "prepared" and caller_gone and caller_gone(live[0]):
            c.execute(
                "update native_executions set state = 'closed', released_at = now(), outcome = 'caller replaced' "
                "where operation = %s and attempt = %s and state = 'prepared'",
                (operation, live[0]),
            )
            live = None
        if live and live[0] != attempt:
            raise HTTPException(
                409,
                f"operation {operation}: attempt {live[0]} holds its native execution ({live[1]}); attempt {attempt} waits "
                "until the worker settled or its loss is confirmed",
            )
        if live is None:
            own = c.execute(
                "select state from native_executions where operation = %s and attempt = %s", (operation, attempt)
            ).fetchone()
            if own is None:
                c.execute(
                    "insert into native_executions (operation, attempt, state, region, model) values (%s, %s, 'prepared', %s, %s)",
                    (operation, attempt, region, model),
                )
            # else: this attempt's own row is terminal (released, lost, closed): the request is over for it; no new
            # execution for the same attempt (a new pod is a new attempt)
    return get(operation)


def attach(operation: str, attempt: str, worker: dict, worker_request_id: str | None) -> dict:
    """The verified worker pod, before any GPU enqueue: `prepared -> attached` for the prepared attempt, while the
    operation's reservation is live, no cancel was requested and the row is not closed. Anything else is 409 and the
    worker fails closed. Idempotent for the same (attempt, pod UID, request id)."""
    with db.pool().connection() as c, c.transaction():
        _lock(c, operation)
        if _fenced(c, operation):
            raise HTTPException(409, f"operation {operation}: its native executions are over; attach refused (fenced)")
        cur = c.execute(
            "select state, pod_uid, worker_request_id, cancel_requested_at from native_executions "
            "where operation = %s and attempt = %s for update",
            (operation, attempt),
        ).fetchone()
        if cur is None:
            live = c.execute(
                "select attempt, state from native_executions where operation = %s and state in ('prepared', 'attached')",
                (operation,),
            ).fetchone()
            if live:
                raise HTTPException(
                    409,
                    f"operation {operation}: native execution is {live[1]} for attempt {live[0]}; attach of attempt "
                    f"{attempt} refused (stale attempt or another caller pod)",
                )
            raise HTTPException(
                409,
                f"operation {operation}: no prepared native execution for attempt {attempt} (the hook prepares it on the POST)",
            )
        state, uid, wrid, cancel_at = cur
        if state == "attached" and uid == worker["uid"] and (wrid or None) == (worker_request_id or None):
            return get(operation)  # the same worker's retry after a dropped attach response
        if state != "prepared":
            raise HTTPException(
                409, f"operation {operation}: native execution of attempt {attempt} is {state}; attach refused (fenced)"
            )
        if cancel_at is not None:
            raise HTTPException(409, f"operation {operation}: cancelled before the worker attached; attach refused")
        # the operation's live reservation: its own, or its root's for an active member (an endpoint child shares its
        # root's slot and has no row of its own; the first LeRobot child on 109 was refused here with a live root)
        if keys.reservation_key_of(operation) is None:
            raise HTTPException(409, f"operation {operation}: its reservation is released; attach refused")
        r = c.execute(
            "update native_executions set state = 'attached', attached_at = now(), pod = %s, pod_namespace = %s, "
            "pod_uid = %s, worker_request_id = %s, restart_count = %s where operation = %s and attempt = %s "
            "and state = 'prepared' and cancel_requested_at is null returning operation",
            (
                worker["pod"],
                worker["namespace"],
                worker["uid"],
                worker_request_id,
                worker.get("restart_count"),
                operation,
                attempt,
            ),
        ).fetchone()
        if r is None:
            raise HTTPException(409, f"operation {operation}: attach of attempt {attempt} lost the race; refused")
    return get(operation)


def release(
    operation: str, attempt: str, worker: dict, worker_request_id: str | None, outcome: str, gpu_seconds: float | None
) -> dict:
    """The attached pod, after its own settlement: `attached -> released` for exactly this attempt, pod and request id;
    lane occupancy measured by the clock, GPU seconds only as the worker measured them (null = unknown). Idempotent
    for the same tuple. Frees nothing by itself: the member settles at the join (billing.bill_once)."""
    if outcome not in OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {OUTCOMES}")
    with db.pool().connection() as c, c.transaction():
        _lock(c, operation)
        r = c.execute(
            "update native_executions set state = 'released', released_at = now(), outcome = %s, gpu_seconds = %s, "
            "lane_seconds = extract(epoch from (now() - attached_at)) "
            "where operation = %s and attempt = %s and state = 'attached' and pod_uid = %s "
            "and worker_request_id is not distinct from %s returning operation",
            (outcome, gpu_seconds, operation, attempt, worker["uid"], worker_request_id),
        ).fetchone()
        if r is None:
            cur = c.execute(
                "select state, pod_uid, worker_request_id from native_executions where operation = %s and attempt = %s",
                (operation, attempt),
            ).fetchone()
            if (
                cur
                and cur[0] == "released"
                and cur[1] == worker["uid"]
                and (cur[2] or None) == (worker_request_id or None)
            ):
                return get(operation)  # the same worker's retry after a dropped release response
            raise HTTPException(
                409,
                f"operation {operation}: native execution of attempt {attempt} is {cur[0] if cur else 'absent'}; "
                "only its attached worker, with its request id, releases it",
            )
    return get(operation)


def request_cancel(operation: str, reason: str) -> None:
    """A cancel is a request to the attached worker, and refuses a later attach; it releases nothing."""
    with db.pool().connection() as c, c.transaction():
        _lock(c, operation)
        c.execute(
            "update native_executions set cancel_requested_at = coalesce(cancel_requested_at, now()), "
            "cancel_reason = coalesce(cancel_reason, %s) where operation = %s and state in ('prepared', 'attached')",
            (reason, operation),
        )


# ---- the join (billing) ------------------------------------------------------------------------------------------
def settle_decision(operation: str, required: bool) -> tuple[bool, float | None, str]:
    """The caller is over (its Job finished): what that means for the operation's member, decided atomically with the
    rows: (settle_now, gpu_seconds | None, why). An attached execution -> not yet. A prepared (never attached) live row
    is CLOSED here, in this transaction, so a delayed worker cannot attach after the join. Then the terminal rows'
    evidence is summed: unknown when any attached-then-ended row carries no measurement or the worker was lost;
    required mode with nothing ever attached -> unknown ('unattached'); optional mode with nothing attached -> the
    caller's own (zero for an endpoint call)."""
    with db.pool().connection() as c, c.transaction():
        _lock(c, operation)
        rows = [
            _row(r)
            for r in c.execute(
                f"select {COLUMNS} from native_executions where operation = %s order by prepared_at", (operation,)
            ).fetchall()
        ]
        if any(r["state"] == "attached" for r in rows):
            return False, None, "native execution attached: the worker has not settled"
        c.execute(
            "update native_executions set state = 'closed', released_at = now() where operation = %s and state = 'prepared'",
            (operation,),
        )
        # the durable fence, in this transaction: from here no prepare (a new or different attempt's POST) and no
        # attach can open native work for the operation, whatever the ledger does next and whenever the process dies
        c.execute(
            "insert into native_fences (operation, reason) values (%s, 'caller finished; settlement decided') on conflict (operation) do nothing",
            (operation,),
        )
        ended = [r for r in rows if r["state"] in ("released", "lost")]
        if not ended:
            return (True, None, "unattached") if required else (True, 0.0, "no native execution")
        if any(r["state"] == "lost" for r in ended):
            return True, None, "worker lost"
        if any(r["gpu_seconds"] is None for r in ended):
            return True, None, "released, usage unknown"
        total = round(sum(float(r["gpu_seconds"]) for r in ended), 1)
        return True, total, f"released ({', '.join(r['outcome'] for r in ended)})"


def mark_settled(operation: str) -> None:
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "update native_executions set settled_at = now() where operation = %s and settled_at is null", (operation,)
        )


# ---- the worker's identity ----------------------------------------------------------------------------------------
def verify_worker(token: str, e: dict, endpoint_model) -> dict:
    """The pod behind the worker's token, on the execution's cluster, serving the execution's model
    (sessions.verify_worker: TokenReview, pod UID, the endpoint's own pod)."""
    import sessions

    return sessions.verify_worker(token, {"region": e["region"], "model": e["model"]}, endpoint_model)


# ---- the sweep ------------------------------------------------------------------------------------------------------
def sweep() -> list[str]:
    """Attached executions whose pod (exact UID) is gone or terminated are `lost` (usage unknown). A readiness change,
    a timeout or an API lookup that failed changes nothing: the execution stays attached and the slot held."""
    if not enabled():
        return []
    out = []
    with db.pool().connection() as c:
        rows = c.execute(f"select {COLUMNS} from native_executions where state = 'attached'").fetchall()
    for r in rows:
        e = _row(r)
        try:
            gone = _worker_gone(e)
        except Exception as ex:  # noqa: BLE001 - unknown is not lost
            log.warning("native execution %s: worker check deferred: %s", e["operation"], ex)
            continue
        if not gone:
            continue
        why = gone if isinstance(gone, str) else "pod gone"
        with db.pool().connection() as c, c.transaction():
            _lock(c, e["operation"])
            r2 = c.execute(
                "update native_executions set state = 'lost', released_at = now(), outcome = 'lost', lost_reason = %s, "
                "lane_seconds = extract(epoch from (now() - attached_at)) "
                "where operation = %s and attempt = %s and state = 'attached' returning operation",
                (why, e["operation"], e["attempt"]),
            ).fetchone()
        if r2:
            out.append(f"{e['operation']}:lost")
    return out


def caller_gone(namespace: str, pod: str, region: str) -> bool:
    """An endpoint call's caller pod, by exact name in its own namespace: gone (404) or finished. Anything else
    (alive, or an API lookup that failed) keeps its prepared row: fail closed."""
    try:
        obj = retry(kube.core(region).read_namespaced_pod, pod, namespace)
    except ApiException as e:
        return e.status == 404
    except Exception:
        return False
    return (obj.status.phase if obj.status else None) in ("Succeeded", "Failed")


def _worker_gone(e: dict):
    """Confirmed loss only, as a reason (falsy = alive): the pod by exact UID no longer exists, finished, or its worker
    container restarted since the attach (same pod, same UID: the kubelet restarted the engine, its GPU state and the
    request are gone, nothing will release; the restart count of the non-queue-proxy containers is recorded at attach
    and compared here). Readiness, a timeout or an unreadable API never count."""
    try:
        obj = retry(kube.core(e["region"]).read_namespaced_pod, e["pod"], e["pod_namespace"])
    except ApiException as ex:
        if ex.status == 404:
            return "pod gone"
        raise
    if obj.metadata.uid != e["pod_uid"]:
        return "pod replaced"
    if (obj.status.phase if obj.status else None) in ("Succeeded", "Failed"):
        return "pod finished"
    now = worker_restarts(obj)
    if e.get("restart_count") is not None and now > e["restart_count"]:
        return f"worker container restarted ({e['restart_count']} -> {now})"
    return None


def worker_restarts(obj) -> int:
    """The restart count of the worker's containers (every container but Knative's queue-proxy, whose restart loses no
    engine state)."""
    statuses = (obj.status.container_statuses if obj.status else None) or []
    return sum(int(cs.restart_count or 0) for cs in statuses if cs.name != "queue-proxy")


LEASE_MODES = ("optional", "required")


def mode_of(m: dict) -> str:
    return str(m.get("native_lease") or "optional")
