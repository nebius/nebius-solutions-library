# Jobs: run classes, endpoint calls, checkpoint resume

*For developers and operators: how a run becomes a Job, how it is queued, placed, resumed and cancelled, and the multi-node variant.*

Every asynchronous operation of the API is one Kubernetes `batch/v1` Job in a tenant namespace.
No workflow engine: the Job controller retries, Kueue queues and admits, the CSI volume keeps the
checkpoints, a 30 MB runner image moves data. `services/api/jobs.py` renders,
submits, reads, cancels and resumes them. The fleet's API creates them on the MultiKueue manager (the
dedicated control cluster) and lets Kueue plus the cost dispatcher place them on a worker ("Fleet placement"
below); in single-cluster mode (`control_plane.dedicated = false`) it creates them in its own cluster.

## Run class = catalog entry with a `job` block

```yaml
id: container-run
mode: run
job:
  image: "{{image}}"              # any {{name}} is a parameter (caller's input > region defaults > entry defaults)
  images: { h100: <image>:h100 }  # optional: one image per GPU class (keys: default or a gpu.classes entry); the class is
                                  # chosen at submission and the run waits for it (docs/SCHEDULING.md "Per-GPU images for runs")
  command: "{{command}}"          # string -> /bin/sh -c; or a list [bash, -c, "..."]
  args: []                        # optional
  env: { CHECKPOINT_DIR: /work/checkpoint, OPERATION: "{{operation}}" }
  gpu: "{{gpus}}"                 # nvidia.com/gpu request = limit (0 = CPU job)
  cpu: "8"                        # request; cpuLimit optional (many solvers run without one: a CFS quota throttles them)
  memory: 64Gi                    # request = limit (memoryLimit overrides the limit)
  pvcSizeGi: 100                  # the work volume (/work), ReadWriteOnce, regional
  shm: 16Gi                       # /dev/shm emptyDir
  pool: "{{pool}}"                # nodeSelector serverless2.nebius/pool (omit: Kueue's flavor decides)
  imagePullSecret: ngc            # Secret in the tenant namespace
  graceSeconds: 120               # SIGTERM -> SIGKILL window (checkpoint + partial upload); 300 for slow checkpoints
  s3Env: true                     # envFrom Secret `s3` (AWS_* for the tenant bucket) on the main container
  inputs: "{{input_prefix}}"      # default: the input_prefix parameter
  uploadExcludes: "--exclude 'scratch/*'"
gpu: { count: 1, classes: [rtx-pro-6000, h100] }   # classes the image runs on, PREFERRED FIRST (scheduling profile)
deployments:                      # optional: the regions the run class may land in (default: every region of the fleet); per-region parameter defaults and price
  hub:           { price_per_gpu_hour: 2.15, parameters: { nb_min_ci: "16000" } }
  <region id>:   { price_per_gpu_hour: 1.60, parameters: { nb_min_ci: "16000" } }
parameters:
  - { name: image, type: string, required: true }
```

`gpu.classes` does two things: the first class names the scheduling profile (LocalQueue
`prefer-<class>` in the tenant namespace, `default` when the entry has no classes), and the whole
list becomes a `nodeAffinity` on `serverless2.nebius/pool` over every fleet pool of those classes
(pools from the `fleet-prices` ConfigMap), so a preference queue never falls back to a GPU the
image does not run on (an image without sm_89 code: no L40S). A `pool` in the `job` block pins
instead (regional path only). Images are named through the fleet's logical registry host
(`registry.serverless2.local/<alias>/<path>`, the same reference in every region: `docs/IMAGES.md`).
With `job.images` (one image per GPU class) the API chooses the class at submission from the
dispatcher's ranking, renders that class's image and limits the affinity to that class's pools; the
operation reports it as `gpu_class` and `image`, and a resume keeps it.

Three run classes ship (`catalog/models`): `container-run` (any image and command; the generic job
class), `hello-run` (CPU smoke test) and `distributed-run` (multi-node JobSet, "Multi-node runs"
below). Application layers define their own run classes through the model API (`kind: job`).
`parameters` is what a client shows and what `input` may contain (unknown names, or a missing
`required` one, are a 400); `deployments.<cluster>.parameters` are per-region defaults the caller
never sees (pool, in-region image, tuning). `input_prefix` and `output_prefix` are always accepted;
the API sets `output_prefix` to `s3://<tenant bucket of the region>/operations/<id>`.

## The pod

```
initContainers: fetch     runner image   aws s3 sync $INPUT_PREFIX/ /work/in/   (skipped on a resumed attempt)
containers:     main      the model      the rendered command, cwd /work, GPUs
                uploader  runner image   waits for `main` to end (pod status through the API for the exit code,
                                         plus the shared PID namespace: no process outside its own container
                                         and pause), aws s3 sync /work/ $OUTPUT_PREFIX/ (minus in/), STATUS.json,
                                         attempts/<pod>.json, deletes the work PVC on exit 0; keeps it otherwise
volumes:        work      PVC <id>-work (compute-csi, RWO)       shm   emptyDir (memory)
                data      the tenant's shared claim, subPath data/<path>, read-only (only when the run names `data`)
```

### Shared data

Reference databases, model parameters, anything too large to sync per run: a directory of the tenant's area on the
region's shared filesystem (`/mnt/weights/tenants/<tenant>/data/<path>`, the same RWX claim `checkpoints: shared`
uses) mounted into the run, read-only by default. The generic classes expose it as the `data` parameter (a relative
directory, no parent references) and `data_readonly` (false for a seeding run that fills it); a run class defined
through the API declares it as `data: {path, mount_path, readonly}`. The fetch step never sees it, the uploader never
uploads it. A region without the shared filesystem has no claim, and a run that names `data` fails to start there.

The pod runs with `fsGroup: 10001` (the runner's uid) so fetch and upload work whatever user the
model image runs as, and with a shared PID namespace so the command's shell is not PID 1 and
SIGTERM really terminates `sh -c ...` commands (PID 1 ignores it, and the pod would hold its GPU
until the grace period's SIGKILL). The runner image (`services/jobs`, the jobs image of
`images.versions`, through the logical registry host; `RUNNER_IMAGE` on the API names it) is alpine +
aws-cli + curl + jq + three scripts. The pod
carries the Kueue labels (`kueue.x-k8s.io/queue-name: <profile>`, priority class `customer-batch` or
`bulk-backfill` for `priority: low`), so the Job is gated until the profile's ClusterQueue admits it:
QUEUED for as long as it takes, hours or days, nothing times out while waiting.

Job spec: `parallelism: 1, completions: 1, backoffLimit: 3` (application failures),
`ttlSecondsAfterFinished: 90 d` (operation history), `activeDeadlineSeconds` from `timeout_s`,
`restartPolicy: Never`, and

```yaml
podFailurePolicy:
  rules:
    - { action: Ignore, onPodConditions: [{ type: DisruptionTarget, status: "True" }] }   # drain, preemption, node loss
    - { action: Ignore, onExitCodes: { containerName: main, operator: In, values: [137, 143] } }   # SIGKILL, SIGTERM
```

## Resume

**Automatic.** A spot interruption, node drain or eviction fails the pod with `DisruptionTarget`
(or kills `main` with 137/143). The rule above does not count it against `backoffLimit`; the Job
controller starts a replacement pod, which attaches the same PVC (the volume is regional, so the
replacement lands in the same region by construction). `fetch` sees the `.inputs-fetched` marker
and skips, `main` finds its checkpoints: a solver's checkpoint file, a training script its
`$CHECKPOINT_DIR`. The operation shows every attempt (`attempts[]`, status `PREEMPTED` for the
lost one), billing sums the GPU-seconds of all of them.

Preemption **deletes** the victim pod (scheduler preemption by an interactive endpoint, the taint
manager on a lost node), so the pod is not the record. The uploader writes `attempts/<pod>.json`
(operation, pod, node, status, exit code, GPUs, start and end) into the work volume and the bucket
on every exit path, including a partial upload on SIGTERM; `GET /v1/operations/{id}` and the billing
pass merge these records with the live pods (a live pod wins over its own record). The list
endpoint shows live pods only. An endpoint scaling up on a shared pool can preempt a run's pod
seconds after `main` has finished: the Job is still `Complete`, the outputs are in the bucket, the
pod is gone. A node that dies without warning (a spot VM stopped by Nebius: no SIGTERM reaches the
pod) leaves the "started" record the uploader writes to the volume and the bucket when `main`
begins: the attempt shows as `PREEMPTED` with reason "node lost without warning", no end time, and
bills nothing. A stop that is orderly enough for the SIGTERM path gets the full record; the start
record makes the attempt visible at once either way.

**Manual: `POST /v1/operations/{id}:resume`.** For a run that is `FAILED` (retries exhausted,
or a bug that has since been fixed in the inputs) or `CANCELLED`. The API renders Job `<id>-r<n>`
again from the catalog entry with the original `input` (so a copy of the finished Job's spec, with
the selector, workload annotation and flavor labels the Job controller and Kueue injected, is
never reused: Kueue rejects such a copy with `PodSetUpdate: conflict`), in the same region,
mounting the same `<id>-work` PVC, with annotation `resumed-from: <id>` (the root id; `-r1`,
`-r2`, ... chain from it). The new Job is added to the PVC's owner references so the volume
outlives either Job; its outputs and attempt records go under `operations/<id>-r<n>/`. 409 when the run is still queued or running, or when the volume is
gone: a successful run's uploader deletes it (`KEEP_PVC=true` in the uploader env keeps it), the
billing pass deletes it when the uploader was preempted before it could, and the PVC is
garbage-collected with its last owning Job after the 90 d TTL. `resumable: true` on an operation
means "finished unsuccessfully, run class"; the volume check happens on the call.

Resume is same-region only, deliberately: the volume is a regional disk and the inputs are in the
regional bucket. Rerunning elsewhere is a new submission with `region`.

## Multi-node runs (JobSet, InfiniBand optional)

A job class with `nodes: N` (N > 1; catalog class `distributed-run`, parameters `nodes`,
`gpus_per_node`, `interconnect`, `checkpoints`) is rendered as a **JobSet**
(`jobset.x-k8s.io/v1alpha2`, installed on every cluster, Kueue and MultiKueue integration on)
instead of a Job: one replicated job `workers` holding one indexed Job of N pods, one pod per
whole node (`gpu` = the preset's GPU count, so the pod takes the node), the same `fetch` /
`main` / `uploader` containers, and:

- **Rank discovery.** JobSet gives the pods stable DNS names through a headless Service
  (`<id>-workers-0-<rank>.<id>`); the API sets `MASTER_ADDR=<id>-workers-0-0.<id>`,
  `MASTER_PORT=29500`, `NODE_RANK` (the pod's completion index), `NNODES`, `GPUS_PER_NODE`,
  `WORLD_SIZE = NNODES x GPUS_PER_NODE`: the torchrun/NCCL conventions
  (`torchrun --nnodes=$NNODES --node_rank=$NODE_RANK --master_addr=$MASTER_ADDR ...`).
- **Placement.** Node affinity on the pools of the model's GPU classes whose preset has
  `gpus_per_node` GPUs (whole nodes only); `interconnect: required` adds the pools' InfiniBand label
  (`serverless2.nebius/interconnect=infiniband`, set by Terraform on pools with
  `interconnect: infiniband`, and on their Kueue flavors' `nodeLabels`, so Kueue never admits such
  a run onto a non-InfiniBand pool); `preferred` is a soft preference; `none` ignores it.
  **`required` is the default** for `nodes > 1`: a multi-node run without the fabric runs NCCL over
  TCP, which is useless for the models that need several nodes, so `none` is an explicit opt-in. A
  `required` run with no InfiniBand pool of a matching class in the fleet is refused with 400 at
  submission (the message says how to opt into TCP). Kueue admits the whole set at once (one pod set of N pods); the fleet path dispatches it
  like any run (the dispatcher accepts JobSet owners). The manager resolves the run's InfiniBand claim before
  MultiKueue dispatches it, so the control cluster carries the tenant's claim templates (charts/tenant, rendered
  there whenever any region has an InfiniBand pool) and the same Kueue `deviceClassMappings` as the workers
  (`clusters/control/values/kueue.yaml`); without either the workload stays `QuotaReserved=False
  DRAResourcesUnresolved`.
- **InfiniBand in the pod, without privileges.** Nebius GPU node images ship the InfiniBand
  drivers (8 x 400 Gb/s NDR ports `mlx5_0..7`, interfaces `ib0..7` on an 8-GPU H100 node in a GPU
  cluster), and Managed Kubernetes runs **DraNet** (Kubernetes DRA driver for the RDMA
  NICs; DeviceClasses `ib.networking.nebius.ai` and `rdma.networking.nebius.ai` on every cluster)
  on the nodes of a node group that sets `gpu_settings.dra = true` (`nebius.com/dranet-rdma-capable`
  label, one ResourceSlice per node). Terraform sets it on every `interconnect: infiniband` pool;
  without it a node has the NICs but publishes no devices and a claiming pod never schedules
  (`cannot allocate all claims`). A `required` run references the tenant's
  ResourceClaimTemplate `ib-<n>` (charts/tenant, `infiniband: true`; n = the NICs per node of the
  pool, ExactCount because that is what Kueue's DRA accounting supports): the
  node's `n` RDMA interfaces move into the pod, `main` gets `resources.claims: [ib]`, Kueue charges
  `nebius.ai/infiniband` x n against the pool's quota (`charts/fleet`, Kueue `deviceClassMappings`),
  and the pod gets the NCCL environment of the
  Nebius NCCL tutorial (`NCCL_IB_HCA=mlx5`, `NCCL_SOCKET_IFNAME=eth0`, `UCX_NET_DEVICES=eth0`,
  `NCCL_COLLNET_ENABLE=0`, `SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1`; a job class's own `env`
  wins). **No capability, no root, no privileged namespace.** RDMA pins memory
  (`ibv_reg_mr`), which `RLIMIT_MEMLOCK` bounds: the Nebius node image ships 8 MiB, and with every
  capability dropped NCCL fails at `ibv_reg_mr_iova2` / `ibv_create_cq` with "Cannot allocate
  memory". The usual fix is
  `CAP_IPC_LOCK` (it bypasses the limit), which is outside Pod Security Admission `baseline` and
  would force the namespace to `privileged`. Instead the GPU nodes' container runtime runs with an
  unlimited memlock limit (`LimitMEMLOCK=infinity` on the containerd unit, written by the pools'
  cloud-init at boot and by the node-config DaemonSet on older nodes): every container inherits
  it, the run keeps `drop: [ALL]`, seccomp `RuntimeDefault`, no privilege escalation, non-root
  runner sidecars, and the tenant namespace stays at `baseline`. (The same approach as NVIDIA's
  InfiniBand node configuration for Kubernetes; `privileged: true` in Nebius' own NCCL test
  manifest exists only to raise that limit.) NVIDIA Network Operator is not needed on the Nebius node
  images (docs.nebius.com/kubernetes/gpu/set-up: required only for custom images or B200); it is
  installed when the fleet uses the GPU Operator (docs/FLEET.md "GPU drivers").
- **Scratch and checkpoints.** `/work` is node-local per pod (`emptyDir`, `disk_gi` limit; the
  kubelet's ephemeral storage, which is the local NVMe on presets that have it), not a per-run RWO
  volume: N pods on N nodes cannot share one disk. Inputs are fetched by every pod. Checkpoints:
  `checkpoints: shared` mounts the tenant's RWX claim `scratch-shared` (charts/tenant renders a
  static PV on the cluster's shared weights filesystem, `/mnt/weights/tenants/<tenant>`) at
  `/work/checkpoint` under `checkpoints/<root id>`, so a restart and a `:resume` continue from it;
  `local` (default) keeps them under `/work`, gone with the pod. Rank 0 uploads `/work`
  (minus `in/` and `checkpoint/`) and `STATUS.json`; every rank uploads its attempt record
  (`UPLOAD_SCOPE=rank0`).
- **Failure, preemption, resume.** The indexed Job has `backoffLimit: 0`; any pod loss (spot
  preemption, node loss, an application exit) fails it and the JobSet's
  `failurePolicy: {maxRestarts: 3, restartStrategy: Recreate}` recreates every pod, so the ranks
  restart together from the last checkpoint (restart-all; a partial rank set cannot continue an
  NCCL job). After `maxRestarts` the run is FAILED. `:cancel` suspends the JobSet (pods get
  SIGTERM, uploaders record the attempts) and annotates it CANCELLED; `:resume` renders
  `<id>-r<n>` again on the same shared checkpoint directory (409 with `checkpoints: local`).
  Status, attempts (one per pod per restart) and billing (pod-seconds x GPUs, summed over ranks and
  restarts) come from the pods and the attempt records exactly as for a Job.

**Scale-from-zero does not work with DRA claims.** The cluster autoscaler builds the template of an
empty node group without ResourceSlices, so a pod that claims InfiniBand NICs never triggers a
scale-up (`pod didn't trigger scale-up: cannot allocate all claims`; GKE documents the same rule for
DRA node pools). It does not grow from one node either: the autoscaler's simulation of a new node
copies the devices without their attributes and the DeviceClass selector fails (`"DynamicResources"
filter plugin: class ib.networking.nebius.ai: selector #0: CEL runtime error: no such key: ifName`).
The dispatcher therefore moves the pool's floor itself (docs/SCHEDULING.md "InfiniBand pools grow on
demand"): submitting a run is all it takes; the nodes boot after admission (a few minutes, the usual
start of a scaled-from-zero pool). Reserved pools (the usual home of a 16-node job) are fixed-size anyway.

### Limits of multi-node runs

What works, and where the edges are (sources: docs.nebius.com/compute/clusters/gpu,
docs.nebius.com/kubernetes/gpu/clusters, docs.nebius.com/kubernetes/node-groups/manage):

| Limit | Why |
|---|---|
| All pods of one run land in **one pool = one GPU cluster = one InfiniBand fabric**. No cross-pool, cross-region or mixed-platform runs. | A Nebius GPU cluster lives in one fabric of one region and one project; nodes outside it have no InfiniBand path to it (Nebius isolates GPU clusters with InfiniBand partition keys: nodes of different GPU clusters cannot talk over the fabric even on the same physical fabric). The API pins the JobSet to the pools of the model's classes that have the fabric; Kueue admits the whole pod set on one flavor. |
| **8-GPU presets only.** | GPU clusters accept only full-node presets (`8gpu-*`); the renderer, the solution's tfvars validation and the Nebius API refuse others. One pod takes one whole node. |
| **The pool's floor follows the run, not the autoscaler** (the autoscaler never adds InfiniBand nodes); `terraform plan` shows the raised floor as drift meanwhile. | docs/SCHEDULING.md "InfiniBand pools grow on demand". Reserved pools are fixed-size anyway. |
| **NIC claims are ExactCount: 8 per node (4 on GB300).** | Kueue's DRA accounting (0.20) supports ExactCount claims only; the tenant's `ib-8` / `ib-4` templates hand the pod every NIC of its node (the pod has the whole node anyway). |
| **Size of one run**: `nodes <= MULTINODE_MAX_NODES` (64 by default), a node group holds at most 100 nodes, and a fabric has limited GPU capacity. | Nebius publishes no fixed maximum per GPU cluster; "each fabric has limited GPU capacity" and a GPU cluster is bounded by the quota of its project and the free capacity of its fabric. A 16-node run needs 16 nodes in ONE pool (one node group, one GPU cluster): size `max_nodes` (and the reservation) accordingly; the Kueue quota of the pool is NICs per node x `max_nodes`. |
| **Spot preemption = restart-all** from the last checkpoint; after `maxRestarts` (3) the run is FAILED. | A partial rank set cannot continue an NCCL job. Use `checkpoints: shared` so the restart (and a `:resume`) continues from the shared filesystem; `local` checkpoints die with the pod. Reserved or on-demand pools avoid the restarts. |
| **One run per node at a time.** | A pod requests the preset's GPU count, CPU and memory; nothing else fits on the node while it runs. |
| **Which platforms and regions have fabrics** | H100 (`gpu-h100-sxm`): eu-north1 `fabric-2/3/4/6`. H200 (`gpu-h200-sxm`): eu-north1 `fabric-7`, eu-west1 `fabric-5`, eu-north2 `eu-north2-a`. B200 (`gpu-b200-sxm`, `8gpu-160vcpu-1792gb`): us-central1, me-west1. B300 (`gpu-b300-sxm`, `8gpu-192vcpu-2768gb`): uk-south1, eu-west2, us-north1. GB300 (`gpu-gb300`): 4 NICs per node. No fabric: L40S, RTX PRO 6000, every 1-GPU preset. The fabric names change as Nebius adds capacity: `infiniband_fabric` in `terraform.tfvars` is the one place to update (docs.nebius.com/compute/clusters/gpu). |

Terraform side (`terraform.tfvars`, docs/FLEET.md): a pool with `interconnect: infiniband` (8-GPU preset)
gets a `nebius_compute_v1_gpu_cluster` on `infiniband_fabric` (region and platform specific:
`fabric-2/3/4/6` H100 eu-north1, `fabric-7` H200 eu-north1, `fabric-5` H200 eu-west1,
`eu-north2-a` H200 eu-north2; B300 in uk-south1/eu-west2/us-north1, B200 in us-central1/me-west1
per docs.nebius.com/kubernetes/gpu/clusters) and its node group attaches through
`template.gpu_cluster`. Pools without it can run multi-node jobs over the pod network only on explicit request
(`interconnect: none`), which is fine for small models and useless for 16 x B300.

Measured on a pool of 2 x 8 H100 nodes in one GPU cluster: DRA claims allocated (8 NICs per pod),
NCCL `NET/IB` on `mlx5_0..7`, a 1 GiB all-reduce over 16 GPUs at 436.5 GB/s bus bandwidth, restart-all
after rank failures, rank-0 upload. 16 x B300 is the same path on an `8gpu-192vcpu-2768gb` pool
(uk-south1, eu-west2, us-north1).

## Model weights for runs

A job class may name `weights: {path, mount_path}` (the generic classes expose it as the `weights` parameter): a
directory of the region's shared model weights, the filesystem endpoints cache into (`weights` of an endpoint),
mounted read-only at `/weights` (or `mount_path`) through the tenant's read-only claim `weights-shared`. A run can
thus read what an endpoint seeded without copying it; it cannot write there (the tenant's writable area is `data`).
Every run's image reference goes through the region's cache like an endpoint's (`registry.serverless2.local/...`,
docs/IMAGES.md), whether it comes from the job class or from the caller's `image` parameter.

## Execution options

A run may carry an `execution` block next to `input` (`services/api/execution.py`; unknown fields are refused,
`GET /v1/fleet` `execution` lists what the tenant may ask for). Everything it names is rendered by the operator
into the tenant (terraform.tfvars `tenants.<name>`: `storage`, `identities`, `execution_profiles`); the API resolves
the references against the tenant's own namespace, never against raw Kubernetes names, so another tenant's
identity or profile does not exist for a caller.

```json
{"input": {"image": "...", "command": "..."},
 "execution": {"io_mode": "application",
               "profile_ref": "confidential-job-v1",
               "workload_identity_ref": "customer-a",
               "private_input_ref": "job-7-manifest",
               "placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "allowed_clusters": ["us-central1", "eu-north1"], "fallback_wait_s": 60},
               "recovery": {"on_infrastructure_loss": "retry", "checkpoint_ref": null}}}
```

| Field | What it does |
|---|---|
| `io_mode: application` | the run moves its own data: no `fetch` init container, no tenant S3 credentials in any container, the uploader only records the attempt (`UPLOAD_MODE=record`), `input_prefix`/`output_prefix` are refused, `/result` lists no artifacts. A tenant with `storage = "external"` has no bucket at all and can only run this way. `managed` (default) is the tenant-bucket path described above |
| `profile_ref` | an execution profile of the tenant: the pod carries `serverless2.nebius/execution-profile=<name>`, which takes it out of the cluster-wide "world" egress policy; the profile's own policy (charts/tenant `execution.yaml`) lists what it may reach: DNS, the API server, the gateway, the fleet's API hostnames and the profile's `egress_fqdns`/`egress_cidrs`. The metadata service and other namespaces are never in the list. The shared filesystem (`data`, `checkpoints: shared`) stays out unless the profile sets `shared_filesystem = true` |
| `workload_identity_ref` | an identity of the tenant: the pod runs as the ServiceAccount `identity-<ref>` (same Role as the runner, nothing more) and the main container gets a projected token with the identity's audience at `/var/run/secrets/workload-identity/token` (`AWS_WEB_IDENTITY_TOKEN_FILE` set, plus the identity's `env`, e.g. `AWS_ROLE_ARN`). The kubelet mints it per pod and renews it; a storage provider trusts the cluster's OIDC issuer (`GET /v1/fleet` `execution.oidc_issuers`, Nebius Managed Kubernetes publishes `https://oidc.mk8s.api.nebius.cloud/<cluster id>`) and its own role/trust policy enforces the permitted bucket, paths and actions. The token's subject is the ServiceAccount, so two runs with the same identity are the same principal to the provider; a narrower grant is the provider's (or the application's) to enforce |
| `private_input_ref` | a binding of the tenant (`PUT /v1/bindings/<ref>` with string data: credentials where no federation exists, a sensitive manifest): copied into a per-operation Secret and mounted read-only into the main container only, under `/var/run/serverless2/private/<key>` (`PRIVATE_INPUT_DIR`). A binding serves one operation and that operation's resumes (another operation gets 409: a new job needs a new grant); a new `PUT` refreshes the files of a running attempt within the kubelet's sync period (no restart); `DELETE` empties them first, then removes everything. Nothing of it enters the Job, the operation's response or the logs |
| `placement` | `gpu_preference` narrows and orders the model's GPU classes (a class the model does not list is an error), `allowed_clusters` narrows its regions; `fallback_wait_s` bounds how long the run waits for its first choice before the next class is tried (docs/SCHEDULING.md) |
| `recovery` | `on_infrastructure_loss: retry` (default: a replacement attempt on the same volume, as above) `fail` (the operation fails on a disruption; resume it with a checkpoint reference) or `recover` (opt-in: after a confirmed infrastructure loss the control API continues the run from its newest acknowledged checkpoint pointer on the next class of its preference, docs/SCHEDULING.md "Automatic recovery after infrastructure loss"; `max_recoveries` 1..3 default 1, `wait_s` default 300, `deadline_s`: the successor inherits the root's remaining deadline). `checkpoint_ref` reaches the run as `CHECKPOINT_REF`; `POST /v1/operations/<id>:resume` with `{"checkpoint_ref": ..., "gpu_class": ...}` resumes a run from a portable checkpoint the application restores itself: no previous volume, any permitted cluster, optionally another class and therefore that class's image |

Every run pod, with or without the block, mounts the runner's Kubernetes token into the uploader only
(`automountServiceAccountToken: false`, a projected 1-hour token): the model's container holds no Kubernetes credential.

## Attempts and finalization

Every attempt (a pod of an operation) is on record twice: the uploader writes `attempts/<pod>.json` into the tenant
bucket (managed I/O) and posts the same record to the API that rendered the Job (`POST /internal/attempts`, with the
pod's projected ServiceAccount token: a TokenReview against the issuing cluster names the pod, so a record can only
describe the pod it comes from). The control API keeps the posted records in the fleet database
(`services/api/attempts.py`): they survive the pod, the worker's copy of the Job and the Job's TTL, and they exist
without a bucket. Every operation reports `root_operation_id` (the first operation of a resume chain), `attempt_id`
(the newest pod), `epoch` (monotonic over the chain), `revision` (a cursor: `GET /v1/operations/<id>?since=<revision>`
answers 304 while nothing changed), `cluster`, `image_digest`, `export_state`, `result_ref` and `checkpoint_ref`.

Fencing: the first record of a pod opens an epoch; a terminal record from a pod whose epoch is no longer the chain's
newest is refused (409), as is a success after `:cancel`. A worker that returns after a network partition therefore
cannot finalize the operation its replacement owns. This fences the platform's own state; it does not revoke an
external storage credential the old worker still holds (the provider's expiry or an online check does that).

Live checkpoint pointers: while it runs, the application may write `/work/CHECKPOINT.json`
(`{"checkpoint_ref": "...", "generation": N}`, opaque to the platform) after it committed a checkpoint to its own
storage. The uploader notices the change within its poll interval, records the pointer through the same
authenticated, epoch-fenced path, and writes the acknowledgement to `/work/CHECKPOINT.ack.json` (`generation`,
`epoch`, `revision`, `acknowledged_at`) for the application to read. A pod that was replaced cannot publish (409).
`POST :resume` without `checkpoint_ref` uses the newest acknowledged pointer of the chain when the caller asks for
another class or the volume is gone (an abrupt node loss), so a run recovers on a fresh volume elsewhere. The
operation reports `checkpoint_ref` and `checkpoint_generation`.

Private input delivery is part of Job creation, including timed GPU fallback, resume and export retries. The
per-operation Secret is present in the allowed worker regions before the new Job can be admitted. A missing or
revoked binding fails before admission; fallback leaves the original handle unchanged. Finishing or billing an
older operation removes only its own private copies, not those of an active successor. The tenant's source
binding remains available for explicit resumes until it is revoked. A portable non-fleet resume creates a fresh
work volume rather than looking for the previous region's volume.

Idempotent resume: `POST :resume` with an `Idempotency-Key` header creates one continuation per (source operation,
key); a replay returns it (200), the same key with different input is 409, concurrent replays collide on the Job
name. Without the header a new `<root>-r<n>` is created each time.

Delivery, not exit code: a run's `SUCCEEDED` needs more than `main` exiting 0. A completed run without any
delivery record is `FINALIZING` (the uploader may still be exporting or posting) for ten minutes, then
`EXPORT_FAILED`: a missing record is never success. With managed I/O the uploader's export
must be verified (the sync succeeded and a second, size-only pass finds nothing left), its `STATUS.json` is published
last, and the work volume is released only then; otherwise the operation is `EXPORT_FAILED`, the volume is kept, and
`POST /v1/operations/<id>:export` runs the transfer again on that volume without the simulation (`<id>-x<n>`). The
uploader releases the work volume only once delivery is durable: the export verified or RESULT.json present, and the
API's acknowledgement of the final record (it retries the record four times); without that acknowledgement the
volume stays, the operation shows FINALIZING/EXPORT_FAILED and `:export` re-delivers without recompute. With
application-managed I/O the application writes `/work/RESULT.json` (`{"result_ref": "...", "checkpoint_ref": "..."}`)
after it committed and validated its outputs; the uploader's final record carries it and an attempt without it is
`EXPORT_FAILED` ("no finalization record"). Billing, volume release and cleanup key off the same state.

## Finalization by the application

With application-managed I/O the platform cannot re-upload what the application owns. A run class may therefore
declare a model-owned finalizer, `job.finalize = {"command": "...", "on_exit_codes": [75], "timeout_s": 1800,
"cpu": "1", "memory": "2Gi"}`: a fixed command in the model's own image that re-delivers from the kept work volume
after the scientific compute ended with one of `on_exit_codes` ("compute done, delivery pending"). Never a
caller-supplied executable, never a GPU, never a recomputation.

`POST /v1/operations/<id>:export` (the existing verb) is then accepted for a FAILED application run whose newest
attempt ended with one of those exit codes and whose volume is kept: `<root>-x<n>` is rendered on the same
per-operation volume, in that attempt's cluster, with the original environment (identity token, private input,
execution profile, `CHECKPOINT_REF`, the model's `env`) plus `ATTEMPT_ROLE=finalize`, `FINALIZE_OF=<the compute
attempt's pod>`, `FINALIZE_EPOCH`, `FINALIZE_EXIT_CODE` and `WORK_DIR=/work`; `main` is the model's image (the
finalized attempt's digest when on record) running `finalize.command` with `cpu`/`memory` as requests and limits and
no node selector. An init container removes the compute attempt's bridge files first (`CHECKPOINT.json`,
`CHECKPOINT.ack.json`, `RESULT.json`, `attempts/.ack.json`), so the finalizer's uploader records only what the
finalizer writes and an old result marker can never become this attempt's success; the application's own workspace
and delivery marker are untouched, and whether the workspace is complete is the application's decision from its own
marker. A failed computation (any other exit code) is never finalized; a model without `finalize` keeps the ordinary
behaviour. A declared exit code is a `FailJob` rule on the compute Job: the GPU computation is never restarted
by the Job's backoff for a delivery problem (the ordinary policy, disruptions and SIGKILL/SIGTERM ignored, is
unchanged). A retry is pinned to the cluster that admitted the original (by cluster id, as the dispatcher's
region pin and as the only candidate), so it reuses the kept volume there and never gets a fresh volume in
another cluster of the same class.

Exit semantics: `0` with `/work/RESULT.json` -> the finalizer's record carries `result_ref`, the root reports
`SUCCEEDED` with `exported_by`, the volume is released after the acknowledged record; an exit code in
`on_exit_codes` or any other non-zero -> the finalizer is FAILED, the volume kept, another `:export` allowed; `0`
without RESULT.json -> EXPORT_FAILED. At most three retries per root (`-x1..-x3`, 409 after); one active finalizer per
root: `:export` while one is QUEUED/RUNNING returns it (200). Retries always name the compute attempt in
`FINALIZE_OF`, never a previous finalizer, also when called through a finalizer's own handle.

Identity and accounting: the finalizer's `started` record opens the next epoch of the root (every earlier pod of the
chain is fenced); records carry `attempt_role` (`run`, `export`, `finalize`), stamped by the API from the Job it
rendered, never from the pod's claim; `gpus 0`, no class, no GPU seconds billed. While a finalizer is active the
root's handle (and every chain member's) reports `status: FINALIZING`, `finalizing_by`, and the finalizer pod's
`attempt_id`, `epoch` and `attempt_role`; `:cancel` on the root cancels the active finalizer and leaves the root
FAILED with its volume; `finalize_attempts` counts them. `GET /v1/fleet` lists `execution.finalize`.

## Fleet placement (control cluster API, `FLEET_MANAGER=true`)

The control cluster's API is the entry point for runs. It renders the same Job but creates it on
the control cluster with `spec.managedBy: kueue.x-k8s.io/multikueue`: the local Job controller
leaves it alone, Kueue reserves quota on the manager ClusterQueue of the profile (fleet-wide
flavors `<region>-<pool>`), the cost dispatcher nominates a worker (`docs/SCHEDULING.md`) and
MultiKueue copies the Job there, syncing its status back. The manager Job is the operation; its
copy on the worker carries the pods. What differs from the regional path:

- **Bucket.** The worker is not known when the run is submitted, so inputs and outputs live in
  the tenant's hub-region bucket (`s3://<fleet>-<tenant>-<hub region>`, the `tenant-storage`
  Secret of the control cluster's tenant namespace; `input_prefix` elsewhere is a 400). The runner
  containers read it through the Secret `s3-fleet` that the platform stage writes into every worker's
  tenant namespace (hub-region key, public endpoint). The regional path keeps the regional bucket (`s3`).
- **Region.** None unless pinned: by the caller (`region`) or by a resume (the worker that ran the
  original; `serverless2.nebius/region` label, copied onto the Workload). Image references are
  fleet-wide (`registry.serverless2.local/...`, `docs/IMAGES.md`), so nothing in the Job depends on
  the region: an unpinned run is placed at queue time by the dispatcher, which re-nominates the next
  cluster every 5 min while the run waits. The `region` of the operation is the cluster that admitted
  it (null while it is only nominated).
- **Work volume.** Created by the dispatcher in the worker that ADMITTED the run (claim name and
  `pvc-size-gi` from the pod template; the worker's pod waits for it for at most one reconcile),
  without an owner there: MultiKueue deletes the worker's copy of the Job (and its pods) the moment
  the manager's Job finishes, and a failed run must keep its checkpoints for `:resume`. The uploader
  deletes it after a successful upload; the dispatcher's sweeper deletes it once no manager Job
  references it any more (TTL, a cancel of a queued run; 15 min after creation at the earliest), once
  every Job that still references it was superseded by a continuation on a fresh volume (fallback,
  automatic recovery: the handles stay readable), or when every Job that uses it was
  admitted in another cluster. A reference is a manager Job whose pod template names the claim or that
  the volume pass recorded on it, in any state: a FAILED, EXPORT_FAILED or cancelled handle keeps its
  volume for `:resume`/`:export` until the handle itself goes; a same-volume resume or finalizer counts
  from the moment its Job exists. Deletion completes only once no pod mounts the claim.
- **Status.** The manager Job's status is MultiKueue's mirror of the worker's; while the run is
  active, attempts and logs use the worker's pods (the API finds the worker through the Workload's
  `status.clusterName`, `region` is null until then). After it finishes the worker's copy is gone
  and the attempts are the uploader's records in the bucket (`attempts/<pod>.json`), which is why
  the uploader waits for the kubelet to report `main`'s exit code (up to 2 min)
  before it writes the record. The worker's copy is never listed as an operation of its own
  (label `kueue.x-k8s.io/multikueue-origin`).
- **Cancel / resume.** Cancel of a running run patches the deadline onto the worker's copy and the
  `cancelled` annotation onto the manager's (MultiKueue mirrors the spec only at creation); a
  queued one is deleted on the manager, which removes the worker's copy. Resume checks the volume
  on the worker and creates `<id>-r<n>` on the manager pinned to that region.
- **Billing.** The CronJob bills the manager's Job from the attempt records with the price of
  the region its Workload was admitted in (`cost` and `gpu_seconds` on the operation).

The control cluster's tenant namespaces are rendered by the same chart (`clusters.control`,
`manager: true`): namespace, the profile LocalQueues pointing at the manager ClusterQueues and the
API's RoleBinding; nothing runs there.

## Cancel

`POST /v1/operations/{id}:cancel`: a Job that never started (gated by Kueue, or pending for a node)
is deleted with its PVC and Secret (owner references). A started Job gets `activeDeadlineSeconds: 1`
and the `cancelled` annotation: the controller terminates the pod (SIGTERM, grace period; the
uploader does a partial upload with status `interrupted` at once, then waits for `main`'s own
SIGTERM handling to end and uploads again), the Job ends `Failed/DeadlineExceeded` and is reported
`CANCELLED`, resumable. The kubelet does not refresh a terminating pod's container statuses, which
is why the uploader watches `main`'s processes and not only the API (with the API alone it would
sit until the grace period's SIGKILL, holding the GPU for `graceSeconds`).

## Endpoint calls (`mode: async`)

The same Job shape without a volume: `main` is the runner's `call.sh`, which POSTs the request body
to the model's LiteLLM pass-through route (the caller's key, in a per-operation Secret owned by the
Job, so spend lands on that key) or to the endpoint's **gateway hostname**
(`https://<model>-predictor.models.<region domain>`, `ENDPOINT_DOMAINS`; the Job runs in the endpoint's region) with the
caller's key: the same path, key check and per-key rate limit as an external client, never the
predictor Service (the tenant policy does not reach `models`, so no call skips the key check or the
accounting).
`call.sh` reaches the gateway's in-cluster Service with curl `--connect-to` while keeping the
public TLS name (`ENDPOINT_CONNECT_TO`; the public IP works from pods too, the Service avoids the
NAT hop). It retries 408/429/5xx/connection
errors with backoff for as long as the Job lives (a scaled-to-zero or saturated endpoint is simply
waited for; any other 4xx is the caller's error: exit 2, which a `FailJob` rule turns into a FAILED
operation without pod retries). The uploader puts `out/response.json` and `out/call.json` in the
bucket; `GET /v1/operations/{id}/result` returns the response inline from there. The request body
names the served model as the endpoint expects it (`served_model`, injected when the caller omits `model`).

### Endpoint calls with private input and application-owned output

An endpoint call may carry the `execution` block of a run (`io_mode: application`, `private_input_ref`,
`workload_identity_ref`, `profile_ref`; no placement or recovery): the request and the destination then come from
the binding, never from the Job, and the capture goes to the application's own storage, never to the platform bucket.
`GET /v1/fleet` → `execution.async_private_io` says an API supports it. The binding's files:

| key | meaning |
|---|---|
| `request.json` or `input_url` | the body to POST (inline, 64 KiB bound), or a URL to fetch it from at transfer time |
| `response_url`, `call_url` | presigned PUT URLs for the capture (`response.bin`, `call.json`) |
| `result_ref` | the application's stable reference to the delivered result (opaque; returned as `result_ref`) |
| `refresh_url`, `refresh_token` (optional) | the caller POSTs `{"operation_id", "attempt_id", "phase": "input"\|"output"}` with `Authorization: Bearer <refresh_token>` and, with an identity, `X-Workload-Identity: <projected token>`; the answer carries fresh `input_url` / `response_url` / `call_url` |

With `refresh_url` every transfer asks the application for URLs at that moment (a queued call outlives a URL minted
at submission); static URLs work too. The caller (`call.sh`) fetches the input, probes the endpoint's readiness through
the gateway with an idempotent GET (`health_path`, else `/v1/models` on an OpenAI endpoint, else `/`; a cold or
scaled-to-zero endpoint is woken by it and the probe repeats until it answers), then POSTs the request **exactly once**
with the key and the API's admission token (`x-serverless2-admitted`: the hook counts the call once). `posted.json` is
written before the POST: a pod retried after it finds the marker without a capture and never posts again. No response
(the connection broke, or the edge gave up before the first byte) is an **unknown** outcome: the operation is `FAILED`
("inference outcome unknown"), there is nothing to export, and the application decides whether to submit again; only a
429 from the edge (rejected before the model) is asked again. Any response is captured as `response.bin` plus
`call.json` (`operation_id`, `attempt_id`, `http_status`, `content_type`, `size_bytes`, `sha256`, `timings` with
`posted_at`, `first_byte_at`, `completed_at` as RFC 3339 UTC and `duration_s`, `capture_complete: true`), written to the
call's retained work volume first, `call.json` last. Then it uploads both and writes `RESULT.json` with the `result_ref`
for the uploader's attempt record: the operation is `SUCCEEDED` only with that record; a failed upload is
`EXPORT_FAILED` with the capture kept, and `POST /v1/operations/{id}:export` renders the caller again on the same volume
with `TRANSFER_ONLY=1`: it delivers the complete capture and never calls the endpoint again (no complete capture: the
retry fails with "nothing to export"); while it runs the root reports `FINALIZING`, `finalizing_by` and the retry's
`attempt_id` (`attempt_role: export`). Any other status from the endpoint is captured and delivered as well: the operation is
`FAILED` (exit 2 for a 4xx, the caller's error; exit 5 for another status, the endpoint's error) and
`GET /v1/operations/{id}/result` reports `export_state: succeeded` with the `result_ref` of that delivered error
response; a re-delivery through `:export` ends with the same captured outcome, so a delivered error never becomes a
success, live or after the Job is gone. A FAILED call whose delivery failed (`export_state: failed`, a complete capture)
is delivered again by `:export` like an EXPORT_FAILED one; an unknown outcome has nothing to deliver. The endpoint's pods receive an HTTP request and nothing else: no URL,
token or credential of the binding reaches them. The call's reservation on the key (slot and request, docs/API.md
"Keys") is released by the billing pass when the Job ends; a continuation joins it as a member.

## Status mapping

`EXPORT_FAILED`: the run itself succeeded, its delivery did not (see "Attempts and finalization"); resumable and
exportable again. `FINALIZING`: the run ended, its delivery record is not on record yet. `SUPERSEDED`: capacity
fallback moved the run; the operation's `successor` is live, and reading the original handle returns the successor
(with `requested_id`). An operation whose Job object expired is still served from its durable attempt rows.


| Job | Operation |
|---|---|
| no pod running, no terminal condition | `QUEUED` (gated, pending, or between attempts after a preemption) |
| a pod with `main` running | `RUNNING` |
| condition `Complete` / `SuccessCriteriaMet` | `SUCCEEDED` |
| condition `Failed` / `FailureTarget` | `FAILED`, or `CANCELLED` with the `cancelled` annotation |

`error` is the last failed attempt's container reason or the Job condition message; `logs_url` is a
Grafana Explore query over the operation's pods in Loki (`GRAFANA_URLS`); `gpu_seconds` and `cost`
appear once the billing CronJob has annotated the Job (within two minutes of completion).

## RBAC

The API identity (ClusterRole `serverless2-api-tenant`, bound per tenant namespace on every cluster
including the control cluster): Jobs create/get/list/watch/patch/delete, pods get/list, PVCs
create/get/patch/delete, Secrets create (plus `tenant-storage` get), Kueue Workloads get/list (the
worker of a fleet-placed run); plus `kueue-system/fleet-prices` get (pools per class, registries).
The job ServiceAccount (`job-runner`, Role `job-runner` in `charts/tenant`): pods get/patch (the
uploader polls its own pod), PVCs get/delete (release after success). The dispatcher's identity on
a worker (`multikueue`, `charts/platform/templates/fleet-access`) adds PVC create/get/list/patch/delete
for the work volumes of fleet-placed runs.

## Writing a job for `container-run`

```sh
curl -X POST $API/v1/models/container-run:invoke -H "Authorization: Bearer $KEY" -d '{
  "name": "finetune-7b", "region": "eu-north1", "priority": "normal",
  "input": { "image": "registry.serverless2.local/nebius/trainer:1.2", "gpus": 1, "disk_gi": 200,
             "input_prefix": "s3://<fleet>-<tenant>-eu-north1/uploads/<id>",
             "command": "python train.py --data /work/in --out /work/out --resume-from $CHECKPOINT_DIR" } }'
```

The contract for the container: inputs under `/work/in`, write results under `/work/out`, write
checkpoints under `/work/checkpoint` (`$CHECKPOINT_DIR`) and start from them when they exist,
exit 0 when done, handle SIGTERM (up to `graceSeconds`) by checkpointing. Everything under
`/work` except `in/` is uploaded to `output_prefix` when `main` ends, whatever the exit code.
SIGTERM reaches the shell of a string command (shared PID namespace); a long-running child of
that shell (`python train.py`) only sees it if the script forwards it (`exec python train.py`,
or a `trap`), so prefer `exec` for the final command of a script.

## Scratch storage

A run class may set `job.scratch: network | local-nvme` (`container-run` exposes it as the `scratch`
parameter, default `network`). `network` is the per-operation PVC described above (automatic resume on
the same volume). `local-nvme` renders `/work` as an emptyDir sized `pvcSizeGi` on the node's host NVMe
and pins the pod to nodes labelled `serverless2.nebius/local-nvme=true` (pools with `local_nvme = true`);
the uploader still syncs `/work` to the bucket, but a preempted pod's scratch is gone with it, so the
run must checkpoint to object storage itself and `:resume` is not available (`resumable` stays false).
Every run pod carries seccomp `RuntimeDefault`, no capabilities and no privilege escalation; the tenant
namespace enforces Pod Security `baseline`.
