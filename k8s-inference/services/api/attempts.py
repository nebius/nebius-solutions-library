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
import re
from datetime import timezone

from fastapi import HTTPException
from kubernetes.client.rest import ApiException
from psycopg.types.json import Json

import db
import kube
import status
from config import JOBSET_NAME_LABEL, REGION, TENANT_NS_PREFIX
from resilience import iso, parse_ts, retry

log = logging.getLogger("api.attempts")

TERMINAL = ("succeeded", "failed", "interrupted")
VALID = TERMINAL + ("started",)


def enabled() -> bool:
    return db.enabled()


def token_review(token: str, region: str, audience: str | None = None):
    """The TokenReview status of a bound ServiceAccount token on `region`'s cluster (raises ApiException/OSError)."""
    from kubernetes import client

    spec = client.V1TokenReviewSpec(token=token, **({"audiences": [audience]} if audience else {}))
    return retry(client.AuthenticationV1Api(kube._client(region)).create_token_review, client.V1TokenReview(spec=spec))


def pod_claims(st) -> tuple[list[str], str, str]:
    """(the username's `system:serviceaccount:<ns>:<sa>` parts, pod name, pod UID) of an authenticated TokenReview."""
    extra = st.user.extra or {}
    pod = (extra.get("authentication.kubernetes.io/pod-name") or [""])[0]
    uid = (extra.get("authentication.kubernetes.io/pod-uid") or [""])[0]
    return (st.user.username or "").split(":"), pod, uid


def verify_pod_token(token: str) -> dict:
    """TokenReview of a run pod's projected token against the clusters this API reaches, the GPU regions first (a
    run's pod lives on one of them; the control cluster of a dedicated fleet issues no run tokens and answers
    "unauthenticated" at once): {region, namespace, pod, uid}. 401 when no cluster issued it or it is not a bound pod
    token of a tenant namespace. The record itself names no cluster: the uploader only knows its pod."""
    for region in sorted(kube.regions(), key=lambda r: r == REGION):
        try:
            review = token_review(token, region)
        except (ApiException, OSError) as e:
            log.warning("token review in %s: %s", region, str(e)[:120])
            continue
        st = review.status
        if not st or not st.authenticated:
            continue
        parts, pod, uid = pod_claims(st)
        if len(parts) != 4 or parts[:2] != ["system", "serviceaccount"] or not parts[2].startswith(TENANT_NS_PREFIX):
            raise HTTPException(401, "not a tenant run identity")
        if not pod:
            raise HTTPException(401, "token is not bound to a pod")
        return {"region": region, "namespace": parts[2], "pod": pod, "uid": uid}
    raise HTTPException(401, "token not recognised by any cluster of this fleet")


def verify_runner_token(token: str) -> tuple[str, str, str]:
    """`verify_pod_token` of an uploader's token as (region, namespace, pod)."""
    d = verify_pod_token(token)
    return d["region"], d["namespace"], d["pod"]


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
    # the pod as the API OBSERVES it (one read): its UID (every pod: a single-node run, an endpoint caller, a CPU
    # finalizer, a gang rank), its runtime digest, and for a JobSet pod its rank and incarnation (the JobSet
    # controller's restart-attempt, the indexed Job's completion index); nothing of it comes from the record
    facts = _pod_facts(region, ns, pod, operation)
    gang = facts["gang"]
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (root,))
        cur = c.execute(
            "select epoch, status from attempts where operation = %s and pod = %s", (operation, pod)
        ).fetchone()
        top = c.execute("select coalesce(max(epoch), 0) from attempts where root = %s", (root,)).fetchone()[0]
        marks = c.execute(
            "select cancelled_at, successor from operation_marks where operation = %s", (operation,)
        ).fetchone()
        cancelled_at, successor = marks or (None, None)
        epoch = _epoch_of(c, operation, pod, cur, gang, top)
        ends = status in ("interrupted", "failed")
        for stale, why in (
            # a replaced attempt (epoch < top): its own end (interruption or failure, with its measured time) may still
            # close the accounting of an attempt already on record; a late start, progress or success of it is refused
            (
                epoch < top and (not cur or not ends),
                f"attempt {pod} (epoch {epoch}) was replaced by epoch {top}: stale record refused",
            ),
            # fenced before its successor exists: a worker that comes back after the platform moved the run cannot
            # publish progress (a start or pointer record) or a final success; its own interruption or failure may
            # still close its attempt (docs/SCHEDULING.md "Automatic recovery after infrastructure loss")
            (successor and not ends, f"operation {operation} was superseded by {successor}: record refused"),
            (cancelled_at and status == "succeeded", f"operation {operation} was cancelled: finalization refused"),
        ):
            if stale:
                raise HTTPException(409, why)
        if (gang and gang.get("rank") not in (None, 0)) or epoch < top or successor:
            # a peer (rank 0 alone publishes the gang's pointer and result), a replaced attempt or a superseded pod
            # promotes nothing: no pointer, generation or result reaches the durable row; timings, exit and GPUs do
            body = {k: v for k, v in body.items() if k not in ("checkpoint_ref", "checkpoint_generation", "result_ref")}
        if cur and cur[1] in TERMINAL and status == "started":
            return _row(c, operation, pod)  # a late duplicate of the current attempt's start record
        if status == "started" and not body.get("image_digest"):
            body = {**body, "image_digest": facts["digest"]}  # observed on the pod, or still None
        resume = (
            _resume_for(c, operation, root, epoch, gang, body.get("_explicit_checkpoint_ref"))
            if gang and not cur
            else (None, None, None)
        )
        row = {
            "operation": operation,
            "pod": pod,
            "root": root,
            "tenant": tenant,
            "namespace": ns,
            "epoch": epoch,
            "region": region,
            "status": status,
            **{k: body.get(k) or None for k in _NULLABLE},
            "gpus": int(body.get("gpus") or 0),
            "exit_code": body.get("exit_code"),
            "started_at": parse_ts(body.get("started_at")),
            "ended_at": parse_ts(body.get("ended_at")),
            "export_state": body.get("export")
            or ("n/a" if body.get("io_mode") == "application" and status != "succeeded" else None),
            "checkpoint_generation": (
                int(body["checkpoint_generation"]) if body.get("checkpoint_generation") is not None else None
            ),
            "role": str(body.get("role") or "run"),
            "gang_rank": gang.get("rank") if gang else None,
            "gang_restart": gang.get("restart") if gang else None,
            "pod_uid": facts["uid"],  # observed on every pod; an unreadable pod keeps the UID its row already has
            **dict(zip(RESTORE_KEYS, resume, strict=True)),
        }
        capture = _capture_of(body.get("capture"), operation, root, c)
        row["capture"] = Json(capture) if capture is not None else None
        c.execute(
            f"""insert into attempts ({", ".join(row)}, updated_at) values ({", ".join("%s" for _ in row)}, now())
                     on conflict (operation, pod) do update set
                        cluster = coalesce(excluded.cluster, attempts.cluster),
                        capture = coalesce(excluded.capture, attempts.capture),
                        resume_checkpoint_ref = coalesce(attempts.resume_checkpoint_ref, excluded.resume_checkpoint_ref),
                        resume_source_epoch = coalesce(attempts.resume_source_epoch, excluded.resume_source_epoch),
                        resume_source = coalesce(attempts.resume_source, excluded.resume_source),
                        gang_rank = coalesce(excluded.gang_rank, attempts.gang_rank),
                        gang_restart = coalesce(excluded.gang_restart, attempts.gang_restart),
                        pod_uid = coalesce(excluded.pod_uid, attempts.pod_uid),
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
            tuple(row.values()),
        )
        return _row(c, operation, pod)


_NULLABLE = "node pool gpu_class image image_digest result_ref checkpoint_ref cluster mode model io_mode".split()


def _epoch_of(c, operation: str, pod: str, cur, gang: dict | None, top: int) -> int:
    """The epoch the record belongs to: a known pod keeps the epoch it was recorded with, whatever can be read now;
    an ordinary new pod opens the next. One epoch per gang incarnation: the siblings of one JobSet restart share it
    (they never fence each other); a new incarnation (every pod recreated) is the next epoch, and the prior one is
    stale as any replaced attempt. The incarnation is the restart-attempt OBSERVED on the pod; a gang pod the API
    cannot read and never recorded has no proof of its incarnation: it is refused (409), never attributed to any gang
    (an old coordinator coming back late must not acquire the current epoch's publication authority)."""
    if cur:
        return cur[0]
    if not gang:
        return top + 1
    if gang.get("restart") is None:
        raise HTTPException(
            409,
            f"attempt {pod}: gang membership unverifiable (the pod cannot be read and has no record); "
            "an unverified incarnation is never attributed to the gang",
        )
    r = c.execute(
        "select epoch from attempts where operation = %s and gang_restart = %s order by epoch limit 1",
        (operation, gang["restart"]),
    ).fetchone()
    return int(r[0]) if r else top + 1


def _resume_for(c, operation: str, root: str, epoch: int, gang: dict, explicit: str | None) -> tuple:
    """The resume pointer of a gang incarnation, FROZEN per incarnation (docs/JOBS.md "Gang attempts"): when a sibling of
    the same incarnation (observed restart-attempt) recorded already, its stored value is copied, whatever was
    committed since; otherwise, chosen once: the newest COMMITTED checkpoint pointer of a COORDINATOR (rank 0, or an
    ordinary attempt of the chain) of an EARLIER epoch of the same root — never the current epoch, never a peer's —
    (`committed`, with its source epoch), else the explicit continuation reference the run was rendered with
    (`explicit`, no epoch), else nothing. Returns (ref, source_epoch, source)."""
    if gang.get("restart") is not None:
        r = c.execute(
            "select resume_checkpoint_ref, resume_source_epoch, resume_source from attempts where operation = %s and gang_restart = %s order by revision limit 1",
            (operation, gang["restart"]),
        ).fetchone()
        if r is not None:
            return (r[0], r[1], r[2])  # frozen: the incarnation's first row decided
    r = c.execute(
        "select checkpoint_ref, epoch from attempts where root = %s and checkpoint_ref is not null and epoch < %s "
        "and (gang_rank = 0 or gang_rank is null) order by epoch desc, checkpoint_generation desc nulls last, revision desc limit 1",
        (root, epoch),
    ).fetchone()
    if r is not None:
        return (r[0], int(r[1]), "committed")
    if explicit:
        return (str(explicit), None, "explicit")
    return (None, None, None)


CAPTURE_SHA = re.compile(r"^[0-9a-f]{64}$")


def _capture_of(raw, operation: str, root: str, c) -> dict | None:
    """The capture manifest the platform's uploader forwards from the caller's RESULT.json (docs/JOBS.md "Endpoint
    children"): only these bounded fields, checked, and immutable per operation lineage: a transfer-only retry re-posts
    the ORIGINAL producing attempt's manifest (same sha256, same producer: idempotent); a manifest that contradicts one
    already on record for the lineage (`<root>` or its `-x<n>` retries) is refused (409), never an overwrite."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(
            400, "capture: an object {sha256, content_type, size_bytes, http_status, capture_attempt_id, ...}"
        )
    sha = str(raw.get("sha256") or "")
    if not CAPTURE_SHA.match(sha):
        raise HTTPException(400, "capture.sha256: 64 hex digits")
    ct = str(raw.get("content_type") or "application/octet-stream")[:128]
    size, st = raw.get("size_bytes"), raw.get("http_status")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise HTTPException(400, "capture.size_bytes: a non-negative integer")
    if not isinstance(st, int) or isinstance(st, bool) or not 100 <= st <= 599:
        raise HTTPException(400, "capture.http_status: an HTTP status")
    producer = str(raw.get("capture_attempt_id") or "")[:253]
    if not producer:
        raise HTTPException(400, "capture.capture_attempt_id: the attempt that captured the response")
    cap = {"sha256": sha, "content_type": ct, "size_bytes": size, "http_status": st, "capture_attempt_id": producer}
    for k in ("posted_at", "completed_at"):
        if raw.get(k):
            cap[k] = str(raw[k])[:40]
    lineage = c.execute(
        "select capture from attempts where capture is not null and (operation = %s or operation = %s or operation like %s) limit 1",
        (operation, root, root + "-x%"),
    ).fetchone()
    if lineage and lineage[0]:
        have = lineage[0]
        if have.get("sha256") != sha or have.get("capture_attempt_id") != producer:
            raise HTTPException(
                409,
                f"capture contradicts the record of {root}: on record {have.get('sha256')} by {have.get('capture_attempt_id')}, "
                f"posted {sha} by {producer}; a capture is immutable per operation",
            )
    return cap


GANG_POD = re.compile(r"^(?P<op>.+)-workers-\d+-(?P<rank>\d+)-[a-z0-9]{5}$")


def _pod_facts(region: str | None, namespace: str, pod: str, operation: str) -> dict:
    """What the API observes on the recording pod, in ONE read: {"uid", "digest", "gang", "read"}. `uid` is the pod's
    UID (every pod of every operation: a single-node run, an endpoint caller, a CPU finalizer, a gang rank), `digest`
    the main container's runtime imageID when the kubelet reported it, `gang` the JobSet membership ({"uid", "rank",
    "restart"}) or None for an ordinary pod. The TokenReview already proved the pod name; what is read here is the
    cluster's own statement about that pod, never the record's. Unreadable (a late record after the pod is gone):
    uid/digest None (the row keeps what it has), `gang` with the rank from a JobSet pod name and the incarnation
    unknown (`record` then keeps only the pod's existing row and refuses a pod without one), or None for an ordinary
    pod name (ordinary semantics unchanged)."""
    out: dict = {"uid": None, "digest": None, "gang": None, "read": False}
    try:
        obj = kube.core(region or kube.REGION).read_namespaced_pod(pod, namespace)
    except Exception:  # noqa: BLE001 - unreadable: the name is all that is left
        m = GANG_POD.match(pod)
        if m and m.group("op") == operation:
            out["gang"] = {"uid": None, "rank": int(m.group("rank")), "restart": None}
        return out
    p = kube.core(region or kube.REGION).api_client.sanitize_for_serialization(obj)
    md = p.get("metadata") or {}
    out["read"], out["uid"], out["digest"] = True, md.get("uid"), status._pod_digest(p)
    labels, ann = md.get("labels") or {}, md.get("annotations") or {}
    if (labels.get(JOBSET_NAME_LABEL) or ann.get(JOBSET_NAME_LABEL)) == operation:
        g = status._pod_gang_fields(p)
        out["gang"] = {"uid": md.get("uid"), "rank": g["gang_rank"], "restart": g["gang_restart"]}
    return out


def authority(rows: list[dict]) -> dict | None:
    """The record that speaks for an operation's newest attempt: the newest epoch's row; for a gang (rows with a rank)
    the coordinator's (rank 0) when it recorded, so the lifecycle names the rank that publishes."""
    if not rows:
        return None
    top = max(int(r.get("epoch") or 0) for r in rows)
    newest = [r for r in rows if int(r.get("epoch") or 0) == top]
    if any(r.get("gang_rank") is not None for r in newest):
        return next((r for r in newest if r.get("gang_rank") == 0), newest[-1])
    return newest[-1]


def gang_status(rows: list[dict]) -> str | None:
    """A gang's outcome from its newest incarnation's records (every rank counts, as the JobSet's `All` success
    policy): succeeded only when every rank succeeded; failed when any rank failed; interrupted when any rank was
    interrupted and none failed; else started. None for an ordinary operation (no gang rows)."""
    if not rows:
        return None
    top = max(int(r.get("epoch") or 0) for r in rows)
    newest = [r for r in rows if int(r.get("epoch") or 0) == top and r.get("gang_rank") is not None]
    if not newest:
        return None
    states = {r["status"] for r in newest}
    if "failed" in states:
        return "failed"
    if "interrupted" in states:
        return "interrupted"
    if states == {"succeeded"}:
        return "succeeded"
    return "started"


COLS = (
    "operation, pod, root, tenant, namespace, epoch, region, node, pool, gpu_class, gpus, image, image_digest, status, exit_code, "
    "started_at, ended_at, export_state, result_ref, checkpoint_ref, checkpoint_generation, revision, updated_at, role, cluster, "
    "mode, model, io_mode, gang_rank, gang_restart, pod_uid, capture, resume_checkpoint_ref, resume_source_epoch, resume_source"
)


def _dict(r) -> dict:
    keys = [k.strip() for k in COLS.split(",")]
    d = dict(zip(keys, r))
    for k in ("started_at", "ended_at", "updated_at"):
        d[k] = iso(d[k].astimezone(timezone.utc)) if d.get(k) else None
    return d


def _row(c, operation: str, pod: str) -> dict:
    return _dict(
        c.execute(f"select {COLS} from attempts where operation = %s and pod = %s", (operation, pod)).fetchone()
    )


def for_operation(root: str) -> list[dict]:
    """Every attempt of the root operation and its resumes, oldest first (the uploader's record shape plus the
    lifecycle fields), for status and billing."""
    return for_operations({root}).get(root, [])


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


def durable_index(tenant: str, owner: str | None, model: str | None) -> list[dict]:
    """Every operation the attempt records know for the tenant (the roots and their continuations; a Job that expired
    leaves these rows), one entry each: operation, root, model, mode, region, created_at (its first attempt's start, as
    the API renders it). `owner` narrows to the operations whose root the owner records name (migration 9
    `operation_owners`: the owner's roots and their continuations), None for an admin's whole tenant; `model` narrows
    by the provenance the records carry (migration 12). Nothing here is a new ledger: it reads the records that
    GET /v1/operations/{id} already reports an expired operation from."""
    if not enabled():
        return []
    where, params = ["a.tenant = %s"], [tenant]
    if owner is not None:
        where.append(
            "(a.root in (select operation from operation_owners where tenant = %s and owner = %s) "
            "or a.operation in (select operation from operation_owners where tenant = %s and owner = %s))"
        )
        params += [tenant, owner, tenant, owner]
    with db.pool().connection() as c:
        rows = c.execute(
            "select a.operation, a.root, max(a.model), max(a.mode), max(a.region), "
            "min(coalesce(a.started_at, a.updated_at)) from attempts a where "
            + " and ".join(where)
            + " group by a.operation, a.root"
            + (" having max(a.model) = %s" if model else ""),
            params + ([model] if model else []),
        ).fetchall()
    return [
        {
            "operation": r[0],
            "root": r[1],
            "model": r[2],
            "mode": r[3] or "run",
            "region": r[4],
            "created_at": iso(r[5].astimezone(timezone.utc)) if r[5] else "",
        }
        for r in rows
    ]


_SYNTHETIC_ATTEMPT = (
    "started_at ended_at node gpu_class epoch export_state result_ref checkpoint_ref image_digest pod_uid gang_rank "
    "gang_restart resume_checkpoint_ref resume_source_epoch resume_source"
).split()


def synthesize(operation: str, tenant: str) -> dict | None:
    """An operation whose Job is gone (TTL, cleanup) rebuilt from its rows: status, attempts and lifecycle survive
    the Kubernetes objects (docs/JOBS.md "Attempts and finalization")."""
    rows = [r for r in for_operation(operation) if r["operation"] == operation and r["tenant"] == tenant]
    if not rows:
        return None
    last = authority(rows)  # a gang: the coordinator of the newest incarnation; its outcome counts every rank
    state = status.RECORD_STATUS.get(gang_status(rows) or last["status"], "FAILED")
    export = last.get("export_state")
    if state == "SUCCEEDED" and (
        export == "failed" or export is None or (export == "n/a" and not last.get("result_ref"))
    ):
        state = "EXPORT_FAILED"
    m = _marks(operation)
    if m and m[1]:
        state = "CANCELLED"
    attempts = [
        {
            "index": i + 1,
            "attempt_id": r["pod"],
            "status": status.RECORD_STATUS.get(r["status"], "FAILED"),
            **{k: r.get(k) for k in _SYNTHETIC_ATTEMPT},
        }
        for i, r in enumerate(rows)
    ]
    status.cancel_attempt(
        attempts, state == "CANCELLED"
    )  # the cancelled attempt's record is the cancel, not a preemption
    return {
        "id": operation,
        "name": None,
        # provenance that outlives the Job (migration 12): the operation's mode, model and I/O mode
        "model": last.get("model"),
        "mode": last.get("mode") or "run",
        "status": state,
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


def delivered(op: dict, name: str, root: str) -> dict:
    """What a transfer-only retry or an application finalizer (`<name>-x<n>`) that delivered means for `name`
    (docs/JOBS.md "Attempts and finalization", "Finalization by the application"): a run or an endpoint call whose
    inference succeeded is SUCCEEDED with the delivering attempt's result; a delivered capture of an endpoint error
    (the retry's exit code 2 or 5, the captured status class) stays FAILED and reports the delivery next to it, so
    delivery never turns a failed inference into a success."""
    done = [
        r
        for r in for_operation(root)
        if r["operation"].startswith(f"{name}-x")
        and (
            (r.get("export_state") or r.get("export")) == "succeeded"
            or (r.get("result_ref") and r["status"] == "succeeded")
        )
    ]
    if not done:
        return {}
    last = done[-1]
    out = {
        "export_state": last.get("export_state") or "succeeded",
        "exported_by": last["operation"],
        # the delivering attempt: what a consumer of the result (a dependent stage, DEPENDENCY_RESULTS) must name
        "exported_by_attempt_id": last.get("pod"),
        "exported_by_epoch": last.get("epoch"),
        "result_ref": last.get("result_ref") or op.get("result_ref"),
    }
    if last.get("exit_code") in (2, 5) or (last["status"] != "succeeded" and last.get("exit_code")):
        out.update(
            {
                "status": "FAILED",
                "error": "the endpoint answered an error; its response was delivered (see result_ref), inference did not succeed",
            }
        )
    else:
        out.update({"status": "SUCCEEDED", "error": None})
    return out


def mark_cancelled(operation: str) -> None:
    if not enabled():
        return
    with db.pool().connection() as c:
        c.execute(
            "insert into operation_marks (operation, cancelled_at) values (%s, now()) on conflict (operation) do update set cancelled_at = now(), revision = nextval('operation_marks_revision_seq')",
            (operation,),
        )


def _marks(operation: str) -> tuple | None:
    """The operation's mark row (revision, cancelled_at, successor), or None."""
    return marks_for({operation}).get(operation)


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


def observed_digest(pods: list | None, pod_name: str | None) -> str | None:
    """The runtime image digest of the attempt's main container as the API observes it on the live pod
    (`containerStatuses[main].imageID`): what actually runs, never the expected image. None until the kubelet reports it."""
    return next(
        (d for p in pods or [] if (p.get("metadata") or {}).get("name") == pod_name and (d := status._pod_digest(p))),
        None,
    )


def digest_state(latest: dict | None, digest: str | None) -> str | None:
    """`observed`: the exact runtime digest is on record or on the live pod; `pending`: the attempt runs and the kubelet
    has not reported it yet (transient, converges within seconds: a reader retries); `unknown`: the attempt ended
    without one (never guessed from the expected image)."""
    if not latest:
        return None
    if digest:
        return "observed"
    return "pending" if latest.get("status") == "started" else "unknown"


RESTORE_KEYS = ("resume_checkpoint_ref", "resume_source_epoch", "resume_source")


def restore_fields(r: dict | None) -> dict:
    """The restore triple of an attempt record as the owner sees it (docs/JOBS.md "Restoring a gang"): present exactly
    on a gang's authenticated records (an observed `gang_rank`: the start ACK of such a record is where the pointer
    is frozen, `_resume_for`, and a decided "nothing to restore" is an explicit null triple there); absent on every
    other row (a single-pod attempt, a record without an observed rank, a live pod without its record), never a
    manufactured null."""
    if not r or r.get("gang_rank") is None:
        return {}
    return {k: r.get(k) for k in RESTORE_KEYS}


def lifecycle(
    operation: str, root: str, rows: list[dict], marks: tuple | None | bool = False, pods: list | None = None
) -> dict:
    """The lifecycle fields of an operation (docs/API.md): root, newest attempt and epoch, revision cursor, export
    and finalization state. `marks`: the operation's mark row when the caller fetched it already (`marks_for`).
    `pods`: the operation's live pods as the API read them: the image digest comes from the newest attempt's pod when
    its record does not carry it yet (the uploader's start record can precede the kubelet's imageID)."""
    mine = [r for r in rows if r["operation"] == operation]
    latest = authority(mine)  # the newest epoch's record; a gang's coordinator (rank 0)
    at = latest or {}
    digest = (at.get("image_digest") or observed_digest(pods, at.get("pod"))) if latest else None
    revision = max([r.get("revision") or 0 for r in rows] + [0])
    m = _marks(operation) if marks is False else marks
    if m:
        revision = max(revision, m[0] or 0)
    return {
        "root_operation_id": root,
        "attempt_id": at.get("pod"),
        "epoch": at.get("epoch"),
        "revision": revision,
        "export_state": at.get("export_state") or at.get("export"),
        "result_ref": at.get("result_ref"),
        "checkpoint_ref": at.get("checkpoint_ref"),
        "image_digest": digest,
        "image_digest_state": digest_state(latest, digest),
        # the canonical cluster id (configured name); the Nebius region is the operation's `region`
        "cluster": (at.get("cluster") or kube.cluster_id_or_none(at.get("region"))) if latest else None,
        "checkpoint_generation": at.get("checkpoint_generation"),
        "attempt_role": (at.get("role") or "run") if latest else None,
        # observed on the pod by the API when it recorded (docs/JOBS.md "Gang attempts"): never the record's claim
        "pod_uid": at.get("pod_uid"),
        "gang_rank": at.get("gang_rank"),
        "gang_restart": at.get("gang_restart"),
        # the incarnation-frozen resume pointer the gang was told to restore (the start ACK's), never "the latest":
        # the three keys are present only when the ACK decided them (`resume_source` on record: committed, explicit or
        # fresh); an undecided row (unACKed, legacy) carries no triple, never a manufactured null
        **restore_fields(latest),
    }
