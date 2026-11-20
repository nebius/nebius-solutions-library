"""Model catalog: the definitions of the fleet database (services/api/models.py `definition`, one shape for the
renderer and every reader) and the bundled files of catalog/models/*.yaml (catalog/README.md): the built-in run
classes, written in the catalog's entry shape (id, displayName, description, task, mode: run, gpu {count, classes},
price {unit, usd}, job {image, command, gpu, cpu, memory, pvcSizeGi, ...}, parameters [], deployments {<cluster>:
{parameters, price_per_gpu_hour, paused}}; "hub" = this API's region). A file endpoint (the test fixtures) is a spec of
docs/API.md "Models" plus its placement under `deployments` and the display fields `price`, `cold_start_class`."""

import glob
import os

import yaml

import db
from config import CATALOG_DIRS, HUB_REGION, LABEL, MODELS_NAMESPACE

_cache: dict = {"mtime": None, "models": {}}
FILE_FIELDS = ("price", "cold_start_class", "cold_start_s", "deployments")  # of a file endpoint, beside its spec


def _load() -> dict:
    models = {}
    for d in CATALOG_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(f) as fh:
                doc = yaml.safe_load(fh) or {}
            if doc.get("id"):
                m = normalise(doc) if doc.get("mode") == "run" or doc.get("kind") == "job" else endpoint_file(doc)
                models[m["name"]] = m
    return models


def endpoint_file(doc: dict) -> dict:
    """A file endpoint (the module docstring): its spec, its placement as written, its display fields."""
    import models as runtime_store

    spec = {k: v for k, v in doc.items() if k not in FILE_FIELDS}
    m = runtime_store.definition(spec, managed_by="catalog", deployments=doc.get("deployments") or {})
    m.update({k: doc[k] for k in FILE_FIELDS[:-1] if k in doc})
    _price(m)
    return m


def _price(m: dict) -> None:
    """A file's `price {unit, usd}` as the catalog shows it: "$<usd> / <unit>", `price_per_call` for a per-call unit."""
    price = m.get("price")
    if isinstance(price, dict) and price:
        m["price"] = f"${price['usd']} / {price['unit']}"
        m["price_per_call"] = price.get("usd") if price.get("unit") == "call" else None


def normalise(doc: dict) -> dict:
    """A run class's entry (a bundled file, or `models.definition` of an API-defined class) as every reader holds it."""
    m = dict(doc)
    gpu = m.get("gpu") or {}
    eps = {k: v for k, v in (m.get("endpoints") or {}).items() if k not in ("ready", "live", "metrics")}
    m["name"] = m["id"]
    m["display_name"] = m.get("displayName", m["id"])
    m.setdefault(
        "managed_by", "catalog"
    )  # catalog (bundled file, read-only) | api (services/api/models.py, the fleet database)
    m.setdefault("k8s_name", m["id"].replace(".", "-"))
    m.setdefault("mode", "sync")
    m.setdefault("modes", ["run"] if m["mode"] == "run" else ["sync", "async"])
    m.setdefault("namespace", MODELS_NAMESPACE)
    m["endpoints"] = eps
    m.setdefault("endpoint_path", next(iter(eps.values()), "/"))
    _price(m)
    m["gpu"] = (
        f"{gpu.get('count', 1)}x {'/'.join(gpu.get('classes', []))}"
        if isinstance(gpu, dict) and gpu
        else (gpu or "none")
    )
    m["gpu_classes"] = (
        list(gpu.get("classes") or []) if isinstance(gpu, dict) else []
    )  # preferred first (scheduling profile)
    m["nodes"] = int(m.get("nodes") or 1)
    m["cold_start_class"] = m.get("coldStartClass")
    if not m.get("deployments") and m["mode"] == "run":
        import kube  # lazy: catalog must not import kube at load

        # a run class without deployments runs in every region of the fleet (the fleet document's pools), else here
        regions = sorted(
            {kube.cluster_region(p["region"]) for p in (kube.fleet().get("pools") or {}).values() if p.get("region")}
        )
        m["deployments"] = {r: {} for r in (regions or kube.regions())}
    dep = {
        (k if k != "hub" else HUB_REGION): (v or {})
        for k, v in (m.get("deployments") or {"hub": {}}).items()
        if not (v or {}).get("paused")
    }
    m["deployments"] = dep
    m["regions"] = list(dep)
    if m["mode"] != "run" and not m.get("endpoint_host"):
        m["endpoint_host"] = (
            f"http://{m['k8s_name']}-predictor.{m['namespace']}.svc.cluster.local"  # on this cluster only
        )
    if m["mode"] == "run":
        m["job"] = dict(m.get("job") or {})  # image, command, gpu, cpu, memory, ... (docs/JOBS.md)
        m["delegates"] = [str(x) for x in (m["job"].get("delegates") or [])]  # docs/JOBS.md "Delegated stages"
        images = m["job"].get("images")  # per-GPU-class images (docs/SCHEDULING.md "Per-GPU images for runs")
        if isinstance(images, dict) and images.get("default") and not m["job"].get("image"):
            m["job"]["image"] = images["default"]
        m["protocol"] = m.get("protocol") or "kubernetes-job"
    return m


_runtime: dict = {"until": 0.0, "models": {}}
RUNTIME_TTL_S = 5.0


def runtime_models(force: bool = False) -> dict:
    """The definitions of the fleet database (services/api/models.py), re-read every few seconds or after a write."""
    import time

    if force or _runtime["until"] < time.time():
        import models as runtime_store

        _runtime["models"] = runtime_store.list_runtime()
        _runtime["until"] = time.time() + RUNTIME_TTL_S
    return _runtime["models"]


def invalidate():
    _runtime["until"] = 0.0


def all_models() -> dict:
    key = tuple(sorted((f, os.path.getmtime(f)) for d in CATALOG_DIRS for f in glob.glob(os.path.join(d, "*.yaml"))))
    if _cache["mtime"] != key:
        _cache["models"], _cache["mtime"] = _load(), key
    # file entries (the bundled classes, Terraform's catalog) win over a runtime entry of the same id
    return {**runtime_models(), **_cache["models"]}


def get(name: str) -> dict | None:
    return all_models().get(name)


def pinned(model: dict, ann: dict) -> dict:
    """The model as the operation was rendered from it: a resume or an export retry re-renders from the revision the
    Job pinned (`serverless2.nebius/model-version`, models_history), never silently from a newer definition."""
    want = ann.get(f"{LABEL}/model-version")
    if not want or str(model.get("version")) == str(want) or model.get("managed_by") != "api" or not db.enabled():
        return model
    import models as runtime_store

    for h in db.history(model["name"], 500):
        if str(h["version"]) == str(want) and h.get("spec"):
            return runtime_store.definition(h["spec"], managed_by="api", version=h["version"])
    return model


def region_params(m: dict, region: str) -> dict:
    """Per-region parameter defaults for run models (deployments.<region>.parameters)."""
    return dict((m["deployments"].get(region) or {}).get("parameters") or {})


def fleet_params(m: dict) -> dict:
    """Parameter defaults for a run placed by the fleet (region unknown at render time): the
    `deployments.<region>.parameters` every deployment agrees on. A per-region value (a pool pin, a
    per-region image) is left out; the entry's `parameters[].default` or the caller must supply it."""
    sets = [region_params(m, r) for r in m["regions"]]
    if not sets:
        return {}
    return {k: v for k, v in sets[0].items() if all(s.get(k) == v for s in sets[1:])}


def to_public(m: dict, region_status: list | None = None) -> dict:
    """Shape of docs/API.md Model."""
    price = m.get("price") or (f"${m['price_per_call']} / call" if m.get("price_per_call") is not None else "metered")
    return {
        "id": m["name"],
        "name": m["display_name"],
        "description": m.get("description", ""),
        "modes": m["modes"],
        "default_mode": m["mode"],
        "gpu": m.get("gpu", "none"),
        "price": price,
        "price_per_call": m.get("price_per_call"),
        "image": m.get("image") or ((m.get("job") or {}).get("image") if m["mode"] == "run" else None),
        "protocol": m.get("protocol"),
        "cold_start_s": m.get("cold_start_s"),
        "cold_start_class": m.get("cold_start_class"),
        "parameters": m.get("parameters", []),
        "endpoints": m.get("endpoints"),
        "task": m.get("task"),
        "managed_by": m.get("managed_by", "catalog"),
        "spec": m.get("spec"),
        "example": m.get("example"),  # a valid request body (the console's "Test request" tab)
        "regions": region_status or [{"region": r, "status": "ready"} for r in m["regions"]],
        "execution": execution_identity(m) if m["mode"] == "run" else None,
        "native_lease": (m.get("native_lease") or "optional") if m["mode"] != "run" else None,
    }


def execution_identity(m: dict) -> dict:
    """The sanitized execution identity of a run class (docs/API.md "Models"): the image reference as registered,
    whether it is digest-pinned, the per-class images, the classes, the node count, the operator-approved delegate
    ids, the retry policy, whether a finalizer exists. Never the command, args, env, secrets, volumes or profiles."""
    job = m.get("job") or {}
    image = job.get("image")
    images = job.get("images") if isinstance(job.get("images"), dict) else None
    retry = job.get("retry") if isinstance(job.get("retry"), dict) else None
    try:
        nodes = int(job.get("nodes", 1))  # a template token (`{{nodes}}`): decided per run
    except (TypeError, ValueError):
        nodes = None
    return {
        "image": image,
        "immutable": bool(image and "@sha256:" in image),
        "images": {k: v for k, v in images.items() if k != "default"} if images else None,
        "gpu_classes": list(m.get("gpu_classes") or []),
        "nodes": nodes,
        "delegates": list(m.get("delegates") or []),
        "retry": (
            {"max_attempts": int(retry["maxAttempts"]), "on_exit_codes": list(retry.get("onExitCodes") or [137, 143])}
            if retry and retry.get("maxAttempts")
            else None
        ),
        "finalize": bool(job.get("finalize")),
    }


def require(name: str) -> dict:
    """Resolve a model consistently for routes and the controller."""
    from fastapi import HTTPException

    model = get(name)
    if model is None:
        raise HTTPException(404, f"model {name} not found")
    return model
