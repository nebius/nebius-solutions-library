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

This document is deliberately kept tight: install → run → self-test →
Grafana → attach-your-own-job, plus the limitations that are still
genuinely open today. Every investigation write-up, historical bug fix,
and "how this was found and why it's built this way" narrative that used
to live inline here has been moved to **[`DESIGN_NOTES.md`](DESIGN_NOTES.md)**,
organized by mechanism — nothing was deleted, only relocated and
reorganized (see that file's own intro for what moved). `MAINTENANCE.md`
remains the separate ownership/ops reference it always was.

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
                    fallback, the wait-induced-straggler check (its 3+-
                    member generalization), role-baseline exclusion,
                    storage-path verdicts, plus a standalone (not
                    auto-wired) RAS fail-stop watcher
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
touched), and a real, exact-version NCCL build at a path `install.sh`'s
own Step 2 already checks for and prefers.

**NCCL — checked, not rebuilt, whenever a valid tree already exists
(fixed real bug: this used to only check ONE of the two locations
`install.sh` itself accepts).** `environment.sh` now searches **both**
of `install.sh`'s own candidates, same preference order — a sibling of
this repo checkout itself (`<repo-root>/nccl-2.28-src`,
`environment.sh`'s own default, writable by whoever can already write
to their own clone, no root needed), then the absolute
`/root/nccl-2.28-src` (this project's own original dev-cluster
convention) — before deciding anything needs building. Set
`NCCL_SRC_DIR` explicitly to pin either script to exactly one location
instead. "Valid" means more than "the directory exists": a real build
output (`build/lib/libnccl.so` + `build/include`) must be present AND
its own `build/include/nccl.h` must carry the exact pinned
`NCCL_MAJOR`/`MINOR`/`PATCH` (`2.28.9`) — a stale or partial prior
attempt (confirmed real: an interrupted clone+build, or a tree checked
out at the wrong tag) is never mistaken for valid; it falls through to
obtaining a real one, the same safe-direction-only discipline as the
aggregator offset-checkpoint fix above (§7).

If nothing valid is found, **the first thing tried is NOT a source
build** — `environment.sh` downloads NVIDIA's own prebuilt
`libnccl2`/`libnccl-dev` packages at the exact pinned version
(`apt-get download`, never `apt-get install` — confirmed live,
including deliberately after a partial/failed extraction, that this
registers **zero** entries in the host's own `dpkg` database, so the
host's own currently-installed NCCL package, whatever version that is,
is never touched or downgraded) and extracts them directly into
`$NCCL_SRC_DIR/build/`. This is dramatically faster than a source
build — confirmed live, this cluster: **~9-37s** for the
download+extract (network-dependent; both real, measured runs) against
**~16 minutes** for a real `git clone` + `make -j$(nproc) src.build`
from scratch (also measured live, not estimated from NCCL's own build
time claims). Any failure at any step of the apt path — the exact
version unavailable, a network/download failure, a corrupt or partial
`.deb`, or the extracted result somehow not matching — cleans up fully
and falls back to the existing `git clone` + build path automatically,
no manual intervention needed.

This two-tier design is safe specifically because the Inspector
plugin's own build has **zero real dependency on NCCL_HOME at all**
(confirmed directly: zero `#include` of `nccl.h`/`nccl_device*`
anywhere in `inspector-plugin/`, and a real build succeeds even with
`NCCL_HOME` pointed at a nonexistent path — its own profiler-API
headers are a permanently vendored copy under `inspector-plugin/nccl/`,
not read from `NCCL_HOME`; validated live against an apt-extracted tree
too, producing a byte-identical `.so`). What a real, exact-version
**compiled** `libnccl.so` genuinely is still needed for is the separate
`NCCL_LIB_PATH`/`LD_LIBRARY_PATH` runtime-pinning several workload
launch scripts use — which the apt-extracted package satisfies exactly
as well as a from-source build does, since it's NVIDIA's own binary for
the identical pinned tag.

It deliberately does **not** duplicate anything `install.sh` already
handles itself (`bpftrace`, `libibverbs-dev`, `logrotate`,
Slurm/`ssh`/`python3` detection, per-node GPU/NCCL/`/tmp` discovery) —
those stay in `install.sh`'s own Step 1, unchanged.

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

### 6.5 `straggler_incident_detected` — counting real incidents without needing a cause confirmed

Answers a different question from section 6.4's `[ALERT] confidence=...`:
not "what caused this," but "did a real, sustained, impactful straggler
just happen, regardless of whether we ever find out why?" A genuinely
sustained software-only straggler with no DCGM/storage corroboration
could previously never rise above `PROBABLE` — a tier that carries this
pipeline's entire measured false-positive rate (22-25/hour), making it
useless for reliably counting real incidents. This signal fires instead
on **persistence** (3 consecutive qualifying windows) + **impact**
(`mm > 2.0x` peer-relative), entirely independent of cause-tier.

**Where it shows up**, additive to the existing `[ALERT]` output:
- `[STRAGGLER-INCIDENT] stat=... host=... member=... severity_ratio=...
  persisted_s=...` in both log files, alongside the finding's own
  `[ALERT]` block, sharing an `incident_id=host:comm:member:bucket:coll`
  field with it so the two can be confirmed as the same event.
- `agg_straggler_incident_detected` / `agg_straggler_incident_severity_
  ratio` in VictoriaMetrics, and a dashboard panel.

Lost-compute-time estimation is deliberately **not** built directly from
this — use the existing `agg_job_throughput_ratio_to_baseline` for that,
and treat an incident as a likely contributing cause. See
[DESIGN_NOTES.md §1.1](DESIGN_NOTES.md#11-straggler_incident_detected--counting-stragglers-without-needing-a-cause-confirmed)
for the full design rationale and validation.

### 6.6 Evidence panels, and the z/mm staleness fix

An alert's own displayed z/mm value could previously be stale (a real
number, but from an earlier window than the one that actually fired) —
fixed for both the CV and mean paths by reading from VictoriaMetrics'
`/api/v1/export` and correlating by exact timestamp rather than
"whatever is currently latest." Alerts may now fire up to ~30s earlier
than before in some cases — a pure latency improvement, never a change
to the firing decision itself. Full root cause and validation:
[DESIGN_NOTES.md §1.2](DESIGN_NOTES.md#12-trusting-what-an-alert-shows-you--the-zmm-staleness-fix-cross-reference-ids-and-evidence-panels).

**Two Grafana panels**, filterable by the **Comm** and **Member (PID)**
dashboard variables (pasted straight from an alert's own text):
- **"Flag evidence trajectory"** — the real z/mm history around the
  event, not just the single value the alert text shows.
- **"Peer timing comparison"** — every member of the same communicator's
  own real exec time at the same moment.

No new metrics collection for either — both read series already being
pushed.

### 6.7 Worst-selection sign fix, and the direct-impact lost-compute-time estimate

`node_aggregator_ref.py`'s mean-path "worst" selection used to flag
whoever deviated most from the median in *either* direction — a real bug
that could select a comm-local role position for being consistently
**faster**, not slower, than peers. Fixed by switching to signed
deviation; false-positive rate for the affected role position dropped
from 40-86% to under 7%. Full investigation:
[DESIGN_NOTES.md §1.3](DESIGN_NOTES.md#13-worst-selection-sign-fix-and-the-direct-impact-lost-compute-time-estimate).

For a CONFIRMED finding that's the only one for its job in a recent
window, `agg_direct_impact_gpu_seconds` computes a real GPU-seconds
figure for the root's own direct peer group — root-only by design (never
traced across non-shared-member stages/cascades).

> **Permanent caveat — travels with this number everywhere it's shown
> (log text, this README, any dashboard panel):**
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

### 6.8 Path C's real incident window

Path C (storage/eBPF) now queries its evidence over the incident's own
real, measured duration (`persist_duration_s`) rather than a fixed 10s
lookback — a strict widening, never narrower. Each reading also reports
window-overlap-strength (how much of the incident's duration the eBPF
log actually covers) — purely informational, confirmed by direct trace
to never feed any tier-decision logic. Full detail:
[DESIGN_NOTES.md §1.4](DESIGN_NOTES.md#14-path-cs-real-incident-window-and-its-window-overlap-strength-display).
(Path B/DCGM and fabric checks do **not** yet have this — see section 7
below.)

### 6.9 Inspector plugin: lock-free ring buffer

The Inspector plugin's old single-slot-per-communicator design could
silently overwrite a completed collective's record if a second one
completed before the dump thread's next wakeup — confirmed live losing
~17-20% of real collectives on a high-frequency TP4 communicator. Fixed
with a lock-free, bounded (256-record) ring buffer per communicator;
loss is now never silent — a `queue_drops_total` counter and rate-limited
`[WARN]` log line surface any drop. Full design, validation, and two
follow-on overhead fixes (`gRetireLock` scoping, `collEvtTrk` lean-mode
skip — ~12% pooled overhead after both):
[DESIGN_NOTES.md §5.1](DESIGN_NOTES.md#51-inspector-plugin-lock-free-ring-buffer-replacing-a-silent-single-slot-data-loss-bug).

**Known, open capacity question**: MoE and DLRM (this project's
highest-collective-frequency shapes) can push past the 256-record
capacity — real observed drops up to 15,506 (MoE) and 7,924 (DLRM) on a
short run, with burst magnitude that does not appear to converge to a
safe ceiling across the samples gathered. `queue_drops_total` always
surfaces this when it happens; whether to raise the capacity for these
specific shapes is an open, undecided sizing question.

### 6.10 sacct job context — a second, authoritative source alongside squeue

`sacct` supplements (never replaces) the existing squeue-based
job-boundary detection, adding: a disagreement cross-check (logs, never
acts), real job context appended to `[ALERT]`/`[STRAGGLER-INCIDENT]`
text (`sacct_info` is structurally unreachable from tier/decision logic
— informational only), and Grafana job-start/job-end annotations. Two
real bugs found and fixed during validation (a missing `cluster` label,
a job-end race). Full detail, real rendered output example, and
validation evidence:
[DESIGN_NOTES.md §4.4](DESIGN_NOTES.md#44-sacct-job-context--a-second-authoritative-source-alongside-squeue).

### 6.11 Worker/GPU/rank identity in alerts, and a composed incident summary panel

`gpu_slot`/`role_rank`/`role_n` now appear in the main `[ALERT]` header
and both below-floor fallback headers (previously bare PID only),
matching `[STRAGGLER-INCIDENT]`'s own format. A new **"Composed incident
summary"** Grafana table joins tier/persisted_s/severity_ratio/Path-C-
verdict with zero new free-text labels and zero new unbounded
cardinality — the Tier column is colored so `PROBABLE`/`UNCONFIRMED`
can never visually read as `CONFIRMED`. Full gap analysis, rendering
validation (through Grafana's own backend, against real fired events),
and a `tools/self_test.sh` regression this work found and fixed (a rank-
extraction regex collision with the new `role_rank=` field):
[DESIGN_NOTES.md §4.1](DESIGN_NOTES.md#41-workerGPU-rank-identity-in-alert-headers-and-a-composed-incident-summary-in-grafana).

**Operational note, not fixed (Slurm quirk)**: a just-cancelled self-test
job can linger in `COMPLETING` state for several minutes, at least once
long enough to block a subsequent `self_test.sh` run's clean-queue
precheck. `scontrol update ... State=RESUME` is rejected as an invalid
transition from this state; waiting it out (a few minutes) is what
actually works.

### 6.12 Grafana visibility for the wait-induced and role-baseline checks

Two panels — **"Wait-Induced Detections"** and **"Role-Baseline
Detections"** — plus a **"Role-baseline exclusion health"** table,
giving both of these checks (section 7) the same Grafana visibility
every older detection path already has, with the identical no-
overstatement Tier color discipline as 6.11's composed-summary panel.
Includes a `Volatile` column (a genuinely separate, independently-
computed check, not reused from the live detection gate). Full design
and real validated query output:
[DESIGN_NOTES.md §4.2](DESIGN_NOTES.md#42-grafana-visibility-for-the-wait-induced-and-role-baseline-checks).

### 6.13 `tools/incident_correlator.py` — assembling evidence across the three checks

Investigating a real event today means manually cross-referencing three
log formats (`[ALERT]`, `[WAIT-INDUCED-ALERT]`, `[ROLE-BASELINE-ALERT]`)
and remembering which check is known-noisy/blind on which workload
shape. This tool assembles that into one read-only report — **it never
computes a new confidence score, never overrides the existing tiering,
and never states a comparative lean**; every line is either a real value
from VictoriaMetrics or raw log text, plus a pre-written reference note
quoted verbatim.

**A real walkthrough.** You're tailing `var/alert_summary.log` and see:
```
[WAIT-INDUCED-ALERT] rank=2692421 gpu_slot=3 role_rank=3 role_n=4 comm=0x38bd68767bcecb node=worker-0 type=compute confidence=PROBABLE severity=LOG-ONLY
```
Paste the exact line, unmodified:
```bash
tools/incident_correlator.py --log-line '[WAIT-INDUCED-ALERT] rank=2692421 gpu_slot=3 role_rank=3 role_n=4 comm=0x38bd68767bcecb node=worker-0 type=compute confidence=PROBABLE severity=LOG-ONLY'
```
It parses `hostname`/`comm` out of the text, resolves a real time window
from VictoriaMetrics, and prints a report checking all three detection
paths for that identity/window, each annotated with the reference
table's own note for this workload shape (silent/fired/time-confirmed,
plus whether that check is `KNOWN_RELIABLE`/`KNOWN_NOISY`/
`KNOWN_BLIND_SPOT`/`UNVALIDATED` on this shape). **You draw the
conclusion from the assembled facts yourself** — the tool never does.

**The other two ways to invoke it**, same output format:
```bash
# from a Grafana panel with hostname/comm/bucket visible
tools/incident_correlator.py --host worker-0 --comm 0x38bd68767bcecb --bucket 3234251 --coll AllReduce

# from just a rough time window and host
tools/incident_correlator.py --host worker-1 --from 14:25 --to 14:35
```

**The one real limitation it always discloses**: raw `[ALERT]`/
`[WAIT-INDUCED-ALERT]`/`[ROLE-BASELINE-ALERT]` log lines carry no
timestamp — a finding only has one if it also pushed a metric. Anything
matched by identity alone, not confirmed against a real window, is
labeled inline `IDENTITY-ONLY MATCH -- not time-confirmed`, never
silently presented as a fresh, confirmed match.

The reference table backing this
(`observability/workload_reliability_reference.yaml`) is a plain,
hand-edited YAML file — `UNVALIDATED` is the hard default for anything
not explicitly entered. Design rationale and how it's kept in sync:
[DESIGN_NOTES.md §4.3](DESIGN_NOTES.md#43-toolsincident_correlatorpy--design-rationale-and-reference-table-mechanics).

Explicitly read-only — only ever issues VictoriaMetrics GET queries or
reads a local log file. Safe to run at any time.

### 6.14 `[ROLE-BASELINE-ALERT-UNVALIDATED]` — read this before trusting a role-baseline finding on a new workload shape

The role-baseline check now gates its **own** confidence by workload
shape. At emission time it resolves the firing comm's real `workload_
sig` and looks it up in `observability/workload_reliability_reference.
yaml`. A shape whose role-baseline entry is not exactly `KNOWN_RELIABLE`
still fires, with every real value unchanged, but under a distinctly
different header — `[ROLE-BASELINE-ALERT-UNVALIDATED]`, with an added
paragraph stating the shape hasn't been characterized and the finding
should be read with extra skepticism. This is deliberately presentation-
only, never suppression: silently hiding the finding or leaving it
unflagged were both explicitly considered and rejected.

**One safe-direction side effect**: a workload's sig can resolve later
than the role-baseline check's own 3-sample firing requirement, so even
a `KNOWN_RELIABLE` shape can show the UNVALIDATED header once, early in
a job's life, before its sig resolves. The error only ever runs toward
more caution, never toward false confidence.

Why this exists and the real false-positive numbers that motivated it:
[DESIGN_NOTES.md §1.7](DESIGN_NOTES.md#17-role-baseline-alert-unvalidated--the-role-baseline-check-now-gates-its-own-confidence-by-workload-shape).
A related, partial fix for one of the underlying mechanisms (TP4-
standalone's cross-job bimodality) is in
[DESIGN_NOTES.md §1.8](DESIGN_NOTES.md#18-same-job-cross-tp-group-comparison-for-tp4-standalones-role-baseline-false-positives--a-real-partial-fix) —
its two still-open gaps are tracked in section 7 below.

`tools/self_test.sh`: clean PASS throughout.

## 7. Known limitations (read this before relying on any alert)

This section has two parts: which fault classes this pipeline is
validated for at all (repeated from section 1, since it's the most
important single fact in this document), and, separately, the real,
honest validation-confidence status of specific detection paths even
within that supported scope. **It is not softened.** For the full
investigation history and fixes behind any closed item below, see
[DESIGN_NOTES.md](DESIGN_NOTES.md).

**V1 scope, again, plainly**: sustained/compute, host/CPU, and storage
stragglers are the supported, production-validated V1 scope. Medium-
duration, jitter, network-fabric, and data-pipeline stragglers are real,
planned, but **not** production-hardened in this release — do not
assume they're silently covered.

**Behavior change: the mean-path check now requires 3 consecutive
windows, same as CV already did** — it no longer pages on a single
window. A deliberate tightening of existing behavior, closing an
asymmetry between CV detection (always required 3 consecutive windows)
and the mean-path/outlier_count checks (previously could fire off one).
If your own alerting/dashboards depend on a mean-path finding firing the
instant a single window crosses threshold, this is a real behavior
change to account for.

### 7.1 Genuinely open — detection-path gaps and blind spots

- **A specific comm-local role position gets persistently misattributed
  as "worst," independent of any real fault.** A comm-local role
  position (role_rank=0 within its TP group) can get persistently
  misattributed by the mean-path positive-deviation check, independent
  of any real fault — a structural, position-correlated effect (13
  firings vs. 0-1 for TP-peers in one real run). Partially mitigated by
  the separate Role-Baseline-Deviation check (which evaluates each
  member against its own historical baseline rather than peers-in-
  window); the original mean-path mechanism itself is unchanged. Don't
  treat a solo mean-path firing on this shape as meaningful without
  corroboration. Origin and the two role-baseline fixes built on top:
  [DESIGN_NOTES.md §1.6](DESIGN_NOTES.md#16-origin-of-the-role-baseline-deviation-check--the-rank-12-misattribution).

- **ResNet's jitter/burst fault produces silent non-detection.**
  Re-confirmed over a full 7-minute run past calibration grace: real
  fault injected, **no measurable collective-level timing elevation**,
  no alert of any tier. A genuine absence of signal for this specific
  burst/jitter mechanism — jitter-class faults are not production-
  hardened in this release (consistent with the V1 scope note above).

- **TP4-standalone role-baseline: role_rank=1 pattern + a startup race,
  both still open.** The same-job lockstep fix
  ([DESIGN_NOTES.md §1.8](DESIGN_NOTES.md#18-same-job-cross-tp-group-comparison-for-tp4-standalones-role-baseline-false-positives--a-real-partial-fix))
  is deliberately NOT marked `KNOWN_RELIABLE`: role_rank=1 at both
  buckets measures well below the lockstep-detection threshold (0.07-
  0.42 vs. 0.7) — a smaller, noisier pattern this fix does not address.
  A disclosed startup race also exists: an early firing can fall through
  to the old cross-job path before this job's other same-shape comms
  have posted enough data for same-job comparison to resolve — always
  surfaces as `[ROLE-BASELINE-ALERT-UNVALIDATED]` (section 6.14), so
  this is a safe-direction gap, not a silent one.
  `observability/workload_reliability_reference.yaml` records this
  shape's role_baseline status as `KNOWN_NOISY`.

- **NVLink detection has never been validated against a real fault.**
  Built and reasoned correctly (TP traffic runs exclusively over
  NVLink, so a real NVLink fault is currently invisible to the network/
  IB check alone) — three genuinely different real injection approaches
  were tried and none could produce one. The first detector in this
  project that is correctly built but has never fired against ground
  truth. No rolling-buffer sampler exists for it either — live-query
  only.

- **DCGM ECC/PCIe-replay counters are supporting evidence only** —
  explicitly annotated "not validated as a cause — worth a look"
  whenever nonzero, never independently driving a CONFIRMED tier.

- **Path A (thermal) is PROVISIONAL.** Fired correctly exactly once
  against a real fault (chronic GPU3 degradation) and correctly declined
  on two normal-throughput cases — but calibration is thin (only 3 GPUs
  have ever exercised this path).

- **XID hardware-fault path is PROVISIONAL, never validated.** This
  cluster's own chronic hardware fault has never logged a real XID
  event — every real fault validated here has been a performance/
  counter signal, never a driver-logged event.

- **Host-load-ratio detection has never once reached CONFIRMED** in any
  test in this project's history.

- **MoE single-rank localization is a permanent architectural
  limitation.** Peer-relative statistics cannot localize a single-rank
  AllToAll fault to a specific rank (the waiting ranks show elevated
  timing, not the delayed rank). Job-wide MoE detection works correctly;
  use `moe-two-stage-detector/` (a standalone offline tool) to localize
  further once job-wide detection has already flagged a MoE job.

- **DLRM shares the same non-localization boundary as MoE.** A real
  fault-injection run produced a correct job-wide alert but named a
  different rank than the one injected — newly confirmed for DLRM
  specifically, same architectural boundary, not a regression.

- **2-member communicator self-detection is mathematically degenerate**
  below `SELF_DETECTION_FLOOR=3` real members (TP2, TP-inference) —
  peer-relative CV cannot compute at all. Mitigated, not eliminated, by
  a DCGM fallback and the P27.2 timing-asymmetry fallback — the
  statistical limit itself is permanent.

- **Above-floor scoring (`agg_mean_z_worst`/`agg_mean_fired`/`agg_cv_
  fired`/`agg_cv_z_worst`) is blind to any communicator whose real
  membership splits across hosts such that no single node locally hosts
  3+ members** — each node's aggregator only ever sees its own local
  subset, so a genuinely cross-node comm (confirmed real on a TP4 job's
  own DP-gradient-sync communicators) never gets scored by this path at
  all, regardless of parallelism strategy. **Confirmed covered in
  practice, not a currently-exploitable silent miss**: a deliberately
  adversarial real fault on exactly this comm shape (~1,700x real
  elevation, above-floor metrics confirmed still 0 series) was still
  caught correctly, via `_wait_induced_fallback_evaluate`'s own
  independent, cross-host member discovery. That coverage depends on
  `_poll_host` continuing to invoke the wait-induced/role-baseline
  checks unconditionally on every comm — a future change that made that
  conditional on below-floor status would silently remove it. Full
  investigation, including the two real test iterations and the
  structural-artifact confound the first one caught:
  [DESIGN_NOTES.md §1.9](DESIGN_NOTES.md#19-above-floor-scorings-node-local-only-blind-spot-for-cross-node-communicators--found-root-caused-confirmed-covered-fix-deferred).

- **`workload_signature()` can lock a permanently-incomplete fingerprint
  under a severe-enough fault** — it locks the moment bucket-discovery
  rate stabilizes, not once every real collective type has fired at
  least once; a collective rarer than the ones driving stability (FSDP's
  own `AllReduce`, confirmed live) can simply not have appeared yet,
  breaking cross-job role-baseline matching for every role in that job.
  **Mitigated, not eliminated**: a 300s minimum real-elapsed-time floor
  (`SIG_LOCK_MIN_ELAPSED_S`), grounded in real historical lock-delay data
  and validated live on FSDP — but this only raises the fault-severity
  bar needed to reproduce it, and the lock itself is still permanent and
  never re-evaluated later in the job. Confirmed NOT FSDP-specific: this
  project's own historical signature log already shows one independent
  prior occurrence on a different below-floor shape. Full writeup,
  including the honest severity-bar/no-re-eval caveats and the
  three-tier cross-shape risk assessment (confirmed / plausible /
  unconfirmed-but-not-proven-safe):
  [MAINTENANCE.md §5](MAINTENANCE.md#5-known-limitations--disclosed-gaps--current-status).

- **`_correlate_firing_timing_alerts` (the PP/Hybrid cross-comm cascade
  grouping mechanism) cannot identify a wait-induced-style straggler as
  the root cause, by design.** It correctly groups alerts that are
  genuinely physically connected (confirmed live via a real BFS-path
  test on Hybrid TP+PP) — that part works. But it only ever traces
  connections from an alert's ELEVATED (waiting) side; a true
  wait-induced culprit is always the SUPPRESSED (sleeping) side of its
  own comm, so it structurally can never surface as this mechanism's
  `ROOT_CAUSE_CANDIDATE`. **For this fault class, rely on the individual
  `[WAIT-INDUCED-ALERT]`/P27.2 timing-fallback line's own named culprit
  — not this mechanism's root-cause/downstream labeling.** Real test
  evidence: Hybrid job 3867, a real rank-0 fault correctly produced two
  alerts that were correctly grouped via a genuine, real shared-member
  BFS path — but both ended up labeled mutual "possible echoes" of each
  other, with neither marked `ROOT_CAUSE_CANDIDATE`. **An earliest-real-
  timestamp tiebreak fix was designed, implemented, and tested live —
  then fully reverted** after real ground-truth testing (3 live Hybrid
  runs, different target ranks) showed the tiebreak picks an innocent
  rank over the true target: the real ~1s gap between alerts reflects
  which comm's persistence window happened to close first, not genuine
  causal order. This is now a confirmed dead end, not an untried idea —
  do not re-attempt the same fix without a genuinely different signal.
  Full writeup, including why, the attempted fix, and an open
  calibration question about the 60s correlation window itself:
  [MAINTENANCE.md §5](MAINTENANCE.md#5-known-limitations--disclosed-gaps--current-status).

- **`node_aggregator_ref.py` can go completely dark — heartbeat and
  scoring output, not just slow — under MoE's extreme comm/bucket
  cardinality. This is a different, more severe failure mode than the
  ring-buffer overflow in section 6.9 — do not conflate them**; that one
  is bounded, known record loss at the Inspector-plugin level, this is
  unbounded growth in the aggregator's own Python heap/blocking on its
  own flush path. One real cause (`mean_unconsumed`/`cv_unconsumed`,
  uncapped since the P22.5 fix only ever touched the separate
  `all_vals`/`rate_samples` lists) is **fixed** (500-entry drop-oldest
  cap, same precedent). **A second, likely-dominant cause is confirmed
  but NOT fixed**: `push_buf` grows unbounded during a single large
  `poll_files()` catch-up cycle, with no flush or heartbeat until that
  one call returns — and `poll_files()`'s own deadline check never
  fires in practice (it compares against the run's multi-year overall
  duration, not a per-call budget). Re-validating the first fix with the
  identical real scenario still produced **15+ minutes of continuous
  blackout and a new memory peak above the original incident** — the
  cap was real and correct for its own target, just not sufficient.
  **Whether MoE's job-wide detection itself works is still an open,
  unresolved question** — no alert ever fired in either test run, but
  the aggregator was never healthy long enough, in either run, to give
  normal detection a fair chance; this is not evidence either way.
  `self.state`/`comm_bucket_members` key-count growth (the third
  candidate) is confirmed NOT the dominant factor, now grounded in a
  real measurement: this workload's full bucket vocabulary converges
  within ~1 minute wall-clock (confirmed twice, 202 buckets both times),
  so key count does not scale with job duration — deferred with that
  reasoning recorded, not just asserted. Full mechanism, live evidence
  from both validation passes, the confirmed-insufficient recovery
  procedure (including a case needing direct cleanup of a run's own
  dump files rather than waiting out a restart), and the still-open
  `push_buf` fix as the next priority:
  [MAINTENANCE.md §5](MAINTENANCE.md#5-known-limitations--disclosed-gaps--current-status).

- **Pure-software-delay faults cap at PROBABLE forever**, never
  CONFIRMED/PAGE. A real, correctly-localized, high-confidence anomaly
  (z-score up to 510, arrival lag up to 692x peers) on a well-above-
  floor communicator stays at PROBABLE indefinitely, because CONFIRMED-
  tier escalation requires corroborating DCGM/Path-C cause-evidence, and
  a pure Python-level `time.sleep()` straggler produces neither. **No
  pure-software-only compute straggler, however large and well-
  localized, will ever page a human via the standard detection path** in
  this release.

- **Path B (DCGM) and fabric/IB checks are single-instant snapshots, not
  window-matched to the real incident duration** — the same class of
  gap section 6.8 closed for Path C, still open here. A corroborating
  DCGM reading only ever shows "this was true at one moment near the
  alert," never "this was true throughout the incident." The window-
  matched query functions already exist
  (`classifier/rolling_buffer.py`) and are genuinely wired into
  `classifier.py` — what's not deployed is narrower: the one live call
  site hardcodes `buffer=None`, and the continuous per-second sampler
  that would populate a real buffer is never launched by any supervisor.
  A deliberate, scoped deferral (real standing per-node resource cost
  if deployed), not a bug.

- **ECC/PCIe's cumulative-counter gap is structurally distinct from the
  Path B gap above** — fixing Path B's window-matching alone would
  **not** fix this. ECC/PCIe-replay are monotonic cumulative counters; a
  single snapshot (even window-matched) can't show how much accumulated
  during an incident — that needs two time-separated samples bracketing
  it. Needs either the rolling buffer above or two dedicated DCGM
  queries at incident start/end — not attempted.

- **cuDNN/cuBLASLt disable workaround remains unresolved.** A real
  2-node SIGABRT was hit under cuDNN+NCCL multi-process load; a
  dedicated session could not reproduce it (0/3 attempts). The
  disable-cuDNN default is kept defensively, not proven necessary.

- **MoE RDMA fault shim's build command** is reconstructed from this
  project's history, not yet independently re-verified — see
  `workloads/moe/README.md`.

- **TP-inference's final `dist.barrier()` can time out** (minor,
  script-level, not a detection issue) — a 10-minute TCPStore wait at
  the script's own final whole-world barrier after training and
  detection both already succeeded. Not yet root-caused (possibly a
  group-scoping mismatch between TP-scoped collectives and a bare
  default-group barrier).

- **VMSingle Helm chart's "survives a restart, self-heals" claim** is
  only validated at the storage layer. The live-cluster mechanism
  (Kubernetes noticing/recreating a dead pod) has never been exercised —
  needs a real cluster with real kubeconfig access, which this project
  has not had on any cluster tested so far (see
  `../straggler-vmsingle/DEPRECATED.md`).

### 7.2 Operational notes (current behavior, not open gaps)

- **`[CHECK-FAILED]` vs. `[PIPELINE-DOWN]` — don't conflate them.**
  `[CHECK-FAILED]` is a per-check exception guard (zero organic
  occurrences in this project's history). `[PIPELINE-DOWN]`/
  `[PIPELINE-RECOVERED]` is an unrelated heartbeat dead-man's-switch that
  fires routinely around every deliberate restart — expected, not a
  failure, as long as every episode has a matching recovery. Full
  explanation: [DESIGN_NOTES.md §3.2](DESIGN_NOTES.md#32-check-failed-vs-pipeline-down--two-distinct-real-health-signals-easy-to-conflate).

- **Killing a supervised process's Python child does NOT pick up a
  change to its shell supervisor script.** Every `run_*_supervised.sh`
  is a `while true; do ...; done` loop parsed once at supervisor
  startup — killing only the leaf process relaunches it with the
  supervisor's **old** command line. **Kill the supervisor process
  itself** (e.g. `pkill -f run_alert_engine_supervised.sh`), then
  re-launch via `run.sh`. Confirmed live, three separate times across
  this project's history: [DESIGN_NOTES.md §3.3](DESIGN_NOTES.md#33-the-shell-supervisor-restart-gotcha-confirmed-live).

- **A short test/validation run will not show
  `agg_detection_coverage_achieved=1`** — expected, not a failure, for
  any comm shape. `BUCKET_MATURITY_GRACE_S` (120s) requires 120 real
  wall-clock seconds since first calibration before coverage can flip to
  1, regardless of member count or iteration count. `tools/
  self_test.sh` (which stops a job the moment detection fires) will
  routinely finish without ever reaching `coverage=1` for this same
  reason — don't read a short run's `coverage=0` as evidence anything is
  broken.

- **Baseline alert rate under normal (non-fault) operation**: ~22-38/hour
  across independent measurement sessions, settling around ~32/hour —
  all PROBABLE/LOG-ONLY, never paged. See
  [DESIGN_NOTES.md §5.2](DESIGN_NOTES.md#52-overhead-measurements-across-workloads--the-real-measured-numbers-full-investigation)
  for the full overhead/resource-cost numbers across workloads
  (ResNet's ~4.5x, Megatron's ~12-13%, the aggregator/log/iowait-logger
  memory caps already shipped, and more).

### 7.3 Closed — real findings, investigated and fixed

Every item below was a real, confirmed gap at the time it was found.
Each is now fixed and validated; the full investigation (root cause,
real data, and how it was verified) lives in `DESIGN_NOTES.md`.

- **Wait-induced stragglers** — a real, total blind spot (zero alert at
  any tier, on any 3+-member communicator, for a fault whose signature
  is an inverse/negative deviation) — closed by the new
  `[WAIT-INDUCED-ALERT]` path. [DESIGN_NOTES.md §1.5](DESIGN_NOTES.md#15-wait-induced-stragglers--a-real-total-blind-spot-found-and-closed).
- **P27.2 / TP2 long-soak false-positive storm** — reproduced,
  root-caused (natural timing variance widening over a long enough run),
  and stopgapped (Part D closed). [DESIGN_NOTES.md §2.1](DESIGN_NOTES.md#21-p272-the-tp2-long-soak-false-positive-storm--reproduced-root-caused-fixed-part-d-closed).
- **RL "weak detection" false alarm** — resolved as a test-harness bug
  (`STRAGGLER_PHASE=none`), not a real gap. [DESIGN_NOTES.md §2.2](DESIGN_NOTES.md#22-rl-an-earlier-weakinconsistent-detection-finding-was-itself-wrong--the-fault-was-never-actually-being-injected).
- **Hybrid below-floor blind spot** — the P27.2 fallback never fired due
  to topology (no live peer sibling); fixed via job-wide peer pooling
  (P27.5). [DESIGN_NOTES.md §2.3](DESIGN_NOTES.md#23-hybrid-the-below-floor-p272-fallback-never-fires-despite-a-very-strong-raw-signal--and-the-job-wide-peer-pool-fix-p275).
- **Hybrid role-baseline provenance gap** — a single unrepresentative
  survivor was treated as a valid baseline; fixed via a minimum-history
  floor. [DESIGN_NOTES.md §2.4](DESIGN_NOTES.md#24-hybrid-a-second-different-role-baseline-gap--baseline-provenance-not-attribution-correctness-fixed).
- **PP self-reinforcing contamination** — looked permanently
  contaminated; the real blocker was unreset per-job signature state.
  [DESIGN_NOTES.md §2.5](DESIGN_NOTES.md#25-pp-shape-9-a-self-reinforcing-cross-job-history-contamination--root-caused-and-fixed).
- **DLRM throughput-reference + signature job-scoping bugs** — compared
  against the wrong job's data, and couldn't find its own history; both
  fixed. [DESIGN_NOTES.md §2.6](DESIGN_NOTES.md#26-dlrm-shape-13-throughput-reference-wiring-bug-and-signature-job-scoping-bug--both-fixed).
- **PP vs. DLRM vs. Hybrid synthesis** — two of the three shared one root
  cause underneath. [DESIGN_NOTES.md §2.7](DESIGN_NOTES.md#27-pp-vs-dlrm-vs-hybrid--a-real-evidenced-comparison-not-three-guesses).
- **A fourth PP gap** — below-floor role-baseline was hostname-pinned,
  failing on clusters with variable node placement; fixed via a 3-tier
  any-host fallback. [DESIGN_NOTES.md §2.8](DESIGN_NOTES.md#28-a-fourth-distinct-pp-gap--below-floor-role-baseline-was-hostname-pinned-not-just-role-shape-pinned-fixed).
- **Coverage-achieved never fired for any 2-member comm** — fixed by
  reordering the coverage push ahead of the below-floor early-return.
  [DESIGN_NOTES.md §2.9](DESIGN_NOTES.md#29-below-floor-coverage-achieved-signal-never-fired-for-any-2-member-comm-fixed).
- **Aggregator restart used to replay the entire dump backlog from byte
  0** — fixed via persistent offset checkpointing (~30s resume vs. 9-15
  minutes from scratch). [DESIGN_NOTES.md §3.1](DESIGN_NOTES.md#31-aggregator-offset-checkpoint-resume--eliminating-full-backlog-replay-on-restart).
- **Dump-disk, Path C, Grafana, and VictoriaMetrics liveness watchdogs**
  — four dead-man's-switches, each closing a real gap found live (a
  91GB-volume crash, a never-launched iowait logger, a 2+ day silent
  Grafana outage, and VM's own distinct "up but serving wrong data"
  failure modes). [DESIGN_NOTES.md §3.4](DESIGN_NOTES.md#34-dump-disk-warndump-disk-criticaldump-disk-recovered--build-out)–[§3.7](DESIGN_NOTES.md#37-vm-downvm-recovered--build-out).
  The Grafana watchdog's own validation also found and fixed a real
  port-3000-default collision in `install.sh`/`grafana-setup.sh`'s own
  instance-discovery logic.

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
