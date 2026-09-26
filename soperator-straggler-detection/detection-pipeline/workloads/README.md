# Fault-injection workload shapes (15 validated)

Each subdirectory is one real, independently-validated workload shape
used to fault-inject and confirm this pipeline's detection coverage. See
`../INVENTORY.md` Category F for the full audit (data dependency, launch
parameters) behind this list.

- **`nanogpt-base/`** — `model.py` + `configurator.py` + `LICENSE`
  (MIT, Andrej Karpathy's nanoGPT) + the `train_shakespeare_char.py`
  config, kept as the **canonical reference copy** for Stage 2 to
  deduplicate against.
  **Correction (found in this session's second adversarial pass — the
  first pass's claim below was wrong):** `nanogpt/`, `tp2/`, and `fsdp/`
  do **not** actually share this directory at runtime. Their
  `train.py`/`train_fsdp.py` each do a bare `from model import
  GPTConfig, GPT` (a cwd/script-directory-relative import, not a
  `sys.path.insert(...)` reaching into `nanogpt-base/`) — on the
  original host this "just worked" because `model.py` physically sat
  next to `train.py` in the same directory; once split into this
  package's separate `nanogpt-base/` directory, that import would have
  **failed outright** (a real, load-bearing gap, same severity class as
  MoE's missing files from the first adversarial pass). Fixed by giving
  `nanogpt/`, `tp2/`, and `fsdp/` each their own local copy of
  `model.py`/`configurator.py` (confirmed byte-identical to
  `nanogpt-base/`'s copy via direct diff) rather than trying to rewire
  the import mechanism in this inventory-only session. **This means the
  model is currently duplicated 4 ways** (`nanogpt-base/`, `nanogpt/`,
  `tp2/`, `fsdp/`) — a real, disclosed piece of Stage 2 cleanup work
  (convert to a genuine shared relative import), not a mistake to leave
  silently unexplained. `tp4/` shares `tp2/train.py` and
  `tp2/train_node_tp.sh` outright (same script, launched with
  `TP_SIZE=4` instead of `2`) — this one genuinely works as claimed,
  confirmed by reading `run_tp4_nanogpt.sh` directly.
- **`shared-data/shakespeare_char/`** — the pre-built `train.bin`/
  `val.bin`/`meta.pkl` (~2.2MB) used by nanogpt/tp2/tp4/fsdp/moe, vendored
  directly so a new, potentially air-gapped cluster doesn't need internet
  egress to regenerate them. `input.txt`/`prepare.py`/`readme.md` are
  included too, for provenance and in case regeneration is ever needed.
  (This one's sharing claim was not contradicted by the deeper trace —
  each shape's dataset loading code takes a directory path as a
  constructor/config argument rather than a bare relative import, so it
  resolves correctly regardless of which directory the training script
  itself lives in.)

**Known gap, not yet fixed here** (flagged in the top-level README's
"known limitations" section and in `STAGE2_HANDOFF.md`): `rl/train_rl.py`
does not use the shared copies above — it has its own hardcoded absolute
`sys.path.insert(...)` and `DATA_DIR` pointing into the *original*
development host's directory layout, and its own `train_node_rl.sh`
`cd`s into that same original directory too. It happens to need the
exact same base model and dataset as nanogpt/tp2/fsdp, just not wired to
the shared copies in this package yet — and, unlike nanogpt/tp2/fsdp,
its hardcoded absolute path means it still actually *works* as shipped
(pointed at the original host's directory), it's just not portable.

**`nanogpt/` also gained two additional launch-script pairs this
session**: `run_shape1_nanogpt.sh`/`train_node_shape1.sh` (the more
current, GPT-2-124M-scale, `STRAGGLER_MODE=file_trigger`-demonstrating
launch convention this project's own later V1 Beta validation work
actually used) and `run_shape1_nomonitor.sh`/`train_node_shape1_nomonitor.sh`
(the matching monitoring-off counterpart, for direct A/B overhead
comparison) — found missing from the first pass, which only had the
older, Shakespeare-char-toy-scale `run_straggler_nanogpt.sh` convention.
Both conventions are now present; the older one is not removed, since
it's still a real, functional, simpler smoke-test path.

Every shape's own `run_*.sh` (and `train_node_*.sh` companion, where
present) still hardcodes this project's original 2-node/8-GPU-per-node
cluster shape (`worker-0`/`worker-1`, `--nodes=2 --gpus-per-node=8`) —
this is the single largest item flagged for the next-stage
`install.sh`/`run.sh` work, not something silently glossed over here.

**`nanogpt-longrun/` — found missing entirely in the third completeness
pass.** This is not one of the 15 fault-injection shapes above — it's the
real, dedicated launch harness (`run_nanogpt_longrun.sh` +
`train_node_nanogpt_longrun.sh`) this project used for its own sustained,
multi-hour continuous-run hardening and blind end-to-end validation (see
`../docs/straggler_dectection_history.md` Phase 5/6), distinct from every
other nanoGPT launch convention in this directory in one specific way:
its own `train.py` is a genuinely **plain, unpatched** copy (no
`STRAGGLER_*` fault-injection support at all — confirmed via direct diff:
identical to nanogpt-base's model logic, but missing every
`STRAGGLER_SLEEP_MS`/`STRAGGLER_TARGET_RANKS`/`file_trigger` addition the
other nanoGPT copies carry). This is deliberate, not stale: this harness's
whole purpose is testing sustained stability (memory leaks, log growth,
metric drift) under real, unmodified, hours-long training, not testing
fault injection. `train_node_nanogpt_longrun.sh` itself `cd`s into a
fourth hardcoded absolute path not seen elsewhere in this package
(`/root/P3_real_workload/nanoGPT`) — added to `../STAGE2_HANDOFF.md`.
