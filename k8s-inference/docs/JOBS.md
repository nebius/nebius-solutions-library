# Jobs: run classes, endpoint calls, checkpoint resume

*For developers and operators: how a run becomes a Job, how it is queued, placed, resumed and cancelled, and the multi-node variant.*

Every asynchronous operation of the API is one Kubernetes `batch/v1` Job in a tenant namespace.
No workflow engine: the Job controller retries, Kueue queues and admits, the CSI volume keeps the
checkpoints, a 30 MB runner image moves data. `services/api/jobs.py` (about 450 lines) renders,
submits, reads, cancels and resumes them. Two entry points create them: a regional API (hub,
eu-south1) creates the Job in its own cluster; the fleet API on the control cluster creates it on the
MultiKueue manager and lets Kueue plus the cost dispatcher place it on a worker ("Fleet placement"
below). Argo Workflows is gone (lane F5, 2026-10-07): no WorkflowTemplates, executor identities or
archive database remain.

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
deployments:                      # the regions the run class may land in; per-region parameter defaults and price
  hub:       { price_per_gpu_hour: 2.15, parameters: { nb_min_ci: "16000" } }
  eu-south1: { price_per_gpu_hour: 1.60, parameters: { nb_min_ci: "16000" } }
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

Three run classes ship: `container-run` (any image and command; the generic job class),
`hello-run` (CPU smoke test). Application layers define their own run classes through the model API.
`parameters` is what the UI shows and what `input` may contain (unknown names, or a missing
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
```

The pod runs with `fsGroup: 10001` (the runner's uid) so fetch and upload work whatever user the
model image runs as, and with a shared PID namespace so the command's shell is not PID 1 and
SIGTERM really terminates `sh -c ...` commands (PID 1 ignores it, and the pod would hold its GPU
until the grace period's SIGKILL). The runner image (`services/jobs`, `serverless2/jobs:0.1.4` in
each region's registry under the same path; `RUNNER_IMAGE` on the API names the hub one) is alpine +
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
endpoint shows live pods only. Measured on the hub: an endpoint scaling up on the shared H100 pool
preempted a run's pod two seconds after `main` had finished; the Job was still `Complete`, the
outputs were in the bucket, the pod was gone. A node that dies without warning (a spot VM stopped by
Nebius: no SIGTERM reaches the pod; measured 2026-10-09 on a B300 spot node, `docs/dev-fleet/VERIFICATION-DEV.md`)
leaves the "started" record the uploader writes to the volume and the bucket when `main` begins (jobs
image 0.1.8): the attempt shows as `PREEMPTED` with reason "node lost without warning", no end time, and
bills nothing. A stop that is orderly enough for the SIGTERM path (the B300 spot VM stopped on
2026-10-09) gets the full record on the volume, which before 0.1.8 reached the bucket, and the
operation, only with the next upload on that volume (the resumed attempt's); the start record makes
the attempt visible at once either way.

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
  like any run (the dispatcher accepts JobSet owners).
- **InfiniBand in the pod, without privileges.** Nebius GPU node images ship the InfiniBand
  drivers (8 x 400 Gb/s NDR ports `mlx5_0..7`, interfaces `ib0..7`, measured on an 8x H100 node in a
  fabric-3 GPU cluster), and Managed Kubernetes runs **DraNet** (Kubernetes DRA driver for the RDMA
  NICs; DeviceClasses `ib.networking.nebius.ai` and `rdma.networking.nebius.ai` on every cluster)
  on the nodes of a node group that sets `gpu_settings.dra = true` (`nebius.com/dranet-rdma-capable`
  label, one ResourceSlice per node). Terraform sets it on every `interconnect: infiniband` pool;
  without it a node has the NICs but publishes no devices and a claiming pod never schedules
  (`cannot allocate all claims`, measured 2026-10-08). A `required` run references the tenant's
  ResourceClaimTemplate `ib-<n>` (charts/tenant, `infiniband: true`; n = the pool's
  `ib_devices_per_node`, ExactCount because that is what Kueue's DRA accounting supports): the
  node's `n` RDMA interfaces move into the pod, `main` gets `resources.claims: [ib]`, Kueue charges
  `nebius.ai/infiniband` x n against the pool's quota (`charts/fleet`, Kueue `deviceClassMappings`),
  and the pod gets the NCCL environment of the
  Nebius NCCL tutorial (`NCCL_IB_HCA=mlx5`, `NCCL_SOCKET_IFNAME=eth0`, `UCX_NET_DEVICES=eth0`,
  `NCCL_COLLNET_ENABLE=0`, `SHARP_COLL_ENABLE_PCI_RELAXED_ORDERING=1`; a job class's own `env`
  wins). **No capability, no root, no privileged namespace.** RDMA pins memory
  (`ibv_reg_mr`), which `RLIMIT_MEMLOCK` bounds: the Nebius node image ships 8 MiB, and with every
  capability dropped NCCL fails at `ibv_reg_mr_iova2` / `ibv_create_cq` with "Cannot allocate
  memory" (measured 2026-10-08, two restarts, docs/VERIFICATION.md). The usual fix is
  `CAP_IPC_LOCK` (it bypasses the limit), which is outside Pod Security Admission `baseline` and
  would force the namespace to `privileged`. Instead the GPU nodes' container runtime runs with an
  unlimited memlock limit (`LimitMEMLOCK=infinity` on the containerd unit, written by the pools'
  cloud-init at boot and by the node-config DaemonSet on older nodes): every container inherits
  it, the run keeps `drop: [ALL]`, seccomp `RuntimeDefault`, no privilege escalation, non-root
  runner sidecars, and the tenant namespace stays at `baseline`. (The same approach as NVIDIA's
  InfiniBand node configuration for Kubernetes; `privileged: true` in Nebius' own NCCL test
  manifest exists only to raise that limit.) NVIDIA Network Operator is not needed on the Nebius node
  images (docs.nebius.com/kubernetes/gpu/set-up: required only for custom images or B200).
- **Scratch and checkpoints.** `/work` is node-local per pod (`emptyDir`, `disk_gi` limit; the
  kubelet's ephemeral storage, which is the local NVMe on presets that have it), not a per-run RWO
  volume: N pods on N nodes cannot share one disk. Inputs are fetched by every pod. Checkpoints:
  `checkpoints: shared` mounts the tenant's RWX claim `scratch-shared` (charts/tenant renders a
  static PV on the cluster's shared weights filesystem, `/mnt/weights/tenants/<tenant>`) at
  `/work/checkpoint` under `checkpoints/<root id>`, so a restart and a `:resume` continue from it;
  `local` (default) keeps them under `/work`, gone with the pod. Rank 0 uploads `/work`
  (minus `in/` and `checkpoint/`) and `STATUS.json`; every rank uploads its attempt record
  (`UPLOAD_SCOPE=rank0`, runner 0.1.6).
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
scale-up (`pod didn't trigger scale-up: cannot allocate all claims`, measured on the hub
2026-10-08; GKE documents the same rule for DRA node pools). An InfiniBand pool therefore keeps
`min_nodes >= 1` (the renderer and the solution's tfvars validation enforce it); from one node the
autoscaler templates the ResourceSlices of the existing node. Reserved pools (the usual home of a
16-node job) are fixed-size anyway.

### Limits of multi-node runs

What works, and where the edges are (sources: docs.nebius.com/compute/clusters/gpu,
docs.nebius.com/kubernetes/gpu/clusters, docs.nebius.com/kubernetes/node-groups/manage, and the
measurements in docs/VERIFICATION.md):

| Limit | Why |
|---|---|
| All pods of one run land in **one pool = one GPU cluster = one InfiniBand fabric**. No cross-pool, cross-region or mixed-platform runs. | A Nebius GPU cluster lives in one fabric of one region and one project; nodes outside it have no InfiniBand path to it (Nebius isolates GPU clusters with InfiniBand partition keys: nodes of different GPU clusters cannot talk over the fabric even on the same physical fabric). The API pins the JobSet to the pools of the model's classes that have the fabric; Kueue admits the whole pod set on one flavor. |
| **8-GPU presets only.** | GPU clusters accept only full-node presets (`8gpu-*`); the renderer, the solution's tfvars validation and the Nebius API refuse others. One pod takes one whole node. |
| **`min_nodes >= 1`** on an InfiniBand pool (no scale from zero). | The cluster autoscaler templates an empty node group without DRA ResourceSlices, so a pod that claims the NICs never triggers a scale-up (`cannot allocate all claims`, measured). From one node on, the autoscaler scales the pool up to `max_nodes`. Reserved pools are fixed-size anyway. |
| **NIC claims are ExactCount: 8 per node (4 on GB300).** | Kueue's DRA accounting (0.20) supports ExactCount claims only; the tenant's `ib-8` / `ib-4` templates hand the pod every NIC of its node (the pod has the whole node anyway). |
| **Size of one run**: `nodes <= MULTINODE_MAX_NODES` (64 by default), a node group holds at most 100 nodes, and a fabric has limited GPU capacity. | Nebius publishes no fixed maximum per GPU cluster; "each fabric has limited GPU capacity" and a GPU cluster is bounded by the quota of its project and the free capacity of its fabric. A 16-node run needs 16 nodes in ONE pool (one node group, one GPU cluster): size `max_nodes` (and the reservation) accordingly; the Kueue quota of the pool is `ib_devices_per_node x max_nodes` NICs. |
| **Spot preemption = restart-all** from the last checkpoint; after `maxRestarts` (3) the run is FAILED. | A partial rank set cannot continue an NCCL job. Use `checkpoints: shared` so the restart (and a `:resume`) continues from the shared filesystem; `local` checkpoints die with the pod. Reserved or on-demand pools avoid the restarts. |
| **One run per node at a time.** | A pod requests the preset's GPU count, CPU and memory; nothing else fits on the node while it runs. |
| **Which platforms and regions have fabrics today** | H100 (`gpu-h100-sxm`): eu-north1 `fabric-2/3/4/6`. H200 (`gpu-h200-sxm`): eu-north1 `fabric-7`, eu-west1 `fabric-5`, eu-north2 `eu-north2-a`. B200 (`gpu-b200-sxm`, `8gpu-160vcpu-1792gb`): us-central1, me-west1. B300 (`gpu-b300-sxm`, `8gpu-192vcpu-2768gb`): uk-south1, eu-west2, us-north1. GB300 (`gpu-gb300`): 4 NICs per node. No fabric: L40S, RTX PRO 6000, every 1-GPU preset, eu-south1 today. The fabric names change as Nebius adds capacity: `fleet.yaml infiniband_fabric` is the one place to update (docs.nebius.com/compute/clusters/gpu). |

Terraform side (`fleet.yaml`, docs/FLEET.md): a pool with `interconnect: infiniband` (8-GPU preset)
gets a `nebius_compute_v1_gpu_cluster` on `infiniband_fabric` (region and platform specific:
`fabric-2/3/4/6` H100 eu-north1, `fabric-7` H200 eu-north1, `fabric-5` H200 eu-west1,
`eu-north2-a` H200 eu-north2; B300 in uk-south1/eu-west2/us-north1, B200 in us-central1/me-west1
per docs.nebius.com/kubernetes/gpu/clusters) and its node group attaches through
`template.gpu_cluster`. Pools without it can run multi-node jobs over the pod network only on explicit request
(`interconnect: none`), which is fine for small models and useless for 16 x B300.

Verified (2026-10-08, docs/VERIFICATION.md): the control path on the hub with a 2-pod CPU-only
JobSet through the API (creation, Kueue admission, rank env, rank-0 upload, cancel), and the
InfiniBand data path on a temporary pool of 2 x 8 H100 spot nodes in a fabric-3 GPU cluster:
DRA claims allocated (8 NICs per pod), NCCL `NET/IB` on `mlx5_0..7`, a 1 GiB all-reduce over
16 GPUs at 436.5 GB/s bus bandwidth, restart-all after rank failures, rank-0 upload. 16 x B300 is
the same path on an `8gpu-192vcpu-2768gb` pool (uk-south1, eu-west2, us-north1).

## Fleet placement (control cluster API, `FLEET_MANAGER=true`)

The control cluster's API is the entry point for runs. It renders the same Job but creates it on
the control cluster with `spec.managedBy: kueue.x-k8s.io/multikueue`: the local Job controller
leaves it alone, Kueue reserves quota on the manager ClusterQueue of the profile (fleet-wide
flavors `<region>-<pool>`), the cost dispatcher nominates a worker (`docs/SCHEDULING.md`) and
MultiKueue copies the Job there, syncing its status back. The manager Job is the operation; its
copy on the worker carries the pods. What differs from the regional path:

- **Bucket.** The worker is not known when the run is submitted, so inputs and outputs live in
  the tenant's hub-region bucket (`s3://serverless2-<tenant>-eu-north1`, the `tenant-storage`
  Secret of the control cluster's tenant namespace; `input_prefix` elsewhere is a 400). The runner
  containers read it through the Secret `s3-fleet` that onboarding writes into every worker's tenant
  namespace (hub-region key, public endpoint). The regional path keeps the regional bucket (`s3`).
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
  deletes it after a successful upload; the dispatcher's sweeper deletes it once none of the manager
  Jobs recorded on it exists any more (TTL, a cancel of a queued run; 15 min after creation at the
  earliest), or when every Job that uses it was admitted in another cluster.
- **Status.** The manager Job's status is MultiKueue's mirror of the worker's; while the run is
  active, attempts and logs use the worker's pods (the API finds the worker through the Workload's
  `status.clusterName`, `region` is null until then). After it finishes the worker's copy is gone
  and the attempts are the uploader's records in the bucket (`attempts/<pod>.json`), which is why
  the uploader waits for the kubelet to report `main`'s exit code (up to 2 min, runner 0.1.3)
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
is why the uploader watches `main`'s processes and not only the API (measured: with the API alone
it sat until the grace period's SIGKILL, holding the GPU for `graceSeconds`).

## Endpoint calls (`mode: async`)

The same Job shape without a volume: `main` is the runner's `call.sh`, which POSTs the request body
to the model's LiteLLM pass-through route (the caller's key, in a per-operation Secret owned by the
Job, so spend lands on that key) or to the endpoint's **gateway hostname**
(`https://<model>-predictor.models.<ENDPOINT_DOMAIN>`, the regional API's `ENDPOINT_DOMAIN`) with the
caller's key: the same path, key check and per-key rate limit as an external client, never the
predictor Service (the tenant policy does not reach `models`, so no call skips the key check or the
accounting).
`call.sh` reaches the gateway's in-cluster Service with curl `--connect-to` while keeping the
public TLS name (`ENDPOINT_CONNECT_TO`, runner 0.1.4; the public IP works from pods too, measured,
the Service avoids the NAT hop). It retries 408/429/5xx/connection
errors with backoff for as long as the Job lives (a scaled-to-zero or saturated endpoint is simply
waited for; any other 4xx is the caller's error: exit 2, which a `FailJob` rule turns into a FAILED
operation without pod retries). The uploader puts `out/response.json` and `out/call.json` in the
bucket; `GET /v1/operations/{id}/result` returns the response inline from there. The request body
names the served model as the endpoint expects it (`served_model`, injected when the caller omits `model`).

## Status mapping

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
a worker (`multikueue`, `clusters/common/manifests/fleet-access`) adds PVC create/get/list/patch/delete
for the work volumes of fleet-placed runs.

## Verified on the hub (2026-10-07, tenant `eval`, API run locally against the cluster)

| Case | Result |
|---|---|
| `hello-run` | QUEUED -> RUNNING -> SUCCEEDED in 58 s; `STATUS.json`, `out/result.json`, `attempts/<pod>.json` in the bucket; PVC deleted by the uploader |
| a GPU run class (molecular dynamics, 5k steps), H100 spot | SUCCEEDED; outputs and checkpoint uploaded; billed 12 GPU-s = $0.0072 to the key (`/key/update`), PVC released |
| `container-run` checkpoint loop, pod deleted mid-run | replacement pod on the same volume, `FETCH SKIP`, `start at n=9`; Job stayed `Running`, not failed |
| cancel while running | pod gone 12 s after `:cancel` (SIGTERM reaches `sh` through the shared PID namespace); operation CANCELLED, attempt CANCELLED, resumable |
| `:resume` of the cancelled loop | `<id>-r1` on the same PVC, `start at n=37`, SUCCEEDED, `out/done.txt` under `operations/<id>-r1/`, PVC released; the PVC had both Jobs as owners |
| `async` call to an OpenAI endpoint | SUCCEEDED in 155 s (cold start waited for by `call.sh`), response inline from `out/response.json`; a wrong `model` name is a 404: FAILED at once |
| endpoint preempts a run | an endpoint scaling up preempted the run's pod 2 s after `main` ended: Job `Complete`, pod gone, attempt record from the bucket is the only history |
| billing pass | 7 runs billed once, idempotent on the second pass, two leftover volumes released |

Two findings changed the design during verification: a pod deleted by preemption leaves no
attempt (hence the records in the bucket), and a resumed Job must be re-rendered, not copied
(Kueue `PodSetUpdate: conflict`). A copy of a finished Job's spec is never a valid new Job.

## Writing a job for `container-run`

```sh
curl -X POST $API/v1/models/container-run:invoke -H "Authorization: Bearer $KEY" -d '{
  "name": "finetune-7b", "region": "eu-south1", "priority": "normal",
  "input": { "image": "registry.serverless2.local/nebius/trainer:1.2", "gpus": 1, "disk_gi": 200,
             "input_prefix": "s3://serverless2-<tenant>-eu-south1/uploads/<id>",
             "command": "python train.py --data /work/in --out /work/out --resume-from $CHECKPOINT_DIR" } }'
```

The contract for the container: inputs under `/work/in`, write results under `/work/out`, write
checkpoints under `/work/checkpoint` (`$CHECKPOINT_DIR`) and start from them when they exist,
exit 0 when done, handle SIGTERM (up to `graceSeconds`) by checkpointing. Everything under
`/work` except `in/` is uploaded to `output_prefix` when `main` ends, whatever the exit code.
SIGTERM reaches the shell of a string command (shared PID namespace); a long-running child of
that shell (`python train.py`) only sees it if the script forwards it (`exec python train.py`,
or a `trap`), so prefer `exec` for the final command of a script.

## Terraform solution (2026-10-08): scratch storage

A run class may set `job.scratch: network | local-nvme` (`container-run` exposes it as the `scratch`
parameter, default `network`). `network` is the per-operation PVC described above (automatic resume on
the same volume). `local-nvme` renders `/work` as an emptyDir sized `pvcSizeGi` on the node's host NVMe
and pins the pod to nodes labelled `serverless2.nebius/local-nvme=true` (pools with `local_nvme = true`);
the uploader still syncs `/work` to the bucket, but a preempted pod's scratch is gone with it, so the
run must checkpoint to object storage itself and `:resume` is not available (`resumable` stays false).
Every run pod carries seccomp `RuntimeDefault`, no capabilities and no privilege escalation; the tenant
namespace enforces Pod Security `baseline`.
