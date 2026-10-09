"""Nebius Serverless 2.0 customer API (docs/API.md). Run: uvicorn app:app"""
import time, urllib.parse, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
import logging
import artifacts, billing, catalog, db, jobs, kube, models, placement as placing
log = logging.getLogger("api")
import endpoints as ep
from auth import Principal, check_budget, check_model, forget, principal
from config import FLEET_MANAGER, LITELLM_MASTER_KEY, LITELLM_URL, PUBLIC_API_URL, REGION, REGION_API_URLS, SYNC_TIMEOUT_S
from resilience import retry_http

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """The control API owns the fleet database schema (services/api/db.py MIGRATIONS), applied at startup."""
    if db.enabled():
        log.info("database schema version %s", db.migrate())
    yield


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.9.3", lifespan=_lifespan)


class InvokeRequest(BaseModel):
    name: str | None = None
    mode: str | None = None
    input: dict = Field(default_factory=dict)
    region: str | None = None
    priority: str | None = None
    timeout_s: int | None = None


class UploadRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    expires_s: int = 3600
    region: str = REGION


class ScalingRequest(BaseModel):
    min_replicas: int | None = None
    max_replicas: int | None = None
    target_concurrency: int | None = None
    scale_to_zero_after_s: int | None = None


class KeyRequest(BaseModel):
    alias: str
    budget: float = 10.0
    models: list[str] = Field(default_factory=list)
    expires_days: int | None = None



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


def _public(m: dict) -> dict:
    if m["mode"] == "run":
        return catalog.to_public(m)
    if FLEET_MANAGER:        # the endpoints live on the workers (deployments); their live state is the regional API's
        return catalog.to_public(m, [{"region": r, "status": "ready", "replicas_ready": None} for r in m["regions"]])
    return catalog.to_public(m, [{"region": REGION, **kube.endpoint_status(m["k8s_name"], m["namespace"])}])


@app.get("/healthz")
def healthz():
    return {"ok": True, "models": len(catalog.all_models()), "region": REGION, "regions": kube.regions(), "fleet_manager": FLEET_MANAGER,
            "gpu_classes": sorted({p["gpu_class"] for p in kube.fleet()["pools"].values() if p.get("gpu_class")})}


@app.get("/v1/models")
def list_models(p: Principal = Depends(principal)):
    return [_public(m) for m in catalog.all_models().values()]


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


def _undeploy(entry: dict, clusters: list[str], after: dict):
    if entry.get("mode") == "run":
        return
    for cid in clusters:
        region = kube.cluster_region(cid)
        models.delete_rendered(entry, cid, region)
        models.sync_certificate(region, after)
        models.litellm_group_delete(entry["id"], cid)


def _write_model(spec: dict, p: Principal, replace: bool) -> dict:
    """Create or replace a model: validate, render its endpoint on the clusters of its regions, store it (with
    history) and refresh the regions' read-only copies."""
    _writes_here()
    entry = models.to_entry(spec, managed_by="api")
    mid = entry["id"]
    existing = catalog.get(mid)
    if existing and not replace:
        raise HTTPException(409, f"model {mid} exists (PUT replaces it)")
    if existing:
        _runtime_or_409(mid)
    result = {"id": mid, "kind": spec.get("kind", "endpoint"), "regions": list(entry["deployments"]), "applied": {}}
    runtime = models.list_runtime()
    before, after = runtime.get(mid) or {}, {**runtime, mid: entry}
    _deploy(entry, after, result)
    _undeploy(before, [c for c in (before.get("deployments") or {}) if c not in entry["deployments"]], after)   # left a region
    row = models.persist(entry, spec, by=p.info.get("key_alias") or p.info.get("key_name"))
    catalog.invalidate()
    result.update({"version": row.get("version"), "model": catalog.to_public(catalog.get(mid))})
    return result


@app.post("/v1/models", status_code=201)
def create_model(req: ModelSpec, p: Principal = Depends(principal)):
    """Define a model from a container (an endpoint or a job class): the console's "New model" form. Admin
    keys, on the fleet's API (the control cluster, where the database is). services/api/models.py."""
    admin(p)
    return _write_model(req.model_dump(), p, replace=False)


@app.put("/v1/models/{model}")
def replace_model(model: str, req: ModelSpec, p: Principal = Depends(principal)):
    admin(p)
    spec = req.model_dump()
    if spec.get("id") != model:
        raise HTTPException(400, "id in the body must match the path")
    return _write_model(spec, p, replace=True)


@app.delete("/v1/models/{model}", status_code=204)
def delete_model(model: str, p: Principal = Depends(principal)):
    admin(p)
    _writes_here()
    m = _runtime_or_409(model)
    if not m:
        raise HTTPException(404, f"model {model} not found")
    runtime = models.list_runtime()
    raw = runtime.pop(model, None)
    if raw:
        _undeploy(raw, list(raw.get("deployments") or {}), runtime)
    models.unpersist(model, by=p.info.get("key_alias") or p.info.get("key_name"))
    catalog.invalidate()


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
    return _public(_model(model))


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
    if mode == "sync":
        return await _sync(m, req, p)
    # admission: Kueue (profile queues) bounds concurrent runs; LiteLLM rpm/budget bound async calls;
    # the gateway BackendTrafficPolicy caps request rates. Nothing is counted here.
    name = jobs.op_name(p.tenant, model, idempotency_key)
    timeout = req.timeout_s          # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret = None
    if mode == "async":
        output_prefix = f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        job, secret = jobs.build_async(name, m, req.input, p.tenant, p.key, req.name, idempotency_key, timeout, output_prefix)
    elif FLEET_MANAGER:
        _guard_run_input(m, req, p, REGION)
        job = _fleet_run(m, req, p, name, timeout, idempotency_key)
        region = REGION                      # created on the manager; the dispatcher picks the worker
    else:
        _guard_run_input(m, req, p, region)
        output_prefix = f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        defaults = catalog.region_params(m, region)
        defaults.setdefault("output_prefix", output_prefix)
        job = jobs.build_run(name, m, req.input, p.tenant, req.name, idempotency_key, timeout, req.priority, region, defaults, billing.key_hash(p.key),
                             gpu_class=_class_for(m, req, region))
    created_job, created = jobs.create(p.namespace, job, region, secret)
    return JSONResponse(jobs.normalise(created_job, m.get("price_per_call")), status_code=202 if created else 200)


def _guard_run_input(m: dict, req: InvokeRequest, p: Principal, region: str) -> None:
    """A caller-supplied output_prefix must stay inside the tenant's own bucket of that region (never another
    tenant's), and an `image` parameter (container-run) must match the tenant's image allow-list when one is set."""
    out = req.input.get("output_prefix")
    if out:
        bucket = artifacts.storage(p.namespace, region)["bucket"]
        if not str(out).startswith(f"s3://{bucket}/"):
            raise HTTPException(400, f"output_prefix must be inside the tenant's bucket s3://{bucket}/")
    if req.input.get("image"):
        jobs.check_image_allowed(p.namespace, str(req.input["image"]), region)


def _fleet_run(m: dict, req: InvokeRequest, p: Principal, name: str, timeout: int | None, idempotency_key: str | None) -> dict:
    """Fleet placement (control cluster, docs/JOBS.md): the Job is created on the manager and MultiKueue +
    the dispatcher pick the worker among the model's regions and GPU classes. Inputs and outputs live in the
    tenant's hub-region bucket (the one this cluster's tenant-storage names). Per-region parameter defaults
    apply when every deployment agrees on them, or when the caller pins a region. Image references are
    fleet-wide (the logical registry host, docs/IMAGES.md), so an unpinned run is placed at queue time: the
    dispatcher nominates the cheapest free cluster and re-nominates while the run waits."""
    bucket = artifacts.storage(p.namespace, REGION)["bucket"]
    inp = req.input.get("input_prefix")
    if inp and not str(inp).startswith(f"s3://{bucket}/"):
        raise HTTPException(400, f"input_prefix must be in the tenant's fleet bucket s3://{bucket}/ (upload through this API, or pass region to run next to another bucket)")
    placement = {"manager": True, "profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]}
    region = req.region
    defaults = catalog.region_params(m, region) if region else catalog.fleet_params(m)
    defaults.setdefault("output_prefix", f"s3://{bucket}/operations/{name}")
    return jobs.build_run(name, m, req.input, p.tenant, req.name, idempotency_key, timeout, req.priority, region, defaults,
                          billing.key_hash(p.key), placement=placement, gpu_class=_class_for(m, req, region))


def _class_for(m: dict, req: InvokeRequest, region: str | None) -> str | None:
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
    cls, how = placing.choose_class(m, region, gpus, available)
    log.info("%s: GPU class %s chosen by %s", m["name"], cls, how)
    return cls


async def _forward(model: str, req: InvokeRequest, p: Principal, idempotency_key: str | None, base: str):
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
    if m.get("litellm_route"):
        url, headers = LITELLM_URL + m["litellm_route"], {"Authorization": f"Bearer {p.key}"}
    else:
        url, headers = m["endpoint_host"] + m["endpoint_path"], {}
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


def _owned(p: Principal, id: str) -> tuple[dict, str]:
    job, region = jobs.find(p.namespace, id)
    if job["metadata"].get("labels", {}).get("serverless2.nebius/tenant") != p.tenant:
        raise HTTPException(404, f"operation {id}: not found")
    return job, region


@app.get("/v1/operations/{id}")
def get_operation(id: str, p: Principal = Depends(principal)):
    job, region = _owned(p, id)
    return jobs.normalise(job, _price(job), artifacts.attempt_records(p.namespace, id, region or REGION, jobs.output_prefix(job)))


@app.get("/v1/operations/{id}/result")
def get_result(id: str, p: Principal = Depends(principal)):
    job, region = _owned(p, id)
    op = jobs.normalise(job)
    res = {"operation_id": id, "status": op["status"], "region": region, "error": op.get("error")}
    if op["status"] in ("QUEUED", "RUNNING"):
        return JSONResponse(res, status_code=409)
    prefix = jobs.output_prefix(job)
    if op["mode"] == "async":
        res["result"] = artifacts.read_json(p.namespace, f"operations/{id}/out/response.json", region or REGION, prefix=prefix)
    res["artifacts"] = artifacts.list_outputs(p.namespace, id, region or REGION, prefix) if op["mode"] == "run" else []
    return res


def _home(job: dict) -> str:
    """The cluster the operation's Job object lives in: the manager for fleet-placed runs."""
    return REGION if jobs.is_manager_job(job) else (job.get("_region") or REGION)


@app.post("/v1/operations/{id}:cancel")
def cancel(id: str, p: Principal = Depends(principal)):
    job, _ = _owned(p, id)
    op = jobs.normalise(jobs.cancel(p.namespace, id, _home(job)))
    if op["status"] in ("QUEUED", "RUNNING"):
        op["status"] = "CANCELLED"   # the Job controller converges within seconds (pods get SIGTERM)
    return op


@app.post("/v1/operations/{id}:resume")
def resume(id: str, p: Principal = Depends(principal)):
    """A new operation `<id>-r<n>` in the same region on the same work volume (checkpoints); for a
    FAILED or CANCELLED run whose volume still exists (docs/JOBS.md). Preemptions resume on their own.
    A fleet-placed run is resumed through the manager, pinned to the worker that ran it."""
    orig, region = _owned(p, id)
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    placement = {"profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]} if jobs.is_manager_job(orig) else None
    job, created = jobs.resume(p.namespace, id, _home(orig), m, catalog.region_params(m, region) if region else {}, placement)
    return JSONResponse(jobs.normalise(job, _price(job)), status_code=202 if created else 200)


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


def _endpoint_model(eid: str) -> dict:
    for m in catalog.all_models().values():
        if m["k8s_name"] == eid and m["mode"] != "run":
            return m
    raise HTTPException(404, f"endpoint {eid} not found")


@app.get("/v1/endpoints")
def list_endpoints(region: str | None = None, p: Principal = Depends(principal)):
    available = kube.regions()
    if region and region not in available:
        raise HTTPException(400, f"region {region} is not served by this API")
    out = []
    for m in catalog.all_models().values():
        if m["mode"] == "run":
            continue
        for r in ([region] if region else available):
            if r in m["regions"] and (i := kube.isvc(m["k8s_name"], m["namespace"], r)):
                out.append(ep.to_public(m, i, r))
    return out


@app.get("/v1/endpoints/{id}")
def get_endpoint(id: str, region: str = REGION, p: Principal = Depends(principal)):
    if region not in kube.regions():
        raise HTTPException(400, f"region {region} is not served by this API")
    m = _endpoint_model(id)
    i = kube.isvc(m["k8s_name"], m["namespace"], region) or {}
    if not i:
        raise HTTPException(404, f"endpoint {id} not deployed in {region}")
    return ep.to_public(m, i, region)


@app.patch("/v1/endpoints/{id}")
def update_endpoint(id: str, req: ScalingRequest, region: str = REGION, p: Principal = Depends(admin)):
    """Patch live scaling in one region. Model definitions own persistent replica limits; patches on
    git-managed endpoints are reverted by deployment synchronization."""
    if region not in kube.regions():
        raise HTTPException(400, f"region {region} is not served by this API")
    m = _endpoint_model(id)
    try:
        i = kube.api(region).patch_namespaced_custom_object("serving.kserve.io", "v1beta1", m["namespace"], "inferenceservices", id,
                                                      ep.scaling_patch(req.model_dump()))
    except ApiException as e:
        raise HTTPException(404 if e.status == 404 else 502, f"kserve: {e.reason}")
    kube.invalidate_endpoint(id, m["namespace"], region)
    return ep.to_public(m, i, region)


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


@app.delete("/v1/keys/{alias}", status_code=204)
async def revoke_key(alias: str, p: Principal = Depends(admin)):
    if not any(k["alias"] == alias for k in await list_keys(p)):
        raise HTTPException(404, f"key {alias} not found")
    await _litellm("POST", "/key/delete", json={"key_aliases": [alias]})
