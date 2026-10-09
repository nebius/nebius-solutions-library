"""GPU accounting: an idempotent ledger and atomic LiteLLM spend increment in its existing database.

The Job annotation is a receipt, not the deduplication mechanism. The pinned LiteLLM schema and its
database-authoritative budget checks are part of this integration contract (docs/OPERATIONS.md).
"""
import asyncio, hashlib, json, logging, os, sys
from kubernetes.client.rest import ApiException
import artifacts, catalog, jobs, kube
from config import LABEL
from resilience import retry
from status import _attempts, _secs

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
    rates = json.loads(job["metadata"].get("annotations", {}).get(f"{LABEL}/billing-rates", "{}"))
    legacy = ((model or {}).get("deployments", {}).get(region) or {}).get("price_per_gpu_hour")
    if not rates and legacy is not None:
        return gpu_s, round(gpu_s / 3600 * float(legacy), 6)
    regional = rates.get(region, {})
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


def record_spend(token: str, usd: float, operation: str, gpu_s: float) -> float:
    import psycopg
    url = os.environ.get("BILLING_DATABASE_URL")
    if not url:
        raise RuntimeError("BILLING_DATABASE_URL is required; GPU accounting fails closed")
    with psycopg.connect(url) as c:
        c.execute("select pg_advisory_xact_lock(1229003)")
        c.execute("""create table if not exists serverless_gpu_charges (
            operation text primary key, token text not null, gpu_seconds double precision not null,
            usd double precision not null, charged_at timestamptz not null default now())""")
        inserted = c.execute("""insert into serverless_gpu_charges (operation, token, gpu_seconds, usd)
            values (%s, %s, %s, %s) on conflict do nothing returning operation""", (operation, token, gpu_s, usd)).fetchone()
        if inserted:
            row = c.execute('UPDATE "LiteLLM_VerificationToken" SET spend = spend + %s, total_spend = total_spend + %s WHERE token = %s RETURNING spend', (usd, usd, token)).fetchone()
            if row is None:
                raise ValueError("billing key does not exist; transaction rolled back")
            return float(row[0])
        receipt = c.execute("select token, gpu_seconds, usd from serverless_gpu_charges where operation = %s", (operation,)).fetchone()
        if receipt[0] != token:
            raise ValueError("operation already belongs to another billing key")
        return float(c.execute('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token = %s', (token,)).fetchone()[0])


async def add_spend(token: str, usd: float, operation: str, gpu_s: float) -> float:
    return await asyncio.to_thread(record_spend, token, usd, operation, gpu_s)


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
                try:
                    gpu_s, usd = cost_of(job, catalog.get(job["metadata"]["labels"].get(f"{LABEL}/model", "")), records, price_region)
                    identity = job["metadata"].get("uid")
                    if not identity:
                        raise ValueError("job UID is required for accounting")
                    total = await add_spend(ann[KEY], usd, f"{region}/{identity}", gpu_s)
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
