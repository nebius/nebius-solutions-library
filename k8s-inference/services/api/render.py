"""How a run becomes a Kubernetes Job: the small pieces every job kind shares (docs/JOBS.md).

- `op_name`: the operation id (deterministic with an idempotency key)
- `profile_of` / `gpu_classes`: the scheduling profile a model asks for
- `_render`: `{{name}}` placeholders of a catalog `job` block
- `_meta`: labels and annotations of the Job (tenant, model, queue, priority, region pin, input)
- `_uploader`: the sidecar that uploads outputs and attempt records
- hardening: the security context of every rendered container and pod
- `_job_shell`: the Job envelope (failure policy, deadline, MultiKueue `managedBy`)

Nothing here talks to Kubernetes; jobs.py does."""
import hashlib, json, re, uuid
from fastapi import HTTPException
from config import JOB_BACKOFF_LIMIT, JOB_TTL_S, KUEUE_PRIORITY, KUEUE_QUEUE, LABEL, MULTIKUEUE_MANAGED_BY, RUNNER_IMAGE, S3_ENV_SECRET

TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
PRIORITY_CLASS = {"high": "serverless2-batch-priority", "low": "serverless2-batch", None: "serverless2-batch", "normal": "serverless2-batch"}


def op_name(tenant: str, model: str, idem: str | None) -> str:
    if idem:
        return "op-" + hashlib.sha256(f"{tenant}|{model}|{idem}".encode()).hexdigest()[:16]
    return f"op-{model[:20].lower().replace('_', '-')}-{uuid.uuid4().hex[:8]}"


def profile_of(model: dict) -> str:
    """Scheduling profile = LocalQueue name in the tenant namespace: `prefer-<first GPU class of the
    catalog entry>` (charts/fleet renders one per class on every cluster), `default` without a preference."""
    classes = gpu_classes(model)
    return f"prefer-{classes[0]}" if classes else KUEUE_QUEUE


def gpu_classes(model: dict) -> list[str]:
    """Catalog `gpu.classes`, preferred first (catalog.py keeps the list as gpu_classes)."""
    return list(model.get("gpu_classes") or [])


def _render(value, params: dict):
    """Substitute {{name}} in strings, recursively; a token without a value is a 400."""
    if isinstance(value, str):
        def sub(m):
            k = m.group(1)
            if k not in params or params[k] is None:
                raise HTTPException(400, f"missing parameter {k}")
            v = params[k]
            return v if isinstance(v, str) else json.dumps(v)
        return TOKEN.sub(sub, value)
    if isinstance(value, list):
        return [_render(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, params) for k, v in value.items()}
    return value


def _meta(name: str, tenant: str, model: str, mode: str, region: str | None, label: str | None, inp: dict, idem: str | None,
          priority: str | None, key_hash: str | None, profile: str = KUEUE_QUEUE) -> dict:
    labels = {f"{LABEL}/tenant": tenant, f"{LABEL}/model": model, f"{LABEL}/mode": mode, f"{LABEL}/profile": profile,
              "kueue.x-k8s.io/queue-name": profile,
              "kueue.x-k8s.io/priority-class": "bulk-backfill" if priority == "low" else KUEUE_PRIORITY}
    if region:                        # pinned: the regional path always, the fleet path for explicit regions and resumes
        labels[f"{LABEL}/region"] = region
    if priority:
        labels[f"{LABEL}/priority"] = priority
    ann = {f"{LABEL}/input": json.dumps(inp)[:60000]}
    if key_hash:
        ann[f"{LABEL}/key"] = key_hash          # sha256 of the submitting key: billing target, never the key itself
    if label:
        ann[f"{LABEL}/name"] = label
    if idem:
        ann[f"{LABEL}/idempotency-key"] = idem
    return {"name": name, "labels": labels, "annotations": ann}


def _uploader(name: str, region: str | None, output_prefix: str, pvc: str | None, excludes: str = "", gpus: int = 0,
              placement: dict | None = None) -> dict:
    env = [{"name": "OPERATION", "value": name}, {"name": "OUTPUT_PREFIX", "value": output_prefix}, {"name": "MAIN_CONTAINER", "value": "main"},
           {"name": "GPUS", "value": str(gpus)},
           {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
           {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}]
    if pvc:
        env.append({"name": "PVC_NAME", "value": pvc})
    if excludes:
        env.append({"name": "UPLOAD_EXCLUDES", "value": excludes})
    return {"name": "uploader", "image": RUNNER_IMAGE, "command": ["/usr/local/bin/upload.sh"], "env": env,
            "envFrom": [{"secretRef": {"name": S3_ENV_SECRET}}],
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
            "volumeMounts": [{"name": "work", "mountPath": "/work"}], "securityContext": RUNNER_SECURITY_CONTEXT}


# Hardening of every rendered pod (docs/SECURITY-PREREVIEW.md F1): no privilege escalation, no capabilities, the
# runtime's default seccomp profile. The runner containers (fetch, uploader, call) run as the image's uid 10001;
# the model container keeps the image's user unless the job class sets `runAsNonRoot` (GROMACS/NIM images are root).
# Together with Pod Security Admission `baseline` on the tenant namespaces (charts/tenant) this is what the API
# can promise: it never renders a privileged pod.
CONTAINER_HARDENING = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                       "seccompProfile": {"type": "RuntimeDefault"}}
RUNNER_SECURITY_CONTEXT = {**CONTAINER_HARDENING, "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001}
POD_SECURITY_CONTEXT = {"seccompProfile": {"type": "RuntimeDefault"}}


def _pod_failure_policy() -> dict:
    return {"rules": [
        {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}]},   # node drain / preemption
        {"action": "Ignore", "onExitCodes": {"containerName": "main", "operator": "In", "values": [137, 143]}},   # SIGKILL / SIGTERM
    ]}


def _job_shell(name: str, meta: dict, pod_spec: dict, timeout_s: int | None, placement: dict | None = None) -> dict:
    """placement (fleet path): {manager: bool, classes: [...], regions: [...]}; the pod template carries what
    the dispatcher needs (it sees the Workload's pod template, not the Job): allowed GPU classes and regions,
    work volume size."""
    tmpl_meta = {"labels": {k: v for k, v in meta["labels"].items() if k.startswith(LABEL)}}
    ann = {}
    if meta["annotations"].get(f"{LABEL}/pvc-size-gi"):
        ann[f"{LABEL}/pvc-size-gi"] = meta["annotations"][f"{LABEL}/pvc-size-gi"]
    if placement and placement.get("classes"):
        ann[f"{LABEL}/gpu-classes"] = ",".join(placement["classes"])
    if placement and placement.get("regions"):
        ann[f"{LABEL}/regions"] = ",".join(placement["regions"])
    if ann:
        tmpl_meta["annotations"] = ann
    spec = {"parallelism": 1, "completions": 1, "backoffLimit": JOB_BACKOFF_LIMIT, "ttlSecondsAfterFinished": JOB_TTL_S,
            "podFailurePolicy": _pod_failure_policy(), "template": {"metadata": tmpl_meta, "spec": pod_spec}}
    if timeout_s:
        spec["activeDeadlineSeconds"] = timeout_s
    if placement and placement.get("manager"):
        spec["managedBy"] = MULTIKUEUE_MANAGED_BY     # the manager's Job controller leaves it to Kueue MultiKueue
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta, "spec": spec}


