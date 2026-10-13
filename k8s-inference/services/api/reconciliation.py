"""Durable fleet reconciliation, independent of HTTP routing.

Desired model generations commit before reconciliation. The database lock serializes reconcilers;
finish_change fences completion by generation. Partial failures remain pending and retry idempotently.
Authorization policies stay until the corresponding predictor routes have disappeared.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

import attempts
import catalog
import config as cfg
import db
import execution
import jobs
import keys
import kube
import models
import status
from config import FLEET_MANAGER, REGION

log = logging.getLogger("api")
RESERVATION_STALE_S = 3600


def _deploy(entry: dict, after: dict, result: dict):
    """Render the endpoint on every cluster of its `deployments`, put its hostname on the region's certificate
    and register its LiteLLM group there. `after` is the runtime catalog as it will be after this write."""
    if entry["mode"] == "run":
        return
    for cid in entry["deployments"]:
        region = kube.cluster_region(cid)
        if region not in kube.regions():
            raise HTTPException(400, f"regions: no connection to {region} from this API (REGION_KUBECONFIGS)")
        result["applied"][cid] = models.apply(entry, cid, region)
        models.sync_certificate(region, after)
        if w := models.litellm_group_upsert(entry, cid, region):
            result.setdefault("warnings", []).append(w)
            raise RuntimeError(w)


def _undeploy(entry: dict, clusters: list[str], after: dict):
    if entry.get("mode") == "run":
        return
    for cid in clusters:
        region = kube.cluster_region(cid)
        models.delete_rendered(entry, cid, region)
        models.sync_certificate(region, after)
        if warning := models.litellm_group_delete(
            entry["id"], cid, [v["name"] for v in models.variants(entry, cid)[1:]]
        ):
            raise RuntimeError(warning)


def _prune_variants(before: dict, entry: dict, clusters: list[str]):
    """A redefinition that drops a variant while its region stays deployed: `apply` renders only the new variants, so
    the dropped one's InferenceService (its warm replicas hold GPUs), authorization and LiteLLM deployment are
    removed here, with the same route-teardown retention as a region's removal (models.delete_variants)."""
    for cid in clusters:
        objects, ids = models.removed_variants(before, entry, cid)
        if not objects:
            continue
        models.delete_variants(before, cid, objects, kube.cluster_region(cid))
        if warning := models.litellm_deployments_delete(ids):
            raise RuntimeError(warning)


def reconcile_models() -> dict:
    results = {}
    with db.reconciler() as acquired:
        if not acquired:
            return results
        for change in db.pending_changes():
            mid, entry, before = change["id"], change["entry"], change["previous"]
            result = {"applied": {}, "version": change["version"]}
            try:
                after = models.list_runtime()
                if entry:
                    _deploy(entry, after, result)
                removed = list(before.get("deployments", {}))
                if entry and entry.get("mode") != "run":
                    removed = [c for c in removed if c not in entry.get("deployments", {})]
                if before:
                    _undeploy(before, removed, after)
                    if entry and entry.get("mode") != "run" and before.get("mode") != "run":
                        _prune_variants(before, entry, [c for c in before.get("deployments", {}) if c not in removed])
                db.finish_change(mid, change["version"])
            except Exception:
                log.exception("model reconciliation failed for %s generation %s", mid, change["version"])
                result["pending"] = True
                db.finish_change(mid, change["version"], "Deployment reconciliation failed; retrying.")
            results[mid] = result
    return results


def idle_shutdown() -> list[str]:
    """Multi-node endpoints (`nodes` > 1) with scaling.min 1 and scaling.idle_s: stopped (scaling.min written to 0 in
    the fleet database, history entry "idle shutdown") once no request reached them for idle_s seconds, counted from
    the later of the last request (the regional API stamps the LeaderWorkerSet on every authorized call) and the
    moment the leader became Ready. Returns the ids stopped. Control API only."""
    if not db.enabled():
        return []
    stopped = []
    now = datetime.now(timezone.utc)
    for m in list(catalog.all_models().values()):
        spec = m.get("spec") or {}
        sc = spec.get("scaling") or {}
        if (
            m.get("managed_by") != "api"
            or int(m.get("nodes") or 1) <= 1
            or int(sc.get("min") or 0) < 1
            or not sc.get("idle_s")
        ):
            continue
        try:
            busy = False
            for cid in m.get("deployments") or {}:
                region = kube.cluster_region(cid)
                st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m["nodes"]))
                marks = [st.get(k) for k in ("ready_at", "last_request_at") if st.get(k)]
                if st["status"] != "ready" or not marks:
                    busy = True  # starting, or no Ready pod yet: nothing to measure
                    continue
                last = max(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in marks)
                if (now - last).total_seconds() < int(sc["idle_s"]):
                    busy = True
            if busy:
                continue
            new_spec = {**spec, "scaling": {**sc, "min": 0}}
            entry = models.to_entry(new_spec, managed_by="api")
            row = db.get_model(m["name"])
            try:  # an admin's write in between wins: this round skips, the next one re-reads
                db.upsert_model(
                    entry, new_spec, by="idle-shutdown", expected=row.get("version") if row else None, create_only=False
                )
            except HTTPException as e:
                if e.status_code != 409:
                    raise
                continue
            catalog.invalidate()
            stopped.append(m["name"])
            log.info("idle shutdown: %s stopped after %ss without a request", m["name"], sc["idle_s"])
        except Exception:
            log.exception("idle shutdown check failed for %s", m["name"])
    return stopped


def continue_run(root: str, build, transfer_only: bool = False):
    from app import continue_run as _continue_run  # app imports this module; resolved at call time

    return _continue_run(root, build, transfer_only)


def settle_deleted(job: dict) -> None:
    from app import settle_deleted as _settle_deleted

    _settle_deleted(job)


def fallback_switch() -> list[str]:
    """Fleet runs that asked for `placement.fallback_wait_s` and are still unplaced after it: moved to the next class of
    their preference (jobs.switch_class). Returns the new operation ids. Control API only."""
    if not FLEET_MANAGER:
        return []
    moved = []
    now = datetime.now(timezone.utc)
    try:
        namespaces = [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{cfg.LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
    for ns in namespaces:
        try:
            b = kube.batch()
            items = [
                b.api_client.sanitize_for_serialization(j)
                for j in b.list_namespaced_job(ns, label_selector=f"{cfg.LABEL}/mode=run").items
            ]
        except Exception:  # noqa: BLE001
            continue
        clusters = None
        for job in items:
            ann = job["metadata"].get("annotations") or {}
            wait = ann.get(f"{cfg.LABEL}/fallback-wait-s")
            if (
                wait is None
                or not jobs.is_manager_job(job)
                or status.finished(job)
                or ann.get(f"{cfg.LABEL}/successor")
            ):
                continue
            created = datetime.fromisoformat(str(job["metadata"]["creationTimestamp"]).replace("Z", "+00:00"))
            if (now - created).total_seconds() < int(wait):
                continue
            if clusters is None:
                clusters = kube.workload_clusters(ns)
            if clusters.get(job["metadata"].get("uid", "")):
                continue  # placed: the wait is over
            try:
                ex = jobs.normalise(job).get("execution") or {}
                pref = (ex.get("placement") or {}).get("gpu_preference") or []
                current = job["metadata"].get("labels", {}).get(f"{cfg.LABEL}/gpu-class") or (pref[0] if pref else None)
                remaining = pref[pref.index(current) + 1 :] if current in pref else []
                if not remaining:
                    continue  # the last class of the preference: keep waiting
                m = catalog.require(job["metadata"]["labels"].get(f"{cfg.LABEL}/model", ""))
                root = ann.get(f"{cfg.LABEL}/resumed-from") or job["metadata"]["name"]
                new, _ = continue_run(
                    root,
                    lambda admit: jobs.switch_class(
                        ns,
                        job["metadata"]["name"],
                        REGION,
                        m,
                        catalog.fleet_params(m),
                        remaining[0],
                        remaining,
                        admit=admit,
                    ),
                )
                moved.append(new["metadata"]["name"])
                log.info(
                    "fallback: %s/%s -> %s on %s", ns, job["metadata"]["name"], new["metadata"]["name"], remaining[0]
                )
            except Exception:  # noqa: BLE001
                log.exception("fallback of %s/%s failed", ns, job["metadata"]["name"])
    return moved


def _lost(op: dict, job: dict) -> tuple[dict | None, str | None]:
    """The confirmed infrastructure loss of a run, if any: (the lost attempt, why) when no attempt is RUNNING and the
    newest attempt that ran ended PREEMPTED (a pod with DisruptionTarget or deleted, an uploader record `interrupted`,
    or a `started` record whose pod no longer exists on the worker). Never: an application failure (exit code, OOM),
    a deadline, a cancel, a finished run, a run that never started, or a worker whose pods are unknown."""
    if job.get("_pods_unknown") or op["status"] not in ("QUEUED", "RUNNING"):
        return None, None
    attempts_ = op.get("attempts") or []
    if any(a["status"] == "RUNNING" for a in attempts_):
        return None, None
    ran = [a for a in attempts_ if a.get("started_at")]
    if not ran or ran[-1]["status"] != "PREEMPTED":
        return None, None
    return ran[-1], ran[-1].get("reason") or "infrastructure loss"


def recovery_switch() -> list[str]:
    """Runs that asked for `recovery.on_infrastructure_loss: recover` and lost their worker (docs/SCHEDULING.md
    "Automatic recovery after infrastructure loss"): once the loss stands for `wait_s` without a replacement attempt,
    the run continues from its newest acknowledged checkpoint pointer on the next class of its preference
    (jobs.recover), within `max_recoveries` and `deadline_s`. Returns the successors' ids. Control API only."""
    if not FLEET_MANAGER or not attempts.enabled():
        return []
    moved = []
    now = datetime.now(timezone.utc)
    try:
        namespaces = [x.metadata.name for x in kube.core().list_namespace(label_selector=f"{cfg.LABEL}/tenant").items]
    except Exception:  # noqa: BLE001
        return []
    for ns in namespaces:
        try:
            b = kube.batch()
            items = [
                b.api_client.sanitize_for_serialization(j)
                for j in b.list_namespaced_job(ns, label_selector=f"{cfg.LABEL}/mode=run").items
            ]
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            md = item["metadata"]
            ann = md.get("annotations") or {}
            name = md["name"]
            try:
                ex = json.loads(ann.get(f"{cfg.LABEL}/execution") or "null") or {}
            except ValueError:
                ex = {}
            rec = ex.get("recovery") or {}
            if (
                rec.get("on_infrastructure_loss") != "recover"
                or not jobs.is_manager_job(item)
                or status.finished(item)
                or ann.get(f"{cfg.LABEL}/successor")
                or ann.get(f"{cfg.LABEL}/cancelled")
            ):
                continue
            try:
                job = jobs.get(ns, name, REGION)
                root = ann.get(f"{cfg.LABEL}/resumed-from") or name
                op = jobs.normalise(job, records=attempts.for_operation(root))
                lost, why = _lost(op, job)
                if lost is None:
                    if ann.get(f"{cfg.LABEL}/lost-at"):  # back on its own class: the loss is over
                        _annotate(ns, name, {f"{cfg.LABEL}/lost-at": None})
                    continue
                lost_at = ann.get(f"{cfg.LABEL}/lost-at")
                if not lost_at:
                    _annotate(ns, name, {f"{cfg.LABEL}/lost-at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
                    lost_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                if (now - datetime.fromisoformat(lost_at.replace("Z", "+00:00"))).total_seconds() < int(
                    rec.get("wait_s") or 0
                ):
                    continue
                tenant = md["labels"][f"{cfg.LABEL}/tenant"]
                marks = attempts._marks(name)
                if marks and marks[1]:
                    continue  # cancelled: never resurrected
                stop = None
                pointer = (attempts.latest_checkpoint(root) or {}).get("checkpoint_ref")
                if not pointer:
                    stop = "no acknowledged checkpoint pointer: the run cannot continue elsewhere (POST :resume after a cancel)"
                done = jobs.recoveries(ns, tenant, root)
                if not stop and done >= int(rec.get("max_recoveries") or 1):
                    stop = f"recovery exhausted: {done} of {rec.get('max_recoveries')} automatic continuations used"
                remaining_s = None
                if rec.get("deadline_s"):
                    root_job = job if root == name else jobs.get(ns, root, REGION)
                    born = datetime.fromisoformat(str(root_job["metadata"]["creationTimestamp"]).replace("Z", "+00:00"))
                    remaining_s = int(born.timestamp() + int(rec["deadline_s"]) - now.timestamp())
                    if not stop and remaining_s <= 0:
                        stop = f"recovery deadline passed: {rec['deadline_s']} s after the root operation was created"
                m = catalog.require(md["labels"].get(f"{cfg.LABEL}/model", ""))
                pref = (ex.get("placement") or {}).get("gpu_preference") or jobs.gpu_classes(m)
                current = (
                    lost.get("gpu_class")
                    or md.get("labels", {}).get(f"{cfg.LABEL}/gpu-class")
                    or (pref[0] if pref else None)
                )
                remaining = pref[pref.index(current) + 1 :] if current in pref else list(pref)
                next_class = remaining[0] if remaining else current
                if not stop and not next_class:
                    stop = "no GPU class to continue on"
                if stop:
                    if ann.get(f"{cfg.LABEL}/recovery") != stop:
                        _annotate(ns, name, {f"{cfg.LABEL}/recovery": stop})
                    continue
                placement = (
                    execution.narrow_placement(
                        {"profile": jobs.profile_of(m), "classes": [next_class], "regions": m["regions"]}, ex
                    )
                    or {}
                )
                placement["classes"] = [next_class]
                new, created = continue_run(
                    root,
                    lambda admit: jobs.recover(
                        ns,
                        name,
                        REGION,
                        m,
                        placement,
                        pointer,
                        int(lost.get("epoch") or 0),
                        next_class,
                        remaining or [next_class],
                        why,
                        timeout_s=remaining_s,
                        admit=admit,
                    ),
                )
                marks = attempts._marks(name)
                if marks and marks[1]:  # cancelled while we were moving it: cancel the successor, too
                    attempts.mark_cancelled(new["metadata"]["name"])
                    settle_deleted(jobs.cancel(ns, new["metadata"]["name"], REGION))  # never started: deleted
                    _annotate(
                        ns,
                        name,
                        {
                            f"{cfg.LABEL}/cancelled": "true",
                            f"{cfg.LABEL}/recovery": "cancelled while recovering: the successor was withdrawn",
                        },
                    )
                    continue
                if created:
                    moved.append(new["metadata"]["name"])
                    log.info("recovery: %s/%s -> %s on %s (%s)", ns, name, new["metadata"]["name"], next_class, why)
            except Exception:  # noqa: BLE001
                log.exception("recovery of %s/%s failed", ns, name)
    return moved


def _annotate(ns: str, name: str, annotations: dict) -> None:
    kube.batch().patch_namespaced_job(name, ns, {"metadata": {"annotations": annotations}})


async def _reconcile_loop():
    while True:
        try:
            await asyncio.to_thread(idle_shutdown)
            await asyncio.to_thread(fallback_switch)
            await asyncio.to_thread(recovery_switch)
            await asyncio.to_thread(reconcile_models)
            await asyncio.to_thread(release_stale_reservations)
            await asyncio.to_thread(gate_stages)
            await asyncio.to_thread(sweep_sessions)
            await asyncio.to_thread(keys.retire_mirrors)
        except Exception:
            log.exception("model reconciliation unavailable")
        await asyncio.sleep(15)


def gate_stages() -> list[str]:
    """Stage composition (docs/JOBS.md): admit waiting stages whose dependencies delivered, cancel what cannot run,
    close graphs that are over (stages.gate). Needs the ledger (the database)."""
    if not db.enabled():
        return []
    import stages

    return stages.gate()


def sweep_sessions() -> list[str]:
    """Live sessions (docs/API.md "Sessions"): expired claims, unattached bindings, deadlines, lost workers, unsettled
    ends (sessions.sweep). Nothing without the feature or the database."""
    import sessions

    return sessions.sweep()


def release_stale_reservations() -> list[str]:
    """A key reservation whose operation no longer exists (a Job deleted before billing accounted it, a submission
    that failed after admission) is released after RESERVATION_STALE_S; billing releases the rest with the attempt's
    GPU seconds. A run's reservation is its root's: it goes when every member's Job is gone, each member settled at
    zero when it never held a GPU (no bound) and as `unknown` otherwise (a GPU attempt that vanished unmeasured is
    not zero). Control API only. Returns `<operation>` for a released reservation, `<operation>:unknown` for one
    whose GPU time stays on record."""
    if not db.enabled():
        return []
    out = []
    for operation, tenant in keys.stale_reservations(RESERVATION_STALE_S):
        ns = f"{cfg.TENANT_NS_PREFIX}{tenant}"
        members = keys.run_members(operation) if hasattr(keys, "run_members") else []
        if not members:
            try:
                jobs.find(ns, operation)
            except HTTPException as e:
                if e.status_code == 404:
                    keys.release(operation, None)
                    out.append(operation)
            continue
        unknown = False
        for m in members:
            if m["state"] != "active":
                continue
            try:
                jobs.find(ns, m["operation"])
                break  # a member's Job still exists: billing settles it
            except HTTPException as e:
                if e.status_code != 404:
                    break
            keys.settle_run_member(m["operation"], 0.0 if not m["gpu_bound"] else None)
            unknown = unknown or bool(m["gpu_bound"])
        else:
            out.append(f"{operation}:unknown" if unknown else operation)
    return out
