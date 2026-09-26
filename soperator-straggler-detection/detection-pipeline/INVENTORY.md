# Exhaustive, code-verified inventory (V1 Beta packaging)

Every claim below was verified against the actual, live, currently-running
code on disk (primarily `/root/P20c_alerting/` and `/root/P18k_classifier/`
on the original development host) — not from memory, not from this
project's own prior summary documents, which were used only as a starting
point and then independently re-checked. Where a prior document (under
`docs/`, staged as `migration_package/` on the original host) was found to
be stale relative to the live code, that discrepancy is called out
explicitly rather than silently trusting the older source.

## Category A — Python dependency tracing (transitive)

**Local module graph** (real, from actual `import`/`from` lines):
- `node_aggregator_ref.py` → `promql_cv_verify` (co-located), `detection`
  (P18k_classifier)
- `alert_engine.py` → `classifier` (as `p18k`), `storage_evidence`,
  `report` (as `p18k_report`) — all from P18k_classifier; `thresholds`,
  `coverage_guard`, `pipeline_health`, `persistence` — all co-located
- `classifier.py` → `detection`, `cause_metrics`, `rolling_buffer`,
  `storage_evidence`
- `rolling_buffer.py` → `cause_metrics`
- `cv_fixed.py`, `decay_counter.py`, `transient_latency.py`,
  `calibration.py`, classifier-scoped `persistence.py` → `detection` (+
  `classifier` for the last one)
- `ras_alert.py` → `health_exclusions`
- `node_aggregator_with_shim.py` → `node_aggregator_ref` (confirmed **not**
  part of the live launch path — excluded from the package; see the top
  README's "explicitly excluded" section)

**Standard library only, the complete real set**: `argparse, glob, json,
math, subprocess, sys, time, urllib.parse, urllib.request, statistics,
collections (deque/defaultdict/Counter), threading, concurrent.futures,
logging, logging.handlers, os, re`.

**Third-party packages required by the detection pipeline itself: ZERO.**
Verified by grepping every import line across all 20 live `.py` files that
participate in the actual detection/alerting/classification pipeline. The
only third-party imports found anywhere in these two source directories
are in `train_resnet.py` (`torch`, `torch.nn`, `torch.distributed`,
`torchvision.models`) — a workload/fault-injection script, not part of the
detection pipeline, requiring its own separate PyTorch/torchvision/CUDA
dependency exactly like every other workload script under `workloads/`.

**`requirements.txt` for `classifier/`+`aggregator/`+`alerting/`: empty.**
This is a genuine, verified finding, not an oversight.

**Stale-copy discrepancy confirmed** (direct diff against the original
host's `migration_package/` staging copy, before this session sourced
everything from the live code instead):
- `migration_package/P20c_alerting/alert_engine.py` was missing `import
  storage_evidence` entirely (predates the storage-classifier wiring fix).
- `migration_package/P18k_classifier/cause_metrics.py` was missing
  `import sys` / `import concurrent.futures` (predates the
  SSH-parallelization/deadline fix).
- `migration_package/P20c_alerting/iowait_logger.py` was missing `import
  logging` / `import logging.handlers` entirely (predates log rotation).
- `alert_engine.py`, `node_aggregator_ref.py`, `iowait_logger.py`,
  `cause_metrics.py`, `classifier.py`, `cv_fixed.py`, `decay_counter.py`,
  `detection.py`, `storage_evidence.py`, `transient_latency.py` all
  differed from their live counterparts.

## Category B — Environment variable audit (exhaustive)

**NCCL Inspector plugin config vars** (verified against
`inspector-plugin/inspector.cc`, the only file with `getenv` calls in the
plugin):

| Var | Default | Purpose |
|---|---|---|
| `NCCL_INSPECTOR_ENABLE` | `0` | Enable/disable the plugin entirely |
| `NCCL_INSPECTOR_DUMP_THREAD_ENABLE` | `1` | Enable/disable the background dump thread |
| `NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS` | `0` | Dump thread flush interval |
| `NCCL_INSPECTOR_DUMP_DIR` | auto-generated | Output directory for per-rank JSON dump logs |
| `NCCL_INSPECTOR_DUMP_VERBOSE` | `0` | Verbose event-trace dumping (`0` = lean mode, the current production config) |
| `SLURM_JOBID` | none | Tags dumps with job id when running under Slurm |
| `NCCL_PROFILER_PLUGIN` | none | Read by NCCL core itself, points at the built `.so` |

**Flagged dead variable**: every launch script also sets
`NCCL_INSPECTOR_PROM_DUMP=0`, but this name is never referenced anywhere
in the plugin source — set everywhere, has zero effect on the current
build. See top README's "known limitations" item 4.

**Fault-injection vars (`STRAGGLER_*`)**, read identically across every
workload script:
- `STRAGGLER_SLEEP_MS` (ms, default `"0"`) / `STRAGGLER_TARGET_RANKS`
  (comma-separated global ranks, default `""`) — present in all 15.
- `STRAGGLER_MODE` (`"constant"` default; nanoGPT/FSDP also support
  `"file_trigger"` for live, mid-run, unannounced fault injection).
- `STRAGGLER_PHASE` (`"backward"`/`"forward"`/`"optimizer"`/`"checkpoint"`,
  default varies by script).
- `STRAGGLER_TRIGGER_FILE` (default `/tmp/straggler_trigger.json`,
  file-trigger mode only).
- `STRAGGLER_WARMUP_ITERS`, `STRAGGLER_ON_S`, `STRAGGLER_CYCLE_S`,
  `STRAGGLER_EVERY_N` — nanoGPT-specific cyclic-fault knobs.
- Workload-specific fault vars: `MOE_FAULT_TARGET_RANK` /
  `MOE_FAULT_DELAY_US` (MoE), `INJECT_ENABLE` / `INJECT_RANK` /
  `INJECT_BURST_MS` / `INJECT_PERIOD_MS` (ResNet), `ALLTOALL_PROBE_ITERS`
  (MoE all-to-all probe).

**Pipeline/aggregator config vars: none.** Every real config knob in
`node_aggregator_ref.py` and `alert_engine.py` is a CLI argument
(argparse), not an env var — zero `os.environ`/`os.getenv` hits in either
file. The only env var anywhere in that directory tree is
`AGGREGATOR_LOCAL_LOG`, in the excluded `node_aggregator_with_shim.py`.

**Everything else**: standard DDP/torchrun-injected identity vars read
directly by every workload script — `RANK`, `LOCAL_RANK`, `WORLD_SIZE`
(all required, bracket-form `os.environ['X']`, will `KeyError` without
torchrun) — plus per-workload sizing vars (`BATCH_SIZE`, `MAX_ITERS`,
`LOG_INTERVAL`, `TP_SIZE`, `N_MICROBATCHES`, `STAGE0_RANK`, `NUM_TABLES`,
`IMAGE_SIZE`, `BLOCK_SIZE`, `SEQ_LEN`, etc.), each with a sane default.

## Category C — Hardcoded path/node-identity audit

**Absolute paths** (non-diagnostic files only):
- `alert_engine.py`: `sys.path.insert(0, "/root/P18k_classifier")`,
  `sys.path.insert(0, "/root/P20c_alerting")`, `IOWAIT_LOG_DIR =
  "/root/P20c_alerting/iowait_logs"`.
- `iowait_logger.py`: `AGENT_BT =
  "/root/P20d_e2e_validation/storage_ebpf/iowait_agent.bt"`.
- `node_aggregator_ref.py`: `sys.path.insert(0, "/root/P19a_metrics")`
  (real, load-bearing — `promql_cv_verify.stat_cv` is actually called;
  confirmed the file is byte-identical to the co-located copy already in
  this package, so the external path dependency is resolved simply by
  using the co-located one), `sys.path.insert(0, "/root/P18k_classifier")`.
- `health_exclusions.py` / `cause_metrics.py`: both default to
  `/root/P4d_clean/health/bench_all_gpus.py` — real, live, confirmed
  called from `classifier.py`. The 4 real files this points at are now
  under `health-checks/`.
- `workloads/rl/train_rl.py`: `sys.path.insert(0,
  "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler")` **and** a
  separately hardcoded `DATA_DIR` into that same sibling directory — the
  only workload not using a relative/shared dataset reference.

**Hardcoded hostnames/node-count/GPU-count — the real portability
blockers**: every one of the 15 `workloads/*/run_*.sh` launch scripts
hardcodes `srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8
-w worker-0,worker-1` (or the torchrun equivalent) verbatim; every
`train_node_*.sh` companion hardcodes a literal `if [ "$(hostname)" =
"worker-0" ]; then RANK=0; else RANK=1; fi` (a third node would silently
get `RANK=1`); every rendezvous endpoint is a literal `worker-0:$PORT`
string.

**Important counter-evidence — the Python core is already largely
dynamic**:
- `AlertEngine.__init__`'s `hostnames=None` default means **dynamic
  discovery** — confirmed the actual production launch command
  (`python3 alert_engine.py <vm_url> --duration 0`) never passes
  `--hostnames`, i.e. already uses live discovery in practice.
- `node_aggregator_ref.py`'s `--node choices=["A","B"]` CLI flag is
  **confirmed vestigial by its own docstring**: "no longer selects a rank
  set (communicator membership is discovered from data) — kept only so
  existing launch scripts that pass `--node` keep working." `args.node` is
  never even passed into `NodeAggregator()`.
- `cause_metrics.py`'s `discover_gpu_count(host)` does real live discovery
  via `nvidia-smi -L | wc -l` — this project's own prior fix for an
  earlier hardcoded-8 bug elsewhere in the same file, and the correct
  pattern the shell launch scripts should be generalized to.

**Net finding**: the hardcoded-cluster-shape problem is concentrated
almost entirely in the shell launch layer (`run_*.sh`/`train_node_*.sh`),
not in the Python detection/alerting/classification core.

## Category D — External binary/tool audit

- **ssh** — `cause_metrics.py`: `subprocess.run(["ssh", "-o",
  "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", host, cmd], ...)`.
- **Slurm** (`squeue`/`srun`/`scontrol`) — e.g. `cause_metrics.py`:
  `subprocess.run(["squeue", "-w", host, "-h", "-o", "%i",
  "--states=R"], ...)`.
- **nvidia-smi** — `cause_metrics.py`: `nvidia-smi -i {i} -q -d
  PERFORMANCE` (per-GPU) and `nvidia-smi -L` (count discovery).
- **dcgmi / nv-hostengine** — `cause_metrics.py`: `dcgmi discovery -l`,
  `dcgmi dmon -e {field_ids} -c 1` — nv-hostengine liveness is itself
  monitored by this pipeline (a real, previously-found-down instance was
  discovered and fixed this project's own history).
- **bpftrace** — real, specific version already confirmed working on two
  separate clusters in a row: **0.20.2-1ubuntu4.3** (installed via apt;
  pulls in `llvm-18`/`libllvm18` automatically as its own dependency — do
  not install that separately).
- **logrotate** — apt-installed; used for `alert_engine_supervised.log`'s
  own rotation (added this project's own log-rotation fix).
- **NCCL / CUDA / compiler toolchain** — building `inspector-plugin/`
  requires a full NCCL source checkout with its own `build/` already
  built (`NCCL_HOME := ../../build` in the Makefile) and a real
  `$CUDA_HOME`. See the Inspector-plugin-provenance section below for the
  exact, currently-confirmed version story.
- **`iowait_agent.bt`** does not assume a specific kernel version, but
  does hardcode a jail PID-namespace nesting depth
  (`thread_pid->numbers[1].nr`) — see Category E.
- **`libibverbs-dev` (or equivalent RDMA dev package) + a C compiler** —
  found in this session's adversarial completeness pass, not the first
  inventory pass: `workloads/moe/qp_rate_limit_shim.c` `#include`s
  `infiniband/verbs.h` and is `LD_PRELOAD`ed by
  `workloads/moe/rank_fault_wrapper.sh` to real-RDMA-throttle exactly one
  target rank's queue pairs (`ibv_modify_qp_rate_limit()`, applied the
  instant that rank's QPs reach RTS) — a genuine RDMA-layer fault
  mechanism distinct from every other GPU-clock/software-sleep fault in
  this package. No build command for this file was found documented
  anywhere in this project's history; see `workloads/moe/README.md` for a
  reconstructed (not independently verified) one.
- **NumPy** — found missing from the requirements list in the first
  pass: every nanoGPT-family workload script (`nanogpt/`, `tp2/`, `fsdp/`,
  `moe/`, `rl/`) uses `numpy.memmap` to load its dataset. A real,
  load-bearing third-party dependency for those shapes, alongside PyTorch.

## Category E — Container/base-image audit

- **Local-disk-only I/O visibility** (a real, confirmed *limitation*, not
  a bug): block-layer tracepoints see local disk I/O only — confirmed live
  that this platform's own virtiofs jail root and its `/data` submount
  produce **zero** `block_rq_issue`/`block_rq_complete` events, and
  S3-backed access is pure network I/O with the same null result. Install
  docs must state plainly: the storage-classifier's Path C evidence only
  works for real, host-bind-mounted local-disk paths (matching this
  project's own `/tmp:/tmp` bind-mount convention in every launch script's
  `MOUNTS=` string) — not the container's own virtiofs root, not
  S3-backed paths.
- **PID-namespace nesting depth**: `iowait_agent.bt`'s
  `thread_pid->numbers[1].nr` hardcodes "level 1" as the correct jailed-PID
  namespace depth for *this* cluster's own one-level-deep jail
  architecture. A different container/jail nesting depth needs this
  index changed — **this won't silently fail, and won't silently
  self-correct either**; it needs an explicit, documented manual
  re-verification step on a new cluster, not an assumption either way.
- **`/sys-host` mount requirement**: a read-only bind of the real host's
  `/sys` is required because these containers/jails run in their own
  mount namespace without the host's tracing/bpf filesystems mounted at
  their standard paths — without it, bpftrace has nothing real to attach
  to. Confirmed re-verified across multiple nodes/clusters, held up
  unchanged.
- **Tracefs bind-mount wrapper** (`storage-ebpf/bpftrace-tracefs-wrapper.sh`):
  bind-mounts `/sys-host/kernel/tracing` → `/sys/kernel/tracing`
  per-invocation in a private mount namespace, since there's no reachable
  PID1/systemd inside this jail to install a persistent boot-time
  bind-mount. **Do not assume this fix is needed** — probe first (does
  `/sys/kernel/tracing` already work?) before applying it.
- **Possible libLLVM SONAME conflict**: only if the NVIDIA driver's
  bundled LLVM shadows bpftrace's required `libLLVM.so.18.1`. Real probe:
  `ldd $(readlink -f /usr/bin/bpftrace.real) | grep llvm`. Confirmed
  needed on **0 of 3 nodes** in this project's most recent migration — a
  conditional check, never applied unconditionally.
- **Node image inhomogeneity is a real, documented risk**: this project's
  own prior migration found one node in a fleet already had a stale,
  pre-existing Inspector `.so` baked into its system library path — do
  not assume every node in a target fleet starts from the same clean
  state; verify each node independently.
- **apt/IPv6 routing gotcha**: one node in a prior migration needed
  `apt-get -o Acquire::ForceIPv4=true install ...` due to a missing IPv6
  route; two other nodes in the same fleet needed no such flag — a
  conditional retry, not a blanket default.
- **"LD_LIBRARY_PATH fix" — CORRECTED in this session's adversarial pass;
  the first inventory pass's "could not be substantiated" finding was
  wrong, a real miss.** `workloads/long-context/run_longctx_node.sh:12`
  contains `export LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:${LD_LIBRARY_PATH:-}`
  — this explicitly forces the long-context workload to link against the
  custom-built NCCL 2.28.9 tree (the same one the Inspector plugin is
  built against) rather than whatever the container/host would otherwise
  resolve to via the standard library search path. This is the *only* one
  of the 15 workload launch scripts that does this — every other workload
  relies implicitly on the host-bind-mount-shadowing behavior documented
  in the Inspector-plugin-provenance section above (which lands on NCCL
  2.30.1, not 2.28.9) instead. **A real, disclosed inconsistency**: the
  long-context shape was validated against a different NCCL version than
  the other 14 shapes, and this line is why. Confirmed genuinely
  version-relevant, not vestigial: also confirmed absent from
  `workloads/resnet/`'s current scripts, where an earlier, since-removed
  copy once had it and a dedicated root-cause session (see the cuDNN/
  cuBLASLt entry below) explicitly tested removing it with no observed
  change for that specific workload — i.e. this override is workload-
  specific, not a blanket requirement, and must be evaluated per shape
  rather than assumed universal in Stage 2.

## Inspector plugin provenance and version (live-verified)

**1. NCCL version — three distinct versions genuinely coexist; the real
runtime-linked version is 2.30.1, not any version named in older project
docs**:
- The container base image (`nvcr.io#nvidia/pytorch:25.01-py3`) bundles
  NCCL **2.25.1** (confirmed: `python3 -c "import torch;
  print(torch.cuda.nccl.version())"` → `(2, 25, 1)`; `libnccl.so.2.25.1`
  present under `/usr`).
- A custom-built tree at the Inspector-plugin build location produces
  **2.28.9** (`strings libnccl.so | grep -m1 "NCCL version"` → `NCCL
  version 2.28.9 compiled with CUDA 12.8`) — used only to build the
  plugin against, not as the runtime core library.
- The version **actually linked at runtime for real training jobs is
  2.30.1** (package `2.30.3-1`) — confirmed directly from a real NCCL
  error line in a live training log: `"...NCCL version 2.30.1"`.
  Root-caused: every launch script's `MOUNTS` variable bind-mounts the
  **host's** `/usr/lib/x86_64-linux-gnu` over the container's own copy,
  and the host has NCCL 2.30.1 (`.deb` package `2.30.3-1+cuda13.2`)
  installed system-wide — this silently shadows the container's bundled
  2.25.1. **A new cluster's host must have a compatible NCCL installed
  system-wide, or this bind-mount removed/adjusted**, or the version
  actually exercised will silently differ from what was validated here.

**2. Confirmed genuine NVIDIA upstream source, not a fork**: the
Inspector plugin's own source tree has `git remote -v` →
`origin https://github.com/NVIDIA/nccl.git`, with real upstream commits in
its log (`NCCL 2.28.9-1`, `NCCL 2.28.7-1`, etc.). This project's own prior
investigation (`inspector_vs_comma.md`) independently names this exact
tree as the correct one versus a separate fork vendored elsewhere for
unrelated analysis purposes. Independently re-verified in the live source
that the fork's specific known bug (silently returning success on a
communicator-init failure) is **not** present here: on
`inspectorGlobalInit` failure, the plugin explicitly `return
ncclInternalError;` (not `ncclSuccess`).

**3. Both required patches confirmed present in the current source**:
- **`ncclProfileP2p` visibility patch** — live code requests
  `ncclProfileColl | ncclProfileP2p | ncclProfileKernelCh` in the
  activation mask, with an adjacent comment explaining it was added after
  a live probe (4000 real `all_to_all`/`all_to_all_single` calls) found
  zero records were produced for point-to-point traffic without it.
- **Deferred-free retirement queue fix** — a full, bounded-capacity FIFO
  retirement queue replacing immediate `free()` on completed collective
  info structs, with a comment documenting the real use-after-free root
  cause it fixes (a late NCCL profiler callback racing a freed struct,
  reproduced live under a DLRM workload's real timing at ~6000
  iterations). This is the same fix item independently confirmed via
  Category G's fix cross-reference below.

**4. Confirmed built from source, current, not a stale precompiled
binary**: a real Makefile drives the build (`g++`, proper `-soname`
linking); the last-built `.so` artifact's timestamp is newer than every
`.cc` source file it's built from, confirming a genuine, current
build-from-source workflow at the time of this audit — not a stale
binary left over from an earlier source change.

**5. Confirmed current production launch config is genuinely lean mode**:
the currently-used V1 Beta launch scripts set `NCCL_INSPECTOR_DUMP_VERBOSE=0`
(lean); several earlier-phase, no-longer-current per-workload scripts
still set `=1` (verbose) and are not part of the current production
configuration. Lean mode's core field, `coll_exec_time_us`, is confirmed
as the field `node_aggregator_ref.py` actually reads.

## Category F — Fault-injection workload shape audit (all 15 located)

| Shape | Directory | Data dependency | Notes |
|---|---|---|---|
| nanoGPT | `workloads/nanogpt/` | Shakespeare-char (shared) | Base for tp2/tp4/fsdp/rl |
| ResNet | `workloads/resnet/` | Synthetic (ResNet18, DDP) | |
| TP2 | `workloads/tp2/` | Shakespeare-char (shared) | |
| TP4 | `workloads/tp4/` | Shakespeare-char (shared) | Shares `tp2/train.py` + `tp2/train_node_tp.sh`, launched with `TP_SIZE=4` |
| FSDP | `workloads/fsdp/` | Shakespeare-char (shared, via symlink on the original host) | |
| MoE | `workloads/moe/` | Shakespeare-char (shared, via symlink on the original host) | Own `model_moe.py`; also its own `configurator.py` (`exec(open(...))`-loaded, not importable), `rank_fault_wrapper.sh`, and `qp_rate_limit_shim.c` (a real RDMA-layer fault mechanism) — all 3 found missing from the first inventory pass, added in this session's adversarial completeness check, see `workloads/moe/README.md` |
| ViT | `workloads/vit/` | Synthetic (`torch.randn`/`torch.randint`) | |
| TP-inference | `workloads/tp-inference/` | Synthetic | Own `model.py` |
| plain PP | `workloads/pp/` | Synthetic | |
| Hybrid (TP+PP) | `workloads/hybrid/` | Synthetic | |
| long-context | `workloads/long-context/` | Synthetic | |
| diffusion | `workloads/diffusion/` | Synthetic | |
| DLRM | `workloads/dlrm/` | Synthetic sparse ids | Also has a `_noinspector` launch variant |
| multi-modal (VLM) | `workloads/multi-modal/` | Synthetic | |
| RL | `workloads/rl/` | Shakespeare-char, via a **hardcoded absolute path** into the original nanoGPT directory, not the shared copy — see Category C | Shares base `model.py`/`configurator.py` too, via the same hardcoded import |

nanogpt/tp2/fsdp share an identical, unmodified base `model.py` +
`configurator.py` (confirmed via direct diff) — vendored once under
`workloads/nanogpt-base/` rather than 3 duplicate copies. RL's own
`train_rl.py` imports the same base model directly from the *original*
project layout via a hardcoded absolute path (not a duplicate file, and
not yet updated to use the shared copy in this package — flagged in the
top README).

The Shakespeare-char dataset's ultimate source is a live download in its
own `prepare.py` (`raw.githubusercontent.com/karpathy/char-rnn`), but the
already-generated `train.bin`/`val.bin`/`meta.pkl` (~2.2MB) are vendored
directly under `workloads/shared-data/shakespeare_char/` to avoid
requiring internet egress on a new, potentially air-gapped cluster.

## Category G — Cross-reference against fix history (final patched files
confirmed present, not stale pre-fix copies)

| Fix | Status | Evidence |
|---|---|---|
| P27.2 MAD-based fix | Confirmed | `alerting/alert_engine.py`: `TIMING_FALLBACK_MAD_MULTIPLE = 6`, used in the elevated-member gate |
| MoE signature/throughput fixes | Confirmed | `aggregator/node_aggregator_ref.py`: `workload_signature()`, `agg_job_throughput_rate_ref` query, self-calibration fallback |
| nv-hostengine monitoring | Confirmed | `alerting/alert_engine.py`: explicit nv-hostengine dead-man's-switch |
| FSDP grace period | Confirmed | `aggregator/node_aggregator_ref.py`: `BUCKET_MATURITY_GRACE_S = 120.0`, gated on it |
| PP's fixes | Confirmed (as a set) | Topology-agnostic member discovery, a documented Send/Recv tie-break bugfix, cross-node bucket union — all in `alert_engine.py` |
| Hybrid's fixes | Confirmed (as a set) | Multiple explicitly-tagged hotfix comments discussing hybrid TP+PP scenarios in `alert_engine.py` |
| Inspector plugin crash fix | Confirmed, strong evidence | `inspector-plugin/inspector_plugin.cc`: detailed use-after-free root-cause writeup + the deferred-free retirement queue (same fix as the provenance section above) |
| TP-inference cold-start fix | Confirmed | `aggregator/node_aggregator_ref.py`: explicit cold-start fallback comments for a workload signature with no cross-job history yet |
| cuDNN/cuBLASLt fix | **Confirmed present; root cause backfilled this session, and it is genuinely inconclusive — not a resolved mystery** | `torch.backends.cudnn.enabled = False` (diffusion/ViT/VLM) traces back to a 2-node SIGABRT (cuDNN+NCCL, multi-process) hit during an earlier ResNet debugging session (project phase P18i), worked around by disabling cuDNN. A dedicated later session (P18k, "fix the 2-node cuDNN crash properly") tried to root-cause it instead of just keeping the workaround, and could **not** reproduce the crash under the exact original conditions (0/3 clean runs — cuDNN enabled, `benchmark=True`, full Inspector, full container mounts, an `LD_LIBRARY_PATH` override, `CUDA_MODULE_LOADING=EAGER`). That session systematically ruled out the container-mount overlay, the `LD_LIBRARY_PATH` override, and `CUDA_MODULE_LOADING=EAGER` as causes (removing each: no change). **The real root cause was never conclusively identified** — best unverified guess: transient state from rapid back-to-back `srun` job reuse on the same allocation while actively debugging, not a deterministic property of the code+config. The disable-cudnn setting was kept as a defensive default and propagated to later conv/attention-heavy workloads out of caution, not because the crash was proven to require it. Separately, and more load-bearing for anyone tuning ResNet: that same P18k session found Inspector's own profiling overhead dominates ResNet's iteration time by ~4.5x (85ms with Inspector vs. 19ms without vs. 7.57ms bare single-GPU cuDNN-accelerated compute) regardless of cuDNN state — the crash workaround turned out to be irrelevant to ResNet's real performance story. |
| SSH-parallelization/deadline fix | Confirmed | `alerting/alert_engine.py`: `ThreadPoolExecutor(max_workers=64)` |
| Storage-classifier timing fix | Confirmed, both halves | `anomaly_ts` threaded through `build_finding_for_alert`/`_emit`/both check call sites in `alert_engine.py`; `pid = int(pid)` type-coercion fix present in `classifier/storage_evidence.py` |
| Log rotation | Confirmed | `observability/iowait_logger.py`: `logging.handlers.RotatingFileHandler`; `observability/alert_engine_supervised.logrotate` present |
| Dashboard datasource fix | Confirmed | `observability/dashboards/provisioning/datasources/local.yaml`: `url: http://worker-0:8428` (the live production VM, not the dead pre-migration instance) |

12 of 13 fixes fully confirmed with direct code evidence; the cuDNN/
cuBLASLt item is confirmed present in code with a genuinely missing
documented rationale — reported as a real gap, not glossed over. Nothing
in this list could not be located at all.

## Stale/duplicated files found and excluded (Step 4)

- `inspector-plugin/inspector_plugin.cc.bak_p32`,
  `libnccl-profiler-inspector.so.bak_p22`,
  `libnccl-profiler-inspector.so.bak_p32_pre_fix` — stale pre-fix backups,
  same directory as the current source. **Excluded.**
- `train_fsdp.py.bak_20260921_045357`, `train_tp_inference.py.bak_20260921_045357`,
  `train_vit.py.bak_20260921_045357` — stale pre-fix backups of workload
  scripts already present in patched form. **Excluded.**
- `soperator-straggler-detection/straggler-vmsingle/` (this repo, carried
  forward from an earlier, never-merged branch) — a Kubernetes VMSingle
  Helm chart superseded by the plain-Slurm-job `vm_standalone` approach
  after being confirmed RBAC-blocked (zero in-pod Kubernetes permissions)
  on two separate clusters in a row. **Kept for historical reference,
  clearly marked deprecated via its own `DEPRECATED.md`**, not deleted (a
  future session with real kubeconfig access may still want the
  shared-Grafana integration it was built for) and not the default path.
- `migration_package/`'s own `.py` copies of this pipeline (see Category
  A) — stale relative to live code. **Excluded**; live code sourced
  directly instead. Its `.md` docs were current enough and **are**
  carried forward under `docs/`.
- Two files legitimately named `persistence.py`
  (`classifier/persistence.py` and `alerting/persistence.py`) — **not a
  duplicate requiring de-duplication**: genuinely different modules (the
  classifier's own internal persistence stage vs. the alert layer's
  independently-reimplemented, standalone persistence tracker, deliberately
  not importing from the aggregator's internal state so a bug there can't
  silently become alerting ground truth). Kept as two files, in two
  separate package directories, exactly as the live code already
  structures them.
