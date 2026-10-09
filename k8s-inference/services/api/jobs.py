"""Operations as Kubernetes Jobs (docs/JOBS.md): build from the catalog entry's `job` block, create,
read, list, cancel, resume, and normalise to the API's operation shape.

A run-class Job is one pod: `fetch` init container (runner image, inputs prefix -> /work/in),
`main` (the model's image and command, /work on a per-operation PVC), `uploader` (runner image,
waits for `main` to end, syncs /work to the outputs prefix, deletes the PVC on success). Spot
interruptions do not count against backoffLimit (podFailurePolicy) and the replacement pod reattaches
the PVC in the same region, so checkpointed work resumes automatically; a FAILED or CANCELLED
operation whose PVC still exists can be resumed by hand (`:resume`). Kueue admits the Job through
the tenant's LocalQueue of the model's scheduling profile (gated = QUEUED, for as long as it takes).

Two submission paths share this module. A regional API creates the Job in its own cluster (region
known, pool and image from `deployments.<region>.parameters`). The fleet API on the control cluster
(FLEET_MANAGER) creates it there with `managedBy: kueue.x-k8s.io/multikueue`: Kueue's MultiKueue and
the cost dispatcher pick the worker (restricted to the model's regions and GPU classes) at queue time, the
dispatcher creates the work volume where the run is admitted, and the manager Job's status is mirrored
back; pods, logs and billing come from the worker. Image references are fleet-wide (the logical registry
host of fleet.yaml `images`, docs/IMAGES.md): nothing in a Job depends on the region it lands in."""
import json
from fastapi import HTTPException
from kubernetes import client
from kubernetes.client.rest import ApiException
import kube
from config import (EXECUTOR_SA, GRAFANA_URLS, JOB_BACKOFF_LIMIT, JOB_PVC_SIZE_GI, JOB_TTL_S, KUEUE_PRIORITY, KUEUE_QUEUE, LABEL,
                    ENDPOINT_CONNECT_TO, LITELLM_URL, MULTIKUEUE_MANAGED_BY, MULTIKUEUE_ORIGIN_LABEL, REGION, RUNNER_IMAGE, S3_ENV_SECRET,
                    INFINIBAND_CLAIM_TEMPLATE, INFINIBAND_ENV, JOBSET_GROUP, JOBSET_NAME_LABEL, JOBSET_PLURAL, JOBSET_VERSION,
                    MULTINODE_MAX_NODES, SHARED_SCRATCH_PVC)
from resilience import retry
from render import (CONTAINER_HARDENING, POD_SECURITY_CONTEXT, PRIORITY_CLASS, RUNNER_SECURITY_CONTEXT, TOKEN, _job_shell, _meta,
                    _pod_failure_policy, _render, _uploader, class_images, gpu_classes, image_for_class, op_name, profile_of)
from status import (PHASE, RECORD_STATUS, _attempts, _gpus, _main_state, _secs, _ts, finished, gpu_seconds, is_jobset, is_manager_job,
                    is_mirror, logs_url, normalise)

def build_run(name: str, model: dict, params: dict, tenant: str, label: str | None, idem: str | None, timeout_s: int | None,
              priority: str | None, region: str | None = REGION, defaults: dict | None = None, key_hash: str | None = None,
              pvc: str | None = None, placement: dict | None = None, gpu_class: str | None = None) -> dict:
    """Render the catalog entry's `job` block with the parameters (caller's input over the region's
    defaults over the entry's defaults) into a Job; the per-operation PVC is `<name>-work` unless a
    resume passes the original run's. region None = fleet placement (the dispatcher picks the worker).
    `gpu_class`: the class chosen at submission for a run class with per-class images (`job.images`,
    placement.choose_class): that class's image, a node affinity over that class's pools only, and the
    class's profile queue; a run with one image keeps switching class at queue time."""
    job = dict(model.get("job") or {})
    images = class_images(model)
    if images:
        if not gpu_class:
            raise HTTPException(500, f"model {model['name']} has per-class images; the GPU class must be chosen at submission")
        if gpu_class not in gpu_classes(model):
            raise HTTPException(400, f"GPU class {gpu_class!r} is not one of {gpu_classes(model)} for model {model['name']}")
        job["image"] = image_for_class(model, gpu_class)
        job.pop("images", None)
    else:
        gpu_class = None
    if not job.get("image") or not job.get("command"):
        raise HTTPException(500, f"model {model['name']} has no job.image/job.command")
    declared = {p["name"]: p.get("default") for p in model.get("parameters", [])}
    declared.update({k: None for k in (defaults or {})})
    declared.update({"input_prefix": None, "output_prefix": None})
    unknown = [k for k in params if k not in declared]
    if unknown:
        raise HTTPException(400, f"unknown parameters {unknown}; model accepts {sorted(declared)}")
    values = {k: v for k, v in declared.items() if v is not None}
    values.update({k: v for k, v in (defaults or {}).items() if v is not None})
    values.update(params)
    missing = [p["name"] for p in model.get("parameters", []) if p.get("required") and values.get(p["name"]) in (None, "")]
    if missing:
        raise HTTPException(400, f"missing required parameters {missing}")
    values["operation"] = name
    r = _render(job, values)
    if argument_parameter := job.get("argsParameter"):
        arguments = values.get(argument_parameter, [])
        if not isinstance(arguments, list) or len(arguments) > 100 or not all(isinstance(a, str) for a in arguments):
            raise HTTPException(400, "args must be a list of at most 100 strings")
        r["args"] = arguments
    if environment_parameter := job.get("envParameter"):
        import re
        environment = values.get(environment_parameter, {})
        if not isinstance(environment, dict) or len(environment) > 100 or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and isinstance(v, str) for k, v in environment.items()):
            raise HTTPException(400, "env must map valid environment names to string values (at most 100)")
        r["env"] = {**environment, **(r.get("env") or {})}
    cmd = r["command"]
    if isinstance(cmd, str):
        # Shell strings still receive typed arguments as literal argv values, not shell syntax.
        cmd = ["/bin/sh", "-c", cmd + ' "$@"', "--"] if r.get("args") else ["/bin/sh", "-c", cmd]
    gpus = int(r.get("gpu", 0) or 0)
    res = {"requests": {"cpu": str(r.get("cpu", "1")), "memory": str(r.get("memory", "2Gi"))}, "limits": {"memory": str(r.get("memoryLimit", r.get("memory", "2Gi")))}}
    if r.get("cpuLimit"):
        res["limits"]["cpu"] = str(r["cpuLimit"])
    if gpus:
        res["requests"]["nvidia.com/gpu"] = res["limits"]["nvidia.com/gpu"] = str(gpus)
    pvc = pvc or f"{name}-work"
    # scratch: network (default) = the per-operation PVC, re-attached to the replacement pod after a preemption
    # (automatic resume); local-nvme = an emptyDir on the node's host NVMe (pools with local_nvme), fastest I/O,
    # gone with the pod: the run must checkpoint to object storage itself. Pins the run to NVMe nodes.
    scratch = str(r.get("scratch") or "network").lower()
    if scratch not in ("network", "local-nvme"):
        raise HTTPException(400, f"scratch must be network or local-nvme, not {scratch!r}")
    local_nvme = scratch == "local-nvme"
    main = {"name": "main", "image": r["image"], "command": cmd, "resources": res, "workingDir": r.get("workDir", "/work"),
            "volumeMounts": [{"name": "work", "mountPath": "/work"}, {"name": "shm", "mountPath": "/dev/shm"}],
            "securityContext": {**CONTAINER_HARDENING, **({"runAsNonRoot": True} if r.get("runAsNonRoot") else {})}}
    if r.get("envFrom"):
        main["envFrom"] = r["envFrom"]
    if r.get("args"):
        main["args"] = r["args"]
    if r.get("env"):
        main["env"] = [{"name": k, "value": str(v)} for k, v in r["env"].items()]
    if r.get("s3Env", True):
        main["envFrom"] = [{"secretRef": {"name": S3_ENV_SECRET}}]
    fetch = {"name": "fetch", "image": RUNNER_IMAGE, "command": ["/usr/local/bin/fetch.sh"],
             "env": [{"name": "INPUT_PREFIX", "value": r.get("inputs") or values.get("input_prefix") or ""}],
             "envFrom": [{"secretRef": {"name": S3_ENV_SECRET}}],
             "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
             "volumeMounts": [{"name": "work", "mountPath": "/work"}], "securityContext": RUNNER_SECURITY_CONTEXT}
    pod = {"restartPolicy": "Never", "serviceAccountName": EXECUTOR_SA, "terminationGracePeriodSeconds": int(r.get("graceSeconds", 120)),
           "priorityClassName": PRIORITY_CLASS.get(priority, "serverless2-batch"),
           # the runner containers run as uid 10001; the volume is group-owned by them so fetch/upload work
           # whatever user the model image runs as (root-created files stay group-readable)
           "securityContext": {"fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch", **POD_SECURITY_CONTEXT},
           # shared PID namespace: the command's shell is not PID 1, so SIGTERM (preemption, cancel) actually
           # terminates `sh -c ...` commands instead of being ignored until the grace period's SIGKILL
           "shareProcessNamespace": True,
           "initContainers": [fetch], "containers": [main, _uploader(name, region, values["output_prefix"], None if local_nvme else pvc, r.get("uploadExcludes", ""), gpus, placement, gpu_class)],
           "volumes": [{"name": "work", "emptyDir": {"sizeLimit": f"{r.get('pvcSizeGi', JOB_PVC_SIZE_GI)}Gi"}} if local_nvme else {"name": "work", "persistentVolumeClaim": {"claimName": pvc}},
                       {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": r.get("shm", "8Gi")}}]}
    if gpus:
        pod["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    terms = []
    if r.get("pool"):
        pod["nodeSelector"] = {f"{LABEL}/pool": r["pool"]}
    elif gpus and gpu_classes(model):
        # the preference queue may fall back to another GPU class; keep it to the classes the image runs on
        # (Kueue assigns only flavors whose node labels satisfy the affinity, docs/SCHEDULING.md). With per-class
        # images the image is the chosen class's, so only that class's pools qualify.
        pools = kube.class_pools([gpu_class] if gpu_class else gpu_classes(model))
        if pools:
            terms.append({"key": f"{LABEL}/pool", "operator": "In", "values": pools})
    if local_nvme:
        terms.append({"key": f"{LABEL}/local-nvme", "operator": "In", "values": ["true"]})
        # only regions with an NVMe pool are candidates for the dispatcher: Kueue ignores an affinity key that
        # no flavor of a cluster declares, so a worker without such a pool would admit the run and leave its pod
        # Pending for ever (measured 2026-10-08/09; the flavor of a local_nvme pool carries the label since then)
        nvme_regions = sorted({p["region"] for p in (kube.fleet().get("pools") or {}).values() if p.get("local_nvme") in (True, "true")})
        if kube.fleet().get("pools") and not nvme_regions:
            raise HTTPException(400, "scratch local-nvme needs a pool with local_nvme = true; this fleet has none (docs/FLEET.md \"Local NVMe\")")
        if placement is not None and nvme_regions:
            wanted = placement.get("regions") or nvme_regions
            keep = [r for r in wanted if r in nvme_regions]
            if not keep:
                raise HTTPException(400, f"scratch local-nvme: none of the regions {wanted} has a pool with local_nvme = true (regions with one: {nvme_regions})")
            placement = {**placement, "regions": keep}
    if terms:
        pod["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": terms}]}}}
    if r.get("imagePullSecret"):
        pod["imagePullSecrets"] = [{"name": r["imagePullSecret"]}]
    profile = f"prefer-{gpu_class}" if gpu_class else ((placement or {}).get("profile") or profile_of(model))
    if gpu_class and placement:
        placement = {**placement, "profile": profile, "classes": [gpu_class]}   # the dispatcher keeps the run on this class
    meta = _meta(name, tenant, model["name"], "run", region, label, params, idem, priority, key_hash, profile, gpu_class)
    rates = {}
    for pool in kube.fleet().get("pools", {}).values():
        pool_region = kube.cluster_region(pool["region"])
        rate = (model.get("deployments", {}).get(pool_region) or {}).get("price_per_gpu_hour", pool.get("usd_per_gpu_hour"))
        if rate is not None:
            rates.setdefault(pool_region, {})[pool["pool"]] = rate
    meta["annotations"][f"{LABEL}/billing-rates"] = json.dumps(rates, sort_keys=True)
    meta["annotations"][f"{LABEL}/scratch"] = scratch
    if gpu_class:
        meta["annotations"][f"{LABEL}/image"] = job["image"]
    nodes = int(r.get("nodes", 1) or 1)
    if nodes > 1:
        return _multinode(name, model, r, values, pod, nodes, gpus, meta, timeout_s, placement)
    if not local_nvme:
        meta["annotations"][f"{LABEL}/pvc"] = pvc
        meta["annotations"][f"{LABEL}/pvc-size-gi"] = str(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    return _job_shell(name, meta, pod, timeout_s, placement)


# ---------------------------------------------------------------------------------------------------------------
# Multi-node runs (docs/JOBS.md "Multi-node runs"): a job class with `nodes: N` (N > 1) renders a JobSet of one
# indexed Job with N pods, one per node (a full node each: `gpu` = the preset's GPU count), a headless Service
# for rank discovery, torchrun/NCCL environment, and restart-all on any pod loss (spot preemption). `interconnect`
# (none | preferred | required) decides whether the pods must land on an InfiniBand pool (fleet.yaml pools with
# `interconnect: infiniband`, Terraform GPU cluster) and get the fabric NICs through a DRA ResourceClaimTemplate.
INTERCONNECT = ("none", "preferred", "required")


def _multinode(name: str, model: dict, r: dict, values: dict, pod: dict, nodes: int, gpus: int, meta: dict,
               timeout_s: int | None, placement: dict | None) -> dict:
    if nodes > MULTINODE_MAX_NODES:
        raise HTTPException(400, f"nodes must be <= {MULTINODE_MAX_NODES}")
    # Default `required`: a multi-node run without the fabric runs NCCL over TCP, which is useless for the models
    # that need several nodes (docs/JOBS.md "Limits of multi-node runs"); `none` is an explicit opt-in.
    interconnect = str(r.get("interconnect") or "required")
    if interconnect not in INTERCONNECT:
        raise HTTPException(400, f"interconnect must be one of {INTERCONNECT}")
    checkpoints = str(r.get("checkpoints", "local") or "local")
    if checkpoints not in ("local", "shared"):
        raise HTTPException(400, "checkpoints must be local or shared")
    ib = interconnect == "required"
    main = next(c for c in pod["containers"] if c["name"] == "main")
    uploader = next(c for c in pod["containers"] if c["name"] == "uploader")
    # placement: whole nodes of the model's classes; InfiniBand pools only when required (preferred = soft). The
    # local-NVMe term of `scratch: local-nvme` (build_run) is kept; the single-node class affinity is replaced.
    classes = gpu_classes(model)
    kept = [t for t in (((pod.get("affinity") or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {}).get("nodeSelectorTerms", [{}])[0].get("matchExpressions", [])
            if t.get("key") == f"{LABEL}/local-nvme"]
    pools: list[str] = []
    if classes and gpus:
        pools = kube.class_pools(classes, gpus_per_node=gpus, interconnect="infiniband" if ib else None)
        if not pools and kube.fleet()["pools"]:
            raise HTTPException(400, f"no {'InfiniBand ' if ib else ''}pool with {gpus}-GPU nodes for classes {classes} in the fleet"
                                     + (" (multi-node runs need the fabric: pass interconnect: none to run NCCL over TCP on purpose)" if ib else "")
                                     + " (fleet.yaml pools, docs/FLEET.md)")
    terms = kept + ([{"key": f"{LABEL}/pool", "operator": "In", "values": pools}] if pools else [])
    if terms:
        pod["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": terms}]}}}
    elif "affinity" in pod:
        del pod["affinity"]
    ib_count = int(r.get("ibDevicesPerNode") or kube.ib_devices_per_node(pools))
    if ib:
        pod.setdefault("affinity", {}).setdefault("nodeAffinity", {}).setdefault("requiredDuringSchedulingIgnoredDuringExecution", {"nodeSelectorTerms": [{"matchExpressions": []}]})
        pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"].append(
            {"key": f"{LABEL}/interconnect", "operator": "In", "values": ["infiniband"]})
    elif interconnect == "preferred":
        pod.setdefault("affinity", {}).setdefault("nodeAffinity", {})["preferredDuringSchedulingIgnoredDuringExecution"] = [
            {"weight": 100, "preference": {"matchExpressions": [{"key": f"{LABEL}/interconnect", "operator": "In", "values": ["infiniband"]}]}}]
    # /work per pod: node-local scratch (the kubelet's ephemeral storage: local NVMe on pools that have it), not a
    # per-run RWO volume (N pods on N nodes). Checkpoints: `shared` = the tenant's RWX claim on the cluster's
    # shared filesystem (survives a restart and a :resume), `local` = under /work, gone with the pod.
    disk = int(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    pod["volumes"] = [{"name": "work", "emptyDir": {"sizeLimit": f"{disk}Gi"}}] + [v for v in pod["volumes"] if v["name"] != "work"]
    root = meta["annotations"].get(f"{LABEL}/resumed-from") or name
    if checkpoints == "shared":
        pod["volumes"].append({"name": "checkpoints", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC}})
        for c in (main, uploader):
            c["volumeMounts"].append({"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{root}"})
    rank = {"name": "NODE_RANK", "valueFrom": {"fieldRef": {"fieldPath": "metadata.annotations['batch.kubernetes.io/job-completion-index']"}}}
    dist = {"NNODES": str(nodes), "GPUS_PER_NODE": str(gpus), "WORLD_SIZE": str(nodes * max(gpus, 1)),
            "MASTER_ADDR": f"{name}-workers-0-0.{name}", "MASTER_PORT": "29500", "CHECKPOINT_DIR": "/work/checkpoint"}
    if ib:
        dist.update(INFINIBAND_ENV)
    env = {e["name"]: e for e in main.get("env", [])}
    for k, v in dist.items():
        env.setdefault(k, {"name": k, "value": v})       # the job class's own env wins
    main["env"] = [rank] + list(env.values())
    uploader["env"] = [e for e in uploader["env"] if e["name"] != "PVC_NAME"] + [rank, {"name": "UPLOAD_SCOPE", "value": "rank0"}]
    if r.get("uploadExcludes") is None:
        uploader["env"].append({"name": "UPLOAD_EXCLUDES", "value": "--exclude checkpoint/*"})   # checkpoints are not results
    if ib:
        # every InfiniBand NIC of the node through DRA (DeviceClass ib.networking.nebius.ai, DraNet on the node image).
        # No capability: RDMA pins memory, which RLIMIT_MEMLOCK bounds, and the GPU nodes' container runtime runs
        # with an unlimited memlock limit (cloud-init + node-config), so `drop: [ALL]` under PSA baseline holds
        # (measured 2026-10-08, docs/JOBS.md "Multi-node runs").
        pod["resourceClaims"] = [{"name": "ib", "resourceClaimTemplateName": f"{INFINIBAND_CLAIM_TEMPLATE}-{ib_count}"}]
        main["resources"]["claims"] = [{"name": "ib"}]
    pod["subdomain"] = name
    tmpl_meta = {"labels": {k: v for k, v in meta["labels"].items() if k.startswith(LABEL)}}
    ann = {}
    if placement and placement.get("classes"):
        ann[f"{LABEL}/gpu-classes"] = ",".join(placement["classes"])
    if placement and placement.get("regions"):
        ann[f"{LABEL}/regions"] = ",".join(placement["regions"])
    if ann:
        tmpl_meta["annotations"] = ann
    job_spec = {"parallelism": nodes, "completions": nodes, "completionMode": "Indexed", "backoffLimit": 0,
                "template": {"metadata": tmpl_meta, "spec": pod}}
    if timeout_s:
        job_spec["activeDeadlineSeconds"] = timeout_s
    meta["annotations"][f"{LABEL}/nodes"] = str(nodes)
    meta["annotations"][f"{LABEL}/interconnect"] = interconnect
    meta["annotations"][f"{LABEL}/checkpoints"] = checkpoints
    spec = {"network": {"enableDNSHostnames": True, "subdomain": name, "publishNotReadyAddresses": True},
            "successPolicy": {"operator": "All", "targetReplicatedJobs": ["workers"]},
            # any pod loss (preemption, node loss, application failure) fails the indexed Job (backoffLimit 0) and the
            # JobSet recreates every pod: ranks restart together from the last checkpoint
            "failurePolicy": {"maxRestarts": JOB_BACKOFF_LIMIT, "restartStrategy": "Recreate"},
            "ttlSecondsAfterFinished": JOB_TTL_S,
            "replicatedJobs": [{"name": "workers", "replicas": 1, "template": {"metadata": {"labels": dict(tmpl_meta["labels"])}, "spec": job_spec}}]}
    if placement and placement.get("manager"):
        spec["managedBy"] = MULTIKUEUE_MANAGED_BY
    return {"apiVersion": f"{JOBSET_GROUP}/{JOBSET_VERSION}", "kind": "JobSet", "metadata": meta, "spec": spec}


def _jobset_as_job(js: dict) -> dict:
    """A JobSet read back from the API in the shape the rest of this module handles (metadata as is; the indexed
    Job's template under spec.template; Completed/Failed conditions as Complete/Failed). `kind` stays JobSet."""
    spec = js.get("spec") or {}
    rj = ((spec.get("replicatedJobs") or [{}])[0].get("template") or {}).get("spec") or {}
    conds = []
    for c in (js.get("status") or {}).get("conditions") or []:
        conds.append({**c, "type": {"Completed": "Complete"}.get(c.get("type"), c.get("type"))})
    return {"apiVersion": js.get("apiVersion"), "kind": "JobSet", "metadata": js.get("metadata", {}),
            "spec": {"managedBy": spec.get("managedBy"), "suspend": spec.get("suspend"), "template": rj.get("template", {}),
                     "activeDeadlineSeconds": rj.get("activeDeadlineSeconds"), "parallelism": rj.get("parallelism")},
            "status": {"conditions": conds, "restarts": (js.get("status") or {}).get("restarts")}}


def _jobset_api(region: str):
    return kube.api(region)


def read_jobset(ns: str, name: str, region: str) -> dict:
    return _jobset_as_job(retry(_jobset_api(region).get_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, name))


def list_jobsets(ns: str, region: str, selector: str) -> list[dict]:
    items = retry(_jobset_api(region).list_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, label_selector=selector).get("items", [])
    return [_jobset_as_job(j) for j in items]


def patch_jobset(ns: str, name: str, region: str, body: dict) -> dict:
    return _jobset_as_job(retry(_jobset_api(region).patch_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, name, body))


def delete_jobset(ns: str, name: str, region: str) -> None:
    retry(_jobset_api(region).delete_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, name)


def pods_selector(job: dict) -> str:
    return f"{JOBSET_NAME_LABEL}={job['metadata']['name']}" if is_jobset(job) else f"job-name={job['metadata']['name']}"


def build_async(name: str, model: dict, inp: dict, tenant: str, key: str, label: str | None, idem: str | None, timeout_s: int | None,
                output_prefix: str, region: str = REGION) -> tuple[dict, dict]:
    """(Job, Secret): a queued endpoint call through LiteLLM's pass-through route when the model has
    one (spend lands on the caller's key), else through the cluster's gateway with the caller's key
    (TLS, key check, per-key rate limit: the same path as an external client; never the predictor
    Service, so no call skips the key check or the accounting). The key goes into a per-operation Secret owned by the
    Job, never into the Job spec."""
    env = []
    if model.get("litellm_route"):
        url, auth = LITELLM_URL + model["litellm_route"], f"Bearer {key}"
    else:
        if not model.get("endpoint_external_host"):
            raise HTTPException(503, "async calls need the cluster's endpoint domain (ENDPOINT_DOMAIN)")
        url, auth = model["endpoint_external_host"] + model["endpoint_path"], f"Bearer {key}"
        if ENDPOINT_CONNECT_TO:
            host = url.split("/")[2]
            env.append({"name": "CONNECT_TO", "value": f"{host}:443:{ENDPOINT_CONNECT_TO}"})
    secret_name = f"{name}-auth"
    if model.get("protocol") == "openai" and model.get("served_model") and isinstance(inp, dict) and not inp.get("model"):
        inp = {**inp, "model": model["served_model"]}          # callers know the catalog id, not the served name
    env += [{"name": "URL", "value": url}, {"name": "REQUEST_BODY", "value": json.dumps(inp)},
            {"name": "AUTH_HEADER", "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "authorization", "optional": True}}},
            {"name": "CALL_TIMEOUT", "value": str(min(timeout_s or 3600, 3600))}]
    main = {"name": "main", "image": RUNNER_IMAGE, "command": ["/usr/local/bin/call.sh"], "env": env,
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
            "volumeMounts": [{"name": "work", "mountPath": "/work"}], "securityContext": RUNNER_SECURITY_CONTEXT}
    pod = {"restartPolicy": "Never", "serviceAccountName": EXECUTOR_SA, "terminationGracePeriodSeconds": 60,
           "priorityClassName": "serverless2-batch", "securityContext": POD_SECURITY_CONTEXT,
           "containers": [main, _uploader(name, region, output_prefix, None)],
           "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "1Gi"}}]}
    meta = _meta(name, tenant, model["name"], "async", region, label, inp, idem, None, None)
    job = _job_shell(name, meta, pod, timeout_s)
    job["spec"]["backoffLimit"] = 6
    # a 4xx from the endpoint is the caller's error (call.sh exit 2): fail the Job, no pod retries
    job["spec"]["podFailurePolicy"]["rules"].insert(0, {"action": "FailJob", "onExitCodes": {"containerName": "main", "operator": "In", "values": [2]}})
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": secret_name, "labels": {f"{LABEL}/tenant": tenant}},
              "stringData": {"authorization": auth}}
    return job, secret


def check_image_allowed(ns: str, image: str, region: str = REGION) -> None:
    """Tenant image allow-list: the tenant namespace's annotation serverless2.nebius/allowed-images (comma-separated
    prefixes, charts/tenant `allowedImages`); absent = any image. Raises 400 for an image outside the list."""
    try:
        ns_obj = retry(kube.core(region).read_namespace, ns)
        allowed = ((ns_obj.metadata.annotations or {}).get(f"{LABEL}/allowed-images") or "").strip()
    except ApiException:
        return
    prefixes = [p.strip() for p in allowed.split(",") if p.strip()]
    if prefixes and not any(image.startswith(p) for p in prefixes):
        raise HTTPException(400, f"image {image!r} is not allowed for this tenant (allowed prefixes: {prefixes})")


def _main_container(job: dict) -> dict:
    return next((c for c in job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) if c.get("name") == "main"), {})


def output_prefix(job: dict) -> str | None:
    """The uploader's OUTPUT_PREFIX: s3://<bucket>/operations/<id>; names the bucket of the run's artifacts."""
    for c in job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        if c.get("name") == "uploader":
            return next((e.get("value") for e in c.get("env", []) if e.get("name") == "OUTPUT_PREFIX"), None)
    return None


def create(ns: str, job: dict, region: str = REGION, secret: dict | None = None) -> tuple[dict, bool]:
    """Returns (job, created). AlreadyExists -> the existing one (idempotent replay). The per-operation
    PVC and Secret are owned by the Job (garbage-collected with it). A manager Job gets no PVC here: the
    dispatcher creates it on the worker it picks (docs/JOBS.md)."""
    name = job["metadata"]["name"]
    import hashlib
    request_hash = hashlib.sha256(json.dumps(job["spec"], sort_keys=True).encode()).hexdigest()
    job["metadata"].setdefault("annotations", {})[f"{LABEL}/request-hash"] = request_hash
    was_created = True
    try:
        if is_jobset(job):
            retry(_jobset_api(region).create_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, job)
        else:
            retry(kube.batch(region).create_namespaced_job, ns, job)
    except ApiException as e:
        if e.status == 409:
            was_created = False
        elif e.status == 404:
            raise HTTPException(404, f"tenant namespace {ns} is not onboarded in {region}")
        else:
            raise HTTPException(502, f"kubernetes ({region}): {e.status} {e.reason}: {(e.body or '')[:300]}")
    created = get(ns, name, region)
    if not was_created:
        if created["metadata"].get("annotations", {}).get(f"{LABEL}/request-hash", request_hash) != request_hash:
            raise HTTPException(409, "idempotency key was already used with different input")
        job = created
    if is_jobset(job):
        return created, was_created    # no per-run volume: /work is node-local, checkpoints on the shared claim
    owner = {"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": created["metadata"].get("uid", "")}
    ann, core = job["metadata"].get("annotations", {}), kube.core(region)
    pvc = None if is_manager_job(job) else ann.get(f"{LABEL}/pvc")
    if pvc and not ann.get(f"{LABEL}/resumed-from"):
        body = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc, "ownerReferences": [owner], "labels": {f"{LABEL}/operation": name}},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": f"{ann[f'{LABEL}/pvc-size-gi']}Gi"}}}}
        _create_ignore_exists(core.create_namespaced_persistent_volume_claim, ns, body, region)
    elif pvc:
        # a resumed run co-owns the volume, so it outlives the original Job's TTL or deletion
        try:
            cur = core.api_client.sanitize_for_serialization(retry(core.read_namespaced_persistent_volume_claim, pvc, ns))
            refs = [r for r in (cur["metadata"].get("ownerReferences") or []) if r.get("uid") != owner["uid"]] + [owner]
            retry(core.patch_namespaced_persistent_volume_claim, pvc, ns, {"metadata": {"ownerReferences": refs}})
        except ApiException as e:
            raise HTTPException(502, f"kubernetes ({region}): work volume {pvc}: {e.reason}")
    if secret:
        secret["metadata"]["ownerReferences"] = [owner]
        _create_ignore_exists(core.create_namespaced_secret, ns, secret, region)
    return created, was_created


def _create_ignore_exists(fn, ns: str, body: dict, region: str):
    try:
        retry(fn, ns, body)
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(502, f"kubernetes ({region}): {e.status} {e.reason}: {(e.body or '')[:300]}")


def get(ns: str, name: str, region: str = REGION) -> dict:
    try:
        job = retry(kube.batch(region).read_namespaced_job, name, ns)
        job = kube.batch(region).api_client.sanitize_for_serialization(job)
    except ApiException as e:
        # 403: the namespace is not readable in this region (the control cluster has no tenant namespaces;
        # find() then continues with the next region)
        if e.status not in (403, 404):
            raise HTTPException(502, f"operation {name}: {e.reason}")
        try:
            job = read_jobset(ns, name, region)       # a multi-node run
        except ApiException as e2:
            raise HTTPException(404 if e2.status in (403, 404) else 502, f"operation {name}: {e2.reason}")
    if is_mirror(job):
        raise HTTPException(404, f"operation {name}: not found")      # the operation is the manager's Job
    if is_manager_job(job):
        return _with_worker(job, ns, kube.workload_clusters(ns))
    return _with_pods(job, ns, region)


def _with_worker(job: dict, ns: str, clusters: dict) -> dict:
    """A manager Job: pods (attempts) come from the worker that admitted its Workload; nothing yet while
    the dispatcher still has to place it. `_region` is the worker's region or None."""
    cluster = clusters.get(job["metadata"].get("uid", ""))
    if not cluster:
        job["_pods"], job["_region"], job["_cluster"] = [], None, None
        return job
    region = kube.cluster_region(cluster)
    try:
        out = _with_pods(job, ns, region)
    except HTTPException:            # the worker's kubeconfig is not mounted here: status only
        job["_pods"], job["_region"] = [], region
        out = job
    out["_cluster"] = cluster
    return out


def _with_pods(job: dict, ns: str, region: str, pods: list | None = None) -> dict:
    if pods is None:
        try:
            raw = retry(kube.core(region).list_namespaced_pod, ns, label_selector=pods_selector(job)).items
            pods = [kube.core(region).api_client.sanitize_for_serialization(p) for p in raw]
        except ApiException:
            pods = []
    job["_pods"] = sorted(pods, key=lambda p: p["metadata"].get("creationTimestamp", ""))
    job["_region"] = region
    return job


def find(ns: str, name: str) -> tuple[dict, str]:
    """Operation ids are Job names; look in every region (own region first). For a manager Job the
    returned region is the worker's region once placed (None before)."""
    for r in kube.regions():
        try:
            job = get(ns, name, r)
            return job, (job["_region"] if is_manager_job(job) else r)
        except HTTPException as e:
            if e.status_code != 404:
                raise
    raise HTTPException(404, f"operation {name}: not found")


def list_ops(ns: str, tenant: str, model: str | None, limit: int) -> list[dict]:
    sel = f"{LABEL}/tenant={tenant}" + (f",{LABEL}/model={model}" if model else "")
    items, by_job, managers = [], {}, []
    for r in kube.regions():
        try:
            b = kube.batch(r)
            jobs = [b.api_client.sanitize_for_serialization(j) for j in retry(b.list_namespaced_job, ns, label_selector=sel).items]
            pods = [b.api_client.sanitize_for_serialization(p) for p in retry(kube.core(r).list_namespaced_pod, ns, label_selector=f"{LABEL}/tenant={tenant}").items]
            jobs += list_jobsets(ns, r, sel)
        except ApiException as e:
            if e.status in (403, 404):          # tenant not onboarded in that region
                continue
            raise HTTPException(502, f"kubernetes ({r}): {e.reason}")
        for p in pods:
            labels = p["metadata"].get("labels", {})
            by_job.setdefault((r, labels.get(JOBSET_NAME_LABEL) or labels.get("job-name", "")), []).append(p)
        for j in jobs:
            if is_mirror(j):
                continue                          # the worker's copy of a manager Job: pods are joined below
            if is_manager_job(j):
                managers.append(j)
            else:
                items.append(_with_pods(j, ns, r, by_job.get((r, j["metadata"]["name"]), [])))
    if managers:
        clusters = kube.workload_clusters(ns)
        for j in managers:
            cluster = clusters.get(j["metadata"].get("uid", ""))
            region = kube.cluster_region(cluster) if cluster else None
            j["_cluster"] = cluster
            items.append(_with_pods(j, ns, region, by_job.get((region, j["metadata"]["name"]), [])) if region else _with_worker(j, ns, {}))
    items.sort(key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)
    return items[:limit]


def cancel(ns: str, name: str, region: str = REGION) -> dict:
    """A queued (never started) Job is deleted; a running one is stopped by a 1 s deadline and kept
    (status CANCELLED through the annotation), pods terminate within the grace period. For a manager
    Job the deadline goes onto the worker's copy (MultiKueue mirrors spec only at creation) and the
    annotation onto the manager's; deleting the manager Job removes the worker's copy."""
    job = get(ns, name, region)
    op = normalise(job)
    if op["status"] not in ("QUEUED", "RUNNING"):
        return job
    if is_jobset(job):
        return _cancel_jobset(ns, name, region, job)
    b = kube.batch(region)
    # a manager Job's status is MultiKueue's mirror and lags: "started" is "a worker has it" (the Workload names
    # the cluster); the worker's copy gets the deadline, so the attempt is recorded and the volume kept
    started = job.get("_cluster") is not None if is_manager_job(job) else bool(job.get("status", {}).get("startTime"))
    if not started:
        try:
            retry(b.delete_namespaced_job, name, ns, propagation_policy="Background")
        except ApiException as e:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
        job["metadata"].setdefault("annotations", {})[f"{LABEL}/cancelled"] = "deleted"
        return job
    try:
        if is_manager_job(job):
            try:
                retry(kube.batch(job["_region"]).patch_namespaced_job, name, ns, {"spec": {"activeDeadlineSeconds": 1}})
            except ApiException as e:
                if e.status != 404:
                    raise
                # nominated but not copied to the worker yet: nothing has run, delete on the manager
                retry(b.delete_namespaced_job, name, ns, propagation_policy="Background")
                job["metadata"].setdefault("annotations", {})[f"{LABEL}/cancelled"] = "deleted"
                return job
            patched = retry(b.patch_namespaced_job, name, ns, {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}})
        else:
            patched = retry(b.patch_namespaced_job, name, ns, {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}, "spec": {"activeDeadlineSeconds": 1}})
    except ApiException as e:
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    out = _with_pods(b.api_client.sanitize_for_serialization(patched), ns, job["_region"], job["_pods"])
    out["_cluster"] = job.get("_cluster")
    return out


def _cancel_jobset(ns: str, name: str, region: str, job: dict) -> dict:
    """A multi-node run: never started -> deleted; running -> its Kueue Workload deactivated (Kueue evicts it and never
    admits it again; a suspend alone is undone within seconds and the pods restart) and the JobSet suspended (the
    JobSet controller deletes the pods, each uploader records its attempt) and annotated CANCELLED. A manager JobSet:
    the Workload is the manager's, the suspend goes to the worker's copy (MultiKueue mirrors the spec at creation only);
    a run pinned to a region holds both on that cluster."""
    started = job.get("_cluster") is not None if is_manager_job(job) else bool(job.get("_pods"))
    try:
        if not started:
            delete_jobset(ns, name, region)
            job["metadata"].setdefault("annotations", {})[f"{LABEL}/cancelled"] = "deleted"
            return job
        if is_manager_job(job):
            kube.deactivate_workload(ns, job["metadata"].get("uid", ""), region)
            try:
                patch_jobset(ns, name, job["_region"], {"spec": {"suspend": True}})
            except ApiException as e:
                if e.status != 404:
                    raise
                # the eviction already removed the worker's copy (or it was never created): the run is over either
                # way; the manager's record stays so the operation keeps its history and attempt records
            patched = patch_jobset(ns, name, region, {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}})
        else:
            kube.deactivate_workload(ns, job["metadata"].get("uid", ""), region)
            patched = patch_jobset(ns, name, region, {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}, "spec": {"suspend": True}})
    except ApiException as e:
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    out = _with_pods(patched, ns, job["_region"], job["_pods"])
    out["_cluster"] = job.get("_cluster")
    return out


def resume(ns: str, name: str, region: str, model: dict, defaults: dict, placement: dict | None = None) -> tuple[dict, bool]:
    """A new Job `<root>-rN` rendered again from the catalog entry with the original input, on the
    original PVC (same region: the volume is regional), linked through the resumed-from annotation.
    Rendering anew (rather than copying the finished Job's spec) leaves behind everything the Job
    controller and Kueue injected into it (selector, workload annotation, flavor labels). The
    original must be finished and its PVC must exist. `region` is the cluster the original is read
    from (the manager for a fleet-placed run); the new Job is pinned to the worker's region."""
    orig = get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    if op["status"] in ("QUEUED", "RUNNING"):
        raise HTTPException(409, f"operation {name} is {op['status']}; cancel it first")
    if op["mode"] != "run":
        raise HTTPException(400, "only run operations can be resumed")
    pvc = ann.get(f"{LABEL}/pvc")
    worker = orig["_region"] if is_manager_job(orig) else region
    if not worker:
        raise HTTPException(409, f"operation {name} never reached a worker; submit a new run")
    if is_jobset(orig):
        if ann.get(f"{LABEL}/checkpoints") != "shared":
            raise HTTPException(409, f"operation {name} kept its checkpoints on node-local scratch (checkpoints: local); submit a new run")
        pvc = None                                  # the shared claim is mounted by name, under checkpoints/<root>
    else:
        try:
            retry(kube.core(worker).read_namespaced_persistent_volume_claim, pvc, ns)
        except ApiException as e:
            raise HTTPException(409 if e.status == 404 else 502, f"checkpoint volume {pvc} is gone (successful runs release it); submit a new run")
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + sum(1 for j in list_ops(ns, labels[f"{LABEL}/tenant"], None, 500) if j["metadata"].get("annotations", {}).get(f"{LABEL}/resumed-from") == root)
    new = f"{root}-r{n}"
    out = output_prefix(orig) or ""
    d = dict(defaults)
    d["output_prefix"] = out.rsplit("/", 1)[0] + "/" + new if out else f"operations/{new}"
    pl = dict(placement or {})
    if is_manager_job(orig):
        pl["manager"] = True
    # a run with per-class images keeps the class it was submitted with (its checkpoints were written by that image)
    job = build_run(new, model, op["input"], labels[f"{LABEL}/tenant"], ann.get(f"{LABEL}/name"), None, op["timeout_s"],
                    labels.get(f"{LABEL}/priority"), worker, d, ann.get(f"{LABEL}/key"), pvc=pvc, placement=pl or None,
                    gpu_class=labels.get(f"{LABEL}/gpu-class") or (gpu_classes(model)[0] if class_images(model) else None))
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = root
    if is_jobset(job):          # the shared checkpoint directory is keyed by the root operation
        for c in job["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"]:
            for m in c.get("volumeMounts", []):
                if m["name"] == "checkpoints":
                    m["subPath"] = f"checkpoints/{root}"
    return create(ns, job, region)
