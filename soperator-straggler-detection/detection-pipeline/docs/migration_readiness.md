# Migration Readiness Inventory

Real, verified inventory of what needs to move to run this GPU
straggler-detection pipeline on a different cluster, what won't transfer
automatically, and a step-by-step setup sequence. Every path below was
confirmed to exist on disk this session (see `migration_package/` — every
file referenced here was copied into it and verified present).

## Status on the new 2-node H200 cluster (this migration)

**Why this migration is happening**: the 6-node "soperator" cluster this
package was last validated on became unusable for further testing not
because of any problem with this pipeline, but because of real, external
resource contention — `worker-2`/`worker-3` (this project's own
established GPU-test node pair, chosen specifically to avoid `worker-0`/
`worker-1`'s persistent unrelated `kings-rows` CPU job) got occupied by a
sequence of unrelated jobs (`cuda04-crt8-n25-33`, then
`cuda04-crt16-gpu`, with more queued behind them), each with independent
3-to-7-day Slurm time limits and no visibility into their real completion
time from outside. This is a real, unpredictable, potentially multi-day
block, not a transient scheduling hiccup — moving to a fresh, dedicated
2-node H200 cluster was the correct call rather than waiting indefinitely.

**What this means concretely**: assume NOTHING is verified on the new
cluster yet. Every item in this document's "confirmed on the 6-node
cluster" annotations below describes the PREVIOUS cluster, not this one.
Treat the new cluster the same way this document's own established
discipline already treats every prior migration — check each item live,
per node, rather than assuming anything transfers just because it worked
on hardware that happens to also be H200s. Real, specific new things to
re-verify that did NOT exist as open questions on the 6-node cluster:
- **The relative-tolerance throughput-baseline fix and the tolerant
  signature-matching fix** (`node_aggregator_ref.py`, P27.5/P27.6 — see
  `codebase_reference.md`) were implemented and validated as far as
  possible on the old cluster before the migration became necessary, but
  the LAST validation step (a real MoE netfault re-test confirming the
  cross-job-reference path engages and `uniform_slowdown` correctly
  fires) was never completed — the old cluster's `worker-2`/`worker-3`
  became unavailable before that run could happen. **This is the first
  thing to re-run on the new cluster once VM and the pipeline are
  standing.**
- **The `_timing_asymmetry_fallback_evaluate` 2-member fallback** (Part 1,
  new this session) was fully validated on the old cluster (exact rank
  match on both TP2 and TP-inference, zero false positives) — this is a
  real, closed result that should transfer, but re-confirm on the new
  cluster's own hardware/NCCL version before trusting it in production,
  the same discipline this document already applies to every other
  mechanism.
- **A real, reproducible, still-unexplained finding**: a `type=host
  CONFIRMED/PAGE` alert fired identically (same ~0.15 vs ~0.02 load/core
  ratio, three separate times) on `worker-2` specifically during MoE
  healthy-baseline tests on the old cluster, root cause never identified
  (no visible Slurm job explains it; likely some node-level background
  daemon). Watch for whether this recurs on the new cluster's own nodes —
  if it doesn't, that's real evidence it was a 6-node-cluster-specific
  artifact, not something in this pipeline's own code.

See `codebase_reference.md` and `straggler_dectection_history.md`
(Phase 13 onward) for the full technical and narrative account of
everything fixed/found this session, before repeating any of this
investigation from scratch.

## Status on the current (6-node H200) cluster

Environment migration is **complete and live-verified on worker-1,
worker-3, and worker-4** (3 of 6 nodes) as of this session:
NCCL Inspector plugin rebuilt and confirmed patched on each node, bpftrace
+ tracefs access working on each node (independently re-verified, not
assumed from one node to the rest), and a real 3-node/12-GPU NCCL job
confirmed correct cross-node communication with `gpu_slot_index`
resolving correctly on every physical node — the first time this
project's tooling has been validated on more than 2 physical nodes.

**Not done / out of scope this session**:
- `worker-0` and `worker-2` were never touched (occupied by unrelated
  real jobs) — assume nothing about their state; repeat the full setup
  sequence there before using them.
- `worker-5` carries a real, pre-existing `node_problem` (`ib-gpu-perf`)
  flag from this cluster's own automated health monitoring — treat as a
  known bad node to avoid, not yet investigated.
- The full live pipeline (`node_aggregator_ref.py` → VictoriaMetrics →
  `alert_engine.py`) has **not** been run end-to-end on this cluster yet
  — this session validated the NCCL Inspector/`gpu_slot_index` layer
  directly (via `nccl-tests`), not the full alerting stack. VMSingle
  install is still blocked (see §3) so the aggregator has nowhere to push
  to yet.
- IB device count / per-host GPU count discovery (`discover_ib_devices`,
  `discover_gpu_count`) still not live-re-validated — this session didn't
  exercise `P18k_classifier`/`cause_metrics.py` directly.
- No fault injection (disk-bound or clock-lock) has been run on this
  cluster yet beyond the eBPF storage-fault positive/negative control
  test (§5) — that part passed cleanly on all three nodes.

## 1. Files/paths that need to move

| Path | What it is |
|---|---|
| `/root/nccl-2.28-src/ext-profiler/inspector/` (source, not just the built `.so`) | The patched NCCL Inspector plugin — `inspector.cc`, `inspector.h`, `inspector_plugin.cc`, `json.cc/h`, `version.cc/h`, `Makefile`, `README.md`. Built artifact `libnccl-profiler-inspector.so` included too (184KB, small enough to ship prebuilt; still ship source since the target cluster's own NCCL/CUDA version may require a rebuild — see build instructions below). |
| `/root/P20c_alerting/*.py` (every script) | The live alerting pipeline — `node_aggregator_ref.py`, `alert_engine.py`, `coverage_guard.py`, `pipeline_health.py`, `thresholds.py`, `persistence.py`, `health_exclusions.py`, `ras_alert.py`, `iowait_logger.py`, `node_aggregator_with_shim.py` (P27-era: thin wrapper adding `AGGREGATOR_LOCAL_LOG`-driven redundant local JSONL logging of every push, used throughout this session's validation for full-resolution data verification independent of VM itself). |
| `/root/P18k_classifier/*.py` | The offline/cause-evidence classifier — `classifier.py`, `cause_metrics.py`, `detection.py`, `calibration.py`, `rolling_buffer.py`, `report.py`, `storage_evidence.py`, `persistence.py`, `cv_fixed.py`, `decay_counter.py`, `transient_latency.py`. |
| `/root/P20d_e2e_validation/storage_ebpf/iowait_agent.bt` | The eBPF storage-fault agent script (bpftrace). |
| `vm_standalone/` (this package, **new this migration**) | The real, working detection backend — a portable standalone VictoriaMetrics binary, launched as a plain Slurm job, no Kubernetes needed at all. **Use this, not §3/§4 below.** See `vm_standalone/README.md` for the exact launch command and flag rationale. Do not copy its `data/` subdirectory — see that README's own note. |
| `straggler-vmsingle/` | The Kubernetes VMSingle Helm chart — **deprecated, see `straggler-vmsingle/DEPRECATED.md`**. Kept for reference only; RBAC-blocked on two separate clusters in a row. |
| `workloads/` (this package, **new this migration**) | Every real, validated fault-injection-capable training script this project's P27-era regression sweep depends on — see the dedicated table below. Without these, ResNet/ViT/TP-inference/FSDP/MoE fault injection cannot be reproduced on a new cluster; they live outside `/root/P2*` and were never part of this package before this migration. |
| All docs in this folder | `codebase_reference.md`, `migration_readiness.md` (this file), `straggler_dectection_history.md`. |

### `workloads/` — training scripts with real fault-injection mechanisms

| Dir | Script(s) | Fault mechanism | Real data dependency |
|---|---|---|---|
| `resnet/` | `train_resnet.py` | `STRAGGLER_SLEEP_MS`/`STRAGGLER_TARGET_RANKS` env-gated `time.sleep()` before `loss.backward()` | None — synthetic `torch.randn`/`torch.randint` data generated in-script. torchvision's `resnet18` only. |
| `vit/` | `train_vit.py` | Same STRAGGLER convention, same insertion point | None — synthetic data, torchvision only. |
| `tp_inference/` | `train_tp_inference.py`, `model.py` | Same STRAGGLER convention, placed before the forward-only `model(idx, targets=None)` call (this script has no backward pass at all) | None — synthetic token IDs; `model.py` is nanoGPT's own GPT implementation, needed for the TP-sharded forward pass. |
| `fsdp/` | `train_fsdp.py`, `model.py`, `configurator.py`, `data/shakespeare_char/` | Same STRAGGLER convention — **this one was found to be an unimplemented placeholder** (comment-only, no real `time.sleep()`) before this session; now real. No `require_backward_grad_sync`-style gate needed (FSDP2's `fully_shard` units sync every backward pass unconditionally, unlike DDP). | Real nanoGPT shakespeare-char data (3.3MB, included). |
| `tp/` | `train.py` (nanoGPT_tp), `model.py`, `configurator.py`, `config/train_shakespeare_char.py`, `data/shakespeare_char/` | **Already had** a real STRAGGLER mechanism before this session — confirmed, not built this session. `TP_SIZE=1` collapses this to plain single-comm DDP (used as the regression sweep's DDP baseline); `TP_SIZE=2`/`4`/etc. give real tensor-parallel sharding. | Same shakespeare-char data. |
| `moe/` | `train_moe.py`, `model_moe.py`, `configurator.py`, `data/shakespeare_char/`, `rank_fault_wrapper.sh`, `run_moe_netfault.sh`, `run_moe_rankfault.sh` | **Two independent, genuinely real mechanisms, NOT the STRAGGLER convention**: `rank_fault_wrapper.sh` uses `LD_PRELOAD` of a `qp_rate_limit_shim.so` for a single-rank AllToAll fault (confirmed permanent architectural non-localization — see `straggler_dectection_history.md` Phase 9; explicitly out of scope for rank-level detection, only ever validated for job-wide detection); `run_moe_netfault.sh` sets job-wide `NCCL_MAX_NCHANNELS=1` via `--export=ALL` for a uniform, job-wide network fault (confirmed real via raw data: 8.4x median AllToAll slowdown). | Same shakespeare-char data. |

## 2. Environment/config prerequisites

**NCCL env vars** (real, grepped from every real launch script this
project uses):
- `NCCL_PROFILER_PLUGIN` — absolute path to the built
  `libnccl-profiler-inspector.so`.
- `NCCL_INSPECTOR_ENABLE=1`
- `NCCL_INSPECTOR_DUMP_VERBOSE=1`
- `NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS` — dump-write cadence
  (this project has used 500 throughout).
- `NCCL_INSPECTOR_DUMP_DIR` — per-rank dump directory,
  `node_aggregator_ref.py`'s own input.
- `NCCL_INSPECTOR_PROM_DUMP=0` — disables the plugin's own (unused by
  this pipeline) direct Prometheus export; `node_aggregator_ref.py` reads
  the raw dump files instead.

**Fault-injection env vars (P27-era, real and portable — see `workloads/`
table above)**:
- `STRAGGLER_SLEEP_MS` — milliseconds of real `time.sleep()` to inject.
  `200` was this project's own validated, real-effect-confirmed value
  (near-exact match between injected and observed delta on
  TP-inference: 198.6ms observed vs. 200ms injected) — re-verify on new
  hardware rather than assume, but this is a much smaller ask than
  re-deriving a clock-lock value, since the mechanism is pure software.
- `STRAGGLER_TARGET_RANKS` — comma-separated global ranks to target
  (e.g. `3` or `3,7`). Empty/unset means no rank sleeps — this is how to
  run a genuine healthy baseline with the exact same code path as the
  fault test, changing nothing but this one env var.

**`/sys-host` mount** — a read-only bind of the real HOST's `/sys`
(confirmed real on this cluster: `sysfs on /sys-host type sysfs
(ro,relatime)`, with the host's own `tracing`, `debug/tracing`, `bpf`,
`cgroup`, `security`, `pstore`, `fuse/connections` filesystems visible
underneath). Required because this project's containers/jails run in
their own mount namespace without the host's tracing/bpf filesystems
mounted at their standard paths — without `/sys-host`, `bpftrace` has
nothing real to attach to. **Re-confirmed present on worker-1/3/4 on the
6-node cluster** (this migration) — held up unchanged.

**Persistent bpftrace/tracefs fixes** — **do not assume either fix is
needed; check each one live on the target before applying it.** On the
6-node H200 cluster (this migration), fix #2 was needed on every node
tested; fix #1 was needed on **none** of them:
1. `libLLVM.so.18.1` SONAME conflict — only a real problem when the
   NVIDIA driver's own bundled LLVM copy shadows the one bpftrace needs
   (this was the case on the *previous* 2-node cluster, where a driver
   upgrade had removed the driver's bundled `libLLVM.so.18.1` in favor of
   20.1). **Check first**: `ldd $(readlink -f /usr/bin/bpftrace.real) |
   grep llvm` — if it already resolves to a real `libLLVM.so.18.1` (e.g.
   via the `libllvm18` apt package, which was sufficient on all 3 nodes
   tested on the 6-node cluster), no fix is needed. Only if resolution
   fails or points at a wrong/missing file, apply the fix: point
   `/etc/ld.so.conf.d/99-bpftrace-nvidia-llvm.conf` at
   `/usr/lib/llvm-18/lib` (the `llvm-18`/`libllvm18` **apt package**'s own
   copy — not subject to being removed by a future driver upgrade).
2. Tracefs bind-mount wrapper — `/usr/local/bin/bpftrace` (a shell
   wrapper) bind-mounts the host's real tracefs
   (`/sys-host/kernel/tracing`) to bpftrace's expected standard path
   (`/sys/kernel/tracing`) per-invocation, in a private mount namespace,
   since there's no reachable PID1/systemd inside this jail to install a
   persistent boot-time bind-mount. **Check first** with a real probe
   (`bpftrace -e 'tracepoint:syscalls:sys_enter_openat {...}'`) before
   assuming the wrapper is missing — one node on the 6-node cluster
   (`worker-4`) already had this exact wrapper pre-baked into its image
   (see "node image inhomogeneity" note below), so blindly reinstalling
   everywhere wastes a step; it's harmless to reapply, but check first.

**Required packages**: `bpftrace` (apt, confirmed working version
`0.20.2-1ubuntu4.3` on both the original and the 6-node cluster).
`llvm-18`/`libllvm18` is pulled in automatically as bpftrace's own apt
dependency — don't install it separately.

**apt/network troubleshooting (new this migration)**: on one node
(`worker-1`) `apt-get install` failed with `Network is unreachable` —
root cause was the node having IPv4 connectivity but **no IPv6 route**,
while apt/curl preferred the mirror's IPv6 address and never fell back.
Fixed with `sudo apt-get -o Acquire::ForceIPv4=true install ...`. This did
**not** reproduce on the other two nodes tested (`worker-3`, `worker-4`)
— plain `apt-get update` worked fine there. **Check each node
individually** (`apt-get update` first, plain) rather than assuming this
fix is universally needed or universally unnecessary across a fleet.

**Node image inhomogeneity (new finding this migration)**: don't assume
every node in the target fleet starts from the same clean state, even
within one migration. On the 6-node cluster: `worker-1` had neither
bpftrace nor any tracefs wrapper installed; `worker-3` had bpftrace
pre-installed plus a partially-broken pre-existing wrapper; `worker-4`
had bpftrace **and** a fully-working tracefs wrapper already baked in
(byte-identical to the one this doc describes, but confirmed via
differing device IDs to be independently present, not shared storage).
All three also had the same **stale, pre-existing Inspector `.so`**
baked into `/usr/lib/x86_64-linux-gnu/` (see §4 step 2) — meaning the
fleet's golden image already carries some artifacts from earlier,
unlogged provisioning. **Verify each node's actual state independently;
do not extrapolate from one node to the rest of the fleet.**

## 3. VMSingle Helm chart — pointing the aggregator at a fresh instance

Chart: `/root/P26_5_vmsingle_chart/straggler-vmsingle/` (see its own
README.md for full install/validate steps, dedup rationale, and real
sizing basis). Install:

```
helm install straggler-vmsingle ./straggler-vmsingle --namespace straggler-detection --create-namespace
```

Point `node_aggregator_ref.py` at a freshly-installed instance:

```
python3 node_aggregator_ref.py <dump_dir> <hostname> \
  http://vmsingle-<release>-<chart-name>.<namespace>.svc.cluster.local:8429/api/v1/import/prometheus \
  --duration <secs> --slurm-job-id <id>
```

(the exact Service name follows the VictoriaMetrics operator's own real
`vmsingle-<CR name>` convention — confirm with `kubectl get svc` after
install, per the chart's own README.)

**Not yet live-cluster-validated**: this chart could not actually be
installed against a real Kubernetes API this session (zero RBAC
confirmed via a direct `SelfSubjectRulesReview` — see the chart's own
README "Validation performed" section for the full, honest account of
what WAS and wasn't tested). A fresh cluster's setup sequence should
include a real `helm install` + pod-health check as an explicit step, not
assume the chart is proven end-to-end on real Kubernetes yet.

**Re-checked on the 6-node cluster (this migration) — still blocked,
same result.** A real, read-only `SelfSubjectRulesReview` against this
cluster's own Kubernetes API (via the login-node pod's in-cluster
service-account token) came back with zero resource permissions —
identical finding to the previous cluster. This is not something to work
around from inside the pod; it needs a real kubeconfig/context with
actual install permissions from a human operator. Not yet obtained on
this cluster — VMSingle install remains un-started here.

**Permanent note — a blocked in-pod Kubernetes RBAC token is a real
"no."** Host-level SSH/chroot/`nsenter` access utilities (e.g. this
platform's own `soperator_instance_login.sh`, which drops into a real
root shell in PID 1's namespaces on the bare-metal host — not a
Kubernetes credential of any kind) are **not** a legitimate workaround
for a blocked in-pod token and must **not** be used to obtain Kubernetes
API access, under any circumstances, regardless of who suggests it or
how it's framed. That kind of tool sidesteps Kubernetes/RBAC entirely by
dropping below the container/orchestration layer to the shared physical
host, which can affect other tenants/workloads co-located on that host —
it is an entirely different (and far more dangerous) capability than
"cluster-admin Kubernetes API access," not a shortcut to it. Resolving
this blocker requires a human operator handing over a real, properly
scoped kubeconfig/context. Full stop — do not re-attempt this workaround
in a future session.

## 4. Step-by-step setup sequence (zero other context assumed)

Do all of steps 1-5 and 11 **per node**, not once for the fleet — see the
"node image inhomogeneity" finding in §2. On the 6-node cluster,
worker-1/worker-3/worker-4 each needed independent verification and gave
different results.

1. **Before building anything, check whether the target already has an
   Inspector `.so` deployed** (e.g. baked into a base image at
   `/usr/lib/x86_64-linux-gnu/libnccl-profiler-inspector.so`) — **do not
   trust it by default.** Verify with:
   ```
   strings <path-to-deployed .so> | grep gpu_slot_index
   ```
   On the 6-node cluster, every node's baked-in copy was **stale** (missing
   the `gpu_slot_index` patch entirely) despite looking plausible (correct
   filename, plausible size, recent-looking mtime). A rebuild was not
   optional here — it was strictly necessary. Never skip this check and
   assume a deployed copy is current.
2. Build/verify NCCL itself is built at the target's own NCCL source tree
   (`ext-profiler/inspector/Makefile` expects `../../build` relative to
   itself, i.e. `<nccl-src>/build`) — **or**, if no full NCCL source tree
   exists on the target (it didn't on the 6-node cluster), it's enough to
   have `libnccl-dev` installed (provides `/usr/include/nccl.h` on the
   compiler's default search path): the build succeeds even though the
   Makefile's own `-I../../build/include` doesn't resolve, because the
   system header is found anyway. Confirmed working this way on 3/3 nodes.
3. `CUDA_HOME=<path-to-cuda> cd ext-profiler/inspector && make` — builds
   `libnccl-profiler-inspector.so` against the target's own CUDA/NCCL
   headers. **`CUDA_HOME` must be set explicitly on the `make` invocation
   itself** (the Makefile's own `$(CUDA_HOME)` reference does not fall
   back to a default) — an empty/unset `CUDA_HOME` fails with `cuda_runtime.h:
   No such file or directory` even though the toolkit is installed. No new
   link-time CUDA dependency is introduced (see the Makefile's own
   comment: the plugin resolves `cudaGetDevice()` at load time from
   whatever CUDA runtime the training process already has loaded, the
   same precedent NCCL's own `cudawrap.cc` uses) — this build works
   unmodified against a different CUDA/driver version (confirmed: built
   against CUDA 13.0 / NCCL 2.29.7 here vs. the original cluster's own
   versions).
4. Deploy the freshly-built `.so` via the `NCCL_PROFILER_PLUGIN` env var
   pointing directly at it — this does **not** require overwriting the
   node's system-installed copy (if any); the env var is sufficient and
   is the safer option on a shared/live node.
5. Install `bpftrace` (`apt-get install -y bpftrace`, pulls in
   `llvm-18`/`libllvm18` automatically) on every host that will run the
   eBPF storage check — **check first whether it's already installed**
   (`dpkg -l bpftrace`), and if `apt-get update`/`install` fails with
   `Network is unreachable`, check for an IPv6-only routing failure and
   retry with `sudo apt-get -o Acquire::ForceIPv4=true install -y
   bpftrace` (see §2's apt/network troubleshooting note) rather than
   assuming the mirror itself is down.
6. Check, then apply only if needed, the two persistent bpftrace fixes
   from §2 (libLLVM SONAME resolution; tracefs bind-mount wrapper) — do
   not apply either blindly; both are real fixes for real but
   *conditional* problems, not universal requirements.
7. Confirm `/sys-host` (or an equivalent host-tracefs-reachable mount) is
   present on the target's own container/jail setup — if the target
   platform's container runtime doesn't already bind-mount host `/sys`
   somewhere reachable, this needs its own platform-level change before
   anything eBPF-based will work at all.
8. Real disk-fault positive/negative control test, per node: write a real
   file to a genuinely local, writable disk path on the target (**don't
   assume any specific path name transfers** — e.g. `/mnt/local-data` on
   the original cluster's own convention was not writable by the
   migration user on the 6-node cluster; `df -T` and look for a real
   local filesystem type, not virtiofs/NFS/overlay, that you can actually
   write to — `/tmp` worked on the 6-node cluster), drop caches, do a real
   cold O_DIRECT read while `iowait_agent.bt` is running, and confirm (a)
   the reading PID shows real io-wait above the CONFIRMED floor and (b) a
   concurrent CPU-only busy-loop process shows none. Confirmed both
   directions, independently, on 3/3 nodes this migration.
9. Validate `gpu_slot_index` with a **true one-process-per-GPU** launch
   (e.g. `nccl-tests`' `all_reduce_perf -g 1` with one Slurm task per GPU),
   not a single-process-multi-GPU launch (`-g N` in one process) — the
   latter doesn't exercise the same per-thread capture path
   `gpu_slot_index` was built against and won't give a clean per-GPU
   signal (confirmed this migration: a `-g 8` single-process test only
   ever logged one `gpu_slot_index` value per node, while a proper
   one-process-per-GPU test correctly logged 0/1/2/3 independently on
   every physical node tested).
10. Install the VMSingle Helm chart (§3), confirm the pod comes up
    healthy. **Still blocked on the 6-node cluster** — needs a real
    kubeconfig with actual install permissions, not the in-cluster pod
    token.
11. Launch `node_aggregator_ref.py` per host, pointed at the fresh
    VMSingle instance, alongside a real training/inference job (env vars
    from §2 set on the job itself).
12. Launch `alert_engine.py` pointed at the same VMSingle instance.
13. (Optional, for storage-fault detection) launch `iowait_logger.py
    <hostname> <log_dir>` per host, and point `alert_engine.py`'s
    `IOWAIT_LOG_DIR` constant at that same `<log_dir>`.
14. Smoke test: run a short real job, confirm real metrics/alerts appear
    for a genuinely healthy run (zero false CONFIRMED), then inject a
    real fault (prefer `STRAGGLER_SLEEP_MS`/`STRAGGLER_TARGET_RANKS` —
    see `workloads/` table above and §2's env var entry — over
    `nvidia-smi -lgc`, which is now confirmed unreliable on this
    project's own hardware; see §5) and confirm the corresponding real
    alert fires. **Fully completed and re-validated multiple times this
    session** (P27-era regression sweep + gap-closure work) on the
    6-node cluster's worker-2/worker-3 — see `codebase_reference.md` and
    `straggler_dectection_history.md` (Phase 13 onward) for the full
    real results. **Mandatory verification standard established this
    session, apply on every future cluster**: record the real injected
    `STRAGGLER_TARGET_RANKS` value (or, for MoE's job-wide netfault, that
    it's job-wide, not rank-specific) BEFORE checking alerts; after an
    alert fires, cross-reference its flagged `member` (a PID) against
    the real `gpu_slot_index` that PID reported (via
    `agg_member_gpu_slot_index{member="<pid>"}` in VictoriaMetrics, or
    the PID's own dump file's `metadata.gpu_slot_index`/`header.rank`
    fields) and explicitly state whether it's an exact match — "the
    alert fired correctly" without this explicit rank-vs-flagged
    comparison is not a sufficient validation standard for this
    pipeline going forward. Also grep every log for `[CHECK-FAILED]`
    after every test (should be 0 — this is the P27-hotfix
    defense-in-depth marker in `alert_engine.py`'s `_run_check` wrapper;
    see `codebase_reference.md`).

## 5. What will NOT transfer automatically

- **`nvidia-smi -lgc` clock-lock, confirmed BROKEN on this project's own
  H200 hardware (new finding, P27-era, this session)** — not merely
  "hardware-specific" (the previous framing below), but confirmed to
  produce **zero measurable effect** on real collective timing for
  ResNet, ViT, and TP-inference on this exact H200 SKU, discovered only
  because a long-unexplained "TP-inference at 3+ members: zero detectable
  signal" mystery turned out to be this, not a detection-engine gap. **Do
  not assume clock-lock fault injection works on a new cluster without
  independently re-verifying it produces a real, measured slowdown in raw
  collective-timing data first** — the old assumption (any H200 will
  respond to `-lgc`) is now confirmed false at least once, so don't
  extrapolate it to new hardware either. **The real, portable
  alternative, validated this session**: the `STRAGGLER_SLEEP_MS`/
  `STRAGGLER_TARGET_RANKS` software-sleep convention (see `workloads/`
  table above) — a real `time.sleep()` gated by an env var, placed
  directly before the target rank's real contribution to its collective.
  Being pure software, this has no hardware-clock-range dependency at all
  and should transfer to any GPU generation unchanged; it's also now this
  project's primary, preferred injection method for exactly the workloads
  (ResNet/ViT/TP-inference/FSDP) that used to rely on clock-lock alone.
- **The H200-specific 345MHz clock floor** used throughout this project's
  own EARLIER fault-injection testing (`nvidia-smi -lgc 345,345`) is a
  real, hardware-specific value for THIS H200 SKU's own valid clock
  range — it is not a stored constant anywhere in the codebase (it's a
  manual test-invocation convention, not code); given the finding directly
  above, treat this value as unreliable for reproducing a real fault at
  all on a new cluster, not just as "needs recalibrating."
- **`gpu_slot_index`'s `cudaGetDevice()`-on-correct-thread dependency** —
  the capture happens specifically on the thread that has the real CUDA
  context bound at communicator-init time (see Inspector plugin section
  of `codebase_reference.md`); this is NOT something that follows
  automatically from just rebuilding the plugin — a different NCCL
  version's own internal init/threading flow could plausibly call this
  differently, in which case the capture point may need to be
  re-verified (not just recompiled) on the new NCCL version.
  **Re-verified this migration** on the 6-node cluster (NCCL 2.29.7, a
  different NCCL version than the original cluster): a real
  one-process-per-GPU multi-node job (3 nodes × 4 GPUs = 12 real ranks)
  confirmed `gpu_slot_index` resolves correctly (0/1/2/3, matching real
  device order) independently on every one of the 3 physical nodes
  tested — the first time this has been validated beyond a 2-node
  cluster. Must be re-verified with a true one-process-per-GPU launch
  (see §4 step 9) — a single-process-multi-GPU test does not exercise
  the same code path and gives an inconclusive result.
- **The jail-namespace PID-nesting-depth assumption in `iowait_agent.bt`**
  — `curtask->thread_pid->numbers[1].nr` hardcodes "level 1" as the
  correct jailed-PID namespace depth, confirmed correct for THIS
  cluster's own one-level-deep jail architecture. A different
  container/jail nesting depth (e.g. two levels deep) would need this
  changed to the correct `numbers[N].nr` for its own real nesting — this
  won't silently fail, but it also won't silently self-correct; it needs
  a real re-verification against the new environment's own process tree
  the same way the original P20c session verified level 1 here.
  **Re-verified this migration**, independently on each of worker-1,
  worker-3, and worker-4 (real cold-disk-read PID matched exactly by the
  agent's `numbers[1].nr` capture on all three) — level 1 still holds on
  the 6-node cluster, but this was re-checked live per node, not assumed
  from the original cluster or from one node to the next.
- **`detection.py`'s offline/historical-replay analysis engine's
  2-node/8-rank-per-node data model** (`NODE_A`/`NODE_B`/`rank_node()`,
  and everything built on it — `score_node_scoped_per_node`,
  `score_node_vs_node`, `classifier.py`'s own node-grouping suppression
  logic, `cv_fixed.py`/`decay_counter.py`/`transient_latency.py`'s peer
  computation) — found and confirmed this session (P27-maintenance
  cluster-topology audit), NOT fixed. This is real, disclosed, and
  confined to the OFFLINE post-hoc dump-analysis/calibration/sweep tools,
  not the live production alerting path (see below — that part WAS
  fixed). A cluster with a different node count or GPUs-per-node would
  get silently wrong node-vs-node/per-node grouping from these specific
  offline tools; genericizing them means redesigning this engine's
  per-rank data model, a substantial separate undertaking.
- **The IB device count / per-host GPU count discovery fixes** made this
  session (`discover_ib_devices`, `discover_gpu_count`) are implemented
  and confirmed safe (graceful `[]`/`0` degradation on a host with none),
  but the real *positive* case — correctly discovering a differently-
  shaped node's actual device/GPU count — has not been live-re-validated
  against real GPU/IB hardware; GPU/worker access was down this entire
  session.

- **A real, reproducible, still-unexplained host-load artifact
  (P27-era, this session)**: `worker-2` on the 6-node cluster fired a
  `type=host CONFIRMED/PAGE` alert (real ratio ~7-9x, but tiny absolute
  load — ~0.15 vs ~0.02 load/core) in 3 out of 3 independent MoE
  healthy-baseline runs, with no corresponding Slurm job visible via
  `squeue` to explain it. This is NOT caused by anything in this
  pipeline's own detection logic (the host-load check's own math is
  simple and was not touched this session) — it looks like a real,
  small, persistent background process specific to that one physical
  node. Watch whether this recurs on the new cluster's own nodes; if it
  doesn't, that confirms it was a 6-node-cluster-specific artifact worth
  reporting to whoever administers that cluster, not a pipeline bug to
  chase further here.

**Fixed this session, no longer a migration risk**: the LIVE production
alerting path's own host/GPU-slot targeting
(`alert_engine.py`'s `build_finding_for_alert` →
`P18k_classifier.build_single_rank_finding`) used to hardcode a "rank <
8 → worker-0, else worker-1" / "rank % 8" 2-node/8-GPU-per-node
assumption via a "placeholder rank" adapter trick — this was the single
most impactful finding of this session's audit, since it affected LIVE
alerting, not just offline analysis. Now passes the real hostname and
real `gpu_slot_index`-derived slot straight through with no arithmetic
translation at all — live-validated with a deliberately different-shaped
hostname (`"gpu-node-3"`). This item is REMOVED from migration risk for
the live path; the OFFLINE analysis engine's own separate instance of
this same class of assumption (above) is NOT removed.
