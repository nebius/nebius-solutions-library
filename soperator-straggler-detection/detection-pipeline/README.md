# GPU straggler / fail-slow detection pipeline (V1 Beta)

Real-time detection of straggling or slowing GPUs on a Soperator (Slurm-on-Kubernetes) cluster. NCCL's Inspector
profiler plugin writes per-collective timing from every rank; per-node aggregators turn it into metrics in
VictoriaMetrics; `alert_engine.py` compares ranks against their peers and prints `[ALERT]` lines (and Grafana panels)
naming the host, GPU and process. Pure-Python pipeline, no third-party packages.

**Platform**: This pipeline runs on **two platforms** — Slurm/soperator (original) and **plain Kubernetes** (K8s, added
2026-11). The detection code is shared; only the launcher and topology-discovery layer differ. Both platforms verified
on 2-node 16-GPU H200 (see Known limitations for shapes not yet tested on K8s).

---

## Quickstart — Platform: Kubernetes (plain K8s)

> **Kubernetes verification status** (commit `691c296` + this task, 2026-11):
> - **nanoGPT 85M (DDP)**: tiered mean check fires 3/3 fault trials, zero false positives 3/3 healthy. ✓
> - **Megatron DP (TP=1/PP=1)**: existing `_check_mean` detects straggler signal (8-byte post-backward AllReduces);
>   tiered check correctly silent (small bucket below 512-byte floor). 3/3 healthy trials clean. ✓
> - **Megatron TP/PP (TP>1 or PP>1)**: BLOCKED by HPC-X NCCL multi-communicator segfault in pytorch:25.01-py3.
>   Unresolved. See DESIGN_NOTES §6.6.1.
> - **Node counts > 2**: NOT TESTED. Tier structure at 4+ nodes is an unknown.

**Prerequisites** (cluster-agnostic; substitute your cluster's values where shown):
- `kubectl` >= 1.27 configured to your cluster
- At least 2 GPU nodes schedulable (not exclusively reserved for other workloads)
- A `ReadWriteMany` StorageClass (shown as `<RWX_STORAGECLASS>` below; `nfs` and similar work)
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

4. **Megatron TP/PP run** (requires resolving HPC-X NCCL incompatibility first — see Known Limitations)
   ```bash
   source cluster_k8s.env
   k8s/run_megatron_k8s.sh 100   # TP=2/PP=2/DP=4, tiny model, 100 steps
   # NOTE: fails on pytorch:25.01-py3 due to HPC-X multi-communicator segfault. Use a container
   # without HPC-X, or TP=1/PP=1 (Megatron DP), until this is resolved. See DESIGN_NOTES §6.6.1.
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
| GPUs | 8x NVIDIA H200 per node, driver `580.159.04`. 2 nodes (16 GPUs) tested in the latest audit |
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

- **Scale:** the latest audit validated 16 GPUs (2 nodes) on Slurm and 16 GPUs (2 nodes) on K8s. 48 GPUs / 6 nodes is the next step.
- **CPU pinning is new code**, validated on both platforms at 16 GPUs. Overhead (Slurm): full pinning +0.64%,
  dump-only +6.4%, unpinned +11.5%. Overhead (K8s, nanoGPT shape): `mfu=76.77%` full, `mfu=75.32%` dump.
  Untested: single-NUMA, 4-GPU nodes, SMT off, ARM, small CPU masks.
- **Tiered mean check (`_check_mean_tiered`), Slurm status:** verified on one shape only (2 nodes / 16 GPUs, Megatron
  TP4/PP2/DP2, 100 ms fault on rank 8, 3 healthy + 3 fault trials on the final code). Healthy: 0 tiered incidents and 0
  `CHECK-FAILED`. Fault: true rank named in 3/3 at 167, 167 and 169 s (wait-induced path: 65, 55, 54 s; one earlier trial
  took 223 s), each with a complete `[ALERT]` that is `UNCONFIRMED` / `LOG-ONLY` (does not page). Live testing found and
  fixed three bugs (NameError, persistence state shared across groups, classifier KeyError) and added a minimum message
  size of 512 B (`TIERED_MIN_BUCKET_BYTES`) because healthy runs false-fired on 4 B and 99 B collectives. That floor rests on
  one shape and 6 trials, is not a general constant, and leaves collectives below 512 B uncovered. Untested: other node
  counts, other communicator layouts, other fault sizes or ranks. Details: DESIGN_NOTES.md 6.5.
- **Noise path:** the main-path `mean` check is gated off for 4-member communicators (`MEAN_CHECK_GATED_ROLE_N=4`; set it to `""` to restore). It produced about 64% of healthy-run alerts on the TP4/PP2/DP2 shape and never named the faulted rank. After the gate, 2 healthy runs went from 18 and 24 total alerts to 9 and 6 (mean-check alerts 12 and 16 down to 0, no PAGE), and 3 fault runs still named the true rank in 51, 50 and 56 s. The criterion comes from one shape, so a 4-member communicator in another job is gated too. Remaining LOG-ONLY noise is from other paths.
- **Memory growth:** aggregators about +5 to +7 MB/hour and the alert engine about +4 MB/hour over a 107-minute soak;
  cause not found. Restart them between very long campaigns.
- **Unexplained:** fault runs show fewer alerts than healthy runs on the TP4/PP2/DP2 shape.
- **Attribution:** in 2 of 9 fault trials the wait-induced check also named the mirror-position replica rank
  (LOG-ONLY). `rank=` in alerts is an OS PID, not the training rank.
- **DCGM clock fallback** now needs a sustained signal before it pages, so a real sustained clock fault pages
  about 10-60 s later than before.
- Only `PAGE` alerts page a human; `LOG-ONLY` alerts are recorded for the dashboard and logs.
- **K8s — node counts > 2**: validated on 2 nodes (16 GPUs) only. Tier structure at 4+ nodes is completely
  untested. Do not claim production readiness for K8s without validating at the target node count.
- **K8s — Megatron TP/PP**: HPC-X NCCL multi-communicator segfault in pytorch:25.01-py3 blocks testing.
  Any TP>1 or PP>1 shape fails. Use a container without HPC-X. See DESIGN_NOTES §6.6.1.
- **K8s — tiered check on Megatron**: sleep-before-backward injection does not create a detectable fast-
  straggler signal on large Megatron gradient AllReduces (DDP async overlap). The signal appears on small
  post-backward collectives (< 512 B, below the floor); `_check_mean` detects it. See DESIGN_NOTES §6.6.2.
- **K8s — DCGM**: SSH-based cause classification unavailable in pods. All K8s alerts are UNCONFIRMED.

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
