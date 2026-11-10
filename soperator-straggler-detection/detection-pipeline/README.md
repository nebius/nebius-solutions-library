# GPU straggler / fail-slow detection pipeline (V1 Beta)

Real-time detection of straggling or slowing GPUs on a Soperator (Slurm-on-Kubernetes) cluster. NCCL's Inspector
profiler plugin writes per-collective timing from every rank; per-node aggregators turn it into metrics in
VictoriaMetrics; `alert_engine.py` compares ranks against their peers and prints `[ALERT]` lines (and Grafana panels)
naming the host, GPU and process. Pure-Python pipeline, no third-party packages.

**Platform**: This pipeline runs on **two platforms** — Slurm/soperator (original) and **plain Kubernetes** (K8s, added
2026-11). The detection code is shared; only the launcher and topology-discovery layer differ. K8s verified on 2-node
16-GPU H200. **Slurm now verified at full production scale: 6 nodes / 48 GPUs H200** (2026-11, commit `691c296d` +
this validation), in addition to the earlier 2-node/16-GPU shape (see Known limitations for exact scope and what
remains 2-node-only). **The Slurm quickstart below has been run start-to-finish from a genuinely fresh clone**
(2026-11): `environment.sh`/`vm-setup.sh`/`install.sh`/`run.sh`/`tools/self_test.sh` all pass exactly as documented,
with one real `run.sh` health-check bug found and fixed in the process (see DESIGN_NOTES §6.31.5).

---

## Quickstart — Platform: Kubernetes (plain K8s)

> **Kubernetes verification status** (2026-11, 2 nodes / 16 H200 GPUs):
> - **nanoGPT 85M (DDP)**: tiered mean check fires 3/3 fault trials, zero false positives 3/3 healthy. ✓
> - **FSDP (Fully Sharded Data Parallel)**: tiered mean check fires on rank=1, rank=5, rank=9 (mm=27–979×,
>   440KB AllGather). Zero false positives 3/3 healthy. ✓ Detection is rank-position-agnostic. Requires RDMA
>   (NCCL_IB_DISABLE=1 must NOT be set — FSDP is single-process-group, no HPC-X segfault risk). Requires
>   NCCL >= 2.26 (profiler_v5 support) — build via `deploy.sh --build-nccl`. See DESIGN_NOTES §6.14.
> - **MoE (Mixture of Experts)**: training works (3/3 clean, TORCHRUN_EXIT: 0). NOT DETECTABLE with
>   sleep-before-backward injection — AllToAll expert routing is in forward pass (before sleep); tiny per-param
>   AllReduce after backward is below the bucket floor. See DESIGN_NOTES §6.13.2.
> - **Megatron DP (TP=1/PP=1)**: existing `_check_mean` detects straggler signal (8-byte post-backward AllReduces);
>   tiered check correctly silent (small bucket below 512-byte floor). 3/3 healthy trials clean. ✓
> - **Megatron TP/PP training (TP=2/PP=2)**: TRAINING WORKS (TORCHRUN_EXIT: 0, 6/6 trials). ✓
>   HPC-X segfault resolved with `NCCL_NET_PLUGIN=/nonexistent` + PyTorch 2.6 `device_id` fix. See DESIGN_NOTES §6.9.1.
> - **Megatron TP/PP straggler detection**: SHAPE-DEPENDENT (corrected from "2-node structural limit").
>   **TP=4/PP=1/DP=4 WORKS** on this 2-node/16-GPU cluster: 4/4 fault trials detected via
>   `mean_tiered` on the TP AllReduce (role_n=4), mean_tiered 0 false positives on 3 healthy trials. ✓
>   Shapes with any 2-member gradient-sync group (e.g. TP=2/PP=2/DP=4) remain undetectable (z≈0.707
>   invariant for 2-member groups). The constraint is shape-dependent, not node-count-dependent.
>   See DESIGN_NOTES §6.25 for the full analysis and working shape specification.
> - **Alert engine stability**: 33 MB RSS stable over 3000-step soak run. No memory leaks confirmed.
> - **Node counts > 2**: NOT TESTED. Tier structure at 4+ nodes is an unknown.

**Prerequisites** (cluster-agnostic; substitute your cluster's values where shown):

> **⚠️ NCCL 2.32.3 is required for straggler detection.** The system NCCL in pytorch:25.01-py3
> (2.25.1) only supports profiler API v1/v2; Inspector exports only v5. Without NCCL 2.32.3,
> Inspector writes zero dumps — training runs fine, but detection produces zero alerts silently.
> The startup check `[NCCL-PROFILER-CHECK]` will log `[CHECK-FAIL]` with the exact remedy.
> Build NCCL 2.32.3 by passing `--build-nccl` to `deploy.sh` (adds ~2 min), or run:
> ```bash
> bash pinning/nccl/fetch_build_nccl.sh --dest /workspace/nccl-2.32.3 \
>   --gencode "-gencode=arch=compute_90,code=sm_90"
> ```

- `kubectl` >= 1.27 configured to your cluster
- At least 2 GPU nodes schedulable (not exclusively reserved for other workloads)
- A `ReadWriteMany` StorageClass — to find yours:
  ```bash
  kubectl get storageclass
  # Look for a class backed by an NFS or shared-filesystem provisioner
  # (e.g. nfs.csi.k8s.io, efs.csi.aws.com, filestore.csi.storage.gke.io).
  # StorageClass output doesn't show access modes directly; NFS-based
  # provisioners typically support ReadWriteMany, which is what deploy.sh needs.
  # On Nebius clusters the class is typically "nfs". On other clusters it may
  # be "efs", "azurefile", "filestore", etc.
  # Pass it as: STORAGE_CLASS=<name> k8s/deploy.sh
  ```
- github.com reachable from pods (for initial workspace clone)
- Container image `nvcr.io/nvidia/pytorch:25.01-py3` pullable from pods

**On soperator (Slurm-on-K8s) clusters**: GPU nodes are exclusively allocated to Slurm workers. Free them first:
```bash
kubectl scale nodeset worker -n soperator --replicas=0   # confirm no active Slurm jobs first
# After K8s testing: kubectl scale nodeset worker -n soperator --replicas=<original>
```

1. **Clone** (on the control machine where kubectl runs)
   ```bash
   git clone -b add/straggler-detection-v1-beta https://github.com/nebius/nebius-solutions-library.git
   cd nebius-solutions-library/soperator-straggler-detection/detection-pipeline
   ```

2. **One-command deploy** (creates namespace, workspace PVC, VictoriaMetrics, alert engine)
   ```bash
   # With your cluster's RWX storage class (default: nfs):
   STORAGE_CLASS=<RWX_STORAGECLASS> k8s/deploy.sh
   # Optional flags:
   #   --gpu-label nebius.com/gpu=true   (default; override for other clusters)
   #   --build-nccl                      (builds NCCL 2.32.3 for full CPU pinning, ~15 min)
   ```
   Success = last line `=== Deploy complete ===` and `cluster_k8s.env` written.
   Alert engine starts automatically inside the cluster (direct ClusterIP access to VM, no port-forward needed).

3. **Smoke test** (nanoGPT 85M, 2 nodes, verify Inspector + aggregator + tiered detection)
   ```bash
   source cluster_k8s.env
   k8s/run_k8s.sh 300    # 300 training steps, ~90s
   # Watch alerts: kubectl logs -n straggler-detection deployment/alert-engine -f | grep -v DCGM
   # Inject fault (100ms sleep on rank 1) for a live detection test:
   SLEEP_MS=100 TARGET_RANKS=1 k8s/run_k8s.sh 2000
   # Expected: [STRAGGLER-INCIDENT] stat=mean_tiered + [ALERT] within 2-3 min of training start
   ```

   > **Inspector cold-start note (fresh deployments):** On some cluster NCCL builds, the Inspector
   > profiler plugin may not activate on the VERY FIRST training run after a fresh `deploy.sh`. If
   > the fault test shows TORCHRUN_EXIT: 0 but no `[STRAGGLER-INCIDENT]` fires, run a 100-step
   > warmup first (`k8s/run_k8s.sh 100`) then re-run the fault test. This is a known issue with
   > NCCL versions that only support profiler_v1/v2 (e.g. NCCL 2.25.1); the pipeline runs
   > correctly with NCCL >= 2.26 or when NCCL 2.32.3 is built via `deploy.sh --build-nccl`.
   > See DESIGN_NOTES §6.13.7 for the full diagnosis.

4. **FSDP smoke test** (Fully Sharded Data Parallel — requires 2 GPU nodes, 8 GPUs each)

   **Prerequisites for this step**: Step 2 (`deploy.sh`) must have been run with `--build-nccl`
   (NCCL 2.32.3 is required for FSDP detection; without it Inspector writes no dumps). The
   `cluster_k8s.env` file used below is generated automatically by `deploy.sh` in Step 2.

   ```bash
   source cluster_k8s.env   # generated by deploy.sh
   # Healthy run first (1000 steps, ~6 min — verify no false positives):
   bash k8s/run_fsdp_k8s.sh 1000 29511 dump 0 "" constant backward
   # Fault injection (100ms sleep on rank=1, 2000 steps — detection within ~2 min of data flowing):
   bash k8s/run_fsdp_k8s.sh 2000 29511 dump 100 "1" constant backward
   # Watch alerts: kubectl logs -n straggler-detection deployment/alert-engine -f | grep STRAGGLER-INCIDENT
   # Expected on fault run: triage=PRIMARY[likely-root-cause] role_rank=1 gpu_slot=1 severity_ratio=25-979x
   ```

5. **Megatron TP/PP run** (training validated; straggler detection requires 4+ nodes — see Known Limitations)
   ```bash
   source cluster_k8s.env
   # Training works on pytorch:25.01-py3 (HPC-X segfault + PyTorch 2.6 device_id fixed):
   MEGA_TP=2 MEGA_PP=2 MEGA_LAYERS=8 MEGA_HIDDEN=512 MEGA_HEADS=8 MEGA_SEQ=512 MEGA_MBS=1 MEGA_GBS=4 \
     k8s/run_megatron_k8s.sh 500 29500 dump 0 ""
   # NOTE: TP=2/PP=2/DP=4 gives only 2-member groups (z≈0.707 invariant) — not detectable.
   # For detection on 2-node/16-GPU clusters, use TP=4/PP=1/DP=4 instead (all groups role_n=4).
   # See DESIGN_NOTES §6.25 for the validated working shape and §6.11 for the shape-dependency analysis.
   ```

**K8s-specific notes** (confirmed on-cluster, 2026-11):
- **Rendezvous**: headless Service + pod DNS (`<job>-<index>.<headless-svc>.<ns>.svc.cluster.local`).
  ClusterIP routing-to-self is blocked by Cilium on this cluster (hairpin NAT).
- **`/dev/shm`**: overridden to 32Gi in pod spec. K8s default 64MB is too small for NCCL with 8 GPUs.
- **Alert engine**: runs as a K8s Deployment (direct ClusterIP to VM). Do NOT use port-forward for the
  alert engine — queries are 0.5s each through port-forward, making cycles > 3 minutes; inside the
  cluster they are < 1ms.
- **DCGM**: SSH-based cause classification unavailable in pods. All K8s alerts show `confidence=UNCONFIRMED`.
  This is a known, documented limitation, not a bug.
- **Job boundary message**: "JOB NOT CONFIRMED STILL RUNNING AT ALERT TIME" appears on every K8s alert
  (squeue unavailable). This is correct behavior — it conservatively reduces confidence at job exit.
- **Megatron startup**: train_node_megatron_k8s.sh adds a rank-staggered sleep before torchrun
  (`15 + rank * 20` seconds) to allow headless service DNS to propagate. This is not needed for
  nanoGPT because pin_resolve.py provides a natural equivalent delay.
- When done: `kubectl scale nodeset worker -n soperator --replicas=<N>` to restore Slurm workers.

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
   pinning/nccl/fetch_build_nccl.sh
   ```
   No `--dest` needed for a normal checkout: the script's own default (`pinning/nccl/build-<tag>`, git-ignored,
   already under this package root and therefore already on the shared filesystem every compute node mounts) is
   correct as-is. **Real bug found and fixed (2026-11, capstone validation):** an earlier version of this step
   told you to pass `--dest /root/nccl-2.32.3` — on a non-root checkout (the common case) this fails immediately
   with `mkdir: cannot create directory '/root': Permission denied`, a confusing failure for a step billed as
   optional-but-safe. Only pass `--dest` if you specifically need the build somewhere other than this package's
   own tree.
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

### Known beta limitations (plain-language summary)

These three are the ones that matter most for a go/no-go POC decision. Everything else below is
detail; these are structural, currently-accepted gaps, not bugs waiting to be fixed before launch.

1. **No alert reaches PAGE tier today — every finding requires a human to read the log.** This
   detector will not page or auto-notify anyone on its own right now. Nearly every real finding
   comes back `confidence=PROBABLE severity=LOG-ONLY`, and that tier is deliberately *not* wired to
   page, because this project's own measured false-positive rate at that confidence level is
   **22-25 alerts/hour** — auto-paging on it would flood on-call with false pages, not catch real
   ones. `PAGE` is reserved for `CONFIRMED` (real hardware evidence, e.g. a DCGM-confirmed fault),
   which a synthetic/software fault correctly never produces. Setting a safe auto-page threshold is
   **planned as post-POC work**, once real POC-scale traffic provides enough data to calibrate one
   responsibly — this was deliberately not guessed at with synthetic-fault data alone. See
   DESIGN_NOTES.md §6.26 for the full investigation and a genuinely independent reviewer's verdict.
2. **Megatron TP/PP straggler detection requires 4+ nodes.** At 2 nodes, every TP/PP gradient-sync
   communicator has exactly 2 members, and a 2-member group's z-score is mathematically invariant
   (z≈0.7) regardless of whether a real straggler is present — this is a structural property of the
   statistic, not a detector-quality gap. Validated working correctly starting at 4 nodes. See
   DESIGN_NOTES.md §6.11/§6.21.
3. **MoE AllToAll straggler detection/localization: RESOLVED at 20ms and 100ms fault magnitudes
   (this project's standard), via a modified Inspector build — a maintained fork, not upstream
   NVIDIA source — after five prior independent attempts failed.** Job-wide MoE slowdown detection
   has always worked (a real, confirmed fault produces a real, job-wide signal); the gap was
   specifically *localizing* it to one rank. (1) NCCL/Inspector-level kernel-timing aggregation
   (`_check_alltoall_tiered`, "Option C") is structurally impossible for AllToAll: it's a
   synchronous collective, so a late rank elevates every rank's measured kernel time together, not
   a fast-outlier/slow-waiter split. (2) Application-level Python-call timing ("Option D") finds a
   real signal, but only above a floor strictly between 100ms and 1000ms, and requires editing the
   training script, violating this project's zero-customer-code-change design philosophy. (3) The
   existing `moe-two-stage-detector/arrival_order.py` ("last-to-arrive" from Inspector's verbose
   timestamps) fails for a real, root-caused round-boundary desync bug. (4) A CUDA-event
   LD_PRELOAD shim on `ncclSend`/`ncclRecv` was built and tested at 1ms/20ms/100ms; it also fails
   to localize at any magnitude — root cause: the CUDA event is recorded at enqueue time, not at
   the point NCCL's real cross-rank synchronization blocks for a slow peer (an
   instrumentation-placement problem). (5) **What finally worked**: modifying Inspector's own
   source (`inspector-plugin/`) to request NCCL profiler v5's `ncclProfileProxyOp`/
   `ncclProfileProxyStep` event types and track the real proxy-thread send/recv peer-wait
   intervals — a signal class none of the first four attempts touched. The straggler rank shows up
   as the **lowest** recv-wait relative to its structural peer group (not highest — everyone else
   waits *on* it; it, arriving late, barely waits at all), confirmed with z-scores of −8 to −13
   (vs. a healthy noise band of ±1.1) across 3 trials each at 20ms and 100ms, with an independent
   secondary confirmation via its direct expert-parallel peers' elevated wait specifically when
   sending to it. **Has its own honest floor**: at 1ms, the same signal is real but weak
   (z≈−1.6 to −2.3), not reliably separable from healthy noise — not shipped as a default check
   below 20ms. Building this required one real crash (a cross-process NCCL proxy-context hazard
   plus a stale-handle-on-reuse hazard, both found and fixed) — see DESIGN_NOTES.md §6.27.2,
   §6.29.6, §6.33, §6.35, §6.36, §6.40 (the five failures) and §6.41 (the fix, the crash, and the
   confirmed result) for the full history and evidence. **Detection today still requires a manual,
   offline analysis step** (`moe_signal_check.py`) — a live, automated scorer/firing function
   (`alert_engine.py`'s `_check_proxy_wait`) was built and is confirmed *mathematically correct*
   (reproduces the manual z-score exactly, zero false positives, no crashes) in isolated testing,
   but NOT yet confirmed to reliably fire throughout a production job's lifetime. K8s validation
   (§6.44) confirmed: (1) `_check_proxy_wait` poll cost is 0.12–0.23s on K8s (not the >300s
   Slurm problem — fresh cluster, less accumulated state); (2) the check fires reliably during
   NCCL communicator setup (first ~4 minutes of wall-clock time) with z=6.08–6.35 on K8s,
   matching Slurm's magnitude when measured with the same live 90s window; (3) after the startup
   phase, the signal fades to z≈4.4 during the training iteration phase of a 300-step job and does
   NOT re-trigger. This means: for a persistent fault present from job start, the check fires
   during startup and is then silent for the remainder of the job. A manual offline analysis
   (`moe_signal_check_k8s.py`) remains the only confirmed-reliable end-to-end path. Two real bugs
   found and fixed during K8s validation: (a) `KeyError: 'proxy_wait'` in classifier dispatch (fix:
   map `proxy_wait → mean`, same pattern as `mean_tiered`); (b) log readability — added explicit
   `straggler_direction=` field to alert header (the inverted direction is counter-intuitive and
   misread without it). See DESIGN_NOTES.md §6.44 for the full timeline, parity confirmation, and
   bounded-limitation statement.

- **Shape/strategy coverage (2026-11, DESIGN_NOTES.md 6.12-6.19):** MoE has a real, intermittent (1-in-3 observed)
  collective-participation hang on healthy runs, independent of the detector, not root-caused (6.12.1). Deep
  pipeline-parallel (TP=2/PP=8/DP=3) confirmed a real detection gap: TP/PP groups below `_check_mean_tiered`'s
  3-member floor produce no attributed alert even for a genuine, dominant fault (6.13.3). FSDP's launcher had no
  fault-injection path at all (fixed); its own "no incident" result is likely confounded by a long-lived engine
  instance's accumulating stale rank-collision state, not confirmed as an independent FSDP-specific failure
  (6.14.3; now superseded by a confirmed, resolved PASS at full 6-node scale -- see DESIGN_NOTES §6.31.2
  and this document's own Known Limitations list above). Fault-position diversity (rank 0, a structural pipeline-stage-0/root position) is correctly attributed
  via the wait-induced alert path in 3/3 trials; the full tiered-incident path did not fire in the tested (short)
  duration (6.16). A storage-migration mount bug broke every containerized job's write access to `var/dump`/`var/runs`
  until fixed in the Megatron/FSDP/MoE launchers (6.15 correction). Log readability was reviewed this round by
  self-review only, not genuinely fresh-eyes (6.18) — see DESIGN_NOTES.md 6.19 for the precise, non-sweeping list of
  what remains untested.
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
- **CPU pinning**, validated on both platforms at 16 GPUs and now at 48 GPUs (Slurm), with a genuinely interleaved
  re-measurement superseding the original two-block result. **Pinned vs. unpinned (Inspector on in both arms),
  healthy: pinning saves 8.02% (77.84ms vs. 84.09ms, Welch t=-26.58, n=420/420, 3 truly-interleaved pairs, each
  individually significant: +9.34%/+4.53%/+10.30% — confirms a real effect, not a block-order artifact). Under a
  100ms/rank-8 fault: pinning still saves 0.59% (t=-12.49, n=420/420) — small because the fault's own ~390ms/iter
  penalty dominates. Separately: Inspector-with-full-pinning vs. no Inspector at all (the total real-world cost
  question): -0.58% (pinned is marginally faster, t=-2.32, p~0.02, n=420/420) — not practically distinguishable
  from zero overhead at this scale.** Full dedication verified via real `/proc` affinity checks on 48/48 ranks
  across all 6 nodes (`[PIN-VERIFY] verdict=FULLY_DEDICATED ranks=8/8` on every node) and via `INSPECTOR=off`
  genuinely disabling the plugin (`[PIN-PREFLIGHT] mode=off`, no profiler-plugin load). See DESIGN_NOTES.md 6.7.3
  for the full table. Not sampled at this scale: per-core CPU utilization (the 1-core/role sufficiency confirmation
  still rests on the 16-GPU-scale sampling plus 48-GPU `/proc` affinity checks, not a fresh utilization sample).
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
- **RDMA/NCCL crash risk (2026-11 review):** this cluster uses and actively loads the same HPC-X SHARP/IB plugin
  implicated in a Kubernetes-side `ncclIbDeregMr` crash, and routinely creates the concurrent-communicator precondition
  that triggers it (every TP>1/PP>1 job). A direct, realistically-shaped stress test against the live plugin passed
  clean on real hardware; this cluster is not currently exhibiting that crash, on the evidence gathered so far. A new,
  general-purpose startup self-check (`self-checks/net_selfcheck.sh`) now verifies this on every real job and falls
  back to a safer config if it ever isn't. Also added: a rank-identity-collision self-check and an explicit DCGM
  diagnostic-path startup log. Live-validated at this project's largest shape yet (48 GPUs/6 nodes): 1 healthy + 1
  fault trial (100 ms, rank 20) -- the fault was correctly named, `confidence=PROBABLE severity=LOG-ONLY`. Full
  writeup, including a real, pre-existing cross-job-ID mislabeling bug this review's own new check surfaced (not
  introduced by it, not fixed -- out of scope): DESIGN_NOTES.md §6.8.
- **K8s — node counts > 2**: validated on 2 nodes (16 GPUs) only. Tier structure at 4+ nodes is completely
  untested. Do not claim production readiness for K8s without validating at the target node count.
- **K8s — FSDP detection requires NCCL >= 2.26 and RDMA**: NCCL 2.25.1 (system default in pytorch:25.01-py3)
  only supports profiler_v1/v2; Inspector exports only profiler_v5. No dumps → no detection. Build NCCL 2.32.3
  via `deploy.sh --build-nccl`. Do NOT set NCCL_IB_DISABLE=1 for FSDP (single process group, no segfault risk).
  With RDMA and NCCL 2.32.3: detected at rank=1,5,9 (mm=27-979×, rank-position-agnostic). See DESIGN_NOTES §6.14.
- **K8s — Megatron TP/PP training**: resolved. `NCCL_NET_PLUGIN=/nonexistent` + PyTorch 2.6 `device_id` fix.
  TORCHRUN_EXIT: 0 on 6 trials. See DESIGN_NOTES §6.9.1.
- **K8s — Megatron TP/PP straggler detection**: SHAPE-DEPENDENT, not a 2-node structural limit.
  **TP=4/PP=1/DP=4 works** (4/4 fault trials detected, 0 mean_tiered false positives) — all gradient-sync
  groups have role_n=4. Shapes where any gradient-sync group is 2-member (e.g. TP=2/PP=2/DP=4) remain
  undetectable (z≈0.707 invariant for 2-member groups). See DESIGN_NOTES §6.25.
- **K8s — tiered check on Megatron DP**: sleep-before-backward injection does not create a detectable fast-
  straggler signal on large Megatron gradient AllReduces (DDP async overlap). The signal appears on small
  post-backward collectives (< 512 B, below the floor); `_check_mean` detects it. See DESIGN_NOTES §6.9.2.
- **K8s — DCGM**: SSH-based cause classification unavailable in pods. All K8s alerts are UNCONFIRMED.
- **<3-member communicator groups (Slurm and K8s, unified finding)**: a confirmed, permanent detection gap, not
  fixable within this design. TP needs ≥3, DP needs ≥3; PP's Send/Recv is always exactly 2 members at any depth,
  by construction. See DESIGN_NOTES §6.21.
- **MoE (Slurm, 6-node/48-GPU)**: a real, escalation-worthy, intermittent (~1-in-5) collective-participation hang
  on healthy runs, confirmed as an infrastructure issue independent of this detector, not yet root-caused. See
  DESIGN_NOTES §6.22.
- **FSDP (Slurm, 6-node/48-GPU)**: now resolved with a real, evidenced PASS, not open. Detects correctly via
  the `[WAIT-INDUCED-ALERT]` path (`confidence=PROBABLE severity=LOG-ONLY`) for all 3 tested rank positions
  (1, 5, 9; 100 ms fault, large AllGather/ReduceScatter bucket), 0/1 false positives on a healthy control.
  Requires at least 3 prior job launches at the exact same large-message-size communicator shape before the
  wait-induced/role-baseline checks have enough cross-job history to fire — a real, newly-documented "warm-up"
  property for any brand-new large-scale shape's first few runs (not a bug; `_check_mean`/`_check_mean_tiered`
  are confirmed structurally unable to catch this inverse-direction signature at all, by design). This shape is
  now warmed up from this round's own trials. See DESIGN_NOTES §6.31.2.
- **Log readability**: `confidence=`/`severity=` alone cannot distinguish a repeated, consistent finding from a
  one-off (both read `PROBABLE`/`LOG-ONLY` for every synthetic fault this project has ever injected, since
  `CONFIRMED` requires real hardware evidence a synthetic fault correctly never produces). Three rounds of
  independent review across §6.23/§6.25/§6.26: round 1's `pattern=SINGLE/REPEATED(n=N)` field did not resolve the
  core complaint (every alert still reads identically); round 2 added volume/burst classification
  (`burst=ISOLATED`/`BURST(n=N,window=30s)`), fixed the `worst_host`/`participation=1.00` contradiction, `pid=`
  naming, and three layers of duplicate-line fixes; round 3 (a genuinely independent, zero-context review of the
  complete, unedited output) confirmed these are real, verified improvements for a human actively triaging, but
  returned a plain **"I would not page off this alone"** — nothing in this pipeline reaches a tier an on-call
  engineer should act on unattended, which is the known-beta-limitation #1 above, not a presentation defect. See
  DESIGN_NOTES §6.26.

**Final, consolidated, honest state (Slurm side): DESIGN_NOTES §6.24.**

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
