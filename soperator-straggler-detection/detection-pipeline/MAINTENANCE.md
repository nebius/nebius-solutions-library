# Maintenance Guide

This file answers one question: **what do you need to continuously watch,
update, and re-validate to keep this pipeline working end-to-end, especially
as new versions ship?**

It is the maintenance *lens* on top of `README.md` — where a topic is already
fully documented there (architecture, install steps, troubleshooting), this
file summarizes and points at the section rather than repeating it. Every
claim below was verified directly against the current code/README at the time
this file was written (2026-10-02, originally against HEAD `d684a6d8`,
updated against `add/straggler-detection-v1-beta` after the `[VM-DOWN]`
watchdog and the two constant/doc corrections below landed) — if it's been a
while, re-grep the cited file:line before trusting it.

## If you only read one section

The five items most likely to cause a real incident if neglected, based on
this project's own history of things that actually broke:

1. **A supervisor `.sh` edit does nothing until you kill the supervisor
   process itself, not just its child.** Every one of the five long-running
   processes is a bash `while true` loop parsed once at supervisor start
   (§1). Killing the leaf Python/binary just relaunches the *same already-
   parsed* script. This has already cost real debugging time this session.
2. **Calibration constants are duplicated across files and can drift
   silently** — confirmed real: `aggregator/promql_cv_verify.py` was found
   carrying stale `PERSIST_REQUIRED=2`/`CV_Z_THRESH=20.0` against the live
   pipeline's real `PERSIST_REQUIRED=3`/`CV_Z_THRESH=60.0` (§6, now fixed).
   Whenever you change a threshold in `thresholds.py` or
   `node_aggregator_ref.py`, grep for other copies of the same constant name
   before assuming it's the only place that needed the change.
3. **Relative `DUMPDIR_BASE` silently breaks dump output** — no crash, no
   error, just silent non-detection, because self-dispatching workload
   launchers `cd` before the relative path resolves (README.md §8, line
   1333). Always pass an absolute path.
4. **Overhead is not one number.** Five different measurements exist
   (4.5x / ~6.5% / ~12.3% / ~13.1% / ~12.0%) across different workload
   shapes, scales, and pipeline states (§8). Never quote one of these as
   "the" overhead figure, and re-measure after any workload/scale/plugin
   change rather than reusing an old number.
5. **VictoriaMetrics' own two real "up but wrong" failure modes are now
   watched (`[VM-DOWN]`/`[VM-RECOVERED]`, §2) — but know their actual
   shapes if you're ever debugging around them.** A destroyed data
   directory does NOT leave VM in a long-lived degraded state like
   Grafana's SQLite quirk did — VM's own free-disk-space watcher panics the
   whole process within under a minute. The slower, more realistic failure
   is low free disk space: VM stays alive indefinitely, `/health` still
   returns 200, but every write gets a real HTTP 503 ("read-only mode").
   Both are confirmed live, not assumed from docs — see §2.

---

## 1. Standing processes & supervisors

Five supervised long-running processes, all following the identical pattern
in `observability/run_*_supervised.sh`: a bash `while true; do <exec>; sleep
3; done` loop, each with a background `logrotate`-triggering subshell if
`logrotate` is on PATH.

| Process | Supervisor | Launched by | Restart behavior |
|---|---|---|---|
| Aggregator (per node) | `run_aggregator_supervised.sh:362-370` | `run.sh:76-78` (ssh, per node) | relaunches `node_aggregator_ref.py --duration 315360000` (no native persistent mode — a 10-year fake-forever) |
| alert_engine | `run_alert_engine_supervised.sh:445-453` | `run.sh:122-124` (local) | `--duration 0` is genuine native forever |
| Grafana | `run_grafana_supervised.sh:490-497` | via `grafana-setup.sh` | — |
| VictoriaMetrics | `run_vm_supervised.sh:591-602` | via `vm-setup.sh` | fixed flags `-dedup.minScrapeInterval=0s -retentionPeriod=100y` |
| iowait_logger | `run_iowait_logger_supervised.sh:543-550` | `run.sh` | — |

**Confirmed gotcha (real, already cost time this session):** each supervisor
script's `while true` loop is parsed once when the supervisor's own bash
process starts. Killing only the leaf child (e.g. `pgrep -f
node_aggregator_ref.py`) does not pick up an edit to the `.sh` file — the
loop just re-execs the same already-parsed command. **To deploy a code or
supervisor-script change, kill the supervisor's own PID, not just the
child.**

**Check:** after any change to a `run_*_supervised.sh` file or to a script it
directly execs, confirm you killed the right PID (`ps aux | grep
run_.*_supervised`) before assuming the new behavior is live.

## 2. Watchdogs / dead-man's-switches

Seven distinct dead-man's-switches exist today, all in
`alerting/alert_engine.py` unless noted:

| Switch | Detects | Threshold/interval | Source |
|---|---|---|---|
| `[PIPELINE-DOWN]`/`-RECOVERED` | aggregator heartbeat gone stale/missing in VM | `HEARTBEAT_STALE_THRESH_S=90.0`, `CONSEC_FAIL_THRESH=3` | `alerting/pipeline_health.py:46-48` |
| `[DCGM-HOSTENGINE-DOWN]`/`-RECOVERED` | DCGM hostengine unreachable | `DCGM_FALLBACK_CHECK_INTERVAL_S=20.0` (alert_engine.py:97) | alert_engine.py:3694/3699 |
| `[DUMP-DISK-WARN]`/`[CRITICAL]`/`-RECOVERED` | dump-directory disk usage | `DUMP_DISK_WARN_PCT=80.0` / `DUMP_DISK_CRITICAL_PCT=95.0` (env-overridable, alert_engine.py:114-115), 60s interval | alert_engine.py:3757/3753/3766 |
| `[PATH-C-DOWN]`/`-RECOVERED` | missing supervisor log / crash-looping iowait_logger | `PATH_C_CHECK_INTERVAL_S=60.0` (alert_engine.py:138) | alert_engine.py:3801/3845/3852 |
| `[GRAFANA-DOWN]`/`-RECOVERED` | Grafana up-but-serving-errors (concurrent probe, not just port-open) | `GRAFANA_CHECK_INTERVAL_S=60.0` (alert_engine.py:161); no-op if `GRAFANA_URL` unset | alert_engine.py:3979/3988 |
| `[VM-DOWN]`/`-RECOVERED` | VM unreachable, OR reachable but ALL supervised hosts' heartbeats stale at once (e.g. VM's own low-disk-space read-only mode) | `VM_CHECK_INTERVAL_S=60.0` | added this pass — see below |
| `[CHECK-FAILED]` | an exception inside one named check function | — | alert_engine.py:1440-1455 |

**What `[CHECK-FAILED]` does NOT cover:** a crash in `poll_once()`'s own
surrounding code, or the process dying from a signal — that class is covered
instead by the supervisor restart loop in §1, and by `[PIPELINE-DOWN]` for
the aggregator side specifically.

**`[VM-DOWN]`/`[VM-RECOVERED]` (closed — this was the previous edition's
single biggest flagged gap).** Real investigation (two isolated scratch VM
instances, deliberately broken) found VM's own failure modes differ from
Grafana's: destroying its data directory while running does **not** produce
a long-lived degraded state — VM's own free-disk-space watcher panics the
whole process within under a minute (confirmed live, ~10-43s across runs).
The real, slow-onset "up but wrong" analogue is **low free disk space**:
confirmed live via `-storage.minFreeDiskSpaceBytes` — VM stays alive
indefinitely, `/health` still returns 200, but every write gets a real HTTP
503 ("the storage is in read-only mode"), silently starving every
supervised host's heartbeat at once. The new check combines a direct
`/health` reachability probe (catches the crash case) with an
all-hosts-stale-at-once check reusing the already-computed
`self.pipeline_down` state (catches the read-only-mode case) — no new
metric, no new cardinality. Validated end to end with a standalone harness
against the real `AlertEngine` class and a real, deliberately-broken
scratch instance (160s clean healthy baseline, a real crash→`[VM-DOWN]`
within ~10s, a real restart→`[VM-RECOVERED]` once the new heartbeat cleared
VM's own new-series visibility lag), then deployed to the real production
`alert_engine.py` supervisor and re-validated against the real VM:
`tools/self_test.sh` clean PASS, zero false positives.

## 3. Disk/resource growth

- **Aggregator unbounded-memory fix:** hardcoded `500`-entry caps (not a
  named constant) at `aggregator/node_aggregator_ref.py:1301,1304` —
  `if len(s.all_vals) > 500:` / `if len(s.rate_samples) > 500:`. Also bounded
  via `deque(maxlen=PERSIST_WINDOW)` at lines 421-422.
- **Inspector plugin ring buffer:** `INSPECTOR_RING_CAPACITY = 256` —
  `inspector-plugin/inspector.h:26`.
- **Dump-directory accumulation (measured, workload-dependent — DESIGN_NOTES.md §5.2, line 2069):**
  nanoGPT lean-mode ≈ **28.5 MB/min/node**; Megatron TP4/PP4 lean-mode ≈
  **123 MB/min/node**. These are not interchangeable — re-measure per
  workload shape before sizing disk for a new one.
- **Log rotation:** every `run_*_supervised.sh` wires a generated
  `logrotate` config (warns loudly if `logrotate` isn't installed;
  `install.sh:324-328` auto-installs it). Separately, `iowait_logger.py`
  uses Python's own `RotatingFileHandler` directly:
  `IOWAIT_LOG_MAX_BYTES = 10*1024*1024` (10MB), `IOWAIT_LOG_BACKUP_COUNT = 10`
  (iowait_logger.py:77-78,99).
- **Disk watchdog:** `DUMP_DISK_WARN_PCT=80.0` / `DUMP_DISK_CRITICAL_PCT=95.0`
  (alert_engine.py:114-115, env-overridable) — see §2.
- **`classifier/rolling_buffer.py` precision correction (fixed in README,
  this pass)**: its query functions are NOT dead code — `classifier.py:25`
  imports and calls `query_gpu_window`/`query_host_window`/`query_ib_window`
  as a real, reachable branch (`source == "rolling_buffer"` vs.
  `"live_query"`, classifier.py:466 etc.). What's actually inert is
  narrower: the one live call site (`alert_engine.py`'s
  `build_global_drift_finding` call) hardcodes `buffer=None`, and
  `rolling_buffer.run_sampler` (the continuous per-second sampler that
  would need to populate a real buffer) is never launched by any
  supervisor or by `run.sh` — confirmed via `grep -rn "run_sampler"
  --include=*.sh .` returning nothing. So it always takes the
  `live_query` branch today, by that one explicit `buffer=None`, not
  because the code is unwired. README now states this precisely.
- **Backlog-replay RSS spike:** one from-scratch checkpoint-seeding replay
  during validation drove aggregator RSS to ~**60GB** before settling —
  pre-existing, unrelated to the checkpoint fix itself, flagged not fixed
  (DESIGN_NOTES.md §3.1, line 1055).

## 4. Version-pinned dependencies

| Dependency | Pinned version | Where pinned |
|---|---|---|
| NCCL | `2.28.9` / apt `2.28.9-1+cuda13.0` | `environment.sh:60-61` (`NCCL_VERSION_DOTS`, `NCCL_APT_VERSION`) |
| CUDA | `13.0` | `environment.sh:78` (`CUDA_VERSION_WANT`) |
| VictoriaMetrics | `v1.150.0` | `vm-setup.sh:49` |
| Grafana | `11.5.1` | `grafana-setup.sh:58` |
| bpftrace | not pinned/installed by this repo — `0.20.2-1ubuntu4.3` is only the confirmed-working version recorded in `VERSIONS.md:75` | n/a |
| PyYAML (`python3-yaml`) | not pinned, whatever `apt install python3-yaml` currently resolves to (`6.0.1-2build2` on this dev cluster) | **Now required by the live `alert_engine.py` too, not just `tools/incident_correlator.py`** — both import the shared `observability/reliability_reference.py` module, which reads `observability/workload_reliability_reference.yaml` for the role-baseline UNVALIDATED gate (see §5/§6 below). This is a deliberate, disclosed break from this pipeline's prior stdlib-only design — `node_aggregator_ref.py` remains stdlib-only; `alert_engine.py` does not anymore. `pip install pyyaml` is refused on this cluster's externally-managed Python (PEP 668); use the system package, and confirm it's present before restarting `alert_engine.py`'s supervisor — a missing import here degrades to every role-baseline finding presenting as UNVALIDATED (see the engine's own load-failure log line), not a crash, but still a real loss of signal quality worth noticing promptly. |

**Known version-sensitive gotchas (both from a real launch-script bind-mount
of the host's `/usr/lib/x86_64-linux-gnu` into the container):**
- **Host-NCCL-shadowing** (README.md §3, line 251): the mount silently
  shadows the container's bundled NCCL with whatever's host-installed. Some
  shapes force `LD_LIBRARY_PATH` to the pinned 2.28.9 build to compensate —
  a disclosed inconsistency, not a universal fix.
- **TE/CUBLAS shadowing** (README.md §3, line 263, same bullet as above):
  the same mount also shadows CUBLAS; a real Megatron TP4/PP4/DP3 run
  crashed with a `cublasLtGetVersion` symbol error under
  `--transformer-impl=transformer_engine`, worked around with
  `--transformer-impl=local`.

**If any of these versions change:** re-run `tools/self_test.sh`, re-check
both shadowing gotchas above still apply/don't apply, and re-measure overhead
(§8) — a toolchain version bump is exactly the kind of change that silently
invalidates an old overhead number.

## 5. Known limitations / disclosed gaps — current status

Pulled from README §7 ("Known limitations," README.md:1075) and verified
against its current text, not memory. **README.md was split into a tight
README.md + a new DESIGN_NOTES.md during the V1 Beta doc-consolidation
pass** — every investigation narrative previously cited here by README
line number now lives in DESIGN_NOTES.md instead; the table below was
re-pointed accordingly, re-verified directly against both current files,
not carried forward from memory:

| Item | Status | Note |
|---|---|---|
| NVLink never validated against a real fault | **GENUINELY STILL OPEN** | Three different real injection approaches tried, none could produce one (README.md §7.1, line 1136) |
| ECC/PCIe not validated as independent cause | **ACCEPTED-AS-IS by design** | Explicitly never used alone to drive a CONFIRMED tier (README.md §7.1, line 1145) |
| `rolling_buffer.py` sampler "not deployed" | **FIXED (precision correction in README)** | The sampler genuinely isn't launched, but the query functions ARE live, reachable code gated behind one `buffer=None` call site — see §3 |
| "Rank-12-style" structural role-position bias | **PARTIALLY ADDRESSED** | Original TP-group-local role_rank=0 finding still tracked as open (README.md §7.1, line 1102), with a pointer to the full origin story and the two distinct false-positive fixes (contamination, volatility) built on top of it (DESIGN_NOTES.md §1.6, line 388) |
| MoE/DLRM ring-buffer capacity overflow | **GENUINELY STILL OPEN** | 4 real runs showed drop counts spanning ~1000x for identical code/config; root cause undetermined, no capacity change made (README.md §6.9, line 942; full investigation DESIGN_NOTES.md §5.1) |
| Cross-node PP-link `baseline_source` gap (Hybrid) | **ACCEPTED, permanent topology limitation** — but see caveat | Hybrid's 2-ranks/node layout has no independent same-shape peer comm on the SAME host for the below-floor fallback to use (DESIGN_NOTES.md §2.3, line 627) — the underlying topology limitation is real and permanent, but P27.5 (same section) works around it for any real multi-worker Hybrid deployment by pooling job-wide instead of same-host; only a truly single-worker-equivalent isolated test still hits the raw gap |
| First-seed replay cost after dump-backlog checkpoint fix | **ACCEPTED-AS-IS, disclosed** | The very first checkpoint-seeding replay still pays the full from-scratch cost (DESIGN_NOTES.md §3.1, line 1045) |
| `workload_signature()` can lock permanently incomplete under a severe-enough fault | **MITIGATED (severity bar raised), NOT eliminated; see full writeup below** | `SIG_LOCK_MIN_ELAPSED_S=300.0` fix, confirmed live on FSDP |

**Maintenance note, found live during the original version of this
table's own staleness, and again confirmed during the doc-consolidation
pass**: any future large edit to either README.md or DESIGN_NOTES.md can
silently re-stale every line number in this table again — there is no
automated check tying these together. Re-grep each real quoted phrase
above (not just trust the number) whenever this table is next consulted
for anything higher-stakes than a quick read.

**Resolved this pass:** README §7's rolling_buffer paragraph now states
precisely which part is deployed (the query functions, reachable code) vs.
not (the sampler process and the one `buffer=None` call site) instead of
a blanket "not deployed."

**`workload_signature()`'s premature-lock gap, found and partially
fixed this session.** `workload_signature()` (node_aggregator_ref.py)
locks a job's cross-job-comparison fingerprint the moment bucket-
discovery RATE stabilizes -- a proxy for "comm structure has settled,"
not for "every real collective type this job will ever use has fired at
least once." A collective that fires less often than the ones driving
denom-stability (confirmed live: FSDP's own `(4, 'AllReduce')`, a once-
per-optimizer-step scalar reduction, vs. `AllGather`/`ReduceScatter`
firing once per layer per iteration) can simply not have appeared yet at
lock time -- permanently baking an incomplete signature that then never
matches a healthy historical run's complete one, breaking
`_member_role_baseline`'s cross-job lookup for every role position in
that job, not just the real injected target's.

Fixed by `SIG_LOCK_MIN_ELAPSED_S=300.0`: the lock now also requires 300s
of real elapsed dump-time since the job's own first record, **in
addition to** (never instead of) the existing denom-stability
requirement. 300s is grounded in real historical data, not a guess --
measured directly against 6 historical FSDP jobs' own real sig-lock
delay (110-193s, including one at the identical injected fault severity
that still locked correctly), with real margin above the observed range.
Validated live: a fresh FSDP fault test (STRAGGLER_SLEEP_MS=5000,
different target rank, same severity as the original finding) now locks
a sig that correctly includes `AllReduce`, matching every healthy
historical job exactly, confirmed directly via
`agg_job_workload_sig_info`. `tools/self_test.sh` (DP) confirmed
unaffected -- DP's own sig locks fast already (no rare/asymmetric
collective type), so the new floor costs it nothing.

**Two honest limitations, deliberately not addressed by this fix (do
not read the above as "solved"):**
1. **This raises the fault-severity bar needed to reproduce the bug, it
   does not categorically eliminate it.** A second real validation
   attempt at the identical severity, targeting a different rank, still
   had not produced a complete signature even after 20+ minutes -- an
   order of magnitude past the new floor. No fixed time floor can fully
   defend against an arbitrarily severe fault; it only makes the bug
   require a worse one to reproduce.
2. **The lock is permanent and never re-evaluated for the rest of the
   job, even if the missing collective type appears later** --
   confirmed directly: `agg_job_workload_sig_info` holds exactly one
   sample for a job's entire lifetime, no matter how long it keeps
   running afterward. A more complete fix would allow re-locking if the
   discovered `coll_types` set ever grows past what's already recorded.
   Deliberately **not** built as part of this fix -- scoped out as its
   own, larger, separate future investigation, not bundled into this
   time-floor mitigation.

**Cross-shape risk -- three distinct tiers, not one blanket statement:**
- **Confirmed affected:** FSDP (this investigation, directly fixed and
  validated above). Separately, this project's own historical signature
  log already shows a second, independent real occurrence of the
  identical pattern on a different below-floor shape (same `n_comms=11`
  family, one real historical sig reading
  `AllGather+AllReduce+Recv+Send:4+8+99+524288+1048576+2097152`, another
  real historical sig for the apparent same shape reading only
  `AllReduce+Send:4+8+524288+1048576` -- missing both `AllGather` and
  `Recv`) -- this is not hypothetical, it has already happened at least
  once before, independent of this session's FSDP finding.
- **Plausible but unconfirmed:** DLRM/MoE-shaped jobs (sig composed of
  `AllReduce` only). Their real message-size-bin composition varies
  job to job in this project's own history (`8+12+166+1024+...` vs.
  `8+2097152+...` vs. `8+2965821+...`, etc.), meaning which large,
  less-frequent buckets have been discovered by lock-time is not fixed
  -- a severe fault delaying one of those could trigger the same class
  of bug, keyed on message size rather than collective type. Not
  directly tested.
- **Lower-risk in practice, but not proven safe:** PP/Hybrid. Every
  real historical sig sample found for this shape shows `AllReduce`
  consistently co-occurring with every other collective type across
  many samples, suggesting its own collectives discover close together
  in practice -- but this is an observation from available history, not
  a structural guarantee the same race can never happen there under a
  severe enough fault.

Do not flatten these three tiers into "this is fixed for every shape" --
only FSDP has been directly fixed and validated; the others range from
"independently confirmed to have happened" to "unconfirmed but
plausible" to "no evidence yet, not proven safe."

## 6. Calibration constants needing re-validation on change

All of these were calibrated against *specific observed behavior* on this
project's dev cluster/workload mix — not universal constants.

| Constant | Value | Location |
|---|---|---|
| `SELF_DETECTION_FLOOR` | 3 | alert_engine.py:848 |
| `PERSIST_REQUIRED` | 3 | thresholds.py:25, node_aggregator_ref.py:104 |
| `PERSIST_WINDOW` | 3 | thresholds.py:24, node_aggregator_ref.py:103 |
| `CV_Z_THRESH` | 60.0 | thresholds.py:23, node_aggregator_ref.py:105 |
| `MEAN_Z_THRESH` | 30.0 | thresholds.py:31, node_aggregator_ref.py:106 |
| `MEAN_MM_THRESH` | 2.0 | thresholds.py:32, node_aggregator_ref.py:107 |
| `TIMING_FALLBACK_MAD_MULTIPLE` | 6 | alert_engine.py:239 |
| `TIMING_FALLBACK_STOPGAP_ACTIVE` | True | alert_engine.py:343 |
| `PATH_B_AND_TIMING_SUPPRESS_RATIO` | 0.6 | classifier/classifier.py:94 |
| `WAIT_INDUCED_STOPGAP_ACTIVE` | True | alert_engine.py (module-level, near `TIMING_FALLBACK_STOPGAP_ACTIVE`) |
| `ROLE_BASELINE_MIN_HISTORY` | 3 | alert_engine.py:300 |
| `ROLE_BASELINE_ALERT_STOPGAP_ACTIVE` | True | alert_engine.py (module-level, near `WAIT_INDUCED_STOPGAP_ACTIVE`) |
| `ROLE_BASELINE_VOLATILITY_MIN_N` | 15 | alert_engine.py (near `ROLE_BASELINE_MIN_HISTORY`) |
| `ROLE_BASELINE_VOLATILE_FLAG` | 0.3 | alert_engine.py (near `ROLE_BASELINE_MIN_HISTORY`) |
| `ROLE_BASELINE_MIN_HISTORY_VOLATILE` | 10 | alert_engine.py (near `ROLE_BASELINE_MIN_HISTORY`) |
| `ROLE_BASELINE_UNVALIDATED_GATING_ACTIVE` | True | alert_engine.py (module-level, near `ROLE_BASELINE_ALERT_STOPGAP_ACTIVE`) |
| `ROLE_BASELINE_JOB_LOCKSTEP_FLAG` | 0.7 | alert_engine.py (near `ROLE_BASELINE_VOLATILE_FLAG`) |
| `BUCKET_MATURITY_GRACE_S` | 120.0 | node_aggregator_ref.py:190 |
| `SIG_LOCK_MIN_ELAPSED_S` | 300.0 | node_aggregator_ref.py:451 -- grounded in 6 real historical FSDP jobs' own sig-lock delay (110-193s observed); see §5's own full writeup for the real gap this closes and its two disclosed limitations |
| Ring buffer capacity | 256 | inspector-plugin/inspector.h:26 |
| `DUMP_DISK_WARN_PCT` / `CRITICAL_PCT` | 80.0 / 95.0 (env-overridable) | alert_engine.py:114-115 |

**The wait-induced-straggler check (`_wait_induced_fallback_evaluate`)
shares every one of its real thresholds with the P27.2 2-member timing-
asymmetry fallback above — `PATH_B_AND_TIMING_SUPPRESS_RATIO`,
`TIMING_FALLBACK_MAD_MULTIPLE`, `ROLE_BASELINE_MIN_HISTORY`,
`ROLE_BASELINE_MAX_RELATIVE_MAD` — by deliberate design, not
coincidence (it's a direct generalization of that same mechanism to
3+ members). Retuning any of these for P27.2's own sake also retunes
this check; re-validate both together, not just the one you meant to
change. `WAIT_INDUCED_STOPGAP_ACTIVE` is its own, separate flag (tier
PROBABLE vs CONFIRMED) — mirrors `TIMING_FALLBACK_STOPGAP_ACTIVE`'s own
precedent (that flag exists because the identical evidence type
false-fired CONFIRMED/PAGE on 5/5 healthy runs before a fix landed);
do not flip it to `False` without the same breadth of real-world
exposure that earlier false-fire took to surface, not just one
session's clean validation.**

**The role-baseline-deviation check (`_role_baseline_fallback_
evaluate`) and its own Grafana panels ("Wait-Induced Detections",
"Role-Baseline Detections", "Role-baseline exclusion health" — see
README §6.12) all reuse the SAME underlying role-baseline/exclusion
infrastructure `_member_role_baseline`/`_push_role_baseline_exclusion`/
`_excluded_role_pool_members` provides, not separate copies.
`ROLE_BASELINE_VOLATILITY_MIN_N`/`ROLE_BASELINE_VOLATILE_FLAG`/
`ROLE_BASELINE_MIN_HISTORY_VOLATILE` specifically gate whether a role
shape's narrow, sig-filtered pool is trusted at all (see `_member_role_
baseline`'s own `broad_hist_rows` comment) — retuning any of them
changes which findings fire `role` vs. degrade to `cross_comm_peer`,
which changes the real `baseline_source`/`volatile` values the Grafana
panels display, not just the underlying detection behavior. Re-
validate both the detection outcome AND the dashboard panels together
after any change here — a change that looks correct in the alert log
can still silently break what the panels show (e.g. a `volatile` label
that stops matching what the live gate actually used) if only the
detection side is re-checked.**

**`ROLE_BASELINE_UNVALIDATED_GATING_ACTIVE` has one confirmed, real,
safe-direction side effect worth knowing about, not a bug to chase:**
resolving a comm's `workload_sig` requires `agg_job_workload_sig_info`
to have been pushed, which itself needs the job to reach throughput
stability -- a separate, sometimes-later threshold than role-baseline's
own 3-consecutive-sample persistence requirement. Confirmed live: a
real TP-inference fault-injection run (a KNOWN_RELIABLE shape) fired
`[ROLE-BASELINE-ALERT-UNVALIDATED]`, not the plain header, because its
sig hadn't resolved yet at the moment of firing -- re-checking moments
later, the same sig resolved correctly to KNOWN_RELIABLE. This means
even a well-characterized shape can transiently show the UNVALIDATED
variant during a job's own early cold-start window. The error is always
in the safe direction (toward MORE skepticism, never toward false
confidence), so this was not treated as a defect worth fixing as part
of the original change -- but don't be surprised by it, and don't
interpret an early UNVALIDATED finding on a known-good shape as a sign
the reference table or the lookup logic is broken.

**`ROLE_BASELINE_JOB_LOCKSTEP_FLAG` has the IDENTICAL kind of early-job
race, confirmed live, for the same structural reason.** This gate
promotes `_cross_comm_peer_median` (same-Slurm-job comparison) ahead of
cross-job role-baseline for a role shape whose per-job-median spread
exceeds 0.7 (see README.md §7.1's TP4-standalone entry, and the full
mechanism in DESIGN_NOTES.md §1.8). `_cross_comm_peer_median` needs this
job's OTHER same-shape comms to have posted fresh data; the role-
baseline elevated gate's own 3-consecutive-sample persistence
requirement can complete before that happens, letting an early firing
fall through to the old `role`/`role_cross_host` path even on a
lockstep-flagged shape -- confirmed live, several such firings in a
fresh clean run's first 1-2 minutes. Re-validate this interaction
specifically (not just the steady-state behavior) if `T.PERSIST_
REQUIRED` or any poll-interval timing changes -- a faster persistence
gate makes this race wider, not narrower. Deliberately NOT a blocker
for shipping, since `ROLE_BASELINE_UNVALIDATED_GATING_ACTIVE` already
catches every one of these (TP4-standalone's own role_baseline status
is `KNOWN_NOISY`, not `KNOWN_RELIABLE` -- see `observability/
workload_reliability_reference.yaml`), same safe-direction reasoning as
the sig-resolution race above. Separately, `_role_shape_job_lockstep`
deliberately does NOT apply `_excluded_role_pool_members`' own
exclusion filter (unlike every other broad-pool read in this file) --
confirmed live this is load-bearing, not cosmetic: applying it hid the
real bimodality signal entirely (this function read 0.0 instead of the
real 0.96 for TP4's own confirmed-bimodal shape) because the
contamination-exclusion mechanism had already removed every "high"
sample as an earlier false-positive artifact. Do not "fix" this by
re-adding the exclusion filter without re-deriving the whole threshold
against unfiltered data again.

**Drift found and fixed this pass:** `aggregator/promql_cv_verify.py` (a
standalone manual verification CLI, not part of the live detection path —
confirmed `node_aggregator_ref.py` only imports its `stat_cv()` function,
which uses `TRIM` only, not the thresholds below) was carrying stale
`PERSIST_REQUIRED=2`/`CV_Z_THRESH=20.0` against the live pipeline's real
`PERSIST_REQUIRED=3`/`CV_Z_THRESH=60.0` — directly contradicting its own
module docstring's claim of "matching detection.py/classifier.py exactly."
Corrected to `3`/`60.0` (promql_cv_verify.py:21-28); a manual run of this
tool now reaches the same fire/no-fire decision the live pipeline would.
**The general risk remains** — these are separate constants in a separate
file with no shared source of truth, so this can drift again. Whenever you
change a threshold in `thresholds.py` or `node_aggregator_ref.py`, grep for
other copies of the same constant name across the repo.

**Re-validate all of the above whenever:** the workload mix changes
meaningfully, cluster scale changes (more nodes/GPUs per node), or new
hardware generation is introduced — these numbers were calibrated on a
2-node/16×H200 cluster against the specific workload shapes in
`workloads/`, not derived analytically.

**`observability/workload_reliability_reference.yaml`** (read by
`tools/incident_correlator.py`, see README §6.13) is this same
re-validation discipline applied to check-RELIABILITY findings rather
than numeric constants: every time a new workload shape or a new
noisy/blind/fixed check-behavior pattern like the ones in §6's own
`ROLE_BASELINE_*` paragraph above is characterized, add it there as a
new entry (or a new `workload_sigs` variant on an existing one — exact
string match, never fuzzy; a real validation run this session found the
SAME workload producing two slightly different real signature strings
across two different jobs). An entry that silently goes stale (a check's
real behavior changes after a code fix, but its note here still
describes the old behavior) is worse than no entry at all, since a human
reading it would be actively misled rather than honestly told
"unvalidated."

## 7. Build/install fragility

- **`NCCL_HOME` override must be command-line form.** The Inspector plugin
  Makefile uses `:=` plain assignment, which an exported environment
  variable does **not** override — must be `make NCCL_HOME=...`. Already
  handled correctly in `install.sh:366-373`; if you ever touch that build
  invocation, preserve the command-line form.
- **Path-mismatch bug pattern (fixed once, can recur):** `install.sh:353-359`
  and `environment.sh:280-288` each independently check the same two
  candidate NCCL source directories, in the same order. They were
  previously out of sync (environment.sh only checked one). **Any future
  change to one candidate list without the matching change to the other
  reintroduces this exact bug class** — grep both files together whenever
  either changes.
- **apt-extraction fallback depends on NVIDIA's apt repo being reachable.**
  `try_nccl_apt_extraction()` (environment.sh:244-278) fails gracefully at
  every stage (download failure, corrupt `.deb`, bad layout, post-extraction
  validity mismatch) and falls through to git-clone+build — but there's no
  separate preflight check for repo reachability; the failure only surfaces
  inside the download attempt itself. On a cluster without that apt repo
  configured, expect this fallback to silently cost a failed attempt before
  falling through to the ~16-minute from-scratch build.
- **`DUMPDIR_BASE` must be absolute.** Every `workloads/*/run_*.sh` launcher
  (14 files) takes it as a plain positional arg with no normalization.
  Self-dispatching shapes `cd` into their own workload directory before a
  relative path resolves — **silent non-detection, no error** (README.md
  §8, line 1333). Always pass `"$PWD/var/dump"` or similar.

## 8. Overhead/performance — all current numbers, none permanent

Five distinct measurements exist, each under different conditions — never
treat one as "the" number, and re-measure after any workload-shape, scale,
or plugin change:

All five numbers now live in DESIGN_NOTES.md (moved out of README.md
during the V1 Beta doc-consolidation pass; README.md §7.2 keeps only the
one-line summary and a pointer):

1. ResNet, 1-GPU, earliest/undocumented mode: **~4.5x** (85ms Inspector vs.
   19ms without vs. 7.57ms bare) — DESIGN_NOTES.md §5.2, line 1993.
2. 48-GPU Megatron TP4/PP4/DP3, pre-lean-mode: **~6.5%** relative
   (94.7ms vs. 89.0ms/iter) — DESIGN_NOTES.md §5.2, line 2005.
3. 2-node/16-GPU TP4/PP4/DP1, lean mode confirmed active, 500 iter:
   **~12.3%** relative (193.50ms vs. 172.31ms) — DESIGN_NOTES.md §5.2,
   line 2049.
4. Same shape, after the ring-buffer fix: **~13.1%** relative (192.05ms vs.
   169.82ms) — DESIGN_NOTES.md §5.1, line 1882. Ring buffer itself adds no
   measurable new cost; the 0.8pp shift from #3 is noise.
5. Same shape, after `gRetireLock` scoping + lean-mode `collEvtTrk` skip:
   **~12.0%** pooled (individual re-measurements: 14.84% and 9.21%,
   n=980 each) — DESIGN_NOTES.md §5.1, line 1941. Explicitly labeled a
   small, directionally real improvement, not dramatic — run-to-run spread
   exceeds the apparent gain.

**Rule:** re-measure overhead (same methodology as #3-5: n≥500 steady-state
iterations, ON vs OFF) whenever a new workload shape, a scale change, or an
Inspector-plugin change lands. Don't reuse an old number across a
meaningfully different condition.

## 9. Version-to-version risk areas — when you touch X, re-check Y

Patterns this project has already seen recur in different forms:

- **Per-job state not reset → cross-job/cross-workload contamination.**
  `comm_calib`/`comm_bucket_members` not being reset per job was
  independently discovered by the PP-fix and Hybrid-fix work as the root
  cause blocking their own cross-job role-pool separation (fixed in
  `6f763337`, documented further in `56f893bc`). **When you add any new
  per-communicator or per-workload cached/calibrated state, check whether
  it's reset on job boundary — this exact bug shape has already recurred at
  least twice.**
- **`abs()` vs. signed deviation in "worst member" selection.** Fixed in
  `b3e2f90a`: `score_mean_window()` used
  `max(means, key=lambda p: abs(means[p] - median))`, flagging whichever
  member deviated most in *either* direction — role_rank=0 (the fastest
  member) was wrongly selected "worst" in 40-86% of healthy windows before
  the fix. The same commit explicitly checked `check_outlier_count()` for
  the identical bug shape and confirmed it was already safe. **Whenever you
  add or touch a "pick the extreme member" selection, grep sibling
  scoring functions for the same unsigned-deviation mistake — it's a
  pattern, not a one-off.**
- **Lock-scoping changes need explicit lifecycle/teardown re-verification,
  not just a mechanical narrowing.** `gRetireLock` was narrowed from one
  process-wide mutex to one per communicator (`987896be`) — doing this
  safely required re-reading the retention invariant's own docstring, not
  assuming the narrower scope preserved it. **Any future lock-scoping
  change (narrowing OR widening) needs the same explicit re-check of what
  invariant the lock was actually protecting**, not just "it compiles and
  tests pass."
- **Duplicated calibration constants drift.** See §6 —
  `promql_cv_verify.py` already drifted from the live pipeline's thresholds.
  **Whenever you change a threshold in `thresholds.py` or
  `node_aggregator_ref.py`, grep for other copies of the same constant name
  across the repo** before assuming it's the only place that needed the
  change.

## 10. Cross-cluster/environment variability

This pipeline live-probes the following at install time rather than
assuming consistency with the 2-node/16×H200 dev cluster it was built and
validated on — expect these to differ on a new cluster:

- **Node list / count**: `sinfo -N -h -o '%N'` (install.sh:120), deduped.
- **GPUs per node**: `scontrol show node ... Gres=` (install.sh:128-156) —
  explicitly NOT assumed uniform; a mixed fleet takes the minimum and warns.
- **Host NCCL library presence** (install.sh:171-179) — warns if absent.
- **`/tmp` filesystem type** per node (install.sh:189-197) — the
  storage-fault classifier needs real disk-backed `/tmp`, not tmpfs.
- **bpftrace/tracefs functionality** per node (install.sh:206-275) —
  three-tier probe (installed? can it attach directly? does the
  tracefs-wrapper fix actually work after install?); a
  CAP_SYS_ADMIN/seccomp-restricted SSH jail is flagged as a real,
  unfixable-by-this-script infra gap.
- **libLLVM SONAME conflict** with the NVIDIA driver's bundled LLVM
  (install.sh:267-274) — already confirmed needed on *some* nodes of this
  very cluster and not others; don't assume node homogeneity even within
  one cluster.
- **DCGM hostengine liveness** (install.sh:301-318) — attempts auto-start,
  warns if no root/sudo/known systemd unit exists.
- **NVIDIA apt repo configured/reachable** — no explicit preflight; only
  surfaces as a failure inside the CUDA-keyring fetch or NCCL
  apt-extraction attempt (§7).
- **Host NCCL version itself** — observed to drift on this same cluster
  within one session (2.29.7, then later 2.30.1) — never assume the
  host-shadowing gotcha (§4) resolves to the same version twice.

**Takeaway for a new cluster:** run `install.sh` fresh and read its warnings
carefully rather than copying a working `cluster.env` from another cluster —
every item above is a live probe specifically because it's been observed to
vary.
