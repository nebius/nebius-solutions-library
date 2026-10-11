"""Nebius Serverless 2.0 customer API (docs/API.md). Run: uvicorn app:app"""
import asyncio, re, time, urllib.parse, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
import logging
import re
import artifacts, attempts, billing, catalog, config as cfg, db, execution, jobs, kube, models, monitoring, placement as placing
log = logging.getLogger("api")
import endpoints as ep
from execution import BindingRequest, ExecutionSpec
from auth import Principal, allows_model, check_budget, check_model, forget, principal
from config import FLEET_MANAGER, GRAFANA_URLS, LITELLM_MASTER_KEY, LITELLM_URL, PUBLIC_API_URL, REGION, REGION_API_URLS, SYNC_TIMEOUT_S
from resilience import retry_http

def check_placeholders() -> None:
    """Refuse to start with a manifest placeholder (clusters/common/manifests/api/api.yaml carries
    `example.invalid` hostnames that the platform stage or the cluster overlay must replace): a placeholder
    would end up in every invoke link and Grafana link this API hands out."""
    bad = [name for name, value in (("PUBLIC_API_URL", cfg.PUBLIC_API_URL), ("GRAFANA_URLS", ",".join(cfg.GRAFANA_URLS.values())))
           if "example.invalid" in value]
    if bad:
        raise RuntimeError(f"placeholder hostnames in {', '.join(bad)}: set them per cluster (stack/platform/stage.tf)")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """The control API owns the fleet database schema (services/api/db.py MIGRATIONS), applied at startup."""
    check_placeholders()
    if db.enabled():
        log.info("database schema version %s", db.migrate())
        log.info("queued %s model(s) for a re-render after start", db.requeue_all())
    task = asyncio.create_task(_reconcile_loop()) if db.enabled() else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.11.12", lifespan=_lifespan)


class InvokeRequest(BaseModel):
    name: str | None = None
    mode: str | None = None
    input: dict = Field(default_factory=dict)
    region: str | None = None
    priority: str | None = None
    timeout_s: int | None = None
    execution: ExecutionSpec | None = None     # docs/JOBS.md "Execution options" (runs only)


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
    if FLEET_MANAGER:        # the endpoints live on the workers (deployments); their live state is the regional API's
        statuses = []
        for region in m["regions"]:
            try:
                st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
            except (HTTPException, ApiException):
                st = {"status": "unavailable", "replicas_ready": None}
            statuses.append({"region": region, **st})
        return catalog.to_public(m, statuses)
    return catalog.to_public(m, [{"region": REGION, **kube.endpoint_status(m["k8s_name"], m["namespace"])}])


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
                             "api_url": PUBLIC_API_URL if region == REGION else REGION_API_URLS.get(region),
                             "grafana_url": GRAFANA_URLS.get("control" if control else region) or GRAFANA_URLS.get(region),
                             **kube.cluster_summary(region)})
        out["clusters"] = clusters
    return out


@app.get("/v1/models")
def list_models(p: Principal = Depends(principal)):
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
    """Model definitions are written where the fleet database is: the control API (DATABASE_URL)."""
    if not db.enabled():
        hint = f" ({PUBLIC_API_URL})" if PUBLIC_API_URL else ""
        raise HTTPException(409, f"this API ({REGION}) has no model database; define models on the fleet's API{hint}")


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
                for region in models.copy_regions():
                    models.save(entry, region) if entry else models.remove(mid, region)
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
            if wait is None or not jobs.is_manager_job(job) or jobs.finished(job) or ann.get(f"{cfg.LABEL}/successor"):
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


async def _reconcile_loop():
    while True:
        try:
            await asyncio.to_thread(idle_shutdown)
            await asyncio.to_thread(fallback_switch)
            await asyncio.to_thread(reconcile_models)
        except Exception:
            log.exception("model reconciliation unavailable")
        await asyncio.sleep(15)


def _write_model(spec: dict, p: Principal, replace: bool, expected: int | None = None) -> dict:
    """Commit desired state and history, then attempt its durable reconciliation."""
    _writes_here()
    entry = models.to_entry(spec, managed_by="api")
    if entry.get("spec", {}).get("scaling", {}).get("metric") in ("concurrency_utilization", "requests_per_second"):
        # Store the same policy we render: discard obsolete utilization factors and
        # retain the normalized units/defaults, including unlimited RPS concurrency.
        spec = {**spec, "scaling": entry["spec"]["scaling"]}
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
    p = await principal(key)
    m = catalog.get(model) or _endpoint_model(model)
    check_model(p, m["name"])
    check_budget(p)
    if m["mode"] == "run":
        raise HTTPException(404, "not an endpoint")
    if int(m.get("nodes") or 1) > 1:
        kube.stamp_last_request(m["k8s_name"], m["namespace"])       # the control API's idle shutdown reads it
    return JSONResponse({}, headers={"x-serverless2-tenant": p.tenant, "x-serverless2-key-alias": str(p.info.get("key_alias") or "")})


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
    region = req.region or (REGION if REGION in m["regions"] else m["regions"][0])
    if region not in m["regions"]:
        raise HTTPException(400, f"model {model} is deployed in {m['regions']}, not {region}")
    if mode != "run" and region != REGION:
        if region in REGION_API_URLS:        # the control cluster hosts no endpoint: forward to the region's API
            return await _forward(model, req, p, idempotency_key, REGION_API_URLS[region])
        raise HTTPException(400, f"sync/async calls for {region} go to that region's API; this one serves {REGION}")
    if req.execution and mode != "run":
        raise HTTPException(400, "execution options apply to runs only")
    if mode == "sync":
        return await _sync(m, req, p)
    # admission: Kueue (profile queues) bounds concurrent runs; LiteLLM rpm/budget bound async calls;
    # the gateway BackendTrafficPolicy caps request rates. Nothing is counted here.
    name = jobs.op_name(p.tenant, model, idempotency_key)
    timeout = req.timeout_s          # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret = None
    ex = execution.resolve(req.execution, p.namespace, m) if mode == "run" else None
    if ex and ex.get("placement", {}).get("allowed_clusters") and region and region not in ex["placement"]["allowed_clusters"]:
        raise HTTPException(400, f"region {region} is not in allowed_clusters {ex['placement']['allowed_clusters']}")
    if mode == "async":
        output_prefix = f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        job, secret = jobs.build_async(name, m, req.input, p.tenant, p.key, req.name, idempotency_key, timeout, output_prefix)
    elif FLEET_MANAGER:
        _guard_run_input(m, req, p, REGION, ex)
        job = _fleet_run(m, req, p, name, timeout, idempotency_key, ex)
        execution.deliver(ex, name, name, p.namespace, p.tenant, [r for r in job["spec"]["template"]["metadata"].get("annotations", {}).get(f"{cfg.LABEL}/regions", "").split(",") if r] or [region or REGION])
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
        execution.deliver(ex, name, name, p.namespace, p.tenant, [region])
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
                          billing.key_hash(p.key), placement=placement, gpu_class=_class_for(m, req, region, p.tenant, ex), ex=ex)


def _class_for(m: dict, req: InvokeRequest, region: str | None, tenant: str | None = None, ex: dict | None = None) -> str | None:
    """A run class with per-GPU-class images gets its class at submission (docs/SCHEDULING.md "Per-GPU images for
    runs"): the dispatcher's best candidate among the model's classes and regions (the caller's region when
    given). None for a class with one image: Kueue may then switch class at queue time as before."""
    if not jobs.class_images(m):
        return None
    gpus = (m.get("job") or {}).get("gpu", 1)
    try:
        gpus = int(gpus)
    except (TypeError, ValueError):           # a parameter placeholder: one GPU for the ranking
        gpus = int(req.input.get("gpus", 1) or 1) if isinstance(req.input.get("gpus", 1), (int, str)) and str(req.input.get("gpus", 1)).isdigit() else 1
    pools = kube.fleet().get("pools") or {}
    available = sorted({p.get("gpu_class") for p in pools.values() if p.get("gpu_class")}) or None
    if ex and (ex.get("placement") or {}).get("gpu_preference"):          # the caller's order narrows the model's
        available = [c for c in ex["placement"]["gpu_preference"] if available is None or c in available]
    cls, how = placing.choose_class(m, region, gpus, available, tenant)
    log.info("%s: GPU class %s chosen by %s", m["name"], cls, how)
    return cls


async def _forward(model: str, req: InvokeRequest, p: Principal, idempotency_key: str | None, base: str):
    cfg.refresh_trust()
    """Same request to the regional API (which validates the key, serves the endpoint and bills); the
    answer is returned as is. Covers the UI and clients that only know the control API's hostname."""
    headers = {"Authorization": f"Bearer {p.key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=(req.timeout_s or SYNC_TIMEOUT_S) + 30) as c:
            r = await c.post(f"{base}/v1/models/{model}:invoke", json=req.model_dump(exclude_none=True), headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, f"regional API {base} did not answer within {(req.timeout_s or SYNC_TIMEOUT_S) + 30}s")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"regional API {base} unreachable: {e}")
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:500]}
    return JSONResponse(body, status_code=r.status_code)


async def _sync(m: dict, req: InvokeRequest, p: Principal):
    """Proxy to the endpoint. Via LiteLLM's pass-through route when the model has one (budget,
    spend and the per-call price are enforced there), else straight to the predictor."""
    if int(m.get("nodes") or 1) > 1:
        st = kube.endpoint_status(m["k8s_name"], m["namespace"], nodes=int(m["nodes"]))
        kube.stamp_last_request(m["k8s_name"], m["namespace"])     # a direct proxy call bypasses the gateway's authorize hook
        if st["status"] != "ready":
            raise HTTPException(503, f"endpoint {m['name']} is {st['status']} ({m['nodes']} nodes): a multi-node endpoint is started with "
                                     f"PATCH /v1/endpoints/{m['k8s_name']} {{\"scaling\": {{\"min\": 1}}}} (or the console's Start button) and takes "
                                     f"minutes to load; it stops again after scaling.idle_s without a request")
    if m.get("litellm_route"):
        url, headers = LITELLM_URL + m["litellm_route"], {"Authorization": f"Bearer {p.key}"}
    else:
        v = _variant_for(m, req.input, models.cluster_id())
        host = m["endpoint_host"] if v["object"] == m["k8s_name"] else f"http://{v['object']}-predictor.{m['namespace']}.svc.cluster.local"
        url, headers = host + m["endpoint_path"], {}
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
        body = r.text
    op = {"id": f"sync-{uuid.uuid4().hex[:12]}", "name": req.name, "model": m["name"], "mode": "sync", "region": REGION,
          "status": "SUCCEEDED" if ok else "FAILED", "tenant": p.tenant, "created_at": started, "started_at": started,
          "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_s": round(time.time() - t0, 3),
          "attempts": [{"index": 1, "status": "SUCCEEDED" if ok else "FAILED", "reason": None if ok else f"HTTP {r.status_code}"}],
          "input": req.input, "cost": m.get("price_per_call") if ok else None, "error": None if ok else str(body)[:500]}
    return JSONResponse({"result": body, "operation": op}, status_code=200 if ok else (r.status_code if r.status_code in (400, 401, 402, 403, 404, 413, 429) else 502))


def _price(job: dict) -> float | None:
    m = catalog.get(job["metadata"].get("labels", {}).get("serverless2.nebius/model", "")) or {}
    return m.get("price_per_call")


@app.get("/v1/operations")
def list_operations(p: Principal = Depends(principal), limit: int = Query(50, le=500), model: str | None = None):
    return [jobs.normalise(j, _price(j)) for j in jobs.list_ops(p.namespace, p.tenant, model, limit)]


def _owned(p: Principal, id: str, follow: bool = True) -> tuple[dict, str]:
    """The operation's Job. A handle that capacity fallback superseded resolves to its current successor (the
    original stays readable, docs/SCHEDULING.md); an operation whose Job expired is served from its durable rows."""
    current = id
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
                raise
            return {"_synthetic": synthetic, "metadata": {"name": current, "labels": {"serverless2.nebius/tenant": p.tenant, "serverless2.nebius/mode": "run"}, "annotations": {}}}, synthetic.get("region")
        if job["metadata"].get("labels", {}).get("serverless2.nebius/tenant") != p.tenant:
            raise HTTPException(404, f"operation {id}: not found")
        nxt = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/successor")
        if follow and nxt and nxt != current:
            current = nxt
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


def _operation(p: Principal, job: dict, id: str, region: str | None) -> dict:
    if job.get("_synthetic"):
        return job["_synthetic"]
    name = job["metadata"]["name"]
    records, root = _records(p, job, name, region)
    op = jobs.normalise(job, _price(job), records)
    op.update(attempts.lifecycle(name, root, records))
    if name != id:
        op["requested_id"] = id             # the caller's handle was superseded; this is its current successor
    if op["status"] in ("EXPORT_FAILED", "FINALIZING") and attempts.enabled():
        # a transfer-only retry (`<id>-x<n>`) that delivered closes the original (docs/JOBS.md "Attempts and finalization")
        done = [r for r in attempts.for_operation(root) if r["operation"].startswith(f"{name}-x")
                and ((r.get("export_state") or r.get("export")) == "succeeded" or (r.get("result_ref") and r["status"] == "succeeded"))]
        if done:
            op.update({"status": "SUCCEEDED", "export_state": done[-1].get("export_state") or "succeeded", "exported_by": done[-1]["operation"],
                       "result_ref": op.get("result_ref") or done[-1].get("result_ref"), "error": None})
    return op


@app.get("/v1/operations/{id}")
def get_operation(id: str, since: int | None = None, p: Principal = Depends(principal)):
    """`since=<revision>`: 304 while nothing about the operation's attempts changed (polling cursor; the live
    pod state is still re-read on every 200)."""
    job, region = _owned(p, id)
    op = _operation(p, job, id, region)
    if since is not None and op.get("revision") and op["revision"] <= since and op["status"] not in ("QUEUED", "RUNNING"):
        return JSONResponse(None, status_code=304)
    return op


@app.get("/v1/operations/{id}/result")
def get_result(id: str, p: Principal = Depends(principal)):
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
    orig, region = _owned(p, id)
    op = _operation(p, orig, id, region)
    if op["status"] not in ("EXPORT_FAILED", "FINALIZING"):
        raise HTTPException(409, f"operation {id} is {op['status']}; only an EXPORT_FAILED (or still FINALIZING) run is exported again")
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    job, created = jobs.export_again(p.namespace, orig["metadata"]["name"], _home(orig), m, catalog.region_params(m, region) if region else {})
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
    except HTTPException:
        body["root"] = operation                 # the Job is already gone (TTL, cancel): the record still counts
    return await asyncio.to_thread(attempts.record, region, ns, pod, body)


def _home(job: dict) -> str:
    """The cluster the operation's Job object lives in: the manager for fleet-placed runs."""
    return REGION if jobs.is_manager_job(job) else (job.get("_region") or REGION)


@app.post("/v1/operations/{id}:cancel")
def cancel(id: str, p: Principal = Depends(principal)):
    job, _ = _owned(p, id)
    if job.get("_synthetic"):
        raise HTTPException(409, f"operation {id} is over; nothing to cancel")
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
    ex = jobs.normalise(job).get("execution")
    if ex and ex.get("private_input_ref"):
        root = job["metadata"]["annotations"].get("serverless2.nebius/resumed-from") or id
        regions = [r for r in job["spec"]["template"]["metadata"].get("annotations", {}).get(f"{cfg.LABEL}/regions", "").split(",") if r] or [job["metadata"]["labels"].get(f"{cfg.LABEL}/region") or REGION]
        execution.deliver(ex, job["metadata"]["name"], root, p.namespace, p.tenant, regions)
    return JSONResponse(jobs.normalise(job, _price(job)), status_code=202 if created else 200)


# ---- bindings: confidential inputs for runs (docs/JOBS.md "Execution options") ----
@app.put("/v1/bindings/{ref}")
def put_binding(ref: str, req: BindingRequest, p: Principal = Depends(principal)):
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
    if req.target_concurrency is not None and spec["scaling"].get("metric") in ("concurrency_utilization", "requests_per_second"):
        raise HTTPException(422, "Use scaling.target for metric-specific targets; target_concurrency is only valid for legacy raw policies.")
    if req.timeout_s is not None:
        spec["timeout_s"] = req.timeout_s
    for key, target in (("min_replicas", "min"), ("max_replicas", "max"), ("target_concurrency", "target"), ("scale_to_zero_after_s", "idle_s")):
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


async def _remote_monitoring(p: Principal, region: str, path: str, params: dict):
    cfg.refresh_trust()
    url = REGION_API_URLS.get(region)
    if not url:
        if region not in kube.regions():
            # a region that left the fleet (its cluster, Prometheus and Loki with it): the operation's record stays,
            # its metrics and logs are gone; the attempt records and outputs in the tenant bucket remain (/result)
            raise HTTPException(410, f"Region {region} is no longer part of this fleet: the metrics and logs of this operation lived "
                                     f"on its cluster and are gone. Its attempt records and outputs in the bucket are still there (result).")
        raise HTTPException(503, "Monitoring for this region is not connected to the fleet API.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url.rstrip("/") + path, params={k: v for k, v in params.items() if v is not None},
                                        headers={"Authorization": f"Bearer {p.key}"})
        if response.status_code in (401, 403, 404, 422):
            raise HTTPException(response.status_code, "Resource monitoring is not available for this request.")
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Monitoring for this region is temporarily unavailable.")


@app.get("/v1/endpoints/{id}/metrics")
async def endpoint_metrics(id: str, region: str | None = None, range: str = "1h", end: float | None = None,
                           p: Principal = Depends(principal)):
    m = _endpoint_model(id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    monitoring.window(range, end)
    if region != REGION:
        return await _remote_monitoring(p, region, f"/v1/endpoints/{urllib.parse.quote(id, safe='')}/metrics", {"range": range, "end": end})
    return await monitoring.metrics("endpoint", m["namespace"], m["k8s_name"], region, range, end)


@app.get("/v1/endpoints/{id}/logs")
async def endpoint_logs(id: str, region: str | None = None, range: str = "1h", search: str = Query("", max_length=200),
                        limit: int = Query(300, ge=1, le=1000), end: float | None = None, p: Principal = Depends(principal)):
    m = _endpoint_model(id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    monitoring.window(range, end)
    if region != REGION:
        return await _remote_monitoring(p, region, f"/v1/endpoints/{urllib.parse.quote(id, safe='')}/logs",
                                        {"range": range, "search": search, "limit": limit, "end": end})
    return await monitoring.logs("endpoint", m["namespace"], m["k8s_name"], region, range, search, limit, end)


def _monitored(p: Principal, id: str) -> tuple[str | None, str]:
    """(region, kind) of an operation for its metrics and logs. A worker's Job is deleted when the run completes
    (MultiKueue), so a regional API may no longer hold it while Prometheus and Loki still do: the fleet API has
    verified ownership before forwarding, and every query is scoped to the caller's own namespace anyway, so a
    missing Job means "this region, either kind" rather than 404 (the console showed "not available" for every
    finished run, 2026-10-10)."""
    try:
        job, region = jobs.find(p.namespace, id)
    except HTTPException as e:
        if e.status_code == 404 and not FLEET_MANAGER and re.fullmatch(r"op-[a-z0-9-]{3,80}", id):
            return REGION, "run"                            # absent here: completed; the queries stay in p.namespace
        raise
    if job["metadata"].get("labels", {}).get("serverless2.nebius/tenant") != p.tenant:
        raise HTTPException(404, f"operation {id}: not found")    # someone else's run is never served
    return region, "jobset" if jobs.is_jobset(job) else "job"


@app.get("/v1/operations/{id}/metrics")
async def operation_metrics(id: str, range: str = "1h", end: float | None = None, p: Principal = Depends(principal)):
    region, _ = _monitored(p, id)
    monitoring.window(range, end)
    if region and region != REGION:
        return await _remote_monitoring(p, region, f"/v1/operations/{urllib.parse.quote(id, safe='')}/metrics", {"range": range, "end": end})
    return await monitoring.metrics("job", p.namespace, id, region or REGION, range, end)


@app.get("/v1/operations/{id}/logs")
async def operation_logs(id: str, range: str = "1h", search: str = Query("", max_length=200),
                         limit: int = Query(300, ge=1, le=1000), end: float | None = None, p: Principal = Depends(principal)):
    region, kind = _monitored(p, id)
    monitoring.window(range, end)
    if region and region != REGION:
        return await _remote_monitoring(p, region, f"/v1/operations/{urllib.parse.quote(id, safe='')}/logs",
                                        {"range": range, "search": search, "limit": limit, "end": end})
    return await monitoring.logs(kind, p.namespace, id, region or REGION, range, search, limit, end)


async def _litellm(method: str, path: str, **kw):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await retry_http(lambda: c.request(method, LITELLM_URL + path, headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}, **kw))
    if r.status_code >= 300:
        raise HTTPException(502, f"litellm {path}: {r.status_code} {r.text[:200]}")
    return r.json()


def _key_public(i: dict, tenant: str) -> dict:
    exhausted = i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]
    return {"id": i.get("key_alias"), "key_preview": i.get("key_name"), "alias": i.get("key_alias"), "tenant": tenant, "budget": i.get("max_budget"),
            "spend": i.get("spend", 0), "models": i.get("models", []), "created_at": i.get("created_at"), "expires_at": i.get("expires"),
            "status": "exhausted" if exhausted else "active", "role": (i.get("metadata") or {}).get("role", "user")}


@app.get("/v1/keys")
async def list_keys(p: Principal = Depends(admin)):
    data = await _litellm("GET", "/key/list", params={"return_full_object": "true", "size": 100})
    return [_key_public(k, p.tenant) for k in data.get("keys", []) if isinstance(k, dict) and (k.get("metadata") or {}).get("tenant") == p.tenant]


@app.post("/v1/keys", status_code=201)
async def create_key(req: KeyRequest, p: Principal = Depends(admin)):
    meta = {"tenant": p.tenant, "allowed_passthrough_routes": (p.info.get("metadata") or {}).get("allowed_passthrough_routes", [])}
    body = {"key_alias": f"{p.tenant}-{req.alias}", "max_budget": req.budget, "models": req.models, "metadata": meta}
    if req.expires_days:
        body["duration"] = f"{req.expires_days}d"
    k = await _litellm("POST", "/key/generate", json=body)
    out = _key_public({**body, "key_name": "sk-..." + k["key"][-4:], "spend": 0, "created_at": k.get("created_at")}, p.tenant)
    out["key"] = k["key"]
    return out


@app.patch("/v1/keys/{alias}")
async def update_key(alias: str, req: KeyUpdate, p: Principal = Depends(admin)):
    """Change a key's budget, allowed models or expiry (LiteLLM `/key/update` on the key's token hash; the spend ledger
    is untouched, docs/RELIABILITY.md)."""
    data = await _litellm("GET", "/key/list", params={"return_full_object": "true", "size": 100})
    mine = [k for k in data.get("keys", []) if isinstance(k, dict) and (k.get("metadata") or {}).get("tenant") == p.tenant]
    k = next((x for x in mine if x.get("key_alias") == alias), None)
    if not k:
        raise HTTPException(404, f"key {alias} not found")
    if not k.get("token"):
        raise HTTPException(502, "key service returned no token hash for the key")
    body: dict = {"key": k["token"]}
    if req.budget is not None:
        body["max_budget"] = req.budget
    if req.models is not None:
        body["models"] = req.models
    if req.expires_days is not None:
        body["duration"] = f"{req.expires_days}d" if req.expires_days else None
    if len(body) == 1:
        raise HTTPException(400, "nothing to change: pass budget, models or expires_days")
    await _litellm("POST", "/key/update", json=body)
    forget(k.get("key") or "")              # a cached principal would keep the old budget for AUTH_CACHE_S
    updated = await _litellm("GET", "/key/list", params={"return_full_object": "true", "size": 100})
    row = next((x for x in updated.get("keys", []) if isinstance(x, dict) and x.get("key_alias") == alias), None) or {**k, **body}
    return _key_public(row, p.tenant)


@app.delete("/v1/keys/{alias}", status_code=204)
async def revoke_key(alias: str, p: Principal = Depends(admin)):
    if not any(k["alias"] == alias for k in await list_keys(p)):
        raise HTTPException(404, f"key {alias} not found")
    await _litellm("POST", "/key/delete", json={"key_aliases": [alias]})
