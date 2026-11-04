# Audit helper scripts (copied from the audit working directory)

These drove the 2026-11 validation runs. They contain **absolute paths from the audit host**
(`/root/inspector-audit`, `/root/nccl-2.32.3-src`, hosts `worker-0/1`) and are provided for
reproducibility of method, not as a portable tool. Edit the paths before reusing.

- `run_cfg.sh` — `run_one NAME ITERS INSPECTOR PIN_MODE NCCL(host|2323)`: waits for an empty Slurm queue, runs the 16-GPU Megatron shape, records run window.
- `rss_sampler.sh OUT SECONDS` — 60 s RSS series for alert_engine, VictoriaMetrics, aggregators.
- `run_validation_matrix.sh` — healthy ×2 + faults on ranks 2/13/8 (100/100/25 ms).
- `run_clock_throttle_test.sh TAG` — real ground-truth clock fault: `nvidia-smi -lgc 600,600` on worker-1 GPU 0 for 240 s, with an EXIT-trap `-rgc` restore and post-restore clock query. **Modifies GPU state; only run on a node you own.**
- `run_final_validation.sh` — healthy ×2, fault ×3, long soak with both DCGM false-page patches active.
- `probe_prof.py`, `run_fault_trials.sh` — earlier probes/trials.
