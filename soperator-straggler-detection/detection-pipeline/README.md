# GPU straggler / fail-slow detection pipeline (V1 Beta)

Real-time detection of straggling or slowing GPUs on a Soperator (Slurm-on-Kubernetes) cluster. NCCL's Inspector
profiler plugin writes per-collective timing from every rank; per-node aggregators turn it into metrics in
VictoriaMetrics; `alert_engine.py` compares ranks against their peers and prints `[ALERT]` lines (and Grafana panels)
naming the host, GPU and process. Pure-Python pipeline, no third-party packages.

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
   `install.sh` ends with a summary; any line starting `FAIL` must be fixed before continuing.
3. **Build NCCL >= 2.32.3 for full CPU pinning** (optional; without it the dump thread is still pinned)
   ```bash
   pinning/nccl/fetch_build_nccl.sh --dest /root/nccl-2.32.3     # a path every compute node can read
   ```
   It ends with `ncclGetVersion() = 2.32.3` and `profiler symbol: readable`. On a GPU-less login node add
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
   Takes up to ~10 minutes. Expected last line: `PASS` (it found an `[ALERT]` naming exactly the injected rank).
6. **Optional: Megatron run with Inspector and pinning** (default shape = 6 nodes / 48 GPUs; set `MEGA_*` and a
   matching `--nodes` on fewer nodes, e.g. 2 nodes = TP4/PP2)
   ```bash
   cd workloads/megatron
   export SRUN_EXTRA="--exclusive --cpus-per-task=$(nproc) --cpu-bind=none"
   ./run_megatron_tp4pp4dp3.sh 300 /root/run1 29500 /tmp/insp_dump_run1
   grep -h 'PIN-PREFLIGHT\|PIN-VERIFY' /root/run1/*.log /root/run1/pinlogs/launch_*.log
   ```
   Expected: one `[PIN-PREFLIGHT] ... resolved=full` (or `resolved=dump` on NCCL < 2.32.3) per node and
   `[PIN-VERIFY] ... verdict=FULLY_DEDICATED` (`DUMP_ONLY_DEDICATED` when resolved=dump) with `ranks=8/8`.
   Alerts appear in `var/alert_engine_supervised.log`; `python3 tools/readable_alerts.py var/alert_engine_supervised.log`
   prints them in a short form. Turn pinning off with `PIN_MODE=off`.

## Known limitations

- **Scale:** the latest audit validated 16 GPUs (2 nodes) only. 48 GPUs / 6 nodes is the next step.
- **CPU pinning is new code**, validated on one cluster, Megatron launcher only. Overhead at 16 GPUs: full pinning
  +0.64% (not significant), dump-only +6.4%, unpinned +11.5%. Untested: single-NUMA, 4-GPU nodes, SMT off, ARM, small
  Slurm CPU masks.
- **Noise path:** the main-path `mean` check is gated off for 4-member communicators (`MEAN_CHECK_GATED_ROLE_N=4`; set it to `""` to restore). It produced about 64% of healthy-run alerts on the TP4/PP2/DP2 shape and never named the faulted rank. After the gate, 2 healthy runs went from 18 and 24 total alerts to 9 and 6 (mean-check alerts 12 and 16 down to 0, no PAGE), and 3 fault runs still named the true rank in 51, 50 and 56 s. The criterion comes from one shape, so a 4-member communicator in another job is gated too. Remaining LOG-ONLY noise is from other paths.
- **Memory growth:** aggregators about +5 to +7 MB/hour and the alert engine about +4 MB/hour over a 107-minute soak;
  cause not found. Restart them between very long campaigns.
- **Unexplained:** fault runs show fewer alerts than healthy runs on the TP4/PP2/DP2 shape.
- **Attribution:** in 2 of 9 fault trials the wait-induced check also named the mirror-position replica rank
  (LOG-ONLY). `rank=` in alerts is an OS PID, not the training rank.
- **DCGM clock fallback** now needs a sustained signal before it pages, so a real sustained clock fault pages
  about 10-60 s later than before.
- Only `PAGE` alerts page a human; `LOG-ONLY` alerts are recorded for the dashboard and logs.

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
