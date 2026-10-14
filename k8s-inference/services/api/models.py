"""Models as an input: a container plus a few knobs, defined through the API (`POST /v1/models`, the console
form) and kept in the fleet database (services/api/db.py). Nothing about a model lives in terraform.tfvars.

A model definition ("spec") is the shape of the console form:

    {"id": "my-llm", "kind": "endpoint" | "job",
     "image": "vllm/vllm-openai:v0.11.0",          # any registry; pulled through the fleet's image cache
     "command": ["..."] | "sh -c ...", "args": [...], "env": {"NAME": "value"},
     "port": 8000, "protocol": "openai" | "http" | "websocket" | "grpc", "path": "/v1/chat/completions",
     "health_path": "/health", "served_model": "llm",
     "gpu": {"count": 1, "classes": ["h100", "l40s"]},       # preferred class first; pools of these classes
     "resources": {"cpu": "8", "memory": "64Gi", "cpu_limit": "24", "memory_limit": "128Gi",   # requests; limits optional
                   "ephemeral_storage": "4Gi", "ephemeral_storage_limit": "16Gi"},              # (= requests unless given)
     "scaling": {"min": 0, "max": 2, "target": 4},
     "timeout_s": 600, "shm_gib": 4, "pull_secret": "ngc",
     "weights": {"path": "my-llm", "mount_path": "/weights", "env": {"HF_HOME": "/weights"}},
     "regions": ["eu-north1"],                                 # default: every region with a pool of a class
     "display_name": "...", "description": "...",
     # endpoints on several whole nodes (one model served by N nodes, e.g. tensor x pipeline parallel): nodes > 1
     # renders a LeaderWorkerSet instead of a KServe InferenceService (docs/API.md "Multi-node endpoints"):
     # gpu.count is the GPUs per node (a whole-node preset), `command` runs on the leader, `worker_command` on the
     # others, `interconnect` none | preferred | required (InfiniBand pools cannot scale from zero, so `none`,
     # pipeline parallel over Ethernet, is the default); scaling.min 1 | 0 starts and stops it, scaling.idle_s stops
     # it after that many seconds without a request (the control API's reconcile loop), no autoscaling in between
     "nodes": 2, "worker_command": "...", "interconnect": "none",
     # a valid request body for the endpoint (the console's "Test request" tab, GET /v1/models `example`)
     "example": {"messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 64},
     # jobs only
     "cpu": "4", "memory": "16Gi", "disk_gi": 50, "grace_seconds": 300, "scratch": "network" | "local-nvme",
     "finalize": {"command": "...", "on_exit_codes": [75], "timeout_s": 1800, "cpu": "1", "memory": "2Gi"},   # application finalizer
     # a directory of the tenant's area on the region's shared filesystem, mounted into the run (read-only unless
     # readonly is false: reference databases, model parameters, anything too big to sync per run; docs/JOBS.md "Shared data")
     "data": {"path": "refdb/uniref", "mount_path": "/data", "readonly": true},
     "parameters": [{"name": "...", "type": "string", "default": "..."}]}

Variants and routing (docs/API.md "Variants"): `images` (per GPU class: {"default": ..., "h100": ...}; an
endpoint's deployment takes the image of its pool's class), `deployments.<region>.variants` (several deployments
of one model in a region behind one name, in priority order: e.g. a warm floor on reserved GPUs plus scale-to-zero
on spot; each variant is its own Knative service `<id>-<variant>`, the first keeps the id), `routing`
({field, map, default}: a request field picks a variant, else the first ready one in priority order). LiteLLM's
model group lists every variant of every cluster as a deployment with its priority (`order`).

`to_entry` turns a spec into the catalog entry the rest of the API and the endpoint renderer
(services/api/render_endpoint.py) understand (catalog/README.md); the entry is stored next to the spec in the fleet
database (`models` table, services/api/db.py). Entries that come from files (the bundled classes) are read-only here.
"""

import json
import logging
import re
import sys

import yaml
from fastapi import HTTPException
from kubernetes.client.rest import ApiException
from pydantic import ValidationError

import config
import db
import kube
import render_endpoint
from config import (
    ENDPOINT_DOMAIN,
    ENDPOINT_DOMAINS,
    GATEWAY_NAMESPACE,
    IMAGES_HOST,
    IMAGES_SOURCE,
    INFINIBAND_CLAIM_TEMPLATE,
    LABEL,
    LITELLM_MASTER_KEY,
    LITELLM_URL,
    MODELS_CERTIFICATE,
    MODELS_ISSUER,
    MODELS_NAMESPACE,
    MULTINODE_MAX_NODES,
    REGION,
    ROUTER_KEY,
)
from pod_metadata import normalise as normalise_pod_metadata
from resilience import retry
from schemas import EphemeralVolumeSpec
from schemas import ScalingSpec as ScalingSpec

log = logging.getLogger("api")

CATALOG_LABEL = f"{LABEL}/catalog"  # runtime
MODEL_LABEL = f"{LABEL}/model"
MANAGED_BY_LABEL = f"{LABEL}/managed-by"  # api | terraform
FIELD_MANAGER = "serverless2-api"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
PROTOCOLS = ("http", "openai", "websocket", "grpc")
PROTOCOL_PATH = {"openai": "/v1/chat/completions", "http": "/", "websocket": "/", "grpc": "/"}
RESERVED = ("images", "routing")  # carried on the entry as given (validated below)
VARIANT_RE = re.compile(r"^[a-z][a-z0-9-]{0,20}$")
MAX_VARIANTS = 8
DATA_PATH_RE = re.compile(
    r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$"
)  # a relative directory, no parent references


def _endpoint_bindings(s: dict) -> None:
    if "empty_dirs" not in s and "env_field_refs" not in s:
        return
    if s.get("kind", "endpoint") != "endpoint":
        raise HTTPException(400, "empty_dirs and env_field_refs: endpoints only")
    fields = s.get("env_field_refs", {})
    if not isinstance(fields, dict) or any(
        not isinstance(k, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or v not in ("metadata.uid", "metadata.name", "metadata.namespace")
        or k in (s.get("env") or {})
        for k, v in fields.items()
    ):
        raise HTTPException(400, "env_field_refs: distinct env names mapped to pod metadata uid/name/namespace")
    volumes = s.get("empty_dirs", [])
    if not isinstance(volumes, list) or len(volumes) > 16:
        raise HTTPException(400, "empty_dirs: a list of at most 16 bounded scratch volumes")
    try:
        volumes = [EphemeralVolumeSpec.model_validate(v).model_dump() for v in volumes]
    except ValidationError:
        raise HTTPException(400, "empty_dirs: each entry needs name, mount_path and size_limit in Mi/Gi") from None
    names, paths = {"weights", "shm"}, {"/dev/shm"}
    if s.get("weights"):
        paths.add(s["weights"].get("mount_path", "/weights"))
    for v in volumes:
        path = v["mount_path"].rstrip("/") or "/"
        if (
            v["name"] in names
            or path == "/"
            or "//" in path
            or any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for p in paths)
        ):
            raise HTTPException(400, "empty_dirs: names/mounts must be distinct and cannot shadow weights or shm")
        names.add(v["name"])
        paths.add(path)
        v["mount_path"] = path
    s["empty_dirs"] = volumes


# public registries -> the alias the image cache serves them under (terraform.tfvars `images.upstreams`)
UPSTREAM_ALIAS = {
    "docker.io": "docker",
    "registry-1.docker.io": "docker",
    "index.docker.io": "docker",
    "nvcr.io": "nvcr",
    "ghcr.io": "ghcr",
    "quay.io": "quay",
    "registry.k8s.io": "k8s",
}


def normalise_image(ref: str) -> str:
    """A plain image reference the way people write it (`vllm/vllm-openai:latest`, `nvcr.io/nim/x:1`,
    `<fleet registry>/team/x:1`) -> the fleet's logical registry host, which every node resolves to its
    region's cache (docs/IMAGES.md). Already-logical and unknown private registries are left as they are.
    Without the cache (IMAGES_HOST empty: `images.cache.enabled = false`) every reference stays as written."""
    ref = ref.strip()
    if not ref or not IMAGES_HOST or ref.startswith(IMAGES_HOST + "/") or "{{" in ref:
        return ref
    first = ref.split("/", 1)[0]
    has_registry = ("." in first or ":" in first or first == "localhost") and "/" in ref
    if not has_registry:
        path = ref if "/" in ref else f"library/{ref}"
        return f"{IMAGES_HOST}/docker/{path}"
    host, path = ref.split("/", 1)
    if IMAGES_SOURCE and ref.startswith(IMAGES_SOURCE + "/"):
        return f"{IMAGES_HOST}/nebius/{ref[len(IMAGES_SOURCE) + 1:]}"
    alias = UPSTREAM_ALIAS.get(host)
    return f"{IMAGES_HOST}/{alias}/{path}" if alias else ref


def _command(v):
    if v is None or v == "" or v == []:
        return None
    return ["/bin/sh", "-c", v] if isinstance(v, str) else list(v)


KNATIVE_RESERVED_ENV = ("PORT", "K_SERVICE", "K_CONFIGURATION", "K_REVISION")


def _endpoint_timeout(s: dict) -> None:
    """An endpoint's `timeout_s` is its Knative revision timeout: above the fleet's max-revision-timeout-seconds the
    definition would store and register but its InferenceService would never reconcile (ReconcileFailed on every
    region). Refused at registration with the knob that raises the ceiling."""
    t = s.get("timeout_s", 600)
    if not isinstance(t, int) or isinstance(t, bool) or t < 1:
        raise HTTPException(400, "timeout_s: a positive integer (seconds)")
    limit = int(config.KNATIVE_MAX_REVISION_TIMEOUT_S)
    if t > limit:
        raise HTTPException(
            400,
            f"timeout_s {t} exceeds this fleet's Knative max-revision-timeout-seconds ({limit}); an endpoint above it registers "
            f"but never reconciles. Raise fleet.knative.max_revision_timeout_s (stack tfvars: the platform chart sets Knative "
            f"and the API from that one value) to at least {t}, or lower timeout_s",
        )


def _reserved_env(s: dict) -> None:
    """Knative Serving reserves PORT (it injects the serving port) and K_SERVICE/K_CONFIGURATION/K_REVISION in a
    revision's containers: a definition that sets them stores but its InferenceService never reconciles. Refused at
    registration (endpoints only; run classes are plain Jobs and keep their env as declared)."""
    places = {
        "env": s.get("env") or {},
        "env_field_refs": s.get("env_field_refs") or {},
        "weights.env": ((s.get("weights") or {}).get("env") or {}) if isinstance(s.get("weights"), dict) else {},
    }
    for where, env in places.items():
        if not isinstance(env, dict):
            continue
        bad = sorted(k for k in env if str(k).upper() in KNATIVE_RESERVED_ENV)
        if bad:
            hint = (
                " (Knative injects the serving port into PORT: declare `port` and read PORT at runtime)"
                if "PORT" in [b.upper() for b in bad]
                else ""
            )
            raise HTTPException(
                400,
                f"{where}: {', '.join(bad)} reserved by Knative Serving ({', '.join(KNATIVE_RESERVED_ENV)}); an endpoint "
                f"setting it registers but never reconciles{hint}",
            )


def _env_list(env: dict | None) -> list[dict]:
    return [{"name": k, "value": str(v)} for k, v in (env or {}).items()]


def cluster_id() -> str:
    """The fleet's id of this API's cluster (fleet-prices `region` of its pools: `hub`, or the region name in the
    Terraform solution): the key of `deployments` and the chart's `cluster` value."""
    for p in kube.fleet()["pools"].values():
        cid = p.get("region") or REGION
        if kube.cluster_region(cid) == REGION:
            return cid
    return REGION


def regions_for(classes: list[str], wanted: list[str] | None) -> list[str]:
    """Cluster ids (the fleet's region ids) where a pool of one of the classes exists. `wanted` names regions
    or ids; both are accepted."""
    have: dict[str, list[dict]] = {}
    for p in kube.fleet()["pools"].values():
        have.setdefault(p.get("region") or REGION, []).append(p)
    if wanted:
        ids = []
        for w in wanted:
            match = next((cid for cid in have if cid == w or kube.cluster_region(cid) == w), None)
            if match is None:
                raise HTTPException(400, f"regions: {w} is not a region of this fleet (known: {sorted(have)})")
            ids.append(match)
        return ids
    return sorted(
        cid for cid, pools in have.items() if not classes or any(p.get("gpu_class") in classes for p in pools)
    )


def pool_for(cid: str, classes: list[str], count: int = 1, nodes: int = 1, interconnect: str = "none") -> str | None:
    """The pool an endpoint of these classes is pinned to on a cluster: the first class that has a pool there
    whose nodes carry at least `count` GPUs (an 8-GPU replica never fits a 1-GPU preset); among those, reserved
    pools first (a reservation is paid for whether it serves or not), then the smallest fitting preset (a 1-GPU
    model must not boot an 8-GPU on-demand node), then on-demand before spot, plain pools before
    InfiniBand ones (those nodes are for multi-node runs), then by name. A multi-node endpoint (`nodes` > 1) takes
    whole nodes: only pools whose preset has exactly `count` GPUs; `interconnect` required = InfiniBand pools only,
    preferred = those first."""
    order = {"reserved": 0, "on_demand": 1, "spot": 2}
    # a pool dedicated to one tenant (fleet tenants.<name>.dedicated_pools) serves that tenant's runs only
    pools = [p for p in kube.fleet()["pools"].values() if (p.get("region") or REGION) == cid and not p.get("tenant")]
    whole = int(nodes) > 1
    for c in classes:
        cands = [
            p
            for p in pools
            if p.get("gpu_class") == c
            and (
                int(p.get("gpus_per_node") or 1) == int(count)
                if whole
                else int(p.get("gpus_per_node") or 1) >= max(int(count), 1)
            )
        ]
        if whole and interconnect == "required":
            cands = [p for p in cands if (p.get("interconnect") or "none") == "infiniband"]
        ib_first = whole and interconnect == "preferred"
        cands.sort(
            key=lambda p: (
                p.get("capacity") != "reserved",
                int(p.get("gpus_per_node") or 1),
                order.get(p.get("capacity"), 9),
                ((p.get("interconnect") or "none") == "none")
                if ib_first
                else ((p.get("interconnect") or "none") != "none"),
                p["pool"],
            )
        )
        if cands:
            return cands[0]["pool"]
    return None


def validate(spec: dict) -> dict:
    s = dict(spec or {})
    _endpoint_bindings(s)
    if "pod_metadata" in s:
        s["pod_metadata"] = normalise_pod_metadata(s["pod_metadata"])
    secrets = s.get("env_secrets", [])
    if not isinstance(secrets, list) or any(
        not isinstance(name, str) or len(name) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name)
        for name in secrets
    ):
        raise HTTPException(400, "env_secrets: a list of Kubernetes Secret names in the workload namespace")
    mid = s.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        raise HTTPException(400, "id: lowercase letters, digits and dashes, 2-41 characters, starting with a letter")
    kind = s.get("kind", "endpoint")
    if kind not in ("endpoint", "job"):
        raise HTTPException(400, "kind: endpoint | job")
    if (
        "example" in s
        and s["example"] is not None
        and (not isinstance(s["example"], dict) or len(json.dumps(s["example"])) > 16384)
    ):
        raise HTTPException(400, "example: a JSON object of at most 16 KiB, a valid request body of the endpoint")
    if s.get("data") is not None:
        d = s["data"]
        if kind != "job" or not isinstance(d, dict) or not DATA_PATH_RE.match(str(d.get("path", ""))):
            raise HTTPException(
                400,
                "data: jobs only, {path (a relative directory of the tenant's shared data, no '..'), mount_path, readonly}",
            )
    if s.get("weights") is not None and kind == "job":
        w = s["weights"]
        if not isinstance(w, dict) or not DATA_PATH_RE.match(str(w.get("path", ""))):
            raise HTTPException(
                400, "weights (jobs): {path (a directory of the region's shared weights, no '..'), mount_path}"
            )
    if "automount_service_account_token" in s:
        if kind != "endpoint" or not isinstance(s["automount_service_account_token"], bool):
            raise HTTPException(
                400, "automount_service_account_token: an endpoint-only boolean; run helpers require their token"
            )
    if not s.get("image") and not (s.get("images") or {}).get("default"):
        raise HTTPException(400, "image is required (or images.default)")
    gpu = s.get("gpu") or {}
    if gpu and not isinstance(gpu, dict):
        raise HTTPException(400, "gpu: {count, classes}")
    if kind == "endpoint":
        if s.get("protocol", "http") not in PROTOCOLS:
            raise HTTPException(400, f"protocol: one of {PROTOCOLS}")
        if not isinstance(s.get("port", 8080), int):
            raise HTTPException(400, "port: an integer")
        _endpoint_timeout(s)
        _reserved_env(s)
        try:
            scaling = ScalingSpec.model_validate(s.get("scaling") or {}).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise HTTPException(400, f"scaling: {exc.errors()[0]['msg']}") from exc
        if scaling.get("min", 0) > scaling.get("max", 1):
            raise HTTPException(400, "Minimum replicas cannot exceed maximum replicas.")
        # the stored policy is complete: metric, target and limit are explicit, so a later partial update or a
        # reader never has to guess a default
        if scaling.setdefault("metric", "concurrency_utilization") == "concurrency_utilization":
            target = scaling.setdefault("target", 100)
            concurrency = scaling.setdefault("container_concurrency", 1)
            if target > 100 or concurrency < 1:
                raise HTTPException(400, "Concurrency target must be 1–100%; replica concurrency must be at least 1.")
        else:
            scaling.setdefault("target", 5)
            scaling["container_concurrency"] = 0  # request-rate mode has no concurrency cap
        scaling.setdefault("window_s", 30)
        s["scaling"] = scaling
        nodes = s.get("nodes", 1)
        if not isinstance(nodes, int) or nodes < 1 or nodes > MULTINODE_MAX_NODES:
            raise HTTPException(400, f"nodes: an integer from 1 to {MULTINODE_MAX_NODES}")
        if nodes > 1:
            if not (gpu.get("classes") and int(gpu.get("count") or 0) >= 1):
                raise HTTPException(
                    400, "nodes > 1: gpu.count (GPUs per node, a whole-node preset) and gpu.classes are required"
                )
            if s.get("interconnect", "none") not in ("none", "preferred", "required"):
                raise HTTPException(400, "interconnect: none | preferred | required")
            if scaling.get("max", 1) != max(scaling.get("min", 0), 1):
                scaling["max"] = max(
                    scaling.get("min", 0), 1
                )  # replica groups of whole nodes do not autoscale: max = running count
            if "worker_command" in s and not _command(s["worker_command"]):
                raise HTTPException(400, "worker_command: a string or a list")
    classes = list(gpu.get("classes") or [])
    fleet_classes = sorted({p.get("gpu_class") for p in kube.fleet()["pools"].values() if p.get("gpu_class")})
    if classes and fleet_classes and not any(c in fleet_classes for c in classes):
        raise HTTPException(400, f"gpu.classes {classes}: none exists in this fleet (classes: {fleet_classes})")
    images = s.get("images")
    if images is not None and (
        not isinstance(images, dict)
        or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in images.items())
    ):
        raise HTTPException(400, 'images: {"default": <image>, "<gpu class>": <image>, ...}')
    _validate_variants(s, kind)
    s["kind"] = kind
    return s


def _validate_variants(s: dict, kind: str) -> None:
    """`deployments.<region>.variants` and `routing` of an endpoint (docs/API.md "Variants")."""
    deployments, routing = s.get("deployments"), s.get("routing")
    if deployments is None and routing is None:
        return
    if kind != "endpoint":
        raise HTTPException(400, "deployments.variants and routing: endpoints only")
    pools = kube.fleet()["pools"]
    names: set[str] = set()
    if deployments is not None:
        if not isinstance(deployments, dict) or not deployments:
            raise HTTPException(400, "deployments: {<region>: {variants: [{name, pool, min, max, image}]}}")
        for region, d in deployments.items():
            cid = regions_for([], [region])[0]
            variants = (d or {}).get("variants") if isinstance(d, dict) else None
            if not isinstance(variants, list) or not variants or len(variants) > MAX_VARIANTS:
                raise HTTPException(400, f"deployments.{region}.variants: a list of 1 to {MAX_VARIANTS} variants")
            seen = set()
            for i, v in enumerate(variants):
                if not isinstance(v, dict) or not v.get("pool"):
                    raise HTTPException(400, f"deployments.{region}.variants[{i}]: needs a pool")
                name = str(v.get("name") or ("primary" if i == 0 else f"v{i}"))
                if not VARIANT_RE.match(name) or name in seen:
                    raise HTTPException(
                        400, f"deployments.{region}.variants[{i}].name: a unique DNS-style label, {name!r} is not"
                    )
                seen.add(name)
                names.add(name)
                pool = next(
                    (p for p in pools.values() if p.get("pool") == v["pool"] and (p.get("region") or REGION) == cid),
                    None,
                )
                if pool is None:
                    raise HTTPException(
                        400, f"deployments.{region}.variants[{i}].pool: {v['pool']} is not a pool of {region}"
                    )
                if pool.get("tenant"):
                    raise HTTPException(
                        400, f"deployments.{region}.variants[{i}].pool: {v['pool']} is dedicated to a tenant"
                    )
                for k in ("min", "max"):
                    if k in v and (not isinstance(v[k], int) or v[k] < 0):
                        raise HTTPException(400, f"deployments.{region}.variants[{i}].{k}: a non-negative integer")
                if v.get("min", 0) > v.get("max", 1):
                    raise HTTPException(400, f"deployments.{region}.variants[{i}]: min exceeds max")
                if "image" in v and (not isinstance(v["image"], str) or not v["image"]):
                    raise HTTPException(400, f"deployments.{region}.variants[{i}].image: a non-empty image reference")
                v["name"] = name
    if routing is not None:
        if not isinstance(routing, dict) or not isinstance(routing.get("field"), str) or not routing["field"]:
            raise HTTPException(
                400,
                "routing: {field: <request field, dotted>, map: {<value>: <variant>}, default: priority | <variant>}",
            )
        mapping = routing.get("map") or {}
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
        ):
            raise HTTPException(400, "routing.map: {<value>: <variant name>}")
        targets = set(mapping.values()) | (
            {routing["default"]} if routing.get("default") not in (None, "priority") else set()
        )
        unknown = sorted(t for t in targets if t not in names)
        if unknown:
            raise HTTPException(400, f"routing: variants {unknown} are not defined in deployments.<region>.variants")
        routing.setdefault("default", "priority")


def _assets_spec(raw) -> list[dict]:
    """`assets: [{alias, mount_path}]`: shape checked here (the registry and the grant are checked at render, against
    the fleet document of the moment)."""
    from jobs import MAX_ASSETS  # one bound for the registration and the render

    if not isinstance(raw, list) or len(raw) > MAX_ASSETS:
        raise HTTPException(400, f"assets: a list of at most {MAX_ASSETS} {{alias, mount_path}} entries")
    out = []
    for x in raw:
        if not isinstance(x, dict) or not re.fullmatch(
            r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", str(x.get("alias") or "")
        ):
            raise HTTPException(400, "assets: alias is a DNS label")
        path = str(x.get("mount_path") or "")
        if not path.startswith("/") or path == "/":
            raise HTTPException(400, "assets: mount_path is an absolute path")
        out.append({"alias": x["alias"], "mountPath": path})
    return out


def _work_aliases(raw) -> list[dict]:
    import jobs  # the render's own validation (jobs.work_aliases), at registration already

    return [{"path": a["path"], "scope": a["scope"]} for a in jobs.work_aliases({"workMountAliases": raw})]


def _retry_policy(r) -> dict:
    """`retry: {max_attempts 1..10, on_exit_codes [1..255] (default [137, 143])}` of a run class spec."""
    from schemas import RETRY_MAX_ATTEMPTS

    if not isinstance(r, dict) or "max_attempts" not in r:
        raise HTTPException(400, "retry: {max_attempts (1..10), on_exit_codes (default [137, 143])}")
    try:
        n = int(r["max_attempts"])
    except (TypeError, ValueError):
        raise HTTPException(400, "retry.max_attempts: 1..10")
    if not 1 <= n <= RETRY_MAX_ATTEMPTS:
        raise HTTPException(400, "retry.max_attempts: 1..10")
    codes = r.get("on_exit_codes", [137, 143])
    if not isinstance(codes, list) or not codes or any(not isinstance(c, int) or not 1 <= c <= 255 for c in codes):
        raise HTTPException(400, "retry.on_exit_codes: a non-empty list of exit codes 1..255")
    return {"maxAttempts": n, "onExitCodes": sorted(set(codes))}


def to_entry(spec: dict, managed_by: str = "api") -> dict:
    """Catalog entry (catalog/README.md schema) for a spec; the chart and the job renderer read this."""
    s = validate(spec)
    mid, kind = s["id"], s["kind"]
    gpu = s.get("gpu") or {}
    classes = list(gpu.get("classes") or [])
    count = gpu.get("count", 1 if classes else 0)
    image = normalise_image(s.get("image") or (s.get("images") or {}).get("default"))
    regions = regions_for(classes, s.get("regions"))
    entry: dict = {
        "id": mid,
        "displayName": s.get("display_name") or mid,
        "description": s.get("description", ""),
        "task": s.get("task", "custom"),
        "managed_by": managed_by,
        "spec": s,
    }
    if s.get("pod_metadata"):
        entry["podMetadata"] = s["pod_metadata"]
    if isinstance(s.get("example"), dict):
        entry["example"] = s["example"]
    for k in RESERVED:
        if k in s:
            entry[k] = s[k]
    if kind == "endpoint":
        protocol = s.get("protocol", "http")
        path = s.get("path") or PROTOCOL_PATH[protocol]
        port = int(s.get("port", 8080))
        scaling = s.get("scaling") or {}
        # Knative: `concurrency_utilization` is the concurrency metric with the replica limit as the scale target and
        # the percentage as its utilization; `requests_per_second` is the rps metric at 100 % with no concurrency limit
        rate = scaling.get("metric") == "requests_per_second"
        concurrency = int(scaling.get("container_concurrency", 0 if rate else 1))
        res = s.get("resources") or {}
        runtime: dict = {
            "image": image,
            "port": port,
            "protocol": protocol,
            "resources": {
                "gpu": count,
                "cpu": str(res.get("cpu", "4")),
                "memory": str(res.get("memory", "16Gi")),
                # optional, as a run class's: limits above the requests (cpu/memory are the requests, = the limits
                # unless these are given) and the kubelet's node-local scratch (request; limit = request unless given);
                # kept through the projection, nothing rendered without them
                **({"cpuLimit": str(res["cpu_limit"])} if res.get("cpu_limit") else {}),
                **({"memoryLimit": str(res["memory_limit"])} if res.get("memory_limit") else {}),
                **({"ephemeralStorage": str(res["ephemeral_storage"])} if res.get("ephemeral_storage") else {}),
                **(
                    {"ephemeralStorageLimit": str(res["ephemeral_storage_limit"])}
                    if res.get("ephemeral_storage_limit")
                    else {}
                ),
            },
            "scaling": {
                "minReplicas": int(scaling.get("min", 0)),
                "maxReplicas": int(scaling.get("max", 1)),
                "metric": "rps" if rate else "concurrency",
                "target": int(scaling.get("target", 5)) if rate else concurrency,
                "containerConcurrency": concurrency,
                **({"scaleToZeroRetention": f"{int(scaling['idle_s'])}s"} if scaling.get("idle_s") is not None else {}),
            },
            "timeout": int(s.get("timeout_s", 600)),
            "shm": {"enabled": True, "size": f"{int(s.get('shm_gib', 1))}Gi"},
        }
        if "automount_service_account_token" in s:
            runtime["automountServiceAccountToken"] = s["automount_service_account_token"]
        if s.get("call_scratch"):
            runtime["callScratch"] = s["call_scratch"]
        annotations = {
            f"autoscaling.knative.dev/{a}": f"{scaling[k]}s"
            for k, a in (("cooldown_s", "scale-down-delay"), ("window_s", "window"))
            if k in scaling
        }
        annotations["autoscaling.knative.dev/target-utilization-percentage"] = (
            "100" if rate else str(scaling.get("target", 100))
        )
        runtime["annotations"] = annotations
        if s.get("args"):
            runtime["args"] = [str(a) for a in s["args"]]
        if cmd := _command(s.get("command")):
            runtime["command"] = cmd
        if s.get("env"):
            runtime["env"] = _env_list(s["env"])
        if s.get("env_field_refs"):
            runtime.setdefault("env", []).extend(
                {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
                for name, field in s["env_field_refs"].items()
            )
        if s.get("empty_dirs"):
            runtime["volumes"] = [
                {
                    "name": v["name"],
                    "mountPath": v["mount_path"],
                    "sizeLimit": v["size_limit"],
                    **({"medium": "Memory"} if v["memory"] else {}),
                }
                for v in s["empty_dirs"]
            ]
        if s.get("env_secrets"):
            runtime["envFrom"] = [{"secretRef": {"name": name}} for name in s["env_secrets"]]
        if s.get("health_path"):
            runtime["readinessProbe"] = {"path": s["health_path"], "port": port}
        if s.get("native_lease"):
            if s["native_lease"] not in ("optional", "required"):
                raise HTTPException(400, "native_lease: optional | required")
            runtime["nativeLease"] = s["native_lease"]
        if s.get("ingress_isolation"):
            # opt-in (docs/API.md "Ingress isolation"): nothing changes for an endpoint that does not name it
            if s["ingress_isolation"] != "declared-ports":
                raise HTTPException(400, "ingress_isolation: declared-ports")
            runtime["ingressIsolation"] = s["ingress_isolation"]
        if s.get("pull_secret"):
            runtime["imagePullSecrets"] = [s["pull_secret"]]
        w = s.get("weights") or {}
        if w:
            read_only = bool(w.get("read_only"))
            runtime["weights"] = {
                "sharedFilesystem": {
                    "enabled": True,
                    "path": w.get("path", mid),
                    "mountPath": w.get("mount_path", "/weights"),
                    "readOnly": read_only,
                    "prepare": w["prepare"] if w.get("prepare") is not None else not read_only,
                },
                "env": dict(w.get("env") or {}),
            }
        nodes, interconnect = int(s.get("nodes", 1)), s.get("interconnect", "none")
        if nodes > 1 and not s.get("regions"):  # by default every region that has a whole-node pool of the class
            regions = [r for r in regions if pool_for(r, classes, count, nodes, interconnect)]
        explicit = s.get("deployments") or {}
        if explicit and not s.get("regions"):  # variants name their regions
            regions = [regions_for([], [r])[0] for r in explicit]
        deployments = {
            r: (
                {"pool": p}
                if (p := (pool_for(r, classes, count, nodes, interconnect) if classes else "system"))
                else {}
            )
            for r in regions
        }
        pool_class = {p["pool"]: p.get("gpu_class") for p in kube.fleet()["pools"].values()}
        images = s.get("images") or {}
        for r, d in deployments.items():
            spec_region = next((k for k in explicit if regions_for([], [k])[0] == r), None)
            variants = (explicit.get(spec_region) or {}).get("variants") if spec_region else None
            if variants:
                d["pool"] = variants[0]["pool"]
                d["variants"] = [
                    {
                        "name": v["name"],
                        "pool": v["pool"],
                        "min": int(v.get("min", 0)),
                        "max": int(v.get("max", 1)),
                        "image": normalise_image(
                            v.get("image")
                            or images.get(pool_class.get(v["pool"]) or "")
                            or s.get("image")
                            or images.get("default")
                        ),
                    }
                    for v in variants
                ]
                d["image"] = d["variants"][0]["image"]
            elif d.get("pool") and images.get(pool_class.get(d["pool"]) or ""):
                d["image"] = normalise_image(images[pool_class[d["pool"]]])  # the class's own image for this deployment
        if nodes > 1:
            # render_endpoint._leaderworkerset: one replica group of `nodes` pods, the leader serves
            runtime["nodes"] = nodes
            runtime["interconnect"] = interconnect
            if wc := _command(s.get("worker_command")):
                runtime["workerCommand"] = wc
            if scaling.get("idle_s") is not None:
                runtime["idleSeconds"] = int(scaling["idle_s"])
            for r, d in deployments.items():
                if not d.get("pool"):
                    raise HTTPException(
                        400,
                        f"nodes: no pool in {r} whose preset has {count} GPUs per node for classes {classes}"
                        + (" with InfiniBand" if interconnect == "required" else ""),
                    )
                pool = next((p for p in kube.fleet()["pools"].values() if p.get("pool") == d["pool"]), {})
                if (pool.get("interconnect") or "none") == "infiniband" and interconnect != "none":
                    d["ibDevices"] = kube.ib_devices_per_node(
                        [d["pool"]]
                    )  # DRA claim of every fabric NIC (docs/JOBS.md)
        entry.update(
            {
                "mode": "sync",
                "protocol": protocol,
                "port": port,
                "endpoints": {
                    "chat" if protocol == "openai" else "invoke": path,
                    **({"ready": s["health_path"]} if s.get("health_path") else {}),
                },
                "gpu": {"count": count, "classes": classes} if classes else "none",
                "servedModel": s.get("served_model"),
                "runtime": runtime,
                **({"routing": s["routing"]} if s.get("routing") else {}),
                **({"nodes": nodes} if nodes > 1 else {}),
                # no GPU class: the endpoint runs on the region's system pool (CPU nodes, label serverless2.nebius/pool=system)
                "deployments": deployments,
            }
        )
        if not entry["servedModel"]:
            entry.pop("servedModel")
    else:
        job: dict = {
            "image": image,
            "command": s.get("command") or "",
            "gpu": count,
            "cpu": str(s.get("cpu", "4")),
            "memory": str(s.get("memory", "16Gi")),
            # separate limits and node-local scratch (docs/JOBS.md "Run class"): a request is what the scheduler
            # guarantees, a limit what the container may grow to; absent limits keep the chart's rules (memory limit =
            # request, no CPU quota)
            **({"cpuLimit": str(s["cpu_limit"])} if s.get("cpu_limit") else {}),
            **({"memoryLimit": str(s["memory_limit"])} if s.get("memory_limit") else {}),
            **({"ephemeralStorage": str(s["ephemeral_storage"])} if s.get("ephemeral_storage") else {}),
            **(
                {"ephemeralStorageLimit": str(s["ephemeral_storage_limit"])} if s.get("ephemeral_storage_limit") else {}
            ),
            # run classes a graph rooted here may bind stages to (docs/JOBS.md "Delegated stages"): operator-owned
            **({"delegates": [str(x) for x in s["delegates"]]} if s.get("delegates") else {}),
            # the class's bounded retry policy (docs/JOBS.md "Retries"): {maxAttempts, onExitCodes}
            **({"retry": _retry_policy(s["retry"])} if s.get("retry") is not None else {}),
            "pvcSizeGi": int(s.get("disk_gi", 50)),
            "graceSeconds": int(s.get("grace_seconds", 300)),
            "scratch": s.get("scratch", "network"),
            # canonical aliases of the work volume (docs/JOBS.md "Work mount aliases"): operator-owned, validated here
            **({"workMountAliases": _work_aliases(s["work_mount_aliases"])} if s.get("work_mount_aliases") else {}),
            # restricted assets (docs/JOBS.md "Restricted assets"): operator-owned aliases of the fleet registry
            **({"assets": _assets_spec(s["assets"])} if s.get("assets") else {}),
            "env": dict(s.get("env") or {}),
            "envFrom": [{"secretRef": {"name": name}} for name in s.get("env_secrets", [])],
            # per-GPU-class images (docs/SCHEDULING.md "Per-GPU images for runs"): the class chosen at submission picks one
            **(
                {"images": {k: normalise_image(v) for k, v in s["images"].items()}}
                if isinstance(s.get("images"), dict) and s["images"]
                else {}
            ),
        }
        if s.get("args"):
            job["args"] = s["args"]
        if (fz := s.get("finalize")) is not None:
            # the model-owned finalizer (docs/JOBS.md "Finalization by the application"): a fixed command in the model's
            # own image that re-delivers from the kept work volume after the run's compute ended with one of
            # `on_exit_codes`; never a caller-supplied executable, never a GPU
            if not isinstance(fz, dict) or not str(fz.get("command") or "").strip():
                raise HTTPException(
                    400,
                    "finalize: {command (required), on_exit_codes (default [75]), timeout_s (60..86400), cpu, memory}",
                )
            codes = fz.get("on_exit_codes", [75])
            if not isinstance(codes, list) or not codes or not all(isinstance(c, int) and 1 <= c <= 255 for c in codes):
                raise HTTPException(
                    400,
                    "finalize.on_exit_codes: a non-empty list of exit codes 1..255 of the compute that mean 'delivery pending'",
                )
            timeout = int(fz.get("timeout_s", 1800))
            if not 60 <= timeout <= 86400:
                raise HTTPException(400, "finalize.timeout_s: 60..86400")
            job["finalize"] = {
                "command": str(fz["command"]),
                "onExitCodes": codes,
                "timeoutS": timeout,
                "cpu": str(fz.get("cpu", "1")),
                "memory": str(fz.get("memory", "2Gi")),
            }
        if s.get("child_endpoints") is not None:
            # endpoint children (docs/JOBS.md "Endpoint children"): the operator's allowlist of endpoint models a live
            # attempt may ask for, with an explicit bound per root (never a silent default: the retained declared
            # range of a dataset-driven workload is the operator's statement, not the platform's guess)
            ce = s["child_endpoints"]
            if (
                not isinstance(ce, list)
                or not ce
                or not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", x) for x in ce)
            ):
                raise HTTPException(400, "child_endpoints: a non-empty list of endpoint model ids")
            if s.get("child_max_units") is None:
                raise HTTPException(
                    400, "child_endpoints needs child_max_units (the explicit bound of children per root, 1..65536)"
                )
            n = int(s["child_max_units"])
            if not 1 <= n <= 65536:
                raise HTTPException(400, "child_max_units: 1..65536")
            job["childEndpoints"] = sorted(set(ce))
            job["childMaxUnits"] = n
            delivery = str(s.get("child_delivery") or "platform")
            if delivery not in ("platform", "application"):
                raise HTTPException(400, "child_delivery: platform | application")
            job["childDelivery"] = delivery
        elif s.get("child_max_units") is not None:
            raise HTTPException(400, "child_max_units applies with child_endpoints")
        if s.get("pull_secret"):
            job["imagePullSecret"] = s["pull_secret"]
        if d := s.get("data"):
            job.update(
                {
                    "data": d["path"],
                    "dataMountPath": d.get("mount_path", "/data"),
                    "dataReadOnly": bool(d.get("readonly", True)),
                }
            )
        if w := s.get("weights"):
            job.update({"weights": w["path"], "weightsMountPath": w.get("mount_path", "/weights")})
        entry.update(
            {
                "mode": "run",
                "protocol": "kubernetes-job",
                "gpu": {"count": count, "classes": classes} if classes else "none",
                "job": job,
                "parameters": list(s.get("parameters") or []),
                "deployments": {r: {} for r in regions},
            }
        )
    return entry


# ---- persistence: the fleet database ------------------------------------------------------------------------
# The API (DATABASE_URL, services/api/db.py) owns the definitions: every write lands in the `models` table (with
# its history); the reconciler renders them on the clusters of their `deployments` (services/api/app.py).


def list_runtime() -> dict[str, dict]:
    """Runtime catalog entries: {id: entry}, from the database (empty without one: the bundled classes only)."""
    if not db.enabled():
        return {}
    return {mid: {**row["entry"], "version": row["version"]} for mid, row in db.list_models().items()}


# ---- rendering: the endpoint's objects (render_endpoint.py), applied with server-side apply --------------

PLURALS = {
    "InferenceService": ("serving.kserve.io", "v1beta1", "inferenceservices"),
    "SecurityPolicy": ("gateway.envoyproxy.io", "v1alpha1", "securitypolicies"),
    "LocalModelCache": ("serving.kserve.io", "v1alpha1", "localmodelcaches"),
    # multi-node endpoints (runtime.nodes > 1): the replica group, its leader Service's route to the gateway
    "LeaderWorkerSet": kube.LWS,
    "HTTPRoute": ("gateway.networking.k8s.io", "v1", "httproutes"),
    # opt-in declared-port ingress isolation of an endpoint's pods (docs/API.md "Ingress isolation")
    "CiliumNetworkPolicy": ("cilium.io", "v2", "ciliumnetworkpolicies"),
}


def variants(entry: dict, cluster: str) -> list[dict]:
    """The deployments of an endpoint on a cluster in priority order: [{name, object, pool, min, max, image}]; a
    plain deployment is one variant named "primary" under the model's own name (docs/API.md "Variants")."""
    deps = entry.get("deployments") or {}
    d = (
        deps.get(cluster) or deps.get(kube.cluster_region(cluster)) or {}
    )  # entries store cluster ids, normalised ones region names
    vs = d.get("variants") or [
        {"name": "primary", "pool": d.get("pool"), "min": None, "max": None, "image": d.get("image")}
    ]
    return [{**v, "object": entry["id"] if i == 0 else f"{entry['id']}-{v['name']}"} for i, v in enumerate(vs)]


def variant_names(entry: dict) -> list[str]:
    return sorted({v["name"] for c in (entry.get("deployments") or {}) for v in variants(entry, c)})


def render(entry: dict, cluster: str) -> list[dict]:
    """The objects of an endpoint on one cluster, once per variant (services/api/render_endpoint.py)."""
    # the gateway hostname of a multi-node endpoint's route is <id>-predictor.<ns>.<domain> (Knative's shape); on a
    # GPU region the gateway authorizes through the `api` Backend (this API's public hostname), here through the Service
    domain = endpoint_domain(kube.cluster_region(cluster))
    remote = kube.cluster_region(cluster) != REGION
    return [
        doc
        for v in variants(entry, cluster)
        for doc in render_endpoint.render(entry, cluster, domain, INFINIBAND_CLAIM_TEMPLATE, v, remote)
    ]


def apply(entry: dict, cluster: str, region: str = REGION) -> list[str]:
    """Render and server-side apply the endpoint on one cluster; returns the applied object names."""
    applied = []
    for doc in render(entry, cluster):
        kind, md = doc["kind"], doc["metadata"]
        ns, name = md.get("namespace", "models"), md["name"]
        if kind in PLURALS:
            g, v, plural = PLURALS[kind]
            retry(
                kube.api(region).patch_namespaced_custom_object,
                g,
                v,
                ns,
                plural,
                name,
                doc,
                field_manager=FIELD_MANAGER,
                force=True,
                _content_type="application/apply-patch+yaml",
            )
        elif kind == "PersistentVolumeClaim":
            retry(
                kube.core(region).patch_namespaced_persistent_volume_claim,
                name,
                ns,
                doc,
                field_manager=FIELD_MANAGER,
                force=True,
                _content_type="application/apply-patch+yaml",
            )
        elif kind == "Service":
            retry(
                kube.core(region).patch_namespaced_service,
                name,
                ns,
                doc,
                field_manager=FIELD_MANAGER,
                force=True,
                _content_type="application/apply-patch+yaml",
            )
        elif kind == "ConfigMap":  # the endpoint's trust bundle file (render_endpoint._trust_configmap)
            retry(
                kube.core(region).patch_namespaced_config_map,
                name,
                ns,
                doc,
                field_manager=FIELD_MANAGER,
                force=True,
                _content_type="application/apply-patch+yaml",
            )
        else:
            raise HTTPException(500, f"endpoint renderer produced an unexpected kind {kind}")
        applied.append(f"{kind}/{ns}/{name}")
    return applied


def delete_rendered(entry: dict, cluster: str, region: str = REGION) -> list[str]:
    return _delete(render(entry, cluster), region)


def delete_variants(entry: dict, cluster: str, objects: set[str], region: str = REGION) -> list[str]:
    """Delete the objects of the variants of `entry` (as it was) whose object names are in `objects`: a variant that
    a new definition dropped while its region stayed deployed (`apply` prunes nothing)."""
    docs = [
        doc
        for doc in render(entry, cluster)
        if doc["metadata"].get("labels", {}).get("app.kubernetes.io/name") in objects
    ]
    return _delete(docs, region)


def removed_variants(before: dict, after: dict, cluster: str) -> tuple[set[str], list[str]]:
    """What a redefinition dropped on a cluster that stays deployed: the object names of the variants `after` no
    longer renders, and the LiteLLM deployment ids of theirs (docs/API.md "Variants"; `litellm_group_upsert`)."""
    kept = {v["object"] for v in variants(after, cluster)}
    gone = [v for v in variants(before, cluster) if v["object"] not in kept]
    ids = [
        f"{before['id']}--{cluster}" if v["object"] == before["id"] else f"{before['id']}--{cluster}--{v['name']}"
        for v in gone
    ]
    return {v["object"] for v in gone}, ids


def _delete(docs: list[dict], region: str) -> list[str]:
    gone = []
    # Retain authorization while Knative tears routes down asynchronously. Deleted models deny
    # immediately; reconciliation removes their policy only after the last route disappears.
    for doc in sorted(docs, key=lambda d: d["kind"] == "SecurityPolicy"):
        kind, md = doc["kind"], doc["metadata"]
        ns, name = md.get("namespace", "models"), md["name"]
        if kind == "SecurityPolicy":
            routes = retry(
                kube.api(region).list_namespaced_custom_object,
                "gateway.networking.k8s.io",
                "v1",
                ns,
                "httproutes",
                label_selector=f"serving.knative.dev/route={name.removesuffix('-authorization')}-predictor",
            )
            if routes.get("items"):
                raise HTTPException(503, "waiting for predictor routes to be removed; authorization retained")
        try:
            if kind in PLURALS:
                g, v, plural = PLURALS[kind]
                retry(kube.api(region).delete_namespaced_custom_object, g, v, ns, plural, name)
            elif kind == "PersistentVolumeClaim":
                retry(kube.core(region).delete_namespaced_persistent_volume_claim, name, ns)
            elif kind == "Service":
                retry(kube.core(region).delete_namespaced_service, name, ns)
            elif kind == "ConfigMap":
                retry(kube.core(region).delete_namespaced_config_map, name, ns)
        except ApiException as e:
            if e.status != 404:
                raise HTTPException(502, f"{kind} {name}: {e.reason}")
        gone.append(f"{kind}/{ns}/{name}")
    return gone


# ---- TLS: the model endpoints' certificate of a cluster follows the endpoints deployed there ----------------
# charts/platform/templates/gateway/models-tls.yaml: Certificate `models` of the gateway namespace, second
# certificateRef of the https listener. Terraform creates the self-signed placeholder and ignores the
# three fields below; the API owns them (server-side apply, its own field manager).


def endpoint_host(entry: dict, region: str, obj: str | None = None) -> str:
    return f"{obj or entry['id']}-predictor.{entry.get('namespace', MODELS_NAMESPACE)}.{endpoint_domain(region)}"


def variant_hosts(entry: dict, region: str) -> list[str]:
    """The gateway hostnames of every variant of an endpoint deployed on the clusters of a region."""
    return [
        endpoint_host(entry, region, v["object"])
        for c in (entry.get("deployments") or {})
        if kube.cluster_region(c) == region
        for v in variants(entry, c)
    ]


def sync_certificate(region: str, entries: dict[str, dict]) -> list[str]:
    """Rewrite the `models` certificate of a region: one hostname per endpoint deployed on a cluster of that
    region (MODELS_ISSUER: the regions' issuer, fleet-ca by default), or the placeholder when there is none. Returns the hostnames."""
    domain = endpoint_domain(region)
    if not domain:
        return []
    hosts = sorted(
        {h for e in entries.values() if e.get("mode") != "run" and e.get("runtime") for h in variant_hosts(e, region)}
    )
    spec = (
        {"issuerRef": {"name": MODELS_ISSUER, "kind": "ClusterIssuer"}, "commonName": hosts[0], "dnsNames": hosts}
        if hosts
        else {
            "issuerRef": {"name": "selfsigned", "kind": "ClusterIssuer"},
            "commonName": "models.example.invalid",
            "dnsNames": ["models.example.invalid"],
        }
    )
    body = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": MODELS_CERTIFICATE, "namespace": GATEWAY_NAMESPACE},
        "spec": spec,
    }
    try:
        retry(
            kube.api(region).patch_namespaced_custom_object,
            "cert-manager.io",
            "v1",
            GATEWAY_NAMESPACE,
            "certificates",
            MODELS_CERTIFICATE,
            body,
            field_manager=FIELD_MANAGER,
            force=True,
            _content_type="application/apply-patch+yaml",
        )
    except ApiException as e:
        raise HTTPException(502, f"certificate {MODELS_CERTIFICATE} in {region}: {e.reason}")
    return hosts


# ---- LiteLLM: one model group per OpenAI endpoint ------------------------------------------------------
# A model defined here is also a LiteLLM model (`model_name` = the model id) whose deployment is the endpoint's
# gateway hostname, so tenant keys (mirrored into LiteLLM by services/api/keys.py) can call it through LiteLLM's
# /v1/chat/completions with LiteLLM's own budget and spend accounting, and so a second deployment of the same model (a reserved-GPU variant, another region) is
# one more entry of the same group, added by configuration (LiteLLM routes and fails over inside a group).


def endpoint_domain(region: str) -> str:
    """The gateway domain of a region's endpoints: ENDPOINT_DOMAINS (every GPU region, keyed by Nebius region),
    ENDPOINT_DOMAIN for the API's own cluster (single-cluster mode). A cluster id (`north`, the fleet's own names for
    its workers, as runtime definitions and placement use them) is mapped to its region first: a fleet with custom
    ids answered 503 on every real endpoint call before."""
    if region in ENDPOINT_DOMAINS:
        return ENDPOINT_DOMAINS[region]
    import kube

    mapped = kube.cluster_region(region) if region else region
    return ENDPOINT_DOMAINS.get(mapped) or (ENDPOINT_DOMAIN if REGION in (region, mapped) else "")


def litellm_group_upsert(entry: dict, cluster: str, region: str = REGION) -> str | None:
    """Register a cluster's deployment of an OpenAI endpoint in LiteLLM (api_base = the endpoint's gateway
    hostname under that region's domain); returns a warning or None."""
    domain = endpoint_domain(region)
    if entry.get("protocol") != "openai" or not LITELLM_MASTER_KEY or not domain:
        return None
    if not ROUTER_KEY:
        # the group would call the gateway hostname, where the edge requires a key of the fleet (docs/API.md "Model groups")
        log.warning(
            "model %s: ROUTER_KEY is not set (Secret api/router-key); LiteLLM group not registered", entry["id"]
        )
        return f"LiteLLM group not registered for {entry['id']}: ROUTER_KEY is not set (the endpoint still answers through this API)"
    import httpx

    mid = entry["id"]
    served = entry.get("servedModel") or mid
    try:
        with httpx.Client(timeout=20) as c:
            headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
            # one LiteLLM deployment per variant, `order` = its priority (LiteLLM tries lower orders first: the warm
            # reserved variant before the spot overflow); the first variant keeps the id of the single-deployment days
            for i, v in enumerate(variants(entry, cluster)):
                api_base = f"https://{endpoint_host(entry, region, v['object'])}/v1"
                did = f"{mid}--{cluster}" if i == 0 else f"{mid}--{cluster}--{v['name']}"
                # the router key is what the edge on the models hostnames sees (role `router`: no budget, not billed);
                # LiteLLM has already applied the caller's own key (budget, rate limit, spend) before it routes to the group
                body = {
                    "model_name": mid,
                    "litellm_params": {
                        "model": f"openai/{served}",
                        "api_base": api_base,
                        "api_key": ROUTER_KEY,
                        "order": i + 1,
                    },
                    "model_info": {"id": did, "cluster": cluster, "variant": v["name"]},
                }
                r = c.patch(f"{LITELLM_URL}/model/{did}/update", json=body, headers=headers)
                if r.status_code == 404:
                    r = c.post(f"{LITELLM_URL}/model/new", json=body, headers=headers)
                if r.status_code >= 300:
                    return f"litellm model reconciliation {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001 - the endpoint works without the LiteLLM group
        return f"litellm unreachable: {e}"
    return None


def litellm_group_delete(mid: str, cluster: str, names: list[str] | None = None) -> str | None:
    """Remove the cluster's deployments of a model from LiteLLM: the first variant's id and one per further variant."""
    return litellm_deployments_delete(
        [f"{mid}--{cluster}"] + [f"{mid}--{cluster}--{n}" for n in (names or [])]
    )  # names: the further variants


def litellm_deployments_delete(ids: list[str]) -> str | None:
    """Remove LiteLLM deployments by id; returns a warning or None."""
    if not LITELLM_MASTER_KEY:
        return None
    import httpx

    try:
        with httpx.Client(timeout=20) as c:
            for did in ids:
                r = c.post(
                    f"{LITELLM_URL}/model/delete",
                    json={"id": did},
                    headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                )
                if (
                    r.status_code >= 300
                    and r.status_code != 404
                    and not (r.status_code == 400 and "not found" in r.text)
                ):
                    # LiteLLM answers 400 "Model with id=... not found in db" for an entry it no longer has: that is the
                    # state we want (a deleted model whose entry was already gone retried its reconciliation forever)
                    return f"litellm /model/delete {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001
        return f"litellm unreachable: {e}"
    return None


# ---- offline rendering for the quality gate -----------------------------------------------------------
if __name__ == "__main__":
    # `python models.py entry --pools <pools.json> < spec.json` prints the catalog entry of a spec against a
    # given fleet; `python models.py render <cluster> [--domain <domain>] < entry.yaml` prints the entry's objects
    # on that cluster (tools/check.sh renders the example model this way, no cluster needed).
    if len(sys.argv) >= 2 and sys.argv[1] == "entry":
        pools = json.load(open(sys.argv[sys.argv.index("--pools") + 1])) if "--pools" in sys.argv else {}
        kube.fleet = lambda: {"pools": pools}
        print(yaml.safe_dump(to_entry(json.load(sys.stdin)), sort_keys=False))
    elif len(sys.argv) >= 3 and sys.argv[1] == "render":
        kube.cluster_region = lambda c: c
        ENDPOINT_DOMAINS[sys.argv[2]] = sys.argv[sys.argv.index("--domain") + 1] if "--domain" in sys.argv else ""
        print(yaml.safe_dump_all(render(yaml.safe_load(sys.stdin) or {}, sys.argv[2]), sort_keys=False))
