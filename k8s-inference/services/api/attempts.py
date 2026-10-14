"""Durable attempt lifecycle of runs (docs/JOBS.md "Attempts and finalization"): the fleet database keeps one row
per attempt (a pod of an operation), written by the uploader of that pod through `POST /internal/attempts` with its
projected ServiceAccount token (verified with a TokenReview against the cluster that issued it: the token is bound
to the pod, so a record can only come from the pod it describes).

Why: pods and Kubernetes Jobs are transient (preemption deletes pods, MultiKueue deletes the worker's copy at
completion, Jobs expire), the tenant bucket does not exist for application-managed I/O, and an attempt that is
replaced must not finalize the operation afterwards. The rows give every operation a stable root, a unique attempt
id (the pod), a monotonic epoch, a revision cursor for polling, the selected cluster/pool/class/image digest, and the
export and finalization state that decides whether "exit 0" became a delivered result.

"""

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import db
import kube
from config import REGION, TENANT_NS_PREFIX
from resilience import retry

log = logging.getLogger("api.attempts")

TERMINAL = ("succeeded", "failed", "interrupted")
VALID = TERMINAL + ("started",)


def enabled() -> bool:
    return db.enabled()


def verify_runner_token(token: str) -> tuple[str, str, str]:
    """TokenReview of an uploader's projected token against the clusters this API reaches, the GPU regions first (a
    run's pod lives on one of them; the control cluster of a dedicated fleet issues no run tokens and answers
    "unauthenticated" at once): (region, namespace, pod). 401 when no cluster issued it or it is not a bound pod
    token of a tenant namespace. The record itself names no cluster: the uploader only knows its pod."""
    from kubernetes import client

    for region in sorted(kube.regions(), key=lambda r: r == REGION):
        try:
            review = retry(
                client.AuthenticationV1Api(kube._client(region)).create_token_review,
                client.V1TokenReview(spec=client.V1TokenReviewSpec(token=token)),
            )
        except (ApiException, OSError) as e:
            log.warning("token review in %s: %s", region, str(e)[:120])
            continue
        st = review.status
        if not st or not st.authenticated:
            continue
        user = st.user.username or ""
        parts = user.split(":")
        if (
            len(parts) != 4
            or parts[0] != "system"
            or parts[1] != "serviceaccount"
            or not parts[2].startswith(TENANT_NS_PREFIX)
        ):
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
    tenant = ns[len(TENANT_NS_PREFIX) :]
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (root,))
        cur = c.execute(
            "select epoch, status from attempts where operation = %s and pod = %s", (operation, pod)
        ).fetchone()
        top = c.execute("select coalesce(max(epoch), 0) from attempts where root = %s", (root,)).fetchone()[0]
        marks = c.execute(
            "select cancelled_at, successor from operation_marks where operation = %s", (operation,)
        ).fetchone()
        cancelled = (marks[0],) if marks else None
        epoch = cur[0] if cur else top + 1
        if epoch < top:
            # a replaced attempt: its own end (interruption or failure, with its measured time) may still close the
            # accounting of an attempt already on record; it promotes nothing (no pointer, generation or result) and
            # changes no operation status; a late start, progress or success of a replaced attempt is refused
            if not cur or status not in ("interrupted", "failed"):
                raise HTTPException(
                    409, f"attempt {pod} (epoch {epoch}) was replaced by epoch {top}: stale record refused"
                )
            body = {k: v for k, v in body.items() if k not in ("checkpoint_ref", "checkpoint_generation", "result_ref")}
        if marks and marks[1]:
            # fenced before its successor exists: a worker that comes back after the platform moved the run cannot
            # publish progress (a start or pointer record) or a final success; its own interruption or failure may
            # still close its attempt, but promotes nothing: no pointer, generation or result from a superseded pod
            # ever reaches the durable row (docs/SCHEDULING.md "Automatic recovery after infrastructure loss")
            if status not in ("interrupted", "failed"):
                raise HTTPException(409, f"operation {operation} was superseded by {marks[1]}: record refused")
            body = {k: v for k, v in body.items() if k not in ("checkpoint_ref", "checkpoint_generation", "result_ref")}
        if cur and cur[1] in TERMINAL and status == "started":
            return _row(c, operation, pod)  # a late duplicate of the current attempt's start record
        if status == "started" and not body.get("image_digest"):
            body = {**body, "image_digest": _pod_digest(region, ns, pod)}  # observed on the pod, or still None
        if status in TERMINAL and cancelled and cancelled[0] and status == "succeeded":
            raise HTTPException(409, f"operation {operation} was cancelled: finalization refused")
        export = body.get("export") or (
            "n/a" if body.get("io_mode") == "application" and status != "succeeded" else None
        )
        c.execute(
            """insert into attempts (operation, pod, root, tenant, namespace, epoch, region, node, pool, gpu_class, gpus, image,
                        image_digest, status, exit_code, started_at, ended_at, export_state, result_ref, checkpoint_ref, checkpoint_generation, updated_at, role, cluster,
                        mode, model, io_mode)
                     values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s)
                     on conflict (operation, pod) do update set
                        cluster = coalesce(excluded.cluster, attempts.cluster),
                        mode = coalesce(excluded.mode, attempts.mode), model = coalesce(excluded.model, attempts.model),
                        io_mode = coalesce(excluded.io_mode, attempts.io_mode),
                        status = excluded.status, exit_code = excluded.exit_code, node = coalesce(excluded.node, attempts.node),
                        pool = coalesce(excluded.pool, attempts.pool), image = coalesce(excluded.image, attempts.image),
                        image_digest = coalesce(excluded.image_digest, attempts.image_digest),
                        started_at = coalesce(attempts.started_at, excluded.started_at), ended_at = excluded.ended_at,
                        export_state = coalesce(excluded.export_state, attempts.export_state),
                        result_ref = coalesce(excluded.result_ref, attempts.result_ref),
                        checkpoint_ref = coalesce(excluded.checkpoint_ref, attempts.checkpoint_ref),
                        checkpoint_generation = coalesce(excluded.checkpoint_generation, attempts.checkpoint_generation),
                        revision = nextval('attempts_revision_seq'), updated_at = now()""",
            (
                operation,
                pod,
                root,
                tenant,
                ns,
                epoch,
                region,
                body.get("node") or None,
                body.get("pool") or None,
                body.get("gpu_class") or None,
                int(body.get("gpus") or 0),
                body.get("image") or None,
                body.get("image_digest") or None,
                status,
                body.get("exit_code"),
                _ts(body.get("started_at")),
                _ts(body.get("ended_at")),
                export,
                body.get("result_ref") or None,
                body.get("checkpoint_ref") or None,
                int(body["checkpoint_generation"]) if body.get("checkpoint_generation") is not None else None,
                str(body.get("role") or "run"),
                body.get("cluster") or None,
                body.get("mode") or None,
                body.get("model") or None,
                body.get("io_mode") or None,
            ),
        )
        return _row(c, operation, pod)


COLS = (
    "operation, pod, root, tenant, namespace, epoch, region, node, pool, gpu_class, gpus, image, image_digest, status, exit_code, "
    "started_at, ended_at, export_state, result_ref, checkpoint_ref, checkpoint_generation, revision, updated_at, role, cluster, "
    "mode, model, io_mode"
)


def _dict(r) -> dict:
    keys = [k.strip() for k in COLS.split(",")]
    d = dict(zip(keys, r))
    for k in ("started_at", "ended_at", "updated_at"):
        d[k] = d[k].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d.get(k) else None
    return d


def _row(c, operation: str, pod: str) -> dict:
    return _dict(
        c.execute(f"select {COLS} from attempts where operation = %s and pod = %s", (operation, pod)).fetchone()
    )


def for_operation(root: str) -> list[dict]:
    """Every attempt of the root operation and its resumes, oldest first (the uploader's record shape plus the
    lifecycle fields), for status and billing."""
    if not enabled():
        return []
    with db.pool().connection() as c:
        rows = c.execute(
            f"select {COLS} from attempts where root = %s or operation = %s order by epoch, started_at", (root, root)
        ).fetchall()
    return [_dict(r) for r in rows]


def for_operations(roots: set[str]) -> dict[str, list[dict]]:
    """`for_operation` for many roots in one query: {root: its rows, oldest first}."""
    if not enabled() or not roots:
        return {}
    with db.pool().connection() as c:
        rows = c.execute(
            f"select {COLS} from attempts where root = any(%s) or operation = any(%s) order by epoch, started_at",
            (list(roots), list(roots)),
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = _dict(r)
        out.setdefault(d["root"] if d["root"] in roots else d["operation"], []).append(d)
    return out


def latest_checkpoint(root: str) -> dict | None:
    """The newest acknowledged checkpoint pointer of the operation chain (the highest epoch, then generation):
    what a resume uses when the caller names none."""
    rows = [r for r in for_operation(root) if r.get("checkpoint_ref")]
    if not rows:
        return None
    best = max(rows, key=lambda r: (r.get("epoch") or 0, r.get("checkpoint_generation") or 0, r.get("revision") or 0))
    return {
        "checkpoint_ref": best["checkpoint_ref"],
        "generation": best.get("checkpoint_generation"),
        "epoch": best.get("epoch"),
        "attempt_id": best["pod"],
    }


def mark_successor(operation: str, successor: str) -> None:
    """Capacity fallback: the original handle names its replacement (docs/SCHEDULING.md)."""
    if not enabled():
        return
    with db.pool().connection() as c:
        c.execute(
            "insert into operation_marks (operation, successor) values (%s, %s) on conflict (operation) do update set successor = excluded.successor, revision = nextval('operation_marks_revision_seq')",
            (operation, successor),
        )


def successor_of(operation: str) -> str | None:
    m = _marks(operation)
    return m[2] if m and len(m) > 2 else None


def synthesize(operation: str, tenant: str) -> dict | None:
    """An operation whose Job is gone (TTL, cleanup) rebuilt from its rows: status, attempts and lifecycle survive
    the Kubernetes objects (docs/JOBS.md "Attempts and finalization")."""
    rows = [r for r in for_operation(operation) if r["operation"] == operation and r["tenant"] == tenant]
    if not rows:
        return None
    last = rows[-1]
    status = {"succeeded": "SUCCEEDED", "failed": "FAILED", "interrupted": "PREEMPTED", "started": "PREEMPTED"}.get(
        last["status"], "FAILED"
    )
    export = last.get("export_state")
    if status == "SUCCEEDED" and (
        export == "failed" or export is None or (export == "n/a" and not last.get("result_ref"))
    ):
        status = "EXPORT_FAILED"
    m = _marks(operation)
    if m and m[1]:
        status = "CANCELLED"
    attempts = [
        {
            "index": i + 1,
            "attempt_id": r["pod"],
            "status": {
                "succeeded": "SUCCEEDED",
                "failed": "FAILED",
                "interrupted": "PREEMPTED",
                "started": "PREEMPTED",
            }.get(r["status"], "FAILED"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "node": r.get("node"),
            "gpu_class": r.get("gpu_class"),
            "epoch": r.get("epoch"),
            "export_state": r.get("export_state"),
            "result_ref": r.get("result_ref"),
            "checkpoint_ref": r.get("checkpoint_ref"),
            "image_digest": r.get("image_digest"),
        }
        for i, r in enumerate(rows)
    ]
    return {
        "id": operation,
        "name": None,
        # provenance that outlives the Job (migration 12): the operation's mode, model and I/O mode
        "model": last.get("model"),
        "mode": last.get("mode") or "run",
        "status": status,
        "region": last.get("region"),
        "tenant": tenant,
        "phase": None,
        "created_at": rows[0].get("started_at"),
        "started_at": rows[0].get("started_at"),
        "ended_at": last.get("ended_at"),
        "attempts": attempts,
        "error": None,
        "input": {},
        "resumed_from": last["root"] if last["root"] != operation else None,
        "gpu_class": last.get("gpu_class"),
        "image": last.get("image"),
        "resumable": False,
        "nodes": 1,
        "execution": {"io_mode": last["io_mode"]} if last.get("io_mode") else None,
        "reason": "the Job object expired; this is the durable record",
        "cost": None,
        "gpu_seconds": None,
        **lifecycle(operation, last["root"], rows),
    }


def mark_cancelled(operation: str) -> None:
    if not enabled():
        return
    with db.pool().connection() as c:
        c.execute(
            "insert into operation_marks (operation, cancelled_at) values (%s, now()) on conflict (operation) do update set cancelled_at = now(), revision = nextval('operation_marks_revision_seq')",
            (operation,),
        )


def _marks(operation: str):
    if not enabled():
        return None
    with db.pool().connection() as c:
        return c.execute(
            "select revision, cancelled_at, successor from operation_marks where operation = %s", (operation,)
        ).fetchone()


def _pod_digest(region: str | None, namespace: str, pod: str) -> str | None:
    """The main container's imageID as the API reads it on the pod right now; None when not reported yet or unreadable
    (the record then stays `pending` and the live read at GET time converges it)."""
    try:
        obj = kube.core(region or kube.REGION).read_namespaced_pod(pod, namespace)
    except Exception:
        return None
    for cs in (obj.status.container_statuses if obj.status else None) or []:
        if cs.name == MAIN_CONTAINER and cs.image_id:
            return cs.image_id
    return None


def marks_for(operations: set[str]) -> dict[str, tuple]:
    """`_marks` for many operations in one query (the operation list)."""
    if not enabled() or not operations:
        return {}
    with db.pool().connection() as c:
        rows = c.execute(
            "select operation, revision, cancelled_at, successor from operation_marks where operation = any(%s)",
            (list(operations),),
        ).fetchall()
    return {r[0]: tuple(r[1:]) for r in rows}


MAIN_CONTAINER = "main"


def observed_digest(pods: list | None, pod_name: str | None) -> str | None:
    """The runtime image digest of the attempt's main container as the API observes it on the live pod
    (`containerStatuses[main].imageID`): what actually runs, never the expected image. None until the kubelet reports it."""
    for p in pods or []:
        if ((p.get("metadata") or {}).get("name")) != pod_name:
            continue
        for cs in (p.get("status") or {}).get("containerStatuses") or []:
            if cs.get("name") == MAIN_CONTAINER and cs.get("imageID"):
                return cs["imageID"]
    return None


def digest_state(latest: dict | None, digest: str | None) -> str | None:
    """`observed`: the exact runtime digest is on record or on the live pod; `pending`: the attempt runs and the kubelet
    has not reported it yet (transient, converges within seconds: a reader retries); `unknown`: the attempt ended
    without one (never guessed from the expected image)."""
    if not latest:
        return None
    if digest:
        return "observed"
    return "pending" if latest.get("status") == "started" else "unknown"


def lifecycle(
    operation: str, root: str, rows: list[dict], marks: tuple | None | bool = False, pods: list | None = None
) -> dict:
    """The lifecycle fields of an operation (docs/API.md): root, newest attempt and epoch, revision cursor, export
    and finalization state. `marks`: the operation's mark row when the caller fetched it already (`marks_for`).
    `pods`: the operation's live pods as the API read them: the image digest comes from the newest attempt's pod when
    its record does not carry it yet (the uploader's start record can precede the kubelet's imageID)."""
    mine = [r for r in rows if r["operation"] == operation]
    latest = mine[-1] if mine else None
    digest = (latest.get("image_digest") or observed_digest(pods, latest.get("pod"))) if latest else None
    revision = max([r.get("revision") or 0 for r in rows] + [0])
    m = _marks(operation) if marks is False else marks
    if m:
        revision = max(revision, m[0] or 0)
    return {
        "root_operation_id": root,
        "attempt_id": latest.get("pod") if latest else None,
        "epoch": latest.get("epoch") if latest else None,
        "revision": revision,
        "export_state": (latest.get("export_state") or latest.get("export")) if latest else None,
        "result_ref": latest.get("result_ref") if latest else None,
        "checkpoint_ref": latest.get("checkpoint_ref") if latest else None,
        "image_digest": digest,
        "image_digest_state": digest_state(latest, digest),
        # the canonical cluster id (configured name); the Nebius region is the operation's `region`
        "cluster": (latest.get("cluster") or kube.cluster_id_or_none(latest.get("region"))) if latest else None,
        "checkpoint_generation": latest.get("checkpoint_generation") if latest else None,
        "attempt_role": (latest.get("role") or "run") if latest else None,
    }
