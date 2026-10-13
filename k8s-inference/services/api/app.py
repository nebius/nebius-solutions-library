"""Nebius Serverless 2.0 customer API (docs/API.md). Run: uvicorn app:app"""
import asyncio, base64, hashlib, hmac, json, time, urllib.parse, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
import logging
import artifacts, attempts, billing, catalog, config as cfg, db, execution, jobs, keys, kube, models, monitoring, placement as placing
log = logging.getLogger("api")
import endpoints as ep
from execution import BindingRequest, ExecutionSpec
from auth import Principal, _principal, allows_model, check_budget, check_model, check_scope, forget, forget_hash, principal
import render
import status
from config import FLEET_MANAGER, GRAFANA_URLS, LITELLM_URL, PUBLIC_API_URL, REGION, SYNC_TIMEOUT_S

def check_placeholders() -> None:
    """Refuse to start with a manifest placeholder (charts/platform/templates/api/api.yaml carries
    `example.invalid` hostnames that the platform stage or the cluster overlay must replace): a placeholder
    would end up in every invoke link and Grafana link this API hands out."""
    bad = [name for name, value in (("PUBLIC_API_URL", cfg.PUBLIC_API_URL), ("GRAFANA_URLS", ",".join(cfg.GRAFANA_URLS.values())))
           if "example.invalid" in value]
    if bad:
        raise RuntimeError(f"placeholder hostnames in {', '.join(bad)}: set them per cluster (stack/platform/stage.tf)")



@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """The control API owns the fleet database schema (services/api/db.py MIGRATIONS), applied at startup; it also
    holds the keys (services/api/keys.py): the router key of this fleet."""
    check_placeholders()
    tasks = []
    if db.enabled():
        log.info("database schema version %s", db.migrate())
        if cfg.ROUTER_KEY:
            keys.ensure_router(cfg.ROUTER_KEY)
        else:
            log.warning("ROUTER_KEY is not set: LiteLLM model groups are not registered")
        tasks = [asyncio.create_task(_reconcile_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.11.35", lifespan=_lifespan)


INVOKE_TIMEOUT_MAX_S = 7 * 86400     # a sync call or a run may wait at most a week; nothing holds a connection or a Job beyond it
INPUT_MAX_BYTES = 60000               # a run's or async call's input lives on its Job (annotation, env): bounded, never truncated


class InvokeRequest(BaseModel):
    name: str | None = None
    mode: str | None = None
    input: dict = Field(default_factory=dict)
    region: str | None = None
    priority: str | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=INVOKE_TIMEOUT_MAX_S)
    execution: ExecutionSpec | None = None     # docs/JOBS.md "Execution options" (runs only)


ADMITTED_HEADER = "x-serverless2-admitted"     # the API's own proxied call to a gateway: the hook must not count it again
RESERVATION_STALE_S = 3600                        # a reservation whose Job is gone this long after creation is released


def _admit(p: Principal, operation: str, gpu_seconds: float = 0.0, counts_request: bool = True) -> None:
    """Reserve the slot, the request and the GPU seconds of `operation` on the caller's key (keys.admit); nothing
    without the database, nothing for the router key (it carries no budget)."""
    if not db.enabled() or p.role == keys.ROUTER["role"]:
        return
    try:
        keys.admit(keys.key_hash(p.key), operation, p.tenant, gpu_seconds, counts_request)
    except keys.KeyLimit as e:
        raise HTTPException(e.status, e.detail)


def _release(operation: str, gpu_seconds: float | None = None) -> None:
    if db.enabled():
        try:
            keys.release(operation, gpu_seconds)
        except Exception as e:  # noqa: BLE001 - the reconciler's sweep releases what this missed
            log.warning("release of %s: %s", operation, e)


def _job_gpus(job: dict) -> int:
    """GPUs a rendered Job (or JobSet) holds while it runs: nvidia.com/gpu over its containers times its parallelism."""
    def pods(spec: dict) -> int:
        tmpl = (spec.get("template") or {}).get("spec") or {}
        n = 0
        for c in (tmpl.get("containers") or []) + (tmpl.get("initContainers") or []):
            res = (c.get("resources") or {})
            n += int(str((res.get("limits") or {}).get("nvidia.com/gpu") or (res.get("requests") or {}).get("nvidia.com/gpu") or 0))
        return n * int(spec.get("parallelism") or 1)
    if job.get("kind") == "JobSet":
        return sum(pods((rj.get("template") or {}).get("spec") or {}) * int(rj.get("replicas") or 1) for rj in (job.get("spec") or {}).get("replicatedJobs") or [])
    return pods(job.get("spec") or {})


def _gpu_reservation(gpus: int, req: "InvokeRequest", p: Principal) -> float:
    """GPU seconds a run may take: its GPUs times its enforced timeout, for a key with a finite GPU-second budget
    (such a key must give timeout_s on a GPU run); unbounded keys and CPU-only runs reserve nothing."""
    if p.info.get("budget_gpu_seconds") is None or gpus == 0:
        return 0.0
    if not req.timeout_s:
        raise HTTPException(400, "this key has a GPU-second budget: give timeout_s so the run's reservation is bounded")
    return float(gpus * req.timeout_s)


def _admitted_token(key_hash: str) -> str:
    """HMAC the gateway hook verifies on the API's own proxied calls (one API call + its gateway authorization is one
    logical request); the router key is the shared secret of this fleet."""
    return hmac.new((cfg.ROUTER_KEY or "").encode(), key_hash.encode(), hashlib.sha256).hexdigest()


class ResumeRequest(BaseModel):
    checkpoint_ref: str | None = Field(default=None, max_length=1024)   # a portable checkpoint: no volume, any permitted cluster/class
    gpu_class: str | None = None


class UploadRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    expires_s: int = 3600
    region: str = REGION


class ScalingRequest(BaseModel):
    min_replicas: int | None = Field(default=None, ge=0)
    max_replicas: int | None = Field(default=None, ge=1)
    target_concurrency: int | None = Field(default=None, ge=1)
    scale_to_zero_after_s: int | None = Field(default=None, ge=0)
    scaling: models.ScalingSpec | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=600)


class KeyRequest(BaseModel):
    alias: str
    budget: float = 10.0
    models: list[str] = Field(default_factory=list)
    expires_days: int | None = None


class KeyUpdate(BaseModel):
    """What an admin may change on an existing key: the spending limit, the model allow-list, the expiry (days from
    now; 0 = never). Fields left out stay as they are; spend and the key value never change here."""
    budget: float | None = Field(default=None, ge=0)
    models: list[str] | None = None
    expires_days: int | None = Field(default=None, ge=0)



def with_served_model(m: dict, inp):
    """OpenAI-protocol endpoints: fill in the served model name when the caller omitted `model`."""
    if isinstance(inp, dict) and m.get("protocol") == "openai" and m.get("served_model") and not inp.get("model"):
        return {**inp, "model": m["served_model"]}
    return inp

def _model(name: str) -> dict:
    m = catalog.get(name)
    if not m:
        raise HTTPException(404, f"model {name} not found")
    return m


def _variant_for(m: dict, inp, cluster: str) -> dict:
    """Which variant of an endpoint answers a request (docs/API.md "Variants"): the one the model's `routing`
    field selects, else the first variant in priority order that has a ready replica, else the first (its cold
    start is the request's)."""
    vs = models.variants(m, cluster)
    if len(vs) == 1:
        return vs[0]
    routing = m.get("routing") or {}
    value = inp
    for part in (routing.get("field") or "").split("."):
        value = value.get(part) if isinstance(value, dict) else None
    target = (routing.get("map") or {}).get(str(value)) if value is not None else None
    if target is None and routing.get("default") not in (None, "priority"):
        target = routing["default"]
    if target:
        return next((v for v in vs if v["name"] == target), vs[0])
    for v in vs:
        if (kube.endpoint_status(v["object"], m["namespace"]).get("replicas_ready") or 0) > 0:
            return v
    return vs[0]


def _public(m: dict, p: Principal | None = None) -> dict:
    if p is not None and (p.info.get("metadata") or {}).get("role") != "admin":
        m = {**m, "spec": None}  # container configuration belongs to fleet administrators
    if m["mode"] == "run":
        return catalog.to_public(m)
    statuses = []
    for region in m["regions"]:          # the endpoints' live state, read in their clusters (REGION_KUBECONFIGS)
        try:
            st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
        except (HTTPException, ApiException):
            st = {"status": "unavailable", "replicas_ready": None}
        statuses.append({"region": region, **st})
    return catalog.to_public(m, statuses)


@app.get("/livez", include_in_schema=False)
def livez():
    """Liveness: the process answers. Nothing here depends on the database or the clusters (the readiness probe
    does, through /healthz): a database outage must not restart every API replica and take ext-auth with it."""
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True, "models": len(catalog.all_models()), "region": REGION, "regions": kube.regions(), "fleet_manager": FLEET_MANAGER,
            "gpu_classes": sorted({p["gpu_class"] for p in kube.fleet()["pools"].values() if p.get("gpu_class")})}


@app.get("/v1/fleet")
def fleet_info(live: bool = False, p: Principal = Depends(principal)):
    """The fleet as Terraform shaped it (pools per region, GPU class, preset, capacity type, price) and, with
    `live=true`, what is up right now: ready nodes per pool and the GPUs their pods hold (the console's Fleet page)."""
    pools = list(kube.fleet().get("pools", {}).values())
    regions = sorted({kube.cluster_region(pool["region"]) for pool in pools if pool.get("region")}
                     | {r for m in catalog.all_models().values() for r in m.get("regions", [])})
    out = {"regions": regions, "gpu_classes": sorted({pool["gpu_class"] for pool in pools if pool.get("gpu_class")}),
           "fleet_manager": FLEET_MANAGER, "region": REGION,
           "execution": execution.capabilities(p.namespace),      # what this tenant may ask for (services/api/execution.py)
           "pools": [{"region": kube.cluster_region(pool.get("region") or REGION), "cluster": pool.get("region"), "pool": pool.get("pool"),
                      "gpu_class": pool.get("gpu_class"), "platform": pool.get("platform"), "preset": pool.get("preset"),
                      "gpus_per_node": int(pool.get("gpus_per_node") or 1), "capacity": pool.get("capacity"),
                      "usd_per_gpu_hour": pool.get("usd_per_gpu_hour"), "max_nodes": pool.get("max_nodes"),
                      "interconnect": pool.get("interconnect"), "local_nvme": bool(pool.get("local_nvme"))} for pool in pools]}
    # the caller's tenant: its quotas as terraform.tfvars shaped them (charts/fleet tenants.yaml) and, live, what its
    # queues hold per region (docs/FLEET.md "Tenants")
    t = (kube.fleet().get("tenants") or {}).get(p.tenant) or {}
    out["tenant"] = {"name": p.tenant, "gpu_quota": t.get("gpu_quota"), "fair_share_weight": t.get("fair_share_weight", 1),
                     "dedicated_pools": list(t.get("dedicated_pools") or [])}
    if live:
        out["tenant"]["usage"] = {region: kube.tenant_usage(p.tenant, region) for region in kube.regions() if not (FLEET_MANAGER and region == REGION)}
        for entry in out["pools"]:
            entry.update(kube.pool_usage(entry["cluster"], entry["pool"]))
        # the clusters: this one (the control plane when it is the fleet manager) and every region it reaches
        by_cluster = {pool.get("region"): pool for pool in pools}
        clusters = []
        for region in kube.regions():
            cid = next((cid for cid, pool in by_cluster.items() if kube.cluster_region(cid) == region), region)
            pool = by_cluster.get(cid) or {}
            control = region == REGION and FLEET_MANAGER
            clusters.append({"id": "control" if control else cid, "region": "control" if control and region == "control" else region,
                             "project": pool.get("project"), "roles": ["control", "manager"] if control else ["worker"],
                             "api_url": PUBLIC_API_URL if region == REGION else None,     # the fleet's one API
                             "grafana_url": GRAFANA_URLS.get("control" if control else region) or GRAFANA_URLS.get(region),
                             **kube.cluster_summary(region)})
        out["clusters"] = clusters
    return out


@app.get("/v1/models")
def list_models(p: Principal = Depends(principal)):
    check_scope(p, "read:catalog")
    return [_public(m, p) for m in catalog.all_models().values() if allows_model(p, m["name"])]


class ModelSpec(BaseModel):
    model_config = {"extra": "allow"}   # the spec of services/api/models.py; reserved fields pass through
    id: str
    kind: str = "endpoint"
    image: str | None = None


def _runtime_or_409(mid: str) -> dict:
    m = catalog.get(mid)
    if m and m.get("managed_by") != "api":
        raise HTTPException(409, f"model {mid} is a built-in class of the solution ({m.get('managed_by')}); it cannot be changed through the API")
    return m


def _writes_here():
    """Model definitions are written to the fleet database (DATABASE_URL)."""
    if not db.enabled():
        raise HTTPException(409, "this API has no fleet database (DATABASE_URL): model definitions cannot be stored")


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
        if (w := models.litellm_group_upsert(entry, cid, region)):
            result.setdefault("warnings", []).append(w)
            raise RuntimeError(w)


def _undeploy(entry: dict, clusters: list[str], after: dict):
    if entry.get("mode") == "run":
        return
    for cid in clusters:
        region = kube.cluster_region(cid)
        models.delete_rendered(entry, cid, region)
        models.sync_certificate(region, after)
        if (warning := models.litellm_group_delete(entry["id"], cid, [v["name"] for v in models.variants(entry, cid)[1:]])):
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
        if m.get("managed_by") != "api" or int(m.get("nodes") or 1) <= 1 or int(sc.get("min") or 0) < 1 or not sc.get("idle_s"):
            continue
        try:
            busy = False
            for cid in m.get("deployments") or {}:
                region = kube.cluster_region(cid)
                st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m["nodes"]))
                marks = [st.get(k) for k in ("ready_at", "last_request_at") if st.get(k)]
                if st["status"] != "ready" or not marks:
                    busy = True                      # starting, or no Ready pod yet: nothing to measure
                    continue
                last = max(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in marks)
                if (now - last).total_seconds() < int(sc["idle_s"]):
                    busy = True
            if busy:
                continue
            new_spec = {**spec, "scaling": {**sc, "min": 0}}
            entry = models.to_entry(new_spec, managed_by="api")
            row = db.get_model(m["name"])
            try:    # an admin's write in between wins: this round skips, the next one re-reads
                db.upsert_model(entry, new_spec, by="idle-shutdown", expected=row.get("version") if row else None, create_only=False)
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
            items = [b.api_client.sanitize_for_serialization(j) for j in b.list_namespaced_job(ns, label_selector=f"{cfg.LABEL}/mode=run").items]
        except Exception:  # noqa: BLE001
            continue
        clusters = None
        for job in items:
            ann = job["metadata"].get("annotations") or {}
            wait = ann.get(f"{cfg.LABEL}/fallback-wait-s")
            if wait is None or not jobs.is_manager_job(job) or status.finished(job) or ann.get(f"{cfg.LABEL}/successor"):
                continue
            created = datetime.fromisoformat(str(job["metadata"]["creationTimestamp"]).replace("Z", "+00:00"))
            if (now - created).total_seconds() < int(wait):
                continue
            if clusters is None:
                clusters = kube.workload_clusters(ns)
            if clusters.get(job["metadata"].get("uid", "")):
                continue                      # placed: the wait is over
            try:
                ex = jobs.normalise(job).get("execution") or {}
                pref = (ex.get("placement") or {}).get("gpu_preference") or []
                current = job["metadata"].get("labels", {}).get(f"{cfg.LABEL}/gpu-class") or (pref[0] if pref else None)
                remaining = pref[pref.index(current) + 1:] if current in pref else []
                if not remaining:
                    continue                  # the last class of the preference: keep waiting
                m = _model(job["metadata"]["labels"].get(f"{cfg.LABEL}/model", ""))
                new, _ = jobs.switch_class(ns, job["metadata"]["name"], REGION, m, catalog.fleet_params(m), remaining[0], remaining)
                moved.append(new["metadata"]["name"])
                log.info("fallback: %s/%s -> %s on %s", ns, job["metadata"]["name"], new["metadata"]["name"], remaining[0])
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
            items = [b.api_client.sanitize_for_serialization(j) for j in b.list_namespaced_job(ns, label_selector=f"{cfg.LABEL}/mode=run").items]
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
            if rec.get("on_infrastructure_loss") != "recover" or not jobs.is_manager_job(item) or status.finished(item) \
                    or ann.get(f"{cfg.LABEL}/successor") or ann.get(f"{cfg.LABEL}/cancelled"):
                continue
            try:
                job = jobs.get(ns, name, REGION)
                root = ann.get(f"{cfg.LABEL}/resumed-from") or name
                op = jobs.normalise(job, records=attempts.for_operation(root))
                lost, why = _lost(op, job)
                if lost is None:
                    if ann.get(f"{cfg.LABEL}/lost-at"):          # back on its own class: the loss is over
                        _annotate(ns, name, {f"{cfg.LABEL}/lost-at": None})
                    continue
                lost_at = ann.get(f"{cfg.LABEL}/lost-at")
                if not lost_at:
                    _annotate(ns, name, {f"{cfg.LABEL}/lost-at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
                    lost_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                if (now - datetime.fromisoformat(lost_at.replace("Z", "+00:00"))).total_seconds() < int(rec.get("wait_s") or 0):
                    continue
                tenant = md["labels"][f"{cfg.LABEL}/tenant"]
                marks = attempts._marks(name)
                if marks and marks[1]:
                    continue                                       # cancelled: never resurrected
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
                m = _model(md["labels"].get(f"{cfg.LABEL}/model", ""))
                pref = (ex.get("placement") or {}).get("gpu_preference") or jobs.gpu_classes(m)
                current = lost.get("gpu_class") or md.get("labels", {}).get(f"{cfg.LABEL}/gpu-class") or (pref[0] if pref else None)
                remaining = pref[pref.index(current) + 1:] if current in pref else list(pref)
                next_class = remaining[0] if remaining else current
                if not stop and not next_class:
                    stop = "no GPU class to continue on"
                if stop:
                    if ann.get(f"{cfg.LABEL}/recovery") != stop:
                        _annotate(ns, name, {f"{cfg.LABEL}/recovery": stop})
                    continue
                placement = execution.narrow_placement({"profile": jobs.profile_of(m), "classes": [next_class], "regions": m["regions"]}, ex) or {}
                placement["classes"] = [next_class]
                new, created = jobs.recover(ns, name, REGION, m, placement, pointer, int(lost.get("epoch") or 0), next_class,
                                            remaining or [next_class], why, timeout_s=remaining_s)
                marks = attempts._marks(name)
                if marks and marks[1]:                                 # cancelled while we were moving it: cancel the successor, too
                    attempts.mark_cancelled(new["metadata"]["name"])
                    jobs.cancel(ns, new["metadata"]["name"], REGION)   # never started: deleted
                    _annotate(ns, name, {f"{cfg.LABEL}/cancelled": "true", f"{cfg.LABEL}/recovery": "cancelled while recovering: the successor was withdrawn"})
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
        except Exception:
            log.exception("model reconciliation unavailable")
        await asyncio.sleep(15)


def release_stale_reservations() -> list[str]:
    """A key reservation whose operation no longer exists (a Job deleted before billing accounted it, a submission
    that failed after admission) is released after RESERVATION_STALE_S with nothing measured; billing releases the
    rest with the attempt's GPU seconds. Control API only. Returns the operations released."""
    if not db.enabled():
        return []
    out = []
    for operation, tenant in keys.stale_reservations(RESERVATION_STALE_S):
        try:
            jobs.find(f"{cfg.TENANT_NS_PREFIX}{tenant}", operation)
        except HTTPException as e:
            if e.status_code == 404:
                keys.release(operation, None)
                out.append(operation)
    return out


def _write_model(spec: dict, p: Principal, replace: bool, expected: int | None = None) -> dict:
    """Commit desired state and history, then attempt its durable reconciliation."""
    _writes_here()
    entry = models.to_entry(spec, managed_by="api")
    if "scaling" in entry["spec"]:
        spec = {**spec, "scaling": entry["spec"]["scaling"]}   # store the complete policy that was rendered (models.validate)
    mid = entry["id"]
    existing = catalog.get(mid)
    if existing and not replace:
        raise HTTPException(409, f"model {mid} exists (PUT replaces it)")
    if existing:
        _runtime_or_409(mid)
    result = {"id": mid, "kind": spec.get("kind", "endpoint"), "regions": list(entry["deployments"]), "applied": {}}
    row = db.upsert_model(entry, spec, by=p.info.get("key_alias") or p.info.get("key_name"), expected=expected, create_only=not replace)
    catalog.invalidate()
    result.update(reconcile_models().get(mid, {"pending": True}))
    result.update({"version": row.get("version"), "model": catalog.to_public(catalog.normalise(entry))})
    return result


@app.post("/v1/models", status_code=201)
def create_model(req: ModelSpec, p: Principal = Depends(principal)):
    """Define a model from a container (an endpoint or a job class): the console's "New model" form. Admin
    keys, on the fleet's API (the control cluster, where the database is). services/api/models.py."""
    admin(p)
    return _write_model(req.model_dump(), p, replace=False)


@app.put("/v1/models/{model}")
def replace_model(model: str, req: ModelSpec, p: Principal = Depends(principal), if_match: int | None = Header(default=None)):
    admin(p)
    spec = req.model_dump()
    if spec.get("id") != model:
        raise HTTPException(400, "id in the body must match the path")
    return _write_model(spec, p, replace=True, expected=if_match)


@app.delete("/v1/models/{model}", status_code=204)
def delete_model(model: str, p: Principal = Depends(principal), if_match: int | None = Header(default=None)):
    admin(p)
    _writes_here()
    m = _runtime_or_409(model)
    if not m:
        raise HTTPException(404, f"model {model} not found")
    db.delete_model(model, by=p.info.get("key_alias") or p.info.get("key_name"), expected=if_match)
    catalog.invalidate()
    reconcile_models()


@app.get("/v1/models/{model}/history")
def model_history(model: str, p: Principal = Depends(principal)):
    """Every version of a model's definition (who changed what, when): the `models_history` table."""
    admin(p)
    _writes_here()
    if not db.get_model(model) and not db.history(model):
        raise HTTPException(404, f"model {model} not found")
    return db.history(model)


@app.get("/v1/models/{model}")
def get_model(model: str, p: Principal = Depends(principal)):
    check_scope(p, "read:catalog")
    check_model(p, model)
    return _public(_model(model), p)


@app.api_route("/internal/authorize/{model}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"], include_in_schema=False)
@app.api_route("/internal/authorize/{model}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"], include_in_schema=False)
async def authorize_endpoint(model: str, request: Request, path: str = ""):
    """Envoy prefixes the original URI with a model identity fixed by the endpoint's SecurityPolicy."""
    key = request.headers.get("authorization", "")
    if not key.lower().startswith("bearer "):
        token = request.query_params.get("api_key", "")
        for protocol in request.headers.get("sec-websocket-protocol", "").split(","):
            protocol = protocol.strip()
            if not token and (protocol.startswith("bearer.") or protocol.startswith("sk-")):
                token = protocol.removeprefix("bearer.")
        key = f"Bearer {token}" if token else ""
    p = await _principal(key, allow_router=True)       # LiteLLM's deployments call here with the fleet's router key
    token = request.headers.get(ADMITTED_HEADER, "")
    admitted = bool(token) and hmac.compare_digest(token, _admitted_token(keys.key_hash(p.key)))
    await asyncio.to_thread(_authorize_model, p, model, admitted)      # catalog (database) and Kubernetes calls: off the event loop
    return JSONResponse({}, headers={"x-serverless2-tenant": p.tenant, "x-serverless2-key-alias": str(p.info.get("key_alias") or "")})


def _authorize_model(p: Principal, model: str, admitted: bool = False) -> None:
    m = catalog.get(model) or _endpoint_model(model)
    check_model(p, m["name"])
    check_budget(p)
    check_scope(p, "invoke:sync")
    if not admitted and p.role != keys.ROUTER["role"]:
        # a direct call to the endpoint's hostname: one request against the key's budget and rate window (the slot is
        # not held: the hook cannot see the call end); the API's own proxied call was admitted already
        gid = f"gw-{uuid.uuid4().hex[:12]}"
        _admit(p, gid)
        _release(gid, 0.0)
    if m["mode"] == "run":
        raise HTTPException(404, "not an endpoint")
    if int(m.get("nodes") or 1) > 1:
        for region in m["regions"]:                                    # the gateway's call names no region: stamp every
            kube.stamp_last_request(m["k8s_name"], m["namespace"], region)   # deployment (idle shutdown reads it, throttled)


@app.get("/v1/keys/me")
def key_me(p: Principal = Depends(principal)):
    forget(p.key)
    return p.public()


@app.post("/v1/models/{model}:invoke")
async def invoke(model: str, req: InvokeRequest, p: Principal = Depends(principal),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    m = _model(model)
    check_model(p, model)
    check_budget(p)
    mode = req.mode or m["mode"]
    if mode not in m["modes"]:
        raise HTTPException(400, f"model {model} supports modes {m['modes']}, not {mode}")
    check_scope(p, {"sync": "invoke:sync", "async": "invoke:async"}.get(mode, "run:submit"))
    region = req.region or (REGION if REGION in m["regions"] else m["regions"][0])
    if region not in m["regions"]:
        raise HTTPException(400, f"model {model} is deployed in {m['regions']}, not {region}")
    if req.execution and mode != "run":
        raise HTTPException(400, "execution options apply to runs only")
    if mode == "sync":
        sid = f"sync-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(_admit, p, sid)                      # a slot and a request for the duration of the call
        try:
            return await _sync(m, req, p, region)
        finally:
            await asyncio.to_thread(_release, sid, 0.0)
    if len(json.dumps(req.input)) > INPUT_MAX_BYTES:
        raise HTTPException(413, f"input exceeds {INPUT_MAX_BYTES} bytes; pass large inputs by reference (input_prefix, an artifact upload or a binding)")
    # the rest renders and creates a Job: synchronous Kubernetes, database and bucket calls, run in a worker thread
    return await asyncio.to_thread(_invoke_job, m, model, req, p, region, mode, idempotency_key)


def _invoke_job(m: dict, model: str, req: InvokeRequest, p: Principal, region: str, mode: str, idempotency_key: str | None):
    # admission: Kueue (profile queues) bounds concurrent runs; LiteLLM rpm/budget bound async calls;
    # the gateway BackendTrafficPolicy caps request rates. Nothing is counted here.
    name = render.op_name(p.tenant, model, idempotency_key)
    timeout = req.timeout_s          # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret = None
    ex = execution.resolve(req.execution, p.namespace, m) if mode == "run" else None
    allowed = (ex.get("placement") or {}).get("allowed_clusters") if ex else None
    if allowed and req.region and req.region not in allowed:
        # an explicit region that contradicts the caller's own cluster list; an implicit one (the default above) is no
        # request: a fleet run is placed within allowed_clusters, a single-cluster run must be allowed here
        raise HTTPException(400, f"region {req.region} is not in allowed_clusters {allowed}")
    if allowed and not FLEET_MANAGER and mode == "run" and region not in allowed:
        raise HTTPException(400, f"this API runs jobs in {region} only, which allowed_clusters {allowed} excludes")
    if mode == "async":                  # the caller Job runs in the endpoint's region, through that region's gateway
        output_prefix = f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        job, secret = jobs.build_async(name, m, req.input, p.tenant, p.key, req.name, idempotency_key, timeout, output_prefix, region)
    elif FLEET_MANAGER:
        _guard_run_input(m, req, p, REGION, ex)
        job = _fleet_run(m, req, p, name, timeout, idempotency_key, ex)
        region = REGION                      # created on the manager; the dispatcher picks the worker
    else:
        _guard_run_input(m, req, p, region, ex)
        defaults = catalog.region_params(m, region)
        if not (ex and ex.get("io_mode") == "application"):
            defaults.setdefault("output_prefix", f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}")
        else:
            defaults.setdefault("output_prefix", "")
        job = jobs.build_run(name, m, req.input, p.tenant, req.name, idempotency_key, timeout, req.priority, region, defaults, billing.key_hash(p.key),
                             gpu_class=_class_for(m, req, region, p.tenant, ex), ex=ex)
    # admission on the caller's key, from the rendered Job (its real GPU count): a slot, a request and, for a key with a
    # GPU-second budget, GPUs x the run's timeout; released with the measured value when the attempt is billed (billing.py)
    _admit(p, name, _gpu_reservation(_job_gpus(job), req, p) if mode == "run" else 0.0)
    created_job, created = jobs.create(p.namespace, job, region, secret)
    return JSONResponse(jobs.normalise(created_job, m.get("price_per_call")), status_code=202 if created else 200)


def _guard_run_input(m: dict, req: InvokeRequest, p: Principal, region: str, ex: dict | None = None) -> None:
    """A caller-supplied output_prefix must stay inside the tenant's own bucket of that region (never another
    tenant's), and an `image` parameter (container-run) must match the tenant's image allow-list when one is set.
    Application-managed I/O names no bucket at all."""
    out = req.input.get("output_prefix")
    if ex and ex.get("io_mode") == "application":
        if out or req.input.get("input_prefix"):
            raise HTTPException(400, "application-managed I/O takes no input_prefix/output_prefix: the run moves its own data")
        out = None
    if out:
        bucket = artifacts.storage(p.namespace, region)["bucket"]
        if not str(out).startswith(f"s3://{bucket}/"):
            raise HTTPException(400, f"output_prefix must be inside the tenant's bucket s3://{bucket}/")
    if req.input.get("image"):
        jobs.check_image_allowed(p.namespace, str(req.input["image"]), region)


def _fleet_run(m: dict, req: InvokeRequest, p: Principal, name: str, timeout: int | None, idempotency_key: str | None,
               ex: dict | None = None) -> dict:
    """Fleet placement (control cluster, docs/JOBS.md): the Job is created on the manager and MultiKueue +
    the dispatcher pick the worker among the model's regions and GPU classes. Inputs and outputs live in the
    tenant's hub-region bucket (the one this cluster's tenant-storage names). Per-region parameter defaults
    apply when every deployment agrees on them, or when the caller pins a region. Image references are
    fleet-wide (the logical registry host, docs/IMAGES.md), so an unpinned run is placed at queue time: the
    dispatcher nominates the cheapest free cluster and re-nominates while the run waits."""
    application = bool(ex and ex.get("io_mode") == "application")
    bucket = None if application else artifacts.storage(p.namespace, REGION)["bucket"]
    inp = req.input.get("input_prefix")
    if inp and not str(inp).startswith(f"s3://{bucket}/"):
        raise HTTPException(400, f"input_prefix must be in the tenant's fleet bucket s3://{bucket}/ (upload through this API, or pass region to run next to another bucket)")
    placement = execution.narrow_placement({"manager": True, "profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]}, ex)
    region = req.region
    defaults = catalog.region_params(m, region) if region else catalog.fleet_params(m)
    defaults.setdefault("output_prefix", "" if application else f"s3://{bucket}/operations/{name}")
    return jobs.build_run(name, m, req.input, p.tenant, req.name, idempotency_key, timeout, req.priority, region, defaults,
                          billing.key_hash(p.key), placement=placement, gpu_class=_class_for(m, req, region, p.tenant, ex, placement.get("regions")), ex=ex)


def _class_for(m: dict, req: InvokeRequest, region: str | None, tenant: str | None = None, ex: dict | None = None,
               regions: list[str] | None = None) -> str | None:
    """A run class with per-GPU-class images gets its class at submission (docs/SCHEDULING.md "Per-GPU images for
    runs"): the best pool of the fleet document among the model's classes and regions (the caller's region when
    given, services/api/placement.py). None for a class with one image: Kueue may then switch class at queue time as before."""
    if not jobs.class_images(m):
        return None
    gpus = (m.get("job") or {}).get("gpu", 1)
    try:
        gpus = int(gpus)
    except (TypeError, ValueError):           # a parameter placeholder: one GPU for the ranking
        gpus = int(req.input.get("gpus", 1) or 1) if isinstance(req.input.get("gpus", 1), (int, str)) and str(req.input.get("gpus", 1)).isdigit() else 1
    available = (ex.get("placement") or {}).get("gpu_preference") if ex else None   # the caller's order narrows the model's
    cls, how = placing.choose_class(m, region, gpus, available, tenant, regions)      # `regions`: the run's allowed clusters
    log.info("%s: GPU class %s chosen by %s", m["name"], cls, how)
    return cls


async def _sync(m: dict, req: InvokeRequest, p: Principal, region: str):
    """Proxy to the endpoint. Via LiteLLM's pass-through route when the model has one (budget, spend and the
    per-call price are enforced there); else straight to the predictor Service when the endpoint runs on this
    cluster, and through the region's gateway hostname (the one LiteLLM uses, TLS on the fleet CA) with the caller's
    key for an endpoint in another region: the gateway's ext-auth checks the key once more against this API."""
    if int(m.get("nodes") or 1) > 1:
        st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m["nodes"]))
        kube.stamp_last_request(m["k8s_name"], m["namespace"], region)     # a direct proxy call bypasses the gateway's authorize hook
        if st["status"] != "ready":
            raise HTTPException(503, f"endpoint {m['name']} is {st['status']} ({m['nodes']} nodes): a multi-node endpoint is started with "
                                     f"PATCH /v1/endpoints/{m['k8s_name']} {{\"scaling\": {{\"min\": 1}}}} (or the console's Start button) and takes "
                                     f"minutes to load; it stops again after scaling.idle_s without a request")
    if m.get("litellm_route"):
        url, headers = LITELLM_URL + m["litellm_route"], {"Authorization": f"Bearer {p.key}"}
    elif region == REGION:
        v = _variant_for(m, req.input, models.cluster_id())
        host = m["endpoint_host"] if v["object"] == m["k8s_name"] else f"http://{v['object']}-predictor.{m['namespace']}.svc.cluster.local"
        url, headers = host + m["endpoint_path"], {}
    else:
        if not models.endpoint_domain(region):
            raise HTTPException(503, f"sync calls need the endpoint domain of {region} (ENDPOINT_DOMAINS)")
        cfg.refresh_trust()
        v = _variant_for(m, req.input, region)
        url, headers = f"https://{models.endpoint_host(m, region, v['object'])}{m['endpoint_path']}", {"Authorization": f"Bearer {p.key}", ADMITTED_HEADER: _admitted_token(keys.key_hash(p.key))}
    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        async with httpx.AsyncClient(timeout=req.timeout_s or SYNC_TIMEOUT_S) as c:
            r = await c.post(url, json=with_served_model(m, req.input), headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, f"endpoint did not answer within {req.timeout_s or SYNC_TIMEOUT_S}s")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"endpoint unreachable: {e}")
    forget(p.key)
    ok = 200 <= r.status_code < 300
    try:
        body = r.json()
    except ValueError:
        media_type = r.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
        if media_type.startswith("text/"):
            body = r.text
        else:
            # ZIP, audio and image bytes must never pass through lossy UTF-8 decoding.
            body = {"encoding": "base64", "media_type": media_type, "size_bytes": len(r.content),
                    "sha256": hashlib.sha256(r.content).hexdigest(), "data": base64.b64encode(r.content).decode("ascii")}
    op = {"id": f"sync-{uuid.uuid4().hex[:12]}", "name": req.name, "model": m["name"], "mode": "sync", "region": region,
          "status": "SUCCEEDED" if ok else "FAILED", "tenant": p.tenant, "created_at": started, "started_at": started,
          "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_s": round(time.time() - t0, 3),
          "attempts": [{"index": 1, "status": "SUCCEEDED" if ok else "FAILED", "reason": None if ok else f"HTTP {r.status_code}"}],
          "input": req.input, "cost": m.get("price_per_call") if ok else None, "error": None if ok else str(body)[:500]}
    return JSONResponse({"result": body, "operation": op}, status_code=200 if ok else (r.status_code if r.status_code in (400, 401, 402, 403, 404, 409, 413, 415, 422, 429) else 502))


def _price(job: dict) -> float | None:
    m = catalog.get(job["metadata"].get("labels", {}).get("serverless2.nebius/model", "")) or {}
    return m.get("price_per_call")


@app.get("/v1/operations")
def list_operations(p: Principal = Depends(principal), limit: int = Query(50, le=500), model: str | None = None):
    check_scope(p, "read:operations")
    return [jobs.normalise(j, _price(j)) for j in jobs.list_ops(p.namespace, p.tenant, model, limit)]


def _owned(p: Principal, id: str, follow: bool = True) -> tuple[dict, str]:
    """The operation's Job. A handle that capacity fallback superseded resolves to its current successor (the
    original stays readable, docs/SCHEDULING.md); an operation whose Job expired is served from its durable rows."""
    current, previous = id, None
    for _ in range(16):
        try:
            job, region = jobs.find(p.namespace, current)
        except HTTPException as e:
            if e.status_code != 404 or not attempts.enabled():
                raise
            nxt = attempts.successor_of(current)
            if follow and nxt and nxt != current:
                current = nxt
                continue
            synthetic = attempts.synthesize(current, p.tenant)
            if synthetic is None:
                if previous is not None:          # a successor that is marked but not created yet: the original stands
                    return previous
                raise
            return {"_synthetic": synthetic, "metadata": {"name": current, "labels": {"serverless2.nebius/tenant": p.tenant, "serverless2.nebius/mode": "run"}, "annotations": {}}}, synthetic.get("region")
        if job["metadata"].get("labels", {}).get("serverless2.nebius/tenant") != p.tenant:
            raise HTTPException(404, f"operation {id}: not found")
        nxt = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/successor")
        if follow and nxt and nxt != current:
            current, previous = nxt, (job, region)
            continue
        return job, region
    raise HTTPException(502, f"operation {id}: successor chain too long")


def _records(p: Principal, job: dict, id: str, region: str | None) -> tuple[list, str]:
    """The attempt records of an operation: the database's (fenced, durable) when this API has one, else the
    uploader's bucket records. Returns (records, root operation)."""
    root = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    rows = [r for r in attempts.for_operation(root) if r["operation"] == id] if attempts.enabled() else []
    if rows:
        return rows, root
    return artifacts.attempt_records(p.namespace, id, region or REGION, jobs.output_prefix(job)), root


def _chain(p: Principal, op: dict, job: dict, root: str) -> dict:
    """The history of a resume/fallback/recovery chain on its effective member (docs/API.md "Chains"): the root's
    creation time, every attempt of every member with its measured GPU time and pinned image, per-member timing
    and consumption, cumulative measured consumption (what is not on record stays unknown: no end record, no
    seconds), and when the newest successor was created. The effective id, status and current attempt are the
    member's own and unchanged."""
    import status as st
    rows = attempts.for_operation(root) if attempts.enabled() else []
    by_pod = {r["pod"]: r for r in rows}                               # one row per pod (the newest wins)
    rows = list(by_pod.values())
    members = sorted([j for j in jobs.list_ops(p.namespace, p.tenant, None, 500)
                      if j["metadata"]["name"] == root or (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") == root],
                     key=lambda j: str(j["metadata"].get("creationTimestamp") or ""))
    name = job["metadata"]["name"]
    all_attempts = st._attempts(job.get("_pods", []), rows)          # every member's records; the live pods are this member's
    for a in all_attempts:
        r = by_pod.get(a.get("attempt_id")) or {}
        a["operation"] = r.get("operation") or name
        a["attempt_role"] = r.get("role") or a.get("attempt_role") or "run"
        a["image"] = r.get("image")
        d = st._secs(a.get("started_at"), a.get("ended_at"))
        a["gpu_seconds"] = round(d * a["_gpus"], 1) if d is not None and a.get("ended_at") else None   # unknown stays unknown
    chain, total, complete = [], 0.0, True
    for m in members:
        mid = m["metadata"]["name"]
        mine = [a for a in all_attempts if a["operation"] == mid]
        measured = [a["gpu_seconds"] for a in mine if a["gpu_seconds"] is not None]
        unknown = any(a["gpu_seconds"] is None and a["status"] != "QUEUED" for a in mine)
        ann = m["metadata"].get("annotations") or {}
        chain.append({"id": mid, "created_at": m["metadata"].get("creationTimestamp"),
                      "status": op["status"] if mid == name else jobs.normalise(m, None, [r for r in rows if r["operation"] == mid])["status"],
                      "gpu_class": (m["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/gpu-class"),
                      "cluster": next(((by_pod[a["attempt_id"]].get("cluster") or kube.cluster_id_or_none(by_pod[a["attempt_id"]].get("region"))) for a in reversed(mine) if a.get("attempt_id") in by_pod), op.get("cluster") if mid == name else None),
                      "reason": ann.get(f"{cfg.LABEL}/fallback") or ann.get(f"{cfg.LABEL}/recovery"),
                      "gpu_seconds": round(sum(measured), 1) if measured else (None if unknown else 0.0), "gpu_seconds_complete": not unknown,
                      "cost": float(ann[f"{cfg.LABEL}/billed"]) if f"{cfg.LABEL}/billed" in ann else None})
        total += sum(measured); complete = complete and not unknown
    root_member = next((c for c in chain if c["id"] == root), None)
    successors = [c for c in chain if c["id"] != root]
    billed = [c["cost"] for c in chain if c["cost"] is not None]
    return {"created_at": (root_member or {}).get("created_at") or min((a["started_at"] for a in all_attempts if a.get("started_at")), default=op.get("created_at")),
            "started_at": min((a["started_at"] for a in all_attempts if a.get("started_at")), default=None),
            "attempts": [{k: v for k, v in a.items() if not k.startswith("_")} for a in all_attempts],
            "chain": chain, "successor_created_at": successors[-1]["created_at"] if successors else None,
            "gpu_seconds": round(total, 1), "gpu_seconds_complete": complete, "cost": round(sum(billed), 6) if billed else None}


def _finalizers(p: Principal, name: str, root: str) -> dict:
    """The application finalizers of an operation (docs/JOBS.md "Finalization by the application"): how many ran
    (`finalize_attempts`) and, while one is QUEUED or RUNNING, the handle shows it: `status FINALIZING`,
    `finalizing_by`, and the finalizer pod's attempt id, epoch and role (the broker's current attempt)."""
    xs = [j for j in jobs.list_ops(p.namespace, p.tenant, None, 500)
          if (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of") == name
          and (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/attempt-role") == "finalize"]
    out: dict = {"finalize_attempts": len(xs)}
    active = [j for j in xs if jobs.normalise(j)["status"] in ("QUEUED", "RUNNING") and not (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/cancelled")]
    if active:
        a = sorted(active, key=lambda j: str(j["metadata"].get("creationTimestamp") or ""))[-1]
        aname = a["metadata"]["name"]
        rows = [r for r in attempts.for_operation(root) if r["operation"] == aname]
        latest = rows[-1] if rows else None
        out.update({"status": "FINALIZING", "finalizing_by": aname, "error": None,
                    "attempt_id": latest.get("pod") if latest else None, "epoch": latest.get("epoch") if latest else None,
                    "attempt_role": "finalize", "cluster": (latest.get("cluster") or kube.cluster_id_or_none(latest.get("region"))) if latest else out.get("cluster")})
    return out


def _operation(p: Principal, job: dict, id: str, region: str | None) -> dict:
    if job.get("_synthetic"):
        return job["_synthetic"]
    name = job["metadata"]["name"]
    records, root = _records(p, job, name, region)
    op = jobs.normalise(job, _price(job), records)
    op.update(attempts.lifecycle(name, root, records))
    if not op.get("cluster") and (job.get("_cluster") or job.get("_region")):
        op["cluster"] = job.get("_cluster") or kube.cluster_id_or_none(job["_region"])   # admitted, no record yet: the admitting cluster's id
    if name != id:
        op["requested_id"] = id             # the caller's handle was superseded; this is its current successor
    if root != name or name != id or (attempts.enabled() and any(r["operation"] != name for r in attempts.for_operation(root))):
        op.update(_chain(p, op, job, root))       # a chain member, a followed handle, or a root with retries/successors on record
    if op["status"] in ("FAILED", "EXPORT_FAILED") and attempts.enabled() and (job["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") == "run":
        op.update(_finalizers(p, name, root))
    if op["status"] in ("EXPORT_FAILED", "FINALIZING", "FAILED") and attempts.enabled():
        # a transfer-only retry or an application finalizer (`<id>-x<n>`) that delivered closes the original (docs/JOBS.md
        # "Attempts and finalization", "Finalization by the application"); a FAILED run only ever has finalizers
        done = [r for r in attempts.for_operation(root) if r["operation"].startswith(f"{name}-x")
                and ((r.get("export_state") or r.get("export")) == "succeeded" or (r.get("result_ref") and r["status"] == "succeeded"))]
        if done:
            op.update({"status": "SUCCEEDED", "export_state": done[-1].get("export_state") or "succeeded", "exported_by": done[-1]["operation"],
                       "result_ref": done[-1].get("result_ref") or op.get("result_ref"), "error": None})   # the delivering attempt's result
    return op


@app.get("/v1/operations/{id}")
def get_operation(id: str, since: int | None = None, p: Principal = Depends(principal)):
    """`since=<revision>`: 304 while nothing about the operation's attempts changed (polling cursor; the live
    pod state is still re-read on every 200)."""
    check_scope(p, "read:operations")
    job, region = _owned(p, id)
    op = _operation(p, job, id, region)
    if since is not None and op.get("revision") and op["revision"] <= since and op["status"] not in ("QUEUED", "RUNNING"):
        return JSONResponse(None, status_code=304)
    return op


@app.get("/v1/operations/{id}/result")
def get_result(id: str, p: Principal = Depends(principal)):
    check_scope(p, "read:results")
    job, region = _owned(p, id)
    op = _operation(p, job, id, region)
    res = {"operation_id": id, "status": op["status"], "region": region, "error": op.get("error"),
           "export_state": op.get("export_state"), "result_ref": op.get("result_ref"), "checkpoint_ref": op.get("checkpoint_ref")}
    if op["status"] in ("QUEUED", "RUNNING"):
        return JSONResponse(res, status_code=409)
    prefix = jobs.output_prefix(job)
    application = (op.get("execution") or {}).get("io_mode") == "application"
    if op["mode"] == "async":
        res["result"] = artifacts.read_json(p.namespace, f"operations/{id}/out/response.json", region or REGION, prefix=prefix)
    res["artifacts"] = artifacts.list_outputs(p.namespace, id, region or REGION, prefix) if op["mode"] == "run" and not application else []
    return res


@app.post("/v1/operations/{id}:export")
def export_again(id: str, p: Principal = Depends(principal)):
    """Transfer-only retry of an EXPORT_FAILED run (managed I/O): a new operation `<id>-x<n>` with the uploader on the
    kept work volume and a main container that does nothing; the simulation is not rerun (docs/JOBS.md)."""
    check_scope(p, "read:results"); check_scope(p, "run:control")
    orig, region = _owned(p, id)
    exported = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of")
    if exported:                          # a previous retry's handle: the retry is of the compute attempt, never of a retry
        orig, region = _owned(p, exported, follow=False)
    op = _operation(p, orig, orig["metadata"]["name"], region)
    if op.get("finalizing_by"):
        # one active finalizer per root: the call is idempotent while it runs
        active, _ = _owned(p, op["finalizing_by"], follow=False)
        return JSONResponse(jobs.normalise(active, _price(active)), status_code=200)
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    finalize = None
    fz = (m.get("job") or {}).get("finalize")
    if op["status"] == "FAILED" and fz and (op.get("execution") or {}).get("io_mode") == "application":
        # the application finalizer (docs/JOBS.md "Finalization by the application"): only after a compute that ended with
        # one of the model's "delivery pending" exit codes; a failed computation is never finalized into a success
        name = orig["metadata"]["name"]
        root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or name
        rows = [r for r in attempts.for_operation(root) if r["operation"] == name and r["status"] == "failed"] if attempts.enabled() else []
        last = rows[-1] if rows else None
        if last is None or last.get("exit_code") not in fz["onExitCodes"]:
            raise HTTPException(409, f"operation {id} is FAILED with exit code {(last or {}).get('exit_code')}; the model finalizes only after exit codes {fz['onExitCodes']} (compute done, delivery pending)")
        finalize = {"spec": fz, "attempt": last}
    elif op["status"] not in ("EXPORT_FAILED", "FINALIZING"):
        raise HTTPException(409, f"operation {id} is {op['status']}; only an EXPORT_FAILED (or still FINALIZING) run is exported again"
                                 + (" (a FAILED application run is finalized only when its model declares job.finalize and the compute ended with a 'delivery pending' exit code)" if op["status"] == "FAILED" else ""))
    job, created = jobs.export_again(p.namespace, orig["metadata"]["name"], _home(orig), m, catalog.region_params(m, region) if region else {}, finalize=finalize)
    return JSONResponse(jobs.normalise(job, _price(job)), status_code=202 if created else 200)


@app.post("/internal/attempts", include_in_schema=False)
async def record_attempt(request: Request, authorization: str = Header(default="")):
    """The uploader of a run pod reports its attempt (services/jobs/bin/upload.sh) with the pod's projected
    ServiceAccount token; the token's TokenReview names the pod, so a record can only describe the pod it comes
    from (services/api/attempts.py). 501 on an API without the fleet database (the bucket records stand)."""
    if not attempts.enabled():
        raise HTTPException(501, "this API keeps no attempt records (no fleet database)")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "a JSON object is expected")
    region, ns, pod = await asyncio.to_thread(attempts.verify_runner_token, authorization[7:].strip())
    operation = str(body.get("operation") or "")
    try:
        job, _ = await asyncio.to_thread(jobs.find, ns, operation)      # the manager's Job for a fleet run (the worker holds a mirror)
        body["root"] = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or operation
        body["role"] = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/attempt-role") or "run"    # the platform's statement, never the pod's
        # the canonical cluster id: the Workload's admitting cluster for a fleet run, else the worker the token came from
        body["cluster"] = job.get("_cluster") or _cluster_of(region)
    except HTTPException:
        body["root"] = operation                 # the Job is already gone (TTL, cancel): the record still counts
        body["role"] = "run"
        body["cluster"] = _cluster_of(region)
    return await asyncio.to_thread(attempts.record, region, ns, pod, body)


def _cluster_of(region: str) -> str:
    """The configured cluster id of the worker a record's token came from; an ambiguous map (two worker clusters in
    one region and no admitting cluster known) is refused rather than stamped wrong."""
    try:
        return kube.cluster_id(region)
    except LookupError as e:
        raise HTTPException(409, f"attempt record not stamped: {e}")


def _home(job: dict) -> str:
    """The cluster the operation's Job object lives in: the manager for fleet-placed runs."""
    return REGION if jobs.is_manager_job(job) else (job.get("_region") or REGION)


@app.post("/v1/operations/{id}:cancel")
def cancel(id: str, p: Principal = Depends(principal)):
    check_scope(p, "run:control")
    job, region = _owned(p, id)
    if job.get("_synthetic"):
        raise HTTPException(409, f"operation {id} is over; nothing to cancel")
    current = job["metadata"]["name"]
    op = _operation(p, job, id, region)
    if op.get("finalizing_by"):           # the root's handle: a cancel stops its active finalizer, the root stays as it is
        job, _ = _owned(p, op["finalizing_by"], follow=False)
        current = job["metadata"]["name"]
    attempts.mark_cancelled(current)       # a late "succeeded" from the cancelled attempt is refused from now on
    op = jobs.normalise(jobs.cancel(p.namespace, current, _home(job)))
    if op["status"] in ("QUEUED", "RUNNING"):
        op["status"] = "CANCELLED"   # the Job controller converges within seconds (pods get SIGTERM)
    return op


@app.post("/v1/operations/{id}:resume")
def resume(id: str, req: ResumeRequest | None = None, p: Principal = Depends(principal),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """A new operation `<id>-r<n>` in the same region on the same work volume (checkpoints); for a
    FAILED or CANCELLED run whose volume still exists (docs/JOBS.md). Preemptions resume on their own.
    A fleet-placed run is resumed through the manager, pinned to the worker that ran it. With a
    `checkpoint_ref` (a portable checkpoint the application restores itself) the volume and the region are not
    needed: the run is placed again, optionally on another `gpu_class` (its image), docs/JOBS.md "Execution options"."""
    check_scope(p, "run:submit"); check_scope(p, "run:control")
    orig, region = _owned(p, id)
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    req = req or ResumeRequest()
    if req.gpu_class and req.gpu_class not in jobs.gpu_classes(m):
        raise HTTPException(400, f"gpu_class {req.gpu_class} is not one of {jobs.gpu_classes(m)}")
    root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    pointer = (attempts.latest_checkpoint(root) or {}).get("checkpoint_ref") if attempts.enabled() and not req.checkpoint_ref else None
    if req.gpu_class and not req.checkpoint_ref and not pointer:
        raise HTTPException(400, "another gpu_class needs a checkpoint_ref (none was acknowledged for this run): the work volume's checkpoints belong to the original class")
    placement = {"profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]} if jobs.is_manager_job(orig) else None
    if req.gpu_class and placement:
        placement["classes"] = [req.gpu_class]
    job, created = jobs.resume(p.namespace, orig["metadata"]["name"], _home(orig), m, catalog.region_params(m, region) if region and not req.checkpoint_ref and not req.gpu_class else {}, placement,
                               checkpoint_ref=req.checkpoint_ref, gpu_class=req.gpu_class, idem=idempotency_key, pointer=pointer)
    return JSONResponse(jobs.normalise(job, _price(job)), status_code=202 if created else 200)


# ---- bindings: confidential inputs for runs (docs/JOBS.md "Execution options") ----
@app.put("/v1/bindings/{ref}")
def put_binding(ref: str, req: BindingRequest, p: Principal = Depends(principal)):
    check_scope(p, "artifacts:write")
    return execution.put_binding(p.namespace, p.tenant, ref, req.data)


@app.get("/v1/bindings")
def list_bindings(p: Principal = Depends(principal)):
    return execution.list_bindings(p.namespace)


@app.delete("/v1/bindings/{ref}", status_code=204)
def delete_binding(ref: str, p: Principal = Depends(principal)):
    if not execution.delete_binding(p.namespace, ref):
        raise HTTPException(404, f"binding {ref}: not found")
    return None


@app.post("/v1/artifacts/uploads", status_code=201)
def upload(req: UploadRequest, p: Principal = Depends(principal)):
    check_scope(p, "artifacts:write")
    return artifacts.presign_upload(p.namespace, req.filename, req.content_type, min(req.expires_s, 7 * 86400), req.region)


@app.get("/v1/artifacts/{uri:path}")
def download(uri: str, p: Principal = Depends(principal)):
    return artifacts.presign_download(p.namespace, urllib.parse.unquote(uri))


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- endpoints (read, scale) and keys: need a key with metadata.role == admin for writes ----
def admin(p: Principal = Depends(principal)) -> Principal:
    if (p.info.get("metadata") or {}).get("role") != "admin":
        raise HTTPException(403, "endpoint scaling and key management need a key with metadata.role=admin")
    return p


def _deployed(m: dict, region: str) -> dict | None:
    """The deployed object of an endpoint in a region: the KServe InferenceService, or the LeaderWorkerSet of a
    multi-node endpoint (services/api/models.py `nodes`)."""
    read = kube.lws if int(m.get("nodes") or 1) > 1 else kube.isvc
    return read(m["k8s_name"], m["namespace"], region)


def _endpoint_model(eid: str) -> dict:
    """The model behind an endpoint object name: the model's own, or one of its variants' (`<id>-<variant>`)."""
    for m in catalog.all_models().values():
        if m["mode"] == "run":
            continue
        if m["k8s_name"] == eid or any(v["object"] == eid for c in (m.get("deployments") or {}) for v in models.variants(m, c)):
            return m
    raise HTTPException(404, f"endpoint {eid} not found")


def _variants_public(m: dict, region: str) -> list[dict]:
    """The variants of an endpoint on a region's cluster with their live state (one line each)."""
    cid = next((c for c in (m.get("deployments") or {}) if kube.cluster_region(c) == region), None)
    if cid is None:
        return []
    vs = models.variants(m, cid)
    if len(vs) == 1:
        return []
    out = []
    for v in vs:
        try:
            st = kube.endpoint_status(v["object"], m["namespace"], region)
        except (HTTPException, ApiException):
            st = {"status": "unavailable", "replicas_ready": None}
        out.append({"name": v["name"], "id": v["object"], "pool": v.get("pool"), "min": v.get("min"), "max": v.get("max"), "image": v.get("image"),
                    "status": st.get("status"), "replicas_ready": st.get("replicas_ready")})
    return out


@app.get("/v1/endpoints")
def list_endpoints(region: str | None = None, p: Principal = Depends(principal)):
    check_scope(p, "read:catalog")
    served_regions = kube.regions()
    if region and region not in served_regions:
        raise HTTPException(400, f"region {region} is not served by this API")
    selected_region = region
    out = []
    for m in catalog.all_models().values():
        if not allows_model(p, m["name"]):
            continue
        if m["mode"] == "run":
            continue
        for region in ([selected_region] if selected_region else served_regions):
            if region not in m["regions"]:
                continue
            try:
                i = _deployed(m, region)
                if i:
                    out.append({**ep.to_public(m, i, region), **({"variants": vs, "routing": m.get("routing")} if (vs := _variants_public(m, region)) else {})})
            except (HTTPException, ApiException):
                out.append({"id": m["k8s_name"], "name": m["display_name"], "model": m["name"], "region": region,
                            "status": "unavailable", "replicas_ready": None, "gpu": m.get("gpu"),
                            "managed_by": m.get("managed_by"), "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke"})
    return out


@app.get("/v1/endpoints/{id}")
def get_endpoint(id: str, region: str | None = None, p: Principal = Depends(principal)):
    check_scope(p, "read:catalog")
    m = _endpoint_model(id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    i = _deployed(m, region) or {}
    if not i:
        raise HTTPException(404, f"endpoint {id} not deployed in {region}")
    return {**ep.to_public(m, i, region), **({"variants": vs, "routing": m.get("routing")} if (vs := _variants_public(m, region)) else {})}


@app.patch("/v1/endpoints/{id}")
def update_endpoint(id: str, req: ScalingRequest, region: str | None = None, p: Principal = Depends(admin)):
    """Persist desired scaling on API-managed definitions and reconcile every deployment."""
    m = _endpoint_model(id)
    if m.get("managed_by") != "api" or not m.get("spec"):
        raise HTTPException(403, "This endpoint is managed by the fleet configuration. Update its source definition.")
    region = _endpoint_region(m, region)
    spec = {**m["spec"], "scaling": {**m["spec"].get("scaling", {})}}
    if req.scaling is not None:
        spec["scaling"].update(req.scaling.model_dump(exclude_none=True))
    if req.timeout_s is not None:
        spec["timeout_s"] = req.timeout_s
    # target_concurrency is the per-replica scale target the endpoint reports (Knative's scaleTarget): the replica
    # concurrency limit under concurrency_utilization, the requests/s target under requests_per_second
    rate = spec["scaling"].get("metric") == "requests_per_second"
    for key, target in (("min_replicas", "min"), ("max_replicas", "max"), ("scale_to_zero_after_s", "idle_s"),
                        ("target_concurrency", "target" if rate else "container_concurrency")):
        if (value := getattr(req, key)) is not None:
            spec["scaling"][target] = value
    if spec["scaling"].get("min", 0) > spec["scaling"].get("max", 1):
        raise HTTPException(422, "Minimum replicas cannot exceed maximum replicas.")
    _write_model(spec, p, replace=True)
    return get_endpoint(id, region, p)


def _endpoint_region(m: dict, region: str | None) -> str:
    selected = region or (REGION if REGION in m["regions"] else m["regions"][0])
    if selected not in kube.regions():
        raise HTTPException(400, f"region {selected} is not served by this API")
    if selected not in m["regions"]:
        raise HTTPException(404, "Endpoint not deployed in this region")
    return selected


@app.get("/v1/endpoints/{id}/metrics")
async def endpoint_metrics(id: str, region: str | None = None, range: str = "1h", end: float | None = None,
                           p: Principal = Depends(principal)):
    check_scope(p, "read:metrics")
    m = await asyncio.to_thread(_endpoint_model, id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    return await monitoring.metrics("endpoint", m["namespace"], m["k8s_name"], region, range, end)


@app.get("/v1/endpoints/{id}/logs")
async def endpoint_logs(id: str, region: str | None = None, range: str = "1h", search: str = Query("", max_length=200),
                        limit: int = Query(300, ge=1, le=1000), end: float | None = None, p: Principal = Depends(principal)):
    check_scope(p, "read:logs")
    m = await asyncio.to_thread(_endpoint_model, id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    return await monitoring.logs("endpoint", m["namespace"], m["k8s_name"], region, range, search, limit, end)


def _monitored(p: Principal, id: str) -> tuple[str | None, str]:
    """(region, kind) of an operation for its metrics and logs: the Job's region (a fleet-placed run: the worker that
    admitted it, None while it waits). Every query is scoped to the caller's own namespace."""
    job, region = jobs.find(p.namespace, id)
    if job["metadata"].get("labels", {}).get("serverless2.nebius/tenant") != p.tenant:
        raise HTTPException(404, f"operation {id}: not found")    # someone else's run is never served
    return region, "jobset" if jobs.is_jobset(job) else "job"


@app.get("/v1/operations/{id}/metrics")
async def operation_metrics(id: str, range: str = "1h", end: float | None = None, p: Principal = Depends(principal)):
    check_scope(p, "read:metrics")
    region, _ = await asyncio.to_thread(_monitored, p, id)
    return await monitoring.metrics("job", p.namespace, id, region or REGION, range, end)


@app.get("/v1/operations/{id}/logs")
async def operation_logs(id: str, range: str = "1h", search: str = Query("", max_length=200),
                         limit: int = Query(300, ge=1, le=1000), end: float | None = None, p: Principal = Depends(principal)):
    check_scope(p, "read:logs")
    region, kind = await asyncio.to_thread(_monitored, p, id)
    return await monitoring.logs(kind, p.namespace, id, region or REGION, range, search, limit, end)


def _key_public(i: dict, tenant: str) -> dict:
    exhausted = i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]
    return {"id": i.get("key_alias"), "key_preview": i.get("key_name"), "alias": i.get("key_alias"), "tenant": tenant, "budget": i.get("max_budget"),
            "spend": i.get("spend", 0), "models": i.get("models", []), "created_at": i.get("created_at"), "expires_at": i.get("expires"),
            "status": "exhausted" if exhausted else "active", "role": (i.get("metadata") or {}).get("role", "user")}


def _fleet_only() -> None:
    if not db.enabled():
        raise HTTPException(409, "keys live in the fleet database (DATABASE_URL), which this API does not have")


@app.get("/v1/keys")
def list_keys(p: Principal = Depends(admin)):
    _fleet_only()
    return [_key_public(k, p.tenant) for k in keys.list_tenant(p.tenant)]


@app.post("/v1/keys", status_code=201)
def create_key(req: KeyRequest, p: Principal = Depends(admin)):
    """A user key of the admin's tenant (alias `<tenant>-<alias>`), with the admin key's pass-through routes; the
    value is returned once and mirrored into LiteLLM (services/api/keys.py)."""
    _fleet_only()
    meta = {"allowed_passthrough_routes": (p.info.get("metadata") or {}).get("allowed_passthrough_routes", [])}
    value, i = keys.create(p.tenant, f"{p.tenant}-{req.alias}", req.budget, req.models, req.expires_days, metadata=meta, by=p.info.get("key_alias"))
    return {**_key_public(i, p.tenant), "key": value}


class KeyImportRequest(BaseModel):
    keys: list[dict]
    dry_run: bool = False


@app.post("/v1/keys:import")
def import_keys(req: KeyImportRequest, p: Principal = Depends(admin)):
    """Pre-hashed keys of the admin's tenant (docs/API.md "Importing keys"): idempotent, all-or-nothing, nothing
    overwritten, conflicts refused (409 with the list), unknown scopes block the key; `dry_run` classifies only."""
    _fleet_only()
    return keys.import_records(req.keys, p.tenant, p.info.get("key_alias") or p.info.get("key_name"), req.dry_run)


@app.patch("/v1/keys/{alias}")
def update_key(alias: str, req: KeyUpdate, p: Principal = Depends(admin)):
    """Change a key's budget, allowed models or expiry; the spend ledger is untouched (docs/RELIABILITY.md)."""
    _fleet_only()
    if req.budget is None and req.models is None and req.expires_days is None:
        raise HTTPException(400, "nothing to change: pass budget, models or expires_days")
    i = keys.update(p.tenant, alias, req.budget, req.models, req.expires_days)
    if i is None:
        raise HTTPException(404, f"key {alias} not found")
    forget_hash(i["token"])                  # a cached principal would keep the old budget for AUTH_CACHE_S
    return _key_public(i, p.tenant)


@app.delete("/v1/keys/{alias}", status_code=204)
def revoke_key(alias: str, p: Principal = Depends(admin)):
    _fleet_only()
    if not keys.revoke(p.tenant, alias):
        raise HTTPException(404, f"key {alias} not found")
