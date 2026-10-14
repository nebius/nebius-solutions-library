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
- idempotent creation (`Idempotency-Key`, owner-scoped): a lost create answer is recovered by the same request under
  the same key: the same session, one row, one admission; while the session is still `claimed` (unexpired, not
  cancelled) the replay answers a usable claim that REPLACES the earlier one (the earlier token no longer binds: still
  exactly one connection ever binds); once bound, attached or ended the replay is the same id without a claim.
  Nothing is extended (`expires_at`, `deadline_at` stay); the same key with another request (model, deployment,
  timeout_s) is 409. The semantic request hash is over the effective values, not the bytes sent.

State lives in the database; every transition is a single conditional UPDATE (fencing). Settlement on the key's
ledger happens once per session (`settled_at`), retried by the sweep if the process died in between."""

import hashlib
import json
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
    "cancel_reason, ended_at, outcome, gpu_seconds, usage, settled_at, ready_seen_at, attach_deadline_at, "
    "idempotency_key, request_hash"
)
IDEMPOTENCY_KEY_MAX = 200


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


def phase_of(s: dict) -> str:
    """The truthful place of a session: `claimed` (no connection yet), `starting` (bound; the endpoint has shown no
    Ready replica since: a pod may be cold-starting, no GPU is allocated to this session), `waiting_for_worker`
    (bound; a Ready replica was observed, the worker has not attached: the attach wait runs), `attached`, `ended`."""
    if s["state"] == "bound":
        return "waiting_for_worker" if s.get("ready_seen_at") else "starting"
    return s["state"]


def public(s: dict) -> dict:
    """The owner's view of a session (no pod identity, no key hash, no token)."""
    return {
        "id": s["id"],
        "model": s["model"],
        "cluster": s["cluster"],
        "region": s["region"],
        "status": s["state"],
        "phase": phase_of(s),
        "ready_seen_at": _ts(s.get("ready_seen_at")),
        "attach_deadline_at": _ts(s.get("attach_deadline_at")),
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


def transport_deadline_s(m: dict) -> int | None:
    """The longest connection the endpoint's transport carries (docs/API.md "Sessions"): a single-node (Knative)
    endpoint ends every request at its revision timeout (`timeout_s` of the model, the catalog's `runtime.timeout`,
    600 s by default; the fleet's Knative config bounds it), WebSocket or NDJSON alike, with no record on the
    session; a multi-node endpoint's route carries no request timeout (`request: 0s`). None = no transport bound."""
    if int(m.get("nodes") or 1) > 1:
        return None
    t = (m.get("runtime") or {}).get("timeout")
    try:
        return int(t) if t is not None else 600
    except (TypeError, ValueError):
        return 600


def open_on(model: str) -> int:
    """Sessions bound or attached on a model right now: an idle-shutdown must count them as activity (the stream
    makes no stamped call of its own). 0 without the feature."""
    if not enabled():
        return 0
    with db.pool().connection() as c:
        r = c.execute(
            "select count(*) from sessions where model = %s and state in ('bound', 'attached')", (model,)
        ).fetchone()
    return int(r[0]) if r else 0


# ---- transitions ------------------------------------------------------------------------------------------------
def request_hash(model: str, cluster: str | None, region: str, timeout_s: int, gpus: int) -> str:
    """The semantic identity of a create request: the EFFECTIVE values (defaults applied, the deployment resolved),
    never the bytes the client sent: `{}` and `{"timeout_s": <the default>}` are one request; another deployment or
    timeout is another request."""
    doc = {"model": model, "cluster": cluster, "region": region, "timeout_s": int(timeout_s), "gpus": int(gpus)}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def find_replay(tenant: str, owner: str, idempotency_key: str) -> dict | None:
    """The session an owner's Idempotency-Key names (owner-scoped: another owner's same key is another namespace)."""
    with db.pool().connection() as c:
        r = c.execute(
            f"select {COLUMNS} from sessions where tenant = %s and owner = %s and idempotency_key = %s",
            (tenant, owner, idempotency_key),
        ).fetchone()
    return _row(r) if r else None


def replay(s: dict, h: str) -> dict:
    """The same request under the key of an existing session: the session as it is (`replay: true`). While it is
    still `claimed` (unexpired, before the deadline, not cancelled) the answer carries a usable claim that REPLACES
    the earlier token (compare-and-swap on `claimed`: a bind racing with this either took the old token first, then
    no claim is issued here, or finds the old token gone, 401); so exactly one connection ever binds. Once bound,
    attached or ended: the same id, no claim, nothing re-issued. Nothing is extended: `expires_at` and `deadline_at`
    stay as admitted. The key with another request is 409."""
    if s.get("request_hash") != h:
        raise HTTPException(
            409, "Idempotency-Key was already used with a different session request (model, region or timeout_s)"
        )
    out = {**public(s), "replay": True}
    if s["state"] == "claimed" and s["cancel_requested_at"] is None:
        token = secrets.token_urlsafe(32)
        with db.pool().connection() as c, c.transaction():
            r = c.execute(
                "update sessions set claim_hash = %s where id = %s and state = 'claimed' and expires_at > now() "
                "and deadline_at > now() and cancel_requested_at is null returning id",
                (_hash(token), s["id"]),
            ).fetchone()
        if r is not None:
            out["claim_token"] = token  # the earlier token binds no more
    return out


def create(
    key_hash: str,
    tenant: str,
    owner: str,
    model: str,
    cluster: str | None,
    region: str,
    timeout_s: int,
    gpus: int,
    idempotency_key: str | None = None,
) -> dict:
    """Admit and record a session (`replay: false`, a fresh claim). Raises keys.KeyLimit as a run's admission does.
    With an `idempotency_key` (owner-scoped) the same request again answers the existing session (`replay`); a
    concurrent duplicate loses the unique index, releases its own admission and answers the winner's session."""
    h = request_hash(model, cluster, region, timeout_s, gpus)
    if idempotency_key:
        s = find_replay(tenant, owner, idempotency_key)
        if s is not None:
            return replay(s, h)
    sid = f"ses-{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(32)
    bound = float(gpus * timeout_s)
    keys.admit(key_hash, sid, tenant, gpu_seconds=bound, counts_request=True, run_member=True)
    now = datetime.now(timezone.utc)
    try:
        with db.pool().connection() as c, c.transaction():
            c.execute(
                "insert into sessions (id, tenant, owner, key_hash, model, cluster, region, claim_hash, gpu_bound, expires_at, deadline_at, idempotency_key, request_hash) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    idempotency_key,
                    h,
                ),
            )
    except Exception as e:
        keys.release(sid, 0.0)  # nothing was bound: the slot goes back
        import psycopg

        if idempotency_key and isinstance(e, psycopg.errors.UniqueViolation):
            s = find_replay(tenant, owner, idempotency_key)  # the concurrent duplicate that won
            if s is not None:
                return replay(s, h)
        raise
    s = get(sid)
    return {**public(s), "claim_token": token, "replay": False}


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
    import leases

    return {"pod": pod, "namespace": ns, "uid": uid, "region": region, "restart_count": leases.worker_restarts(obj)}


# ---- the sweep (reconcile loop) -------------------------------------------------------------------------------------
def endpoint_ready(model: str, region: str, cache: dict) -> bool | None:
    """Whether the session's endpoint has a Ready replica in its region: True, False, or None when the status cannot
    be read (unknown is unknown: never Ready, never idle). One read per (model, region) per sweep (`cache`)."""
    key = (model, region)
    if key in cache:
        return cache[key]
    verdict: bool | None = None
    try:
        import catalog

        m = catalog.get(model)
        if m and m.get("k8s_name") and m.get("namespace"):
            st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m.get("nodes") or 1))
            status = (st or {}).get("status")
            verdict = (
                True
                if status == "ready"
                else (False if status in ("scaled-to-zero", "deploying", "unavailable", "error") else None)
            )
    except Exception as e:  # noqa: BLE001 - unknown, not a verdict
        log.debug("session sweep: endpoint status of %s in %s unknown: %s", model, region, e)
        verdict = None
    cache[key] = verdict
    return verdict


def sweep() -> list[str]:
    """Every 15 s on each replica (idempotent): a claim nobody bound before it expired ends at zero (no worker was
    reached); a bound session is `starting` until its endpoint shows a Ready replica, which is recorded ONCE
    (`ready_seen_at`, never reset by a later flap) together with the attach deadline (`attach_wait_s` from then,
    capped by the admitted `deadline_at`); a bound session no worker attached by that deadline, or that never saw a
    Ready replica by its admitted deadline, ends `unattached` with usage unknown and is fenced; an attached session
    past its deadline gets a cancel request; an attached session whose pod (exact UID) is gone or finished ends
    unknown; ended sessions left unsettled by a crash are settled. Endpoint status is read once per (model, region)
    per sweep. Returns `<id>:<what>`."""
    if not enabled():
        return []
    out = []
    with db.pool().connection() as c:
        rows = c.execute(f"select {COLUMNS} from sessions where state <> 'ended' or settled_at is null").fetchall()
    now = datetime.now(timezone.utc)
    ready_cache: dict = {}
    for r in rows:
        s = _row(r)
        sid = s["id"]
        try:
            with db.pool().connection() as c, c.transaction():
                if s["state"] == "claimed" and s["expires_at"] <= now:
                    if _end(c, sid, "claimed", "expired", 0.0, "none"):
                        out.append(f"{sid}:expired")
                elif s["state"] == "bound":
                    if s["deadline_at"] <= now:
                        # the hard bound: the admitted deadline, whatever the endpoint did (never-ready included)
                        if _end(c, sid, "bound", "unattached", None, "unknown"):
                            out.append(f"{sid}:unattached")
                    elif s["ready_seen_at"] is None:
                        if endpoint_ready(s["model"], s["region"], ready_cache) is True:
                            # recorded once: a compare-and-swap on ready_seen_at, so concurrent sweeps and later flaps
                            # never move the attach deadline
                            n = c.execute(
                                "update sessions set ready_seen_at = now(), attach_deadline_at = least(now() + %s * interval '1 second', deadline_at) "
                                "where id = %s and state = 'bound' and ready_seen_at is null",
                                (cfg.SESSION_ATTACH_WAIT_S, sid),
                            ).rowcount
                            if n:
                                out.append(f"{sid}:ready-seen")
                    elif s["attach_deadline_at"] is not None and s["attach_deadline_at"] <= now:
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
