"""Nebius Serverless 2.0 customer API (docs/API.md). Run: uvicorn app:app"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException

import artifacts
import attempts
import billing
import catalog
import config as cfg
import db
import endpoints as ep
import execution
import jobs
import keys
import kube
import models
import monitoring
import placement as placing
import render
import schemas
from auth import (
    Principal,
    _principal,
    admin,
    allows_model,
    authorize,
    check_model,
    forget,
    forget_hash,
    principal,
    scoped,
)
from auth import check_operation_owner as _check_operation_owner
from auth import may_read_operation as _may_read_operation
from catalog import require as _model
from config import FLEET_MANAGER, GRAFANA_URLS, LITELLM_URL, PUBLIC_API_URL, REGION, SYNC_TIMEOUT_S
from reconciliation import _reconcile_loop, reconcile_models
from reconciliation import fallback_switch as fallback_switch
from reconciliation import idle_shutdown as idle_shutdown
from reconciliation import recovery_switch as recovery_switch
from reconciliation import release_stale_reservations as release_stale_reservations
from schemas import INVOKE_TIMEOUT_MAX_S as INVOKE_TIMEOUT_MAX_S
from schemas import (
    BindingRequest,
    InvokeRequest,
    KeyImportRequest,
    KeyRequest,
    KeyUpdate,
    ModelSpec,
    ResumeRequest,
    ScalingRequest,
    UploadRequest,
)

log = logging.getLogger("api")


def check_placeholders() -> None:
    """Refuse to start with a manifest placeholder (charts/platform/templates/api/api.yaml carries
    `example.invalid` hostnames that the platform stage or the cluster overlay must replace): a placeholder
    would end up in every invoke link and Grafana link this API hands out."""
    bad = [
        name
        for name, value in (
            ("PUBLIC_API_URL", cfg.PUBLIC_API_URL),
            ("GRAFANA_URLS", ",".join(cfg.GRAFANA_URLS.values())),
        )
        if "example.invalid" in value
    ]
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


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.11.40", lifespan=_lifespan)


INPUT_MAX_BYTES = 60000  # a run's or async call's input lives on its Job (annotation, env): bounded, never truncated


ADMITTED_HEADER = "x-serverless2-admitted"  # the API's own proxied call to a gateway: the hook must not count it again


def _admit(p: Principal, operation: str, gpu_seconds: float = 0.0, counts_request: bool = True) -> dict:
    """Reserve the slot, the request and the GPU seconds of `operation` on the caller's key (keys.admit); nothing
    without the database, nothing for the router key (it carries no budget)."""
    if not db.enabled() or p.role == keys.ROUTER["role"]:
        return {"replay": False}
    try:
        return keys.admit(keys.key_hash(p.key), operation, p.tenant, gpu_seconds, counts_request)
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
            res = c.get("resources") or {}
            n += int(
                str(
                    (res.get("limits") or {}).get("nvidia.com/gpu")
                    or (res.get("requests") or {}).get("nvidia.com/gpu")
                    or 0
                )
            )
        return n * int(spec.get("parallelism") or 1)

    if job.get("kind") == "JobSet":
        return sum(
            pods((rj.get("template") or {}).get("spec") or {}) * int(rj.get("replicas") or 1)
            for rj in (job.get("spec") or {}).get("replicatedJobs") or []
        )
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


def _endpoint_limits(p: Principal, mode: str, admitted: bool = False) -> None:
    """An authorization hook cannot measure GPU time or hold a concurrency slot.

    Shared endpoint GPU time is not request wall time. Until instrumentation exists,
    finite GPU budgets are unsupported, never silently charged zero.
    """
    if p.role == keys.ROUTER["role"]:
        return
    if p.info.get("budget_gpu_seconds") is not None:
        raise HTTPException(501, "finite GPU-second budgets are unsupported for shared endpoints; use a metered run")
    if p.info.get("max_budget") is not None and ((p.info.get("metadata") or {}).get("legacy") or {}).get(
        "principal_id"
    ):
        raise HTTPException(
            501,
            "finite USD budgets on imported keys require endpoint accounting; hash-only imports are not mirrored into the router",
        )
    if mode == "gateway" and not admitted and p.info.get("max_concurrent") is not None:
        raise HTTPException(
            501, "direct endpoints cannot hold per-key concurrency reservations; use the synchronous API invoke path"
        )
    if mode == "async" and any(p.info.get(k) is not None for k in ("max_concurrent", "budget_requests", "rate_limit")):
        raise HTTPException(
            501,
            "bounded asynchronous endpoint admission awaits the authenticated caller integration; use synchronous API invoke",
        )


def _continuation_limits(p: Principal) -> None:
    # Resume/export/recovery currently create a new Job outside _create_admitted.
    # Do not let that re-entry path bypass a migrated key's reserved units.
    if any(
        p.info.get(k) is not None for k in ("max_concurrent", "budget_requests", "budget_gpu_seconds", "rate_limit")
    ):
        raise HTTPException(
            501,
            "bounded key continuations require reservation transfer; resume/export/fallback/recovery are not yet supported",
        )


def with_served_model(m: dict, inp):
    """OpenAI-protocol endpoints: fill in the served model name when the caller omitted `model`."""
    if isinstance(inp, dict) and m.get("protocol") == "openai" and m.get("served_model") and not inp.get("model"):
        return {**inp, "model": m["served_model"]}
    return inp


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
    for region in m["regions"]:  # the endpoints' live state, read in their clusters (REGION_KUBECONFIGS)
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
    return {
        "ok": True,
        "models": len(catalog.all_models()),
        "region": REGION,
        "regions": kube.regions(),
        "fleet_manager": FLEET_MANAGER,
        "gpu_classes": sorted({p["gpu_class"] for p in kube.fleet()["pools"].values() if p.get("gpu_class")}),
    }


@app.get("/v1/fleet", responses={200: {"model": schemas.FleetInfo}})
def fleet_info(live: bool = False, p: Principal = Depends(principal)):
    """The fleet as Terraform shaped it (pools per region, GPU class, preset, capacity type, price) and, with
    `live=true`, what is up right now: ready nodes per pool and the GPUs their pods hold (the console's Fleet page)."""
    pools = list(kube.fleet().get("pools", {}).values())
    regions = sorted(
        {kube.cluster_region(pool["region"]) for pool in pools if pool.get("region")}
        | {r for m in catalog.all_models().values() for r in m.get("regions", [])}
    )
    out = {
        "regions": regions,
        "gpu_classes": sorted({pool["gpu_class"] for pool in pools if pool.get("gpu_class")}),
        "fleet_manager": FLEET_MANAGER,
        "region": REGION,
        "execution": execution.capabilities(p.namespace),  # what this tenant may ask for (services/api/execution.py)
        "pools": [
            {
                "region": kube.cluster_region(pool.get("region") or REGION),
                "cluster": pool.get("region"),
                "pool": pool.get("pool"),
                "gpu_class": pool.get("gpu_class"),
                "platform": pool.get("platform"),
                "preset": pool.get("preset"),
                "gpus_per_node": int(pool.get("gpus_per_node") or 1),
                "capacity": pool.get("capacity"),
                "usd_per_gpu_hour": pool.get("usd_per_gpu_hour"),
                "max_nodes": pool.get("max_nodes"),
                "interconnect": pool.get("interconnect"),
                "local_nvme": bool(pool.get("local_nvme")),
            }
            for pool in pools
        ],
    }
    # the caller's tenant: its quotas as terraform.tfvars shaped them (charts/fleet tenants.yaml) and, live, what its
    # queues hold per region (docs/FLEET.md "Tenants")
    t = (kube.fleet().get("tenants") or {}).get(p.tenant) or {}
    out["tenant"] = {
        "name": p.tenant,
        "gpu_quota": t.get("gpu_quota"),
        "fair_share_weight": t.get("fair_share_weight", 1),
        "dedicated_pools": list(t.get("dedicated_pools") or []),
    }
    if live:
        out["tenant"]["usage"] = {
            region: kube.tenant_usage(p.tenant, region)
            for region in kube.regions()
            if not (FLEET_MANAGER and region == REGION)
        }
        for entry in out["pools"]:
            entry.update(kube.pool_usage(entry["cluster"], entry["pool"]))
        # the clusters: this one (the control plane when it is the fleet manager) and every region it reaches
        by_cluster = {pool.get("region"): pool for pool in pools}
        clusters = []
        for region in kube.regions():
            cid = next((cid for cid, pool in by_cluster.items() if kube.cluster_region(cid) == region), region)
            pool = by_cluster.get(cid) or {}
            control = region == REGION and FLEET_MANAGER
            clusters.append(
                {
                    "id": "control" if control else cid,
                    "region": "control" if control and region == "control" else region,
                    "project": pool.get("project"),
                    "roles": ["control", "manager"] if control else ["worker"],
                    "api_url": PUBLIC_API_URL if region == REGION else None,  # the fleet's one API
                    "grafana_url": GRAFANA_URLS.get("control" if control else region) or GRAFANA_URLS.get(region),
                    **kube.cluster_summary(region),
                }
            )
        out["clusters"] = clusters
    return out


@app.get("/v1/models", responses={200: {"model": list[schemas.Model]}})
def list_models(p: Principal = Depends(scoped("read:catalog"))):
    return [_public(m, p) for m in catalog.all_models().values() if allows_model(p, m["name"])]


def _runtime_or_409(mid: str) -> dict:
    m = catalog.get(mid)
    if m and m.get("managed_by") != "api":
        raise HTTPException(
            409,
            f"model {mid} is a built-in class of the solution ({m.get('managed_by')}); it cannot be changed through the API",
        )
    return m


def _writes_here():
    """Model definitions are written to the fleet database (DATABASE_URL)."""
    if not db.enabled():
        raise HTTPException(409, "this API has no fleet database (DATABASE_URL): model definitions cannot be stored")


def _write_model(spec: dict, p: Principal, replace: bool, expected: int | None = None) -> dict:
    """Commit desired state and history, then attempt its durable reconciliation."""
    _writes_here()
    entry = models.to_entry(spec, managed_by="api")
    if "scaling" in entry["spec"]:
        spec = {
            **spec,
            "scaling": entry["spec"]["scaling"],
        }  # store the complete policy that was rendered (models.validate)
    mid = entry["id"]
    existing = catalog.get(mid)
    if existing and not replace:
        raise HTTPException(409, f"model {mid} exists (PUT replaces it)")
    if existing:
        _runtime_or_409(mid)
    result = {"id": mid, "kind": spec.get("kind", "endpoint"), "regions": list(entry["deployments"]), "applied": {}}
    row = db.upsert_model(
        entry, spec, by=p.info.get("key_alias") or p.info.get("key_name"), expected=expected, create_only=not replace
    )
    catalog.invalidate()
    result.update(reconcile_models().get(mid, {"pending": True}))
    result.update({"version": row.get("version"), "model": catalog.to_public(catalog.normalise(entry))})
    return result


@app.post("/v1/models", status_code=201, responses={201: {"model": schemas.ModelWrite}})
def create_model(req: ModelSpec, p: Principal = Depends(admin)):
    """Define a model from a container (an endpoint or a job class): the console's "New model" form. Admin
    keys, on the fleet's API (the control cluster, where the database is). services/api/models.py."""
    return _write_model(req.model_dump(exclude_unset=True, warnings=False), p, replace=False)


@app.put("/v1/models/{model}", responses={200: {"model": schemas.ModelWrite}})
def replace_model(
    model: str, req: ModelSpec, p: Principal = Depends(admin), if_match: int | None = Header(default=None)
):
    spec = req.model_dump(exclude_unset=True, warnings=False)
    if spec.get("id") != model:
        raise HTTPException(400, "id in the body must match the path")
    return _write_model(spec, p, replace=True, expected=if_match)


@app.delete("/v1/models/{model}", status_code=204)
def delete_model(model: str, p: Principal = Depends(admin), if_match: int | None = Header(default=None)):
    _writes_here()
    m = _runtime_or_409(model)
    if not m:
        raise HTTPException(404, f"model {model} not found")
    db.delete_model(model, by=p.info.get("key_alias") or p.info.get("key_name"), expected=if_match)
    catalog.invalidate()
    reconcile_models()


@app.get("/v1/models/{model}/history", responses={200: {"model": list[schemas.ModelHistory]}})
def model_history(model: str, p: Principal = Depends(admin)):
    """Every version of a model's definition (who changed what, when): the `models_history` table."""
    _writes_here()
    if not db.get_model(model) and not db.history(model):
        raise HTTPException(404, f"model {model} not found")
    return db.history(model)


@app.get("/v1/models/{model}", responses={200: {"model": schemas.Model}})
def get_model(model: str, p: Principal = Depends(scoped("read:catalog"))):
    check_model(p, model)
    return _public(_model(model), p)


@app.api_route(
    "/internal/authorize/{model}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@app.api_route(
    "/internal/authorize/{model}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
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
    p = await _principal(key, allow_router=True)  # LiteLLM's deployments call here with the fleet's router key
    token = request.headers.get(ADMITTED_HEADER, "")
    admitted = (
        bool(cfg.ROUTER_KEY) and bool(token) and hmac.compare_digest(token, _admitted_token(keys.key_hash(p.key)))
    )
    await asyncio.to_thread(
        _authorize_model, p, model, admitted
    )  # catalog (database) and Kubernetes calls: off the event loop
    return JSONResponse(
        {}, headers={"x-serverless2-tenant": p.tenant, "x-serverless2-key-alias": str(p.info.get("key_alias") or "")}
    )


def _authorize_model(p: Principal, model: str, admitted: bool = False) -> None:
    m = catalog.get(model) or _endpoint_model(model)
    authorize(p, m["name"], "invoke:sync", budget=True)
    _endpoint_limits(p, "gateway", admitted)
    if m["mode"] == "run":
        raise HTTPException(404, "not an endpoint")
    if not admitted and p.role != keys.ROUTER["role"]:
        # a direct call to the endpoint's hostname: one request against the key's budget and rate window (the slot is
        # not held: the hook cannot see the call end); the API's own proxied call was admitted already
        gid = f"gw-{uuid.uuid4().hex[:12]}"
        _admit(p, gid)
        _release(gid, 0.0)
    if int(m.get("nodes") or 1) > 1:
        for region in m["regions"]:  # the gateway's call names no region: stamp every
            kube.stamp_last_request(
                m["k8s_name"], m["namespace"], region
            )  # deployment (idle shutdown reads it, throttled)


@app.get("/v1/keys/me", responses={200: {"model": schemas.ApiKey}})
def key_me(p: Principal = Depends(principal)):
    forget(p.key)
    return p.public()


@app.post(
    "/v1/models/{model}:invoke",
    responses={200: {"model": schemas.SyncResponse | schemas.Operation}, 202: {"model": schemas.Operation}},
)
async def invoke(
    model: str,
    req: InvokeRequest,
    p: Principal = Depends(principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    m = _model(model)
    mode = req.mode or m["mode"]
    authorize(p, model, {"sync": "invoke:sync", "async": "invoke:async"}.get(mode, "run:submit"), budget=True)
    if mode not in m["modes"]:
        raise HTTPException(400, f"model {model} supports modes {m['modes']}, not {mode}")
    region = req.region or (REGION if REGION in m["regions"] else m["regions"][0])
    if region not in m["regions"]:
        raise HTTPException(400, f"model {model} is deployed in {m['regions']}, not {region}")
    if req.execution and mode != "run":
        raise HTTPException(400, "execution options apply to runs only")
    if mode != "run":
        _endpoint_limits(p, mode)
    if mode == "sync":
        sid = f"sync-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(_admit, p, sid)  # a slot and a request for the duration of the call
        try:
            return await _sync(m, req, p, region)
        finally:
            await asyncio.to_thread(_release, sid, None)  # no per-request GPU measurement exists for shared endpoints
    if len(json.dumps(req.input)) > INPUT_MAX_BYTES:
        raise HTTPException(
            413,
            f"input exceeds {INPUT_MAX_BYTES} bytes; pass large inputs by reference (input_prefix, an artifact upload or a binding)",
        )
    # the rest renders and creates a Job: synchronous Kubernetes, database and bucket calls, run in a worker thread
    return await asyncio.to_thread(_invoke_job, m, model, req, p, region, mode, idempotency_key)


def _invoke_job(
    m: dict, model: str, req: InvokeRequest, p: Principal, region: str, mode: str, idempotency_key: str | None
):
    # Imported principal keys share an idempotency namespace only with the same
    # principal; a tenant's shared bucket never shares private operation state.
    name = render.op_name(p.owner, model, idempotency_key)
    timeout = req.timeout_s  # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret = None
    ex = execution.resolve(req.execution, p.namespace, m) if mode == "run" else None
    if ex and (
        (ex.get("placement") or {}).get("fallback_wait_s") is not None
        or (ex.get("recovery") or {}).get("on_infrastructure_loss") == "recover"
    ):
        _continuation_limits(p)
    allowed = (ex.get("placement") or {}).get("allowed_clusters") if ex else None
    if allowed and req.region and req.region not in allowed:
        # an explicit region that contradicts the caller's own cluster list; an implicit one (the default above) is no
        # request: a fleet run is placed within allowed_clusters, a single-cluster run must be allowed here
        raise HTTPException(400, f"region {req.region} is not in allowed_clusters {allowed}")
    if allowed and not FLEET_MANAGER and mode == "run" and region not in allowed:
        raise HTTPException(400, f"this API runs jobs in {region} only, which allowed_clusters {allowed} excludes")
    if mode == "async":  # the caller Job runs in the endpoint's region, through that region's gateway
        output_prefix = f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        job, secret = jobs.build_async(
            name, m, req.input, p.tenant, p.key, req.name, idempotency_key, timeout, output_prefix, region
        )
    elif FLEET_MANAGER:
        _guard_run_input(m, req, p, REGION, ex)
        job = _fleet_run(m, req, p, name, timeout, idempotency_key, ex)
        region = REGION  # created on the manager; the dispatcher picks the worker
    else:
        _guard_run_input(m, req, p, region, ex)
        defaults = catalog.region_params(m, region)
        if not (ex and ex.get("io_mode") == "application"):
            defaults.setdefault(
                "output_prefix", f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
            )
        else:
            defaults.setdefault("output_prefix", "")
        job = jobs.build_run(
            name,
            m,
            req.input,
            p.tenant,
            req.name,
            idempotency_key,
            timeout,
            req.priority,
            region,
            defaults,
            billing.key_hash(p.key),
            gpu_class=_class_for(m, req, region, p.tenant, ex),
            ex=ex,
        )
    # admission on the caller's key, from the rendered Job (its real GPU count): a slot, a request and, for a key with a
    # GPU-second budget, GPUs x the run's timeout; released with the measured value when the attempt is billed (billing.py)
    ann = job["metadata"].setdefault("annotations", {})
    ann[f"{cfg.LABEL}/owner"] = p.owner
    if db.enabled():
        try:
            keys.own_operation(name, p.tenant, p.owner, model)
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)
    with keys.operation_lock(name) if db.enabled() else nullcontext():
        created_job, created = _create_admitted(p, name, job, region, secret, req, mode)
    if created:
        return JSONResponse(jobs.normalise(created_job, m.get("price_per_call")), status_code=202)
    return JSONResponse(
        _operation(p, created_job, created_job["metadata"]["name"], region), status_code=200
    )  # a replay: its current state


def _create_admitted(p, name, job, region, secret, req, mode):
    """Called under the per-operation database lock; a refund cannot race a replay."""
    # Existing Jobs already own their admission, including terminal replays.
    try:
        existing = jobs.get(p.namespace, name, region)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        existing = None
    if existing:
        _check_operation_owner(p, existing)
        reservation = {"replay": True}
    else:
        reservation = _admit(p, name, _gpu_reservation(_job_gpus(job), req, p) if mode == "run" else 0.0)
    try:
        created_job, created = jobs.create(p.namespace, job, region, secret)
    except Exception:
        if db.enabled() and not reservation.get("replay"):
            # A timeout can mean the create succeeded. Only a confirmed absence
            # permits a refund; ambiguous failures retain the reservation.
            try:
                jobs.get(p.namespace, name, region)
            except HTTPException as missing:
                if missing.status_code == 404:
                    keys.abandon(name, keys.key_hash(p.key))
            except Exception:
                pass
        raise
    _check_operation_owner(p, created_job)
    return created_job, created


def _guard_run_input(m: dict, req: InvokeRequest, p: Principal, region: str, ex: dict | None = None) -> None:
    """A caller-supplied output_prefix must stay inside the tenant's own bucket of that region (never another
    tenant's), and an `image` parameter (container-run) must match the tenant's image allow-list when one is set.
    Application-managed I/O names no bucket at all."""
    out = req.input.get("output_prefix")
    if ex and ex.get("io_mode") == "application":
        if out or req.input.get("input_prefix"):
            raise HTTPException(
                400, "application-managed I/O takes no input_prefix/output_prefix: the run moves its own data"
            )
        out = None
    if out:
        bucket = artifacts.storage(p.namespace, region)["bucket"]
        if not str(out).startswith(f"s3://{bucket}/"):
            raise HTTPException(400, f"output_prefix must be inside the tenant's bucket s3://{bucket}/")
    if req.input.get("image"):
        jobs.check_image_allowed(p.namespace, str(req.input["image"]), region)


def _fleet_run(
    m: dict,
    req: InvokeRequest,
    p: Principal,
    name: str,
    timeout: int | None,
    idempotency_key: str | None,
    ex: dict | None = None,
) -> dict:
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
        raise HTTPException(
            400,
            f"input_prefix must be in the tenant's fleet bucket s3://{bucket}/ (upload through this API, or pass region to run next to another bucket)",
        )
    placement = execution.narrow_placement(
        {"manager": True, "profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]}, ex
    )
    region = req.region
    defaults = catalog.region_params(m, region) if region else catalog.fleet_params(m)
    defaults.setdefault("output_prefix", "" if application else f"s3://{bucket}/operations/{name}")
    return jobs.build_run(
        name,
        m,
        req.input,
        p.tenant,
        req.name,
        idempotency_key,
        timeout,
        req.priority,
        region,
        defaults,
        billing.key_hash(p.key),
        placement=placement,
        gpu_class=_class_for(m, req, region, p.tenant, ex, placement.get("regions")),
        ex=ex,
    )


def _class_for(
    m: dict,
    req: InvokeRequest,
    region: str | None,
    tenant: str | None = None,
    ex: dict | None = None,
    regions: list[str] | None = None,
) -> str | None:
    """A run class with per-GPU-class images gets its class at submission (docs/SCHEDULING.md "Per-GPU images for
    runs"): the best pool of the fleet document among the model's classes and regions (the caller's region when
    given, services/api/placement.py). None for a class with one image: Kueue may then switch class at queue time as before."""
    if not jobs.class_images(m):
        return None
    gpus = (m.get("job") or {}).get("gpu", 1)
    try:
        gpus = int(gpus)
    except (TypeError, ValueError):  # a parameter placeholder: one GPU for the ranking
        gpus = (
            int(req.input.get("gpus", 1) or 1)
            if isinstance(req.input.get("gpus", 1), (int, str)) and str(req.input.get("gpus", 1)).isdigit()
            else 1
        )
    available = (
        (ex.get("placement") or {}).get("gpu_preference") if ex else None
    )  # the caller's order narrows the model's
    cls, how = placing.choose_class(
        m, region, gpus, available, tenant, regions
    )  # `regions`: the run's allowed clusters
    log.info("%s: GPU class %s chosen by %s", m["name"], cls, how)
    return cls


async def _sync(m: dict, req: InvokeRequest, p: Principal, region: str):
    """Proxy to the endpoint. Via LiteLLM's pass-through route when the model has one (budget, spend and the
    per-call price are enforced there); else straight to the predictor Service when the endpoint runs on this
    cluster, and through the region's gateway hostname (the one LiteLLM uses, TLS on the fleet CA) with the caller's
    key for an endpoint in another region: the gateway's ext-auth checks the key once more against this API."""
    if int(m.get("nodes") or 1) > 1:
        st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m["nodes"]))
        kube.stamp_last_request(
            m["k8s_name"], m["namespace"], region
        )  # a direct proxy call bypasses the gateway's authorize hook
        if st["status"] != "ready":
            raise HTTPException(
                503,
                f"endpoint {m['name']} is {st['status']} ({m['nodes']} nodes): a multi-node endpoint is started with "
                f"PATCH /v1/endpoints/{m['k8s_name']} {{\"scaling\": {{\"min\": 1}}}} (or the console's Start button) and takes "
                f"minutes to load; it stops again after scaling.idle_s without a request",
            )
    if m.get("litellm_route"):
        url, headers = LITELLM_URL + m["litellm_route"], {"Authorization": f"Bearer {p.key}"}
    elif region == REGION:
        v = _variant_for(m, req.input, models.cluster_id())
        host = (
            m["endpoint_host"]
            if v["object"] == m["k8s_name"]
            else f"http://{v['object']}-predictor.{m['namespace']}.svc.cluster.local"
        )
        url, headers = host + m["endpoint_path"], {}
    else:
        if not models.endpoint_domain(region):
            raise HTTPException(503, f"sync calls need the endpoint domain of {region} (ENDPOINT_DOMAINS)")
        cfg.refresh_trust()
        v = _variant_for(m, req.input, region)
        url, headers = (
            f"https://{models.endpoint_host(m, region, v['object'])}{m['endpoint_path']}",
            {"Authorization": f"Bearer {p.key}", ADMITTED_HEADER: _admitted_token(keys.key_hash(p.key))},
        )
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
            body = {
                "encoding": "base64",
                "media_type": media_type,
                "size_bytes": len(r.content),
                "sha256": hashlib.sha256(r.content).hexdigest(),
                "data": base64.b64encode(r.content).decode("ascii"),
            }
    op = {
        "id": f"sync-{uuid.uuid4().hex[:12]}",
        "name": req.name,
        "model": m["name"],
        "mode": "sync",
        "region": region,
        "status": "SUCCEEDED" if ok else "FAILED",
        "tenant": p.tenant,
        "created_at": started,
        "started_at": started,
        "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(time.time() - t0, 3),
        "attempts": [
            {"index": 1, "status": "SUCCEEDED" if ok else "FAILED", "reason": None if ok else f"HTTP {r.status_code}"}
        ],
        "input": req.input,
        "cost": m.get("price_per_call") if ok else None,
        "error": None if ok else str(body)[:500],
    }
    return JSONResponse(
        {"result": body, "operation": op},
        status_code=200
        if ok
        else (r.status_code if r.status_code in (400, 401, 402, 403, 404, 409, 413, 415, 422, 429) else 502),
    )


def _price(job: dict) -> float | None:
    m = catalog.get(job["metadata"].get("labels", {}).get("serverless2.nebius/model", "")) or {}
    return m.get("price_per_call")


@app.get("/v1/operations", responses={200: {"model": list[schemas.Operation]}})
def list_operations(
    p: Principal = Depends(scoped("read:operations")), limit: int = Query(50, le=500), model: str | None = None
):
    mine = [j for j in jobs.list_ops(p.namespace, p.tenant, model, 500) if _may_read_operation(p, j)][:limit]
    # the durable attempt records of every listed operation in one query (a pod the worker cluster removed after
    # delivery leaves no live evidence of it; the records are what GET /v1/operations/{id} reports from)
    rows = attempts.for_operations({_root(j) for j in mine}) if attempts.enabled() else {}
    marks = attempts.marks_for({j["metadata"]["name"] for j in mine}) if attempts.enabled() else {}
    out = []
    for j in mine:
        name, root = j["metadata"]["name"], _root(j)
        op = jobs.normalise(j, _price(j), [r for r in rows.get(root, []) if r["operation"] == name])
        if attempts.enabled():
            op.update(attempts.lifecycle(name, root, rows.get(root, []), marks.get(name)))
        out.append(op)
    return out


def _root(job: dict) -> str:
    return (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or job["metadata"]["name"]


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
            rows = [r for r in attempts.for_operation(current) if r["operation"] == current and r["tenant"] == p.tenant]
            if not rows and previous is not None:
                return previous  # a verified owner's successor is not created yet
            synthetic_job = {
                "metadata": {
                    "name": current,
                    "labels": {f"{cfg.LABEL}/tenant": p.tenant, f"{cfg.LABEL}/mode": "run"},
                    "annotations": {f"{cfg.LABEL}/resumed-from": rows[-1]["root"] if rows else current},
                }
            }
            _check_operation_owner(p, synthetic_job)
            nxt = attempts.successor_of(current)
            if follow and nxt and nxt != current:
                current = nxt
                continue
            synthetic = attempts.synthesize(current, p.tenant)
            if synthetic is None:
                if previous is not None:  # a successor that is marked but not created yet: the original stands
                    return previous
                raise
            return {**synthetic_job, "_synthetic": synthetic}, synthetic.get("region")
        _check_operation_owner(p, job)
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
    by_pod = {r["pod"]: r for r in rows}  # one row per pod (the newest wins)
    rows = list(by_pod.values())
    members = sorted(
        [
            j
            for j in jobs.list_ops(p.namespace, p.tenant, None, 500)
            if _may_read_operation(p, j)
            and (
                j["metadata"]["name"] == root
                or (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") == root
            )
        ],
        key=lambda j: str(j["metadata"].get("creationTimestamp") or ""),
    )
    name = job["metadata"]["name"]
    all_attempts = st._attempts(job.get("_pods", []), rows)  # every member's records; the live pods are this member's
    for a in all_attempts:
        r = by_pod.get(a.get("attempt_id")) or {}
        a["operation"] = r.get("operation") or name
        a["attempt_role"] = r.get("role") or a.get("attempt_role") or "run"
        a["image"] = r.get("image")
        d = st._secs(a.get("started_at"), a.get("ended_at"))
        a["gpu_seconds"] = (
            round(d * a["_gpus"], 1) if d is not None and a.get("ended_at") else None
        )  # unknown stays unknown
    chain, total, complete = [], 0.0, True
    for m in members:
        mid = m["metadata"]["name"]
        mine = [a for a in all_attempts if a["operation"] == mid]
        measured = [a["gpu_seconds"] for a in mine if a["gpu_seconds"] is not None]
        unknown = any(a["gpu_seconds"] is None and a["status"] != "QUEUED" for a in mine)
        ann = m["metadata"].get("annotations") or {}
        chain.append(
            {
                "id": mid,
                "created_at": m["metadata"].get("creationTimestamp"),
                "status": op["status"]
                if mid == name
                else jobs.normalise(m, None, [r for r in rows if r["operation"] == mid])["status"],
                "gpu_class": (m["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/gpu-class"),
                "cluster": next(
                    (
                        (
                            by_pod[a["attempt_id"]].get("cluster")
                            or kube.cluster_id_or_none(by_pod[a["attempt_id"]].get("region"))
                        )
                        for a in reversed(mine)
                        if a.get("attempt_id") in by_pod
                    ),
                    op.get("cluster") if mid == name else None,
                ),
                "reason": ann.get(f"{cfg.LABEL}/fallback") or ann.get(f"{cfg.LABEL}/recovery"),
                "gpu_seconds": round(sum(measured), 1) if measured else (None if unknown else 0.0),
                "gpu_seconds_complete": not unknown,
                "cost": float(ann[f"{cfg.LABEL}/billed"]) if f"{cfg.LABEL}/billed" in ann else None,
            }
        )
        total += sum(measured)
        complete = complete and not unknown
    root_member = next((c for c in chain if c["id"] == root), None)
    successors = [c for c in chain if c["id"] != root]
    billed = [c["cost"] for c in chain if c["cost"] is not None]
    return {
        "created_at": (root_member or {}).get("created_at")
        or min((a["started_at"] for a in all_attempts if a.get("started_at")), default=op.get("created_at")),
        "started_at": min((a["started_at"] for a in all_attempts if a.get("started_at")), default=None),
        "attempts": [{k: v for k, v in a.items() if not k.startswith("_")} for a in all_attempts],
        "chain": chain,
        "successor_created_at": successors[-1]["created_at"] if successors else None,
        "gpu_seconds": round(total, 1),
        "gpu_seconds_complete": complete,
        "cost": round(sum(billed), 6) if billed else None,
    }


def _finalizers(p: Principal, name: str, root: str) -> dict:
    """The application finalizers of an operation (docs/JOBS.md "Finalization by the application"): how many ran
    (`finalize_attempts`) and, while one is QUEUED or RUNNING, the handle shows it: `status FINALIZING`,
    `finalizing_by`, and the finalizer pod's attempt id, epoch and role (the broker's current attempt)."""
    xs = [
        j
        for j in jobs.list_ops(p.namespace, p.tenant, None, 500)
        if _may_read_operation(p, j)
        and (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of") == name
        and (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/attempt-role") == "finalize"
    ]
    out: dict = {"finalize_attempts": len(xs)}
    active = [
        j
        for j in xs
        if jobs.normalise(j)["status"] in ("QUEUED", "RUNNING")
        and not (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/cancelled")
    ]
    if active:
        a = sorted(active, key=lambda j: str(j["metadata"].get("creationTimestamp") or ""))[-1]
        aname = a["metadata"]["name"]
        rows = [r for r in attempts.for_operation(root) if r["operation"] == aname]
        latest = rows[-1] if rows else None
        out.update(
            {
                "status": "FINALIZING",
                "finalizing_by": aname,
                "error": None,
                "attempt_id": latest.get("pod") if latest else None,
                "epoch": latest.get("epoch") if latest else None,
                "attempt_role": "finalize",
                "cluster": (latest.get("cluster") or kube.cluster_id_or_none(latest.get("region")))
                if latest
                else out.get("cluster"),
            }
        )
    return out


def _operation(p: Principal, job: dict, id: str, region: str | None) -> dict:
    if job.get("_synthetic"):
        return job["_synthetic"]
    name = job["metadata"]["name"]
    records, root = _records(p, job, name, region)
    op = jobs.normalise(job, _price(job), records)
    op.update(attempts.lifecycle(name, root, records))
    if not op.get("cluster") and (job.get("_cluster") or job.get("_region")):
        op["cluster"] = job.get("_cluster") or kube.cluster_id_or_none(
            job["_region"]
        )  # admitted, no record yet: the admitting cluster's id
    if name != id:
        op["requested_id"] = id  # the caller's handle was superseded; this is its current successor
    if (
        root != name
        or name != id
        or (attempts.enabled() and any(r["operation"] != name for r in attempts.for_operation(root)))
    ):
        op.update(
            _chain(p, op, job, root)
        )  # a chain member, a followed handle, or a root with retries/successors on record
    if (
        op["status"] in ("FAILED", "EXPORT_FAILED")
        and attempts.enabled()
        and (job["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") == "run"
    ):
        op.update(_finalizers(p, name, root))
    if op["status"] in ("EXPORT_FAILED", "FINALIZING", "FAILED") and attempts.enabled():
        # a transfer-only retry or an application finalizer (`<id>-x<n>`) that delivered closes the original (docs/JOBS.md
        # "Attempts and finalization", "Finalization by the application"); a FAILED run only ever has finalizers
        done = [
            r
            for r in attempts.for_operation(root)
            if r["operation"].startswith(f"{name}-x")
            and (
                (r.get("export_state") or r.get("export")) == "succeeded"
                or (r.get("result_ref") and r["status"] == "succeeded")
            )
        ]
        if done:
            op.update(
                {
                    "status": "SUCCEEDED",
                    "export_state": done[-1].get("export_state") or "succeeded",
                    "exported_by": done[-1]["operation"],
                    "result_ref": done[-1].get("result_ref") or op.get("result_ref"),
                    "error": None,
                }
            )  # the delivering attempt's result
    return op


@app.get("/v1/operations/{id}", responses={200: {"model": schemas.Operation}})
def get_operation(id: str, since: int | None = None, p: Principal = Depends(scoped("read:operations"))):
    """`since=<revision>`: 304 while nothing about the operation's attempts changed (polling cursor; the live
    pod state is still re-read on every 200)."""
    job, region = _owned(p, id)
    op = _operation(p, job, id, region)
    if (
        since is not None
        and op.get("revision")
        and op["revision"] <= since
        and op["status"] not in ("QUEUED", "RUNNING")
    ):
        return JSONResponse(None, status_code=304)
    return op


@app.get(
    "/v1/operations/{id}/result",
    responses={200: {"model": schemas.OperationResult}, 409: {"model": schemas.OperationResult}},
)
def get_result(id: str, p: Principal = Depends(scoped("read:results"))):
    job, region = _owned(p, id)
    op = _operation(p, job, id, region)
    res = {
        "operation_id": id,
        "status": op["status"],
        "region": region,
        "error": op.get("error"),
        "export_state": op.get("export_state"),
        "result_ref": op.get("result_ref"),
        "checkpoint_ref": op.get("checkpoint_ref"),
    }
    if op["status"] in ("QUEUED", "RUNNING"):
        return JSONResponse(res, status_code=409)
    prefix = jobs.output_prefix(job)
    application = (op.get("execution") or {}).get("io_mode") == "application"
    if op["mode"] == "async":
        res["result"] = artifacts.read_json(
            p.namespace, f"operations/{id}/out/response.json", region or REGION, prefix=prefix
        )
    res["artifacts"] = (
        artifacts.list_outputs(p.namespace, id, region or REGION, prefix)
        if op["mode"] == "run" and not application
        else []
    )
    return res


@app.post(
    "/v1/operations/{id}:export", responses={200: {"model": schemas.Operation}, 202: {"model": schemas.Operation}}
)
def export_again(id: str, p: Principal = Depends(scoped("read:results", "run:control"))):
    """Transfer-only retry of an EXPORT_FAILED run (managed I/O): a new operation `<id>-x<n>` with the uploader on the
    kept work volume and a main container that does nothing; the simulation is not rerun (docs/JOBS.md)."""
    orig, region = _owned(p, id)
    _continuation_limits(p)
    exported = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of")
    if exported:  # a previous retry's handle: the retry is of the compute attempt, never of a retry
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
        rows = (
            [r for r in attempts.for_operation(root) if r["operation"] == name and r["status"] == "failed"]
            if attempts.enabled()
            else []
        )
        last = rows[-1] if rows else None
        if last is None or last.get("exit_code") not in fz["onExitCodes"]:
            raise HTTPException(
                409,
                f"operation {id} is FAILED with exit code {(last or {}).get('exit_code')}; the model finalizes only after exit codes {fz['onExitCodes']} (compute done, delivery pending)",
            )
        finalize = {"spec": fz, "attempt": last}
    elif op["status"] not in ("EXPORT_FAILED", "FINALIZING"):
        raise HTTPException(
            409,
            f"operation {id} is {op['status']}; only an EXPORT_FAILED (or still FINALIZING) run is exported again"
            + (
                " (a FAILED application run is finalized only when its model declares job.finalize and the compute ended with a 'delivery pending' exit code)"
                if op["status"] == "FAILED"
                else ""
            ),
        )
    job, created = jobs.export_again(
        p.namespace,
        orig["metadata"]["name"],
        _home(orig),
        m,
        catalog.region_params(m, region) if region else {},
        finalize=finalize,
    )
    return _continued(p, job, created, region)


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
        job, _ = await asyncio.to_thread(
            jobs.find, ns, operation
        )  # the manager's Job for a fleet run (the worker holds a mirror)
        body["root"] = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or operation
        body["role"] = (job["metadata"].get("annotations") or {}).get(
            f"{cfg.LABEL}/attempt-role"
        ) or "run"  # the platform's statement, never the pod's
        # the canonical cluster id: the Workload's admitting cluster for a fleet run, else the worker the token came from
        body["cluster"] = job.get("_cluster") or _cluster_of(region)
    except HTTPException:
        body["root"] = operation  # the Job is already gone (TTL, cancel): the record still counts
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


@app.post("/v1/operations/{id}:cancel", responses={200: {"model": schemas.Operation}})
def cancel(id: str, p: Principal = Depends(scoped("run:control"))):
    job, region = _owned(p, id)
    if job.get("_synthetic"):
        raise HTTPException(409, f"operation {id} is over; nothing to cancel")
    current = job["metadata"]["name"]
    op = _operation(p, job, id, region)
    if op.get("finalizing_by"):  # the root's handle: a cancel stops its active finalizer, the root stays as it is
        job, _ = _owned(p, op["finalizing_by"], follow=False)
        current = job["metadata"]["name"]
    attempts.mark_cancelled(current)  # a late "succeeded" from the cancelled attempt is refused from now on
    op = jobs.normalise(jobs.cancel(p.namespace, current, _home(job)))
    if op["status"] in ("QUEUED", "RUNNING"):
        op["status"] = "CANCELLED"  # the Job controller converges within seconds (pods get SIGTERM)
    return op


@app.post(
    "/v1/operations/{id}:resume", responses={200: {"model": schemas.Operation}, 202: {"model": schemas.Operation}}
)
def resume(
    id: str,
    req: ResumeRequest | None = None,
    p: Principal = Depends(scoped("run:submit", "run:control")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """A new operation `<id>-r<n>` in the same region on the same work volume (checkpoints); for a
    FAILED or CANCELLED run whose volume still exists (docs/JOBS.md). Preemptions resume on their own.
    A fleet-placed run is resumed through the manager, pinned to the worker that ran it. With a
    `checkpoint_ref` (a portable checkpoint the application restores itself) the volume and the region are not
    needed: the run is placed again, optionally on another `gpu_class` (its image), docs/JOBS.md "Execution options"."""
    orig, region = _owned(p, id)
    _continuation_limits(p)
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    req = req or ResumeRequest()
    if req.gpu_class and req.gpu_class not in jobs.gpu_classes(m):
        raise HTTPException(400, f"gpu_class {req.gpu_class} is not one of {jobs.gpu_classes(m)}")
    root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    pointer = (
        (attempts.latest_checkpoint(root) or {}).get("checkpoint_ref")
        if attempts.enabled() and not req.checkpoint_ref
        else None
    )
    if req.gpu_class and not req.checkpoint_ref and not pointer:
        raise HTTPException(
            400,
            "another gpu_class needs a checkpoint_ref (none was acknowledged for this run): the work volume's checkpoints belong to the original class",
        )
    placement = (
        {"profile": jobs.profile_of(m), "classes": jobs.gpu_classes(m), "regions": m["regions"]}
        if jobs.is_manager_job(orig)
        else None
    )
    if req.gpu_class and placement:
        placement["classes"] = [req.gpu_class]
    job, created = jobs.resume(
        p.namespace,
        orig["metadata"]["name"],
        _home(orig),
        m,
        catalog.region_params(m, region) if region and not req.checkpoint_ref and not req.gpu_class else {},
        placement,
        checkpoint_ref=req.checkpoint_ref,
        gpu_class=req.gpu_class,
        idem=idempotency_key,
        pointer=pointer,
    )
    return _continued(p, job, created, region)


def _continued(p: Principal, job: dict, created: bool, region: str | None) -> JSONResponse:
    """The response of a continuation: 202 with the new Job, or 200 with the existing one's current state (the
    replay of an idempotent call reports what GET /v1/operations/{id} reports, records included)."""
    if created:
        return JSONResponse(jobs.normalise(job, _price(job)), status_code=202)
    return JSONResponse(_operation(p, job, job["metadata"]["name"], region), status_code=200)


# ---- bindings: confidential inputs for runs (docs/JOBS.md "Execution options") ----
@app.put("/v1/bindings/{ref}", responses={200: {"model": schemas.Binding}})
def put_binding(ref: str, req: BindingRequest, p: Principal = Depends(scoped("artifacts:write"))):
    return execution.put_binding(p.namespace, p.tenant, ref, req.data)


@app.get("/v1/bindings", responses={200: {"model": list[schemas.Binding]}})
def list_bindings(p: Principal = Depends(principal)):
    return execution.list_bindings(p.namespace)


@app.delete("/v1/bindings/{ref}", status_code=204)
def delete_binding(ref: str, p: Principal = Depends(scoped("artifacts:write"))):
    if not execution.delete_binding(p.namespace, ref):
        raise HTTPException(404, f"binding {ref}: not found")
    return None


@app.post("/v1/artifacts/uploads", status_code=201, responses={201: {"model": schemas.Upload}})
def upload(req: UploadRequest, p: Principal = Depends(scoped("artifacts:write"))):
    return artifacts.presign_upload(
        p.namespace, req.filename, req.content_type, min(req.expires_s, 7 * 86400), req.region
    )


@app.get("/v1/artifacts/{uri:path}", responses={200: {"model": schemas.Artifact}})
def download(uri: str, p: Principal = Depends(scoped("read:results"))):
    """A presigned download of one object in the tenant's buckets. An object of an operation (`operations/<id>/...`,
    where the uploaders write) is authorized exactly like `GET /v1/operations/{id}/result`: the results scope, the
    operation's owner (admins read every operation of the tenant) and the key's model allow-list; the tenant bucket
    boundary alone is not the documented per-principal isolation."""
    uri = urllib.parse.unquote(uri)
    if (oid := artifacts.operation_of(uri)) is not None:
        _owned(p, oid, follow=False)  # 404 for another principal's operation or a model outside the allow-list
    return artifacts.presign_download(p.namespace, uri)


@app.middleware("http")
async def no_store(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- endpoints (read, scale) and keys: need a key with metadata.role == admin for writes ----


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
        if m["k8s_name"] == eid or any(
            v["object"] == eid for c in (m.get("deployments") or {}) for v in models.variants(m, c)
        ):
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
        out.append(
            {
                "name": v["name"],
                "id": v["object"],
                "pool": v.get("pool"),
                "min": v.get("min"),
                "max": v.get("max"),
                "image": v.get("image"),
                "status": st.get("status"),
                "replicas_ready": st.get("replicas_ready"),
            }
        )
    return out


@app.get("/v1/endpoints", responses={200: {"model": list[schemas.Endpoint]}})
def list_endpoints(region: str | None = None, p: Principal = Depends(scoped("read:catalog"))):
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
        for region in [selected_region] if selected_region else served_regions:
            if region not in m["regions"]:
                continue
            try:
                i = _deployed(m, region)
                if i:
                    out.append(
                        {
                            **ep.to_public(m, i, region),
                            **(
                                {"variants": vs, "routing": m.get("routing")}
                                if (vs := _variants_public(m, region))
                                else {}
                            ),
                        }
                    )
            except (HTTPException, ApiException):
                out.append(
                    {
                        "id": m["k8s_name"],
                        "name": m["display_name"],
                        "model": m["name"],
                        "region": region,
                        "status": "unavailable",
                        "replicas_ready": None,
                        "gpu": m.get("gpu"),
                        "managed_by": m.get("managed_by"),
                        "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke",
                    }
                )
    return out


@app.get("/v1/endpoints/{id}", responses={200: {"model": schemas.Endpoint}})
def get_endpoint(id: str, region: str | None = None, p: Principal = Depends(scoped("read:catalog"))):
    m = _endpoint_model(id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    i = _deployed(m, region) or {}
    if not i:
        raise HTTPException(404, f"endpoint {id} not deployed in {region}")
    return {
        **ep.to_public(m, i, region),
        **({"variants": vs, "routing": m.get("routing")} if (vs := _variants_public(m, region)) else {}),
    }


@app.patch("/v1/endpoints/{id}", responses={200: {"model": schemas.Endpoint}})
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
    for key, target in (
        ("min_replicas", "min"),
        ("max_replicas", "max"),
        ("scale_to_zero_after_s", "idle_s"),
        ("target_concurrency", "target" if rate else "container_concurrency"),
    ):
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


@app.get("/v1/endpoints/{id}/metrics", responses={200: {"model": schemas.Metrics}})
async def endpoint_metrics(
    id: str,
    region: str | None = None,
    range: str = "1h",
    end: float | None = None,
    p: Principal = Depends(scoped("read:metrics")),
):
    m = await asyncio.to_thread(_endpoint_model, id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    return await monitoring.metrics("endpoint", m["namespace"], m["k8s_name"], region, range, end)


@app.get("/v1/endpoints/{id}/logs", responses={200: {"model": schemas.Logs}})
async def endpoint_logs(
    id: str,
    region: str | None = None,
    range: str = "1h",
    search: str = Query("", max_length=200),
    limit: int = Query(300, ge=1, le=1000),
    end: float | None = None,
    p: Principal = Depends(scoped("read:logs")),
):
    m = await asyncio.to_thread(_endpoint_model, id)
    check_model(p, m["name"])
    region = _endpoint_region(m, region)
    return await monitoring.logs("endpoint", m["namespace"], m["k8s_name"], region, range, search, limit, end)


def _monitored(p: Principal, id: str) -> tuple[str | None, str]:
    """(region, kind) of an operation for its metrics and logs: the Job's region (a fleet-placed run: the worker that
    admitted it, None while it waits). Every query is scoped to the caller's own namespace."""
    job, region = _owned(p, id, follow=False)
    return region, "jobset" if jobs.is_jobset(job) else "job"


@app.get("/v1/operations/{id}/metrics", responses={200: {"model": schemas.Metrics}})
async def operation_metrics(
    id: str, range: str = "1h", end: float | None = None, p: Principal = Depends(scoped("read:metrics"))
):
    region, _ = await asyncio.to_thread(_monitored, p, id)
    return await monitoring.metrics("job", p.namespace, id, region or REGION, range, end)


@app.get("/v1/operations/{id}/logs", responses={200: {"model": schemas.Logs}})
async def operation_logs(
    id: str,
    range: str = "1h",
    search: str = Query("", max_length=200),
    limit: int = Query(300, ge=1, le=1000),
    end: float | None = None,
    p: Principal = Depends(scoped("read:logs")),
):
    region, kind = await asyncio.to_thread(_monitored, p, id)
    return await monitoring.logs(kind, p.namespace, id, region or REGION, range, search, limit, end)


def _key_public(i: dict, tenant: str) -> dict:
    exhausted = i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]
    return {
        "id": i.get("key_alias"),
        "key_preview": i.get("key_name"),
        "alias": i.get("key_alias"),
        "tenant": tenant,
        "budget": i.get("max_budget"),
        "spend": i.get("spend", 0),
        "models": i.get("models", []),
        "created_at": i.get("created_at"),
        "expires_at": i.get("expires"),
        "status": "exhausted" if exhausted else "active",
        "role": (i.get("metadata") or {}).get("role", "user"),
    }


def _fleet_only() -> None:
    if not db.enabled():
        raise HTTPException(409, "keys live in the fleet database (DATABASE_URL), which this API does not have")


@app.get("/v1/keys", responses={200: {"model": list[schemas.ApiKey]}})
def list_keys(p: Principal = Depends(admin)):
    _fleet_only()
    return [_key_public(k, p.tenant) for k in keys.list_tenant(p.tenant)]


@app.post("/v1/keys", status_code=201, responses={201: {"model": schemas.ApiKey}})
def create_key(req: KeyRequest, p: Principal = Depends(admin)):
    """A user key of the admin's tenant (alias `<tenant>-<alias>`), with the admin key's pass-through routes; the
    value is returned once and mirrored into LiteLLM (services/api/keys.py)."""
    _fleet_only()
    meta = {"allowed_passthrough_routes": (p.info.get("metadata") or {}).get("allowed_passthrough_routes", [])}
    value, i = keys.create(
        p.tenant,
        f"{p.tenant}-{req.alias}",
        req.budget,
        req.models,
        req.expires_days,
        metadata=meta,
        by=p.info.get("key_alias"),
    )
    return {**_key_public(i, p.tenant), "key": value}


@app.post("/v1/keys:import", responses={200: {"model": schemas.KeyImportReport}})
def import_keys(req: KeyImportRequest, p: Principal = Depends(admin)):
    """Pre-hashed keys of the admin's tenant (docs/API.md "Importing keys"): idempotent, all-or-nothing, nothing
    overwritten, conflicts refused (409 with the list), unknown scopes block the key; `dry_run` classifies only."""
    _fleet_only()
    return keys.import_records(req.keys, p.tenant, p.info.get("key_alias") or p.info.get("key_name"), req.dry_run)


@app.patch("/v1/keys/{alias}", responses={200: {"model": schemas.ApiKey}})
def update_key(alias: str, req: KeyUpdate, p: Principal = Depends(admin)):
    """Change a key's budget, allowed models or expiry; the spend ledger is untouched (docs/RELIABILITY.md)."""
    _fleet_only()
    if req.budget is None and req.models is None and req.expires_days is None:
        raise HTTPException(400, "nothing to change: pass budget, models or expires_days")
    i = keys.update(p.tenant, alias, req.budget, req.models, req.expires_days)
    if i is None:
        raise HTTPException(404, f"key {alias} not found")
    forget_hash(i["token"])  # a cached principal would keep the old budget for AUTH_CACHE_S
    return _key_public(i, p.tenant)


@app.delete("/v1/keys/{alias}", status_code=204)
def revoke_key(alias: str, p: Principal = Depends(admin)):
    _fleet_only()
    if not keys.revoke(p.tenant, alias):
        raise HTTPException(404, f"key {alias} not found")
