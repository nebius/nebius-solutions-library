# Stage 2 handoff — every hardcoded-cluster-shape assumption, in one list

This is the complete, final input list for `install.sh`/`run.sh`'s
node-discovery mechanism, compiled across both the original packaging
pass and this session's adversarial completeness follow-up. Nothing here
is fixed in this session — per its own scope, this is inventory/planning
only. Each item names the real file(s), quotes the real hardcoded value,
and proposes (not implements) a generic replacement.

## 1. Every workload launch script hardcodes the 2-node/8-GPU topology

**Affected**: all `workloads/*/run_*.sh` (15 shapes) plus
`host-fault-injection/run_host_injection.sh` — 16 scripts total.

- `srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w
  worker-0,worker-1` (or the `torchrun --nnodes=2 --nproc_per_node=8`
  equivalent) appears verbatim in every one.
- **Proposed fix**: derive node count/list from the real Slurm
  allocation at launch time — `scontrol show hostnames
  "$SLURM_JOB_NODELIST"` (or, if launched via a wrapper before `srun`,
  accept the target partition/node count as install.sh parameters and
  interpolate them into the `srun` call rather than hardcoding). GPU count
  per node should use the same live-discovery pattern already proven
  correct elsewhere in this codebase: `cause_metrics.py`'s
  `discover_gpu_count(host)` (`nvidia-smi -L | wc -l`) — generalize that
  same call into the shell layer (e.g. `nvidia-smi -L | wc -l` inline in
  each `run_*.sh`, or precomputed once by install.sh and passed down as an
  env var/argument).

## 2. Every `train_node_*.sh` hardcodes a literal 2-way hostname branch

**Affected**: all `workloads/*/train_node_*.sh` companions (present for
nanogpt, resnet, tp2, fsdp, moe ×2, vit, tp-inference) plus
`host-fault-injection/train_node.sh`.

- `if [ "$(hostname)" = "worker-0" ]; then RANK=0; else RANK=1; fi` — a
  third node would silently get `RANK=1` (wrong), with no path to
  `RANK=2+`.
- **Proposed fix**: derive `RANK` from the node's actual position in the
  live Slurm node list (`scontrol show hostnames "$SLURM_JOB_NODELIST"`,
  index of `$(hostname)` within it) rather than a hardcoded string
  comparison — genuinely N-node-safe, not just 2-node-safe.

## 3. Every rendezvous endpoint hardcodes `worker-0` as the literal string

**Affected**: same script set as #2, e.g. `--rdzv_endpoint=worker-0:$PORT`.

- **Proposed fix**: use the first entry of the same live node list from
  #2 (`scontrol show hostnames ... | head -1`) instead of a literal
  `worker-0`.

## 4. Hardcoded absolute `sys.path.insert(...)` imports (not cluster-shape,
but install-location — same category of "won't work off this exact
host")

- `alerting/alert_engine.py`: `/root/P18k_classifier`, `/root/P20c_alerting`
- `aggregator/node_aggregator_ref.py`: `/root/P19a_metrics`,
  `/root/P18k_classifier`
- `workloads/rl/train_rl.py`: `/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler`
  (model import) **and** its `DATA_DIR` (dataset path) — see #6 below,
  the only workload with two separate hardcoded absolute paths.
- `host-fault-injection/train_node.sh`: `cd /root/P4b_jitter/nanogpt_inject`
- `workloads/moe/rank_fault_wrapper.sh`: `/root/P23_moe/qp_rate_limit_shim.so`
  and `/root/P23_moe/train_moe.py` (found in this session's adversarial
  pass)
- **Proposed fix**: convert to relative/package-style imports resolved
  from the installed package root (e.g. an installer-set `PYTHONPATH`
  pointing at `detection-pipeline/`, with each `sys.path.insert` replaced
  by a path relative to `__file__`), or a single install-time
  path-substitution step that rewrites these to wherever the package
  actually lands.

## 5. Grafana provisioning hardcodes both a datasource URL and a filesystem
path

- `observability/dashboards/provisioning/datasources/local.yaml`: `url:
  http://worker-0:8428` — the real VictoriaMetrics host for *this*
  cluster specifically.
- `observability/dashboards/provisioning/dashboards/local.yaml`: `path:
  /root/P20g_pr_ready/dashboards_dropin` — an absolute filesystem path on
  the original development host's Grafana instance, found in this
  session's Step 1 re-check.
- **Proposed fix**: both need to become install-time template
  substitutions — the VM host from wherever `vm-standalone/` actually
  gets launched (see its own README), and the dashboard-drop path from
  wherever install.sh actually places
  `observability/dashboards/straggler_detection_metrics.json` on the
  target Grafana instance.

## 6. RL workload — the worst-case, doubly-hardcoded example

`workloads/rl/train_rl.py` is the one workload that does **not** use the
package's shared `workloads/nanogpt-base/` or `workloads/shared-data/`
conventions, even though it needs the exact same base model and dataset
every other nanoGPT-family shape uses (confirmed via direct diff earlier
this session — its model is byte-identical to the shared copy):

- `sys.path.insert(0, "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler")`
  then `from model import GPTConfig, GPT` — imports the model from a
  hardcoded absolute path into a *different* project directory instead of
  the co-located, package-relative `../nanogpt-base/model.py`.
- `DATA_DIR = "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler/data/shakespeare_char"`
  — a second, independent hardcoded absolute path, instead of the
  package-relative `../shared-data/shakespeare_char/` every other shape
  effectively uses (via a symlink on the original host for fsdp/moe, or
  direct reference for nanogpt/tp2).
- **Proposed minimal, generic fix** (planning only, not implemented here):
  replace the `sys.path.insert(...)` + `from model import ...` with a
  relative import resolved from `train_rl.py`'s own location (e.g.
  `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
  "nanogpt-base"))`), and replace the literal `DATA_DIR` string with the
  same pattern pointed at `../shared-data/shakespeare_char` — both
  one-line, purely mechanical changes once the package's real relative
  layout (already established by every other shape) is the reference,
  not a new mechanism to design.

## 7. MoE's RDMA fault shim — also hardcoded, also needs a build step

`workloads/moe/rank_fault_wrapper.sh` hardcodes `/root/P23_moe/` twice
(already listed in #4). Additionally, unlike every other workload,
`qp_rate_limit_shim.c` (a real RDMA-layer fault mechanism found missing
from the first pass) needs to be **compiled** before use — see
`workloads/moe/README.md` for the reconstructed build command and its new
`libibverbs-dev` external dependency. install.sh's build step needs to
cover this alongside the Inspector plugin's own from-source build, not
just assume it's already compiled.

## Everything already confirmed dynamic (do not "fix" these — they're
already correct)

- `aggregator/node_aggregator_ref.py`'s `--node choices=["A","B"]` CLI
  flag is confirmed vestigial by its own docstring and never actually
  used internally — no action needed.
- `alerting/alert_engine.py`'s `AlertEngine.__init__(hostnames=None)`
  already means live dynamic discovery, and the real production launch
  command never overrides it — no action needed.
- `classifier/cause_metrics.py`'s `discover_gpu_count(host)` already does
  real live discovery via `nvidia-smi -L` — this is the pattern to
  *generalize into* the shell layer (see #1), not something to change
  itself.
