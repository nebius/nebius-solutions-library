# GPU straggler/fail-slow detection pipeline (V1 Beta package)

A real-time GPU straggler/fail-slow detection pipeline for Soperator
(Slurm-on-Kubernetes) clusters: an NCCL profiler plugin that records
per-collective timing on every rank, a node-side aggregator that turns
those records into peer-relative statistics, a classifier that
corroborates a slow rank against multiple independent evidence sources
(GPU clock/power, host CPU load, disk I/O wait) before naming a cause,
and an alert engine that fires CONFIRMED/PROBABLE/UNCONFIRMED findings
into a Grafana dashboard.

This package went through two stages: **Stage 1** was a structural
packaging pass — an exhaustive, code-verified inventory of every real
component this system depends on (see `INVENTORY.md`), plus four
completeness passes that found and closed real gaps the first pass
missed (see `STAGE2_HANDOFF.md`'s own history). **Stage 2** (this
version) replaced every hardcoded cluster-shape assumption `STAGE2_HANDOFF.md`
found with real, live discovery, and built `install.sh` — see its own
section below. `run.sh` (launching an actual workload/fault-injection
test end-to-end) is Stage 3, still out of scope.

## Layout

```
install.sh          Stage 2: brings a fresh cluster to a ready-to-run
                    state using only real, live discovery -- see its own
                    section below
lib/                 cluster_topology.sh: real, live node/rank/GPU-count/
                    rendezvous-host discovery, sourced by every launch
                    script instead of each hardcoding this project's
                    original 2-node/8-GPU shape
classifier/        Stage 1-4 detection logic (rolling per-rank cause-metric
                    sampling, calibration, detection, classification,
                    storage-evidence corroboration, report formatting)
aggregator/         Node-side aggregator: turns raw Inspector records into
                    peer-relative mean/CV statistics and pushes them as
                    Prometheus-format metrics
alerting/           Alert engine: persistence-gated firing, coverage/
                    trust modifiers, DCGM/timing 2-member fallback,
                    role-baseline exclusion, storage-path verdicts, plus
                    a standalone (not auto-wired) RAS fail-stop watcher
observability/      iowait logger (persists eBPF io-wait samples for the
                    storage classifier), the supervisor script + log-
                    rotation config, and the Grafana dashboard + its
                    provisioning config
inspector-plugin/   The NCCL Inspector profiler plugin source (NVIDIA
                    upstream ext-profiler/inspector, patched) — built
                    from source at install time, never shipped prebuilt
health-checks/      GPU/host health-check scripts used as corroborating
                    evidence sources by the classifier
storage-ebpf/       bpftrace-based per-PID io-wait sampler + the tracefs
                    bind-mount wrapper this jail environment needs, plus
                    the real disk-bound fault-injection scripts used to
                    validate it
host-fault-injection/ Real host-CPU-contention fault mechanism (all-core
                    burner + a fine-grained per-rank jitter injector) —
                    what this project's own aggregator-CPU-under-
                    contention validation actually reuses
vm-standalone/      The real, working metrics backend: a plain
                    VictoriaMetrics binary launched as an ordinary Slurm
                    job (no Kubernetes access needed), with its real,
                    load-bearing non-default config (0s dedup, 100y
                    retention) documented explicitly, not just "install
                    VictoriaMetrics"
tools/              Standalone validation utilities (pre-flight test-
                    duration check, offline persistence replay, PromQL
                    cross-check, dashboard-JSON portability validation
                    against the platform Helm chart's own contract) + a
                    real captured fixture used by one of them
workloads/          All 15 validated fault-injection workload shapes,
                    one directory each, plus a shared nanoGPT base
                    (model.py/configurator.py/LICENSE), a shared,
                    pre-built Shakespeare-char dataset used by several
                    of them, and nanogpt-longrun/ (the separate,
                    unpatched sustained/long-run validation harness)
moe-two-stage-detector/ A real, standalone 5-step diagnostic pipeline for
                    localizing a MoE straggler once job-wide detection
                    has already flagged one — arrival-order signal,
                    token-load check, telemetry wiring, and an isolated
                    pairwise diagnostic sweep. Found missing entirely in
                    a third completeness pass; not part of the always-on
                    alert loop
inspector-crash-repro/ The real, iterative reproduction harness that
                    root-caused the Inspector plugin's use-after-free
                    crash (the fix itself lives in inspector-plugin/;
                    this is the supporting evidence for why). Found
                    missing entirely in the same pass
docs/               This project's own accumulated history/reference
                    docs (carried forward as-is — real prior investigation
                    notes, not rewritten), including TESTING_METHODOLOGY.md
                    (found missing in the third completeness pass — the
                    3 mandatory pre-flight/validation rules this project
                    holds every new-workload test to)
INVENTORY.md        The full Step-3 audit this layout is based on: every
                    Python dependency, every environment variable, every
                    hardcoded path/hostname, every external tool and
                    version constraint, NCCL Inspector plugin provenance,
                    and cross-reference against this project's fix history
STAGE2_HANDOFF.md   Every hardcoded-cluster-shape assumption found across
                    both the original inventory and a later adversarial
                    completeness pass, consolidated into one explicit
                    list with a proposed (not implemented) generic fix
                    for each — the direct input for Stage 2
```

## Requirements (verified, not assumed)

- **Python**: pure standard library for the entire detection/alerting/
  classification pipeline — **zero third-party packages required**
  (verified by tracing every `import` in every file under `classifier/`,
  `aggregator/`, `alerting/`; see INVENTORY.md Category A). Workload
  scripts under `workloads/` separately require **PyTorch, NumPy** (every
  nanoGPT-family shape — `nanogpt/`, `tp2/`, `fsdp/`, `moe/`, `rl/` — uses
  `numpy.memmap` to load its dataset; found missing from this list in this
  session's adversarial completeness pass) **, and torchvision** for the
  ResNet/ViT/VLM shapes — a real GPU training dependency, unrelated to the
  detection pipeline itself.
- **External tools**: `bpftrace` (confirmed working at 0.20.2-1ubuntu4.3),
  `nvidia-smi`, `dcgmi`, `ssh`, `logrotate`, Slurm (`squeue`/`srun`/
  `scontrol`), a C++ compiler + CUDA toolkit + a full NCCL source build to
  build the Inspector plugin, and a C compiler + **`libibverbs-dev`** (or
  the equivalent RDMA development package) to build
  `workloads/moe/qp_rate_limit_shim.c` — a real RDMA-layer fault-injection
  mechanism found missing from the first inventory pass; see
  `workloads/moe/README.md` for the reconstructed (not yet independently
  verified) build command. See INVENTORY.md Category D for exact version
  evidence and where it came from.
- **NCCL — read this before assuming a version.** This cluster's real,
  currently-linked NCCL version is **2.30.1** (package `2.30.3-1`), not the
  container's own bundled 2.25.1 and not the 2.28.9 build tree the
  Inspector plugin compiles against — the launch scripts' host bind-mount
  of `/usr/lib/x86_64-linux-gnu` silently shadows the container's bundled
  library with whatever the *host* has installed system-wide. **A new
  cluster must have a compatible NCCL installed on the host**, or this
  mount removed/adjusted — otherwise the version actually exercised in
  testing will silently differ from what runs on the new cluster. Full
  evidence in INVENTORY.md's Inspector-plugin-provenance section.

## Known limitations — required fixes before this is genuinely portable

This package is a direct copy of code validated on **one specific 2-node,
8-GPU-per-node cluster** (`worker-0`, `worker-1`). It is **not yet
cluster-shape-agnostic**. Every item below is real, found by tracing the
actual code (not inferred), and is required work for the next stage
(`install.sh`/`run.sh`), not fixed in this packaging pass per this
session's own scope constraint. **`STAGE2_HANDOFF.md` consolidates every
hardcoded-cluster-shape assumption below (plus a couple more found in a
later adversarial completeness pass) into one explicit, actionable list
with a proposed generic fix for each — start there for Stage 2, not here.**

1. **Every workload launch script hardcodes the 2-node/8-GPU topology
   literally** — `srun --nodes=2 --ntasks=2 --ntasks-per-node=1
   --gpus-per-node=8 -w worker-0,worker-1` (or `torchrun --nnodes=2
   --nproc_per_node=8`) appears verbatim in all 15 `workloads/*/run_*.sh`
   scripts, and every `train_node_*.sh` companion script hardcodes a
   literal `if [ "$(hostname)" = "worker-0" ]; then RANK=0; else RANK=1;
   fi` two-way branch (a third node would silently get `RANK=1`, wrong,
   with no path to `RANK=2+`). The rendezvous endpoint is also a literal
   `worker-0:$PORT` string. This is the single largest blocker to the
   "drop onto a different cluster shape" goal, and it is concentrated
   entirely in the shell launch layer — the Python core underneath
   (`node_aggregator_ref.py`, `alert_engine.py`, `cause_metrics.py`) is
   *mostly* node-count-agnostic (dynamic hostname discovery,
   `discover_gpu_count()` via live `nvidia-smi -L`, etc. — see
   INVENTORY.md Category C for the specific evidence separating what's
   already dynamic from what isn't). **One confirmed, live exception,
   found in a later adversarial pass**:
   `classifier/detection.py`'s `score_node_scoped_per_node()` hardcodes
   `NODE_A = range(0,8)`/`NODE_B = range(8,16)` mapped to the literal
   strings `"worker-0"`/`"worker-1"`, and is called unconditionally on
   every real per-communicator scoring pass — silently, not with a
   crash, unlike the shell-script hardcoding. See `STAGE2_HANDOFF.md`
   item 8 for the full detail and proposed fix. This was found by
   tracing one specific historical fix session's real file dependencies,
   not by an exhaustive line-by-line audit of the Python core — treat
   "mostly" above as an honest hedge, not a guarantee nothing else like
   it remains.
2. **Hardcoded absolute `sys.path.insert(...)` imports** — `alert_engine.py`
   inserts `/root/P18k_classifier` and `/root/P20c_alerting` literally;
   `node_aggregator_ref.py` inserts `/root/P19a_metrics` and
   `/root/P18k_classifier`; `workloads/rl/train_rl.py` inserts
   `/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler` and its
   `DATA_DIR` is a second hardcoded absolute path into that same sibling
   directory (the only workload not using a relative/symlinked dataset
   reference — every other shape that needs the Shakespeare-char dataset
   references it relatively); `host-fault-injection/train_node.sh` `cd`s
   into a hardcoded `/root/P4b_jitter/nanogpt_inject` for the same reason,
   even though the model it needs is byte-identical to the already-shared
   `workloads/nanogpt-base/`. These will not resolve against this new
   tree's layout as-is; the entry-point/installer needs to either set
   `PYTHONPATH` correctly at launch or these need converting to
   relative/package-style imports.
3. **`observability/dashboards/provisioning/datasources/local.yaml` hardcodes
   `url: http://worker-0:8428`** — the real, already-fixed production
   datasource URL for this cluster specifically (see `docs/` history: an
   earlier session found and fixed this pointing at a dead, pre-migration
   VictoriaMetrics instance). On a new cluster this must become the new
   cluster's own standalone VictoriaMetrics host — see `vm-standalone/README.md`
   for the real config that host needs to run, and
   `../straggler-vmsingle/DEPRECATED.md` for why a Kubernetes-native
   datasource was tried and abandoned (RBAC-blocked on two clusters in a
   row) in favor of a plain Slurm-launched binary. **Also found this
   session**: `observability/dashboards/provisioning/dashboards/local.yaml`
   hardcodes an absolute filesystem path (`path:
   /root/P20g_pr_ready/dashboards_dropin`) for where Grafana's file-based
   dashboard provisioner watches for the dashboard JSON — this must become
   wherever the dashboard actually lands on a new cluster's Grafana host,
   not assumed to be this exact path.
4. **A dead/vestigial environment variable**: every launch script sets
   `NCCL_INSPECTOR_PROM_DUMP=0`, but this variable is never read anywhere
   in the current Inspector plugin source (grepped exhaustively — see
   INVENTORY.md Category B) — harmless today, but worth removing from new
   launch-script templates rather than perpetuating a no-op setting.
5. **Runtime-environment assumptions that must be auto-detected, not
   assumed present or absent**, each with a real, already-documented probe
   command (see INVENTORY.md Category E for the exact commands and the
   real evidence behind each): the `/tmp` tmpfs-vs-real-disk-bind-mount
   requirement for the storage classifier's fault mechanism; the jail's
   PID-namespace nesting depth hardcoded into `iowait_agent.bt`
   (`thread_pid->numbers[1].nr` — confirmed correct for *this* jail's own
   one-level nesting, not something that silently self-corrects on a
   different container architecture); the tracefs bind-mount wrapper
   (`storage-ebpf/bpftrace-tracefs-wrapper.sh`); a possible libLLVM SONAME
   conflict with the NVIDIA driver's own bundled LLVM (confirmed needed on
   0 of 3 nodes in this project's own last migration — must be probed, not
   applied unconditionally); and possible node-to-node image
   inhomogeneity within the same fleet (a stale, pre-existing Inspector
   `.so` was found already sitting in one node's system library path in
   this project's own prior migration).
6. **`workloads/long-context/run_longctx_node.sh` and
   `workloads/rl/train_node_rl.sh` both set
   `LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:...` explicitly** —
   found in this session's adversarial completeness pass; an earlier pass
   incorrectly reported this as "could not be substantiated" (a real miss,
   corrected here), and a later pass found RL does it too (`train_node_rl.sh`
   itself was found entirely missing from the package and added in that
   same pass — see `STAGE2_HANDOFF.md` item 6). These are the *only 2 of
   15* workload scripts that force linking against the Inspector-plugin
   build tree's own NCCL 2.28.9, rather than relying on the
   host-bind-mount-shadowing behavior (item in the NCCL-version note
   above) that lands the other 13 shapes on 2.30.1 instead — a real,
   disclosed version inconsistency across shapes (see `STAGE2_HANDOFF.md`
   item 9), not something to silently standardize away without checking
   whether each shape's own validation depended on its specific version.
7. **cuDNN/cuBLASLt fix — the root cause was genuinely, honestly never
   resolved, not just undocumented.** Traced this session to its real
   originating session (an earlier ResNet debugging phase hit a 2-node
   SIGABRT with cuDNN+NCCL under multi-process load; a later, dedicated
   session tried to root-cause it properly instead of keeping the
   workaround, and could not reproduce the crash at all under the exact
   original conditions — 0/3 attempts, with the container-mount overlay,
   an `LD_LIBRARY_PATH` override, and `CUDA_MODULE_LOADING=EAGER` each
   individually ruled out as the cause). The disable-cudnn setting was
   kept as a defensive default, not because the crash was ever proven to
   require it. See INVENTORY.md's Category G table for the full backfilled
   writeup, including that same session's separate, more load-bearing
   finding: Inspector's own profiling overhead dominates ResNet's
   iteration time regardless of cuDNN state.

## Stage 2: `install.sh`

`install.sh` brings a fresh cluster from nothing to a ready-to-run state
using only real, live discovery — no hardcoded node count, names, or
GPU-per-node assumption anywhere. It:

1. **Discovers the real cluster shape** — every node (`sinfo`), the real
   per-node GPU count (`scontrol show node`'s own live `Gres` field,
   checked per-node, not assumed uniform — warns and uses the minimum if
   the fleet genuinely isn't uniform).
2. **Detects the real NCCL/CUDA/compiler state per node** — reports the
   host's own real, currently-installed NCCL library explicitly (the
   version-shadowing risk documented in `INVENTORY.md`'s Inspector-plugin-
   provenance section), rather than assuming any particular version.
3. **Detects real `/tmp` filesystem type per node** (the storage
   classifier's fault mechanism needs a real local-disk `/tmp`, not
   tmpfs — checked on the host, which is what actually gets bind-mounted
   into the training containers).
4. **Checks and fixes bpftrace/tracefs per node** — installs bpftrace if
   missing; probes whether a real tracepoint can already be attached
   directly, and only installs the tracefs bind-mount wrapper
   (`storage-ebpf/bpftrace-tracefs-wrapper.sh`) where that probe actually
   fails, never unconditionally; checks for (but does not blindly
   "fix") a possible libLLVM SONAME conflict.
5. **Installs `libibverbs-dev`** per node if missing (MoE's RDMA fault
   shim's build dependency).
6. **Builds the Inspector plugin from source** (NVIDIA's own upstream
   tree, confirmed patches already applied) and **MoE's RDMA fault shim**
   from source.
7. **Detects a real, reachable VictoriaMetrics instance** (probes the
   conventional `http://<first-node>:8428/health` live) or reports
   precisely that one needs to be launched per `vm-standalone/README.md`
   — it does not launch one itself (that needs a real binary staged on a
   node and a real Slurm allocation, a substantially different kind of
   step than everything else here).
8. **Generates real, templated Grafana provisioning configs**
   (`var/grafana_provisioning_generated/`) with the real discovered
   `VM_URL` and this package's real installed path substituted in.
9. **Writes `cluster.env`** — the single source of real, discovered truth
   every launch script reads (via `lib/cluster_topology.sh`) instead of
   hardcoding.

Fails loudly and specifically on any genuinely missing prerequisite
(exits non-zero with a real, specific `FATAL:` message) rather than
silently proceeding with a guessed value.

### Real, live validation performed

Run against this project's own real 2-node H200 cluster. **Real values
detected, live** (not simulated): `NODE_LIST=worker-0,worker-1`,
`NUM_NODES=2`, `GPUS_PER_NODE=8` (from `scontrol`'s own live `Gres=gpu:
nvidia_h200:8` field on each node), host NCCL library
`libnccl.so.2.30.1` on both nodes, `/tmp` = real `ext4` on both nodes,
bpftrace already able to attach real tracepoints directly on both nodes
(no wrapper install needed), `libibverbs-dev` missing on worker-0 only
(installed live), a real, reachable VictoriaMetrics instance found at
`http://worker-0:8428`.

**Two real, live failures were caught and fixed during this validation,
not hidden**:
- The Inspector plugin build failed the first real run:
  `profiler.h`/`common.h` not found. Root cause: the packaged
  `inspector-plugin/` was missing an entire `nccl/` subdirectory (9 real
  NVIDIA public profiler-API headers) that the plugin's own Makefile
  requires via `-Inccl` — a real gap in the original Stage 1 packaging,
  found only because this was an actual build, not a file-presence
  check. Fixed by copying that directory in. A second, related bug in
  `install.sh` itself was also found and fixed in the same run: `make`'s
  own `NCCL_HOME := ../../build` (a plain `:=` assignment) is not
  overridden by an environment variable of the same name — only by a
  real command-line `make NCCL_HOME=...` override, which `install.sh`
  now uses.
- A full, real, live 2-node training job launched through one of the
  now-fixed `run_*.sh` scripts (`workloads/nanogpt/run_shape1_nomonitor.sh`,
  no fault injection) failed twice, for two more real, genuine reasons,
  each fixed in turn: (1) `scontrol` (a Slurm client binary) is present
  on the bare host but **not inside the training container** these
  scripts actually exec into — `lib/cluster_topology.sh`'s
  `cluster_topology_job_nodes` now falls back to the real node list
  `install.sh` already discovered (`cluster.env`) when `scontrol` isn't
  found, rather than assuming it's always available; (2) the
  nanoGPT-family `train.py`/`train_fsdp.py`/`train_moe.py` scripts'
  own `data_dir` was still a bare, CWD-relative `'data/<dataset>'` string
  — now resolved relative to each script's real location, against the
  package's actual shared `workloads/shared-data/` directory. **After
  both fixes, the exact same launch command completed cleanly on both
  nodes** (`TORCHRUN_EXIT: 0`), with real training loss decreasing
  (4.31 → 2.68 over 20 iterations) — confirmed via the job's own real
  log output, not assumed.

`[CHECK-FAILED]` count in the standing, already-running production
`alert_engine.py` process throughout this entire validation: **0**
(confirmed before and after every live step — this validation never
touched or restarted that process; it ran entirely separate test jobs
against the same cluster).

## Explicitly excluded from this package (and why)

- `nccl-2.28-src/ext-profiler/inspector/*.bak_p22`, `*.bak_p32_pre_fix`,
  and the currently-compiled `libnccl-profiler-inspector.so` — stale
  pre-fix backups and a build artifact; the plugin is built from source at
  install time, per this project's own established convention (the exact
  same NCCL source tree must produce both the plugin and the library it
  patches into, to avoid version drift).
- `workloads/*.py.bak_20260921_045357` (3 files: FSDP, TP-inference, ViT
  train scripts) — stale pre-fix backups of files already present in
  their patched form.
- Ad-hoc, one-off debugging scripts with hardcoded `worker-0`/`worker-1`
  and no reuse value beyond the single investigation they were written
  for: `_diag_hybrid.py`, `_diag_persist.py`/`_diag_persist2.py`/
  `_diag_persist3.py`, `run_ras_faultstop_test.py`.
- `node_aggregator_with_shim.py` — a wrapper (adds local JSONL logging as
  a redundant safety net) around `aggregator/node_aggregator_ref.py`;
  confirmed via every real launch command across this project's history
  that production always invokes `node_aggregator_ref.py` directly, never
  the shim. **Correction from this session's re-verification**: this was
  not pure dead scratch — project docs describe it actually being used
  during one specific validation session, for redundant full-resolution
  local logging while debugging. Still correctly excluded from the
  production package (never part of the standard launch path), but
  reported accurately as "a real debugging aid used once," not "never
  used."
- Any `migration_package/` copy of this pipeline's own code — confirmed
  via direct diff to be **stale** relative to the live code (missing the
  storage-classifier wiring, the log-rotation fix, and several other
  fixes — see INVENTORY.md Category A). The prose docs under
  `migration_package/*.md` were still current enough to carry forward as
  `docs/`; the *code* copies were not, and are not part of this package.
- The `runs/` historical test-output subdirectory under the original
  `health/` tooling location (JSON/CSV logs from past test campaigns on
  the old cluster) — only the 4 real, reusable script files were copied.

## `ras_alert.py` — included, but not currently wired in

`alerting/ras_alert.py` is a real, independently validated RAS-based
fail-stop watcher (a different failure class from the fail-slow
classifier: "is this rank still alive at all", not "is it measurably
slower than its peers"). It is **not imported or invoked by
`alert_engine.py`** in the current live pipeline (confirmed: zero `import
ras_alert` anywhere in `alert_engine.py`) — it's a standalone tool, run
separately, not part of the main alerting loop. Included here because it's
real and validated, not because it's already integrated.
