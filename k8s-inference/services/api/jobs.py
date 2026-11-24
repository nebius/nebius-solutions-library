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

import base64
import glob
import hashlib
import json
import logging
import math
import os
import re
import threading
import time

import urllib3
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import admissions
import artifacts
import attempts
import catalog
import db
import execution
import fit
import instances
import keys
import kube
import placement
from config import (
    ENDPOINT_CONNECT_TO,
    INFINIBAND_CLAIM_TEMPLATE,
    INFINIBAND_ENV,
    JOB_PVC_SIZE_GI,
    JOBSET_GROUP,
    JOBSET_NAME_LABEL,
    JOBSET_PLURAL,
    JOBSET_VERSION,
    LABEL,
    LITELLM_URL,
    MULTINODE_MAX_NODES,
    OPS_LIST_CACHE_S,
    OPS_LIST_WARM_WINDOW_S,
    PUBLIC_API_URL,
    REGION,
    RUNNER_IMAGE,
    S3_ENV_SECRET,
    SHARED_SCRATCH_PVC,
    TENANT_NS_PREFIX,
    WEIGHTS_SHARED_PVC,
)
from models import normalise_image
from render import (
    CONTAINER_HARDENING,
    PRIORITY_CLASS,
    RUNNER_SECURITY_CONTEXT,
    _job_shell,
    _jobset_shell,
    _meta,
    _pod,
    _render,
    _uploader,
    class_images,
    gpu_classes,
    image_for_class,
    profile_of,
)
from resilience import cached, retry
from status import finished, is_jobset, is_manager_job, is_mirror, normalise

log = logging.getLogger("api.jobs")


SHARED_FS_VOLUMES = ("weights", "data", "checkpoints")  # the claims that live on the region's shared filesystem


def _mounts_shared_fs(spec: dict) -> bool:
    """Whether a rendered pod spec still mounts the shared filesystem through one of its claims (the asset claims are
    not counted: an export or finalizer has them stripped)."""
    return any(v.get("name") in SHARED_FS_VOLUMES and v.get("persistentVolumeClaim") for v in spec.get("volumes") or [])


def needs_shared_fs(r: dict) -> bool:
    """Whether a run class mounts the region's shared filesystem (docs/FLEET.md "Filesystem-aware placement")."""
    return bool(
        str(r.get("weights") or "").strip("/")
        or str(r.get("data") or "").strip("/")
        or r.get("assets")
        or str(r.get("checkpoints") or "local") == "shared"
    )


def _assets_registry() -> dict:
    """The fleet's restricted assets (docs/JOBS.md "Restricted assets"): alias -> {kind, host_path, gid, sha256, size}."""
    return dict(kube.fleet().get("assets") or {})


def _asset_shared_path(a: dict) -> str | None:
    """An asset's path relative to the shared weights filesystem root (what `weights`/`data` subPaths are relative to);
    None when the asset lives elsewhere."""
    hp = str(a.get("host_path") or "")
    for root in ("/mnt/weights",):
        if hp.startswith(root + "/"):
            return hp[len(root) + 1 :]
    return None


def _private_guard(path: str, what: str) -> None:
    """A `weights` or `data` subPath (caller-selectable through parameters) may not reach a registered asset: not its
    path, nothing under it, and no ancestor of it (the parent is root-only, but a container running as root reads a
    0440 root-owned file through an ancestor mount: the kubelet bind-mounts the subPath as root). Assets are mounted
    through `assets`, with a grant, main-only, read-only, never through the shared claims."""
    for alias, a in _assets_registry().items():
        rel = _asset_shared_path(a)
        if rel and _overlaps(path, rel):
            raise HTTPException(
                400,
                f"{what}: {path!r} reaches the restricted asset {alias!r}; assets are mounted through `assets` with a grant",
            )


def _mount_shared(pod: dict, containers: list, name: str, claim: str, mount_path: str, sub_path: str, ro: bool) -> None:
    """A directory of a shared-filesystem claim on `containers`, by subPath."""
    pod["volumes"].append({"name": name, "persistentVolumeClaim": {"claimName": claim, "readOnly": ro}})
    for c in containers:
        c["volumeMounts"].append({"name": name, "mountPath": mount_path, "subPath": sub_path, "readOnly": ro})


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
    _private_guard(f"tenants/{(pod.get('_tenant') or '*')}/data/{path}", "data")
    _mount_shared(
        pod, containers, "data", SHARED_SCRATCH_PVC, str(r.get("dataMountPath") or "/data"), f"data/{path}", ro
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
    _private_guard(path, "weights")
    _mount_shared(
        pod, containers, "weights", WEIGHTS_SHARED_PVC, str(r.get("weightsMountPath") or "/weights"), path, True
    )


ALIAS_ROOT = "aliases"  # under /work: aliases/<slug>[/<pod>] is what an alias path shows (delivered with /work)
POD_NAME_ENV = "S2_POD_NAME"  # platform-owned: the pod's own name (downward API), the attempt subdirectory of an alias
ALIAS_RESERVED = (
    "/work",
    "/dev",
    "/proc",
    "/sys",
    "/etc",
    "/run",
    "/var",
    "/tmp",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/root",
    "/opt/serverless2",
)
MAX_ALIASES = 4
MAX_ASSETS = (
    8  # restricted assets per class (BindCraft: AF2, vanilla and soluble MPNN, the PyRosetta tree, its inventory)
)


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def work_aliases(r: dict) -> list[dict]:
    """A run class's `workMountAliases` (docs/JOBS.md "Work mount aliases"): bounded absolute paths at which the run's
    own work volume is mounted again, `scope: attempt` (default: a per-attempt subdirectory, the pod's own name, so
    retries and resumes on the kept volume never share a canonical writable output) or `run` (one directory on the
    volume). Validated, never caller-supplied: a reserved or overlapping path is refused."""
    raw = r.get("workMountAliases")
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ALIASES:
        raise HTTPException(400, f"workMountAliases: a list of at most {MAX_ALIASES} {{path, scope}} entries")
    out: list[dict] = []
    for x in raw:
        if not isinstance(x, dict):
            raise HTTPException(400, "workMountAliases: each entry is {path, scope}")
        path, scope = str(x.get("path") or ""), str(x.get("scope") or "attempt")
        if scope not in ("attempt", "run"):
            raise HTTPException(400, f"workMountAliases: scope is attempt or run, not {scope!r}")
        if (
            not path.startswith("/")
            or path == "/"
            or path != os.path.normpath(path)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{2,200}", path)
        ):
            raise HTTPException(400, f"workMountAliases: {path!r} is not a bounded absolute path")
        if any(_overlaps(path, p) for p in ALIAS_RESERVED):
            raise HTTPException(400, f"workMountAliases: {path} is under a reserved path ({', '.join(ALIAS_RESERVED)})")
        if any(_overlaps(path, a["path"]) for a in out):
            raise HTTPException(400, f"workMountAliases: {path} overlaps another alias")
        out.append({"path": path, "scope": scope, "slug": re.sub(r"[^A-Za-z0-9._-]+", "-", path.strip("/"))})
    return out


def _mount_assets(r: dict, pod: dict, main: dict, tenant: str) -> str | None:
    """`job.assets` (docs/JOBS.md "Restricted assets"): every entry mounts the tenant's claim `asset-<alias>` (the
    tenant chart's static read-only volume rooted at the asset) on `main` only, read-only twice (claim and mount), at a
    bounded absolute path that overlaps no other mount (data, weights, checkpoints, shm, work aliases); the asset's
    group goes into the pod's supplemental groups (every container carries it; only main has a mount; no ownership
    change anywhere). Alias and grant come from the fleet document, never from the request. Returns the identity
    string recorded on the Job (`alias@sha256:…` or `alias@inventory:…`)."""
    specs = r.get("assets") or []
    if not specs:
        return None
    if not isinstance(specs, list) or len(specs) > MAX_ASSETS:
        raise HTTPException(400, f"assets: a list of at most {MAX_ASSETS} {{alias, mountPath}} entries")
    registry = _assets_registry()
    granted = set(((kube.fleet().get("tenants") or {}).get(tenant) or {}).get("assets") or [])
    gids, identity, seen = set(), [], []
    for x in specs:
        if not isinstance(x, dict):
            raise HTTPException(400, "assets: each entry is {alias, mountPath}")
        alias, path = str(x.get("alias") or ""), str(x.get("mountPath") or "")
        a = registry.get(alias)
        if a is None:
            raise HTTPException(400, f"assets: {alias!r} is not a registered asset of this fleet")
        if alias not in granted:
            raise HTTPException(400, f"assets: {alias!r} is not granted to tenant {tenant!r}")
        if (
            not path.startswith("/")
            or path == "/"
            or path != os.path.normpath(path)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{2,200}", path)
        ):
            raise HTTPException(400, f"assets: {path!r} is not a bounded absolute mount path")
        if any(_overlaps(path, p) for p in ALIAS_RESERVED):
            raise HTTPException(400, f"assets: {path} is under a reserved path")
        for m in main.get("volumeMounts") or []:
            if _overlaps(path, m["mountPath"]):
                raise HTTPException(400, f"assets: {path} overlaps the mount {m['mountPath']}")
        if any(_overlaps(path, p) for p in seen):
            raise HTTPException(400, f"assets: {path} overlaps another asset mount")
        seen.append(path)
        pod["volumes"].append(
            {"name": f"asset-{alias}", "persistentVolumeClaim": {"claimName": f"asset-{alias}", "readOnly": True}}
        )
        main["volumeMounts"].append({"name": f"asset-{alias}", "mountPath": path, "readOnly": True})
        gids.add(int(a.get("gid") or 65532))
        kind = "sha256" if str(a.get("kind") or "file") == "file" else "inventory"
        identity.append(f"{alias}@{kind}:{a.get('sha256') or 'unpinned'}")
    sc = pod.setdefault("securityContext", {})
    sc["supplementalGroups"] = sorted(set(sc.get("supplementalGroups") or []) | gids)
    return ";".join(identity)


def _strip_assets(job: dict) -> None:
    """Restricted assets are for the compute attempt's `main` only (docs/JOBS.md "Restricted assets"): a Job rendered
    again from the same class for a transfer-only export or the application finalizer (CPU, the runner or the model's
    finalize command) drops the asset volumes, main's asset mounts, the supplemental groups they brought and the
    identity annotation. Nothing else of the render changes."""
    spec = job["spec"]["template"]["spec"]
    names = {v["name"] for v in spec.get("volumes") or [] if str(v.get("name", "")).startswith("asset-")}
    if not names:
        return
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] not in names]
    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        if c.get("volumeMounts"):
            c["volumeMounts"] = [m for m in c["volumeMounts"] if m["name"] not in names]
    if spec.get("securityContext"):
        spec["securityContext"].pop("supplementalGroups", None)  # only assets add groups (docs/JOBS.md)
    job["metadata"].get("annotations", {}).pop(f"{LABEL}/assets", None)


def _alias_dir(a: dict) -> str:
    return f"{ALIAS_ROOT}/{a['slug']}" + (f"/$({POD_NAME_ENV})" if a["scope"] == "attempt" else "")


def _mount_aliases(r: dict, containers: list[dict], fetch: dict) -> None:
    """The aliases on `main` (and so on the CPU finalizer, which keeps main's mounts): the same `work` volume at the
    alias path with an operator-owned `subPathExpr` from the pod's own name (`$(S2_POD_NAME)`, downward API; no
    customer interpolation); the fetch init container pre-creates the directories group-writable for the pod's
    fsGroup (kubelet-created subPath directories are root-owned 0755, which a non-root model could not write; 2770, not
    world-writable). No symlink, no privilege, no new volume."""
    aliases = work_aliases(r)
    if not aliases:
        return
    pod_name = {"name": POD_NAME_ENV, "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}
    for c in containers:
        if any(e.get("name") == POD_NAME_ENV for e in c.get("env") or []):
            raise HTTPException(400, f"env {POD_NAME_ENV} is platform-owned (the alias attempt directory)")
        for a in aliases:
            for m in c.get("volumeMounts") or []:
                if _overlaps(a["path"], m["mountPath"]):
                    raise HTTPException(400, f"workMountAliases: {a['path']} overlaps the mount {m['mountPath']}")
        c["env"] = list(c.get("env") or []) + [dict(pod_name)]
        for a in aliases:
            m = {"name": "work", "mountPath": a["path"]}
            if a["scope"] == "attempt":
                m["subPathExpr"] = _alias_dir(a)
            else:
                m["subPath"] = _alias_dir(a)
            c["volumeMounts"].append(m)
    fetch["env"] = list(fetch.get("env") or []) + [
        dict(pod_name),  # before WORK_ALIAS_DIRS: the kubelet expands $(S2_POD_NAME) from the earlier entry
        {"name": "WORK_ALIAS_DIRS", "value": ":".join(_alias_dir(a) for a in aliases)},
    ]


def _required_affinity(terms: list[dict]) -> dict:
    """A required node affinity of one term: the expressions ANDed."""
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": terms}]}
        }
    }


def _fit_terms(pod: dict, r: dict, terms: list, placement: dict | None, region: str | None, name: str):
    """Per-node fit (docs/SCHEDULING.md "Per-node fit", services/api/fit.py): the pools whose ONE node holds the pod's
    effective requests, conjunctive with everything already decided (an explicit `pool:`, the GPU class pools, the
    shared-fs and local-NVMe terms stay as they are; this only narrows the pool set and the candidate clusters). No
    pool at all: 400 `impossible shape` (nothing is enlarged or reduced on the caller's behalf). Without fit.yaml
    (an older fleet chart) nothing changes. Returns the (possibly narrowed) placement."""
    if not fit.table():
        return placement
    req = fit.pod_requests(pod)
    if placement is not None:
        clusters = list(placement.get("regions") or []) or None
    else:  # pinned to a region: its clusters
        clusters = [c for c, x in kube.fleet().get("clusters", {}).items() if (x or {}).get("region") == region] or None
    pool_term = next((t for t in terms if t["key"] == f"{LABEL}/pool"), None)
    candidates = {r["pool"]} if r.get("pool") else (set(pool_term["values"]) if pool_term else None)
    # the capabilities the other terms require, intersected per (cluster, pool) BEFORE a pool counts as feasible: a
    # pool whose node is big enough but lacks the shared filesystem (or local NVMe) the run needs is no candidate, so
    # the affinity never names a set of pools no single node realizes
    need_fs = any(t["key"] == f"{LABEL}/shared-fs" for t in terms)
    need_nvme = any(t["key"] == f"{LABEL}/local-nvme" for t in terms)
    holds = fit.fitting(req, clusters, candidates, shared_fs=need_fs, local_nvme=need_nvme)
    if not holds:
        what = f"the class's pool {r['pool']}" if r.get("pool") else "any eligible pool"
        caps = " with the shared filesystem" if need_fs else ""
        caps += " with local NVMe" if need_nvme else ""
        raise HTTPException(
            400,
            f"impossible shape: the run's pod needs {fit.describe(req)} on one node{caps} and no node of {what} holds "
            f"it; the largest candidate node admits {fit.largest(clusters, candidates, shared_fs=need_fs, local_nvme=need_nvme)} "
            '(the per-node reserves of the fleet apply; docs/SCHEDULING.md "Per-node fit")',
        )
    if not r.get("pool"):
        if pool_term:
            pool_term["values"] = sorted(holds)
        else:
            terms.append({"key": f"{LABEL}/pool", "operator": "In", "values": sorted(holds)})
    if placement is not None and placement.get("regions"):
        fits = sorted({c for cs in holds.values() for c in cs})
        keep = [c for c in placement["regions"] if c in fits]
        if keep and keep != list(placement["regions"]):
            placement = {**placement, "regions": keep}
    log.debug("fit %s: %s -> pools %s", name, fit.describe(req), sorted(holds))
    return placement


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_parameter_bounds(parameters: list, values: dict) -> None:
    """The operator's declared bounds on a class's parameters (docs/JOBS.md "Parameters"), checked on the EFFECTIVE
    values (the caller's input over the region's and the class's defaults) before any reservation or Job: a `number`
    with `min`/`max` (inclusive) and `integer: true` (whole numbers only); a `select` with `options` (exactly one of
    them); a `boolean`. A malformed numeric (a string, a boolean, NaN/inf, a fraction where an integer is declared) is
    refused, never coerced. Parameters without bounds behave as before; a class declaring none is untouched."""
    for p in parameters:
        name, kind = p.get("name"), str(p.get("type") or "string")
        if name not in values or values[name] is None:
            continue
        v = values[name]
        bounded = any(k in p for k in ("min", "max", "integer", "options"))
        if kind == "number" and (bounded or not isinstance(v, str)):
            if not _is_number(v):
                raise HTTPException(400, f"parameter {name}: a finite number is required, not {v!r}")
            if p.get("integer") and float(v) != int(v):
                raise HTTPException(400, f"parameter {name}: an integer is required, not {v!r}")
            if "min" in p and v < p["min"]:
                raise HTTPException(400, f"parameter {name}: {v!r} is below the minimum {p['min']}")
            if "max" in p and v > p["max"]:
                raise HTTPException(400, f"parameter {name}: {v!r} is above the maximum {p['max']}")
            if p.get("integer"):
                values[name] = int(v)
        elif kind == "select" and p.get("options") is not None:
            options = list(p["options"])
            if isinstance(v, bool) or v not in options and str(v) not in [str(o) for o in options]:
                raise HTTPException(400, f"parameter {name}: {v!r} is not one of {options}")
        elif kind == "boolean" and bounded and not isinstance(v, bool):
            raise HTTPException(400, f"parameter {name}: a boolean is required, not {v!r}")


def check_gpu_bound(job: dict, rendered: dict, model_name: str) -> None:
    """`job.maxGpus` (the operator's bound on a run's GPUs: nodes x GPUs per node, docs/JOBS.md "Parameters"), on the
    rendered shape, before any reservation or Job; a class without it is untouched."""
    bound = job.get("maxGpus")
    if bound in (None, ""):
        return
    try:
        nodes, gpus, limit = int(rendered.get("nodes", 1) or 1), int(rendered.get("gpu", 0) or 0), int(bound)
    except (TypeError, ValueError):
        raise HTTPException(400, f"model {model_name}: nodes, gpu and job.maxGpus must be integers")
    if nodes * gpus > limit:
        raise HTTPException(
            400,
            f"model {model_name}: {nodes} node(s) x {gpus} GPU(s) = {nodes * gpus} GPUs exceeds the class's maxGpus ({limit})",
        )


def _run_values(
    name: str, model: dict, params: dict, defaults: dict | None, gpu_class: str | None
) -> tuple[dict, dict, dict, str | None]:
    """The catalog entry's `job` block rendered with the effective parameters (the caller's input over the region's
    defaults over the entry's defaults; the operator's declared bounds checked first): (the job block, the rendered
    block, the values, the GPU class pinned for a class with per-class images, None for a class with one image).
    `gpu_class` (per-class images, placement.choose_class): that class's image; a run with one image keeps switching
    class at queue time."""
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
    check_parameter_bounds(model.get("parameters", []), values)  # the operator's declared bounds, before anything else
    values["operation"] = name
    r = _render(job, values)
    check_gpu_bound(job, r, model["name"])  # job.maxGpus: nodes x GPUs per node, on the rendered shape
    r["image"] = normalise_image(str(r.get("image") or ""))  # after the parameters: `{{image}}` is now a reference
    _typed_inputs(job, r, values)
    return job, r, values, gpu_class


def _typed_inputs(job: dict, r: dict, values: dict) -> None:
    """`argsParameter` / `envParameter`: typed argv and environment from the input, never string interpolation."""
    if argument_parameter := job.get("argsParameter"):
        arguments = values.get(argument_parameter, [])
        if not isinstance(arguments, list) or len(arguments) > 100 or not all(isinstance(a, str) for a in arguments):
            raise HTTPException(400, "args must be a list of at most 100 strings")
        r["args"] = arguments
    if environment_parameter := job.get("envParameter"):
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


def _main_container(r: dict) -> tuple[dict, int]:
    """The model's container of a rendered job block and its GPU count: image and command (a shell string still
    receives typed arguments as literal argv values, not shell syntax), requests and limits, /work and /dev/shm, the
    hardening (the model container keeps the image's user unless the class sets `runAsNonRoot`)."""
    cmd = r["command"]
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd + ' "$@"', "--"] if r.get("args") else ["/bin/sh", "-c", cmd]
    gpus = int(r.get("gpu", 0) or 0)
    res = {
        "requests": {"cpu": str(r.get("cpu", "1")), "memory": str(r.get("memory", "2Gi"))},
        "limits": {"memory": str(r.get("memoryLimit", r.get("memory", "2Gi")))},
    }
    if r.get("cpuLimit"):
        res["limits"]["cpu"] = str(r["cpuLimit"])
    if r.get("ephemeralStorage") or r.get("ephemeralStorageLimit"):
        # the kubelet's ephemeral storage (/work on local-nvme scratch, the container's writable layer, emptyDirs):
        # request = limit unless both are given
        req = r.get("ephemeralStorage") or r.get("ephemeralStorageLimit")
        res["requests"]["ephemeral-storage"] = str(req)
        res["limits"]["ephemeral-storage"] = str(r.get("ephemeralStorageLimit") or req)
    if gpus:
        res["requests"]["nvidia.com/gpu"] = res["limits"]["nvidia.com/gpu"] = str(gpus)
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
    return main, gpus


def _fetch_container(r: dict, values: dict) -> dict:
    """The init container that syncs the inputs prefix to /work/in (runner image)."""
    return {
        "name": "fetch",
        "image": RUNNER_IMAGE,
        "command": ["/usr/local/bin/fetch.sh"],
        "env": [{"name": "INPUT_PREFIX", "value": r.get("inputs") or values.get("input_prefix") or ""}],
        "envFrom": [{"secretRef": {"name": S3_ENV_SECRET}}],
        "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
        "securityContext": RUNNER_SECURITY_CONTEXT,
    }


def _run_scratch(r: dict) -> tuple[str, bool]:
    """`scratch`: network (default) = the per-operation PVC, re-attached to the replacement pod after a preemption
    (automatic resume); local-nvme = an emptyDir on the node's host NVMe (pools with local_nvme), fastest I/O, gone
    with the pod: the run must checkpoint to object storage itself. Pins the run to NVMe nodes."""
    scratch = str(r.get("scratch") or "network").lower()
    if scratch not in ("network", "local-nvme"):
        raise HTTPException(400, f"scratch must be network or local-nvme, not {scratch!r}")
    return scratch, scratch == "local-nvme"


def _run_volumes(r: dict, pvc: str, local_nvme: bool) -> list[dict]:
    return [
        {"name": "work", "emptyDir": {"sizeLimit": f"{r.get('pvcSizeGi', JOB_PVC_SIZE_GI)}Gi"}}
        if local_nvme
        else {"name": "work", "persistentVolumeClaim": {"claimName": pvc}},
        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": r.get("shm", "8Gi")}},
    ]


def _run_terms(
    pod: dict,
    r: dict,
    model: dict,
    gpus: int,
    region: str | None,
    placement: dict | None,
    gpu_class: str | None,
    local_nvme: bool,
) -> tuple[list[dict], dict | None]:
    """Where a run's pod may land, before the per-node fit (docs/SCHEDULING.md): an explicit `pool:` as a nodeSelector
    (a definition pinned to `system` goes to the region's customer CPU pool instead when the fleet keeps tenants off
    the system nodes, kube.tenant_pool; a CPU pool is tainted), else the pools of the image's GPU classes (the
    preference queue may fall back to another class; Kueue assigns only flavors whose node labels satisfy the
    affinity; with per-class images only the chosen class's pools, with a caller's gpu_preference only the narrowed
    list), the local-NVMe and shared-filesystem terms, the pull secret. Returns the required terms and the (possibly
    narrowed) placement."""
    terms = []
    if r.get("pool"):
        cid = kube.cluster_id_or_none(region) if region else None
        pool = kube.tenant_pool(cid, r["pool"])
        pod["nodeSelector"] = {f"{LABEL}/pool": pool}
        if not gpus and kube.is_cpu_pool(cid, pool):
            pod["tolerations"] = [dict(kube.CPU_POOL_TOLERATION)]
    elif gpus and gpu_classes(model):
        pools = kube.class_pools([gpu_class] if gpu_class else ((placement or {}).get("classes") or gpu_classes(model)))
        if pools:
            terms.append({"key": f"{LABEL}/pool", "operator": "In", "values": pools})
    if local_nvme:
        terms.append({"key": f"{LABEL}/local-nvme", "operator": "In", "values": ["true"]})
        placement = _nvme_regions(placement)
    if needs_shared_fs(r):
        # the run mounts the region's shared filesystem (weights, data, shared checkpoints, a restricted asset): only a
        # node that mounts it can run it; Kueue skips a flavor whose nodeLabels contradict this (the system flavor
        # unless weights_filesystem.mount_on_system), docs/FLEET.md "Filesystem-aware placement"
        terms.append({"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]})
    if r.get("imagePullSecret"):
        pod["imagePullSecrets"] = [{"name": r["imagePullSecret"]}]
    return terms, placement


def _nvme_regions(placement: dict | None) -> dict | None:
    """Only regions with an NVMe pool are candidates for the dispatcher: Kueue ignores an affinity key that no flavor
    of a cluster declares, so a worker without such a pool would admit the run and leave its pod Pending for ever
    (the flavor of a local_nvme pool carries the label)."""
    nvme_regions = sorted(
        {p["region"] for p in (kube.fleet().get("pools") or {}).values() if p.get("local_nvme") in (True, "true")}
    )
    if kube.fleet().get("pools") and not nvme_regions:
        raise HTTPException(
            400,
            'scratch local-nvme needs a pool with local_nvme = true; this fleet has none (docs/FLEET.md "Local NVMe")',
        )
    if placement is None or not nvme_regions:
        return placement
    wanted = placement.get("regions") or nvme_regions
    keep = [r for r in wanted if r in nvme_regions]
    if not keep:
        raise HTTPException(
            400,
            f"scratch local-nvme: none of the regions {wanted} has a pool with local_nvme = true (regions with one: {nvme_regions})",
        )
    return {**placement, "regions": keep}


def _run_profile(model: dict, placement: dict | None, gpu_class: str | None) -> tuple[str, dict | None]:
    """The queue follows the class actually asked for: the pinned class, else the first of the run's (narrowed) class
    list, else the model's own profile (a gpu_preference without per-class images used to keep the model's queue and
    so landed every run on the model's first class). A pinned class is what the dispatcher keeps the run on."""
    classes_here = (placement or {}).get("classes") or []
    profile = (
        f"prefer-{gpu_class}"
        if gpu_class
        else (f"prefer-{classes_here[0]}" if classes_here else ((placement or {}).get("profile") or profile_of(model)))
    )
    if gpu_class and placement:
        placement = {**placement, "profile": profile, "classes": [gpu_class]}
    return profile, placement


def _billing_rates(model: dict) -> str:
    """The run's GPU-hour rates per region and pool, frozen on the Job (billing reads the annotation): the model's
    deployment price, else the pool's."""
    rates = {}
    for pool in kube.fleet().get("pools", {}).values():
        pool_region = kube.cluster_region(pool["region"])
        rate = (model.get("deployments", {}).get(pool_region) or {}).get(
            "price_per_gpu_hour", pool.get("usd_per_gpu_hour")
        )
        if rate is not None:
            rates.setdefault(pool_region, {})[pool["pool"]] = rate
    return json.dumps(rates, sort_keys=True)


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
    defaults over the entry's defaults) into a Job (a JobSet for `nodes` > 1, _multinode); the per-operation PVC is
    `<name>-work` unless a resume passes the original run's. region None = fleet placement (the dispatcher picks the
    worker). `gpu_class`: the class chosen at submission for a run class with per-class images (`job.images`,
    placement.choose_class): that class's image, a node affinity over that class's pools only, and the class's
    profile queue. The pod is shaped in order: the containers, the placement terms, the execution block
    (execution.apply_run), the per-node fit over the FINAL pod, then the affinity."""
    job, r, values, gpu_class = _run_values(name, model, params, defaults, gpu_class)
    main, gpus = _main_container(r)
    fetch = _fetch_container(r, values)
    pvc = pvc or f"{name}-work"
    scratch, local_nvme = _run_scratch(r)
    uploader = _uploader(
        name,
        region,
        values["output_prefix"],
        None if local_nvme else pvc,
        r.get("uploadExcludes", ""),
        gpus,
        placement,
        gpu_class,
    )
    pod = _pod(
        [main, uploader],
        _run_volumes(r, pvc, local_nvme),
        int(r.get("graceSeconds", 120)),
        PRIORITY_CLASS.get(priority, "serverless2-batch"),
        init=[fetch],
    )
    if gpus:
        pod["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    terms, placement = _run_terms(pod, r, model, gpus, region, placement, gpu_class, local_nvme)
    profile, placement = _run_profile(model, placement, gpu_class)
    meta = _meta(
        name, tenant, model["name"], "run", region, label, params, idem, priority, key_hash, profile, gpu_class, ex
    )
    _stamp_run(meta, model, scratch, job["image"] if gpu_class else None)
    nodes = int(r.get("nodes", 1) or 1)
    if ex:
        execution.apply_run(pod, r, ex, name, S3_ENV_SECRET)  # io mode, identity, private input, profile (docs/JOBS.md)
    # per-node fit over the FINAL pod (after the execution profile reshaped its containers: an application io mode drops
    # the fetch init container, a profile may add one), then the affinity: every term above plus the fitting pools
    placement = _fit_terms(pod, r, terms, placement, region, name)
    if terms:
        pod["affinity"] = _required_affinity(terms)
    if nodes > 1:
        return _multinode(name, model, r, pod, nodes, gpus, meta, timeout_s, placement, ex)
    _mount_run(r, pod, main, fetch, tenant, meta)
    if not local_nvme:
        meta["annotations"][f"{LABEL}/pvc"] = pvc
        meta["annotations"][f"{LABEL}/pvc-size-gi"] = str(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    if r.get("childEndpoints"):  # endpoint children (docs/JOBS.md): the attempt's own identity, no credential
        _project_parent_identity(pod, name, r)
        _stamp_children(meta, r)
    job = _job_shell(name, meta, pod, timeout_s, placement, model.get("podMetadata"), _delivery_pending(r))
    if ex:
        execution.apply_job(job, ex)
    retry = (ex or {}).get("retry") or _class_retry(r)
    if retry:
        execution.apply_retry(job, retry)  # the request's policy, else the class's (docs/JOBS.md "Retries")
    return job


def _stamp_run(meta: dict, model: dict, scratch: str, image: str | None) -> None:
    """What a run's Job records about its render: the model revision it is rendered from, the GPU-hour rates frozen
    for billing, the scratch kind, the class image when the class was pinned."""
    if model.get("version") is not None:
        meta["annotations"][f"{LABEL}/model-version"] = str(model["version"])
    meta["annotations"][f"{LABEL}/billing-rates"] = _billing_rates(model)
    meta["annotations"][f"{LABEL}/scratch"] = scratch
    if image:
        meta["annotations"][f"{LABEL}/image"] = image


def _stamp_children(meta: dict, r: dict) -> None:
    meta["annotations"][f"{LABEL}/child-endpoints"] = ",".join(r["childEndpoints"])
    meta["annotations"][f"{LABEL}/child-max-units"] = str(int(r.get("childMaxUnits") or 0))
    meta["annotations"][f"{LABEL}/child-delivery"] = str(r.get("childDelivery") or "platform")


def _mount_run(r: dict, pod: dict, main: dict, fetch: dict, tenant: str, meta: dict) -> None:
    """A single-node run's extra mounts, in the order the overlap checks need: shared data and weights, then the work
    aliases (an alias may overlap none of them), then the restricted assets (an asset may overlap no mount at all,
    aliases included; its identity goes onto the Job)."""
    pod["_tenant"] = tenant
    _mount_data(r, pod, [main])
    _mount_weights(r, pod, [main])
    _mount_aliases(r, [main], fetch)
    if ident := _mount_assets(r, pod, main, tenant):
        meta["annotations"][f"{LABEL}/assets"] = ident
    pod.pop("_tenant", None)


def _delivery_pending(r: dict) -> list | None:
    """The class's "compute done, delivery pending" exit codes (`finalize.onExitCodes`): the Job ends at once on
    them, the GPU computation is never restarted by the Job's backoff for a delivery problem; re-delivery is the
    model's finalizer through :export (bounded there). None without a finalizer."""
    fz = r.get("finalize")
    return list(fz["onExitCodes"]) if fz and fz.get("onExitCodes") else None


def _class_retry(r: dict) -> dict | None:
    """A run class's own retry policy (`retry: {maxAttempts, onExitCodes}` in the job block)."""
    pol = r.get("retry")
    if not isinstance(pol, dict) or not pol.get("maxAttempts"):
        return None
    return {
        "max_attempts": int(pol["maxAttempts"]),
        "on_exit_codes": [int(c) for c in pol.get("onExitCodes") or [137, 143]],
    }


# ---------------------------------------------------------------------------------------------------------------
# Multi-node runs (docs/JOBS.md "Multi-node runs"): a job class with `nodes: N` (N > 1) renders a JobSet of one
# indexed Job with N pods, one per node (a full node each: `gpu` = the preset's GPU count), a headless Service
# for rank discovery, torchrun/NCCL environment, and restart-all on any pod loss (spot preemption). `interconnect`
# (none | preferred | required) decides whether the pods must land on an InfiniBand pool (fleet.yaml pools with
# `interconnect: infiniband`, Terraform GPU cluster) and get the fabric NICs through a DRA ResourceClaimTemplate.
INTERCONNECT = ("none", "preferred", "required")


def _provision_init(name: str, path: str, mounts: list[dict]) -> dict:
    """The trusted init container that makes one directory of a shared claim the runner's (services/jobs/bin/scratch.sh
    `provision`: the kubelet creates a missing subPath directory root-owned 0755; this chowns 10001:10001 and chmods
    2770 exactly that mount point, as root with CHOWN/FOWNER/DAC_OVERRIDE only, PSA baseline-allowed; idempotent)."""
    return {
        "name": name,
        "image": RUNNER_IMAGE,
        "command": ["/usr/local/bin/scratch.sh", "provision", path],
        "resources": {"requests": dict(SCRATCH_PROVISION_REQUESTS), "limits": {"cpu": "200m", "memory": "64Mi"}},
        "volumeMounts": mounts,
        "securityContext": {
            **CONTAINER_HARDENING,
            "runAsNonRoot": False,
            "runAsUser": 0,
            "runAsGroup": 0,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"]},
        },
    }


def _checkpoint_provision(root: str) -> dict:
    """The init container that makes a gang's shared checkpoint leaf (`checkpoints/<root>` on the tenant's shared
    claim, docs/JOBS.md "Multi-node runs") the runner's."""
    return _provision_init(
        "checkpoint-provision",
        "/work/checkpoint",
        [{"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{root}"}],
    )


def _multinode(
    name: str,
    model: dict,
    r: dict,
    pod: dict,
    nodes: int,
    gpus: int,
    meta: dict,
    timeout_s: int | None,
    placement: dict | None,
    ex: dict | None,
) -> dict:
    """The single-node pod of build_run reshaped into a rank pod and wrapped in a JobSet (render._jobset_shell):
    whole-node placement, node-local /work, the shared checkpoint leaf, the torchrun/NCCL environment, the fabric.
    Retries, work aliases and assets are single-node things (a gang restarts as a whole; /work is node-local per pod)."""
    if (ex or {}).get("retry") or _class_retry(r):
        raise HTTPException(400, "retry applies to single-node runs (a multi-node run restarts as a whole)")
    if work_aliases(r):
        raise HTTPException(400, "workMountAliases apply to single-node runs (/work is node-local per pod there)")
    if r.get("assets"):
        raise HTTPException(400, "assets apply to single-node runs")
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
    pools = _gang_affinity(pod, model, gpus, interconnect)
    ib_count = int(r.get("ibDevicesPerNode") or kube.ib_devices_per_node(pools))
    # /work per pod: node-local scratch (the kubelet's ephemeral storage: local NVMe on pools that have it), not a
    # per-run RWO volume (N pods on N nodes). Checkpoints: `shared` = the tenant's RWX claim on the cluster's
    # shared filesystem (survives a restart and a :resume), `local` = under /work, gone with the pod.
    disk = int(r.get("pvcSizeGi", JOB_PVC_SIZE_GI))
    pod["volumes"] = [{"name": "work", "emptyDir": {"sizeLimit": f"{disk}Gi"}}] + [
        v for v in pod["volumes"] if v["name"] != "work"
    ]
    root = meta["annotations"].get(f"{LABEL}/resumed-from") or name
    if checkpoints == "shared":
        _gang_checkpoints(pod, (main, uploader), root)
    _mount_data(r, pod, [main])
    _mount_weights(r, pod, [main])
    _gang_env(main, uploader, name, nodes, gpus, ib, r.get("uploadExcludes") is None)
    if ib:
        _gang_fabric(pod, main, ib_count)
    meta["annotations"][f"{LABEL}/nodes"] = str(nodes)
    meta["annotations"][f"{LABEL}/interconnect"] = interconnect
    meta["annotations"][f"{LABEL}/checkpoints"] = checkpoints
    return _jobset_shell(name, meta, pod, nodes, timeout_s, placement, model.get("podMetadata"), _delivery_pending(r))


def _gang_affinity(pod: dict, model: dict, gpus: int, interconnect: str) -> list[str]:
    """Placement of a rank pod: whole nodes of the model's classes; InfiniBand pools only when required (preferred =
    soft). The local-NVMe term of `scratch: local-nvme` and the shared-filesystem term (build_run) are kept; the
    single-node class affinity is replaced. Returns the candidate pools."""
    ib = interconnect == "required"
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
        if t.get("key")
        in (f"{LABEL}/local-nvme", f"{LABEL}/shared-fs")  # the placement needs survive the class rewrite
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
    fabric = {"key": f"{LABEL}/interconnect", "operator": "In", "values": ["infiniband"]}
    if ib:
        terms.append(fabric)
    if terms:
        pod["affinity"] = _required_affinity(terms)
    elif "affinity" in pod:
        del pod["affinity"]
    if interconnect == "preferred":  # soft: useless without the fabric (docs/JOBS.md), kept for the explicit opt-in
        pod.setdefault("affinity", {}).setdefault("nodeAffinity", {})[
            "preferredDuringSchedulingIgnoredDuringExecution"
        ] = [{"weight": 100, "preference": {"matchExpressions": [fabric]}}]
    return pools


def _gang_checkpoints(pod: dict, containers: tuple, root: str) -> None:
    """The gang's shared checkpoint leaf: the tenant's RWX claim at `checkpoints/<root>` on every rank (main and the
    uploader) under /work/checkpoint. The kubelet creates a missing subPath directory root-owned 0755: the ranks
    (10001, no capability) could not write their checkpoints there (the first real 2x1 MPI run on a fresh root). The
    same trusted init as the shared-scratch caller provisions EXACTLY that leaf (chown 10001:10001, chmod 2770 of the
    mount point, not recursive, nothing outside it) before any rank starts; idempotent on a restart or a resume
    (existing files untouched); every rank pod runs it (the leaf is shared, the second one finds it done)."""
    pod["volumes"].append({"name": "checkpoints", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC}})
    for c in containers:
        c["volumeMounts"].append(
            {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{root}"}
        )
    pod["initContainers"] = list(pod.get("initContainers") or []) + [_checkpoint_provision(root)]  # after fetch


def _gang_env(
    main: dict, uploader: dict, name: str, nodes: int, gpus: int, ib: bool, exclude_checkpoints: bool
) -> None:
    """The torchrun/NCCL conventions on every rank (the job class's own env wins), the rank index from the indexed
    Job's completion index, and the uploader scoped to rank 0 (checkpoints are not results)."""
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
    if exclude_checkpoints:
        uploader["env"].append({"name": "UPLOAD_EXCLUDES", "value": "--exclude checkpoint/*"})


def _gang_fabric(pod: dict, main: dict, ib_count: int) -> None:
    """Every InfiniBand NIC of the node through DRA (DeviceClass ib.networking.nebius.ai, DraNet on the node image).
    No capability: RDMA pins memory, which RLIMIT_MEMLOCK bounds, and the GPU nodes' container runtime runs with an
    unlimited memlock limit (cloud-init + node-config), so `drop: [ALL]` under PSA baseline holds (docs/JOBS.md
    "Multi-node runs"). The contract is STATED to the run (an application validator that expects IPC_LOCK/no_new_privs
    reads it instead of probing for capabilities the platform never grants)."""
    pod["resourceClaims"] = [{"name": "ib", "resourceClaimTemplateName": f"{INFINIBAND_CLAIM_TEMPLATE}-{ib_count}"}]
    main["resources"]["claims"] = [{"name": "ib"}]
    main["env"] += [
        {"name": "S2_RDMA_MEMLOCK_POLICY", "value": "node-unlimited"},  # RLIMIT_MEMLOCK unlimited by the node runtime
        {"name": "S2_RDMA_CAPABILITIES", "value": "none"},  # drop ALL stays; no IPC_LOCK, no privilege
        {"name": "S2_RDMA_DEVICES_PER_NODE", "value": str(ib_count)},
    ]


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


def _jobset(method: str, region: str, ns: str, *args, **kw):
    """`<method>_namespaced_custom_object` of the JobSet API on `region` (get, list, create, patch, delete)."""
    fn = getattr(kube.api(region), f"{method}_namespaced_custom_object")
    return retry(fn, JOBSET_GROUP, JOBSET_VERSION, ns, JOBSET_PLURAL, *args, **kw)


def read_jobset(ns: str, name: str, region: str) -> dict:
    return _jobset_as_job(_jobset("get", region, ns, name))


def list_jobsets(ns: str, region: str, selector: str) -> list[dict]:
    return [_jobset_as_job(j) for j in _jobset("list", region, ns, label_selector=selector).get("items", [])]


def patch_jobset(ns: str, name: str, region: str, body: dict) -> dict:
    return _jobset_as_job(_jobset("patch", region, ns, name, body))


def delete_jobset(ns: str, name: str, region: str) -> None:
    _jobset("delete", region, ns, name)


def pods_selector(job: dict) -> str:
    return f"{JOBSET_NAME_LABEL}={job['metadata']['name']}" if is_jobset(job) else f"job-name={job['metadata']['name']}"


def _call_target(model: dict, region: str, key: str) -> tuple[str, str, list[dict]]:
    """Where the caller POSTs and how it authenticates: LiteLLM's pass-through route when the model has one (spend
    lands on the caller's key), else the gateway of the endpoint's region with the caller's key (TLS, key check,
    per-key rate limit: the same path as an external client; never the predictor Service, so no call skips the key
    check or the accounting). Returns (url, authorization header, the caller's first env entries)."""
    import models as runtime_models

    if model.get("litellm_route"):
        return LITELLM_URL + model["litellm_route"], f"Bearer {key}", []
    if not runtime_models.endpoint_domain(region):
        raise HTTPException(503, f"async calls need the endpoint domain of {region} (ENDPOINT_DOMAINS)")
    url = f"https://{runtime_models.endpoint_host(model, region)}{model['endpoint_path']}"
    env = []
    if ENDPOINT_CONNECT_TO:
        env.append({"name": "CONNECT_TO", "value": f"{url.split('/')[2]}:443:{ENDPOINT_CONNECT_TO}"})
    return url, f"Bearer {key}", env


def _call_env(
    name: str,
    model: dict,
    inp: dict,
    url: str,
    secret_name: str,
    timeout_s: int | None,
    ex: dict | None,
    transfer_only: bool,
    admitted: str | None,
) -> tuple[list[dict], dict]:
    """The caller's environment (services/jobs/bin/call.sh) and the request the Job records: the key and the admission
    token come from the per-operation Secret, never the Job spec. With `ex` the request lives in the binding, never in
    the Job (docs/API.md "Endpoint calls"); a transfer-only retry never calls the endpoint."""
    if (
        model.get("protocol") == "openai"
        and model.get("served_model")
        and isinstance(inp, dict)
        and not inp.get("model")
    ):
        inp = {**inp, "model": model["served_model"]}  # callers know the catalog id, not the served name
    env = [
        {"name": "URL", "value": url},
        {"name": "OPERATION", "value": name},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {
            "name": "AUTH_HEADER",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "authorization", "optional": True}},
        },
        {"name": "CALL_TIMEOUT", "value": str(min(timeout_s or 3600, 3600) if not ex else (timeout_s or 3600))},
    ]
    if ex:
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
    return env, inp


def _call_scratch(
    name: str,
    model: dict,
    tenant: str,
    region: str,
    ex: dict | None,
    pvc: str | None,
    scratch_leaf_of: str | None,
    probe: dict,
) -> tuple[str | None, str, list[str], str | None]:
    """The durable capture's home (docs/JOBS.md "Scratch storage"): the tenant's shared-scratch leaf `calls/<op>` when
    the model says `call_scratch: shared` and the region and namespace can carry it (judged on the caller's own
    requests, `probe`), else the per-call claim; a transfer-only retry keeps whatever the original had (`pvc` or
    `scratch_leaf_of`). Returns (leaf, why the leaf was not possible, the leaf's eligible pools, the claim)."""
    leaf, fallback, pools = None, "", []
    want_shared = bool(ex) and (bool(scratch_leaf_of) or (not pvc and model.get("call_scratch") == "shared"))
    if want_shared:
        ok, fallback, pools = shared_scratch_available(f"{TENANT_NS_PREFIX}{tenant}", region, probe)
        if scratch_leaf_of:
            # a transfer-only retry must reach the ORIGINAL's leaf: the same claim and placement rules, judged now; it
            # cannot fall back to a per-call claim (the capture is on the leaf), so it is refused, visibly, instead
            if not ok:
                raise HTTPException(
                    409, f"the retained capture on the shared-scratch leaf cannot be placed now: {fallback}"
                )
            leaf = scratch_leaf_of
        else:
            leaf = scratch_leaf(name) if ok else None
    work = None if leaf else (pvc or (f"{name}-work" if ex else None))
    return leaf, fallback, pools, work


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
    scratch_leaf_of: str | None = None,
) -> tuple[dict, dict]:
    """(Job, Secret): a queued endpoint call through the endpoint's gateway (_call_target). The Job runs in that
    region (services/api/app.py `invoke` creates it there). The key goes into a per-operation Secret owned by the
    Job, never into the Job spec; `admitted` (the API's admission token) goes next to it, so the gateway hook counts
    the call once. `ex` (docs/API.md "Endpoint calls"): private input through a binding, application-owned output,
    the capture on a retained work volume (`<name>-work`); `transfer_only` re-renders the caller on the original's
    volume (`pvc`) to deliver a complete capture again without calling the endpoint."""
    url, auth, env = _call_target(model, region, key)
    secret_name = f"{name}-auth"
    more, inp = _call_env(name, model, inp, url, secret_name, timeout_s, ex, transfer_only, admitted)
    trust = trust_bundle()
    main = _caller_container(env + more, trust)
    uploader = _uploader(name, region, output_prefix, None)
    probe = {  # the caller's own requests (main, uploader, the provisioning init), retry included
        "containers": [main, uploader],
        "initContainers": [{"name": "scratch-provision", "resources": {"requests": SCRATCH_PROVISION_REQUESTS}}],
    }
    leaf, fallback, pools, work = _call_scratch(name, model, tenant, region, ex, pvc, scratch_leaf_of, probe)
    if work:
        uploader["env"].append({"name": "PVC_NAME", "value": work})
    volumes = [
        {"name": "work", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC}}
        if leaf
        else {"name": "work", "persistentVolumeClaim": {"claimName": work}}
        if work
        else {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}}
    ]
    if trust:
        volumes.append(
            {"name": "trust", "secret": {"secretName": secret_name, "items": [{"key": TRUST_KEY, "path": TRUST_KEY}]}}
        )
    pod = _pod([main, uploader], volumes, 60, "serverless2-batch")
    if ex:
        execution.apply_async(pod, ex, name)
    if leaf:
        _mount_scratch_leaf(pod, leaf, pools)
    meta = _meta(name, tenant, model["name"], "async", region, label, inp, idem, None, key_hash, execution=ex)
    _stamp_call(meta, leaf, work, fallback)
    # a 4xx from the endpoint is the caller's error (call.sh exit 2): fail the Job, no pod retries; with application
    # I/O also the endpoint's own error (exit 5), an unknown inference outcome (exit 4) and nothing to export (exit 3):
    # never a second inference POST
    job = _job_shell(name, meta, pod, timeout_s, fail_codes=[2, 3, 4, 5] if ex else [2])
    job["spec"]["backoffLimit"] = 6
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "labels": {f"{LABEL}/tenant": tenant}},
        "stringData": {
            "authorization": auth,
            **({"admitted": admitted} if admitted else {}),
            **({TRUST_KEY: trust} if trust else {}),
        },
    }
    return job, secret


def _caller_container(env: list[dict], trust: str | None) -> dict:
    """The caller (services/jobs/bin/call.sh, the runner image). With a fleet CA bundle (`trust`: public CA material,
    the system roots plus the CAs mounted under /etc/ssl/msp, the fleet CA among them; the regional gateways'
    certificates are fleet-CA signed, edge.regional_certificates, and the runner image knows the public roots only)
    curl, python and the AWS SDK read that one file; verification stays on."""
    if trust:
        env = env + [
            {"name": k, "value": TRUST_FILE}
            for k in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE")
        ]
    return {
        "name": "main",
        "image": RUNNER_IMAGE,
        "command": ["/usr/local/bin/call.sh"],
        "env": env,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}]
        + ([{"name": "trust", "mountPath": TRUST_DIR, "readOnly": True}] if trust else []),
        "securityContext": RUNNER_SECURITY_CONTEXT,
    }


def _stamp_call(meta: dict, leaf: str | None, work: str | None, fallback: str) -> None:
    """Where the call's capture is retained: the tenant's shared-scratch leaf (no claim of its own, nothing to attach),
    or the per-call claim for a transfer-only export retry (create() makes the claim with the Job), and why the leaf
    was not possible."""
    if leaf:
        meta["annotations"][f"{LABEL}/scratch"] = "shared"
        meta["annotations"][f"{LABEL}/scratch-leaf"] = leaf
    elif work:
        meta["annotations"][f"{LABEL}/pvc"] = work
        meta["annotations"][f"{LABEL}/pvc-size-gi"] = str(ASYNC_PVC_SIZE_GI)
        if fallback:
            meta["annotations"][f"{LABEL}/scratch"] = "network"
            meta["annotations"][f"{LABEL}/scratch-fallback"] = fallback


def _mount_scratch_leaf(pod: dict, leaf: str, pools: list[str]) -> None:
    """The caller's /work on the tenant's shared-scratch claim at the leaf ONLY (a kubelet subPath on main and the
    uploader; neither sees the tenant's directory), plus the trusted provisioning init container: the kubelet creates
    a missing subPath directory root-owned 0755, so the runner image, as root with exactly CHOWN/FOWNER/DAC_OVERRIDE
    (PSA baseline-allowed), makes that one directory the runner's (10001:10001, 2770) and exits; idempotent on a retry.
    The pod lands only on nodes that mount the shared filesystem (the flavor's shared-fs label, docs/FLEET.md
    "Filesystem-aware placement"); the uploader empties the leaf after the proven, acknowledged delivery
    (services/jobs/bin/scratch.sh) and never deletes the claim."""
    for c in pod["containers"]:
        for m in c.get("volumeMounts") or []:
            if m["name"] == "work" and m["mountPath"] == "/work":
                m["subPath"] = leaf
        if c["name"] == "uploader":
            c["env"] = [e for e in c.get("env") or [] if e.get("name") != "PVC_NAME"] + [
                {"name": "SCRATCH", "value": "shared"},
                {"name": "SCRATCH_LEAF", "value": leaf},
            ]
    pod["initContainers"] = [
        _provision_init("scratch-provision", "/work", [{"name": "work", "mountPath": "/work", "subPath": leaf}])
    ] + list(pod.get("initContainers") or [])
    _and_required(pod, {"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]})
    # the eligible GPU-free pools only (shared_scratch_available): with the shared-fs label alone, a CPU pool at its
    # quota lets Kueue take the next flavor whose nodes carry the label, a GPU pool; with the pool list the caller
    # waits for its CPU pool (QUEUED), never a GPU node
    _and_required(pod, {"key": f"{LABEL}/pool", "operator": "In", "values": sorted(pools)})


PARENT_TOKEN_DIR = "/var/run/secrets/serverless2"


def _project_parent_identity(pod: dict, name: str, r: dict) -> None:
    """Endpoint children (docs/JOBS.md "Endpoint children"): main gets its own pod-bound ServiceAccount token (a
    projected volume, 1 h, renewed by the kubelet; TokenReview reports the pod's name and UID) and the control API's
    URL, so a live attempt can ask for a bounded child by proving exactly who it is; no bearer, no key, no
    credential of the customer anywhere in the pod. The uploader's runner token stays its own."""
    main = next(c for c in pod["containers"] if c["name"] == "main")
    pod.setdefault("volumes", []).append(
        {
            "name": "s2-parent",
            "projected": {"sources": [{"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}]},
        }
    )
    main.setdefault("volumeMounts", []).append({"name": "s2-parent", "mountPath": PARENT_TOKEN_DIR, "readOnly": True})
    main["env"] = list(main.get("env") or []) + [
        {"name": "S2_API_URL", "value": PUBLIC_API_URL},
        {"name": "S2_OPERATION", "value": name},
        {"name": "S2_PARENT_TOKEN_FILE", "value": f"{PARENT_TOKEN_DIR}/token"},
        {"name": "S2_CHILD_ENDPOINTS", "value": ",".join(r["childEndpoints"])},
        {"name": "S2_CHILD_MAX_UNITS", "value": str(int(r.get("childMaxUnits") or 0))},
    ]


def _and_required(pod: dict, expr: dict) -> None:
    """Add a required node-affinity expression by CONJUNCTION with whatever the pod already requires (an execution
    profile, a class): every existing nodeSelectorTerm gains it (terms are ORed, expressions within a term ANDed); a
    pod without a required affinity gets one term. Nothing already required is dropped or widened."""
    aff = pod.setdefault("affinity", {})
    node = aff.setdefault("nodeAffinity", {})
    req = node.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {})
    terms = req.setdefault("nodeSelectorTerms", [])
    if not terms:
        terms.append({"matchExpressions": []})
    for t in terms:
        exprs = t.setdefault("matchExpressions", [])
        if expr not in exprs:
            exprs.append(dict(expr))


TRUST_DIR = "/etc/ssl/fleet"
TRUST_KEY = "ca-bundle.pem"
TRUST_FILE = f"{TRUST_DIR}/{TRUST_KEY}"


def trust_bundle() -> str | None:
    """The bundle the API itself trusts (config._trust_file: the system roots plus every CA under /etc/ssl/msp), for a
    caller pod that must reach a fleet-CA-signed gateway; None on a fleet without extra CAs (public certificates
    everywhere: the runner's own roots suffice). Public CA material only, never a key."""
    path = os.environ.get("SSL_CERT_FILE")
    if not path or not glob.glob("/etc/ssl/msp/*.pem"):
        return None
    try:
        with open(path) as f:
            pem = f.read()
    except OSError:
        return None
    return pem if "BEGIN CERTIFICATE" in pem else None


ASYNC_PVC_SIZE_GI = 2
SCRATCH_LEAF_PREFIX = "calls"  # the tenant's shared-scratch leaf of a durable endpoint call: calls/<operation>


def scratch_leaf(name: str) -> str:
    """The API-derived, stable per-operation leaf on the tenant's shared-scratch claim (never caller-supplied)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        raise HTTPException(400, f"operation id {name!r} cannot name a scratch leaf")
    return f"{SCRATCH_LEAF_PREFIX}/{name}"


_shared_scratch_cache: dict = {}
SCRATCH_PROVISION_REQUESTS = {"cpu": "50m", "memory": "32Mi"}


def shared_scratch_available(ns: str, region: str, pod: dict) -> tuple[bool, str, list[str]]:
    """Whether the durable endpoint call rendered as `pod` (its containers' and init containers' requests; no GPU, no
    GPU toleration of its own) can keep its capture on the tenant's shared-scratch leaf in `ns` on `region`: the
    region's cluster has a GPU-FREE pool (system or CPU: an endpoint caller never occupies a GPU node) whose nodes
    mount the shared filesystem AND whose one node holds the caller's requests (fit.yaml, docs/SCHEDULING.md
    "Per-node fit": the judgement Kueue's flavor order and the scheduler make; a shared-fs affinity only a GPU flavor
    satisfies would put a 100m caller on a GPU node, or leave it Pending where no flavor matches), and the tenant
    namespace holds the claim, Bound. (True, "", [eligible pool labels]) then, and the render CONJOINS `pool In
    [those]` into the pod's required affinity (next to shared-fs): under CPU-quota saturation Kueue would otherwise
    take the next flavor whose nodeLabels satisfy `shared-fs In [true]`, a GPU flavor; with the pool list the caller
    waits QUEUED for its CPU pool, never a GPU node. (False, why, []) otherwise: the per-call claim, the Job says why."""
    cid = kube.cluster_id_or_none(region)
    table = kube.fleet().get("fit") or {}
    if not cid or cid not in table:
        return False, f"no per-node fit table for {region} (fleet chart < 0.1.12 or unknown cluster)", []
    req = fit.pod_requests(pod)
    if req["gpu"]:
        return False, "the caller requests a GPU (never for a shared-scratch leaf)", []
    eligible = sorted(fit.fitting(req, [cid], shared_fs=True, gpu_free=True))
    if not eligible:
        gpu_free_shared = any(
            (n or {}).get("shared_fs") and (n or {}).get("kind") != "gpu" for n in (table.get(cid) or {}).values()
        )
        if gpu_free_shared:
            return (
                False,
                f"no GPU-free pool of {cid} that mounts the shared filesystem holds the caller ({fit.describe(req)})",
                [],
            )
        if fit.fitting(req, [cid], shared_fs=True):
            return (
                False,
                f"only GPU pools of {cid} mount the shared filesystem (a caller never occupies a GPU node)",
                [],
            )
        return False, f"no GPU-free pool of {cid} mounts the shared filesystem", []
    ok, why = cached(_shared_scratch_cache, (ns, region), 60, lambda: _shared_scratch_bound(ns, region))
    return ok, why, eligible if ok else []


def _shared_scratch_bound(ns: str, region: str) -> tuple[bool, str]:
    """(the tenant's shared-scratch claim is Bound on `region`, why not)."""
    try:
        core = kube.core(region)
        claim = core.api_client.sanitize_for_serialization(
            retry(core.read_namespaced_persistent_volume_claim, SHARED_SCRATCH_PVC, ns)
        )
        phase = ((claim or {}).get("status") or {}).get("phase")
        if phase == "Bound":
            return True, ""
        return False, f"claim {SHARED_SCRATCH_PVC} in {ns} on {region} is not Bound ({phase or 'no status'})"
    except ApiException as e:
        if e.status == 404:
            return False, f"claim {SHARED_SCRATCH_PVC} not found in {ns} on {region}"
        return False, f"claim lookup: {e.reason}"
    except Exception as e:  # noqa: BLE001 - unknown is not available
        return False, f"claim lookup: {e}"


def _require_claim(region: str, ns: str, claim: str, gone: str) -> None:
    """The claim exists on `region` (one read), else 409 `gone` (404) or 502."""
    try:
        retry(kube.core(region).read_namespaced_persistent_volume_claim, claim, ns)
    except ApiException as e:
        raise HTTPException(409 if e.status == 404 else 502, gone)


def execution_of(ann: dict) -> dict | None:
    """The execution block an operation was rendered with (its Job's annotation, names only); None without one."""
    try:
        return json.loads(ann.get(f"{LABEL}/execution") or "null")
    except ValueError:
        return None


def export_async(ns: str, name: str, region: str, model: dict, admit=None) -> tuple[dict, bool]:
    """Transfer-only retry of an endpoint call with application-owned output (docs/API.md "Endpoint calls"): the
    caller rendered again on the original's retained work volume with TRANSFER_ONLY=1, so it delivers the complete
    capture once more and never calls the endpoint; `<root>-x<n>` like a run's export retry."""
    orig = get(ns, name, region)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    pvc = ann.get(f"{LABEL}/pvc")
    leaf = ann.get(f"{LABEL}/scratch-leaf") if ann.get(f"{LABEL}/scratch") == "shared" else None
    ex = execution_of(ann)
    if not (pvc or leaf) or not ex or ex.get("io_mode") != "application":
        raise HTTPException(409, f"operation {name} kept no capture to export (not an application-I/O endpoint call)")
    if pvc:
        _require_claim(region, ns, pvc, f"work volume {pvc} is gone; nothing to export")
    else:  # the shared-scratch leaf: the same claim, the same leaf; the transfer-only caller proves the capture itself
        _require_claim(region, ns, SHARED_SCRATCH_PVC, f"shared scratch claim is gone from {ns}; nothing to export")
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + export_retries(ns, labels[f"{LABEL}/tenant"], root)
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
        scratch_leaf_of=leaf,
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

    instances.inherit_requester(job, ann)  # who asked for the original asked for its continuation
    stages.inherit(job, ann)  # a stage's continuation stays that stage (lineage, dependency results)


def operation_owner(ann: dict, root: str) -> str | None:
    """The stable principal an operation belongs to: its own annotation (the submitting call writes it), else the
    root's durable record (continuations are rendered without it)."""
    owner = ann.get(f"{LABEL}/owner")
    if owner:
        return owner
    if db.enabled():
        rec = keys.operation_owner(root)
        if rec:
            return rec[1]
    return None


def create(ns: str, job: dict, region: str = REGION, secret: dict | None = None) -> tuple[dict, bool]:
    """Returns (job, created). AlreadyExists -> the existing one (idempotent replay). The per-operation
    PVC and Secret are owned by the Job (garbage-collected with it). A manager Job gets no PVC here: the
    dispatcher creates it on the worker it picks (docs/JOBS.md)."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
    name = job["metadata"]["name"]
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
            region=REGION,  # bindings live on the API's own cluster, whatever region the Job is created in
            owner=operation_owner(ann, root),
        )
    was_created = True
    try:
        if is_jobset(job):
            _jobset("create", region, ns, job)
        else:
            retry(kube.batch(region).create_namespaced_job, ns, job)
        admissions.record(job, region, output_prefix(job))  # docs/JOBS.md "Resume": kept past the Job's TTL
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
    if not is_manager_job(job):
        _own_work_volume(ns, region, job, owner)
    if secret:
        secret["metadata"]["ownerReferences"] = [owner]
        _create_ignore_exists(kube.core(region).create_namespaced_secret, ns, secret, region)
    return created, was_created


def _own_work_volume(ns: str, region: str, job: dict, owner: dict) -> None:
    """The Job's work volume, owned by it: created for a first render and for a portable resume (a fresh volume),
    co-owned for a same-region resume that reuses the old one (so it outlives the original Job's TTL or deletion)."""
    name, ann, core = job["metadata"]["name"], job["metadata"].get("annotations", {}), kube.core(region)
    pvc = ann.get(f"{LABEL}/pvc")
    if not pvc:
        return
    if not ann.get(f"{LABEL}/resumed-from") or pvc == f"{name}-work":
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
        return
    try:
        cur = core.api_client.sanitize_for_serialization(retry(core.read_namespaced_persistent_volume_claim, pvc, ns))
        refs = [r for r in (cur["metadata"].get("ownerReferences") or []) if r.get("uid") != owner["uid"]] + [owner]
        retry(core.patch_namespaced_persistent_volume_claim, pvc, ns, {"metadata": {"ownerReferences": refs}})
    except ApiException as e:
        raise HTTPException(502, f"kubernetes ({region}): work volume {pvc}: {e.reason}")


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


def list_ops(ns: str, tenant: str, model: str | None, limit: int | None) -> list[dict]:
    """The tenant's live operations (Jobs and JobSets over every reachable region), newest first; `limit` cuts the
    list AFTER it is complete (None: no cut). The API's listing authorizes before it cuts (app.list_operations)."""
    items, _skipped = list_ops_all(ns, tenant, model)
    return items[:limit] if limit else items


_ops_list_cache: dict = {}  # (ns, tenant, model) -> (until, items, skipped)
_ops_list_wanted: dict = {}  # (ns, tenant, model) -> when a caller last asked: what the warmer keeps fresh
_ops_list_lock = threading.Lock()  # one refresh of a listing at a time (the warmer and a cold request never both read)


def _json_items(fn, *args, **kw) -> list[dict]:
    """A Kubernetes list as the API server's JSON items (dicts), skipping the client's model objects and the
    sanitize pass back to dicts (half the time of listing 600 Jobs, api 0.11.124). `_preload_content=False` hands
    the raw response over; the shape is exactly what sanitize_for_serialization produced."""
    try:
        resp = retry(fn, *args, _preload_content=False, **kw)
    except (
        TypeError
    ):  # a client without the raw option or the watch-cache hint (a fake in the tests): the preloaded items
        kw.pop("resource_version", None)
        resp = retry(fn, *args, **kw)
    data = getattr(resp, "data", None)
    if isinstance(data, (bytes, str)):
        return json.loads(data).get("items") or []
    # a client that preloaded anyway (a fake in the tests, a wrapped client): the items as dicts
    items = getattr(resp, "items", None) or []
    return [i if isinstance(i, dict) else fn.__self__.api_client.sanitize_for_serialization(i) for i in items]


def list_ops_all(ns: str, tenant: str, model: str | None) -> tuple[list[dict], list[str]]:
    """Every live operation of the tenant, newest first, and the regions that could not be read (unreachable: their
    operations are absent from the list, which says so instead of failing every other region's). The listing is
    reused for OPS_LIST_CACHE_S (a console polling every few seconds and its page flips share one read of the
    tenant's Jobs and pods in every region: docs/API.md "Pagination"); a mutation never waits on it, it reads its
    own Job."""
    key = (ns, tenant, model)
    _ops_list_wanted[key] = time.time()
    hit = _ops_list_cache.get(key)
    if hit:
        # the cached list itself, READ-ONLY by contract (a copy of 600 Jobs cost 1.4 s per request, api 0.11.129):
        # a caller copies the entries it changes (app.list_operations copies its page). An expired copy is served
        # too while the warmer refreshes it (stale-while-revalidate): a request never waits behind a refresh
        return hit[1], list(hit[2])
    with _ops_list_lock:
        hit = _ops_list_cache.get(key)  # a refresh that finished while this caller waited for the lock
        if hit:
            return hit[1], list(hit[2])
        return _refresh_ops_listing(key)


def _refresh_ops_listing(key: tuple) -> tuple[list[dict], list[str]]:
    """Read the listing and cache it (OPS_LIST_CACHE_S); a listing with a region missing is never reused, so its
    return shows at once."""
    ns, tenant, model = key
    items, skipped = _list_ops_all(ns, tenant, model)
    if OPS_LIST_CACHE_S > 0 and not skipped:
        _ops_list_cache[key] = (time.time() + OPS_LIST_CACHE_S, items, skipped)
    return items, skipped


def warm_ops_listings(now: float | None = None) -> list[tuple]:
    """The warmer's one pass (app._warm_loop every OPS_LIST_WARM_S): every listing a caller asked for within
    OPS_LIST_WARM_WINDOW_S whose cached copy is older than half its life is read again, so a polling console and
    its page flips are served from memory, cold or not (a page of 50 took 6-8 s when it paid the listing itself,
    api 0.11.128). Returns the keys refreshed. Listings nobody asked for lately are forgotten."""
    now = now or time.time()
    refreshed = []
    for key, asked in list(_ops_list_wanted.items()):
        if asked + OPS_LIST_WARM_WINDOW_S < now:
            _ops_list_wanted.pop(key, None)
            _ops_list_cache.pop(key, None)
            continue
        hit = _ops_list_cache.get(key)
        if hit and hit[0] - OPS_LIST_CACHE_S / 2 > now:
            continue  # young enough
        if _ops_list_lock.locked():
            continue  # a request is reading it cold right now; the next pass sees the fresh copy
        with _ops_list_lock:
            try:
                _refresh_ops_listing(key)
                refreshed.append(key)
            except Exception as e:  # noqa: BLE001 - the next request reads for itself; the warmer never dies
                log.warning("listing warmer: %s not refreshed: %s", key, str(e)[:160])
    return refreshed


def ops_cache_invalidate(ns: str | None = None) -> None:
    """Forget the cached listing of a namespace (every namespace: None): called by every Job creation, patch and
    deletion this API makes, so a submission, a cancellation or a resume shows on the next list at once."""
    for key in list(_ops_list_cache):
        if ns is None or key[0] == ns:
            _ops_list_cache.pop(key, None)


def _list_ops_all(ns: str, tenant: str, model: str | None) -> tuple[list[dict], list[str]]:
    sel = f"{LABEL}/tenant={tenant}" + (f",{LABEL}/model={model}" if model else "")
    items, by_job, managers, skipped = [], {}, [], []
    for r in kube.regions():
        try:
            # resource_version="0": the API server answers from its watch cache (no etcd quorum read): the listing
            # is at most milliseconds behind, which a 10 s cache tolerates, and a large list comes back much faster
            jobs = _json_items(kube.batch(r).list_namespaced_job, ns, label_selector=sel, resource_version="0")
            pods = _json_items(
                kube.core(r).list_namespaced_pod, ns, label_selector=f"{LABEL}/tenant={tenant}", resource_version="0"
            )
            jobs += list_jobsets(ns, r, sel)
        except ApiException as e:
            if e.status in (403, 404):  # tenant not onboarded in that region
                continue
            raise HTTPException(502, f"kubernetes ({r}): {e.reason}")
        except (urllib3.exceptions.HTTPError, OSError) as e:
            # a region that cannot be reached (gone, or its endpoint down) must not hide every other region's
            # operations (the console showed 500 for every tenant while a region was being replaced)
            log.warning("operations: region %s unreachable, skipped: %s", r, str(e)[:160])
            skipped.append(r)
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
    items.sort(key=lambda j: j["metadata"]["name"])
    items.sort(key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)  # newest first, then id
    return items, skipped


_fleet_ops_cache: dict = {"until": 0.0, "items": [], "skipped": []}
FLEET_OPS_CACHE_S = 10


def job_terminal(j: dict) -> bool:
    """`status.finished` of a raw listed Job (app.py; a Job whose status is null counts as not terminal)."""
    return finished({"status": j.get("status") or {}})


def tenant_namespaces() -> list[str]:
    """The namespaces of every tenant the fleet knows: the fleet document's tenants (charts/fleet tenants.yaml) and
    the tenants with keys on record (the database), as `tenant-<name>`, sorted."""
    names = set((kube.fleet().get("tenants") or {}).keys())
    try:
        if db.enabled():
            names |= set(keys.tenants_on_record().keys())
    except Exception as e:  # noqa: BLE001 - the fleet document's tenants still count
        log.warning("fleet operations: tenants on record unavailable: %s", str(e)[:120])
    return sorted(f"{TENANT_NS_PREFIX}{n}" for n in names if n)


def list_fleet_operations() -> tuple[list[dict], list[str]]:
    """Every operation Job of every tenant on every reachable cluster, terminal ones included (the fleet operator's
    read, GET /v1/fleet/operations): Jobs with the tenant label in every tenant namespace, without pods (the status
    is read from the Job's conditions and annotations), cached FLEET_OPS_CACHE_S. Mirrors (a worker's copy of a
    manager Job) are folded into the manager's entry as its cluster and worker status."""
    if _fleet_ops_cache["until"] > time.time():
        return list(_fleet_ops_cache["items"]), list(_fleet_ops_cache["skipped"])
    out, skipped, mirrors = {}, [], {}
    namespaces = tenant_namespaces()
    for r in kube.regions():
        jobs = []
        try:
            # one listing per tenant namespace: the API's identity is granted in the tenant namespaces, never
            # cluster-wide (a cluster-wide listing answered Forbidden on the fleet, api 0.11.125)
            for ns in namespaces:
                try:
                    jobs += _json_items(
                        kube.batch(r).list_namespaced_job, ns, label_selector=f"{LABEL}/tenant", resource_version="0"
                    )
                except ApiException as e:
                    if e.status in (403, 404):  # a tenant not onboarded in that region
                        continue
                    raise
        except ApiException as e:
            raise HTTPException(502, f"kubernetes ({r}): {e.reason}")
        except (urllib3.exceptions.HTTPError, OSError) as e:
            log.warning("fleet operations: region %s unreachable, skipped: %s", r, str(e)[:160])
            skipped.append(r)
            continue
        for j in jobs:
            st = j.get("status") or {}
            name = j["metadata"]["name"]
            if is_mirror(j):
                mirrors[name] = (r, st)
                continue
            j["_region"] = r
            out[name] = j
    for name, (r, st) in mirrors.items():
        if name in out:
            out[name]["_cluster"] = kube.cluster_id_or_none(r)
            out[name]["_worker_status"] = st
    items = sorted(out.values(), key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)
    _fleet_ops_cache.update({"until": time.time() + FLEET_OPS_CACHE_S, "items": items, "skipped": skipped})
    return list(items), list(skipped)


def list_related(ns: str, tenant: str, keep, complete: bool = True) -> list[dict]:
    """The tenant's live Jobs and JobSets (every reachable region) that `keep(job)` selects, and nothing else: the
    bounded lookup of the few relatives of one operation (its finalizers `<root>-x<n>`, its continuations
    `<root>-r<n>`) that `list_ops` listed AND completed for the tenant's every operation (every pod of every region,
    one Workload read per manager Job: 7.5 s and the memory of it on every owner read of a failed run, the 24 s GETs
    and the OOM-killed API pods of 2026-10-14, api 0.11.110). `complete` fills the selected Jobs in like `get` (their
    pods, the worker's placement and reason: a handle needs them); a count of names does not. A region that cannot be
    read is skipped (as `list_ops_all`): its relatives are absent, never a failure of the lookup."""
    out = []
    for r in kube.regions():
        try:
            b = kube.batch(r)
            found = [
                b.api_client.sanitize_for_serialization(j)
                for j in retry(b.list_namespaced_job, ns, label_selector=f"{LABEL}/tenant={tenant}").items
            ]
            found += list_jobsets(ns, r, f"{LABEL}/tenant={tenant}")
        except ApiException as e:
            if e.status in (403, 404):  # tenant not onboarded in that region
                continue
            raise HTTPException(502, f"kubernetes ({r}): {e.reason}")
        except (urllib3.exceptions.HTTPError, OSError) as e:
            log.warning("operations: region %s unreachable, skipped: %s", r, str(e)[:160])
            continue
        for j in found:
            if is_mirror(j) or not keep(j):
                continue
            if not complete:
                out.append(j)
            elif is_manager_job(j):
                out.append(_with_worker(j, ns, kube.workload_clusters(ns)))
            else:
                out.append(_with_pods(j, ns, r))
    out.sort(key=lambda j: j["metadata"]["name"])
    out.sort(key=lambda j: j["metadata"].get("creationTimestamp", ""), reverse=True)  # newest first, then id
    return out


def continuations(ns: str, tenant: str, root: str, recovered: bool = False) -> int:
    """How many continuations `<root>-r<n>` the chain of `root` has (its live Jobs with `resumed-from` the root);
    `recovered`: only the automatic ones (docs/SCHEDULING.md "Automatic recovery")."""
    return len(
        list_related(
            ns,
            tenant,
            lambda j: (j["metadata"].get("annotations") or {}).get(f"{LABEL}/resumed-from") == root
            and (not recovered or (j["metadata"].get("annotations") or {}).get(f"{LABEL}/recovered-from")),
            complete=False,
        )
    )


def work_volume_present(job: dict, region: str) -> bool | None:
    """Whether the operation's own work volume still exists: None when the operation has none of its own (a shared
    scratch leaf, a JobSet with shared checkpoints, no volume), False when the claim is gone from the cluster (an
    uploader released it, or the namespace was cleaned), True when it is there. One read; only for the states whose
    public text promises a kept volume (EXPORT_FAILED), so that promise is never made about a volume that is gone."""
    ann = job["metadata"].get("annotations") or {}
    pvc = ann.get(f"{LABEL}/pvc")
    if not pvc or ann.get(f"{LABEL}/scratch") == "shared":
        return None
    try:
        retry(kube.core(region).read_namespaced_persistent_volume_claim, pvc, job["metadata"]["namespace"])
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    return True


def export_retries(ns: str, tenant: str, root: str) -> int:
    """How many export retries or finalizers `<root>-x<n>` the chain of `root` has (live Jobs)."""
    return len(list_related(ns, tenant, lambda j: j["metadata"]["name"].startswith(f"{root}-x"), complete=False))


def cancel(ns: str, name: str, region: str = REGION) -> dict:
    """A queued (never started) Job is deleted; a running one is stopped by a 1 s deadline and kept
    (status CANCELLED through the annotation), pods terminate within the grace period. For a manager
    Job the deadline goes onto the worker's copy (MultiKueue mirrors spec only at creation) and the
    annotation onto the manager's; deleting the manager Job removes the worker's copy."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
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


# ---- continuations: one re-render path, five policies ---------------------------------------------------------
# Every continuation of a run (a fallback to the next class, an unschedulable switch, an automatic recovery, a manual
# resume, an export retry or finalizer) is the ORIGINAL RENDERED AGAIN by build_run as a new operation `<root>-r<n>`,
# `<root>-r-<sha8>` or `<root>-x<n>` from the original's input, label, priority, key and the pinned model revision:
# nothing the Job controller or Kueue injected (selector, workload annotation, flavor labels) is copied. `_successor`
# is that path; a policy decides what changes about the render (the region or worker, the work volume, the placement,
# the GPU class, the execution block, the deadline) and which lineage marks the successor carries, then shapes the
# result where the render alone cannot say it (a transfer or finalizer container, a pin to the admitting cluster, the
# gang's root checkpoint leaf), and fences the original before the successor exists.


def _successor(
    orig: dict, op: dict, model: dict, new: str, region: str | None, defaults: dict, change: dict, shape=None
) -> dict:
    """The original `orig` (as `get` returns it; `op` its normalised view) rendered again as `new`, with the lineage:
    `resumed-from` names the root, the owner, the requester and the stage follow (_inherit_owner), then the policy's
    own `annotations`. `change`: pvc, placement, gpu_class, ex, timeout_s, annotations. `shape`: the policy's rewrite
    of the rendered Job before the lineage is stamped (a stage's dependency results bind into the final main)."""
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    job = build_run(
        new,
        model,
        op["input"],
        labels[f"{LABEL}/tenant"],
        ann.get(f"{LABEL}/name"),
        None,
        change.get("timeout_s"),
        labels.get(f"{LABEL}/priority"),
        region,
        defaults,
        ann.get(f"{LABEL}/key"),
        pvc=change.get("pvc"),
        placement=change.get("placement"),
        gpu_class=change.get("gpu_class"),
        ex=change.get("ex"),
    )
    if shape:
        shape(job)
    job["metadata"]["annotations"][f"{LABEL}/resumed-from"] = ann.get(f"{LABEL}/resumed-from", md["name"])
    _inherit_owner(job, ann)
    job["metadata"]["annotations"].update(change.get("annotations") or {})
    return job


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


def _continuation_name(ns: str, tenant: str, root: str, name: str, idem: str | None) -> str:
    """`<root>-r-<sha8>` for a keyed continuation (one per (source operation, intent): the same key replays the same
    operation, `create` returns it, a different input under the same key is 409, concurrent replays collide on the
    Job name), else the next `<root>-r<n>` of the chain."""
    if idem:
        return f"{root}-r-{hashlib.sha256(f'{name}:{idem}'.encode()).hexdigest()[:8]}"
    return f"{root}-r{1 + continuations(ns, tenant, root)}"


def _continuation_of(ns: str, orig: dict, defaults: dict) -> tuple[str, str, dict]:
    """The next continuation `<root>-r<n>` of a read Job: (root, its name, the defaults with its output prefix next to
    the original's)."""
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    root = ann.get(f"{LABEL}/resumed-from", md["name"])
    new = _continuation_name(ns, labels[f"{LABEL}/tenant"], root, md["name"], None)
    d = dict(defaults)
    out = output_prefix(orig) or ""
    d["output_prefix"] = out.rsplit("/", 1)[0] + "/" + new if out else ""
    return root, new, d


def _candidate_clusters(orig: dict, ex: dict, model: dict) -> list[str]:
    """The clusters a same-root successor of a fleet run may land in: the caller's (allowed_clusters, else the model's),
    never the original's template annotation: that one was narrowed by the per-node fit to the clusters holding a pool
    of the FIRST class (an RTX-only cluster), where the next class has no pool at all; build_run narrows it again for
    the successor. Only a run pinned to one cluster from the start (the `region` label) keeps that pin."""
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    tmpl_regions = ((orig.get("spec") or {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get(
        f"{LABEL}/regions"
    )
    if tmpl_regions and labels.get(f"{LABEL}/region"):
        return tmpl_regions.split(",")
    return (
        [r for r in (ann.get(f"{LABEL}/regions") or "").split(",") if r]
        or (ex.get("placement") or {}).get("allowed_clusters")
        or model["regions"]
    )


def _patch_job(ns: str, name: str, region: str, body: dict, jobset: bool = False) -> None:
    """A merge patch onto an original's Job (JobSet); one that expired (404, TTL) is left to its durable record."""
    try:
        if jobset:
            patch_jobset(ns, name, region, body)
        else:
            retry(kube.batch(region).patch_namespaced_job, name, ns, body)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")


def _fence(ns: str, name: str, region: str, uid: str, annotations: dict, what: str) -> None:
    """Fence an original BEFORE its successor exists: suspended and annotated (its successor: a readable handle, status
    SUPERSEDED), its Workload deactivated so Kueue never admits it again (the worker's copy and its pods go)."""
    _patch_job(ns, name, region, {"metadata": {"annotations": annotations}, "spec": {"suspend": True}})
    try:
        kube.deactivate_workload(ns, uid, region)
    except Exception as e:  # noqa: BLE001 - the Job is suspended either way
        log.warning("%s: deactivate workload of %s/%s: %s", what, ns, name, e)


def _create_successor(
    ns: str, job: dict, region: str, name: str, uid: str, clear: dict, what: str, admit=None
) -> tuple[dict, bool]:
    """Admit (the run's ledger) and create the successor of a fenced original, then mark it durably. A failed admission
    or create restores the original (the fence's annotations cleared, unsuspended, its Workload active again) and
    raises: nothing was created, the original goes on as before."""
    try:
        if admit:
            admit(job)
        created, was_created = create(ns, job, region)
    except Exception:
        try:
            retry(
                kube.batch(region).patch_namespaced_job,
                name,
                ns,
                {"metadata": {"annotations": clear}, "spec": {"suspend": False}},
            )
            kube.activate_workload(ns, uid, region)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: restoring %s/%s after a failed successor: %s", what, ns, name, e)
        raise
    attempts.mark_successor(name, job["metadata"]["name"])
    return created, was_created


def switch_class(
    ns: str, name: str, region: str, model: dict, defaults: dict, next_class: str, remaining: list[str], admit=None
) -> tuple[dict, bool]:
    """Fallback placement (docs/SCHEDULING.md "Fallback to the next class"): a QUEUED fleet run that waited longer than
    its `fallback_wait_s` for its class is deleted on the manager (nothing ran) and created again as `<root>-rN` for
    `next_class` (that class's image), keeping only the remaining preference; the new Job says why."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
    orig = get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    if op["status"] != "QUEUED" or orig.get("_cluster") or orig.get("_pods"):
        raise HTTPException(
            409, f"operation {name} is {op['status']} (placed: {bool(orig.get('_cluster'))}); no fallback"
        )
    model = catalog.pinned(model, ann)
    ex = execution_of(ann) or {}
    ex["placement"] = {**(ex.get("placement") or {}), "gpu_preference": remaining}
    root, new, d = _continuation_of(ns, orig, defaults)
    placement = {
        "manager": True,
        "profile": f"prefer-{next_class}",
        "classes": remaining,
        "regions": _candidate_clusters(orig, ex, model),
    }
    why = (
        f"{labels.get(f'{LABEL}/gpu-class') or 'preferred class'} -> {next_class} "
        f"after {ann.get(f'{LABEL}/fallback-wait-s', '?')} s without capacity"
    )
    change = {
        "placement": placement,
        "gpu_class": next_class,
        "ex": ex,
        "timeout_s": op["timeout_s"],
        "annotations": {f"{LABEL}/fallback": why},
    }
    job = _successor(orig, op, model, new, None, d, change)
    # the private input must be deliverable before anything is fenced: a revoked binding leaves the original untouched
    execution.deliver(
        ex, new, root, ns, labels[f"{LABEL}/tenant"], placement["regions"] or [region], owner=operation_owner(ann, root)
    )
    # the original is fenced BEFORE its successor exists; only then is the successor admitted on the run's ledger and
    # created, so the two can never run together (_fence, _create_successor)
    _fence(ns, name, region, md.get("uid", ""), {f"{LABEL}/successor": new}, "fallback")
    return _create_successor(ns, job, region, name, md.get("uid", ""), {f"{LABEL}/successor": None}, "fallback", admit)


def _started_pods(pods: list) -> list[dict]:
    """The pods of a run that bound to a node or left Pending (an attempt that started, or ended): {name, uid, node,
    phase, started_at}; the evidence the unschedulable switch must never pretend away."""
    out = []
    for p in pods or []:
        spec, st = p.get("spec") or {}, p.get("status") or {}
        running = next(
            (
                (c.get("state") or {}).get("running", {}).get("startedAt")
                for c in st.get("containerStatuses") or []
                if (c.get("state") or {}).get("running")
            ),
            None,
        )
        if spec.get("nodeName") or st.get("phase") != "Pending" or running:
            out.append(
                {
                    "pod": p["metadata"].get("name"),
                    "uid": p["metadata"].get("uid"),
                    "node": spec.get("nodeName"),
                    "phase": st.get("phase"),
                    "started_at": running or st.get("startTime"),
                }
            )
    return out


def switch_unschedulable(
    ns: str, name: str, region: str, model: dict, defaults: dict, why: str, admit=None
) -> tuple[dict, bool]:
    """Same-root recovery of an admitted run whose pod can never schedule where Kueue put it (docs/SCHEDULING.md
    "Per-node fit", reconciliation.unschedulable_switch): nothing of it ran (every pod Pending, unbound), so the run is
    created again as `<root>-rN` from the same input, placement and class, rendered with the per-node fit affinity;
    the original is fenced first exactly as a fallback is (suspended, named after its successor: status SUPERSEDED, its
    Workload deactivated so the worker's copy and its pod go and Kueue never admits it again), then the successor is
    admitted on the run's ledger and created; a failed admission or create restores the original. The owner, the root
    operation id and the durable handle stay the run's (lineage: resumed-from, recovered-from, the successor mark)."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
    orig = get(ns, name, region)
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    started = _started_pods(orig.get("_pods"))
    if (
        op["status"] not in ("QUEUED", "RUNNING")
        or started
        or ann.get(f"{LABEL}/successor")
        or ann.get(f"{LABEL}/cancelled")
    ):
        raise HTTPException(
            409, f"operation {name} is {op['status']} with {len(started)} started attempt(s); no unschedulable switch"
        )
    snapshot_uids = {p["metadata"].get("uid") for p in orig.get("_pods") or []}
    model = catalog.pinned(model, ann)
    ex = execution_of(ann) or {}
    root, new, d = _continuation_of(ns, orig, defaults)
    gpu_class = labels.get(f"{LABEL}/gpu-class") or None
    placement = {
        "manager": True,
        "profile": labels.get("kueue.x-k8s.io/queue-name") or profile_of(model),
        "classes": [gpu_class] if gpu_class else ((ex.get("placement") or {}).get("gpu_preference") or []),
        "regions": _candidate_clusters(orig, ex, model),  # re-narrowed by the fit for the pod as it is now
    }
    change = {
        "placement": placement,
        "gpu_class": gpu_class,
        "ex": ex,
        "timeout_s": op["timeout_s"],
        "annotations": {f"{LABEL}/recovered-from": name, f"{LABEL}/recovery": why},
    }
    job = _successor(orig, op, model, new, None, d, change)
    execution.deliver(
        ex, new, root, ns, labels[f"{LABEL}/tenant"], placement["regions"] or [region], owner=operation_owner(ann, root)
    )
    # the snapshot-to-fence window, bounded from both sides: (1) re-read the worker's pods immediately before the
    # fence; an attempt that bound meanwhile stops the switch here (nothing fenced, nothing created); (2) after the
    # fence, read the same pods (by the snapshot's UIDs) once more: a binding that slipped into the last instants is
    # evidence that is recorded on the original and on the successor's reason, never pretended away (its uploader
    # records stay accepted: the successor mark comes last)
    fresh = _started_pods([p for p in (get(ns, name, region).get("_pods") or [])])
    if fresh:
        raise HTTPException(
            409,
            f"operation {name}: attempt {fresh[0]['pod']} bound to {fresh[0]['node'] or 'a node'} before the fence; no "
            "unschedulable switch (the run is proceeding)",
        )
    uid = md.get("uid", "")
    _fence(ns, name, region, uid, {f"{LABEL}/successor": new, f"{LABEL}/recovery": why}, "unschedulable switch")
    _record_late_start(ns, name, region, snapshot_uids, why, job)
    # a failed successor restores the original as it was (a recorded late start stays recorded)
    return _create_successor(
        ns,
        job,
        region,
        name,
        uid,
        {f"{LABEL}/successor": None, f"{LABEL}/recovery": None},
        "unschedulable switch",
        admit,
    )


def _record_late_start(ns: str, name: str, region: str, snapshot_uids: set, why: str, job: dict) -> None:
    """After the fence: a pod of the snapshot that bound in the last instants before the fence took effect is recorded
    on the original (`started-before-fence`) and in the successor's reason; its records are retained."""
    try:
        late = _started_pods(
            [p for p in (get(ns, name, region).get("_pods") or []) if p["metadata"].get("uid") in snapshot_uids]
        )
    except Exception:  # noqa: BLE001 - the worker's copy may be gone already: no evidence is not evidence of a start
        return
    if not late:
        return
    note = f"attempt {late[0]['pod']} had bound to {late[0]['node'] or 'a node'} ({late[0]['phase']}) before the fence took effect; its records are retained"
    why = f"{why}; {note}"
    job["metadata"]["annotations"][f"{LABEL}/recovery"] = why
    log.warning("unschedulable switch %s/%s: %s", ns, name, note)
    marks = {f"{LABEL}/started-before-fence": json.dumps(late, sort_keys=True), f"{LABEL}/recovery": why}
    _patch_job(ns, name, region, {"metadata": {"annotations": marks}})


def recoveries(ns: str, tenant: str, root: str) -> int:
    """How many automatic continuations the chain of `root` already has (docs/SCHEDULING.md "Automatic recovery")."""
    return continuations(ns, tenant, root, recovered=True)


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
    fresh volume (resume). The name is a function of (original, lost epoch): competing API replicas and repeated ticks
    converge on one Job (create is ignore-exists). Returns (successor, created)."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
    orig = get(ns, name, region)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    root = ann.get(f"{LABEL}/resumed-from", name)
    idem = f"recover:{lost_epoch}"
    new = _continuation_name(ns, labels[f"{LABEL}/tenant"], root, name, idem)
    ex = execution_of(ann) or {}
    ex["placement"] = {**(ex.get("placement") or {}), "gpu_preference": remaining}
    # the private input must be deliverable before anything is fenced: a revoked binding leaves the original untouched
    regions = [r for r in placement.get("regions") or [] if r] or [region]
    execution.deliver(ex, new, root, ns, labels[f"{LABEL}/tenant"], regions, owner=operation_owner(ann, root))
    attempts.mark_successor(name, new)  # fence 1: no record of the original from now on
    _fence(ns, name, region, md.get("uid", ""), {}, "recovery")  # fence 2: suspended, the worker's copy and pods go
    reason = f"recovered from {name} ({loss}): {labels.get(f'{LABEL}/gpu-class') or 'its class'} -> {next_class}"
    # the successor's execution block (the narrowed preference); `timeout_s` is the root's remaining deadline when the
    # request set `recovery.deadline_s` (the successor never gets a fresh full budget)
    orig["metadata"]["annotations"] = {**ann, f"{LABEL}/execution": json.dumps(ex, sort_keys=True)}
    created, was_created = resume(
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
        superseding={f"{LABEL}/recovered-from": name, f"{LABEL}/recovery": reason},
        _orig=orig,
        timeout_s=timeout_s,
        admit=admit,
    )
    _patch_job(
        ns, name, region, {"metadata": {"annotations": {f"{LABEL}/successor": new, f"{LABEL}/recovery": reason}}}
    )
    return created, was_created


def _expired_original(ns: str, name: str, region: str) -> dict:
    """The original of a resume: its Job, or, once the Job expired (TTL), the admission record (api 0.11.74+) that
    remains of its inputs; without one the resume is refused as before, never reconstructed from thin air
    (docs/JOBS.md "Resume")."""
    try:
        return get(ns, name, region)
    except HTTPException as e:
        if e.status_code != 404:
            raise
    rec = admissions.get(name)
    if rec is None:
        raise HTTPException(
            404,
            f"operation {name}: its Job expired and it has no admission record (admitted before api 0.11.74); submit a new run",
        )
    durable = attempts.synthesize(name, rec["tenant"]) if attempts.enabled() else None
    orig = admissions.as_job(rec, (durable or {}).get("status"))
    if (durable or {}).get("status") == "SUCCEEDED":
        raise HTTPException(409, f"operation {name} SUCCEEDED; its result stands, nothing to restart")
    return orig


def _resume_volume(
    ns: str,
    name: str,
    region: str,
    orig: dict,
    placement: dict | None,
    checkpoint_ref: str | None,
    gpu_class: str | None,
    pointer: str | None,
    restart_from_inputs: bool,
) -> tuple[str | None, str | None, dict | None, str | None]:
    """Where a resume continues from: (the work volume to reuse, the worker to pin to, the execution block, the
    checkpoint reference). A portable checkpoint (explicit, or the chain's newest acknowledged pointer when the volume
    is gone or the caller wants another class) needs neither the old volume nor the old region: a fresh volume where
    the fleet places it. An expired original restarts from its recorded inputs on a fresh volume, asked for explicitly
    (never a checkpoint dropped unnoticed); a fleet-placed run is placed by the fleet again. Otherwise the kept volume
    (a gang: the shared claim, mounted by name under checkpoints/<root>) must still exist."""
    ann = orig["metadata"].get("annotations", {})
    expired = bool(orig.get("_expired"))
    pvc = ann.get(f"{LABEL}/pvc")
    worker = orig["_region"] if is_manager_job(orig) else region
    ex = execution_of(ann)
    if not checkpoint_ref and pointer and not is_jobset(orig):
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
        ex = {**(ex or {}), "recovery": {**((ex or {}).get("recovery") or {}), "checkpoint_ref": checkpoint_ref}}
        return None, (worker if not placement else None), ex, checkpoint_ref
    if expired:
        if not restart_from_inputs:
            raise HTTPException(
                409,
                f"operation {name}: its Job and work volume expired; a resume renders it again from its recorded inputs "
                "on a fresh volume: pass restart_from_inputs: true (or a checkpoint_ref)",
            )
        return None, (None if is_manager_job(orig) else worker), ex, None
    if not worker:
        raise HTTPException(409, f"operation {name} never reached a worker; submit a new run")
    if is_jobset(orig):
        if ann.get(f"{LABEL}/checkpoints") != "shared":
            raise HTTPException(
                409,
                f"operation {name} kept its checkpoints on node-local scratch (checkpoints: local); submit a new run",
            )
        return None, worker, ex, None
    _require_claim(worker, ns, pvc, f"checkpoint volume {pvc} is gone (successful runs release it); submit a new run")
    return pvc, worker, ex, None


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
    restart_from_inputs: bool = False,
) -> tuple[dict, bool]:
    """A new Job `<root>-rN` rendered again from the catalog entry with the original input, on the
    original PVC (same region: the volume is regional), linked through the resumed-from annotation (_successor).
    The original must be finished and its PVC must exist (_resume_volume). `region` is the cluster the original is
    read from (the manager for a fleet-placed run); the new Job is pinned to the worker's region.
    `superseding`: automatic recovery (jobs.recover) continues a run the platform fenced itself; its annotations
    go onto the successor and the original's state is not a precondition."""
    orig = _orig or _expired_original(ns, name, region)
    expired = bool(orig.get("_expired"))
    op = normalise(orig)
    md = orig["metadata"]
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    if op["status"] in ("QUEUED", "RUNNING") and superseding is None:
        raise HTTPException(409, f"operation {name} is {op['status']}; cancel it first")
    if op["mode"] != "run":
        raise HTTPException(400, "only run operations can be resumed")
    model = catalog.pinned(model, ann)
    pvc, worker, ex, checkpoint_ref = _resume_volume(
        ns, name, region, orig, placement, checkpoint_ref, gpu_class, pointer, restart_from_inputs
    )
    root = ann.get(f"{LABEL}/resumed-from", name)
    new = _continuation_name(ns, labels[f"{LABEL}/tenant"], root, name, idem)
    out = output_prefix(orig) or ""
    d = dict(defaults)
    d["output_prefix"] = out.rsplit("/", 1)[0] + "/" + new if out else f"operations/{new}"
    pl = execution.narrow_placement(dict(placement or {}), ex) or {}
    if is_manager_job(orig):
        pl["manager"] = True
    # the successor's marks: its intent key; automatic recovery says where it comes from and why (status.py `reason`);
    # a restart from the recorded inputs names the expired original. A run with per-class images keeps the class it
    # was submitted with (its checkpoints were written by that image), unless the caller resumes a portable checkpoint
    # on another class (`gpu_class`)
    marks = {
        **({f"{LABEL}/idempotency-key": idem} if idem else {}),
        **(superseding or {}),
        **({f"{LABEL}/restarted-from-inputs": name} if expired else {}),
    }
    change = {
        "pvc": pvc,
        "placement": pl or None,
        "gpu_class": gpu_class
        or labels.get(f"{LABEL}/gpu-class")
        or (gpu_classes(model)[0] if class_images(model) else None),
        "ex": ex,
        "timeout_s": min(op["timeout_s"], timeout_s)
        if op["timeout_s"] and timeout_s
        else (timeout_s or op["timeout_s"]),
        "annotations": marks,
    }
    job = _successor(orig, op, model, new, worker, d, change)
    if pvc and not checkpoint_ref and not is_jobset(orig):
        _pin_to_worker(job, orig)  # the same volume: the same cluster, by id
    if is_jobset(job):
        _gang_root_leaf(job, root)
    if admit:
        admit(job)  # the run's ledger admits the member before its Job exists (stages.continuation_admission)
    created, was_created = create(ns, job, region)
    _link_successor(ns, name, new, region, jobset=is_jobset(orig))
    return created, was_created


def _gang_root_leaf(job: dict, root: str) -> None:
    """A resumed gang's shared checkpoint directory is keyed by the root operation: every container of the rank pod,
    the provisioning init included (it must provision the ROOT's leaf, the one the ranks restore from, never a fresh
    `checkpoints/<successor>`)."""
    pod_spec = job["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    for c in list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or []):
        for m in c.get("volumeMounts", []):
            if m["name"] == "checkpoints":
                m["subPath"] = f"checkpoints/{root}"


def _export_preconditions(name: str, ann: dict, pvc: str | None, worker: str | None, gang: bool, finalize) -> None:
    """What an export retry or finalizer needs retained: a single-node run its kept work volume on a known worker; a
    multi-node run keeps no per-pod work volume (/work is node-local on every rank): what survives the gang is the
    coordinator's publication on the tenant's shared checkpoint claim (`checkpoints: shared`, subPath
    checkpoints/<root>); only the model's finalizer can deliver from it (application-owned bytes); a managed export
    has nothing retained to transfer."""
    if not gang:
        if not pvc or not worker:
            raise HTTPException(409, f"operation {name} has no kept work volume to export")
        return
    if ann.get(f"{LABEL}/checkpoints") != "shared":
        raise HTTPException(
            409,
            f"operation {name}: a multi-node run retains bytes only with `checkpoints: shared` (the coordinator's "
            "publication on the shared claim); nothing is kept to export or finalize",
        )
    if not finalize:
        raise HTTPException(
            409,
            f"operation {name}: a multi-node run is delivered again only by its model's finalizer (job.finalize, "
            "application I/O) from the shared checkpoint claim; the platform exports nothing of a gang's node-local /work",
        )
    if not worker:
        raise HTTPException(409, f"operation {name}: the admitting cluster of the gang is unknown; nothing to finalize")


def _finalizer_class(model: dict, labels: dict) -> dict:
    """The gang's model as ONE CPU pod renders it (never the gang again): a single-node render (no nodes, no GPU, no
    fabric), its own small /work claim; the admitted class's immutable image kept before the compute-only image
    selection is dropped (the finalizer prefers the finalized attempt's recorded digest when available)."""
    jb = dict(model.get("job") or {})
    jb["image"] = image_for_class(model, labels.get(f"{LABEL}/gpu-class"))
    for k in ("nodes", "interconnect", "ibDevicesPerNode", "checkpoints", "images"):
        jb.pop(k, None)
    jb["gpu"] = 0
    jb["pvcSizeGi"] = JOB_PVC_SIZE_GI
    return {**model, "job": jb, "gpu_classes": []}


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
    gang = is_jobset(orig)
    _export_preconditions(name, ann, pvc, worker, gang, finalize)
    model = catalog.pinned(model, ann)
    if gang:
        _require_claim(
            worker, ns, SHARED_SCRATCH_PVC, f"shared checkpoint claim {SHARED_SCRATCH_PVC} is gone; nothing to finalize"
        )
        model, pvc = _finalizer_class(model, labels), None
    else:
        _require_claim(worker, ns, pvc, f"work volume {pvc} is gone; nothing to export")
    root = ann.get(f"{LABEL}/resumed-from", name)
    n = 1 + export_retries(ns, labels[f"{LABEL}/tenant"], root)
    if n > EXPORT_RETRIES_MAX:
        raise HTTPException(
            409, f"operation {root}: {EXPORT_RETRIES_MAX} export/finalization retries used; submit a new run"
        )
    new = f"{root}-x{n}"
    d = dict(defaults)
    d["output_prefix"] = output_prefix(orig) or ""  # the same prefix: the retry completes the original export
    fz = (finalize or {}).get("spec") or {}
    role = "finalize" if finalize else "export"  # the platform's statement about this Job: on every attempt record
    marks = {f"{LABEL}/export-of": name, f"{LABEL}/attempt-role": role}
    if finalize:
        marks[f"{LABEL}/finalize-of"] = str((finalize.get("attempt") or {}).get("pod") or name)
    change = {
        "pvc": pvc,
        "placement": {"manager": True} if is_manager_job(orig) else None,
        "gpu_class": None if gang else labels.get(f"{LABEL}/gpu-class"),
        "ex": execution_of(ann),
        "timeout_s": int(fz.get("timeoutS") or 3600),
        "annotations": marks,
    }

    def shape(job: dict) -> None:
        _strip_assets(job)  # main-compute-only: a transfer or finalizer container never sees a restricted asset
        _delivery_pod(job["spec"]["template"]["spec"], finalize, fz, root, gang, ann)
        job["metadata"]["labels"].pop(f"{LABEL}/gpu-class", None)
        _pin_to_worker(job, orig)

    job = _successor(orig, op, model, new, worker, d, change, shape)
    job["spec"]["template"]["metadata"].setdefault("labels", {})[f"{LABEL}/attempt-role"] = role
    if admit:
        admit(job)
    return create(ns, job, region)


def _delivery_pod(spec: dict, finalize: dict | None, fz: dict, root: str, gang: bool, ann: dict) -> None:
    """The rendered compute pod turned into the delivery's: the gang's shared checkpoint subPath exactly where every
    rank had it, the bridge reset before a finalizer, main as the finalizer or a transfer (exit 0), the uploader on no
    GPU, and the compute attempt's class, pool and NVMe constraints dropped: the one placement need that survives is
    the shared filesystem the delivery still mounts (weights, data, shared checkpoints), recomputed from the RETAINED
    volumes, never from the stripped assets (docs/FLEET.md "Filesystem-aware placement")."""
    if gang:
        _gang_delivery_mount(spec, finalize, root, ann)
    spec["initContainers"] = [_checkpoint_provision(root)] if gang else []
    if finalize:
        # the retained scratch still carries the compute attempt's bridge files (its checkpoint pointer, its RESULT.json
        # if any, the uploader's acknowledgement): removed before the finalizer starts, so the new attempt's uploader
        # records only what the finalizer itself writes and an old result marker can never become this attempt's
        # success; the application's own workspace and delivery marker are not touched
        spec["initContainers"] = [_bridge_reset()]
    for c in spec["containers"]:
        if c["name"] == "main" and finalize:
            _finalizer_main(c, fz, finalize.get("attempt") or {})
        elif c["name"] == "main":
            # a transfer-only export: main only exits 0 on the kept volume; its work aliases keep the rendered expression
            # (this pod's own, empty, directory), nothing of the finalizer's context applies here
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
    if _mounts_shared_fs(spec):
        spec["affinity"] = _required_affinity([{"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]}])


def _gang_delivery_mount(spec: dict, finalize: dict | None, root: str, ann: dict) -> None:
    """The gang's shared checkpoint subPath on the finalizer's containers, exactly where every rank had it
    (/work/checkpoint): the finalizer reads the coordinator's publication; the JobSet's incarnation and coordinator
    pod are named in its environment."""
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "checkpoints"] + [
        {"name": "checkpoints", "persistentVolumeClaim": {"claimName": SHARED_SCRATCH_PVC}}
    ]
    done = (finalize or {}).get("attempt") or {}
    for c in spec["containers"]:
        c.setdefault("volumeMounts", []).append(
            {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{root}"}
        )
        if c["name"] == "main":
            c["env"] = [
                e
                for e in c.get("env", [])
                if e["name"] not in ("CHECKPOINT_DIR", "FINALIZE_RANK", "FINALIZE_GANG_RESTART", "FINALIZE_NODES")
            ] + [
                {"name": "CHECKPOINT_DIR", "value": "/work/checkpoint"},
                {
                    "name": "FINALIZE_RANK",
                    "value": str(done.get("gang_rank") if done.get("gang_rank") is not None else 0),
                },
                {
                    "name": "FINALIZE_GANG_RESTART",
                    "value": str(done.get("gang_restart") if done.get("gang_restart") is not None else ""),
                },
                {"name": "FINALIZE_NODES", "value": str(ann.get(f"{LABEL}/nodes") or "")},
            ]


def _bridge_reset() -> dict:
    return {
        "name": "bridge-reset",
        "image": RUNNER_IMAGE,
        "command": [
            "/bin/sh",
            "-c",
            "rm -f /work/CHECKPOINT.json /work/CHECKPOINT.json.tmp /work/CHECKPOINT.ack.json /work/RESULT.json /work/attempts/.ack.json",
        ],
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
        "securityContext": RUNNER_SECURITY_CONTEXT,
        "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
    }


def _finalizer_main(c: dict, fz: dict, done: dict) -> None:
    """main as the model's finalizer: the model's own image (the finalized attempt's digest when on record, else the
    pinned revision's image) and its fixed finalize command; CPU only; the attempt it finalizes named in the
    environment (docs/JOBS.md). The canonical work aliases (docs/JOBS.md "Work mount aliases") show the finalized
    attempt's own directory: the pod name in the alias expression is the finalized pod's, a literal, not this pod's."""
    c["env"] = [
        {"name": POD_NAME_ENV, "value": str(done.get("pod") or "")} if e.get("name") == POD_NAME_ENV else e
        for e in c.get("env") or []
    ]
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


def _link_successor(ns: str, name: str, new: str, region: str, jobset: bool = False) -> None:
    """The resumed operation names its successor, durably and on its Job: the durable mark (operation_marks) is what
    the public handle (app._owned) and the stage gate (stages.state_of) follow once the original Job is gone (TTL), and
    it fences the original's late progress records; the annotation serves the handle while the Job still exists. Until
    0.11.63 only fallback and recovery linked their successor: a manual `:resume` left the original handle on its own
    terminal state (FAILED, epoch 1) for ever once its Job expired, and a graph child waiting on that root kept seeing
    the FAILED original. Idempotent: a replayed resume marks the same successor again."""
    ops_cache_invalidate(None)  # the listing behind GET /v1/operations must show this mutation at once
    try:
        attempts.mark_successor(name, new)
    except Exception as e:  # noqa: BLE001 - the annotation below still links while the Job exists; the mark is retried on replay
        log.warning("successor mark %s -> %s: %s", name, new, e)
    _patch_job(ns, name, region, {"metadata": {"annotations": {f"{LABEL}/successor": new}}}, jobset=jobset)


# ---- submission: what a request becomes before it is admitted -------------------------------------------------
def deadline_of(job: dict) -> int | None:
    """The enforced wall-clock bound of a rendered Job or JobSet (its activeDeadlineSeconds; a JobSet's is on its
    replicated Jobs)."""
    spec = job.get("spec") or {}
    if job.get("kind") == "JobSet":
        ds = [
            int(((rj.get("template") or {}).get("spec") or {}).get("activeDeadlineSeconds") or 0)
            for rj in spec.get("replicatedJobs") or []
        ]
        return max(ds) if ds and max(ds) else None
    d = spec.get("activeDeadlineSeconds")
    return int(d) if d else None


def gpus_of(job: dict) -> int:
    """GPUs a rendered Job (or JobSet) holds while it runs: nvidia.com/gpu over its containers times its parallelism."""

    def pods(spec: dict) -> int:
        tmpl = (spec.get("template") or {}).get("spec") or {}
        n = 0
        for c in (tmpl.get("containers") or []) + (tmpl.get("initContainers") or []):
            res = c.get("resources") or {}
            n += int(
                str(
                    (res.get("limits") or {}).get("nvidia.com/gpu")
                    or (res.get("requests") or {}).get("nvidia.com/gpu")
                    or 0
                )
            )
        return n * int(spec.get("parallelism") or 1)

    if job.get("kind") == "JobSet":
        return sum(
            pods((rj.get("template") or {}).get("spec") or {}) * int(rj.get("replicas") or 1)
            for rj in (job.get("spec") or {}).get("replicatedJobs") or []
        )
    return pods(job.get("spec") or {})


def find_or_none(ns: str, name: str, region: str | None = None) -> tuple[dict, str | None] | None:
    """The operation's Job as (job, region): find over the regions, or get in the one named; a clean
    absence (404) is None, any other failure raises as it is."""
    try:
        return (get(ns, name, region), region) if region is not None else find(ns, name)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        return None


def home_of(job: dict) -> str:
    """The cluster the operation's Job object lives in: the manager for fleet-placed runs."""
    return REGION if is_manager_job(job) else (job.get("_region") or REGION)


def _output_prefix(ns: str, region: str, name: str, ex: dict | None) -> str:
    """Where a managed-I/O operation's uploader writes: the tenant's bucket of the region; nothing for
    application-managed I/O (the run moves its own data)."""
    if ex and ex.get("io_mode") == "application":
        return ""
    return f"s3://{artifacts.storage(ns, region)['bucket']}/operations/{name}"


def _rollout_fence(m: dict, region: str, ex: dict | None) -> dict | None:
    """The endpoint's revision state at admission of an endpoint call (kube._rollout); with
    `execution.require_converged` the call is refused (409) while the created, ready and rolled-out revisions differ
    (a new revision Pending, the previous one serving). A converged endpoint at zero passes: the fence reads the
    revisions, not the pods. Without the option the state is only recorded."""
    try:
        st = kube.endpoint_status(m["k8s_name"], m["namespace"], region, nodes=int(m.get("nodes") or 1))
    except Exception:  # noqa: BLE001
        st = {}
    rollout = st.get("rollout")
    if ex and ex.get("require_converged"):
        if not rollout or not rollout.get("converged"):
            why = (rollout or {}).get("reason") or "rollout state unavailable"
            raise HTTPException(
                409, f"endpoint {m['name']} rollout not converged in {region}: {why} (execution.require_converged)"
            )
    return rollout


def _guard_run_input(m: dict, req, p, region: str, ex: dict | None = None) -> None:
    """A caller-supplied output_prefix must stay inside the tenant's own bucket of that region (never another
    tenant's), and an `image` parameter (container-run) must match the tenant's image allow-list when one is set.
    Application-managed I/O names no bucket at all."""
    out = req.input.get("output_prefix")
    if ex and ex.get("io_mode") == "application":
        if out or req.input.get("input_prefix"):
            raise HTTPException(
                400, "application-managed I/O takes no input_prefix/output_prefix: the run moves its own data"
            )
        out = None
    if out:
        bucket = artifacts.storage(p.namespace, region)["bucket"]
        if not str(out).startswith(f"s3://{bucket}/"):
            raise HTTPException(400, f"output_prefix must be inside the tenant's bucket s3://{bucket}/")
    if req.input.get("image"):
        check_image_allowed(p.namespace, str(req.input["image"]), region)


def _fleet_run(
    m: dict,
    req,
    p,
    name: str,
    timeout: int | None,
    idempotency_key: str | None,
    ex: dict | None = None,
) -> dict:
    """Fleet placement (control cluster, docs/JOBS.md): the Job is created on the manager and MultiKueue +
    the dispatcher pick the worker among the model's regions and GPU classes. Inputs and outputs live in the
    tenant's hub-region bucket (the one this cluster's tenant-storage names). Per-region parameter defaults
    apply when every deployment agrees on them, or when the caller pins a region. Image references are
    fleet-wide (the logical registry host, docs/IMAGES.md), so an unpinned run is placed at queue time: the
    dispatcher nominates the cheapest free cluster and re-nominates while the run waits."""
    application = bool(ex and ex.get("io_mode") == "application")
    bucket = None if application else artifacts.storage(p.namespace, REGION)["bucket"]
    inp = req.input.get("input_prefix")
    if inp and not str(inp).startswith(f"s3://{bucket}/"):
        raise HTTPException(
            400,
            f"input_prefix must be in the tenant's fleet bucket s3://{bucket}/ (upload through this API, or pass region to run next to another bucket)",
        )
    placement = execution.narrow_placement(
        {"manager": True, "profile": profile_of(m), "classes": gpu_classes(m), "regions": m["regions"]}, ex
    )
    region = req.region
    defaults = catalog.region_params(m, region) if region else catalog.fleet_params(m)
    defaults.setdefault("output_prefix", "" if application else f"s3://{bucket}/operations/{name}")
    return build_run(
        name,
        m,
        req.input,
        p.tenant,
        req.name,
        idempotency_key,
        timeout,
        req.priority,
        region,
        defaults,
        keys.key_hash(p.key),
        placement=placement,
        gpu_class=_class_for(m, req, region, p.tenant, ex, placement.get("regions")),
        ex=ex,
    )


def _class_for(
    m: dict,
    req,
    region: str | None,
    tenant: str | None = None,
    ex: dict | None = None,
    regions: list[str] | None = None,
) -> str | None:
    """A run class with per-GPU-class images gets its class at submission (docs/SCHEDULING.md "Per-GPU images for
    runs"): the best pool of the fleet document among the model's classes and regions (the caller's region when
    given, services/api/placement.py). None for a class with one image: Kueue may then switch class at queue time as before."""
    if not class_images(m):
        return None
    gpus = (m.get("job") or {}).get("gpu", 1)
    try:
        gpus = int(gpus)
    except (TypeError, ValueError):  # a parameter placeholder: one GPU for the ranking
        gpus = (
            int(req.input.get("gpus", 1) or 1)
            if isinstance(req.input.get("gpus", 1), (int, str)) and str(req.input.get("gpus", 1)).isdigit()
            else 1
        )
    available = (
        (ex.get("placement") or {}).get("gpu_preference") if ex else None
    )  # the caller's order narrows the model's
    cls, how = placement.choose_class(
        m, region, gpus, available, tenant, regions
    )  # `regions`: the run's allowed clusters
    log.info("%s: GPU class %s chosen by %s", m["name"], cls, how)
    return cls


def render_submission(
    m: dict,
    req,
    p,
    region: str,
    mode: str,
    idempotency_key: str | None,
    ex: dict | None,
    name: str,
    admitted: str | None,
    manager: bool,
) -> tuple[dict, dict | None, str]:
    """The Job (or JobSet) a submission becomes, before its admission (docs/API.md "Runs", "Endpoint calls"): an
    endpoint call's caller Job in the endpoint's region (its bound private input, the admitted token), a fleet-placed
    run on the manager (the dispatcher picks the worker), or a run in the one region; the owner, the requester and,
    for an endpoint call, the revision it could reach are stamped on it. Returns (job, secret, region): the region
    the Job is created in; `manager`: this API is the fleet manager (FLEET_MANAGER, the route's)."""
    _check_submission(mode, ex, req, region, manager)
    timeout = req.timeout_s  # no deadline unless asked for: queued work waits for as long as it takes (days)
    secret, rollout = None, None
    if mode == "async":  # the caller Job runs in the endpoint's region, through that region's gateway
        rollout = _rollout_fence(m, region, ex)
        job, secret = build_async(
            name,
            m,
            req.input,
            p.tenant,
            p.key,
            req.name,
            idempotency_key,
            timeout,
            _output_prefix(p.namespace, region, name, ex),
            region,
            ex=ex,
            key_hash=keys.key_hash(p.key),
            admitted=admitted,
        )
    elif manager:
        _guard_run_input(m, req, p, REGION, ex)
        job = _fleet_run(m, req, p, name, timeout, idempotency_key, ex)
        region = REGION  # created on the manager; the dispatcher picks the worker
    else:
        _guard_run_input(m, req, p, region, ex)
        defaults = catalog.region_params(m, region)
        defaults.setdefault("output_prefix", _output_prefix(p.namespace, region, name, ex))
        job = build_run(
            name,
            m,
            req.input,
            p.tenant,
            req.name,
            idempotency_key,
            timeout,
            req.priority,
            region,
            defaults,
            keys.key_hash(p.key),
            gpu_class=_class_for(m, req, region, p.tenant, ex),
            ex=ex,
        )
    ann = job["metadata"].setdefault("annotations", {})
    ann[f"{LABEL}/owner"] = p.owner
    instances.annotate_requester(
        job, req.requester
    )  # who asked, as the application states it (docs/API.md "Requester")
    if rollout:
        # what the first request could reach at admission (docs/API.md "Models"): evidence of the ready revision and its
        # image then, not per-request provenance (Knative adds no revision to a response)
        if rollout.get("latest_ready"):
            ann[f"{LABEL}/endpoint-revision"] = str(rollout["latest_ready"])
        if rollout.get("serving_images"):
            ann[f"{LABEL}/endpoint-image"] = str(rollout["serving_images"][0])[:1000]
    return job, secret, region


def _check_submission(mode: str, ex: dict | None, req, region: str, manager: bool) -> None:
    """What a submission may ask for: an endpoint call with private input and application-owned output (docs/API.md
    "Endpoint calls") takes no run-only options and no inline request; `require_converged` is a call's; an explicit
    region must be among the caller's allowed_clusters (an implicit one, the default, is no request: a fleet run is
    placed within allowed_clusters, a single-cluster run must be allowed here)."""
    if mode == "async" and ex:
        if ex.get("placement") or ex.get("recovery"):
            raise HTTPException(400, "execution.placement and execution.recovery apply to runs, not to endpoint calls")
        if ex.get("io_mode") == "application" and not ex.get("private_input_ref"):
            raise HTTPException(
                400,
                "an application-I/O endpoint call takes its request and its destination from a binding: give private_input_ref",
            )
        if ex.get("private_input_ref") and req.input:
            raise HTTPException(
                400, "the request of an endpoint call with private_input_ref lives in the binding: input must be empty"
            )
    if ex and ex.get("require_converged") and mode != "async":
        raise HTTPException(400, "execution.require_converged applies to endpoint calls (mode async)")
    allowed = (ex.get("placement") or {}).get("allowed_clusters") if ex else None
    if allowed and req.region and req.region not in allowed and kube.cluster_region(req.region) not in allowed:
        raise HTTPException(400, f"region {req.region} is not in allowed_clusters {allowed}")
    if allowed and not manager and mode == "run" and region not in allowed:
        raise HTTPException(400, f"this API runs jobs in {region} only, which allowed_clusters {allowed} excludes")
