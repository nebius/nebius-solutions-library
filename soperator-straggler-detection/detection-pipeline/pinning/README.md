# CPU pinning for Inspector's threads — NEW, UNVALIDATED implementation

Written from scratch (the original `thread_pin.cc` / `rank_setup.py` could not be located). It is **not** the
validated original; it follows the original's documented methodology. Treat as unvalidated
until reproduced on your own cluster. Validation so far: one 16-GPU cluster (2 × 8×H200), NCCL 2.30.1 and 2.32.3.

## What it does
Gives two threads per rank their own dedicated physical core, and removes those cores from training:
- **dump thread** — Inspector's own; found at `pthread_create` via `dladdr()` of the start routine (plugin .so).
  Works on any NCCL.
- **profiler thread** — NCCL's own (`ncclProfilerThreadCreate`), found by call-site address range. Needs
  **NCCL ≥ 2.32.3 built unstripped** (symtab-visible). NCCL 2.30.1 has no such symbol.

On an 8-GPU node with the default 1 core per role that is 8 ranks × 2 roles = **16 physical cores** (32 logical CPUs
with SMT siblings) reserved per node, taken from the top of each GPU's own NUMA node and never core 0; training
keeps the rest. (Measured on the validation nodes: reserved `48-63,112-127`, training `0-47,64-111`.)

## How it is wired — nothing to run by hand (in the Megatron launcher)

| Step | Who | What |
|---|---|---|
| 1 | `train_node_megatron.sh` | sources `pin_env.sh` and calls `pin_prepare` (default `PIN_MODE=auto` when `INSPECTOR=on`) |
| 2 | `pin_resolve.py` | finds the NCCL that will be loaded, probes it for `ncclProfilerThreadCreate`, resolves `auto`/`full` → `full` or `dump` |
| 3 | `build.sh --if-needed` | builds `libthread_pin_new.so` if missing/stale (flock-serialized; read-only dir → `$PIN_BUILD_DIR`); no gcc → unpinned + warning |
| 4 | `pin_env.sh` | prints one **`[PIN-PREFLIGHT]`** line (requested vs resolved mode, NCCL version/path, reserved CPUs, reason) and starts `verify_pinning.py` |
| 5 | `rank_launch.sh` (per rank) | `rank_setup.py` computes this rank's NUMA-aware plan; sets `LD_PRELOAD` + `taskset` for the training CPUs |
| 6 | `thread_pin_new.c` | pins each recognised thread as it is created, logs the result |
| 7 | `verify_pinning.py` | re-reads real `/proc/<pid>/task/<tid>/status` affinity, cross-checks the NCCL really mapped vs the preflight prediction |

`install.sh` Step 6 also builds the interposer up front and reports what `auto` would do on this host
(`BUILD_NCCL_232=1 ./install.sh` additionally builds NCCL 2.32.3).

**Safety contract:** pinning never aborts a job. Resolution failure, missing interposer, missing gcc, planner failure →
the rank runs **unpinned** with a loud `[PIN][WARN] ... running UNPINNED; this run is NOT dedicated` line. Asking for
`full` on an NCCL that cannot support it is *downgraded to `dump`* and says so — it never claims dedication it lacks.

### Using it from another workload / launcher
```bash
export GPUS_PER_NODE=8 OUTDIR=/some/run/dir         # PIN_MODE defaults to auto
source /path/to/detection-pipeline/pinning/pin_env.sh
pin_prepare                                          # prints [PIN-PREFLIGHT]; sets PIN_WRAP (empty if off/unavailable)
torchrun --nproc_per_node=$GPUS_PER_NODE ... "${PIN_WRAP[@]}" train.py ARGS
```
Only the Megatron launcher is wired and validated; the other workloads (nanoGPT-family self-test etc.) do **not** use
pinning yet.

## Configuration (env)
| Var | Default | Meaning |
|---|---|---|
| `PIN_MODE` | `auto` (Megatron launcher, when `INSPECTOR=on`; `off` when `INSPECTOR=off`) | `auto` / `full` / `dump` / `off` |
| `PIN_CORES_PER_ROLE` | `1` | physical cores per role per rank (measured ~2.5% mean busy on a reserved core; 2 not tested) |
| `NCCL_OVERRIDE_DIR` | unset → `pinning/.nccl_override_dir` (written by `nccl/fetch_build_nccl.sh`) | dir with the NCCL to load first. An explicit value applies even with pinning off |
| `PIN_NCCL_LIB` | derived | force which `libnccl.so.2` to probe |
| `PIN_LOG_DIR` | `$OUTDIR/pinlogs` | thread-creation records, `launch_<host>.log`, `build_<host>.log` |
| `PIN_BUILD_DIR` | `/tmp/inspector-pin-<uid>` | where to build the interposer if the package dir is read-only |
| `PIN_VERIFY_SAMPLE`, `PIN_VERIFY_SAMPLE_AFTER` | `0` | seconds of per-thread CPU/affinity-drift sampling, and delay before it starts |
| `PIN_HOUSEKEEPING` (read by `run.sh`) | `0` | `1` = confine the aggregators / iowait logger / bpftrace to non-reserved CPUs |
| `INSPECTOR` | `on` | `off` = control arm, no plugin loaded |
| `MEGA_TP/PP/LAYERS/HIDDEN/HEADS/SEQ/MBS/GBS` | original tiny TP4/PP4 config | model/parallel shape |
| `SRUN_EXTRA` | unset | e.g. `--exclusive --cpus-per-task=128 --cpu-bind=none` (see Gotchas) |

## Runtime verification
`verify_pinning.py` waits for each rank's thread-creation records, re-reads every recorded thread's affinity from
`/proc`, checks the training main thread excludes the reserved cores, and checks the NCCL actually mapped matches the
preflight prediction. `[PIN-VERIFY] ... verdict=FULLY_DEDICATED` only if every rank has verified threads for **both** roles;
`DUMP_ONLY_DEDICATED` when only the dump role was requested/resolved (older NCCL, `PIN_MODE=dump`); otherwise
`PARTIAL_OR_NOT_DEDICATED` plus `[PIN-VERIFY][WARN]` reasons. With `--sample N` it then
samples each pinned thread for N seconds (per-thread CPU%, last-run CPU, affinity drift). Exit code 3 on any problem.

## Deployment dependency: NCCL ≥ 2.32.3 (reproducible build script in the repo; binary is NOT)

Full dedication needs `ncclProfilerThreadCreate`, which first appears in NCCL 2.32.x (absent in the 2.30.1 the hosts
ship). A 255 MB binary does not belong in git, so the repo carries a pinned, scripted build instead:

```bash
pinning/nccl/fetch_build_nccl.sh [--dest DIR] [--gencode "-gencode=arch=compute_90,code=sm_90"] [--jobs N]
pinning/nccl/fetch_build_nccl.sh --check      # report only: version + pinnable symbol, builds nothing
```
It clones tag **`v2.32.3-1`**, builds with `nice`, auto-detects the GPU arch from `nvidia-smi` when run on a GPU node
(on a GPU-less login node pass `--gencode` for the compute nodes' arch, else NCCL's full multi-arch set is built —
slow), **verifies** the result (`ncclGetVersion()` = 2.32.3 *and* the profiler symbol readable) and records the lib dir
in `pinning/.nccl_override_dir`, which `pin_env.sh` then uses automatically. Requirements: git, make, g++, CUDA
toolkit (nvcc), network access to github.com. Put `--dest` on a **filesystem every compute node can read** (default:
inside this package). Never `strip` the library: the probe reads `.symtab`.

Honest status: `fetch_build_nccl.sh` was tested once, end to end, from an empty directory on the login node: clone of `v2.32.3-1`, single-arch (`sm_90`) build with `--gencode`, ~17 min with 8 jobs, then its own verification (`ncclGetVersion()` = 2.32.3, profiler symbol readable) passed. It has not been run on any other cluster, and a full multi-arch build was not timed. The 2.32.3 library used for all validation here was an
earlier manual build of the same tag (CUDA 13.0.88, default multi-arch), not produced by this script. The repo's
Inspector plugin exports only `ncclProfiler_v5`; NCCL 2.32.3 loads it and writes dumps correctly (verified), but the
plugin was built against 2.28-era headers. `torch.cuda.nccl.version()` is a compile-time constant and does **not**
show the loaded NCCL — use `nccl_version_probe.py` (`ncclGetVersion()`).

## Gotchas found
- Default `srun` here gives each task a **2-CPU mask** even with `--exclusive`; pass `--cpus-per-task=128 --cpu-bind=none`
  or all 8 ranks on a node share 2 CPUs (affects every unpinned number).
- Containers load the host NCCL (2.30.1) via the `/usr/lib/x86_64-linux-gnu` mount, shadowing the container's own.
- `soperatorchecks` health-check jobs run periodically on the workers and contaminate timing; the A/B driver waits
  for an empty queue and `analyze_ab.py` flags overlapping runs.
- A GPU-less host (login node) cannot produce a core plan: `reserved_cpus=none` in the preflight there is expected.

## Measured (16 GPUs, TP4/PP2/DP2, 8B-class, NOT comparable to the 48-GPU baseline; 150 iters after 20 warmup)
NCCL 2.32.3 (4 interleaved rounds): ON unpinned **+11.5%** (p=0.001), dump-only **+6.4%** (p=0.007), full
**+0.64% (p=0.58, not significant)**. NCCL 2.30.1 (3 rounds): ON unpinned **+10.6%**, dump-only **+1.35% (n.s., p=0.19)**.
Full-pinning runs verified FULLY_DEDICATED on 8/8 ranks per node (dump-only runs report DUMP_ONLY_DEDICATED). Reserved cores ~2.5% mean busy (max core ~7%).
Per-thread sampling (soak and matrix): dump thread 5–10% of its core, profiler thread ~0%, 100% of samples on the
assigned CPU, 0 affinity drift. Soak (4500 iterations, 600 s sampled): dump thread 5.5% mean, profiler 0.0%, 100% on assigned CPU, 0 drift.
Raw data lives with the audit run, not in git (`runs/`, `logs/`); method scripts are in `ab/` and `ab/helpers/`.
