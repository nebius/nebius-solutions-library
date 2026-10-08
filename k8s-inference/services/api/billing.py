"""Run-class spend: GPU-seconds (main container running time x GPUs over every attempt of the Job)
x price per GPU-hour of the region (catalog deployments.<region>.price_per_gpu_hour) written to the
submitting key's LiteLLM spend when a run finishes. Runs as one CronJob (`python -m billing`,
clusters/control/apps/overlays/api/billing-cronjob.yaml, concurrencyPolicy Forbid), so a Job is billed
exactly once without a claim protocol; the `billed` annotation makes the pass idempotent.

A fleet-placed run is billed from the manager's Job: MultiKueue deletes the worker's copy (and its pods)
the moment the manager's Job finishes, so the attempts come from the uploader's records in the bucket
(the design's source of truth anyway) and the price from the region the Workload was admitted in."""
import asyncio, hashlib, logging, sys
import httpx
from kubernetes.client.rest import ApiException
import artifacts, catalog, jobs, kube
from config import LABEL, LITELLM_MASTER_KEY, LITELLM_URL
from resilience import retry, retry_http

log = logging.getLogger("billing")
BILLED, KEY = f"{LABEL}/billed", f"{LABEL}/key"


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()   # LiteLLM's token hash; usable with /key/info and /key/update


def cost_of(job: dict, model: dict | None, records: list | None = None, region: str | None = None) -> tuple[float, float]:
    """(gpu_seconds, usd) for a finished run (the job carries its pods under _pods; records are the
    uploader's attempt files for pods that preemption deleted). The price is the region's: the Job's
    region label (regional path, pinned fleet runs), else `region`, the cluster the pods ran in
    (a worker's copy of a fleet-placed run carries no label)."""
    gpu_s = jobs.gpu_seconds(job, records)
    region = job["metadata"].get("labels", {}).get(f"{LABEL}/region") or region or ""
    price = float(((model or {}).get("deployments", {}).get(region) or {}).get("price_per_gpu_hour") or 0)
    return gpu_s, round(gpu_s / 3600 * price, 6)


async def add_spend(token: str, usd: float) -> float:
    """LiteLLM has no increment call: read spend, add, write (/key/update). A pass-through call that
    lands between the two requests loses its increment; the window is one round trip per billed run."""
    h = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await retry_http(lambda: c.get(f"{LITELLM_URL}/key/info", params={"key": token}, headers=h))
        r.raise_for_status()
        spend = float(r.json()["info"].get("spend") or 0) + usd
        r = await retry_http(lambda: c.post(f"{LITELLM_URL}/key/update", json={"key": token, "spend": spend}, headers=h))
        r.raise_for_status()
    return spend


def release_volume(job: dict, ns: str, region: str) -> None:
    """A SUCCEEDED run's work volume is normally deleted by its uploader; when that container was
    preempted before it could, the volume would otherwise sit until the Job's TTL. Idempotent."""
    ann = job["metadata"].get("annotations") or {}
    pvc = ann.get(f"{LABEL}/pvc")
    if not pvc or not any(c.get("type") == "Complete" and c.get("status") == "True" for c in (job.get("status", {}).get("conditions") or [])):
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
            namespaces = [x.metadata.name for x in retry(kube.core(region).list_namespace, label_selector=f"{LABEL}/tenant").items]
        except ApiException as e:
            log.warning("billing: list namespaces in %s: %s", region, e.reason)
            continue
        for ns in namespaces:
            try:
                b = kube.batch(region)
                items = [b.api_client.sanitize_for_serialization(j) for j in retry(b.list_namespaced_job, ns, label_selector=f"{LABEL}/mode=run").items]
                items += jobs.list_jobsets(ns, region, f"{LABEL}/mode=run")      # multi-node runs (docs/JOBS.md)
            except ApiException as e:
                log.warning("billing: list jobs in %s/%s: %s", region, ns, e.reason)
                continue
            pods, clusters = None, None
            for job in items:
                ann, name = job["metadata"].get("annotations") or {}, job["metadata"]["name"]
                if BILLED in ann or KEY not in ann or not jobs.finished(job) or jobs.is_mirror(job):
                    continue                  # a worker's copy of a manager Job is transient; the manager's Job is billed
                price_region = region
                if jobs.is_manager_job(job):
                    if clusters is None:
                        clusters = kube.workload_clusters(ns)
                    cluster = clusters.get(job["metadata"].get("uid", ""))
                    price_region = kube.cluster_region(cluster) if cluster else None
                    job["_pods"] = []         # the worker's pods are gone with its copy: the uploader's records are the attempts
                else:
                    release_volume(job, ns, region)
                    if pods is None:
                        raw = retry(kube.core(region).list_namespaced_pod, ns, label_selector=f"{LABEL}/mode=run").items
                        pods = [kube.core(region).api_client.sanitize_for_serialization(p) for p in raw]
                    job["_pods"] = [p for p in pods if (p["metadata"].get("labels", {}).get("jobset.sigs.k8s.io/jobset-name") or p["metadata"].get("labels", {}).get("job-name")) == name]
                records = artifacts.attempt_records(ns, name, region, jobs.output_prefix(job))
                gpu_s, usd = cost_of(job, catalog.get(job["metadata"]["labels"].get(f"{LABEL}/model", "")), records, price_region)
                try:
                    total = await add_spend(ann[KEY], usd) if usd else None
                    done = {BILLED: f"{usd}", f"{LABEL}/gpu-seconds": f"{gpu_s}"}
                    _annotate(region, ns, name, done, job)
                    log.info("billed %s/%s: %.0f GPU-s -> $%s (key spend now %s)", region, name, gpu_s, usd, total)
                    n += 1
                except Exception as e:  # noqa: BLE001 - unbilled, retried on the next run
                    log.warning("billing %s failed: %s", name, e)
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    billed = asyncio.run(bill_once())
    log.info("billed %d run(s)", billed)
