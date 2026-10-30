"""GPU accounting: the idempotent charge ledger and the key's spend in the fleet database (services/api/keys.py).

The Job annotation is a receipt, not the deduplication mechanism: `charges` is unique per (region, Job UID).
Only the control cluster runs this (CronJob api/billing); it has DATABASE_URL. Endpoint calls that enter
through LiteLLM are accounted by LiteLLM on its own record of the key; `:invoke` calls are accounted here.
"""

import asyncio
import json
import logging
import sys
from contextlib import suppress

import psycopg
from kubernetes.client import V1DeleteOptions, V1Preconditions
from kubernetes.client.rest import ApiException

import artifacts
import attempts
import catalog
import db
import execution
import jobs
import keys
import kube
import leases
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


def cleanup_released_pods(region: str, ns: str) -> int:
    """Finish an already-requested work-volume deletion without shortening Job/attempt retention.

    The uploader (or release_volume) has authorized PVC deletion. Kubernetes PVC protection still keeps it while
    retained terminal Pods reference it. Remove only those exact Pods after checking *all* consumers, their terminal
    owning Jobs and durable Pod-UID attempt receipts in one root. Jobs, attempts and shipped logs remain; a failed
    export's retained PVC, an active/unknown consumer or incomplete provenance never authorizes cleanup.
    """
    if not attempts.enabled():
        return 0
    deleted = 0
    try:
        core, batch = kube.core(region), kube.batch(region)
        claims = [
            core.api_client.sanitize_for_serialization(p)
            for p in retry(core.list_namespaced_persistent_volume_claim, ns, label_selector=f"{LABEL}/operation").items
        ]
        claims = [p for p in claims if p["metadata"].get("deletionTimestamp")]
        if not claims:
            return 0
        # No operation/mode selector: even an unrelated reader blocks deletion of every consumer of this claim.
        pods = [core.api_client.sanitize_for_serialization(p) for p in retry(core.list_namespaced_pod, ns).items]
        records: dict = {}
        for pvc in claims:
            try:
                for md in _released_pod_targets(pvc, pods, batch, ns, records):
                    exact = V1Preconditions(uid=md["uid"], resource_version=md["resourceVersion"])
                    try:
                        retry(core.delete_namespaced_pod, md["name"], ns, body=V1DeleteOptions(preconditions=exact))
                        deleted += 1
                    except ApiException as e:
                        if e.status not in (404, 409):  # gone or changed: reconsider on the next pass, never force
                            raise
            except Exception as e:  # noqa: BLE001 - incomplete evidence leaves this volume protected
                name = pvc["metadata"]["name"]
                log.warning("released volume cleanup %s/%s/%s deferred (%s)", region, ns, name, type(e).__name__)
    except Exception as e:  # noqa: BLE001 - cleanup must not prevent accounting or imply absence on an API failure
        log.warning("released volume cleanup %s/%s deferred (%s)", region, ns, type(e).__name__)
    return deleted


def _released_pod_targets(pvc: dict, pods: list[dict], batch, ns: str, records: dict) -> list[dict]:
    md = pvc["metadata"]
    operation = (md.get("labels") or {}).get(f"{LABEL}/operation")
    if not md.get("uid") or not md.get("deletionTimestamp") or not operation:
        return []
    consumers = [
        p
        for p in pods
        if any(
            (v.get("persistentVolumeClaim") or {}).get("claimName") == md["name"]
            for v in (p.get("spec") or {}).get("volumes") or []
        )
    ]
    targets, roots, names = [], set(), set()
    for pod in consumers:
        pm = pod.get("metadata") or {}
        if (
            (pod.get("status") or {}).get("phase") not in {"Succeeded", "Failed"}
            or pm.get("namespace") != ns
            or not pm.get("uid")
            or not pm.get("resourceVersion")
        ):
            return []
        owners = [r for r in pm.get("ownerReferences") or [] if r.get("controller") is True]
        if len(owners) != 1 or owners[0].get("kind") != "Job" or owners[0].get("apiVersion") != "batch/v1":
            return []
        owner = owners[0]
        job = batch.api_client.sanitize_for_serialization(retry(batch.read_namespaced_job, owner["name"], ns))
        jm = job.get("metadata") or {}
        ann = jm.get("annotations") or {}
        if (
            not owner.get("uid")
            or jm.get("uid") != owner["uid"]
            or jm.get("namespace") != ns
            or not finished(job)
            or (job.get("status") or {}).get("active", 0)
            or ann.get(f"{LABEL}/pvc") != md["name"]
        ):
            return []
        root = ann.get(f"{LABEL}/resumed-from") or jm["name"]
        roots.add(root)
        names.add(jm["name"])
        if len(roots) != 1:
            return []
        if root not in records:
            records[root] = attempts.for_operation(root)
        matches = [r for r in records[root] if r.get("pod") == pm["name"] and r.get("operation") == jm["name"]]
        if len(matches) != 1:
            return []
        r = matches[0]
        if (
            r.get("root") != root
            or r.get("namespace") != ns
            or r.get("pod_uid") != pm["uid"]
            or r.get("status") not in attempts.TERMINAL
            or not r.get("ended_at")
        ):
            return []
        targets.append(pm)
    return targets if operation in roots | names else []


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
            cache: dict = {}  # the namespace's pods and Workload placements, read once per pass
            for job in items:
                if await settle_job(region, ns, job, cache):
                    n += 1
            cleanup_released_pods(region, ns)  # also revisits already-billed Jobs whose PVC is still protected
    return n


KNOWN_ZERO = f"{LABEL}/billed-correction"


def _worker_pods(job: dict, ns: str, region: str):
    """The Job's pods (sanitized) in the manager's region and every worker, worker by worker; an absent namespace
    (404) holds nothing of the run, any other failure raises: an unreadable worker is never evidence."""
    for worker in dict.fromkeys([region, *kube.regions()]):
        core = kube.core(worker)
        try:
            pods = retry(core.list_namespaced_pod, ns, label_selector=jobs.pods_selector(job)).items
        except ApiException as error:
            if error.status == 404:
                continue
            raise
        yield from (core.api_client.sanitize_for_serialization(p) for p in pods)


def no_pods_anywhere(job: dict, ns: str, region: str) -> bool:
    """Strict: every worker (the manager's region and all workers) answered the pod list for this Job and none holds
    a pod of it in ANY phase (a terminal, unrecorded pod is a run). An unreadable worker (anything but an absent
    namespace) is not an absence. Contrast superseded_over, which only refuses non-terminal pods."""
    try:
        return next(_worker_pods(job, ns, region), None) is None
    except Exception:  # noqa: BLE001 - unreadable is never evidence
        return False


def known_zero(job: dict, records: list | None, ns: str | None, region: str | None) -> bool:
    """The settle-time predicate (api 0.11.121): nothing measurable (measured_usage None), NO attempt record from any
    source (database or bucket: an S3-only start without an end is an attempt, never a zero), and never_started on
    authoritative reads."""
    return not records and measured_usage(job, records) is None and never_started(job, ns, region)


def never_started(job: dict, ns: str | None, region: str | None) -> bool:
    """Provably zero GPU time (api 0.11.121), on authoritative reads only; anything unreadable, absent or ambiguous
    proves nothing and leaves `unknown` as it is:
    - a fenced predecessor: the successor annotation AND the durable successor mark agree (attempts.successor_of),
      and the successor Job exists and is a class fallback of this run (`serverless2.nebius/fallback`), its creation
      time being the fence;
    - never resumed by Kueue: suspended, no startTime, no active counter, no Complete/Failed condition, a manager Job;
    - stopped everywhere on successful reads (superseded_over) AND no pod of it in any phase in any worker
      (no_pods_anywhere: a terminal unrecorded pod is a run), and no succeeded/failed counter on the Job;
    - its own Workload still exists, exactly one, owned by this Job's UID, never admitted (no Admitted/QuotaReserved,
      no admission, no cluster) and explicitly Admitted=False since a time not later than the fence
      (kube.workload_admission; a missing, stray or duplicate Workload is not evidence);
    - zero attempt records of this operation in the fleet database, read successfully (the uploader records
      "started" within seconds of any pod); the bucket's best-effort read is never used as evidence.
    A predecessor superseded by RECOVERY ran and has records: it never qualifies."""
    md, st, sp = job.get("metadata") or {}, job.get("status") or {}, job.get("spec") or {}
    ann = md.get("annotations") or {}
    name, uid, successor = md.get("name"), md.get("uid"), ann.get(f"{LABEL}/successor")
    conds = {c.get("type") for c in (st.get("conditions") or []) if c.get("status") == "True"}
    if not (
        successor
        and successor != name
        and uid
        and ns
        and region
        and sp.get("suspend") is True
        and not st.get("startTime")
        and not st.get("active")
        and not ({"Complete", "SuccessCriteriaMet", "Failed", "FailureTarget"} & conds)
        and jobs.is_manager_job(job)
        and not job.get("_pods")
        and not st.get("succeeded")
        and not st.get("failed")
        and attempts.enabled()
        and superseded_over(job, ns, region)
        and no_pods_anywhere(job, ns, region)
    ):
        return False
    try:
        if attempts.successor_of(name) != successor:
            return False
        rows = [r for r in attempts.for_operation(ann.get(f"{LABEL}/resumed-from") or name) if r["operation"] == name]
        if rows:
            return False
        succ = jobs.get(ns, successor, region)
        s_ann = (succ.get("metadata") or {}).get("annotations") or {}
        fence = (succ.get("metadata") or {}).get("creationTimestamp")
        if not s_ann.get(f"{LABEL}/fallback") or not fence:
            return False
        w = kube.workload_admission(ns, uid, region)
    except Exception as e:  # noqa: BLE001 - an unreadable answer is never evidence of zero
        log.warning("billing %s/%s: never-started proof unavailable: %s", region, name, e)
        return False
    return bool(
        w is not None
        and not w["admitted"]
        and not w["cluster"]
        and w["not_admitted_since"]
        and str(w["not_admitted_since"]) <= str(fence)
    )


def unsettled(job: dict, ns: str | None = None, region: str | None = None) -> bool:
    """Whether a Job (or JobSet) is one the billing pass would settle now: admitted on a key, not billed yet, not a
    worker's transient copy of a manager Job, and over: finished (Complete/Failed), a cancelled JobSet, or a fenced
    predecessor whose Workload and Pods stopped (suspended Jobs never reach a terminal condition). One revisit of an
    already-billed Job: `unknown` on a predecessor that provably never started (never_started), so the known zero
    replaces the unknown; every other unknown stays untouched."""
    ann = (job.get("metadata") or {}).get("annotations") or {}
    if KEY not in ann or jobs.is_mirror(job):
        return False
    if BILLED in ann and (
        ann.get(BILLED) != "unknown" or KNOWN_ZERO in ann or not db.enabled() or not never_started(job, ns, region)
    ):
        return False
    return finished(job) or cancelled_over(job, ns, region) or superseded_over(job, ns, region)


def superseded_over(job: dict, ns: str | None, region: str | None) -> bool:
    """A fenced predecessor is over without a Kubernetes Complete/Failed condition.

    Recovery suspends it permanently. Settle its own measured attempts once Kueue
    is inactive and no copy has a live pod; never leave its logical slot reserved
    merely because the successor finished instead of this suspended Job.
    """
    md = job.get("metadata") or {}
    ann = md.get("annotations") or {}
    successor = ann.get(f"{LABEL}/successor")
    if not successor or successor == md.get("name") or (job.get("spec") or {}).get("suspend") is not True:
        return False
    if not ns or not region or not md.get("uid") or job.get("_pods_unknown"):
        return False
    try:
        if not attempts.enabled() or attempts.successor_of(md["name"]) != successor:
            return False  # the durable fence, not just an annotation, refuses any future predecessor publication
        if not kube.workload_inactive(ns, md["uid"], region):
            return False
        live = (p.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for p in _worker_pods(job, ns, region))
        return not any(live)
    except Exception as error:  # noqa: BLE001 - unavailable worker is not a confirmed stop
        log.warning("billing %s: predecessor stop unknown: %s", md.get("name"), error)
        return False


def cancelled_over(job: dict, ns: str | None, region: str | None) -> bool:
    """A cancelled JobSet (jobs.cancel: the Workload deactivated, the JobSet suspended, the pods deleted; a manager's
    worker copy removed by MultiKueue) never gets Complete or Failed, so `finished` never sees it and its key stayed
    reserved (the first MPI cancel, 2026-10-14). It is over for the settlement when its cancel is on record, it is
    suspended, and Kueue holds nothing of it any more (kube.workload_inactive on the cluster that holds the Workload:
    the manager, or the worker for a pinned run) and no pod of it is left where it ran. Usage then settles from the
    attempt records exactly as for a finished run (measured, or unknown when a rank has no end on record)."""
    md = job.get("metadata") or {}
    ann = md.get("annotations") or {}
    if not ann.get(f"{LABEL}/cancelled") or not jobs.is_jobset(job) or not (job.get("spec") or {}).get("suspend"):
        return False
    if ns is None or region is None or not md.get("uid"):
        return False
    if job.get("_pods"):
        return False
    try:
        return kube.workload_inactive(ns, md["uid"], region)
    except Exception as e:  # noqa: BLE001 - unknown is not over
        log.warning("billing %s: workload state unknown: %s", md.get("name"), e)
        return False


class _SettlementLock:
    """One settlement per operation at a time across API replicas and the CronJob: a Postgres transaction-scoped
    advisory lock (`pg_try_advisory_xact_lock`: a second settler skips instead of waiting) on a DEDICATED short-lived
    connection, never one of the pool's: the settlement behind it takes pool connections of its own (the ledger, the
    attempt records, the charge), and a lock holding a pool slot would starve them under concurrent settlements
    (max_size 4). Every step behind the lock is idempotent anyway (the `charges` receipt is unique per operation, the
    ledger release conditional, the annotation a patch): the lock only avoids duplicate work. Without the database,
    or when the connection cannot be made, the settlement proceeds unlocked on that idempotency."""

    def __init__(self, operation: str):
        self.operation, self._conn, self.held, self.locked = operation, None, False, False

    def __enter__(self):
        if not db.enabled():
            return self
        try:
            self._conn = psycopg.connect(db.DATABASE_URL, autocommit=False, connect_timeout=5)
            self.held = bool(
                self._conn.execute(
                    "select pg_try_advisory_xact_lock(hashtextextended(%s, 5))", (self.operation,)
                ).fetchone()[0]
            )
            self.locked = True
        except Exception as e:  # noqa: BLE001 - no lock: the idempotent steps still settle once
            log.warning("settlement lock of %s unavailable (%s); settling unlocked", self.operation, e)
            self._release()
            self.locked = False
        return self

    def _release(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            with suppress(Exception):
                conn.rollback()  # ends the transaction: the advisory lock goes with it
            with suppress(Exception):
                conn.close()

    def __exit__(self, *exc):
        self._release()
        return False

    @property
    def skip(self) -> bool:
        """True when another settler holds the operation right now."""
        return self.locked and not self.held


async def settle_job(region: str, ns: str, job: dict, cache: dict | None = None) -> bool:
    """Settle one finished, unbilled Job or JobSet (the billing pass for one operation; `cache` shares the
    namespace's pods and Workload placements between the calls of one pass). Returns True when it settled now.
    Called by the CronJob's pass and by the owner's read of a finished operation (app.get_operation): the same
    semantics in both, once per operation (the lock, then the `billed` annotation re-read from the API server under
    it): a run's measured GPU seconds from its attempt records (nothing trustworthy -> `unknown`, the reservation
    kept), an endpoint call's join with its native execution (deferred while a worker is attached)."""
    cache = cache if cache is not None else {}
    name = job["metadata"]["name"]
    if not unsettled(job, ns, region):
        return False
    with _SettlementLock(name) as lock:
        if lock.skip:
            return False
        if lock.held:
            # under the lock: the authoritative object (another settler may have finished a moment ago)
            try:
                fresh = jobs.get(ns, name, region)
            except Exception as e:  # noqa: BLE001 - unreadable now: settled by a later pass
                log.warning("billing %s/%s: re-read failed: %s", region, name, e)
                return False
            if not unsettled(fresh, ns, region):
                return False
            fresh.pop("_pods", None)
            job = {**job, "metadata": fresh["metadata"], "status": fresh["status"]}
        if job["metadata"].get("labels", {}).get(f"{LABEL}/mode") == "async":
            return _settle_call(region, ns, job)
        return await _settle_run(region, ns, job, cache)


def _settle_call(region: str, ns: str, job: dict) -> bool:
    """An endpoint call's Job: the key's slot and request were reserved at admission (docs/API.md "Keys"); the
    endpoint's GPU time is not metered per call, so nothing is charged here. The release is a JOIN with the worker's
    native execution (docs/API.md "Native execution leases"): while the worker is attached nothing is freed, whatever
    the caller did."""
    ann, name = job["metadata"].get("annotations") or {}, job["metadata"]["name"]
    try:
        value, how = (0.0, "transfer only") if ann.get(f"{LABEL}/export-of") else (None, "no ledger")
        if db.enabled():
            m = catalog.get(job["metadata"]["labels"].get(f"{LABEL}/model", "")) or {}
            settle_now, value, how = leases.settle_decision(name, leases.mode_of(m) == "required")
            if not settle_now:
                log.info("billing %s/%s: deferred, %s", region, name, how)
                return False
            # Only the platform's transfer-only retry is known to execute no inference. An optional
            # worker without a lease measurement is unknown, not the CPU caller's zero allocation.
            if ann.get(f"{LABEL}/export-of") and how in {"unattached", "worker usage unmeasured"}:
                value, how = 0.0, "transfer only"
            keys.release(name, value)
            leases.mark_settled(name)
        seconds = "unknown" if value is None else f"{value}"
        done = {
            BILLED: "0" if value is not None else "unknown",
            f"{LABEL}/gpu-seconds": seconds,
            f"{LABEL}/native": how,
        }
        _annotate(region, ns, name, done, job)
        if f"{LABEL}/execution" in ann:
            execution.sweep_copies(ns, name)
        return True
    except Exception as e:  # noqa: BLE001 - retried on the next run
        log.warning("settlement of endpoint call %s failed: %s", name, e)
        return False


async def _settle_run(region: str, ns: str, job: dict, cache: dict) -> bool:
    """A run's measured GPU seconds from its attempt records: charged to the key, the reservation released; nothing
    trustworthy measured -> `unknown`, the reservation kept (keys.settle_run_member None) for keys.settle_unknown."""
    ann, name = job["metadata"].get("annotations") or {}, job["metadata"]["name"]
    price_region = region
    if jobs.is_manager_job(job):
        if cache.get("clusters") is None:
            cache["clusters"] = kube.workload_clusters(ns)
        cluster = cache["clusters"].get(job["metadata"].get("uid", ""))
        price_region = kube.cluster_region(cluster) if cluster else None
        job["_pods"] = []  # the worker's pods are gone with its copy: the uploader's records are the attempts
    else:
        if cache.get("pods") is None:
            raw = retry(kube.core(region).list_namespaced_pod, ns, label_selector=f"{LABEL}/mode=run").items
            cache["pods"] = [kube.core(region).api_client.sanitize_for_serialization(p) for p in raw]
        job["_pods"] = [
            p
            for p in cache["pods"]
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
        zero = known_zero(job, records, ns, region)
        if zero:
            measured = 0.0  # nothing ever ran (known_zero): a known zero, written next to the bill
        if measured is None:
            if db.enabled():
                keys.release(name, None)
            _annotate(region, ns, name, {BILLED: "unknown", f"{LABEL}/gpu-seconds": "unknown"}, job)
            log.warning("billing %s/%s: GPU time not measured; reservation kept as unknown", region, name)
            return True
        model = catalog.get(job["metadata"]["labels"].get(f"{LABEL}/model", ""))
        gpu_s, usd = cost_of(job, model, records, price_region)
        total = await add_spend(ann[KEY], usd, f"{region}/{identity}", gpu_s)
        if db.enabled():
            released = keys.release(name, gpu_s)  # the key's slot and its GPU-second reservation (keys.admit)
            if zero and not released:
                keys.settle_unknown(name, 0.0)  # a member an earlier pass stored as unknown: settled at 0
        done = {BILLED: f"{usd}", f"{LABEL}/gpu-seconds": f"{gpu_s}"}
        if zero:
            done[KNOWN_ZERO] = "known zero: fenced before admission, never admitted, no attempt record"
        _annotate(region, ns, name, done, job)
        if f"{LABEL}/execution" in ann:
            execution.sweep_copies(ns, name)  # this attempt only: its successor may still be running
        log.info("billed %s/%s: %.0f GPU-s -> $%s (key spend now %s)", region, name, gpu_s, usd, total)
        return True
    except Exception as e:  # noqa: BLE001 - unbilled, retried on the next run
        log.warning("billing %s failed: %s", name, e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    billed = asyncio.run(bill_once())
    log.info("billed %d run(s)", billed)
