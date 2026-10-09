"""Models as an input: a container plus a few knobs, defined through the API (`POST /v1/models`, the console
form) and kept in the fleet database (services/api/db.py). Nothing about a model lives in terraform.tfvars.

A model definition ("spec") is the shape of the console form:

    {"id": "my-llm", "kind": "endpoint" | "job",
     "image": "vllm/vllm-openai:v0.11.0",          # any registry; pulled through the fleet's image cache
     "command": ["..."] | "sh -c ...", "args": [...], "env": {"NAME": "value"},
     "port": 8000, "protocol": "openai" | "http" | "websocket" | "grpc", "path": "/v1/chat/completions",
     "health_path": "/health", "served_model": "llm",
     "gpu": {"count": 1, "classes": ["h100", "l40s"]},       # preferred class first; pools of these classes
     "resources": {"cpu": "8", "memory": "32Gi"}, "scaling": {"min": 0, "max": 2, "target": 4},
     "timeout_s": 600, "shm_gib": 4, "pull_secret": "ngc",
     "weights": {"path": "my-llm", "mount_path": "/weights", "env": {"HF_HOME": "/weights"}},
     "regions": ["eu-north1"],                                 # default: every region with a pool of a class
     "display_name": "...", "description": "...",
     # jobs only
     "cpu": "4", "memory": "16Gi", "disk_gi": 50, "grace_seconds": 300, "scratch": "network" | "local-nvme",
     "parameters": [{"name": "...", "type": "string", "default": "..."}]}

Reserved for later, accepted and stored but not acted on yet (so adding them is not a breaking change):
`images` (per GPU class: {"default": ..., "h100": ...}), `deployments.<region>.variants` (several
deployments of one model in a region, e.g. a warm floor on reserved GPUs plus scale-to-zero on spot, behind
one model name), `routing` (request field -> variant). The LiteLLM model group is already one entry per
model, so variants can be added by configuration.

`to_entry` turns a spec into the catalog entry the rest of the API and `charts/endpoint` understand
(catalog/README.md); the entry is stored next to the spec (database row on the control API, ConfigMap copy
`catalog-<id>` in the `api` namespace of every region). Entries that come from files (the bundled classes)
are read-only here.
"""
import json, logging, os, re, subprocess, sys, tempfile, time
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
from kubernetes.client.rest import ApiException
import db, kube
from config import ACME_ISSUER, API_NAMESPACE, CHART_DIR, ENDPOINT_DOMAIN, ENDPOINT_DOMAINS, GATEWAY_NAMESPACE, IMAGES_HOST, MODELS_CERTIFICATE, IMAGES_SOURCE, LABEL, LITELLM_INTERNAL_KEY, LITELLM_MASTER_KEY, LITELLM_URL, MODELS_NAMESPACE, REGION
from resilience import retry

log = logging.getLogger("api")

CATALOG_LABEL = f"{LABEL}/catalog"          # runtime
MODEL_LABEL = f"{LABEL}/model"
MANAGED_BY_LABEL = f"{LABEL}/managed-by"    # api | terraform
FIELD_MANAGER = "serverless2-api"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
PROTOCOLS = ("http", "openai", "websocket", "grpc")
PROTOCOL_PATH = {"openai": "/v1/chat/completions", "http": "/", "websocket": "/", "grpc": "/"}
RESERVED = ("images", "routing")            # stored, not acted on


class ScalingSpec(BaseModel):
    """Knative KPA controls supported by the pinned endpoint deployment."""
    model_config = ConfigDict(extra="forbid")
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)
    metric: Literal["concurrency_utilization", "requests_per_second", "concurrency", "rps"] | None = None
    target: int | None = Field(default=None, ge=1)
    utilization_percent: int | None = Field(default=None, ge=1, le=100)
    container_concurrency: int | None = Field(default=None, ge=0, le=1000)
    cooldown_s: int | None = Field(default=None, ge=0, le=3600)
    window_s: int | None = Field(default=None, ge=6, le=3600)
    idle_s: int | None = Field(default=None, ge=0, le=3600)
    buffer: int | None = Field(default=None, ge=0, le=8)   # ready replicas kept above demand while serving (docs/SCHEDULING.md "Scaling buffer")
# public registries -> the alias the image cache serves them under (terraform.tfvars `images.upstreams`)
UPSTREAM_ALIAS = {"docker.io": "docker", "registry-1.docker.io": "docker", "index.docker.io": "docker",
                  "nvcr.io": "nvcr", "ghcr.io": "ghcr", "quay.io": "quay", "registry.k8s.io": "k8s"}


def normalise_image(ref: str) -> str:
    """A plain image reference the way people write it (`vllm/vllm-openai:latest`, `nvcr.io/nim/x:1`,
    `<fleet registry>/team/x:1`) -> the fleet's logical registry host, which every node resolves to its
    region's cache (docs/IMAGES.md). Already-logical and unknown private registries are left as they are."""
    ref = ref.strip()
    if not ref or ref.startswith(IMAGES_HOST + "/") or "{{" in ref:
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


def _env_list(env: dict | None) -> list[dict]:
    return [{"name": k, "value": str(v)} for k, v in (env or {}).items()]


def cluster_id() -> str:
    """The fleet's id of this API's cluster (fleet-prices `region` of its pools; `hub` on the reference fleet, the
    region name in the Terraform solution): the key of `deployments` and the chart's `cluster` value."""
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
    return sorted(cid for cid, pools in have.items() if not classes or any(p.get("gpu_class") in classes for p in pools))


def pool_for(cid: str, classes: list[str], count: int = 1) -> str | None:
    """The pool an endpoint of these classes is pinned to on a cluster: the first class that has a pool there
    whose nodes carry at least `count` GPUs (an 8-GPU replica never fits a 1-GPU preset); among those, reserved
    before on-demand before spot (the fleet's capacity order: a reservation is paid for whether it serves or not),
    then the smallest fitting preset (fewer idle GPUs on the node), plain pools before InfiniBand ones (those nodes
    are for multi-node runs), then by name."""
    order = {"reserved": 0, "on_demand": 1, "spot": 2}
    pools = [p for p in kube.fleet()["pools"].values() if (p.get("region") or REGION) == cid]
    for c in classes:
        cands = sorted((p for p in pools if p.get("gpu_class") == c and int(p.get("gpus_per_node") or 1) >= max(int(count), 1)),
                       key=lambda p: (order.get(p.get("capacity"), 9), int(p.get("gpus_per_node") or 1),
                                      (p.get("interconnect") or "none") != "none", p["pool"]))
        if cands:
            return cands[0]["pool"]
    return None


def validate(spec: dict) -> dict:
    s = dict(spec or {})
    secrets = s.get("env_secrets", [])
    if not isinstance(secrets, list) or any(not isinstance(name, str) or len(name) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name) for name in secrets):
        raise HTTPException(400, "env_secrets: a list of Kubernetes Secret names in the workload namespace")
    mid = s.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        raise HTTPException(400, "id: lowercase letters, digits and dashes, 2-41 characters, starting with a letter")
    kind = s.get("kind", "endpoint")
    if kind not in ("endpoint", "job"):
        raise HTTPException(400, "kind: endpoint | job")
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
        try:
            scaling = ScalingSpec.model_validate(s.get("scaling") or {}).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise HTTPException(400, f"scaling: {exc.errors()[0]['msg']}") from exc
        if scaling.get("min", 0) > scaling.get("max", 1):
            raise HTTPException(400, "Minimum replicas cannot exceed maximum replicas.")
        metric = scaling.get("metric", "concurrency")
        if metric in ("concurrency_utilization", "requests_per_second"):
            if scaling.get("window_s", 30) > 300:
                raise HTTPException(400, "Evaluation interval must be between 6 and 300 seconds.")
            scaling.pop("utilization_percent", None)  # target has metric-specific units
            if metric == "concurrency_utilization":
                target = scaling.setdefault("target", 100)
                concurrency = scaling.setdefault("container_concurrency", 1)
                if target > 100 or concurrency < 1:
                    raise HTTPException(400, "Concurrency target must be 1–100%; replica concurrency must be at least 1.")
            else:
                scaling.setdefault("target", 5)
                scaling["container_concurrency"] = 0  # request-rate mode has no concurrency cap
            scaling.setdefault("window_s", 30)
        s["scaling"] = scaling
    classes = list(gpu.get("classes") or [])
    fleet_classes = sorted({p.get("gpu_class") for p in kube.fleet()["pools"].values() if p.get("gpu_class")})
    if classes and fleet_classes and not any(c in fleet_classes for c in classes):
        raise HTTPException(400, f"gpu.classes {classes}: none exists in this fleet (classes: {fleet_classes})")
    s["kind"] = kind
    return s


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
        "id": mid, "displayName": s.get("display_name") or mid, "description": s.get("description", ""),
        "task": s.get("task", "custom"), "managed_by": managed_by, "spec": s,
    }
    for k in RESERVED:
        if k in s:
            entry[k] = s[k]
    if kind == "endpoint":
        protocol = s.get("protocol", "http")
        path = s.get("path") or PROTOCOL_PATH[protocol]
        port = int(s.get("port", 8080))
        scaling = s.get("scaling") or {}
        metric = scaling.get("metric", "concurrency")
        concurrency = int(scaling.get("container_concurrency", 1 if metric == "concurrency_utilization" else 0))
        native_metric = "concurrency" if metric == "concurrency_utilization" else "rps" if metric == "requests_per_second" else metric
        native_target = concurrency if metric == "concurrency_utilization" else int(scaling.get("target", 5 if metric == "requests_per_second" else 4))
        res = s.get("resources") or {}
        runtime: dict = {
            "image": image, "port": port, "protocol": protocol,
            "resources": {"gpu": count, "cpu": str(res.get("cpu", "4")), "memory": str(res.get("memory", "16Gi"))},
            "scaling": {"minReplicas": int(scaling.get("min", 0)), "maxReplicas": int(scaling.get("max", 1)),
                        "metric": native_metric, "target": native_target,
                        **({"containerConcurrency": concurrency} if "container_concurrency" in scaling or metric == "concurrency_utilization" else {}),
                        **({"scaleToZeroRetention": f"{int(scaling['idle_s'])}s"} if scaling.get("idle_s") is not None else {})},
            "timeout": int(s.get("timeout_s", 600)),
            "shm": {"enabled": True, "size": f"{int(s.get('shm_gib', 1))}Gi"},
        }
        annotations = {}
        for key, annotation, suffix in (
            ("utilization_percent", "target-utilization-percentage", ""),
            ("cooldown_s", "scale-down-delay", "s"),
            ("window_s", "window", "s"),
        ):
            if key in scaling:
                annotations[f"autoscaling.knative.dev/{annotation}"] = f"{scaling[key]}{suffix}"
        if metric in ("concurrency_utilization", "requests_per_second"):
            annotations["autoscaling.knative.dev/target-utilization-percentage"] = str(scaling.get("target", 100)) if metric == "concurrency_utilization" else "100"
            if metric == "requests_per_second":
                runtime["scaling"]["containerConcurrency"] = 0
        if int(scaling.get("buffer") or 0) > 0:
            annotations[f"{LABEL}/buffer"] = str(int(scaling["buffer"]))   # the dispatcher holds min-scale at demand + buffer
        if annotations:
            runtime["annotations"] = annotations
        if s.get("args"):
            runtime["args"] = [str(a) for a in s["args"]]
        if (cmd := _command(s.get("command"))):
            runtime["command"] = cmd
        if s.get("env"):
            runtime["env"] = _env_list(s["env"])
        if s.get("env_secrets"):
            runtime["envFrom"] = [{"secretRef": {"name": name}} for name in s["env_secrets"]]
        if s.get("health_path"):
            runtime["readinessProbe"] = {"path": s["health_path"], "port": port}
        if s.get("pull_secret"):
            runtime["imagePullSecrets"] = [s["pull_secret"]]
        w = s.get("weights") or {}
        if w:
            runtime["weights"] = {"sharedFilesystem": {"enabled": True, "path": w.get("path", mid), "mountPath": w.get("mount_path", "/weights")},
                                  "env": dict(w.get("env") or {})}
        entry.update({
            "mode": "sync", "protocol": protocol, "port": port,
            "endpoints": {"chat" if protocol == "openai" else "invoke": path, **({"ready": s["health_path"]} if s.get("health_path") else {})},
            "gpu": {"count": count, "classes": classes} if classes else "none",
            "servedModel": s.get("served_model"),
            "runtime": runtime,
            # no GPU class: the endpoint runs on the region's system pool (CPU nodes, label serverless2.nebius/pool=system)
            "deployments": {r: ({"pool": p} if (p := (pool_for(r, classes, count) if classes else "system")) else {}) for r in regions},
        })
        if not entry["servedModel"]:
            entry.pop("servedModel")
    else:
        job: dict = {
            "image": image, "command": s.get("command") or "", "gpu": count,
            "cpu": str(s.get("cpu", "4")), "memory": str(s.get("memory", "16Gi")), "pvcSizeGi": int(s.get("disk_gi", 50)),
            "graceSeconds": int(s.get("grace_seconds", 300)), "scratch": s.get("scratch", "network"),
            "env": dict(s.get("env") or {}),
            "envFrom": [{"secretRef": {"name": name}} for name in s.get("env_secrets", [])],
        }
        if s.get("args"):
            job["args"] = s["args"]
        if s.get("pull_secret"):
            job["imagePullSecret"] = s["pull_secret"]
        entry.update({
            "mode": "run", "protocol": "kubernetes-job",
            "gpu": {"count": count, "classes": classes} if classes else "none",
            "job": job, "parameters": list(s.get("parameters") or []),
            "deployments": {r: {} for r in regions},
        })
    return entry


# ---- persistence: the fleet database on the control API, a read-only ConfigMap copy per region --------------
# The control API (the one with DATABASE_URL, services/api/db.py) owns the definitions: every write lands in
# the `models` table (with its history) and is copied as ConfigMap `catalog-<id>` into the `api` namespace of
# every other cluster of the fleet, through the api-agent identities (REGION_KUBECONFIGS). A regional API
# only reads its copies; it answers writes with 409 and points at the fleet's API.

def _cm_name(mid: str) -> str:
    return f"catalog-{mid}"


def list_runtime(region: str = REGION) -> dict[str, dict]:
    """Runtime catalog entries: {id: entry}. On the control API from the database; on a regional API (or
    for another region) from that cluster's ConfigMap copies."""
    if region == REGION and db.enabled():
        return {mid: row["entry"] for mid, row in db.list_models().items()}
    out = {}
    try:
        cms = retry(kube.core(region).list_namespaced_config_map, API_NAMESPACE, label_selector=f"{CATALOG_LABEL}=runtime")
    except Exception:  # noqa: BLE001 - no RBAC / no cluster: an empty runtime catalog
        return out
    for cm in getattr(cms, "items", []) or []:
        data = (getattr(cm, "data", None) or (cm.get("data") if isinstance(cm, dict) else {}) or {})
        try:
            e = yaml.safe_load(data.get("entry.yaml") or "") or {}
        except yaml.YAMLError:
            continue
        if e.get("id"):
            out[e["id"]] = e
    return out


def save(entry: dict, region: str = REGION):
    """The ConfigMap copy of an entry in one cluster (server-side apply)."""
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": _cm_name(entry["id"]), "namespace": API_NAMESPACE,
                         "labels": {CATALOG_LABEL: "runtime", MODEL_LABEL: entry["id"], MANAGED_BY_LABEL: entry.get("managed_by", "api")}},
            "data": {"entry.yaml": yaml.safe_dump(entry, sort_keys=False), "spec.json": json.dumps(entry.get("spec") or {}, indent=1)}}
    try:
        retry(kube.core(region).patch_namespaced_config_map, _cm_name(entry["id"]), API_NAMESPACE, body,
              field_manager=FIELD_MANAGER, force=True, _content_type="application/apply-patch+yaml")
    except ApiException as e:
        raise HTTPException(502, f"catalog ConfigMap in {region}: {e.reason}")


def remove(mid: str, region: str = REGION):
    try:
        retry(kube.core(region).delete_namespaced_config_map, _cm_name(mid), API_NAMESPACE)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(502, f"catalog ConfigMap in {region}: {e.reason}")


def copy_regions() -> list[str]:
    """The clusters that get a ConfigMap copy: every region the control API reaches, except itself (its own
    source is the database)."""
    return [r for r in kube.regions() if r != REGION]


# ---- rendering: the same chart Terraform uses, applied with server-side apply --------------------------

PLURALS = {"InferenceService": ("serving.kserve.io", "v1beta1", "inferenceservices"),
           "SecurityPolicy": ("gateway.envoyproxy.io", "v1alpha1", "securitypolicies"),
           "LocalModelCache": ("serving.kserve.io", "v1alpha1", "localmodelcaches")}


def render(entry: dict, cluster: str) -> list[dict]:
    """`helm template` of charts/endpoint in catalog mode for one cluster: the rendered objects."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(entry, f, sort_keys=False)
        values = f.name
    try:
        r = subprocess.run(["helm", "template", entry["id"], CHART_DIR, "-f", values, "--set", f"cluster={cluster}"],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(values)
    if r.returncode:
        raise HTTPException(400, f"endpoint chart: {r.stderr.strip()[-400:]}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def apply(entry: dict, cluster: str, region: str = REGION) -> list[str]:
    """Render and server-side apply the endpoint on one cluster; returns the applied object names."""
    applied = []
    for doc in render(entry, cluster):
        kind, md = doc["kind"], doc["metadata"]
        ns, name = md.get("namespace", "models"), md["name"]
        if kind in PLURALS:
            g, v, plural = PLURALS[kind]
            retry(kube.api(region).patch_namespaced_custom_object, g, v, ns, plural, name, doc,
                  field_manager=FIELD_MANAGER, force=True, _content_type="application/apply-patch+yaml")
        elif kind == "PersistentVolumeClaim":
            retry(kube.core(region).patch_namespaced_persistent_volume_claim, name, ns, doc,
                  field_manager=FIELD_MANAGER, force=True, _content_type="application/apply-patch+yaml")
        else:
            raise HTTPException(500, f"endpoint chart rendered an unexpected kind {kind}")
        applied.append(f"{kind}/{ns}/{name}")
    return applied


def delete_rendered(entry: dict, cluster: str, region: str = REGION) -> list[str]:
    gone = []
    # Retain authorization while Knative tears routes down asynchronously. Deleted models deny
    # immediately; reconciliation removes their policy only after the last route disappears.
    for doc in sorted(render(entry, cluster), key=lambda d: d["kind"] == "SecurityPolicy"):
        kind, md = doc["kind"], doc["metadata"]
        ns, name = md.get("namespace", "models"), md["name"]
        if kind == "SecurityPolicy":
            routes = retry(kube.api(region).list_namespaced_custom_object,
                           "gateway.networking.k8s.io", "v1", ns, "httproutes",
                           label_selector=f"serving.knative.dev/route={entry['id']}-predictor")
            if routes.get("items"):
                raise HTTPException(503, "waiting for predictor routes to be removed; authorization retained")
        try:
            if kind in PLURALS:
                g, v, plural = PLURALS[kind]
                retry(kube.api(region).delete_namespaced_custom_object, g, v, ns, plural, name)
            elif kind == "PersistentVolumeClaim":
                retry(kube.core(region).delete_namespaced_persistent_volume_claim, name, ns)
        except ApiException as e:
            if e.status != 404:
                raise HTTPException(502, f"{kind} {name}: {e.reason}")
        gone.append(f"{kind}/{ns}/{name}")
    return gone


# ---- TLS: the model endpoints' certificate of a cluster follows the endpoints deployed there ----------------
# clusters/common/manifests/gateway/models-tls.yaml: Certificate `models` of the gateway namespace, second
# certificateRef of the https listener. Terraform creates the self-signed placeholder and ignores the
# three fields below; the API owns them (server-side apply, its own field manager).

def endpoint_host(entry: dict, region: str) -> str:
    return f"{entry['id']}-predictor.{entry.get('namespace', MODELS_NAMESPACE)}.{endpoint_domain(region)}"


def sync_certificate(region: str, entries: dict[str, dict]) -> list[str]:
    """Rewrite the `models` certificate of a region: one hostname per endpoint deployed on a cluster of that
    region (ACME_ISSUER), or the placeholder when there is none. Returns the hostnames."""
    domain = endpoint_domain(region)
    if not domain:
        return []
    hosts = sorted(endpoint_host(e, region) for e in entries.values()
                   if e.get("mode") != "run" and e.get("runtime") and any(kube.cluster_region(c) == region for c in (e.get("deployments") or {})))
    spec = ({"issuerRef": {"name": ACME_ISSUER, "kind": "ClusterIssuer"}, "commonName": hosts[0], "dnsNames": hosts} if hosts
            else {"issuerRef": {"name": "selfsigned", "kind": "ClusterIssuer"}, "commonName": "models.example.invalid", "dnsNames": ["models.example.invalid"]})
    body = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
            "metadata": {"name": MODELS_CERTIFICATE, "namespace": GATEWAY_NAMESPACE}, "spec": spec}
    try:
        retry(kube.api(region).patch_namespaced_custom_object, "cert-manager.io", "v1", GATEWAY_NAMESPACE, "certificates", MODELS_CERTIFICATE, body,
              field_manager=FIELD_MANAGER, force=True, _content_type="application/apply-patch+yaml")
    except ApiException as e:
        raise HTTPException(502, f"certificate {MODELS_CERTIFICATE} in {region}: {e.reason}")
    return hosts


# ---- LiteLLM: one model group per OpenAI endpoint ------------------------------------------------------
# A model defined here is also a LiteLLM model (`model_name` = the model id) whose deployment is the endpoint's
# gateway hostname, so tenant keys can call it through LiteLLM's /v1/chat/completions with their budget and
# spend accounting, and so a second deployment of the same model (a reserved-GPU variant, another region) is
# one more entry of the same group, added by configuration (LiteLLM routes and fails over inside a group).

def endpoint_domain(region: str) -> str:
    """The gateway domain of a region's endpoints: ENDPOINT_DOMAINS on the control API, ENDPOINT_DOMAIN for
    the API's own cluster."""
    return ENDPOINT_DOMAINS.get(region) or (ENDPOINT_DOMAIN if region == REGION else "")


def litellm_group_upsert(entry: dict, cluster: str, region: str = REGION) -> str | None:
    """Register a cluster's deployment of an OpenAI endpoint in LiteLLM (api_base = the endpoint's gateway
    hostname under that region's domain); returns a warning or None."""
    domain = endpoint_domain(region)
    if entry.get("protocol") != "openai" or not LITELLM_MASTER_KEY or not domain:
        return None
    if not LITELLM_INTERNAL_KEY:
        # the group would call the gateway hostname, where the edge requires a LiteLLM key (docs/API.md "Model groups")
        log.warning("model %s: LITELLM_INTERNAL_KEY is not set (Secret api/litellm-internal); LiteLLM group not registered", entry["id"])
        return f"LiteLLM group not registered for {entry['id']}: LITELLM_INTERNAL_KEY is not set (the endpoint still answers through this API)"
    import httpx
    mid = entry["id"]
    served = entry.get("servedModel") or mid
    api_base = f"https://{endpoint_host(entry, region)}/v1"
    # the platform-internal key is what the edge on the models hostnames sees; LiteLLM has already applied the
    # caller's own key (budget, rate limit, spend) before it routes to the group
    body = {"model_name": mid, "litellm_params": {"model": f"openai/{served}", "api_base": api_base, "api_key": LITELLM_INTERNAL_KEY},
            "model_info": {"id": f"{mid}--{cluster}", "cluster": cluster}}
    try:
        with httpx.Client(timeout=20) as c:
            headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
            r = c.patch(f"{LITELLM_URL}/model/{mid}--{cluster}/update", json=body, headers=headers)
            if r.status_code == 404:
                r = c.post(f"{LITELLM_URL}/model/new", json=body, headers=headers)
        if r.status_code >= 300:
            return f"litellm model reconciliation {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001 - the endpoint works without the LiteLLM group
        return f"litellm unreachable: {e}"
    return None


def litellm_group_delete(mid: str, cluster: str) -> str | None:
    if not LITELLM_MASTER_KEY:
        return None
    import httpx
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{LITELLM_URL}/model/delete", json={"id": f"{mid}--{cluster}"}, headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"})
        if r.status_code >= 300 and r.status_code != 404:
            return f"litellm /model/delete {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001
        return f"litellm unreachable: {e}"
    return None


# ---- offline rendering for the quality gate -----------------------------------------------------------
if __name__ == "__main__":
    # `python models.py entry --pools <pools.json> < spec.json` prints the catalog entry of a spec against a
    # given fleet (tools/check.sh renders the example model with charts/endpoint, no cluster needed).
    if len(sys.argv) >= 2 and sys.argv[1] == "entry":
        pools = json.load(open(sys.argv[sys.argv.index("--pools") + 1])) if "--pools" in sys.argv else {}
        kube.fleet = lambda: {"pools": pools}
        print(yaml.safe_dump(to_entry(json.load(sys.stdin)), sort_keys=False))
