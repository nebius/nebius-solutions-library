"""The Kubernetes objects of an endpoint, rendered natively: a catalog entry (services/api/models.py `to_entry`,
catalog/README.md), the cluster id, the gateway domain of its region, the InfiniBand claim-template prefix and
one variant in, the objects out:

    InferenceService (one pod per replica, KServe on Knative)        nodes <= 1
    LeaderWorkerSet + Service + HTTPRoute (N whole nodes per replica)  nodes > 1
    SecurityPolicy (the gateway's authorization of the endpoint's routes)
    PersistentVolumeClaim per `persistentVolumes`, LocalModelCache when enabled

services/api/tests/test_render_endpoint_golden.py holds the objects rendered for every fixture. Value semantics:
the defaults under the entry (DEFAULTS, every setting with its default), the entry's `runtime` merged over them and
`deployments.<cluster>` over that (maps deep-merged, lists and scalars replaced).

Pure functions, no I/O: `render(entry, cluster, domain, ib_claim_template, variant, api_backend)`; `models.render`
calls it once per variant. `api_backend`: the gateway's ext-auth target is the Envoy Gateway `Backend` api/api
(the fleet's API over its public hostname, charts/platform/templates/edge) on a GPU region, the Service api/api
on the API's own cluster (the chart's `apiBackend` value).
"""

import copy

from fastapi import HTTPException

from config import IMAGES_HOST, LABEL

POOL_LABEL = f"{LABEL}/pool"
MODELS_NAMESPACE_DEFAULT = "models"

# Every setting with its default. The entry's top level, then `runtime`, then
# `deployments.<cluster>` override these (see `values`).
DEFAULTS: dict = {
    "name": "",
    "namespace": MODELS_NAMESPACE_DEFAULT,
    "labels": {},
    "annotations": {},
    "podMetadata": {},
    "automountServiceAccountToken": None,
    "image": "",
    "imagePullSecrets": [],
    "command": [],
    "args": [],
    "env": [],
    "envFrom": [],
    "port": 8080,
    "protocol": "http",
    "workingDir": "",
    "resources": {
        "gpu": 1,
        "cpu": "4",
        "memory": "16Gi",
        "cpuRequest": "",
        "memoryRequest": "",
        "ephemeralStorage": "",
    },
    "pool": "",
    "nodeSelector": {},
    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
    "scaling": {
        "minReplicas": 0,
        "maxReplicas": 1,
        "metric": "concurrency",
        "target": 1,
        "containerConcurrency": 0,
        "scaleToZeroRetention": "2m",
    },
    "timeout": 600,
    "priorityClassName": "",
    "queue": {"priorityClass": "customer-batch", "name": ""},
    "images": {},
    "shm": {"enabled": True, "size": "1Gi"},
    "volumes": [],
    "persistentVolumes": [],
    "readinessProbe": {},
    "livenessProbe": {},
    "securityContext": {},
    "containerSecurityContext": {},
    "localModelCache": {"enabled": False, "storageUri": "", "nodeGroups": [], "size": "50Gi"},
    "weights": {
        "sharedFilesystem": {
            "enabled": False,
            "claimName": "weights-shared",
            "path": "",
            "mountPath": "/weights",
            "readOnly": False,
            "prepare": True,
            "prepareImage": f"{IMAGES_HOST}/docker/library/busybox:1.36"
            if IMAGES_HOST
            else "docker.io/library/busybox:1.36",
        },
        "env": {},
    },
    "nodes": 1,
    "workerCommand": [],
    "interconnect": "none",
    "ibDevices": 0,
    "ibClaimTemplate": "ib",
    "idleSeconds": 0,
    "domain": "",
    "gatewayName": "serverless2-external",
    "gatewayNamespace": "envoy-gateway-system",
}
# entry fields that are not settings (the chart's `endpoint.values` omits them before merging `runtime`)
OMIT = (
    "runtime",
    "deployments",
    "cluster",
    "id",
    "task",
    "mode",
    "displayName",
    "description",
    "source",
    "protocol",
    "port",
    "endpoints",
    "gpu",
    "concurrency",
    "coldStartClass",
    "price",
    "scaleToZero",
    "parameters",
    "workflow_template",
)
# entry fields of `deployments.<cluster>` that are not settings either
NOT_SETTINGS = ("paused", "price_per_gpu_hour", "parameters")
# the order `helm template` printed the objects in (its install order: claims, then Services, then the custom
# kinds by name); `apply` follows it, so the Service exists before the route that references it
KIND_ORDER = {"PersistentVolumeClaim": 0, "Service": 1}


def _fail(what: str, obj: str) -> HTTPException:
    return HTTPException(400, f"endpoint ({obj}): {what}")


def _required(value, what: str, obj: str):
    if value in (None, "", [], {}):
        raise _fail(what, obj)
    return value


def _text(value) -> str | None:
    """A value rendered as a string the way the chart quoted it; None stays None (an absent value)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coalesce(user: dict, defaults: dict) -> dict:
    """Values file over values.yaml, the way Helm reads them: the user's value wins, maps merge recursively, a
    user value of null removes the setting."""
    out = dict(user)
    for key, default in defaults.items():
        if key not in out:
            out[key] = copy.deepcopy(default)
        elif out[key] is None:
            del out[key]
        elif isinstance(default, dict) and isinstance(out[key], dict):
            out[key] = _coalesce(out[key], default)
    return out


def _merge_overwrite(dst: dict, src: dict) -> dict:
    """`runtime` over the base and `deployments.<cluster>` over that: maps deep-merge, everything else (scalars,
    lists, also empty and null ones) replaces."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_overwrite(dst[key], value)
        else:
            dst[key] = value
    return dst


def variant_entry(entry: dict, cluster: str, variant: dict) -> dict:
    """The entry for one variant: the cluster's deployment carries the variant's pool, image and replica bounds
    (they override `runtime` in `values`)."""
    e = dict(entry)
    over = dict((entry.get("deployments") or {}).get(cluster) or {})
    over.pop("variants", None)
    over["pool"] = variant.get("pool") or over.get("pool")
    if variant.get("image"):
        over["image"] = variant["image"]
    scaling = {}
    if variant.get("min") is not None:
        scaling["minReplicas"] = int(variant["min"])
    if variant.get("max") is not None:
        scaling["maxReplicas"] = int(variant["max"])
    if scaling:
        over["scaling"] = scaling
    e["deployments"] = {**(entry.get("deployments") or {}), cluster: over}
    return e


def values(entry: dict, cluster: str, domain: str, ib_claim_template: str, variant: dict | None = None) -> dict:
    """The effective settings of one variant of an endpoint on a cluster: DEFAULTS under the entry's top level
    (minus OMIT), `runtime` over that, `deployments.<cluster>` over that; the object name (the model id, or
    `<id>-<variant>` for every variant but the first), the model id, the namespace and the labels
    serverless2.nebius/task|mode|cluster."""
    v = variant or {"name": "primary", "object": entry.get("id"), "pool": None, "min": None, "max": None, "image": None}
    obj = v.get("object") or entry.get("id") or "?"
    e = variant_entry(entry, cluster, v)
    runtime = e.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise _fail("the entry has no runtime (a run class has no endpoint)", obj)
    top = _coalesce(e, DEFAULTS)
    top.update({"cluster": cluster, "domain": domain, "ibClaimTemplate": ib_claim_template})
    over = copy.deepcopy((top.get("deployments") or {}).get(cluster) or {})
    for key in NOT_SETTINGS:
        over.pop(key, None)
    base = {k: copy.deepcopy(x) for k, x in top.items() if k not in OMIT}
    vals = _merge_overwrite(_merge_overwrite(base, copy.deepcopy(runtime)), over)
    mid = _required(top.get("id"), "catalog entry needs id", obj)
    vals["name"] = obj if obj != mid else mid
    vals["modelId"] = mid
    vals["variant"] = v.get("name") if obj != mid else ""
    vals["namespace"] = top.get("namespace") or MODELS_NAMESPACE_DEFAULT
    labels = dict(vals.get("labels") or {})
    labels.update(
        {f"{LABEL}/task": top.get("task") or "", f"{LABEL}/mode": top.get("mode") or "", f"{LABEL}/cluster": cluster}
    )
    vals["labels"] = labels
    return vals


# ---- pieces shared by the InferenceService and the LeaderWorkerSet ---------------------------------------


def _labels(v: dict) -> dict:
    """Metadata labels of every object: the standard Helm set plus the serverless2 labels the API and the
    dispatcher select on, the Kueue priority class, and the entry's own labels (which win)."""
    out = {
        "app.kubernetes.io/name": v["name"],
        "app.kubernetes.io/instance": v["name"],
        "app.kubernetes.io/part-of": "serverless2",
        f"{LABEL}/model": v.get("modelId") or v["name"],
    }
    if v.get("variant"):
        out[f"{LABEL}/variant"] = v["variant"]
    out[f"{LABEL}/protocol"] = v.get("protocol")
    if (v.get("queue") or {}).get("priorityClass"):
        out["kueue.x-k8s.io/priority-class"] = v["queue"]["priorityClass"]
    out.update(v.get("labels") or {})
    return out


def _shared_fs(v: dict) -> dict:
    return (v.get("weights") or {}).get("sharedFilesystem") or {}


def _resources(v: dict) -> dict:
    r = v.get("resources") or {}
    limits = {"nvidia.com/gpu": _text(r.get("gpu")), "cpu": _text(r.get("cpu")), "memory": r.get("memory")}
    requests = {
        "nvidia.com/gpu": _text(r.get("gpu")),
        "cpu": _text(r.get("cpuRequest") or r.get("cpu")),
        "memory": r.get("memoryRequest") or r.get("memory"),
    }
    if r.get("ephemeralStorage"):
        limits["ephemeral-storage"] = requests["ephemeral-storage"] = r["ephemeralStorage"]
    return {"limits": limits, "requests": requests}


def _probes(v: dict) -> dict:
    out = {}
    if p := v.get("readinessProbe"):
        probe = {"httpGet": {"path": p.get("path"), "port": p.get("port") or v.get("port")}}
        for key in ("periodSeconds", "failureThreshold"):
            if p.get(key):
                probe[key] = p[key]
        out["readinessProbe"] = probe
    if p := v.get("livenessProbe"):
        probe = {
            "httpGet": {"path": p.get("path"), "port": p.get("port") or v.get("port")},
            "periodSeconds": p.get("periodSeconds") or 15,
            "failureThreshold": p.get("failureThreshold") or 4,
        }
        if p.get("initialDelaySeconds"):
            probe["initialDelaySeconds"] = p["initialDelaySeconds"]
        out["livenessProbe"] = probe
    return out


def _volume_mounts(v: dict) -> list[dict]:
    w = _shared_fs(v)
    out = []
    if (v.get("shm") or {}).get("enabled"):
        out.append({"name": "shm", "mountPath": "/dev/shm"})
    if w.get("enabled"):
        out.append(
            {
                "name": "weights",
                "mountPath": w.get("mountPath") or "/weights",
                "subPath": _text(w.get("path") or v["name"]),
                "readOnly": bool(w.get("readOnly")),
            }
        )
    for x in (v.get("volumes") or []) + (v.get("persistentVolumes") or []):
        out.append({"name": x.get("name"), "mountPath": x.get("mountPath")})
    return out


def _volumes(v: dict) -> list[dict]:
    w = _shared_fs(v)
    out = []
    if (v.get("shm") or {}).get("enabled"):
        out.append({"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": v["shm"].get("size")}})
    if w.get("enabled"):
        # the region's shared filesystem (Terraform weights_filesystem) through the cluster's RWX claim; subPath <path>
        out.append(
            {"name": "weights", "persistentVolumeClaim": {"claimName": _text(w.get("claimName") or "weights-shared")}}
        )
    for x in v.get("volumes") or []:
        empty = {"sizeLimit": x.get("sizeLimit")}
        if x.get("medium"):
            empty["medium"] = x["medium"]
        out.append({"name": x.get("name"), "emptyDir": empty})
    for x in v.get("persistentVolumes") or []:
        out.append({"name": x.get("name"), "persistentVolumeClaim": {"claimName": f"{v['name']}-{x.get('name')}"}})
    return out


def _weights_prepare(v: dict) -> list[dict]:
    """The init container that creates the model's directory on the shared filesystem (subPath directories are
    created root-owned otherwise)."""
    w = _shared_fs(v)
    if not (w.get("enabled") and w.get("prepare")):
        return []
    path = w.get("path") or v["name"]
    return [
        {
            "name": "weights-prepare",
            "image": w.get("prepareImage"),
            "command": ["sh", "-c", f"mkdir -p /weights-root/{path} && chmod 1777 /weights-root/{path}"],
            "securityContext": {"runAsUser": 0},
            "volumeMounts": [{"name": "weights", "mountPath": "/weights-root"}],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
        }
    ]


def _weights_env(v: dict) -> list[dict]:
    """`weights.env` as EnvVars, by name (the chart ranged over the map, which sorts its keys)."""
    env = (v.get("weights") or {}).get("env") or {}
    return [{"name": _text(k), "value": _text(val)} for k, val in sorted(env.items())]


def _pod_labels_annotations(v: dict, extra: dict | None = None) -> tuple[dict, dict]:
    pm = v.get("podMetadata") or {}
    labels = dict(pm.get("labels") or {})
    labels.update(extra or {})
    return labels, dict(pm.get("annotations") or {})


# ---- the objects -------------------------------------------------------------------------------------------


def _inferenceservice(v: dict) -> dict:
    sc = v.get("scaling") or {}
    queue = v.get("queue") or {}
    pm = v.get("podMetadata") or {}
    pred: dict = {
        "minReplicas": sc.get("minReplicas"),
        "maxReplicas": sc.get("maxReplicas"),
        "scaleMetric": sc.get("metric"),
        "scaleTarget": sc.get("target"),
    }
    if sc.get("containerConcurrency"):
        pred["containerConcurrency"] = sc["containerConcurrency"]
    pred["timeout"] = v.get("timeout")
    # pod labels (KServe copies them to the Knative revision and its pods): the administrator's pod metadata,
    # the Kueue queue and priority class
    if queue.get("name") or queue.get("priorityClass") or pm.get("labels"):
        labels = dict(pm.get("labels") or {})
        if queue.get("name"):
            labels["kueue.x-k8s.io/queue-name"] = queue["name"]
        if queue.get("priorityClass"):
            labels["kueue.x-k8s.io/priority-class"] = queue["priorityClass"]
        pred["labels"] = labels
    if pm.get("annotations"):
        pred["annotations"] = dict(pm["annotations"])
    if v.get("automountServiceAccountToken") is not None:
        pred["automountServiceAccountToken"] = v["automountServiceAccountToken"]
    if v.get("priorityClassName"):
        pred["priorityClassName"] = v["priorityClassName"]
    if v.get("imagePullSecrets"):
        pred["imagePullSecrets"] = [{"name": s} for s in v["imagePullSecrets"]]
    # Kueue placement (queue.name) injects the pool's nodeSelector after admission; a pinned endpoint names its pool
    if not queue.get("name") or v.get("nodeSelector"):
        selector = {}
        if not queue.get("name"):
            selector[POOL_LABEL] = _required(
                v.get("pool"), "pool is required (or queue.name for Kueue placement)", v["name"]
            )
        selector.update(v.get("nodeSelector") or {})
        pred["nodeSelector"] = selector
    if v.get("tolerations"):
        pred["tolerations"] = list(v["tolerations"])
    if v.get("securityContext"):
        pred["securityContext"] = dict(v["securityContext"])
    if init := _weights_prepare(v):
        pred["initContainers"] = init
    # per-pool image: when the endpoint is pinned to a pool, `images.<pool>` overrides `image` (docs/SCHEDULING.md)
    images = v.get("images") or {}
    if not queue.get("name") and v.get("pool") and v["pool"] in images:
        image = images[v["pool"]]
    else:
        image = _required(v.get("image"), "image is required", v["name"])
    c: dict = {"name": "kserve-container", "image": image}
    if v.get("command"):
        c["command"] = list(v["command"])
    if v.get("args"):
        c["args"] = list(v["args"])
    if v.get("workingDir"):
        c["workingDir"] = v["workingDir"]
    wenv = _weights_env(v)
    if v.get("env") or wenv:
        c["env"] = list(v.get("env") or []) + wenv
    if v.get("envFrom"):
        c["envFrom"] = list(v["envFrom"])
    c["ports"] = [{"containerPort": v.get("port"), "protocol": "TCP"}]
    c["resources"] = _resources(v)
    c.update(_probes(v))
    if v.get("containerSecurityContext"):
        c["securityContext"] = dict(v["containerSecurityContext"])
    if mounts := _volume_mounts(v):
        c["volumeMounts"] = mounts
    pred["containers"] = [c]
    if vols := _volumes(v):
        pred["volumes"] = vols
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": _required(v.get("name"), "name is required", v.get("name") or "?"),
            "namespace": v["namespace"],
            "labels": _labels(v),
            "annotations": {
                "serving.kserve.io/deploymentMode": "Knative",
                "autoscaling.knative.dev/scale-to-zero-pod-retention-period": _text(sc.get("scaleToZeroRetention")),
                **(v.get("annotations") or {}),
            },
        },
        "spec": {"predictor": pred},
    }


def _lws_container(v: dict, nodes: int, ib: int, leader: bool, command) -> dict:
    r = v.get("resources") or {}
    c: dict = {"name": "endpoint", "image": _required(v.get("image"), "image is required", v["name"])}
    if command:
        c["command"] = list(command)
    if v.get("args"):
        c["args"] = list(v["args"])
    if v.get("workingDir"):
        c["workingDir"] = v["workingDir"]
    env = [
        {
            "name": "NODE_RANK",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['leaderworkerset.sigs.k8s.io/worker-index']"}},
        },
        {"name": "NNODES", "value": str(nodes)},
        {"name": "GPUS_PER_NODE", "value": _text(r.get("gpu"))},
        {"name": "WORLD_SIZE", "value": str(nodes * int(r.get("gpu") or 0))},
        # the leader pod through the LeaderWorkerSet's headless Service
        {"name": "MASTER_ADDR", "value": f"{v['name']}-0.{v['name']}"},
        {"name": "MASTER_PORT", "value": "29500"},
    ]
    if ib > 0:
        env += [
            {"name": "NCCL_IB_HCA", "value": "mlx5"},
            {"name": "NCCL_SOCKET_IFNAME", "value": "eth0"},
            {"name": "UCX_NET_DEVICES", "value": "eth0"},
        ]
    else:
        # Ethernet between the nodes: NCCL must not try the InfiniBand transport (the nodes carry Mellanox NICs, the
        # pod holds none: "unhandled system error" at communicator setup on B300)
        env += [{"name": "NCCL_SOCKET_IFNAME", "value": "eth0"}, {"name": "NCCL_IB_DISABLE", "value": "1"}]
    # the model's own entries win over the platform defaults (one entry per name: server-side apply refuses duplicates)
    own = list(v.get("env") or []) + _weights_env(v)
    taken = {e.get("name") for e in own}
    c["env"] = [e for e in env if e.get("name") not in taken] + own
    if v.get("envFrom"):
        c["envFrom"] = list(v["envFrom"])
    c["ports"] = [{"containerPort": v.get("port"), "protocol": "TCP"}]
    c["resources"] = _resources(v)
    if ib > 0:
        c["resources"]["claims"] = [{"name": "ib"}]
    if leader:
        c.update(_probes(v))  # a hung leader is restarted; RecreateGroupOnPodRestart recreates the group
    if v.get("containerSecurityContext"):
        c["securityContext"] = dict(v["containerSecurityContext"])
    if mounts := _volume_mounts(v):
        c["volumeMounts"] = mounts
    return c


def _lws_podspec(v: dict, nodes: int, ib: int, leader: bool, command) -> dict:
    spec: dict = {}
    if v.get("automountServiceAccountToken") is not None:
        spec["automountServiceAccountToken"] = v["automountServiceAccountToken"]
    if v.get("priorityClassName"):
        spec["priorityClassName"] = v["priorityClassName"]
    if v.get("imagePullSecrets"):
        spec["imagePullSecrets"] = [{"name": s} for s in v["imagePullSecrets"]]
    spec["nodeSelector"] = {
        POOL_LABEL: _required(v.get("pool"), "pool is required", v["name"]),
        **(v.get("nodeSelector") or {}),
    }
    if v.get("tolerations"):
        spec["tolerations"] = list(v["tolerations"])
    if v.get("securityContext"):
        spec["securityContext"] = dict(v["securityContext"])
    if ib > 0:
        # DRA claim of every fabric NIC of the node (ResourceClaimTemplate <prefix>-<n>, docs/JOBS.md)
        spec["resourceClaims"] = [
            {"name": "ib", "resourceClaimTemplateName": f"{v.get('ibClaimTemplate') or 'ib'}-{ib}"}
        ]
    if init := _weights_prepare(v):
        spec["initContainers"] = init
    spec["containers"] = [_lws_container(v, nodes, ib, leader, command)]
    if vols := _volumes(v):
        spec["volumes"] = vols
    return spec


def _leaderworkerset(v: dict, nodes: int) -> list[dict]:
    """One replica group of `nodes` pods, the leader serves; a Service and an HTTPRoute named like a KServe
    predictor keep the SecurityPolicy, the certificate and the LiteLLM entry the same as for a one-pod endpoint."""
    ib = int(v.get("ibDevices") or 0)
    sc = v.get("scaling") or {}
    name, ns = _required(v.get("name"), "name is required", v.get("name") or "?"), v["namespace"]
    labels = _labels(v)
    pod_labels, pod_annotations = _pod_labels_annotations(
        v,
        {
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/part-of": "serverless2",
            f"{LABEL}/model": name,
            f"{LABEL}/protocol": v.get("protocol") or "http",
        },
    )

    def template(role: str, leader: bool, command) -> dict:
        md = {"labels": {**pod_labels, f"{LABEL}/role": role}}
        if pod_annotations:
            md["annotations"] = pod_annotations
        return {"metadata": md, "spec": _lws_podspec(v, nodes, ib, leader, command)}

    lws = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": labels,
            "annotations": {
                f"{LABEL}/nodes": str(nodes),
                f"{LABEL}/interconnect": _text(v.get("interconnect") or "none"),
                f"{LABEL}/idle-s": _text(v.get("idleSeconds") or 0),
                **(v.get("annotations") or {}),
            },
        },
        "spec": {
            "replicas": sc.get("minReplicas"),
            "startupPolicy": "LeaderCreated",
            "leaderWorkerTemplate": {
                "size": nodes,
                "restartPolicy": "RecreateGroupOnPodRestart",
                "leaderTemplate": template("leader", True, v.get("command")),
                "workerTemplate": template("worker", False, v.get("workerCommand") or v.get("command")),
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{name}-predictor", "namespace": ns, "labels": labels},
        "spec": {
            "type": "ClusterIP",
            "selector": {"leaderworkerset.sigs.k8s.io/name": name, "leaderworkerset.sigs.k8s.io/worker-index": "0"},
            "ports": [{"name": "http", "port": 80, "targetPort": v.get("port"), "protocol": "TCP"}],
        },
    }
    domain = _required(v.get("domain"), "domain is required for a multi-node endpoint", name)
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": f"{name}-predictor",
            "namespace": ns,
            "labels": {**labels, "serving.knative.dev/route": f"{name}-predictor"},
        },
        "spec": {
            "parentRefs": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "Gateway",
                    "name": v.get("gatewayName"),
                    "namespace": v.get("gatewayNamespace"),
                }
            ],
            "hostnames": [f"{name}-predictor.{ns}.{domain}"],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"group": "", "kind": "Service", "name": f"{name}-predictor", "port": 80}],
                    "timeouts": {"request": "0s"},
                }
            ],
        },
    }
    return [lws, service, route]


def _securitypolicy(v: dict, api_backend: bool = False) -> dict:
    """The gateway's authorization of the endpoint's external routes through the API's authorizer."""
    name = v["name"]
    api = (
        {"group": "gateway.envoyproxy.io", "kind": "Backend", "name": "api", "namespace": "api", "port": 443}
        if api_backend
        else {"group": "", "kind": "Service", "name": "api", "namespace": "api", "port": 80}
    )
    return {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "SecurityPolicy",
        "metadata": {"name": f"{name}-authorization", "namespace": v["namespace"], "labels": _labels(v)},
        "spec": {
            "targetSelectors": [
                {
                    "group": "gateway.networking.k8s.io",
                    "kind": "HTTPRoute",
                    "matchLabels": {"serving.knative.dev/route": f"{name}-predictor"},
                    "matchExpressions": [
                        {"key": "networking.knative.dev/visibility", "operator": "NotIn", "values": ["cluster-local"]}
                    ],
                }
            ],
            "extAuth": {
                "failOpen": False,
                "timeout": "10s",
                "headersToExtAuth": [
                    "authorization",
                    "sec-websocket-protocol",
                    "x-serverless2-admitted",
                    "x-serverless2-operation",  # an endpoint call's caller names its operation and attempt: the
                    "x-serverless2-attempt",  # admission token is verified against them (docs/API.md "Endpoint calls")
                    "x-serverless2-session-claim",  # a live session's claim (docs/API.md "Sessions")
                    "x-serverless2-session",  # a client must not supply these: the hook refuses them
                    "x-serverless2-connection",
                ],
                "http": {
                    "path": f"/internal/authorize/{v.get('modelId') or name}",
                    "backendRefs": [api],
                    "headersToBackend": [
                        "x-serverless2-tenant",
                        "x-serverless2-key-alias",
                        "x-serverless2-session",  # set by the hook on a bound session (overrides anything a client sent)
                        "x-serverless2-connection",
                    ],
                },
            },
        },
    }


def _localmodelcache(v: dict) -> list[dict]:
    lmc = v.get("localModelCache") or {}
    if not lmc.get("enabled"):
        return []
    return [
        {
            "apiVersion": "serving.kserve.io/v1alpha1",
            "kind": "LocalModelCache",
            "metadata": {"name": v["name"], "labels": _labels(v)},  # cluster-scoped: no namespace
            "spec": {
                "sourceModelUri": _required(lmc.get("storageUri"), "localModelCache.storageUri", v["name"]),
                "modelSize": lmc.get("size"),
                "nodeGroups": lmc.get("nodeGroups"),
            },
        }
    ]


def _pvcs(v: dict) -> list[dict]:
    out = []
    for p in v.get("persistentVolumes") or []:
        spec: dict = {"accessModes": ["ReadWriteOnce"]}
        if p.get("storageClass"):
            spec["storageClassName"] = p["storageClass"]
        spec["resources"] = {"requests": {"storage": p.get("size")}}
        out.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": f"{v['name']}-{p.get('name')}", "namespace": v["namespace"], "labels": _labels(v)},
                "spec": spec,
            }
        )
    return out


def render(
    entry: dict,
    cluster: str,
    domain: str,
    ib_claim_template: str,
    variant: dict | None = None,
    api_backend: bool = False,
) -> list[dict]:
    """The objects of one variant of an endpoint on a cluster (`variant`: an element of models.variants, the first
    variant keeps the model's id as the object name; `api_backend`: the cluster is a GPU region, not the API's own)."""
    v = values(entry, cluster, domain, ib_claim_template, variant)
    nodes = int(v.get("nodes") or 1)
    docs = [_securitypolicy(v, api_backend)]
    docs += [_inferenceservice(v)] if nodes <= 1 else _leaderworkerset(v, nodes)
    docs += _localmodelcache(v) + _pvcs(v)
    return sorted(
        docs,
        key=lambda d: (KIND_ORDER.get(d["kind"], len(KIND_ORDER)), d["kind"] if d["kind"] not in KIND_ORDER else ""),
    )
