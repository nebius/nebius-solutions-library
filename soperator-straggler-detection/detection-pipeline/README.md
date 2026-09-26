# GPU straggler/fail-slow detection pipeline (V1 Beta)

A real-time GPU straggler/fail-slow detection pipeline for Soperator
(Slurm-on-Kubernetes) clusters. This document is the install/run/
understand guide for someone with **zero prior context** on this
project. It carries forward every real gotcha this project's own
history found — not just the happy path.

- **Packaging history**: Stage 1 was a structural inventory (`INVENTORY.md`).
  Stage 2 built `install.sh` and closed every hardcoded-cluster-shape
  assumption Stage 1 found (`STAGE2_HANDOFF.md`, now **11/11 resolved**).
  Stage 3 built `run.sh` + `tools/self_test.sh` and closed a live Grafana
  anonymous-access gap. This document (Stage 4) is the user-facing guide
  tying all of that together.

## 1. Architecture — what this system does and how the pieces fit

```
 [training job, any real workload]
        |  NCCL collectives (AllReduce/AllToAll/Send-Recv/...)
        v
 [Inspector plugin]   <- NCCL profiler plugin, NVIDIA's own upstream
   per-rank, per-      ext-profiler/inspector source tree, patched
   collective raw       (gpu_slot_index added) and built from source at
   timing records        install time -- see inspector-plugin/
   (JSON, one file
   per real PID)
        | written to a real, per-node dump directory
        v
 [node_aggregator_ref.py]   <- one process per real node (aggregator/)
   reads the raw dump files, groups records by real communicator +
   rank, and turns them into PEER-RELATIVE windowed statistics (mean
   and coefficient-of-variation, per node, per communicator, per
   collective bucket) -- this is what actually makes a "this rank is
   slow" call meaningful: never an absolute threshold, always relative
   to that rank's own real peers in the same communicator, same job.
   Pushes these as real Prometheus-format metrics to VictoriaMetrics.
   This is also where the real, measured VOLUME-REDUCTION happens: raw
   per-collective records (many per second, per rank) are reduced down
   to one pushed sample per (comm, bucket, statistic) per window close
   -- the aggregator is the layer that makes this pipeline's own metrics
   volume tractable to store/query at all, not the raw Inspector stream
   itself.
        v
 [VictoriaMetrics]   <- the real metrics backend (a plain binary, no
                        Kubernetes needed -- see "Installing" below).
                        0s dedup is the single most load-bearing
                        non-default setting in this whole pipeline.
        v
 [alert_engine.py]   <- the alert engine (alerting/), polls
   VictoriaMetrics on its own cadence and applies a TIERED confidence
   model, not a single fire/no-fire threshold:
     - CONFIRMED  -- a real cause (DCGM clock/power suppression, a real
                     host-load spike, real disk-bound io-wait) was
                     independently corroborated alongside the timing
                     anomaly. Paged.
     - PROBABLE   -- the timing anomaly is real and persistent (gated
                     by a real persistence window, not a single noisy
                     sample), but no independent cause could be
                     confirmed. Logged, not paged.
     - UNCONFIRMED -- pattern detected, cause genuinely undeterminable
                     from every check this pipeline knows how to run --
                     surfaced for manual review, not silently dropped.
   This tiering exists because "the timing looks wrong" and "here's
   independently-confirmed evidence of why" are different claims with
   different real false-positive costs -- paging on the first alone,
   for a fleet this size, would be too noisy to trust.
        v
 [classifier/ + cause_metrics.py]   <- cause-evidence gathering, called
   by the alert engine before it decides a tier: DCGM (clocks, power,
   thermal, ECC, PCIe replay, NVLink), host CPU load, and real eBPF
   disk io-wait evidence (bpftrace-sampled, per real PID). See "Known
   limitations" below for exactly which of these are live-validated
   against a real fault vs. built-and-reasoned-but-never-fired.
        v
 [Grafana dashboard]   <- observability/dashboards/ -- real per-node/
   per-comm timing panels, provisioned against the real VictoriaMetrics
   datasource install.sh discovers/generates config for. See "Grafana
   access" below for both real access paths and the now-enforced
   real authentication.
```

### 1.1 What this system does and does NOT do yet (read this before anything else)

**Supported, production-validated V1 scope**: sustained/"sticky" compute
stragglers (a rank that is measurably, persistently slower than its real
peers in the same communicator — the core mean/CV detection path), host/
CPU-contention stragglers (a rank slowed by real host-level CPU
contention, not GPU-side), and storage/io-wait stragglers (a rank
genuinely blocked on real disk-bound I/O, via the eBPF-sampled Path C
evidence). These are the fault classes this pipeline has been built,
calibrated, and repeatedly validated against real injected faults for.

**Real, planned, but NOT yet production-hardened for V1**: medium-
duration/transient stragglers (a fault that comes and goes rather than
persisting), jitter-shaped stragglers (short, repeating bursts rather
than a sustained slowdown), network-fabric stragglers (a genuinely
degraded NIC/switch/link, distinct from the NVLink-specific gap
documented below), and data-pipeline stragglers (a rank slow because its
own dataloader/preprocessing is slow, not because of compute, host, or
storage). These are real, intended future scope — **not** silently
covered by the current mean/CV/DCGM/host/storage detection paths above,
and not something this V1 Beta package claims to catch. See "Known
limitations" (Step 5 below) for the further, more granular caveats even
within the supported scope (NVLink, DCGM ECC/PCIe/thermal validation
status, Path A's own real history).

## 2. Layout

```
install.sh          Brings a fresh cluster to a ready-to-run state using
                    only real, live discovery -- see "Installing" below
run.sh               Launches the standing pipeline (aggregator per real
                    node, supervised alert_engine.py, a confirm-only
                    Grafana check) using only install.sh's real values
tools/self_test.sh   One-command real fault-injection proof that
                    detection+attribution actually work on THIS cluster
cluster.env          Generated by install.sh -- gitignored, real per-
                    cluster values, never hand-edited
lib/                 cluster_topology.sh: real, live node/rank/GPU-count/
                    rendezvous-host discovery, sourced by every launch
                    script instead of each hardcoding a fixed shape
classifier/        Cause-evidence classifier: calibration, node-scoped
                    detection, DCGM/host/storage-evidence corroboration,
                    report formatting
aggregator/         Node-side aggregator: turns raw Inspector records into
                    peer-relative mean/CV statistics and pushes them as
                    Prometheus-format metrics
alerting/           Alert engine: tiered CONFIRMED/PROBABLE/UNCONFIRMED
                    firing, persistence gating, DCGM/timing 2-member
                    fallback, role-baseline exclusion, storage-path
                    verdicts, plus a standalone (not auto-wired) RAS
                    fail-stop watcher
observability/      iowait logger, the alert_engine + aggregator
                    supervisor scripts and their log-rotation configs,
                    and the Grafana dashboard + its provisioning config
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
                    burner + a fine-grained per-rank jitter injector)
vm-standalone/      The real, working metrics backend: a plain
                    VictoriaMetrics binary launched as an ordinary Slurm
                    job (no Kubernetes access needed), with its real,
                    load-bearing non-default config (0s dedup, 100y
                    retention) documented explicitly
grafana-standalone/ The real, enforced-auth Grafana launch recipe
                    (anonymous access disabled, real generated admin
                    password) — same "plain binary, no Kubernetes"
                    pattern as vm-standalone/
tools/              Standalone validation utilities (pre-flight test-
                    duration check, offline persistence replay, PromQL
                    cross-check, dashboard-JSON portability validation)
                    plus self_test.sh and a real captured fixture
workloads/          All 15 validated fault-injection workload shapes —
                    see "Testing/validation reference" (Step 6) below
                    for every one, by name, individually
moe-two-stage-detector/ A standalone diagnostic pipeline for localizing
                    a MoE straggler once job-wide detection has already
                    flagged one — not part of the always-on alert loop
inspector-crash-repro/ The real reproduction harness that root-caused
                    the Inspector plugin's use-after-free crash
docs/               This project's own accumulated history/reference
                    docs (real prior investigation notes, carried
                    forward as-is)
INVENTORY.md        The full Stage-1 audit: every Python dependency,
                    every environment variable, every hardcoded path,
                    every external tool/version constraint
STAGE2_HANDOFF.md   Every hardcoded-cluster-shape assumption found —
                    **now 11/11 resolved**, kept as the historical record
```

## 3. Requirements (verified, not assumed)

- **Python**: pure standard library for the entire detection/alerting/
  classification pipeline — zero third-party packages required.
  Workload scripts under `workloads/` separately require **PyTorch,
  NumPy** (every nanoGPT-family shape uses `numpy.memmap`) and
  **torchvision** for the ResNet/ViT/VLM shapes — a real GPU training
  dependency, unrelated to the detection pipeline itself.
- **External tools**: `bpftrace` (confirmed working at
  `0.20.2-1ubuntu4.3`), `nvidia-smi`, `dcgmi`, `ssh`, `logrotate`, Slurm
  (`squeue`/`srun`/`scontrol`), a C++ compiler + CUDA toolkit + a full
  NCCL source build to build the Inspector plugin, and a C compiler +
  **`libibverbs-dev`** to build `workloads/moe/qp_rate_limit_shim.c`.
  `install.sh` checks for and installs what it can automatically — see
  "Installing" below.
- **NCCL — read this before assuming a version.** A launch script's
  `MOUNTS` bind-mounts the host's own `/usr/lib/x86_64-linux-gnu` into
  the training container, which silently **shadows** the container's
  own bundled NCCL with whatever the *host* has installed system-wide.
  `install.sh` detects and reports the real, currently-linked host NCCL
  version per node explicitly — it does not assume any particular
  version, and neither should you. Two workload scripts
  (`workloads/long-context/` and `workloads/rl/`) additionally force
  `LD_LIBRARY_PATH` to the Inspector-plugin build tree's own NCCL
  version regardless of the host — a real, disclosed version
  inconsistency across shapes (see `STAGE2_HANDOFF.md` item 9), not
  something to silently standardize away without checking whether that
  shape's own validation depended on its specific version.

## 4. Installing — `install.sh`

Run from the Slurm control/login node:

```bash
cd soperator-straggler-detection/detection-pipeline
./install.sh
```

It brings a fresh cluster from nothing to a ready-to-run state using
**only real, live discovery** — no hardcoded node count, names, or
GPU-per-node assumption anywhere — and fails loudly with a specific
`FATAL:` message (non-zero exit) on any genuinely missing prerequisite,
rather than silently proceeding with a guessed value. What it actually
does, in order:

1. **Discovers the real cluster shape** — every node (`sinfo`), the real
   per-node GPU count (`scontrol show node`'s own live `Gres` field,
   checked per node — warns and uses the minimum if the fleet genuinely
   isn't uniform).
2. **Detects the real NCCL/CUDA/compiler state per node** — reports each
   node's own real, currently-installed host NCCL library explicitly
   (see the version-shadowing note above), rather than assuming a
   version.
3. **Detects the real `/tmp` filesystem type per node** — the storage
   classifier's fault mechanism needs a real local-disk `/tmp`, not
   tmpfs; checked on the host, which is what actually gets bind-mounted
   into training containers.
4. **Checks and fixes bpftrace/tracefs per node** — installs `bpftrace`
   if missing; probes whether a real tracepoint can already be attached
   directly, and only installs the tracefs bind-mount wrapper
   (`storage-ebpf/bpftrace-tracefs-wrapper.sh`) where that probe
   actually fails, never unconditionally; checks for (but does not
   blindly "fix") a possible libLLVM SONAME conflict with the NVIDIA
   driver's own bundled LLVM.
5. **Installs `libibverbs-dev`** per node if missing (MoE's RDMA fault
   shim's build dependency).
6. **Builds the Inspector plugin from source** (NVIDIA's own upstream
   tree, confirmed patches already applied) and **MoE's RDMA fault
   shim** from source — never ships a precompiled binary, so the plugin
   and whatever NCCL it links against always come from the exact same
   real build.
7. **Detects a real, reachable VictoriaMetrics instance** (probes the
   conventional `http://<first-node>:8428/health` live) or reports
   precisely that one needs to be launched per `vm-standalone/README.md`
   — it does not launch one itself. **0s dedup
   (`-dedup.minScrapeInterval=0s`) is the single most load-bearing
   non-default setting in this entire pipeline** — VictoriaMetrics's
   normal 30s-ish default dedup window silently drops the vast majority
   of the high-frequency samples the CV detector's persistence window
   depends on. `-retentionPeriod=100y` is the other real, load-bearing
   one (this pipeline relies on real cross-job history persisting across
   many separate Slurm jobs run hours or days apart).
8. **Generates real, templated Grafana provisioning configs**
   (`var/grafana_provisioning_generated/`) with the real discovered
   `VM_URL` and this package's real installed path substituted in.
9. **Generates a real, enforced Grafana auth config** — anonymous access
   explicitly disabled, plus a real, randomly-generated admin password
   (never a hardcoded default), written to
   `var/grafana_admin_credentials.txt` (`chmod 600`). See "Grafana
   access" below for the full detail.
10. **Writes `cluster.env`** — the single source of real, discovered
    truth every launch script reads (via `lib/cluster_topology.sh`)
    instead of hardcoding.

Re-running `install.sh` is safe and idempotent — it reuses an already-
generated real Grafana admin password rather than invalidating an
already-established login, and every other step re-probes live rather
than assuming its own prior output is still true.

### 4.1 Real failure modes already found — troubleshooting reference

Every one of these was found by actually running `install.sh` for real
against a real cluster, not by static review — treat this list as the
troubleshooting reference for what to check first if `install.sh` fails
the same way:

- **`fatal error: profiler.h: No such file or directory` / `common.h:
  No such file or directory`** building the Inspector plugin — the
  plugin's Makefile needs NVIDIA's own public profiler-API headers
  (`nccl/common.h`, `profiler.h`, `profiler_net.h`, `profiler_v1-v5.h`,
  `types.h`) via `-Inccl`, copied into `inspector-plugin/nccl/`. Already
  fixed in this package; if you see this on a from-scratch NCCL source
  tree, copy `<nccl-src>/ext-profiler/inspector/nccl/*.h` into
  `inspector-plugin/nccl/`.
- **Inspector plugin silently builds against the wrong `NCCL_HOME`** —
  the plugin's own Makefile uses `NCCL_HOME := ../../build` (a plain
  `:=` assignment), which an environment variable of the same name does
  **not** override in `make`'s default mode — only a real command-line
  `make NCCL_HOME=...` override does. `install.sh` already uses the
  command-line form; if you're building manually, use
  `make NCCL_HOME=/path/to/nccl/build CUDA_HOME=/usr/local/cuda`, not
  `NCCL_HOME=... make`.
- **`scontrol: command not found` inside the training container** —
  `scontrol` (a Slurm client binary) is present on the bare host but not
  baked into the training container image. `lib/cluster_topology.sh`'s
  `cluster_topology_job_nodes` already falls back to `cluster.env`
  (written by `install.sh` on the host) when `scontrol` isn't found —
  if you see this fail anyway, confirm `cluster.env` exists and is
  readable from inside the container's own bind-mounted `/root`.
- **`FileNotFoundError: data/<dataset>/train.bin`** — an older bug where
  `data_dir` was computed as a bare, CWD-relative string; already fixed
  (resolved relative to each script's own real location against
  `workloads/shared-data/`). If you see this on a modified copy of a
  workload script, check it isn't reintroducing a CWD-relative path.
- **VictoriaMetrics ingestion returns HTTP 400** — `node_aggregator_ref.py`
  needs the real `/api/v1/import/prometheus` endpoint, not the bare
  `VM_URL` (`http://host:8428`) that `alert_engine.py`/health probes
  correctly use as-is. Already handled by
  `observability/run_aggregator_supervised.sh` (which appends the path
  itself) — if you invoke `node_aggregator_ref.py` directly, remember
  to append `/api/v1/import/prometheus` to whatever URL you pass it.
- **`hostname -f` resolves to a Kubernetes-internal-only DNS name**
  (ending `.svc.cluster.local`) — real on a Soperator login pod;
  `install.sh` detects this and falls back to the host's own real
  private IP for Grafana's own printed access host. See "Grafana
  access" below — this is not a bug, it reflects a deliberate "no
  public IP" access design.
- **An ssh-launched background process on a compute node never
  returns** — if you're scripting something similar yourself:
  `ssh node "cmd1 && cmd2 &"` backgrounds the *whole* `&&`-list as one
  job, so `cmd2`'s own stdio redirects don't fully detach it from the
  ssh channel until `cmd2` itself exits. Use `;` instead of `&&` before
  the final backgrounded command, and redirect its stdin
  (`</dev/null`) — this is exactly what `run.sh`'s own aggregator-launch
  code does.

## 5. Running it — `run.sh` + the self-test

```bash
./run.sh
```

Launches the full standing pipeline using **only** `cluster.env`'s real,
already-discovered values — it never re-implements node/environment
discovery (that's `install.sh`'s job; if a value it needs is missing,
`run.sh` reports it as a real `install.sh` gap and stops, rather than
guessing). It:

1. Launches `node_aggregator_ref.py` (via the supervised
   `observability/run_aggregator_supervised.sh`, with real auto-restart
   and log rotation) on every real discovered node.
2. Launches the supervised `alert_engine.py`
   (`observability/run_alert_engine_supervised.sh`, log rotation already
   configured).
3. Confirms Grafana is reachable internally per `install.sh`'s own real
   discovery — never launches or exposes anything externally by
   default.
4. **Real startup health confirmation, not just "the command didn't
   error"**: waits for a genuine `agg_aggregator_heartbeat` sample per
   node in VictoriaMetrics (proves the aggregator is really pushing
   data, not just that `ssh` succeeded), confirms `alert_engine.py` is
   actively cycling via real CPU-tick advancement in `/proc/<pid>/stat`
   (log-line growth alone is **not** a valid liveness signal here — a
   fully healthy cycle produces no new log output at all, since
   `alert_engine.py`'s own pipeline-health check only prints when
   something is DOWN), confirms VictoriaMetrics is reachable, and
   confirms zero new `[CHECK-FAILED]` entries since startup. If any
   check fails, `run.sh` reports exactly which one and why — never a
   generic "something went wrong" — and exits non-zero.
5. Prints real, ready-to-copy Grafana access commands (see below) —
   never a placeholder.

Safe to re-run: if the aggregator or `alert_engine.py` is already
running, `run.sh` detects this and leaves it alone rather than
double-launching.

### 5.1 Self-test — `tools/self_test.sh` (run this first)

**This is the recommended first thing to run after `run.sh`**, before
trusting the pipeline with a real workload. It's the one-command "does
this actually work on THIS cluster" proof, requiring no knowledge of
this project's own history to construct:

```bash
./tools/self_test.sh
```

It injects one real, software fault (`STRAGGLER_SLEEP_MS=200`,
`STRAGGLER_TARGET_RANKS=<a rank on the second real node>` — a validated,
portable injection mechanism, confirmed as `198.6ms` observed delta
against a `200ms` injection) into a real nanoGPT training job, then:

1. Establishes **ground truth** — reads the job's own real dump files to
   find the exact real PID that corresponds to the injected rank,
   *before* looking at any alert.
2. Polls the already-running pipeline (up to 600s — this project's own
   real calibration+grace-period timing, not a guess) for its own real
   `[ALERT]` line.
3. Requires an **exact match** between the alert's flagged identity and
   the real, injected identity established in step 1 — never just "an
   alert fired somewhere."
4. Cleans up the test job automatically on exit.

**What PASS/FAIL means**: `PASS` means the pipeline independently
detected the real injected fault AND correctly attributed it to the
exact real rank/PID/host it was injected on — the strongest single
confirmation available that this cluster's install is genuinely
working end to end. `FAIL` means either no alert appeared within the
wait window (the script then checks the aggregator's own real measured
collective cadence against `tools/preflight_duration_check.py`'s
formula and tells you honestly whether the test simply didn't run long
enough, rather than just declaring failure) or an alert appeared but
named the wrong rank/host — a real detection or attribution problem
worth investigating before trusting this install with production
monitoring.

## 6. Grafana access

`install.sh` never launches or manages a Grafana instance's lifecycle —
that's a real deployment decision left to the operator (same scope
boundary as VictoriaMetrics; see `vm-standalone/README.md`). It detects
whatever instance is reachable, generates real provisioning + auth
config for it, and `run.sh` prints exactly how to reach it. See
`grafana-standalone/README.md` for the full real launch recipe if you
need to stand one up.

### 6.1 Both real access paths

Kubernetes-based access, only printed by `run.sh` if a real Grafana
`Service` is actually found (this project's own real deployment history
found Kubernetes API access RBAC-blocked on every cluster tested so far
— see `../straggler-vmsingle/DEPRECATED.md` — so don't expect this path
to apply by default):

```bash
kubectl port-forward -n <real-namespace> svc/<real-service-name> <port>:<port>
# then open http://localhost:<port>
```

SSH tunnel through the bastion (the cluster's own login node — this is
the real, currently-applicable path on every cluster this project has
actually tested against):

```bash
ssh -L 3000:localhost:3000 -N <user>@<real-bastion-host>
# then open http://localhost:3000
```

`run.sh` prints both with **real, install.sh-discovered values already
filled in** — never placeholder text. If the bastion host's own
`hostname -f` resolves to a Kubernetes-internal DNS name (a real,
confirmed case on Soperator clusters), `install.sh` falls back to that
host's own real private IP instead and says so explicitly — there is
deliberately no public IP in this access design; reach that private
address via whatever VPN/bastion path your organization already uses to
reach this cluster's network.

**Teardown**: the tunnel/port-forward only exists while that command is
running in your terminal. Closing it (Ctrl-C, or closing the terminal)
removes access immediately — nothing is left listening on your machine
or on the cluster once it exits.

### 6.2 Real, enforced authentication (never anonymous)

Anonymous access is **disabled by default**. A real, randomly-generated
admin password (never a hardcoded default — `python3`'s own `secrets`
module, a fresh value per cluster) is created by `install.sh` at install
time and written to:

```
var/grafana_admin_credentials.txt      (chmod 600 -- restricted to this file's own owner)
```

Retrieve it with:

```bash
cat var/grafana_admin_credentials.txt
```

Log in as `admin_user=admin` with that real password. It is never
printed in plaintext to any shared log — `run.sh`'s own printed
instructions only ever reference this file's path, never its contents.
`install.sh` also live-verifies the auth posture of whatever instance is
currently reachable (an unauthenticated request to `/api/org` — 200
means anonymous access is wrongly enabled; 401/302 means real auth is
enforced) and reports the real result rather than assuming.

**A previously-found real gap, now closed**: Stage 3's own live testing
found a pre-existing, drifted dev Grafana instance with anonymous Admin
access enabled — confirmed via its own file mtime to predate this
packaging effort by three weeks, not something `install.sh`/`run.sh`
ever created. Every instance this package generates config for or helps
launch now has real auth enforced from first start; see
`grafana-standalone/README.md`'s own "Verifying real auth is enforced"
section for the exact live check to re-run any time you're unsure.

### 6.3 Sharing access with a coworker

This is a real access-grant decision, not a default anyone gets
automatically:

- **If they already have their own SSH access to this cluster** (their
  own account on the bastion/login node), they use their own existing
  credentials with the same tunnel command above — nothing further to
  grant.
- **If they don't**, the deliberate step is adding their real public SSH
  key to the bastion host's authorized keys (or your organization's own
  cluster-access provisioning process, if one exists) — this is a real
  decision about who can reach this cluster's private network at all,
  not something this package automates or should automate silently.
- **Grafana-level sharing**: everyone currently shares the single real
  `admin` login above (there is no per-user Grafana account
  provisioning in this V1 package). If you need per-user Grafana
  accounts with different permission levels, that's real, additional
  setup on top of what's documented here (Grafana's own user-management
  UI, once logged in as admin) — not something `install.sh` sets up for
  you.

## 7. Known limitations (read this before relying on any alert)

This section is **not softened**. It has two parts: which fault classes
this pipeline is validated for at all (repeated from Step 1 above, since
it's the most important single fact in this document), and — separately
— the real, honest validation-confidence status of specific detection
paths even within that supported scope.

**V1 scope, again, plainly**: sustained/compute, host/CPU, and storage
stragglers are the supported, production-validated V1 scope. Medium-
duration, jitter, network-fabric, and data-pipeline stragglers are real,
planned, but **not** production-hardened in this release — do not
assume they're silently covered.

**Overhead — the real, measured numbers, not a summary**:
- The one direct throughput-overhead measurement in this project's own
  history found Inspector's profiling overhead **dominating ResNet's
  iteration time by ~4.5x** (85ms with Inspector vs. 19ms without vs.
  7.57ms bare single-GPU compute) — it is **not documented** whether
  this was measured in the lean production launch config
  (`NCCL_INSPECTOR_DUMP_VERBOSE=0`, the default every launch script
  uses) or a more verbose debug mode. Treat per-workload overhead as a
  real open question to measure on your own workload, not as
  negligible-by-default. An A/B "monitoring-off" harness exists
  (`workloads/nanogpt/run_shape1_nomonitor.sh`) for exactly this
  comparison; no documented result from actually running it was found
  in this project's own history.
- **Real, escalating resource costs found during this project's own
  sustained/long-run testing — all now fixed by caps already shipped in
  this package, but the real historical numbers are worth knowing**:
  the aggregator's own per-process memory grew **unbounded from ~30MB to
  16.3GB RSS over ~50 minutes** under FSDP's higher event volume before
  a 500-entry rolling cap was added (`aggregator/node_aggregator_ref.py`);
  the supervised `alert_engine.py` log grew **31.8MB/165k lines with
  zero cap over ~3 real hours under heavy fault-injection load** before
  `logrotate` (50M/rotate 10) was added; the iowait logger's own log
  grew from a baseline **~592 rows/hour** to **~3.5 rows/sec during an
  active real storage fault** (~410 B/s worst case) before a 10MB
  rotating-handler cap was added. A fresh install already ships all
  three fixes — these numbers are what this project's own history found
  before they existed, not a current risk, but understand them before
  assuming "leave it running forever" is free on a workload this
  project hasn't already sustained-tested.
- Baseline alert rate under normal (non-fault) operation: two
  independently-measured figures exist in this project's own history —
  **~22-25/hour** and, from a separate 3-hour blind-test session,
  **~22-38/hour (settling around ~32/hour across three runs)** — both
  PROBABLE/LOG-ONLY, not paged.

**NVLink detection**: built and reasoned correctly (TP's traffic runs
exclusively over NVLink, so a real NVLink fault is currently invisible
to the network/IB check alone). **Never validated against a real fault
on any hardware tested** — three genuinely different real injection
approaches were tried (diagnostic tooling, fabric management tooling,
real engineered bandwidth contention) and none could produce one. This
is the first detector in this whole project that is correctly built but
has never fired against ground truth — an honestly different confidence
tier from everything else. NVLink also only has a live-query path, no
rolling-buffer sampler.

**DCGM causality items — ECC, PCIe replay**: both are surfaced as
supporting evidence, explicitly annotated at the code level as **"not
validated as a cause — worth a look"** whenever nonzero — never used to
independently drive a CONFIRMED tier on their own. Raw GPU/memory
temperature readings are not flagged this way (they feed Path A
directly, below).

**Path A (thermal cause-evidence)**: has fired exactly once against a
real fault in this project's history — this cluster's own long-
documented chronic hardware degradation (GPU3) — and correctly declined
to fire on two other cases with an elevated counter but genuinely normal
throughput (GPU4, one other rank). Status is explicitly **PROVISIONAL**:
calibration is thin (only 3 distinct GPUs have ever exercised this path
at all), not because it's unreliable when it does fire.

**XID hardware-fault path**: implemented but **PROVISIONAL and never
validated** — this cluster's own chronic hardware fault has never once
logged a real XID event in this project's entire history; every real
fault ever validated here has been a performance/counter signal, never
a driver-logged hardware-fault event.

**Host-load-ratio detection**: has **never once reached CONFIRMED** in
any test in this project's history.

**MoE single-rank localization**: peer-relative statistics **cannot**
localize a single-rank AllToAll fault to a specific rank — a real,
mechanistic, **permanent** architectural limitation (the waiting ranks
show the elevated timing, not the actually-delayed rank), not something
future tuning will close. Job-wide MoE detection works correctly; use
`moe-two-stage-detector/` (a standalone, offline tool, not part of the
always-on alert loop) to localize further once job-wide detection has
already flagged a MoE job.

**2-member communicator self-detection**: mathematically degenerate for
any exactly-2-member communicator (TP at `TP_SIZE=2`, TP-inference) —
peer-relative CV cannot compute at all below `SELF_DETECTION_FLOOR=3`
real members. Mitigated (not eliminated) by a DCGM-based fallback
(hardware-level clock/thermal suppression only) and a timing-asymmetry
fallback (the P27.2 mechanism, for a pure software fault) — but the
underlying statistical limit is permanent, not a bug to eventually
patch away.

**cuDNN/cuBLASLt disable workaround**: a real 2-node SIGABRT was hit
under cuDNN+NCCL multi-process load; a dedicated later session tried to
root-cause it properly and could not reproduce the crash at all under
the original conditions (0/3 attempts, with several individual causes
ruled out). The disable-cuDNN default was kept defensively, not because
the crash was ever proven to require it — genuinely, honestly
unresolved, not just undocumented.

**MoE RDMA fault shim's build command**: reconstructed from this
project's own history, **not yet independently re-verified** — see
`workloads/moe/README.md`.

**VMSingle Helm chart's "survives a restart, self-heals" claim**: only
ever validated at the storage layer (data survives a process restart).
The live-cluster mechanism (Kubernetes actually noticing and recreating
a dead pod) has **never been exercised** — needs a real cluster with
real kubeconfig access to confirm, which this project has not had on any
cluster tested so far (see `../straggler-vmsingle/DEPRECATED.md`).

## 8. Testing/validation reference — every workload shape, by name

**Start with `tools/self_test.sh`** (see Step 5 above) — it's the
fastest, recommended first validation on a new cluster. Everything below
is the deeper, comprehensive option: every one of the 15 workload shapes
and 6 parallelism strategies this pipeline has actually been validated
against, individually, with its own real launch script and how to run it
directly if you want to confirm a specific shape relevant to your own
real workloads. `STEPS`/`OUTDIR`/`PORT`/`DUMPDIR_BASE` below are always,
respectively: optimizer steps to run, a real output directory, a real
free TCP port, and a real dump-file base directory (use `var/dump` — the
same one `run.sh`'s own standing aggregator already watches — to see a
shape's real detection results live, the same way `tools/self_test.sh`
does).

### 8.1 The 15 validated workload shapes

1. **nanoGPT (DDP)** — single-comm data-parallel baseline; the project's
   own regression-sweep reference shape for "clean, exact rank matches."
   `bash workloads/nanogpt/run_straggler_nanogpt.sh 200 /tmp/out 29500 var/dump 200 3`
   (200ms sleep injected on rank 3; empty target-ranks = healthy
   baseline run).
2. **ResNet (DDP)** — vision/conv workload; the shape whose Inspector
   profiling overhead was directly measured (~4.5x iteration-time
   dominance, see Known limitations above).
   `bash workloads/resnet/run_resnet_p26.sh 200 /tmp/out 29500 var/dump`
   (fault injection is via `train_resnet.py`'s own env-gated jitter, not
   a positional arg).
3. **TP2 (tensor-parallel, 2-way)** — the below-self-detection-floor
   communicator shape: CV's own variance math is mathematically
   degenerate for an exactly-2-member communicator, a real structural
   limitation, not a bug (see Known limitations above).
   `bash workloads/tp2/run_tp_nanogpt.sh 200 /tmp/out 29500 var/dump`
   (script hardcodes `TP_SIZE=2` internally).
4. **TP4 (tensor-parallel, 4-way)** — the above-floor multi-member
   counterpart: at 3+ real members, self-detection works cleanly and
   directly, no fallback needed — confirming the TP2 gap is specific to
   the smallest possible TP configuration, not a general communicator
   problem.
   `bash workloads/tp4/run_tp4_nanogpt.sh 200 /tmp/out 29500 var/dump`.
5. **FSDP** — fully-sharded data-parallel; its own real, quantified
   detection floor: a moderate fault (~58-62% exec-time change) does not
   clear this shape's natural variance ceiling, while a severe fault
   (~75-82% change) does, cleanly and reliably.
   `bash workloads/fsdp/run_fsdp_nanogpt.sh 200 /tmp/out 29500 var/dump`.
6. **MoE** — Mixture-of-Experts, AllToAll traffic. Job-wide detection is
   real and validated (a confirmed 8.4x median AllToAll slowdown case);
   single-rank localization is a **permanent** architectural boundary
   (see Known limitations above), worked around by the standalone
   `moe-two-stage-detector/` tool. Four real variants:
   `bash workloads/moe/run_moe_nanogpt.sh 200 /tmp/out 29500 var/dump` (healthy baseline),
   `bash workloads/moe/run_moe_rankfault.sh 200 /tmp/out 29500 var/dump` (`FAULT_TARGET_RANK=4` env var — single-rank RDMA fault, confirmed permanently non-localizable at rank level),
   `bash workloads/moe/run_moe_netfault.sh 200 /tmp/out 29500 var/dump` (job-wide network fault),
   `bash workloads/moe/run_moe_delayfault.sh 200 /tmp/out 29500 var/dump` (`MOE_FAULT_TARGET_RANK`/`MOE_FAULT_DELAY_US` env vars — software delay fault).
7. **ViT (vision transformer)** — the workload with this project's own
   documented **highest power profile tested (365-402W)**.
   `bash workloads/vit/run_vit_p26.sh 200 /tmp/out 29500 var/dump`.
8. **TP-inference (2-member)** — inference-mode tensor-parallel (forward
   pass only, no backward/optimizer at all); triggers the P27.2 timing-
   asymmetry fallback (see "special mechanisms" below) for the same
   below-floor reason as TP2.
   `bash workloads/tp-inference/run_tpinf_p26.sh 200 /tmp/out 29500 var/dump "" 2`
   (note the empty 5th positional placeholder; `2` is `TP_SIZE_VAL`, the
   real 6th argument — a real numbering gap in the script itself, not a
   typo here).
9. **Plain PP (pipeline-parallel, hand-rolled 2-stage)** — a genuinely
   **cross-node** below-floor communicator (the two pipeline stages sit
   on two different physical nodes); its own legitimate stage-asymmetry
   (stage0's Recv is a real, structural ~2.2x stage1's Recv — not noise,
   not a fault) is handled by comparing each stage only against other
   jobs' own history for that same role, never against the other
   stage's current value directly. This shape's own launch script is
   the per-node payload itself (no separate dispatch wrapper) — run it
   inside your own `srun` allocation, reusing the same real
   image/mounts convention every other shape's dispatcher already uses:
   ```bash
   srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=<N> -w <real-nodelist> \
     --container-image="nvcr.io#nvidia/pytorch:25.01-py3" \
     --container-mounts="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp" \
     bash workloads/pp/run_pp_node.sh 200 /tmp/out 29500 var/dump
   ```
10. **Hybrid TP+PP** — combined parallelism strategies in one job (2
    ranks per node: a TP pair per PP stage); validated with real
    cross-comm fault-cascade findings (one stage's PP fault correctly
    propagating into the next stage's own TP AllReduce, correctly
    attributed). Also a per-node script — launch the same way as PP
    above, substituting `workloads/hybrid/run_hybrid_node.sh`.
11. **Long-context** — large/variable sequence-length stress. One of
    only 2 of the 15 shapes (with RL) that force a specific NCCL version
    via its own explicit `LD_LIBRARY_PATH`, rather than relying on the
    host-shadowing behavior the other 13 get (see the NCCL note in
    Requirements above). Per-node script:
    `workloads/long-context/run_longctx_node.sh`, launched the same way
    as PP above.
12. **Diffusion** — conv+attention hybrid architecture; shares the
    defensive cuDNN-disable default with the other conv/attention-heavy
    shapes. Per-node script: `workloads/diffusion/run_diffusion_node.sh`,
    launched the same way as PP above.
13. **DLRM** — recommendation/embedding-heavy, sparse AllToAll dispatch;
    two real variants — `workloads/dlrm/run_dlrm_node.sh` (with
    Inspector) and `workloads/dlrm/run_dlrm_node_noinspector.sh`
    (without, no `DUMPDIR_BASE` argument at all) — both per-node
    scripts, launched the same way as PP above.
14. **Multi-modal (VLM)** — vision+language combined architecture, the
    language half reusing the same TP-shaped model as TP2/long-context.
    Per-node script: `workloads/multi-modal/run_vlm_node.sh`, launched
    the same way as PP above.
15. **RL** — reinforcement learning, interleaved rollout/policy-update
    phases (a real `STRAGGLER_PHASE` argument targets which phase the
    injected fault lands in). Self-dispatching, like nanoGPT:
    `bash workloads/rl/run_rl.sh 200 /tmp/out 29500 var/dump "" 200 3 rollout`
    (note the empty 5th positional placeholder, same real gap pattern as
    TP-inference; `200`/`3`/`rollout` are sleep-ms/target-rank/phase).

### 8.2 The 6 parallelism strategies validated (independent of which shape exercised them)

- **Data-parallel (DP)** — nanoGPT DDP, the project's baseline shape.
- **Tensor-parallel (TP)** — validated at both **2-way** (TP2,
  below-floor) and **4-way** (TP4, above-floor).
- **Fully-sharded data-parallel (FSDP)** — its own quantified detection
  floor, above.
- **Pipeline-parallel (PP)** — genuinely cross-node, its own legitimate
  stage-asymmetry handling, above.
- **Mixture-of-Experts (MoE) expert-parallel** — job-wide detection
  validated; single-rank localization is a permanent, disclosed
  boundary, above.
- **Hybrid (TP+PP combined in one job)** — real cross-communicator fault
  correlation validated, above.

### 8.3 Special-mechanism notes referenced above

- **MoE's two-stage detector** (`moe-two-stage-detector/`): a standalone,
  offline 5-step diagnostic, run only *after* job-wide detection has
  already flagged a MoE job — real arrival-order timestamps (not
  `coll_exec_time_us`, already proven unreliable for AllToAll
  localization), a real per-rank token-load comparison, retargeted
  DCGM/host/NVLink checks, and an isolated pairwise `torch.distributed`
  sweep between just the suspect rank and one healthy peer.
- **P27.2 timing-asymmetry fallback** (TP2/TP-inference): below
  `SELF_DETECTION_FLOOR=3` real members, a pure software fault shows the
  **true straggler reading LOW** (it sleeps before entering the
  collective) while its healthy partner reads elevated (it's the one
  actually waiting) — the inverse of a naive "whichever member looks
  elevated is the straggler" rule. Confirmed live, independently, on
  both TP2 and TP-inference.

## Appendix: explicitly excluded from this package (and why)

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
  the shim. A real debugging aid used once during one specific
  validation session, not pure dead scratch — but still correctly
  excluded from the production package (never part of the standard
  launch path).
- Any `migration_package/` copy of this pipeline's own code — confirmed
  via direct diff to be **stale** relative to the live code (missing the
  storage-classifier wiring, the log-rotation fix, and several other
  fixes — see `INVENTORY.md` Category A). The prose docs under
  `migration_package/*.md` were still current enough to carry forward as
  `docs/`; the *code* copies were not, and are not part of this package.
- The `runs/` historical test-output subdirectory under the original
  `health/` tooling location (JSON/CSV logs from past test campaigns on
  the old cluster) — only the 4 real, reusable script files were copied.

## Appendix: `ras_alert.py` — included, but not currently wired in

`alerting/ras_alert.py` is a real, independently validated RAS-based
fail-stop watcher (a different failure class from the fail-slow
classifier: "is this rank still alive at all", not "is it measurably
slower than its peers"). It is **not imported or invoked by
`alert_engine.py`** in the current live pipeline (confirmed: zero `import
ras_alert` anywhere in `alert_engine.py`) — it's a standalone tool, run
separately, not part of the main alerting loop. Included here because it's
real and validated, not because it's already integrated.
