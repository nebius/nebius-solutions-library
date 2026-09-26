# Testing methodology — mandatory pre-flight steps

## Pre-flight duration check (mandatory, first step of any new-workload validation)

**Rule:** before interpreting a "zero alerts" result from any new-workload
fault-injection test as meaningful, run `preflight_duration_check.py`
against the workload's own real, measured collective cadence. A test
that ran shorter than the required duration will show zero alerts
regardless of whether a real fault was present — this is not a
detection gap, it is an uninterpretable result.

```
python3 /root/P20c_alerting/preflight_duration_check.py <planned_duration_s> <real_cadence_per_sec> [label]
```

Get `real_cadence_per_sec` from the aggregator's own real, already-printed
`measured cadence ... rate=X.XX/sec` line for the workload's primary
bucket (a real run, even a short smoke test, produces this) — never
guess it.

**Why this exists:** found three separate times, each only by manual
investigation after the fact — PP's forward-only false alarm, the
checkpoint-fault's original attempt, and RL's first training-phase
attempt all initially showed "zero alerts" on a first test. Retroactive
investigation found only RL's case was genuinely explained by this
mechanism (`node_aggregator_ref.py`'s `score_mean_window()` gates
`fired` on `BUCKET_MATURITY_GRACE_S`, independent of signal magnitude).
PP's case never even reaches that gate (its 2-member comm exits at the
`members_here < 3` check first — a different mechanism, the P27.2
below-floor fallback, with its own separate timing requirement). The
checkpoint case's original failure was a wrong-target-rank bug, unrelated
to timing entirely. **This check catches the RL-shaped failure mode. It
does not substitute for verifying rank/topology assumptions (see the
checkpoint-fault lesson) or below-floor-topology behavior (see PP) — run
all three checks, they catch different things.**

**Formula** (reuses `CALIB_MIN_TOTAL_SAMPLES`, `BUCKET_MATURITY_GRACE_S`,
`CV_WINDOW` directly from `node_aggregator_ref.py` — no second, parallel
timing mechanism):

```
minimum_duration_s = (CALIB_MIN_TOTAL_SAMPLES / rate) + BUCKET_MATURITY_GRACE_S + (CV_WINDOW / rate)
```

This is a deliberately conservative (per-member, not aggregate-across-
members) estimate of the calibration term — validated against one real
case where calibration completed faster than the formula assumes
(aggregate arrival rate across multiple real ranks, not modeled here),
so the real transition happened ~30s before the formula's predicted
minimum. The formula errs safe (recommends waiting longer than strictly
necessary), never the other direction, in the one case checked. Treat it
as a floor, not an exact prediction.

## Rank/role verification (mandatory, before trusting any fault-injection target)

**Rule:** explicitly confirm which rank/process performs the role a
fault test targets, from real evidence (a print statement, a log line),
before launching the test — never assume a new workload's rank
assignment mirrors a prior workload's. (The checkpoint-fault session:
targeted rank 5 for a fault that only rank 0/`master_process` could ever
reach — the test's "zero alerts" was really "the fault never fired.")

## Production coverage signal

`agg_detection_coverage_achieved{comm=...,bucket=...,coll=...}` — pushed
live by `node_aggregator_ref.py`'s `score_mean_window()`, reusing the
exact same `past_grace` gate above (not a second mechanism). 0 while a
job hasn't yet had a fair chance for detection to fire; 1 once it has.
Answers "silence is ambiguous between healthy and never-checked" — a
short customer job correctly reads as `0` (not yet covered), not
silently indistinguishable from a long, genuinely-healthy one.
