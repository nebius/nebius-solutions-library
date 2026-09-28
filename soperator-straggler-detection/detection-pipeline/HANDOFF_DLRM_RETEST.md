# Handoff: DLRM throughput-wiring fix — extended re-test

You are starting fresh. This document is the complete context you need —
do not re-derive anything below from scratch; confirm/extend, don't
re-discover. This is a **re-test of an already-implemented fix**, not a
new investigation — do not re-investigate root cause, do not re-read
`node_aggregator_ref.py` looking for the bug. It's found and fixed. Your
job is to strengthen confidence in the fix with more independent runs.

## What was already fixed and validated (do not redo this part)

**Root cause (already confirmed, do not re-derive)**: `node_aggregator_
ref.py`'s `_throughput_rate_ref` (the reference
`agg_job_throughput_ratio_to_baseline` divides by, feeding the
`uniform_slowdown` CONFIRMED/PAGE detector) was a per-aggregator-
**process**-lifetime value — established once by whichever job first
reached stabilization, and never reset across job boundaries. Since
this project's aggregators are long-lived processes watching the same
dump directory across many different jobs, this meant every job after
the first got compared against a stale, unrelated reference from a
completely different, earlier workload. Confirmed with real numbers:
DLRM's own real raw event rate read ~2.0-2.3x an earlier, unrelated
job's stale ~110/sec reference (masking the fault entirely — the ratio
never came close to the 0.5 floor); the same bug's other face produced
a real false `CONFIRMED/PAGE` `uniform_slowdown` alert elsewhere at
0.13-0.47x against that same wrong reference.

**The fix (already implemented, in the current commit on this branch,
`aggregator/node_aggregator_ref.py`)**: added `self._throughput_job_id`
tracking (initialized in `__init__`, alongside the other
`_throughput_*` attributes) and a real job-boundary check at the top of
`maybe_check_job_throughput` — when `self.slurm_job_id` (already kept
live by the existing `refresh_job_id()`, called every `poll_files()`
cycle) genuinely changes to a new, non-empty, non-`"unknown"` value,
every throughput-tracking attribute (`_throughput_rate_ref`,
`_throughput_last_records`, `_throughput_last_ts_us`,
`_throughput_last_live_denom`, `_throughput_denom_stable_checks`,
`_throughput_total_checks`) resets, so the new job gets its own fresh
calibration attempt exactly like a brand-new aggregator process would.

**Validation already done (do not redo)**: confirmed live across 4
consecutive real jobs run back-to-back after restarting both
aggregators — two healthy DLRM baseline runs, one faulted DLRM run,
then one nanoGPT regression run. Each established its own fresh,
job-appropriate self-calibrated reference (4704.43/sec, 5048.64/sec,
240.75/sec, and 110.07/sec respectively — correctly tracking each job's
own wildly different real natural rate) with **zero cross-
contamination** between jobs, confirmed via the aggregator's own
`"throughput tracking reset: new job <id> (previous reference was
established for '<old id>')"` log line appearing correctly at each
transition. The nanoGPT run additionally confirmed per-rank fault
detection is completely unaffected (exact rank/PID match, real alert
fired correctly) and that no new false `uniform_slowdown` alert
appeared (count stayed at the same 4 pre-fix false positives, all
predating the restart).

## What this re-test needs to do

The validation above is real but was a **single pass** through each
scenario. Strengthen confidence with genuinely independent repeats:

1. **Re-run DLRM's real fault-injection case (`STRAGGLER_SLEEP_MS=200`,
   `STRAGGLER_TARGET_RANKS=5` or another real target rank) n>=3-5 MORE
   independent times**, each a fresh, separate Slurm job. For each run:
   - Establish real ground truth first (target rank's real PID, read
     directly from its own dump file's `header.rank`/`metadata.pid`,
     before checking any alert).
   - Confirm the job-boundary reset fires correctly (check the
     aggregator's own log for the `"throughput tracking reset: new
     job..."` line naming the correct, new job id).
   - Confirm the resulting `agg_job_throughput_ratio_to_baseline` is
     being computed against a reference that is actually this job's
     own (not a stale one) — query it directly via VM's
     `query_range` API across the run's real time window, the same way
     the original validation did.
   - Record whether `uniform_slowdown` fires or not, and why (if it
     doesn't: is it because the ratio genuinely never dropped below
     floor, or because self-calibration was used and is blind to an
     always-on fault — see "explicitly NOT in scope" below for why this
     second reason is expected and not a new problem to solve here).

2. **One more independent regression check**, reusing nanoGPT or
   picking a different already-working shape (e.g., FSDP, TP4, RL — see
   `README.md`'s "Testing/validation reference" section for exact
   launch commands). Confirm real, correctly-attributed per-rank
   detection still fires, and confirm no new false `uniform_slowdown`
   alert appears during a genuinely healthy portion of that run.

3. Report the aggregate result: how many of the n>=3-5 extra DLRM runs
   behaved consistently with the single validated pass (correct reset,
   correct job-appropriate reference, no cross-contamination), and
   whether the regression check held across this additional run too.

## Explicitly NOT in scope for this re-test

**DLRM's separate, deeper, already-identified-but-unfixed bug remains
out of scope.** Do not attempt to fix or re-investigate it here; just be
aware it exists so you don't mistake its symptoms for a regression in
the fix you're re-testing:

`comm_calib` and `comm_bucket_members` (the dicts `workload_signature()`
reads to build each job's cross-job-matching "sig") are **also** never
reset per job in a long-lived aggregator — confirmed live: DLRM's own
sig's `n_comms` climbed 53 -> 54 -> 55 -> 56 across consecutive,
unrelated jobs, because these dicts accumulate every comm/bucket/
collective type the aggregator process has EVER seen, not just the
current job's. This means two runs of the exact same workload will
essentially never produce a matching sig in a long-lived aggregator, so
`query_throughput_history`'s cross-job lookup (meant to let a job use a
genuinely healthy cross-job reference instead of self-calibrating on its
own, possibly-always-faulted, rate) will likely keep reporting "0
historical run(s) found" even after several of your own extra DLRM runs
push their own references. **This is expected, already-documented, and
not something to fix in this session.** If your extra DLRM fault runs
still don't trigger `uniform_slowdown`, and self-calibration (not
cross-job history) is what got used, that is the ALREADY-DISCLOSED
"self-calibration is blind to a fault present since job launch" gap
(same as MoE's own documented case) plus this signature-scoping bug —
not a failure of the fix you're re-testing. Document it as such, don't
chase it further. It's a real, precisely-scoped, separate proposal
(rescoping `comm_calib`/`comm_bucket_members` to a real job boundary —
used extensively throughout `node_aggregator_ref.py` for calibration
and bucket-discovery, so real, wider-reaching, riskier design work) for
a future session, already written up in `README.md`.

## Shared-process safety rule (read before touching any live process)

**Before restarting `alert_engine.py` or any `node_aggregator_ref.py`,
confirm no other session (PP's fix, Hybrid's fix) has a test actively in
flight.** These sessions may run in parallel against the same shared
cluster. Check `squeue -u "$USER" -h` and the aggregator/alert_engine
supervisor process states before restarting anything, and coordinate if
another session's job is running. You likely do NOT need to restart
either aggregator process for this re-test (the fix is already live in
the running process from the prior validation, unless something else
has restarted it since) — check current process start times against
`aggregator/node_aggregator_ref.py`'s mtime before assuming a restart is
needed.

## Standing project rules (carry forward, do not relax)

- **n>=5 independent validation** for any new detection signal or
  threshold before trusting it (this is the specific rule this re-test
  exists to satisfy more fully).
- **Explicit rank/PID-match verification** before trusting any
  detection result — establish real ground truth (via each dump file's
  own `header.rank`/`metadata.pid`, read directly, before checking any
  alert) before looking at what fired.
- **Health precheck before/after every live step**: VM 200
  (`curl http://worker-0:8428/-/healthy`), alert engine supervisor alive
  (`ps -ef | grep alert_engine.py`), both aggregators alive, Grafana 401
  on unauthenticated `/api/org` (proves auth still enforced),
  `CHECK-FAILED` count unchanged in `var/alert_engine_supervised.log`.
- **Work only in `add/straggler-detection-v1-beta`** — never touch
  `main`, never modify existing repo content outside this contribution.
- Do not force an incomplete conclusion — if the extra runs surface a
  real inconsistency in the fix, report it precisely rather than
  smoothing over it to reach a clean "n>=5" result.
