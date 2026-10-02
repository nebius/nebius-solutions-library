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

**Getting the code**:

```bash
git clone https://github.com/nebius/nebius-solutions-library.git
cd nebius-solutions-library/soperator-straggler-detection/detection-pipeline
```

(As of this writing, this package lives on the `add/straggler-detection-v1-beta`
branch, pending merge into `main` — add `-b add/straggler-detection-v1-beta`
to the `git clone` above if it hasn't merged yet. Once merged, plain `main`
is correct and this parenthetical no longer applies.)

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
environment.sh      Optional, run BEFORE install.sh on a genuinely fresh
                    host -- covers the one prerequisite tier install.sh
                    does not attempt itself (compiler toolchain, CUDA
                    toolkit, a real NCCL source build) -- see "Installing"
                    below
vm-setup.sh          Optional, run BEFORE install.sh -- brings up a real
                    VictoriaMetrics instance automatically, as a plain
                    auto-restart-supervised background process on this
                    host, so install.sh's own VM auto-detection finds it
                    with zero extra config -- see "Installing" below
grafana-setup.sh     Optional, run BEFORE install.sh -- brings up a real,
                    auth-enforced Grafana instance automatically, same
                    supervised-background-process approach as
                    vm-setup.sh -- see "Installing" below
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
VERSIONS.md          Every pinned/confirmed real version (NCCL, CUDA,
                    the Inspector plugin's own build gaps, container
                    image, VictoriaMetrics, Grafana, bpftrace) this
                    package was actually validated against
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
  `install.sh` checks for and installs what it can automatically (see
  "Installing" below) — **except** the compiler toolchain, CUDA
  toolkit, and NCCL source build, which it deliberately only detects
  and fails loudly on rather than installs itself (a full NCCL source
  checkout + build is a substantial, separate step, out of scope for a
  script whose job is bringing up the *pipeline*, not the *host*). Run
  `./environment.sh` first on a genuinely fresh host to cover exactly
  that gap — see "Installing" below for how the two compose.
  `dcgmi` is the one entry on this list where `install.sh` does not
  install the package itself (same reasoning as CUDA/NCCL — a driver-
  adjacent install) but **does** actively check and fix a real,
  confirmed-live gap: the DCGM *hostengine* daemon (`nv-hostengine`)
  every DCGM-sourced check (clocks, power, thermal, ECC, PCIe — see
  "Path B" in section 4) depends on is not guaranteed to be running even
  when the package is installed, with no loud signal that it's down
  until `alert_engine.py`'s own runtime watchdog happens to notice
  mid-job. `install.sh` now checks every node's DCGM hostengine liveness
  and attempts to start it (systemd, then a direct `nv-hostengine`
  fallback) before that ever becomes a live-job surprise.
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
  shape's own validation depended on its specific version. **A concrete,
  confirmed consequence of this same mount convention**: a real Megatron
  TP4/PP4/DP3 validation run found `--transformer-impl=transformer_engine`
  crashing with a `cublasLtGetVersion` symbol error, because the same
  `/usr/lib/x86_64-linux-gnu` bind-mount shadows the container's own
  CUBLAS library too, not just NCCL — worked around with
  `--transformer-impl=local`. Worth knowing before running any future
  Transformer-Engine-based workload on this repo's own mount convention.

## 4. Installing — `environment.sh` + `vm-setup.sh` + `grafana-setup.sh` (all optional) + `install.sh`

Run from the Slurm control/login node. On a genuinely fresh cluster —
no working compiler/CUDA/NCCL source build, no VictoriaMetrics instance,
no Grafana instance running yet — the full, real, start-to-finish
sequence is:

```bash
cd soperator-straggler-detection/detection-pipeline
./environment.sh    # optional: compiler + CUDA + NCCL source (only what install.sh below does not cover)
./vm-setup.sh        # optional: brings up VictoriaMetrics automatically (only what install.sh below does not cover)
./grafana-setup.sh   # optional: brings up Grafana automatically (only what install.sh below does not cover)
./install.sh
```

All three are independent of each other and of `install.sh` — run any
subset depending on what this host already has; each is a fast no-op if
its own target is already present/reachable, safe to re-run any time,
not just on a first install. `environment.sh` and `vm-setup.sh` don't
depend on `grafana-setup.sh` or vice versa — order among these three
doesn't matter, only that they run before `install.sh`. All three also
auto-install their own apt dependencies via `sudo` when not already
running as root (and refuse with a precise message, not a raw `apt`
error, if neither root nor passwordless `sudo` is available).

**`environment.sh`** covers exactly the one prerequisite tier
`install.sh` does not attempt itself — a real compiler toolchain
(`build-essential` + `git`), a CUDA toolkit matching this package's own
validated value (`VERSIONS.md`'s confirmed `13.0` — installs
`cuda-toolkit-13-0` specifically via NVIDIA's own apt repo, **never**
`cuda`/`cuda-drivers`, so an already-working GPU driver is never
touched), and a real NCCL source tree cloned from NVIDIA's own upstream
(`https://github.com/NVIDIA/nccl.git`), checked out at the exact tag
this package was validated against (`v2.28.9-1` — confirmed to still
exist on NVIDIA's real upstream repo, not assumed), and built at a path
`install.sh`'s own Step 2 already checks for and prefers: a sibling of
this repo checkout itself (`<repo-root>/nccl-2.28-src`,
`environment.sh`'s own default — writable by whoever can already write
to their own clone, no root needed), falling back to the absolute
`/root/nccl-2.28-src` (this project's own original dev-cluster
convention) only if that's what's actually there — set `NCCL_SRC_DIR`
explicitly to override either script's default. It deliberately does
**not** duplicate anything `install.sh` already handles itself
(`bpftrace`, `libibverbs-dev`, `logrotate`, Slurm/`ssh`/`python3`
detection, per-node GPU/NCCL/`/tmp` discovery) — those stay in
`install.sh`'s own Step 1, unchanged.

**`vm-setup.sh`** brings up a real, working VictoriaMetrics instance
automatically, as a **plain background process directly on this host**
(the login node) — matching this project's own already-established
precedent for Grafana (see `grafana-standalone/README.md`), not the
`srun`-on-a-worker-node approach `vm-standalone/README.md` documents as
the alternative (still valid; use that instead if you specifically
want VM isolated to its own dedicated worker node rather than sharing
the login node). It downloads the real, pinned binary
(`VERSIONS.md`'s confirmed `v1.150.0`) directly from VictoriaMetrics'
own GitHub release, sha256-verified against their own published
checksum before extracting, launches it under a real auto-restart
supervisor (`observability/run_vm_supervised.sh`, mirroring the exact
same supervision/log-rotation pattern already used for the aggregator
and `alert_engine.py`), and — critically — verifies it live from a real
worker node (not just `localhost`) before declaring success, so a
cluster whose login node happens to be firewalled off from its workers
fails loudly and precisely here instead of leaving the aggregators
silently unable to ever push data. `install.sh`'s own VM
auto-detection now checks this host's own hostname in addition to the
conventional first-worker-node, so no `VM_URL` needs passing either
way. Port `8428` is VictoriaMetrics' own upstream default — not a
Soperator-specific convention — reused here only because every other
piece of this pipeline (the Grafana datasource config, `install.sh`'s
own detection, every workload's default `VM_URL`) already assumes it;
override `VM_PORT`/`VM_HOST` if you have a real reason to change it,
but every one of those other pieces would then need updating too.

**`grafana-setup.sh`** brings up a real, auth-enforced Grafana instance
automatically, the same way — a plain background process on this host,
under its own auto-restart supervisor
(`observability/run_grafana_supervised.sh`). It downloads the real,
pinned release (`VERSIONS.md`'s confirmed `11.5.1`) directly from
Grafana's own official release server, sha256-verified before
extracting, and generates the exact same real, enforced-auth config
`install.sh`'s own Step 4.7 does (a real random admin password —
**never** anonymous access — written to `var/grafana_admin_credentials.txt`,
`chmod 600`) using the identical idempotent "reuse an already-generated
password" rule, so running `install.sh` afterward correctly detects and
reuses it rather than generating a conflicting one. It also generates
both real provisioning provider files itself (`dashboards/local.yaml`
and `datasources/local.yaml`, the same real sed substitutions
`install.sh`'s own Step 4.5 does) directly under
`var/grafana_provisioning_generated/` — which its `custom.ini` points
`[paths] provisioning` at — **before Grafana's first start**, not just
an empty directory for `install.sh` to fill in later. This matters:
Grafana only discovers *new* provisioning provider files at its own
process startup — `updateIntervalSeconds` only governs an
*already-registered* provider re-scanning its own configured path for
dashboard-JSON content changes, not Grafana noticing a provider file
that didn't exist yet when it booted (a real bug an earlier version of
this script had — the dashboard silently never appeared until Grafana
was manually restarted after `install.sh` ran). The datasource's real
`VM_URL` is a live best guess here (`http://$(hostname):8428`, the same
convention `vm-setup.sh` establishes) since this script may run before
`vm-setup.sh`/`install.sh` know the real value — but because the *file*
already exists and is already being watched, `install.sh`'s later
correction (if the guess was wrong) **is** picked up automatically via
the periodic re-scan, no restart needed for that part. Finally, it
verifies real auth is genuinely enforced (`/api/org` must answer `401`/`302`,
never `200`) before declaring success, exactly like `install.sh`'s own
live check. Port `3000` is likewise Grafana's own upstream default, not
Soperator-specific — override `GRAFANA_PORT` if needed, same caveat as
`VM_PORT` above. See section 6 below for how to actually access it once
it's up.

Once the environment, VM, and Grafana are all ready (with or without
any of the three scripts — a host that already has everything needs
none of them), run:

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
   real build. This step is the one that fails loudly (`FATAL: No NCCL
   source tree with its own build/ found...`) if `environment.sh` above
   was skipped and no real NCCL source build already exists.
7. **Detects a real, reachable VictoriaMetrics instance** (probes both
   the conventional `http://<first-node>:8428/health` and this control
   host's own `http://$(hostname):8428/health` live) or reports
   precisely that one needs to be launched — either via `vm-setup.sh`
   above (this host, automatic) or per `vm-standalone/README.md` (a
   worker node, via `srun`) — it does not launch one itself. **0s dedup
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
   `var/grafana_admin_credentials.txt` (`chmod 600`) — idempotent: reuses
   `grafana-setup.sh`'s own already-generated password here if you used
   it, rather than generating a conflicting one. See "Grafana access"
   below for the full detail.
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
- **Launching VictoriaMetrics via `srun` (as `vm-standalone/README.md`
  literally documents) permanently blocks every subsequent workload test
  for the same user, on a cluster with no spare non-GPU node** — real,
  found live during a from-scratch V1 Beta Stage 5 re-run. VM's own `srun`
  job occupies a real Slurm queue slot under the launching user
  indefinitely (it's meant to run for the whole testing campaign), and
  every workload launch script's own `squeue -u "$USER" -h` guard aborts
  ("ABORT: existing job for $USER already queued/running") whenever
  anything is already queued for that user — including VM's own job. On
  this project's real 2-node development cluster (no dedicated non-GPU
  node exists to isolate VM onto, contrary to that README's general
  advice), this makes it impossible to ever run a workload test after
  launching VM exactly as documented. Real fix used here: launch the same
  official binary directly as a plain background process instead
  (`ssh <node> "setsid nohup /path/to/victoria-metrics-prod <same flags> </dev/null &"`)
  — bypasses Slurm's queue entirely, consistent with how this project's
  own aggregator/alert_engine supervisors are already launched (also not
  Slurm jobs), with zero change to VM's own data or flags.
- **`run.sh`'s own Step 2 alert_engine.py liveness check can false-FATAL
  immediately after a genuinely fresh install** — real, found live the
  same session. The check samples `/proc/<pid>/stat`'s CPU ticks over a
  fixed 6-second window and FATALs if they don't advance. On a freshly-
  created, still-empty VictoriaMetrics instance, `alert_engine.py`'s own
  per-poll-cycle work (nothing to calibrate yet) can cost under one tick
  (10ms) — genuinely alive and correctly cycling (confirmed directly via
  `strace`: real DNS + VM query traffic every `poll_interval`), but
  invisible to a 6-second CPU-tick sample. Not a crash, not a hang —
  simply resolves itself once any real workload traffic starts flowing
  (confirmed live: a subsequent `run.sh` re-run passed cleanly once
  `tools/self_test.sh` had generated real traffic). If you see this exact
  FATAL right after a from-scratch install, re-run `run.sh` after
  launching any real workload rather than assuming the pipeline is down.

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

`install.sh` itself never launches or manages a Grafana instance's
lifecycle — it detects whatever instance is reachable, generates real
provisioning + auth config for it, and `run.sh` prints exactly how to
reach it. Two real, supported ways to actually bring one up:
`grafana-setup.sh` (this control host, automatic — see "Installing"
above) or `grafana-standalone/README.md` (the full manual launch
recipe, e.g. if you want it on a different host).

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

### 6.4 Where real findings actually show up

Grafana's panels (section 1's "worst rank" metrics) show *what the
aggregator measured*, continuously, whether or not anything was actually
flagged. The detector's actual verdicts — a real straggler/fault getting
confirmed and attributed — live somewhere else: `alert_engine.py`'s own
output, captured by its supervisor.

- **`var/alert_engine_supervised.log`** — the full, real output: every
  alert prints a multi-line block (`[ALERT] rank=... node=... comm=...
  type=... confidence=CONFIRMED/PROBABLE/UNCONFIRMED severity=PAGE/
  LOG-ONLY`, followed by the real gathered cause-evidence — DCGM clock/
  power, storage I/O-wait, whatever corroborated it). `run.sh` prints
  this file's real path once it launches the pipeline. This is also
  exactly what `tools/self_test.sh` itself `grep`s to prove detection
  worked, and what every `[PIPELINE-DOWN]`/`[DCGM-HOSTENGINE-DOWN]`-style
  dead-man's-switch signal appears in too.
- **`var/alert_summary.log`** — a distilled, one-line-per-alert feed:
  the same structured `[ALERT] ...` header line above, nothing else,
  with a timestamp — so `tail -f var/alert_summary.log` gives a quick,
  human-scannable "what's been flagged, when" without scrolling past
  full evidence blocks. It's generated from the exact same header line
  the full log already leads with (not recomputed), so it can never say
  something different from what the full log says.

There is currently no external paging (Slack/PagerDuty/email) wired up
— `severity=PAGE` vs. `severity=LOG-ONLY` in the text above is this
project's own internal confidence-based severity label (see section 7),
not an actual page going out anywhere yet. Today, watching for a real
finding means tailing one of these two files, or watching the
corresponding `*_fired` metric (e.g. `agg_mean_fired`,
`agg_persistence_fired`, `agg_outlier_count_fired`) flip to `1` on the
Grafana dashboard.

**Nothing shows up in "the terminal" automatically — you have to go
look.** `alert_engine.py` is a background process (launched via
`nohup`/`setsid` by its supervisor); its output is redirected straight
into the two log files above, not printed to whatever terminal session
you happen to have open. There's no popup, no notification, nothing
proactive. Concretely:

- **To watch live, while a job is running**, open a terminal and leave
  this running:
  ```bash
  tail -f var/alert_summary.log
  ```
  The moment a straggler is caught, a new line appears right there —
  e.g. `2026-10-01T00:19:04Z [ALERT] rank=3982741 comm=0x1a924b2167360c
  node=worker-1 type=compute confidence=PROBABLE severity=LOG-ONLY`. For
  the full evidence behind any one line (DCGM readings, storage I/O-wait,
  the actual reasoning), look up the same `[ALERT] ...` text in
  `var/alert_engine_supervised.log`.
- **To check after your run has already finished** (no live terminal was
  open at the time), the alerts are still there — these are real files,
  append-only, not a transient in-memory stream:
  ```bash
  grep "\[ALERT\]" var/alert_summary.log            # every alert, whole history
  tail -50 var/alert_summary.log                     # just the most recent ones
  ```
  If you need to scope this to one specific job, filter by its real
  Slurm job id or time window — every full-detail block in
  `var/alert_engine_supervised.log` includes `comm=`/`node=`/rank
  identity you can cross-reference against `squeue`/your own job's known
  start/end time.

### 6.5 `straggler_incident_detected` — counting stragglers without needing a cause confirmed

Everything in section 6.4 above (`[ALERT] ... confidence=CONFIRMED/
PROBABLE/UNCONFIRMED`) answers "what caused this, and how confident are
we in the cause?" — a **separate** question from "did a real, persistent,
impactful straggler just happen, regardless of whether we ever find
out why?" Before this signal existed, those two questions were
conflated: a genuinely sustained, high-impact software-only straggler
(no DCGM/storage signature to corroborate it — e.g. a pure scheduling
delay) could never rise above `PROBABLE`, a tier this project's own
measured data says **carries the run's entire false-positive rate
(22-25/hour)** — making it useless for reliably counting real incidents
or estimating lost compute time, even though the underlying timing
anomaly was completely real and sustained.

`straggler_incident_detected` decouples the two. It fires on:
- **Persistence**: the SAME `T.PERSIST_REQUIRED=3`-consecutive-window
  rule CV detection already used (now applied uniformly to mean and
  outlier_count too — see the known-limitations note in section 7 on
  the resulting tightening of mean-path alerting).
- **Impact**: `mm > T.MEAN_MM_THRESH` (2.0x) — this project's own
  already-calibrated "2x slower than peers is real, not noise" floor,
  applied the same way regardless of which statistic (cv/mean)
  triggered it.

— **entirely independent of cause-tier**. Whether DCGM/storage ever
corroborates a hardware cause is looked up and attached *afterward*, as
a secondary annotation on the finding, never a gate on whether this
signal fires at all.

**Where it shows up** — additive to, never replacing, the existing
`[ALERT]` output:
- A `[STRAGGLER-INCIDENT] stat=... host=... member=... gpu_slot=...
  severity_ratio=... persisted_s=...` line in both
  `var/alert_engine_supervised.log` and `var/alert_summary.log`,
  alongside (not instead of) the finding's own `[ALERT]` block.
- Two new VictoriaMetrics metrics: `agg_straggler_incident_detected`
  (1, pushed once per onset, same convention as `agg_mean_fired`) and
  `agg_straggler_incident_severity_ratio` (the real measured ratio —
  `mm` for a mean-sourced finding, `z` for a cv-sourced one — so
  incidents can be ranked by severity directly off the metric's own
  value, rather than binned into another tier).
- A new Grafana panel ("Straggler incident detected") on the
  straggler-detection-metrics dashboard.

`persisted_s` is the real, measured elapsed wall-clock seconds between
the first and last of the 3 qualifying windows (not a sample-count
proxy) — empirically validated against raw NCCL Inspector dump
timestamps during this feature's own implementation.

**Lost compute-time estimation is deliberately NOT built here.** The
already-existing `agg_job_throughput_ratio_to_baseline`/
`agg_job_throughput_rate_ref` (a job's own measured throughput vs. its
own historical baseline) is the right signal for that — treat a
`straggler_incident_detected` event as a likely contributing cause to
whatever shortfall that signal already shows, rather than deriving a
second, possibly-disagreeing number bottom-up from individual
collectives' excess exec time (which would double-count overlapping
communicators and miss non-collective-bound slowdown).

### 6.6 Trusting what an alert shows you — the z/mm staleness fix, cross-reference IDs, and evidence panels

Investigating Cyril's item 2 (making flag evidence inspectable) found a
real, separate bug: **the z/mm value displayed in an alert's own text
could be stale** — a real number, but from a different, later moment
than the one that actually satisfied the firing condition. Root cause:
the mean-path check read the firing decision from `agg_mean_fired` (a
widened lookback query, so a transient window isn't missed between
polls) but read the displayed z/mm from a separate, unwidened "whatever
is currently latest" query — and node_aggregator_ref.py keeps closing
new windows continuously, so by the time this engine polled, the two
could legitimately disagree. Confirmed live: one alert displayed
`z=2.7`; the real window that actually fired showed `z=34.1, mm=3.54`.

**Fixed for both cv and mean, both ultimately via VictoriaMetrics'
`/api/v1/export`** (not `query_range`, which grid-resamples onto fixed
step points and can silently skip a real sample at this project's own
window-close cadence):
- **CV**: `_cv_maxmed`'s own exec-time query now reads the real sample
  closest to the exact real firing timestamp via `/api/v1/export`. A
  PromQL `@`-pinned plain instant query was tried first and rejected —
  caught live, before shipping: it suffers the identical ~30s
  visibility floor described below, and since the firing timestamp is
  by construction always very recent, it would have returned no data
  (blank mm/worst_val) on nearly every real CV-triggered alert.
- **Mean**: the firing decision no longer reads `agg_mean_fired` at
  all. It reads every real raw `agg_mean_z_worst`/`agg_mean_mm_worst`/
  `agg_detection_coverage_achieved` sample in the lookback window via
  `/api/v1/export`, and correlates all three by their exact shared real
  timestamp (all three are pushed together, in the same
  `score_mean_window()` call) to reconstruct precisely the same
  `past_grace and z>30 and mm>2.0` decision node_aggregator_ref.py's own
  code already computes — a pure refactor of *where* the decision is
  read from, never a change to the decision itself.

**Latency, precisely quantified — not just "may fire slightly
earlier."** Verified side by side against the old `agg_mean_fired`-based
decision across two real fault-injection validation runs: every
mismatch (45 total) was the same direction — the new logic qualifying
when the old logic hadn't yet, never the reverse — and in every traced
case the underlying data was real and already genuinely qualifying.
Root cause, measured directly (5 independent controlled trials against
this exact VictoriaMetrics instance): a consistent **~30.2–30.4s delay**
before any freshly-written sample becomes visible via a plain instant
query (`/api/v1/query`) — confirmed to apply to every write, not just
first-ever label combinations (a repeat write to an already-visible
series showed the identical delay) — consistent with this project's own
prior, independently-measured "~30-39s baseline visibility floor" for
this same instance. `/api/v1/export` is not subject to this floor.

**Alerts may fire up to ~30 seconds earlier than the previous
implementation**, in cases where a label combination's visibility on
the old instant-query path was still inside VictoriaMetrics' own ~30s
query-visibility floor. This is strictly a latency improvement — the
new logic never fires on incorrect or less-true data, and never fires
later than the old logic would have. The ~30s bound does not compound
across the 3 required persistence windows: it is a floor on how
recently-written data becomes visible to a query issued "now," not a
per-window penalty — by the time the third (gating) window closes, the
first two are, in every real case observed, already past that floor.

**Cross-reference ID**: when `straggler_incident_detected` also fires
for the same event, both its `[STRAGGLER-INCIDENT]` line and the
corresponding `[ALERT]` line now carry a shared `incident_id=host:comm:
member:bucket:coll` field, so you (or a script) can confirm they refer
to the same real event without inferring it from log adjacency.

**Evidence panels** (push identifiers, pull trajectory — the design
decision: bundling the full trajectory into the alert text itself would
mean re-deriving data already in VictoriaMetrics and needing to parse it
back out of log text; leaving it purely in VM with no guided path would
mean hand-writing PromQL every time): the alert text and
`[STRAGGLER-INCIDENT]` line already push the identifiers (`comm`,
`member`, `bucket`, `coll`, `incident_id`) you need. Two new dashboard
variables, **Comm** and **Member (PID)**, let you paste those straight
in, filtering two new panels:
- **"Flag evidence trajectory"** — the real `agg_mean_z_worst`/
  `agg_mean_mm_worst` history around the event, not just the single
  value the alert text shows.
- **"Peer timing comparison"** — every member of the same communicator's
  own real exec time at the same moment, the same data this project's
  own cascade/"LOCATION UNCERTAIN" investigations already rely on by
  hand, one filter away instead of hand-written PromQL.

No new metrics collection for either panel — both read series that were
already being pushed.

### 6.7 Worst-selection sign fix, and the direct-impact lost-compute-time estimate

**Real bug fixed**: `node_aggregator_ref.py`'s mean-path "worst" selection
used `abs(means[p] - median)` — flagging whoever deviates most from the
median in *either* direction, never checking whether that deviation means
genuinely slower (a real straggler) or simply faster (not a straggler at
all). Confirmed directly against a real, healthy 4-stage Megatron
TP4/PP4/DP3 baseline: a specific comm-local role position was consistently
the *fastest* member of its own TP group in every one of the 4 pipeline
stages, yet got selected as "worst" 40-86% of the time in genuinely healthy
windows purely for being a large, consistent outlier in the wrong
direction. Fixed by switching to signed deviation (`means[p] - median`) —
a straggler detector must never flag a rank for being faster than its
peers. Validated both directions against real data: the false-positive
rate for that role position dropped from the 40-86% range down to under
7% (consistent with ordinary noise, not a structural bias), while the
genuine fault's own relative visibility *increased* once it was no longer
competing against a spurious fast-outlier for the "worst" slot.

**`straggler_incident_detected`'s direct-impact estimate** (Cyril item-4
Part B, root-only scope): for a CONFIRMED finding that is the *only*
CONFIRMED finding for its job within a recent window (reusing the
already-computed tier as the root-cause signal — no new detection
mechanism), computes a real **GPU-seconds** figure for that root's own
direct peer group: the sum, over every real sample in the incident's own
measured window (`duration_s`), of each peer's real exec time above *its
own* cross-job healthy baseline (`_member_role_baseline`, already
existing). This is deliberately the corrected version of the natural
"straggler's own exec time vs. peers" idea — real data showed a
late-arriving straggler's own reading looks *short*, not elevated (it
arrives late; the collective completes quickly once it finally joins) —
the real signal is in the *waiting peers'* elevation, not the root's own
number.

**Explicitly root-only, by design**: this does not trace or attribute
impact to any downstream rank/stage that doesn't share a physical member
with the root's own communicator. A real cascade may still fire its own
separate `PROBABLE` alerts, exactly as it does today — those are never
folded into this number. Reliably separating "root" from "cascade" across
non-shared-member stages was investigated and found infeasible with
what's currently available; this is intentionally the honest, simpler
fallback, not a shortcut.

**Reported in GPU-seconds, not dollars.** Converting to a dollar/GPU-hour
figure needs the job's own expected total runtime/iteration count, which
isn't captured anywhere today — left as an open, undocumented-elsewhere
gap, not estimated here.

**Permanent caveat — this travels with the number everywhere it's shown
(log text, this README, any future dashboard panel), not just here:**
> This estimate can significantly **undercount** real impact for faults
> that are compute-bound but not collective-timing-bound. Demonstrated
> live, real data: a genuine 5.7x GPU clock suppression on one TP shard
> produced **~0.75 GPU-seconds** of measured impact by this method, over
> a real ~5-minute fault window — because that specific collective was
> network-latency-bound at this scale, not compute-bound, so a slower
> GPU clock didn't translate into slower collective timing. **A
> near-zero or small number here does not mean the fault had no real
> impact — only that this specific collective's timing didn't show it.**
> By contrast, the same method against a real storage I/O-wait fault
> (where every downstream collective genuinely had to wait) correctly
> produced ~23.6 GPU-seconds over its own real incident window.

Exposed as `agg_direct_impact_gpu_seconds` (pushed via the same
`push_buf`/VM-ingest path every other metric already uses — no new
collection) and a `[DIRECT-IMPACT-ESTIMATE]` line in both log files,
carrying the caveat text inline every time it fires, not as a one-time
footnote.

### 6.8 Path C's real incident window, and its window-overlap-strength display

Two real, narrow fixes to Path C (storage, eBPF block-I/O-wait), scoped
deliberately to this one cause-path — the only one with a real, queryable
incident window right now (see 7's new Path B/fabric entry below for why
the other DCGM-sourced paths don't yet have this).

**Real incident window, not a fixed 10s guess.** The live alert path
(`alert_engine.py`'s `_emit()` → `build_finding_for_alert`) used to query
Path C over `[anomaly_ts - IOWAIT_LIVE_WINDOW_S, now]` — a fixed 10s
lookback regardless of how long the incident had actually been running.
For a brief fault this was a reasonable guess; for a longer, genuinely
sustained one it could miss real io-wait evidence from everything before
the last 10 seconds. `_emit()` already computes the incident's own real,
measured duration (`persist_duration_s` — the same value
`[STRAGGLER-INCIDENT]`'s own `persisted_s=` reports) via its persistence
gate, so this is now threaded through: `incident_start = anomaly_ts -
persist_duration_s`, strictly a widening of the old window (never
narrower), falling back to the old fixed-10s behavior only on a call path
that genuinely doesn't have `persist_duration_s` yet.

**Window-overlap-strength, informational only.** Every Path C reading now
also reports how much of the incident's own real duration the eBPF
agent's log actually has coverage for — e.g. "iowait evidence present for
27.4 of 31.2 real seconds in this incident's actual duration" — distinct
from whether the target process's own io-wait crossed the fault
threshold (that verdict is unchanged). This answers "did we have log
coverage for this window at all", separate from "was this rank
io-bound". **This number is purely informational and is never read by
any tier-decision logic** — confirmed by direct trace, not assumption:
`storage_evidence.determine_storage_path()` (the only function that
decides Path C's True/False/None verdict) reads exactly two keys,
`target_iowait_us` and `window_s`, both unchanged; `determine_confirmed_
path()` only ever calls that function and never touches the evidence
dict's fields itself. A grep-verified full list of every place this
project reads a Path C evidence dict's fields turns up exactly: the two
decision reads just named, and five display-text reads (this section's
new line included) — nothing else, anywhere in the codebase.

### 6.9 Inspector plugin: lock-free ring buffer, replacing a silent single-slot data-loss bug

**Reconciliation, confirmed with direct evidence before writing any code**:
a prior, separately-documented development track had already found and
fixed this exact bug class and validated a ring-buffer replacement — but
that fix was never part of this repo's own history. Checked directly: `git
log --all` shows exactly one commit, ever, touching `inspector.cc`/
`inspector.h`/`inspector_plugin.cc` on any of this repo's branches (the
original "Add straggler-detection pipeline package" import) — no revert,
because there was never a prior patched version to revert from. This
branch's copy is byte-identical (`diff -q`, confirmed empty) to NVIDIA's
own raw upstream tree (`/root/nccl-2.28-src/ext-profiler/inspector/`), and
`install.sh` builds directly from this repo's own `inspector-plugin/`
source (NCCL is only linked against for headers/`libnccl.so`), so this
was never a wrong-file build problem either — the source itself simply
never had the fix. It was ported/re-implemented here from the validated
design, not cherry-picked.

**The bug**: every NCCL collective's completion wrote into a single
`completedCollInfo` slot per communicator, guarded by a plain
`pthread_rwlock_t`, with one dirty flag. If a second collective completed
on the same communicator before the dump thread's next wakeup
(`NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS`, default 500us), the
earlier record was silently overwritten — gone, with no counter, no log
line, nothing to show it ever happened. Confirmed live this session (see
6.8's own measurement context): **~17-20% of real collectives silently
lost** on TP4's own high-frequency AllReduce communicator, at the default
interval.

**Design (ported, then re-verified against this branch's own real
source before implementing, not assumed to transfer unchanged)**: a
fixed-size, lock-free, bounded ring buffer per communicator, single
producer (confirmed directly against this branch's actual NCCL 2.28.9
source: `ncclProxyProgressCreate`'s guarded, one-time `pthread_create`
guarantees exactly one proxy progress thread per communicator for any
comm not created via an explicit `ncclCommSplit`-with-share — which
Megatron/PyTorch's standard `new_group()`-based TP/PP/DP group creation
does not use, so this holds for every workload this project runs),
single consumer (the dump thread). 256-record capacity, re-confirmed
(not reused blindly) by compiling a real `sizeof()` probe against this
branch's own header: `sizeof(inspectorCompletedCollInfo) = 3168` bytes,
so 256 slots = exactly 792.0 KiB/communicator/rank — matching the prior
validation's own figure precisely, no adjustment needed. Overflow policy
is drop-newest (the producer never touches the consumer's own index,
preserving the lock-free invariant). A cumulative `queue_drops_total`
counter is incremented atomically on every drop and surfaced in **every**
dumped record (never requires scanning history to notice loss), plus a
rate-limited `[WARN]` log line on exact powers of two (1, 2, 4, 8, 16,
...) so a real, ongoing drop never floods the log but also never goes
unnoticed. All access is `__atomic_*` builtins (matching NCCL's own
atomic-builtin idiom) — the old `pthread_rwlock_t` guard is gone
entirely, along with the single dirty-flag field it protected.

**Validation — real before/after data, not assumed to transfer from the
prior track's own numbers**:
- Built clean via `install.sh`'s own exact `make` invocation, `-Wall
  -Wextra`, **zero warnings**.
- **coll_sn continuity (the direct measure of the bug this closes)**,
  same methodology as 6.8's own investigation, same Megatron TP4/PP4/DP1
  shape, same 500us default interval: the highest-frequency communicator
  went from **~17-20% missing (pre-fix) to 0.00% missing, 21,600/21,600
  real records recovered (post-fix)** — `queue_drops_total` stayed `0`
  throughout (256 was comfortably sufficient for this shape's own real
  burst intensity).
- **MoE and DLRM (sparse AllToAll dispatch, this project's own
  highest-collective-frequency shapes) push past 256 capacity at
  points** — real, observed `queue_drops_total` of up to 15,506 (MoE) and
  7,924 (DLRM) on a short run. This is disclosed honestly, not hidden:
  the design's own guarantee is "loss is never silent," not "loss never
  happens" — these counts are real, visible, and exactly what the
  mechanism is for. Whether 256 should be raised for these specific
  shapes is a separate, open sizing question, not resolved here.
- **tools/self_test.sh**: clean PASS, exact rank-match, 0 new
  `CHECK-FAILED`, with the patched plugin loaded.
- **Full-pipeline re-check, same self_test.sh run**: `straggler_incident_
  detected`'s `persisted_s`/`severity_ratio` and the z/mm staleness fix's
  displayed values are internally consistent with each other (the
  displayed `z=3506.4` in the `[ALERT]` text matches the `severity_ratio=
  3506.42` in the paired `[STRAGGLER-INCIDENT]` line, the same
  same-timestamp correlation 6.6's fix established) and sit within the
  same wide, already-documented run-to-run range this exact reference
  scenario has shown across many runs this session (305.82-3506.42) —
  no new instability introduced. Path C fired correctly in the same run
  (ruled out, `target_iowait_us=0`, correct for a compute fault) —
  expected, since Path C's own evidence (eBPF `iowait`, not NCCL
  Inspector) is structurally independent of this plugin entirely; there
  is no mechanism by which this patch could affect it, and the same run
  confirms its machinery is unaffected.
- **TP4-standalone and Megatron fault-injection, reported honestly, not
  oversold**: real 200ms-sleep fault-injection runs on both shapes
  showed the raw data pipeline working correctly end to end (the
  injected rank's own real exec-time samples reported continuously
  throughout, zero gaps) and the aggregator correctly identifying it via
  `agg_persistence_fired` — but neither run's peer-elevation z-score
  cleared the firing threshold (TP4: max observed z=12.28 vs
  `MEAN_Z_THRESH`-class gates; Megatron: z=13.28 vs `CV_Z_THRESH=20.0`).
  This is **not** a ring-buffer regression: it reproduces identically on
  both shapes for the same reason — a real, pre-existing signal-strength
  characteristic of this specific tiny-model-plus-200ms-sleep
  configuration on a small-message TP4 collective, unrelated to data
  completeness. Flagged as a real, separate, open question (is the
  threshold miscalibrated for this shape, or is 200ms genuinely too
  small a fault at this message size) — not investigated further here,
  out of this task's scope.
- **Overhead re-measured with the fix in place**, same methodology as
  6.7/6.8's own numbers, same 2-node TP4/PP4/DP1 shape, 500 iterations:
  **mean 192.05ms/iter (ON) vs 169.82ms/iter (OFF) — ~13.1% relative
  overhead**, against the pre-fix measurement's ~12.3% (172.31ms vs
  193.50ms) — a ~0.8 percentage-point difference, well within this
  shape's own observed run-to-run noise (stdev 13-16ms on both runs).
  **The ring buffer itself adds no meaningful new cost** — the atomic
  ops and larger per-communicator footprint (792 KiB vs one `~3.2KB`
  struct) are not measurably more expensive than the lock it replaced.
  Dump volume for the same 500-iteration run: 553MB (patched) vs 395MB
  (unpatched) — a real, expected increase, since the fix now writes
  every real completed collective instead of silently collapsing bursts
  into one record.

**Follow-up — MoE/DLRM ring-buffer capacity, investigated further; burst
characterized as unbounded-looking, not just "needs a bigger number"**:
4 real MoE runs (256- and 4096-capacity, same 300-step config) showed
drop counts of 2, 592, 875, and 2,212 — a ~1000x spread for IDENTICAL
code and config. Direct analysis of the 875-drop case found the real
cause: a genuine, **sustained** (not instantaneous) production-rate
burst lasting ~2.7 real seconds, concentrated late in the run (~92-96%
through), affecting a different communicator and different ranks each
run — not the same expert/rank every time, and the affected rank's own
real token-routing load (`[MOE-TOKEN-LOAD]`) during the burst was
**within its own normal range**, not an outlier — ruling out "this
rank's own routing imbalance" as the direct cause. The dump thread
itself was confirmed still draining normally throughout (gaps <15ms,
same as quiet periods) — ruling out a consumer stall. The real root
cause remains undetermined (plausibly a downstream/peer-side
synchronization effect), and the drop magnitude shows no sign of
converging to a safe ceiling across the 4 samples gathered. **Given
this, the data does not support "just pick a bigger number" — this
looks like a large-but-not-clearly-bounded tail, not a well-characterized
peak rate.** No capacity change has been made; this is reported as an
open question for a deliberate decision, not resolved here.

**Follow-up — the two validated overhead fixes, implemented**:
1. **`gRetireLock` scoped from one process-wide mutex to one per
   communicator** — confirmed, by re-reading P32's own docstring before
   narrowing it (not assumed safe from the pattern alone), that this can
   only ever *increase* the real-time retention safety margin P32's
   design relies on for any given communicator (no longer sharing queue
   depth with other communicators' retirements), never decrease it.
   Real new correctness point this introduces and had to be handled: a
   per-comm queue, unlike the old process-wide one, does not outlive
   every individual communicator by construction — any entry still
   waiting out its retirement window is now explicitly drained when that
   communicator itself is torn down, rather than silently leaked.
2. **`collEvtTrk` population/copy skipped entirely in lean mode** — the
   dominant cost inside `inspectorUpdateCollPerf`, confirmed (re-verified
   directly against current code, not assumed from history) to be read
   in exactly one place in the whole codebase (`inspectorCompletedCollVerbose`,
   itself gated behind the same verbose flag), so skipping the write
   when lean mode is active never exposes stale data to anything that
   reads it. Full behavior unchanged when verbose mode is explicitly
   requested.

Both built clean, `-Wall -Wextra`, zero warnings. Full revalidation: `tools/self_test.sh`
clean PASS; Megatron coll_sn completeness reconfirmed at 0.00% loss,
0 drops (unchanged). **Real overhead, re-measured twice (same
methodology, same shape) to account for this shared cluster's own
run-to-run noise**: 14.84% and 9.21% individually, **~12.0% pooled**
(n=980 each) — against the pre-fix ~13.1%. A small, directionally
real improvement, **not a dramatic one, and reported honestly as such**:
the measured run-to-run spread (9.2-14.8%) is itself larger than the
~1 percentage-point apparent gain, so this should be read as "roughly
consistent with 13.1%, with a modest improvement more likely than not"
rather than a precisely-quantified win.

**Follow-up — the 319,434-drop MoE outlier, resolved, not left
inconclusive**: one MoE sanity run with the patched plugin showed a
notably higher drop count than any sample gathered during the capacity
investigation above. Investigated directly rather than assumed either
way:
- **Config confirmed identical** to the capacity investigation's own 4
  samples (same unmodified `run_moe_rankfault.sh`, same 300 steps, same
  default fault-target rank, same 2 nodes) — the only real difference
  at the time was which Inspector build was loaded.
- **Re-ran 4 more times, controlling each suspect variable in turn**:
  Part-B-patched code (248,405 drops), the exact pre-Part-B
  ring-buffer-only code re-extracted from its own commit (308,959) —
  ruling out Part B's fixes as the cause — and a completely fresh,
  empty dump directory (324,629) — ruling out accumulated dump-file
  count. All 5 samples (319,434/248,405/253,099/308,959/324,629) landed
  in the same **248K-325K range, with all 16 ranks affected every
  time** — a tight, repeatable cluster, not noise, and categorically
  different from the capacity investigation's own 4 samples
  (2/592/875/2,212, each affecting only 1-2 ranks). **This does not
  belong to the same distribution as the capacity investigation's
  samples.**
- **Found a real, plausible contributing factor, external to this
  plugin and to this package entirely**: `dmesg` shows this host
  receiving frequent, external `echo 3 > /proc/sys/vm/drop_caches`
  calls (full page-cache drops) throughout the session — present
  during the capacity investigation's own window too, so not a clean
  on/off switch, but measurably **increasing in frequency** over time
  (roughly every 5-10 minutes early on, tightening to every 2-4 minutes
  by the time of the high-drop re-checks). This is triggered from
  outside this environment's own visibility (confirmed: this package
  runs inside a chroot/jail with no access to whatever schedules it) —
  not something this investigation can disable to test in isolation,
  and not something a code change in this repo can fix.
- **Plain conclusion**: the 319,434 figure (and its 4 reproductions) is
  real evidence of a separate, host/platform-level condition — not a
  ring-buffer or Part-B regression (both directly ruled out), not
  "just another point" on the capacity investigation's own distribution
  (ruled out by the 100x+ gap and the all-ranks-vs-1-2-ranks pattern
  difference). It needs its own, separate investigation by whoever owns
  this host's platform-level cache-management behavior -- out of this
  package's own scope to fix.

### 6.10 Cyril item-6: sacct job context -- a second, authoritative source alongside squeue

**Investigation first, confirmed before writing any code**: this cluster has
no "Storm API" (zero matches anywhere in this repo, no Soperator Slurm-job
CRD found, `kubectl` unavailable from this environment) -- that specific
integration remains genuinely unresolved and out of scope. What the
investigation did find, real and already installed: `sacct` works on both
the login node and worker-0/worker-1, and returns real `Start`/`End`
epoch timestamps plus real `NNodes`/GPU-rank counts for both running and
completed jobs -- strictly more authoritative than the `squeue -h -o "%i"
--states=R` presence-only check this pipeline has used everywhere since
its own original import (confirmed via grep: nothing in this pipeline
used `sacct` before this). This is the same bug class already patched
reactively multiple times in this project's own history (PP's role-
baseline contamination, DLRM's stale throughput reference, Hybrid's
cold-start gap) -- a long-lived aggregator inferring job boundaries
purely from squeue's live presence.

**Design decision, made explicit**: `sacct` **supplements** the existing
squeue-based mechanism; it does not replace it. `maybe_reset_workload_state()`
is untouched -- job-boundary *resets* are still triggered exclusively by
`refresh_job_id()`'s own squeue poll, at the same `JOB_ID_REFRESH_S=60`
cadence as before. What `sacct` adds is a second, independently-sourced
signal used three ways:

**1. A disagreement cross-check** (`check_sacct_squeue_disagreement()`):
logs (never acts on) a `[SACCT-SQUEUE-DISAGREEMENT]` line if squeue still
attributes live activity to a job that sacct's own authoritative record
says already reached a terminal state -- exactly the race
`alert_engine.py`'s own `_job_still_running()` docstring already
describes squeue as vulnerable to. **Real validation, zero false
positives**: across this entire deployment window, including two full
real job-boundary transitions from live fault-injection runs (jobs 3658
and 3659), `grep -c SACCT-SQUEUE-DISAGREEMENT` on both hosts' logs is
**0** -- the two signals never disagreed in practice here, and the
mechanism never fired spuriously.

**2. Real job context on `[STRAGGLER-INCIDENT]`/`[ALERT]` text**
(`alert_engine.py`'s `_query_job_sacct_info()`), informational/display
only -- confirmed, by `determine_confirmed_path(cause)`'s own signature
(it receives only `finding["cause"]`), that `finding["sacct_info"]` is
structurally unreachable from tier/decision logic, not just
conventionally kept separate. Real rendered output, from a live
fault-injection run (job 3659, rank 916244 on worker-1):
```
[STRAGGLER-INCIDENT] incident_id=worker-1:0x71cb232b5c1fb6:916244:2286960:AllReduce stat=cv host=worker-1 comm=0x71cb232b5c1fb6 member=916244 gpu_slot=0 bucket=2286960 coll=AllReduce severity_ratio=520.01 persisted_s=53.9 role_rank=8 role_n=16 job_elapsed_min=3.0 job_start=2026-10-02T08:12:08Z job_nnodes=2 job_nranks=16

[ALERT] rank=916244 comm=0x71cb232b5c1fb6 node=worker-1 type=compute confidence=PROBABLE severity=LOG-ONLY ...

Job context (informational only, from sacct -- does not affect confidence tier): 3.0 min into a job running since 2026-10-02T08:12:08Z, out of 2 node(s)/16 rank(s) allocated.
```

**3. Grafana job-boundary annotations** -- two new Prometheus-datasource
annotation layers on the existing dashboard (`agg_job_start_time_seconds`
/`agg_job_end_time_seconds`), sourced from sacct's real `time.start`/
`time.end`. Confirmed zero job-boundary markers existed in the dashboard
JSON before this change (clean 26-insertion diff, no reformatting). The
real start/end epoch is encoded as the **sample's own timestamp**
(deliberately backdated, not "now") -- confirmed live, via a real
push+export round-trip against this cluster's VictoriaMetrics, that a
backdated sample timestamp is stored and returned correctly; Grafana's
native annotation query places each marker at the data point's own
timestamp, not at a value reinterpreted as time.

**Real bugs found and fixed during this feature's own validation (not
assumed correct from the design alone)**:
- **Missing `cluster` label**: the first working version pushed these
  two metrics without the `cluster="..."` label every other metric in
  this file carries via `base_labels()`. The dashboard's own annotation
  query filters on `cluster=~"$cluster"` -- a label-absent series does
  not match a non-empty regex value, so the annotations would have
  silently rendered nothing. Fixed by adding the label to match
  convention.
- **Job-end race, found via a real missed case (job 3658)**: `squeue`'s
  own 60s poll and `sacct`'s own 60s poll are independently gated, no
  shared phase. Once squeue stops reporting a job, `self.slurm_job_id`
  goes empty immediately -- and the original code unconditionally
  skipped the sacct check once that happened, so a job whose squeue
  presence disappeared before sacct's own next 60s check landed would
  **never get one more query to observe its real End time at all**.
  Confirmed directly: job 3658, a real ~3.5-minute fault-injection run,
  never got its end-time pushed under the original code. Fixed by
  letting `refresh_job_sacct_info()` target the last-cached job id for
  one more check when squeue's own id has gone empty but that job's end
  was never confirmed -- re-validated on the very next real job (3659):
  end-time pushed correctly, ~65s after the job's real end, both hosts.

**Real validation evidence, from two live fault-injection runs after
both fixes were deployed**:
- `tools/self_test.sh`: clean **PASS** both times, exact rank-match
  (injected PID == alerted PID), 0 new `CHECK-FAILED`.
- Real multi-job sequence, boundaries confirmed correct: `3654 → 3655 →
  (3656, 3657) → 3658 → 3659`, each a real, distinct `workload-signature
  tracking reset: new job <id>` line at the real boundary, no spurious
  mid-job resets.
- `agg_job_start_time_seconds`/`agg_job_end_time_seconds`, confirmed via
  direct VM query (both `/api/v1/query` instant and a `/api/v1/query_range`
  matching Grafana's own annotation query execution path) for job 3659:
  real start `1790930133` (`2026-10-02T08:35:33Z`) and real end
  `1790930351` (`2026-10-02T08:39:11Z`), both hosts, correct `cluster`
  label, exact sacct-sourced epoch (not quantized -- the small offset a
  step-grid range query shows is that query's own evaluation-grid
  artifact, not the stored data, which carries the raw epoch exactly).
- **Job-boundary reset latency itself: unchanged, by design.** The
  reset trigger is still squeue-only (`JOB_ID_REFRESH_S=60`), per the
  explicit "supplement, not replace" scope for this change -- sacct adds
  a cross-check and real context, not a faster reset path. No regression
  and no speed claim either way on the reset itself.

**Known, disclosed limitation**: a job that starts and ends within the
same ~60-120s window (faster than both independent polls can settle)
can still race the job-end fix above in the unlikely case a *third* job
starts before sacct gets its one extra look at the previous job's end --
the cached last-job-id check is abandoned the moment `self.slurm_job_id`
reports a new, different real job. Not observed in this validation's own
runs (all several minutes apart), not fixed further here -- out of this
task's own scope to chase an edge case with no real reproduction.

**Explicitly excluded from this change, per scope**: "Storm API" is
unresolved and untouched -- nothing here guesses at or builds toward it.

## 7. Known limitations (read this before relying on any alert)

**Behavior change: the mean-path check now requires 3 consecutive
windows, same as CV already did — it no longer pages on a single
window.** This is a deliberate tightening of existing alerting
behavior (not a side effect of adding `straggler_incident_detected`
above), closing an asymmetry: CV detection already required
`T.PERSIST_REQUIRED=3` consecutive windows above threshold before
firing at all; the mean-path check (`_check_mean`) and the
corroborating-only outlier_count check did not — either could
previously fire `_emit()` (and, for mean, a CONFIRMED/PAGE alert, if
DCGM/storage also corroborated a cause) off a single qualifying
~100-sample window. Concretely: **a straggler that previously paged
after `rank 151479 · compute straggler · sustained · PROBABLE /
Arrival lag 60.49x node peers (mean, z=84.8)` fired on ONE window now
requires that same condition to recur for 3 consecutive windows before
`_emit()` is even called.** If your own alerting/dashboards depend on
a mean-path finding firing the instant a single window crosses
threshold, this is a real behavior change to account for.

**KNOWN, NOT YET FIXED: a specific comm-local role position gets
persistently misattributed as "worst," independent of any real fault.**
Found live during the Cyril item-2 cascade investigation (a real
Megatron TP4/PP4/DP3 GPU-clock-fault run, job 3570): rank 12
(role_rank=0 within its own 4-member TP group, the last pipeline stage)
fired 13 times during the run, versus 0-1 times each for its three
TP-peers in the identical comm — a persistent, structural skew, not the
noisy/rotating "different member each time" pattern every other
downstream stage showed. This closely resembles this project's own
already-documented "rank 0's already-known, un-excludable overhead
bias" (see `_rank_dependents_by_deviation`'s own docstring), recurring
here as a TP-group-local role_rank=0 bias rather than a global-rank-0
one. Real, measured (not a false positive), but likely a workload-
structural artifact rather than a genuine fault signature — worth its
own dedicated fix, independent of and not blocking the evidence-
inspectability work above. Not fixed as part of this work; tracked here
as an open item.

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

**ResNet's jitter fault re-confirmed as real, silent non-detection (not
misattribution)**: re-ran ResNet's own `INJECT_ENABLE=1` burst/jitter
fault (rank 4, `INJECT_BURST_MS=10`/`INJECT_PERIOD_MS=100`, the script's
own defaults) for a full 8000-iteration, ~7-minute run — well past the
120s calibration grace period. Real fault confirmed injected (own
`[inject] rank=4 ENABLED` log line); the target's own NCCL Inspector
exec-time data showed **no measurable elevation at all** versus peers,
and **no alert fired for it, of any tier or rank, for the whole run**.
This is not a misattribution — it's a genuine, real absence of a
collective-level signature for this specific burst/jitter mechanism, and
it's exactly consistent with (real, direct evidence for) the V1 scope
note directly above: jitter-class faults are not production-hardened in
this release.

**Overhead — the real, measured numbers, not a summary**:
- The one direct throughput-overhead measurement in this project's own
  history found Inspector's profiling overhead **dominating ResNet's
  iteration time by ~4.5x** (85ms with Inspector vs. 19ms without vs.
  7.57ms bare single-GPU compute) — it is **not documented** whether
  this was measured with `NCCL_INSPECTOR_DUMP_VERBOSE=1` or `=0`, or a
  more verbose debug mode still above that. Treat per-workload overhead
  as a real open question to measure on your own workload, not as
  negligible-by-default. An A/B "monitoring-off" harness exists
  (`workloads/nanogpt/run_shape1_nomonitor.sh`) for exactly this
  comparison; no documented result from actually running it was found
  in this project's own history.
- **A second, independent real measurement, different topology/scale —
  added alongside the ResNet number above, not replacing it**: a real
  48-GPU Megatron TP4/PP4/DP3 validation run measured **94.7ms/iter with
  Inspector on vs. 89.0ms/iter off — ~6.5% relative overhead** at full
  48-rank, multi-communicator scale. **Conditions, confirmed directly
  against this project's own commit timeline, not assumed**: this number
  was recorded (`6932b2e1`, as part of that same validation's writeup)
  roughly one hour *after* the lean-mode default flip commit
  (`f89230e2`) — and that flip commit's own message states plainly it
  was triggered by *this exact run*: "verbose Inspector dumps filled a
  91GB shared volume and crashed the pipeline." The Megatron workload's
  own launch scripts were not added to this repo until many hours later
  (`10cbdb94`), so the exact script invoked for this measurement isn't
  preserved in git history — but the causal link is clear: this number
  was measured **before lean mode existed as a default at all**, on the
  run that is on record as crashing specifically *from* verbose-mode
  dump volume. It also predates every one of this session's fixes
  (persistence-gating generalized to mean/`outlier_count`,
  `straggler_incident_detected`, the z/mm staleness fix, Path C
  window-matching, the worst-selection sign-bug fix — all committed
  hours to a full day later). Treat this number as **pre-lean-mode,
  pre-this-session**, not as a lean-mode baseline.
- **Re-measured under current conditions (lean mode confirmed, every
  fix above applied) — added alongside both numbers above, not
  replacing either**: this project's 2-node/16-GPU dev cluster can't
  run the full 6-node/48-GPU shape, so the real, **unmodified**,
  already-committed `workloads/megatron/` scripts were run as-is — they
  auto-discover the available nodes and, on 2 nodes, naturally produce
  **TP4/PP4/DP1** (16 ranks) — exactly *one* real DP replica of the
  original topology's own documented per-replica layout (2 nodes/replica,
  2 PP stages/node, TP4/stage), a genuine proportional scale-down, not an
  approximation. Two full, back-to-back 500-iteration runs (steady-state
  stats below exclude the first 10 warmup/compile iterations; iteration 1
  alone took 17.5s/20.4s off/on, pure CUDA/Triton/NCCL init cost, clearly
  separable from steady-state):
  - **Off** (Inspector fully disabled — no `NCCL_PROFILER_PLUGIN`, no
    `NCCL_INSPECTOR_ENABLE` at all, same convention as
    `train_node_shape1_nomonitor.sh`): mean **172.31ms**/iter, stdev
    17.87ms, min 153.1ms, max 519.1ms, median 171.00ms (n=490).
  - **On** (Inspector enabled, lean mode — confirmed genuinely active by
    direct inspection of the real dump records themselves, not just the
    script default: zero `event_trace_ts`/`event_trace_sn` fields found,
    only the lean-only `dump_timestamp_us`, ~457 bytes/record, matching
    the ~462 bytes/record lean-mode figure already on record above):
    mean **193.50ms**/iter, stdev 16.79ms, min 173.4ms, max 489.9ms,
    median 190.70ms (n=490).
  - **Real overhead at this shape/scale: ~21.2ms absolute, ~12.3%
    relative — meaningfully higher than the 6.5% figure above, not
    roughly consistent with it.** Given the conditions finding directly
    above, this is not surprising in the direction one might first
    guess: the 6.5% figure is suspected to have been measured under
    *verbose* mode (normally the more expensive mode), yet this *lean*-
    mode number reads higher, not lower. The most likely real
    explanation is topology, not verbose-vs-lean: the two measurements
    are at genuinely different absolute scale (89.0ms/iter baseline at
    48-rank DP3 vs. 172.31ms/iter baseline at 16-rank DP1 here — nearly
    2x the per-iteration cost before Inspector is even added), and DP1
    means every rank is on the critical path with no data-parallel
    replica to overlap communication against, unlike DP3. This is
    reported as the most plausible explanation given what this
    investigation could check, not a proven root cause — profiling
    exactly where Inspector's runtime cost goes at this shape is real,
    separate follow-up work, not attempted here.
  - **Disk volume (lean mode, this shape)**: 395MB total dump volume
    across both nodes for these 500 iterations (~96s wall time) — per-
    record size confirms lean mode (above), but the *accumulation rate*
    (~123MB/min/node) is markedly higher here than the ~28.5MB/min/node
    figure already on record for `nanogpt`'s simpler DDP-only
    communication pattern — expected, not a regression: TP4/PP4
    generates far more real NCCL collective calls per iteration than a
    pure data-parallel AllReduce pattern, so lean mode's *per-record*
    reduction holds, but the *total accumulated volume* is real and
    workload-dependent, not a single universal rate.
  - **Disk-usage guard**: both nodes' root volume held at a steady,
    pre-existing 92.7% throughout this entire test (unrelated to it —
    same value before, during, and after; a real, already-filled 2TB
    volume with only 150GB free, flagged separately, not fixed here).
    `[DUMP-DISK-WARN]` kept firing at its already-standing rate; the
    95% `[DUMP-DISK-CRITICAL]` threshold never fired — the guard behaved
    correctly, neither silent nor falsely escalating, for a normal run
    of this length.
- **`NCCL_INSPECTOR_DUMP_VERBOSE` now genuinely defaults to lean mode
  (`=0`) across all 17 launch scripts that set it** — a real, previously
  undocumented discrepancy existed here (this section used to correctly
  flag that 14 of the 15 checked scripts actually ran `=1`, contradicting
  `INVENTORY.md`'s claim that lean mode was already the default; that
  discrepancy is now fixed at the source, not just in the docs). This
  session's own real 48-GPU Megatron validation run hit the verbose-mode
  cost directly: verbose dumps filled a 91GB shared volume and crashed
  the pipeline. Traced field-by-field against the Inspector plugin's own
  C++ source and empirically A/B-validated on this project's own
  regression-reference shape (`workloads/nanogpt/run_straggler_nanogpt.sh`,
  rank 3, 200ms injected sleep) before flipping anything:
  - **Same detection, both modes**: identical `PROBABLE`/`LOG-ONLY` tier
    and exact rank/host attribution under verbose and under lean — the
    live pipeline (`node_aggregator_ref.py` → VM → `alert_engine.py`)
    only ever reads `coll_exec_time_us`/`coll_msg_size_bytes`, emitted
    identically in both modes; the verbose-only `event_trace_ts`/
    `event_trace_sn` fields are read by zero code in the live path.
  - **~8.2x smaller per record** (3,804 vs. 462 bytes/record, measured
    across full dump files from the same fault scenario), and **~8.6x
    less disk accumulated** for the same workload (87MB vs. 8.4MB/node
    over a comparable window).
  - **A real caveat, not just a win**: a genuinely healthy job (not
    throughput-throttled by an injected fault) accumulates disk much
    faster in wall-clock terms than a fault-injection test suggests —
    measured **~28.5MB/min/node even under lean mode** on a healthy
    16-rank run (the fault scenario's own injected sleep synchronously
    throttles every rank's collective, so it under-represents real
    production disk cost). Lean mode is a real, substantial reduction,
    not a guarantee against filling disk on a long real run — see the
    disk-usage guard noted elsewhere in this document.
  - **A small number of offline, non-live tools still genuinely need
    verbose mode**: `classifier.py`'s batch dump-replay path,
    `calibration.py`, `transient_latency.py`, and the MoE two-stage
    detector's `arrival_order.py` all read the verbose-only
    `event_trace_ts.coll_start_ts` for microsecond-scale cross-rank
    timing (arrival order, clock-offset calibration) — `coll_exec_time_us`
    is explicitly documented in `arrival_order.py`'s own code as already
    proven unreliable for that purpose, and lean mode's only timestamp
    (`dump_timestamp_us`, recorded at write/flush time) carries the same
    kind of imprecision for a different reason. Opt in explicitly by
    setting `NCCL_INSPECTOR_DUMP_VERBOSE=1` in the shell that launches a
    `run_*.sh` script (propagated into the container via the existing
    `--export=ALL`) before using one of these tools.
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

**P27.2 timing-asymmetry fallback — false-positive risk on TP2 reproduced,
root-caused, and fix applied and validated (Part D is closed, below)**:
a real, live Stage 5
isolated-validation run found 5 real CONFIRMED/PAGE false positives on a
genuinely healthy (unfaulted) TP2 baseline via this exact fallback. A
same-session re-investigation suspected `peer_mad` computing to 0 or a
degenerate value silently no-oping the MAD gate, but could **not**
reproduce the false positives in a fresh 1250+-iteration healthy TP2
re-run in the time available, and left this genuinely unresolved.

A later V1 Beta closeout session tried a genuinely different method — a
much longer healthy TP2 soak (10,000 iterations, ~15 real minutes, 16
real ranks / 8 real pairs, zero `STRAGGLER_SLEEP_MS`) instead of another
short run — and **reproduced the storm decisively**: 10 of 16 genuinely
healthy members fired real CONFIRMED/PAGE alerts. This also corrected the
earlier suspicion: live `peer_mad` values at the moment of firing were
small but genuinely nonzero (3-13us, not degenerate/zero), and the real
peer-median baseline (105-114us) matched this fallback's own historically
validated real-fault case almost exactly — ruling out stale or
mismatched baseline data. The real, root cause is that this cluster's
own natural per-collective timing variance on individual healthy TP2
members widens enough, given enough elapsed samples in a long enough
run, to blow through the fixed `TIMING_FALLBACK_MAD_MULTIPLE=6` gate on
many pairs at once (observed gap/mad ratios: 26.5-423.9) — a genuine
**duration-dependent** false-positive risk that short validation runs
(the original 5-run MAD-fix validation batch, and this project's own
1250-iteration re-investigation attempt) were simply too short to ever
encounter. TP-inference (the other real 2-member shape using this same
fallback) has never shown this behavior.

**Fix applied and validated**: `TIMING_FALLBACK_STOPGAP_ACTIVE`
(`alerting/alert_engine.py`) has been re-activated (`True`) — the same
downgrade-to-PROBABLE/LOG-ONLY stopgap this project already built and
used for exactly this failure mode before the (now-shown-to-be-
insufficient) MAD-based fix was live-validated and the stopgap reverted.
This is deliberately the minimal, already-proven lever rather than a new
statistical redesign (a larger, riskier change): it keeps every real
detection visible (still emits, at PROBABLE/LOG-ONLY) while removing the
false-PAGE risk, until a genuinely duration-robust statistical gate is
designed separately. The supervised process was restarted via its own
normal path (`SIGTERM` to the leaf process → the existing supervisor
loop's own auto-restart, the same already-documented mechanism, not a
bare `kill -9`) — clean exit (code 143) to relaunch completed in 3
seconds, and the new process's start time was confirmed to postdate the
source file's own mtime before any validation began.

**Validation (n=5 healthy soak runs + 1 real-fault regression, all
post-restart)**: five independent 10,000-iteration (~15-real-minute)
healthy TP2 soak runs all showed **zero** `CONFIRMED/PAGE` alerts
sourced from this fallback (one incidental `CONFIRMED/PAGE` did occur in
run 2, traced directly to a real, independent, hardware-corroborated
Path B/DCGM clock-suppression event — `sm_clock=510 vs peer_median_
sm_clock=1980`, genuinely active power draw — a different detector
entirely, unaffected by and out of scope for this stopgap, so it does
not count against this fix). A real-fault regression run (rank 3,
`STRAGGLER_SLEEP_MS=200`) confirmed detection is fully preserved, not
lost or delayed: the real injected target was still correctly and
exclusively named, firing at ~3:04 elapsed (consistent with this
project's established fault-detection timing), now correctly capped at
PROBABLE/LOG-ONLY instead of the old false CONFIRMED/PAGE. `CHECK-
FAILED` stayed at 0 and the pipeline stayed healthy across every one of
these 6 runs. **Part D is closed.**

**Above-floor pure-software-delay faults cap at PROBABLE, never
CONFIRMED/PAGE**: distinct from the below-floor P27.2 case above. A
real, correctly-localized, high-confidence anomaly (Stage 5's own RL
validation: z-score up to 510, arrival lag up to 692x peers) on a
16-member (well above `SELF_DETECTION_FLOOR=3`) communicator stayed at
PROBABLE/LOG-ONLY indefinitely and never escalated to CONFIRMED/PAGE,
because the standard peer-relative CV path's CONFIRMED-tier escalation
requires corroborating DCGM (Path B, clock/throttle) or Path C (storage
I/O-wait) cause-evidence, and a pure Python-level `time.sleep()`
straggler produces neither — both read genuinely nominal/ambiguous, not
because detection failed but because there is no hardware signature to
corroborate. This is real, expected behavior given the design, not a
bug — but it means **no pure-software-only compute straggler, however
large and well-localized, will ever page a human via the standard
(above-floor) detection path** in this release. Previously undocumented;
added here after Stage 5 found it live.

**RL: an earlier "weak/inconsistent detection" finding in this section
was itself wrong — the fault was never actually being injected**:
Stage 5's own RL validation reached z=510, arrival lag 692x, correctly
localized. A later investigation session re-ran RL's fault injection 3
times and found the target's own z-score stayed weak (roughly 2-7) and
once even saw a healthy neighbor rank misfire instead — and concluded,
incorrectly, that this was a real signal/attribution gap. Root cause,
found in a follow-up session: every one of those reruns (and an
earlier same-session silent run) launched `run_rl.sh` with
`STRAGGLER_PHASE=none` as the phase argument. `train_rl.py` only
injects its `time.sleep()` when `STRAGGLER_PHASE` is `rollout`,
`forward`, or `backward` — `none` matches none of the three conditions
and is silently a full no-op. Every one of those "weak signal" test
runs was, in reality, a completely healthy job; the weak z-scores and
the one stray misfire were ordinary healthy-job noise, not a detection
gap. Corrected re-runs with `STRAGGLER_PHASE=forward` (5 independent
runs, same target rank each time) confirmed reliable, correctly-
attributed detection in all 5: the injected target's own exec time
read 294-350us against ~200,000-205,000us for every peer (~600-700x
elevation, closely matching Stage 5's own 692x), z-scores in the
hundreds, and a real `confidence=PROBABLE` alert naming the exact
injected target rank every time, firing between ~3:41 and ~3:56
elapsed in each run — fully consistent with the original Stage 5
result. **RL's detection was never broken.** The lesson that mattered
here was about this project's own validation harness, not the pipeline:
`STRAGGLER_PHASE` must always be set to a real phase value when
injecting an RL fault — leaving it unset/`none` silently disables the
fault, and a silent no-op fault test is indistinguishable from a real
detection gap unless the underlying signal is checked directly (which
is what caught this).

**Hybrid: the below-floor P27.2 fallback never fires, despite a very
strong raw signal, because this topology has no live peer sibling to
compare against**: a dedicated investigation (2 independent fault-
injection reruns, same target rank both times) found a very strong,
consistently-reproduced partner-elevation signature via direct
`agg_mean_exec_time_us` inspection — the injected target read
17-56us while its healthy TP partner read 5,600-28,200us (a 300-500x
ratio, reproduced almost identically across both runs, and
considerably stronger than PP's own successful 36x signal on the
same mechanism) — yet zero alerts ever fired, over 5+ minutes each
run. Root-caused directly against `_timing_asymmetry_fallback_
evaluate`'s own docstring in `alerting/alert_engine.py`:
`_cross_comm_peer_median` requires an EXTERNAL peer group from
another below-floor comm with genuinely DIFFERENT physical members
on the same host to establish its baseline (self-history was
deliberately abandoned earlier in this project's history due to its
own false-positive risk — see P27.2.3 above). Confirmed directly via
VM query, in both reruns: every below-floor comm active on the
target's host shares the exact same 2 physical members — Hybrid's
2-ranks-per-node layout means the local TP pair IS the same physical
pair that also forms the cross-node PP send/recv endpoint on this
node, so there is no independent same-shape comm with different
members to serve as a peer pool. This is precisely the gap the
fallback's own docstring already discloses ("returns None, honestly,
whenever no OTHER same-shape comm is currently active to serve as
the peer group... a real, disclosable residual gap for a workload
whose below-floor comm has no live sibling at all, not a bug") —
confirmed here as the real, reproducible cause for this specific
2-ranks-per-node topology, not a timing issue and not a weak signal
(the signal is unusually strong; there is simply nothing live to
compare it against). A real fix would mean either falling back to
`_member_role_baseline` more aggressively when no live peer exists
(risking reintroducing the cold-start/self-history problems P27.2.3
already moved away from) or a genuinely new baseline mechanism for
topologies where TP and PP ranks coincide on the same physical node —
both are real design changes beyond this session's scope. Disclosed
here rather than forced.

**Hybrid follow-up (V1 Beta Stage 5 re-run): this gap is real but
condition-dependent, not absolute** — a fresh Stage 5 validation session
re-ran Hybrid's exact fault scenario (same target-rank convention) on a
**genuinely fresh VictoriaMetrics instance** (no prior cross-job history
at all) that had, by the time Hybrid ran, already accumulated real
`agg_mean_exec_time_us` data from several earlier same-session shapes
(TP2/TP4/FSDP/etc.) sharing the same physical GPU slots and message-size
buckets on these same two nodes — and this time the fault **did** fire, a
real `PROBABLE` alert correctly naming the injected target
(`_cross_comm_peer_median`'s `baseline_source='cross_comm_peer'`/`'role'`
paths both observed live in the surrounding trace). This does not
contradict the root cause above — it confirms it precisely: Hybrid's
detection works exactly when a genuinely external peer or cross-job role
history happens to be available, and fails exactly when it isn't (a truly
isolated Hybrid run, or the very first run of its kind against a cold VM,
still has nothing to compare against). Treat this as "real but
environment-dependent," not "fixed" — a testing session run in isolation
(the original finding's own condition) will still very likely see it fail.
**Long-context (also below-floor, same mechanism) hit the cold-start case
directly in this same re-run**: a strong real signal (~100-300x elevation
on the waiting partner, same inverse pattern) produced zero alerts,
because it was long-context's own first-ever run this session and its
message-size buckets are unique to it — no existing sibling or role
history yet. Same mechanism, same root cause, opposite outcome, purely
because of what else happened to have run earlier on this cluster.

**Hybrid: FIXED and validated (P27.5) — the genuine cold-start case above
is now resolved, not just condition-dependent.** Root cause of the fix
direction: `_cross_comm_peer_median`'s live-peer pool (`alerting/
alert_engine.py`, ~line 1820) was scoped to same-host-only, but Hybrid's
own topology (2 ranks per node — a TP pair per PP stage) means each
node's local below-floor comm has no OTHER same-shape comm on that SAME
host to serve as a peer, by construction (the local TP pair IS the local
PP endpoint) — this is exactly why the original "never fires" finding
was genuine, and exactly why it "worked" only when unrelated leftover
data from earlier same-session shapes happened to be lying around.
**Real fix**: relaxed the live-peer pool from same-host to job-wide (any
host in the same Slurm job), with explicit `slurm_job_id` scoping added
to the primary live-peer branch (which previously had none at all,
relying only on the hostname filter this change removes) to prevent a
different job's stale below-floor comm from contaminating the pool —
falls back to the original same-host-only scoping if the job id can't
be determined, never searching job-wide unscoped. This means, for
Hybrid specifically, worker-0's own TP pair now serves as a genuine,
physically-different-member peer for worker-1's TP pair in the SAME
job (and vice versa) — a real property of Hybrid's own topology (every
real Hybrid deployment has at least 2 workers, each with its own local
TP pair), not a test-only convenience, so this closes the cold-start gap
for a genuine first-ever production Hybrid job on a fresh cluster too,
not just this project's own test sequencing.
**Considered and rejected**: deliberately seeding a compatible below-floor
shape (e.g. a plain TP2 job) immediately before Hybrid's own test, as a
test-setup convention rather than a code change — rejected as not a real
fix for a real deployment (a customer running Hybrid as their first/only
workload on a fresh cluster would still hit the cold start; Hybrid's own
multi-worker topology already provides everything the job-wide code fix
needs, without requiring an unrelated shape to run first).
**Validated live, n=5 independent genuinely-isolated Hybrid runs** (no
other shape run beforehand in the same job — the real cold-start
condition, not the "something else happened to run first" condition the
original finding depended on): 5/5 fired a correct `PROBABLE` alert with
exact rank+host attribution; run 1 additionally cross-checked directly
against VictoriaMetrics, confirming `baseline_source='cross_comm_peer'`
sourced from the OTHER node's TP pair in the same job, exactly the new
mechanism (before this fix, the same query would have found no peer at
all for this exact scenario).
**Regression-checked**: TP2 — fault-injection detection unchanged (exact
rank/PID match preserved); a ~6-minute healthy soak produced zero new
`CONFIRMED`/`PAGE` alerts (unchanged count), consistent with
`TIMING_FALLBACK_STOPGAP_ACTIVE` already keeping this fallback's tier
below PAGE-worthy. PP — unaffected either way: a plain 2-rank-per-node PP
job has no other below-floor comm in the same job to serve as a job-wide
peer regardless of this change, so `_cross_comm_peer_median`'s modified
code path isn't reachable differently here; PP's own separately-diagnosed
role-baseline contamination issue (see below) is unrelated to this fix
and remains open.

**Hybrid: a second, different role-baseline gap found and FIXED (V1 Beta
Stage 6) — baseline PROVENANCE, not attribution correctness.** A
confirmation-pass re-test (3/3 runs, correct attribution every time)
noticed the fired baseline for role_rank=1 was suspiciously small
(39.22) against a well-populated, consistent 6-job majority cluster
(role1 ~24k-28k) that should have been available. Root-caused precisely:
Hybrid's own testing convention injects the fault on the SAME target
rank every single run, so `_push_role_baseline_exclusion` (the existing
anti-poisoning safeguard — working exactly as designed, not a bug in
itself) correctly excludes role1's reading as anomalous on all 9 of
Hybrid's own real runs, draining its non-excluded survivor pool down to
a single outlier entry (job 3369, a different, unrelated historical
run that happened to share this exact label combination). `_member_
role_baseline` had no minimum-sample-size floor, so this single
unrepresentative survivor was silently treated as a fully valid
baseline instead of degrading to "not enough data." **Fix**:
`ROLE_BASELINE_MIN_HISTORY = 3` (`alerting/alert_engine.py`, reusing
this project's own established "3 independent data points" precedent —
`THROUGHPUT_XJOB_MIN_HISTORY`, `PERSIST_REQUIRED`/`PERSIST_WINDOW`) —
`_member_role_baseline` now degrades to `(None, None)` below this floor,
routing the caller to the already-validated `_cross_comm_peer_median`
fallback instead. Does not touch PP's own baseline (10 of 40 real
historical entries survive un-excluded there, comfortably above the
floor). Mechanically verified against real historical VM data (role1's
real pool confirmed to have exactly 1 non-excluded survivor, well under
the new floor) and confirmed live that a workload whose own testing
convention has no live peer sibling AND has drained its role-baseline
pool this way correctly falls through rather than firing on a
mismatched, unrepresentative value.

**PP (Shape 9): root-caused — a self-reinforcing cross-job history
contamination, not a code regression and not the same mechanism as
Hybrid's gap.** A dedicated follow-up session traced this precisely by
directly invoking `_timing_asymmetry_fallback_evaluate` against a live
PP run: `_comm_cross_node_members` correctly discovers both real members
across both nodes (the P27-hotfix4 cross-node fix already in this file
works exactly as documented) — the function does NOT return None at the
member-discovery step. It returns None later, because `_member_role_
baseline`'s cross-job history pool for BOTH of PP's roles is 100%
contaminated: every single historical `agg_mean_exec_time_us` entry ever
recorded for PP's role_rank=0 and role_rank=1 (confirmed directly, all
of them, across every job in this cluster's history) reflects the
identical target-rank/200ms fault convention this project's own testing
has always used for PP — there is no genuinely healthy PP history
anywhere. This project already built a real anti-poisoning safeguard for
exactly this (`_excluded_role_pool_members`/`_push_role_baseline_
exclusion`, dropping any (comm,member) a role pool has ever seen
successfully flagged as anomalous) — but it only engages on a
SUCCESSFUL fire, and PP's fallback has never once fired, so the
safeguard has never had a chance to exclude anything. The result: the
current run's fault looks statistically normal (ratio ~1.0 for both
members) because the "healthy" baseline it's being compared against IS
that same fault, repeated. This fully explains the "36x successful
signal" this document previously cited: that measurement most likely
predates this contamination (a clean or empty history at the time), and
does not generalize once this project's own repeated, parameter-
identical fault testing accumulates — a real, reproducible, self-
inflicted regression, not a code change and not a discovery-path bug.
**FIXED (V1 Beta Stage 6) — the circular dependency above turned out not
to need either of the two design changes originally proposed here.** A
dedicated follow-up session found the REAL blocker one layer down:
`node_aggregator_ref.py`'s `comm_calib`/`comm_bucket_members` (the state
`workload_signature()` reads) were never reset per job in a long-lived
aggregator process (the same root cause DLRM's own gap below shares) —
every job that ever reached stabilization got a unique, never-repeating
signature, so `_member_role_baseline`'s cross-job sig-matching (P27-
hotfix7) could never find ANY historical match at all, for either role,
regardless of contamination. Once that's fixed (`maybe_reset_workload_
state()`, called on every real job-boundary transition), PP's role-
baseline mechanism works correctly with **zero changes needed to
`alerting/alert_engine.py`** — validated live, n=5 independent
fault-injection runs across real job boundaries (jobs 3426-3430), every
one firing `baseline_source='role'` against the same stable, correct,
clean-magnitude baseline (~5359.93, ratio ~38x each time), with exact
correct rank+host attribution every time. The original "100% contaminated,
self-reinforcing" diagnosis above was real and correctly described VM's
actual accumulated data at the time — what it missed was that, once
signatures correctly and stably discriminate again, a NEW job's own
correctly-scoped signature no longer coincidentally matches that old,
uniquely-signed contaminated history at all, so genuinely healthy runs
recorded going forward populate a fresh, uncontaminated pool without
needing either of the fire-independent sanity check or varied-fault-
parameter proposals originally floated here.

**DLRM (Shape 13, with-Inspector variant): both the throughput-reference
wiring bug and the workload-signature job-scoping bug are now FIXED and
validated (V1 Beta Stage 6).** Root-caused directly: `node_aggregator_ref.py`'s
`_throughput_rate_ref` (the reference `agg_job_throughput_ratio_to_
baseline` divides by) was a per-aggregator-**process**-lifetime value,
established ONCE by whichever job first reached stabilization and never
reset — confirmed live with real numbers: DLRM's own real raw event rate
was ~2.0-2.3x an earlier, completely unrelated job's stale ~110/sec
reference, and — the same bug's other face — a later diagnostic PP job's
own real rate read as low as 0.13-0.47x that SAME stale reference,
producing real false `CONFIRMED/PAGE` `uniform_slowdown` alerts on
otherwise-unremarkable runs. One root cause, opposite symptoms, purely
depending on which side of an irrelevant reference a given workload's
real rate happens to fall. **Fixed and validated**: `node_aggregator_
ref.py` now tracks which real `slurm_job_id` established the current
throughput reference and resets it (and every supporting stabilization
counter) the moment a genuinely new job is detected — validated live
across 4 consecutive real jobs (DLRM x2, then DLRM-faulted, then
nanoGPT), each establishing its own fresh, job-appropriate self-
calibrated reference (4704/sec, 5048/sec, 240/sec, 110/sec respectively
— correctly tracking each job's own real, wildly different natural
rate) with zero cross-contamination, and a regression check (the same
nanoGPT run) confirming per-rank fault detection is completely
unaffected and no new false `uniform_slowdown` fired. **The second issue
disclosed here — `comm_calib`/`comm_bucket_members` (the dicts
`workload_signature()` reads to build each job's cross-job-matching
"sig") never being reset per job in a long-lived aggregator — is now
FIXED (V1 Beta Stage 6)**, confirmed live: DLRM's own sig's `n_comms` had
climbed 53 -> 54 -> 55 -> 56 across consecutive, unrelated jobs before
the fix (these dicts accumulate every comm/bucket/collective type this
aggregator process has EVER seen, across every job, not just the
current one), meaning two runs of the exact same workload essentially
never produced a matching sig, so `query_throughput_history`'s cross-job
lookup could never find a match (confirmed live pre-fix: DLRM's faulted
validation run found "0 historical run(s)" despite 2 real prior DLRM
runs already having pushed their own reference). **Real fix**:
`maybe_reset_workload_state()` in `node_aggregator_ref.py`, called on
every real job-boundary transition (same trigger, same call site
pattern as this fix's own `_throughput_*` reset above) — resets exactly
`comm_calib`+`comm_bucket_members`, deliberately narrow (does not touch
`self.state`/`phys_comm_role`/`phys_gpu_slot`, already implicitly
job-scoped by never-reused `comm_id` keys). **Validated live across 5
real job-boundary crossings**: two same-shape PP jobs correctly produced
the identical sig and correctly found each other as cross-job history
("1 historical run(s) found," not "0"); a deliberately different shape
(nanoGPT DDP) got its own distinct signature, not merged. As a side
benefit this same fix also corrects `live_denom` (throughput-stability's
own denominator), which was silently inflated by dead cross-job entries
the whole time this bug existed — a second, related bug, same root
cause, not separately disclosed before.

**DLRM: a real, disclosed non-localization pattern found during the
final Stage 5 sweep — same architectural boundary as MoE, not a
regression.** A real fault-injection run (target rank on worker-1)
produced a real, correctly-triggered compute alert on the right comm —
but naming a different rank (on worker-0) than the one actually
injected. This is the same class of gap already documented for MoE
below (a real alert fires job-wide but the mechanism doesn't localize
to the individual injected rank for this collective/topology shape) —
newly confirmed for DLRM specifically, not previously disclosed here.
Not fixed in this release; flagging as a known, disclosed limitation
alongside MoE's own boundary, not a new bug introduced by the Stage 6
fixes above (DLRM's own attribution/localization mechanism is
unchanged by those fixes).

**PP vs. DLRM vs. Hybrid — a real, evidenced comparison, not three
guesses**: all three sit in the same general family (peer-relative/
cross-job detection failing when there's nothing genuinely appropriate
to compare against) but are mechanistically distinct, not one bug with
three faces:
- **Hybrid** (original finding, above): cold-start **starvation** — no
  live sibling comm AND no cross-job history exists yet at all. Nothing
  to compare against because nothing has been recorded. **FIXED (P27.5,
  above)**: the live-peer pool now searches job-wide instead of
  same-host-only, so Hybrid's own multi-worker topology supplies a real
  peer where none existed before — validated n=5, TP2/PP regression
  checked.
- **PP**: looked like the opposite of starvation — **abundant but 100%
  contaminated** history, self-reinforcing (the anti-poisoning safeguard
  depends on a successful detection that had never happened). **FIXED
  (V1 Beta Stage 6)**: the real blocker was one layer down (see above) —
  `comm_calib`/`comm_bucket_members` never resetting per job meant the
  cross-job sig-match could never succeed at all, contamination or not.
  Fixing that alone resolved PP too, n=5 validated, zero changes needed
  to `alert_engine.py`.
- **DLRM**: an **infrastructure/wiring** problem — comparing against the
  literally wrong job's data (fixed first), compounded by the same
  `comm_calib`/`comm_bucket_members` job-scoping bug PP's gap turned out
  to share. **Both pieces now FIXED (V1 Beta Stage 6)**, validated live
  across 5 real job-boundary crossings.

Three distinct-looking symptoms, but two of the three (PP, DLRM) turned
out to share one real, single root cause underneath — the aggregator
never job-scoping `comm_calib`/`comm_bucket_members`, silently breaking
every consumer of `workload_signature()`'s cross-job matching (DLRM's
own throughput-history lookup AND PP's/Hybrid's role-baseline pool
separation) at once. Hybrid's own gap was genuinely different (a live
peer-pool topology limitation, P27.5) and got its own separate, correctly-
scoped fix. Confirmed directly that fixing the shared root cause left
Hybrid's already-fixed P27.5 mechanism untouched, and vice versa
(regression-checked both directions).

**A fourth, distinct PP gap found during a real cross-node validation
(Megatron TP4/PP4/DP3 on a 6-node cluster) — below-floor role-baseline
was hostname-pinned, not just role-shape-pinned — FIXED.** A cross-node
PP-link sleep-fault run landed correct rank/host attribution, but
`baseline_source` resolved to `cross_comm_peer` (the less-precise
fallback) instead of the preferred `role` baseline, even though prior
jobs on the same cluster had already run the identical workload shape
minutes earlier. Traced directly in `_member_role_baseline`
(`alerting/alert_engine.py`): its cross-job history query was scoped by
`hostname="{hostname}"` in addition to `(bucket, coll, role_rank,
role_n)` — not just role shape. That hostname pin is a deliberate, real
tradeoff (it keeps a role's baseline free of cross-node hardware-
variance contamination), but it meant the lookup only ever hit if Slurm
happened to place the SAME role on the SAME physical node across
separate job submissions — trivially true on a small, fixed 2-node
cluster (the 1-dev-cluster case this project's own history above was
validated against), but not guaranteed at all on a larger or shared
cluster, where job-to-node allocation varies run to run.

Re-ran the identical PP-link scenario on this project's own 2-node dev
cluster to isolate the mechanism itself (not the cluster-size-dependent
trigger): `baseline_source='role'` engaged correctly and produced an
accurate, well-evidenced finding (ratio=37.9x, correct rank/host),
confirming the role-baseline mechanism itself works correctly once
matching host-scoped history exists — the gap was specifically about
*history availability* on a cluster where node placement isn't
repeatable, not a wrong-answer bug in the mechanism itself. Attribution
correctness never depended on which `baseline_source` tier engaged
(both this test and the original Megatron validation landed the correct
rank/host either way) — a confidence/precision gap, not a correctness
one.

**Fix**: `_member_role_baseline` (and `_excluded_role_pool_members`,
its matching exclusion-set lookup) now take an `any_host` parameter.
The 2-member timing-asymmetry fallback tries, in order: (1) the
original strict same-host role baseline, UNCHANGED — zero behavior
difference on any cluster where this already succeeds; (2) if that
misses, the identical role-shape query with the `hostname=` constraint
dropped, pooling history for `(bucket, coll, role_rank, role_n)` across
ANY host — labeled `baseline_source='role_cross_host'`, distinct from
plain `'role'`, so it's always auditable which precision tier actually
produced a given finding; (3) only if even that misses, the existing
`cross_comm_peer` fallback, unchanged. Real cross-node hardware variance
may make tier 2 a slightly noisier baseline than tier 1 — which is
exactly why it's inserted as a middle tier, never replacing the
already-validated strict match, not a new default.

Verified two ways, not just read: (a) isolated function-level test —
pushed real synthetic history under `hostname="worker-1"` for a role
shape with zero existing data on `hostname="worker-0"`; confirmed the
strict same-host query from `worker-0` still correctly returns `(None,
None)` (unchanged), `any_host=True` from `worker-0` correctly finds and
computes a real median/MAD from the `worker-1`-tagged data, and the
strict query from `worker-1` itself still works unchanged. (b) A real,
live end-to-end PP-link fault injection on this dev cluster produced a
correct finding with `baseline_source='role'` (tier 1, since this
cluster already has real matching host-scoped history from earlier
testing) — confirming zero regression to the already-working case.
`tools/self_test.sh` re-run clean afterward: exact rank-match, zero new
`[CHECK-FAILED]`.

**Below-floor coverage-achieved signal never fired for ANY 2-member
comm, at any job duration — found and FIXED (V1 Beta Stage 6).**
`agg_detection_coverage_achieved` (meant to answer "has this comm/bucket
had a fair chance to be evaluated yet") was gated behind `score_mean_
window`'s own `if len(members_here) < 3: return` in `node_aggregator_
ref.py` — this check runs BEFORE the coverage push, so it fires and
returns for ANY below-floor comm with exactly 2 physical members before
the push line is ever reached, regardless of real job duration.
Confirmed live for BOTH below-floor shapes this pipeline validates: a
plain PP job (2-rank cross-node Send/Recv) and a TP2 job (2-rank
intra-node AllReduce) both showed `agg_mean_exec_time_us` freshly
pushing for 4+ real minutes — well past `BUCKET_MATURITY_GRACE_S`
(120s) — while `agg_detection_coverage_achieved` stayed structurally
absent the entire time, for both a short job and a long, genuinely-
stabilized one. Practical effect before the fix: there was no honest way
to distinguish "this short 2-member job never got a fair chance" from
"this 2-member job ran forever and is healthy," because the signal
meant to answer that was silently never emitted for either case on
these shapes (it DOES fire correctly for 3+-member below-floor comms,
e.g. Hybrid's own PP-boundary comm, which has TP-sharded activations
crossing it rather than a single rank pair). **Fix**: reordered the
already-computed `bucket_scored_at_ts_us`/grace-period check and the
coverage push to run BEFORE the `< 3` early-return, reusing only
already-tracked state (member count, `bucket_scored_at_ts_us`,
`BUCKET_MATURITY_GRACE_S`) — no new mechanism. The 3+-member CV/z
self-detection scoring itself is completely unchanged, still gated on
real member count; only the coverage SIGNAL is now below-floor-
inclusive. **Validated live, n>=3 independent runs on both shapes**: PP
(short job -> correct real "0"; two normal-length jobs -> both correctly
flipped to "1" at real elapsed ~3:36-3:40 from job start) and TP2 (short
job, 500 steps -> correct "0" across every bucket; normal job, 20000
steps -> correctly flipped to "1" across effectively every real
(host,bucket,comm) combination).

**A short test/validation run will not show `agg_detection_coverage_
achieved=1` — this is expected, not a failure, for any comm shape, not
just the below-floor case above.** `BUCKET_MATURITY_GRACE_S` (120.0,
`aggregator/node_aggregator_ref.py`) requires 120 real wall-clock
seconds to have passed since a bucket was FIRST calibrated before
coverage can flip to `1`, regardless of member count or how many
iterations ran. A real Megatron TP4/PP4/DP3 validation run hit this
directly: several-minute test runs never crossed that floor, so
coverage was never actually confirmed in that exercise — not a bug,
just a real run shorter than the grace period. `tools/self_test.sh`
(which stops a job the moment detection fires, often well under 120s
total) will routinely finish without ever reaching `coverage=1` for the
same reason. Don't read a short run's `coverage=0` as evidence
anything is broken — check it on a job that's run for several real
minutes past its own first calibration instead.

**Aggregator restart no longer replays the entire dump-directory
backlog from byte 0 — fixed via persistent offset checkpointing.**
This was previously documented here as a disclosed, unfixed property
(`self.file_offsets` being purely in-memory, confirmed on a cluster
with 57GB/341 files where one comm alone replayed 235,000+ records on
restart). It became a priority fix after directly causing a real
regression: restarting both aggregators to deploy the Cyril item-6
sacct work (6.10 above) left them replaying an ~11GB/host backlog for
9-15 minutes (the real range observed varied run to run with how much
historical state had accumulated), during which `tools/self_test.sh`
genuinely **FAILED** — the aggregator was still replaying history, not
watching the live test job, when the test's own 600s alert-wait window
expired.

**Design**: `node_aggregator_ref.py` now periodically checkpoints
`self.file_offsets` to a small JSON file (`.aggregator_offsets_
checkpoint.json`) inside each host's own `var/dump/<host>/` directory —
every `OFFSET_CHECKPOINT_INTERVAL_S=10` seconds during normal operation
AND once per file during a large backlog replay itself (so even a
crash mid-replay loses only the interval's own small window, not the
whole in-progress replay), via an atomic write (temp file +
`os.replace()`, a single rename syscall — a reader can never observe a
partially-written checkpoint). A real `SIGTERM` handler is also
installed: the actual restart mechanism this pipeline already uses
(clean `SIGTERM` to the leaf process, supervisor relaunches) uses
Python's default signal disposition by default, which would terminate
the process before any of this code ran — the handler makes a
*deliberate* restart checkpoint right up to the moment of the signal,
not just whatever the last periodic write happened to catch.

**Real edge cases handled, not hand-waved**:
- **Missing/corrupt/stale checkpoint** (first-ever startup, a crash
  mid-write, an unexpected schema): `_load_offset_checkpoint()` fails
  safe on ANY problem, falling back to `{}` — the exact pre-fix
  behavior, never a silent skip of real data. **Validated live**:
  deliberately corrupted the checkpoint file and confirmed the next
  restart logged `[OFFSET-CHECKPOINT] could not load ... falling back
  to re-reading from byte 0` and correctly did a full, safe re-read —
  no crash, no skipped data.
- **A dump file truncated/replaced since the checkpoint was written**
  (the inspector plugin's own writer opens each dump path with
  `fopen(path, "w")` — confirmed in `inspector-plugin/json.cc` — which
  truncates in place if the OS ever reuses a rank process's PID): a
  single guard in `poll_files()` (not duplicated in the checkpoint
  loader) catches this by comparing current file size and inode
  against what's already known, resetting to offset 0 on either
  mismatch — always failing in the safe direction (reprocess, never
  skip). This same guard also protects plain continuous live operation
  against a PID-reuse truncation, independent of any restart.
- **Small amount of re-processed data after a crash**: a crash between
  two checkpoints loses at most `OFFSET_CHECKPOINT_INTERVAL_S` (10s)
  worth of offset progress, re-processing that small window once more
  on resume. Confirmed acceptable, not assumed: this is bounded,
  safe-direction duplication, and the consumers of `handle_record()`'s
  output (rolling calibration/CV windows, VictoriaMetrics ingestion)
  already tolerate the normal jitter of a live stream.

**Real validation, not just a fresh/empty install**: tested against
this cluster's own real, multi-day accumulated dump directories
(~11-12GB/host, 216+ files each), not a synthetic small backlog.
- **Clean, isolated restart (no contending replay on the other host)**:
  checkpoint-assisted resume went from heartbeat-stale to fully live in
  **~30 seconds**, against a **9-15 minute** from-scratch baseline (the
  range observed across this session's own restarts, worse than the
  original ~9-12 minute figure once more historical jobs/communicators
  had accumulated by the time of a later from-scratch test) — confirmed
  via real `agg_aggregator_heartbeat` freshness checks against
  VictoriaMetrics, not inferred.
- **Fault-detection-across-restart, the real scenario that matters**:
  launched a real fault-injection job (rank 8/worker-1,
  `STRAGGLER_SLEEP_MS=200`), let the aggregator checkpoint partway
  through that job's own real dump data, then `SIGKILL`'d it mid-stream
  (simulating a true crash, not a clean shutdown) and let the supervisor
  relaunch it. Confirmed via the checkpoint's own content that it
  resumed from the last checkpointed offset, not byte 0 and not the
  file's current (further-advanced) end — then confirmed the real
  `[ALERT]` still fired afterward, naming the exact injected rank/PID
  (`rank=938521`, `host=worker-1`) with an exact match against the
  ground-truth PID recorded before the crash. No fault evidence was
  lost by the crash-and-resume cycle.
- **Checkpoint-write overhead, measured directly**: a realistic
  216-entry payload (~29KB JSON) writes in **~0.43ms**, measured via
  200 real back-to-back timed writes — negligible at the 10s interval
  (~0.004% of wall-clock), not assumed negligible from the interval
  choice alone.
- `tools/self_test.sh`: clean **PASS** (exact rank-match, 0 new
  `CHECK-FAILED`) after this fix, run against the real, previously-
  failing conditions.

**Remaining, disclosed caveat**: the one-time backlog replay needed to
*seed* the very first checkpoint after deploying this fix (or after any
genuinely fresh install) still pays the full from-scratch cost — this
fix makes every restart *after* that fast, not the very first one. A
narrow, unobserved-in-this-validation edge case also remains: if a
*third* distinct job starts before a crashed job's own end-time ever
gets one more sacct check (see 6.10's own job-end-race fix), the
cached last-job-id check is abandoned in favor of the new job, same
tradeoff already disclosed there. Separately noted, not fixed here (out
of this task's own scope): this replay's real CPU cost (processing
tens of millions of historical JSON records) drove one observed
from-scratch run's RSS to ~60GB before settling back down after
completion — a pre-existing characteristic of how much per-communicator
history this aggregator keeps in memory, unrelated to the checkpoint
mechanism itself, flagging for whoever next looks at long-term memory
growth.

**`[CHECK-FAILED]` vs. `[PIPELINE-DOWN]` — two distinct real health
signals, easy to conflate, don't**: `[CHECK-FAILED]` (`_run_check()`,
`alerting/alert_engine.py`) is a per-check exception guard — an
individual check function (`cv`/`mean`/`pipeline_health`/etc.) throwing
an uncaught exception, logged so one bad check can't silently kill the
whole poll loop. **Zero organic occurrences** across this project's
entire history — a real, meaningful 0, not a metric nobody's checked.
The only historical entries at all trace to deliberate test-session
VictoriaMetrics restarts (each producing a burst of `URLError:
Connection refused` while VM was briefly down), each individually
explained and none reflecting a real check-function bug; no check has
ever failed for any other reason.
`[PIPELINE-DOWN]`/`[PIPELINE-RECOVERED]` (`alerting/pipeline_health.py`)
is a completely different, unrelated signal — a heartbeat dead-man's-
switch (>90s stale) built to catch the historical "run3 vm_url incident"
class of silent push failure. It DOES fire, routinely, around every
deliberate aggregator/`alert_engine.py` restart (see the property directly
above) — this is expected, not a failure, as long as every episode has a
matching recovery. Both are genuinely useful signals; neither is a proxy
for the other, and `tools/self_test.sh`/`run.sh` only check the former.

**Killing a supervised process's own Python child does NOT pick up a
change to its SHELL supervisor script — a real, confirmed-live gotcha
that cost real debugging time on an actual cluster.** Every
`run_*_supervised.sh` (`alert_engine`, aggregator, VM, Grafana,
`iowait_logger`) is a long-running `while true; do ...; done` shell
loop — bash parses that loop body once when the supervisor process
starts, so an already-running supervisor keeps relaunching its child
with whatever command line was on disk *when the supervisor itself
started*, no matter how many times you `git pull` or kill the child
process. Confirmed live: adding `--summary-log` to
`run_alert_engine_supervised.sh` had zero effect on an already-running
deployment — `pkill -f alert_engine.py` + the supervisor relaunching it
still produced a child with no `--summary-log` flag, because the
*supervisor* predated the change. **The fix is to kill the supervisor
process itself** (`pkill -f run_alert_engine_supervised.sh`, or the
equivalent for whichever supervisor's script you changed), then
re-launch via `run.sh` (which only skips re-launching a supervisor it
finds already running — killing it first forces a real, fresh one).
Killing only the Python child is enough to pick up a change to *that
child's own `.py` file* (Python always re-reads its own source fresh on
each new process) — it is never enough to pick up a change to the
supervisor shell script that launches it.

**`[DUMP-DISK-WARN]`/`[DUMP-DISK-CRITICAL]`/`[DUMP-DISK-RECOVERED]`** — a
third, independent dead-man's-switch, added after a real 48-GPU Megatron
validation run filled a 91GB shared volume with Inspector dumps and
crashed the whole pipeline with zero warning beforehand.
`node_aggregator_ref.py` pushes each host's own real `dump_dir` usage
(`agg_dump_disk_usage_pct{hostname=...}`, via `shutil.disk_usage` —
correct whether that path is node-local scratch or a shared mount, since
it checks the real path directly rather than assuming) every real
heartbeat cycle; `alert_engine.py` reads it back every 60s
(`DUMP_DISK_CHECK_INTERVAL_S`) and logs loudly into the same central
`var/alert_engine_supervised.log` a human is already watching. Default
thresholds are 80% (`WARN`) and 95% (`CRITICAL`), both overridable via
`DUMP_DISK_WARN_PCT`/`DUMP_DISK_CRITICAL_PCT` env vars on `alert_engine.py`'s
own process. Tested end-to-end against a real, genuinely-filled tmpfs
(not mocked) before shipping — confirmed `WARN` at 85%, escalation to
`CRITICAL` at ~97%, and `RECOVERED` once usage dropped back down, each a
real log line from the real code path, not inferred from reading it.
This does not stop a job or delete anything itself — it's a loud warning
you act on (clear old dumps, or stop the job), the same "detect and
report, never silently take invasive action" discipline as every other
signal in this project.

**`[PATH-C-DOWN]`/`[PATH-C-RECOVERED]`** — a fourth dead-man's-switch,
closing a real, confirmed gap: `iowait_logger.py` (the real eBPF io-wait
data producer Path C/storage-fault detection depends on) had no launcher
anywhere in this project — unlike the aggregator, `alert_engine.py`
itself, VictoriaMetrics, and Grafana, nothing ever started it, on any
deployment, regardless of whether bpftrace/tracefs itself worked. This
is the full, root-caused explanation for a real customer cluster
reporting Path C silently returning "not checked" (`io_ev=None`) on
every query. `run.sh` now launches it per node
(`observability/run_iowait_logger_supervised.sh`, same auto-restart
convention as every other supervised process here), and `install.sh`'s
bpftrace/tracefs check now re-verifies the tracefs bind-mount wrapper
actually works after installing it (previously assumed, never
confirmed) — if the wrapper's own `unshare -m` call fails with a real
permission error (a jail/container lacking `CAP_SYS_ADMIN`, or a seccomp
policy blocking `unshare()`), both `install.sh` and the new runtime
watchdog now report that exact, actionable cause instead of silently
leaving Path C dead. A plain file-mtime staleness check is NOT a safe
liveness signal for this one (unlike DCGM hostengine): a genuinely
healthy, compute-bound job can go minutes with zero real disk I/O,
producing a legitimate gap in the data indistinguishable from a dead
agent by mtime alone — so `alert_engine.py` instead reads the
supervisor's own wrapper log for a real crash-loop signature (2+ exits
within a 120s window), which can't be confused with genuine healthy
silence. Also fixed a real bug found while testing this: `iowait_logger.py`
silently discarded any bpftrace output it didn't recognize, including
bpftrace's own real error text — so the exact diagnosis above was
reachable in the code but invisible in any log. Fixed to forward
unrecognized lines to its own stderr instead.

Tested end-to-end on a real cluster, not mocked: a genuine disk-bound
fault (`storage-ebpf/real_disk_fault.py`, cold read after
`drop_caches`) produced real `io_ev` evidence (12.46s aggregated
iowait, 487 real block-I/O events) and a correct `CONFIRMED`
`determine_storage_path` verdict; the watchdog itself was verified by
deliberately breaking the tracefs wrapper (`[PATH-C-DOWN]` fired with
the exact `unshare`-permission diagnosis) and restoring it
(`[PATH-C-RECOVERED]` fired once the crash-loop window aged out).

**Aggregator-supervisor auto-restart isolation nuance (reconfirmed)**:
`node_aggregator_ref.py` runs under `run_aggregator_supervised.sh`'s own
restart-loop wrapper. Killing *only* the leaf `node_aggregator_ref.py`
child directly (not the wrapper) causes the wrapper's own loop to
auto-relaunch a fresh process within seconds — independent of, and
faster than, any explicit `run.sh`-driven restart. Re-tested live this
session (deliberate `kill -9` of the leaf PID on a real node): a new
process was already running within ~3 seconds, pipeline remained healthy
throughout (VM reachable, `CHECK-FAILED` count unchanged). Confirmed
still present, same as previously documented — this is expected
supervisor behavior, not a bug, but worth knowing if you ever need to
stop the aggregator itself rather than just bounce it: killing the leaf
alone will not stop it.

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

**TP-inference's own final `dist.barrier()` can time out (minor, script-
level, not a detection issue)**: a V1 Beta Stage 5 re-run saw all 3000
real training iterations complete successfully, fault injected and
correctly detected throughout, and then one rank hit a 600000ms
(10-minute) `TCPStore` wait timeout at the script's own final
whole-world `dist.barrier()` call, producing a `ChildFailedError` and a
nonzero exit code on both nodes. Real detection data is unaffected (fully
streamed before this point) — this is a benign, real bug in
`train_tp_inference.py`'s own end-of-run cleanup, not a pipeline issue.
Not yet root-caused (possibly a group-scoping mismatch between TP-scoped
collectives used throughout training and a bare default-group barrier at
the end).

**VMSingle Helm chart's "survives a restart, self-heals" claim**: only
ever validated at the storage layer (data survives a process restart).
The live-cluster mechanism (Kubernetes actually noticing and recreating
a dead pod) has **never been exercised** — needs a real cluster with
real kubeconfig access to confirm, which this project has not had on any
cluster tested so far (see `../straggler-vmsingle/DEPRECATED.md`).

**Path B (DCGM) and fabric/IB checks are single-instant snapshots, not
window-matched to the real incident duration** — the same class of gap
6.8 above just closed for Path C, still genuinely open here. Every DCGM
query (clocks, power, thermal, ECC, PCIe) and every IB/fabric query reads
one live value at the moment the check runs; none of them query a real
`[t_start, t_end]` window the way Path C's `gather_path_c_storage` now
does. Concretely: a corroborating DCGM reading only ever shows "this was
true at one moment near the alert," never "this was true throughout the
incident" — the same honest caveat this project already gives ECC/PCIe
below, now stated for the whole path, not just those two counters.
`classifier/rolling_buffer.py` already exists, fully built, with exactly
the window-matched query functions needed to close this
(`query_gpu_window`/`query_host_window`/`query_ib_window`) — but **is not
currently deployed anywhere in the live pipeline**. This is a deliberate,
scoped deferral, not a bug or an oversight: deploying it means running a
new continuous per-second sampler, as a new supervised process on every
node, for the life of every job — real, standing resource and maintenance
overhead, mirroring `iowait_logger.py`'s own existing
`install.sh`/`run.sh` supervised-process pattern were it to happen. What
it would buy: genuine window-matched correlation for Path B/fabric (this
entry's own gap), AND a real fix for the ECC/PCIe gap immediately below
(which needs the same two-time-separated-sample capability). This
tradeoff is intentionally left as an open decision, not resolved here.

**ECC/PCIe's cumulative-counter gap is structurally distinct from the
Path B timing-window gap above** — fixing Path B's window-matching alone
would **not** fix this one. ECC and PCIe-replay counters are monotonic,
cumulative values; a single live snapshot (even a window-matched one)
can't tell you how much accumulated between two points in time — that
needs two time-separated samples bracketing the incident (a real
before/after delta: count-at-incident-end minus count-at-incident-start).
Closing this specific gap needs either the rolling buffer above (which
would hold exactly such samples) or two dedicated live DCGM queries taken
at incident start and end — a structurally different fix from anything
window-matching alone provides, and not attempted here.

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

**Real, live-confirmed gap in this section's own example commands (V1
Beta Stage 5 re-run): use an ABSOLUTE path for `DUMPDIR_BASE`, never the
relative `var/dump` shown above.** A genuinely fresh-environment
validation session found the relative form silently breaks dump-file
output for the self-dispatching shapes (`NCCL_INSPECTOR_DUMP_DIR` gets
exported with that same relative string, and by the time it's resolved,
the training process has already `cd`'d into its own `workloads/<shape>/`
subdirectory — so it resolves against the wrong directory, and no dump
files ever appear where the aggregator is watching, no error, no crash,
just silent non-detection). The per-node shapes (PP/Hybrid/long-context/
diffusion/DLRM/multi-modal) have a related but distinct gap: their own
README-documented example `srun` command uses a bare relative script path
(`bash workloads/pp/run_pp_node.sh ...`) with no wrapping `cd` at all —
confirmed live to fail outright (`exit code 127`, "No such file or
directory") whenever the container's own working directory doesn't
happen to match the submitting shell's cwd. Every successful validation
run in this project's own history used an absolute path for both the
script and `DUMPDIR_BASE` (e.g. `"$PWD/var/dump"`) — always do the same;
do not follow the relative-path form literally.

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

## 9. Attaching this to your own real Slurm job (not one of the 15 bundled examples)

**Real gap this section closes**: nothing above this point ever explained
how to attach detection to a job you already have — every prior section
covers running one of the 15 bundled workload shapes. This section does,
and was validated live (Stage 5 batch-fix pass) against a genuinely new,
non-bundled 4-rank DDP script with no relation to any of the 15 shapes.

**Prerequisite**: `run.sh` must already be running — the standing
aggregator per real node (watching `$VAR_DIR/dump/<hostname>`, i.e.
`var/dump/<hostname>` under this package's install path) and the
supervised `alert_engine.py`. See "Running it" above. Nothing else needs
to be told about your job in advance: the aggregator discovers its real
Slurm job ID live via `squeue` on its own refresh cycle, keyed only by
whichever real job is currently running on that host — no job-id file
to write, no workload name to declare, no config to edit anywhere.

Two real things your own training script/launch command must do,
identical to what every one of the 15 validated workload scripts
already does (see e.g. `workloads/nanogpt/train_node_straggler.sh`):

1. **Set these environment variables** before your training process
   starts (same values for every rank on a node; per-process, not
   per-job):
   ```bash
   export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so  # or wherever your own NCCL_HOME build produced it
   export NCCL_INSPECTOR_ENABLE=1
   export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"   # lean mode by default, matching every validated shape's real default; set to 1 only if you need a verbose-only offline tool (see the overhead note above)
   export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
   export NCCL_INSPECTOR_PROM_DUMP=0
   export NCCL_INSPECTOR_DUMP_DIR="$PKG_ROOT/var/dump/$(hostname)"   # see point 2 -- this exact path is the one real integration point
   mkdir -p "$NCCL_INSPECTOR_DUMP_DIR"
   ```
2. **Write Inspector dumps into exactly the same per-hostname directory**
   `run.sh`'s own aggregator was launched to watch — `run.sh`'s own
   printed `dump_dir=...` line at launch names the real, live value.
   The aggregator is a directory watcher, agnostic to what wrote the
   files inside it — any process dumping real Inspector records there
   gets picked up.

**Verifying it worked, without waiting for a fault**:
```bash
ls "$PKG_ROOT/var/dump/<hostname>/"                                   # new files shortly after your job starts
curl -s "$VM_URL/api/v1/query?query=agg_samples_seen" | python3 -m json.tool   # new series tagged with your job's real slurm_job_id (cross-check via squeue)
```

**Validated live this session**: a genuinely new 4-rank DDP script (a
plain `nn.Linear` + `DistributedDataParallel` loop, sharing no code with
any of the 15 bundled shapes) was launched with exactly the steps above
and nothing else. Real dump files (`worker-0-pid649953.log` etc.)
appeared automatically in the aggregator-watched directory, and
`agg_samples_seen{slurm_job_id="3274"}` (that job's own real, live
Slurm job ID, picked up with zero manual registration) showed real,
growing sample counts within one poll cycle — confirming this
integration point is genuinely sufficient on its own, not just
documentation that looks plausible.

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
