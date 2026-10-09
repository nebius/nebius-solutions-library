"""Public KServe endpoint state and live scaling patches. Definitions can be API- or deployment-managed;
deployment synchronization may replace a live patch."""
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
    st = kube.endpoint_status(m["k8s_name"], m["namespace"], region)
    return {"id": m["k8s_name"], "name": m["display_name"], "model": m["name"], "region": region,
            "url": f"{PUBLIC_API_URL}/v1/models/{m['name']}:invoke", "status": "error" if st["status"] == "unavailable" else st["status"],
            "replicas_ready": st["replicas_ready"], "min_replicas": pred.get("minReplicas", 0), "max_replicas": pred.get("maxReplicas", 1),
            "scale_to_zero_after_s": _secs(md.get("annotations", {}).get("autoscaling.knative.dev/scale-to-zero-pod-retention-period", "2m")),
            "target_concurrency": pred.get("scaleTarget"), "gpu": m.get("gpu"), "protocol": m.get("protocol"),
            "created_at": md.get("creationTimestamp"), "managed_by": md.get("labels", {}).get(f"{LABEL}/created-by", "git")}
