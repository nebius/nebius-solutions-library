"""Endpoint children of a run (docs/JOBS.md "Endpoint children"): an operator-allowlisted, bounded primitive by which a
LIVE attempt of a run asks the platform to admit an asynchronous endpoint call as a child of its root, without any
customer credential in the worker and without an application scheduler.

The parent authenticates as its own pod (a projected ServiceAccount token the render gives main when the run class
declares `child_endpoints`; TokenReview on the parent's cluster: namespace, pod name and exact pod UID), and must be a
live attempt of the named run (its pod among the run's pods, by UID; the run not finished, not cancelled; the root's
reservation open). The child inherits the root's owner, namespace and key BY HASH: its caller pod (a platform pod, as
every endpoint call's) carries only the operation-scoped admission credential (`Bearer s2-admitted.<token>`, the HMAC
of the root's key hash, the child model and the child operation), which the gateway hook accepts only while the
child holds a live reservation; no customer or admin key is stored or forwarded anywhere. It inherits the allowlisted
endpoint model and the parent's remaining deadline. Its identity is deterministic per
(root, unit): `<root>-c-<sha256(unit)[:10]>`; its request is bound by an immutable sha256; a replay with the same unit
and request reads the existing child back (any live attempt of the root, a successor included); a different request
for the same unit is refused. One child is active per root at a time; the number of children per root is bounded by
the class. Accounting on the root's ledger (keys.prepare_run_member kind "child"): one request each, the endpoint's
GPUs x the child's timeout reserved on the root's row, settled by the child's own billing as a run member; the root
closes only when nothing is active. Cancellation of the root cascades to active children (the reconciler's sweep, every
15 s, and the cancel route); a child that ended is recorded ended. Nothing here forwards a customer bearer to a worker
and nothing creates a second scheduler or ledger."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import catalog
import db
import jobs
import keys
import kube
import status
from config import LABEL, REGION, TENANT_NS_PREFIX
from resilience import retry

log = logging.getLogger("api")

MIN_REMAINING_S = 60  # a child needs at least this much of the parent's deadline left
UNIT_RE = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"


def enabled() -> bool:
    return db.enabled()


def child_id(root: str, unit_id: str) -> str:
    return f"{root}-c-{hashlib.sha256(unit_id.encode()).hexdigest()[:10]}"


def request_hash(inp: dict) -> str:
    return hashlib.sha256(json.dumps(inp, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_parent_token(token: str) -> dict:
    """TokenReview of the parent attempt's projected token against the clusters this API reaches: {region, namespace,
    pod, uid}; 401 unless a bound pod token of a tenant namespace."""
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
        parts = (st.user.username or "").split(":")
        if (
            len(parts) != 4
            or parts[0] != "system"
            or parts[1] != "serviceaccount"
            or not parts[2].startswith(TENANT_NS_PREFIX)
        ):
            raise HTTPException(401, "not a tenant run identity")
        extra = st.user.extra or {}
        pod = (extra.get("authentication.kubernetes.io/pod-name") or [""])[0]
        uid = (extra.get("authentication.kubernetes.io/pod-uid") or [""])[0]
        if not pod or not uid:
            raise HTTPException(401, "token is not bound to a pod")
        return {"region": region, "namespace": parts[2], "pod": pod, "uid": uid}
    raise HTTPException(401, "token not recognised by any cluster of this fleet")


def _live_parent(identity: dict, parent: str) -> tuple[dict, str, str, dict]:
    """The parent run `parent` as a live attempt of the authenticated pod: (job, region, root, class model). 409 for a
    stale/cancelled/finished attempt or a pod that is not the run's, 404 for an unknown run."""
    ns = identity["namespace"]
    job, region = jobs.find(ns, parent)
    md = job["metadata"]
    ann = md.get("annotations") or {}
    if (md.get("labels") or {}).get(f"{LABEL}/mode") != "run":
        raise HTTPException(409, f"operation {parent} is not a run")
    if status.finished(job) or ann.get(f"{LABEL}/cancelled") or ann.get(f"{LABEL}/successor"):
        raise HTTPException(409, f"operation {parent} is over or superseded; no child attaches to it")
    import attempts

    marks = attempts._marks(parent) if attempts.enabled() else None
    if marks and marks[1]:
        raise HTTPException(409, f"operation {parent} is cancelled; no child attaches to it")
    pods = job.get("_pods") or []
    mine = next((p for p in pods if p["metadata"].get("name") == identity["pod"]), None)
    if mine is None or mine["metadata"].get("uid") != identity["uid"]:
        raise HTTPException(
            409, f"pod {identity['pod']} is not a live attempt of {parent} (stale attempt or another run)"
        )
    if (mine.get("status") or {}).get("phase") not in ("Running", "Pending"):
        raise HTTPException(409, f"attempt {identity['pod']} of {parent} is not running")
    root = ann.get(f"{LABEL}/resumed-from") or parent
    m = catalog.require((md.get("labels") or {}).get(f"{LABEL}/model", ""))
    return job, region, root, m


def _remaining_s(job: dict) -> int | None:
    """Seconds left of the parent's enforced deadline (activeDeadlineSeconds from its start), None without one."""
    spec = job.get("spec") or {}
    dl = spec.get("activeDeadlineSeconds")
    if not dl or int(dl) <= 1:
        return None
    start = (job.get("status") or {}).get("startTime") or job["metadata"].get("creationTimestamp")
    if not start:
        return int(dl)
    t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    return int(int(dl) - (datetime.now(timezone.utc) - t0).total_seconds())


def readback(ns: str, root: str, unit_id: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(
            "select child, parent, parent_pod, model, request_sha256, state, created_at, ended_at from endpoint_children where root = %s and unit_id = %s",
            (root, unit_id),
        ).fetchone()
    if r is None:
        return None
    out = {
        "root": root,
        "unit_id": unit_id,
        "child": r[0],
        "parent": r[1],
        "parent_pod": r[2],
        "model": r[3],
        "request_sha256": r[4],
        "state": r[5],
        "created_at": r[6].strftime("%Y-%m-%dT%H:%M:%SZ") if r[6] else None,
        "ended_at": r[7].strftime("%Y-%m-%dT%H:%M:%SZ") if r[7] else None,
    }
    try:
        job, region = jobs.find(ns, r[0])
        op = jobs.normalise(job)
        out.update(
            {
                "status": op["status"],
                "output_prefix": jobs.output_prefix(job),
                "region": region,
                "error": op.get("error"),
                "ended": op.get("ended_at"),
            }
        )
    except HTTPException as e:
        out.update({"status": "UNKNOWN" if e.status_code == 404 else "UNAVAILABLE", "output_prefix": None})
    return out


def create(identity: dict, parent: str, body: dict) -> tuple[dict, bool]:
    """Admit one child (see the module docstring). Returns (readback, created)."""
    unit_id = str(body.get("unit_id") or "")
    import re

    if not re.fullmatch(UNIT_RE, unit_id):
        raise HTTPException(400, "unit_id: 1..128 of [A-Za-z0-9._:-], starting alphanumeric")
    model = str(body.get("model") or "")
    inp = body.get("input")
    if not isinstance(inp, dict):
        raise HTTPException(400, "input must be an object")
    job, region, root, m = _live_parent(identity, parent)
    ns = identity["namespace"]
    allowed = list((m.get("job") or {}).get("childEndpoints") or [])
    if not allowed:
        raise HTTPException(403, f"run class {m['name']} declares no child endpoints")
    if model not in allowed:
        raise HTTPException(403, f"model {model} is not a child endpoint of {m['name']} ({allowed})")
    cm = catalog.get(model)
    if cm is None or cm.get("mode") == "run":
        raise HTTPException(400, f"child model {model} is not an endpoint")
    max_units = int((m.get("job") or {}).get("childMaxUnits") or 0)
    h = request_hash(inp)
    child = child_id(root, unit_id)
    ann = job["metadata"].get("annotations") or {}
    key_hash = ann.get(f"{LABEL}/key")
    tenant = (job["metadata"].get("labels") or {}).get(f"{LABEL}/tenant")
    if not key_hash or not tenant:
        raise HTTPException(409, f"run {parent} carries no key on record; no child attaches to it")
    ki = keys.info(key_hash)
    if ki is None:
        raise HTTPException(409, f"run {parent}: its key no longer exists; no child attaches to it")
    import app as api

    # a child is an endpoint call of the root's key: the same policy as any endpoint call (a key with a finite
    # GPU-second budget is refused with 501 there too: shared endpoint GPU time is not metered per call)
    api._endpoint_limits(api.Principal(key="", tenant=tenant, info=ki, key_hash=key_hash), "async")
    # the durable row first, under the one-active-child and per-unit identities (compare-and-swap on the primary key)
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 3))", (root,))
        existing = c.execute(
            "select child, request_sha256 from endpoint_children where root = %s and unit_id = %s", (root, unit_id)
        ).fetchone()
        if existing:
            if existing[1] != h:
                raise HTTPException(
                    409, f"unit {unit_id} of {root} already has a child with a different request ({existing[0]})"
                )
            return readback(ns, root, unit_id), False  # replay / readback after a lost response
        n = c.execute("select count(*) from endpoint_children where root = %s", (root,)).fetchone()[0]
        if n >= max_units:
            raise HTTPException(409, f"run {root}: {max_units} children already (child_max_units of {m['name']})")
        active = c.execute(
            "select child, unit_id from endpoint_children where root = %s and state = 'active'", (root,)
        ).fetchone()
        if active:
            raise HTTPException(
                409, f"run {root}: child {active[0]} (unit {active[1]}) is still active; one child at a time"
            )
        remaining = _remaining_s(job)
        if remaining is not None and remaining < MIN_REMAINING_S:
            raise HTTPException(409, f"run {parent}: {remaining} s of its deadline left; no child attaches")
        timeout = int(body.get("timeout_s") or int((cm.get("runtime") or {}).get("timeout") or 600))
        if remaining is not None:
            timeout = min(timeout, remaining)
        if timeout < MIN_REMAINING_S:
            raise HTTPException(400, f"timeout_s must leave at least {MIN_REMAINING_S} s")
        c.execute(
            "insert into endpoint_children (root, unit_id, child, parent, parent_pod, parent_pod_uid, model, request_sha256) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (root, unit_id, child, parent, identity["pod"], identity["uid"], model, h),
        )
    # the ledger: a run member of the root (one request), then the caller Job as every endpoint call
    import sessions

    try:
        keys.prepare_run_member(root, child, None, sessions.gpus_per_replica(cm), timeout, kind="child", stage=unit_id)
    except keys.KeyLimit as e:
        _drop(root, unit_id)
        raise HTTPException(e.status, e.detail)
    try:
        cluster, child_region = api._deployment_of(cm, body.get("region"))
        import artifacts

        prefix = f"s3://{artifacts.storage(ns, child_region)['bucket']}/operations/{child}"
        if not api.cfg.ROUTER_KEY:
            raise HTTPException(503, "endpoint children need the fleet's admission secret (ROUTER_KEY)")
        # the child's caller carries ONLY the operation-scoped credential: the admission token (HMAC of the root's key
        # hash, the child model and the child operation), presented as `Bearer s2-admitted.<token>`; the gateway hook
        # rebuilds the principal from the key record by hash and accepts it only while the child holds a live
        # reservation. No customer or admin key is stored or forwarded; the call goes through the model's gateway
        # (never LiteLLM's pass-through, which would need a key)
        admitted = api._admitted_token(key_hash, cm["name"], child)
        cjob, secret = jobs.build_async(
            child,
            {**cm, "litellm_route": None},
            inp,
            tenant,
            f"{api.ADMITTED_BEARER_PREFIX}{admitted}",
            f"child:{unit_id}",
            None,
            timeout,
            prefix,
            child_region,
            ex=None,
            key_hash=key_hash,
            admitted=admitted,
        )
        a = cjob["metadata"]["annotations"]
        a[f"{LABEL}/child-of"] = parent
        a[f"{LABEL}/child-root"] = root
        a[f"{LABEL}/child-unit"] = unit_id
        a[f"{LABEL}/child-request-sha256"] = h
        jobs._inherit_owner(cjob, ann)
        created, was_created = jobs.create(ns, cjob, child_region, secret)
    except Exception:
        try:
            keys.rollback_run_member(child)
        except Exception as e:  # noqa: BLE001
            log.warning("child %s: ledger rollback: %s", child, e)
        _drop(root, unit_id)
        raise
    log.info("child %s of %s (root %s, unit %s) admitted on %s", child, parent, root, unit_id, child_region)
    return readback(ns, root, unit_id), True


def _drop(root: str, unit_id: str) -> None:
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "delete from endpoint_children where root = %s and unit_id = %s and state = 'active'", (root, unit_id)
        )


def sweep() -> list[str]:
    """Every 15 s: an active child whose Job ended is recorded ended; an active child whose root is cancelled, finished
    or closed is cancelled (the cascade is durable: the row keeps the cause). Returns `<child>:<what>`."""
    if not enabled():
        return []
    out = []
    import attempts

    with db.pool().connection() as c:
        rows = c.execute("select root, unit_id, child, parent from endpoint_children where state = 'active'").fetchall()
    for root, unit_id, child, parent in rows:
        try:
            ns = None
            for cand in _namespaces():
                try:
                    cjob, cregion = jobs.find(cand, child)
                    ns = cand
                    break
                except HTTPException:
                    continue
            if ns is None:
                continue  # the child Job is not visible yet (or expired): nothing to judge
            if status.finished(cjob):
                _end(root, unit_id)
                out.append(f"{child}:ended")
                continue
            cancelled = False
            try:
                pjob, _ = jobs.find(ns, parent)
                pann = pjob["metadata"].get("annotations") or {}
                marks = attempts._marks(root) if attempts.enabled() else None
                cancelled = bool(
                    pann.get(f"{LABEL}/cancelled")
                    or (marks and marks[1])
                    or status.finished(pjob)
                    and pann.get(f"{LABEL}/cancelled")
                )
            except HTTPException as e:
                cancelled = e.status_code == 404 and not (attempts.enabled() and attempts.for_operation(root))
            if cancelled:
                jobs.cancel(ns, child, cregion)
                _end(root, unit_id)
                out.append(f"{child}:cancelled")
        except Exception:  # noqa: BLE001
            log.exception("child sweep: %s", child)
    return out


def cancel_for_root(ns: str, root: str) -> list[str]:
    """The cancel route's immediate cascade (the sweep repeats it durably)."""
    out = []
    if not enabled():
        return out
    with db.pool().connection() as c:
        rows = c.execute(
            "select unit_id, child from endpoint_children where root = %s and state = 'active'", (root,)
        ).fetchall()
    for unit_id, child in rows:
        try:
            _, cregion = jobs.find(ns, child)
            jobs.cancel(ns, child, cregion)
        except HTTPException as e:
            if e.status_code != 404:
                raise
        _end(root, unit_id)
        out.append(child)
    return out


def _end(root: str, unit_id: str) -> None:
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "update endpoint_children set state = 'ended', ended_at = now() where root = %s and unit_id = %s and state = 'active'",
            (root, unit_id),
        )


def _namespaces() -> list[str]:
    try:
        return [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
