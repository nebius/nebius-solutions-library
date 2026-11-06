# GPU straggler / fail-slow detection pipeline (V1 Beta)

Real-time detection of straggling or slowing GPUs on a Soperator (Slurm-on-Kubernetes) cluster. NCCL's Inspector
profiler plugin writes per-collective timing from every rank; per-node aggregators turn it into metrics in
VictoriaMetrics; `alert_engine.py` compares ranks against their peers and prints `[ALERT]` lines (and Grafana panels)
naming the host, GPU and process. Pure-Python pipeline, no third-party packages.

**Platform**: This pipeline runs on **two platforms** — Slurm/soperator (original) and **plain Kubernetes** (K8s, added
2026-11). The detection code is shared; only the launcher and topology-discovery layer differ. K8s verified on 2-node
16-GPU H200. **Slurm now verified at full production scale: 6 nodes / 48 GPUs H200** (2026-11, commit `691c296d` +
this validation), in addition to the earlier 2-node/16-GPU shape (see Known limitations for exact scope and what
remains 2-node-only).

---

## Quickstart — Platform: Kubernetes (plain K8s)

> **Kubernetes status: VERIFIED** (commit `5ee1214`, 2026-11). Tiered mean check fired correctly on 3/3 fault trials
> (100 ms sleep, rank 1, 2-node H200), zero false positives on 3/3 healthy trials, zero crashes or CHECK-FAILED.
> The earlier "3/3 detections" claim used an inline script, not `alert_engine.py`; this re-validation used the
> actual alert engine running as a K8s pod. See DESIGN_NOTES §6.3 for the full data.

Requires: `kubectl` configured to a cluster with at least 2 GPU nodes (H200 tested), `nfs` StorageClass, github.com
access from pods. GPU nodes must be schedulable (not exclusively allocated to Slurm). On soperator clusters, scale
the Slurm worker NodeSet to 0 first: `kubectl scale nodeset worker -n soperator --replicas=0`.

1. **Clone** (on the control machine where kubectl runs)
   ```bash
   git clone -b add/straggler-detection-v1-beta https://github.com/nebius/nebius-solutions-library.git
   cd nebius-solutions-library/soperator-straggler-detection/detection-pipeline
   ```
2. **Install** (creates namespace, NFS workspace, builds Inspector plugin + NCCL 2.32.3 in a GPU pod)
   ```bash
   k8s/install_k8s.sh
   # For full CPU pinning (builds NCCL 2.32.3, ~15 min extra): INSTALL_K8S_BUILD_NCCL=1 k8s/install_k8s.sh
   ```
   Success = `install_k8s.sh completed` and `cluster_k8s.env` written with real node/GPU counts.
3. **Launch the pipeline** (VictoriaMetrics already deployed by install; this starts aggregator sidecars + alert engine)
   ```bash
   source cluster_k8s.env
   k8s/run_k8s.sh [steps] [port] [pin_mode]   # defaults: 300 steps, port 29500, pin_mode=auto
   ```
   Exits after confirming aggregator heartbeats from both GPU nodes in VictoriaMetrics.
4. **Verify CPU pinning** (check PIN-PREFLIGHT line in training pod logs)
   ```bash
   kubectl logs -n straggler-detection -l straggler.nebius.ai/component=training -c training | grep PIN-PREFLIGHT
   # Expected: resolved=full nccl=2.32.3 (if NCCL 2.32.3 built) or resolved=dump
   ```
5. **Monitor**: alerts appear in `var/alert_engine_k8s_supervised.log`. Full-pinning overhead on K8s: `mfu=76.77%`
   (full pinning) vs `75.32%` (dump-only); the full-pinning mode is marginally faster, same as Slurm.

**K8s-specific notes**:
- Rendezvous uses headless Service + pod DNS (K8s 1.27+ sets pod hostname to `<job>-<index>`), not ClusterIP,
  to avoid Cilium's hairpin NAT restriction on this cluster.
- `/dev/shm` is overridden to 32Gi (K8s default 64MB is insufficient for NCCL with 8 GPUs).
- DCGM cause-classification is unavailable (the alert engine cannot SSH to K8s node internal hostnames from the
  control machine). The Inspector-based timing detection (CV/mean checks) is the same code as on Slurm; K8s behaviour is unverified for the tiered check.
- **Detection threshold calibration**: the Slurm-validated thresholds (`CV_Z_THRESH=60`, `MEAN_MM_THRESH=2.0`)
  detect 100ms faults on Slurm within 50-56s. On K8s, the same fault produces lower z-scores due to higher
  baseline InfiniBand timing variability in containers; the thresholds need K8s-specific calibration before
  production use. The raw timing signal IS captured correctly (rank 1 at 377μs vs 182,000μs for all others in
  the 2000ms fault test), and the aggregator's window-level z-scores spike correctly (z=4080 briefly).
- When done testing, return GPU nodes to Slurm: `kubectl scale nodeset worker -n soperator --replicas=2`

---

## Quickstart — Platform: Slurm/soperator (original)

## Prerequisites

| | Tested with |
|---|---|
| Cluster | Soperator / Slurm with pyxis+enroot (`srun --container-image`), passwordless `ssh` login node -> workers, shared `/root` (or any path every node can read) |
| GPUs | 8x NVIDIA H200 per node, driver `580.159.04`. 2 nodes (16 GPUs) and **6 nodes (48 GPUs, full production scale)** tested |
| CUDA toolkit | 13.0 (`nvcc` 13.0.88) on the node that builds the plugin |
| NCCL | Inspector plugin tree: 2.28.9 (`environment.sh` obtains it). Runtime NCCL: host 2.30.1 works (dump-thread pinning only); **>= 2.32.3 for full pinning** (build step below) |
| Container image | `nvcr.io#nvidia/pytorch:25.01-py3` |
| Host tools | `python3`, `gcc`/`g++`, `git`, `make`, `ssh`, `curl`, `squeue`/`srun`/`scontrol`, `nvidia-smi`; `bpftrace`, `dcgmi`, `logrotate`, `libibverbs-dev` (installed by `install.sh` where it can) |
| Network | github.com (clone, NCCL and VictoriaMetrics downloads) and dl.grafana.com from the login node |

Exact pins and why: [VERSIONS.md](VERSIONS.md).

## Quickstart

Run everything from the login node. Allow about 30 minutes plus the NCCL build (~17 min).

1. **Clone**
   ```bash
   git clone -b add/straggler-detection-v1-beta https://github.com/nebius/nebius-solutions-library.git
   cd nebius-solutions-library/soperator-straggler-detection/detection-pipeline
   ```
2. **Bring up prerequisites and install** (each script is safe to re-run; skip one if the host already has it)
   ```bash
   ./environment.sh     # compiler, CUDA 13.0, NCCL 2.28.9 source/libs for the plugin build
   ./vm-setup.sh        # VictoriaMetrics on this host (port 8428)
   ./grafana-setup.sh   # Grafana with enforced login (optional)
   ./install.sh         # discovers nodes, builds the Inspector plugin and the pinning interposer, writes cluster.env
   ```
   Success = exit code 0 and a last line `install.sh completed. Environment is ready`, after it prints the generated
   `cluster.env` (node list, GPUs per node, VM and Grafana URLs). A hostname/DNS `WARNING` about Grafana access is
   informational. Any non-zero exit means stop and read the message above it.
3. **Build NCCL >= 2.32.3 for full CPU pinning** (optional; without it the dump thread is still pinned)
   ```bash
   pinning/nccl/fetch_build_nccl.sh --dest /root/nccl-2.32.3     # a path every compute node can read
   ```
   Allow 10-20 minutes. Success ends with `version=2.32.3 lib=... -> resolved mode=full` and
   `[nccl] OK: recorded <dir> in pinning/.nccl_override_dir` (8.5 minutes in the test run). On a GPU-less login node add
   `--gencode "-gencode=arch=compute_90,code=sm_90"` (H200/H100; use your GPU's arch).
4. **Launch the standing pipeline** (aggregators on every node + alert engine)
   ```bash
   ./run.sh
   ```
   It exits 0 only after it has seen a heartbeat from every node and the engine cycling.
5. **Smoke test: inject a real fault and require the right rank**
   ```bash
   ./tools/self_test.sh
   ```
   Takes about 8-10 minutes. Success: `RANK-MATCH CONFIRMED ... EXACT MATCH` and `=== RESULT: PASS ===` (it found an
   `[ALERT]` naming exactly the injected rank; the alert may be `PROBABLE`/`LOG-ONLY`).
6. **Optional: Megatron run with Inspector and pinning.** The launcher defaults to 6 nodes / 48 GPUs (TP4/PP4/DP3) and
   takes its nodes from `cluster.env`. On 2 nodes / 16 GPUs use the `MEGA_*` shape below (skip that line on 6 nodes).
   `--cpus-per-task` must be a **compute node's** CPU count, not the login node's.
   ```bash
   cd workloads/megatron
   export SRUN_EXTRA="--exclusive --cpus-per-task=$(ssh worker-0 nproc) --cpu-bind=none"   # first node in cluster.env NODE_LIST
   export MEGA_TP=4 MEGA_PP=2 MEGA_LAYERS=40 MEGA_HIDDEN=4096 MEGA_HEADS=32 MEGA_SEQ=2048 MEGA_MBS=1 MEGA_GBS=32   # 2 nodes only
   ./run_megatron_tp4pp4dp3.sh 300 /root/run1 29500 /tmp/insp_dump_run1       # returns at once ("launched, driver pid N")
   while kill -0 $(cat /root/run1/srun_driver.pid) 2>/dev/null; do sleep 10; done   # wait for the job (~10 min)
   grep -h 'PIN-PREFLIGHT' /root/run1/full_output.log; grep -h 'verdict' /root/run1/pin_verify_*.log
   ```
   Expected: one `[PIN-PREFLIGHT] ... resolved=full` per node (`resolved=dump` if NCCL < 2.32.3 was not built) and
   `[PIN-VERIFY] ... verdict=FULLY_DEDICATED` (`DUMP_ONLY_DEDICATED` when resolved=dump) with `ranks=8/8` per node.
   Alerts appear in `var/alert_engine_supervised.log`; `python3 tools/readable_alerts.py var/alert_engine_supervised.log`
   prints them in a short form. Turn pinning off with `PIN_MODE=off`.

## Known limitations

- **Scale:** validated at 16 GPUs (2 nodes) on both Slurm and K8s, and now at **full production scale on Slurm: 48
  GPUs / 6 nodes**, Megatron TP4/PP4/DP3, 8B-class shape (2026-11, see DESIGN_NOTES.md 6.6). K8s remains 2-node-only.
- **Communicator/tier structure at 6 nodes is genuinely richer than the 2-node shape**, not just "more of the same":
  real TP groups (n_ranks=4, 1 node), PP Send/Recv groups (n_ranks=4, spanning 2 nodes), DP ReduceScatter/AllGather
  groups (n_ranks=**3**, spanning **3** nodes — smaller and more node-spread than the 2-node case's DP=2), a 2-member
  tied-embedding AllReduce group, and a previously-unseen job-wide 48-member (6-node) group used for tiny (4-8 B)
  sync collectives. The tiered check's own 512 B floor (6.5.4) already excludes that 48-member group from evaluation
  (its messages are far below the floor); the 2-member embedding group is excluded too, by Guard 2 (needs >=2
  reference members after removing a candidate straggler) — a real, newly-confirmed coverage gap: a fault that only
  affects embedding-tie sync would not be caught by this check at this shape. TP/PP/DP groups above the floor are
  all correctly evaluated (confirmed live, see 6.6.2).
- **CPU pinning**, validated on both platforms at 16 GPUs and now at 48 GPUs (Slurm). Overhead at 16 GPUs: full
  pinning +0.64% (n.s.), dump-only +6.4%, unpinned +11.5%. **At 48 GPUs, full pinning vs. unpinned is a much larger,
  highly significant effect: full-pin iterations are ~7.8% FASTER than unpinned (335.4ms vs. 363.8ms mean, Welch
  t=-19.2, n=870/870 steady-state samples/arm)** — the benefit does not just "hold", it grows substantially at this
  scale (see DESIGN_NOTES.md 6.6.4; the 3 ON vs. 3 OFF trials were run in two blocks, not strictly interleaved pair
  by pair — a real methodological gap from the established interleaved-A/B convention, noted honestly, though the
  effect size make a block-order confound unlikely to explain it). Full dedication verified via real `/proc` affinity
  checks on 48/48 ranks across all 6 nodes (`[PIN-VERIFY] verdict=FULLY_DEDICATED ranks=8/8` on every node).
  Untested: single-NUMA, 4-GPU nodes, SMT off, ARM, small CPU masks.
- **Tiered mean check (`_check_mean_tiered`)**, Slurm status: verified on 2 nodes/16 GPUs (TP4/PP2/DP2, as before)
  AND now on **6 nodes/48 GPUs** (TP4/PP4/DP3, 100 ms fault on rank 8, 3 healthy + 3 fault + 1 storage-fault trial,
  all on the final bug-fixed code, full CPU pinning active). Healthy (3/3): 0 tiered incidents, 0 `CHECK-FAILED`.
  Compute fault (3/3): true rank named every time, with a complete `[ALERT]`, via either the TP AllReduce group
  (role_rank matching the rank's own TP slot) or the PP Send group (role_rank matching its PP stage) — detection
  latency 43-60s persistence at this shape (faster in wall-clock than the 2-node case's 167-169s, though job
  shapes differ enough that this isn't a clean apples-to-apples speed comparison). Each fault trial also produced
  1-2 secondary alerts on a *different*, non-injected rank (downstream PP-stage cascade) — consistent with the
  already-documented "mirror-position" pattern, not a new problem. Storage fault (1 trial, `STORAGE_FAULT_FILE_MB`):
  the true rank (8) WAS named correctly, but a genuinely new nuance showed up — two *other*, non-faulted ranks fired
  with much higher `severity_ratio` (up to ~995 vs. rank 8's own ~2.4-2.6) and kept firing for the entire 40+ minute
  job, not just a brief window. A naive "highest severity wins" triage heuristic would point at the wrong rank for
  this fault type; `severity_ratio` magnitude is not by itself a reliable ranking signal across fault types. The
  512 B floor (`TIERED_MIN_BUCKET_BYTES`) was re-checked against this shape's real message sizes: the gap between
  the largest sub-floor collective (96 B) and the smallest real gradient-bearing collective (~2.29 MB) is far wider
  here than the shape the floor was derived from, so 512 B holds with a large safety margin — no new sweep needed.
  Untested: other fault sizes/ranks, other node counts between 2 and 6. Details: DESIGN_NOTES.md 6.6.
- **Noise path:** the main-path `mean` check is gated off for 4-member communicators (`MEAN_CHECK_GATED_ROLE_N=4`; set it to `""` to restore). It produced about 64% of healthy-run alerts on the TP4/PP2/DP2 shape and never named the faulted rank. After the gate, 2 healthy runs went from 18 and 24 total alerts to 9 and 6 (mean-check alerts 12 and 16 down to 0, no PAGE), and 3 fault runs still named the true rank in 51, 50 and 56 s. The criterion comes from one shape, so a 4-member communicator in another job is gated too. Remaining LOG-ONLY noise is from other paths.
- **Memory growth:** aggregators about +5 to +7 MB/hour and the alert engine about +4 MB/hour over a 107-minute soak;
  cause not found. Restart them between very long campaigns.
- **Unexplained:** fault runs show fewer alerts than healthy runs on the TP4/PP2/DP2 shape.
- **Attribution:** in 2 of 9 fault trials the wait-induced check also named the mirror-position replica rank
  (LOG-ONLY). `rank=` in alerts is an OS PID, not the training rank.
- **DCGM clock fallback** now needs a sustained signal before it pages, so a real sustained clock fault pages
  about 10-60 s later than before.
- Only `PAGE` alerts page a human; `LOG-ONLY` alerts are recorded for the dashboard and logs.
- **`run.sh`/`self_test.sh` can silently validate against a stale `alert_engine.py`**: both detect an
  already-running `run_alert_engine_supervised.sh` purely by process name and reuse its log path, with no check
  that it's running *this* checkout's code. Found live (2026-11): a supervisor from an older, unrelated checkout had
  been running for weeks; `self_test.sh`'s PASS was against that stale code, not the current bug-fixed
  `_check_mean_tiered`. If you need to confirm behavior of code you just changed, check the running process's actual
  script path and `sha256sum` it against your checkout before trusting `run.sh`'s "already running -- leaving it
  alone" message, or launch a second `alert_engine.py` instance pointed at the same `VM_URL` with its own
  `--summary-log`/log path (safe: it's a stateless, read-only VM consumer, no lock file).
- **The quickstart's example `OUTDIR`/`DUMPDIR` (`/root/run1`, `/tmp/insp_dump_run1`) assume root access.** A
  non-root user cannot write to `/root` at all (confirmed live: `Permission denied`), and a path outside this
  package's root or `/root`/`/tmp` is **not bind-mounted into the training container**, so the job reports
  `TORCHRUN_EXIT: 0` almost instantly with an empty `train_<host>.log` -- a silent near-no-op, not an error. Use a
  path under the package root (e.g. `var/runs/<name>`, `var/dump`) or `/tmp`, which are mounted; `/tmp` is node-local
  (not shared with the login node) and `var/dump` is this package's own NFS mount, so cross-reference with `pwd` on
  the right host before assuming a dump/log file should exist somewhere.
- **K8s platform**: DCGM SSH-based cause classification unavailable (control machine cannot SSH to K8s node
  internal hostnames). Detection thresholds (`CV_Z_THRESH=60`, `MEAN_MM_THRESH=2.0`) validated on Slurm need
  K8s-specific calibration: K8s InfiniBand timing variability in containers raises the noise floor, making the
  same 100ms sleep fault produce lower z-scores than on Slurm. The timing signal IS captured (raw difference
  confirmed); the ALERT doesn't fire for small faults without threshold recalibration.

## Further reading

| For | Read |
|---|---|
| Everything that used to be in this README (architecture, every install detail, troubleshooting, Grafana, all findings, the 15 validated shapes) | [docs/DETAILED_REFERENCE.md](docs/DETAILED_REFERENCE.md) |
| 2026-11 audit findings and decisions | [docs/DETAILED_REFERENCE.md](docs/DETAILED_REFERENCE.md) section 7.0 |
| CPU pinning design, config, measured data | [pinning/README.md](pinning/README.md) |
| Operations / ownership / on-call | [MAINTENANCE.md](MAINTENANCE.md) |
| Why it is built this way, by mechanism | [DESIGN_NOTES.md](DESIGN_NOTES.md) |
| Project history and validation record | [docs/straggler_dectection_history.md](docs/straggler_dectection_history.md), [docs/TESTING_METHODOLOGY.md](docs/TESTING_METHODOLOGY.md) |
| Code map | [docs/codebase_reference.md](docs/codebase_reference.md) |
| Attaching your own Slurm job | [docs/DETAILED_REFERENCE.md](docs/DETAILED_REFERENCE.md) section 9 |
