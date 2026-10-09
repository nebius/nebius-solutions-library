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
nominated cluster, and the one whose Workload carries `Admitted: True` runs the Job (measured 2026-10-07:
the manager's own `status.clusterName` / admission check never turn while the external dispatcher is in
charge, and `clusterName` is cleared for finished runs anyway). The admitting cluster is written onto the
manager Workload (`serverless2.nebius/admitted-cluster`), once, for the API (region of the operation,
price, where a resume must go) and for this dispatcher's own bookkeeping.
Patches use the `kueue-admission` field manager (Kueue MultiKueue docs, external dispatcher caveat).
The same ranking is served over HTTP (`GET /v1/rank?profile=&gpus=&classes=&regions=&pin=`, port RANK_PORT) from
the last reconcile's snapshot: the API asks it at submission for a run class with per-GPU-class images, renders
the Job with the best class's image and queues it on that class (docs/SCHEDULING.md "Per-GPU images for runs").
"""
import base64
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml
from kubernetes import client, config

log = logging.getLogger("dispatcher")
GROUP, VERSION = "kueue.x-k8s.io", "v1beta2"
NS = os.environ.get("PRICES_NAMESPACE", "kueue-system")
FLEET_CM = os.environ.get("FLEET_CONFIGMAP", "fleet-prices")   # charts/fleet: pools.yaml
SPOT_CM = os.environ.get("SPOT_CONFIGMAP", "spot-prices")      # price_feed.py: spot.json
INTERVAL = int(os.environ.get("INTERVAL_S", "15"))
RENOMINATE_AFTER_S = int(os.environ.get("RENOMINATE_AFTER_S", "300"))
ORPHAN_AFTER_S = int(os.environ.get("ORPHAN_AFTER_S", "900"))   # grace before a volume whose manager Jobs are gone is deleted
RANK_PORT = int(os.environ.get("RANK_PORT", "8080"))              # GET /v1/rank (the API's class choice at submission), /healthz
DISPATCH = os.environ.get("DISPATCH", "true").lower() != "false"  # false: only refresh the ranking snapshot (a second, read-only copy)
LABEL = "serverless2.nebius"
REGION_LABEL = f"{LABEL}/region"
CLASSES_ANN = f"{LABEL}/gpu-classes"
REGIONS_ANN = f"{LABEL}/regions"
PVC_SIZE_ANN = f"{LABEL}/pvc-size-gi"
NOMINATED_AT = f"{LABEL}/nominated-at"
ADMITTED_ANN = f"{LABEL}/admitted-cluster"   # on the manager Workload: the worker that admitted it (status.clusterName is cleared when the worker's copy is gone)


KEPT_STATUS = ("admission", "admissionChecks")   # status fields Kueue writes as `kueue-admission` too (see nominate)


def nominate(custom, ns, name, want, status=None):
    """Write status.nominatedClusterNames with SERVER-SIDE APPLY as field manager `kueue-admission`.

    Kueue admits a MultiKueue workload by setting status.clusterName and clearing nominatedClusterNames in one
    server-side apply as `kueue-admission`; a value written with a merge patch (an "Update" managedFields entry,
    even under the same manager name) is not removed by that apply and the API server then rejects Kueue's patch
    ("clusterName and nominatedClusterNames are mutually exclusive"): the manager Workload stays Pending forever
    (measured 2026-10-07, Kueue 0.20). docs: kueue.sigs.k8s.io/docs/concepts/multikueue "Workload Dispatching".

    The same manager name owns Kueue's quota reservation (`status.admission`) and the admission-check states
    (`status.admissionChecks`): an apply that names only nominatedClusterNames drops them (the server treats
    the omitted fields as released by this manager), the manager scheduler re-admits the workload and, after a
    retry, the API server rejects the apply ("admissionChecks[0].state: Required value"; measured 2026-10-08).
    The apply therefore restates those fields exactly as read.
    """
    keep = {k: status[k] for k in KEPT_STATUS if status and status.get(k)}
    custom.patch_namespaced_custom_object_status(
        GROUP, VERSION, ns, "workloads", name,
        {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Workload",
         "metadata": {"name": name, "namespace": ns},
         "status": {"nominatedClusterNames": want, **keep}},
        field_manager="kueue-admission", force=True, _content_type="application/apply-patch+yaml")
PVC_JOBS_ANN = f"{LABEL}/jobs"          # on a worker PVC: Jobs that use it (owners once they exist)
MANAGED_LABEL = f"{LABEL}/managed-by"
CAP_RANK = {"reserved": 0, "on_demand": 1, "ondemand": 1, "spot": 2}   # fleet.yaml capacity_order


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
            **p, "class": p.get("gpu_class"), "price": float(p.get("usd_per_gpu_hour", 1e9))}
    return out


def price_of(pool: dict, spot_prices: dict, key: str) -> float:
    cap = pool.get("capacity", "spot")
    if cap == "reserved":
        return float(pool.get("price", 0.0))
    live = spot_prices.get(key) if cap == "spot" else None
    return float(live if live is not None else pool.get("price", 1e9))


def profile_from_name(name: str | None, clusters: dict, spot_prices: dict) -> dict:
    """Manager ClusterQueue name -> {"classes": [...in preference order], "strategy"}.
    prefer-<class>: that class first, then the others by their cheapest pool; anything else: cheapest of all."""
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


def rank(profile: dict, clusters: dict, free: dict, gpus: int, spot_prices: dict, pin: str | None = None,
         allowed_classes: list | None = None, allowed_clusters: list | None = None) -> list:
    """Candidates best-first as (cluster, flavor, price, is_free).

    profile:  {"classes": [preferred, ...], "strategy": "preferred" | "cheapest"}
    clusters: {cluster: {"pools": {flavor: {"class", "capacity": reserved|on_demand|spot, "price"}}}}
    free:     {(cluster, flavor): free_gpus}
    allowed_classes / allowed_clusters: the model's GPU classes and deployment regions (None = no restriction)
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
            if pool.get("class") not in classes:
                continue
            price = price_of(pool, spot_prices, f"{cname}/{fname}")
            is_free = free.get((cname, fname), 0) >= max(gpus, 1)
            cands.append((cname, fname, price, is_free))

    def key(c):
        cname, fname, price, is_free = c
        pool = clusters[cname]["pools"][fname]
        cap = CAP_RANK.get(pool.get("capacity", "spot"), 3)
        if strategy == "preferred":   # class preference, then reserved -> on-demand -> spot, then price
            return (0 if is_free else 1, classes.index(pool["class"]), cap, price, cname, fname)
        return (0 if is_free else 1, price, cap, cname, fname)

    return sorted(cands, key=key)


SNAPSHOT: dict = {"clusters": {}, "spot": {}, "free": {}, "aliases": {}, "at": None}   # the last reconcile's view, served by /v1/rank


def rank_response(snapshot: dict, profile_name: str | None, gpus: int, classes: list | None, regions: list | None,
                  pin: str | None = None) -> dict:
    """The /v1/rank answer from a reconcile snapshot: every (cluster, pool) candidate best-first with its GPU class,
    capacity type, price and whether it is free now; `best` is the first one. The API asks this at submission for a
    run class with per-GPU-class images (docs/SCHEDULING.md "Per-GPU images for runs"): the class of `best` decides
    the image, and the run is then queued on that class's profile (it may still move between regions of the class)."""
    clusters, spot, free, aliases = snapshot["clusters"], snapshot["spot"], snapshot["free"], snapshot["aliases"]
    names = {v: k for k, v in aliases.items()}
    prof = profile_from_name(profile_name, clusters, spot)
    allowed = [aliases.get(r, r) for r in regions] if regions else None
    ranked = rank(prof, clusters, free, gpus, spot, aliases.get(pin, pin) if pin else None, classes, allowed)
    out = []
    for cname, fname, price, is_free in ranked:
        pool = clusters[cname]["pools"][fname]
        out.append({"cluster": cname, "region": names.get(cname, cname), "pool": fname, "gpu_class": pool.get("class"),
                    "capacity": pool.get("capacity", "spot"), "price": price, "free": is_free})
    return {"profile": prof, "ranked": out, "best": out[0] if out else None, "snapshot_at": snapshot.get("at")}


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


def pvc_of(template: dict) -> str | None:
    for v in (template.get("spec") or {}).get("volumes") or []:
        if v.get("persistentVolumeClaim", {}).get("claimName"):
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


def volume_action(pvc_cluster: str, wanted: list, admitted: dict, ns: str, jobs_exist: bool, age_s: float) -> str | None:
    """What the sweeper does with a dispatcher-created volume: "orphan" when none of its manager Jobs exists any
    more (after the grace period), "elsewhere" when every Job that uses it was admitted in another cluster,
    None to keep it."""
    if not jobs_exist:
        return "orphan" if age_s >= ORPHAN_AFTER_S else None
    if wanted and all(admitted.get((ns, j)) not in (None, pvc_cluster) for j in wanted):
        return "elsewhere"
    return None


# ---------- cluster access ----------
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
        out[name] = {"custom": client.CustomObjectsApi(api), "core": client.CoreV1Api(api), "batch": client.BatchV1Api(api)}
    return out, aliases


def free_quota(workers: dict) -> dict:
    """(cluster, flavor) -> free nvidia.com/gpu: worker ClusterQueue nominal quota minus flavorsUsage."""
    free = {}
    for cname, w in workers.items():
        try:
            cqs = w["custom"].list_cluster_custom_object(GROUP, VERSION, "clusterqueues").get("items", [])
        except Exception as e:  # unreachable worker: no free capacity there
            log.warning("worker %s unreachable: %s", cname, e)
            continue
        for cq in cqs:
            nominal, used = {}, {}
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
                if n:  # the queue that holds the nominal quota (default); prefer-* queues have 0
                    free[(cname, fname)] = max(free.get((cname, fname), 0), n - used.get(fname, 0))
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


def ensure_pvc(w: dict, ns: str, claim: str, size_gi: str, job_name: str) -> None:
    """The run's work volume in the chosen worker; `job_name` is recorded so the adoption pass can make the
    worker's Job its owner. Idempotent (a resume reuses the original's volume)."""
    core = w["core"]
    try:
        cur = core.read_namespaced_persistent_volume_claim(claim, ns)
        jobs = [j for j in ((cur.metadata.annotations or {}).get(PVC_JOBS_ANN) or "").split(",") if j]
        if job_name not in jobs:
            core.patch_namespaced_persistent_volume_claim(claim, ns, {"metadata": {"annotations": {PVC_JOBS_ANN: ",".join(jobs + [job_name])},
                                                                                   "labels": {MANAGED_LABEL: "dispatcher"}}})
        return
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
    body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": claim, "labels": {MANAGED_LABEL: "dispatcher", f"{LABEL}/operation": job_name},
                         "annotations": {PVC_JOBS_ANN: job_name}},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": f"{size_gi}Gi"}}}}
    try:
        core.create_namespaced_persistent_volume_claim(ns, body)
        log.info("created %s/%s (%s Gi) for %s", ns, claim, size_gi, job_name)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise


def sweep_volumes(workers: dict, batch: client.BatchV1Api, admitted: dict) -> None:
    """Worker PVCs the dispatcher created: delete one once none of its Jobs exists on the manager any more
    (older than ORPHAN_AFTER_S), or when every Job that uses it was admitted in another cluster (`admitted`:
    (namespace, manager job) -> cluster)."""
    for cname, w in workers.items():
        try:
            pvcs = w["core"].list_persistent_volume_claim_for_all_namespaces(label_selector=f"{MANAGED_LABEL}=dispatcher").items
        except Exception as e:
            log.warning("worker %s: list volumes: %s", cname, e)
            continue
        for pvc in pvcs:
            ns, name = pvc.metadata.namespace, pvc.metadata.name
            age = (datetime.now(timezone.utc) - pvc.metadata.creation_timestamp).total_seconds()
            wanted = [j for j in ((pvc.metadata.annotations or {}).get(PVC_JOBS_ANN) or "").split(",") if j]
            alive = False
            for jn in wanted:
                try:
                    batch.read_namespaced_job(jn, ns)
                    alive = True
                    break
                except client.exceptions.ApiException as e:
                    if e.status != 404:
                        log.warning("manager job %s/%s: %s", ns, jn, e.reason)
                        alive = True          # unknown: keep
                        break
            action = volume_action(cname, wanted, admitted, ns, alive, age)
            if action:
                w["core"].delete_namespaced_persistent_volume_claim(name, ns)
                log.info("worker %s: deleted volume %s/%s (%s; manager jobs %s, admitted %s)", cname, ns, name, action, wanted,
                         {j: admitted.get((ns, j)) for j in wanted})



def reconcile(core, custom, batch):
    clusters, spot = load_fleet(core)
    workers, aliases = worker_clients(core, custom)
    free = free_quota(workers)
    SNAPSHOT.update({"clusters": clusters, "spot": spot, "free": dict(free), "aliases": aliases, "at": datetime.now(timezone.utc).isoformat()})
    if not DISPATCH:
        return
    workloads = custom.list_cluster_custom_object(GROUP, VERSION, "workloads").get("items", [])
    for wl in workloads:
        if not pending_multikueue(wl):
            continue
        meta, st = wl["metadata"], wl.get("status", {})
        ns, job_name = meta["namespace"], owner_job(wl)
        prof = profile_from_name(profile_of(custom, wl), clusters, spot)
        pin = (meta.get("labels") or {}).get(REGION_LABEL)
        pin = aliases.get(pin, pin)
        classes, regions = allowed_of(wl)
        allowed_clusters = [aliases.get(r, r) for r in regions] if regions else None
        already = st.get("nominatedClusterNames") or []
        stamp = (meta.get("annotations") or {}).get(NOMINATED_AT)
        age = (time.time() - datetime.fromisoformat(stamp).timestamp()) if stamp else 0.0
        gpus = gpu_request(wl)
        ranked = rank(prof, clusters, free, gpus, spot, pin, classes, allowed_clusters)
        if not ranked:
            log.warning("%s/%s: no candidate cluster (profile=%s pin=%s classes=%s regions=%s)", ns, meta["name"], prof, pin, classes, regions)
            continue
        want = nominations(ranked, already, age)
        if want and want != already:
            log.info("%s/%s profile=%s pin=%s classes=%s gpus=%d -> %s best=%s", ns, meta["name"], prof, pin, classes, gpus, want, ranked[:1])
            nominate(custom, ns, meta["name"], want, st)
            custom.patch_namespaced_custom_object(
                GROUP, VERSION, ns, "workloads", meta["name"],
                {"metadata": {"annotations": {NOMINATED_AT: datetime.now(timezone.utc).isoformat()}}})
        if ranked and ranked[0][3]:  # account for the capacity this workload will take
            cname, fname = ranked[0][0], ranked[0][1]
            free[(cname, fname)] = free.get((cname, fname), 0) - max(gpus, 1)
    # work volumes: exactly one, in the cluster that admitted the run
    admitted = {}
    for wl in workloads:
        meta = wl["metadata"]
        recorded = (meta.get("annotations") or {}).get(ADMITTED_ANN)
        cname = admitted_cluster(wl, None if (recorded or finished(wl)) else remote_workloads(workers, wl))
        job_name = owner_job(wl)
        if cname and job_name:
            admitted[(meta["namespace"], job_name)] = cname
        if cname and recorded != cname:
            custom.patch_namespaced_custom_object(GROUP, VERSION, meta["namespace"], "workloads", meta["name"],
                                                  {"metadata": {"annotations": {ADMITTED_ANN: cname}}})
            log.info("%s/%s admitted in %s", meta["namespace"], meta["name"], cname)
        vol = admitted_volume(wl, cname)
        if vol and vol[0] in workers:
            cname, ns, claim, size, job_name = vol
            ensure_pvc(workers[cname], ns, claim, size, job_name)
    sweep_volumes(workers, batch, admitted)



class RankHandler(BaseHTTPRequestHandler):
    """GET /v1/rank?profile=prefer-h100&gpus=1&classes=h100,l40s&regions=eu-north1,eu-south1&pin=eu-south1 ; GET /healthz"""
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json(200, {"ok": SNAPSHOT.get("at") is not None, "snapshot_at": SNAPSHOT.get("at")})
        if u.path != "/v1/rank":
            return self._json(404, {"error": "not found"})
        q = parse_qs(u.query)
        classes = [c for c in q.get("classes", [""])[0].split(",") if c] or None
        regions = [r for r in q.get("regions", [""])[0].split(",") if r] or None
        try:
            gpus = int(q.get("gpus", ["1"])[0])
        except ValueError:
            return self._json(400, {"error": "gpus must be an integer"})
        if SNAPSHOT.get("at") is None:
            return self._json(503, {"error": "no snapshot yet"})
        return self._json(200, rank_response(SNAPSHOT, q.get("profile", [None])[0], gpus, classes, regions, q.get("pin", [None])[0]))

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet
        return


def serve_rank():
    srv = ThreadingHTTPServer(("0.0.0.0", RANK_PORT), RankHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("rank endpoint on :%d (dispatching=%s)", RANK_PORT, DISPATCH)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core, custom, batch = client.CoreV1Api(), client.CustomObjectsApi(), client.BatchV1Api()
    serve_rank()
    while True:
        try:
            reconcile(core, custom, batch)
        except Exception as e:
            log.exception("reconcile failed: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
