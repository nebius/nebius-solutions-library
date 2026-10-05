# GPU Soak Test — MK8s Equivalent of cloudmeter auto-loader

A Kubernetes-native GPU soak test that replicates the cloudmeter auto-loader's goal on MK8s clusters: sustained GPU compute + HBM fill + InfiniBand/NVLink stress with structured pass/fail output and a clean report.

It runs as a **PyTorchJob** using a single PyTorch image on every pod. `torchrun`
launches one `torch.distributed` rank per GPU, and each rank concurrently:

1. **Fills HBM** with a resident FP16 tensor (default 75% of device memory),
2. **Stresses compute** with a continuous FP16 matmul loop (drives util to ~100%),
3. **Stresses the fabric** with a timed, verified `all_reduce` every iteration.

This avoids the launcher/worker image split that a `nccl-tests` + `mpirun` setup
requires — PyTorch bundles NCCL, so the same container does the memory fill and
the collective, and the collective's exit code is authoritative (a real NCCL
failure fails the job).

## What it tests

| Auto-loader (Slurm) | This tool (MK8s) |
|---|---|
| HPL stresses GPUs | PyTorch FP16 matmul stresses GPUs |
| Fills GPU memory | Dynamic HBM fill — 75% of total memory per GPU (tunable via `HBM_FILL_FRACTION`; leaves NCCL headroom) |
| NCCL allreduce stresses IB | `torch.distributed.all_reduce` every iteration, verified + busbw measured |
| GPU temperature monitoring | GPU temperature monitoring (fail >83°C) |
| GPU overheating check | XID error detection via node dmesg (best-effort; flagged UNVERIFIED if unavailable) |
| sacct job tracking | Kubernetes pod status tracking |
| tmux session | Foreground monitor + structured report file |

## Prerequisites

- Kubeflow Training Operator installed (provides the PyTorchJob CRD):
  ```bash
  kubectl apply -k 'github.com/kubeflow/training-operator/manifests/overlays/standalone?ref=v1.7.0'
  ```
  > **Heads-up for clusters that already run an MPI operator** (e.g. Slurm-on-K8s /
  > soperator setups): the Training Operator's standalone bundle also ships an
  > `mpijobs.kubeflow.org` CRD, which can collide with an existing one at a
  > different version and leave the operator crash-looping (PyTorchJobs then never
  > reconcile). If you already have a Training Operator, skip this install; if you
  > have a conflicting MPI operator, reconcile the `mpijobs` CRD versions first.
- GPU nodes present and Ready in the cluster
- kubectl configured and authenticated

## Usage

```bash
chmod +x run-soak-test.sh scripts/monitor.sh

# 10 minute POC sanity check (2 nodes)
./run-soak-test.sh 600 2

# 1 hour soak (2 nodes)
./run-soak-test.sh 3600 2

# 2 hour production handoff run (4 nodes)
./run-soak-test.sh 7200 4

# Non-interactive (CI): auto-delete the namespace at the end
AUTO_CLEANUP=y ./run-soak-test.sh 600 2

# More aggressive HBM fill
HBM_FILL_FRACTION=0.85 ./run-soak-test.sh 3600 2
```

Duration argument is in seconds: 600 = 10 min, 3600 = 1 hour, 7200 = 2 hours.

For a customer running week-long training jobs, 10 minutes is a sanity check only. Run 2-4 hours minimum for a production handoff to catch thermal creep, slow HBM memory errors, and IB link degradation that only surface under sustained load.

## GPU compatibility — choosing the image (READ THIS for GB300 / B200)

The soak *workload* is generic PyTorch and runs on any CUDA GPU. The only thing
tied to a specific GPU is the **container image** (`SOAK_IMAGE`), which must match
both the GPU's compute architecture and the node's CPU architecture:

| GPU | Architecture | CPU | Default image (`24.01`) | `SOAK_IMAGE` to set |
|---|---|---|---|---|
| **H100 SXM** | Hopper (sm_90) | x86 | ✅ works (validated) | none — default |
| **H200 SXM** | Hopper (sm_90) | x86 | ✅ works | none — default |
| **B200 SXM** | Blackwell (sm_100) | x86 | ❌ CUDA too old | `nvcr.io/nvidia/pytorch:25.06-py3` (or newer) |
| **GB200** | Blackwell | ARM (Grace) | ❌ arch + CUDA | `nvcr.io/nvidia/pytorch:25.06-py3` (or newer) + `MAX_TEMP=90` |
| **GB300** | Blackwell Ultra | ARM (Grace) | ❌ arch + CUDA | `nvcr.io/nvidia/pytorch:25.06-py3` (or newer) + `MAX_TEMP=90` |

Why the default doesn't cover Blackwell: `nvcr.io/nvidia/pytorch:24.01-py3` ships
CUDA 12.3, which predates Blackwell (sm_100) — kernels fail with *"no kernel image
available for execution on the device."* **Blackwell support begins in the NGC
PyTorch `25.01` release** (CUDA 12.8+); use a recent `25.x` or newer. NGC PyTorch
tags are **multi-arch** (the same tag serves x86 and ARM/SBSA), so GB200/GB300
Grace nodes pull the arm64 build automatically from the same tag — no separate
arm64 tag needed.

```bash
# H100 / H200 — nothing to set, the default just works
./run-soak-test.sh 7200 2

# B200 (x86 Blackwell)
SOAK_IMAGE=nvcr.io/nvidia/pytorch:25.06-py3 ./run-soak-test.sh 7200 2

# GB200 / GB300 (ARM Blackwell) — same tag (multi-arch) + higher thermal threshold
SOAK_IMAGE=nvcr.io/nvidia/pytorch:25.06-py3 MAX_TEMP=90 ./run-soak-test.sh 7200 2
```

`25.06-py3` is a known-good example — prefer the **newest** `YY.MM-py3` (≥ `25.01`),
or a **Nebius-recommended image** if their GB300 docs publish one. Before a
multi-hour run on a new GPU, verify the tag and run a short smoke:

```bash
# Confirm CUDA >=12.6 and Blackwell (sm_100) in the image's compiled arch list:
docker run --rm nvcr.io/nvidia/pytorch:25.06-py3 \
  python -c "import torch; print(torch.version.cuda); print(torch.cuda.get_arch_list())"
# Smoke test (5 min) before committing:
AUTO_CLEANUP=y SOAK_IMAGE=nvcr.io/nvidia/pytorch:25.06-py3 ./run-soak-test.sh 300 2
```

Everything else — GPU detection, HBM sizing, 8-GPU layout, NCCL, monitoring,
report — adapts automatically; only `SOAK_IMAGE` (and `MAX_TEMP` for Blackwell)
changes per platform.

## GPU type detection — automatic

The script automatically detects whatever GPU node type is present in the cluster at runtime. No configuration needed.

```
Detecting GPU node type in cluster...
Detected GPU type: gpu-h100-sxm
GPU name: H100 SXM (80GB HBM3)
```

| Hardware | nodeSelector (auto-detected) |
|---|---|
| H100 SXM | `gpu-h100-sxm` |
| H200 SXM | `gpu-h200-sxm` |
| B200 SXM | `gpu-b200-sxm` |
| B300 SXM | `gpu-b300-sxm` |

To confirm available GPU types in your cluster:
```bash
kubectl get nodes -L node.kubernetes.io/instance-type
```

## HBM memory fill — dynamic

Each rank queries total GPU memory at runtime and fills `HBM_FILL_FRACTION`
(default 0.75) of it, regardless of GPU type. Allocation order reserves the
all_reduce buffer and matmul working set first, then a resident "hog" tensor
occupies the remainder — so the collective always has headroom and never OOMs.

| GPU | Total HBM | Fill target (75%) |
|---|---|---|
| H100 SXM | 80 GB | ~60 GB |
| H200 SXM | 141 GB | ~106 GB |
| B200 SXM | 192 GB | ~144 GB |

## What runs

**Every pod (master + workers), one rank per GPU via `torchrun`:**
- Resident FP16 hog tensor fills HBM to the configured fraction
- Continuous `torch.matmul(ma, mb, out=mc)` loop (zero runtime allocation) → ~100% util
- Timed `dist.all_reduce` each iteration across all ranks (IB inter-node, NVLink intra-node)
- Verifies the all_reduce result each iteration and measures busbw
- Master (rank 0) prints `BUSBW_GBPS` / `BUSBW_AVG_GBPS` and the pass/fail line;
  its exit code fails the job on any incorrect result

**Monitor:**
- Polls every 30 seconds across all job pods
- Checks GPU temperature, utilization, power draw via nvidia-smi
- Single end-of-run XID check via node dmesg (marked UNVERIFIED if it cannot run)
- Checks node Ready status
- Writes a structured log file and generates a clean report at completion

## Pass/fail criteria

| Check | Pass condition |
|---|---|
| NCCL all_reduce | Master exit code 0 (all iterations correct) |
| GPU temperature | Never exceeds 83°C |
| XID errors | Zero detected (WARN if the check could not run) |
| GPU utilization | Stays above 80% (fewer than 10 low-util events) |

## Expected results on H100 SXM

| Metric | Expected |
|---|---|
| GPU utilization | ~100% |
| HBM fill | ~75% (~60 GB of 80 GB) |
| Temperature | 60–80°C under sustained load |
| Power draw | 650–700W per GPU |
| all_reduce busbw | >300 GB/s intra-node (NVLink); inter-node bounded by IB fabric |

## Output files

After each run two files are written to the `gpu-soak-test/` directory:

- `soak-monitor-YYYYMMDD_HHMMSS.log` — full detailed log with every poll and GPU stat
- `soak-report-YYYYMMDD_HHMMSS.txt` — clean summary report with pass/fail verdict

## Monitoring during the run

```bash
# Watch master (rank 0) logs — busbw + iteration results
kubectl logs -f -n gpu-soak -l training.kubeflow.org/replica-type=master

# Watch all pod status
kubectl get pods -n gpu-soak -w

# Check GPU stats on any job pod directly
POD=$(kubectl get pods -n gpu-soak -l training.kubeflow.org/replica-type=master -o name | head -1)
kubectl exec -n gpu-soak ${POD#pod/} -c pytorch -- \
  nvidia-smi --query-gpu=index,temperature.gpu,utilization.gpu,power.draw \
  --format=csv,noheader
```

## Cleanup

The script prompts for cleanup at the end (or use `AUTO_CLEANUP=y`). To clean up manually:

```bash
kubectl delete namespace gpu-soak
```

## Troubleshooting

**Worker pod stuck in `Init:Error` on the first run.** The Kubeflow Training
Operator injects a worker init container that waits only ~200s for the master
pod's DNS. On a cold cluster the first-time pull of the (large, ~20GB) NGC image
can take longer than that, so the worker gives up before the master is Ready.

The runner **pre-pulls the image onto the target GPU nodes** (via a short-lived
DaemonSet) before submitting the job, which avoids this in the normal case. If
you still hit it — e.g. the pre-pull timed out on a very slow registry — just
re-run once the image is cached, or pre-pull manually:

```bash
kubectl get nodes -l node.kubernetes.io/instance-type=gpu-h200-sxm -o name
# then let the pre-pull finish, or crictl pull the image on those nodes
```

## Known limitations

- **Single-tenant** — a soak saturates every GPU on the cluster, so run only one
  at a time. The script refuses to start if the `gpu-soak` namespace already has
  running pods, rather than deleting another run's resources out from under it.
- Requires the Kubeflow Training Operator (PyTorchJob CRD)
- Inter-node IB stress requires 2+ GPU nodes — a single node exercises intra-node NVLink only
- 10 minute run is a sanity check only — use 2+ hours for production handoff
- XID detection relies on node dmesg via `kubectl debug node`; where that is not
  permitted (and no DCGM exporter is present) XID is reported UNVERIFIED rather
  than falsely clean
- Auto-loader uses HPL (High Performance Linpack), a certified benchmark — this tool uses PyTorch matmul as the compute stressor, which achieves equivalent GPU utilization and thermal stress but is not the same certified benchmark
