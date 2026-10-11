"""Durable attempt lifecycle of runs (docs/JOBS.md "Attempts and finalization"): the fleet database keeps one row
per attempt (a pod of an operation), written by the uploader of that pod through `POST /internal/attempts` with its
projected ServiceAccount token (verified with a TokenReview against the cluster that issued it: the token is bound
to the pod, so a record can only come from the pod it describes).

Why: pods and Kubernetes Jobs are transient (preemption deletes pods, MultiKueue deletes the worker's copy at
completion, Jobs expire), the tenant bucket does not exist for application-managed I/O, and an attempt that is
replaced must not finalize the operation afterwards. The rows give every operation a stable root, a unique attempt
id (the pod), a monotonic epoch, a revision cursor for polling, the selected cluster/pool/class/image digest, and the
export and finalization state that decides whether "exit 0" became a delivered result.

Only the control API has the database (fleet placement); a regional API without one keeps the bucket records.
"""
import logging
from datetime import datetime, timezone
from fastapi import HTTPException
from kubernetes.client.rest import ApiException
import db, kube
from config import LABEL, REGION, TENANT_NS_PREFIX
from resilience import retry

log = logging.getLogger("api.attempts")

TERMINAL = ("succeeded", "failed", "interrupted")
VALID = TERMINAL + ("started",)


def enabled() -> bool:
    return db.enabled()


def verify_runner_token(token: str) -> tuple[str, str, str]:
    """TokenReview of an uploader's projected token against every cluster this API reaches: (region, namespace,
    pod). 401 when no cluster issued it or it is not a bound pod token of a tenant namespace."""
    from kubernetes import client
    from config import FLEET_MANAGER
    for region in [r for r in kube.regions() if not (FLEET_MANAGER and r == REGION)] or kube.regions():
        try:
            review = retry(client.AuthenticationV1Api(kube._client(region)).create_token_review,
                           client.V1TokenReview(spec=client.V1TokenReviewSpec(token=token)))
        except (ApiException, OSError) as e:
            log.warning("token review in %s: %s", region, str(e)[:120])
            continue
        st = review.status
        if not st or not st.authenticated:
            continue
        user = st.user.username or ""
        parts = user.split(":")
        if len(parts) != 4 or parts[0] != "system" or parts[1] != "serviceaccount" or not parts[2].startswith(TENANT_NS_PREFIX):
            raise HTTPException(401, "not a tenant run identity")
        pod = ((st.user.extra or {}).get("authentication.kubernetes.io/pod-name") or [""])[0]
        if not pod:
            raise HTTPException(401, "token is not bound to a pod")
        return region, parts[2], pod
    raise HTTPException(401, "token not recognised by any cluster of this fleet")


def _ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def record(region: str, ns: str, pod: str, body: dict) -> dict:
    """Upsert the attempt the pod reports. `started` opens the attempt (epoch = the root's next); a terminal status
    from a pod that is no longer the root's newest epoch, or after a cancel, is stale and refused (409): the
    replacement attempt owns the operation. Returns the row."""
    operation = str(body.get("operation") or "")
    status = str(body.get("status") or "")
    if not operation or not pod.startswith(operation + "-"):
        raise HTTPException(400, f"pod {pod} does not belong to operation {operation!r}")
    if status not in VALID:
        raise HTTPException(400, f"status must be one of {VALID}")
    root = str(body.get("root") or operation)
    tenant = ns[len(TENANT_NS_PREFIX):]
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (root,))
        cur = c.execute("select epoch, status from attempts where operation = %s and pod = %s", (operation, pod)).fetchone()
        top = c.execute("select coalesce(max(epoch), 0) from attempts where root = %s", (root,)).fetchone()[0]
        cancelled = c.execute("select cancelled_at from operation_marks where operation = %s", (operation,)).fetchone()
        if cur:
            epoch = cur[0]
            if cur[1] in TERMINAL and status == "started":
                return _row(c, operation, pod)          # a late duplicate of the start record
        else:
            epoch = top + 1
        if status in TERMINAL and epoch < top:
            raise HTTPException(409, f"attempt {pod} (epoch {epoch}) was replaced by epoch {top}: stale finalization refused")
        if status in TERMINAL and cancelled and cancelled[0] and status == "succeeded":
            raise HTTPException(409, f"operation {operation} was cancelled: finalization refused")
        export = body.get("export") or ("n/a" if body.get("io_mode") == "application" and status != "succeeded" else None)
        c.execute("""insert into attempts (operation, pod, root, tenant, namespace, epoch, region, node, pool, gpu_class, gpus, image,
                        image_digest, status, exit_code, started_at, ended_at, export_state, result_ref, checkpoint_ref, updated_at)
                     values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                     on conflict (operation, pod) do update set
                        status = excluded.status, exit_code = excluded.exit_code, node = coalesce(excluded.node, attempts.node),
                        pool = coalesce(excluded.pool, attempts.pool), image = coalesce(excluded.image, attempts.image),
                        image_digest = coalesce(excluded.image_digest, attempts.image_digest),
                        started_at = coalesce(attempts.started_at, excluded.started_at), ended_at = excluded.ended_at,
                        export_state = coalesce(excluded.export_state, attempts.export_state),
                        result_ref = coalesce(excluded.result_ref, attempts.result_ref),
                        checkpoint_ref = coalesce(excluded.checkpoint_ref, attempts.checkpoint_ref),
                        revision = nextval('attempts_revision_seq'), updated_at = now()""",
                  (operation, pod, root, tenant, ns, epoch, region, body.get("node") or None, body.get("pool") or None,
                   body.get("gpu_class") or None, int(body.get("gpus") or 0), body.get("image") or None, body.get("image_digest") or None,
                   status, body.get("exit_code"), _ts(body.get("started_at")), _ts(body.get("ended_at")), export,
                   body.get("result_ref") or None, body.get("checkpoint_ref") or None))
        return _row(c, operation, pod)


COLS = ("operation, pod, root, tenant, namespace, epoch, region, node, pool, gpu_class, gpus, image, image_digest, status, exit_code, "
        "started_at, ended_at, export_state, result_ref, checkpoint_ref, revision, updated_at")


def _dict(r) -> dict:
    keys = [k.strip() for k in COLS.split(",")]
    d = dict(zip(keys, r))
    for k in ("started_at", "ended_at", "updated_at"):
        d[k] = d[k].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d.get(k) else None
    return d


def _row(c, operation: str, pod: str) -> dict:
    return _dict(c.execute(f"select {COLS} from attempts where operation = %s and pod = %s", (operation, pod)).fetchone())


def for_operation(root: str) -> list[dict]:
    """Every attempt of the root operation and its resumes, oldest first (the uploader's record shape plus the
    lifecycle fields), for status and billing."""
    if not enabled():
        return []
    with db.pool().connection() as c:
        rows = c.execute(f"select {COLS} from attempts where root = %s or operation = %s order by epoch, started_at", (root, root)).fetchall()
    return [_dict(r) for r in rows]


def mark_cancelled(operation: str) -> None:
    if not enabled():
        return
    with db.pool().connection() as c:
        c.execute("insert into operation_marks (operation, cancelled_at) values (%s, now()) on conflict (operation) do update set cancelled_at = now(), revision = nextval('operation_marks_revision_seq')", (operation,))


def _marks(operation: str):
    if not enabled():
        return None
    with db.pool().connection() as c:
        return c.execute("select revision, cancelled_at from operation_marks where operation = %s", (operation,)).fetchone()


def lifecycle(operation: str, root: str, rows: list[dict]) -> dict:
    """The lifecycle fields of an operation (docs/API.md): root, newest attempt and epoch, revision cursor, export
    and finalization state."""
    mine = [r for r in rows if r["operation"] == operation]
    latest = mine[-1] if mine else None
    revision = max([r.get("revision") or 0 for r in rows] + [0])
    m = _marks(operation)
    if m:
        revision = max(revision, m[0] or 0)
    return {"root_operation_id": root, "attempt_id": latest.get("pod") if latest else None, "epoch": latest.get("epoch") if latest else None,
            "revision": revision, "export_state": (latest.get("export_state") or latest.get("export")) if latest else None,
            "result_ref": latest.get("result_ref") if latest else None, "checkpoint_ref": latest.get("checkpoint_ref") if latest else None,
            "image_digest": latest.get("image_digest") if latest else None, "cluster": latest.get("region") if latest else None}
