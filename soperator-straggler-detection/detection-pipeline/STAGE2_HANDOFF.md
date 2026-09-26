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
  now a **triple**-hardcoded workload, not double: its companion
  `workloads/rl/train_node_rl.sh` (found missing entirely and added in
  this session's second adversarial pass) also `cd`s into
  `/root/P31_rl` and separately sets its own
  `LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:...` (see #9 below —
  this corrects an earlier claim that long-context was the *only*
  workload doing this).
- `host-fault-injection/train_node.sh`: `cd /root/P4b_jitter/nanogpt_inject`
- `workloads/moe/rank_fault_wrapper.sh`: `/root/P23_moe/qp_rate_limit_shim.so`
  and `/root/P23_moe/train_moe.py` (found in this session's first
  adversarial pass)
- `workloads/tp4/run_tp4_nanogpt.sh`: `bash /root/P21_multicomm/train_node_tp.sh`
  — an absolute path to the *original* directory rather than this
  package's own co-located `../tp2/train_node_tp.sh` (found in this
  session's second adversarial pass; TP4's own sharing-with-TP2 claim is
  otherwise confirmed correct — same script, just `TP_SIZE=4`).
- `workloads/nanogpt/run_shape1_nanogpt.sh` (added this session):
  `STRAGGLER_TRIGGER_FILE=/root/_v1beta_dryrun/trigger.json` and `bash
  /root/_v1beta_dryrun/train_node_shape1.sh` — same category of
  hardcoded absolute path as everything else here, inherited from the
  original V1 Beta dry-run scripts this file was copied from unmodified.
- `moe-two-stage-detector/telemetry_check.py` (found missing entirely,
  added in the third completeness pass): `sys.path.insert(0,
  "/root/P18k_classifier")`.
- `moe-two-stage-detector/run_pairwise_sweep.py` (same pass):
  `sys.path.insert(0, "/root/P30_moe_detector")` and hardcodes
  `SCRIPT = "/root/P30_moe_detector/pairwise_sweep.py"`.
- `inspector-crash-repro/run_repro_node.sh` / `run_repro_v2_node.sh` /
  `run_repro_v3_node.sh` (found missing entirely, added in the third
  completeness pass): all three `cd /root/P32_inspector_repro` and set
  their own `LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:...` — see #9
  below, a third instance of this pattern.
- `workloads/nanogpt-longrun/train_node_nanogpt_longrun.sh` (found
  missing entirely, added in the third completeness pass): `cd
  /root/P3_real_workload/nanoGPT` — a fourth distinct hardcoded absolute
  directory this package's various nanoGPT-family scripts collectively
  reference (alongside the P20d/P21_multicomm/P4b_jitter ones already
  listed above).
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

## 6. RL workload — the worst case, now confirmed triple-hardcoded, not
double

`workloads/rl/train_rl.py` and its companion `train_node_rl.sh` (found
missing entirely and added in this session's second adversarial pass) do
**not** use the package's shared `workloads/nanogpt-base/` or
`workloads/shared-data/` conventions, even though it needs the exact same
base model and dataset every other nanoGPT-family shape uses (confirmed
via direct diff — its model is byte-identical to the shared copy):

- `sys.path.insert(0, "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler")`
  then `from model import GPTConfig, GPT` — imports the model from a
  hardcoded absolute path into a *different* project directory instead of
  the co-located, package-relative `../nanogpt-base/model.py`.
- `DATA_DIR = "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler/data/shakespeare_char"`
  — a second, independent hardcoded absolute path, instead of the
  package-relative `../shared-data/shakespeare_char/`.
- `train_node_rl.sh` **additionally** `cd`s into `/root/P31_rl` and sets
  its own `export LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:...` — a
  third, independent hardcoded absolute path, and the second workload
  (after long-context) to force-link against the Inspector-build NCCL
  tree rather than relying on the host-bind-mount-shadowing behavior —
  see #9 below.
- **Proposed minimal, generic fix** (planning only, not implemented here):
  replace the `sys.path.insert(...)` + `from model import ...` with a
  relative import resolved from `train_rl.py`'s own location (e.g.
  `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
  "nanogpt-base"))`), and replace the literal `DATA_DIR` string and
  `train_node_rl.sh`'s `cd` target with the same pattern pointed at
  `../shared-data/shakespeare_char` and the RL directory itself,
  respectively — mechanical changes once the package's real relative
  layout is the reference, not a new mechanism to design. Note this fix
  is now the same shape needed for #10 below (nanogpt/tp2/fsdp's own
  model-import duplication) — worth doing as one pass across all 4
  shapes in Stage 2, not 4 separate ones.

## 7. MoE's RDMA fault shim — also hardcoded, also needs a build step

`workloads/moe/rank_fault_wrapper.sh` hardcodes `/root/P23_moe/` twice
(already listed in #4). Additionally, unlike every other workload,
`qp_rate_limit_shim.c` (a real RDMA-layer fault mechanism found missing
from the first pass) needs to be **compiled** before use — see
`workloads/moe/README.md` for the reconstructed build command and its new
`libibverbs-dev` external dependency. install.sh's build step needs to
cover this alongside the Inspector plugin's own from-source build, not
just assume it's already compiled.

## 8. `classifier/detection.py`'s `score_node_scoped_per_node()` — a real,
LIVE, Python-level hardcoded 2-node/8-GPU assumption (found in this
session's second adversarial pass — the most significant single finding
of this pass, since it's silent, not a loud shell-script crash)

```python
NODE_A = set(range(0, 8))
NODE_B = set(range(8, 16))
...
def score_node_scoped_per_node(per_rank, stat_name):
    return {
        "worker-0": score_node_scoped({r: v for r, v in per_rank.items() if r in NODE_A}, stat_name),
        "worker-1": score_node_scoped({r: v for r, v in per_rank.items() if r in NODE_B}, stat_name),
        ...
```

This function is **not dead code** — confirmed called unconditionally,
for every stat, on every real per-communicator scoring pass, from
`_score_one_comm_result()` (`classifier/detection.py:719-720`), which is
itself the core of the classification path. Its own docstring already
self-discloses this exact gap ("still a real, separate, disclosed
cluster-shape assumption at THAT call site") — a sibling function
(`score_node_scoped`'s own peer-selection logic) was already fixed this
project's own history to derive peers from the real per-rank keys it was
actually given instead of an assumed range, but this per-node variant was
not carried along with that fix.

**Why this is worse than the shell-script hardcoding**: a hardcoded
`worker-0`/`worker-1` string in a launch script fails loudly (the third
node never gets scheduled, or gets the wrong rank, and the job visibly
breaks). This function, on a cluster with different node names, a
different rank-to-node split, or more than 2 nodes, does **not** crash —
it silently attributes every rank ≥8 to a literal string key `"worker-1"`
regardless of that rank's real hostname, and silently drops/misgroups
ranks beyond 0-15 entirely. A real fault on a real third node would be
attributed to whatever node happens to hold rank 8-15, or vanish from the
per-node view outright, with no error raised anywhere.

**Proposed generic fix** (planning only): `_score_one_comm_result()`
already receives `rank_hosts` (the real, live rank→hostname mapping,
already plumbed through from `_comm_cross_node_members`-style discovery
elsewhere in this codebase) as its own parameter but does not pass it
into `score_node_scoped_per_node()`. The minimal fix is to thread
`rank_hosts` through and group by the real, discovered hostnames it
already contains, exactly the same fix pattern already proven correct in
`score_node_scoped`'s own sibling peer-selection logic — not a new
mechanism to design, just extending an already-established one to this
one remaining call site.

**Related, lower-severity finding from the same targeted grep**:
`alerting/ras_alert.py`'s own functions (`query_ras_snapshot`,
`compute_current_exclusions`, `RASFailStopWatcher.__init__`) all default
`host="worker-0"` / `hosts=("worker-0", "worker-1")`, and
`alerting/health_exclusions.py`'s `degraded_gpus_live(hosts=("worker-0",
"worker-1"), ...)` is only ever called (from `ras_alert.py`) with that
same default passed straight through. Lower severity than the
`detection.py` finding above because `ras_alert.py` is already documented
(top README) as a standalone tool not auto-wired into the production
alert loop — but if it's ever wired in or run manually on a differently-
shaped cluster, these defaults need the same real-hostname-discovery
treatment, not silent reliance on 2 literal strings.

## 9. `LD_LIBRARY_PATH` is now confirmed set by 2 of 15 workloads plus one
diagnostic tool, not 1 — a real, disclosed version inconsistency, not a
single outlier

`workloads/long-context/run_longctx_node.sh` and
`workloads/rl/train_node_rl.sh` both set
`LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:${LD_LIBRARY_PATH:-}`,
forcing those two shapes to link against the Inspector-plugin build
tree's own NCCL 2.28.9, while the other 13 shapes rely on the host-bind-
mount-shadowing behavior documented in INVENTORY.md's Inspector-plugin-
provenance section (which lands on NCCL 2.30.1 instead). **Found in the
third completeness pass**: `inspector-crash-repro/`'s three
`run_repro*_node.sh` scripts do the same — makes sense for that one
specifically, since the whole point of that harness is reproducing a bug
against the exact NCCL build the Inspector plugin is compiled against,
not an accident to fix. **Stage 2 needs to decide, deliberately, whether
every *workload* (not the repro harness, which has a real reason) should
standardize on one NCCL version or whether this per-shape difference is
intentional and should be preserved** — right now it's an accident of
which shapes happened to get this line added and which didn't, not a
documented
design decision.

## 10. `model.py`/`configurator.py` are now duplicated 4 ways
(`nanogpt-base/`, `nanogpt/`, `tp2/`, `fsdp/`) — real Stage 2 cleanup
work, not a mistake

See `workloads/README.md` for the full story: `nanogpt/train.py`,
`tp2/train.py`, and `fsdp/train_fsdp.py` each do a bare `from model
import GPTConfig, GPT` (a script-directory-relative import) rather than a
`sys.path.insert(...)` reaching into `nanogpt-base/` — this only "worked"
implicitly on the original host because `model.py` physically sat next
to each `train.py` there. Splitting them into this package's separate
`nanogpt-base/` directory broke that implicit resolution (a real,
load-bearing gap, found and fixed in this session's second adversarial
pass by giving each shape its own local copy). The 4-way duplication this
created should be collapsed back to one canonical copy in Stage 2 by
converting all 4 (including RL, per #6 above) to genuine
`sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
"nanogpt-base"))`-style relative imports — one mechanical pass, not 4
separate ones.

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
- **Correction**: the Python core is *not* uniformly dynamic — see #8
  above (`score_node_scoped_per_node()`), a real, live exception found in
  this session's second adversarial pass. Don't assume the rest of
  `classifier/`/`aggregator/`/`alerting/` is clean by extension; this
  session traced ~13 more historical scenarios and found this one by
  tracing PP's cross-node fix, not by auditing the Python core directly
  end-to-end — a full line-by-line audit of every function in
  `detection.py`/`classifier.py` for similar patterns has still not been
  done.
