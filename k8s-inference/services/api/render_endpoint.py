"""The Kubernetes objects of an endpoint, rendered natively: a catalog entry (services/api/models.py `to_entry`,
catalog/README.md), the cluster id, the gateway domain of its region, the InfiniBand claim-template prefix and
one variant in, the objects out:

    InferenceService (one pod per replica, KServe on Knative)        nodes <= 1
    LeaderWorkerSet + Service + HTTPRoute (N whole nodes per replica)  nodes > 1
    SecurityPolicy (the gateway's authorization of the endpoint's routes)
    ConfigMap (the fleet's trust bundle, `nativeLease`), CiliumNetworkPolicy (`ingressIsolation`)

services/api/tests/test_render_endpoint_golden.py holds the objects rendered for every case. Value semantics: the
entry's `runtime` over DEFAULTS, `deployments.<cluster>` (the variant's pool, image and replica bounds) over that;
maps deep-merge, lists and scalars replace.

Pure functions, no I/O: `render(entry, cluster, domain, ib_claim_template, variant, api_backend)`; `models.render`
calls it once per variant. `api_backend`: the gateway's ext-auth target is the Envoy Gateway `Backend` api/api
(the fleet's API over its public hostname, charts/platform/templates/edge) on a GPU region, the Service api/api
on the API's own cluster (the chart's `apiBackend` value).
"""

import copy

from fastapi import HTTPException

from config import IMAGES_HOST, LABEL, PUBLIC_API_URL, SESSION_TOKEN_AUDIENCE

# Lease clients (docs/API.md "Native execution leases", "Lease clients"): what an endpoint pod that attaches to its
# calls' native executions gets from the platform: a projected ServiceAccount token bound to the pod (audience
# LEASE_AUDIENCE, the one the API's TokenReview expects), the control API's URL, and the files' paths as env.
LEASE_DIR = "/var/run/secrets/serverless2"
LEASE_AUDIENCE = SESSION_TOKEN_AUDIENCE or "serverless2"
# the fleet's trust bundle (jobs.trust_bundle: the API's own merged roots, public material only), on a fleet whose control
# hostnames are not publicly signed: projected as a FILE next to the token (a ConfigMap of the endpoint, one per
# object name), never as an environment value (a full bundle is hundreds of KB: above the kernel's single-argument
# bound, the container never execs). The standard OpenSSL seam (SSL_CERT_FILE) is what the pinned shared client
# (services/lease-client/s2lease.py: ssl.create_default_context) and every SDK in the pod honour without a rebuild;
# S2_CA_FILE names the same file for a client that wants it explicitly.
TRUST_KEY = "ca-bundle.pem"
TRUST_FILE = f"{LEASE_DIR}/{TRUST_KEY}"
TRUST_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "AWS_CA_BUNDLE", "S2_CA_FILE")
TRUST_MAX_BYTES = 1_000_000  # a ConfigMap holds 1 MiB; the bundle is projected whole or the render refuses, never cut

POOL_LABEL = f"{LABEL}/pool"
PRIORITY_CLASS = "customer-batch"  # the Kueue priority class of every endpoint object and pod
PREPARE_IMAGE = f"{IMAGES_HOST}/docker/library/busybox:1.36" if IMAGES_HOST else "docker.io/library/busybox:1.36"
# what `runtime` may leave out (models.to_entry sets everything else explicitly): the GPU toleration (replaced by the
# CPU pool's in `values`), Knative's scale-to-zero retention, the weights directory prepared unless said otherwise
DEFAULTS: dict = {
    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
    "scaling": {"scaleToZeroRetention": "2m"},
    "weights": {"sharedFilesystem": {"prepare": True}},
}
# the apply order (the pod's mounts and its Service exist before the pod, the route before the policy on it)
KINDS = (
    "ConfigMap",
    "Service",
    "CiliumNetworkPolicy",
    "HTTPRoute",
    "InferenceService",
    "LeaderWorkerSet",
    "SecurityPolicy",
)


def _fail(what: str, obj: str) -> HTTPException:
    return HTTPException(400, f"endpoint ({obj}): {what}")


def _required(value, what: str, obj: str):
    if value in (None, "", [], {}):
        raise _fail(what, obj)
    return value


def _text(value) -> str | None:
    """A value as the string Kubernetes expects; None stays None (an absent value)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return None if value is None else str(value)


def _merge_overwrite(dst: dict, src: dict) -> dict:
    """Maps deep-merge, everything else (scalars, lists, also empty and null ones) replaces."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_overwrite(dst[key], value)
        else:
            dst[key] = value
    return dst


def values(entry: dict, cluster: str, domain: str, ib_claim_template: str, variant: dict | None = None) -> dict:
    """The effective settings of one variant of an endpoint on a cluster: `runtime` over DEFAULTS, the cluster's
    deployment with the variant's pool, image and replica bounds over that; the object name (the model id, or
    `<id>-<variant>` for every variant but the first), the model id, the namespace and the labels
    serverless2.nebius/task|mode|cluster."""
    v = variant or {"name": "primary", "object": entry.get("id"), "pool": None, "min": None, "max": None, "image": None}
    obj = v.get("object") or entry.get("id") or "?"
    runtime = entry.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        raise _fail("the entry has no runtime (a run class has no endpoint)", obj)
    over = dict((entry.get("deployments") or {}).get(cluster) or {})
    over["pool"] = v.get("pool") or over.get("pool")
    if v.get("image"):
        over["image"] = v["image"]
    if scaling := {k: int(v[s]) for s, k in (("min", "minReplicas"), ("max", "maxReplicas")) if v.get(s) is not None}:
        over["scaling"] = scaling
    base = {
        **DEFAULTS,
        "podMetadata": entry.get("podMetadata") or {},
        "domain": domain,
        "ibClaimTemplate": ib_claim_template,
    }
    vals = _merge_overwrite(_merge_overwrite(copy.deepcopy(base), copy.deepcopy(runtime)), copy.deepcopy(over))
    if vals.get("pool"):
        # a deployment pinned to `system` (a GPU-free endpoint) serves from the cluster's customer CPU pool instead
        # when the fleet keeps tenants off the system nodes (kube.tenant_pool, docs/FLEET.md "Customer CPU pools");
        # that pool is tainted, so its toleration replaces the GPU one
        import kube

        vals["pool"] = pool = kube.tenant_pool(cluster, vals["pool"])
        if kube.is_cpu_pool(cluster, pool):
            vals["tolerations"] = [t for t in (vals.get("tolerations") or []) if t.get("key") != "nvidia.com/gpu"] + [
                dict(kube.CPU_POOL_TOLERATION)
            ]
    mid = _required(entry.get("id"), "catalog entry needs id", obj)
    vals.update(
        {
            "name": obj,
            "modelId": mid,
            "variant": v.get("name") if obj != mid else "",
            "namespace": entry.get("namespace") or "models",
            "labels": {
                f"{LABEL}/task": entry.get("task") or "",
                f"{LABEL}/mode": entry.get("mode") or "",
                f"{LABEL}/cluster": cluster,
            },
        }
    )
    return vals


# ---- pieces shared by the InferenceService and the LeaderWorkerSet ---------------------------------------


def _labels(v: dict) -> dict:
    """Metadata labels of every object: the standard Helm set plus the serverless2 labels the API and the
    dispatcher select on, and the Kueue priority class."""
    out = {
        "app.kubernetes.io/name": v["name"],
        "app.kubernetes.io/instance": v["name"],
        "app.kubernetes.io/part-of": "serverless2",
        f"{LABEL}/model": v.get("modelId") or v["name"],
    }
    if v.get("variant"):
        out[f"{LABEL}/variant"] = v["variant"]
    out[f"{LABEL}/protocol"] = v.get("protocol")
    out["kueue.x-k8s.io/priority-class"] = PRIORITY_CLASS
    out.update(v.get("labels") or {})
    return out


def _shared_fs(v: dict) -> dict:
    return (v.get("weights") or {}).get("sharedFilesystem") or {}


def _resources(v: dict) -> dict:
    """`cpu`/`memory` are the requests, `cpuLimit`/`memoryLimit` the limits above them (docs/API.md); requests = limits
    unless both are given, the same for the kubelet's node-local scratch (`ephemeralStorage[Limit]`, as a run class's)."""
    r = v.get("resources") or {}
    gpu = _text(r.get("gpu"))
    limits = {
        "nvidia.com/gpu": gpu,
        "cpu": _text(r.get("cpuLimit") or r.get("cpu")),
        "memory": r.get("memoryLimit") or r.get("memory"),
    }
    requests = {"nvidia.com/gpu": gpu, "cpu": _text(r.get("cpu")), "memory": r.get("memory")}
    if scratch := r.get("ephemeralStorage") or r.get("ephemeralStorageLimit"):
        requests["ephemeral-storage"] = scratch
        limits["ephemeral-storage"] = r.get("ephemeralStorageLimit") or scratch
    return {"limits": limits, "requests": requests}


def _probes(v: dict) -> dict:
    p = v.get("readinessProbe")
    return {"readinessProbe": {"httpGet": {"path": p.get("path"), "port": p.get("port") or v.get("port")}}} if p else {}


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
    out += [{"name": x.get("name"), "mountPath": x.get("mountPath")} for x in v.get("volumes") or []]
    if v.get("nativeLease"):
        out.append({"name": "s2-lease", "mountPath": LEASE_DIR, "readOnly": True})
    return out


def _volumes(v: dict) -> list[dict]:
    out = []
    if (v.get("shm") or {}).get("enabled"):
        out.append({"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": v["shm"].get("size")}})
    if _shared_fs(v).get("enabled"):
        # the region's shared filesystem (Terraform weights_filesystem) through the cluster's RWX claim; subPath <path>
        out.append({"name": "weights", "persistentVolumeClaim": {"claimName": "weights-shared"}})
    for x in v.get("volumes") or []:
        empty = {"sizeLimit": x.get("sizeLimit")}
        if x.get("medium"):
            empty["medium"] = x["medium"]
        out.append({"name": x.get("name"), "emptyDir": empty})
    if v.get("nativeLease"):
        # the lease client's identity: a token bound to this pod (TokenReview reports the pod's name and UID), audience
        # LEASE_AUDIENCE, renewed by the kubelet; readable by the container as a file, never in the environment
        token = {"serviceAccountToken": {"path": "token", "audience": LEASE_AUDIENCE, "expirationSeconds": 3600}}
        out.append({"name": "s2-lease", "projected": {"sources": [token]}})
        if _trust(v):
            # the fleet's trust bundle as a file beside the token (TRUST_FILE), from the endpoint's own ConfigMap
            # (_trust_configmap, applied before the pod's object); required: a pod without its trust must not start
            out[-1]["projected"]["sources"].append(
                {"configMap": {"name": f"{v['name']}-trust", "items": [{"key": TRUST_KEY, "path": TRUST_KEY}]}}
            )
    return out


def _lease_env(v: dict) -> list[dict]:
    """The lease client's environment (docs/API.md "Lease clients"): the control API, the token file, the mode."""
    if not v.get("nativeLease"):
        return []
    out = [
        {"name": "S2_API_URL", "value": PUBLIC_API_URL},
        {"name": "S2_LEASE_TOKEN_FILE", "value": f"{LEASE_DIR}/token"},
        {"name": "S2_LEASE_MODE", "value": str(v["nativeLease"])},
        {"name": "S2_LEASE_AUDIENCE", "value": LEASE_AUDIENCE},
    ]
    if _trust(v):
        # the fleet's CA bundle as a file (TRUST_FILE): the standard variables the async caller gets as well
        # (jobs.py: OpenSSL/Python, requests, curl, the AWS SDKs) plus S2_CA_FILE; never the bundle itself as a value
        out += [{"name": k, "value": TRUST_FILE} for k in TRUST_ENV]
    return out


def _trust(v: dict) -> str | None:
    """The bundle to project for this render (None without `native_lease` or on a fleet with public certificates):
    read once per render (values() keeps it under `trustBundle`), the full file, never truncated."""
    if not v.get("nativeLease"):
        return None
    if "trustBundle" not in v:
        v["trustBundle"] = trust_bundle()
    pem = v["trustBundle"]
    if pem and len(pem.encode()) > TRUST_MAX_BYTES:
        raise HTTPException(
            500,
            f"endpoint ({v['name']}): the fleet trust bundle ({len(pem.encode())} bytes) exceeds the projected bound",
        )
    return pem or None


def _trust_configmap(v: dict) -> dict:
    """The endpoint's trust bundle object: public CA material only (jobs.trust_bundle), the whole file under
    TRUST_KEY; applied and deleted with the endpoint's other objects (models.apply / models._delete)."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{v['name']}-trust", "namespace": v["namespace"], "labels": _labels(v)},
        "data": {TRUST_KEY: _trust(v)},
    }


def trust_bundle() -> str | None:
    from jobs import trust_bundle as _bundle  # the async caller's projection (import here: jobs imports this module)

    return _bundle()


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
            "image": PREPARE_IMAGE,
            "command": ["sh", "-c", f"mkdir -p /weights-root/{path} && chmod 1777 /weights-root/{path}"],
            "securityContext": {"runAsUser": 0},
            "volumeMounts": [{"name": "weights", "mountPath": "/weights-root"}],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
        }
    ]


def _weights_env(v: dict) -> list[dict]:
    """`weights.env` as EnvVars, by name."""
    return [
        {"name": _text(k), "value": _text(x)} for k, x in sorted(((v.get("weights") or {}).get("env") or {}).items())
    ]


def _container(v: dict, name: str, command, env: list[dict]) -> dict:
    c: dict = {"name": name, "image": _required(v.get("image"), "image is required", v["name"])}
    if command:
        c["command"] = list(command)
    if v.get("args"):
        c["args"] = list(v["args"])
    if env:
        c["env"] = env
    if v.get("envFrom"):
        c["envFrom"] = list(v["envFrom"])
    c["ports"] = [{"containerPort": v.get("port"), "protocol": "TCP"}]
    c["resources"] = _resources(v)
    if mounts := _volume_mounts(v):
        c["volumeMounts"] = mounts
    return c


def _pod(v: dict, ib: int = 0) -> dict:
    """The pod spec without its containers: pinned to the pool, the GPU (or CPU pool) toleration, the weights init
    container, the volumes; `ib` > 0 claims every fabric NIC of the node (ResourceClaimTemplate <prefix>-<n>, docs/JOBS.md)."""
    spec: dict = {}
    if v.get("automountServiceAccountToken") is not None:
        spec["automountServiceAccountToken"] = v["automountServiceAccountToken"]
    if v.get("imagePullSecrets"):
        spec["imagePullSecrets"] = [{"name": s} for s in v["imagePullSecrets"]]
    spec["nodeSelector"] = {
        POOL_LABEL: _required(v.get("pool"), "pool is required (or queue.name for Kueue placement)", v["name"])
    }
    spec["tolerations"] = list(v["tolerations"])
    if ib > 0:
        spec["resourceClaims"] = [
            {"name": "ib", "resourceClaimTemplateName": f"{v.get('ibClaimTemplate') or 'ib'}-{ib}"}
        ]
    if init := _weights_prepare(v):
        spec["initContainers"] = init
    if vols := _volumes(v):
        spec["volumes"] = vols
    return spec


# ---- the objects -------------------------------------------------------------------------------------------


def _inferenceservice(v: dict) -> dict:
    sc = v.get("scaling") or {}
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
    # pod labels (KServe copies them to the Knative revision and its pods): the administrator's pod metadata and
    # the Kueue priority class
    pred["labels"] = {**(pm.get("labels") or {}), "kueue.x-k8s.io/priority-class": PRIORITY_CLASS}
    if pm.get("annotations"):
        pred["annotations"] = dict(pm["annotations"])
    pred.update(_pod(v))
    c = _container(v, "kserve-container", v.get("command"), list(v.get("env") or []) + _weights_env(v) + _lease_env(v))
    c.update(_probes(v))
    pred["containers"] = [c]
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
    c = _container(v, "endpoint", command, [e for e in env if e.get("name") not in taken] + own + _lease_env(v))
    if ib > 0:
        c["resources"]["claims"] = [{"name": "ib"}]
    if leader:
        c.update(_probes(v))  # a hung leader is restarted; RecreateGroupOnPodRestart recreates the group
    return c


def _leaderworkerset(v: dict, nodes: int) -> list[dict]:
    """One replica group of `nodes` pods, the leader serves; a Service and an HTTPRoute named like a KServe
    predictor keep the SecurityPolicy, the certificate and the LiteLLM entry the same as for a one-pod endpoint."""
    ib = int(v.get("ibDevices") or 0)
    sc = v.get("scaling") or {}
    name, ns = _required(v.get("name"), "name is required", v.get("name") or "?"), v["namespace"]
    _required(v.get("pool"), "pool is required", name)
    labels = _labels(v)
    pm = v.get("podMetadata") or {}
    pod_labels = {
        **(pm.get("labels") or {}),
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "serverless2",
        f"{LABEL}/model": name,
        f"{LABEL}/protocol": v.get("protocol") or "http",
    }

    def template(role: str, leader: bool, command) -> dict:
        md = {"labels": {**pod_labels, f"{LABEL}/role": role}}
        if pm.get("annotations"):
            md["annotations"] = dict(pm["annotations"])
        return {"metadata": md, "spec": {**_pod(v, ib), "containers": [_lws_container(v, nodes, ib, leader, command)]}}

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
                    "name": "serverless2-external",
                    "namespace": "envoy-gateway-system",
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


# Knative's data path into an endpoint pod is the queue-proxy: 8012 (http1) and 8112 (h2c) carry every request the
# gateway or the activator sends (the user container is reached over localhost only); 9090 is the queue-proxy's
# autoscaler metrics, 9091 the user metrics it proxies (PodMonitor `knative-queue-proxy`); 8022 (drain) is called by the
# kubelet (host entity). A multi-node endpoint has no queue-proxy: its leader Service targets the declared port.
QUEUE_PROXY_DATA = ("8012", "8112")
QUEUE_PROXY_METRICS = ("9090", "9091")


def _tcp(ports) -> list[dict]:
    return [{"ports": [{"port": str(p), "protocol": "TCP"} for p in ports]}]


def _ingress_policy(v: dict, nodes: int) -> dict:
    """`ingress_isolation: declared-ports` (docs/API.md "Ingress isolation"): a namespaced CiliumNetworkPolicy on this
    endpoint's pods that admits only the platform's own data path on the declared ports (the gateway and the Knative
    activator to the queue-proxy, or to the container port of a multi-node leader), the node (kubelet probes, drain),
    Prometheus to the metrics ports, and the replica group's own pods (multi-node). Every other ingress is dropped: a
    second port an engine binds on 0.0.0.0 (a NIM on 9000 behind a wrapper on 8080), the container port itself, from
    any other model, tenant or namespace. Egress is not touched (the lease client's path stays). Cilium policies are
    allow-lists that add up: this one widens nothing anywhere else."""
    name, ns = v["name"], v["namespace"]
    multi = nodes > 1
    data = [str(v.get("port"))] if multi else list(QUEUE_PROXY_DATA)
    metrics = [str(v.get("port"))] if multi else list(QUEUE_PROXY_METRICS)
    selector = (
        {"matchLabels": {f"{LABEL}/model": name}}
        if multi
        else {"matchLabels": {"serving.kserve.io/inferenceservice": name}}
    )
    ingress = [
        {
            "fromEntities": ["host", "remote-node"]
        },  # kubelet probes and the queue-proxy drain (:8022), node-local traffic
        {
            "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "envoy-gateway-system"}}],
            "toPorts": _tcp(data),
        },
    ]
    if not multi:
        ingress.append(  # the activator (scale from zero, burst) and the autoscaler's queue-proxy scrape
            {
                "fromEndpoints": [{"matchLabels": {"io.kubernetes.pod.namespace": "knative-serving"}}],
                "toPorts": _tcp(data + ["9090"]),
            }
        )
    ingress.append(
        {
            "fromEndpoints": [
                {"matchLabels": {"io.kubernetes.pod.namespace": "monitoring", "app.kubernetes.io/name": "prometheus"}}
            ],
            "toPorts": _tcp(metrics),
        }
    )
    if multi:
        ingress.append(  # the group's own pods (NCCL, the engine's own ports): every port, nothing else selects them
            {
                "fromEndpoints": [
                    {"matchLabels": {"io.kubernetes.pod.namespace": ns, "leaderworkerset.sigs.k8s.io/name": name}}
                ]
            }
        )
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": f"{name}-ingress", "namespace": ns, "labels": _labels(v)},
        "spec": {
            "description": f"{name}: only the platform's data path on the declared ports reaches the endpoint's pods",
            "endpointSelector": selector,
            "ingress": ingress,
        },
    }


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
                        "x-serverless2-operation",  # the verified admitted operation and attempt (empty on a direct call):
                        "x-serverless2-attempt",  # the worker attaches its native execution to them (docs/API.md)
                    ],
                },
            },
        },
    }


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
    docs = [_securitypolicy(v, api_backend)] + ([_inferenceservice(v)] if nodes <= 1 else _leaderworkerset(v, nodes))
    if _trust(v):
        docs.append(_trust_configmap(v))
    if v.get("ingressIsolation"):
        if str(v["ingressIsolation"]) != "declared-ports":
            raise _fail("ingress_isolation: declared-ports is the only mode", v["name"])
        docs.append(_ingress_policy(v, nodes))
    return sorted(docs, key=lambda d: KINDS.index(d["kind"]))
