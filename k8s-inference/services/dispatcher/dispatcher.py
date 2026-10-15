"""Cost-aware MultiKueue dispatcher for Serverless 2.0 (Kueue external dispatcher mode).

Runs on the MultiKueue manager (the control cluster). For every Workload that holds a QuotaReservation and
still waits for the MultiKueue AdmissionCheck it nominates worker clusters (`status.nominatedClusterNames`):
  1. candidates = (region id, pool) pairs from the fleet-prices ConfigMap (charts/fleet renders it from
     fleet.yaml) whose GPU class the workload's profile allows; the profile is the manager ClusterQueue the
     workload sits in: `prefer-<class>` = that class first (reserved, on-demand, spot), then the other classes
     by price; `default` = cheapest pool of any class. The pod template's annotations narrow the candidates
     further: `serverless2.nebius/gpu-classes` (what the model's image runs on) and `serverless2.nebius/regions`
     (where the catalog entry is deployed);
  2. a `serverless2.nebius/region` label on the Workload (region id or region name) pins the candidates to
     that cluster (resume in the same region, an explicit region on the request);
  3. a candidate is "free" when the worker's ClusterQueue `default` has enough unused nominal GPU quota in
     the pool's flavor (read through the MultiKueueCluster kubeconfig Secrets);
  4. spot pools cost the live price from the spot-prices ConfigMap (price_feed.py) when present, else the
     fleet list price; reserved pools cost their marginal price (0);
  5. the best free candidate's cluster is nominated; with nothing free the best cluster is nominated anyway
     (the job queues there); after RENOMINATE_AFTER_S without admission the next cluster is appended.
When a worker has ADMITTED a run with a work volume (manager Workload `status.clusterName`; a PVC claim in the
pod template, size from the template annotation `serverless2.nebius/pvc-size-gi`), the volume is created in
that worker (the worker's pod waits for it for at most one reconcile), labelled `serverless2.nebius/managed-by:
dispatcher` with the manager Jobs that use it in an annotation (a resume adds its Job). Nominations create
nothing: a run may be re-nominated to another cluster while it waits. The volume has no owner on purpose:
MultiKueue deletes the worker's copy of a Job the moment the manager's Job finishes, and a failed run must
keep its checkpoints for `:resume`. The uploader deletes the volume after a successful upload; the sweeper
here deletes it once none of its Jobs exists on the manager any more (the Job's TTL, a cancel of a queued
run; after ORPHAN_AFTER_S), or when every Job that uses it was admitted in another cluster.
Admission is read on the WORKERS: MultiKueue creates the remote Workload under the same name in every
nominated cluster, and the one whose Workload carries `Admitted: True` runs the Job (the manager's own `status.clusterName` / admission check never turn while the external dispatcher is in
charge, and `clusterName` is cleared for finished runs anyway). The admitting cluster is written onto the
manager Workload (`serverless2.nebius/admitted-cluster`), once, for the API (region of the operation,
price, where a resume must go) and for this dispatcher's own bookkeeping.
Patches use the `kueue-admission` field manager (Kueue MultiKueue docs, external dispatcher caveat).
The API reproduces the order of `rank()` from the fleet document alone (services/api/placement.py) when it has to
pick a run's GPU class at submission (docs/SCHEDULING.md "Per-GPU images for runs"); keep the two in step.
"""

import base64
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import yaml
from kubernetes import client, config

import infiniband
import nebius_cli

log = logging.getLogger("dispatcher")
GROUP, VERSION = "kueue.x-k8s.io", "v1beta2"
NS = os.environ.get("PRICES_NAMESPACE", "kueue-system")
FLEET_CM = os.environ.get("FLEET_CONFIGMAP", "fleet-prices")  # charts/fleet: pools.yaml
SPOT_CM = os.environ.get("SPOT_CONFIGMAP", "spot-prices")  # price_feed.py: spot.json
INTERVAL = int(os.environ.get("INTERVAL_S", "15"))
INFINIBAND = os.environ.get("INFINIBAND", "1") == "1"  # InfiniBand pool floors for multi-node runs (infiniband.py)
RENOMINATE_AFTER_S = int(os.environ.get("RENOMINATE_AFTER_S", "300"))
ORPHAN_AFTER_S = int(os.environ.get("ORPHAN_AFTER_S", "900"))
REQUEST_TIMEOUT_S = int(
    os.environ.get("REQUEST_TIMEOUT_S", "30")
)  # every Kubernetes API call (a hung worker API server never stalls the loop)   # grace before a volume whose manager Jobs are gone is deleted
LABEL = "serverless2.nebius"
REGION_LABEL = f"{LABEL}/region"
CLASSES_ANN = f"{LABEL}/gpu-classes"
REGIONS_ANN = f"{LABEL}/regions"
PVC_SIZE_ANN = f"{LABEL}/pvc-size-gi"
NOMINATED_AT = f"{LABEL}/nominated-at"
ADMITTED_ANN = f"{LABEL}/admitted-cluster"  # on the manager Workload: the worker that admitted it (status.clusterName is cleared when the worker's copy is gone)


KEPT_STATUS = ("admission", "admissionChecks")  # status fields Kueue writes as `kueue-admission` too (see nominate)


def nominate(custom, ns, name, want, status=None):
    """Write status.nominatedClusterNames with SERVER-SIDE APPLY as field manager `kueue-admission`.

    Kueue admits a MultiKueue workload by setting status.clusterName and clearing nominatedClusterNames in one
    server-side apply as `kueue-admission`; a value written with a merge patch (an "Update" managedFields entry,
    even under the same manager name) is not removed by that apply and the API server then rejects Kueue's patch
    ("clusterName and nominatedClusterNames are mutually exclusive"): the manager Workload stays Pending forever
    (Kueue 0.20). docs: kueue.sigs.k8s.io/docs/concepts/multikueue "Workload Dispatching".

    The same manager name owns Kueue's quota reservation (`status.admission`) and the admission-check states
    (`status.admissionChecks`): an apply that names only nominatedClusterNames drops them (the server treats
    the omitted fields as released by this manager), the manager scheduler re-admits the workload and, after a
    retry, the API server rejects the apply ("admissionChecks[0].state: Required value").
    The apply therefore restates those fields exactly as read.
    """
    keep = {k: status[k] for k in KEPT_STATUS if status and status.get(k)}
    custom.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        ns,
        "workloads",
        name,
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "Workload",
            "metadata": {"name": name, "namespace": ns},
            "status": {"nominatedClusterNames": want, **keep},
        },
        field_manager="kueue-admission",
        force=True,
        _content_type="application/apply-patch+yaml",
    )


PVC_JOBS_ANN = f"{LABEL}/jobs"  # on a worker PVC: Jobs that use it (owners once they exist)
MANAGED_LABEL = f"{LABEL}/managed-by"
SUCCESSOR_ANN = (
    f"{LABEL}/successor"  # on a manager Job the platform superseded (services/api/jobs.py switch_class, recover)
)
CAP_RANK = {"reserved": 0, "on_demand": 1, "ondemand": 1, "spot": 2}  # fleet.yaml capacity_order


# ---------- pure functions (unit-tested) ----------
def gpu_request(wl: dict) -> int:
    """GPUs requested by the workload: sum over pod sets of count * nvidia.com/gpu per pod."""
    total = 0
    for ps in wl.get("spec", {}).get("podSets", []):
        per_pod = 0
        for c in ps.get("template", {}).get("spec", {}).get("containers", []):
            per_pod += int((c.get("resources", {}).get("requests") or {}).get("nvidia.com/gpu", 0))
        total += per_pod * int(ps.get("count", 1))
    return total


def clusters_from_pools(pools: dict) -> dict:
    """fleet-prices pools.yaml ({"<region id>-<pool>": {region, pool, gpu_class, capacity, usd_per_gpu_hour, ...}})
    -> {region id: {"pools": {pool: {"class", "capacity", "price", ...}}}}."""
    out = {}
    for _, p in (pools or {}).items():
        out.setdefault(p["region"], {"pools": {}})["pools"][p["pool"]] = {
            **p,
            "class": p.get("gpu_class"),
            "price": float(p.get("usd_per_gpu_hour", 1e9)),
        }
    return out


def price_of(pool: dict, spot_prices: dict, key: str) -> float:
    cap = pool.get("capacity", "spot")
    if cap == "reserved":
        return float(pool.get("price", 0.0))
    live = spot_prices.get(key) if cap == "spot" else None
    return float(live if live is not None else pool.get("price", 1e9))


def profile_from_name(name: str | None, clusters: dict, spot_prices: dict, tenant: str | None = None) -> dict:
    """Manager ClusterQueue name -> {"classes": [...in preference order], "strategy"}. A tenant's queue is named
    <tenant>-<profile> (charts/fleet); with the tenant known the prefix is stripped, then prefer-<class> means that
    class first and the others by their cheapest pool; anything else: cheapest of all."""
    if name and tenant and name.startswith(f"{tenant}-"):
        name = name[len(tenant) + 1 :]
    cheapest = {}
    for cname, cl in clusters.items():
        for fname, pool in (cl.get("pools") or {}).items():
            c = pool.get("class")
            pr = price_of(pool, spot_prices, f"{cname}/{fname}")
            if c and (c not in cheapest or pr < cheapest[c]):
                cheapest[c] = pr
    by_price = sorted(cheapest, key=lambda c: (cheapest[c], c))
    if name and name.startswith("prefer-") and name[7:] in cheapest:
        pref = name[7:]
        return {"classes": [pref] + [c for c in by_price if c != pref], "strategy": "preferred"}
    return {"classes": by_price, "strategy": "cheapest"}


def rank(
    profile: dict,
    clusters: dict,
    free: dict,
    gpus: int,
    spot_prices: dict,
    pin: str | None = None,
    allowed_classes: list | None = None,
    allowed_clusters: list | None = None,
    tenant: str | None = None,
) -> list:
    """Candidates best-first as (cluster, flavor, price, is_free).

    profile:  {"classes": [preferred, ...], "strategy": "preferred" | "cheapest"}
    clusters: {cluster: {"pools": {flavor: {"class", "capacity": reserved|on_demand|spot, "price", "tenant"}}}}
    free:     {(cluster, flavor): free_gpus}
    allowed_classes / allowed_clusters: the model's GPU classes and deployment regions (None = no restriction)
    tenant: the run's tenant; a pool dedicated to another tenant (fleet tenants.<name>.dedicated_pools) is never a candidate
    """
    classes = [c for c in profile.get("classes", []) if not allowed_classes or c in allowed_classes]
    strategy = profile.get("strategy", "preferred")
    cands = []
    for cname, cl in clusters.items():
        if pin and cname != pin:
            continue
        if allowed_clusters is not None and cname not in allowed_clusters:
            continue
        for fname, pool in (cl.get("pools") or {}).items():
            if pool.get("tenant") and pool.get("tenant") != tenant:
                continue
            if pool.get("class") not in classes:
                continue
            price = price_of(pool, spot_prices, f"{cname}/{fname}")
            is_free = free.get((cname, fname), 0) >= max(gpus, 1)
            cands.append((cname, fname, price, is_free))

    def key(c):
        cname, fname, price, is_free = c
        pool = clusters[cname]["pools"][fname]
        cap = CAP_RANK.get(pool.get("capacity", "spot"), 3)
        # what the run costs per hour: its GPUs on a node that is up with room, else the whole node the pool
        # would boot (a 1-GPU run on an 8-GPU pool pays eight GPUs: it boots a full node)
        node = int(pool.get("gpus_per_node") or 1)
        eff = price * (max(gpus, 1) if is_free else max(max(gpus, 1), node))
        if (
            strategy == "preferred"
        ):  # class preference, then reserved -> on-demand -> spot, then cost (mirrored in services/api/placement.py)
            return (0 if is_free else 1, classes.index(pool["class"]), cap, eff, cname, fname)
        return (0 if is_free else 1, eff, cap, cname, fname)

    return sorted(cands, key=key)


def nominations(ranked: list, already: list, age_s: float) -> list:
    """Clusters to nominate: the best one, plus the next one every RENOMINATE_AFTER_S while not admitted."""
    order = []
    for cname, *_ in ranked:
        if cname not in order:
            order.append(cname)
    if not order:
        return list(already)
    n = 1 + int(age_s // RENOMINATE_AFTER_S) if already else 1
    want = order[: max(n, len(already))]
    for a in already:  # never drop a cluster that was already nominated
        if a not in want:
            want.append(a)
    return want


def pending_multikueue(wl: dict) -> bool:
    st = wl.get("status", {})
    conds = st.get("conditions", [])
    if st.get("clusterName") or any(c["type"] == "Finished" and c["status"] == "True" for c in conds):
        return False
    if not any(c["type"] == "QuotaReserved" and c["status"] == "True" for c in conds):
        return False
    return any(ac.get("state") in ("Pending", "Retry") for ac in st.get("admissionChecks", []))


def template_of(wl: dict) -> dict:
    """The first pod set's template (a run is one pod)."""
    ps = wl.get("spec", {}).get("podSets") or [{}]
    return ps[0].get("template") or {}


def allowed_of(wl: dict) -> tuple[list | None, list | None]:
    """(gpu classes, region names) the pod template allows; None = unrestricted."""
    ann = (template_of(wl).get("metadata") or {}).get("annotations") or {}
    classes = [c for c in (ann.get(CLASSES_ANN) or "").split(",") if c] or None
    regions = [r for r in (ann.get(REGIONS_ANN) or "").split(",") if r] or None
    return classes, regions


WORK_VOLUME = "work"  # the run's own scratch volume (services/api/jobs.py); the only claim the dispatcher owns


def pvc_of(template: dict) -> str | None:
    """The claim behind the pod's `work` volume, None when it is not a claim (multi-node runs scratch on an emptyDir).
    Other claims of the pod are never the dispatcher's: a multi-node run with `checkpoints: shared` mounts the
    tenant's `scratch-shared` claim (charts/tenant), which the sweeper deleted as an "orphan" when it was taken for
    the work volume (docs/VERIFICATION.md)."""
    for v in (template.get("spec") or {}).get("volumes") or []:
        if v.get("name") == WORK_VOLUME and v.get("persistentVolumeClaim", {}).get("claimName"):
            return v["persistentVolumeClaim"]["claimName"]
    return None


def finished(wl: dict) -> bool:
    return any(c["type"] == "Finished" and c["status"] == "True" for c in wl.get("status", {}).get("conditions", []))


def is_admitted(wl: dict) -> bool:
    return any(c["type"] == "Admitted" and c["status"] == "True" for c in wl.get("status", {}).get("conditions", []))


def admitted_cluster(wl: dict, remote: dict | None = None) -> str | None:
    """The worker that admitted the run: the manager's status.clusterName when Kueue sets it, else the record on
    the Workload, else the nominated worker whose remote Workload (same name) is Admitted (`remote`:
    cluster -> remote Workload dict, looked up by the caller)."""
    st = wl.get("status", {})
    rec = st.get("clusterName") or (wl["metadata"].get("annotations") or {}).get(ADMITTED_ANN)
    if rec:
        return rec
    for cname in st.get("nominatedClusterNames") or []:
        if remote and is_admitted(remote.get(cname) or {}):
            return cname
    return None


def remote_workloads(workers: dict, wl: dict) -> dict:
    """cluster -> the remote copy of this manager Workload in each nominated worker (absent ones skipped)."""
    out = {}
    ns, name = wl["metadata"]["namespace"], wl["metadata"]["name"]
    for cname in wl.get("status", {}).get("nominatedClusterNames") or []:
        w = workers.get(cname)
        if not w:
            continue
        try:
            out[cname] = w["custom"].get_namespaced_custom_object(GROUP, VERSION, ns, "workloads", name)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                log.warning("worker %s: workload %s/%s: %s", cname, ns, name, e.reason)
    return out


def admitted_volume(wl: dict, cname: str | None = None) -> tuple | None:
    """(cluster, namespace, claim, size Gi, manager job) when a worker admitted this unfinished run and its pod
    template claims a work volume; None while the run is only nominated, has no volume, or is finished."""
    cname, job_name = cname or wl.get("status", {}).get("clusterName"), owner_job(wl)
    tmpl = template_of(wl)
    claim = pvc_of(tmpl)
    if not cname or not job_name or not claim or finished(wl):
        return None
    size = ((tmpl.get("metadata") or {}).get("annotations") or {}).get(PVC_SIZE_ANN) or "50"
    return cname, wl["metadata"]["namespace"], claim, size, job_name


def volume_action(pvc_cluster: str, users: set, superseded: set, admitted: dict, ns: str, age_s: float) -> str | None:
    """What the sweeper does with a dispatcher-created volume. `users`: the manager Jobs that reference the claim and
    were not superseded (any state: an unfinished run, a pending or running resume or finalizer, a FAILED or
    EXPORT_FAILED handle whose volume `:resume`/`:export` may still use); `superseded`: the referencing Jobs the
    platform fenced and continued elsewhere (fallback, automatic recovery: the `successor` annotation). "orphan"
    when nothing references it any more (after the grace period), "superseded" when every Job of it that still
    exists was superseded (the chain moved to fresh volumes, the readable handles stay), "elsewhere" when every user
    was admitted in another cluster, None to keep it."""
    if not users:
        if superseded:
            return "superseded"
        return "orphan" if age_s >= ORPHAN_AFTER_S else None
    if all(admitted.get((ns, j)) not in (None, pvc_cluster) for j in users):
        return "elsewhere"
    return None


def manager_jobs(batch: client.BatchV1Api, ns: str, cache: dict) -> dict | None:
    """name -> manager Job of a tenant namespace, listed once per reconcile (`cache`); None when the manager could not
    be listed (unknown: callers preserve what exists)."""
    if ns in cache:
        return cache[ns]
    try:
        out = {j.metadata.name: j for j in batch.list_namespaced_job(ns).items}
    except Exception as e:  # noqa: BLE001 - unknown is a state of its own here
        log.warning("manager jobs %s: %s", ns, e)
        out = None
    cache[ns] = out
    return out


def is_superseded(job) -> bool:
    """A manager Job the platform superseded (fallback or automatic recovery, services/api/jobs.py): suspended, its
    Workload deactivated, a successor on a fresh volume; a readable handle that never runs again."""
    return bool(((job.metadata.annotations or {}) if job is not None else {}).get(SUCCESSOR_ANN))


def may_ensure_volume(job, wl: dict) -> bool:
    """Only a live manager Job authorizes its work claim. Workload Finished can lag Job completion, after the
    uploader has already released the claim. Unknown/deleted/replaced owners must not recreate it. This is a
    creation/adoption fence only: terminal failed/export-needed claims remain references for the sweeper."""
    if job is None or is_superseded(job) or getattr(job.metadata, "deletion_timestamp", None):
        return False
    owners = [o for o in wl["metadata"].get("ownerReferences") or [] if o.get("kind") in ("Job", "JobSet")]
    if len(owners) != 1 or owners[0].get("kind") != "Job" or owners[0].get("name") != job.metadata.name:
        return False
    if owners[0].get("uid") and owners[0]["uid"] != getattr(job.metadata, "uid", None):
        return False
    return not any(
        c.type in ("Complete", "Failed") and c.status == "True"
        for c in getattr(getattr(job, "status", None), "conditions", None) or []
    )


def job_claim(job) -> str | None:
    """The work claim a manager Job's pod template names (a resume or finalizer shares the original's)."""
    tmpl = getattr(getattr(job, "spec", None), "template", None)
    for v in getattr(getattr(tmpl, "spec", None), "volumes", None) or []:
        pvc = getattr(v, "persistent_volume_claim", None)
        if getattr(v, "name", None) == WORK_VOLUME and pvc is not None and getattr(pvc, "claim_name", None):
            return pvc.claim_name
    return None


def claim_references(wanted: list, idx: dict, claim: str) -> tuple[set, set]:
    """(users, superseded): the manager Jobs that reference a volume: those recorded on it (`wanted`, the admitted-
    volume pass) and every Job whose pod template claims it, recorded or not (a same-volume resume or finalizer the
    API created that the volume pass has not seen admitted yet). Recorded Jobs that are gone count for nothing."""
    users, superseded = set(), set()
    for name, job in idx.items():
        if name in wanted or job_claim(job) == claim:
            (superseded if is_superseded(job) else users).add(name)
    return users, superseded


def load_fleet(core: client.CoreV1Api):
    data = core.read_namespaced_config_map(FLEET_CM, NS).data or {}
    pools = yaml.safe_load(data.get("pools.yaml", "") or "") or {}
    try:
        spot = json.loads((core.read_namespaced_config_map(SPOT_CM, NS).data or {}).get("spot.json", "{}"))
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        spot = {}
    return clusters_from_pools(pools), spot


class Timed:
    """A Kubernetes API client whose every call carries `_request_timeout` (the dispatcher runs one thread: one
    worker API server that stops answering would otherwise stall every cluster's dispatch)."""

    def __init__(self, api, timeout_s: int = REQUEST_TIMEOUT_S):
        self._api, self._timeout = api, timeout_s

    def __getattr__(self, name: str):
        fn = getattr(self._api, name)
        if not callable(fn):
            return fn
        return lambda *a, **kw: fn(*a, **{"_request_timeout": self._timeout, **kw})


def worker_clients(core: client.CoreV1Api, custom: client.CustomObjectsApi):
    """Per MultiKueueCluster (from its kubeconfig Secret): {"custom", "core", "batch"} API clients,
    plus {region name: cluster id}."""
    out, aliases = {}, {}
    for mkc in custom.list_cluster_custom_object(GROUP, VERSION, "multikueueclusters").get("items", []):
        name = mkc["metadata"]["name"]
        region = (mkc["metadata"].get("labels") or {}).get(REGION_LABEL)
        if region:
            aliases[region] = name
        kc = mkc["spec"].get("kubeConfig") or mkc["spec"].get("clusterSource", {}).get("kubeConfig", {})
        if kc.get("locationType", "Secret") != "Secret":
            continue
        sec = core.read_namespaced_secret(kc["location"], NS)
        path = os.path.join(tempfile.gettempdir(), f"{name}.kubeconfig")
        with open(path, "wb") as f:
            f.write(base64.b64decode(sec.data.get("kubeconfig")))
        api = config.new_client_from_config(path)
        out[name] = {
            "custom": Timed(client.CustomObjectsApi(api)),
            "core": Timed(client.CoreV1Api(api)),
            "batch": Timed(client.BatchV1Api(api)),
        }
    return out, aliases


def free_quota(workers: dict) -> dict:
    """(cluster, flavor) -> free nvidia.com/gpu: the nominal quota of a flavor (the fleet's `default` queue lends the
    shared pools, a tenant queue holds its dedicated pool) minus the usage of EVERY queue of the cohort on that flavor
    (tenant queues borrow from the lender: their usage is what empties it)."""
    free = {}
    for cname, w in workers.items():
        try:
            cqs = w["custom"].list_cluster_custom_object(GROUP, VERSION, "clusterqueues").get("items", [])
        except Exception as e:  # unreachable worker: no free capacity there
            log.warning("worker %s unreachable: %s", cname, e)
            continue
        nominal, used = {}, {}
        for cq in cqs:
            for rg in cq.get("spec", {}).get("resourceGroups", []):
                for fl in rg.get("flavors", []):
                    for r in fl.get("resources", []):
                        if r["name"] == "nvidia.com/gpu":
                            nominal[fl["name"]] = nominal.get(fl["name"], 0) + int(r.get("nominalQuota", 0))
            for fl in cq.get("status", {}).get("flavorsUsage", []) or []:
                for r in fl.get("resources", []):
                    if r["name"] == "nvidia.com/gpu":
                        used[fl["name"]] = used.get(fl["name"], 0) + int(float(str(r.get("total", 0))))
        for fname, n in nominal.items():
            if n:
                free[(cname, fname)] = max(0, n - used.get(fname, 0))
    return free


def profile_of(custom: client.CustomObjectsApi, wl: dict):
    lq = wl["spec"].get("queueName")
    if not lq:
        return None
    obj = custom.get_namespaced_custom_object(GROUP, VERSION, wl["metadata"]["namespace"], "localqueues", lq)
    return obj["spec"]["clusterQueue"]


def owner_job(wl: dict) -> str | None:
    """The run that owns a Workload: a Job, or a JobSet for multi-node runs (docs/JOBS.md); same name = operation id."""
    for o in wl["metadata"].get("ownerReferences") or []:
        if o.get("kind") in ("Job", "JobSet"):
            return o["name"]
    return None


def ensure_pvc(w: dict, ns: str, claim: str, size_gi: str, job_name: str, batch, wl: dict) -> None:
    """The run's work volume in the chosen worker; `job_name` is recorded so the adoption pass can make the
    worker's Job its owner. Idempotent (a resume reuses the original's volume)."""
    core = w["core"]
    try:
        cur = core.read_namespaced_persistent_volume_claim(claim, ns)
        jobs = [j for j in ((cur.metadata.annotations or {}).get(PVC_JOBS_ANN) or "").split(",") if j]
        if job_name not in jobs:
            core.patch_namespaced_persistent_volume_claim(
                claim,
                ns,
                {
                    "metadata": {
                        "annotations": {PVC_JOBS_ANN: ",".join(jobs + [job_name])},
                        "labels": {MANAGED_LABEL: "dispatcher"},
                    }
                },
            )
        return
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
    # The namespace list can predate completion during this reconcile. Re-read only for a missing claim, as close
    # to creation as possible; a timeout/404 is unknown, never permission to resurrect a released work volume.
    try:
        job = batch.read_namespaced_job(job_name, ns)
    except Exception as e:  # noqa: BLE001 - fail closed without interrupting the rest of the reconcile
        log.warning("manager job %s/%s unavailable before volume creation: %s", ns, job_name, type(e).__name__)
        return
    if not may_ensure_volume(job, wl):
        return
    body = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": claim,
            "labels": {MANAGED_LABEL: "dispatcher", f"{LABEL}/operation": job_name},
            "annotations": {PVC_JOBS_ANN: job_name},
        },
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": f"{size_gi}Gi"}}},
    }
    try:
        core.create_namespaced_persistent_volume_claim(ns, body)
        log.info("created %s/%s (%s Gi) for %s", ns, claim, size_gi, job_name)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise


def sweep_volumes(workers: dict, batch: client.BatchV1Api, admitted: dict, jobs_cache: dict | None = None) -> None:
    """Worker PVCs the dispatcher created: delete one once nothing on the manager references it any more (older than
    ORPHAN_AFTER_S), once every Job that still references it was superseded by a continuation on a fresh volume, or
    when every Job that uses it was admitted in another cluster (`admitted`: (namespace, manager job) -> cluster).
    References are the actual manager Jobs (template claim or recorded on the volume), never the record alone; a
    namespace whose Jobs cannot be listed is left alone. Deletion completes only once no pod mounts the claim
    (Kubernetes' pvc-protection finalizer)."""
    jobs_cache = {} if jobs_cache is None else jobs_cache
    for cname, w in workers.items():
        try:
            pvcs = (
                w["core"]
                .list_persistent_volume_claim_for_all_namespaces(label_selector=f"{MANAGED_LABEL}=dispatcher")
                .items
            )
        except Exception as e:
            log.warning("worker %s: list volumes: %s", cname, e)
            continue
        for pvc in pvcs:
            ns, name = pvc.metadata.namespace, pvc.metadata.name
            if not name.endswith(f"-{WORK_VOLUME}"):
                continue  # not a work volume the dispatcher created: never swept, whatever its labels say
            idx = manager_jobs(batch, ns, jobs_cache)
            if idx is None:
                continue  # unknown: keep
            age = (datetime.now(timezone.utc) - pvc.metadata.creation_timestamp).total_seconds()
            wanted = [j for j in ((pvc.metadata.annotations or {}).get(PVC_JOBS_ANN) or "").split(",") if j]
            users, superseded = claim_references(wanted, idx, name)
            action = volume_action(cname, users, superseded, admitted, ns, age)
            if action:
                w["core"].delete_namespaced_persistent_volume_claim(name, ns)
                log.info(
                    "worker %s: deleted volume %s/%s (%s; recorded %s, users %s, superseded %s, admitted %s)",
                    cname,
                    ns,
                    name,
                    action,
                    wanted,
                    sorted(users),
                    sorted(superseded),
                    {j: admitted.get((ns, j)) for j in sorted(users | superseded)},
                )


def reconcile(core, custom, batch):
    clusters, spot = load_fleet(core)
    workers, aliases = worker_clients(core, custom)
    free = free_quota(workers)
    if INFINIBAND:
        try:  # its own failures (a worker down, the CLI) must not stop the dispatch below
            infiniband.reconcile(core, workers, clusters, aliases)
        except Exception as e:  # noqa: BLE001
            log.exception("infiniband pools: %s", e)
    workloads = custom.list_cluster_custom_object(GROUP, VERSION, "workloads").get("items", [])
    for wl in workloads:
        if not pending_multikueue(wl):
            continue
        meta, st = wl["metadata"], wl.get("status", {})
        ns, job_name = meta["namespace"], owner_job(wl)
        tenant = ns[len("tenant-") :] if ns.startswith("tenant-") else None
        prof = profile_from_name(profile_of(custom, wl), clusters, spot, tenant)
        pin = (meta.get("labels") or {}).get(REGION_LABEL)
        pin = aliases.get(pin, pin)
        classes, regions = allowed_of(wl)
        allowed_clusters = [aliases.get(r, r) for r in regions] if regions else None
        already = st.get("nominatedClusterNames") or []
        stamp = (meta.get("annotations") or {}).get(NOMINATED_AT)
        age = (time.time() - datetime.fromisoformat(stamp).timestamp()) if stamp else 0.0
        gpus = gpu_request(wl)
        ranked = rank(prof, clusters, free, gpus, spot, pin, classes, allowed_clusters, tenant)
        if not ranked:
            log.warning(
                "%s/%s: no candidate cluster (profile=%s pin=%s classes=%s regions=%s)",
                ns,
                meta["name"],
                prof,
                pin,
                classes,
                regions,
            )
            continue
        want = nominations(ranked, already, age)
        if want and want != already:
            log.info(
                "%s/%s profile=%s pin=%s classes=%s gpus=%d -> %s best=%s",
                ns,
                meta["name"],
                prof,
                pin,
                classes,
                gpus,
                want,
                ranked[:1],
            )
            nominate(custom, ns, meta["name"], want, st)
            custom.patch_namespaced_custom_object(
                GROUP,
                VERSION,
                ns,
                "workloads",
                meta["name"],
                {"metadata": {"annotations": {NOMINATED_AT: datetime.now(timezone.utc).isoformat()}}},
            )
        if ranked and ranked[0][3]:  # account for the capacity this workload will take
            cname, fname = ranked[0][0], ranked[0][1]
            free[(cname, fname)] = free.get((cname, fname), 0) - max(gpus, 1)
    # work volumes: exactly one, in the cluster that admitted the run
    admitted = {}
    jobs_cache: dict = {}
    for wl in workloads:
        meta = wl["metadata"]
        recorded = (meta.get("annotations") or {}).get(ADMITTED_ANN)
        cname = admitted_cluster(wl, None if (recorded or finished(wl)) else remote_workloads(workers, wl))
        job_name = owner_job(wl)
        if cname and job_name:
            admitted[(meta["namespace"], job_name)] = cname
        if cname and recorded != cname:
            custom.patch_namespaced_custom_object(
                GROUP,
                VERSION,
                meta["namespace"],
                "workloads",
                meta["name"],
                {"metadata": {"annotations": {ADMITTED_ANN: cname}}},
            )
            log.info("%s/%s admitted in %s", meta["namespace"], meta["name"], cname)
        vol = admitted_volume(wl, cname)
        if vol and vol[0] in workers:
            cname, ns, claim, size, job_name = vol
            # Workload Finished can lag a terminal Job. Missing/unreadable owners cannot authorize a new claim.
            idx = manager_jobs(batch, ns, jobs_cache)
            if not may_ensure_volume(idx.get(job_name) if idx is not None else None, wl):
                continue
            ensure_pvc(workers[cname], ns, claim, size, job_name, batch, wl)
    sweep_volumes(workers, batch, admitted, jobs_cache)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core, custom, batch = Timed(client.CoreV1Api()), Timed(client.CustomObjectsApi()), Timed(client.BatchV1Api())
    if INFINIBAND:
        nebius_cli.login()  # one CLI profile per mounted region key (the node-group updates of infiniband.py)
    # Reuse the Kubernetes client's resource lock; only the elected replica mutates workloads/PVCs (the other one
    # waits as a standby). A replica losing renewal exits immediately, fencing its reconcile loop before another
    # leader starts.
    leading = threading.Event()
    from kubernetes.leaderelection import electionconfig, leaderelection
    from kubernetes.leaderelection.resourcelock.configmaplock import ConfigMapLock

    lock = ConfigMapLock("dispatcher-leader", NS, os.environ.get("HOSTNAME", "dispatcher"))
    election = leaderelection.LeaderElection(electionconfig.Config(lock, 60, 30, 5, leading.set, lambda: os._exit(1)))

    def elect():
        try:
            election.run()
        finally:
            os._exit(1)

    threading.Thread(target=elect, daemon=True).start()
    while True:
        if leading.is_set():
            try:
                reconcile(core, custom, batch)
            except Exception as e:
                log.exception("reconcile failed: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
