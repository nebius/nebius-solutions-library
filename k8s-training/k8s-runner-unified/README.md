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
| `nccl-test-template.yaml` | Parameterized Kubeflow MPIJob, x86/device-plugin (`nvidia.com/gpu` limits) |
| `nccl-test-template-dra.yaml` | Same MPIJob for DRA/GB300 (GPU claims + ComputeDomain channel) |
| `dra/gpu-resourceclaim-template.yaml` | DRA GPU claim for worker pods (GB300) |
| `dra/compute-domain.yaml` | ComputeDomain enabling cross-node NCCL over MNNVL (GB300) |
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
6. **GPU request mechanism** — device-plugin (`nvidia.com/gpu` limits, x86) vs
   **DRA** (`gpu.nvidia.com` DeviceClass, GB300/Grace). On DRA there is no
   allocatable `nvidia.com/gpu`, so the runner switches to the `-dra` MPIJob
   template (GPU claims), reads GPUs/node from the GFD label / resourceslices, and
   creates a ComputeDomain for cross-node NCCL. The x86 path is unchanged.

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

## GB300 / DRA clusters — automatic

On DRA-native clusters (GB300/Grace) the runner needs **no code changes and no flags**
— it detects DRA and adapts. The `IMAGE` is multi-arch, so the same tag runs on x86
and arm64. What changes automatically:

- Worker pods claim GPUs via `dra/gpu-resourceclaim-template.yaml` + the `-dra` MPIJob
  template, instead of `nvidia.com/gpu` limits.
- A **ComputeDomain** (`dra/compute-domain.yaml`, sized to the largest host count) is
  created so cross-node NCCL forms one MNNVL/NVLink domain — without it NCCL's NVLS
  setup fails with `Cuda failure 801`. It's torn down when the runner exits.
- The report reads the **actual** transport from the NCCL logs (MNNVL vs IB).

By default the cross-node collective rides **MNNVL/NVLink** (fastest on GB300). To
soak the **InfiniBand** fabric instead, set `NCCL_TRANSPORT=ib` (disables MNNVL+NVLS):

```bash
NODE_GROUP_ID=<gb300-group> NCCL_TRANSPORT=ib ./nccl_runner_unified.sh
```

Validated on a 2-node GB300 DRA cluster: all_reduce + alltoall, 1 and 2 hosts, auto
and `ib` transport — all passed (2-host all_reduce peak ~843 GB/s over MNNVL, 0 errors).

## Configuration

Top-of-script vars (several are also environment-overridable, so you don't edit the file):

| Var | Default | Meaning |
|---|---|---|
| `NAMESPACE` | `nccl-tests` | Namespace to run in |
| `IMAGE` | Nebius `nccl-tests` image (multi-arch) | Must be pullable on the cluster |
| `NODE_GROUP_ID` | (placeholder) | **Set per cluster** — env-overridable |
| `NCCL_TRANSPORT` | `auto` | DRA/GB300 only: `auto` (MNNVL/NVLink) or `ib` (InfiniBand) |
| `RESERVE_CPU_CORES` / `RESERVE_MEM_GI` | `4` / `50` | Headroom left for kubelet/daemonsets |
| `MAX_LAUNCH_ATTEMPTS` | `6` | Retry budget for the transient launcher glitch |
| `HOSTS` (`NCCL_HOSTS`) | `1 2 3 4` | Host counts to sweep. Auto-capped **down** to available Ready nodes, but never scales **up** past the values listed — on clusters larger than 4 nodes, set higher counts to test at full scale (e.g. `NCCL_HOSTS="1 2 4 8 16"`). |
| `TESTS` (`NCCL_TESTS`) | `all_reduce alltoall` | Add `all_gather reduce_scatter reduce gather broadcast scatter` for a fuller sweep |

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
- **Supported as-is:** Nebius **x86 + InfiniBand** GPU types (H100, H200, B200, B300)
  and **GB300 / DRA** clusters (Grace-ARM, auto-detected — see above).
- **Not validated:** GH200 / GB200 (aarch64) — the multi-arch image should run, but the
  binding math and DRA path haven't been exercised there.
- Assumes an **InfiniBand** fabric (RoCE/Ethernet clusters return no IB devices and
  the runner errors out) and the Nebius `nebius.com/node-group-id` label.
- `busbw` (bus bandwidth) is the figure to compare against reference numbers.
  Single-host runs measure intra-node NVLink; multi-host runs cross the IB fabric.
