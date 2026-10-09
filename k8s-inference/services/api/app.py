"""Nebius Serverless 2.0 customer API (docs/API.md). Run: uvicorn app:app"""
import asyncio, time, urllib.parse, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field
import logging
import artifacts, billing, catalog, config as cfg, db, jobs, kube, models, monitoring, placement as placing
log = logging.getLogger("api")
import endpoints as ep
from auth import Principal, allows_model, check_budget, check_model, forget, principal
from config import FLEET_MANAGER, LITELLM_MASTER_KEY, LITELLM_URL, PUBLIC_API_URL, REGION, REGION_API_URLS, SYNC_TIMEOUT_S
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


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.10.0", lifespan=_lifespan)


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
def fleet_info(p: Principal = Depends(principal)):
    pools = list(kube.fleet().get("pools", {}).values())
    regions = sorted({kube.cluster_region(pool["region"]) for pool in pools if pool.get("region")}
                     | {r for m in catalog.all_models().values() for r in m.get("regions", [])})
    return {"regions": regions, "gpu_classes": sorted({pool["gpu_class"] for pool in pools if pool.get("gpu_class")}),
            "fleet_manager": FLEET_MANAGER, "region": REGION}


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
        if (warning := models.litellm_group_delete(entry["id"], cid)):
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


async def _reconcile_loop():
    while True:
        try:
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
    check_model(p, model)
    check_budget(p)
    m = _model(model)
    if m["mode"] == "run":
        raise HTTPException(404, "not an endpoint")
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
                i = kube.isvc(m["k8s_name"], m["namespace"]) if region == REGION else kube.isvc(m["k8s_name"], m["namespace"], region)
                if i:
                    out.append(ep.to_public(m, i, region))
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
    i = (kube.isvc(m["k8s_name"], m["namespace"]) if region == REGION else kube.isvc(m["k8s_name"], m["namespace"], region)) or {}
    if not i:
        raise HTTPException(404, f"endpoint {id} not deployed in {region}")
    return ep.to_public(m, i, region)


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
    url = REGION_API_URLS.get(region)
    if not url:
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


@app.get("/v1/operations/{id}/metrics")
async def operation_metrics(id: str, range: str = "1h", end: float | None = None, p: Principal = Depends(principal)):
    job, region = _owned(p, id)
    monitoring.window(range, end)
    if region and region != REGION:
        return await _remote_monitoring(p, region, f"/v1/operations/{urllib.parse.quote(id, safe='')}/metrics", {"range": range, "end": end})
    return await monitoring.metrics("job", p.namespace, id, region or REGION, range, end)


@app.get("/v1/operations/{id}/logs")
async def operation_logs(id: str, range: str = "1h", search: str = Query("", max_length=200),
                         limit: int = Query(300, ge=1, le=1000), end: float | None = None, p: Principal = Depends(principal)):
    job, region = _owned(p, id)
    monitoring.window(range, end)
    if region and region != REGION:
        return await _remote_monitoring(p, region, f"/v1/operations/{urllib.parse.quote(id, safe='')}/logs",
                                        {"range": range, "search": search, "limit": limit, "end": end})
    return await monitoring.logs("jobset" if jobs.is_jobset(job) else "job", p.namespace, id, region or REGION, range, search, limit, end)


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
