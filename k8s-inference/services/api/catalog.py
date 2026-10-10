"""Model catalog: one schema, one directory (catalog/models/*.yaml, see catalog/README.md).

Keys: id, displayName, description, task, mode (sync | async | run), modes, protocol, port,
endpoints {name: path}, gpu {count, classes}, price {unit, usd}, coldStartClass, cold_start_s,
runtime {image, port, ...} (endpoints), job {image, command, gpu, cpu, memory, pvcSizeGi, ...} and
parameters [] (run, rendered into a Kubernetes Job by jobs.py, docs/JOBS.md), namespace, litellm_route, endpoint_host, deployments {<cluster>: {pool, image,
priorityClassName, parameters, price_per_gpu_hour, paused}} ("hub" = this API's region)."""
import glob, os, yaml
from config import CATALOG_DIRS, ENDPOINT_DOMAIN, HUB_REGION, MODELS_NAMESPACE

_cache: dict = {"mtime": None, "models": {}}


def _load() -> dict:
    models = {}
    for d in CATALOG_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(f) as fh:
                doc = yaml.safe_load(fh) or {}
            if doc.get("id"):
                m = normalise(doc)
                models[m["name"]] = m
    return models


def normalise(doc: dict) -> dict:
    m = dict(doc)
    rt, price, gpu = m.get("runtime") or {}, m.get("price") or {}, m.get("gpu") or {}
    eps = {k: v for k, v in (m.get("endpoints") or {}).items() if k not in ("ready", "live", "metrics")}
    m["name"] = m["id"]
    m["display_name"] = m.get("displayName", m["id"])
    m.setdefault("managed_by", "catalog")      # catalog (bundled file, read-only) | api (services/api/models.py, the fleet database)
    m.setdefault("k8s_name", m["id"].replace(".", "-"))
    m.setdefault("mode", "sync")
    m.setdefault("modes", ["run"] if m["mode"] == "run" else ["sync", "async"])
    m.setdefault("namespace", MODELS_NAMESPACE)
    m["image"] = rt.get("image", m.get("image"))
    m["port"] = rt.get("port", m.get("port", 8080))
    m["endpoints"] = eps
    m.setdefault("endpoint_path", next(iter(eps.values()), "/"))
    # OpenAI-protocol endpoints need the served model name in the request body (`model`); the server registers it
    # under `--model_name=<name>` (KServe huggingfaceserver, vLLM). `servedModel:` in the catalog overrides; the
    # API injects it when the caller omits `model` (sync and async), so tenants only know the catalog id.
    if not m.get("served_model"):
        m["served_model"] = m.get("servedModel") or next(
            (a.split("=", 1)[1] for a in (rt.get("args") or []) if a.startswith("--model_name=")), None)
    if isinstance(price, dict) and price:
        m["price"] = f"${price['usd']} / {price['unit']}"
        m["price_per_call"] = price.get("usd") if price.get("unit") == "call" else None
    m["gpu"] = f"{gpu.get('count', 1)}x {'/'.join(gpu.get('classes', []))}" if isinstance(gpu, dict) and gpu else (gpu or "none")
    m["gpu_classes"] = list(gpu.get("classes") or []) if isinstance(gpu, dict) else []    # preferred first (scheduling profile)
    m["nodes"] = int(m.get("nodes") or rt.get("nodes") or 1)       # > 1: a multi-node endpoint (LeaderWorkerSet, services/api/models.py)
    m["cold_start_class"] = m.get("coldStartClass")
    dep = {(k if k != "hub" else HUB_REGION): (v or {}) for k, v in (m.get("deployments") or {"hub": {}}).items() if not (v or {}).get("paused")}
    m["deployments"] = dep
    m["regions"] = list(dep)
    if m["mode"] != "run" and not m.get("endpoint_host"):
        m["endpoint_host"] = f"http://{m['k8s_name']}-predictor.{m['namespace']}.svc.cluster.local"
    if m["mode"] != "run" and not m.get("endpoint_external_host") and ENDPOINT_DOMAIN:
        # the gateway's hostname of the endpoint (Knative external domain): what tenant Jobs call (docs/JOBS.md)
        m["endpoint_external_host"] = f"https://{m['k8s_name']}-predictor.{m['namespace']}.{ENDPOINT_DOMAIN}"
    if m["mode"] == "run":
        m["job"] = dict(m.get("job") or {})          # image, command, gpu, cpu, memory, ... (docs/JOBS.md)
        images = m["job"].get("images")              # per-GPU-class images (docs/SCHEDULING.md "Per-GPU images for runs")
        if isinstance(images, dict) and images.get("default") and not m["job"].get("image"):
            m["job"]["image"] = images["default"]
        m["protocol"] = m.get("protocol") or "kubernetes-job"
    return m


_runtime: dict = {"until": 0.0, "models": {}}
RUNTIME_TTL_S = 5.0


def runtime_models(force: bool = False) -> dict:
    """Entries defined through the API (services/api/models.py: the fleet database on the control API, ConfigMap copies
    in the regions), re-read every few seconds or after a write."""
    import time
    if force or _runtime["until"] < time.time():
        import models as runtime_store
        _runtime["models"] = {mid: normalise(e) for mid, e in runtime_store.list_runtime().items()}
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
    """Shape of docs/API.md and ui/src/api/types.ts Model."""
    price = m.get("price") or (f"${m['price_per_call']} / call" if m.get("price_per_call") is not None else "metered")
    return {
        "id": m["name"], "name": m["display_name"], "description": m.get("description", ""),
        "modes": m["modes"], "default_mode": m["mode"], "gpu": m.get("gpu", "none"), "price": price,
        "price_per_call": m.get("price_per_call"), "image": m.get("image"), "protocol": m.get("protocol"),
        "cold_start_s": m.get("cold_start_s"), "cold_start_class": m.get("cold_start_class"), "parameters": m.get("parameters", []),
        "endpoints": m.get("endpoints"), "task": m.get("task"), "managed_by": m.get("managed_by", "catalog"), "spec": m.get("spec"),
        "regions": region_status or [{"region": r, "status": "ready"} for r in m["regions"]],
    }
