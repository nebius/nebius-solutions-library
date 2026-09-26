# Fault-injection workload shapes (15 validated)

Each subdirectory is one real, independently-validated workload shape
used to fault-inject and confirm this pipeline's detection coverage. See
`../INVENTORY.md` Category F for the full audit (data dependency, launch
parameters) behind this list.

Shared resources, not duplicated per-shape:

- **`nanogpt-base/`** — `model.py` + `configurator.py` + `LICENSE`
  (MIT, Andrej Karpathy's nanoGPT) + the `train_shakespeare_char.py`
  config. `nanogpt/`, `tp2/`, and `fsdp/` each carry only their own
  *patched* `train.py`/`train_fsdp.py` — the underlying model definition
  is identical across all three (confirmed via direct diff) and lives
  here once. `tp4/` additionally shares `tp2/train.py` and
  `tp2/train_node_tp.sh` outright (same script, launched with
  `TP_SIZE=4` instead of `2`).
- **`shared-data/shakespeare_char/`** — the pre-built `train.bin`/
  `val.bin`/`meta.pkl` (~2.2MB) used by nanogpt/tp2/tp4/fsdp/moe, vendored
  directly so a new, potentially air-gapped cluster doesn't need internet
  egress to regenerate them. `input.txt`/`prepare.py`/`readme.md` are
  included too, for provenance and in case regeneration is ever needed.

**Known gap, not yet fixed here** (flagged in the top-level README's
"known limitations" section): `rl/train_rl.py` does not use the shared
copies above — it has its own hardcoded absolute `sys.path.insert(...)`
and `DATA_DIR` pointing into the *original* development host's directory
layout. It happens to need the exact same base model and dataset as
nanogpt/tp2/fsdp, just not wired to the shared copies in this package yet.

Every shape's own `run_*.sh` (and `train_node_*.sh` companion, where
present) still hardcodes this project's original 2-node/8-GPU-per-node
cluster shape (`worker-0`/`worker-1`, `--nodes=2 --gpus-per-node=8`) —
this is the single largest item flagged for the next-stage
`install.sh`/`run.sh` work, not something silently glossed over here.
