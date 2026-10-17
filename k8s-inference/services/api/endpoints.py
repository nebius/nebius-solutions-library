"""Read regional KServe InferenceServices. Scaling writes persist through model definitions;
fleet-managed objects are read-only through the customer API."""

import re

import instances
import kube
from config import PUBLIC_API_URL, REGION


def _secs(d: str) -> int:
    m = re.fullmatch(r"(\d+)([smh])", str(d))
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 120


def _public(
    m: dict,
    obj: dict,
    st: dict,
    region: str,
    nodes: int,
    extra: dict,
    scaling: dict,
    bounds: tuple,
    timeout: int,
    container: dict,
) -> dict:
    """What every endpoint kind reports (docs/API.md `GET /v1/endpoints`): identity, live status, start-up timing of the
    newest pod, the example request, who owns the definition (api: through POST /v1/models, editable in the console;
    git: a built-in class), the replicas with their placement, the scaling policy (`bounds`: its older flat fields min,
    max, scale-to-zero seconds, target concurrency) and the serving container."""
    md = obj.get("metadata", {})
    resources = container.get("resources", {}).get("requests", {})
    return {
        "id": m["k8s_name"],
        "name": m["display_name"],
        "model": m["name"],
        "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke",
        "status": "error" if st["status"] == "unavailable" else st["status"],
        "replicas_ready": st["replicas_ready"],
        "gpu": m.get("gpu"),
        "protocol": m.get("protocol"),
        "startup_s": st.get("startup_s"),
        "started_at": st.get("started_at"),
        "ready_at": st.get("ready_at"),
        "example": m.get("example"),
        "created_at": md.get("creationTimestamp"),
        "managed_by": "api" if m.get("managed_by") == "api" else "git",
        "region": region,
        "replicas": instances.replicas(
            kube.endpoint_pods(m["k8s_name"], m["namespace"], region, nodes=nodes), kube.cluster_id_or_none(region)
        ),
        "nodes": nodes,
        **extra,
        "min_replicas": bounds[0],
        "max_replicas": bounds[1],
        "scale_to_zero_after_s": bounds[2],
        "target_concurrency": bounds[3],
        "scaling": scaling,
        "timeout_s": timeout,
        "image": container.get("image"),
        "cpu": resources.get("cpu"),
        "memory": resources.get("memory"),
    }


def to_public(m: dict, obj: dict, region: str = REGION) -> dict:
    """The public shape of a deployed endpoint: a KServe InferenceService (one pod per replica, Knative scaling) or, for
    `nodes` > 1, a LeaderWorkerSet (one replica group of whole nodes, started and stopped explicitly)."""
    if obj.get("kind") == "LeaderWorkerSet" or int(m.get("nodes") or 1) > 1:
        return _lws_public(m, obj, region)
    pred = obj.get("spec", {}).get("predictor", {})
    st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
    containers = pred.get("containers") or []
    ann = obj.get("metadata", {}).get("annotations") or {}
    # the native vocabulary read back from Knative's fields (models.py `to_entry` is the inverse); an API-managed
    # definition's saved policy wins, it carries what Knative defaults silently
    rate = pred.get("scaleMetric") == "rps"
    utilization = int(float(ann.get("autoscaling.knative.dev/target-utilization-percentage", 70)))
    idle = _secs(ann.get("autoscaling.knative.dev/scale-to-zero-pod-retention-period", "2m"))
    knative = {
        "min": pred.get("minReplicas", 0),
        "max": pred.get("maxReplicas", 1),
        "metric": "requests_per_second" if rate else "concurrency_utilization",
        "target": pred.get("scaleTarget", 5) if rate else utilization,
        "container_concurrency": 0 if rate else pred.get("containerConcurrency") or pred.get("scaleTarget", 1),
        "cooldown_s": _secs(ann.get("autoscaling.knative.dev/scale-down-delay", "0s")),
        "window_s": _secs(ann.get("autoscaling.knative.dev/window", "60s")),
        "idle_s": idle,
    }
    scaling = {**knative, **((m.get("spec") or {}).get("scaling") or {})}
    bounds = (knative["min"], knative["max"], idle, pred.get("scaleTarget"))
    return _public(
        m, obj, st, region, 1, {}, scaling, bounds, pred.get("timeout", 600), containers[0] if containers else {}
    )


def _lws_public(m: dict, obj: dict, region: str) -> dict:
    """A multi-node endpoint (services/api/models.py `nodes`): one LeaderWorkerSet replica group of `nodes` pods;
    `replicas` 1 or 0 is started or stopped, there is no autoscaling in between, `idle_s` stops it (control API)."""
    ann, spec = obj.get("metadata", {}).get("annotations") or {}, obj.get("spec", {})
    nodes = int(
        ann.get("serverless2.nebius/nodes") or m.get("nodes") or spec.get("leaderWorkerTemplate", {}).get("size") or 1
    )
    st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=nodes)
    containers = (spec.get("leaderWorkerTemplate", {}).get("leaderTemplate") or {}).get("spec", {}).get(
        "containers"
    ) or []
    saved = (m.get("spec") or {}).get("scaling") or {}
    idle = int(ann.get("serverless2.nebius/idle-s") or saved.get("idle_s") or 0)
    replicas = int(spec.get("replicas", 0))
    scaling = {"min": replicas, "max": max(replicas, 1), "metric": "none", "idle_s": idle}
    extra = {
        "interconnect": ann.get("serverless2.nebius/interconnect", "none"),
        "pods_ready": st.get("pods_ready"),
        "last_request_at": st.get("last_request_at"),
    }
    timeout = int((m.get("spec") or {}).get("timeout_s") or 600)
    bounds = (replicas, max(replicas, 1), idle, None)
    return _public(m, obj, st, region, nodes, extra, scaling, bounds, timeout, containers[0] if containers else {})
