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
  memory: 64Gi                    # request = limit (memoryLimit overrides the limit: what the container may grow to, not guaranteed memory)
  retry: { maxAttempts: 2, onExitCodes: [137, 143] }   # optional (docs/JOBS.md "Retries"): the class's bounded retry policy; without it the platform's rules below
  ephemeralStorage: 200Gi         # optional: the kubelet's node-local scratch (the writable layer, emptyDirs, /work on local-nvme); ephemeralStorageLimit optional (= the request)
  pvcSizeGi: 100                  # the work volume (/work), ReadWriteOnce, regional
  shm: 16Gi                       # /dev/shm emptyDir
  pool: "{{pool}}"                # nodeSelector serverless2.nebius/pool (omit: Kueue's flavor decides)
  imagePullSecret: ngc            # Secret in the tenant namespace
  graceSeconds: 120               # SIGTERM -> SIGKILL window (checkpoint + partial upload); 300 for slow checkpoints
  s3Env: true                     # envFrom Secret `s3` (AWS_* for the tenant bucket) on the main container
  inputs: "{{input_prefix}}"      # default: the input_prefix parameter
  uploadExcludes: "--exclude 'scratch/*'"
  workMountAliases: [{ path: /mnt/fs2-scientific/work, scope: attempt }]   # optional (docs/JOBS.md "Work mount aliases"): the work volume at a canonical path
  assets: [{ alias: af3-params, mountPath: /models/af3.bin.zst }]          # optional (docs/JOBS.md "Restricted assets"): a registered, granted asset on main, read-only
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
`required` one, are a 400). A parameter may carry the operator's bounds, enforced at invoke on the effective value
(the caller's over the defaults) before any reservation or Job: a `number` with `min`/`max` (inclusive) and
`integer: true` (whole numbers only; a string, a boolean, NaN/inf or a fraction is refused, never coerced), a `select`
with `options` (exactly one of them). `job.maxGpus: N` bounds a class's whole shape (`nodes` x GPUs per node ≤ N) on
the rendered values. A class declaring none of these behaves as before; the renderer's own refusals (a shape no pool
has, the node maximum) stay. With these an operator catalog class with a templated shape (`nodes: "{{nodes}}"`,
`gpu: "{{gpus_per_node}}"`) admits only operator-approved shapes under one model id, whatever key calls it directly.
`deployments.<cluster>.parameters` are per-region defaults the caller
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

## Work mount aliases

An application that writes to a fixed canonical path (a retained collector reads `/mnt/fs2-scientific/work`, say) gets
the run's **own work volume** mounted there as well: `job.workMountAliases: [{path, scope}]` (public spec
`work_mount_aliases`), operator-owned on the run class, at most four entries. Nothing new is created: the alias is the
same `work` volume (the per-operation claim, or the node-local scratch of `scratch: local-nvme`) mounted a second
time on `main`, with `/work` unchanged, so everything under the alias is uploaded, kept across a resume and finalized
like the rest of `/work`.

- `scope: attempt` (default): the alias shows `/work/aliases/<slug>/<pod name>/`, the attempt's own directory, through
  a `subPathExpr` the API renders from the pod's own name (`$(S2_POD_NAME)`, downward API; the name is
  platform-owned, a class that sets that variable is refused; nothing a caller sends reaches the expression). A retry or a
  resume on the kept volume is a new pod and so a new directory: attempts never share a canonical writable output, and the
  attempt record (`attempt_id`) names the subtree. The CPU finalizer sees the **finalized** attempt's directory (the API
  sets the name to that pod's, a literal).
- `scope: run`: `/work/aliases/<slug>/`, one directory per run (shared by its attempts on the kept volume).
- `<slug>` is the path with `/` turned into `-` (`mnt-fs2-scientific-work`).

Validation at registration and at render: absolute, normalised, no `..`, `[A-Za-z0-9._/-]`; never `/`, `/work` or
anything under it, nor under `/dev /proc /sys /etc /run /var /tmp /usr /bin /sbin /lib /lib64 /boot /root
/opt/serverless2`; no overlap with another alias or with any mount of the pod (data, weights, checkpoint, shm);
single-node runs only. The fetch init container creates the attempt directory `2770`, owned by the runner user and the
pod's `fsGroup` (every container of the pod carries that group, whatever user the model image runs as; setgid keeps the
files group-owned for the uploader), tenant-private rather than world-writable: a subPath directory the kubelet creates
is root-owned and a non-root model could not write it. No symlink, no
privilege, no host path, no shared tenant mount: the alias cannot reach anything the run does not already own.

## Resume

A resumed operation names its successor: durably (the attempt marks the handle and the stage gate follow, also once
the original Job expired) and on its Job while it exists. `GET /v1/operations/<original>` therefore answers with the
current head of the lineage (the original stays readable through its rows), a graph child waiting on a resumed
dependency waits on the successor's state, and a second `:resume` of the original is refused naming the head.

**Restart from inputs** (api 0.11.74): every run admitted leaves an admission record in the ledger (the Job's labels
and annotations, region, timeout, output prefix), kept past the Job's TTL. When a FAILED root's Job and work volume are
gone (the Job's TTL garbage-collects the claim it owns) and no checkpoint pointer exists, `:resume` with
`restart_from_inputs: true` renders the run again from that record on a fresh work volume on the same cluster, as a
continuation of the same root (`<root>-rN`, annotation `restarted-from-inputs`): same owner, key, class binding and
execution block, the current placement rules. A fleet-placed root restarts as a manager Job placed by the fleet again
(the record keeps its `managedBy`; never a plain Job on the manager's cluster, never the old worker pin); a
single-cluster root restarts on its own cluster. The terminal state comes from the durable attempt rows: a SUCCEEDED
root is never restarted (409, its result stands), CANCELLED and FAILED may be. Without the flag the resume is refused
(409) so a checkpoint is never dropped unnoticed; a root admitted before 0.11.74 has no record and is refused (404
"submit a new run"): nothing is reconstructed from thin air. The run
ledger's lineage head (`keys.run_head`, `stage_head`) is the authority behind the link: the handle follows it even
without an annotation or mark, and the owner check applies to the named operation and to the head. `:cancel` by a
graph root's id cancels the graph (its stages and the root lineage's head) also once the root's Job expired.

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
  The leaf `checkpoints/<root>` is a kubelet subPath: created root-owned 0755 when missing, so every rank pod (and
  the gang's CPU finalizer) first runs the trusted provisioning init (`scratch.sh provision /work/checkpoint`, root with
  CHOWN/FOWNER/DAC_OVERRIDE only) on exactly that mount point: 10001:10001, 2770, not recursive, idempotent; the ranks
  themselves stay non-root without a capability.

**Scale-from-zero does not work with DRA claims.** The cluster autoscaler builds the template of an
empty node group without ResourceSlices, so a pod that claims InfiniBand NICs never triggers a
scale-up (`pod didn't trigger scale-up: cannot allocate all claims`; GKE documents the same rule for
DRA node pools). It does not grow from one node either: the autoscaler's simulation of a new node
copies the devices without their attributes and the DeviceClass selector fails (`"DynamicResources"
filter plugin: class ib.networking.nebius.ai: selector #0: CEL runtime error: no such key: ifName`).
The dispatcher therefore moves the pool's floor itself (docs/SCHEDULING.md "InfiniBand pools grow on
demand"): submitting a run is all it takes; the nodes boot after admission (a few minutes, the usual
start of a scaled-from-zero pool). Reserved pools (the usual home of a 16-node job) are fixed-size anyway.

### Gang attempts

Every record carries the pod's UID as the API observes it on the pod it reads anyway for the runtime digest
(`pod_uid`: a single-node run's pod, an endpoint caller, a CPU finalizer, a gang rank alike; a pod the API cannot read any
more keeps the UID its row holds; never the record's claim). Every pod of a JobSet records its attempt
(`POST /internal/attempts`, its own projected token), and the API observes the pod's gang membership on the pod itself,
never from the record: the JobSet controller's `jobset.sigs.k8s.io/restart-attempt`
(the incarnation), the indexed Job's `batch.kubernetes.io/job-completion-index` (the rank) and the pod UID (migration 24:
`gang_rank`, `gang_restart`, `pod_uid`). **One epoch per incarnation**: the ranks of one restart share it, so a peer's
start record never fences the coordinator (before 0.11.96 every sibling took the next epoch and the coordinator's
checkpoint and success came back 409 "stale record refused"); the next incarnation (JobSet `Recreate`: every pod
recreated) is the next epoch, and the prior gang is fenced like any replaced attempt (its own interruption or failure
still closes its record, promoting nothing). **Coordinator authority**: only rank 0's record carries the gang's
`checkpoint_ref`/`checkpoint_generation` and `result_ref` (a peer's are dropped; its timings, exit code and GPUs stay:
every rank is accounted). The operation's `attempt_id`/`epoch` name the coordinator of the newest incarnation, and the
owner-scoped operation exposes the observed fields on its current attempt and on every `attempts[]` entry (`pod_uid`,
`gang_rank`, `gang_restart`: from the record, and from the live pod itself while it exists), so an application broker can
verify a peer against the observed coordinator of the same epoch and incarnation without trusting any caller's claim; the
gang's outcome counts every rank (the JobSet's `All` success policy): `SUCCEEDED` only when every rank succeeded,
`FAILED` when any rank failed. A late record of a pod the API cannot read any more keeps only that pod's own earlier row
(its epoch, its fences); a gang pod never recorded and unreadable has no proof of its incarnation and is refused (409),
never attributed to any gang: an old coordinator coming back late cannot acquire the current incarnation's authority.
Ordinary single-pod runs keep one epoch per pod, unchanged.

**RDMA memlock, stated.** An InfiniBand gang runs with `drop: [ALL]` under PSA baseline and no IPC_LOCK: RDMA pins
memory under RLIMIT_MEMLOCK, which the GPU nodes' container runtime sets unlimited (cloud-init + node-config). The
render states that contract to the run, `S2_RDMA_MEMLOCK_POLICY=node-unlimited`, `S2_RDMA_CAPABILITIES=none`,
`S2_RDMA_DEVICES_PER_NODE=<n>`, so an application validator written for a capability-based host (IPC_LOCK,
`no_new_privs=0`) can accept the native path explicitly instead of probing for capabilities the platform never grants.
No capability is added for it.

**Restoring a gang: the incarnation-frozen pointer.** A JobSet `Recreate` restarts every rank with the environment it
was rendered with (`CHECKPOINT_REF` is the explicit continuation reference of the render, if any); the newest committed
state lives in the records. The start ACK of EVERY rank (the API's answer to its start record, which the uploader keeps
at `/work/attempts/.ack.json`) therefore carries `resume_checkpoint_ref`, `resume_source_epoch` and `resume_source`:
chosen ONCE when the incarnation opens (its first start record, whichever rank), as the newest committed checkpoint
pointer of a COORDINATOR (rank 0, or an ordinary attempt of the chain) of an EARLIER epoch of the same root
(`committed`, with that epoch), else the explicit continuation reference (`explicit`), else null; stored on that first
row and copied to every sibling of the incarnation (migration 26), so all ranks restore the same state, a sibling that
records later never sees a pointer committed by its own incarnation, and a late record of a rank keeps its own row's
value. Never a peer's pointer, never "the latest" at read time. The owner-scoped operation shows the same three fields on
its current attempt and on every `attempts[]` entry of a gang record (an observed rank: the start ACK froze the triple
there; all three null = decided, nothing to restore), and NO such keys on a row the ACK never decided (a single-pod
attempt, a live pod without its record): absent is not "fresh". A broker authorises exactly the restored inputs. The application
decides what to do with it (restore, or fail when the expected recovery identity is missing); the platform maintains no
application pointer.

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
| **"Delivery pending" never restarts the gang.** A class with `finalize.on_exit_codes` installs that exit as a `podFailurePolicy` FailJob on the indexed Job and a JobSet failure-policy rule (`FailJobSet` on reason `PodFailurePolicy`): the completed science is never recomputed; `:export` runs the model's finalizer as ONE CPU pod on the shared checkpoint claim ("Finalization by the application"). Needs `checkpoints: shared`; a gang without it retains nothing to deliver (409). A managed-I/O gang has no re-export (rank 0's `/work` is node-local). | JobSet ≥ v0.6 (`failurePolicy.rules`). |
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

## Restricted assets

A licensed parameter file or a pinned private tree (AlphaFold 3 parameters, a BindCraft/PyRosetta tree) reaches a run
through three operator-owned pieces; no request or model-request field can choose a path, a claim, a group or an image:

1. *Registry* (`terraform.tfvars` `assets`, in the fleet document as `assets.yaml`): `alias: {kind: file | tree,
   host_path, gid (65532), sha256, size}`. `sha256` is the file's digest or, for a tree, the inventory digest (the tree's
   directory is already named by it); it is recorded on every run that mounts the alias
   (`serverless2.nebius/assets: af3-params@sha256:…` / `bindcraft-tree@inventory:…`), never computed per run.
2. *Grant* (`tenants.<name>.assets`): the tenant chart renders, per granted alias, one static PersistentVolume rooted
   **at the asset itself** (`hostPath` of type `File` or `Directory`, `ReadOnlyMany`) and the claim `asset-<alias>` in
   the tenant namespace. The volume root is the file or the pinned tree: no parent to traverse from inside a pod, no
   sibling in the volume, no `subPath`. The staging parents stay root-only (the kubelet performs the bind mount as root).
   A tenant without the grant has no claim: a class naming the alias is refused at render.
3. *Class* (`job.assets: [{alias, mountPath}]`, public spec `assets: [{alias, mount_path}]`, admin-defined, at most eight
   entries): the claim is
   mounted on `main` **only**, read-only on the claim reference and on the mount, at a bounded absolute path that
   overlaps no other mount (data, weights, checkpoints, shm, work aliases); the asset's `gid` is added to the pod's
   `supplementalGroups` (every container carries the group; only `main` has a mount; a group without a mount reads
   nothing). Single-node runs only. **Main-compute-only**: a transfer-only export retry and the application finalizer
   (the class rendered again on the kept volume with a CPU container in main's place) carry no asset volume, mount or
   group and no identity annotation.

Ownership never changes: `fsGroup: 10001` stays on the pod for the work volume and emptyDirs (the volume types the
kubelet applies ownership management to), and a hostPath volume is never chowned by the kubelet, so the files keep
`0440 root:<gid>` (directories `0550`). `main` may run as any user: it reads through the group. The sidecars keep
`/work` exactly as before. Proof after a run: `stat -c '%U:%G %a' <host_path>` on the node is unchanged.

The shared claims cannot reach an asset: a `weights` or `data` path that equals, lies under, or is an ancestor of a
registered asset's path on the shared filesystem is refused (a container running as root would read a 0440 root-owned
file through an ancestor mount). What the platform cannot prove is an application that copies the file into `/work`
itself: that stays the application's licence responsibility. CPU and GPU stage classes declare their own assets: the
AF3 prep class names the public database through `weights` and no asset; the GPU class names `af3-params` and no
database.

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
| `retry` | bounded application retries of this run, within the same Job (one member, one reservation, one GPU bound): `{"max_attempts": 2, "on_exit_codes": [137, 143]}` renders at most two pods, a replacement only after an exit in `on_exit_codes` (the retained scientific policy's SIGKILL/SIGTERM exits by default), any other non-zero exit fails the run at once; infrastructure disruptions stay the platform's (ignored, never an attempt). Overrides the run class's own `retry` policy (docs/JOBS.md "Retries"); single-node runs only |
| `recovery` | `on_infrastructure_loss: retry` (default: a replacement attempt on the same volume, as above) `fail` (the operation fails on a disruption; resume it with a checkpoint reference) or `recover` (opt-in: after a confirmed infrastructure loss the control API continues the run from its newest acknowledged checkpoint pointer on the next class of its preference, docs/SCHEDULING.md "Automatic recovery after infrastructure loss"; `max_recoveries` 1..3 default 1, `wait_s` default 300, `deadline_s`: the successor inherits the root's remaining deadline). `checkpoint_ref` reaches the run as `CHECKPOINT_REF`; `POST /v1/operations/<id>:resume` with `{"checkpoint_ref": ..., "gpu_class": ...}` resumes a run from a portable checkpoint the application restores itself: no previous volume, any permitted cluster, optionally another class and therefore that class's image |

Every run pod, with or without the block, mounts the runner's Kubernetes token into the uploader only
(`automountServiceAccountToken: false`, a projected 1-hour token): the model's container holds no Kubernetes credential.

## Stage composition

A root run can declare, at its submission, the stages that will follow it and what each depends on
(`services/api/stages.py`; the control API's ledger is required). Each stage is submitted later as its own run
and waits until every dependency delivered; the stages share the root's key slot and request, and the root is
terminal only when every stage is. The shape it is built for: one CPU preparation → two concurrent GPU stages →
one CPU collection.

```json
POST /v1/models/prep-class:invoke
{"input": {...}, "execution": {"graph": {"stages": {"gpu-0": [], "gpu-1": [], "collect": ["gpu-0", "gpu-1"]},
                                         "deadline_s": 86400}}}
→ 202 {"id": "op-prep-1a2b…", ...}

POST /v1/models/md-class:invoke
{"input": {...}, "execution": {"stage": {"root": "op-prep-1a2b…", "id": "gpu-0"}, "private_input_ref": "grant-0"}}
→ 202 {"id": "op-prep-1a2b…-s-gpu-0", "status": "QUEUED", "phase": "waiting_for_dependencies", ...}
```

| Rule | What it means |
|---|---|
| sealed graph | `execution.graph.stages` maps stage ids (letters, digits, `-`; never `root`) to the stages they depend on; a stage with no dependencies depends on the root. At most 256 stages (128 shards and their collector fit), no cycle, no undeclared or duplicate dependency (400). The graph is written with the root's admission and never extended: a stage id that was not declared, a second graph, or a stage on a root without a graph is refused; nothing attaches to a cancelled or closed graph (409) |
| one lineage per stage | a stage's operation id is `<root>-s-<id>`; it needs no `Idempotency-Key` (one is refused). A declared stage is submitted once: a resubmission answers with the current head of the stage's lineage (the stage, or its newest continuation) and never creates a Job |
| same owner, same key | a stage is submitted by the root's owner (403 otherwise) in the root's tenant, and admitted on the root's key as a member of the root's run (docs/API.md "Keys": the key still valid, the stage's GPU bound within the budget next to its siblings); the root's slot and request are released only when every declared stage settled or the graph was cancelled |
| lineages | inside the root's run every stage is its own lineage: the stage's `:resume`, `:export`, finalizer, fallback and recovery members follow that stage's current head (409 for a stale head), on the root's key and budget, never the root's own lineage (the root and its continuations) or a sibling's; siblings continue while one stage is resumed. A continuation of a stage carries the stage's identity and the dependency results bound into it; the gate judges a dependency by the head of its lineage, so a resumed dependency counts once it delivered |
| waiting | a submitted stage is a suspended Job without a Kueue queue label: Kueue does not see it, no node is held, no binding is claimed, no credential consumed. Its handle shows `QUEUED` with `phase: waiting_for_dependencies` and the gate's last finding as `reason` |
| the gate | the control API's reconcile loop (every 15 s, idempotent, any replica) admits a waiting stage when every dependency is `SUCCEEDED` on durable records with a `result_ref`: it claims and delivers the stage's own binding now, writes the dependencies' results into the pod (`DEPENDENCY_RESULTS`: `{"<dep>": {"operation", "producer", "result_ref", "attempt_id", "epoch"}}`, `root` for the root; `operation`/`attempt_id`/`epoch` are the delivering ones, a finalizer's or export retry's when one delivered, `producer` the lineage member the result belongs to), and adds the queue label; from then on the stage is a run like any other (Kueue, placement, attempts, billing). A binding that is not there yet keeps the stage waiting (reason `not admitted: …`) |
| failure | a `FAILED` dependency is resumable, so its dependents keep waiting (reason `gpu-1 FAILED (…: :resume it, or :cancel the root)`) until its lineage delivers, the root is cancelled, the deadline passes or the failure settlement below closes the graph; a `CANCELLED` dependency, or one `SUCCEEDED` without a `result_ref` (success without the required output is not runnable), cancels the stage once (never started: deleted, settled at zero, the reason on the root's handle); siblings that do not depend on it continue. A dependency whose export failed keeps its dependents waiting until `:export` delivers |
| root | the root's handle carries `graph`: every declared stage with `depends_on`, `operation` (the lineage head), `status` (`NOT_SUBMITTED` until submitted), `attempt_id` and `epoch` (the delivering attempt: of `delivered_by` when an export retry or finalizer delivered), `result_ref`, `reason`, `continuations`; `graph.status` is `SUCCEEDED` when every stage's head delivered, `CANCELLED` after the root's cancel, `FAILED` when the graph is over without that, else `PENDING`. A root whose own run succeeded reports `RUNNING` with `phase: stages` while the graph is pending (its `/result` is 409 until then), then the graph's outcome: the final outputs are late-bound from the stages' validated results, never from a declaration. A stage's handle carries `stage` (`root`, `id`, `depends_on`, `reason`) |
| cancel | `:cancel` on the root cancels every active stage member (waiting ones deleted and settled at zero, running ones stopped as any run) and closes the graph; `:cancel` on a stage cancels that stage, its dependents follow through the gate. A root that failed on its own is resumable like any run: its stages wait for its lineage |
| deadline | `deadline_s` (default 14 days, 60 s to 30 days) bounds how long the graph stays open: when it passes, waiting stages are cancelled, never-submitted stages are recorded in the graph's reason, the graph closes and the root's run releases its slot (running stages settle through billing as usual) |
| limits | a stage is a single-node run (no JobSet); stages are not endpoint calls; after the graph closed, no new stage attaches but the stages' lineages may still be continued (the slot is re-acquired as for any closed run); the gate reads durable attempt records, so a dependency that delivered through a retry or finalizer counts once that is on record |

#**Action required.** While the graph is open, a FAILED stage does not fail the graph (it may be resumed); when nothing
is active any more (no admitted QUEUED, RUNNING, FINALIZING or delivery-retryable stage: a dependency-gated QUEUED
member counts as parked, never as active) and a stage FAILED, the root handle reads `phase: action_required` with the
remedy in `reason`, and `graph.action_required`, `graph.blocked_on` (the failed lineage heads, their attempts, the
actions `:resume <operation>` or `:cancel <root>`, their parked dependents) and `graph.waiting` say exactly what is
blocked on whom. `status` stays `RUNNING`; nothing is failed or retried by the platform; the graph's deadline still
closes an unattended graph. A `:resume` of the failed head clears the signal (the successor is active). A root that
FAILED on its own before its stages ran is a blocking head too (`blocked_on[].stage` is `root`); every `blocked_on`
entry carries `failed_at` when the failure's time is on record.

**Failure settlement.** A graph left in `action_required` for `GRAPH_FAILURE_GRACE_S` (API environment, default 1800 s)
after its newest failure is closed by the gate the way the deadline closes it: the stages parked on the failure are
retired (never started: deleted, settled at zero, the reason on the root's handle: `<stage> FAILED (<operation>); not
resumed within 1800 s of the failure: graph closed`), the graph is cancelled with that reason (its status `FAILED`),
and the root's run releases its key slot once nothing is active. The failed head's Job, work volume, attempt records
and charges are untouched, and its lineage may still be continued afterwards (a `:resume` re-acquires the slot: see
"limits"). An admitted QUEUED, RUNNING, FINALIZING or delivery-retryable stage holds the graph open past the grace (it
may still deliver); a failure whose time is unknown waits for the deadline. Before api 0.11.111 a root that failed on
its own kept its slot and its never-launched stages active until the graph's deadline, days later.

## Delegated stages

A stage may run on a run class the caller's key is not granted, but only when four things meet, none of which the
caller can mint alone: the root's run class lists the class among its `delegates` (operator-owned: the model spec of
`POST /v1/models`, or the catalog file); the root's graph declared the stage bound to exactly that class
(`"gpu-0": {"depends_on": [], "model": "fold-gpu"}`; a declaration naming a class that does not exist is 400 at the
root); the caller is the root's owner on the root's key, and the root is active (its reservation live, the graph
open); the key has `run:submit`. The stage is then admitted on the root's key and budget as any stage. A stage
declared without a `model`, or on a model outside the root class's `delegates`, or by another owner, or after the
root closed, is refused (403) and a direct `:invoke` of the class stays refused: the declaration confers nothing by
itself. The owner reads its delegated stage's handle as any of its operations; a key of the same owner without the
grant sees it through the same rule.

What the gate changes on a waiting Job is limited to what Kubernetes allows on a never-started suspended Job
(KEP-2926): the queue label (from the waiting queue to the stage's profile, which Kueue allows while the Job is suspended; it recreates the Workload) and the waiting label and the pod template's `serverless2.nebius/dependency-results`
annotation, which the main container reads through the downward API. The results are the operations' `result_ref`s
(bucket prefixes for managed I/O, the application's references for application I/O); the stage's own access to
them is its own (its bucket credentials, its identity or its binding), as for any run.

## Retries

Without a policy a run keeps the platform's rules: a disruption (node drain, preemption) and a SIGKILL/SIGTERM exit
(137/143) are ignored and the pod is replaced, any other failure counts against `JOB_BACKOFF_LIMIT` (3). A run class
may carry its own bounded policy (`retry: {maxAttempts, onExitCodes}` in the job block, `retry: {max_attempts,
on_exit_codes}` on `POST /v1/models`), and a request may override it with `execution.retry`: at most `max_attempts`
pods of the same Job, a replacement only after an exit in `on_exit_codes` (counted against `backoffLimit =
max_attempts - 1`), any other non-zero exit fails the run at once, disruptions still ignored (unless `recovery:
on_infrastructure_loss: fail` makes them fatal), the finalizer's "delivery pending" exit codes still ahead of it. Every
attempt is a pod of the same Job: the same operation, member, reservation and GPU bound (GPUs x `timeout_s` covers all
of them), the same attempt records and epochs; nothing is admitted, reserved or recomputed again, a successful sibling
stage is untouched. `max_attempts` counts pods that ended with an exit in `on_exit_codes` only: a disruption is never an attempt, so a
run may be preempted any number of times and still have its `max_attempts` application attempts (it is not a cap on
total pods). `max_attempts` 1..10; single-node runs only (a multi-node run restarts as a whole). The retained
scientific policy is `{"max_attempts": 2, "on_exit_codes": [137, 143]}` with `graceSeconds: 60`.

## Attempts and finalization

Every attempt (a pod of an operation) is on record twice: the uploader writes `attempts/<pod>.json` into the tenant
bucket (managed I/O) and posts the same record to the API that rendered the Job (`POST /internal/attempts`, with the
pod's projected ServiceAccount token: a TokenReview against the issuing cluster names the pod, so a record can only
describe the pod it comes from). The control API keeps the posted records in the fleet database
(`services/api/attempts.py`): they survive the pod, the worker's copy of the Job and the Job's TTL, and they exist
without a bucket. Every operation reports `root_operation_id` (the first operation of a resume chain), `attempt_id`
(the newest pod), `epoch` (monotonic over the chain), `revision` (a cursor: `GET /v1/operations/<id>?since=<revision>`
answers 304 while nothing changed), `cluster`, `image_digest`, `export_state`, `result_ref` and `checkpoint_ref`.
`image_digest` is the exact **runtime** digest of the attempt's main container (`containerStatuses[main].imageID`),
never the expected image: the uploader waits for it (bounded) before its start record and republishes the start record
once when it appears later; the API also fills a start record from the pod it reads and, at `GET` time, takes the
newest attempt's live pod. `image_digest_state` says what a null means: `pending` (the attempt runs, the kubelet has not
reported it yet: transient, a reader retries the `GET`), `unknown` (the attempt ended without one), `observed`.

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

For a multi-node run (JobSet, `checkpoints: shared`) the same verb renders the finalizer as ONE CPU pod (a single-node
render of the class: no `nodes`, no GPU, no fabric, a small `/work` claim of its own) with the gang's shared checkpoint
subPath mounted where every rank had it (`/work/checkpoint`, `CHECKPOINT_DIR`), the coordinator's record as the finalized
attempt (`FINALIZE_OF` = the rank-0 pod of the newest incarnation, `FINALIZE_EPOCH`, `FINALIZE_RANK=0`,
`FINALIZE_GANG_RESTART`, `FINALIZE_NODES`); the delivery verdict is the coordinator's exit code, never a peer's teardown
exit. The GPU gang is never rendered again for a delivery. The finalizer's image is the finalized attempt's recorded
digest when on record, else the image of the class the gang was admitted on (a class with per-class `job.images` keeps
that image; the per-class map itself is dropped with the other compute-only keys).

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
  the region its Workload was admitted in (`cost` and `gpu_seconds` on the operation; `usage` says what they
  mean: `measured`, `unknown` (settled without a trustworthy measurement, e.g. a native call whose worker released
  with no metering: `gpu_seconds` and `cost` are `null`, the key's reservation is kept as unknown GPU time, never
  zero), `pending` (not settled yet)). The owner's read of a finished operation settles it at once (the pass's body
  for that operation, under a per-operation lock; the same answer carries the result), so `pending` is seen only by
  a reader that arrives while another settler holds the operation; the CronJob (every 2 minutes) remains the settler
  of record for operations nobody reads.

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
`FAILED` (exit 2 for a 4xx, the caller's error; exit 5 for another status, the endpoint's error, or an endpoint that
did not become ready within `READY_MAX_WAIT`, the call's timeout by default) and
`GET /v1/operations/{id}/result` reports `export_state: succeeded` with the `result_ref` of that delivered error
response; a re-delivery through `:export` ends with the same captured outcome, so a delivered error never becomes a
success, live or after the Job is gone. A FAILED call whose delivery failed (`export_state: failed`, a complete capture)
is delivered again by `:export` like an EXPORT_FAILED one; an unknown outcome has nothing to deliver. The endpoint's pods receive an HTTP request and nothing else: no URL,
token or credential of the binding reaches them. The call's reservation on the key (slot and request, docs/API.md
"Keys") is released by the billing pass when the Job ends; a continuation joins it as a member.

## Endpoint children

A run class may let its live attempts ask the platform for bounded asynchronous endpoint calls as **children** of
their root, without any customer credential in the worker, without storage credentials in the worker, and without an
application scheduler:

```yaml
child_endpoints: [cosmos3-nano-s2-r1]   # operator allowlist: endpoint models only
child_max_units: 16384                  # explicit bound of children per root (1..65536); required with child_endpoints
```

The render then gives `main` its own pod-bound ServiceAccount token (`S2_PARENT_TOKEN_FILE`, projected, 1 h, renewed)
and the control API's URL (`S2_API_URL`, `S2_OPERATION`, `S2_CHILD_ENDPOINTS`, `S2_CHILD_MAX_UNITS`), and FREEZES the
allow-list and the bound on the Job (annotations `child-endpoints`, `child-max-units`): a later change of the class
neither widens nor moves a running run's grant, and a run rendered without the grant has none. The attempt calls,
with that token as its bearer:

| Call | Answer |
|---|---|
| `POST /internal/operations/<run>/children` `{unit_id, model, input \| input_ref, transport?, timeout_s?, region?}` | 201 the child; 200 the existing child of that unit when the bound request is the same (a replay, a readback after a lost response, a successor attempt of the same root; renewed `transport` URLs are taken); 409 when the unit already has a child with a different request; 503 when the intent is recorded but its creation did not confirm (replay the same request: it reads the intent back and finishes it) |
| `GET /internal/operations/<run>/children/<unit_id>` | the readback (below) |

**The request.** `input` (an inline object, up to 48 KiB: it travels in the child's private binding as
`request.json`) or `input_ref: {sha256, size_bytes?}` with `transport: {input_url}` (an https URL the caller fetches
the exact request body from at transfer time; the caller verifies the bytes against the bound sha256 before its one
POST and refuses other bytes; a 512 MiB reference video goes this way, never through the Job object). The **bound
request** is `{model, input | input_ref.sha256, timeout_s, region}` and is immutable per unit (`request_sha256`);
transfer URLs are renewable and never part of it: a replay of the same bound request with a new `transport.input_url`
renews what the broker serves, nothing else. The child model's image is recorded at admission (`model_image`); an
intent is never completed against another image.

**The grant.** The caller's credential is the admission token of the root's key for exactly this child and model; at
the gateway hook it is accepted while the child holds a live reservation and names a live attempt, under the key's
scope and budget but **not** the key's model allow-list: the child model was allowed by the run class's frozen
`child_endpoints`, so a key granted the parent class alone drives its children without being granted the child
endpoints (a direct call of the key to the child endpoint stays refused).

Before the child Job is created, its inherited tenant/owner and actual endpoint
model are committed to the durable ownership record. Native execution attachment
therefore returns the same authenticated owner context as a direct endpoint call;
the worker never relies on an uncommitted row or a client-supplied owner header.

**Unit retry (explicit).** A unit whose child ended without delivering its capture is not necessarily final: a live
attempt of a **later** parent of the same root (the owner's resume, never the parent that owned the ended child)
may POST the same bound request with `retry: true`; the unit then reopens as the next incarnation
(`<root>-c-<sha>-r<n>`: a new Job, binding and ledger member for the same unit; the earlier child's Job and records
stay as history) and the readback and the owner route carry `incarnation` and `previous_child`. The retry is refused
(409, naming why) for the same parent, for a unit that delivered its capture (a unit infers once), and for a child
whose inference was posted with an unknown outcome (call.sh exit 4: the application's own recovery, never a second
inference); only a child that definitely never inferred (no attempt, or exit 2/3 with no capture) is retried.
Without `retry` the ended row replays as before. The readback carries `can_retry` (the same verdict a
retry would get, before the later-parent rule): a caller decides from it, never from a status guess.

**The capture (the native one-POST protocol, docs/API.md "Endpoint calls").** The child is rendered as an
application-I/O endpoint call: its request in the private binding (`binding-<child>`, platform-written on the API's own
cluster like a caller's `PUT /v1/bindings/{ref}`, owned by the root's owner, delivered as the `<child>-private` copy into
the child's region with its Job), `IO_MODE=application`, the readiness probe, exactly **one inference POST** per operation (the durable
`posted.json` marker: a lost response is an unknown outcome, `exit 4`, never a second POST, whatever the status;
408/429-at-the-edge/5xx/000 are a capture or an unknown outcome, not retries), the response bytes and their manifest
(`call.json`: sha256, media type from the response headers, size, HTTP status, producing attempt, timings) made
durable on the retained work volume (`<child>-work`, or the tenant's shared-scratch leaf when the model says
`call_scratch: shared`) BEFORE delivery. Delivery goes to a platform-owned destination,
`s3://<tenant bucket of the child's region>/operations/<child>/response.bin` + `call.json`, through presigned PUT URLs
the API itself mints for the child on demand: the binding's `refresh_url` is `POST /internal/operations/<child>:refresh`
on this API and its `refresh_token` the child's own operation-scoped token (the admission HMAC). The broker answers
only a request that proves that token, names the child (or one of its transfer-only export retries `<child>-x<n>`)
holding a live reservation on the root's key, and names a **live attempt** (a Pending/Running pod) of that operation;
a stale pod, a settled operation or another model's token gets 401/403/409 and delivers nothing. The input phase
returns the parent's current `input_url`; the output phase returns one-hour PUT URLs for the two objects (any content
type). A failed delivery leaves the capture on the volume: the owner's `POST /v1/operations/<child>:export` renders the
transfer-only retry (TRANSFER_ONLY: nothing is inferred again), admitted as a `child` member after the child's own
head in the parent run's ledger (no second request), served by the same broker with the same binding.

**Application delivery (external storage).** A tenant whose storage the platform does not own (`storage = external`:
no `tenant-storage` Secret) declares `child_delivery: application` on the class. At the PARENT's admission the API
checks that first (`platform` on such a tenant: 400 before anything is admitted, naming the child endpoint's region and
the fix; `application`: nothing is probed or created). Each child request then carries the application's delivery
and reference: `transport: {refresh_url, refresh_token}` (the parent's broker: the SAME call.sh refresh contract,
`POST refresh_url {operation_id, attempt_id, phase}` with `Bearer refresh_token` → `{input_url?, response_url,
call_url}`; the child's caller sends its own projected identity token as `X-Workload-Identity` when the parent's
execution had one) or static `transport: {response_url, call_url, input_url?}`, and **`result_ref`** (the application's
immutable reference for the captured object, ≤ 512 chars, part of the bound request). The child's caller inherits the
parent's FROZEN execution (`identity`, `profile` from the parent Job's annotation, never the current tenant config), the
binding gets the application's keys (no platform broker, no platform destination), and no customer key or storage
credential enters the child. The capture becomes durable through the platform's own uploader: call.sh's RESULT.json
carries the manifest it just made durable (`capture: {sha256, content_type, size_bytes, http_status, capture_attempt_id
= the ORIGINAL producer, posted_at, completed_at}`), the uploader posts it with the attempt record (`attempts.capture`,
migration 25: allowlisted bounded fields, immutable per operation lineage: a transfer-only retry re-posts the same
manifest, a contradicting one is 409). The readback then takes `capture` from that record, `result_ref` is the
application's, there is no `download_url` (the live parent fetches the exact object through its own broker), and only a
delivered capture makes `outcome: succeeded`. The platform's `:refresh` broker answers 409 for such a child. The
capability block `execution.endpoint_children` (`GET /v1/fleet`) advertises `delivery: [platform, application]`.

**The readback** (authoritative past the Job's TTL: the durable row, the destination, the attempt records; nothing is
guessed from a prefix). The owner reads the same readback with its key: `GET /v1/operations/<run>/children/<unit_id>`
(scope `read:operations`; `<run>` the root or any member of the run, the unit is keyed by the durable root; another
owner's run and an unknown unit are 404) — identical, minus the attempt-scoped `download_url` (the owner reads the
capture object through `GET /v1/operations/<child>`, its own operation, which also carries `child: {root, unit,
parent}`). Fields: `child`, `state` (`pending` | `active` | `ended`), `status` (the Job's while it exists, else the
record's, else `PENDING`/`ENDED`/`UNKNOWN`), `request`, `request_sha256`, `model_image`, `timeout_s`, `region`,
`result_ref` (the stable `s3://…/response.bin`), `capture` (None until delivered; else `sha256`, `content_type`,
`size_bytes`, `http_status`, `attempt_id`, `timings`, `delivered: true`, and `download_url`, a one-hour presigned GET
of that one object for the live attempt of the root: attempt-scoped, no tenant credentials), `attempt` (the latest
record: pod, status, exit code, export state, image digest), `outcome`: `succeeded` (a delivered capture with a 2xx)
| `failed` (a delivered endpoint error, or ended without a capture: refused, cancelled, lost) | `undelivered`
(the producing attempt captured, exit 0/2/5, and failed only its delivery: `exportable: true`, the owner's `:export`)
| `unknown` (exit 4: posted, no response captured; exit 3: nothing captured; not exportable, never inferred again
under this unit) | `pending`. A parent marks a unit done only on `succeeded`, and resolves a lost answer by reading the
SAME unit back, never by a new unit.

**Fail-closed rules, in order:** the token must be a bound pod token of a tenant namespace (TokenReview on the
parent's cluster: namespace, pod name, exact pod UID); the pod must be a live attempt of the named run (among its pods
by UID, Running/Pending; the run not finished, cancelled or superseded; its root's reservation open); the model must
be in the frozen allow-list and an endpoint; the key's endpoint-call policy holds (a finite GPU-second budget: 501, as
for any endpoint call); the unit's bound request must match an existing child or no child may exist for the unit; at
most `child_max_units` children per root (ended ones counted: a bound on executions); **one open child per root**
(`pending` or `active`: 409 naming it until its terminal settlement); the parent's remaining deadline must leave at
least 60 s, and the child's timeout is capped by it. Identity is deterministic: `<root>-c-<sha256(unit_id)[:10]>`;
children of a root are keyed by the root, so an allowed successor attempt reads its predecessor's child back before any
new submit; no replay ever creates a second execution for a unit.

**Lifecycle (a durable intent, finished deterministically).** The row is written FIRST (`pending`, with the namespace,
the resolved region, the effective timeout, the bound request and the transport); completion is the ledger member
(idempotent), the binding, then the caller Job (found, or created; AlreadyExists is the same Job), then `active`. An
API process that dies in between, or a create whose answer was lost (the Job may exist), leaves the intent pending
with its identity: the next replay of the same request and the reconciler's sweep (every 15 s) finish it from the row
alone; nothing is rolled back unless the Job's absence is confirmed and the refusal definite (the ledger's 4xx, the API
server's definite 4xx: synchronously nothing stays recorded; from the sweep the row ends with `reason: refused: …`).
`active` lasts until the child's **terminal settlement**: its run member settled by billing (the key release joined
with the worker's native execution lease), or, for a never-started child deleted by its cancel, at that deletion. A
cancel (the root's cancel route at once; the sweep's cascade for a parent that is cancelled, finished without a
successor, or gone) is issued to the child's Job and to its worker (the lease's cancel request) and recorded
(`cancel_requested_at`, `reason`), and the row stays open until the settlement: no next child starts while the old
caller or worker may still be draining. A finished parent WITH a successor (a resume) is judged by the successor. A
child whose Job is gone with nothing to settle is ended as `lost`.

Accounting, on the root's ledger (`keys.prepare_run_member` kind `child`): the root's concurrency slot is shared (no
second slot); each child counts **one request** against the key's request budget and rate window (a transfer-only
retry none); the key's endpoint-call policy applies unchanged (a key with a finite GPU-second budget gets the same 501
as any endpoint call: shared endpoint GPU time is not metered per call, so a child reserves no GPU bound); the child
settles as a run member; the root closes only when nothing is active, children included. The child's caller is the
platform's caller pod (as every endpoint call's), carrying ONLY an operation-scoped credential: `Bearer
s2-admitted.<token>`, the admission token (HMAC of the root's key hash, the child model and the child operation),
which the gateway hook accepts by rebuilding the principal from the key record by hash and only while the child holds
a live reservation (it dies with cancel and settlement, and is useless for any other operation or model). No customer
or admin key is stored or forwarded, the parent worker never sees a bearer or a storage credential; the call goes
through the model's gateway (never LiteLLM's pass-through). A run class without `child_endpoints` renders exactly as
before.

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

**Durable endpoint calls on the tenant's shared scratch (`call_scratch: shared`, endpoint models, opt-in).** An
endpoint call with application I/O keeps its capture (request, response, manifest) on a retained volume so a
replacement attempt delivers it without a second inference POST. By default that volume is a per-call network claim
(`<op>-work`), whose provisioning and attach cost ~50 s before the caller's first byte on Nebius (measured: scheduled
09:06:05, attached 09:06:58). With `call_scratch: shared` on the endpoint model the caller's `/work` is instead the
tenant's shared-scratch claim (`scratch-shared`, charts/tenant: one static RWX volume per tenant namespace rooted at
the tenant's own directory on the region's shared filesystem) at the API-derived leaf `calls/<operation>`, a kubelet
subPath: main and the uploader mount the leaf only, never the tenant's directory; no claim is created or attached, the
filesystem is on the node already. The same leaf is what a CPU delivery retry and `:export` mount (`scratch-leaf`
annotation; no PVC lookup), so the one-POST rule and the retained capture hold unchanged. The leaf is provisioned by a
trusted init container (`scratch.sh provision`: the runner image as root with exactly CHOWN/FOWNER/DAC_OVERRIDE,
PSA-baseline allowed; the kubelet creates a missing subPath directory root-owned 0755, the init makes that one
directory `10001:10001 2770` and exits; idempotent). Publication is durable, not assumed: `call.sh` fsyncs the
one-POST marker before the POST, the capture before its manifest, the manifest and RESULT.json after writing (a
stream that reached a shared filesystem's server is not on disk yet). Cleanup is the uploader's, on exactly the owned
leaf, only after the delivery is acknowledged by the API and the capture proven (`scratch.sh proof`: manifest complete,
`response.bin` hashes to the manifest's sha256, RESULT.json says export succeeded): `scratch.sh cleanup` removes the
leaf's entries, every one owned by the runner, symlinks as links and never followed, nothing outside the mount; the
empty leaf directory stays (the pod cannot unlink its own mount point) and is the recorded GC gap, never scanned or
reaped by age. A failed or unknown delivery keeps everything, as a kept volume does (`:export` retries). The shared
claim is never deleted. The pod lands only on nodes that mount the shared filesystem (`shared-fs` affinity). Where the
region has no shared filesystem, the cluster is unknown to the fit table, or the tenant namespace has no claim, the
caller falls back to the per-call claim and records why (`scratch: network`, `scratch-fallback`). The judgement is
the caller's own placement, not the region's: a GPU-free pool (system or CPU; a caller never occupies a GPU node) that
mounts the filesystem must hold the caller's requests on one node and the claim must be Bound, else the fallback; the
shared-fs requirement joins the pod's required affinity by conjunction with anything an execution profile already
requires. Runs and multi-node runs are unchanged (their scratch stays `network` / `local-nvme`).
