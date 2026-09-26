# Host-level CPU contention fault injection

A real, validated host-CPU-contention fault mechanism, distinct from the
GPU-compute (`STRAGGLER_SLEEP_MS`) and disk-io (`storage-ebpf/`) faults
used elsewhere in this package. This is what this project's own
"aggregator CPU overhead under contention" validation work explicitly
reuses ("the existing all-core CPU burner pattern... reuse it exactly as
the real CPU-saturation source, don't build a new synthetic benchmark") —
found missing from the first packaging pass and added in this session's
adversarial completeness check.

- **`cpu_burn.sh`** — an all-core, 5s-on/5s-off duty-cycle CPU burner, run
  directly on a host via SSH (outside any container). Genuinely saturates
  the host (confirmed 97%+ CPU, `mpstat`-verified in this project's own
  validation runs) — used to test whether the aggregator's own CPU usage
  competes with, or is affected by, real CPU-bound contention on the same
  node it runs on.
- **`run_host_injection.sh`** — launches a real 2-node training job via
  `train_node.sh` and, in `cpu_inject` mode, starts `cpu_burn.sh` on
  `worker-0` partway through and stops it before the job ends. Hardcoded
  to this project's own 2-node/8-GPU cluster shape exactly like the 15
  `workloads/` scripts — see the top-level README's "known limitations"
  list.
- **`train_node.sh`** — the training driver this scenario uses. Note: it
  `cd`s into a **hardcoded absolute path**
  (`/root/P4b_jitter/nanogpt_inject`, the original development host's
  layout) to run `train_inject.py` — a portability issue on top of the
  usual 2-node hardcoding, flagged for Stage 2.
- **`train_inject.py`** — a nanoGPT variant with its own fine-grained,
  Python-level periodic jitter injection (`INJECT_ENABLE`/`INJECT_RANK`/
  `INJECT_BURST_MS`/`INJECT_PERIOD_MS`), separate from and complementary
  to the coarser `cpu_burn.sh` host-level saturation. Shares the exact
  same underlying nanoGPT `model.py`/`configurator.py` as
  `../workloads/nanogpt-base/` (confirmed via direct diff — byte-identical)
  — not duplicated here; point this script at `../workloads/nanogpt-base/`
  once the hardcoded `cd` above is fixed in Stage 2.
