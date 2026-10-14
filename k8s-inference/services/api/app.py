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
from datetime import datetime, timedelta, timezone

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
import leases
import models
import monitoring
import placement as placing
import render
import schemas
import sessions
import stages
import status
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
from reconciliation import unschedulable_switch as unschedulable_switch
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


app = FastAPI(title="Nebius Serverless 2.0 customer API", version="0.11.86", lifespan=_lifespan)


INPUT_MAX_BYTES = 60000  # a run's or async call's input lives on its Job (annotation, env): bounded, never truncated


ADMITTED_HEADER = "x-serverless2-admitted"  # the API's own proxied call to a gateway: the hook must not count it again


def _admit(
    p: Principal, operation: str, gpu_seconds: float = 0.0, counts_request: bool = True, run_member: bool = False
) -> dict:
    """Reserve the slot, the request and the GPU seconds of `operation` on the caller's key (keys.admit); nothing
    without the database, nothing for the router key (it carries no budget). `run_member`: the operation is the root
    of a logical run whose continuations join its reservation (keys.prepare_run_member)."""
    if not db.enabled() or p.role == keys.ROUTER["role"]:
        return {"replay": False}
    try:
        return keys.admit(keys.key_hash(p.key), operation, p.tenant, gpu_seconds, counts_request, run_member)
    except keys.KeyLimit as e:
        raise HTTPException(e.status, e.detail)


def _job_deadline(job: dict) -> int | None:
    """The enforced wall-clock bound of a rendered Job or JobSet (its activeDeadlineSeconds; a JobSet's is on its
    replicated Jobs)."""
    spec = job.get("spec") or {}
    if job.get("kind") == "JobSet":
        ds = [
            int(((rj.get("template") or {}).get("spec") or {}).get("activeDeadlineSeconds") or 0)
            for rj in spec.get("replicatedJobs") or []
        ]
        return max(ds) if ds and max(ds) else None
    d = spec.get("activeDeadlineSeconds")
    return int(d) if d else None


def continuation_admission(root: str, predecessor: str | None, transfer_only: bool = False, stage: str | None = None):
    """The admission a continuation builder calls with its rendered Job, right before creating it: the member joins
    the run's reservation on the root's original key (keys.prepare_run_member: key still valid, current head, budget
    for its GPU bound, the slot reused or re-acquired). The returned callable raises HTTPException with the ledger's
    status."""

    def admit(job: dict) -> None:
        try:
            keys.prepare_run_member(
                root,
                job["metadata"]["name"],
                predecessor,
                _job_gpus(job),
                _job_deadline(job),
                transfer_only,
                **({"kind": "stage", "stage": stage} if stage else {}),
            )
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)

    return admit


def _require_current(p: Principal, source: str, root: str) -> None:
    """A public continuation renders from `source` (the Job the caller named), while the ledger admits it after the
    lineage's current head. The two must be the same operation, or the head an export retry / finalizer of the source
    (`export-of` = source: the compute attempt stays where it was): a resume or export of an older member after a
    newer compute successor exists is refused with the head named, never a silent reopening of old state."""
    if not db.enabled():
        return
    member = keys.member_of(root)
    if member is None:
        return
    head = keys.stage_head(member["root"], member["stage"]) if member["kind"] == "stage" else keys.run_head(root)
    if head in (None, source):
        return
    try:
        head_job, _ = jobs.find(p.namespace, head)
        if (head_job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of") == source:
            return
    except HTTPException as e:
        if e.status_code != 404:
            raise
    raise HTTPException(
        409,
        f"operation {source} was continued by {head}; continue {head} (the current member of its lineage), not {source}",
    )


def continue_run(root: str, build, transfer_only: bool = False):
    """Run a continuation builder (jobs.resume/export_again/switch_class/recover, given as a callable taking the
    admission callback) under the run's ledger: the new member follows the current head; a builder that fails after
    admission with no Job created rolls the member back. Without a ledger for the root (no database, a run admitted
    before the ledger existed) the builder runs unadmitted, as for an unbounded key."""
    if not db.enabled():
        return build(None)
    member = keys.member_of(root)
    if member and member["kind"] == "stage":
        # a stage of a graph (docs/JOBS.md "Stage composition"): its continuations follow the stage's own head inside
        # the graph root's run (the root's key, slot and budget), never the root's lineage or a sibling's
        root, stage = member["root"], member["stage"]
        head = keys.stage_head(root, stage)
    else:
        stage = None
        head = keys.run_head(root)
    if head is None:
        return build(None)
    pending: dict = {}

    def admit(job: dict) -> None:
        continuation_admission(root, head, transfer_only, stage=stage)(job)
        pending["name"] = job["metadata"]["name"]

    try:
        return build(admit)
    except Exception:
        if pending.get("name"):
            try:
                keys.rollback_run_member(pending["name"])  # the builder raised at create: no Job exists
            except Exception as e:  # noqa: BLE001
                log.warning("rollback of continuation %s: %s", pending["name"], e)
        raise


def settle_deleted(job: dict) -> None:
    """A run cancelled before it ever started (jobs.cancel deleted it): its member measured nothing and held no GPU
    (never-started is the evidence, not an absent pod): settled at zero, the run closes when nothing else is active."""
    if not db.enabled() or (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/cancelled") != "deleted":
        return
    try:
        keys.settle_run_member(job["metadata"]["name"], 0.0)
    except Exception as e:  # noqa: BLE001 - the reconciler's sweep settles what this missed
        log.warning("settlement of cancelled %s: %s", job["metadata"]["name"], e)


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


OPERATION_HEADER = "x-serverless2-operation"  # the admitted operation a caller names (with its token)
ATTEMPT_HEADER = "x-serverless2-attempt"  # the caller's pod (an endpoint call's attempt)
_admission_cache: dict[tuple, float] = {}  # (operation, attempt, key hash) -> verified until (the readiness loop polls)
_admission_region: dict[tuple, str] = {}  # (operation, attempt) -> the region the caller's Job runs in
ADMISSION_CACHE_S = 30


def _admitted_token(key_hash: str, model: str | None = None, operation: str | None = None) -> str:
    """HMAC the gateway hook verifies on the API's own admitted calls: one API admission + its gateway authorization
    is one logical request. Bound to the key, the model and the operation the admission was for
    (`<key hash>:<model>:<operation>`: a proxied synchronous call's `sync-…` admission, an endpoint call's operation);
    the router key is the shared secret of this fleet. The shorter forms are the tokens of releases up to 0.11.53."""
    msg = ":".join(x for x in (key_hash, model, operation) if x)
    return hmac.new((cfg.ROUTER_KEY or "").encode(), msg.encode(), hashlib.sha256).hexdigest()


def _is_admitted(p: Principal, token: str, model: str, operation: str, attempt: str) -> bool:
    """An exact, bounded proof that the gateway call is the API's own admitted call (docs/API.md "Endpoint calls"): the
    token is the HMAC for this key, this model and the operation named; the operation holds a live reservation on
    this key (admitted, not released: the ledger); for an endpoint call's caller the operation's Job is this key's,
    this model's and not over, and the attempt named is one of its live pods. The reservation and the cancel fence
    are read on every call; only the identity (Job, owner, pod) is remembered for ADMISSION_CACHE_S (the caller's
    readiness loop), so a release or a cancel closes the gate at once. ADMITTED_LEGACY accepts the key-only and
    key+model tokens of releases up to 0.11.53 for the callers they rendered."""
    if not cfg.ROUTER_KEY or not token:
        return False
    key_hash = keys.key_hash(p.key)
    if operation and hmac.compare_digest(token, _admitted_token(key_hash, model, operation)):
        # the live state, on every call (never cached): the reservation and the cancel fence
        if db.enabled():
            try:
                if keys.reservation_key(operation) != key_hash:
                    return False  # not admitted on this key, or released already (terminal, cancelled, billed)
                if not operation.startswith("sync-") and attempts.enabled():
                    marks = attempts._marks(operation)
                    if marks and marks[1]:
                        return False  # cancelled: no fresh work for a cancelled operation
            except Exception as e:  # noqa: BLE001 - no proof without the ledger
                log.warning("admission check of %s: %s", operation, e)
                return False
        if operation.startswith("sync-"):
            return True
        # the identity, cached ADMISSION_CACHE_S (immutable while it holds): the operation's Job is this key's and this
        # model's, not over, and the attempt named is one of its live pods
        now = time.time()
        if _admission_cache.get((operation, attempt, key_hash), 0) > now:
            return True
        try:
            job, region = jobs.find(p.namespace, operation)
        except HTTPException:
            return False
        _admission_region[(operation, attempt)] = region or REGION
        md = job["metadata"]
        if (md.get("annotations") or {}).get(f"{cfg.LABEL}/key") != key_hash:
            return False
        if (md.get("labels") or {}).get(f"{cfg.LABEL}/model") != model or status.finished(job):
            return False
        if not attempt:
            return False
        pods = [
            x
            for x in (job.get("_pods") or [])
            if x["metadata"]["name"] == attempt and (x.get("status") or {}).get("phase") in ("Pending", "Running")
        ]
        if not pods:
            return False
        _admission_cache[(operation, attempt, key_hash)] = now + ADMISSION_CACHE_S
        return True
    if cfg.ADMITTED_LEGACY:
        return hmac.compare_digest(token, _admitted_token(key_hash, model)) or hmac.compare_digest(
            token, _admitted_token(key_hash)
        )
    return False


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


def _deployment_of(m: dict, requested: str | None) -> tuple[str, str]:
    """Where a request for model `m` goes: (the deployment's cluster id as the catalog keys it, the Nebius region this
    API reaches it in). `requested` (a request's `region`) may name the cluster id (`north`) or the region
    (`eu-north1`); nothing = this API's own cluster when the model is deployed there, else the model's only
    deployment. Two deployments in one region are never guessed between (400: name the cluster); a cluster this API
    cannot reach is 400 (one kubeconfig per region, docs/API.md)."""
    deps = list(m.get("regions") or [])
    if not deps:
        raise HTTPException(400, f"model {m.get('name')} is not deployed anywhere")
    by_region: dict[str, list[str]] = {}
    for cid in deps:
        by_region.setdefault(kube.cluster_region(cid), []).append(cid)
    if requested is None:
        own = by_region.get(REGION) or []
        if len(own) > 1:
            raise HTTPException(400, f"model {m['name']} has several deployments in {REGION} ({own}); give region")
        cid = own[0] if own else (deps[0] if len(deps) == 1 or len(by_region) == 1 and len(deps) == 1 else None)
        if cid is None:
            if len(deps) == 1:
                cid = deps[0]
            else:
                raise HTTPException(
                    400, f"model {m['name']} is deployed in {deps}; give region (a cluster id or a region)"
                )
    elif requested in deps:
        cid = requested
    else:
        same = by_region.get(requested) or []
        if not same:
            raise HTTPException(400, f"model {m['name']} is deployed in {deps}, not {requested}")
        if len(same) > 1:
            raise HTTPException(
                400, f"region {requested} has several deployments of {m['name']} ({same}); give the cluster id"
            )
        cid = same[0]
    region = kube.cluster_region(cid)
    if region not in kube.regions():
        raise HTTPException(400, f"cluster {cid} ({region}) is not served by this API (available: {kube.regions()})")
    if len(by_region.get(region) or []) > 1 and kube.cluster_id_or_none(region) != cid:
        raise HTTPException(
            400,
            f"cluster {cid} shares region {region} with {by_region[region]}; this API reaches one cluster per region",
        )
    return cid, region


def _public(m: dict, p: Principal | None = None) -> dict:
    if p is not None and (p.info.get("metadata") or {}).get("role") != "admin":
        m = {**m, "spec": None}  # container configuration belongs to fleet administrators
    if m["mode"] == "run":
        return catalog.to_public(m)
    statuses = []
    for cid in m[
        "regions"
    ]:  # deployments by cluster id; the live state is read in the cluster's region (REGION_KUBECONFIGS)
        region = kube.cluster_region(cid)
        try:
            st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
        except (HTTPException, ApiException):
            st = {"status": "unavailable", "replicas_ready": None}
        statuses.append({"region": region, "cluster": cid, **st})
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
    if sessions.enabled():
        if any(request.headers.get(h) for h in sessions.INJECTED):
            # only the hook sets these towards the worker; a client that sends them is forging a worker identity
            raise HTTPException(403, "x-serverless2-session / x-serverless2-connection are not client headers")
        claim = sessions.claim_of(request.headers)
        if claim:
            # a live session (docs/API.md "Sessions"): the claim binds exactly once, the session already holds the
            # key's slot and request; the worker learns the session and connection ids from the injected headers
            m = catalog.get(model) or _endpoint_model(model)
            s = await asyncio.to_thread(sessions.bind, claim, m["name"])
            return JSONResponse(
                {},
                headers={
                    "x-serverless2-tenant": s["tenant"],
                    "x-serverless2-key-alias": "",
                    sessions.SESSION_HEADER: s["id"],
                    sessions.CONNECTION_HEADER: s["connection"],
                },
            )
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
    verified = await asyncio.to_thread(
        _authorize_model,
        p,
        model,
        token,
        request.headers.get(OPERATION_HEADER, ""),
        request.headers.get(ATTEMPT_HEADER, ""),
        request.method,
    )  # catalog (database) and Kubernetes calls: off the event loop
    # the markers the worker attaches its native execution to come only from this verified context: on an admitted
    # call the operation and attempt the token proved, on any other call empty (Envoy overrides what a client sent)
    return JSONResponse(
        {},
        headers={
            "x-serverless2-tenant": p.tenant,
            "x-serverless2-key-alias": str(p.info.get("key_alias") or ""),
            OPERATION_HEADER: verified[0],
            ATTEMPT_HEADER: verified[1],
        },
    )


def _authorize_model(
    p: Principal, model: str, token: str = "", operation: str = "", attempt: str = "", method: str = "GET"
) -> tuple[str, str]:
    """The gateway's authorization of a call to an endpoint: the key's model allow-list and budget always; a direct
    call needs `invoke:sync` and is counted here; the API's own admitted call (the admission token bound to this key
    and model: a proxied synchronous call, or an endpoint call's caller probing readiness and posting its one
    inference) was counted at admission and needs the scope that admission had, `invoke:sync` or `invoke:async`: an
    asynchronous-only key reaches its own endpoint call, nothing else."""
    m = catalog.get(model) or _endpoint_model(model)
    admitted = _is_admitted(p, token, m["name"], operation, attempt) if p.role != keys.ROUTER["role"] else False
    authorize(p, m["name"], ("invoke:sync", "invoke:async") if admitted else "invoke:sync", budget=True)
    _endpoint_limits(p, "gateway", admitted)
    if admitted and method.upper() == "POST" and not operation.startswith("sync-") and db.enabled():
        # the inference POST of an endpoint call's caller: prepare its native execution (docs/API.md "Native execution
        # leases"); a retry attempt's POST while another attempt's execution is prepared or attached is refused here,
        # so no two native executions overlap under one reservation
        region = _admission_region.get((operation, attempt)) or REGION
        ns = p.namespace
        leases.prepare(
            operation, attempt, region, m["name"], caller_gone=lambda old: leases.caller_gone(ns, old, region)
        )
    if m["mode"] == "run":
        raise HTTPException(404, "not an endpoint")
    if method.upper() == "POST":
        if admitted:
            # one line per admitted inference (never per readiness probe): the proof that the markers left the hook
            # towards the gateway at this instant; a prepared-never-attached execution after this line is a loss
            # downstream of the hook (policy, Knative data path, the worker's reading), never a missing emission
            log.info(
                "authorize %s: admitted POST of %s attempt %s: markers emitted (x-serverless2-operation/-attempt)",
                m["name"],
                operation,
                attempt or "-",
            )
        elif leases.mode_of(m) == "required":
            # a direct POST to an endpoint whose worker requires a lease carries no markers: the worker refuses it
            # (required mode fails closed, unchanged); named here so the refusal is attributable from the API log
            log.info(  # no caller identity in this line: the shared telemetry carries no customer identities
                "authorize %s: a direct POST carries no markers (the model requires a native lease: the worker refuses it)",
                m["name"],
            )
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
    return (operation, attempt) if admitted else ("", "")


@app.get("/v1/keys/me", responses={200: {"model": schemas.ApiKey}})
def key_me(p: Principal = Depends(principal)):
    forget(p.key)
    out = p.public()
    if db.enabled() and p.role != keys.ROUTER["role"]:
        out["usage"] = keys.run_usage(keys.key_hash(p.key))
    return out


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
    if mode == "run" and req.execution and req.execution.stage and not allows_model(p, model):
        # a stage on a model the key is not granted: only as a delegated stage of a root the operator's run class
        # delegates to (docs/JOBS.md "Delegated stages"); the declaration alone confers nothing
        _require_delegation(p, model, req.execution.stage.root, req.execution.stage.id)
        authorize(p, model, "run:submit", budget=True, model_check=False)
    else:
        authorize(p, model, {"sync": "invoke:sync", "async": "invoke:async"}.get(mode, "run:submit"), budget=True)
    if mode not in m["modes"]:
        raise HTTPException(400, f"model {model} supports modes {m['modes']}, not {mode}")
    if mode == "run":
        # a run's region is where its Job is created (the manager on the fleet path, where the dispatcher places it):
        # the class's first region by default; a cluster id or a region name of the class is accepted
        region = req.region or (REGION if REGION in m["regions"] else m["regions"][0])
        if region not in m["regions"] and kube.cluster_region(region) not in m["regions"]:
            raise HTTPException(400, f"model {model} is deployed in {m['regions']}, not {region}")
        region = region if region in m["regions"] else kube.cluster_region(region)
    else:
        _cluster, region = _deployment_of(
            m, req.region
        )  # the endpoint's cluster id, and the region the API reaches it in
    if req.execution and mode == "sync":
        raise HTTPException(
            400, "execution options apply to runs and asynchronous endpoint calls, not to synchronous calls"
        )
    if mode != "run":
        _endpoint_limits(p, mode)
    if mode == "sync":
        sid = f"sync-{uuid.uuid4().hex[:12]}"
        await asyncio.to_thread(_admit, p, sid)  # a slot and a request for the duration of the call
        try:
            return await _sync(m, req, p, region, sid)
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
    timeout = req.timeout_s  # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret = None
    ex = execution.resolve(req.execution, p.namespace, m) if mode in ("run", "async") else None
    stage, graph = _stage_request(ex, mode, p, idempotency_key)
    if stage:
        name = stages.stage_name(stage["root"], stage["id"])
        done = _settled_stage(p, graph, stage)
        if done is not None:
            return JSONResponse(done, status_code=200)  # a stage that ran is never run again: its durable state
        if graph.get("closed_at"):
            raise HTTPException(409, f"the graph of {stage['root']} is closed: no new stage attaches")
    else:
        name = render.op_name(p.owner, model, idempotency_key)
    if mode == "async" and ex:
        # an endpoint call with private input and application-owned output (docs/API.md "Endpoint calls")
        if ex.get("placement") or ex.get("recovery"):
            raise HTTPException(400, "execution.placement and execution.recovery apply to runs, not to endpoint calls")
        if ex.get("io_mode") == "application" and not ex.get("private_input_ref"):
            raise HTTPException(
                400,
                "an application-I/O endpoint call takes its request and its destination from a binding: give private_input_ref",
            )
        if ex.get("private_input_ref") and req.input:
            raise HTTPException(
                400, "the request of an endpoint call with private_input_ref lives in the binding: input must be empty"
            )
    if ex and ex.get("require_converged") and mode != "async":
        raise HTTPException(400, "execution.require_converged applies to endpoint calls (mode async)")
    allowed = (ex.get("placement") or {}).get("allowed_clusters") if ex else None
    if allowed and req.region and req.region not in allowed and kube.cluster_region(req.region) not in allowed:
        # an explicit region that contradicts the caller's own cluster list; an implicit one (the default above) is no
        # request: a fleet run is placed within allowed_clusters, a single-cluster run must be allowed here
        raise HTTPException(400, f"region {req.region} is not in allowed_clusters {allowed}")
    if allowed and not FLEET_MANAGER and mode == "run" and region not in allowed:
        raise HTTPException(400, f"this API runs jobs in {region} only, which allowed_clusters {allowed} excludes")
    if mode == "async":  # the caller Job runs in the endpoint's region, through that region's gateway
        rollout = _rollout_fence(m, region, ex)
        application = bool(ex and ex.get("io_mode") == "application")
        output_prefix = (
            "" if application else f"s3://{artifacts.storage(p.namespace, region)['bucket']}/operations/{name}"
        )
        job, secret = jobs.build_async(
            name,
            m,
            req.input,
            p.tenant,
            p.key,
            req.name,
            idempotency_key,
            timeout,
            output_prefix,
            region,
            ex=ex,
            key_hash=billing.key_hash(p.key),
            admitted=_admitted_token(keys.key_hash(p.key), m["name"], name) if cfg.ROUTER_KEY else None,
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
    if mode == "async" and rollout:
        # what the first request could reach at admission (docs/API.md "Models"): evidence of the ready revision and its
        # image then, not per-request provenance (Knative adds no revision to a response)
        if rollout.get("latest_ready"):
            ann[f"{cfg.LABEL}/endpoint-revision"] = str(rollout["latest_ready"])
        if rollout.get("serving_images"):
            ann[f"{cfg.LABEL}/endpoint-image"] = str(rollout["serving_images"][0])[:1000]
    if stage:
        stages.mark_waiting(job, stage["root"], stage["id"], stages.dependencies(graph, stage["id"]))
    if db.enabled():
        try:
            keys.own_operation(name, p.tenant, p.owner, model)
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)
    with keys.operation_lock(name) if db.enabled() else nullcontext():
        created_job, created = _create_admitted(p, name, job, region, secret, req, mode, stage=stage)
    if created:
        return JSONResponse(jobs.normalise(created_job, m.get("price_per_call")), status_code=202)
    return JSONResponse(
        _operation(p, created_job, created_job["metadata"]["name"], region), status_code=200
    )  # a replay: its current state


def _require_delegation(p: Principal, model: str, root: str, stage: str) -> None:
    """A stage on a model the caller's key is not granted is allowed only when every one of these holds: the root's graph
    exists in this tenant and belongs to this owner, is open, and declared this stage bound to exactly this model; the
    root's run class (operator-owned) lists this model among its `delegates`; the root holds a live reservation on the
    caller's key (the same key, still admitted). Nothing is granted beyond that graph."""
    if not db.enabled():
        raise HTTPException(403, f"model {model} is not granted to this key")
    g = keys.graph_of(root)
    if g is None or g["tenant"] != p.tenant or g["owner"] != p.owner:
        raise HTTPException(403, f"model {model} is not granted to this key (no delegating root {root} of yours)")
    if g.get("cancelled_at") or g.get("closed_at"):
        raise HTTPException(403, f"model {model} is not granted to this key (the graph of {root} is over)")
    if (g.get("models") or {}).get(stage) != model:
        raise HTTPException(
            403, f"model {model} is not granted to this key (stage {stage!r} of {root} is not declared on it)"
        )
    record = keys.operation_owner(root)
    root_model = _model(record[2]) if record else None
    if not root_model or model not in (root_model.get("delegates") or []):
        raise HTTPException(403, f"model {model} is not granted to this key (the root's class does not delegate to it)")
    if keys.reservation_key(root) != keys.key_hash(p.key):
        raise HTTPException(403, f"model {model} is not granted to this key (root {root} is not active on this key)")


def _rollout_fence(m: dict, region: str, ex: dict | None) -> dict | None:
    """The endpoint's revision state at admission of an endpoint call (kube._rollout); with
    `execution.require_converged` the call is refused (409) while the created, ready and rolled-out revisions differ
    (a new revision Pending, the previous one serving). A converged endpoint at zero passes: the fence reads the
    revisions, not the pods. Without the option the state is only recorded."""
    try:
        st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m.get("nodes") or 1))
    except Exception:  # noqa: BLE001
        st = {}
    rollout = st.get("rollout")
    if ex and ex.get("require_converged"):
        if not rollout or not rollout.get("converged"):
            why = (rollout or {}).get("reason") or "rollout state unavailable"
            raise HTTPException(
                409, f"endpoint {m['name']} rollout not converged in {region}: {why} (execution.require_converged)"
            )
    return rollout


def _stage_request(
    ex: dict | None, mode: str, p: Principal, idempotency_key: str | None
) -> tuple[dict | None, dict | None]:
    """A submission that is a declared stage of a root's graph (docs/JOBS.md "Stage composition"): the root's graph
    must exist in this tenant, belong to the same owner, declare the stage, and still be open. Returns (stage, graph)
    or (None, None) for an ordinary submission."""
    stage = (ex or {}).get("stage")
    if not stage:
        return None, None
    if mode != "run":
        raise HTTPException(400, "execution.stage: a stage is a run (not an endpoint call)")
    if not db.enabled():
        raise HTTPException(409, "stage composition needs the control API's ledger (no database here)")
    if idempotency_key:
        raise HTTPException(400, "a stage's identity is its root and id: no Idempotency-Key")
    g = keys.graph_of(stage["root"])
    if g is None or g["tenant"] != p.tenant:
        raise HTTPException(404, f"operation {stage['root']}: no stage graph (declare execution.graph on the root)")
    if g["owner"] != p.owner:
        raise HTTPException(403, f"operation {stage['root']}: the stages of a graph belong to its root's owner")
    if stage["id"] not in (g["stages"] or {}):
        raise HTTPException(400, f"stage {stage['id']!r} is not declared by {stage['root']} ({sorted(g['stages'])})")
    if g.get("cancelled_at"):
        raise HTTPException(409, f"the graph of {stage['root']} is cancelled: no stage attaches")
    # a closed graph (every stage settled): a stage that ran answers with its durable head (_settled_stage); one that
    # never was submitted cannot attach any more (409 below, after the lookup)
    return stage, g


def _settled_stage(p: Principal, g: dict, stage: dict) -> dict | None:
    """A stage whose member already settled (ran, was cancelled) is not run again: its current state, or None when
    the stage has no settled member (a replay of an active stage is the Job's replay)."""
    head = keys.stage_head(stage["root"], stage["id"])
    if head is None:
        return None  # a declared stage is submitted once: its lineage (the stage, its continuations) answers afterwards
    try:
        job, region = _owned(p, head)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        raise HTTPException(
            409, f"stage {stage['id']!r} of {stage['root']} already ran ({head}); nothing durable remains"
        )
    return _operation(p, job, job["metadata"]["name"], region)


def _create_admitted(p, name, job, region, secret, req, mode, stage: dict | None = None):
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
    elif stage:
        # a declared stage joins its root's run on the root's key (keys.prepare_run_member kind=stage: the key still
        # valid, its GPU bound within the budget next to its siblings, the root's slot shared; no head rule)
        try:
            reservation = keys.prepare_run_member(
                stage["root"], name, None, _job_gpus(job), _job_deadline(job), kind="stage", stage=stage["id"]
            )
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)
    else:
        reservation = _admit(
            p,
            name,
            _gpu_reservation(_job_gpus(job), req, p) if mode == "run" else 0.0,
            run_member=mode in ("run", "async"),  # a run or an endpoint call: its continuations join this reservation
        )
    ex = json.loads((job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/execution") or "null") or {}
    if db.enabled() and ex.get("graph") and not reservation.get("replay"):
        # the root's stages are sealed with its admission (docs/JOBS.md "Stage composition"); the run stays open
        # (slot, request) until every declared stage settled or the graph is cancelled
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=int(ex["graph"].get("deadline_s") or stages.DEFAULT_DEADLINE_S)
        )
        keys.declare_graph(name, p.tenant, p.owner, ex["graph"]["stages"], deadline, ex["graph"].get("models"))
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
                    if stage:
                        keys.rollback_run_member(name)
                    else:
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


async def _sync(m: dict, req: InvokeRequest, p: Principal, region: str, admission: str = ""):
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
            {
                "Authorization": f"Bearer {p.key}",
                ADMITTED_HEADER: _admitted_token(keys.key_hash(p.key), m["name"], admission),
                OPERATION_HEADER: admission,
            },
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
            op.update(attempts.lifecycle(name, root, rows.get(root, []), marks.get(name), pods=j.get("_pods")))
        out.append(op)
    return out


def _root(job: dict) -> str:
    return (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or job["metadata"]["name"]


def _ledger_head(ns: str, operation: str) -> str | None:
    return stages.ledger_head(ns, operation)


def _owned(p: Principal, id: str, follow: bool = True) -> tuple[dict, str]:
    """The operation's Job. A handle that a continuation superseded (capacity fallback, recovery, a manual resume)
    resolves to its current successor: the successor annotation on the Job, the durable mark once the Job is gone,
    and the run ledger's lineage head as the authority behind both (the original stays readable through its rows);
    an operation whose Job expired is served from its durable rows. The owner check applies to every operation on the
    way, the named one and the head."""
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
            last = rows[-1] if rows else {}
            synthetic_job = {
                "metadata": {
                    "name": current,
                    "labels": {
                        f"{cfg.LABEL}/tenant": p.tenant,
                        f"{cfg.LABEL}/mode": last.get("mode") or "run",
                        **(
                            {f"{cfg.LABEL}/model": last["model"]} if last.get("model") else {}
                        ),  # the allow-list applies
                    },
                    "annotations": {f"{cfg.LABEL}/resumed-from": last.get("root") or current},
                }
            }
            _check_operation_owner(p, synthetic_job)
            synthetic = attempts.synthesize(current, p.tenant)
            nxt = attempts.successor_of(current) or (_ledger_head(p.namespace, current) if follow else None)
            if follow and nxt and nxt != current:
                if synthetic is not None:  # the named operation's durable view stands in when its head is gone too
                    previous = ({**synthetic_job, "_synthetic": synthetic}, synthetic.get("region"))
                current = nxt
                continue
            if synthetic is None:
                if previous is not None:  # a successor that is marked but not created yet: the original stands
                    return previous
                raise
            return {**synthetic_job, "_synthetic": synthetic}, synthetic.get("region")
        _check_operation_owner(p, job)
        nxt = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/successor") or (
            _ledger_head(p.namespace, current) if follow else None
        )
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
    # every member's attempts from that member's own live pods and records (a member listed without pods, or expired,
    # stands on its records alone); the queried member's pods are the ones its handle already carries
    pods_by_member = {m["metadata"]["name"]: m.get("_pods") or [] for m in members}
    pods_by_member[name] = job.get("_pods") or pods_by_member.get(name) or []
    all_attempts = []
    for mid, pods in pods_by_member.items():
        for a in st._attempts(pods, rows, name=mid):
            a["operation"] = mid
            all_attempts.append(a)
    for a in st._attempts([], [r for r in rows if r.get("operation") not in pods_by_member]):
        a["operation"] = (by_pod.get(a.get("attempt_id")) or {}).get("operation") or name  # an expired member
        all_attempts.append(a)
    for a in all_attempts:
        r = by_pod.get(a.get("attempt_id")) or {}
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
                "cost": st.measured(ann.get(f"{cfg.LABEL}/billed")),  # "unknown" (settled without a measurement) = null
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
        and (
            (j["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/attempt-role") == "finalize"
            or (j["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") == "async"
        )
    ]
    role = (
        "export" if xs and (xs[0]["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") == "async" else "finalize"
    )
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
                "attempt_role": role,
                "cluster": (latest.get("cluster") or kube.cluster_id_or_none(latest.get("region")))
                if latest
                else out.get("cluster"),
            }
        )
    return out


def _delivered(op: dict, name: str, root: str) -> dict:
    """What a transfer-only retry or an application finalizer (`<name>-x<n>`) that delivered means for `name`
    (docs/JOBS.md "Attempts and finalization", "Finalization by the application"): a run or an endpoint call whose
    inference succeeded is SUCCEEDED with the delivering attempt's result; a delivered capture of an endpoint error
    (the retry's exit code 2 or 5, the captured status class) stays FAILED and reports the delivery next to it, so
    delivery never turns a failed inference into a success."""
    done = [
        r
        for r in attempts.for_operation(root)
        if r["operation"].startswith(f"{name}-x")
        and (
            (r.get("export_state") or r.get("export")) == "succeeded"
            or (r.get("result_ref") and r["status"] == "succeeded")
        )
    ]
    if not done:
        return {}
    last = done[-1]
    out = {
        "export_state": last.get("export_state") or "succeeded",
        "exported_by": last["operation"],
        # the delivering attempt: what a consumer of the result (a dependent stage, DEPENDENCY_RESULTS) must name
        "exported_by_attempt_id": last.get("pod"),
        "exported_by_epoch": last.get("epoch"),
        "result_ref": last.get("result_ref") or op.get("result_ref"),
    }
    if last.get("exit_code") in (2, 5) or (last["status"] != "succeeded" and last.get("exit_code")):
        out.update(
            {
                "status": "FAILED",
                "error": "the endpoint answered an error; its response was delivered (see result_ref), inference did not succeed",
            }
        )
    else:
        out.update({"status": "SUCCEEDED", "error": None})
    return out


def _operation(p: Principal, job: dict, id: str, region: str | None) -> dict:
    if job.get("_synthetic"):
        op = dict(job["_synthetic"])
        if op["status"] in ("EXPORT_FAILED", "FINALIZING", "FAILED") and attempts.enabled():
            root = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or op["id"]
            op.update(_delivered(op, op["id"], root))  # a retry that delivered after the Job was gone
        return op
    name = job["metadata"]["name"]
    records, root = _records(p, job, name, region)
    op = jobs.normalise(job, _price(job), records)
    op.update(attempts.lifecycle(name, root, records, pods=job.get("_pods")))
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
    if op["status"] in ("FAILED", "EXPORT_FAILED") and attempts.enabled():
        # a run's application finalizer, or an endpoint call's transfer-only retry: while one runs, the root's handle
        # shows FINALIZING and the active attempt (the identity a refresh broker verifies against the pod's token)
        op.update(_finalizers(p, name, root))
    if op["status"] in ("EXPORT_FAILED", "FINALIZING", "FAILED") and attempts.enabled():
        op.update(_delivered(op, name, root))  # a retry or finalizer that delivered closes the original
    _stage_fields(p, op, job, root)
    ann = job["metadata"].get("annotations") or {}
    if ann.get(f"{cfg.LABEL}/endpoint-revision") or ann.get(f"{cfg.LABEL}/endpoint-image"):
        op["endpoint_revision"] = ann.get(f"{cfg.LABEL}/endpoint-revision")
        op["endpoint_image"] = ann.get(f"{cfg.LABEL}/endpoint-image")
    if op.get("mode") == "async" and db.enabled():
        op["native"] = leases.public(leases.get(name), leases.history(name))  # the worker's side (docs/API.md)
    return op


def _stage_fields(p: Principal, op: dict, job: dict, root: str) -> None:
    """Stage composition on the handle (docs/JOBS.md "Stage composition"): a root with a graph reports its stages and
    is terminal only with them; a stage reports its root, dependencies and why it still waits."""
    if st := stages.stage_of(job):
        op["stage"] = {k: v for k, v in st.items() if k != "waiting"}
    if not db.enabled():
        return
    g = keys.graph_of(root)
    if g and g["tenant"] == p.tenant:
        stages.apply_to_root(op, stages.graph_view(p.namespace, g))


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
    if op["mode"] == "async" and not application:
        res["result"] = artifacts.read_json(
            p.namespace, f"operations/{id}/out/response.json", region or REGION, prefix=prefix
        )
    elif op["mode"] == "async":
        # application-owned output (docs/JOBS.md "Endpoint calls with private input and application-owned output"): the
        # capture is in the application's storage; `result_ref` names it, `export_state` says it arrived; a FAILED
        # call (the endpoint answered 4xx) is delivered the same way and reports both
        res["result"] = None
        res["exported_by"] = op.get("exported_by")
        res["capture_attempt_id"] = op.get("attempt_id")
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
    orig, region = _owned(p, id, follow=False)  # the named member itself: _require_current names the head
    exported = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of")
    if exported:  # a previous retry's handle: the retry is of the compute attempt, never of a retry
        orig, region = _owned(p, exported, follow=False)
    op = _operation(p, orig, orig["metadata"]["name"], region)
    _require_current(
        p,
        orig["metadata"]["name"],
        (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or orig["metadata"]["name"],
    )
    if op.get("finalizing_by"):
        # one active finalizer per root: the call is idempotent while it runs
        active, _ = _owned(p, op["finalizing_by"], follow=False)
        return JSONResponse(jobs.normalise(active, _price(active)), status_code=200)
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    if op["mode"] == "async":
        # an endpoint call is delivered again when its capture is complete and its delivery failed: EXPORT_FAILED (a 2xx
        # capture), or FAILED with export_state failed when the capture is an endpoint error (exit 2 or 5); an unknown
        # outcome (exit 4) or a retry that found nothing (exit 3) has no capture to deliver
        last_exit = next(
            (r.get("exit_code") for r in reversed(op.get("attempts") or []) if r.get("exit_code") is not None), None
        )
        failed_capture = op["status"] == "FAILED" and op.get("export_state") == "failed" and last_exit in (0, 2, 5)
        if op["status"] not in ("EXPORT_FAILED", "FINALIZING") and not failed_capture:
            raise HTTPException(
                409,
                f"operation {id} is {op['status']}"
                + (" with no complete capture (inference outcome unknown)" if last_exit == 4 else "")
                + "; only an endpoint call whose capture is complete and undelivered is delivered again",
            )
        root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or orig["metadata"]["name"]
        job, created = continue_run(
            root,
            lambda admit: jobs.export_async(p.namespace, orig["metadata"]["name"], _home(orig), m, admit=admit),
            transfer_only=True,
        )
        return _continued(p, job, created, region)
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
    root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or orig["metadata"]["name"]
    job, created = continue_run(
        root,
        lambda admit: jobs.export_again(
            p.namespace,
            orig["metadata"]["name"],
            _home(orig),
            m,
            catalog.region_params(m, region) if region else {},
            finalize=finalize,
            admit=admit,
        ),
        transfer_only=True,
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


# ---- live sessions (docs/API.md "Sessions") -----------------------------------------------------------------------
@app.post("/v1/models/{model}:session", responses={202: {"model": schemas.Session}}, status_code=202)
def create_session(
    model: str, req: schemas.SessionRequest | None = None, p: Principal = Depends(scoped("invoke:sync"))
):
    """An admitted stream to an endpoint: the key's slot and request are taken now (and, for a key with a GPU-second
    budget, the endpoint's GPUs x timeout_s); the answer carries a one-time claim token the connection presents
    (`Sec-WebSocket-Protocol: s2-session.<token>` or `x-serverless2-session-claim`) within SESSION_CLAIM_TTL_S."""
    sessions.require()
    req = req or schemas.SessionRequest()
    m = _model(model)
    authorize(p, m["name"], "invoke:sync", budget=True)
    if m["mode"] == "run":
        raise HTTPException(400, "sessions are streams to an endpoint; this model is a run class")
    if p.role == keys.ROUTER["role"]:
        raise HTTPException(403, "the router key opens no sessions")
    bound = sessions.transport_deadline_s(m)
    if req.timeout_s is None:
        req.timeout_s = bound if bound is not None else 3600  # the longest the transport carries, never above it
    if bound is not None and req.timeout_s > bound:
        # the stream would be cut by the endpoint's transport before the session's deadline, mid-stream, with no record
        # on the session: refused here with the number to use instead (never silently shortened)
        raise HTTPException(
            400,
            f"session timeout_s {req.timeout_s} exceeds the endpoint's transport deadline of {bound} s (the model's "
            f"timeout_s: Knative ends every request of a single-node endpoint there, WebSocket or NDJSON alike); ask for "
            f"timeout_s <= {bound}, or raise the model's timeout_s within the fleet's Knative revision maximum",
        )
    cluster, region = _deployment_of(m, req.region)
    try:
        return sessions.create(
            keys.key_hash(p.key),
            p.tenant,
            p.owner,
            m["name"],
            cluster,
            region,
            req.timeout_s,
            sessions.gpus_per_replica(m),
        )
    except keys.KeyLimit as e:
        raise HTTPException(e.status, e.detail)


def _owned_session(p: Principal, id: str) -> dict:
    s = sessions.get(id)
    if s is None or s["tenant"] != p.tenant or s["owner"] != p.owner:
        raise HTTPException(404, f"session {id}: not found")
    return s


@app.get("/v1/sessions", responses={200: {"model": list[schemas.Session]}})
def list_sessions(limit: int = Query(default=100, ge=1, le=500), p: Principal = Depends(scoped("read:operations"))):
    sessions.require()
    return sessions.list_owner(p.tenant, p.owner, limit)


@app.get("/v1/sessions/{id}", responses={200: {"model": schemas.Session}})
def get_session(id: str, p: Principal = Depends(scoped("read:operations"))):
    sessions.require()
    return sessions.public(_owned_session(p, id))


@app.post("/v1/sessions/{id}:cancel", responses={200: {"model": schemas.Session}})
def cancel_session(id: str, p: Principal = Depends(scoped("invoke:sync"))):
    """A cancellation request: a session nobody bound ends now; a bound or attached one keeps its slot until its
    worker releases it after cleanup or its pod is confirmed gone (the reconciler)."""
    sessions.require()
    _owned_session(p, id)
    return sessions.public(sessions.cancel(id, "cancelled by the owner"))


async def _worker_session(id: str, authorization: str) -> tuple[dict, dict]:
    """The session and the verified identity of the worker pod calling with its own bound ServiceAccount token."""
    sessions.require()
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a worker calls with its pod's ServiceAccount token")
    s = sessions.get(id)
    if s is None:
        raise HTTPException(404, "no such session")
    worker = await asyncio.to_thread(sessions.verify_worker, authorization[7:].strip(), s, _endpoint_model)
    return s, worker


@app.post("/internal/sessions/{id}:attach", include_in_schema=False)
async def attach_session(id: str, body: schemas.SessionAttach, authorization: str = Header(default="")):
    """The worker that accepted the connection, before any decoder, reset or GPU work: `bound -> attached` for the
    connection id the hook injected; a retry with the same (connection, pod) is idempotent; after the attach wait,
    a cancel or the deadline it fails for good."""
    _s, worker = await _worker_session(id, authorization)
    s = await asyncio.to_thread(sessions.attach, id, body.connection, worker, body.worker_session_id)
    return {**sessions.public(s), "connection": s["connection"]}


@app.post("/internal/sessions/{id}:release", include_in_schema=False)
async def release_session(id: str, body: schemas.SessionRelease, authorization: str = Header(default="")):
    """The attached worker, after its own post-done cleanup: the only release of an attached session (besides
    confirmed pod loss). `gpu_seconds` null = unknown (unmetered): the bound stays against the budget."""
    s, worker = await _worker_session(id, authorization)
    if s.get("connection") and body.connection != s["connection"]:
        raise HTTPException(409, "not this session's connection")
    return sessions.public(await asyncio.to_thread(sessions.release, id, worker, body.outcome, body.gpu_seconds))


@app.get("/internal/sessions/{id}", include_in_schema=False)
async def watch_session(id: str, authorization: str = Header(default="")):
    """The attached worker's view (only it may watch): status, `cancel_requested`, the deadline."""
    s, worker = await _worker_session(id, authorization)
    if s["state"] != "attached" or s["pod_uid"] != worker["uid"]:
        raise HTTPException(403, "only the attached worker watches its session")
    return {
        "id": id,
        "status": s["state"],
        "cancel_requested": s["cancel_requested_at"] is not None,
        "cancel_reason": s["cancel_reason"],
        "deadline_at": sessions._ts(s["deadline_at"]),
    }


# ---- native execution leases (docs/API.md "Native execution leases") ----------------------------------------------
async def _worker_execution(operation: str, authorization: str) -> tuple[dict, dict]:
    if not db.enabled():
        raise HTTPException(404, "native execution leases need the control API's ledger")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a worker calls with its pod's ServiceAccount token")
    e = leases.get(operation)
    if e is None:
        raise HTTPException(404, f"operation {operation}: no prepared native execution")
    worker = await asyncio.to_thread(leases.verify_worker, authorization[7:].strip(), e, _endpoint_model)
    return e, worker


@app.post("/internal/executions/{operation}:attach", include_in_schema=False)
async def attach_execution(operation: str, body: schemas.NativeAttach, authorization: str = Header(default="")):
    """The worker, atomically before it enqueues GPU work for the request carrying these markers: the prepared
    execution of this attempt becomes attached to this pod; a stale attempt, another pod or no prepared execution is
    409 and the worker fails closed. Idempotent for the same tuple (a dropped response is retried safely)."""
    _e, worker = await _worker_execution(operation, authorization)
    out = leases.public(await asyncio.to_thread(leases.attach, operation, body.attempt, worker, body.worker_request_id))
    out["owner"] = _owner_context(operation)
    return out


@app.post("/internal/executions/{operation}:release", include_in_schema=False)
async def release_execution(operation: str, body: schemas.NativeRelease, authorization: str = Header(default="")):
    """The attached worker, after its own settlement (natural completion, or the stop ACK of a drain): outcome, GPU
    seconds as measured (null = unknown). Frees nothing by itself: the member settles at the join with the caller."""
    _e, worker = await _worker_execution(operation, authorization)
    return leases.public(
        await asyncio.to_thread(
            leases.release, operation, body.attempt, worker, body.worker_request_id, body.outcome, body.gpu_seconds
        )
    )


@app.get("/internal/executions/{operation}", include_in_schema=False)
async def watch_execution(operation: str, authorization: str = Header(default="")):
    """The attached worker's view: `cancel_requested` (the cooperative abort signal) and the state."""
    e, worker = await _worker_execution(operation, authorization)
    if e["state"] != "attached" or e["pod_uid"] != worker["uid"]:
        raise HTTPException(403, "only the attached worker watches its execution")
    return {
        "operation": operation,
        "state": e["state"],
        "cancel_requested": e["cancel_requested_at"] is not None,
        "cancel_reason": e["cancel_reason"],
        "owner": _owner_context(operation),
    }


def _owner_context(operation: str) -> dict | None:
    """The trusted owner context of an admitted operation, for the worker that attached to it (docs/API.md "Native
    execution leases"): the operation's durable owner record (tenant, the stable opaque owner id, model), written at
    admission by the API and readable only through the pod-authenticated lease routes; never a client header."""
    record = keys.operation_owner(operation) if db.enabled() else None
    if not record:
        return None
    tenant, owner, model = record
    return {"tenant": tenant, "owner": owner, "model": model}


@app.post("/v1/operations/{id}:cancel", responses={200: {"model": schemas.Operation}})
def cancel(id: str, p: Principal = Depends(scoped("run:control"))):
    job, region = _owned(p, id, follow=False)  # cancel the operation named, never its continuation
    if job.get("_synthetic"):
        # the Job is gone (TTL). A graph root is still the handle its owner cancels the graph by (docs/JOBS.md "Stage
        # composition"): every stage member and the root lineage's head go, nothing attaches any more; any other
        # expired operation has nothing left to cancel
        return _cancel_graph_of(p, id, job)
    current = job["metadata"]["name"]
    op = _operation(p, job, id, region)
    if op.get("finalizing_by"):  # the root's handle: a cancel stops its active finalizer, the root stays as it is
        job, _ = _owned(p, op["finalizing_by"], follow=False)
        current = job["metadata"]["name"]
    attempts.mark_cancelled(current)  # a late "succeeded" from the cancelled attempt is refused from now on
    cancelled = jobs.cancel(p.namespace, current, _home(job))
    settle_deleted(cancelled)
    op = jobs.normalise(cancelled)
    if op["status"] in ("QUEUED", "RUNNING"):
        op["status"] = "CANCELLED"  # the Job controller converges within seconds (pods get SIGTERM)
    if db.enabled() and (job["metadata"].get("labels") or {}).get(f"{cfg.LABEL}/mode") == "async":
        leases.request_cancel(current, "cancelled by the owner")  # a request to the attached worker, never a release
    root = (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    if db.enabled() and (g := keys.graph_of(root)) and not g.get("cancelled_at"):
        # the root of a graph: every stage goes with it, and so does the root lineage's current head (a resumed root
        # that is still queued or running); nothing attaches any more (docs/JOBS.md "Stage composition")
        names = _cancel_graph_members(p, root, current)
        op["graph"] = stages.graph_view(p.namespace, keys.graph_of(root))
        op["status"] = "CANCELLED"
        log.info("cancel %s: stages cancelled: %s", root, names)
    return op


def _cancel_graph_members(p: Principal, root: str, current: str) -> list[str]:
    """The graph's cancellation: its stage members, and the root lineage's head when that is another, still active
    operation (a manual continuation of the root): marked, stopped, settled like any cancel. The graph is closed."""
    why = f"cancelled by the caller with root {root}"
    names = stages.cancel_members(p.namespace, root, why)
    head = keys.run_head(root)
    if head and head != current:
        try:
            hj, hr = jobs.find(p.namespace, head)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            hj = None
        if hj is not None and not status.finished(hj):
            attempts.mark_cancelled(head)
            settle_deleted(jobs.cancel(p.namespace, head, _home(hj)))
            names.append(head)
    keys.cancel_graph(root, why)
    return names


def _cancel_graph_of(p: Principal, id: str, synthetic: dict) -> dict:
    """`:cancel` on an operation whose Job expired: a graph root still cancels its graph (the stages and the root
    lineage's head); anything else is over."""
    root = (synthetic["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    if not (db.enabled() and (g := keys.graph_of(root))):
        raise HTTPException(409, f"operation {id} is over; nothing to cancel")
    op = dict(synthetic["_synthetic"])
    if not g.get("cancelled_at"):
        attempts.mark_cancelled(id)
        names = _cancel_graph_members(p, root, id)
        log.info("cancel %s (expired root): graph cancelled: %s", root, names)
    op["graph"] = stages.graph_view(p.namespace, keys.graph_of(root))
    op["status"] = "CANCELLED"
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
    orig, region = _owned(p, id, follow=False)  # the named member itself: _require_current names the head
    m = _model(orig["metadata"]["labels"].get("serverless2.nebius/model", ""))
    req = req or ResumeRequest()
    if req.gpu_class and req.gpu_class not in jobs.gpu_classes(m):
        raise HTTPException(400, f"gpu_class {req.gpu_class} is not one of {jobs.gpu_classes(m)}")
    root = (orig["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/resumed-from") or id
    _require_current(p, orig["metadata"]["name"], root)
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
    job, created = continue_run(
        root,
        lambda admit: jobs.resume(
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
            admit=admit,
            restart_from_inputs=bool(req.restart_from_inputs),
        ),
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
def put_binding(
    ref: str,
    req: BindingRequest,
    p: Principal = Depends(scoped("artifacts:write")),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Create or replace the caller's binding. A binding belongs to the principal that created it: another principal
    of the tenant cannot read, replace, revoke or consume it (404); an admin may. `If-Match: <revision>` makes the
    replacement a compare-and-swap (409 when the binding changed meanwhile)."""
    return execution.put_binding(p.namespace, p.tenant, ref, req.data, p.owner, p.role == "admin", if_match)


@app.get("/v1/bindings", responses={200: {"model": list[schemas.Binding]}})
def list_bindings(p: Principal = Depends(principal)):
    return execution.list_bindings(p.namespace, p.owner, p.role == "admin")


@app.delete("/v1/bindings/{ref}", status_code=204)
def delete_binding(ref: str, p: Principal = Depends(scoped("artifacts:write"))):
    if not execution.delete_binding(p.namespace, ref, p.owner, p.role == "admin"):
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
    """Every deployment of every endpoint the key may see: one entry per deployment cluster (`cluster` is the id the
    catalog keys it by, `region` the Nebius region the API reaches it in). `region` selects a cluster id or a region."""
    served_regions = kube.regions()
    if region and region not in served_regions and kube.cluster_region(region) not in served_regions:
        raise HTTPException(400, f"region {region} is not served by this API (available: {served_regions})")
    out = []
    for m in catalog.all_models().values():
        if not allows_model(p, m["name"]):
            continue
        if m["mode"] == "run":
            continue
        for cid in m["regions"]:
            cluster_region = kube.cluster_region(cid)
            if region and region not in (cid, cluster_region):
                continue
            if cluster_region not in served_regions:
                continue
            try:
                _deployment_of(m, cid)  # 400 for a cluster this API cannot tell apart from another in its region
                i = _deployed(m, cluster_region)
                if i:
                    out.append(
                        {
                            **ep.to_public(m, i, cluster_region),
                            "cluster": cid,
                            **(
                                {"variants": vs, "routing": m.get("routing")}
                                if (vs := _variants_public(m, cluster_region))
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
                        "region": cluster_region,
                        "cluster": cid,
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
    """The Nebius region an endpoint request is served in, from a cluster id or a region (`_deployment_of`)."""
    return _deployment_of(m, region)[1]


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
