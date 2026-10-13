"""Live sessions (docs/API.md "Sessions"): an admitted stream to an endpoint for workers that hold GPU state across one
long connection (speech). One session = one claim, one connection, one worker pod, released once.

- `create`: admitted on the caller's key exactly as a run's root (one slot, one request, a GPU bound of the endpoint's
  GPUs x timeout for a key with a GPU-second budget; keys.admit run_member) and answers a one-time claim token.
- `bind` (the gateway's authorization hook, on the WebSocket upgrade that carries `s2-session.<token>`): a
  compare-and-swap `claimed -> bound` with a server-generated connection id; exactly one upgrade ever binds a session,
  across API replicas; the hook injects the session and connection ids towards the worker and refuses any such header
  a client supplied.
- `attach` (the worker pod, with its own bound ServiceAccount token): `bound -> attached` for the right connection id
  only; the pod's identity (namespace, name, UID, the endpoint it belongs to, the cluster) is verified server-side
  through TokenReview and the pod object, never from headers. A worker feeds the stream only after a successful
  attach. After the attach wait a bound session ends `unattached` with usage unknown and is fenced: a late attach
  fails for good.
- `release` (the attached pod, after its own post-done cleanup): `attached -> ended` with the outcome and the GPU
  seconds it measured (null = unknown: the bound stays against the budget). Idempotent for the same pod.
- `cancel` (the owner), the deadline, and a client's disconnect are requests: nothing is released on them; the
  attached worker observes `cancel_requested` (`GET /internal/sessions/<id>`) and releases after cleanup. Only the
  worker's release, or the exact disappearance of the attached pod (UID), ends an attached session (usage unknown).
  Never a replica-wide drain. No reconnect: a dropped connection ends at the worker; the token binds once.

State lives in the database; every transition is a single conditional UPDATE (fencing). Settlement on the key's
ledger happens once per session (`settled_at`), retried by the sweep if the process died in between."""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import config as cfg
import db
import keys
import kube
from resilience import retry

log = logging.getLogger("api")
PROTOCOL_PREFIX = "s2-session."  # Sec-WebSocket-Protocol entry carrying the claim token (WebSocket workers)
CLAIM_HEADER = "x-serverless2-session-claim"  # the claim token on a plain HTTP request (NDJSON POST workers)
SESSION_HEADER = "x-serverless2-session"  # injected by the hook towards the worker; refused from clients
CONNECTION_HEADER = "x-serverless2-connection"
INJECTED = (SESSION_HEADER, CONNECTION_HEADER)
OUTCOMES = ("done", "cancelled", "disconnected", "busy", "error")
COLUMNS = (
    "id, tenant, owner, key_hash, model, cluster, region, state, connection, pod, pod_namespace, pod_uid, "
    "worker_session_id, gpu_bound, created_at, expires_at, deadline_at, bound_at, attached_at, cancel_requested_at, "
    "cancel_reason, ended_at, outcome, gpu_seconds, usage, settled_at"
)


def enabled() -> bool:
    return cfg.SESSIONS_ENABLED and db.enabled()


def require() -> None:
    if not enabled():
        raise HTTPException(404, "sessions are not enabled on this fleet")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r) -> dict:
    return dict(zip([c.strip() for c in COLUMNS.split(",")], r))


def _ts(t) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def public(s: dict) -> dict:
    """The owner's view of a session (no pod identity, no key hash)."""
    return {
        "id": s["id"],
        "model": s["model"],
        "cluster": s["cluster"],
        "region": s["region"],
        "status": s["state"],
        "outcome": s["outcome"],
        "cancel_requested": s["cancel_requested_at"] is not None,
        "cancel_reason": s["cancel_reason"],
        "attached": s["attached_at"] is not None,
        "worker_session_id": s["worker_session_id"],
        "created_at": _ts(s["created_at"]),
        "expires_at": _ts(s["expires_at"]),
        "deadline_at": _ts(s["deadline_at"]),
        "bound_at": _ts(s["bound_at"]),
        "attached_at": _ts(s["attached_at"]),
        "ended_at": _ts(s["ended_at"]),
        "gpu_seconds": float(s["gpu_seconds"]) if s["gpu_seconds"] is not None else None,
        "usage": s["usage"],
    }


def gpus_per_replica(m: dict) -> int:
    """The catalog's `gpu` ("2x h100", "none"): GPUs one replica of the endpoint holds."""
    g = str(m.get("gpu") or "")
    try:
        return int(g.split("x", 1)[0]) if "x" in g else 0
    except ValueError:
        return 0


# ---- transitions ------------------------------------------------------------------------------------------------
def create(
    key_hash: str, tenant: str, owner: str, model: str, cluster: str | None, region: str, timeout_s: int, gpus: int
) -> dict:
    """Admit and record a session. Raises keys.KeyLimit as a run's admission does."""
    sid = f"ses-{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(32)
    bound = float(gpus * timeout_s)
    keys.admit(key_hash, sid, tenant, gpu_seconds=bound, counts_request=True, run_member=True)
    now = datetime.now(timezone.utc)
    try:
        with db.pool().connection() as c, c.transaction():
            c.execute(
                "insert into sessions (id, tenant, owner, key_hash, model, cluster, region, claim_hash, gpu_bound, expires_at, deadline_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    sid,
                    tenant,
                    owner,
                    key_hash,
                    model,
                    cluster,
                    region,
                    _hash(token),
                    bound,
                    now + timedelta(seconds=cfg.SESSION_CLAIM_TTL_S),
                    now + timedelta(seconds=timeout_s),
                ),
            )
    except Exception:
        keys.release(sid, 0.0)  # nothing was bound: the slot goes back
        raise
    s = get(sid)
    return {**public(s), "claim_token": token}


def get(sid: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(f"select {COLUMNS} from sessions where id = %s", (sid,)).fetchone()
    return _row(r) if r else None


def list_owner(tenant: str, owner: str, limit: int = 100) -> list[dict]:
    with db.pool().connection() as c:
        rows = c.execute(
            f"select {COLUMNS} from sessions where tenant = %s and owner = %s order by created_at desc limit %s",
            (tenant, owner, limit),
        ).fetchall()
    return [public(_row(r)) for r in rows]


def claim_of(headers) -> str | None:
    """The claim token a request carries: `Sec-WebSocket-Protocol: s2-session.<token>` (an upgrade) or the header
    `x-serverless2-session-claim: <token>` (a plain HTTP request, e.g. an NDJSON POST). Both transports bind the same
    way; the token is read here and never logged (the gateways log no request headers)."""
    token = (headers.get(CLAIM_HEADER) or "").strip()
    if token:
        return token
    for protocol in (headers.get("sec-websocket-protocol") or "").split(","):
        protocol = protocol.strip()
        if protocol.startswith(PROTOCOL_PREFIX):
            return protocol[len(PROTOCOL_PREFIX) :]
    return None


def bind(token: str, model: str) -> dict:
    """The upgrade that carries a claim: exactly one binds (compare-and-swap on `claimed` with the claim's hash,
    unexpired, before the deadline, for this model). 401 for anything else: an unknown, used, expired or cancelled
    claim, or a claim for another model."""
    connection = uuid.uuid4().hex
    with db.pool().connection() as c, c.transaction():
        r = c.execute(
            "update sessions set state = 'bound', connection = %s, bound_at = now() "
            "where claim_hash = %s and state = 'claimed' and expires_at > now() and deadline_at > now() "
            "and cancel_requested_at is null and model = %s returning id, tenant, key_hash",
            (connection, _hash(token), model),
        ).fetchone()
    if r is None:
        raise HTTPException(
            401, "session claim not accepted: unknown, already used, expired, cancelled or another model"
        )
    return {"id": r[0], "tenant": r[1], "key_hash": r[2], "connection": connection}


def attach(sid: str, connection: str, worker: dict, worker_session_id: str | None) -> dict:
    """The verified worker pod takes the bound session: `bound -> attached` for the hook's connection id only; once."""
    with db.pool().connection() as c, c.transaction():
        r = c.execute(
            "update sessions set state = 'attached', attached_at = now(), pod = %s, pod_namespace = %s, pod_uid = %s, worker_session_id = %s "
            "where id = %s and state = 'bound' and connection = %s and deadline_at > now() returning id",
            (worker["pod"], worker["namespace"], worker["uid"], worker_session_id, sid, connection),
        ).fetchone()
        if r is None:
            s = c.execute("select state, connection, pod_uid from sessions where id = %s", (sid,)).fetchone()
            if s is None:
                raise HTTPException(404, "no such session")
            if s[0] == "attached" and s[2] == worker["uid"] and s[1] == connection:
                return get(sid)  # the same worker's retry
            raise HTTPException(
                409,
                f"session is {s[0]}; attach needs a bound session with this connection id (fenced: no late or second attach)",
            )
    return get(sid)


def release(sid: str, worker: dict, outcome: str, gpu_seconds: float | None) -> dict:
    """The attached worker, after its cleanup: `attached -> ended`; the key's slot goes, usage measured or unknown.
    Idempotent for the same pod."""
    if outcome not in OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {OUTCOMES}")
    with db.pool().connection() as c, c.transaction():
        r = c.execute(
            "update sessions set state = 'ended', ended_at = now(), outcome = %s, gpu_seconds = %s, usage = %s "
            "where id = %s and state = 'attached' and pod_uid = %s returning id",
            (outcome, gpu_seconds, "measured" if gpu_seconds is not None else "unknown", sid, worker["uid"]),
        ).fetchone()
        if r is None:
            s = c.execute("select state, pod_uid from sessions where id = %s", (sid,)).fetchone()
            if s is None:
                raise HTTPException(404, "no such session")
            if s[0] == "ended" and s[1] == worker["uid"]:
                return settle(sid)  # the same worker's retry: settled once
            raise HTTPException(409, f"session is {s[0]}; only its attached worker releases it")
    return settle(sid)


def cancel(sid: str, reason: str) -> dict:
    """A cancellation request: a claimed session (no connection ever) ends now at zero; a bound or attached one keeps
    its slot until the worker releases it or its pod is gone (the sweep)."""
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "update sessions set cancel_requested_at = coalesce(cancel_requested_at, now()), cancel_reason = coalesce(cancel_reason, %s) "
            "where id = %s and state <> 'ended'",
            (reason, sid),
        )
        c.execute(
            "update sessions set state = 'ended', ended_at = now(), outcome = 'cancelled', gpu_seconds = 0, usage = 'none' "
            "where id = %s and state = 'claimed'",
            (sid,),
        )
    return settle(sid)


def _end(c, sid: str, from_state: str, outcome: str, gpu_seconds: float | None, usage: str) -> bool:
    return (
        c.execute(
            "update sessions set state = 'ended', ended_at = now(), outcome = %s, gpu_seconds = %s, usage = %s "
            "where id = %s and state = %s returning id",
            (outcome, gpu_seconds, usage, sid, from_state),
        ).fetchone()
        is not None
    )


def settle(sid: str) -> dict:
    """The ended session's member on the key's ledger: measured seconds spent, unknown kept against the budget, none
    (never bound, nothing ran) at zero. Once; the sweep retries what a crash left unsettled."""
    s = get(sid)
    if s is None:
        raise HTTPException(404, "no such session")
    if s["state"] != "ended" or s["settled_at"] is not None:
        return s
    value = None if s["usage"] == "unknown" else float(s["gpu_seconds"] or 0)
    try:
        keys.release(sid, value)
    except Exception as e:  # noqa: BLE001 - the sweep settles it on the next pass
        log.warning("session %s: settlement deferred: %s", sid, e)
        return s
    with db.pool().connection() as c, c.transaction():
        c.execute("update sessions set settled_at = now() where id = %s and settled_at is null", (sid,))
    return get(sid)


# ---- the worker's identity ----------------------------------------------------------------------------------------
def verify_worker(token: str, s: dict, endpoint_model) -> dict:
    """The pod behind a worker's bound ServiceAccount token, on the session's cluster: TokenReview (the configured
    audience or the cluster's), the pod's namespace, name and UID from the token's claims, the pod object itself
    (same UID; the endpoint's own pods: the InferenceService or LeaderWorkerSet of the session's model). Nothing of it
    comes from request headers. `endpoint_model(label)` maps a predictor label to its catalog model."""
    from kubernetes import client

    region = s["region"]
    spec = client.V1TokenReviewSpec(token=token)
    if cfg.SESSION_TOKEN_AUDIENCE:
        spec.audiences = [cfg.SESSION_TOKEN_AUDIENCE]
    try:
        review = retry(
            client.AuthenticationV1Api(kube._client(region)).create_token_review, client.V1TokenReview(spec=spec)
        )
    except (ApiException, OSError) as e:
        raise HTTPException(502, f"token review on {region}: {str(e)[:120]}")
    st = review.status
    if not st or not st.authenticated:
        raise HTTPException(401, "worker token not accepted by the session's cluster")
    parts = (st.user.username or "").split(":")
    if len(parts) != 4 or parts[0] != "system" or parts[1] != "serviceaccount":
        raise HTTPException(401, "not a pod identity")
    extra = st.user.extra or {}
    pod = (extra.get("authentication.kubernetes.io/pod-name") or [""])[0]
    uid = (extra.get("authentication.kubernetes.io/pod-uid") or [""])[0]
    if not pod or not uid:
        raise HTTPException(401, "token is not bound to a pod")
    ns = parts[2]
    try:
        obj = retry(kube.core(region).read_namespaced_pod, pod, ns)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(401, "the token's pod does not exist")
        raise HTTPException(502, f"pod lookup on {region}: {e.reason}")
    md = obj.metadata
    if md.uid != uid:
        raise HTTPException(401, "the token's pod UID does not match the pod")
    labels = md.labels or {}
    label = labels.get("serving.kserve.io/inferenceservice") or labels.get("leaderworkerset.sigs.k8s.io/name")
    if not label:
        raise HTTPException(403, "the pod is not an endpoint worker")
    try:
        m = endpoint_model(label)
    except HTTPException:
        raise HTTPException(403, "the pod belongs to no endpoint of this fleet")
    if m["name"] != s["model"] or ns != m["namespace"]:
        raise HTTPException(403, f"the pod serves {m['name']}, the session is for {s['model']}")
    return {"pod": pod, "namespace": ns, "uid": uid, "region": region}


# ---- the sweep (reconcile loop) -------------------------------------------------------------------------------------
def sweep() -> list[str]:
    """Every 15 s on each replica (idempotent): a claim nobody bound before it expired ends at zero (no worker was
    reached); a bound session no worker attached within the attach wait ends unknown and is fenced; an attached
    session past its deadline gets a cancel request; an attached session whose pod (exact UID) is gone or finished
    ends unknown; ended sessions left unsettled by a crash are settled. Returns `<id>:<what>`."""
    if not enabled():
        return []
    out = []
    with db.pool().connection() as c:
        rows = c.execute(f"select {COLUMNS} from sessions where state <> 'ended' or settled_at is null").fetchall()
    now = datetime.now(timezone.utc)
    for r in rows:
        s = _row(r)
        sid = s["id"]
        try:
            with db.pool().connection() as c, c.transaction():
                if s["state"] == "claimed" and s["expires_at"] <= now:
                    if _end(c, sid, "claimed", "expired", 0.0, "none"):
                        out.append(f"{sid}:expired")
                elif s["state"] == "bound" and s["bound_at"] + timedelta(seconds=cfg.SESSION_ATTACH_WAIT_S) <= now:
                    if _end(c, sid, "bound", "unattached", None, "unknown"):
                        out.append(f"{sid}:unattached")
                elif s["state"] == "attached":
                    if s["deadline_at"] <= now and s["cancel_requested_at"] is None:
                        c.execute(
                            "update sessions set cancel_requested_at = now(), cancel_reason = 'deadline' where id = %s and cancel_requested_at is null",
                            (sid,),
                        )
                        out.append(f"{sid}:cancel-requested")
                    if _worker_gone(s):
                        if _end(c, sid, "attached", "worker_lost", None, "unknown"):
                            out.append(f"{sid}:worker-lost")
            settled = settle(sid)
            if (
                settled.get("state") == "ended"
                and settled.get("settled_at")
                and s["settled_at"] is None
                and s["state"] == "ended"
            ):
                out.append(f"{sid}:settled")
        except Exception:  # noqa: BLE001
            log.exception("session sweep: %s", sid)
    return out


def _worker_gone(s: dict) -> bool:
    """The attached pod, by exact UID, no longer exists or finished: confirmed worker loss."""
    try:
        obj = retry(kube.core(s["region"]).read_namespaced_pod, s["pod"], s["pod_namespace"])
    except ApiException as e:
        if e.status == 404:
            return True
        raise
    if obj.metadata.uid != s["pod_uid"]:
        return True
    return (obj.status.phase if obj.status else None) in ("Succeeded", "Failed")
