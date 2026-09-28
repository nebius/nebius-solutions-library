# Handoff: Hybrid (TP+PP combined) below-floor fault-detection fix

You are starting fresh. This document is the complete context you need —
do not re-derive anything below from scratch; confirm/extend, don't
re-discover.

## Confirmed root cause: cold-start starvation (genuinely different from PP's problem)

Hybrid's below-floor P27.2 timing-asymmetry fallback (same mechanism PP
and TP2 use — `_timing_asymmetry_fallback_evaluate` in
`alerting/alert_engine.py`) has a real, confirmed gap: it only fires
once a genuinely external comparison basis (a live cross-comm-peer or a
cross-job role-baseline history) already exists. When nothing exists
yet, `_member_role_baseline` returns `(None, None)` (a real, honest cold
start — not a bug), and `_cross_comm_peer_median` also returns
`(None, None)` if no other same-shape below-floor comm is currently
active on that host either. With both baselines unavailable, the
function returns `None` and the fault goes undetected — **not because
the signal is weak** (confirmed the opposite: this fallback's signal on
Hybrid/TP-shaped pairs is unusually strong, 300-500x in the original
dedicated investigation).

**This is genuinely different from PP's problem** (see
`HANDOFF_PP_FIX.md` if you want the contrast, though you do not need
that document to do this task): PP's historical data is **abundant but
100% wrong** (every entry reflects the same repeated fault). Hybrid's
problem is that the data is **simply absent or insufficient** — not
wrong, just not there yet. Do not conflate these; the two problems need
different fixes, and a fix aimed at one will not help the other (this
was explicitly checked and confirmed during the investigation that
produced this handoff).

## What "enough history" looks like — the precise condition already observed

During the Stage 5 re-run, Hybrid's fault **was observed firing
correctly** (a real `PROBABLE` alert correctly naming the injected
target) — directly contradicting this project's own older documentation
that claimed the fallback "never fires" on Hybrid. Tracing why: by the
time Hybrid ran in that session, **several other same-session shapes
(TP2, TP4, FSDP, and others) had already run on the same two physical
nodes**, using the same GPU slot numbers and, critically, the same
message-size buckets Hybrid's own TP-shaped comm uses. This left behind
exactly the kind of live cross-comm-peer data (other below-floor comms,
same host, same bucket/coll, different physical members) and/or
cross-job role-baseline history that `_cross_comm_peer_median`/
`_member_role_baseline` need. The surrounding alert trace explicitly
showed `baseline_source='cross_comm_peer'` and `baseline_source='role'`
being used successfully for Hybrid's real fault in that run.

**The precise, falsifiable condition**: Hybrid's fallback fires when
at least one of the following is true at the time it's evaluated —
(a) another below-floor comm with genuinely different physical members
is *currently live* on the same host, matching Hybrid's own comm's
`(bucket, coll)`, or (b) cross-job role-baseline history exists for
Hybrid's own `(role_rank, role_n)` shape from an earlier, different job
sharing the same workload signature. When run in genuine isolation (the
condition under which the original "never fires" finding was
documented), neither exists, and it does not fire. A related shape,
long-context (also below-floor, same mechanism), hit this exact
cold-start case directly in the same Stage 5 session — strong signal,
zero alerts, because it was long-context's own first-ever run with
unique buckets nothing else had touched.

## Investigate first: can this be solved by deliberately seeding history, or does it need a code fix?

Two genuinely different approaches — investigate both before choosing:

1. **Operational/test-setup fix (no code change)**: deliberately run a
   compatible below-floor shape (e.g., a plain TP2 job) immediately
   before Hybrid's own test, on purpose, as part of Hybrid's own test
   setup/documentation — seeding the cross-comm-peer data Hybrid's
   fallback needs before Hybrid itself runs. This may be sufficient for
   testing/validation purposes, but consider whether it's an acceptable
   answer for a **real production deployment**, where a customer running
   Hybrid as their only/first workload on a fresh cluster would still
   hit the cold start. If this is only a test-convention workaround and
   not a real fix for a real deployment, say so explicitly rather than
   presenting it as resolved.

2. **Code-level fix**: reduce how much history/how live a peer needs to
   be before the fallback engages — see the specific candidate direction
   already scoped below.

## Already-rejected direction — do not re-attempt

**A per-comm historical self-baseline (comparing a member's current
value against its own past `avg_over_time` history) was already
investigated and found definitively unsafe.** This is exactly the
design P27.2.3 already reverted, for a confirmed, live, reproducible
reason: this hardware's own real per-collective timing spikes
(100-1000x, unpredictable, affecting both members, unrelated to any
real fault) land in one side of a self-history ratio and not the other
purely by chance, producing spurious CONFIRMED/PAGE alerts on
completely healthy pairs. This was confirmed via a second reviewer's
independent raw-data cross-check in this project's own history — see
`_timing_asymmetry_fallback_evaluate`'s own docstring (P27.2.3 section)
in `alerting/alert_engine.py` for the full account. **Do not propose or
implement a return to self-history comparison for Hybrid.** This applies
regardless of how the idea is framed (e.g., "just for the cold-start
case" is still the same rejected mechanism).

## The real, more promising direction already scoped (not yet implemented)

Relax `_cross_comm_peer_median`'s hostname scope from same-host-only to
**job-wide** (any host in the same Slurm job), so e.g. Hybrid's
worker-0 TP pair can serve as a genuine, physically-different-member
peer group for worker-1's TP pair in the same job, instead of requiring
an entirely separate below-floor comm to already exist.

Specifically, in `_cross_comm_peer_median` (`alerting/alert_engine.py`,
~line 1799):
- The **primary "live peer" branch** (~lines 1875-1882) currently has
  **no `slurm_job_id` scoping at all** — it's implicitly scoped to "this
  job" only via the shared `hostname` filter. Removing the `hostname`
  filter to allow job-wide search **requires explicitly adding
  `slurm_job_id` scoping** to this branch first, or you will introduce
  real cross-job contamination (a different job's stale below-floor
  comm on a different host bleeding into this job's peer pool).
- The **"no live peer, same-job last-value fallback" branch**
  (~lines 1883-1894) already scopes by `slurm_job_id` via
  `_comm_slurm_job_id`, but still additionally filters by the same
  hostname — that hostname restriction is what would need relaxing.

This is flagged as **correctness-sensitive** — it changes a shared
function TP2 and PP also rely on for their own already-working
detection paths. Any change here needs:
- **n>=5 independent validation** on Hybrid's own cold-start fault case
  (a genuinely isolated Hybrid run, no other shape run beforehand, to
  test the actual cold-start scenario this fix is meant to solve).
- **An explicit regression check against TP2 and PP's already-working
  behavior** — confirm neither's real fault detection or false-positive
  rate changes. (Note: at the time of this handoff, PP's own detection
  is a separate, currently-broken investigation — see
  `HANDOFF_PP_FIX.md` — so "PP's already-working behavior" may need
  re-scoping depending on that other session's progress. Check its
  status before relying on PP as a stable regression baseline.)

Not implemented or prototyped yet — this is this session's real work.

## Shared-process safety rule (read before touching any live process)

**Before restarting `alert_engine.py` or any `node_aggregator_ref.py`,
confirm no other session (PP's fix, DLRM's re-test) has a test actively
in flight.** These sessions may run in parallel against the same shared
cluster. Check `squeue -u "$USER" -h` and the aggregator/alert_engine
supervisor process states before restarting anything, and coordinate if
another session's job is running. A restart you trigger will affect
every other session's in-flight test too. This is especially relevant
here: if you modify `_cross_comm_peer_median`, restarting `alert_engine.py`
to pick up the change will also change behavior for any other session's
in-flight TP2/PP test.

## Standing project rules (carry forward, do not relax)

- **n>=5 independent validation** for any new detection signal or
  threshold before trusting it.
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
- Restart `alert_engine.py`/aggregators via clean `SIGTERM` to the leaf
  process (letting the supervisor wrapper auto-relaunch), never
  `kill -9` — and always confirm the new process's start time postdates
  your source-file edit's mtime before trusting a restart picked up
  your change.
- Do not force an incomplete fix if the real investigation shows this
  needs larger design work — a precise, scoped proposal is a complete
  and acceptable outcome.
