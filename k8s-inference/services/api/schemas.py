"""Shared API schemas. FastAPI generates /openapi.json from these types.

Requests are shared with the model and execution validators; response types document the wire shapes
without adding defaults or filtering handler output. Contract tests exercise the actual responses.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from config import REGION

INVOKE_TIMEOUT_MAX_S = 14 * 86400  # the long-run contract: 14-day MD runs with an explicit, budgeted timeout
RECOVERY_MAX = 3


class ScalingSpec(BaseModel):
    """The scaling vocabulary of an endpoint (docs/API.md "Scaling"), rendered onto Knative's KPA by `to_entry`:
    `concurrency_utilization` scales on `target` percent of `container_concurrency` (the per-replica request limit),
    `requests_per_second` on `target` requests/s per replica with no concurrency limit."""

    model_config = ConfigDict(extra="forbid")
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=1)
    metric: Literal["concurrency_utilization", "requests_per_second"] | None = None
    target: int | None = Field(default=None, ge=1)
    container_concurrency: int | None = Field(default=None, ge=0, le=1000)
    cooldown_s: int | None = Field(default=None, ge=0, le=3600)
    window_s: int | None = Field(default=None, ge=6, le=300)
    idle_s: int | None = Field(default=None, ge=0, le=3600)


class EphemeralVolumeSpec(BaseModel):
    """Endpoint scratch space, not a host path, customer bucket or shared cache."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    mount_path: str = Field(pattern=r"^/[A-Za-z0-9_/-]+$")
    size_limit: str = Field(pattern=r"^[1-9][0-9]*(Mi|Gi)$")
    memory: bool = False


class PlacementSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gpu_preference: list[str] | None = None
    allowed_clusters: list[str] | None = None
    fallback_wait_s: int | None = Field(default=None, ge=0, le=7 * 86400)


class RecoverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_infrastructure_loss: Literal["retry", "fail", "recover"] = "retry"
    checkpoint_ref: str | None = Field(default=None, max_length=1024)
    max_recoveries: int | None = Field(
        default=None, ge=1, le=RECOVERY_MAX
    )  # `recover` only: automatic continuations per root
    wait_s: int | None = Field(default=None, ge=0, le=86400)  # `recover` only: time to come back on its own class first
    deadline_s: int | None = Field(
        default=None, ge=60, le=14 * 86400
    )  # `recover` only: no recovery past root.created_at + deadline_s (a 14-day run may recover until its end)


MAX_STAGES = 256


class GraphSpec(BaseModel):
    """The stages a root declares at submission (docs/JOBS.md "Stage composition"): stage id -> the stages it depends on
    (`root` = the root's own run). Sealed: no stage attaches later. Acyclic, at most MAX_STAGES stages."""

    model_config = ConfigDict(extra="forbid")
    stages: dict[str, "list[str] | StageDecl"]
    deadline_s: int | None = Field(
        default=None, ge=60, le=30 * 86400
    )  # stages still waiting after it are cancelled (default 14 days)


class StageDecl(BaseModel):
    """A declared stage: its dependencies and, optionally, the model it is bound to (docs/JOBS.md "Delegated stages":
    the root's owner may then submit it on that model when the root's run class delegates to it)."""

    model_config = ConfigDict(extra="forbid")
    depends_on: list[str] = Field(default_factory=list)
    model: str | None = None


class StageSpec(BaseModel):
    """A run that is one declared stage of a root's graph: admitted only after its dependencies delivered."""

    model_config = ConfigDict(extra="forbid")
    root: str
    id: str


RETRY_MAX_ATTEMPTS = 10


class RetrySpec(BaseModel):
    """Bounded application retries of one run (docs/JOBS.md "Retries"): at most `max_attempts` pods of the same Job, a
    replacement only after an exit code in `on_exit_codes` (137/143 by default: the retained scientific policy); any
    other non-zero exit fails the run at once. Infrastructure disruptions stay the platform's (never counted)."""

    model_config = ConfigDict(extra="forbid")
    max_attempts: int = Field(ge=1, le=RETRY_MAX_ATTEMPTS)
    on_exit_codes: list[int] = Field(default_factory=lambda: [137, 143])


class ExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown execution field is an error, never silently ignored
    io_mode: Literal["managed", "application"] = "managed"
    profile_ref: str | None = None
    workload_identity_ref: str | None = None
    private_input_ref: str | None = None
    placement: PlacementSpec | None = None
    recovery: RecoverySpec | None = None
    graph: GraphSpec | None = None
    stage: StageSpec | None = None
    retry: RetrySpec | None = None  # docs/JOBS.md "Retries": overrides the run class's own policy
    require_converged: bool | None = None  # endpoint calls: refused while the endpoint's rollout is not converged


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, str]


class InvokeRequest(BaseModel):
    name: str | None = None
    mode: str | None = None
    input: dict = Field(default_factory=dict)
    region: str | None = None
    priority: str | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=INVOKE_TIMEOUT_MAX_S)
    execution: ExecutionSpec | None = None  # docs/JOBS.md "Execution options" (runs; endpoint calls: docs/API.md)


class SessionRequest(BaseModel):
    """`POST /v1/models/{model}:session` (docs/API.md "Sessions")."""

    model_config = ConfigDict(extra="forbid")
    timeout_s: int = Field(default=3600, ge=60, le=86400)  # the session's deadline from its creation
    region: str | None = None  # a cluster id or region of the model's deployments (docs/API.md)


class Session(BaseModel):
    id: str
    model: str
    cluster: str | None = None
    region: str
    status: Literal["claimed", "bound", "attached", "ended"]
    outcome: str | None = None
    cancel_requested: bool
    cancel_reason: str | None = None
    attached: bool
    worker_session_id: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    deadline_at: str | None = None
    bound_at: str | None = None
    attached_at: str | None = None
    ended_at: str | None = None
    gpu_seconds: float | None = None
    usage: str | None = None
    claim_token: str | None = None  # on creation only


class SessionAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: str
    worker_session_id: str | None = Field(default=None, max_length=200)


class SessionRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection: str
    outcome: Literal["done", "cancelled", "disconnected", "busy", "error"]
    gpu_seconds: float | None = Field(default=None, ge=0)  # null = unknown (no metering): the bound stays


class ResumeRequest(BaseModel):
    checkpoint_ref: str | None = Field(
        default=None, max_length=1024
    )  # a portable checkpoint: no volume, any permitted cluster/class
    gpu_class: str | None = None


class UploadRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    expires_s: int = 3600
    region: str = REGION


class ScalingRequest(BaseModel):
    min_replicas: int | None = Field(default=None, ge=0)
    max_replicas: int | None = Field(default=None, ge=1)
    target_concurrency: int | None = Field(default=None, ge=1)
    scale_to_zero_after_s: int | None = Field(default=None, ge=0)
    scaling: ScalingSpec | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=600)


class KeyRequest(BaseModel):
    alias: str
    budget: float = 10.0
    models: list[str] = Field(default_factory=list)
    expires_days: int | None = None


class KeyUpdate(BaseModel):
    """What an admin may change on an existing key: the spending limit, the model allow-list, the expiry (days from
    now; 0 = never). Fields left out stay as they are; spend and the key value never change here."""

    budget: float | None = Field(default=None, ge=0)
    models: list[str] | None = None
    expires_days: int | None = Field(default=None, ge=0)


class KeyImportRequest(BaseModel):
    keys: list[dict]
    dry_run: bool = False


class Mode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"
    RUN = "run"


class OperationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PREEMPTED = "PREEMPTED"
    FINALIZING = "FINALIZING"
    EXPORT_FAILED = "EXPORT_FAILED"
    SUPERSEDED = "SUPERSEDED"


class AttemptStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PREEMPTED = "PREEMPTED"
    CANCELLED = "CANCELLED"


class ExportStateValue(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    N_A = "n/a"


ExportState = ExportStateValue | None


class KeyImportConflict(BaseModel):
    alias: str | None = None
    reason: str | None = None


class KeyImportUnknownScope(BaseModel):
    alias: str | None = None
    scopes: list[str] | None = None


class KeyImportReport(BaseModel):
    dry_run: bool | None = None
    inserted: list[str] | None = None
    unchanged: list[str] | None = None
    conflicts: list[KeyImportConflict] | None = None
    blocked_for_unknown_scope: list[KeyImportUnknownScope] | None = None
    not_mirrored: list[str] | None = None


class PodMetadataSpec(BaseModel):
    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None


class GpuSpec(BaseModel):
    count: int | None = None
    classes: list[str] | None = None


class ResourcesSpec(BaseModel):
    cpu: str | None = None
    memory: str | None = None


class WeightsSpec(BaseModel):
    path: str | None = None
    mount_path: str | None = None
    env: dict[str, str] | None = None
    read_only: bool | None = None  # a verified, immutable payload: mounted readOnly, no prepare step (default false)
    prepare: bool | None = None  # the init step that creates the directory (default: not read_only)


class DataSpec(BaseModel):
    path: str | None = None
    mount_path: str | None = None
    readonly: bool | None = None


class VariantSpec(BaseModel):
    name: str | None = None
    pool: str
    min: int | None = None
    max: int | None = None
    image: str | None = None


class DeploymentSpec(BaseModel):
    variants: list[VariantSpec] | None = None


class RoutingSpec(BaseModel):
    field: str | None = None
    map: dict[str, str] | None = None
    default: str | None = None


class FinalizeSpec(BaseModel):
    command: str | list[str] | None = None
    on_exit_codes: list[int] | None = None
    timeout_s: int | None = None
    cpu: str | None = None
    memory: str | None = None


class ModelSpec(BaseModel):
    """Container definition shape; models.validate owns fleet-dependent and cross-field validation.

    Definition options retain their raw values so coercion or unknown nested fields cannot bypass the
    existing validation. The three original request fields retain FastAPI's structural validation.
    """

    model_config = ConfigDict(extra="allow")
    id: str
    kind: str = "endpoint"
    image: str | None = None
    images: SkipValidation[dict[str, str] | None] = None
    command: SkipValidation[str | list[str] | None] = None
    worker_command: SkipValidation[str | list[str] | None] = None
    args: SkipValidation[list[str] | None] = None
    env: SkipValidation[dict[str, Any] | None] = None
    env_secrets: SkipValidation[list[str] | None] = None
    pod_metadata: SkipValidation[PodMetadataSpec | None] = None
    automount_service_account_token: SkipValidation[bool | None] = None
    port: SkipValidation[int | None] = None
    protocol: SkipValidation[Literal["http", "openai", "websocket", "grpc"] | None] = None
    path: SkipValidation[str | None] = None
    health_path: SkipValidation[str | None] = None
    served_model: SkipValidation[str | None] = None
    example: SkipValidation[dict[str, Any] | None] = None
    gpu: SkipValidation[GpuSpec | None] = None
    nodes: SkipValidation[int | None] = None
    interconnect: SkipValidation[Literal["none", "preferred", "required"] | None] = None
    resources: SkipValidation[ResourcesSpec | None] = None
    scaling: SkipValidation[ScalingSpec | None] = None
    timeout_s: SkipValidation[int | None] = None
    shm_gib: SkipValidation[int | None] = None
    empty_dirs: SkipValidation[list[EphemeralVolumeSpec] | None] = None
    env_field_refs: SkipValidation[dict[str, Literal["metadata.uid", "metadata.name", "metadata.namespace"]] | None] = (
        None
    )
    pull_secret: SkipValidation[str | None] = None
    weights: SkipValidation[WeightsSpec | None] = None
    data: SkipValidation[DataSpec | None] = None
    regions: SkipValidation[list[str] | None] = None
    deployments: SkipValidation[dict[str, DeploymentSpec] | None] = None
    routing: SkipValidation[RoutingSpec | None] = None
    display_name: SkipValidation[str | None] = None
    description: SkipValidation[str | None] = None
    task: SkipValidation[str | None] = None
    cpu: SkipValidation[str | None] = None  # request
    memory: SkipValidation[str | None] = None  # request (= limit unless memory_limit)
    cpu_limit: SkipValidation[str | None] = None  # optional: no CFS quota without it
    memory_limit: SkipValidation[str | None] = None  # a limit above the request is not guaranteed memory
    ephemeral_storage: SkipValidation[str | None] = None  # node-local scratch request (the kubelet's ephemeral storage)
    ephemeral_storage_limit: SkipValidation[str | None] = None  # = the request unless given
    delegates: SkipValidation[list[str] | None] = (
        None  # run classes a graph of this root may bind stages to (operator-owned)
    )
    native_lease: SkipValidation[Literal["optional", "required"] | None] = (
        None  # endpoints: whether the worker must attach to each call's native execution (docs/API.md)
    )
    ingress_isolation: SkipValidation[Literal["declared-ports"] | None] = (
        None  # endpoints: a CiliumNetworkPolicy admits only the platform's data path into the pods (docs/API.md)
    )
    retry: SkipValidation[dict[str, Any] | None] = (
        None  # {max_attempts, on_exit_codes}: the class's policy (docs/JOBS.md "Retries")
    )
    disk_gi: SkipValidation[int | None] = None
    grace_seconds: SkipValidation[int | None] = None
    scratch: SkipValidation[Literal["network", "local-nvme"] | None] = None
    work_mount_aliases: SkipValidation[list[dict[str, str]] | None] = (
        None  # [{path: /abs/canonical, scope: attempt | run}]: the work volume at a canonical path (docs/JOBS.md "Work mount aliases")
    )
    parameters: SkipValidation[list[dict[str, Any]] | None] = None
    finalize: SkipValidation[FinalizeSpec | None] = None


class Error(BaseModel):
    detail: str | None = None


class ModelGpu(BaseModel):
    count: int | None = None
    classes: list[str] | None = None


class ModelRegion(BaseModel):
    region: str | None = None
    cluster: str | None = None
    status: Literal["ready", "scaled-to-zero", "deploying", "error", "unavailable"] | None = None
    replicas_ready: int | None = None
    reason: str | None = None
    nodes: int | None = None
    pods_ready: int | None = None
    startup_s: int | None = None
    started_at: str | None = None
    ready_at: str | None = None
    last_request_at: str | None = None
    rollout: ModelRollout | None = None


class ModelParameter(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "select", "text", "file"]
    label: str | None = None
    default: Any = None
    options: list[str] | None = None
    required: bool | None = None
    help: str | None = None


class ModelExecution(BaseModel):
    """The public execution identity of a run class (docs/API.md "Models"): what a caller may know without the
    container configuration (`spec` stays fleet administrators'): the image reference as registered, whether it is
    digest-pinned, per-GPU-class images, the classes, the node count, the operator-approved delegate class ids
    (no grant implied), the bounded retry policy, whether a finalizer exists."""

    image: str | None = None
    immutable: bool = False
    images: dict[str, str] | None = None
    gpu_classes: list[str] | None = None
    nodes: int | None = None
    delegates: list[str] | None = None
    retry: dict[str, Any] | None = None
    finalize: bool = False


class ModelRollout(BaseModel):
    """An endpoint's revision state (docs/API.md "Models"): Knative routes to the latest *ready* revision, so a spec
    change serves the previous revision until the new one is ready; `converged` = the created, ready and rolled-out
    revisions are one (a property of the revisions, true for a converged endpoint at zero), `serving_images` what the
    ready pods run."""

    latest_created: str | None = None
    latest_ready: str | None = None
    latest_rolled_out: str | None = None
    generation: int | None = None  # the InferenceService's spec generation and what the controller reconciled
    observed_generation: int | None = None
    desired_image: str | None = (
        None  # the spec's image; `revision_image` the ready Revision's (stored template evidence)
    )
    revision_image: str | None = None
    revision_image_digest: str | None = None
    serving_images: list[str] | None = None
    converged: bool | None = None  # reconciled + one revision + revision image == desired + ready pods on it
    reason: str | None = None  # why not converged


class Model(BaseModel):
    id: str
    name: str
    description: str | None = None
    modes: list[Mode]
    default_mode: Mode
    gpu: str | ModelGpu | None = None
    price: str | None = None
    price_per_call: float | None = None
    regions: list[ModelRegion]
    image: str | None = None
    protocol: str | None = None
    cold_start_s: float | None = None
    cold_start_class: str | None = None
    endpoints: dict[str, str] | None = None
    task: str | None = None
    managed_by: Literal["git", "catalog", "terraform", "api"] | None = None
    spec: ModelSpec | None = None
    example: dict[str, Any] | None = None
    parameters: list[ModelParameter] | None = None
    execution: ModelExecution | None = None  # run classes: the sanitized execution identity
    native_lease: Literal["optional", "required"] | None = None  # endpoints: docs/API.md "Native execution leases"
    ingress_isolation: Literal["declared-ports"] | None = None  # endpoints: docs/API.md "Ingress isolation"


class SyncResponse(BaseModel):
    result: Any
    operation: Operation


class Attempt(BaseModel):
    index: int | None = None
    attempt_id: str | None = None
    attempt_role: Literal["run", "export", "finalize"] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    status: AttemptStatus | None = None
    node: str | None = None
    gpu_class: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    epoch: int | None = None
    export_state: ExportState | None = None
    result_ref: str | None = None
    checkpoint_ref: str | None = None
    image_digest: str | None = None  # the runtime digest observed on the pod, or the record's
    operation: str | None = None
    image: str | None = None
    gpu_seconds: float | None = None


class ChainMember(BaseModel):
    id: str | None = None
    created_at: str | None = None
    status: OperationStatus | None = None
    gpu_class: str | None = None
    cluster: str | None = None
    reason: str | None = None
    gpu_seconds: float | None = None
    gpu_seconds_complete: bool | None = None
    cost: float | None = None


class NativeExecution(BaseModel):
    state: Literal["prepared", "attached", "released", "lost", "closed"] | None = None
    attempt: str | None = None
    worker_request_id: str | None = None
    prepared_at: str | None = None
    attached_at: str | None = None
    released_at: str | None = None
    outcome: str | None = None
    lane_seconds: float | None = None  # wall-clock occupancy of the worker's lane (attached -> released/lost)
    gpu_seconds: float | None = None  # only what the worker measured; null = unknown, never zero
    cancel_requested: bool | None = None
    settled: bool | None = None
    attempts: list[dict[str, Any]] | None = None  # every attempt's evidence, oldest first
    lost_reason: str | None = (
        None  # why the reconciler marked it lost: pod gone | pod replaced | pod finished | worker container restarted (...)
    )


class NativeAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: str
    worker_request_id: str | None = Field(default=None, max_length=200)


class NativeRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: str
    worker_request_id: str | None = Field(default=None, max_length=200)  # the same tuple the worker attached with
    outcome: Literal["done", "cancelled", "error", "disconnected"]
    gpu_seconds: float | None = Field(default=None, ge=0)


class GraphStage(BaseModel):
    id: str | None = None
    depends_on: list[str] | None = None
    operation: str | None = None
    status: str | None = None
    attempt_id: str | None = None  # the delivering attempt (of `delivered_by` when a finalizer or retry delivered)
    epoch: int | None = None
    delivered_by: str | None = None
    result_ref: str | None = None
    continuations: int | None = None
    reason: str | None = None


class GraphStatus(BaseModel):
    """A root's declared stages and where each stands (docs/JOBS.md "Stage composition")."""

    status: str | None = None
    stages: list[GraphStage] | None = None
    deadline_at: str | None = None
    cancelled: bool | None = None


class Operation(BaseModel):
    id: str
    name: str | None = None
    model: str | None
    mode: Mode
    status: OperationStatus
    phase: Literal["waiting_for_admission", "waiting_for_node", "starting", None] = None
    reason: str | None = None
    region: str | None
    cluster: str | None = None
    tenant: str | None = None
    profile: str | None = None
    priority: str | None = None
    created_at: str | None
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    timeout_s: int | None = None
    attempts: list[Attempt] | None = None
    logs_url: str | None = None
    error: str | None = None
    input: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    graph: GraphStatus | None = None  # a root that declared stages
    stage: GraphStage | None = None  # a run that is a stage of a root's graph
    resumed_from: str | None = None
    resumable: bool | None = None
    gpu_class: str | None = None
    image: str | None = None
    nodes: int | None = None
    interconnect: str | None = None
    cost: float | None = None
    gpu_seconds: float | None = None
    gpu_seconds_complete: bool | None = None
    successor: str | None = None
    recovered_from: str | None = None
    requested_id: str | None = None
    root_operation_id: str | None = None
    successor_created_at: str | None = None
    chain: list[ChainMember] | None = None
    attempt_id: str | None = None
    attempt_role: Literal["run", "export", "finalize", None] = None
    epoch: int | None = None
    revision: int | None = None
    export_state: ExportState | None = None
    exported_by: str | None = None
    exported_by_attempt_id: str | None = None  # the delivering attempt (an export retry or finalizer that delivered)
    endpoint_revision: str | None = None  # an endpoint call: the ready revision at admission (not per-request proof)
    endpoint_image: str | None = None
    native: NativeExecution | None = None  # an endpoint call's native execution lease (docs/API.md)
    exported_by_epoch: int | None = None
    result_ref: str | None = None
    checkpoint_ref: str | None = None
    checkpoint_generation: int | None = None
    image_digest: str | None = None
    image_digest_state: Literal["observed", "pending", "unknown"] | None = (
        None  # observed = exact runtime digest; pending = the attempt runs, the kubelet has not reported it yet (transient); unknown = ended without one
    )
    finalize_attempts: int | None = None
    finalizing_by: str | None = None


class Artifact(BaseModel):
    name: str
    uri: str | None = None
    url: str
    size_bytes: int | None = None


class OperationResult(BaseModel):
    operation_id: str
    status: OperationStatus
    region: str | None = None
    error: str | None = None
    export_state: ExportState | None = None
    result_ref: str | None = None
    checkpoint_ref: str | None = None
    result: Any = None
    artifacts: list[Artifact] | None = None


class Upload(BaseModel):
    uri: str
    url: str
    method: Literal["PUT"]
    headers: dict[str, str] | None = None
    expires_at: str


class EndpointScaling(BaseModel):
    min: int | None = None
    max: int | None = None
    metric: Literal["concurrency_utilization", "requests_per_second", "none"] | None = None
    target: int | None = None
    container_concurrency: int | None = None
    cooldown_s: int | None = None
    window_s: int | None = None
    idle_s: int | None = None


class EndpointVariant(BaseModel):
    name: str | None = None
    id: str | None = None
    pool: str | None = None
    min: int | None = None
    max: int | None = None
    image: str | None = None
    status: Literal["ready", "scaled-to-zero", "deploying", "error", "unavailable", None] = None
    replicas_ready: int | None = None


class Endpoint(BaseModel):
    id: str
    name: str
    model: str
    region: str
    cluster: str | None = (
        None  # the deployment's cluster id as the catalog keys it (`north`); `region` is where the API reaches it
    )
    url: str
    status: Literal["ready", "scaled-to-zero", "deploying", "error", "unavailable"]
    replicas_ready: int | None
    nodes: int | None = None
    interconnect: str | None = None
    pods_ready: int | None = None
    last_request_at: str | None = None
    startup_s: int | None = None
    started_at: str | None = None
    ready_at: str | None = None
    example: dict[str, Any] | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    scale_to_zero_after_s: int | None = None
    target_concurrency: int | None = None
    scaling: EndpointScaling | None = None
    timeout_s: int | None = None
    gpu: str | dict[str, Any] | None = None
    image: str | None = None
    cpu: str | None = None
    memory: str | None = None
    protocol: str | None = None
    created_at: str | None = None
    managed_by: Literal["git", "catalog", "terraform", "api"] | None = None
    variants: list[EndpointVariant] | None = None
    routing: dict[str, Any] | None = None


class FleetPool(BaseModel):
    region: str | None = None
    cluster: str | None = None
    pool: str | None = None
    gpu_class: str | None = None
    platform: str | None = None
    preset: str | None = None
    gpus_per_node: int | None = None
    capacity: str | None = None
    usd_per_gpu_hour: float | None = None
    max_nodes: int | None = None
    interconnect: str | None = None
    local_nvme: bool | None = None
    nodes_ready: int | None = None
    gpus_total: int | None = None
    gpus_used: int | None = None


class FleetCluster(BaseModel):
    id: str | None = None
    region: str | None = None
    project: str | None = None
    roles: list[Literal["control", "manager", "worker"]] | None = None
    api_url: str | None = None
    grafana_url: str | None = None
    reachable: bool | None = None
    nodes_ready: int | None = None
    gpu_nodes_ready: int | None = None
    kubernetes_version: str | None = None


class TenantUsage(BaseModel):
    gpus_by_pool: dict[str, int] | None = None
    admitted: int | None = None
    pending: int | None = None


class FleetTenant(BaseModel):
    name: str | None = None
    gpu_quota: int | None = None
    fair_share_weight: float | None = None
    dedicated_pools: list[str] | None = None
    usage: dict[str, TenantUsage] | None = None


class ExecutionCapabilitiesFinalize(BaseModel):
    roles: list[str] | None = None
    max_retries: int | None = None
    trigger: str | None = None
    declared_by: str | None = None


class ExecutionCapabilitiesRecoveryDefaults(BaseModel):
    max_recoveries: int | None = None
    wait_s: int | None = None


class ExecutionCapabilitiesRecovery(BaseModel):
    on_infrastructure_loss: list[str] | None = None
    max_recoveries: int | None = None
    defaults: ExecutionCapabilitiesRecoveryDefaults | None = None
    requires: str | None = None


class ExecutionCapabilitiesAsyncPrivateIO(BaseModel):
    """Endpoint calls with private input and application-owned output (docs/JOBS.md): present when the API has them."""

    version: int | None = None
    refresh: bool | None = None
    retained_export: bool | None = None
    binding_keys: list[str] | None = None


class ExecutionCapabilitiesRetry(BaseModel):
    max_attempts: list[int] | None = None  # [min, max]
    default_exit_codes: list[int] | None = None


class ExecutionCapabilities(BaseModel):
    io_modes: list[Literal["managed", "application"]] | None = None
    async_private_io: ExecutionCapabilitiesAsyncPrivateIO | None = None
    identities: list[str] | None = None
    execution_profiles: list[str] | None = None
    oidc_issuers: dict[str, str] | None = None
    private_input_dir: str | None = None
    identity_token_file: str | None = None
    finalize: ExecutionCapabilitiesFinalize | None = None
    recovery: ExecutionCapabilitiesRecovery | None = None
    retry: ExecutionCapabilitiesRetry | None = None  # docs/JOBS.md "Retries"


class FleetInfo(BaseModel):
    regions: list[str] | None = None
    gpu_classes: list[str] | None = None
    fleet_manager: bool | None = None
    region: str | None = None
    execution: ExecutionCapabilities | None = None
    pools: list[FleetPool] | None = None
    tenant: FleetTenant | None = None
    clusters: list[FleetCluster] | None = None


class Binding(BaseModel):
    ref: str | None = None
    operation: str | None = None
    keys: list[str] | None = None
    updated_at: str | None = None
    owner: str | None = None
    revision: str | None = None


class MetricSeries(BaseModel):
    name: str | None = None
    points: list[tuple[float, float | None]] = None


class MetricPanel(BaseModel):
    id: str | None = None
    title: str | None = None
    unit: str | None = None
    unavailable: bool | None = None
    series: list[MetricSeries] | None = None


class Metrics(BaseModel):
    region: str | None = None
    start: float | None = None
    end: float | None = None
    step: int | None = None
    panels: list[MetricPanel] | None = None


class LogLine(BaseModel):
    timestamp: str | None = None
    line: str | None = None
    pod: str | None = None
    container: str | None = None


class Logs(BaseModel):
    region: str | None = None
    start: float | None = None
    end: float | None = None
    truncated: bool | None = None
    lines: list[LogLine] | None = None


class RateLimit(BaseModel):
    requests: int | None = None
    window_s: int | None = None


class KeyUsage(BaseModel):
    """What the key holds beyond its spend (docs/API.md "Keys"): operations in flight, GPU seconds reserved by active
    work, GPU seconds of attempts that ended without a trustworthy measurement (kept against the budget)."""

    active_operations: int | None = None
    reserved_gpu_seconds: float | None = None
    unknown_gpu_seconds: float | None = None


class ApiKey(BaseModel):
    id: str | None = None
    key: str | None = None
    role: Literal["user", "admin", "router"] | None = None
    key_preview: str | None = None
    alias: str | None = None
    tenant: str | None = None
    budget: float | None = None
    spend: float | None = None
    models: list[str] | None = None
    created_at: str | None = None
    expires_at: str | None = None
    status: Literal["active", "exhausted", "expired"] | None = None
    scopes: list[str] | None = None
    max_concurrent: int | None = None
    budget_requests: float | None = None
    spent_requests: float | None = None
    budget_gpu_seconds: float | None = None
    spent_gpu_seconds: float | None = None
    rate_limit: RateLimit | None = None
    principal_id: str | None = None
    legacy: dict[str, Any] | None = None
    usage: KeyUsage | None = None


class ModelWrite(BaseModel):
    id: str
    kind: str
    regions: list[str]
    applied: dict[str, Any]
    version: int | None = None
    model: Model
    pending: bool | None = None
    warnings: list[str] | None = None


class ModelHistory(BaseModel):
    version: int
    action: str
    by: str | None
    at: str
    spec: ModelSpec
