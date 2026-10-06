# Filesystem CSI Validation

## Document Metadata

- Created By: Aaron Fagan
- Created On: 2026-03-17
- Version: 0.1.0

## Purpose

This README explains what each helper file in this folder does, why it exists,
and the order in which to use the validation workflow.

## Reference Docs

- https://docs.nebius.com/kubernetes/storage/filesystem-over-csi

This folder contains the commands and manifests used to validate the
Terraform-managed Nebius Shared Filesystem over CSI workflow for this
`k8s-training` deployment.

These files are not run automatically.

Important notes:

- This repo mounts the Nebius Shared Filesystem on nodes at `/mnt/data`.
- The Terraform already attaches the shared filesystem to the node groups and
  mounts it with cloud-init.
- Terraform now installs the CSI driver and patches the default `StorageClass`
  when a shared filesystem is present.
- The remaining purpose of this folder is host-mount verification, pod-level
  validation, and cleanup of temporary validation resources.
- The scripts default to the current `kubectl` namespace. Set
  `TEST_NAMESPACE=<namespace>` if you want to keep the validation resources
  somewhere explicit.
- Steps `02` and `03` intentionally omit `storageClassName` from their test
  PVCs so they can verify that the cluster default `StorageClass` is applied
  automatically.
- Step `01` records the temporary node-debugger pod names in `.state/` so step
  `04` can clean up only the debugger pods created by this workflow.
- Step `01` now defaults to a quick single-node shared filesystem spot check.
  Set
  `VERIFY_ALL_NODES=true` to validate every node, or `TARGET_NODE=<node-name>`
  to validate one specific node.
- The smoke and RWX manifests now use workflow-specific resource names to make
  reruns and cleanup easier to understand.

Suggested order:

1. Run `./01-verify-node-filesystem-mounts.sh`
2. Run `./02-run-csi-smoke-test.sh`
3. Run `./03-run-csi-rwx-cross-node-test.sh`
4. Run `./05-run-checkpoint-restore-test.sh` to validate model-checkpoint
   write/restore on the writer node and across every other GPU node (verifies
   data integrity by checksum; cleans up its own resources on exit)
5. (Optional, performance) Run `./06-run-fio-performance-test.sh` to measure
   per-host storage performance with FIO (sequential + random read/write). This
   is a **performance** check, separate from the functional checks above — see
   the FIO section below. It writes a large dataset, so read its warnings first.
6. Run `./04-cleanup-csi-test-resources.sh` to remove any temporary test
   resources left from steps 01–03 when you are done testing

> **Functional vs performance:** steps 01–05 validate that storage *works
> correctly* (mounts, binds, RWX, checksum integrity). Step 06 (FIO) measures how
> *fast* it is. They complement each other — a passing FIO run is not a substitute
> for the correctness checks, and vice versa. (Parallel/aggregate throughput tools
> such as IOR and MDTEST live outside this suite in the separate `cloudmeter` repo,
> not in `nebius-solutions-library`.)

Prerequisites:

- `kubectl` points to the target cluster.
- The cluster nodes are already provisioned by this Terraform stack.
- Terraform apply has already completed successfully, including any automatic
  shared filesystem CSI installation and default `StorageClass` patching.

## Shared Defaults

- `TEST_NAMESPACE` defaults to the current `kubectl` namespace, then falls back
  to `default`.
- `MOUNT_POINT` defaults to the mount path discovered from the Terraform
  cloud-init template, then falls back to `/mnt/data`.
- `FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME` defaults to
  `csi-mounted-fs-path-sc`.
- `VERIFY_ALL_NODES` defaults to `false` for step `01`.
- `TARGET_NODE` can be used in step `01` to test one explicit node.
- The validation resources use the following fixed names:
  - `filesystem-csi-smoke-pvc`
  - `filesystem-csi-smoke-pod`
  - `filesystem-csi-rwx-pvc`
  - `filesystem-csi-rwx-writer`
  - `filesystem-csi-rwx-reader`

## Performance validation — FIO (step 06)

`06-run-fio-performance-test.sh` measures **per-host** storage performance with
**FIO** (Flexible I/O Tester) against a Kubernetes-mounted filesystem. It is a
performance tool, separate from the functional checks (01–05) and from IOR /
MDTEST — it complements them, it does not replace them.

### The four tests

| # | Pattern | Block size | Primary metric | Why |
|---|---------|-----------|----------------|-----|
| 1 | Sequential read (`rw=read`) | 1 MiB | **throughput** (GB/s) | Large, contiguous reads — streaming a dataset/checkpoint back. Big blocks maximise bytes/sec. |
| 2 | Sequential write (`rw=write`) | 1 MiB | **throughput** (GB/s) | Large, contiguous writes — writing a checkpoint. |
| 3 | Random read (`rw=randread`) | 4 KiB | **IOPS** | Small, scattered reads — metadata/shards. Small blocks expose per-operation cost, so IOPS (ops/sec) is what matters, not bytes/sec. |
| 4 | Random write (`rw=randwrite`) | 4 KiB | **IOPS** | Small, scattered writes. |

Each test also reports the other dimensions (throughput, IOPS, and latency).

- **Throughput** = bytes moved per second (GB/s decimal, GiB/s binary). Best for
  **sequential** work. The report shows both GB/s (`÷1e9`) and GiB/s (`÷2^30`)
  and never silently calls GiB/s "GB/s".
- **IOPS** = I/O operations per second. Best for **random** work.
- **Latency** = how long each operation took (avg, p50, p95, p99). High tail
  latency (p99) matters even when average looks fine.
- **1 MiB** blocks for sequential throughput, **4 KiB** blocks for random IOPS —
  these are the standard block sizes that isolate each property.

### Why the methodology is the way it is

- **Direct I/O (`direct=1`)** bypasses the page cache so you measure the
  *filesystem*, not RAM. Canonical mode **requires** it and fails (rather than
  silently falling back to buffered I/O) if the filesystem can't do direct I/O.
- **Preconditioning:** read tests need real data on disk. The script fully writes
  every file *before* measuring, so a read test isn't secretly reading sparse
  holes, and the measured sequential-write test *overwrites* prepared files
  rather than measuring first-time sparse allocation. Preconditioning output is
  kept separate and is **not** the measured write result.
- **64 jobs × 10 GiB = 640 GiB:** 64 parallel jobs drive the filesystem hard
  enough to reach its ceiling; one 10 GiB file per job means 640 GiB of real
  data. That is why canonical mode needs ≥ ~704 GiB free (dataset + margin).
- **Queue depth 32:** each job keeps up to 32 I/Os in flight (via the async
  `libaio` engine) so the storage pipeline stays full. A synchronous engine
  cannot deliver real queue-depth-32 behaviour — the script uses an async engine
  and prints it.
- **One host at a time:** the reference numbers are *per host*. The script pins
  the FIO pod to one node, runs the full benchmark, reports that exact node, then
  moves to the next. It never runs all nodes at once and calls the result
  "per-host" — that would be an aggregate/contention test, labelled separately.

### Per-host vs multi-host

Pass `--target-node "A B C"` (or let it auto-pick) to benchmark several hosts —
each runs the **complete** benchmark **sequentially** and produces its **own**
report. Concurrent multi-host (contention/aggregate scaling) is intentionally
not what this does.

### Multi-file vs single-file

- **Multi-file (canonical):** 64 jobs, one distinct 10 GiB file each. This is the
  mode the reference values use.
- **Single-file (`--file-mode single`):** all jobs share one file at
  non-overlapping offsets. Reported separately and **never** compared to the
  multi-file reference — single-file performance is often lower due to per-file
  locking/metadata contention.

### Canonical vs custom mode

- **Canonical** reproduces the validated SFS POC preset (fio 3.36, `libaio`,
  `direct=1`, 64 jobs, qd 32, 10 GiB/job, 640 GiB, 120 s/test, 1M/4K blocks).
- **Custom:** change any canonical parameter and the run auto-labels itself
  **non-comparable** (and lists exactly which parameters changed). Changed
  parameters make results not directly comparable to the reference or to other
  runs.

### Storage scope — reference is SFS-specific

The initial reference values are for **Nebius Shared Filesystem (SFS / data-fs)**
only. FIO is a generic tool and can point at any mounted filesystem (via a
`--storage-class` or `--existing-pvc`), but the script only shows the SFS
reference comparison when the target *is* SFS. Non-SFS, custom, single-file, or
concurrent multi-host results are **not** compared to the SFS numbers. The report
prints the tested StorageClass, PVC, filesystem type, mount, and capacity, and
labels results with the storage target.

### Relationship to the other fio scripts in the repo

The repo already ships fio benchmarks under `data-transfer/`
(`benchmark_fio_single_node.sh`, `benchmark_fio_multi_node_read.sh`,
`benchmark_fio_multi_node_write.sh`) and an fio step in
`applications/osmo/workflows/osmo/test_mnt_data.yaml`. Those are a **different
methodology** — a different preset (`bsrange=64k-2M`, 16 jobs, iodepth 16, 20 G,
60 s) driven over SSH to named workers rather than this script's kubectl-driven
canonical preset. **Their numbers are not comparable to this script's canonical
preset or to the SFS reference values below.** Pick one methodology and stay in it
when comparing runs; this script (06) is the one the SFS reference figures belong to.

### SFS POC reference values (observed, not guarantees)

| Metric | Reference |
|---|---|
| Sequential read, 1 MiB | 25.61 GB/s |
| Sequential write, 1 MiB | 19.82 GB/s |
| Random read, 4 KiB | 157.4k IOPS |
| Random write, 4 KiB | 110.9k IOPS |

Scope: Nebius SFS/data-fs, per host, multi-file, fio 3.36, 64 jobs, qd 32, direct
I/O, 10 GiB/job, 640 GiB dataset, 120 s/test. These are **observed POC figures**,
**not** pass/fail requirements or service guarantees. The script does **not** fail
for being below them (any enforceable threshold is off by default). Execution
correctness and performance comparison are kept separate.

### Capacity, runtime, cleanup

- **Capacity:** canonical writes **≥ 640 GiB** during preconditioning plus
  additional measured writes. The script checks free space from inside the mount
  and refuses to run if there isn't enough (dataset + configurable margin).
- **Runtime:** the four measured tests alone take **≥ 8 minutes** (4 × 120 s),
  excluding image pull, scheduling, preconditioning, and cleanup.
- **Cleanup:** on EXIT/INT/TERM the script removes only *its own* run's pod, PVC
  (if it created one), and dataset directory (by exact, validated path — never a
  broad glob, never the mount root or a home directory). Use `--retain-data` to
  keep everything for inspection; pod logs and raw FIO JSON are always preserved
  under the result directory.

### Examples

```bash
# 1. Interactive canonical run (prompts before writing ~640 GiB)
./06-run-fio-performance-test.sh

# 2. Noninteractive canonical run
./06-run-fio-performance-test.sh --yes

# 3. Select a StorageClass
./06-run-fio-performance-test.sh --storage-class csi-mounted-fs-path-sc --yes

# 4. Use an existing PVC (e.g. an SFS-backed claim)
./06-run-fio-performance-test.sh --existing-pvc my-sfs-pvc --yes

# 5. Target a specific node
./06-run-fio-performance-test.sh --target-node computeinstance-xxxx --yes

# 6. Custom parameters (auto-labelled non-comparable)
./06-run-fio-performance-test.sh --numjobs 16 --runtime 60 --yes

# 7. Retain the dataset for inspection
./06-run-fio-performance-test.sh --retain-data --yes

# 8. Quick smoke (NON-CANONICAL — tiny dataset, short runtime)
./06-run-fio-performance-test.sh --smoke --yes
```

Run `./06-run-fio-performance-test.sh --help` for every option and environment
variable. Raw FIO JSON, pod logs, and a per-host `report.md` are written under
`--result-dir` (default `./fio-results/<run-id>/<node>/`).
