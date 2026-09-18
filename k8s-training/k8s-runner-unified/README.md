# Unified NCCL Test Runner

A single, reusable NCCL benchmarking toolchain for Nebius MK8s clusters.
It **auto-adapts to the GPU node type it is pointed at** (H100 / H200 / B200 / B300)
instead of hardcoding per-hardware assumptions that break when switching clusters,
runs `all_reduce` + `alltoall` over both NVLink (single-node) and InfiniBand
(multi-node), and produces a shareable markdown report.

## Contents

| File | Role |
|---|---|
| `nccl_runner_unified.sh` | Orchestrator — detects hardware, renders + runs jobs, retries, reports |
| `nccl-test-template.yaml` | Parameterized Kubeflow MPIJob (envsubst `${VAR}` placeholders) |
| `generate-report.sh` | Parses raw logs → `report.md` (auto-invoked by the runner) |

## What it auto-detects (so you don't hardcode it)

1. **Node capacity** — allocatable CPU / memory / GPU → worker resource sizing
   (`allocatable − RESERVE_*`).
2. **IB devices** — a throwaway pod runs `ibv_devinfo`; only ports that are
   `link_layer: InfiniBand` **and** `state: PORT_ACTIVE` are kept, giving the real
   `NCCL_IB_HCA` list (e.g. B300 → `mlx5_4…11`, H200 → `mlx5_0…7`).
3. **CPU topology** — `nproc` + NUMA node count + GPU count are used to compute the
   process binding: `pe = CPUs / GPUs`, `procs_per_numa = GPUs / NUMA`, rendered as
   `--map-by ppr:${procs_per_numa}:numa:pe=${pe} -bind-to hwthread`. Falls back to
   coarse `-bind-to numa` when GPUs don't divide evenly across NUMA nodes.
   *(This is the fix for the B300→H200 breakage: a fixed `pe=24` needs 96
   hwthreads/NUMA and aborts on H200's 64/NUMA.)*
4. **MPIJob API version** — `v1` vs `v2beta1` (installs differ per cluster);
   `launcherCreationPolicy` is only emitted on `v2beta1`.
5. **Host-count cap** — requested host counts are capped to the number of
   **Ready, schedulable** nodes in the node group (Cordoned / NotReady nodes are
   excluded), so the sweep never hangs on unschedulable jobs.

Every run creates **uniquely-named, labelled resources** (`nccl-test-<pid>-<epoch>`,
labelled `nccl-runner/run=<id>`), so two engineers can run against the same
namespace without touching each other's MPIJob or pods. A local **PID lock**
(`/tmp/nccl_runner_unified.lock`) additionally prevents accidental double-runs on
the same workstation, and a launch is retried up to `MAX_LAUNCH_ATTEMPTS` times if
it fails to start.

## Usage

```bash
# 1. Install the MPI Operator if it isn't present (the runner tells you if not):
kubectl apply --server-side -k \
  "github.com/kubeflow/mpi-operator/manifests/overlays/standalone?ref=v0.6.0"

# 2. Set NODE_GROUP_ID (and review IMAGE) at the top of nccl_runner_unified.sh.
#    Find the node group ID with:
kubectl get nodes -o custom-columns=\
'NAME:.metadata.name,GROUP:.metadata.labels.nebius\.com/node-group-id,GPU:.status.capacity.nvidia\.com/gpu'

# 3. Run:
./nccl_runner_unified.sh
#    -> results/nccl-<timestamp>/<test>-<hosts>.log  (raw NCCL output)
#    -> results/nccl-<timestamp>/report.md           (auto-generated summary)
```

Regenerate a report from existing logs at any time:

```bash
./generate-report.sh results/nccl-<timestamp>
```

> **Concurrent runs are isolated by unique resource names + labels**, so multiple
> engineers can share a namespace safely. Cleanup is always scoped to a run's own
> MPIJob and cascaded gracefully — the runner never force-deletes pods or touches
> pods it doesn't own.

## Configuration (top of `nccl_runner_unified.sh`)

| Var | Default | Meaning |
|---|---|---|
| `NAMESPACE` | `nccl-tests` | Namespace to run in |
| `IMAGE` | Nebius `nccl-tests` image | Must be pullable on the cluster |
| `NODE_GROUP_ID` | — | **Set per cluster** |
| `RESERVE_CPU_CORES` / `RESERVE_MEM_GI` | `4` / `50` | Headroom left for kubelet/daemonsets |
| `MAX_LAUNCH_ATTEMPTS` | `6` | Retry budget for the transient launcher glitch |
| `HOSTS` | `(1 2 3 4)` | Host counts to sweep. Auto-capped **down** to available Ready nodes, but never scales **up** past the values listed — on clusters larger than 4 nodes, edit this to test at full scale (e.g. `(1 2 4 8 16)` for a 16-node group). |
| `TESTS` | `(all_reduce alltoall)` | Add `all_gather reduce_scatter reduce gather broadcast scatter` for a fuller sweep |

## Scope & caveats

- **Sweep size is set by `HOSTS`, not auto-scaled.** The runner caps requested
  counts down to the available Ready nodes so it never hangs, but it won't test
  beyond the largest value in `HOSTS`. For clusters bigger than 4 nodes, add
  higher counts (powers of two up to your node count, plus the full count) so the
  full-cluster fabric actually gets measured.
  - *Example — 100-node cluster:* set `HOSTS=(1 2 4 8 16 32 64 100)`. Powers of
    two give a clean scaling curve, and `100` measures the full cluster. Leaving
    the default `(1 2 3 4)` would test only 4 of your 100 nodes and tell you
    nothing about the fabric at scale.
- **Supported as-is:** Nebius **x86 + InfiniBand** GPU types — H100, H200, B200, B300.
- **Not supported as-is:** Grace-ARM types (GH200 / GB200) — aarch64, so the x86
  image won't run and the binding math should be re-validated.
- Assumes an **InfiniBand** fabric (RoCE/Ethernet clusters return no IB devices and
  the runner errors out) and the Nebius `nebius.com/node-group-id` label.
- `busbw` (bus bandwidth) is the figure to compare against reference numbers.
  Single-host runs measure intra-node NVLink; multi-host runs cross the IB fabric.
