# Checkpointing to Nebius Object Storage

## Overview

ML training checkpointing to Nebius Object Storage on a Soperator cluster:
Terraform provisioning that delivers a bucket and credentials into the jail, a
reference training example built on FSDP2 + PyTorch Distributed Checkpoint
(sharded async writes, atomic commit, auto-resume, retention), a scripted
end-to-end verification ([`verify.sh`](./verify.sh)), and the infrastructure
contract - which termination signals a job actually receives and how nodes come
back after preemption.

The document serves two audiences:

- **Architects and platform operators** - [the first part](#for-architects-provisioning-and-lifecycle)
  covers what gets created, how credentials are delivered, verification,
  disaster recovery, and data-safe teardown.
- **ML researchers** - [the second part](#for-researchers-the-workload-contract)
  covers what your job experiences on this infrastructure: the environment
  contract, the signals that reach your ranks, safe signal handling, the
  reference example, and a sizing checklist.

## For architects: provisioning and lifecycle

### Prerequisites

- **Terraform workstation:** use Linux, macOS, or WSL2 on Windows. In addition to the
  [normal Soperator prerequisites](../../README.md), checkpoint provisioning requires
  the Nebius CLI, `kubectl`, `jq`, and `base64`. The safe destroy guard uses the AWS CLI
  as an Object Storage protocol client; it must be installed before destroying an
  installation that owns a checkpoint bucket. Follow the
  [Nebius AWS CLI setup guide](https://docs.nebius.com/object-storage/interfaces/aws-cli),
  and run Terraform from a shell whose compatibility credentials can list, write, and
  delete objects and list, create, and abort multipart uploads in that exact bucket.
  Those teardown credentials must remain valid independently of the checkpoint service
  account managed by this installation, because Terraform revokes its job credentials
  before the post-Slurm bucket cleanup. Native Windows PowerShell is not supported
  because the Terraform provisioners are Bash scripts.
- **IAM permission:** the identity running Terraform must be allowed to administer IAM
  groups in the checkpoint bucket's project. When `existing.project_id` points to a
  different project, it instead needs tenant-level IAM group administration so the
  workload service account can be granted access across projects.
- **Login node:** Linux on x86_64 or arm64, Python 3.10 or newer with `venv`, `curl`,
  `tar`, and `sha256sum`, plus outbound access to the Python package index and GitHub.
  `bootstrap.sh` checks these tools, installs the exact versions in
  [`requirements.txt`](./requirements.txt), and installs a checksum-verified `s5cmd`
  binary into the shared jail; it does not require a system-wide package install.
- The cluster is deployed with `checkpoint_storage_enabled = true` (see
  `soperator/installations/example/terraform.tfvars`). This creates the checkpoints
  bucket, a service account with an access key, the `jail-checkpoints` secret, and
  renders `/etc/nebius-checkpoints.env` into the jail. Jobs use
  `NEBIUS_OBJECT_STORAGE_ENDPOINT`, `NEBIUS_OBJECT_STORAGE_REGION`, and
  `NEBIUS_CHECKPOINT_BUCKET`; the file also exports the AWS-named credential
  variables required by S3-protocol-compatible SDKs.
- By default, `/etc/nebius-checkpoints.env` is owned by `root:root` and has mode `600`.
  If jobs run as non-root users, set `checkpoint_storage_env_file_owner` to their
  numeric `uid:gid`, or use `0:<shared gid>` with
  `checkpoint_storage_env_file_mode = "640"` and put submitters in that group.
- The service account belongs to a dedicated IAM group whose access permit grants
  `storage.object-editor` on the checkpoint bucket only.
- To rotate the access key, explicitly replace it from the example installation
  directory; an ordinary `terraform apply` does not rotate credentials:

  ```shell
  terraform apply -replace='module.checkpoints_access[0].nebius_iam_v2_access_key.checkpoints_access_key'
  ```

  The replacement also recreates the Kubernetes secret and jail environment file.

### Deploy and prepare

Enable the feature in the installation's `terraform.tfvars`. This can be part of the
initial deployment; Terraform waits for the Slurm jail before rendering credentials:

```hcl
checkpoint_storage_enabled = true
```

```shell
terraform apply
```

Creating the bucket, least-privilege IAM resources, access key, Kubernetes Secret, and
jail renderer usually takes **1–3 minutes** after Slurm is ready. A complete Soperator
cluster deployment is separate and takes longer.

Deliver the verification and example from this repository to the login node:

```shell
cd soperator/test
./deliver.sh -t checkpointing -u <login-user> -k <private-key> -a <login-address>
```

Then prepare the shared runtime from the login node:

```shell
cd /opt/slurm-test/checkpointing
./bootstrap.sh
```

Allow roughly **5–15 minutes** on the first run for Python packages, depending on the
network and package cache. Re-running the bootstrap is safe and normally much faster.

### Verify the installation

One command gives a pass/fail answer on whether checkpointing actually works
end-to-end on this cluster:

```shell
./verify.sh
```

It submits the reference job (overridden to one GPU on each of two nodes, so
cross-node sharding is exercised without a full allocation) and checks four
properties: a checkpoint is committed to Object Storage; after a hard `SIGKILL`
(no warning, no graceful save) the `latest` marker still points at a complete
checkpoint; a new submission with the same prefix resumes from that step and
commits further progress; and `SIGUSR1` commits the current step before exit.
The kill phase *attempts* to land the `SIGKILL` while the next
upload is in flight (it waits for new step objects to appear and reports what it
observed); if no upload starts within one save cadence it kills anyway, so
hard-kill recovery is always verified even when interrupted-upload atomicity is
not exercised on that run. Each phase prints `PASS` or `FAIL`, the script exits
non-zero on any failure, and it cleans up its own checkpoint prefix - including
aborting any incomplete multipart upload the kill left behind. Expect
**5–10 minutes** of runtime once the two nodes are allocated; queue time is
extra.

Deliberately out of the script's scope: automatic Slurm requeue, real instance
preemption, and node recovery. Those depend on scheduler events and operator
action - [Interruptions](#interruptions-what-the-job-receives-and-when)
describes how to exercise them manually (`scontrol requeue`, or stop a worker
instance and watch the job requeue and resume).

### Resuming on a different cluster (disaster recovery)

Because checkpoints live in Nebius Object Storage, they outlive the cluster that
wrote them.
To continue training on a new cluster - after a teardown, a migration, or a region
capacity move - point the new installation at the same bucket and pass the prefix:

```hcl
# terraform.tfvars of the new cluster
checkpoint_storage_enabled = true
checkpoint_storage_bucket = {
  existing = {
    name = "<old cluster name>-checkpoints"
    # Required when the old bucket belongs to another project of the same tenant.
    # Cross-tenant bucket reuse is not supported.
    project_id = "project-..."
    # Required when the bucket is in a different region from the new cluster.
    endpoint = "https://storage.<bucket-region>.nebius.cloud:443"
  }
}
```

```shell
TRAIN_ARGS="--prefix <job name>-<job id>" sbatch checkpoint_train.sbatch
```

The job reads the marker and resumes from the last committed step. Existing buckets
are never cleaned up or deleted by the new installation's destroy. PyTorch DCP can
reshard a checkpoint across a different rank count; the model and optimizer
definitions must still remain checkpoint-compatible.

### Safe Terraform teardown

An ordinary `terraform destroy` never deletes checkpoint data. Destroying an
installation whose created checkpoint bucket is not empty stops early - before
anything is revoked or torn down - with a warning that prints these three options:

1. **Keep the checkpoints, delete everything else.** Detach the bucket and guard from
   state, then destroy (record the bucket name first; a replacement installation can
   reuse it as an `existing` bucket):

   ```shell
   terraform state rm \
     'module.checkpoints_store[0].nebius_storage_v1_bucket.checkpoints_bucket[0]' \
     'module.checkpoints_store[0].terraform_data.cleanup_bucket[0]' \
     'terraform_data.checkpoint_storage_destroy_guard[0]'
   terraform destroy
   ```

2. **Delete the data yourself, then destroy.** Plain `aws s3 rm` is not enough:
   interrupted checkpoint saves leave *incomplete multipart uploads* that ordinary
   object listing does not show, and the guard checks those too. The helper script
   deletes both inventories and verifies the bucket is stably empty (the guard's
   warning prints this command with the paths and names filled in):

   ```shell
   bash ../../modules/checkpoints_store/scripts/bucket_teardown.sh \
     empty <bucket-name> <endpoint-url>
   terraform destroy
   ```

3. **Force Terraform to delete the data and the bucket:**

   ```shell
   CHECKPOINTS_FORCE_CLEANUP=<bucket-name> terraform destroy
   ```

The force switch is an environment variable read at destroy execution and scoped to the
exact bucket name: it lives outside Terraform state and saved plans, so a stale or
speculative plan can never authorize deletion. With it set, the destroy guard first
probes object write/delete and multipart create/abort permissions - before checkpoint
access, Slurm, or storage resources are removed - then cleanup deletes completed
objects and aborts incomplete uploads. All paths need the `aws` CLI compatibility
client. Buckets supplied as `existing` are never emptied or deleted by this installation.

On a standard installation, the Object Storage compatibility credentials come from the
example's `.envrc`, and the access key it creates **expires after one day**. Run
`source .envrc` before `terraform destroy` or the helper command above so the guard and
cleanup act with fresh credentials instead of failing closed on an expired key.

For a module-created bucket, Terraform also installs a seven-day lifecycle rule that
automatically aborts incomplete multipart uploads; it does not expire completed
checkpoints. When reusing an existing bucket, configure the equivalent rule on that
bucket so interrupted large uploads cannot accumulate indefinitely.

## For researchers: the workload contract

What the infrastructure guarantees your job, and what it expects back. The
environment contract: every node sees `/etc/nebius-checkpoints.env` with the
bucket, endpoint, region, and credentials (see
[Prerequisites](#prerequisites) for names and permissions). The expectations:
checkpoint through the commit protocol below, handle the signals in the
[interruption table](#interruptions-what-the-job-receives-and-when), and submit
with `--requeue` so recovery is unattended.

### The reference example: async sharded checkpoints (`checkpoint_train.sbatch`)

```shell
sbatch checkpoint_train.sbatch
```

A 2-node training job (toy transformer; the model is a prop, the checkpointing is
the point) that demonstrates the pattern we recommend for real workloads. It is
built on FSDP2 (`fully_shard`) and PyTorch Distributed Checkpoint - the same
primitives TorchTitan's checkpointing uses, so the pattern transfers directly to
workloads based on it. The sample has a 30-minute allocation cap and writes its
first periodic checkpoint after roughly two minutes; use `sbatch --time=...` or
`TRAIN_ARGS="--steps ..."` to select a different bound.

- **Sharded, direct Nebius Object Storage writes**: model and optimizer state are
  sharded across ranks by FSDP2, so with DCP every rank uploads only its own
  shard - no shared-FS staging, no rank-0 gather.
- **Async saves**: `dcp.async_save` blocks training only for GPU->host staging
  (typically well under a second); the upload runs in the background. The job log
  prints the measured blocking time for every save. A failed background upload
  fails the job on the next save - it is never silently ignored.
- **Atomic commit via the `latest` marker**: a checkpoint only "exists" once the
  small `latest` object points at it, and the marker is only moved after the upload
  fully completes. A job killed mid-upload leaves no usable trace of the partial
  checkpoint, and resume never sees it. Nebius Object Storage has no atomic rename - never
  rely on listing to find the newest checkpoint. Failure to *read* the marker is an
  error, not a fresh start: a transient Nebius Object Storage read failure must not
  silently restart training from scratch.
- **Checkpoint prefixes**: checkpoints land under `<job name>-<job id>/` in the
  configured Nebius Object Storage bucket. Commands and connector logs render this as
  `s3://<bucket>/<job name>-<job id>/` because `s3://` is the URI syntax expected by
  the compatible tools; the endpoint remains Nebius Object Storage.
  The job ID keeps independent submissions isolated from each other while staying
  stable across requeues of the same job (so unattended recovery works). To resume
  across separate submissions - or from another cluster - pass an explicit
  `--prefix` via `TRAIN_ARGS`.
  A prefix is a single-writer namespace: never run two active jobs against the same
  explicit prefix, because both would update `latest` and apply retention independently.
- **Auto-resume on node failure and preemption**: with `#SBATCH --requeue`, Slurm
  requeues the job when a node dies or is preempted; on restart it reads the marker
  and continues from the latest complete checkpoint - unattended at the *job* level.
  See [Interruptions](#interruptions-what-the-job-receives-and-when) for exactly
  which signals arrive when, and what the platform side of the contract is.

Watch it recover from a mid-run kill:

```shell
scancel --signal=USR1 <jobid>   # graceful: final checkpoint written before exit
scontrol requeue <jobid>        # or kill a worker node and let --requeue do it
```

Inspect the checkpoints:

```shell
source /etc/nebius-checkpoints.env
./bin/s5cmd --endpoint-url "$NEBIUS_OBJECT_STORAGE_ENDPOINT" \
  ls "s3://$NEBIUS_CHECKPOINT_BUCKET/*"
```

### Interruptions: what the job receives and when

The part of checkpointing that is usually undocumented is not the save call - it is
what actually reaches the training processes when infrastructure takes the job away.
The reference script traps `SIGUSR1` and `SIGTERM`, commits the current step,
and exits; `SIGKILL` and preemption give no opportunity to react, and
recovering from them cleanly is exactly what the commit protocol is for.

| Event | What the ranks receive | Warning time | Requeued with `--requeue`? |
|---|---|---|---|
| Node preemption or hardware failure | Assume nothing: the node disappears mid-collective and surviving ranks stall on NCCL/Gloo. Nebius delivers a VM-level `SIGTERM` about 60 s before stopping a [preemptible VM](https://docs.nebius.com/compute/virtual-machines/preemptible), but that notice reaches the VM's init system, not your ranks - do not count on it unless the cluster explicitly propagates and validates it | none you can rely on | Yes, after Slurm declares the node down (**262 s** from a controlled instance stop in the reference validation) |
| Job time limit | `SIGCONT` + `SIGUSR1` (from `--signal=USR1@120`), then `SIGCONT` + `SIGTERM` at the limit if the job is still running; `SIGKILL` follows only if it remains alive after the site [`KillWait`](https://slurm.schedmd.com/slurm.conf.html#OPT_KillWait) | requested **120 s**; Slurm may send it up to 60 s earlier | No, unless the site sets [`RequeueExit`](https://slurm.schedmd.com/slurm.conf.html#OPT_RequeueExit) - do not rely on it |
| `scancel <jobid>` | `SIGCONT` + `SIGTERM`; `SIGKILL` follows only if the job is still alive after the site `KillWait` (**180 s** in the reference deployment) | seconds | **No** - `scancel` never requeues; resubmit with the same `--prefix` instead |
| `scancel --signal=USR1 <jobid>` | `SIGUSR1` only; the job checkpoints and exits on its own schedule | job-controlled | No from `--requeue` alone; an exit-code-based site `RequeueExit` policy can still apply |
| `scontrol requeue <jobid>` | `SIGCONT` + `SIGTERM`, then the same job ID returns to the queue and its `Restarts` count increments | seconds, plus site scheduling delay before restart | Yes |

Two details in the sbatch header exist because of this table. `--signal=USR1@120`
must **not** use the `B:` prefix - `B:` delivers the scheduled warning's
`SIGCONT` + `SIGUSR1` only to the batch shell, and the training ranks never see
it. And `--open-mode=append` keeps the log
across requeues instead of truncating it, so the pre-interruption history survives.
Slurm evaluates time-based events periodically, so its
[`--signal`](https://slurm.schedmd.com/sbatch.html#OPT_signal) documentation allows
the warning to arrive up to 60 seconds earlier than requested. Treat `@120` as a
120-second minimum target, not an exact timestamp, and size it for the worst-case
checkpoint duration. `SIGCONT` wakes the target before the scheduled warning and
before termination on the reference deployment; it needs no checkpoint handler.
Handle the accompanying `SIGUSR1` or `SIGTERM` (see Slurm's
[job termination sequence](https://slurm.schedmd.com/job_launch.html#job_termination)).

**Handling the warning safely** - the pattern in `train_fsdp.py` to copy:

- The signal handler only **records intent** (sets a flag). It runs asynchronously,
  at an unpredictable point in the training step, possibly inside a collective -
  doing real work there is unsafe.
- Ranks never act on a signal unilaterally. Each step boundary, they combine their
  flags through a collective on a **dedicated control process group** and stop
  together at that safe point. A rank that leaves the training loop on its own
  deadlocks the others; sharing the control collective with the async checkpoint's
  process group can interleave operations across ranks and deadlock too.
- The final save first **drains any in-flight async upload**. If that upload
  already covers the current step, it is the final checkpoint; otherwise the
  job writes the current step synchronously. Budget the warning window to cover
  the worst-case training step, that drain, and the final save; the example's
  120 s covers it comfortably at this scale - measure yours from the log.

#### The platform side of the contract

A preemption stops the underlying instance; assume the job gets no usable in-band
notice. From there, two independent clocks run (times as measured on the reference
deployment):

- **The job**: requeued 262 seconds after a controlled node stop in the reference
  validation, it waits in the queue until enough nodes are up again, then resumes
  from the marker. This path needs no human.
- **The nodes**: the managed Kubernetes node group did **not** restart the stopped
  preemptible instance. Someone - the cluster operator or their automation - must
  start it again. In the reference validation the same instance made the job
  runnable **185 seconds** after that start request (Kubernetes reported the node
  ready after 87 seconds); startup and capacity delays vary, so budget more headroom
  in production. If an instance shows `RUNNING` in the cloud console but never
  rejoins Slurm, delete it rather than repeatedly restarting it so the node group can
  recreate it cleanly.

So the checkpointing loop is unattended at the job level, while instance recovery
is the operator's side of the contract. Budget the node-return time, not the
requeue time, as the real cost of a preemption.

### Using the commit protocol from any framework

Frameworks without a direct Nebius Object Storage integration can reuse the same
commit protocol from a shell wrapper or callback: save to fast local storage (the
node-local `/scratch` disk if the cluster has one), upload the *completed* checkpoint
directory, and only then overwrite the `latest` marker:

```shell
s5cmd --endpoint-url "$NEBIUS_OBJECT_STORAGE_ENDPOINT" \
  cp "$LOCAL_DIR/" "s3://$NEBIUS_CHECKPOINT_BUCKET/$PREFIX/step-$STEP/"
echo "$STEP" | s5cmd --endpoint-url "$NEBIUS_OBJECT_STORAGE_ENDPOINT" \
  pipe "s3://$NEBIUS_CHECKPOINT_BUCKET/$PREFIX/latest"
```

The invariants to preserve: resume only from the step the marker names (never from a
listing); treat a marker read failure as an error, not as a fresh start; clear a step
directory before re-uploading into it after a failed attempt. A two-tier variant of
this pattern - the framework checkpoints natively to the shared jail while an external
watcher mirrors only completed checkpoints to Object Storage and restores the committed
copy on startup - suits frameworks whose checkpoint formats are directory-based and
tracker-driven.

### Sizing and cadence checklist

- **Checkpoint size**: roughly bytes-per-parameter times parameter count. FP32
  weights plus two FP32 AdamW moments (this example) is ~**12 bytes/param**;
  mixed precision with BF16 weights, FP32 master weights, and both moments is
  ~**14 bytes/param**. Measure the real output - formats add metadata and padding.
- **Bucket budget**: `keep_last x checkpoint size`, plus any milestone checkpoints
  retained separately. Retention is `--keep-last N` (default 3), applied after
  each commit.
- **What a save costs**: with async saves, training stalls only for GPU->host
  staging - the log prints this blocking time per save. Each rank uploads its own
  shard in parallel, so aggregate upload bandwidth grows with the cluster.
- **Interval**: Young/Daly, `sqrt(2 * C * M)` - `C` the blocking time, `M` the mean
  time between failures (on preemptible nodes: the observed preemption rate).
  Example: C = 1 s and one failure per 3 hours gives ~2.5 minutes.
- **Interval floor**: the background upload must finish (host memory, disk, and
  Object Storage throughput) before the interval elapses; the log's upload
  completion times show the margin.

## Troubleshooting

- **`/etc/nebius-checkpoints.env` is missing:** confirm
  `checkpoint_storage_enabled = true`, run `terraform apply` after Slurm is deployed,
  then inspect `kubectl -n <slurm-namespace> logs job/jail-checkpoints-env`. The
  provisioner fails the apply if the renderer cannot complete.
- **Bootstrap reports an Object Storage permission error:** verify that the checkpoint
  service account is in its dedicated group and that the group's
  `storage.object-editor` access permit targets the expected bucket. The bootstrap probe
  deliberately tests write, read, and delete.
- **A job refuses to start fresh after a marker read error:** this is intentional.
  Authentication, endpoint, DNS, and transport failures must not be interpreted as an
  absent checkpoint. Correct the connection and resubmit with the same prefix.
- **`terraform destroy` stops on checkpoint data:** choose one of the explicit retain,
  empty, or confirmed-delete paths in [Safe Terraform teardown](#safe-terraform-teardown).

## Version notes

- `s3torchconnector[dcp]`, `S3StorageReader`, `S3StorageWriter`, and
  `S3ClientConfig` are upstream package/API names. The sample configures them
  against the Nebius Object Storage endpoint. Install the `[dcp]` extra because
  the base package is missing `tenacity`, which its DCP integration needs.
- `boto3`, `s5cmd`, and the connector use the S3-compatible protocol and therefore
  retain upstream `AWS_*` credential names and `s3://` URI syntax at that technical
  boundary. The customer-facing endpoint, region, and bucket variables are the
  `NEBIUS_*` values in `/etc/nebius-checkpoints.env`.
- The connector's DCP writer classes take the endpoint from the compatibility
  `AWS_ENDPOINT_URL` environment variable plus
  `S3ClientConfig(force_path_style=True)`; they have no endpoint argument of their own.
- `dcp.async_save` requires a CPU backend in the process group: initialize with
  `cpu:gloo,cuda:nccl`.
