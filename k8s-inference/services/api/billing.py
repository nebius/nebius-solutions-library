"""GPU accounting: the idempotent charge ledger and the key's spend in the fleet database (services/api/keys.py).

The Job annotation is a receipt, not the deduplication mechanism: `charges` is unique per (region, Job UID).
Only the control cluster runs this (CronJob api/billing); it has DATABASE_URL. Endpoint calls that enter
through LiteLLM are accounted by LiteLLM on its own record of the key; `:invoke` calls are accounted here.
"""

import asyncio
import json
import logging
import sys

from kubernetes.client.rest import ApiException

import artifacts
import attempts
import catalog
import db
import execution
import jobs
import keys
import kube
from config import LABEL
from resilience import retry
from status import _attempts, _secs, finished, gpu_seconds

log = logging.getLogger("billing")
BILLED, KEY = f"{LABEL}/billed", f"{LABEL}/key"
key_hash = keys.key_hash  # the Job carries the key's sha256, never the key


def cost_of(
    job: dict, model: dict | None, records: list | None = None, region: str | None = None
) -> tuple[float, float]:
    """(gpu_seconds, usd) for a finished run (the job carries its pods under _pods; records are the
    uploader's attempt files for pods that preemption deleted). The price is the region's: the Job's
    region label (regional path, pinned fleet runs), else `region`, the cluster the pods ran in
    (a worker's copy of a fleet-placed run carries no label)."""
    gpu_s = gpu_seconds(job, records)
    rates = json.loads(job["metadata"].get("annotations", {}).get(f"{LABEL}/billing-rates", "{}"))
    regional = _rates_for(rates, job["metadata"].get("labels", {}).get(f"{LABEL}/region"), region)
    usd = 0.0
    for attempt in _attempts(job.get("_pods", []), records, job["metadata"]["name"]):
        seconds = (_secs(attempt["started_at"], attempt["ended_at"]) or 0) * attempt["_gpus"]
        if not seconds:
            continue
        price = regional.get(attempt.get("_pool"))
        if price is None and len(set(regional.values())) == 1:
            price = next(iter(regional.values()))
        if price is None:
            raise ValueError("GPU attempt has no verified pool price; accounting deferred")
        usd += seconds / 3600 * float(price)
    if gpu_s and not regional:
        raise ValueError("GPU run has no price snapshot; accounting deferred")
    return gpu_s, round(usd, 6)


def _rates_for(rates: dict, label: str | None, region: str | None) -> dict:
    """The price snapshot of the cluster the run ran in. The snapshot (`billing-rates`, written at submission) is keyed
    by Nebius region; the Job's region label is a cluster id when the run was pinned to its admitting cluster (a
    resume on the kept volume, `_pin_to_worker`: `north`), a region name when it was placed by region, so the label
    is resolved through the fleet document (`kube.cluster_region`: `north` -> `eu-north1`, a region maps to itself)
    before the lookup; the raw label stands for a snapshot that was keyed by it. Nothing is guessed: no matching key
    -> no prices -> "accounting deferred" (unknown), never a zero."""
    candidates: list[str] = []
    if label:
        candidates.append(label)
        try:
            candidates.append(kube.cluster_region(label))
        except Exception:  # noqa: BLE001 - no fleet document here: the raw label and the caller's region remain
            pass
    if region:
        candidates.append(region)
    for key in candidates:
        if key in rates:
            return rates[key]
    return {}


def measured_usage(job: dict, records: list | None = None) -> float | None:
    """The run's GPU seconds when every GPU attempt was measured: a valid start and end, a finite non-negative
    duration and a GPU count; None when a GPU attempt lacks any of them (a lost worker, a record cut short): unknown
    is reported as unknown, never as zero. A run without GPUs measures zero."""
    attempts_ = _attempts(job.get("_pods", []), records, job.get("metadata", {}).get("name"))
    job_gpus = 0
    spec = job.get("spec") or {}
    for c in ((spec.get("template") or {}).get("spec") or {}).get("containers") or []:
        job_gpus += int(str(((c.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu") or 0))
    total = 0.0
    for a in attempts_:
        gpus = a.get("_gpus") or 0
        if not gpus:
            continue
        d = _secs(a.get("started_at"), a.get("ended_at"))
        if d is None or d < 0 or d != d or d == float("inf"):
            return None
        total += d * gpus
    if job_gpus and not any(a.get("_gpus") for a in attempts_):
        return None  # a GPU run with no measured attempt at all: nothing trustworthy to settle
    return round(total, 1)


def record_spend(token: str, usd: float, operation: str, gpu_s: float) -> float | None:
    if not db.enabled():
        raise RuntimeError("DATABASE_URL is required; GPU accounting fails closed")
    return keys.add_spend(token, usd, operation, gpu_s)


async def add_spend(token: str, usd: float, operation: str, gpu_s: float) -> float | None:
    return await asyncio.to_thread(record_spend, token, usd, operation, gpu_s)


def release_volume(job: dict, ns: str, region: str) -> None:
    """A SUCCEEDED run's work volume is normally deleted by its uploader; when that container was
    preempted before it could, the volume would otherwise sit until the Job's TTL. Idempotent."""
    ann = job["metadata"].get("annotations") or {}
    pvc = ann.get(f"{LABEL}/pvc")
    if not pvc or not any(
        c.get("type") == "Complete" and c.get("status") == "True"
        for c in (job.get("status", {}).get("conditions") or [])
    ):
        return
    try:
        retry(kube.core(region).delete_namespaced_persistent_volume_claim, pvc, ns)
        log.info("released %s/%s/%s", region, ns, pvc)
    except ApiException as e:
        if e.status != 404:
            log.warning("release %s/%s: %s", ns, pvc, e.reason)


def _annotate(region: str, ns: str, name: str, done: dict, job: dict | None = None) -> None:
    if job is not None and jobs.is_jobset(job):
        jobs.patch_jobset(ns, name, region, {"metadata": {"annotations": done}})
        return
    retry(kube.batch(region).patch_namespaced_job, name, ns, {"metadata": {"annotations": done}})


async def bill_once() -> int:
    """Bill every finished, unbilled run in every tenant namespace of every region. Returns the count."""
    n = 0
    for region in kube.regions():
        try:
            namespaces = [
                x.metadata.name for x in retry(kube.core(region).list_namespace, label_selector=f"{LABEL}/tenant").items
            ]
        except ApiException as e:
            log.warning("billing: list namespaces in %s: %s", region, e.reason)
            continue
        for ns in namespaces:
            try:
                b = kube.batch(region)
                items = [
                    b.api_client.sanitize_for_serialization(j)
                    for mode in ("run", "async")  # runs are metered; endpoint calls release their key reservation
                    for j in retry(b.list_namespaced_job, ns, label_selector=f"{LABEL}/mode={mode}").items
                ]
                items += jobs.list_jobsets(ns, region, f"{LABEL}/mode=run")  # multi-node runs (docs/JOBS.md)
            except ApiException as e:
                log.warning("billing: list jobs in %s/%s: %s", region, ns, e.reason)
                continue
            pods, clusters = None, None
            for job in items:
                ann, name = job["metadata"].get("annotations") or {}, job["metadata"]["name"]
                if BILLED in ann or KEY not in ann or not finished(job) or jobs.is_mirror(job):
                    continue  # a worker's copy of a manager Job is transient; the manager's Job is billed
                if job["metadata"].get("labels", {}).get(f"{LABEL}/mode") == "async":
                    # an endpoint call's Job: the key's slot and request were reserved at admission (docs/API.md
                    # "Keys"); the endpoint's GPU time is not metered per call, so nothing is charged here
                    try:
                        if db.enabled():
                            keys.release(name, 0.0)
                        _annotate(region, ns, name, {BILLED: "0", f"{LABEL}/gpu-seconds": "0"}, job)
                        if f"{LABEL}/execution" in ann:
                            execution.sweep_copies(ns, name)
                        n += 1
                    except Exception as e:  # noqa: BLE001 - retried on the next run
                        log.warning("settlement of endpoint call %s failed: %s", name, e)
                    continue
                price_region = region
                if jobs.is_manager_job(job):
                    if clusters is None:
                        clusters = kube.workload_clusters(ns)
                    cluster = clusters.get(job["metadata"].get("uid", ""))
                    price_region = kube.cluster_region(cluster) if cluster else None
                    job[
                        "_pods"
                    ] = []  # the worker's pods are gone with its copy: the uploader's records are the attempts
                else:
                    if pods is None:
                        raw = retry(kube.core(region).list_namespaced_pod, ns, label_selector=f"{LABEL}/mode=run").items
                        pods = [kube.core(region).api_client.sanitize_for_serialization(p) for p in raw]
                    job["_pods"] = [
                        p
                        for p in pods
                        if (
                            p["metadata"].get("labels", {}).get("jobset.sigs.k8s.io/jobset-name")
                            or p["metadata"].get("labels", {}).get("job-name")
                        )
                        == name
                    ]
                root = ann.get(f"{LABEL}/resumed-from") or name
                records = attempts.for_operation(root) if attempts.enabled() else []
                records = [r for r in records if r["operation"] == name] or artifacts.attempt_records(
                    ns, name, region, jobs.output_prefix(job)
                )
                if not jobs.is_manager_job(job) and jobs.normalise(job, None, records)["status"] == "SUCCEEDED":
                    release_volume(job, ns, region)  # delivered: the volume may go (an EXPORT_FAILED run keeps it)
                try:
                    identity = job["metadata"].get("uid")
                    if not identity:
                        raise ValueError("job UID is required for accounting")
                    measured = measured_usage(job, records)
                    if measured is None:
                        # nothing trustworthy to charge: the key's reservation stays on record as unknown GPU time
                        # (keys.settle_run_member None); an operator settles it later (keys.settle_unknown)
                        if db.enabled():
                            keys.release(name, None)
                        done = {BILLED: "unknown", f"{LABEL}/gpu-seconds": "unknown"}
                        _annotate(region, ns, name, done, job)
                        log.warning("billing %s/%s: GPU time not measured; reservation kept as unknown", region, name)
                        n += 1
                        continue
                    gpu_s, usd = cost_of(
                        job, catalog.get(job["metadata"]["labels"].get(f"{LABEL}/model", "")), records, price_region
                    )
                    total = await add_spend(ann[KEY], usd, f"{region}/{identity}", gpu_s)
                    if db.enabled():
                        keys.release(name, gpu_s)  # the key's slot and its GPU-second reservation (keys.admit)
                    done = {BILLED: f"{usd}", f"{LABEL}/gpu-seconds": f"{gpu_s}"}
                    _annotate(region, ns, name, done, job)
                    if f"{LABEL}/execution" in ann:
                        execution.sweep_copies(ns, name)  # this attempt only: its successor may still be running
                    log.info("billed %s/%s: %.0f GPU-s -> $%s (key spend now %s)", region, name, gpu_s, usd, total)
                    n += 1
                except Exception as e:  # noqa: BLE001 - unbilled, retried on the next run
                    log.warning("billing %s failed: %s", name, e)
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    billed = asyncio.run(bill_once())
    log.info("billed %d run(s)", billed)
