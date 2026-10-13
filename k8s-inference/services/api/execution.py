"""Execution options of a run (docs/JOBS.md "Execution options"): the `execution` block of an invoke request.

    io_mode               managed (default: the tenant bucket, fetch and uploader) | application (the run does its own I/O:
                          no bucket, no tenant S3 credentials, no fetch, the uploader only records the attempt)
    profile_ref           an execution profile of the tenant (terraform.tfvars `tenants.<name>.execution_profiles`): the
                          pod gets the label serverless2.nebius/execution-profile=<name>, which selects the profile's
                          network policy (charts/tenant) in place of the tenant-wide "world" egress, and the shared
                          filesystem stays out of the pod unless the profile allows it
    workload_identity_ref an identity of the tenant (`tenants.<name>.identities`): the pod runs as the ServiceAccount
                          identity-<ref> and the main container gets a projected service-account token with the
                          identity's audience at /var/run/secrets/workload-identity/token (plus the identity's env, e.g.
                          AWS_ROLE_ARN), for OIDC federation against the cluster's issuer
    private_input_ref     a binding of the tenant (`PUT /v1/bindings/<ref>`): its data is delivered to the main container
                          only, as files under /var/run/serverless2/private, from a per-operation Secret; a binding
                          serves one operation (and its resumes); a new PUT refreshes the files of a running attempt
    placement             gpu_preference (narrows and orders the model's classes), allowed_clusters (narrows its regions),
                          fallback_wait_s (docs/SCHEDULING.md)
    recovery              on_infrastructure_loss: retry (default) | fail | recover; checkpoint_ref: handed to the run as
                          CHECKPOINT_REF. `recover` (opt-in): after a confirmed infrastructure loss the control API moves
                          the run to the next class of its preference from its newest acknowledged checkpoint pointer,
                          bounded by max_recoveries (1..3, default 1), wait_s (default 300) and deadline_s
                          (docs/SCHEDULING.md "Automatic recovery after infrastructure loss")

References are resolved against what the operator rendered into the tenant namespace (annotations of the namespace,
charts/tenant), never against raw Kubernetes names: a reference another tenant owns does not exist here. Nothing
confidential enters the Job: the resolved block (names only) is an annotation, the data lives in Secrets.
"""

import json
import re
import time

from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import kube
from config import LABEL, REGION
from resilience import retry
from schemas import RECOVERY_MAX as RECOVERY_MAX
from schemas import BindingRequest as BindingRequest
from schemas import ExecutionSpec as ExecutionSpec
from schemas import PlacementSpec as PlacementSpec
from schemas import RecoverySpec as RecoverySpec

REF = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
IO_MODES = ("managed", "application")
PRIVATE_DIR = "/var/run/serverless2/private"
IDENTITY_DIR = "/var/run/secrets/workload-identity"
RUNNER_TOKEN_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
IDENTITIES_ANNOTATION = f"{LABEL}/identities"
PROFILES_ANNOTATION = f"{LABEL}/execution-profiles"
BINDING_PREFIX = "binding-"
MAX_BINDING_BYTES = 64 * 1024
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # a binding key is a file name under the private directory


RECOVERY_DEFAULTS = {"max_recoveries": 1, "wait_s": 300}


_cache: dict[str, tuple[float, dict]] = {}


def tenant_config(ns: str, region: str = REGION) -> dict:
    """The identities and execution profiles the operator rendered for the tenant (namespace annotations, charts/tenant)."""
    hit = _cache.get(f"{region}/{ns}")
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        ann = retry(kube.core(region).read_namespace, ns).metadata.annotations or {}
    except ApiException as e:
        raise HTTPException(503 if e.status != 404 else 404, f"tenant namespace {ns}: {e.reason}")
    out = {}
    for key, name in ((IDENTITIES_ANNOTATION, "identities"), (PROFILES_ANNOTATION, "execution_profiles")):
        try:
            out[name] = json.loads(ann.get(key) or "{}")
        except ValueError:
            out[name] = {}
    _cache[f"{region}/{ns}"] = (time.time() + 60, out)
    return out


def capabilities(ns: str, region: str = REGION) -> dict:
    """Capability discovery (`GET /v1/fleet`): what this tenant may ask for, so a caller can tell an unsupported
    feature from a misconfiguration."""
    from config import REGION_OIDC_ISSUERS

    cfg = tenant_config(ns, region)
    return {
        "io_modes": list(IO_MODES),
        "identities": sorted(cfg["identities"]),
        "execution_profiles": sorted(cfg["execution_profiles"]),
        "oidc_issuers": dict(REGION_OIDC_ISSUERS),
        "private_input_dir": PRIVATE_DIR,
        "identity_token_file": f"{IDENTITY_DIR}/token",
        # durable endpoint calls with private input and application-owned output (docs/API.md "Endpoint calls"): the
        # binding's file contract, per-attempt URL refresh, retained capture for transfer-only export retries
        "async_private_io": {
            "version": 1,
            "refresh": True,
            "retained_export": True,
            "binding_keys": [
                "request.json",
                "input_url",
                "response_url",
                "call_url",
                "result_ref",
                "refresh_url",
                "refresh_token",
            ],
        },
        # automatic continuation after a confirmed infrastructure loss (docs/SCHEDULING.md): what a caller may ask for
        # application finalizer (docs/JOBS.md "Finalization by the application"): the model-owned command that re-delivers
        "finalize": {
            "roles": ["run", "export", "finalize"],
            "max_retries": 3,
            "trigger": "POST /v1/operations/{id}:export",
            "declared_by": "the model's job.finalize",
        },
        "recovery": {
            "on_infrastructure_loss": ["retry", "fail", "recover"],
            "max_recoveries": RECOVERY_MAX,
            "defaults": dict(RECOVERY_DEFAULTS),
            "requires": "an acknowledged checkpoint pointer (CHECKPOINT.json) and a gpu_preference with a next class",
        },
    }


def _ref(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not REF.match(value):
        raise HTTPException(400, f"{what}: a DNS label (lowercase letters, digits, dashes)")
    return value


def resolve(spec: ExecutionSpec | None, ns: str, model: dict, region: str = REGION) -> dict | None:
    """The execution block of a request, checked against the tenant's rendered identities and profiles and the
    model's classes and regions. Returns the resolved block (names only) or None when the request has none."""
    if spec is None:
        return None
    cfg = tenant_config(ns, region)
    ex: dict = {"io_mode": spec.io_mode}
    if name := _ref(spec.profile_ref, "profile_ref"):
        profile = cfg["execution_profiles"].get(name)
        if profile is None:
            raise HTTPException(
                403, f"profile_ref {name!r} is not an execution profile of this tenant (GET /v1/fleet lists them)"
            )
        ex["profile"] = {"name": name, "shared_filesystem": bool(profile.get("shared_filesystem"))}
    if name := _ref(spec.workload_identity_ref, "workload_identity_ref"):
        identity = cfg["identities"].get(name)
        if identity is None:
            raise HTTPException(
                403, f"workload_identity_ref {name!r} is not an identity of this tenant (GET /v1/fleet lists them)"
            )
        env = identity.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise HTTPException(500, f"identity {name}: env must map names to strings (charts/tenant)")
        ex["identity"] = {
            "ref": name,
            "service_account": f"identity-{name}",
            "audience": str(identity.get("audience") or ""),
            "env": env,
        }
    if name := _ref(spec.private_input_ref, "private_input_ref"):
        ex["private_input_ref"] = name
    if spec.placement:
        pl = {}
        if spec.placement.gpu_preference is not None:
            classes = model.get("gpu_classes") or []
            pref = [c for c in spec.placement.gpu_preference if c in classes]
            if not pref:
                raise HTTPException(
                    400,
                    f"gpu_preference {spec.placement.gpu_preference} has no class of model {model.get('name')} ({classes})",
                )
            pl["gpu_preference"] = pref
        if spec.placement.allowed_clusters is not None:
            regions = [r for r in spec.placement.allowed_clusters if r in (model.get("regions") or [])]
            if not regions:
                raise HTTPException(
                    400,
                    f"allowed_clusters {spec.placement.allowed_clusters} has no region of model {model.get('name')} ({model.get('regions')})",
                )
            pl["allowed_clusters"] = regions
        if spec.placement.fallback_wait_s is not None:
            pl["fallback_wait_s"] = spec.placement.fallback_wait_s
        if pl:
            ex["placement"] = pl
    if spec.graph and spec.stage:
        raise HTTPException(400, "execution.graph declares a root's stages; execution.stage submits one: not both")
    if spec.graph:
        ex["graph"] = validate_graph(spec.graph.stages, spec.graph.deadline_s)
    if spec.stage:
        if not (STAGE_ID.match(spec.stage.id) and _ref(spec.stage.root, "stage.root")):
            raise HTTPException(400, "execution.stage: id (letters, digits, -) and root are required")
        ex["stage"] = {"root": spec.stage.root, "id": spec.stage.id}
    if spec.recovery:
        ex["recovery"] = {"on_infrastructure_loss": spec.recovery.on_infrastructure_loss}
        if spec.recovery.checkpoint_ref:
            ex["recovery"]["checkpoint_ref"] = spec.recovery.checkpoint_ref
        bounds = {
            k: getattr(spec.recovery, k)
            for k in ("max_recoveries", "wait_s", "deadline_s")
            if getattr(spec.recovery, k) is not None
        }
        if spec.recovery.on_infrastructure_loss == "recover":
            ex["recovery"].update({**RECOVERY_DEFAULTS, **bounds})
        elif bounds:
            raise HTTPException(400, f"recovery.{'/'.join(bounds)}: only with on_infrastructure_loss: recover")
    return ex


STAGE_ID = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")
MAX_STAGES = 256  # ESMFold-class fan-outs: up to 128 shards plus their collector, with room
ROOT_STAGE = "root"


def validate_graph(stages: dict, deadline_s: int | None) -> dict:
    """A declared stage graph: 1..MAX_STAGES stage ids, dependencies among them (or `root`), no cycle, no stage named
    `root`, no self-dependency, no duplicate dependency. Returns the resolved block."""
    if not stages or len(stages) > MAX_STAGES:
        raise HTTPException(400, f"execution.graph.stages: 1 to {MAX_STAGES} stages")
    for sid, deps in stages.items():
        if sid == ROOT_STAGE or not STAGE_ID.match(sid):
            raise HTTPException(400, f"execution.graph.stages: stage id {sid!r} (letters, digits, -; not 'root')")
        if not isinstance(deps, list) or len(deps) != len(set(deps)):
            raise HTTPException(400, f"stage {sid!r}: dependencies must be a list without duplicates")
        for d in deps:
            if d == sid:
                raise HTTPException(400, f"stage {sid!r} depends on itself")
            if d != ROOT_STAGE and d not in stages:
                raise HTTPException(400, f"stage {sid!r} depends on {d!r}, which is not declared")
    # cycles: depth-first over the declared edges
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise HTTPException(400, f"execution.graph.stages: cycle through {' -> '.join(path + [n])}")
        state[n] = 1
        for d in stages.get(n) or []:
            if d != ROOT_STAGE:
                visit(d, path + [n])
        state[n] = 2

    for sid in stages:
        visit(sid, [])
    out = {"stages": {sid: list(deps) for sid, deps in stages.items()}}
    if deadline_s:
        out["deadline_s"] = int(deadline_s)
    return out


def narrow_placement(placement: dict | None, ex: dict | None) -> dict | None:
    """The caller's gpu_preference / allowed_clusters narrow (never widen) the model's classes and regions."""
    if not placement or not ex or not ex.get("placement"):
        return placement
    out = dict(placement)
    pl = ex["placement"]
    if pl.get("gpu_preference"):
        out["classes"] = [c for c in pl["gpu_preference"] if c in (placement.get("classes") or pl["gpu_preference"])]
    if pl.get("allowed_clusters"):
        out["regions"] = [
            r for r in pl["allowed_clusters"] if r in (placement.get("regions") or pl["allowed_clusters"])
        ]
    return out


# ---- the pod ---------------------------------------------------------------------------------------------------


def scope_runner_token(pod: dict) -> None:
    """No run pod mounts the ServiceAccount token into the model's container: the token (pods get, the work volume) is
    projected into the uploader only (docs/JOBS.md "Execution options"); PSA baseline allows projected volumes."""
    pod["automountServiceAccountToken"] = False
    if not any(c.get("name") == "uploader" for c in pod.get("containers", [])):
        return
    pod.setdefault("volumes", []).append(
        {
            "name": "runner-token",
            "projected": {
                "sources": [
                    {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
                    {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
                    {
                        "downwardAPI": {
                            "items": [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]
                        }
                    },
                ]
            },
        }
    )
    for c in pod["containers"]:
        if c.get("name") == "uploader":
            c.setdefault("volumeMounts", []).append(
                {"name": "runner-token", "mountPath": RUNNER_TOKEN_DIR, "readOnly": True}
            )


def apply_run(pod: dict, r: dict, ex: dict, name: str, s3_secret: str) -> None:
    """Shape a run pod after its execution block (build_run calls this before the Job shell is built)."""
    main = next(c for c in pod["containers"] if c["name"] == "main")
    uploader = next((c for c in pod["containers"] if c["name"] == "uploader"), None)
    if ex.get("io_mode") == "application":
        pod["initContainers"] = [c for c in pod.get("initContainers", []) if c.get("name") != "fetch"]
        main["envFrom"] = [e for e in main.get("envFrom", []) if (e.get("secretRef") or {}).get("name") != s3_secret]
        if not main["envFrom"]:
            del main["envFrom"]
        if uploader:
            uploader.pop("envFrom", None)
            uploader["env"] = [e for e in uploader["env"] if e["name"] != "OUTPUT_PREFIX"] + [
                {"name": "UPLOAD_MODE", "value": "record"}
            ]
    profile = ex.get("profile")
    if (
        profile
        and not profile.get("shared_filesystem")
        and (r.get("data") or str(r.get("checkpoints") or "") == "shared")
    ):
        raise HTTPException(
            400,
            f"execution profile {profile['name']} keeps the shared filesystem out of the run: no data, no shared checkpoints",
        )
    identity = ex.get("identity")
    if identity:
        pod["serviceAccountName"] = identity["service_account"]
        token = {"path": "token", "expirationSeconds": 3600}
        if identity.get("audience"):
            token["audience"] = identity["audience"]
        pod.setdefault("volumes", []).append(
            {"name": "workload-identity", "projected": {"sources": [{"serviceAccountToken": token}]}}
        )
        main.setdefault("volumeMounts", []).append(
            {"name": "workload-identity", "mountPath": IDENTITY_DIR, "readOnly": True}
        )
        env = {e["name"]: e for e in main.get("env", [])}
        env["AWS_WEB_IDENTITY_TOKEN_FILE"] = {"name": "AWS_WEB_IDENTITY_TOKEN_FILE", "value": f"{IDENTITY_DIR}/token"}
        for k, v in identity.get("env", {}).items():
            env[k] = {"name": k, "value": v}  # the identity's values win over the caller's
        main["env"] = list(env.values())
    if ex.get("private_input_ref"):
        pod.setdefault("volumes", []).append(
            {"name": "private-input", "secret": {"secretName": f"{name}-private", "defaultMode": 0o440}}
        )
        main.setdefault("volumeMounts", []).append(
            {"name": "private-input", "mountPath": PRIVATE_DIR, "readOnly": True}
        )
        main.setdefault("env", []).append({"name": "PRIVATE_INPUT_DIR", "value": PRIVATE_DIR})
    ckpt = (ex.get("recovery") or {}).get("checkpoint_ref")
    if ckpt:
        main.setdefault("env", []).append({"name": "CHECKPOINT_REF", "value": ckpt})


def apply_async(pod: dict, ex: dict, name: str) -> None:
    """Shape an endpoint-call pod (mode async) after its execution block (docs/API.md "Endpoint calls"): the private
    binding mounted for the caller, the workload identity projected, the uploader in record mode (the caller delivers
    to the application's own destination; the platform bucket is not used)."""
    main = next(c for c in pod["containers"] if c["name"] == "main")
    uploader = next((c for c in pod["containers"] if c["name"] == "uploader"), None)
    if ex.get("io_mode") == "application" and uploader:
        uploader.pop("envFrom", None)
        uploader["env"] = [e for e in uploader["env"] if e["name"] != "OUTPUT_PREFIX"] + [
            {"name": "UPLOAD_MODE", "value": "record"}
        ]
        main.setdefault("env", []).append({"name": "IO_MODE", "value": "application"})
    identity = ex.get("identity")
    if identity:
        pod["serviceAccountName"] = identity["service_account"]
        token = {"path": "token", "expirationSeconds": 3600}
        if identity.get("audience"):
            token["audience"] = identity["audience"]
        pod.setdefault("volumes", []).append(
            {"name": "workload-identity", "projected": {"sources": [{"serviceAccountToken": token}]}}
        )
        main.setdefault("volumeMounts", []).append(
            {"name": "workload-identity", "mountPath": IDENTITY_DIR, "readOnly": True}
        )
        main.setdefault("env", []).append({"name": "IDENTITY_TOKEN_FILE", "value": f"{IDENTITY_DIR}/token"})
        for k, v in identity.get("env", {}).items():
            main["env"].append({"name": k, "value": v})
    if ex.get("private_input_ref"):
        pod.setdefault("volumes", []).append(
            {"name": "private-input", "secret": {"secretName": f"{name}-private", "defaultMode": 0o440}}
        )
        main.setdefault("volumeMounts", []).append(
            {"name": "private-input", "mountPath": PRIVATE_DIR, "readOnly": True}
        )
        main.setdefault("env", []).append({"name": "PRIVATE_INPUT_DIR", "value": PRIVATE_DIR})


def apply_job(job: dict, ex: dict) -> None:
    """Recovery policy on the Job: `fail` turns an infrastructure loss into a FAILED operation (resumable with a
    checkpoint reference) instead of a replacement attempt."""
    if (ex.get("recovery") or {}).get("on_infrastructure_loss") == "fail" and job.get("kind") == "Job":
        job["spec"]["backoffLimit"] = 0
        job["spec"]["podFailurePolicy"] = {
            "rules": [{"action": "FailJob", "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}]}]
        }


# ---- bindings: confidential inputs delivered per operation ------------------------------------------------------


def _secret(region: str, ns: str, name: str) -> dict | None:
    try:
        s = retry(kube.core(region).read_namespaced_secret, name, ns)
        return kube.core(region).api_client.sanitize_for_serialization(s)
    except ApiException as e:
        if e.status == 404:
            return None
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")


OWNER_ANNOTATION = f"{LABEL}/owner"
BINDING_UID_ANNOTATION = f"{LABEL}/binding-uid"


def _owned_by(secret: dict, owner: str | None, admin: bool) -> bool:
    """The binding is the caller's: its owner annotation (the stable principal the API records on every operation,
    auth.Principal.owner) equals the caller's. An admin may act on any binding of the tenant. A binding without an
    owner (written before owners existed) belongs to the principal of the operation it is bound to, when the fleet
    database knows that owner (the in-flight runs of the day owners arrived); unbound, it is nobody's to an ordinary
    caller: its owner re-creates it."""
    if admin:
        return True
    if not owner:
        return False
    ann = secret["metadata"].get("annotations") or {}
    current = ann.get(OWNER_ANNOTATION)
    if current:
        return current == owner
    bound = ann.get(f"{LABEL}/operation")
    tenant = (secret["metadata"].get("labels") or {}).get(f"{LABEL}/tenant")
    return bool(bound) and bool(tenant) and _operation_owner(bound, tenant) == owner


def _operation_owner(operation: str, tenant: str) -> str | None:
    """The durable owner of an operation of `tenant` (keys.operation_owner): the binding's own tenant label and the
    record's tenant must agree, so a record of another tenant never vouches for a binding."""
    import db
    import keys

    if not db.enabled():
        return None
    rec = keys.operation_owner(operation)
    return rec[1] if rec and rec[0] == tenant else None


def put_binding(
    ns: str,
    tenant: str,
    ref: str,
    data: dict[str, str],
    owner: str | None = None,
    admin: bool = False,
    if_match: str | None = None,
    region: str = REGION,
) -> dict:
    """Create or replace the caller's binding `ref`; a replacement also refreshes the copies of the operation it is
    bound to (the files in a running attempt change within the kubelet's sync period, no restart). A binding has an
    owner (the caller's stable principal, set at creation and never changed); another principal of the tenant sees
    404. `if_match` (the `revision` a read returned) and the Secret's resourceVersion make the replacement a
    compare-and-swap: a concurrent write answers 409."""
    _ref(ref, "binding")
    if (
        not data
        or sum(len(k) + len(v) for k, v in data.items()) > MAX_BINDING_BYTES
        or not all(KEY.match(k) and ".." not in k for k in data)
    ):
        raise HTTPException(
            400, "binding data: 1 to 64 KiB of string values under file-name keys (letters, digits, . _ -)"
        )
    name = BINDING_PREFIX + ref
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "labels": {f"{LABEL}/tenant": tenant, f"{LABEL}/binding": ref}},
        "stringData": data,
    }
    core = kube.core(region)
    existing = _secret(region, ns, name)
    if existing and not _owned_by(existing, owner, admin):
        raise HTTPException(404, f"binding {ref}: not found")
    revision = (existing or {}).get("metadata", {}).get("resourceVersion")
    if existing and if_match is not None and if_match != str(revision):
        raise HTTPException(409, f"binding {ref} changed (revision {revision}); re-read it before replacing it")
    written = None
    try:
        if existing:
            ann = dict(existing["metadata"].get("annotations") or {})
            ann.setdefault(OWNER_ANNOTATION, owner or "")
            body["metadata"]["annotations"] = {k: v for k, v in ann.items() if v}
            if revision:
                body["metadata"]["resourceVersion"] = str(revision)  # the API server refuses a stale replacement
            body["data"] = None
            written = retry(core.replace_namespaced_secret, name, ns, body)
        else:
            body["metadata"]["annotations"] = {OWNER_ANNOTATION: owner} if owner else {}
            written = retry(core.create_namespaced_secret, ns, body)
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(409, f"binding {ref} was written concurrently; re-read it and replace it again")
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    bound = ((existing or {}).get("metadata", {}).get("annotations") or {}).get(f"{LABEL}/operation")
    if bound:
        _refresh_copies(ns, ref, data, existing)
    return {
        "ref": ref,
        "operation": bound,
        "keys": sorted(data),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "owner": (body["metadata"].get("annotations") or {}).get(OWNER_ANNOTATION),
        "revision": _revision(written),
    }


def _revision(obj) -> str | None:
    if obj is None:
        return None
    md = obj.get("metadata") if isinstance(obj, dict) else getattr(obj, "metadata", None)
    rv = md.get("resourceVersion") if isinstance(md, dict) else getattr(md, "resource_version", None)
    return str(rv) if rv else None


def list_bindings(ns: str, owner: str | None = None, admin: bool = False, region: str = REGION) -> list[dict]:
    """The caller's bindings (an admin: every binding of the tenant, with its owner)."""
    try:
        items = _list(region, ns, f"{LABEL}/binding")
    except ApiException as e:
        raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    out = []
    for s in items:
        md = s["metadata"]
        if not md["name"].startswith(BINDING_PREFIX):
            continue  # a per-operation copy
        if not _owned_by(s, owner, admin):
            continue
        out.append(
            {
                "ref": (md.get("labels") or {}).get(f"{LABEL}/binding"),
                "operation": (md.get("annotations") or {}).get(f"{LABEL}/operation"),
                "keys": sorted((s.get("data") or {}).keys()),
                "updated_at": md.get("creationTimestamp"),
                "owner": (md.get("annotations") or {}).get(OWNER_ANNOTATION),
                "revision": _revision(s),
            }
        )
    return out


def _list(region: str, ns: str, selector: str) -> list[dict]:
    core = kube.core(region)
    return [
        core.api_client.sanitize_for_serialization(s)
        for s in retry(core.list_namespaced_secret, ns, label_selector=selector).items
    ]


def delete_binding(ns: str, ref: str, owner: str | None = None, admin: bool = False, region: str = REGION) -> bool:
    """Revocation by the binding's owner (or an admin): the copies of a bound operation are emptied first (the files
    of a running attempt become empty), then removed with the binding. Another principal's binding: not found."""
    _ref(ref, "binding")
    existing = _secret(region, ns, BINDING_PREFIX + ref)
    if existing is None or not _owned_by(existing, owner, admin):
        return False
    # the original goes first, conditionally on being the object that was read (uid and resourceVersion): a binding
    # re-created under the same ref meanwhile is never deleted by a late revoker; then its copies, by its identity
    md = existing["metadata"]
    try:
        retry(
            kube.core(region).delete_namespaced_secret,
            BINDING_PREFIX + ref,
            ns,
            body=_preconditions(md.get("uid"), md.get("resourceVersion")),
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(409, f"binding {ref} changed meanwhile; re-read it before revoking it")
        if e.status != 404:
            raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    _refresh_copies(ns, ref, {"revoked": ""}, existing, delete=True)
    return True


def _refresh_copies(ns: str, ref: str, data: dict[str, str], binding: dict, delete: bool = False) -> None:
    """Replace (or empty and delete) the per-operation copies of `binding`: those under its ref that carry its uid, or
    (copies from before the uid existed) those of the operation it is bound to. Each replacement keeps the copy's
    labels, annotations and owner references and is a compare-and-swap on the copy's resourceVersion; a deletion
    requires the copy's uid and resourceVersion. A copy that changed meanwhile is left alone (the next write sees it)."""
    md = binding.get("metadata") or {}
    uid = md.get("uid")
    bound = (md.get("annotations") or {}).get(f"{LABEL}/operation")
    for r in kube.regions():
        try:
            items = _list(r, ns, f"{LABEL}/binding={ref}")
        except (ApiException, OSError):
            continue
        for s in items:
            name = s["metadata"]["name"]
            if name.startswith(BINDING_PREFIX):
                continue
            ann = dict(s["metadata"].get("annotations") or {})
            copy_uid = ann.get(BINDING_UID_ANNOTATION)
            if copy_uid:
                if not uid or copy_uid != uid:
                    continue  # a copy of another binding that had this ref
            elif not bound or (s["metadata"].get("labels") or {}).get(f"{LABEL}/operation") != bound:
                continue  # an old copy without identity: only the bound operation's own
            if uid:
                ann[BINDING_UID_ANNOTATION] = uid
            rv = s["metadata"].get("resourceVersion")
            body = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "labels": s["metadata"].get("labels"),
                    "annotations": ann,
                    "ownerReferences": s["metadata"].get("ownerReferences"),
                    **({"resourceVersion": str(rv)} if rv else {}),
                },
                "stringData": data,
            }
            try:
                written = retry(kube.core(r).replace_namespaced_secret, name, ns, body)
                if delete:
                    new_rv = _revision(written) or rv
                    retry(
                        kube.core(r).delete_namespaced_secret,
                        name,
                        ns,
                        body=_preconditions(s["metadata"].get("uid"), new_rv),
                    )
            except ApiException as e:
                if e.status in (404, 409):
                    continue  # gone, or changed by a later writer: not ours to touch now
                raise HTTPException(502, f"kubernetes ({r}): {e.reason}")


def _preconditions(uid: str | None, resource_version: str | None):
    """Delete options that make a deletion conditional on the object being the one that was read."""
    from kubernetes import client as k8s

    if not uid and not resource_version:
        return None
    return k8s.V1DeleteOptions(
        preconditions=k8s.V1Preconditions(uid=uid, resource_version=str(resource_version) if resource_version else None)
    )


def deliver(
    ex: dict | None,
    name: str,
    root: str,
    ns: str,
    tenant: str,
    regions: list[str],
    region: str = REGION,
    owner: str | None = None,
) -> None:
    """Before an operation is created: claim its binding (one binding serves one operation and that operation's
    resumes; another operation gets 409) and copy the data into the per-operation Secret `<name>-private` in every
    region the run may land in. Copies carry the operation label; the billing pass removes them when it ends.
    `owner` is the operation's (the root's, for a continuation): only the binding's owner consumes it; another
    principal's binding, or one without an owner, is "no such binding"."""
    ref = (ex or {}).get("private_input_ref")
    if not ref:
        return
    binding = _secret(region, ns, BINDING_PREFIX + ref)
    if binding is None or not _owned_by(binding, owner, False):
        raise HTTPException(404, f"private_input_ref {ref!r}: no such binding (PUT /v1/bindings/{ref} first)")
    ann = binding["metadata"].get("annotations") or {}
    bound = ann.get(f"{LABEL}/operation")
    if bound and bound != root:
        raise HTTPException(
            409, f"binding {ref!r} is bound to operation {bound}; a new operation needs its own binding"
        )
    if not bound or (owner and not ann.get(OWNER_ANNOTATION)):
        # the claim (and, for a binding from before owners existed, the owner it continues under) is written with the
        # Secret's resourceVersion: two operations claiming one binding at once cannot both win (the loser re-reads
        # and finds it bound to the other: 409)
        claim = dict(ann)
        claim[f"{LABEL}/operation"] = root
        if owner:
            claim.setdefault(OWNER_ANNOTATION, owner)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": BINDING_PREFIX + ref,
                "labels": binding["metadata"].get("labels"),
                "annotations": claim,
                **(
                    {"resourceVersion": str(binding["metadata"]["resourceVersion"])}
                    if binding["metadata"].get("resourceVersion")
                    else {}
                ),
            },
            "data": binding.get("data") or {},
        }
        try:
            retry(kube.core(region).replace_namespaced_secret, BINDING_PREFIX + ref, ns, body)
        except ApiException as e:
            if e.status == 409:
                again = _secret(region, ns, BINDING_PREFIX + ref) or {}
                now_bound = (again.get("metadata", {}).get("annotations") or {}).get(f"{LABEL}/operation")
                if now_bound == root:
                    binding = again
                else:
                    raise HTTPException(
                        409,
                        f"binding {ref!r} was claimed concurrently (by {now_bound}); a new operation needs its own binding",
                    )
            else:
                raise HTTPException(502, f"kubernetes ({region}): {e.reason}")
    copy = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"{name}-private",
            "labels": {f"{LABEL}/tenant": tenant, f"{LABEL}/binding": ref, f"{LABEL}/operation": root},
            # the identity of the binding this copy came from: a refresh or revocation of a later binding under the
            # same ref (deleted and re-created, possibly by another principal) never reaches this copy
            "annotations": {
                k: v
                for k, v in {
                    BINDING_UID_ANNOTATION: binding["metadata"].get("uid"),
                    OWNER_ANNOTATION: (binding["metadata"].get("annotations") or {}).get(OWNER_ANNOTATION) or owner,
                    f"{LABEL}/operation": root,
                }.items()
                if v
            },
        },
        "data": binding.get("data") or {},
    }
    # `regions` are the run's candidate clusters as the dispatcher names them (cluster ids: `north`, `south`, `hub`, or a
    # region name); the Kubernetes client is keyed by the Nebius region, so every id is mapped through the fleet
    # document first (kube.cluster_region) and deduplicated (two ids in one region share a cluster only never)
    targets = sorted({kube.cluster_region(r) for r in (regions or [region])})
    for r in targets:
        try:
            retry(kube.core(r).create_namespaced_secret, ns, copy)
        except ApiException as e:
            if e.status == 409:
                continue
            raise HTTPException(502, f"kubernetes ({r}): private input copy: {e.reason}")


def sweep_copies(ns: str, operation: str) -> None:
    """Remove only this finished operation's copies, never those of a live successor sharing its root binding."""
    for r in kube.regions():
        try:
            retry(kube.core(r).delete_namespaced_secret, f"{operation}-private", ns)
        except (ApiException, OSError):
            continue
