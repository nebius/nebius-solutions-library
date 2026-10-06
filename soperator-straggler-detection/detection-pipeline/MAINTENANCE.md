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
| `_correlate_firing_timing_alerts` cannot identify a wait-induced-style culprit as root cause | **GENUINELY OPEN, by design — a tiebreak fix was tried, tested live, and reverted as disproven; see full writeup below** | Confirmed live on Hybrid jobs 3867, 3869, 3870, 3871 |
| Inspector's `coll_sn` field never increments for `Send`/`Recv` (stays `0`, confirmed against 5,289 real records) — not usable as a cross-rank sequencing signal for point-to-point ops, only for `AllReduce`/`Broadcast` | **CONFIRMED DEAD END, real plugin gap, not a lean/verbose access issue** | A real plugin-level fix would be needed, not a query/panel change |
| `_emit_timing_fallback` (the below-floor P27.2 cascade path — TP2/TP-inference/PP/Hybrid) never calls `_push_visibility_metric` | **CONFIRMED STRUCTURAL COVERAGE GAP, not a bug in any panel's own logic** | `agg_straggler_incident_tier` has zero real rows from any below-floor incident across this project's entire history; see full writeup below |
| `node_aggregator_ref.py`'s `mean_unconsumed`/`cv_unconsumed` lists grew without bound under MoE's extreme comm/bucket cardinality | **FIXED (500-entry drop-oldest cap, matching the existing `all_vals`/`rate_samples` precedent), confirmed correct for its own target** | Reduced this mechanism's real measured contribution from a ~15M-entry lower bound to ~65MB; see full writeup below |
| `node_aggregator_ref.py`'s `push_buf` grows unbounded during a single large `poll_files()` catch-up cycle (no flush/heartbeat until that call returns, and its own deadline check never fires in practice) | **FIXED (`POLL_FILES_MAX_SECONDS=5.0` real per-call deadline), confirmed live over a full 48-minute run — zero heartbeat gaps, memory flat/bounded** | Was the dominant cause of the heartbeat/scoring blackout; see full writeup below. MoE's own separate, open detection-timing question (not this bug) is the one remaining item before MoE is fully validated |
| `self.state`/`comm_bucket_members`/`calib.scored` key count, uncapped | **CONFIRMED NOT the dominant factor; deferred with a real growth-rate projection, not just "wasn't dominant once"** | Real bucket-identity discovery converges within ~1 minute wall-clock (confirmed twice, 202 buckets both times) — key count does not scale with job duration for this workload; see full writeup below |

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

**`_correlate_firing_timing_alerts`'s root-cause labeling cannot
identify a wait-induced-style culprit, by design — found and confirmed
live, not implemented around (disclosure only).** This is the PP/Hybrid
cross-comm cascade-grouping mechanism (P27-hotfix9/9b). Re-exercised
this session after several nearby functions it depends on
(`_timing_asymmetry_fallback_evaluate`, `_wait_induced_fallback_
evaluate`, `_role_baseline_fallback_evaluate`, the sig-lock fix above)
were all touched or fixed this session — confirmed via `git log -p`
that `_correlate_firing_timing_alerts` and its own call site were NOT
modified by any of that work, so this is a regression check on
untouched code, not a retest of something changed.

**What still works, confirmed live:** the grouping itself. Launched a
real Hybrid TP+PP job (3867, `STRAGGLER_SLEEP_MS=1000`, real target
global rank 0, ground truth confirmed from its own dump file before
checking any alert). Two real `[ALERT]` lines fired within the 60s
correlation window, each correctly naming its own true straggler
(`rank=1060966`, the real injected target, on the direct rank0↔rank2 PP
comm; `rank=1278664` on the rank1↔rank3 PP comm — never faulted, the
exact "innocent downstream pair" shape the mechanism was built to
catch). `_correlate_firing_timing_alerts` correctly found a real,
genuine physical connection between them via an actual BFS path over
shared comm membership — not two alerts treated as independent faults.
That part is proven, not assumed.

**What does NOT work, confirmed live, and will not be fixed by
retrying or recalibrating: the root-cause-vs-downstream labeling,
specifically for a wait-induced-style straggler.** Both of job 3867's
real alerts came back labeled mutual "possible echoes" of EACH OTHER —
neither was marked `ROOT_CAUSE_CANDIDATE`, despite one of them being
the comm containing the real, ground-truth-confirmed injected target.
Root cause, traced precisely in the mechanism's own logic: it only ever
seeds its BFS from an alert's ELEVATED (waiting) member — by this
fallback's own foundational, already-established signature (confirmed
throughout this project), the true wait-induced culprit is always the
SUPPRESSED (sleeping, normal-reading) side of its own comm, never the
elevated side. A real culprit can therefore never be the BFS's own
starting point, and so can never itself be identified as the thing
nothing-else-explains (`ROOT_CAUSE_CANDIDATE`) — it is structurally
invisible to this specific algorithm's notion of "root cause," no
matter how clean or well-isolated the real fault is. **For this fault
class, a human must read the individual `[WAIT-INDUCED-ALERT]`/P27.2
timing-fallback line's own named culprit directly — do not trust this
correlation layer's root-cause/downstream labeling to surface it.**

A secondary, real contributing factor found in the same test: the BFS
path connecting the two alerts ran through a tiny, world-spanning
administrative AllReduce (`bucket=4`, trivially shared by all 4 ranks
in the job) rather than a substantial, real-payload comm like the
original validated case's own TP AllReduce hop. A comm that every rank
in a job shares membership in in makes any two alerts "BFS-reachable"
from each other almost by construction, regardless of whether a
meaningful causal relationship exists — worth knowing if this mechanism
is ever extended or re-tuned, since it means shared-membership alone is
weaker evidence when the shared comm is this trivial.

**A third, independently real signal (stage1's own TP pair, also
genuinely elevated, matching the original 3-comm Hybrid validation's
own shape) fired in the same test but never joined the group at all —
it fired 225 real seconds before the other two, outside the 60s
`RECENT_TIMING_CORRELATION_WINDOW_S` correlation window.** This is an
expected, timing-dependent property of a fixed window, not a defect —
different comms' own persistence requirements can genuinely complete at
different real times. It does raise a real, open calibration question,
not resolved here: 225s of real propagation delay between genuinely
related alerts was observed live, well past the current 60s window.
Whether 60s is still the right value, or whether real-world fault
propagation in a larger/slower topology can routinely exceed it, is a
genuine open question for whoever next revisits this constant — not
resized here, since this was a disclosure-only pass, not a fix.

**Update — a fix was attempted, implemented, tested live, and reverted.
This is now a confirmed dead end, not an untried idea; do not re-attempt
the same approach without new evidence.** A follow-up session designed
and implemented an additive `root_cause_candidate_tiebreak`: for a fully
mutual echo cycle (the exact case above, where the existing heuristic
produces zero root-cause candidates), pick whichever alert's own
already-identified culprit fired with the earliest real sample
timestamp. The additivity held up as designed — confirmed by
construction and live testing that it never engages for any group the
existing heuristic already resolves (including the original validated
3-hop case). **But the tiebreak RULE itself was tested against real
ground truth, three times, and failed every time it could be precisely
checked.** Three live Hybrid tests (targeting rank 0, rank 1, and rank 2
in turn, deliberately exercising both the stage0 and stage1 code paths)
all reproduced the identical mutual-echo cycle regardless of which rank
was targeted — confirming the cycle is a structural property of this
topology's own shared, world-spanning administrative comm, not
rank-dependent. In the cases checked precisely (real per-sample
timestamps pulled directly via the same `timestamp()` PromQL technique
`_query_instant_real_ts` itself uses, not query-range step boundaries or
print order), the tiebreak picked an innocent, never-faulted rank over
the real ground-truth target both times — the real gap between the two
alerts' own sample timestamps was ~1 second, reflecting which comm's
persistence window happened to close first in that poll cycle, not
genuine causal order. **Reverted in full** (`alerting/alert_engine.py`
matches its pre-fix commit exactly) rather than left in place
undocumented: a confidently-wrong root-cause label is worse than the
original honest "possible echo" ambiguity it would have replaced.

**Net result: the original limitation stands, fully open, unsolved.**
`_correlate_firing_timing_alerts` still correctly groups physically-
connected alerts via a real BFS path (that part remains proven, live,
multiple times), but still cannot label which one is the true root
cause when the group forms a fully mutual cycle — and earliest-real-
timestamp is now a *ruled-out* candidate fix, not merely an untried one.
For this fault class, a human must keep reading the individual
`[WAIT-INDUCED-ALERT]`/P27.2 line's own named culprit directly, exactly
as originally disclosed above — nothing about that guidance has
changed. Any future fix here needs a genuinely different signal than
real sample timestamps, since this test confirmed those specifically
don't track causal order in this mechanism's real data.

**`_emit_timing_fallback` never pushes to VictoriaMetrics — the composed-
incident-summary panel (Grafana panel id=22, dashboard
`straggler-detection-metrics`) has zero visibility into any below-floor
incident, confirmed live and across this project's entire metric
history.** Found while validating that same panel's `incident_role`/
sort-order feature (sorting a real root-cause row above its downstream
rows) against a genuine multi-role cascade, specifically requested to
confirm this live rather than by code reading alone.

The validation test itself worked exactly as intended: a live Hybrid
TP+PP job (2 nodes, world_size=4), `STRAGGLER_SLEEP_MS=1000` injected on
rank 0's PP send to rank 2, ground truth (real PID per rank) confirmed
from each rank's own dump-file header before checking any alert. The
resulting `[ALERT]` text is completely correct: it names rank 0
(PID 1303182, worker-0) as the real injected straggler and rank 2
(PID 1468478, worker-1, `ratio=136.786`, `current_mean_us=1006631`,
matching the ~1s injection almost exactly) as the correlated downstream
wait, on comm `0x8dfe44bde44499`. A second real incident — a cascade-
mislocalization case onto the stage1 TP pair (comm `0x864a163fd8b4f8`,
this project's own already-documented P21.6 phenomenon) — fired in the
same test, independently confirming the mechanism's known
mislocalization behavior is still real and still present.

Querying VictoriaMetrics directly for `agg_straggler_incident_tier`
scoped to either real comm returned **zero rows for both** — not a
query-scoping artifact (confirmed by also querying bare `{member=...}`
for each real PID, same empty result) and not a staleness-window race
(confirmed by retrying after a delay). The panel's new `incident_role`
column, its null-safety mappings, and its sort order were never actually
exercised by this test, because the data they read from was never
written in the first place.

Root cause, confirmed by reading the code, not inferred from the empty
query: `_timing_asymmetry_fallback_evaluate` (P27.2, the below-floor —
under `SELF_DETECTION_FLOOR=3` members — fallback covering TP2,
TP-inference, PP, and Hybrid's cross-comm cases) renders its finding
through `_emit_timing_fallback()` (`alerting/alert_engine.py:3296`), a
dedicated text formatter that appends to `self.alerts` and the summary
log but **never calls `_push_visibility_metric`** anywhere in its body.
This is a wholly different function from `_emit()` (line 4119), the only
place `agg_straggler_incident_detected`/`_severity_ratio`/`_persisted_s`/
`_tier` (and `incident_role`) are ever pushed. Both real call sites of
`_timing_asymmetry_fallback_evaluate` — the standalone trigger (line
3962) and PP's own cascade trigger (line 4426, the dependent-comm
propagation path) — go through `_emit_timing_fallback`, never `_emit()`.
There is no partial/conditional branch here; this entire fallback
mechanism structurally cannot reach any dashboard panel's data source.

This also explains something that had looked like it might just be
sampling luck: a full scan of `agg_straggler_incident_tier`'s real
history shows `incident_role="root_cause"` on exactly 7 distinct comms
(all single-rank self-test/above-floor runs) and `incident_role=
"downstream"` on **zero**, ever. That is not a gap in what's been
tested — it is exactly what this code path predicts. Every real incident
this metric has ever recorded came from the above-floor `_check_cv`/
`_check_mean` path; the below-floor fallback class has never, in this
project's lifetime, produced a single row here.

**This is a real, structural gap in panel COVERAGE, not a defect in any
panel's own display logic**, and it is separate in scope from the panel
edit that surfaced it. The sort/color/null-mapping behavior validated
earlier against real above-floor data (the single-rank self-test
incident, and an older multi-rank historical incident with
`incident_role` absent) remains correct for the data it is actually
given. Wiring `_emit_timing_fallback` into `_push_visibility_metric` is
a separate, nontrivial design question — does every below-floor alert
need the full `incident_role`/`is_likely_root_cause` computation, or just
basic tier/detected visibility; what's the right identity to key it on,
given this fallback's own per-comm 2-member shape — and deserves its own
investigation, not a quick patch bolted onto a dashboard commit; not
attempted here.

**Practical consequence for anyone operating this dashboard today: the
composed-incident-summary panel will show nothing at all for a real
TP2/TP-inference/PP/Hybrid below-floor incident.** For that fault class,
read the individual `[ALERT]`/P27.2 timing-fallback line directly — the
same guidance already given above for `_correlate_firing_timing_alerts`'s
own labeling gap, now extended: below-floor incidents are invisible to
this panel before any labeling question even arises.

**`node_aggregator_ref.py` can go completely dark — heartbeat and
scoring output, not just slow — under MoE's extreme comm/bucket
cardinality. This is a different, more severe failure mode than the
already-documented MoE/DLRM ring-buffer overflow above; do not fold the
two together.** The ring-buffer row is a bounded, known-loss condition
at the Inspector-plugin level (a `queue_drops_total` counter always
surfaces it). What's documented here is unbounded growth in the
aggregator's own Python heap, confirmed live, with no counter
surfacing it at all until the process goes silent.

Found while running a real MoE job-wide fault-injection validation
(`run_moe_rankfault.sh`, default `FAULT_TARGET_RANK=4`, 16 real ranks
across 2 nodes). Ground truth established first, as always: real target
= rank 4, PID 1343059, host worker-0. Within ~2 minutes of the job
starting, both `[PIPELINE-DOWN]` and `[VM-DOWN]` began firing
repeatedly. Investigated directly rather than assumed:

- Both real `node_aggregator_ref.py` processes were alive (`ps aux`
  confirmed), not crashed.
- Worker-0's own heartbeat AND its own real scoring counter
  (`agg_mean_windows_total`) were both completely absent from
  VictoriaMetrics — a genuine failure to push anything, not a logging
  quirk.
- RSS climbed continuously: 20GB → 23.6GB → 30GB → 31.6GB → 33GB over
  the job's ~19-minute life, **and kept climbing for 3+ minutes after
  the job was cancelled** (no new input at all) — ruling out "just
  catching up on live load." System memory headroom was never at risk
  (1.5TiB/node, ~78GiB used) — this is a correctness bug, not an
  imminent-OOM emergency.
- `agg_records_seen_total` vs. `agg_mean_windows_total`, sampled live at
  four points through the job's life: the records-consumed-per-window-
  closed ratio climbed from **766 → 857 → 2,037 → 1,741** — a widening
  gap, not a stable steady-state, confirming records were piling into
  unconsumed queues faster than those queues could drain.
- The checkpointed read-offset file (`.aggregator_offsets_checkpoint.
  json`) confirmed the real scale directly: **2.17GB of unread backlog**
  on worker-0 alone at the point of investigation, concentrated in the
  MoE job's own dump files — each rank's dump file was **~3GB** from a
  single 19-minute run (the real injected target's own file,
  `worker-0-pid1343059.log`, had 1.18GB of that still unread). No other
  shape tested this project has come close to this per-rank dump volume.

**Root cause, confirmed by reading the code and matching it to the live
counter/offset evidence above, not assumed from the symptom alone —
two independent, compounding accumulators, both keyed by comm/bucket
CARDINALITY, not event volume:**

1. **`RankBucketState.mean_unconsumed`/`cv_unconsumed`** (plain Python
   lists, appended on every record) have **no size cap at all**. The
   P22.5 scalability fix (the comment at the `all_vals`/`rate_samples`
   500-entry cap, `node_aggregator_ref.py` line ~1348) only ever touched
   those two SEPARATE lists — it never capped the unconsumed-queue pair.
   These only drain via `close_windows()`, which requires **every member
   currently known for that exact `(comm, bucket)` to simultaneously
   hold ≥ window_size unconsumed samples** before any window closes. MoE's
   dynamic, data-dependent expert routing makes that condition
   structurally unlikely to ever hold for many buckets — confirmed live:
   **218-220+ distinct `(bucket, coll)` combinations discovered on a
   single comm, still climbing when the job was cancelled** (every other
   shape tested this project stays in the single digits). For any bucket
   where even one member never catches up, every other member's queue
   for that bucket grows for the rest of the job's life, uncapped.
2. **`self.state` / `self.comm_bucket_members` / `calib.scored`** are
   keyed by `(comm, phys_id, bucket)` / `(comm, bucket)` with **no cap on
   the number of distinct keys** — only per-key list CONTENTS got partial
   capping (point 1's `all_vals`/`rate_samples`), never the key count
   itself. This grows directly with cardinality, independent of the
   unconsumed-queue issue, and compounds it.

**Recovery procedure, confirmed to work operationally — but it is a
mitigation, not a fix.** Checked first that no other session had a test
in flight (empty `PARALLEL_WORK_LOG.md`, empty `squeue` across all
users). Sent a clean `SIGTERM` to the leaf `node_aggregator_ref.py`
process on each node (never the supervisor, never `kill -9`) — confirmed
via the supervisor log: clean `exit_code=0` (the `_checkpoint_and_exit`
SIGTERM handler fired, flushing `file_offsets` before exit) and
relaunch within 3s on both nodes. Memory reset immediately (33GB→6GB /
29GB→6GB). **But this does not fix the underlying bug — confirmed
live**: worker-0 briefly reproduced the identical symptom (heartbeat
dark again, RSS climbing to 27GB) while the freshly-restarted process
caught up through its own still-large remaining backlog, because the
in-memory state resets on restart but the uncapped code paths and the
on-disk backlog driving them do not. Both nodes fully caught up (0 bytes
remaining, fresh sub-1s heartbeats) after roughly 10 minutes; `tools/
self_test.sh` confirmed the recovered pipeline clean afterward (exact
rank-match PASS, 0 new `[CHECK-FAILED]`).

**Scope, stated honestly: confirmed on MoE specifically, but not a
guaranteed MoE-only limitation.** The mechanism is comm/bucket
cardinality combined with uneven per-member routing — any future
workload shape that shares that combination (high distinct-bucket count
+ sparse/uneven per-member sample rates within a bucket) could trigger
the same structural growth. MoE is simply the only shape that has
exercised it so far; this is not proven safe for DLRM or any other
high-frequency shape, only unconfirmed for them.

**Update — Option A (the `mean_unconsumed`/`cv_unconsumed` cap) was
designed, implemented, and confirmed correct for its own target, but
validation found it does NOT resolve the real-world symptom: a second,
separate, likely-dominant unbounded path was found live during the same
validation pass.** Before implementing, real data from the triggering
job's own dump files (51M records, all 8 of worker-0's own ranks, parsed
directly with the aggregator's own `coarsen_msg_size()` logic) answered
the two open design questions with real numbers, not guesses:

- **Permanent stall or transient delay? Confirmed transient, not
  permanent.** All 202 real `(bucket, coll)` keys had every one of 8
  local members report **and** exceed both window thresholds
  (`MEAN_WINDOW=100`/`CV_WINDOW=125`) by job's end — lowest observed
  per-member-per-bucket count was 273. But the real per-bucket member
  skew reaches **~15x** (one real bucket: 208,996 records for the
  fastest member vs. 13,749 for the slowest, same ~1053s span). Summed
  across all 202 buckets, the fast-member-minus-slow-member gap alone —
  a real lower bound on peak unconsumed backlog — totals **~7.58M
  entries for one list, ~15.16M combined**, on worker-0 alone, in 19
  minutes. This confirmed a drop-oldest cap (not a force-close) was
  safe: since every member demonstrably does eventually catch up, a
  500-entry cap (identical to the existing `all_vals`/`rate_samples`
  precedent) leaves comfortably more than `window_size` real recent
  samples for every member at all times.
- **Implemented**: the identical 500-entry drop-oldest cap, applied to
  both `mean_unconsumed` and `cv_unconsumed` right where they're
  appended (`node_aggregator_ref.py`, in `handle_record()`, immediately
  before the existing `all_vals` cap). Reduces this specific mechanism's
  real measured contribution from the ~15M-entry lower bound above to
  roughly 65MB (1,616 real `(comm,member,bucket)` keys × 500 × 2 lists ×
  ~40 bytes/entry) — confirmed by the same arithmetic used to size the
  original problem, not a new estimate.

**Re-validated live, same scenario, same fault mechanism (ground truth
established from dump files before the job even finished, this time) —
and the aggregator still went dark, worse than before:** both nodes'
heartbeats went **continuously** dark for 15+ minutes straight (not
intermittently, as in the original incident), and memory climbed past
the original run's own peak (worker-1 reached 43GB vs. the original
34GB). The cap is real and does what it was designed to do — but it is
not sufficient, because it was not the dominant mechanism after all.

**Second real cause found, previously missed in the original
investigation: `push_buf` is unbounded during a single large catch-up
cycle, and `poll_files()` has no practical per-call size limit despite
accepting a `deadline` parameter.** `push_buf` (the pending-metric-push
list) gets a new entry on every record processed (`agg_samples_seen`,
appended unconditionally in `handle_record()`), but is only flushed
once per outer `run()` loop iteration, **after** `poll_files()` returns.
`poll_files()`'s own internal deadline check
(`node_aggregator_ref.py` line ~1236, `DEADLINE_CHECK_EVERY=2000`) only
compares against the run's overall multi-year `--duration` deadline —
in practice that is never true, so a single `poll_files()` call
processes an **entire available backlog, across every file, in one
unbroken pass**, with no flush and no heartbeat push until it fully
returns. Live evidence from the re-validation: a fresh real `push
error... Connection refused` appeared in the log exactly matching the
original incident's own symptom, and `agg_pushes_total`/
`agg_records_seen_total` both read as having zero fresh samples
(`N/A`) while the process was confirmed alive and busy (`ps` showed `R`
state, climbing RSS) — consistent with a single still-in-flight
`poll_files()` call that had not yet returned to flush anything.
**This is a more complete explanation for the heartbeat/scoring
blackout than the original Step 1-4 investigation identified** — that
investigation correctly found a real bug (confirmed, now fixed) but did
not examine `push_buf`, which appears to be the larger contributor to
the actual blackout symptom specifically (as opposed to raw RSS growth,
where both mechanisms contribute).

**Recovery for this re-validation run needed an extra step beyond the
established SIGTERM-the-leaf-process procedure.** Because the run lasted
~39 minutes (over 2x the original) before being cancelled, its own dump
files had grown to ~6.3GB/rank (~50GB/node total) by the time of
cancellation — a restart alone would have forced the fresh process to
replay that entire backlog before recovering, plausibly taking far
longer than the original incident's ~10-minute recovery. Since this
specific run's own dump files had already yielded everything needed
(ground truth, the push_buf finding) and were confirmed to serve no
further purpose, they were deleted directly rather than waited out —
both aggregators recovered to fresh sub-1s heartbeats and flat memory
within ~4 minutes of that cleanup. `tools/self_test.sh`-equivalent
health checks (heartbeat, `CHECK-FAILED` delta, queue state) confirmed
the pipeline healthy afterward; `CHECK-FAILED` count unchanged (117).

**Detection question: unresolved, not negative — investigated, not
assumed.** No `[ALERT]`/`[STRAGGLER-INCIDENT]` ever appeared for the
real injected target (rank 4) in either the original 19-minute run or
this 39-minute re-validation — zero matches in the alert log, both
times. This is **not** evidence that MoE's job-wide detection itself is
broken: both aggregators were confirmed dark (no heartbeat, no scoring
metrics reaching VictoriaMetrics, which `alert_engine.py` polls
exclusively) for the large majority of both runs' real duration. There
was no sustained window in either test where the aggregator ran in a
genuinely healthy, live-paced state (not still catching up on stale
backlog) for long enough to give the normal per-bucket mean/CV detection
path a fair chance to fire. **This question remains open and blocked on
fixing `push_buf`/`poll_files()` first** — it cannot be answered by
retrying the same test again without that fix, since the same blackout
would very likely recur and confound the result the same way twice now.

**Option B (`self.state`/`comm_bucket_members`/`calib.scored` key-count
cap): still deferred, now grounded in a real growth-rate measurement,
not just "wasn't dominant in a 19-minute test."** Direct analysis of
exactly when each of the 202 real bucket identities first appeared in
the dump files: **192/202 (95%) appeared within the first 0.38 seconds**
of the job, and all 202 were present by 38 seconds — a 19-minute (and,
in the re-validation, 39-minute) job's full bucket vocabulary is fixed
almost immediately, not discovered progressively over the job's life.
The continuous "calibrated new bucket" log lines seen throughout a run
reflect the **calibration threshold** (10 occurrences) being crossed
late for rare buckets, not new bucket **identities** appearing late.
**Reproduced exactly in the re-validation run: 202 distinct buckets
again**, on a different real comm — strong evidence this is a fixed
property of this model/config's own routing-size vocabulary, not an
open-ended or run-to-run-variable quantity. Projected forward: a
realistic multi-hour production MoE job on this same model/config would
plausibly retain essentially the **same** ~1,616-key count (worker-0)
observed in a 19-minute run, not scale up with duration — key-count
overhead, even fully uncapped, stays in the low tens of MB, genuinely
negligible next to the GB-to-tens-of-GB scale of the `push_buf`/
unconsumed-queue mechanisms. **Caveat, stated honestly**: this
conclusion is specific to a workload whose routing-size vocabulary is
itself bounded by the model/config, as observed here twice; a
hypothetical MoE config with a genuinely unbounded or continuously-novel
message-size space was not tested and could behave differently. Design
for eviction (LRU by last-seen real `dump_timestamp_us`, tie-broken
toward evicting the lower-lifetime-occurrence candidate among idle
ones, to distinguish "abandoned" from "rare but real and recurring" as
best as an online heuristic can) remains recorded here for whoever picks
this up, but is **not next in priority** — fixing `push_buf`/
`poll_files()` is the higher-priority follow-up, since it is both the
likely-dominant real cause and the blocker on ever answering the
detection question above.

**Update — the `push_buf`/`poll_files()` fix was designed, implemented,
and validated live over a full 48-minute MoE run. The heartbeat/memory
blackout is resolved, confirmed; a separate, narrower, genuinely open
detection-timing question was found in the process of validating it.**

Root cause, confirmed precisely rather than assumed: `poll_files()`'s
own deadline check (`node_aggregator_ref.py`, inside the per-line loop)
compared against `run()`'s own `t_end = time.time() + duration_s` — and
`duration_s` is always the supervisor's `--duration 315360000`
(~10 years). That comparison is not "rarely true," it is **unreachable
by any single real call**, so a `poll_files()` call facing a real
backlog ran every available byte across every file in one unbroken
pass, with zero opportunity for `flush()` (the only place anything in
`push_buf` — including every real detection metric `alert_engine.py`
reads, not just the heartbeat display value — reaches VictoriaMetrics)
to run until it fully returned. A second gap: the only check that
existed lived inside the *inner* per-line loop of whichever file was
currently open; the *outer* per-file loop had no check at all, so even
a working inner bound wouldn't have stopped the call from then opening
and fully reading the next file's own backlog too.

**Fixed** with `POLL_FILES_MAX_SECONDS = 5.0`, a short per-call budget
computed fresh at the start of every `poll_files()` call
(`effective_deadline = min(deadline, time.time() + POLL_FILES_MAX_
SECONDS)`), checked at the top of the outer per-file loop (new) and in
place of the old inner-loop comparison (existing cadence, corrected
target). 5.0s is grounded, not invented: half of the already-real
`OFFSET_CHECKPOINT_INTERVAL_S` (10.0), with ~18x margin under
`pipeline_health.py`'s real `HEARTBEAT_STALE_THRESH_S=90.0` even
accounting for `flush()`'s own worst-case 10s network timeout landing
immediately after. `run()`'s existing loop structure needed no changes
— `maybe_heartbeat()` → `maybe_checkpoint_offsets()` → `flush()` already
runs right after every `poll_files()` return, so a capped return simply
means that sequence now happens every ~5s during a backlog instead of
once after 15+ minutes; the backlog itself is processed identically,
just across more, shorter calls, with offsets already correctly tracked
incrementally even on a partial-file break (confirmed from the existing
code, unchanged by this fix).

**Validated live, same real scenario, ground truth established before
checking any alert, same discipline as every prior attempt — run
extended to 48 minutes (over 2x either prior attempt) specifically to
give detection a fair, uninterrupted shot:**
- **Heartbeat: zero gaps, the entire 48 minutes.** Sampled every minute;
  age never exceeded ~1s, start to finish — categorically different
  from the 15+ continuous minutes of blackout immediately before this
  fix, on the identical test.
- **Memory: flat and bounded, sampled continuously, not assumed from
  Option A's own orthogonality.** RSS bounced between ~1-7GB (worker-0)
  and ~1-11GB (worker-1, one transient spike that receded the very next
  sample) for the full 48 minutes — no sustained upward trend, a
  categorically different shape from the prior monotonic climb to
  33-43GB. Confirms Option A continues to hold with this fix layered on
  top.
- **Detection: still did not fire — but now for a real, separately
  confirmed reason, not infrastructure failure. Not a confirmed miss.**
  `agg_mean_windows_total` on worker-0 crawled from 251 to 264 over the
  final ~20 minutes of observation (real, continuing forward progress,
  not stalled) while `agg_mean_z_worst`/`agg_cv_z_worst` (the actual
  values `alert_engine.py` acts on) never produced a single sample
  despite 64.4M records processed. This is consistent with, not
  contradicted by, the real per-bucket member-arrival skew already
  measured directly from this workload's own dump files during the
  design phase (**up to ~15x between the fastest and slowest member
  reporting the identical bucket**) — this fix stopped that skew from
  consuming unbounded memory, it does not make a structurally slow
  member's own arrival rate for a given bucket any faster. Getting even
  one scored window for whichever bucket(s) are relevant to this fault
  may simply take substantially longer under MoE's routing pattern than
  on any other shape validated in this project, independent of both
  infrastructure bugs just fixed.

**This is now the one remaining open item before MoE can be considered
fully validated.** Everything else about MoE is closed: job-wide
detection's own documented scope (permanent non-localization, README
§7.1), the Inspector-plugin ring-buffer capacity limitation (bounded,
known loss, section above), and both real aggregator infrastructure
bugs found and fixed this investigation (`mean_unconsumed`/
`cv_unconsumed`'s cap, and this `push_buf`/`poll_files()` fix). Whether
the detection-timing gap needs its own fix (e.g., adjusting
`close_windows()`'s all-members-simultaneously gate to tolerate
sustained, structural per-member imbalance rather than only transient
noise) or simply needs a much longer soak test to confirm eventual
firing is a genuinely open design question — not decided here, not
guessed at, left for whoever picks this up next with the real evidence
above as the starting point.

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
| `RECENT_TIMING_CORRELATION_WINDOW_S` | 60.0 (`DCGM_FALLBACK_CHECK_INTERVAL_S * 3`) | alert_engine.py:283 -- **open calibration question, not yet resolved**: a real test (Hybrid job 3867, §5) observed 225s of real propagation delay between two genuinely-related alerts, well past this window, so the third related alert never joined the group. Not resized -- flagged for whoever next revisits this constant |
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
