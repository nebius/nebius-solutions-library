"""Read regional KServe InferenceServices. Scaling writes persist through model definitions;
fleet-managed objects are read-only through the customer API."""
import re
from config import LABEL, PUBLIC_API_URL, REGION
import kube


def _secs(d: str) -> int:
    m = re.fullmatch(r"(\d+)([smh])", str(d))
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 120


def scaling_patch(sc: dict) -> dict:
    pred = {k2: sc[k] for k, k2 in (("min_replicas", "minReplicas"), ("max_replicas", "maxReplicas"), ("target_concurrency", "scaleTarget")) if sc.get(k) is not None}
    body = {"spec": {"predictor": pred}}
    if sc.get("scale_to_zero_after_s") is not None:
        body["metadata"] = {"annotations": {"autoscaling.knative.dev/scale-to-zero-pod-retention-period": f"{int(sc['scale_to_zero_after_s'])}s"}}
    return body


def _base(m: dict, md: dict, st: dict) -> dict:
    """What every endpoint kind reports (docs/API.md `GET /v1/endpoints`): identity, live status, start-up timing of the
    newest pod, the example request, and who owns the definition (api: through POST /v1/models, editable in the console;
    git: a built-in class)."""
    return {"id": m["k8s_name"], "name": m["display_name"], "model": m["name"],
            "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke", "status": "error" if st["status"] == "unavailable" else st["status"],
            "replicas_ready": st["replicas_ready"], "gpu": m.get("gpu"), "protocol": m.get("protocol"),
            "startup_s": st.get("startup_s"), "started_at": st.get("started_at"), "ready_at": st.get("ready_at"),
            "example": m.get("example"), "created_at": md.get("creationTimestamp"),
            "managed_by": "api" if m.get("managed_by") == "api" else "git"}


def to_public(m: dict, obj: dict, region: str = REGION) -> dict:
    """The public shape of a deployed endpoint: a KServe InferenceService (one pod per replica, Knative scaling) or, for
    `nodes` > 1, a LeaderWorkerSet (one replica group of whole nodes, started and stopped explicitly)."""
    if obj.get("kind") == "LeaderWorkerSet" or int(m.get("nodes") or 1) > 1:
        return _lws_public(m, obj, region)
    pred, md = obj.get("spec", {}).get("predictor", {}), obj.get("metadata", {})
    st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
    containers = pred.get("containers") or []
    container = containers[0] if containers else {}
    resources = container.get("resources", {}).get("requests", {})
    annotations = md.get("annotations") or {}
    scaling = {"min": pred.get("minReplicas", 0), "max": pred.get("maxReplicas", 1),
               "metric": pred.get("scaleMetric", "concurrency"), "target": pred.get("scaleTarget", 4),
               "container_concurrency": pred.get("containerConcurrency", 0),
               "utilization_percent": float(annotations.get("autoscaling.knative.dev/target-utilization-percentage", 70)),
               "cooldown_s": _secs(annotations.get("autoscaling.knative.dev/scale-down-delay", "0s")),
               "window_s": _secs(annotations.get("autoscaling.knative.dev/window", "60s")),
               "idle_s": _secs(annotations.get("autoscaling.knative.dev/scale-to-zero-pod-retention-period", "2m"))}
    if int(((m.get("spec") or {}).get("scaling") or {}).get("buffer") or 0) > 0:   # held by the dispatcher (buffer.py), not rendered
        scaling["buffer"] = int(m["spec"]["scaling"]["buffer"])
    saved_metric = (m.get("spec") or {}).get("scaling", {}).get("metric")
    if saved_metric in ("concurrency_utilization", "requests_per_second"):
        scaling["metric"] = saved_metric
        if saved_metric == "concurrency_utilization":
            scaling["target"] = scaling["utilization_percent"]
        scaling.pop("utilization_percent", None)
    return {**_base(m, md, st), "region": region, "nodes": 1,
            "min_replicas": pred.get("minReplicas", 0), "max_replicas": pred.get("maxReplicas", 1),
            "scale_to_zero_after_s": _secs(annotations.get("autoscaling.knative.dev/scale-to-zero-pod-retention-period", "2m")),
            "target_concurrency": pred.get("scaleTarget"), "scaling": scaling, "timeout_s": pred.get("timeout", 600),
            "image": container.get("image"), "cpu": resources.get("cpu"), "memory": resources.get("memory")}


def _lws_public(m: dict, obj: dict, region: str) -> dict:
    """A multi-node endpoint (services/api/models.py `nodes`): one LeaderWorkerSet replica group of `nodes` pods;
    `replicas` 1 or 0 is started or stopped, there is no autoscaling in between, `idle_s` stops it (control API)."""
    md, spec = obj.get("metadata", {}), obj.get("spec", {})
    nodes = int((md.get("annotations") or {}).get("serverless2.nebius/nodes") or m.get("nodes") or spec.get("leaderWorkerTemplate", {}).get("size") or 1)
    st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=nodes)
    leader = (spec.get("leaderWorkerTemplate", {}).get("leaderTemplate") or {}).get("spec", {})
    containers = leader.get("containers") or []
    container = containers[0] if containers else {}
    resources = container.get("resources", {}).get("requests", {})
    saved = (m.get("spec") or {}).get("scaling") or {}
    idle = int((md.get("annotations") or {}).get("serverless2.nebius/idle-s") or saved.get("idle_s") or 0)
    scaling = {"min": int(spec.get("replicas", 0)), "max": max(int(spec.get("replicas", 0)), 1), "metric": "none", "idle_s": idle}
    return {**_base(m, md, st), "region": region, "nodes": nodes,
            "interconnect": (md.get("annotations") or {}).get("serverless2.nebius/interconnect", "none"),
            "pods_ready": st.get("pods_ready"), "last_request_at": st.get("last_request_at"),
            "min_replicas": scaling["min"], "max_replicas": scaling["max"], "scale_to_zero_after_s": idle,
            "target_concurrency": None, "scaling": scaling, "timeout_s": int((m.get("spec") or {}).get("timeout_s") or 600),
            "image": container.get("image"), "cpu": resources.get("cpu"), "memory": resources.get("memory")}
