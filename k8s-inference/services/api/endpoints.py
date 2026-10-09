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


def to_public(m: dict, isvc: dict, region: str = REGION) -> dict:
    pred, md = isvc.get("spec", {}).get("predictor", {}), isvc.get("metadata", {})
    st = kube.endpoint_status(m["k8s_name"], m["namespace"]) if region == REGION else kube.endpoint_status(m["k8s_name"], m["namespace"], region)
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
    if annotations.get(f"{LABEL}/buffer"):
        scaling["buffer"] = int(annotations[f"{LABEL}/buffer"])
    saved_metric = (m.get("spec") or {}).get("scaling", {}).get("metric")
    if saved_metric in ("concurrency_utilization", "requests_per_second"):
        scaling["metric"] = saved_metric
        if saved_metric == "concurrency_utilization":
            scaling["target"] = scaling["utilization_percent"]
        scaling.pop("utilization_percent", None)
    return {"id": m["k8s_name"], "name": m["display_name"], "model": m["name"], "region": region,
            "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke", "status": "error" if st["status"] == "unavailable" else st["status"],
            "replicas_ready": st["replicas_ready"], "min_replicas": pred.get("minReplicas", 0), "max_replicas": pred.get("maxReplicas", 1),
            "scale_to_zero_after_s": _secs(md.get("annotations", {}).get("autoscaling.knative.dev/scale-to-zero-pod-retention-period", "2m")),
            "target_concurrency": pred.get("scaleTarget"), "gpu": m.get("gpu"), "protocol": m.get("protocol"),
            "scaling": scaling, "timeout_s": pred.get("timeout", 600),
            "image": container.get("image"), "cpu": resources.get("cpu"), "memory": resources.get("memory"),
            "created_at": md.get("creationTimestamp"), "managed_by": md.get("labels", {}).get(f"{LABEL}/created-by", "git")}
