# Design notes — investigation history, mechanism design, and closed findings

This file holds the "how it was found, and why it was built this way"
narratives that used to live inside `README.md`. `README.md` itself keeps
only what you need to install, run, and operate the pipeline today,
plus the limitations that are still genuinely open. Everything here is
historical/design-rationale detail for anyone going deeper on a specific
mechanism — real numbers, real validation runs, real root causes, not
paraphrased.

Nothing in this file was deleted or shortened from its original
README.md text — it was moved and regrouped by topic (not by the order
it was originally discovered/written), with only cross-reference section
numbers corrected to match the new layout. See `README.md`'s own
`## 7. Known limitations` for the current, trimmed, still-open items;
each one that has a fuller history here is pointed at directly.

## Contents

1. Detection mechanism design and discovery
2. The below-floor (2-member) fallback saga
3. Infrastructure watchdogs — build-out and validation
4. Observability tooling
5. Performance and overhead investigation

---

## 1. Detection mechanism design and discovery

### 1.1 `straggler_incident_detected` — counting stragglers without needing a cause confirmed

Everything answered by `[ALERT] ... confidence=CONFIRMED/PROBABLE/
UNCONFIRMED` (see README §6.4) is "what caused this, and how confident
are we in the cause?" — a **separate** question from "did a real,
persistent, impactful straggler just happen, regardless of whether we
ever find out why?" Before this signal existed, those two questions were
conflated: a genuinely sustained, high-impact software-only straggler
(no DCGM/storage signature to corroborate it — e.g. a pure scheduling
delay) could never rise above `PROBABLE`, a tier this project's own
measured data says **carries the run's entire false-positive rate
(22-25/hour)** — making it useless for reliably counting real incidents
or estimating lost compute time, even though the underlying timing
anomaly was completely real and sustained.

`straggler_incident_detected` decouples the two. It fires on:
- **Persistence**: the SAME `T.PERSIST_REQUIRED=3`-consecutive-window
  rule CV detection already used (now applied uniformly to mean and
  outlier_count too — see README §7's note on the resulting tightening
  of mean-path alerting).
- **Impact**: `mm > T.MEAN_MM_THRESH` (2.0x) — this project's own
  already-calibrated "2x slower than peers is real, not noise" floor,
  applied the same way regardless of which statistic (cv/mean)
  triggered it.

— **entirely independent of cause-tier**. Whether DCGM/storage ever
corroborates a hardware cause is looked up and attached *afterward*, as
a secondary annotation on the finding, never a gate on whether this
signal fires at all.

**Where it shows up** — additive to, never replacing, the existing
`[ALERT]` output:
- A `[STRAGGLER-INCIDENT] stat=... host=... member=... gpu_slot=...
  severity_ratio=... persisted_s=...` line in both
  `var/alert_engine_supervised.log` and `var/alert_summary.log`,
  alongside (not instead of) the finding's own `[ALERT]` block.
- Two new VictoriaMetrics metrics: `agg_straggler_incident_detected`
  (1, pushed once per onset, same convention as `agg_mean_fired`) and
  `agg_straggler_incident_severity_ratio` (the real measured ratio —
  `mm` for a mean-sourced finding, `z` for a cv-sourced one — so
  incidents can be ranked by severity directly off the metric's own
  value, rather than binned into another tier).
- A new Grafana panel ("Straggler incident detected") on the
  straggler-detection-metrics dashboard.

`persisted_s` is the real, measured elapsed wall-clock seconds between
the first and last of the 3 qualifying windows (not a sample-count
proxy) — empirically validated against raw NCCL Inspector dump
timestamps during this feature's own implementation.

**Lost compute-time estimation is deliberately NOT built here.** The
already-existing `agg_job_throughput_ratio_to_baseline`/
`agg_job_throughput_rate_ref` (a job's own measured throughput vs. its
own historical baseline) is the right signal for that — treat a
`straggler_incident_detected` event as a likely contributing cause to
whatever shortfall that signal already shows, rather than deriving a
second, possibly-disagreeing number bottom-up from individual
collectives' excess exec time (which would double-count overlapping
communicators and miss non-collective-bound slowdown).

### 1.2 Trusting what an alert shows you — the z/mm staleness fix, cross-reference IDs, and evidence panels

Investigating item 2 (making flag evidence inspectable) found a
real, separate bug: **the z/mm value displayed in an alert's own text
could be stale** — a real number, but from a different, later moment
than the one that actually satisfied the firing condition. Root cause:
the mean-path check read the firing decision from `agg_mean_fired` (a
widened lookback query, so a transient window isn't missed between
polls) but read the displayed z/mm from a separate, unwidened "whatever
is currently latest" query — and node_aggregator_ref.py keeps closing
new windows continuously, so by the time this engine polled, the two
could legitimately disagree. Confirmed live: one alert displayed
`z=2.7`; the real window that actually fired showed `z=34.1, mm=3.54`.

**Fixed for both cv and mean, both ultimately via VictoriaMetrics'
`/api/v1/export`** (not `query_range`, which grid-resamples onto fixed
step points and can silently skip a real sample at this project's own
window-close cadence):
- **CV**: `_cv_maxmed`'s own exec-time query now reads the real sample
  closest to the exact real firing timestamp via `/api/v1/export`. A
  PromQL `@`-pinned plain instant query was tried first and rejected —
  caught live, before shipping: it suffers the identical ~30s
  visibility floor described below, and since the firing timestamp is
  by construction always very recent, it would have returned no data
  (blank mm/worst_val) on nearly every real CV-triggered alert.
- **Mean**: the firing decision no longer reads `agg_mean_fired` at
  all. It reads every real raw `agg_mean_z_worst`/`agg_mean_mm_worst`/
  `agg_detection_coverage_achieved` sample in the lookback window via
  `/api/v1/export`, and correlates all three by their exact shared real
  timestamp (all three are pushed together, in the same
  `score_mean_window()` call) to reconstruct precisely the same
  `past_grace and z>30 and mm>2.0` decision node_aggregator_ref.py's own
  code already computes — a pure refactor of *where* the decision is
  read from, never a change to the decision itself.

**Latency, precisely quantified — not just "may fire slightly
earlier."** Verified side by side against the old `agg_mean_fired`-based
decision across two real fault-injection validation runs: every
mismatch (45 total) was the same direction — the new logic qualifying
when the old logic hadn't yet, never the reverse — and in every traced
case the underlying data was real and already genuinely qualifying.
Root cause, measured directly (5 independent controlled trials against
this exact VictoriaMetrics instance): a consistent **~30.2–30.4s delay**
before any freshly-written sample becomes visible via a plain instant
query (`/api/v1/query`) — confirmed to apply to every write, not just
first-ever label combinations (a repeat write to an already-visible
series showed the identical delay) — consistent with this project's own
prior, independently-measured "~30-39s baseline visibility floor" for
this same instance. `/api/v1/export` is not subject to this floor.

**Alerts may fire up to ~30 seconds earlier than the previous
implementation**, in cases where a label combination's visibility on
the old instant-query path was still inside VictoriaMetrics' own ~30s
query-visibility floor. This is strictly a latency improvement — the
new logic never fires on incorrect or less-true data, and never fires
later than the old logic would have. The ~30s bound does not compound
across the 3 required persistence windows: it is a floor on how
recently-written data becomes visible to a query issued "now," not a
per-window penalty — by the time the third (gating) window closes, the
first two are, in every real case observed, already past that floor.

**Cross-reference ID**: when `straggler_incident_detected` also fires
for the same event, both its `[STRAGGLER-INCIDENT]` line and the
corresponding `[ALERT]` line now carry a shared `incident_id=host:comm:
member:bucket:coll` field, so you (or a script) can confirm they refer
to the same real event without inferring it from log adjacency.

**Evidence panels** (push identifiers, pull trajectory — the design
decision: bundling the full trajectory into the alert text itself would
mean re-deriving data already in VictoriaMetrics and needing to parse it
back out of log text; leaving it purely in VM with no guided path would
mean hand-writing PromQL every time): the alert text and
`[STRAGGLER-INCIDENT]` line already push the identifiers (`comm`,
`member`, `bucket`, `coll`, `incident_id`) you need. Two dashboard
variables, **Comm** and **Member (PID)**, let you paste those straight
in, filtering two panels:
- **"Flag evidence trajectory"** — the real `agg_mean_z_worst`/
  `agg_mean_mm_worst` history around the event, not just the single
  value the alert text shows.
- **"Peer timing comparison"** — every member of the same communicator's
  own real exec time at the same moment, the same data this project's
  own cascade/"LOCATION UNCERTAIN" investigations already rely on by
  hand, one filter away instead of hand-written PromQL.

No new metrics collection for either panel — both read series that were
already being pushed.

### 1.3 Worst-selection sign fix, and the direct-impact lost-compute-time estimate

**Real bug fixed**: `node_aggregator_ref.py`'s mean-path "worst" selection
used `abs(means[p] - median)` — flagging whoever deviates most from the
median in *either* direction, never checking whether that deviation means
genuinely slower (a real straggler) or simply faster (not a straggler at
all). Confirmed directly against a real, healthy 4-stage Megatron
TP4/PP4/DP3 baseline: a specific comm-local role position was consistently
the *fastest* member of its own TP group in every one of the 4 pipeline
stages, yet got selected as "worst" 40-86% of the time in genuinely healthy
windows purely for being a large, consistent outlier in the wrong
direction. Fixed by switching to signed deviation (`means[p] - median`) —
a straggler detector must never flag a rank for being faster than its
peers. Validated both directions against real data: the false-positive
rate for that role position dropped from the 40-86% range down to under
7% (consistent with ordinary noise, not a structural bias), while the
genuine fault's own relative visibility *increased* once it was no longer
competing against a spurious fast-outlier for the "worst" slot.

**`straggler_incident_detected`'s direct-impact estimate** (item-4
Part B, root-only scope): for a CONFIRMED finding that is the *only*
CONFIRMED finding for its job within a recent window (reusing the
already-computed tier as the root-cause signal — no new detection
mechanism), computes a real **GPU-seconds** figure for that root's own
direct peer group: the sum, over every real sample in the incident's own
measured window (`duration_s`), of each peer's real exec time above *its
own* cross-job healthy baseline (`_member_role_baseline`, already
existing). This is deliberately the corrected version of the natural
"straggler's own exec time vs. peers" idea — real data showed a
late-arriving straggler's own reading looks *short*, not elevated (it
arrives late; the collective completes quickly once it finally joins) —
the real signal is in the *waiting peers'* elevation, not the root's own
number.

**Explicitly root-only, by design**: this does not trace or attribute
impact to any downstream rank/stage that doesn't share a physical member
with the root's own communicator. A real cascade may still fire its own
separate `PROBABLE` alerts, exactly as it does today — those are never
folded into this number. Reliably separating "root" from "cascade" across
non-shared-member stages was investigated and found infeasible with
what's currently available; this is intentionally the honest, simpler
fallback, not a shortcut.

**Reported in GPU-seconds, not dollars.** Converting to a dollar/GPU-hour
figure needs the job's own expected total runtime/iteration count, which
isn't captured anywhere today — left as an open, undocumented-elsewhere
gap, not estimated here.

> The permanent caveat that travels with this number everywhere it's
> shown (log text, dashboard panel) is quoted in README §7 — read it
> before trusting a small/zero value from this estimate.

Exposed as `agg_direct_impact_gpu_seconds` (pushed via the same
`push_buf`/VM-ingest path every other metric already uses — no new
collection) and a `[DIRECT-IMPACT-ESTIMATE]` line in both log files,
carrying the caveat text inline every time it fires, not as a one-time
footnote.

### 1.4 Path C's real incident window, and its window-overlap-strength display

Two real, narrow fixes to Path C (storage, eBPF block-I/O-wait), scoped
deliberately to this one cause-path — the only one with a real, queryable
incident window right now (see README §7's Path B/fabric entry for why
the other DCGM-sourced paths don't yet have this).

**Real incident window, not a fixed 10s guess.** The live alert path
(`alert_engine.py`'s `_emit()` → `build_finding_for_alert`) used to query
Path C over `[anomaly_ts - IOWAIT_LIVE_WINDOW_S, now]` — a fixed 10s
lookback regardless of how long the incident had actually been running.
For a brief fault this was a reasonable guess; for a longer, genuinely
sustained one it could miss real io-wait evidence from everything before
the last 10 seconds. `_emit()` already computes the incident's own real,
measured duration (`persist_duration_s` — the same value
`[STRAGGLER-INCIDENT]`'s own `persisted_s=` reports) via its persistence
gate, so this is now threaded through: `incident_start = anomaly_ts -
persist_duration_s`, strictly a widening of the old window (never
narrower), falling back to the old fixed-10s behavior only on a call path
that genuinely doesn't have `persist_duration_s` yet.

**Window-overlap-strength, informational only.** Every Path C reading now
also reports how much of the incident's own real duration the eBPF
agent's log actually has coverage for — e.g. "iowait evidence present for
27.4 of 31.2 real seconds in this incident's actual duration" — distinct
from whether the target process's own io-wait crossed the fault
threshold (that verdict is unchanged). This answers "did we have log
coverage for this window at all", separate from "was this rank
io-bound". **This number is purely informational and is never read by
any tier-decision logic** — confirmed by direct trace, not assumption:
`storage_evidence.determine_storage_path()` (the only function that
decides Path C's True/False/None verdict) reads exactly two keys,
`target_iowait_us` and `window_s`, both unchanged; `determine_confirmed_
path()` only ever calls that function and never touches the evidence
dict's fields itself. A grep-verified full list of every place this
project reads a Path C evidence dict's fields turns up exactly: the two
decision reads just named, and five display-text reads — nothing else,
anywhere in the codebase.

### 1.5 Wait-induced stragglers — a real, total blind spot, found and closed

**CLOSED.** A real, severe fault class previously produced ZERO alert at
any tier, in any communicator with 3+ members. Found via direct
investigation, not assumed: a real injected fault whose own timing reads
LOW (it caused every other member of its communicator to wait on it,
while it itself arrives late and exits fast — the inverse of a normal
compute straggler's signature) produced a massive, real, directly-measured
timing elevation in `agg_mean_exec_time_us` the entire time, and still
fired nothing, confirmed live on both a real TP-inference run (11+
minutes) and real Megatron TP4/PP4/DP3 training (85+ minutes). Root
cause, traced precisely: the existing worst-selection logic (`b3e2f90a`,
the same fix that closed the rank-12-adjacent "fastest member misflagged
as worst" bug, §1.3) only ever considers members reading ABOVE the
window's own median — correctly, for that bug — but this means a fault
whose signature is a lone NEGATIVE deviation with clustered positive
deviation on its peers can never be selected, regardless of how large the
real deviation is.

Fixed with a new, independent detection path (`_wait_induced_fallback_
evaluate`/`_check_wait_induced`, wired into `_poll_host` alongside the
existing `_check_mean`/`_check_cv` — never modifying either), a direct
generalization of the already-validated P27.2 2-member timing-asymmetry
fallback: its own per-member helpers already compare each member's
current reading against an EXTERNAL historical baseline for that exact
role shape, not against other members in the comm at that instant, so
there was no 2-member assumption in the comparison itself to begin
with — only the firing rule ("exactly 1 of 2 elevated") needed
generalizing, to "exactly N-1 of N elevated, flag the one that isn't."
Fires `[WAIT-INDUCED-ALERT]` — a distinct header from both `[ALERT]`
and `[STRAGGLER-INCIDENT]`, so the detection mechanism responsible for
a given finding is never ambiguous in the logs.

**Tier: PROBABLE, not CONFIRMED**, via a new `WAIT_INDUCED_STOPGAP_
ACTIVE` flag — deliberately the same caution `TIMING_FALLBACK_STOPGAP_
ACTIVE` already applies to its sibling mechanism (§2.1), and for the
identical reason: that stopgap exists because this exact evidence type
(a ratio-based timing-asymmetry comparison) was found, in this project's
own history, to false-fire CONFIRMED/PAGE on 5/5 genuinely healthy runs
before a MAD-gated fix landed. This check's own validation is real and
clean, but a single session's worth of exposure is not the same standard
of evidence that earlier false-fire took to surface.

**Validated, both directions, real data, both as a standalone scoped
test and again after integration:** a real TP-inference fault fired
20/25 times (the 5 non-fires were a transient cold-start gap in one
PEER's own baseline availability early in the run, self-resolved by
poll 6 onward, never a miss on the target itself), always correctly
naming the true injected rank; real Megatron TP4/PP4/DP3 training fired
20/20 times on the TP communicator, and additionally — unprompted —
correctly caught a real secondary cascade effect on a second, unfaulted
TP group sharing the same pipeline (confirmed via raw data: genuine
~22-27ms peer elevation against a ~140µs culprit reading, the same real
signature, not a false positive); the 16-member DP communicator for the
same job correctly stayed silent (no false fire, simply no additional
coverage at that level — detection is carried entirely by the TP
communicator, which already fires reliably). The safety check this is
hard-constrained against — re-running the exact clean, no-fault scenario
that originally exposed the rank-12 bias (§1.6) — produced 40/40 clean
polls across all 4 real TP communicators as a standalone test, then a
further 42+ minutes of continuous clean production polling after
integration, zero false positives either time, including the exact
previously-affected role position. `tools/self_test.sh`: clean PASS
throughout, 0 new `[CHECK-FAILED]`, the existing positive-deviation
path's own exact rank-match completely unaffected by this check running
alongside it in the same poll cycle.

### 1.6 Origin of the Role-Baseline-Deviation check — the rank-12 misattribution

Found live during the item-2 cascade investigation (a real
Megatron TP4/PP4/DP3 GPU-clock-fault run, job 3570): rank 12
(role_rank=0 within its own 4-member TP group, the last pipeline stage)
fired 13 times during the run, versus 0-1 times each for its three
TP-peers in the identical comm — a persistent, structural skew, not the
noisy/rotating "different member each time" pattern every other
downstream stage showed. This closely resembles this project's own
already-documented "rank 0's already-known, un-excludable overhead
bias" (see `_rank_dependents_by_deviation`'s own docstring), recurring
here as a TP-group-local role_rank=0 bias rather than a global-rank-0
one. Real, measured (not a false positive), but likely a workload-
structural artifact rather than a genuine fault signature.

This was addressed by a new, separate Role-Baseline-Deviation check
(`_role_baseline_fallback_evaluate`/`_check_role_baseline`, behind
`ROLE_BASELINE_ALERT_STOPGAP_ACTIVE`, see §4.2 for what it pushes to
Grafana), evaluating each member independently against its own
historical baseline for its exact role shape rather than against its
peers in-window. Two genuinely different false-positive causes were
found and fixed on top of it since: a baseline-pool contamination issue
(repeated fault-injection tests left their own anomalous readings
unexcluded from future baselines) and, separately, a small-sample
MAD-reliability issue for roles whose broader history is thin (see
`observability/workload_reliability_reference.yaml`'s own
`role_baseline` entry for the Megatron/TP-inference shape for the full
numbers on both fixes — commits `6454b61a`/`be085441`).

### 1.7 `[ROLE-BASELINE-ALERT-UNVALIDATED]` — the role-baseline check now gates its OWN confidence by workload shape

**The real gap this closes**: a release-gate regression sweep ran the
role-baseline check against TP4-standalone (nanoGPT TP4, no PP) — a
shape the reference table had no entry for — and found **9-13 real
false positives per clean run**, confirmed reproducible, confirmed NOT
the already-fixed contamination mechanism (zero
`agg_role_baseline_excluded` entries for the shape) and NOT literal
external contention (reproduced in isolation). At least two distinct,
still-uncharacterized real mechanisms are present (see MAINTENANCE.md's
own `ROLE_BASELINE_UNVALIDATED_GATING_ACTIVE` entry for the real
numbers) — and the check was presenting them with the exact same
`[ROLE-BASELINE-ALERT]` header and PROBABLE tier as a finding on a
genuinely validated shape like Megatron or TP-inference, with no visual
distinction at all.

**The fix, deliberately presentation-only, never suppression**: at
emission time, the check now resolves the firing comm's own real
`workload_sig` and looks it up in
`observability/workload_reliability_reference.yaml` (via the new shared
`observability/reliability_reference.py` module, also used by
`tools/incident_correlator.py` — one lookup implementation, not two). A
shape whose role-baseline entry is not exactly `KNOWN_RELIABLE` still
fires, with every real value unchanged — ratio, baseline,
baseline_source, tier, severity — just under a distinctly different
header, `[ROLE-BASELINE-ALERT-UNVALIDATED]`, with an added paragraph
explicitly stating the shape hasn't been characterized and the finding
should be read with extra skepticism. The two rejected alternatives
were explicitly considered and rejected: silently suppressing the
finding (hides real signal) and leaving it unflagged (overstates
confidence) — this is the middle path, matching this project's own
"never silently hide evidence, never silently overstate it either"
discipline everywhere else.

**Validated against the real TP4-standalone false positives**: a fresh
clean TP4-standalone run, re-run with the gate in place, produced **zero
new plain `[ROLE-BASELINE-ALERT]` lines and 11 new
`[ROLE-BASELINE-ALERT-UNVALIDATED]` lines** — both of the real
mechanisms found in the investigation (the ~36-41x dramatic pattern and
the ~1.8-3.5x noisier one) correctly downgraded. Zero-regression
confirmed the other direction too: a real TP-inference fault-injection
run (role_rank=3, a `KNOWN_RELIABLE` shape) still produced the plain,
undowngraded header on its correctly-resolved firings.

**One real, honest, safe-direction side effect found during
validation**: resolving a workload's sig requires the job to have
reached throughput stability, a sometimes-LATER threshold than
role-baseline's own 3-consecutive-sample firing requirement — confirmed
live, a real `KNOWN_RELIABLE` TP-inference firing showed the
UNVALIDATED header once, early in its own job's life, before its sig
had resolved. The error only ever runs toward MORE caution, never
toward false confidence, so this was disclosed (see MAINTENANCE.md) and
not treated as a defect to chase down further.

`tools/self_test.sh`: clean PASS throughout.

### 1.8 Same-job cross-TP-group comparison for TP4-standalone's role-baseline false positives — a real, PARTIAL fix

**The refined root cause**: the §1.7 TP4-standalone investigation was
extended to 8 real job launches. Role_rank=0 at bucket=11863283 (and,
found along the way, roles 0/3 at bucket=25874004 too) is genuinely
BIMODAL across separate job launches — a real ~180-230us cluster and a
real ~7,000-12,700us cluster, confirmed across jobs, not noise. The
existing `_role_shape_volatile` relative-MAD check cannot see this: when
one mode holds a numeric majority of historical samples, median/MAD
(robust statistics, by design) ignore the minority mode entirely,
reporting a deceptively tight baseline. The real discriminator that DOES
catch it: computing each PAST JOB's own median first, then measuring the
spread ACROSS those per-job medians — TP4's confirmed-bimodal shape
scored 0.96 this way; every already-validated role-baseline shape
(Megatron, TP-inference) scored 0.17-0.52 measured identically,
comfortable real headroom for the new `ROLE_BASELINE_JOB_LOCKSTEP_FLAG
=0.7`.

**The fix**: for a role shape flagged this way, `_cross_comm_peer_median`
— already, by its own P27.5 design (§2.3), scoped to the SAME real Slurm
job, not a new mechanism — is tried BEFORE the cross-job role-baseline
lookup, not just as its last-resort fallback. Real same-job TP-groups
agree with each other within ~1% in the data; cross-job medians can
differ by 40-70x, so the same-job comparison is categorically more
trustworthy for exactly this signature. Every other, non-flagged shape
keeps the original role → role_cross_host → cross_comm_peer order
unchanged.

**Validated, both directions**: a clean TP4-standalone run produced
**zero** role_rank=0/bucket=11863283 firings over a full 6+ minute run
once past job startup. A real injected fault (`STRAGGLER_SLEEP_MS`
targeting one TP-group's role_rank=0) was still correctly caught on that
same TP-group's role_rank=2, via the new `same_job_peer_lockstep` source
(ratio=16.3x, gap/mad=152.79), with zero false attribution to the 3
healthy TP-groups — sensitivity fully preserved.

> This fix is deliberately NOT marked `KNOWN_RELIABLE` — two real gaps
> remain and are tracked as genuinely open in README §7 (role_rank=1's
> own below-threshold pattern, and a startup race that can let an early
> firing fall through to the old cross-job path). See
> `observability/workload_reliability_reference.yaml`'s own
> `role_baseline` entry for this shape, which records `KNOWN_NOISY`, not
> `KNOWN_RELIABLE`.

`tools/self_test.sh`: clean PASS.

### 1.9 Above-floor scoring's node-local-only blind spot for cross-node communicators — found, root-caused, confirmed covered (fix deferred)

**OPEN at the code level, but confirmed covered in practice — not a
silent-miss risk today.** Found while validating the multi-bucket fix
(§2.x below) across parallelism shapes: `node_aggregator_ref.py`'s own
above-floor scoring (`score_mean_window`/`score_cv_window`) gates on
`len(members_here) < 3` (mean) / `len(cvs) < 3` (CV) before computing
`agg_mean_z_worst`/`agg_mean_fired`/`agg_cv_fired`/`agg_cv_z_worst` —
but `members_here`/`cvs` are built only from phys_ids **this one node's
own aggregator process has itself seen** (each node runs its own
aggregator, reading only its own host's dump files — see this file's
own module docstring). For a communicator whose real membership splits
across hosts such that no single node locally hosts 3+ of its members,
every node's aggregator independently sees too few members and bails
before scoring — the above-floor path goes completely silent for that
comm, regardless of whether a real fault is present. `check_outlier_
count`'s own gate is only `< 2`, so it does partially score such a comm
— but only ever compares each node's own local 2-member subset against
itself, never the real full group, a different and subtler
miscalculation, not complete silence.

Confirmed real and precisely characterized with live data, not
assumed: launched a real TP4 job and traced actual communicator
membership directly (not inferred from launch parameters). TP4 creates
8 real communicators per job — 4 TP-internal ones (node-local by this
workload's own design: `tp_rank = ddp_rank % tp_size` groups consecutive
ranks, and GPUS_PER_NODE being a multiple of TP_SIZE keeps every TP
group on one node) and 4 DP-gradient-sync communicators, one per
TP-rank, each genuinely spanning both nodes 2-members-per-host. Range-
queried `agg_mean_z_worst`/`agg_mean_fired`/`agg_cv_fired`/`agg_cv_
z_worst` for one of the cross-node comms over its entire real lifetime:
0 series, 0 points, on either host — while raw `agg_mean_exec_time_us`
existed fine (pushed unconditionally, above the gate). A node-local
comm in the same job, same window, scored normally (278 real z_worst/
fired points) — direct contrast, not an assumption.

Not TP-specific — this is about comm *topology*, not parallelism
strategy. Any communicator (DP, FSDP, whatever) whose real membership
happens to split such that no single node locally hosts 3+ members hits
the identical blind spot.

**A first attempt at proving real-world impact found no impact — and
the honest reason why turned out to matter more than the result.** A
real fault injected on a rank that is genuinely part of a cross-node
DP-reduce group produced *no* elevation on that comm at all; the delay
landed entirely on a node-local comm instead (and was correctly caught
there). Traced to the injection point: `train.py`'s existing
`STRAGGLER_SLEEP_MS` hook fires *before* `backward()`, and the real
cross-node collective (the explicit DTensor/TP-sharded-parameter
`dist.all_reduce(..., group=dp_group)`, run in its own unconditional
loop *after* `backward()` fully returns) never saw the delay — it was
fully absorbed/resynced by whichever synchronizing collective backward()
itself triggers first. This also surfaced a separate, unrelated
confound worth disclosing on its own: all 4 DP-reduce comms showed their
`role_rank=0` member reading ~70x faster than its peers *regardless of
which rank was actually targeted* — a pre-existing structural artifact
(consistent with this project's already-documented role_rank=0/rank-0
overhead bias elsewhere), not fault signal. An earlier, less careful
reading of this same data mistook that artifact for a real detection
gap; re-verifying directly, rather than trusting the first read, is what
caught it.

**A second, deliberately adversarial test closed the question.** Added
a new, narrowly-scoped, opt-in, default-off test hook
(`STRAGGLER_SLEEP_AT_DP_REDUCE_MS` in `workloads/tp2/train.py`) that
injects the delay immediately before the explicit `dp_group` all_reduce
loop, after `backward()` has already fully completed — nothing else
synchronizing sits between the sleep and that collective, so the delay
cannot be absorbed anywhere else first. Real result, ground-truth rank
confirmed via its own dump file: the targeted rank's own reading stayed
at 141µs while its 3 real peers on the cross-node comm read
~240,000–270,000µs — a ~1,700x elevation, the cleanest signature
produced in this entire investigation. `agg_mean_z_worst`/`agg_mean_
fired`/`agg_cv_fired`/`agg_cv_z_worst`: confirmed still 0 series, 0
points — the above-floor path is completely blind to it, exactly as the
code predicts. **And it was still caught**: a real `[WAIT-INDUCED-ALERT]`
fired, correctly naming the targeted rank, with full evidence (all 3
peers at ratio 1658–1713x, gap/mad in the tens of thousands) — via
`_wait_induced_fallback_evaluate`, which uses `_comm_cross_node_members`
(a real, cross-host VM query) rather than any one node's local view, so
it never hits the aggregator's own locality gate at all.

**Why this is covered today, and the one way it could stop being
covered without anyone noticing.** `_poll_host` calls `_check_wait_
induced`/`_check_role_baseline` unconditionally for every discovered
comm (see §1.5 above for the original reason this pair of checks exists
at all: the mean-path's own worst-selection logic has a different,
separate structural blind spot, and these checks were built from the
start as an independent, universal companion to it, not a below-floor-
only mechanism) — so a cross-node comm gets evaluated by the real,
global membership the same way any other comm does, never gated by
whatever the aggregator's own per-node view happened to see. This is a
genuine, currently-reliable safety net, not a coincidence. The one real
fragility: nothing explicitly documents that this pair of checks is also
load-bearing for the cross-node gap specifically — a future change that
made their invocation conditional on "comm is below SELF_DETECTION_
FLOOR" (a reasonable-looking cleanup, since that's the scenario they
were originally built for) would silently remove this coverage with no
error, no log line, and no warning that anything had changed.

**Recommendation: document, defer the aggregator-level fix.** The
concrete fix (resolve `members_here`/`cvs` against real, globally-
discovered membership — e.g. the same `_comm_cross_node_members`-style
cross-host union already used correctly in `alert_engine.py` — instead
of each node's own local view, before applying the `< 3` gate) remains a
real, available option, scoped additively to `score_mean_window`/
`score_cv_window` only; it would not touch `_wait_induced_fallback_
evaluate`, `_role_baseline_fallback_evaluate`, or any other already-
validated below-floor mechanism. Not implemented — deferred as lower
priority given the confirmed, real coverage above, tracked in README §7
alongside this project's other disclosed, fallback-mitigated gaps.

A smaller, related finding from the same investigation:
`agg_detection_coverage_achieved` is pushed unconditionally (1 = checked)
for every comm/bucket once past its grace period, regardless of whether
the `< 3` gate below it ever let real scoring proceed — so the
dashboard's own "was this ever checked" signal reads "yes" for exactly
the comms affected by this gap. Not separately fixed; disclosed here
since it shares the same root cause.

`tools/self_test.sh`: unaffected (the new test hook lives only in
`workloads/tp2/train.py`, a different file from `workloads/nanogpt/
train.py`, which `self_test.sh` actually exercises) — confirmed clean
PASS after the unrelated multi-bucket fixes landed in the same session.

---

## 2. The below-floor (2-member) fallback saga

### 2.1 P27.2: the TP2 long-soak false-positive storm — reproduced, root-caused, fixed (Part D closed)

A real, live Stage 5 isolated-validation run found 5 real CONFIRMED/PAGE
false positives on a genuinely healthy (unfaulted) TP2 baseline via the
P27.2 timing-asymmetry fallback. A same-session re-investigation
suspected `peer_mad` computing to 0 or a degenerate value silently
no-oping the MAD gate, but could **not** reproduce the false positives in
a fresh 1250+-iteration healthy TP2 re-run in the time available, and
left this genuinely unresolved.

A later V1 Beta closeout session tried a genuinely different method — a
much longer healthy TP2 soak (10,000 iterations, ~15 real minutes, 16
real ranks / 8 real pairs, zero `STRAGGLER_SLEEP_MS`) instead of another
short run — and **reproduced the storm decisively**: 10 of 16 genuinely
healthy members fired real CONFIRMED/PAGE alerts. This also corrected the
earlier suspicion: live `peer_mad` values at the moment of firing were
small but genuinely nonzero (3-13us, not degenerate/zero), and the real
peer-median baseline (105-114us) matched this fallback's own historically
validated real-fault case almost exactly — ruling out stale or
mismatched baseline data. The real root cause is that this cluster's own
natural per-collective timing variance on individual healthy TP2 members
widens enough, given enough elapsed samples in a long enough run, to blow
through the fixed `TIMING_FALLBACK_MAD_MULTIPLE=6` gate on many pairs at
once (observed gap/mad ratios: 26.5-423.9) — a genuine **duration-
dependent** false-positive risk that short validation runs (the original
5-run MAD-fix validation batch, and this project's own 1250-iteration
re-investigation attempt) were simply too short to ever encounter.
TP-inference (the other real 2-member shape using this same fallback)
has never shown this behavior.

**Fix applied and validated**: `TIMING_FALLBACK_STOPGAP_ACTIVE`
(`alerting/alert_engine.py`) has been re-activated (`True`) — the same
downgrade-to-PROBABLE/LOG-ONLY stopgap this project already built and
used for exactly this failure mode before the (now-shown-to-be-
insufficient) MAD-based fix was live-validated and the stopgap reverted.
This is deliberately the minimal, already-proven lever rather than a new
statistical redesign (a larger, riskier change): it keeps every real
detection visible (still emits, at PROBABLE/LOG-ONLY) while removing the
false-PAGE risk, until a genuinely duration-robust statistical gate is
designed separately. The supervised process was restarted via its own
normal path (`SIGTERM` to the leaf process → the existing supervisor
loop's own auto-restart, the same already-documented mechanism, not a
bare `kill -9`) — clean exit (code 143) to relaunch completed in 3
seconds, and the new process's start time was confirmed to postdate the
source file's own mtime before any validation began.

**Validation (n=5 healthy soak runs + 1 real-fault regression, all
post-restart)**: five independent 10,000-iteration (~15-real-minute)
healthy TP2 soak runs all showed **zero** `CONFIRMED/PAGE` alerts
sourced from this fallback (one incidental `CONFIRMED/PAGE` did occur in
run 2, traced directly to a real, independent, hardware-corroborated
Path B/DCGM clock-suppression event — `sm_clock=510 vs peer_median_
sm_clock=1980`, genuinely active power draw — a different detector
entirely, unaffected by and out of scope for this stopgap, so it does
not count against this fix). A real-fault regression run (rank 3,
`STRAGGLER_SLEEP_MS=200`) confirmed detection is fully preserved, not
lost or delayed: the real injected target was still correctly and
exclusively named, firing at ~3:04 elapsed (consistent with this
project's established fault-detection timing), now correctly capped at
PROBABLE/LOG-ONLY instead of the old false CONFIRMED/PAGE. `CHECK-
FAILED` stayed at 0 and the pipeline stayed healthy across every one of
these 6 runs. **Part D is closed.**

### 2.2 RL: an earlier "weak/inconsistent detection" finding was itself wrong — the fault was never actually being injected

Stage 5's own RL validation reached z=510, arrival lag 692x, correctly
localized. A later investigation session re-ran RL's fault injection 3
times and found the target's own z-score stayed weak (roughly 2-7) and
once even saw a healthy neighbor rank misfire instead — and concluded,
incorrectly, that this was a real signal/attribution gap. Root cause,
found in a follow-up session: every one of those reruns (and an earlier
same-session silent run) launched `run_rl.sh` with `STRAGGLER_PHASE=none`
as the phase argument. `train_rl.py` only injects its `time.sleep()` when
`STRAGGLER_PHASE` is `rollout`, `forward`, or `backward` — `none` matches
none of the three conditions and is silently a full no-op. Every one of
those "weak signal" test runs was, in reality, a completely healthy job;
the weak z-scores and the one stray misfire were ordinary healthy-job
noise, not a detection gap. Corrected re-runs with `STRAGGLER_PHASE=
forward` (5 independent runs, same target rank each time) confirmed
reliable, correctly-attributed detection in all 5: the injected target's
own exec time read 294-350us against ~200,000-205,000us for every peer
(~600-700x elevation, closely matching Stage 5's own 692x), z-scores in
the hundreds, and a real `confidence=PROBABLE` alert naming the exact
injected target rank every time, firing between ~3:41 and ~3:56 elapsed
in each run — fully consistent with the original Stage 5 result. **RL's
detection was never broken.** The lesson that mattered here was about
this project's own validation harness, not the pipeline: `STRAGGLER_
PHASE` must always be set to a real phase value when injecting an RL
fault — leaving it unset/`none` silently disables the fault, and a
silent no-op fault test is indistinguishable from a real detection gap
unless the underlying signal is checked directly (which is what caught
this).

### 2.3 Hybrid: the below-floor P27.2 fallback never fires, despite a very strong raw signal — and the job-wide peer-pool fix (P27.5)

A dedicated investigation (2 independent fault-injection reruns, same
target rank both times) found a very strong, consistently-reproduced
partner-elevation signature via direct `agg_mean_exec_time_us`
inspection — the injected target read 17-56us while its healthy TP
partner read 5,600-28,200us (a 300-500x ratio, reproduced almost
identically across both runs, and considerably stronger than PP's own
successful 36x signal on the same mechanism) — yet zero alerts ever
fired, over 5+ minutes each run. Root-caused directly against
`_timing_asymmetry_fallback_evaluate`'s own docstring in `alerting/
alert_engine.py`: `_cross_comm_peer_median` requires an EXTERNAL peer
group from another below-floor comm with genuinely DIFFERENT physical
members on the same host to establish its baseline (self-history was
deliberately abandoned earlier in this project's history due to its own
false-positive risk — see §2.1 above). Confirmed directly via VM query,
in both reruns: every below-floor comm active on the target's host
shares the exact same 2 physical members — Hybrid's 2-ranks-per-node
layout means the local TP pair IS the same physical pair that also
forms the cross-node PP send/recv endpoint on this node, so there is no
independent same-shape comm with different members to serve as a peer
pool. This is precisely the gap the fallback's own docstring already
discloses ("returns None, honestly, whenever no OTHER same-shape comm is
currently active to serve as the peer group... a real, disclosable
residual gap for a workload whose below-floor comm has no live sibling
at all, not a bug") — confirmed here as the real, reproducible cause for
this specific 2-ranks-per-node topology, not a timing issue and not a
weak signal (the signal is unusually strong; there is simply nothing
live to compare it against).

**Follow-up: this gap is real but condition-dependent, not absolute** —
a fresh Stage 5 validation session re-ran Hybrid's exact fault scenario
on a **genuinely fresh VictoriaMetrics instance** (no prior cross-job
history at all) that had, by the time Hybrid ran, already accumulated
real `agg_mean_exec_time_us` data from several earlier same-session
shapes (TP2/TP4/FSDP/etc.) sharing the same physical GPU slots and
message-size buckets on these same two nodes — and this time the fault
**did** fire, a real `PROBABLE` alert correctly naming the injected
target (`_cross_comm_peer_median`'s `baseline_source='cross_comm_peer'`/
`'role'` paths both observed live in the surrounding trace). This
confirms the root cause precisely: Hybrid's detection works exactly when
a genuinely external peer or cross-job role history happens to be
available, and fails exactly when it isn't (a truly isolated Hybrid run,
or the very first run of its kind against a cold VM, still has nothing
to compare against). **Long-context (also below-floor, same mechanism)
hit the cold-start case directly in this same re-run**: a strong real
signal (~100-300x elevation on the waiting partner, same inverse
pattern) produced zero alerts, because it was long-context's own
first-ever run this session and its message-size buckets are unique to
it — no existing sibling or role history yet. Same mechanism, same root
cause, opposite outcome, purely because of what else happened to have
run earlier on this cluster.

**FIXED and validated (P27.5) — the genuine cold-start case is now
resolved, not just condition-dependent.** Root cause of the fix
direction: `_cross_comm_peer_median`'s live-peer pool (`alerting/
alert_engine.py`, ~line 1820) was scoped to same-host-only, but Hybrid's
own topology (2 ranks per node — a TP pair per PP stage) means each
node's local below-floor comm has no OTHER same-shape comm on that SAME
host to serve as a peer, by construction (the local TP pair IS the local
PP endpoint) — this is exactly why the original "never fires" finding
was genuine, and exactly why it "worked" only when unrelated leftover
data from earlier same-session shapes happened to be lying around.
**Real fix**: relaxed the live-peer pool from same-host to job-wide (any
host in the same Slurm job), with explicit `slurm_job_id` scoping added
to the primary live-peer branch (which previously had none at all,
relying only on the hostname filter this change removes) to prevent a
different job's stale below-floor comm from contaminating the pool —
falls back to the original same-host-only scoping if the job id can't be
determined, never searching job-wide unscoped. This means, for Hybrid
specifically, worker-0's own TP pair now serves as a genuine,
physically-different-member peer for worker-1's TP pair in the SAME job
(and vice versa) — a real property of Hybrid's own topology (every real
Hybrid deployment has at least 2 workers, each with its own local TP
pair), not a test-only convenience, so this closes the cold-start gap
for a genuine first-ever production Hybrid job on a fresh cluster too,
not just this project's own test sequencing.

**Considered and rejected**: deliberately seeding a compatible below-floor
shape (e.g. a plain TP2 job) immediately before Hybrid's own test, as a
test-setup convention rather than a code change — rejected as not a real
fix for a real deployment (a customer running Hybrid as their first/only
workload on a fresh cluster would still hit the cold start; Hybrid's own
multi-worker topology already provides everything the job-wide code fix
needs, without requiring an unrelated shape to run first).

**Validated live, n=5 independent genuinely-isolated Hybrid runs** (no
other shape run beforehand in the same job — the real cold-start
condition, not the "something else happened to run first" condition the
original finding depended on): 5/5 fired a correct `PROBABLE` alert with
exact rank+host attribution; run 1 additionally cross-checked directly
against VictoriaMetrics, confirming `baseline_source='cross_comm_peer'`
sourced from the OTHER node's TP pair in the same job, exactly the new
mechanism (before this fix, the same query would have found no peer at
all for this exact scenario).

**Regression-checked**: TP2 — fault-injection detection unchanged (exact
rank/PID match preserved); a ~6-minute healthy soak produced zero new
`CONFIRMED`/`PAGE` alerts (unchanged count), consistent with `TIMING_
FALLBACK_STOPGAP_ACTIVE` already keeping this fallback's tier below
PAGE-worthy. PP — unaffected either way: a plain 2-rank-per-node PP job
has no other below-floor comm in the same job to serve as a job-wide
peer regardless of this change, so `_cross_comm_peer_median`'s modified
code path isn't reachable differently here; PP's own separately-diagnosed
role-baseline contamination issue (§2.5) is unrelated to this fix and
remains open.

### 2.4 Hybrid: a second, different role-baseline gap — baseline PROVENANCE, not attribution correctness (FIXED)

A confirmation-pass re-test (3/3 runs, correct attribution every time)
noticed the fired baseline for role_rank=1 was suspiciously small
(39.22) against a well-populated, consistent 6-job majority cluster
(role1 ~24k-28k) that should have been available. Root-caused precisely:
Hybrid's own testing convention injects the fault on the SAME target
rank every single run, so `_push_role_baseline_exclusion` (the existing
anti-poisoning safeguard — working exactly as designed, not a bug in
itself) correctly excludes role1's reading as anomalous on all 9 of
Hybrid's own real runs, draining its non-excluded survivor pool down to
a single outlier entry (job 3369, a different, unrelated historical run
that happened to share this exact label combination). `_member_
role_baseline` had no minimum-sample-size floor, so this single
unrepresentative survivor was silently treated as a fully valid baseline
instead of degrading to "not enough data."

**Fix**: `ROLE_BASELINE_MIN_HISTORY = 3` (`alerting/alert_engine.py`,
reusing this project's own established "3 independent data points"
precedent — `THROUGHPUT_XJOB_MIN_HISTORY`, `PERSIST_REQUIRED`/
`PERSIST_WINDOW`) — `_member_role_baseline` now degrades to `(None,
None)` below this floor, routing the caller to the already-validated
`_cross_comm_peer_median` fallback instead. Does not touch PP's own
baseline (10 of 40 real historical entries survive un-excluded there,
comfortably above the floor). Mechanically verified against real
historical VM data (role1's real pool confirmed to have exactly 1
non-excluded survivor, well under the new floor) and confirmed live that
a workload whose own testing convention has no live peer sibling AND has
drained its role-baseline pool this way correctly falls through rather
than firing on a mismatched, unrepresentative value.

### 2.5 PP (Shape 9): a self-reinforcing cross-job history contamination — root-caused and fixed

**Root-caused — a self-reinforcing cross-job history contamination, not
a code regression and not the same mechanism as Hybrid's gap.** A
dedicated follow-up session traced this precisely by directly invoking
`_timing_asymmetry_fallback_evaluate` against a live PP run:
`_comm_cross_node_members` correctly discovers both real members across
both nodes (the P27-hotfix4 cross-node fix already in this file works
exactly as documented) — the function does NOT return None at the
member-discovery step. It returns None later, because `_member_role_
baseline`'s cross-job history pool for BOTH of PP's roles is 100%
contaminated: every single historical `agg_mean_exec_time_us` entry ever
recorded for PP's role_rank=0 and role_rank=1 (confirmed directly, all
of them, across every job in this cluster's history) reflects the
identical target-rank/200ms fault convention this project's own testing
has always used for PP — there is no genuinely healthy PP history
anywhere. This project already built a real anti-poisoning safeguard for
exactly this (`_excluded_role_pool_members`/`_push_role_baseline_
exclusion`, dropping any (comm,member) a role pool has ever seen
successfully flagged as anomalous) — but it only engages on a
SUCCESSFUL fire, and PP's fallback has never once fired, so the
safeguard has never had a chance to exclude anything. The result: the
current run's fault looks statistically normal (ratio ~1.0 for both
members) because the "healthy" baseline it's being compared against IS
that same fault, repeated. This fully explains the "36x successful
signal" this document previously cited: that measurement most likely
predates this contamination (a clean or empty history at the time), and
does not generalize once this project's own repeated, parameter-
identical fault testing accumulates — a real, reproducible, self-
inflicted regression, not a code change and not a discovery-path bug.

**FIXED — the circular dependency above turned out not to need either of
the two design changes originally proposed here.** A dedicated follow-up
session found the REAL blocker one layer down: `node_aggregator_ref.py`'s
`comm_calib`/`comm_bucket_members` (the state `workload_signature()`
reads) were never reset per job in a long-lived aggregator process (the
same root cause DLRM's own gap, §2.6, shares) — every job that ever
reached stabilization got a unique, never-repeating signature, so
`_member_role_baseline`'s cross-job sig-matching (P27-hotfix7) could
never find ANY historical match at all, for either role, regardless of
contamination. Once that's fixed (`maybe_reset_workload_state()`, called
on every real job-boundary transition), PP's role-baseline mechanism
works correctly with **zero changes needed to `alerting/alert_engine.py`**
— validated live, n=5 independent fault-injection runs across real job
boundaries (jobs 3426-3430), every one firing `baseline_source='role'`
against the same stable, correct, clean-magnitude baseline (~5359.93,
ratio ~38x each time), with exact correct rank+host attribution every
time. The original "100% contaminated, self-reinforcing" diagnosis above
was real and correctly described VM's actual accumulated data at the
time — what it missed was that, once signatures correctly and stably
discriminate again, a NEW job's own correctly-scoped signature no longer
coincidentally matches that old, uniquely-signed contaminated history at
all, so genuinely healthy runs recorded going forward populate a fresh,
uncontaminated pool without needing either of the fire-independent
sanity check or varied-fault-parameter proposals originally floated
here.

### 2.6 DLRM (Shape 13): throughput-reference wiring bug and signature job-scoping bug — both FIXED

**Both the throughput-reference wiring bug and the workload-signature
job-scoping bug are now FIXED and validated.** Root-caused directly:
`node_aggregator_ref.py`'s `_throughput_rate_ref` (the reference
`agg_job_throughput_ratio_to_baseline` divides by) was a per-aggregator-
**process**-lifetime value, established ONCE by whichever job first
reached stabilization and never reset — confirmed live with real
numbers: DLRM's own real raw event rate was ~2.0-2.3x an earlier,
completely unrelated job's stale ~110/sec reference, and — the same
bug's other face — a later diagnostic PP job's own real rate read as low
as 0.13-0.47x that SAME stale reference, producing real false
`CONFIRMED/PAGE` `uniform_slowdown` alerts on otherwise-unremarkable
runs. One root cause, opposite symptoms, purely depending on which side
of an irrelevant reference a given workload's real rate happens to fall.

**Fixed and validated**: `node_aggregator_ref.py` now tracks which real
`slurm_job_id` established the current throughput reference and resets
it (and every supporting stabilization counter) the moment a genuinely
new job is detected — validated live across 4 consecutive real jobs
(DLRM x2, then DLRM-faulted, then nanoGPT), each establishing its own
fresh, job-appropriate self-calibrated reference (4704/sec, 5048/sec,
240/sec, 110/sec respectively — correctly tracking each job's own real,
wildly different natural rate) with zero cross-contamination, and a
regression check (the same nanoGPT run) confirming per-rank fault
detection is completely unaffected and no new false `uniform_slowdown`
fired.

**The second issue — `comm_calib`/`comm_bucket_members` (the dicts
`workload_signature()` reads to build each job's cross-job-matching
"sig") never being reset per job in a long-lived aggregator — is now
FIXED**, confirmed live: DLRM's own sig's `n_comms` had climbed 53 ->
54 -> 55 -> 56 across consecutive, unrelated jobs before the fix (these
dicts accumulate every comm/bucket/collective type this aggregator
process has EVER seen, across every job, not just the current one),
meaning two runs of the exact same workload essentially never produced a
matching sig, so `query_throughput_history`'s cross-job lookup could
never find a match (confirmed live pre-fix: DLRM's faulted validation
run found "0 historical run(s)" despite 2 real prior DLRM runs already
having pushed their own reference).

**Real fix**: `maybe_reset_workload_state()` in `node_aggregator_ref.py`,
called on every real job-boundary transition (same trigger, same call
site pattern as the `_throughput_*` reset above) — resets exactly
`comm_calib`+`comm_bucket_members`, deliberately narrow (does not touch
`self.state`/`phys_comm_role`/`phys_gpu_slot`, already implicitly
job-scoped by never-reused `comm_id` keys). **Validated live across 5
real job-boundary crossings**: two same-shape PP jobs correctly produced
the identical sig and correctly found each other as cross-job history
("1 historical run(s) found," not "0"); a deliberately different shape
(nanoGPT DDP) got its own distinct signature, not merged. As a side
benefit this same fix also corrects `live_denom` (throughput-stability's
own denominator), which was silently inflated by dead cross-job entries
the whole time this bug existed — a second, related bug, same root
cause, not separately disclosed before.

### 2.7 PP vs. DLRM vs. Hybrid — a real, evidenced comparison, not three guesses

All three sit in the same general family (peer-relative/cross-job
detection failing when there's nothing genuinely appropriate to compare
against) but are mechanistically distinct, not one bug with three faces:

- **Hybrid** (§2.3): cold-start **starvation** — no live sibling comm AND
  no cross-job history exists yet at all. Nothing to compare against
  because nothing has been recorded. **FIXED (P27.5)**: the live-peer
  pool now searches job-wide instead of same-host-only, so Hybrid's own
  multi-worker topology supplies a real peer where none existed before —
  validated n=5, TP2/PP regression checked.
- **PP** (§2.5): looked like the opposite of starvation — **abundant but
  100% contaminated** history, self-reinforcing (the anti-poisoning
  safeguard depends on a successful detection that had never happened).
  **FIXED**: the real blocker was one layer down — `comm_calib`/
  `comm_bucket_members` never resetting per job meant the cross-job
  sig-match could never succeed at all, contamination or not. Fixing
  that alone resolved PP too, n=5 validated, zero changes needed to
  `alert_engine.py`.
- **DLRM** (§2.6): an **infrastructure/wiring** problem — comparing
  against the literally wrong job's data (fixed first), compounded by
  the same `comm_calib`/`comm_bucket_members` job-scoping bug PP's gap
  turned out to share. **Both pieces now FIXED**, validated live across
  5 real job-boundary crossings.

Three distinct-looking symptoms, but two of the three (PP, DLRM) turned
out to share one real, single root cause underneath — the aggregator
never job-scoping `comm_calib`/`comm_bucket_members`, silently breaking
every consumer of `workload_signature()`'s cross-job matching (DLRM's
own throughput-history lookup AND PP's/Hybrid's role-baseline pool
separation) at once. Hybrid's own gap was genuinely different (a live
peer-pool topology limitation, P27.5) and got its own separate,
correctly-scoped fix. Confirmed directly that fixing the shared root
cause left Hybrid's already-fixed P27.5 mechanism untouched, and vice
versa (regression-checked both directions).

### 2.8 A fourth, distinct PP gap — below-floor role-baseline was hostname-pinned, not just role-shape-pinned (FIXED)

Found during a real cross-node validation (Megatron TP4/PP4/DP3 on a
6-node cluster). A cross-node PP-link sleep-fault run landed correct
rank/host attribution, but `baseline_source` resolved to
`cross_comm_peer` (the less-precise fallback) instead of the preferred
`role` baseline, even though prior jobs on the same cluster had already
run the identical workload shape minutes earlier. Traced directly in
`_member_role_baseline` (`alerting/alert_engine.py`): its cross-job
history query was scoped by `hostname="{hostname}"` in addition to
`(bucket, coll, role_rank, role_n)` — not just role shape. That hostname
pin is a deliberate, real tradeoff (it keeps a role's baseline free of
cross-node hardware-variance contamination), but it meant the lookup
only ever hit if Slurm happened to place the SAME role on the SAME
physical node across separate job submissions — trivially true on a
small, fixed 2-node cluster (the 1-dev-cluster case this project's own
history was validated against), but not guaranteed at all on a larger or
shared cluster, where job-to-node allocation varies run to run.

Re-ran the identical PP-link scenario on this project's own 2-node dev
cluster to isolate the mechanism itself (not the cluster-size-dependent
trigger): `baseline_source='role'` engaged correctly and produced an
accurate, well-evidenced finding (ratio=37.9x, correct rank/host),
confirming the role-baseline mechanism itself works correctly once
matching host-scoped history exists — the gap was specifically about
*history availability* on a cluster where node placement isn't
repeatable, not a wrong-answer bug in the mechanism itself. Attribution
correctness never depended on which `baseline_source` tier engaged (both
this test and the original Megatron validation landed the correct
rank/host either way) — a confidence/precision gap, not a correctness
one.

**Fix**: `_member_role_baseline` (and `_excluded_role_pool_members`, its
matching exclusion-set lookup) now take an `any_host` parameter. The
2-member timing-asymmetry fallback tries, in order: (1) the original
strict same-host role baseline, UNCHANGED — zero behavior difference on
any cluster where this already succeeds; (2) if that misses, the
identical role-shape query with the `hostname=` constraint dropped,
pooling history for `(bucket, coll, role_rank, role_n)` across ANY host
— labeled `baseline_source='role_cross_host'`, distinct from plain
`'role'`, so it's always auditable which precision tier actually
produced a given finding; (3) only if even that misses, the existing
`cross_comm_peer` fallback, unchanged. Real cross-node hardware variance
may make tier 2 a slightly noisier baseline than tier 1 — which is
exactly why it's inserted as a middle tier, never replacing the
already-validated strict match, not a new default.

Verified two ways, not just read: (a) isolated function-level test —
pushed real synthetic history under `hostname="worker-1"` for a role
shape with zero existing data on `hostname="worker-0"`; confirmed the
strict same-host query from `worker-0` still correctly returns `(None,
None)` (unchanged), `any_host=True` from `worker-0` correctly finds and
computes a real median/MAD from the `worker-1`-tagged data, and the
strict query from `worker-1` itself still works unchanged. (b) A real,
live end-to-end PP-link fault injection on this dev cluster produced a
correct finding with `baseline_source='role'` (tier 1, since this
cluster already has real matching host-scoped history from earlier
testing) — confirming zero regression to the already-working case.
`tools/self_test.sh` re-run clean afterward: exact rank-match, zero new
`[CHECK-FAILED]`.

### 2.9 Below-floor coverage-achieved signal never fired for ANY 2-member comm (FIXED)

`agg_detection_coverage_achieved` (meant to answer "has this comm/bucket
had a fair chance to be evaluated yet") was gated behind `score_mean_
window`'s own `if len(members_here) < 3: return` in `node_aggregator_
ref.py` — this check runs BEFORE the coverage push, so it fires and
returns for ANY below-floor comm with exactly 2 physical members before
the push line is ever reached, regardless of real job duration.
Confirmed live for BOTH below-floor shapes this pipeline validates: a
plain PP job (2-rank cross-node Send/Recv) and a TP2 job (2-rank
intra-node AllReduce) both showed `agg_mean_exec_time_us` freshly
pushing for 4+ real minutes — well past `BUCKET_MATURITY_GRACE_S`
(120s) — while `agg_detection_coverage_achieved` stayed structurally
absent the entire time, for both a short job and a long, genuinely-
stabilized one. Practical effect before the fix: there was no honest way
to distinguish "this short 2-member job never got a fair chance" from
"this 2-member job ran forever and is healthy," because the signal
meant to answer that was silently never emitted for either case on
these shapes (it DOES fire correctly for 3+-member below-floor comms,
e.g. Hybrid's own PP-boundary comm, which has TP-sharded activations
crossing it rather than a single rank pair).

**Fix**: reordered the already-computed `bucket_scored_at_ts_us`/grace-
period check and the coverage push to run BEFORE the `< 3` early-return,
reusing only already-tracked state (member count, `bucket_scored_at_ts_
us`, `BUCKET_MATURITY_GRACE_S`) — no new mechanism. The 3+-member CV/z
self-detection scoring itself is completely unchanged, still gated on
real member count; only the coverage SIGNAL is now below-floor-
inclusive. **Validated live, n>=3 independent runs on both shapes**: PP
(short job -> correct real "0"; two normal-length jobs -> both correctly
flipped to "1" at real elapsed ~3:36-3:40 from job start) and TP2 (short
job, 500 steps -> correct "0" across every bucket; normal job, 20000
steps -> correctly flipped to "1" across effectively every real
(host,bucket,comm) combination).

---

## 3. Infrastructure watchdogs — build-out and validation

### 3.1 Aggregator offset-checkpoint resume — eliminating full-backlog replay on restart

This was previously documented as a disclosed, unfixed property
(`self.file_offsets` being purely in-memory, confirmed on a cluster with
57GB/341 files where one comm alone replayed 235,000+ records on
restart). It became a priority fix after directly causing a real
regression: restarting both aggregators to deploy the sacct work (§4.4)
left them replaying an ~11GB/host backlog for 9-15 minutes (the real
range observed varied run to run with how much historical state had
accumulated), during which `tools/self_test.sh` genuinely **FAILED** —
the aggregator was still replaying history, not watching the live test
job, when the test's own 600s alert-wait window expired.

**Design**: `node_aggregator_ref.py` now periodically checkpoints
`self.file_offsets` to a small JSON file (`.aggregator_offsets_
checkpoint.json`) inside each host's own `var/dump/<host>/` directory —
every `OFFSET_CHECKPOINT_INTERVAL_S=10` seconds during normal operation
AND once per file during a large backlog replay itself (so even a crash
mid-replay loses only the interval's own small window, not the whole
in-progress replay), via an atomic write (temp file + `os.replace()`, a
single rename syscall — a reader can never observe a partially-written
checkpoint). A real `SIGTERM` handler is also installed: the actual
restart mechanism this pipeline already uses (clean `SIGTERM` to the
leaf process, supervisor relaunches) uses Python's default signal
disposition by default, which would terminate the process before any of
this code ran — the handler makes a *deliberate* restart checkpoint
right up to the moment of the signal, not just whatever the last
periodic write happened to catch.

**Real edge cases handled, not hand-waved**:
- **Missing/corrupt/stale checkpoint** (first-ever startup, a crash
  mid-write, an unexpected schema): `_load_offset_checkpoint()` fails
  safe on ANY problem, falling back to `{}` — the exact pre-fix
  behavior, never a silent skip of real data. **Validated live**:
  deliberately corrupted the checkpoint file and confirmed the next
  restart logged `[OFFSET-CHECKPOINT] could not load ... falling back
  to re-reading from byte 0` and correctly did a full, safe re-read —
  no crash, no skipped data.
- **A dump file truncated/replaced since the checkpoint was written**
  (the inspector plugin's own writer opens each dump path with
  `fopen(path, "w")` — confirmed in `inspector-plugin/json.cc` — which
  truncates in place if the OS ever reuses a rank process's PID): a
  single guard in `poll_files()` (not duplicated in the checkpoint
  loader) catches this by comparing current file size and inode against
  what's already known, resetting to offset 0 on either mismatch —
  always failing in the safe direction (reprocess, never skip). This
  same guard also protects plain continuous live operation against a
  PID-reuse truncation, independent of any restart.
- **Small amount of re-processed data after a crash**: a crash between
  two checkpoints loses at most `OFFSET_CHECKPOINT_INTERVAL_S` (10s)
  worth of offset progress, re-processing that small window once more
  on resume. Confirmed acceptable, not assumed: this is bounded,
  safe-direction duplication, and the consumers of `handle_record()`'s
  output (rolling calibration/CV windows, VictoriaMetrics ingestion)
  already tolerate the normal jitter of a live stream.

**Real validation, not just a fresh/empty install**: tested against this
cluster's own real, multi-day accumulated dump directories (~11-12GB/
host, 216+ files each), not a synthetic small backlog.
- **Clean, isolated restart (no contending replay on the other host)**:
  checkpoint-assisted resume went from heartbeat-stale to fully live in
  **~30 seconds**, against a **9-15 minute** from-scratch baseline (the
  range observed across this session's own restarts, worse than the
  original ~9-12 minute figure once more historical jobs/communicators
  had accumulated by the time of a later from-scratch test) — confirmed
  via real `agg_aggregator_heartbeat` freshness checks against
  VictoriaMetrics, not inferred.
- **Fault-detection-across-restart, the real scenario that matters**:
  launched a real fault-injection job (rank 8/worker-1, `STRAGGLER_
  SLEEP_MS=200`), let the aggregator checkpoint partway through that
  job's own real dump data, then `SIGKILL`'d it mid-stream (simulating a
  true crash, not a clean shutdown) and let the supervisor relaunch it.
  Confirmed via the checkpoint's own content that it resumed from the
  last checkpointed offset, not byte 0 and not the file's current
  (further-advanced) end — then confirmed the real `[ALERT]` still fired
  afterward, naming the exact injected rank/PID (`rank=938521`,
  `host=worker-1`) with an exact match against the ground-truth PID
  recorded before the crash. No fault evidence was lost by the
  crash-and-resume cycle.
- **Checkpoint-write overhead, measured directly**: a realistic
  216-entry payload (~29KB JSON) writes in **~0.43ms**, measured via 200
  real back-to-back timed writes — negligible at the 10s interval
  (~0.004% of wall-clock), not assumed negligible from the interval
  choice alone.
- `tools/self_test.sh`: clean **PASS** (exact rank-match, 0 new
  `CHECK-FAILED`) after this fix, run against the real, previously-
  failing conditions.

**Remaining, disclosed caveat**: the one-time backlog replay needed to
*seed* the very first checkpoint after deploying this fix (or after any
genuinely fresh install) still pays the full from-scratch cost — this
fix makes every restart *after* that fast, not the very first one. A
narrow, unobserved-in-this-validation edge case also remains: if a
*third* distinct job starts before a crashed job's own end-time ever
gets one more sacct check (§4.4's own job-end-race fix), the cached
last-job-id check is abandoned in favor of the new job, same tradeoff
already disclosed there. Separately noted, not fixed (out of scope):
this replay's real CPU cost (processing tens of millions of historical
JSON records) drove one observed from-scratch run's RSS to ~60GB before
settling back down after completion — a pre-existing characteristic of
how much per-communicator history this aggregator keeps in memory,
unrelated to the checkpoint mechanism itself, flagging for whoever next
looks at long-term memory growth.

### 3.2 `[CHECK-FAILED]` vs. `[PIPELINE-DOWN]` — two distinct real health signals, easy to conflate

`[CHECK-FAILED]` (`_run_check()`, `alerting/alert_engine.py`) is a
per-check exception guard — an individual check function (`cv`/`mean`/
`pipeline_health`/etc.) throwing an uncaught exception, logged so one bad
check can't silently kill the whole poll loop. **Zero organic
occurrences** across this project's entire history — a real, meaningful
0, not a metric nobody's checked. The only historical entries at all
trace to deliberate test-session VictoriaMetrics restarts (each
producing a burst of `URLError: Connection refused` while VM was briefly
down), each individually explained and none reflecting a real
check-function bug; no check has ever failed for any other reason.

`[PIPELINE-DOWN]`/`[PIPELINE-RECOVERED]` (`alerting/pipeline_health.py`)
is a completely different, unrelated signal — a heartbeat dead-man's-
switch (>90s stale) built to catch the historical "run3 vm_url incident"
class of silent push failure. It DOES fire, routinely, around every
deliberate aggregator/`alert_engine.py` restart — this is expected, not
a failure, as long as every episode has a matching recovery. Both are
genuinely useful signals; neither is a proxy for the other, and `tools/
self_test.sh`/`run.sh` only check the former.

### 3.3 The shell-supervisor restart gotcha, confirmed live

Every `run_*_supervised.sh` (`alert_engine`, aggregator, VM, Grafana,
`iowait_logger`) is a long-running `while true; do ...; done` shell
loop — bash parses that loop body once when the supervisor process
starts, so an already-running supervisor keeps relaunching its child
with whatever command line was on disk *when the supervisor itself
started*, no matter how many times you `git pull` or kill the child
process. Confirmed live: adding `--summary-log` to
`run_alert_engine_supervised.sh` had zero effect on an already-running
deployment — `pkill -f alert_engine.py` + the supervisor relaunching it
still produced a child with no `--summary-log` flag, because the
*supervisor* predated the change. This exact gotcha was reconfirmed
twice more this project's own history, once for the Grafana supervisor
(§3.5) and once for the `alert_engine.py` supervisor during the VM
watchdog's own deployment (§3.6) — each time, killing only the leaf
process had zero effect, and killing the supervisor shell process
itself (then re-launching via `run.sh`) was what actually picked up the
change. See README §4.1 for the short, actionable version of this
gotcha.

### 3.4 `[DUMP-DISK-WARN]`/`[DUMP-DISK-CRITICAL]`/`[DUMP-DISK-RECOVERED]` — build-out

A third, independent dead-man's-switch, added after a real 48-GPU
Megatron validation run filled a 91GB shared volume with Inspector
dumps and crashed the whole pipeline with zero warning beforehand.
`node_aggregator_ref.py` pushes each host's own real `dump_dir` usage
(`agg_dump_disk_usage_pct{hostname=...}`, via `shutil.disk_usage` —
correct whether that path is node-local scratch or a shared mount,
since it checks the real path directly rather than assuming) every real
heartbeat cycle; `alert_engine.py` reads it back every 60s
(`DUMP_DISK_CHECK_INTERVAL_S`) and logs loudly into the same central
`var/alert_engine_supervised.log` a human is already watching. Default
thresholds are 80% (`WARN`) and 95% (`CRITICAL`), both overridable via
`DUMP_DISK_WARN_PCT`/`DUMP_DISK_CRITICAL_PCT` env vars on `alert_
engine.py`'s own process. Tested end-to-end against a real, genuinely-
filled tmpfs (not mocked) before shipping — confirmed `WARN` at 85%,
escalation to `CRITICAL` at ~97%, and `RECOVERED` once usage dropped
back down, each a real log line from the real code path, not inferred
from reading it. This does not stop a job or delete anything itself —
it's a loud warning you act on (clear old dumps, or stop the job), the
same "detect and report, never silently take invasive action"
discipline as every other signal in this project.

### 3.5 `[PATH-C-DOWN]`/`[PATH-C-RECOVERED]` — build-out

A fourth dead-man's-switch, closing a real, confirmed gap:
`iowait_logger.py` (the real eBPF io-wait data producer Path C/storage-
fault detection depends on) had no launcher anywhere in this project —
unlike the aggregator, `alert_engine.py` itself, VictoriaMetrics, and
Grafana, nothing ever started it, on any deployment, regardless of
whether bpftrace/tracefs itself worked. This is the full, root-caused
explanation for a real customer cluster reporting Path C silently
returning "not checked" (`io_ev=None`) on every query. `run.sh` now
launches it per node (`observability/run_iowait_logger_supervised.sh`,
same auto-restart convention as every other supervised process here),
and `install.sh`'s bpftrace/tracefs check now re-verifies the tracefs
bind-mount wrapper actually works after installing it (previously
assumed, never confirmed) — if the wrapper's own `unshare -m` call fails
with a real permission error (a jail/container lacking `CAP_SYS_ADMIN`,
or a seccomp policy blocking `unshare()`), both `install.sh` and the new
runtime watchdog now report that exact, actionable cause instead of
silently leaving Path C dead. A plain file-mtime staleness check is NOT
a safe liveness signal for this one (unlike DCGM hostengine): a
genuinely healthy, compute-bound job can go minutes with zero real disk
I/O, producing a legitimate gap in the data indistinguishable from a
dead agent by mtime alone — so `alert_engine.py` instead reads the
supervisor's own wrapper log for a real crash-loop signature (2+ exits
within a 120s window), which can't be confused with genuine healthy
silence. Also fixed a real bug found while testing this: `iowait_
logger.py` silently discarded any bpftrace output it didn't recognize,
including bpftrace's own real error text — so the exact diagnosis above
was reachable in the code but invisible in any log. Fixed to forward
unrecognized lines to its own stderr instead.

Tested end-to-end on a real cluster, not mocked: a genuine disk-bound
fault (`storage-ebpf/real_disk_fault.py`, cold read after `drop_
caches`) produced real `io_ev` evidence (12.46s aggregated iowait, 487
real block-I/O events) and a correct `CONFIRMED` `determine_storage_
path` verdict; the watchdog itself was verified by deliberately breaking
the tracefs wrapper (`[PATH-C-DOWN]` fired with the exact `unshare`-
permission diagnosis) and restoring it (`[PATH-C-RECOVERED]` fired once
the crash-loop window aged out).

### 3.6 `[GRAFANA-DOWN]`/`[GRAFANA-RECOVERED]` — build-out, plus the two real Grafana bugs found validating it

A fifth dead-man's-switch, closing a real gap found entirely by accident
during unrelated work: this project's own `grafana-standalone` instance
had been running for **over two days** serving real HTTP 503s (its
SQLite data directory, `/tmp`-based, no longer existed) with zero loud
signal anywhere — every other silent-failure class this pipeline watches
for already had one; Grafana itself had none. (This same stale instance
was independently rediscovered once more, in passing, during the worker/
GPU/rank-identity Grafana-rendering validation in §4.1 — same root
cause, same fix, not a second occurrence of the underlying bug.)
`alert_engine.py` now probes a real, authenticated endpoint (`/api/org`,
the same one `install.sh`'s own Grafana-reachability check already uses)
every 60s (`GRAFANA_CHECK_INTERVAL_S`), reusing the same generated admin
credentials every other part of this project already relies on
(`GRAFANA_ADMIN_CREDENTIALS_FILE`). `run.sh` exports `GRAFANA_URL`/
`GRAFANA_ADMIN_CREDENTIALS_FILE` from `cluster.env`'s own real, already-
discovered values into `alert_engine.py`'s environment before launching
it — the same env-var handoff pattern `IOWAIT_LOG_DIR_OVERRIDE` already
establishes, no new config-loading mechanism. A no-op, not a `DOWN`
alarm, when `GRAFANA_URL` isn't configured at all — Grafana is
documented elsewhere (README §4) as optional; the rest of this pipeline
works without it. This only fires once Grafana was configured/expected
and then stops answering correctly.

**A real, non-obvious design refinement, found only by deliberately
reproducing the actual outage, not by reading Grafana's own docs**: the
real failure is NOT "every request fails." Grafana's own SQLite
connection pool keeps already-open connections usable, so individual
requests fail *probabilistically* depending on whether they happen to
need a fresh one. Confirmed live, twice, while deliberately breaking a
real instance the same way the real outage happened (removing its data
directory out from under an already-running process, not a bare startup
failure): an unauthenticated `/api/health` ping **never** failed under
this condition (confirmed useless for this specific failure mode — it
apparently never touches the database at all), and even *sequential*
`/api/org` probes, a full second apart, kept reusing the same still-valid
pooled connection and returned a clean `200/200/200` against a confirmed-
broken instance. Only **concurrent** requests reliably force the pool to
open genuinely new connections, which is what actually fails — a 5-way
concurrent burst against the same broken instance reliably returned a
real mix of `401`/`500` failures every time it was tried, while the
identical burst against a genuinely healthy instance returned `200` on
every probe, every time. The watchdog fires `[GRAFANA-DOWN]` on any
single failure among `GRAFANA_CHECK_CONCURRENT_PROBES` (5) concurrent
probes, not a bare single request.

Validated end to end against this exact real failure, not assumed from
the design alone: stopped the real instance, removed its real data
directory, relaunched it pointed at the now-missing path (reproducing a
healthy-looking, port-bound process serving real errors, not a crash) —
`[GRAFANA-DOWN] :: 4/5 concurrent probes to .../api/org failed (e.g.
real HTTP 401 ...)` fired correctly. Restored the real data directory
and confirmed a genuine 10/10 concurrent-burst recovery independently
first, then `[GRAFANA-RECOVERED] :: .../api/org answering real HTTP 200
on all 5 concurrent probes again` fired on the watchdog's own next
cycle. `tools/self_test.sh`: clean PASS throughout, confirming this new
watchdog adds no regression to the rest of the pipeline.

**Found and fixed, while validating this watchdog: the port-3000-default
collision.** `cluster.env`'s own `GRAFANA_URL` had been wrong (`http://
localhost:3000` — a different, unrelated, pre-existing Grafana instance
on this host, not this project's own) because `install.sh`'s own "is
Grafana already running" probe hardcoded `http://localhost:3000`
unconditionally — no `GRAFANA_PORT` respected at all — and, more
fundamentally, neither `install.sh` nor `grafana-setup.sh` ever verified
the thing found was genuinely *this package's own* instance, just that
*something* answered on that port. (This was the same collision briefly
flagged in passing during the worker/GPU/rank-identity work in §4.1 —
the full root cause and fix live here.) Both are now fixed:

- **`install.sh`** now checks `http://localhost:$GRAFANA_PORT`
  (`GRAFANA_PORT`, same name/default `grafana-setup.sh` already used —
  it was never the one hardcoding 3000) instead of a bare hardcoded
  `http://localhost:3000`.
- **Both scripts** now perform a real identity check, not just a
  presence check, before trusting a found instance: if this package's
  own generated credentials (`var/grafana_admin_credentials.txt`)
  already exist, authenticate against the found instance's real
  `/api/dashboards/uid/straggler-detection-metrics` endpoint — this
  package's own dashboard ships with that exact, fixed, committed UID.
  A real HTTP 200 means the found instance accepts *our* stored
  credentials AND has *our* dashboard provisioned — the strongest
  ownership signal available (Grafana has no dedicated identity
  endpoint). Anything else (401/403 — wrong credentials, a genuinely
  different instance; 404 — right credentials but our dashboard isn't
  there) means it's NOT verifiably ours.
- **Honest, disclosed limitation**: if no credentials file exists yet (a
  genuinely fresh host where something else already occupies the
  configured port), identity cannot be verified either way — this is
  treated as unverifiable, not silently trusted either direction.
- **What happens on a confirmed mismatch, deliberately different per
  script's own real responsibility**: `grafana-setup.sh` (whose job is
  to *launch and own* an instance on that exact port) now fails loudly
  and stops (`FATAL: ... Set GRAFANA_PORT to use a different port ...`)
  rather than risk a second instance colliding on the same port.
  `install.sh` (whose job here is only *discovery*, already documented
  elsewhere as non-fatal — the rest of the pipeline works without
  Grafana) warns loudly and simply leaves `GRAFANA_URL` empty, rather
  than either silently wiring to the wrong instance (the original bug)
  or hard-aborting an otherwise-successful install over an optional
  component.

**Validated against the real, reproduced collision, both directions**:
with the real foreign instance still on port 3000 and this package's own
real instance on 3098 (confirmed live, both running simultaneously) —
`grafana-setup.sh` with the default port correctly printed `FATAL: ...
dashboard-by-UID check returned HTTP 401, expected 200` and exited 1
(previously: silently printed "already up, nothing to do" and exited 0,
the exact real bug); `install.sh` with the default port correctly warned
and wrote `GRAFANA_URL=""` into `cluster.env` (previously: silently
wrote the wrong instance's URL); `install.sh` run again with `GRAFANA_
PORT=3098` (this cluster's own real value) correctly confirmed identity
and wrote the real, correct `GRAFANA_URL="http://localhost:3098"` into
`cluster.env` — no manual edit needed, the real tool doing its own job
correctly. `tools/self_test.sh`: clean PASS after re-running the full
`install.sh`, confirming no regression to the rest of the pipeline.

A second, distinct occurrence of a related datasource-provisioning issue
is covered in §4.2 below (the *live Grafana datasource record*, not the
discovery probe, serving a stale URL after a prior fix to the on-disk
provisioning file).

### 3.7 `[VM-DOWN]`/`[VM-RECOVERED]` — build-out

A sixth dead-man's-switch, closing the gap `MAINTENANCE.md`'s own
repo-wide scan flagged as the single biggest remaining blind spot:
nothing watched VictoriaMetrics itself for "technically up, serving
wrong/stale data" — the same failure *class* that let Grafana run broken
for 2+ days before `[GRAFANA-DOWN]` existed. **Deliberately not a copy of
that fix** — a real investigation (two isolated scratch VictoriaMetrics
instances, the same real `victoria-metrics-prod` binary this cluster
runs, deliberately broken two different ways) found VM's own failure
modes are genuinely different from Grafana's SQLite-connection-pool
quirk:

- **Data directory destroyed while running**: `/health` keeps returning
  200, an already-visible query keeps its last cached value, and a new
  push is even silently accepted (`204`) — for a bounded window. VM's
  own background free-disk-space watcher then **panics the entire
  process** the next time it polls (confirmed live: as fast as ~10s, up
  to ~43s across two real runs) — unlike Grafana, this does **not** stay
  degraded-but-alive indefinitely; it hard-crashes.
- **Low free disk space** (the realistic, slow-onset production version
  of the above): confirmed live via `-storage.minFreeDiskSpaceBytes` —
  VM stays alive indefinitely, `/health` still returns 200, but every
  new push is explicitly rejected with a real HTTP `503` ("the storage
  is in read-only mode"). This is the genuine, reproducible "up but
  broken" VM analogue of the Grafana outage, and a bare `/health` check
  does not catch it — the same lesson Grafana's own `/api/health`
  already taught (it never touches its database either).

Because read-only mode rejects writes from **every** node's aggregator
simultaneously (a storage-wide state, not per-connection), the existing
per-host `[PIPELINE-DOWN]` watchdog would technically still fire for
each host individually here — but as N separate, individually-labeled
messages that don't themselves say "this is VM, not N independent
aggregator crashes." `[VM-DOWN]` makes that explicit: it reuses the
already-pushed `agg_aggregator_heartbeat` series this pipeline already
relies on (no new metric, no new cardinality) and asks the VM-wide
question directly — are **all** currently-supervised hosts stale **at
once** — reusing `self.pipeline_down`'s own freshly-computed state from
the same poll cycle rather than re-querying VM a second time. It's
paired with a direct `VM_URL/health` reachability probe, which
independently catches VM outright unreachable (the post-crash case
above) without waiting on heartbeat-staleness interpretation. Same 60s
cadence (`VM_CHECK_INTERVAL_S`) as the other periodic infra checks.

**Validated end to end against the real, reproduced failure, not assumed
from the design** — a standalone harness constructing the real
`AlertEngine` class (not a reimplementation) against a real, deliberately
broken scratch VM instance: a 160s healthy-baseline run produced zero
false `[VM-DOWN]`/`[PIPELINE-DOWN]` signals against the real 90s
heartbeat-staleness threshold; deliberately destroying the scratch
instance's data directory while running produced exactly one `[VM-DOWN]
:: URLError: ... Connection refused querying .../health` within ~10s of
the real crash, holding steady (no flapping) for the rest of the outage;
restarting the instance and reseeding a fresh heartbeat produced exactly
one `[VM-RECOVERED] :: .../health answering real HTTP 200 again, and at
least one supervised host has a fresh heartbeat` once the new heartbeat
cleared VM's own ~30-40s new-series visibility lag. Deployed to the
real, production-supervised `alert_engine.py` (restarted `run_alert_
engine_supervised.sh`'s own supervisor process itself, not just the leaf
`alert_engine.py` child — the same supervisor-restart gotcha §3.3
documents, reconfirmed for a different process) and re-validated against
the real, healthy production VM: zero false `[VM-DOWN]` over the full
run, zero new `[CHECK-FAILED]`. `tools/self_test.sh`: clean PASS.

**Aggregator-supervisor auto-restart isolation nuance (reconfirmed)**:
`node_aggregator_ref.py` runs under `run_aggregator_supervised.sh`'s own
restart-loop wrapper. Killing *only* the leaf `node_aggregator_ref.py`
child directly (not the wrapper) causes the wrapper's own loop to
auto-relaunch a fresh process within seconds — independent of, and
faster than, any explicit `run.sh`-driven restart. Re-tested live this
session (deliberate `kill -9` of the leaf PID on a real node): a new
process was already running within ~3 seconds, pipeline remained healthy
throughout (VM reachable, `CHECK-FAILED` count unchanged). This is
expected supervisor behavior, not a bug, but worth knowing if you ever
need to stop the aggregator itself rather than just bounce it: killing
the leaf alone will not stop it.

---

## 4. Observability tooling

### 4.1 Worker/GPU/rank identity in alert headers, and a composed incident summary in Grafana

**Gap-check against the original requirement's own wording, done first, before proposing
anything** (full investigation reported separately): "timings" and
"detection evidence" were already covered (the trajectory panel, the
peer-timing table, the z/mm staleness fix, §1.2). "Without overstating an
unconfirmed cause" was already satisfied by the existing CONFIRMED/
PROBABLE/UNCONFIRMED tiering. Two real gaps, confirmed by direct
inspection, not assumed: (1) `gpu_slot`/`role_rank`/`role_n` were
already computed and already pushed as labels on most metrics, but
**missing from exactly the places a human looks first** — the main
`[ALERT]` header (`format_alert()`) and both below-floor fallback
headers only ever showed a bare PID; the trajectory panel's own legend
never surfaced them either, even though they were right there in the
same metric's labels. (2) Confirmed via this Grafana instance's own
datasource provisioning (`observability/dashboards/provisioning/
datasources/*.yaml`): there is exactly **one** datasource here, a
Prometheus/VictoriaMetrics one — no Loki, no log-shaped source at all —
so the actual human-readable alert/incident text has never been visible
anywhere in Grafana, only in `var/alert_summary.log`/`var/alert_engine_
supervised.log`.

**Item 8 — worker/GPU/rank identity, PID kept exactly as-is.** The
`[ALERT]` header (all three emission paths — the main one and both
2-member fallbacks) now shows `gpu_slot=`/`role_rank=`/`role_n=`
alongside `rank=` (the real PID, unchanged), matching the format
`[STRAGGLER-INCIDENT]`'s own header already used. The two fallback paths'
own internal data (`per_member`) never tracked comm-local role at all —
confirmed directly, not assumed — so they show `role_rank=na role_n=na`,
the same honest "not yet/never discovered" convention `node_aggregator_
ref.py`'s own `phys_comm_role` dict already uses elsewhere, never a
guess. The trajectory panel's legend now leads with identity: `"z:
{{hostname}} GPU{{gpu_slot}} rank={{role_rank}}/{{role_n}} (pid=
{{member}}) (...)"`. The peer-timing table gained an "Organize fields"
transformation renaming/reordering `hostname`/`gpu_slot`/`role_rank`/
`role_n`/`member` into `Worker`/`GPU`/`Rank`/`OfN`/`PID`, left to right,
ahead of every other column.

**Item 9 — a composed incident summary, with zero new cardinality.**
Rejected pushing free-text alert content as a label outright: every
existing label in this pipeline is low-cardinality by design (a handful
of real values each); a label carrying literal alert text would be
unique per incident, creating a new, permanently-retained time series
for every alert ever emitted — a real, unbounded storage-growth risk,
not a cosmetic choice. Instead, a new table panel ("Composed incident
summary") joins four already-structured, already-low-cardinality signals
via Grafana's own `merge`/`organize` transformations: real `tier` (added
as a label — exactly 3 possible values, the same bounded-cardinality
discipline every other label here already follows, categorically
different from free text), `persisted_s` and `severity_ratio` (new value
metrics, reusing the exact label set `agg_straggler_incident_detected`
already established), and the already-existing `agg_path_c_verdict`.
Zero new free text, zero new unbounded cardinality.

**"Don't overstate" is a real visual constraint, not just a label.** The
Tier column has an explicit value mapping: `CONFIRMED` is colored red;
`PROBABLE` and `UNCONFIRMED` are colored the same (blue) as each other
and never as `CONFIRMED` — confirmed directly in the dashboard JSON
Grafana itself loaded (`fieldConfig.overrides` on the `tier` field), not
just intended. The tier label remains the only certainty signal; the
table's own color design cannot imply more confidence than it states.

**Real validation, actually rendered, not just "the JSON is correct"**:
no image-renderer plugin is installed in this environment (confirmed
directly, not assumed — checked `/api/plugins` and `rendererAvailable`),
so no literal screenshot was possible. (Validating this also surfaced a
real, pre-existing Grafana outage unrelated to this task — see §3.6 for
the full story and fix.) With a genuinely working instance, every change
was verified through **Grafana's own backend** — its dashboard API
(confirming the exact JSON it parsed and loaded, not just what was
written to disk) and its datasource-proxy query API (confirming real
data resolves through the identical path a rendered panel would use) —
against a real fault-injection event: the trajectory legend's real
inputs (`hostname=worker-1, gpu_slot=0, role_rank=8, role_n=16,
member=1062855`) confirm it renders as `"cv-z: worker-1 GPU0 rank=8/16
(pid=1062855) (AllReduce@2286960)"`; the peer-timing table's real,
organized-and-renamed columns correctly show the injected rank's own
elevated exec-time values (e.g. `0.1466` vs. peers' `~0.07-0.08`)
labeled `Worker=worker-1 GPU=0 Rank=8 OfN=16 PID=1062855`; the composed-
summary panel's four queries each resolved correctly for this same event
(`Tier=PROBABLE, PersistedS=54.30, SeverityRatio=1484.50, PathCVerdict=0`)
and share identical join labels, confirming they combine into one row.
The one honest, disclosed limit: the literal pixel result of a
client-side transformation and template substitution could not be
screenshotted in this environment — every input to that rendering was
independently confirmed real and correct through Grafana's own query
engine instead, the closest equivalent available here.

**Found and fixed a real regression in `tools/self_test.sh` itself**:
its own rank-extraction regex, `grep -oP '(?<=rank=)\S+'`, also matched
inside the new `role_rank=` field (a real substring collision — "role_
**rank=**8" contains the literal text the lookbehind searched for),
corrupting the extracted value and failing a genuinely correct alert on
the first post-change run. Fixed by anchoring on the header's own unique
`"[ALERT] rank="` prefix instead of a bare `"rank="`. Re-validated
immediately after: real injected rank exact-match PASS, `[ALERT]
rank=1072867 gpu_slot=0 role_rank=8 role_n=16 comm=... node=worker-1
...` — the real, new header text, against a live event.

**A real, recurring Slurm scheduler quirk, flagged but not fixed (out of
scope)**: hit three times during this session's own validation runs — a
just-cancelled self-test job lingers in `COMPLETING` state for several
minutes afterward (`State=IDLE+CLOUD+COMPLETING` at the node level,
`AllocTRES=` empty, nothing real left running on the node), at least once
long enough to block a subsequent `self_test.sh` run's own clean-queue
precheck. `scontrol update NodeName=... State=RESUME` (the standard
remedy for a stuck node state) was rejected as an invalid transition from
this specific state combination; waiting it out (a few minutes) was what
actually worked each time.

### 4.2 Grafana visibility for the wait-induced and role-baseline checks

**Gap confirmed before designing anything**: neither
`_emit_wait_induced_alert` nor `_emit_role_baseline_alert` pushed
anything to VictoriaMetrics — both only wrote to the log/alert-summary
path, so the two checks this session's own work added (§1.5, §1.6) had
zero Grafana visibility, even though every older detection path already
does via `_push_visibility_metric` (the same generic helper added for
`agg_straggler_incident_*`/`agg_path_c_verdict`/`agg_host_load_ratio`,
reused here unchanged — no new push mechanism).

**Metrics added, fired once per real firing (never per poll)**:
`agg_wait_induced_detected` (the culprit, `role_in_event="culprit"`),
`agg_wait_induced_peer_ratio` (one row per elevated peer, `role_in_
event="elevated_peer"`, each with its own measured ratio and `baseline_
source`), `agg_wait_induced_tier`; `agg_role_baseline_detected`
(`baseline_source` plus a `volatile` label — see below), `agg_role_
baseline_ratio`, `agg_role_baseline_tier`. All reuse the same bounded-
cardinality label discipline §4.1 already established (`baseline_source`
has exactly 3 real values: `role`/`role_cross_host`/`cross_comm_peer`;
`role_in_event` exactly 2; `volatile` exactly 2; `tier` exactly 3) — no
free text, no new unbounded series.

**`volatile` is a genuinely new, separate computation, not reused from
the live detection gate.** `_member_role_baseline`'s own broad-pool
volatility check (see §1.6's role_rank=1 fix) only ever returns `(None,
None)` either way when it degrades — the caller can't tell *why* from
that alone. Rather than thread a third return value through that
method's 2-tuple across its 4 existing, already-validated call sites
just to expose a dashboard label, a small separate read-only method
(`_role_shape_volatile`) recomputes the identical broad-pool median/MAD
check independently, called only at alert-emission time (a real firing,
not every poll cycle) — zero risk to the live detection path, guaranteed
to agree with it because it's the same formula.

**Panel 1 — "Wait-Induced Detections"**: one table, joining the three
metrics above via the same `merge`+`organize` pattern as the composed-
incident-summary panel (§4.1). The culprit row and each elevated-peer
row share the same comm but different `member`/`Role` values, so they
land as distinct rows in one table — a human can see, in one place, who
caused the wait and who was waiting, and which baseline resolved each of
them.

**Panel 2 — "Role-Baseline Detections"**: same pattern, one row per
independently-qualifying member, with its own ratio, `baseline_source`,
and `Volatile` column.

**Panel 3 — "Role-baseline exclusion health"**: a table of raw
`agg_role_baseline_excluded` rows (already existed as a metric, never
had its own dedicated table before — only the existing timeseries panel,
"Below-floor fallback activity"), organized into Worker/Rank/OfN/Bucket/
Coll/PID/Comm columns with `countRows` shown in the footer — a real,
current total exclusion count at a glance. A role/bucket that shows real
detections above but stays near-empty here is a sign the exclusion
wiring isn't firing for that shape.

**No-overstatement discipline reused exactly, not reinvented**: both new
panels apply the identical `Tier` column value-mapping §4.1's composed-
incident-summary panel already established — `CONFIRMED` red, `PROBABLE`/
`UNCONFIRMED` the same blue, never implying more certainty than the tier
label states. Both checks are PROBABLE-only today (via their own
stopgap flags), so neither panel will show red under current settings,
but the mapping is there unchanged in case either stopgap is ever
lifted.

**Found and fixed a second occurrence of the §3.6 datasource bug.**
Before validating anything, every query against this dashboard's
datasource returned HTTP 502 (`dial tcp 10.24.142.162:8428: connect:
connection refused` — `10.24.142.162` is `login-0`, confirmed via
`getent hosts`). The on-disk provisioning file (`observability/
dashboards/provisioning/datasources/local.yaml`) already correctly said
`http://worker-0:8428`; the *live* Grafana datasource record (`version:
1`, i.e. never edited since creation) was still serving the old, wrong
value from whatever provisioning read happened at its last startup —
Grafana datasources are only reconciled from disk at startup, unlike
dashboards (`updateIntervalSeconds: 30`), so a corrected file alone does
not fix an already-running instance. Fixed the same way as §3.6:
restarted the Grafana supervisor (killing the supervisor shell PIDs, not
just the leaf process — the §3.3 gotcha again); confirmed via `GET /api/
datasources/1` that the live record now reads `http://worker-0:8428`
post-restart.

**Real validation, through Grafana's own backend, against real fired
events — not just "the JSON is correct"**: no image-renderer plugin is
installed here either (same disclosed limit as §4.1). Triggered both of
this session's own fault scenarios (TP-inference, `STRAGGLER_TARGET_
RANKS=3` and `=1`) against the restarted pipeline. Confirmed, via `GET
/api/datasources/proxy/uid/<uid>/api/v1/query` (the identical path a
rendered panel uses, not a direct VictoriaMetrics query) using each
panel's own real target expression: `agg_wait_induced_detected`
correctly returned the true injected culprit both times (`role_rank=3,
baseline_source=role` on the first run; `role_rank=1, baseline_source=
role_cross_host` on the second — correctly varying with whichever real
fallback tier actually resolved it each time, not a fixed value);
`agg_wait_induced_peer_ratio` returned exactly the 3 real elevated peers
per event, each with its own ratio; `agg_role_baseline_detected`
returned 3 real rows per event with `volatile=1` on every role in this
shape (consistent with §1.8's fix — the whole shape family, not just
role_rank=1, now carries the raised floor once its broad pool qualifies);
`agg_role_baseline_excluded` returned exactly 4 real rows (one per comm
member) immediately after each event, directly confirming the
exclusion-push wiring fires for every qualifying member, not just a
subset.

### 4.3 `tools/incident_correlator.py` — design rationale and reference-table mechanics

**What this is, and the hard constraint it was built under.** Investigating
a real event today means manually cross-referencing three different log
formats (`[ALERT]`, `[WAIT-INDUCED-ALERT]`, `[ROLE-BASELINE-ALERT]`) and
remembering, from memory, which check is known-noisy or known-blind on
which real workload shape (§1.6/§1.7's own role-baseline findings, for
instance). This tool assembles that evidence into one read-only report.
**It never computes a new confidence score, never overrides the existing
CONFIRMED/PROBABLE/UNCONFIRMED tiering, and never states a comparative
lean ("more consistent with noise than a real fault") — every line it
prints is either a real value copied verbatim from VictoriaMetrics or
the raw log text itself, plus a pre-written, human-authored reference
note quoted verbatim.** A design variant that *did* state a comparative
lean was explicitly considered and explicitly rejected — that's a
separate, deliberate decision, not something to default into if this
tool is ever extended.

See README §6.5 for the real, worked walkthrough (a copy-pasted log line
in, a full assembled report out) and the two other invocation forms —
this is the part worth reading first if you've never used the tool.

**The one real limitation this tool always discloses rather than hides**:
raw `[ALERT]`/`[WAIT-INDUCED-ALERT]`/`[ROLE-BASELINE-ALERT]` log lines
carry no timestamp at all (confirmed directly against the real log
format — only this project's own supervisor bookkeeping lines do). A
finding only has a real, confirmable timestamp when it also pushed a
VictoriaMetrics metric (every wait-induced/role-baseline finding does; a
plain `[ALERT]` only does if it additionally crosses the sustained
`straggler_incident_detected` gate). Every line in this tool's output
that could only be matched by identity (hostname/comm/member), not
confirmed against the real requested window, is labeled, inline,
`IDENTITY-ONLY MATCH -- not time-confirmed` — confirmed live against a
real, long-aged-out comm (job 3745, well past VM's retention) where NO
real timestamp could be resolved for the identity at all: the tool
printed `window: UNRESOLVED (no real timestamp found)` up front and
marked every subsequent match `IDENTITY-ONLY MATCH -- not time-confirmed
(no real timestamp resolvable for this identity at all)`, rather than
silently presenting a decade-spanning log match as if it were a fresh,
confirmed one.

**The reference table**
(`observability/workload_reliability_reference.yaml`) is a plain,
hand-edited YAML file, reviewed alongside MAINTENANCE.md §6 — not logic.
It holds exactly the real check-reliability findings this documentation
already describes (the role_rank=0 Megatron bias, the TP-inference
wait-induced blind spot/fix, the role_rank=1/3 role-baseline
false-positive fixes), quoted verbatim, keyed by this project's own real
`workload_sig` string (reusing `_job_workload_sig`, no new
workload-detection mechanism). `UNVALIDATED` is the hard default for
anything not explicitly entered — confirmed live against a real
historical event whose workload signature didn't match either entry in
the file: the tool printed `resolved workload shape: unknown workload
shape` and the file's own `defaults.unknown_sig_note`, rather than
guessing it was close enough to a known shape. Found live, during
validation, that the exact-match design requires a real workload's
entry to list every real signature variant it's ever actually produced,
not just one: the identical real Megatron TP4/PP4/DP3 workload produced
two slightly different real sigs across two different jobs (one with an
extra small, legitimate message size) — fixed by making `workload_sigs`
a list per entry, re-validated against that same real event afterward.

**Explicitly read-only.** Every function in this tool only ever issues
VictoriaMetrics GET queries or reads a local log file — no writes, no
pushes, no interaction with the live `alert_engine.py` process. Safe to
run at any time, including while the pipeline is actively polling.

### 4.4 sacct job context — a second, authoritative source alongside squeue

**Investigation first, confirmed before writing any code**: this cluster
has no "Storm API" (zero matches anywhere in this repo, no Soperator
Slurm-job CRD found, `kubectl` unavailable from this environment) — that
specific integration remains genuinely unresolved and out of scope. What
the investigation did find, real and already installed: `sacct` works on
both the login node and worker-0/worker-1, and returns real `Start`/
`End` epoch timestamps plus real `NNodes`/GPU-rank counts for both
running and completed jobs — strictly more authoritative than the
`squeue -h -o "%i" --states=R` presence-only check this pipeline has
used everywhere since its own original import (confirmed via grep:
nothing in this pipeline used `sacct` before this). This is the same bug
class already patched reactively multiple times in this project's own
history (PP's role-baseline contamination §2.5, DLRM's stale throughput
reference §2.6, Hybrid's cold-start gap §2.3) — a long-lived aggregator
inferring job boundaries purely from squeue's live presence.

**Design decision, made explicit**: `sacct` **supplements** the existing
squeue-based mechanism; it does not replace it. `maybe_reset_workload_
state()` is untouched — job-boundary *resets* are still triggered
exclusively by `refresh_job_id()`'s own squeue poll, at the same `JOB_ID_
REFRESH_S=60` cadence as before. What `sacct` adds is a second,
independently-sourced signal used three ways:

**1. A disagreement cross-check** (`check_sacct_squeue_disagreement()`):
logs (never acts on) a `[SACCT-SQUEUE-DISAGREEMENT]` line if squeue still
attributes live activity to a job that sacct's own authoritative record
says already reached a terminal state — exactly the race `alert_
engine.py`'s own `_job_still_running()` docstring already describes
squeue as vulnerable to. **Real validation, zero false positives**:
across this entire deployment window, including two full real
job-boundary transitions from live fault-injection runs (jobs 3658 and
3659), `grep -c SACCT-SQUEUE-DISAGREEMENT` on both hosts' logs is **0** —
the two signals never disagreed in practice here, and the mechanism
never fired spuriously.

**2. Real job context on `[STRAGGLER-INCIDENT]`/`[ALERT]` text**
(`alert_engine.py`'s `_query_job_sacct_info()`), informational/display
only — confirmed, by `determine_confirmed_path(cause)`'s own signature
(it receives only `finding["cause"]`), that `finding["sacct_info"]` is
structurally unreachable from tier/decision logic, not just
conventionally kept separate. Real rendered output, from a live
fault-injection run (job 3659, rank 916244 on worker-1):
```
[STRAGGLER-INCIDENT] incident_id=worker-1:0x71cb232b5c1fb6:916244:2286960:AllReduce stat=cv host=worker-1 comm=0x71cb232b5c1fb6 member=916244 gpu_slot=0 bucket=2286960 coll=AllReduce severity_ratio=520.01 persisted_s=53.9 role_rank=8 role_n=16 job_elapsed_min=3.0 job_start=2026-10-02T08:12:08Z job_nnodes=2 job_nranks=16

[ALERT] rank=916244 comm=0x71cb232b5c1fb6 node=worker-1 type=compute confidence=PROBABLE severity=LOG-ONLY ...

Job context (informational only, from sacct -- does not affect confidence tier): 3.0 min into a job running since 2026-10-02T08:12:08Z, out of 2 node(s)/16 rank(s) allocated.
```

**3. Grafana job-boundary annotations** — two new Prometheus-datasource
annotation layers on the existing dashboard (`agg_job_start_time_
seconds`/`agg_job_end_time_seconds`), sourced from sacct's real
`time.start`/`time.end`. Confirmed zero job-boundary markers existed in
the dashboard JSON before this change (clean 26-insertion diff, no
reformatting). The real start/end epoch is encoded as the **sample's
own timestamp** (deliberately backdated, not "now") — confirmed live,
via a real push+export round-trip against this cluster's
VictoriaMetrics, that a backdated sample timestamp is stored and
returned correctly; Grafana's native annotation query places each
marker at the data point's own timestamp, not at a value reinterpreted
as time.

**Real bugs found and fixed during this feature's own validation (not
assumed correct from the design alone)**:
- **Missing `cluster` label**: the first working version pushed these
  two metrics without the `cluster="..."` label every other metric in
  this file carries via `base_labels()`. The dashboard's own annotation
  query filters on `cluster=~"$cluster"` — a label-absent series does
  not match a non-empty regex value, so the annotations would have
  silently rendered nothing. Fixed by adding the label to match
  convention.
- **Job-end race, found via a real missed case (job 3658)**: `squeue`'s
  own 60s poll and `sacct`'s own 60s poll are independently gated, no
  shared phase. Once squeue stops reporting a job, `self.slurm_job_id`
  goes empty immediately — and the original code unconditionally
  skipped the sacct check once that happened, so a job whose squeue
  presence disappeared before sacct's own next 60s check landed would
  **never get one more query to observe its real End time at all**.
  Confirmed directly: job 3658, a real ~3.5-minute fault-injection run,
  never got its end-time pushed under the original code. Fixed by
  letting `refresh_job_sacct_info()` target the last-cached job id for
  one more check when squeue's own id has gone empty but that job's end
  was never confirmed — re-validated on the very next real job (3659):
  end-time pushed correctly, ~65s after the job's real end, both hosts.

**Real validation evidence, from two live fault-injection runs after
both fixes were deployed**:
- `tools/self_test.sh`: clean **PASS** both times, exact rank-match
  (injected PID == alerted PID), 0 new `CHECK-FAILED`.
- Real multi-job sequence, boundaries confirmed correct: `3654 → 3655 →
  (3656, 3657) → 3658 → 3659`, each a real, distinct `workload-signature
  tracking reset: new job <id>` line at the real boundary, no spurious
  mid-job resets.
- `agg_job_start_time_seconds`/`agg_job_end_time_seconds`, confirmed via
  direct VM query (both `/api/v1/query` instant and a `/api/v1/query_
  range` matching Grafana's own annotation query execution path) for job
  3659: real start `1790930133` (`2026-10-02T08:35:33Z`) and real end
  `1790930351` (`2026-10-02T08:39:11Z`), both hosts, correct `cluster`
  label, exact sacct-sourced epoch (not quantized — the small offset a
  step-grid range query shows is that query's own evaluation-grid
  artifact, not the stored data, which carries the raw epoch exactly).
- **Job-boundary reset latency itself: unchanged, by design.** The
  reset trigger is still squeue-only (`JOB_ID_REFRESH_S=60`), per the
  explicit "supplement, not replace" scope for this change — sacct adds
  a cross-check and real context, not a faster reset path. No regression
  and no speed claim either way on the reset itself.

**Known, disclosed limitation**: a job that starts and ends within the
same ~60-120s window (faster than both independent polls can settle) can
still race the job-end fix above in the unlikely case a *third* job
starts before sacct gets its one extra look at the previous job's end —
the cached last-job-id check is abandoned the moment `self.slurm_job_id`
reports a new, different real job. Not observed in this validation's own
runs (all several minutes apart), not fixed further here — out of this
task's own scope to chase an edge case with no real reproduction.

**Explicitly excluded from this change, per scope**: "Storm API" is
unresolved and untouched — nothing here guesses at or builds toward it.

---

## 5. Performance and overhead investigation

### 5.1 Inspector plugin: lock-free ring buffer, replacing a silent single-slot data-loss bug

**Reconciliation, confirmed with direct evidence before writing any
code**: a prior, separately-documented development track had already
found and fixed this exact bug class and validated a ring-buffer
replacement — but that fix was never part of this repo's own history.
Checked directly: `git log --all` shows exactly one commit, ever,
touching `inspector.cc`/`inspector.h`/`inspector_plugin.cc` on any of
this repo's branches (the original "Add straggler-detection pipeline
package" import) — no revert, because there was never a prior patched
version to revert from. This branch's copy is byte-identical (`diff -q`,
confirmed empty) to NVIDIA's own raw upstream tree (`/root/nccl-2.28-src/
ext-profiler/inspector/`), and `install.sh` builds directly from this
repo's own `inspector-plugin/` source (NCCL is only linked against for
headers/`libnccl.so`), so this was never a wrong-file build problem
either — the source itself simply never had the fix. It was ported/
re-implemented here from the validated design, not cherry-picked.

**The bug**: every NCCL collective's completion wrote into a single
`completedCollInfo` slot per communicator, guarded by a plain
`pthread_rwlock_t`, with one dirty flag. If a second collective completed
on the same communicator before the dump thread's next wakeup
(`NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS`, default 500us), the
earlier record was silently overwritten — gone, with no counter, no log
line, nothing to show it ever happened. Confirmed live this session:
**~17-20% of real collectives silently lost** on TP4's own high-frequency
AllReduce communicator, at the default interval.

**Design (ported, then re-verified against this branch's own real
source before implementing, not assumed to transfer unchanged)**: a
fixed-size, lock-free, bounded ring buffer per communicator, single
producer (confirmed directly against this branch's actual NCCL 2.28.9
source: `ncclProxyProgressCreate`'s guarded, one-time `pthread_create`
guarantees exactly one proxy progress thread per communicator for any
comm not created via an explicit `ncclCommSplit`-with-share — which
Megatron/PyTorch's standard `new_group()`-based TP/PP/DP group creation
does not use, so this holds for every workload this project runs),
single consumer (the dump thread). 256-record capacity, re-confirmed
(not reused blindly) by compiling a real `sizeof()` probe against this
branch's own header: `sizeof(inspectorCompletedCollInfo) = 3168` bytes,
so 256 slots = exactly 792.0 KiB/communicator/rank — matching the prior
validation's own figure precisely, no adjustment needed. Overflow policy
is drop-newest (the producer never touches the consumer's own index,
preserving the lock-free invariant). A cumulative `queue_drops_total`
counter is incremented atomically on every drop and surfaced in **every**
dumped record (never requires scanning history to notice loss), plus a
rate-limited `[WARN]` log line on exact powers of two (1, 2, 4, 8, 16,
...) so a real, ongoing drop never floods the log but also never goes
unnoticed. All access is `__atomic_*` builtins (matching NCCL's own
atomic-builtin idiom) — the old `pthread_rwlock_t` guard is gone
entirely, along with the single dirty-flag field it protected.

**Validation — real before/after data, not assumed to transfer from the
prior track's own numbers**:
- Built clean via `install.sh`'s own exact `make` invocation, `-Wall
  -Wextra`, **zero warnings**.
- **coll_sn continuity (the direct measure of the bug this closes)**,
  same Megatron TP4/PP4/DP1 shape, same 500us default interval: the
  highest-frequency communicator went from **~17-20% missing (pre-fix)
  to 0.00% missing, 21,600/21,600 real records recovered (post-fix)** —
  `queue_drops_total` stayed `0` throughout (256 was comfortably
  sufficient for this shape's own real burst intensity).
- **MoE and DLRM (sparse AllToAll dispatch, this project's own
  highest-collective-frequency shapes) push past 256 capacity at
  points** — real, observed `queue_drops_total` of up to 15,506 (MoE) and
  7,924 (DLRM) on a short run. This is disclosed honestly, not hidden:
  the design's own guarantee is "loss is never silent," not "loss never
  happens" — these counts are real, visible, and exactly what the
  mechanism is for. Whether 256 should be raised for these specific
  shapes is a separate, open sizing question, not resolved here.
- **tools/self_test.sh**: clean PASS, exact rank-match, 0 new
  `CHECK-FAILED`, with the patched plugin loaded.
- **Full-pipeline re-check, same self_test.sh run**: `straggler_
  incident_detected`'s `persisted_s`/`severity_ratio` and the z/mm
  staleness fix's displayed values are internally consistent with each
  other (the displayed `z=3506.4` in the `[ALERT]` text matches the
  `severity_ratio=3506.42` in the paired `[STRAGGLER-INCIDENT]` line,
  the same same-timestamp correlation §1.2's fix established) and sit
  within the same wide, already-documented run-to-run range this exact
  reference scenario has shown across many runs this session
  (305.82-3506.42) — no new instability introduced. Path C fired
  correctly in the same run (ruled out, `target_iowait_us=0`, correct
  for a compute fault) — expected, since Path C's own evidence (eBPF
  `iowait`, not NCCL Inspector) is structurally independent of this
  plugin entirely; there is no mechanism by which this patch could
  affect it, and the same run confirms its machinery is unaffected.
- **TP4-standalone and Megatron fault-injection, reported honestly, not
  oversold**: real 200ms-sleep fault-injection runs on both shapes
  showed the raw data pipeline working correctly end to end (the
  injected rank's own real exec-time samples reported continuously
  throughout, zero gaps) and the aggregator correctly identifying it via
  `agg_persistence_fired` — but neither run's peer-elevation z-score
  cleared the firing threshold (TP4: max observed z=12.28 vs `MEAN_Z_
  THRESH`-class gates; Megatron: z=13.28 vs `CV_Z_THRESH=20.0`). This is
  **not** a ring-buffer regression: it reproduces identically on both
  shapes for the same reason — a real, pre-existing signal-strength
  characteristic of this specific tiny-model-plus-200ms-sleep
  configuration on a small-message TP4 collective, unrelated to data
  completeness. Flagged as a real, separate, open question (is the
  threshold miscalibrated for this shape, or is 200ms genuinely too
  small a fault at this message size) — not investigated further, out
  of this task's scope.
- **Overhead re-measured with the fix in place**, same 2-node TP4/PP4/
  DP1 shape, 500 iterations: **mean 192.05ms/iter (ON) vs 169.82ms/iter
  (OFF) — ~13.1% relative overhead**, against the pre-fix measurement's
  ~12.3% (172.31ms vs 193.50ms) — a ~0.8 percentage-point difference,
  well within this shape's own observed run-to-run noise (stdev 13-16ms
  on both runs). **The ring buffer itself adds no meaningful new cost**
  — the atomic ops and larger per-communicator footprint (792 KiB vs one
  `~3.2KB` struct) are not measurably more expensive than the lock it
  replaced. Dump volume for the same 500-iteration run: 553MB (patched)
  vs 395MB (unpatched) — a real, expected increase, since the fix now
  writes every real completed collective instead of silently collapsing
  bursts into one record.

**Follow-up — MoE/DLRM ring-buffer capacity, investigated further; burst
characterized as unbounded-looking, not just "needs a bigger number"**:
4 real MoE runs (256- and 4096-capacity, same 300-step config) showed
drop counts of 2, 592, 875, and 2,212 — a ~1000x spread for IDENTICAL
code and config. Direct analysis of the 875-drop case found the real
cause: a genuine, **sustained** (not instantaneous) production-rate
burst lasting ~2.7 real seconds, concentrated late in the run (~92-96%
through), affecting a different communicator and different ranks each
run — not the same expert/rank every time, and the affected rank's own
real token-routing load (`[MOE-TOKEN-LOAD]`) during the burst was
**within its own normal range**, not an outlier — ruling out "this
rank's own routing imbalance" as the direct cause. The dump thread
itself was confirmed still draining normally throughout (gaps <15ms,
same as quiet periods) — ruling out a consumer stall. The real root
cause remains undetermined (plausibly a downstream/peer-side
synchronization effect), and the drop magnitude shows no sign of
converging to a safe ceiling across the 4 samples gathered. **Given
this, the data does not support "just pick a bigger number" — this
looks like a large-but-not-clearly-bounded tail, not a well-characterized
peak rate.** No capacity change has been made; this is reported as an
open question for a deliberate decision, not resolved here.

**Follow-up — two validated overhead fixes, implemented**:
1. **`gRetireLock` scoped from one process-wide mutex to one per
   communicator** — confirmed, by re-reading P32's own docstring before
   narrowing it (not assumed safe from the pattern alone), that this can
   only ever *increase* the real-time retention safety margin P32's
   design relies on for any given communicator (no longer sharing queue
   depth with other communicators' retirements), never decrease it.
   Real new correctness point this introduces and had to be handled: a
   per-comm queue, unlike the old process-wide one, does not outlive
   every individual communicator by construction — any entry still
   waiting out its retirement window is now explicitly drained when that
   communicator itself is torn down, rather than silently leaked.
2. **`collEvtTrk` population/copy skipped entirely in lean mode** — the
   dominant cost inside `inspectorUpdateCollPerf`, confirmed (re-verified
   directly against current code, not assumed from history) to be read
   in exactly one place in the whole codebase (`inspectorCompletedCollVerbose`,
   itself gated behind the same verbose flag), so skipping the write
   when lean mode is active never exposes stale data to anything that
   reads it. Full behavior unchanged when verbose mode is explicitly
   requested.

Both built clean, `-Wall -Wextra`, zero warnings. Full revalidation:
`tools/self_test.sh` clean PASS; Megatron coll_sn completeness
reconfirmed at 0.00% loss, 0 drops (unchanged). **Real overhead,
re-measured twice (same methodology, same shape) to account for this
shared cluster's own run-to-run noise**: 14.84% and 9.21% individually,
**~12.0% pooled** (n=980 each) — against the pre-fix ~13.1%. A small,
directionally real improvement, **not a dramatic one, and reported
honestly as such**: the measured run-to-run spread (9.2-14.8%) is itself
larger than the ~1 percentage-point apparent gain, so this should be
read as "roughly consistent with 13.1%, with a modest improvement more
likely than not" rather than a precisely-quantified win.

**Follow-up — the 319,434-drop MoE outlier, resolved, not left
inconclusive**: one MoE sanity run with the patched plugin showed a
notably higher drop count than any sample gathered during the capacity
investigation above. Investigated directly rather than assumed either
way:
- **Config confirmed identical** to the capacity investigation's own 4
  samples (same unmodified `run_moe_rankfault.sh`, same 300 steps, same
  default fault-target rank, same 2 nodes) — the only real difference at
  the time was which Inspector build was loaded.
- **Re-ran 4 more times, controlling each suspect variable in turn**:
  Part-B-patched code (248,405 drops), the exact pre-Part-B
  ring-buffer-only code re-extracted from its own commit (308,959) —
  ruling out Part B's fixes as the cause — and a completely fresh, empty
  dump directory (324,629) — ruling out accumulated dump-file count. All
  5 samples (319,434/248,405/253,099/308,959/324,629) landed in the same
  **248K-325K range, with all 16 ranks affected every time** — a tight,
  repeatable cluster, not noise, and categorically different from the
  capacity investigation's own 4 samples (2/592/875/2,212, each
  affecting only 1-2 ranks). **This does not belong to the same
  distribution as the capacity investigation's samples.**
- **Found a real, plausible contributing factor, external to this
  plugin and to this package entirely**: `dmesg` shows this host
  receiving frequent, external `echo 3 > /proc/sys/vm/drop_caches` calls
  (full page-cache drops) throughout the session — present during the
  capacity investigation's own window too, so not a clean on/off switch,
  but measurably **increasing in frequency** over time (roughly every
  5-10 minutes early on, tightening to every 2-4 minutes by the time of
  the high-drop re-checks). This is triggered from outside this
  environment's own visibility (confirmed: this package runs inside a
  chroot/jail with no access to whatever schedules it) — not something
  this investigation can disable to test in isolation, and not something
  a code change in this repo can fix.
- **Plain conclusion**: the 319,434 figure (and its 4 reproductions) is
  real evidence of a separate, host/platform-level condition — not a
  ring-buffer or Part-B regression (both directly ruled out), not "just
  another point" on the capacity investigation's own distribution
  (ruled out by the 100x+ gap and the all-ranks-vs-1-2-ranks pattern
  difference). It needs its own, separate investigation by whoever owns
  this host's platform-level cache-management behavior — out of this
  package's own scope to fix.

### 5.2 Overhead measurements across workloads — the real, measured numbers, full investigation

- The one direct throughput-overhead measurement in this project's own
  history found Inspector's profiling overhead **dominating ResNet's
  iteration time by ~4.5x** (85ms with Inspector vs. 19ms without vs.
  7.57ms bare single-GPU compute) — it is **not documented** whether
  this was measured with `NCCL_INSPECTOR_DUMP_VERBOSE=1` or `=0`, or a
  more verbose debug mode still above that. Treat per-workload overhead
  as a real open question to measure on your own workload, not as
  negligible-by-default. An A/B "monitoring-off" harness exists
  (`workloads/nanogpt/run_shape1_nomonitor.sh`) for exactly this
  comparison; no documented result from actually running it was found
  in this project's own history.
- **A second, independent real measurement, different topology/scale —
  added alongside the ResNet number above, not replacing it**: a real
  48-GPU Megatron TP4/PP4/DP3 validation run measured **94.7ms/iter with
  Inspector on vs. 89.0ms/iter off — ~6.5% relative overhead** at full
  48-rank, multi-communicator scale. **Conditions, confirmed directly
  against this project's own commit timeline, not assumed**: this number
  was recorded (`6932b2e1`, as part of that same validation's writeup)
  roughly one hour *after* the lean-mode default flip commit
  (`f89230e2`) — and that flip commit's own message states plainly it
  was triggered by *this exact run*: "verbose Inspector dumps filled a
  91GB shared volume and crashed the pipeline." The Megatron workload's
  own launch scripts were not added to this repo until many hours later
  (`10cbdb94`), so the exact script invoked for this measurement isn't
  preserved in git history — but the causal link is clear: this number
  was measured **before lean mode existed as a default at all**, on the
  run that is on record as crashing specifically *from* verbose-mode
  dump volume. It also predates every one of this session's fixes
  (persistence-gating generalized to mean/`outlier_count`, `straggler_
  incident_detected`, the z/mm staleness fix, Path C window-matching,
  the worst-selection sign-bug fix — all committed hours to a full day
  later). Treat this number as **pre-lean-mode, pre-this-session**, not
  as a lean-mode baseline.
- **Re-measured under current conditions (lean mode confirmed, every fix
  above applied) — added alongside both numbers above, not replacing
  either**: this project's 2-node/16-GPU dev cluster can't run the full
  6-node/48-GPU shape, so the real, **unmodified**, already-committed
  `workloads/megatron/` scripts were run as-is — they auto-discover the
  available nodes and, on 2 nodes, naturally produce **TP4/PP4/DP1** (16
  ranks) — exactly *one* real DP replica of the original topology's own
  documented per-replica layout (2 nodes/replica, 2 PP stages/node,
  TP4/stage), a genuine proportional scale-down, not an approximation.
  Two full, back-to-back 500-iteration runs (steady-state stats below
  exclude the first 10 warmup/compile iterations; iteration 1 alone took
  17.5s/20.4s off/on, pure CUDA/Triton/NCCL init cost, clearly separable
  from steady-state):
  - **Off** (Inspector fully disabled — no `NCCL_PROFILER_PLUGIN`, no
    `NCCL_INSPECTOR_ENABLE` at all, same convention as `train_node_
    shape1_nomonitor.sh`): mean **172.31ms**/iter, stdev 17.87ms, min
    153.1ms, max 519.1ms, median 171.00ms (n=490).
  - **On** (Inspector enabled, lean mode — confirmed genuinely active by
    direct inspection of the real dump records themselves, not just the
    script default: zero `event_trace_ts`/`event_trace_sn` fields found,
    only the lean-only `dump_timestamp_us`, ~457 bytes/record, matching
    the ~462 bytes/record lean-mode figure already on record above):
    mean **193.50ms**/iter, stdev 16.79ms, min 173.4ms, max 489.9ms,
    median 190.70ms (n=490).
  - **Real overhead at this shape/scale: ~21.2ms absolute, ~12.3%
    relative — meaningfully higher than the 6.5% figure above, not
    roughly consistent with it.** Given the conditions finding directly
    above, this is not surprising in the direction one might first
    guess: the 6.5% figure is suspected to have been measured under
    *verbose* mode (normally the more expensive mode), yet this *lean*-
    mode number reads higher, not lower. The most likely real
    explanation is topology, not verbose-vs-lean: the two measurements
    are at genuinely different absolute scale (89.0ms/iter baseline at
    48-rank DP3 vs. 172.31ms/iter baseline at 16-rank DP1 here — nearly
    2x the per-iteration cost before Inspector is even added), and DP1
    means every rank is on the critical path with no data-parallel
    replica to overlap communication against, unlike DP3. This is
    reported as the most plausible explanation given what this
    investigation could check, not a proven root cause — profiling
    exactly where Inspector's runtime cost goes at this shape is real,
    separate follow-up work, not attempted here.
  - **Disk volume (lean mode, this shape)**: 395MB total dump volume
    across both nodes for these 500 iterations (~96s wall time) — per-
    record size confirms lean mode (above), but the *accumulation rate*
    (~123MB/min/node) is markedly higher here than the ~28.5MB/min/node
    figure already on record for `nanogpt`'s simpler DDP-only
    communication pattern — expected, not a regression: TP4/PP4
    generates far more real NCCL collective calls per iteration than a
    pure data-parallel AllReduce pattern, so lean mode's *per-record*
    reduction holds, but the *total accumulated volume* is real and
    workload-dependent, not a single universal rate.
  - **Disk-usage guard**: both nodes' root volume held at a steady,
    pre-existing 92.7% throughout this entire test (unrelated to it —
    same value before, during, and after; a real, already-filled 2TB
    volume with only 150GB free, flagged separately, not fixed here).
    `[DUMP-DISK-WARN]` kept firing at its already-standing rate; the 95%
    `[DUMP-DISK-CRITICAL]` threshold never fired — the guard behaved
    correctly, neither silent nor falsely escalating, for a normal run
    of this length.
- **`NCCL_INSPECTOR_DUMP_VERBOSE` now genuinely defaults to lean mode
  (`=0`) across all 17 launch scripts that set it** — a real, previously
  undocumented discrepancy existed here (this used to correctly flag
  that 14 of the 15 checked scripts actually ran `=1`, contradicting
  `INVENTORY.md`'s claim that lean mode was already the default; that
  discrepancy is now fixed at the source, not just in the docs). This
  session's own real 48-GPU Megatron validation run hit the verbose-mode
  cost directly: verbose dumps filled a 91GB shared volume and crashed
  the pipeline. Traced field-by-field against the Inspector plugin's own
  C++ source and empirically A/B-validated on this project's own
  regression-reference shape (`workloads/nanogpt/run_straggler_
  nanogpt.sh`, rank 3, 200ms injected sleep) before flipping anything:
  - **Same detection, both modes**: identical `PROBABLE`/`LOG-ONLY` tier
    and exact rank/host attribution under verbose and under lean — the
    live pipeline (`node_aggregator_ref.py` → VM → `alert_engine.py`)
    only ever reads `coll_exec_time_us`/`coll_msg_size_bytes`, emitted
    identically in both modes; the verbose-only `event_trace_ts`/
    `event_trace_sn` fields are read by zero code in the live path.
  - **~8.2x smaller per record** (3,804 vs. 462 bytes/record, measured
    across full dump files from the same fault scenario), and **~8.6x
    less disk accumulated** for the same workload (87MB vs. 8.4MB/node
    over a comparable window).
  - **A real caveat, not just a win**: a genuinely healthy job (not
    throughput-throttled by an injected fault) accumulates disk much
    faster in wall-clock terms than a fault-injection test suggests —
    measured **~28.5MB/min/node even under lean mode** on a healthy
    16-rank run (the fault scenario's own injected sleep synchronously
    throttles every rank's collective, so it under-represents real
    production disk cost). Lean mode is a real, substantial reduction,
    not a guarantee against filling disk on a long real run.
  - **A small number of offline, non-live tools still genuinely need
    verbose mode**: `classifier.py`'s batch dump-replay path,
    `calibration.py`, `transient_latency.py`, and the MoE two-stage
    detector's `arrival_order.py` all read the verbose-only `event_
    trace_ts.coll_start_ts` for microsecond-scale cross-rank timing
    (arrival order, clock-offset calibration) — `coll_exec_time_us` is
    explicitly documented in `arrival_order.py`'s own code as already
    proven unreliable for that purpose, and lean mode's only timestamp
    (`dump_timestamp_us`, recorded at write/flush time) carries the same
    kind of imprecision for a different reason. Opt in explicitly by
    setting `NCCL_INSPECTOR_DUMP_VERBOSE=1` in the shell that launches a
    `run_*.sh` script (propagated into the container via the existing
    `--export=ALL`) before using one of these tools.
- **Real, escalating resource costs found during this project's own
  sustained/long-run testing — all now fixed by caps already shipped in
  this package, but the real historical numbers are worth knowing**: the
  aggregator's own per-process memory grew **unbounded from ~30MB to
  16.3GB RSS over ~50 minutes** under FSDP's higher event volume before
  a 500-entry rolling cap was added (`aggregator/node_aggregator_ref.py`);
  the supervised `alert_engine.py` log grew **31.8MB/165k lines with
  zero cap over ~3 real hours under heavy fault-injection load** before
  `logrotate` (50M/rotate 10) was added; the iowait logger's own log grew
  from a baseline **~592 rows/hour** to **~3.5 rows/sec during an active
  real storage fault** (~410 B/s worst case) before a 10MB rotating-
  handler cap was added. A fresh install already ships all three fixes —
  these numbers are what this project's own history found before they
  existed, not a current risk, but understand them before assuming
  "leave it running forever" is free on a workload this project hasn't
  already sustained-tested.
- Baseline alert rate under normal (non-fault) operation: two
  independently-measured figures exist in this project's own history —
  **~22-25/hour** and, from a separate 3-hour blind-test session,
  **~22-38/hour (settling around ~32/hour across three runs)** — both
  PROBABLE/LOG-ONLY, not paged.
