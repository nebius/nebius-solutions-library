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
import logging

import urllib3
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import execution
import kube
from config import (
    ENDPOINT_CONNECT_TO,
    EXECUTOR_SA,
    INFINIBAND_CLAIM_TEMPLATE,
    INFINIBAND_ENV,
    JOB_BACKOFF_LIMIT,
    JOB_PVC_SIZE_GI,
    JOB_TTL_S,
    JOBSET_GROUP,
    JOBSET_NAME_LABEL,
    JOBSET_PLURAL,
    JOBSET_VERSION,
    LABEL,
    LITELLM_URL,
    MULTIKUEUE_MANAGED_BY,
    MULTINODE_MAX_NODES,
    REGION,
    RUNNER_IMAGE,
    S3_ENV_SECRET,
    SHARED_SCRATCH_PVC,
    WEIGHTS_SHARED_PVC,
)
from pod_metadata import normalise as normalise_pod_metadata
from render import (
    CONTAINER_HARDENING,
    POD_SECURITY_CONTEXT,
    PRIORITY_CLASS,
    RUNNER_SECURITY_CONTEXT,
    _job_shell,
    _meta,
    _render,
    _uploader,
    class_images,
    gpu_classes,
    image_for_class,
    profile_of,
)
from resilience import retry
from status import is_jobset, is_manager_job, is_mirror, normalise

log = logging.getLogger("api.jobs")


def _mount_data(r: dict, pod: dict, containers: list) -> None:
    """`job.data` (docs/JOBS.md "Shared data"): a directory of the tenant's area on the region's shared filesystem
    (the tenant's RWX claim, the one `checkpoints: shared` uses), mounted read-only at `dataMountPath` (default /data)
    unless `dataReadOnly` is false (a seeding run). An empty path renders nothing: the generic run classes expose it
    as a parameter."""
    path = str(r.get("data") or "").strip("/")
    if not path:
        return
    if ".." in path.split("/"):
        raise HTTPException(400, "data: a relative directory, no parent references")
    ro = str(r.get("dataReadOnly", True)).lower() not in ("false", "0", "no")
    pod["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC, "readOnly": ro}})
    for c in containers:
        c["volumeMounts"].append(
            {
                "name": "data",
                "mountPath": str(r.get("dataMountPath") or "/data"),
                "subPath": f"data/{path}",
                "readOnly": ro,
            }
        )


def placement_regions(job: dict) -> list[str]:
    """The regions a rendered run may land in (the pod template's annotation the dispatcher reads), for a Job or a
    JobSet (whose pod template sits under its replicated Job)."""
    tmpl = job.get("spec", {}).get("template")
    if tmpl is None and is_jobset(job):
        tmpl = (job["spec"].get("replicatedJobs") or [{}])[0].get("template", {}).get("spec", {}).get("template", {})
    ann = ((tmpl or {}).get("metadata") or {}).get("annotations") or {}
    return [r for r in (ann.get(f"{LABEL}/regions") or "").split(",") if r]


def _mount_weights(r: dict, pod: dict, containers: list) -> None:
    """`job.weights` (docs/JOBS.md "Model weights for runs"): a directory of the region's shared weights filesystem (the
    one endpoints cache into), mounted read-only at `weightsMountPath` (default /weights). Empty: nothing."""
    path = str(r.get("weights") or "").strip("/")
    if not path:
        return
    if ".." in path.split("/"):
        raise HTTPException(400, "weights: a relative directory, no parent references")
    pod["volumes"].append(
        {"name": "weights", "persistentVolumeClaim": {"claimName": WEIGHTS_SHARED_PVC, "readOnly": True}}
    )
    for c in containers:
        c["volumeMounts"].append(
            {
                "name": "weights",
                "mountPath": str(r.get("weightsMountPath") or "/weights"),
                "subPath": path,
                "readOnly": True,
            }
        )


def build_run(
    name: str,
    model: dict,
    params: dict,
    tenant: str,
    label: str | None,
    idem: str | None,
    timeout_s: int | None,
    priority: str | None,
    region: str | None = REGION,
    defaults: dict | None = None,
    key_hash: str | None = None,
    pvc: str | None = None,
    placement: dict | None = None,
    gpu_class: str | None = None,
    ex: dict | None = None,
) -> dict:
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
            raise HTTPException(
                500, f"model {model['name']} has per-class images; the GPU class must be chosen at submission"
            )
        if gpu_class not in gpu_classes(model):
            raise HTTPException(
                400, f"GPU class {gpu_class!r} is not one of {gpu_classes(model)} for model {model['name']}"
            )
        job["image"] = image_for_class(model, gpu_class)
        job.pop("images", None)
    else:
        gpu_class = None
    if not job.get("image") or not job.get("command"):
        raise HTTPException(500, f"model {model['name']} has no job.image/job.command")
    from models import normalise_image  # a caller-supplied image (container-run) goes through the region's cache too

    job["image"] = normalise_image(str(job["image"]))
    declared = {p["name"]: p.get("default") for p in model.get("parameters", [])}
    declared.update({k: None for k in (defaults or {})})
    declared.update({"input_prefix": None, "output_prefix": None})
    unknown = [k for k in params if k not in declared]
    if unknown:
        raise HTTPException(400, f"unknown parameters {unknown}; model accepts {sorted(declared)}")
    values = {k: v for k, v in declared.items() if v is not None}
    values.update({k: v for k, v in (defaults or {}).items() if v is not None})
    values.update(params)
    missing = [
        p["name"] for p in model.get("parameters", []) if p.get("required") and values.get(p["name"]) in (None, "")
    ]
    if missing:
        raise HTTPException(400, f"missing required parameters {missing}")
    values["operation"] = name
    r = _render(job, values)
    r["image"] = normalise_image(str(r.get("image") or ""))  # after the parameters: `{{image}}` is now a reference
    if argument_parameter := job.get("argsParameter"):
        arguments = values.get(argument_parameter, [])
        if not isinstance(arguments, list) or len(arguments) > 100 or not all(isinstance(a, str) for a in arguments):
            raise HTTPException(400, "args must be a list of at most 100 strings")
        r["args"] = arguments
    if environment_parameter := job.get("envParameter"):
        import re

        environment = values.get(environment_parameter, {})
        if (
            not isinstance(environment, dict)
            or len(environment) > 100
            or not all(
                isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and isinstance(v, str)
                for k, v in environment.items()
            )
        ):
            raise HTTPException(400, "env must map valid environment names to string values (at most 100)")
        r["env"] = {**environment, **(r.get("env") or {})}
    cmd = r["command"]
    if isinstance(cmd, str):
        # Shell strings still receive typed arguments as literal argv values, not shell syntax.
        cmd = ["/bin/sh", "-c", cmd + ' "$@"', "--"] if r.get("args") else ["/bin/sh", "-c", cmd]
    gpus = int(r.get("gpu", 0) or 0)
    res = {
        "requests": {"cpu": str(r.get("cpu", "1")), "memory": str(r.get("memory", "2Gi"))},
        "limits": {"memory": str(r.get("memoryLimit", r.get("memory", "2Gi")))},
    }
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
    main = {
        "name": "main",
        "image": r["image"],
        "command": cmd,
        "resources": res,
        "workingDir": r.get("workDir", "/work"),
        "volumeMounts": [{"name": "work", "mountPath": "/work"}, {"name": "shm", "mountPath": "/dev/shm"}],
        "securityContext": {**CONTAINER_HARDENING, **({"runAsNonRoot": True} if r.get("runAsNonRoot") else {})},
    }
    if r.get("envFrom"):
        main["envFrom"] = r["envFrom"]
    if r.get("args"):
        main["args"] = r["args"]
    if r.get("env"):
        main["env"] = [{"name": k, "value": str(v)} for k, v in r["env"].items()]
    if r.get("s3Env", True):
        main["envFrom"] = (main.get("envFrom") or []) + [
            {"secretRef": {"name": S3_ENV_SECRET}}
        ]  # next to env_secrets, not instead
    fetch = {
        "name": "fetch",
        "image": RUNNER_IMAGE,
        "command": ["/usr/local/bin/fetch.sh"],
        "env": [{"name": "INPUT_PREFIX", "value": r.get("inputs") or values.get("input_prefix") or ""}],
        "envFrom": [{"secretRef": {"name": S3_ENV_SECRET}}],
        "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
        "securityContext": RUNNER_SECURITY_CONTEXT,
    }
    pod = {
        "restartPolicy": "Never",
        "serviceAccountName": EXECUTOR_SA,
        "terminationGracePeriodSeconds": int(r.get("graceSeconds", 120)),
        "priorityClassName": PRIORITY_CLASS.get(priority, "serverless2-batch"),
        # the runner containers run as uid 10001; the volume is group-owned by them so fetch/upload work
        # whatever user the model image runs as (root-created files stay group-readable)
        "securityContext": {"fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch", **POD_SECURITY_CONTEXT},
        # shared PID namespace: the command's shell is not PID 1, so SIGTERM (preemption, cancel) actually
        # terminates `sh -c ...` commands instead of being ignored until the grace period's SIGKILL
        "shareProcessNamespace": True,
        "initContainers": [fetch],
        "containers": [
            main,
            _uploader(
                name,
                region,
                values["output_prefix"],
                None if local_nvme else pvc,
                r.get("uploadExcludes", ""),
                gpus,
                placement,
                gpu_class,
            ),
        ],
        "volumes": [
            {"name": "work", "emptyDir": {"sizeLimit": f"{r.get('pvcSizeGi', JOB_PVC_SIZE_GI)}Gi"}}
            if local_nvme
            else {"name": "work", "persistentVolumeClaim": {"claimName": pvc}},
            {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": r.get("shm", "8Gi")}},
        ],
    }
    if gpus:
        pod["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    terms = []
    if r.get("pool"):
        pod["nodeSelector"] = {f"{LABEL}/pool": r["pool"]}
    elif gpus and gpu_classes(model):
        # the preference queue may fall back to another GPU class; keep it to the classes the image runs on
        # (Kueue assigns only flavors whose node labels satisfy the affinity, docs/SCHEDULING.md). With per-class
        # images the image is the chosen class's, so only that class's pools qualify.
        # the run's own class list when the caller narrowed it (execution.placement.gpu_preference), else the model's
        pools = kube.class_pools([gpu_class] if gpu_class else ((placement or {}).get("classes") or gpu_classes(model)))
        if pools:
            terms.append({"key": f"{LABEL}/pool", "operator": "In", "values": pools})
    if local_nvme:
        terms.append({"key": f"{LABEL}/local-nvme", "operator": "In", "values": ["true"]})
        # only regions with an NVMe pool are candidates for the dispatcher: Kueue ignores an affinity key that
        # no flavor of a cluster declares, so a worker without such a pool would admit the run and leave its pod
        # Pending for ever (the flavor of a local_nvme pool carries the label)
        nvme_regions = sorted(
            {p["region"] for p in (kube.fleet().get("pools") or {}).values() if p.get("local_nvme") in (True, "true")}
        )
        if kube.fleet().get("pools") and not nvme_regions:
            raise HTTPException(
                400,
                'scratch local-nvme needs a pool with local_nvme = true; this fleet has none (docs/FLEET.md "Local NVMe")',
            )
        if placement is not None and nvme_regions:
            wanted = placement.get("regions") or nvme_regions
            keep = [r for r in wanted if r in nvme_regions]
            if not keep:
                raise HTTPException(
                    400,
                    f"scratch local-nvme: none of the regions {wanted} has a pool with local_nvme = true (regions with one: {nvme_regions})",
                )
            placement = {**placement, "regions": keep}
    if terms:
        pod["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": terms}]}
            }
        }
    if r.get("imagePullSecret"):
        pod["imagePullSecrets"] = [{"name": r["imagePullSecret"]}]
    # the queue follows the class actually asked for: the pinned class, else the first of the run's (narrowed) class
    # list, else the model's own profile; a gpu_preference without per-class images used to keep the model's queue
    # and so landed every run on the model's first class
    classes_here = (placement or {}).get("classes") or []
    profile = (
        f"prefer-{gpu_class}"
        if gpu_class
        else (f"prefer-{classes_here[0]}" if classes_here else ((placement or {}).get("profile") or profile_of(model)))
    )
    if gpu_class and placement:
        placement = {
            **placement,
            "profile": profile,
            "classes": [gpu_class],
        }  # the dispatcher keeps the run on this class
    meta = _meta(
        name, tenant, model["name"], "run", region, label, params, idem, priority, key_hash, profile, gpu_class, ex
    )
    if model.get("version") is not None:
        meta["annotations"][f"{LABEL}/model-version"] = str(model["version"])  # the revision this run is rendered from
    rates = {}
    for pool in kube.fleet().get("pools", {}).values():
        pool_region = kube.cluster_region(pool["region"])
        rate = (model.get("deployments", {}).get(pool_region) or {}).get(
            "price_per_gpu_hour", pool.get("usd_per_gpu_hour")
        )
        if rate is not None:
            rates.setdefault(pool_region, {})[pool["pool"]] = rate
    meta["annotations"][f"{LABEL}/billing-rates"] = json.dumps(rates, sort_keys=True)
    meta["annotations"][f"{LABEL}/scratch"] = scratch
    if gpu_class:
        meta["annotations"][f"{LABEL}/image"] = job["image"]
    nodes = int(r.get("nodes", 1) or 1)
    if ex:
        execution.apply_run(pod, r, ex, name, S3_ENV_SECRET)  # io mode, identity, private input, profile (docs/JOBS.md)
    if nodes > 1:
        return _multinode(name, model, r, values, pod, nodes, gpus, meta, timeout_s, placement)
    _mount_data(r, pod, [main])
    _mount_weights(r, pod, [main])
    if not local_nvme:
        meta["annotations"][f"{LABEL}/pvc"] = pvc
        meta["annotations"][f"{LABEL}/pvc-size-gi"] = str(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    job = _job_shell(name, meta, pod, timeout_s, placement, model.get("podMetadata"))
    if (fz := r.get("finalize")) and fz.get("onExitCodes"):
        # "compute done, delivery pending" ends the Job at once: the GPU computation is never restarted by the Job's
        # backoff for a delivery problem; re-delivery is the model's finalizer through :export (bounded there)
        job["spec"]["podFailurePolicy"]["rules"].insert(
            0,
            {
                "action": "FailJob",
                "onExitCodes": {"containerName": "main", "operator": "In", "values": list(fz["onExitCodes"])},
            },
        )
    if ex:
        execution.apply_job(job, ex)
    return job


# ---------------------------------------------------------------------------------------------------------------
# Multi-node runs (docs/JOBS.md "Multi-node runs"): a job class with `nodes: N` (N > 1) renders a JobSet of one
# indexed Job with N pods, one per node (a full node each: `gpu` = the preset's GPU count), a headless Service
# for rank discovery, torchrun/NCCL environment, and restart-all on any pod loss (spot preemption). `interconnect`
# (none | preferred | required) decides whether the pods must land on an InfiniBand pool (fleet.yaml pools with
# `interconnect: infiniband`, Terraform GPU cluster) and get the fabric NICs through a DRA ResourceClaimTemplate.
INTERCONNECT = ("none", "preferred", "required")


def _multinode(
    name: str,
    model: dict,
    r: dict,
    values: dict,
    pod: dict,
    nodes: int,
    gpus: int,
    meta: dict,
    timeout_s: int | None,
    placement: dict | None,
) -> dict:
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
    kept = [
        t
        for t in (
            ((pod.get("affinity") or {}).get("nodeAffinity") or {}).get(
                "requiredDuringSchedulingIgnoredDuringExecution"
            )
            or {}
        )
        .get("nodeSelectorTerms", [{}])[0]
        .get("matchExpressions", [])
        if t.get("key") == f"{LABEL}/local-nvme"
    ]
    pools: list[str] = []
    if classes and gpus:
        pools = kube.class_pools(classes, gpus_per_node=gpus, interconnect="infiniband" if ib else None)
        if not pools and kube.fleet()["pools"]:
            raise HTTPException(
                400,
                f"no {'InfiniBand ' if ib else ''}pool with {gpus}-GPU nodes for classes {classes} in the fleet"
                + (
                    " (multi-node runs need the fabric: pass interconnect: none to run NCCL over TCP on purpose)"
                    if ib
                    else ""
                )
                + " (fleet.yaml pools, docs/FLEET.md)",
            )
    terms = kept + ([{"key": f"{LABEL}/pool", "operator": "In", "values": pools}] if pools else [])
    if terms:
        pod["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": terms}]}
            }
        }
    elif "affinity" in pod:
        del pod["affinity"]
    ib_count = int(r.get("ibDevicesPerNode") or kube.ib_devices_per_node(pools))
    if ib:
        pod.setdefault("affinity", {}).setdefault("nodeAffinity", {}).setdefault(
            "requiredDuringSchedulingIgnoredDuringExecution", {"nodeSelectorTerms": [{"matchExpressions": []}]}
        )
        pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0][
            "matchExpressions"
        ].append({"key": f"{LABEL}/interconnect", "operator": "In", "values": ["infiniband"]})
    elif interconnect == "preferred":
        pod.setdefault("affinity", {}).setdefault("nodeAffinity", {})[
            "preferredDuringSchedulingIgnoredDuringExecution"
        ] = [
            {
                "weight": 100,
                "preference": {
                    "matchExpressions": [{"key": f"{LABEL}/interconnect", "operator": "In", "values": ["infiniband"]}]
                },
            }
        ]
    # /work per pod: node-local scratch (the kubelet's ephemeral storage: local NVMe on pools that have it), not a
    # per-run RWO volume (N pods on N nodes). Checkpoints: `shared` = the tenant's RWX claim on the cluster's
    # shared filesystem (survives a restart and a :resume), `local` = under /work, gone with the pod.
    disk = int(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    pod["volumes"] = [{"name": "work", "emptyDir": {"sizeLimit": f"{disk}Gi"}}] + [
        v for v in pod["volumes"] if v["name"] != "work"
    ]
    root = meta["annotations"].get(f"{LABEL}/resumed-from") or name
    if checkpoints == "shared":
        pod["volumes"].append({"name": "checkpoints", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC}})
        for c in (main, uploader):
            c["volumeMounts"].append(
                {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{root}"}
            )
    _mount_data(r, pod, [main])
    _mount_weights(r, pod, [main])
    rank = {
        "name": "NODE_RANK",
        "valueFrom": {"fieldRef": {"fieldPath": "metadata.annotations['batch.kubernetes.io/job-completion-index']"}},
    }
    dist = {
        "NNODES": str(nodes),
        "GPUS_PER_NODE": str(gpus),
        "WORLD_SIZE": str(nodes * max(gpus, 1)),
        "MASTER_ADDR": f"{name}-workers-0-0.{name}",
        "MASTER_PORT": "29500",
        "CHECKPOINT_DIR": "/work/checkpoint",
    }
    if ib:
        dist.update(INFINIBAND_ENV)
    env = {e["name"]: e for e in main.get("env", [])}
    for k, v in dist.items():
        env.setdefault(k, {"name": k, "value": v})  # the job class's own env wins
    main["env"] = [rank] + list(env.values())
    uploader["env"] = [e for e in uploader["env"] if e["name"] != "PVC_NAME"] + [
        rank,
        {"name": "UPLOAD_SCOPE", "value": "rank0"},
    ]
    if r.get("uploadExcludes") is None:
        uploader["env"].append(
            {"name": "UPLOAD_EXCLUDES", "value": "--exclude checkpoint/*"}
        )  # checkpoints are not results
    if ib:
        # every InfiniBand NIC of the node through DRA (DeviceClass ib.networking.nebius.ai, DraNet on the node image).
        # No capability: RDMA pins memory, which RLIMIT_MEMLOCK bounds, and the GPU nodes' container runtime runs
        # with an unlimited memlock limit (cloud-init + node-config), so `drop: [ALL]` under PSA baseline holds
        # (docs/JOBS.md "Multi-node runs").
        pod["resourceClaims"] = [{"name": "ib", "resourceClaimTemplateName": f"{INFINIBAND_CLAIM_TEMPLATE}-{ib_count}"}]
        main["resources"]["claims"] = [{"name": "ib"}]
    pod["subdomain"] = name
    execution.scope_runner_token(pod)
    extra = normalise_pod_metadata(model.get("podMetadata"))
    tmpl_meta = {
        "labels": {**extra.get("labels", {}), **{k: v for k, v in meta["labels"].items() if k.startswith(LABEL)}}
    }
    ann = dict(extra.get("annotations", {}))
    if placement and placement.get("classes"):
        ann[f"{LABEL}/gpu-classes"] = ",".join(placement["classes"])
    if placement and placement.get("regions"):
        ann[f"{LABEL}/regions"] = ",".join(placement["regions"])
    if ann:
        tmpl_meta["annotations"] = ann
    job_spec = {
        "parallelism": nodes,
        "completions": nodes,
        "completionMode": "Indexed",
        "backoffLimit": 0,
        "template": {"metadata": tmpl_meta, "spec": pod},
    }
    if timeout_s:
        job_spec["activeDeadlineSeconds"] = timeout_s
    meta["annotations"][f"{LABEL}/nodes"] = str(nodes)
    meta["annotations"][f"{LABEL}/interconnect"] = interconnect
    meta["annotations"][f"{LABEL}/checkpoints"] = checkpoints
    spec = {
        "network": {"enableDNSHostnames": True, "subdomain": name, "publishNotReadyAddresses": True},
        "successPolicy": {"operator": "All", "targetReplicatedJobs": ["workers"]},
        # any pod loss (preemption, node loss, application failure) fails the indexed Job (backoffLimit 0) and the
        # JobSet recreates every pod: ranks restart together from the last checkpoint
        "failurePolicy": {"maxRestarts": JOB_BACKOFF_LIMIT, "restartStrategy": "Recreate"},
        "ttlSecondsAfterFinished": JOB_TTL_S,
        "replicatedJobs": [
            {
                "name": "workers",
                "replicas": 1,
                "template": {"metadata": {"labels": dict(tmpl_meta["labels"])}, "spec": job_spec},
            }
        ],
    }
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
    return {
        "apiVersion": js.get("apiVersion"),
        "kind": "JobSet",
        "metadata": js.get("metadata", {}),
        "spec": {
            "managedBy": spec.get("managedBy"),
            "suspend": spec.get("suspend"),
            "template": rj.get("template", {}),
            "activeDeadlineSeconds": rj.get("activeDeadlineSeconds"),
            "parallelism": rj.get("parallelism"),
        },
        "status": {"conditions": conds, "restarts": (js.get("status") or {}).get("restarts")},
    }


def _jobset_api(region: str):
    return kube.api(region)


def read_jobset(ns: str, name: str, region: str) -> dict:
    return _jobset_as_job(
        retry(_jobset_api(region).get_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, name)
    )


def list_jobsets(ns: str, region: str, selector: str) -> list[dict]:
    items = retry(
        _jobset_api(region).list_namespaced_custom_object,
        JOBSET_GROUP,
        JOBSET_VERSION,
        ns,
        JOBSET_PLURAL,
        label_selector=selector,
    ).get("items", [])
    return [_jobset_as_job(j) for j in items]


def patch_jobset(ns: str, name: str, region: str, body: dict) -> dict:
    return _jobset_as_job(
        retry(
            _jobset_api(region).patch_namespaced_custom_object,
            JOBSET_GROUP,
            JOBSET_VERSION,
            ns,
            JOBSET_PLURAL,
            name,
            body,
        )
    )


def delete_jobset(ns: str, name: str, region: str) -> None:
    retry(_jobset_api(region).delete_namespaced_custom_object, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, name)


def pods_selector(job: dict) -> str:
    return f"{JOBSET_NAME_LABEL}={job['metadata']['name']}" if is_jobset(job) else f"job-name={job['metadata']['name']}"


def build_async(
    name: str,
    model: dict,
    inp: dict,
    tenant: str,
    key: str,
    label: str | None,
    idem: str | None,
    timeout_s: int | None,
    output_prefix: str,
    region: str = REGION,
    ex: dict | None = None,
    key_hash: str | None = None,
    admitted: str | None = None,
    pvc: str | None = None,
    transfer_only: bool = False,
) -> tuple[dict, dict]:
    """(Job, Secret): a queued endpoint call through LiteLLM's pass-through route when the model has
    one (spend lands on the caller's key), else through the gateway of the endpoint's region with the caller's key
    (TLS, key check, per-key rate limit: the same path as an external client; never the predictor
    Service, so no call skips the key check or the accounting). The Job runs in that region (services/api/app.py
    `invoke` creates it there). The key goes into a per-operation Secret owned by the Job, never into the Job spec;
    `admitted` (the API's admission token) goes next to it, so the gateway hook counts the call once.
    `ex` (docs/API.md "Endpoint calls"): private input through a binding, application-owned output, the capture on a
    retained work volume (`<name>-work`); `transfer_only` re-renders the caller on the original's volume (`pvc`) to
    deliver a complete capture again without calling the endpoint."""
    import models as runtime_models

    env = []
    if model.get("litellm_route"):
        url, auth = LITELLM_URL + model["litellm_route"], f"Bearer {key}"
    else:
        if not runtime_models.endpoint_domain(region):
            raise HTTPException(503, f"async calls need the endpoint domain of {region} (ENDPOINT_DOMAINS)")
        url, auth = f"https://{runtime_models.endpoint_host(model, region)}{model['endpoint_path']}", f"Bearer {key}"
        if ENDPOINT_CONNECT_TO:
            host = url.split("/")[2]
            env.append({"name": "CONNECT_TO", "value": f"{host}:443:{ENDPOINT_CONNECT_TO}"})
    secret_name = f"{name}-auth"
    if (
        model.get("protocol") == "openai"
        and model.get("served_model")
        and isinstance(inp, dict)
        and not inp.get("model")
    ):
        inp = {**inp, "model": model["served_model"]}  # callers know the catalog id, not the served name
    env += [
        {"name": "URL", "value": url},
        {"name": "OPERATION", "value": name},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {
            "name": "AUTH_HEADER",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "authorization", "optional": True}},
        },
        {"name": "CALL_TIMEOUT", "value": str(min(timeout_s or 3600, 3600) if not ex else (timeout_s or 3600))},
    ]
    if ex:  # the request lives in the binding, never in the Job (docs/API.md "Endpoint calls")
        inp = {}
        if transfer_only:
            env.append({"name": "TRANSFER_ONLY", "value": "1"})
        elif not model.get("litellm_route"):
            # the readiness probe before the one inference POST: the endpoint's health path, else an idempotent GET
            # on the OpenAI models listing or the endpoint's root, through the same gateway
            base = url[: len(url) - len(model["endpoint_path"])] if url.endswith(model["endpoint_path"]) else url
            probe = model.get("health_path") or ("/v1/models" if model.get("protocol") == "openai" else "/")
            env.append({"name": "READY_URL", "value": base + probe})
    else:
        env.append({"name": "REQUEST_BODY", "value": json.dumps(inp)})
    if admitted:
        env.append(
            {
                "name": "ADMITTED",
                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "admitted", "optional": True}},
            }
        )
    main = {
        "name": "main",
        "image": RUNNER_IMAGE,
        "command": ["/usr/local/bin/call.sh"],
        "env": env,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
        "securityContext": RUNNER_SECURITY_CONTEXT,
    }
    work = pvc or (f"{name}-work" if ex else None)
    pod = {
        "restartPolicy": "Never",
        "serviceAccountName": EXECUTOR_SA,
        "terminationGracePeriodSeconds": 60,
        "priorityClassName": "serverless2-batch",
        # the runner (uid/gid 10001) writes the work volume (a claim's root is root-owned until the kubelet applies the
        # group) and reads the private binding (a Secret volume, mode 0440): the file-system group makes both its, as
        # for a run's pod (build_run)
        "securityContext": {"fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch", **POD_SECURITY_CONTEXT},
        # shared PID namespace, as a run's pod: the uploader records `started` only once it sees the caller's process
        # (upload.sh main_alive) and `finished` when it is gone; in separate namespaces it never sees either and the
        # attempt is never opened (the application's refresh broker then refuses the caller for good: a deadlock)
        "shareProcessNamespace": True,
        "containers": [main, _uploader(name, region, output_prefix, work)],
        "volumes": [
            {"name": "work", "persistentVolumeClaim": {"claimName": work}}
            if work
            else {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}}
        ],
    }
    if ex:
        execution.apply_async(pod, ex, name)
    meta = _meta(name, tenant, model["name"], "async", region, label, inp, idem, None, key_hash, execution=ex)
    if work:  # the capture is retained for a transfer-only export retry (create() makes the claim with the Job)
        meta["annotations"][f"{LABEL}/pvc"] = work
        meta["annotations"][f"{LABEL}/pvc-size-gi"] = str(ASYNC_PVC_SIZE_GI)
    job = _job_shell(name, meta, pod, timeout_s)
    job["spec"]["backoffLimit"] = 6
    # a 4xx from the endpoint is the caller's error (call.sh exit 2): fail the Job, no pod retries; with application
    # I/O also the endpoint's own error (exit 5), an unknown inference outcome (exit 4) and nothing to export (exit 3):
    # never a second inference POST
    job["spec"]["podFailurePolicy"]["rules"].insert(
        0,
        {
            "action": "FailJob",
            "onExitCodes": {"containerName": "main", "operator": "In", "values": [2, 3, 4, 5] if ex else [2]},
        },
    )
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "labels": {f"{LABEL}/tenant": tenant}},
        "stringData": {"authorization": auth, **({"admitted": admitted} if admitted else {})},
    }
    return job, secret


ASYNC_PVC_SIZE_GI = 2


def export_async(ns: str, name: str, region: str, model: dict, admit=None) -> tuple[dict, bool]:
    """Transfer-only retry of an endpoint call with application-owned output (docs/API.md "Endpoint calls"): the
    caller rendered again on the original's retained work volume with TRANSFER_ONLY=1, so it delivers the complete
    capture once more and never calls the endpoint; `<root>-x<n>` like a run's export retry."""
    orig = get(ns, name, region)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    pvc = ann.get(f"{LABEL}/pvc")
    try:
        ex = json.loads(ann.get(f"{LABEL}/execution") or "null")
    except ValueError:
        ex = None
    if not pvc or not ex or ex.get("io_mode") != "application":
        raise HTTPException(409, f"operation {name} kept no capture to export (not an application-I/O endpoint call)")
    try:
        retry(kube.core(region).read_namespaced_persistent_volume_claim, pvc, ns)
    except ApiException as e:
        raise HTTPException(409 if e.status == 404 else 502, f"work volume {pvc} is gone; nothing to export")
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + sum(
        1 for j in list_ops(ns, labels[f"{LABEL}/tenant"], None, 500) if j["metadata"]["name"].startswith(f"{root}-x")
    )
    if n > EXPORT_RETRIES_MAX:
        raise HTTPException(409, f"operation {root}: {EXPORT_RETRIES_MAX} export retries used; submit a new call")
    new = f"{root}-x{n}"
    auth_secret = _secret_copy(ns, f"{name}-auth", region)
    job, secret = build_async(
        new,
        model,
        {},
        labels[f"{LABEL}/tenant"],
        "",
        ann.get(f"{LABEL}/name"),
        None,
        3600,
        "",
        region,
        ex=ex,
        key_hash=ann.get(f"{LABEL}/key"),
        pvc=pvc,
        transfer_only=True,
    )
    secret["stringData"] = auth_secret  # the original's key and admission token, never a new one
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = root
    job["metadata"]["annotations"][f"{LABEL}/export-of"] = name
    _inherit_owner(job, ann)
    if admit:
        admit(job)
    return create(ns, job, region, secret)


def _secret_copy(ns: str, name: str, region: str) -> dict:
    try:
        s = retry(kube.core(region).read_namespaced_secret, name, ns)
    except ApiException as e:
        raise HTTPException(
            409 if e.status == 404 else 502, f"the call's credentials Secret {name} is gone; submit a new call"
        )
    import base64

    raw = s if isinstance(s, dict) else kube.core(region).api_client.sanitize_for_serialization(s)
    if raw.get("stringData"):
        return dict(raw["stringData"])
    return {k: base64.b64decode(v).decode() for k, v in (raw.get("data") or {}).items()}


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


def output_prefix(job: dict) -> str | None:
    """The uploader's OUTPUT_PREFIX: s3://<bucket>/operations/<id>; names the bucket of the run's artifacts."""
    for c in job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        if c.get("name") == "uploader":
            return next((e.get("value") for e in c.get("env", []) if e.get("name") == "OUTPUT_PREFIX"), None)
    return None


def _inherit_owner(job: dict, ann: dict) -> None:
    """A continuation belongs to the original's principal: the owner annotation follows it onto the successor (the
    durable record is by root; the annotation keeps the fence intact on an API without the database)."""
    owner = ann.get(f"{LABEL}/owner")
    if owner:
        job["metadata"]["annotations"][f"{LABEL}/owner"] = owner
    import stages

    stages.inherit(job, ann)  # a stage's continuation stays that stage (lineage, dependency results)


def operation_owner(ann: dict, root: str) -> str | None:
    """The stable principal an operation belongs to: its own annotation (the submitting call writes it), else the
    root's durable record (continuations are rendered without it)."""
    owner = ann.get(f"{LABEL}/owner")
    if owner:
        return owner
    import db
    import keys

    if db.enabled():
        rec = keys.operation_owner(root)
        if rec:
            return rec[1]
    return None


def create(ns: str, job: dict, region: str = REGION, secret: dict | None = None) -> tuple[dict, bool]:
    """Returns (job, created). AlreadyExists -> the existing one (idempotent replay). The per-operation
    PVC and Secret are owned by the Job (garbage-collected with it). A manager Job gets no PVC here: the
    dispatcher creates it on the worker it picks (docs/JOBS.md)."""
    name = job["metadata"]["name"]
    import hashlib

    request_hash = hashlib.sha256(json.dumps(job["spec"], sort_keys=True).encode()).hexdigest()
    job["metadata"].setdefault("annotations", {})[f"{LABEL}/request-hash"] = request_hash
    # Every admission path (initial, resume, fallback, export retry) must deliver private inputs before Kueue
    # can start the Job. Keeping this here prevents a new path from creating an unstartable successor.
    ann = job["metadata"]["annotations"]
    ex = json.loads(ann.get(f"{LABEL}/execution") or "null")
    root = ann.get(f"{LABEL}/resumed-from") or name
    if job["metadata"].get("labels", {}).get(f"{LABEL}/stage-waiting") != "true":
        # a waiting stage (docs/JOBS.md "Stage composition") gets its binding from the gate when its dependencies
        # delivered: its request-scoped credentials are not consumed while it waits
        execution.deliver(
            ex,
            name,
            root,
            ns,
            job["metadata"]["labels"][f"{LABEL}/tenant"],
            placement_regions(job) or [region],
            owner=operation_owner(ann, root),
        )
    was_created = True
    try:
        if is_jobset(job):
            retry(
                _jobset_api(region).create_namespaced_custom_object,
                JOBSET_GROUP,
                JOBSET_VERSION,
                ns,
                JOBSET_PLURAL,
                job,
            )
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
        return created, was_created  # no per-run volume: /work is node-local, checkpoints on the shared claim
    owner = {"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": created["metadata"].get("uid", "")}
    ann, core = job["metadata"].get("annotations", {}), kube.core(region)
    pvc = None if is_manager_job(job) else ann.get(f"{LABEL}/pvc")
    if pvc and (not ann.get(f"{LABEL}/resumed-from") or pvc == f"{name}-work"):
        # A portable resume gets a fresh volume, unlike a same-region resume that reuses the old one.
        body = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc, "ownerReferences": [owner], "labels": {f"{LABEL}/operation": name}},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": f"{ann[f'{LABEL}/pvc-size-gi']}Gi"}},
            },
        }
        _create_ignore_exists(core.create_namespaced_persistent_volume_claim, ns, body, region)
    elif pvc:
        # a resumed run co-owns the volume, so it outlives the original Job's TTL or deletion
        try:
            cur = core.api_client.sanitize_for_serialization(
                retry(core.read_namespaced_persistent_volume_claim, pvc, ns)
            )
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
            job = read_jobset(ns, name, region)  # a multi-node run
        except ApiException as e2:
            raise HTTPException(404 if e2.status in (403, 404) else 502, f"operation {name}: {e2.reason}")
    if is_mirror(job):
        raise HTTPException(404, f"operation {name}: not found")  # the operation is the manager's Job
    if is_manager_job(job):
        return _with_worker(job, ns, kube.workload_clusters(ns))
    return _with_pods(job, ns, region)


def _with_worker(job: dict, ns: str, clusters: dict) -> dict:
    """A manager Job: pods (attempts) come from the worker that admitted its Workload; nothing yet while
    the dispatcher still has to place it. `_region` is the worker's region or None."""
    cluster = clusters.get(job["metadata"].get("uid", ""))
    if not cluster:
        job["_pods"], job["_region"], job["_cluster"] = [], None, None
        try:
            job["_reason"] = kube.workload_reasons(ns).get(job["metadata"].get("uid", ""))
        except Exception:  # noqa: BLE001 - advisory
            job["_reason"] = None
        return job
    region = kube.cluster_region(cluster)
    try:
        out = _with_pods(job, ns, region)
    except HTTPException:  # the worker is unreachable (no kubeconfig here, its API down): status only
        job["_pods"], job["_region"], job["_pods_unknown"] = [], region, True
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
        except (urllib3.exceptions.HTTPError, OSError) as e:
            log.warning("operation %s: region %s unreachable, skipped: %s", name, r, str(e)[:160])
    raise HTTPException(404, f"operation {name}: not found")


def list_ops(ns: str, tenant: str, model: str | None, limit: int) -> list[dict]:
    sel = f"{LABEL}/tenant={tenant}" + (f",{LABEL}/model={model}" if model else "")
    items, by_job, managers = [], {}, []
    for r in kube.regions():
        try:
            b = kube.batch(r)
            jobs = [
                b.api_client.sanitize_for_serialization(j)
                for j in retry(b.list_namespaced_job, ns, label_selector=sel).items
            ]
            pods = [
                b.api_client.sanitize_for_serialization(p)
                for p in retry(kube.core(r).list_namespaced_pod, ns, label_selector=f"{LABEL}/tenant={tenant}").items
            ]
            jobs += list_jobsets(ns, r, sel)
        except ApiException as e:
            if e.status in (403, 404):  # tenant not onboarded in that region
                continue
            raise HTTPException(502, f"kubernetes ({r}): {e.reason}")
        except (urllib3.exceptions.HTTPError, OSError) as e:
            # a region that cannot be reached (gone, or its endpoint down) must not hide every other region's
            # operations (the console showed 500 for every tenant while a region was being replaced)
            log.warning("operations: region %s unreachable, skipped: %s", r, str(e)[:160])
            continue
        for p in pods:
            labels = p["metadata"].get("labels", {})
            by_job.setdefault((r, labels.get(JOBSET_NAME_LABEL) or labels.get("job-name", "")), []).append(p)
        for j in jobs:
            if is_mirror(j):
                continue  # the worker's copy of a manager Job: pods are joined below
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
            items.append(
                _with_pods(j, ns, region, by_job.get((region, j["metadata"]["name"]), []))
                if region
                else _with_worker(j, ns, {})
            )
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
            patched = retry(
                b.patch_namespaced_job, name, ns, {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}}
            )
        else:
            patched = retry(
                b.patch_namespaced_job,
                name,
                ns,
                {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}, "spec": {"activeDeadlineSeconds": 1}},
            )
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
            patched = patch_jobset(
                ns,
                name,
                region,
                {"metadata": {"annotations": {f"{LABEL}/cancelled": "true"}}, "spec": {"suspend": True}},
            )
    except ApiException as e:
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    out = _with_pods(patched, ns, job["_region"], job["_pods"])
    out["_cluster"] = job.get("_cluster")
    return out


EXPORT_RETRIES_MAX = 3


def _pin_to_worker(job: dict, orig: dict) -> None:
    """A retry on the original's kept volume must land on the cluster that holds it: the Job is pinned to the admitting
    cluster by its id (the dispatcher's `serverless2.nebius/region` label, Kueue copies it to the Workload) and the
    candidate list is that cluster alone (the `regions` annotation); a region name would only pin through the
    MultiKueueCluster alias and is ambiguous with two workers in one region. A run pinned to a region from the start
    (no manager Workload) keeps its region label."""
    cluster = orig.get("_cluster")
    if not cluster:
        return
    job["metadata"]["labels"][f"{LABEL}/region"] = cluster
    job["spec"]["template"]["metadata"].setdefault("annotations", {})[f"{LABEL}/regions"] = cluster


def export_again(
    ns: str, name: str, region: str, model: dict, defaults: dict, finalize: dict | None = None, admit=None
) -> tuple[dict, bool]:
    """`<root>-x<n>`: the original run's Job rendered again on its kept work volume with a main container that only
    exits 0, so the uploader exports /work once more (docs/JOBS.md "Attempts and finalization"). With `finalize`
    (docs/JOBS.md "Finalization by the application": {"spec": job.finalize, "attempt": the failed attempt's record})
    the main container is the model's own image running the model's `finalize.command` on that volume, CPU only,
    with the finalized attempt named in its environment: the application re-delivers what it computed; the
    platform re-runs nothing."""
    orig = get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    pvc = ann.get(f"{LABEL}/pvc")
    worker = orig["_region"] if is_manager_job(orig) else region
    if not pvc or not worker or is_jobset(orig):
        raise HTTPException(409, f"operation {name} has no kept work volume to export")
    import catalog

    model = catalog.pinned(model, ann)
    try:
        retry(kube.core(worker).read_namespaced_persistent_volume_claim, pvc, ns)
    except ApiException as e:
        raise HTTPException(409 if e.status == 404 else 502, f"work volume {pvc} is gone; nothing to export")
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + sum(
        1 for j in list_ops(ns, labels[f"{LABEL}/tenant"], None, 500) if j["metadata"]["name"].startswith(f"{root}-x")
    )
    if n > EXPORT_RETRIES_MAX:
        raise HTTPException(
            409, f"operation {root}: {EXPORT_RETRIES_MAX} export/finalization retries used; submit a new run"
        )
    new = f"{root}-x{n}"
    d = dict(defaults)
    out = output_prefix(orig) or ""
    d["output_prefix"] = out  # the same prefix: the retry completes the original export
    try:
        ex = json.loads(ann.get(f"{LABEL}/execution") or "null")
    except ValueError:
        ex = None
    fz = (finalize or {}).get("spec") or {}
    job = build_run(
        new,
        model,
        op["input"],
        labels[f"{LABEL}/tenant"],
        ann.get(f"{LABEL}/name"),
        None,
        int(fz.get("timeoutS") or 3600),
        labels.get(f"{LABEL}/priority"),
        worker,
        d,
        ann.get(f"{LABEL}/key"),
        pvc=pvc,
        placement={"manager": True} if is_manager_job(orig) else None,
        gpu_class=labels.get(f"{LABEL}/gpu-class"),
        ex=ex,
    )
    spec = job["spec"]["template"]["spec"]
    spec["initContainers"] = []
    role = "finalize" if finalize else "export"
    if finalize:
        # the retained scratch still carries the compute attempt's bridge files (its checkpoint pointer, its RESULT.json
        # if any, the uploader's acknowledgement): removed before the finalizer starts, so the new attempt's uploader
        # records only what the finalizer itself writes and an old result marker can never become this attempt's
        # success; the application's own workspace and delivery marker are not touched
        spec["initContainers"] = [
            {
                "name": "bridge-reset",
                "image": RUNNER_IMAGE,
                "command": [
                    "/bin/sh",
                    "-c",
                    "rm -f /work/CHECKPOINT.json /work/CHECKPOINT.json.tmp /work/CHECKPOINT.ack.json /work/RESULT.json /work/attempts/.ack.json",
                ],
                "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                "securityContext": RUNNER_SECURITY_CONTEXT,
                "resources": {
                    "requests": {"cpu": "50m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "64Mi"},
                },
            }
        ]
    for c in spec["containers"]:
        if c["name"] == "main" and finalize:
            # the model's own image (the finalized attempt's digest when on record, else the pinned revision's image) and
            # its fixed finalize command; CPU only; the attempt it finalizes named in the environment (docs/JOBS.md)
            done = finalize.get("attempt") or {}
            c.update(
                {
                    "image": done.get("image_digest") or c["image"],
                    "command": ["/bin/sh", "-c", fz["command"]],
                    "resources": {
                        "requests": {"cpu": fz["cpu"], "memory": fz["memory"]},
                        "limits": {"cpu": fz["cpu"], "memory": fz["memory"]},
                    },
                }
            )
            c.pop("args", None)
            c["env"] = [
                e
                for e in c.get("env", [])
                if e["name"] not in ("ATTEMPT_ROLE", "FINALIZE_OF", "FINALIZE_EPOCH", "FINALIZE_EXIT_CODE", "WORK_DIR")
            ] + [
                {"name": "ATTEMPT_ROLE", "value": "finalize"},
                {"name": "FINALIZE_OF", "value": str(done.get("pod") or "")},
                {"name": "FINALIZE_EPOCH", "value": str(done.get("epoch") or "")},
                {
                    "name": "FINALIZE_EXIT_CODE",
                    "value": str(done.get("exit_code") if done.get("exit_code") is not None else ""),
                },
                {"name": "WORK_DIR", "value": "/work"},
            ]
        elif c["name"] == "main":
            c.update(
                {
                    "image": RUNNER_IMAGE,
                    "command": ["/bin/sh", "-c", "true"],
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "128Mi"}},
                }
            )
            c.pop("args", None)
        elif c["name"] == "uploader":
            # the transfer runs on no GPU: its attempt record must say so (the record stands in for the pod once it is
            # gone, and billing reads the record's gpus; the original class is not this attempt's)
            c["env"] = [e for e in c.get("env", []) if e["name"] not in ("GPUS", "GPU_CLASS")] + [
                {"name": "GPUS", "value": "0"},
                {"name": "GPU_CLASS", "value": ""},
            ]
    spec.pop("nodeSelector", None)
    spec.pop("affinity", None)
    spec.pop("tolerations", None)
    job["metadata"]["labels"].pop(f"{LABEL}/gpu-class", None)
    _pin_to_worker(job, orig)
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = root
    job["metadata"]["annotations"][f"{LABEL}/export-of"] = name
    _inherit_owner(job, ann)
    # the role is the platform's statement about this Job: the API stamps every attempt record of it with it
    job["metadata"]["annotations"][f"{LABEL}/attempt-role"] = role
    job["spec"]["template"]["metadata"].setdefault("labels", {})[f"{LABEL}/attempt-role"] = role
    if finalize:
        job["metadata"]["annotations"][f"{LABEL}/finalize-of"] = str((finalize.get("attempt") or {}).get("pod") or name)
    if admit:
        admit(job)
    return create(ns, job, region)


def switch_class(
    ns: str, name: str, region: str, model: dict, defaults: dict, next_class: str, remaining: list[str], admit=None
) -> tuple[dict, bool]:
    """Fallback placement (docs/SCHEDULING.md "Fallback to the next class"): a QUEUED fleet run that waited longer than
    its `fallback_wait_s` for its class is deleted on the manager (nothing ran) and created again as `<root>-rN` for
    `next_class` (that class's image), keeping only the remaining preference; the new Job says why."""
    orig = get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    if op["status"] != "QUEUED" or orig.get("_cluster") or orig.get("_pods"):
        raise HTTPException(
            409, f"operation {name} is {op['status']} (placed: {bool(orig.get('_cluster'))}); no fallback"
        )
    import catalog

    model = catalog.pinned(model, ann)
    try:
        ex = json.loads(ann.get(f"{LABEL}/execution") or "{}") or {}
    except ValueError:
        ex = {}
    ex["placement"] = {**(ex.get("placement") or {}), "gpu_preference": remaining}
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + sum(
        1
        for j in list_ops(ns, labels[f"{LABEL}/tenant"], None, 500)
        if j["metadata"].get("annotations", {}).get(f"{LABEL}/resumed-from") == root
    )
    new = f"{root}-r{n}"
    d = dict(defaults)
    out = output_prefix(orig) or ""
    d["output_prefix"] = out.rsplit("/", 1)[0] + "/" + new if out else ""
    placement = {
        "manager": True,
        "profile": f"prefer-{next_class}",
        "classes": remaining,
        "regions": [r for r in (ann.get(f"{LABEL}/regions") or "").split(",") if r]
        or (ex.get("placement") or {}).get("allowed_clusters")
        or model["regions"],
    }
    tmpl_regions = ((orig.get("spec") or {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get(
        f"{LABEL}/regions"
    )
    if tmpl_regions:
        placement["regions"] = tmpl_regions.split(",")
    job = build_run(
        new,
        model,
        op["input"],
        labels[f"{LABEL}/tenant"],
        ann.get(f"{LABEL}/name"),
        None,
        op["timeout_s"],
        labels.get(f"{LABEL}/priority"),
        None,
        d,
        ann.get(f"{LABEL}/key"),
        placement=placement,
        gpu_class=next_class,
        ex=ex,
    )
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = root
    _inherit_owner(job, ann)
    job["metadata"]["annotations"][f"{LABEL}/fallback"] = (
        f"{labels.get(f'{LABEL}/gpu-class') or 'preferred class'} -> {next_class} after {ann.get(f'{LABEL}/fallback-wait-s', '?')} s without capacity"
    )
    # the private input must be deliverable before anything is fenced: a revoked binding leaves the original untouched
    execution.deliver(
        ex, new, root, ns, labels[f"{LABEL}/tenant"], placement["regions"] or [region], owner=operation_owner(ann, root)
    )
    # the original is fenced BEFORE its successor exists: suspended and named after its successor (a readable handle,
    # status SUPERSEDED), its Workload deactivated so Kueue never admits it; only then is the successor admitted on the
    # run's ledger and created, so the two can never run together. A failed admission or create restores the original.
    try:
        retry(
            kube.batch(region).patch_namespaced_job,
            name,
            ns,
            {"metadata": {"annotations": {f"{LABEL}/successor": new}}, "spec": {"suspend": True}},
        )
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    try:
        kube.deactivate_workload(ns, md.get("uid", ""), region)
    except Exception as e:  # noqa: BLE001 - the Job is suspended either way
        log.warning("fallback: deactivate workload of %s/%s: %s", ns, name, e)
    try:
        if admit:
            admit(job)
        created, was_created = create(ns, job, region)
    except Exception:
        try:  # nothing was created: the original queues on as before
            retry(
                kube.batch(region).patch_namespaced_job,
                name,
                ns,
                {"metadata": {"annotations": {f"{LABEL}/successor": None}}, "spec": {"suspend": False}},
            )
            kube.activate_workload(ns, md.get("uid", ""), region)
        except Exception as e:  # noqa: BLE001
            log.warning("fallback: restoring %s/%s after a failed successor: %s", ns, name, e)
        raise
    import attempts

    attempts.mark_successor(name, new)
    return created, was_created


def recoveries(ns: str, tenant: str, root: str) -> int:
    """How many automatic continuations the chain of `root` already has (docs/SCHEDULING.md "Automatic recovery")."""
    return sum(
        1
        for j in list_ops(ns, tenant, None, 500)
        if (j["metadata"].get("annotations") or {}).get(f"{LABEL}/resumed-from") == root
        and (j["metadata"].get("annotations") or {}).get(f"{LABEL}/recovered-from")
    )


def recover(
    ns: str,
    name: str,
    region: str,
    model: dict,
    placement: dict,
    pointer: str,
    lost_epoch: int,
    next_class: str,
    remaining: list[str],
    loss: str,
    timeout_s: int | None = None,
    admit=None,
) -> tuple[dict, bool]:
    """Automatic recovery after a confirmed infrastructure loss (docs/SCHEDULING.md "Automatic recovery after
    infrastructure loss"): the original is fenced first (successor mark in the fleet database: every further record of
    it is refused; its Workload deactivated: the worker's copy and any pod go; the Job suspended), then the successor
    `<root>-r-<sha8>` is created from the acknowledged checkpoint pointer on `next_class` with that class's image and a
    fresh volume. The name is a function of (original, lost epoch): competing API replicas and repeated ticks
    converge on one Job (create is ignore-exists). Returns (successor, created)."""
    import hashlib

    import attempts

    orig = get(ns, name, region)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    root = ann.get(f"{LABEL}/resumed-from", name)
    idem = f"recover:{lost_epoch}"
    new = f"{root}-r-{hashlib.sha256(f'{name}:{idem}'.encode()).hexdigest()[:8]}"
    try:
        ex = json.loads(ann.get(f"{LABEL}/execution") or "{}") or {}
    except ValueError:
        ex = {}
    ex["placement"] = {**(ex.get("placement") or {}), "gpu_preference": remaining}
    # the private input must be deliverable before anything is fenced: a revoked binding leaves the original untouched
    regions = [r for r in placement.get("regions") or [] if r] or [region]
    execution.deliver(ex, new, root, ns, labels[f"{LABEL}/tenant"], regions, owner=operation_owner(ann, root))
    attempts.mark_successor(name, new)  # fence 1: no record of the original from now on
    try:
        kube.deactivate_workload(ns, md.get("uid", ""), region)  # fence 2: the worker's copy and its pods go
    except Exception as e:  # noqa: BLE001 - the Job is suspended below either way
        log.warning("recovery: deactivate workload of %s/%s: %s", ns, name, e)
    try:
        retry(kube.batch(region).patch_namespaced_job, name, ns, {"spec": {"suspend": True}})
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    reason = f"recovered from {name} ({loss}): {labels.get(f'{LABEL}/gpu-class') or 'its class'} -> {next_class}"
    # a modified execution block for the successor: the same block minus what the pinned original already carries
    job_ann = {f"{LABEL}/recovered-from": name, f"{LABEL}/recovery": reason}
    orig["metadata"]["annotations"] = {**ann, f"{LABEL}/execution": json.dumps(ex, sort_keys=True)}
    created, was_created = _resume_from(
        orig, ns, name, region, model, placement, pointer, next_class, idem, job_ann, timeout_s, admit
    )
    try:
        retry(
            kube.batch(region).patch_namespaced_job,
            name,
            ns,
            {"metadata": {"annotations": {f"{LABEL}/successor": new, f"{LABEL}/recovery": reason}}},
        )
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    return created, was_created


def _resume_from(
    orig: dict,
    ns: str,
    name: str,
    region: str,
    model: dict,
    placement: dict,
    pointer: str,
    next_class: str,
    idem: str,
    job_ann: dict,
    timeout_s: int | None,
    admit=None,
) -> tuple[dict, bool]:
    """resume() on an already-read original (its execution annotation adjusted by the caller); `timeout_s` is the
    root's remaining deadline when the request set `recovery.deadline_s` (the successor never gets a fresh full budget)."""
    return resume(
        ns,
        name,
        region,
        model,
        {},
        placement,
        checkpoint_ref=pointer,
        gpu_class=next_class,
        idem=idem,
        pointer=pointer,
        superseding=job_ann,
        _orig=orig,
        timeout_s=timeout_s,
        admit=admit,
    )


def resume(
    ns: str,
    name: str,
    region: str,
    model: dict,
    defaults: dict,
    placement: dict | None = None,
    checkpoint_ref: str | None = None,
    gpu_class: str | None = None,
    idem: str | None = None,
    pointer: str | None = None,
    superseding: dict | None = None,
    _orig: dict | None = None,
    timeout_s: int | None = None,
    admit=None,
) -> tuple[dict, bool]:
    """A new Job `<root>-rN` rendered again from the catalog entry with the original input, on the
    original PVC (same region: the volume is regional), linked through the resumed-from annotation.
    Rendering anew (rather than copying the finished Job's spec) leaves behind everything the Job
    controller and Kueue injected into it (selector, workload annotation, flavor labels). The
    original must be finished and its PVC must exist. `region` is the cluster the original is read
    from (the manager for a fleet-placed run); the new Job is pinned to the worker's region.
    `superseding`: automatic recovery (jobs.recover) continues a run the platform fenced itself; its annotations
    go onto the successor and the original's state is not a precondition."""
    orig = _orig or get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    if op["status"] in ("QUEUED", "RUNNING") and superseding is None:
        raise HTTPException(409, f"operation {name} is {op['status']}; cancel it first")
    if op["mode"] != "run":
        raise HTTPException(400, "only run operations can be resumed")
    import catalog

    model = catalog.pinned(model, ann)
    pvc = ann.get(f"{LABEL}/pvc")
    worker = orig["_region"] if is_manager_job(orig) else region
    try:
        ex = json.loads(ann.get(f"{LABEL}/execution") or "null")
    except ValueError:
        ex = None
    if not checkpoint_ref and pointer and not is_jobset(orig):
        # no explicit checkpoint: the newest acknowledged pointer of the chain stands in when the volume is gone or the
        # caller wants another class (docs/JOBS.md "Attempts and finalization")
        volume_ok = False
        if pvc and worker:
            try:
                retry(kube.core(worker).read_namespaced_persistent_volume_claim, pvc, ns)
                volume_ok = True
            except ApiException:
                volume_ok = False
        if gpu_class or not volume_ok:
            checkpoint_ref = pointer
    if checkpoint_ref:
        # a portable checkpoint (docs/JOBS.md "Execution options"): the application restores it from its own storage, so
        # the new attempt needs neither the old volume nor the old region: a fresh volume where the fleet places it
        ex = {**(ex or {}), "recovery": {**((ex or {}).get("recovery") or {}), "checkpoint_ref": checkpoint_ref}}
        pvc, worker = None, (worker if not placement else None)
    elif not worker:
        raise HTTPException(409, f"operation {name} never reached a worker; submit a new run")
    if checkpoint_ref:
        pass
    elif is_jobset(orig):
        if ann.get(f"{LABEL}/checkpoints") != "shared":
            raise HTTPException(
                409,
                f"operation {name} kept its checkpoints on node-local scratch (checkpoints: local); submit a new run",
            )
        pvc = None  # the shared claim is mounted by name, under checkpoints/<root>
    else:
        try:
            retry(kube.core(worker).read_namespaced_persistent_volume_claim, pvc, ns)
        except ApiException as e:
            raise HTTPException(
                409 if e.status == 404 else 502,
                f"checkpoint volume {pvc} is gone (successful runs release it); submit a new run",
            )
    root = ann.get(f"{LABEL}/resumed-from", name)
    if idem:
        # one continuation per (tenant, source operation, intent): the same key replays the same operation (`create`
        # returns it; a different input under the same key is 409), concurrent replays collide on the Job name
        import hashlib

        new = f"{root}-r-{hashlib.sha256(f'{name}:{idem}'.encode()).hexdigest()[:8]}"
    else:
        n = 1 + sum(
            1
            for j in list_ops(ns, labels[f"{LABEL}/tenant"], None, 500)
            if j["metadata"].get("annotations", {}).get(f"{LABEL}/resumed-from") == root
        )
        new = f"{root}-r{n}"
    out = output_prefix(orig) or ""
    d = dict(defaults)
    d["output_prefix"] = out.rsplit("/", 1)[0] + "/" + new if out else f"operations/{new}"
    pl = execution.narrow_placement(dict(placement or {}), ex) or {}
    if is_manager_job(orig):
        pl["manager"] = True
    # a run with per-class images keeps the class it was submitted with (its checkpoints were written by that image),
    # unless the caller resumes a portable checkpoint on another class (`gpu_class`)
    job = build_run(
        new,
        model,
        op["input"],
        labels[f"{LABEL}/tenant"],
        ann.get(f"{LABEL}/name"),
        None,
        min(op["timeout_s"], timeout_s) if op["timeout_s"] and timeout_s else (timeout_s or op["timeout_s"]),
        labels.get(f"{LABEL}/priority"),
        worker,
        d,
        ann.get(f"{LABEL}/key"),
        pvc=pvc,
        placement=pl or None,
        gpu_class=gpu_class
        or labels.get(f"{LABEL}/gpu-class")
        or (gpu_classes(model)[0] if class_images(model) else None),
        ex=ex,
    )
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = root
    _inherit_owner(job, ann)
    if idem:
        job["metadata"]["annotations"][f"{LABEL}/idempotency-key"] = idem
    if superseding:  # automatic recovery: the successor says where it comes from and why (status.py `reason`)
        job["metadata"]["annotations"].update(superseding)
    if pvc and not checkpoint_ref and not is_jobset(orig):
        _pin_to_worker(job, orig)  # the same volume: the same cluster, by id
    if is_jobset(job):  # the shared checkpoint directory is keyed by the root operation
        for c in job["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"]:
            for m in c.get("volumeMounts", []):
                if m["name"] == "checkpoints":
                    m["subPath"] = f"checkpoints/{root}"
    if admit:
        admit(job)  # the run's ledger admits the member before its Job exists (app.continuation_admission)
    return create(ns, job, region)
