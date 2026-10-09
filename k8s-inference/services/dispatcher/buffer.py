"""Ready-replica buffer for endpoints (docs/SCHEDULING.md "Scaling buffer").

A model with `scaling.buffer = N` keeps N ready replicas more than its load needs while it serves: the live
revision's `min-scale` is held at demand + N. Demand is Knative's own number, the stable concurrency (or
requests per second) over the per-pod target, read from the autoscaler's metrics, so the floor we set never
hides it. At zero demand the floor returns to the model's own minimum, so scale to zero still works, and a
floor is lowered only after the model's cooldown (its scale-down delay, 120 s by default). The buffer is read from the API's
per-region model copy (a ConfigMap), never from the InferenceService, so changing it rolls no revision. The floor
goes on the Revision, not the InferenceService: a Revision annotation takes effect at once and creates no
rollout (verified on Knative 1.23, 2026-10-09); a new revision starts from the template and is picked up on
the next cycle. Runs on the elected dispatcher replica, through the same worker kubeconfigs as the dispatch.
"""
import json
import logging
import math
import re
import time

LABEL = "serverless2.nebius"
CATALOG_NS, CATALOG_SELECTOR = "api", f"{LABEL}/catalog=runtime"   # the API's per-region model copies (services/api/models.py save)
RAISED_ANN = f"{LABEL}/buffer-raised-at"    # on the Revision: when we last raised its floor (epoch seconds)
MIN_ANN = "autoscaling.knative.dev/min-scale"
COOLDOWN_ANN = "autoscaling.knative.dev/scale-down-delay"
METRICS_PATH = "/api/v1/namespaces/knative-serving/services/autoscaler:9090/proxy/metrics"
NAMESPACE = "models"
METRIC_RE = re.compile(r'^kn_revision_((?:concurrency|rps)_(?:stable|target))\{([^}]*)\}\s+([0-9.eE+-]+)')
log = logging.getLogger("dispatcher.buffer")


def parse_metrics(text: str) -> dict:
    """{revision: {"concurrency_stable": x, "concurrency_target": y, "rps_stable": ..., "rps_target": ...}}
    from the Knative autoscaler's Prometheus exposition (OTel names: kn_revision_<metric>{kn_revision_name=...})."""
    out: dict = {}
    for line in text.splitlines():
        m = METRIC_RE.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        rev = re.search(r'kn_revision_name="([^"]*)"', labels)
        if rev:
            out.setdefault(rev.group(1), {})[name] = float(value)
    return out


def demand(m: dict) -> int | None:
    """Replicas the load needs now, Knative's formula: stable metric over its per-pod target. None: not measured."""
    for kind in ("concurrency", "rps"):
        stable, target = m.get(f"{kind}_stable"), m.get(f"{kind}_target")
        if stable is not None and target:
            return math.ceil(stable / target - 1e-9) if stable > 0 else 0
    return None


def floor_for(dem: int | None, base_min: int, max_replicas: int, buffer: int) -> int:
    """The min-scale to hold: the model's own minimum while idle or unmeasured, else demand + buffer within max."""
    if dem is None or dem <= 0 or buffer <= 0:
        return base_min
    return max(base_min, min(max_replicas, dem + buffer))


def _seconds(v, default: int) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", str(v or ""))
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)] if m else default


def reconcile_buffers(workers: dict, now: float | None = None) -> dict:
    """One pass over every worker; returns {cluster: {revision: new floor}} for the log and the tests."""
    now = now or time.time()
    changed: dict = {}
    for cname, w in workers.items():
        try:
            changed[cname] = reconcile_worker(w["custom"], w["core"], now)
        except Exception as e:   # one worker's trouble never stops the others
            log.warning("buffer: %s: %s", cname, e)
    return changed


def reconcile_worker(custom, core, now: float) -> dict:
    isvcs = custom.list_namespaced_custom_object("serving.kserve.io", "v1beta1", NAMESPACE, "inferenceservices").get("items", [])
    by_name = {i["metadata"]["name"]: i for i in isvcs}
    buffers = model_buffers(core)
    buffered = {n: i for n, i in by_name.items() if buffers.get(n, 0) > 0}
    revisions = custom.list_namespaced_custom_object("serving.knative.dev", "v1", NAMESPACE, "revisions").get("items", [])
    metrics, changed = None, {}
    for rev in revisions:
        md = rev["metadata"]
        labels, ann = md.get("labels") or {}, md.get("annotations") or {}
        model = labels.get(f"{LABEL}/model") or labels.get("serving.kserve.io/inferenceservice")
        isvc, ours = buffered.get(model), RAISED_ANN in ann
        if isvc is None and not ours:
            continue                                        # no buffer on this model, nothing of ours to undo
        active = labels.get("serving.knative.dev/routingState") == "active"
        pred = ((by_name.get(model) or {}).get("spec") or {}).get("predictor") or {}
        base_min, max_replicas = int(pred.get("minReplicas") or 0), int(pred.get("maxReplicas") or 1)
        if isvc is not None and active:
            if metrics is None:
                metrics = parse_metrics(fetch_metrics(core))
            buffer = buffers[model]
            target = floor_for(demand(metrics.get(md["name"], {})), base_min, max_replicas, buffer)
        else:
            target = base_min                               # buffer removed, or a revision no longer routed
        current = int(ann.get(MIN_ANN) or base_min)
        if target == current:
            continue
        cooldown = max(60, _seconds(((by_name.get(model) or {}).get("metadata") or {}).get("annotations", {}).get(COOLDOWN_ANN), 120))
        if target < current and ours and now - float(ann.get(RAISED_ANN) or 0) < cooldown:
            continue                                        # lowering waits for the model's cooldown
        patch = {MIN_ANN: str(target), RAISED_ANN: f"{now:.0f}" if target > base_min else None}
        custom.patch_namespaced_custom_object("serving.knative.dev", "v1", NAMESPACE, "revisions", md["name"],
                                              {"metadata": {"annotations": patch}})
        log.info("buffer: %s/%s min-scale %s -> %s (model %s)", NAMESPACE, md["name"], current, target, model)
        changed[md["name"]] = target
    return changed


def model_buffers(core) -> dict:
    """{model id: scaling.buffer} from the API's model copies in this cluster (`spec.json` of each catalog ConfigMap)."""
    out = {}
    for cm in core.list_namespaced_config_map(CATALOG_NS, label_selector=CATALOG_SELECTOR).items:
        try:
            spec = json.loads((cm.data or {}).get("spec.json") or "{}")
            mid = spec.get("id") or (cm.metadata.labels or {}).get(f"{LABEL}/model")
            if mid:
                out[mid] = int((spec.get("scaling") or {}).get("buffer") or 0)
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def fetch_metrics(core) -> str:
    """The autoscaler's /metrics through the API server's service proxy (RBAC: services/proxy on knative-serving)."""
    resp = core.api_client.call_api(METRICS_PATH, "GET", auth_settings=["BearerToken"],
                                    _return_http_data_only=True, _preload_content=False)
    data = getattr(resp, "data", None)
    return (data.decode() if isinstance(data, bytes) else str(data)) if data is not None else str(resp)
