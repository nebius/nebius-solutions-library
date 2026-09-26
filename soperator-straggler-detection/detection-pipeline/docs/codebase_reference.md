# GPU Straggler Detection — Codebase Reference

Real, file-by-file documentation of every script in `/root/P20c_alerting/`
and `/root/P18k_classifier/`, plus the patched NCCL Inspector plugin.
Written from the actual current code (this session), not from memory or
prior reports where the two disagree — where a limitation is only known
from project chat history and not from a file, that's noted explicitly
rather than invented.

## Pipeline overview

Per-node `node_aggregator_ref.py` reads NCCL Inspector's raw per-collective
dump files, computes rolling peer-relative statistics (mean/CV exec-time,
outlier rate, job throughput), and pushes them to VictoriaMetrics.
`alert_engine.py` polls VictoriaMetrics, decides which statistics have
fired, and for each fired alert calls into `P18k_classifier/classifier.py`
to gather real cause-evidence (DCGM clocks/thermal/power, IB/NVLink, host
CPU, and now storage via eBPF) and decide a confidence tier (CONFIRMED /
PROBABLE / UNCONFIRMED). `coverage_guard.py`, `pipeline_health.py`,
`thresholds.py`, `persistence.py`, and `health_exclusions.py` are shared
support modules used by the above two. `ras_alert.py` is a separate,
narrower RAS-error alerting path.

---

## `/root/P20c_alerting/node_aggregator_ref.py`

**What it does**: runs on (or reads dumps from) a single node, tailing
NCCL Inspector's per-PID raw dump files, and computes/pushes real,
windowed peer-relative statistics per (communicator, member, bucket) to
VictoriaMetrics via `/api/v1/import/prometheus`.

**Role**: the only real data source in this pipeline — everything
downstream (alert_engine.py, the classifier) consumes what this pushes.

**Key configurable values (real, current)**:
- `HEARTBEAT_INTERVAL_S = 10.0` — how often `agg_aggregator_heartbeat` and
  `agg_member_gpu_slot_index` are re-pushed (the dead-man's-switch and
  gpu_slot_index re-push cadence).
- 500-entry rolling cap on `all_vals`/`rate_samples` per (comm, bucket,
  member) — bounds memory/CPU growth on long-running jobs (a real,
  measured fix: unbounded growth hit ~16.3GB RSS over ~50 minutes before
  this cap was added).
- `workload_signature(n_comms, coll_types, msg_size_bins)` — the
  self-calibration key used by `coverage_guard.py`'s throughput baseline;
  fixed this project's own recurring failure pattern (previously collided
  between different workloads, e.g. ResNet and nanoGPT could hash to the
  same signature).
- `agg_member_gpu_slot_index` — pushed from Inspector's own dump-schema
  `gpu_slot_index` field (see Inspector section below); `-1` means the
  plugin couldn't capture it for that process, reported honestly, not
  silently treated as slot 0.

**New this session (P27.5) — `maybe_check_job_throughput`'s stability
check was structurally unreachable for MoE**: the throughput-reference
readiness gate required `live_denom` (sum of currently-scored
bucket-member counts) to be BYTE-EXACT identical across `PERSIST_WINDOW`
(3) consecutive 10s checks before `workload_signature()` was ever even
called. Confirmed live via direct VictoriaMetrics query
(`agg_job_workload_sig_info{slurm_job_id=...}`) that this NEVER happened
for MoE across three separate test runs (healthy, netfault, and a
dedicated 700s seeding run 5.4x the original test length) — zero samples
of that metric ever existed for any of them. Root cause: MoE's real,
data-dependent expert routing keeps discovering new, rare message sizes
indefinitely, so `live_denom` never truly stops growing, even over 700+
seconds. Fixed by relaxing the stability check from exact equality to a
relative tolerance — `abs(live_denom - prev) <= CALIB_MIN_FREQ_FRAC *
prev` (reusing this file's own existing 0.02 "significant activity"
threshold, no new constant), which correctly still rejects a genuinely
still-ramping-up job (large relative jumps) while tolerating a large,
mostly-settled denominator absorbing a continued trickle of rare new
buckets (small relative jumps). An optional, secondary
`THROUGHPUT_CEILING_CHECKS` backstop (`10 * PERSIST_WINDOW`, scaled off
the existing constant) accepts whatever `live_denom` exists after a
generous real-time budget even if relative tolerance never settles —
confirmed inert in every test this session; relative tolerance alone did
the real work every time. **Live-validated**: a fresh MoE healthy run
established a real throughput reference (self-calibrated cold start,
correctly — zero prior history existed yet) at **167s** (worker-3) and
**196s** (worker-2) from real training start, via direct
`timestamp()`-verified VictoriaMetrics query — compare to *never*, not
even at 700s, before this fix.

**New this session (P27.6) — `query_throughput_history`'s exact-string
signature matching was too strict**: `workload_signature`'s message-size
SET component records every distinct size seen by stabilization time;
a genuinely slower real run (a real fault, or ordinary variance) can
reach that point having observed one fewer rare size than a healthy
reference run did. Confirmed live: a real MoE netfault run's own
signature differed from 3 real healthy reference runs' signature by
exactly one rare entry (message size `19951585`) — a one-entry
difference caused a complete string mismatch, falling back to
self-calibration (which is separately, already known to be blind to a
fault present since job launch — see the `attempt 3`/`attempt 4` history
in this function's own docstring), so `uniform_slowdown` never fired
despite a real, confirmed 8.4x AllToAll slowdown present in raw data the
whole time. Fixed by decoupling the two roles the sig string used to
serve: `n_comms` and `composition` (confirmed, real STRUCTURAL
discriminators — DDP=`(1,{AllReduce})`, FSDP=`(1,{AllGather,AllReduce,
ReduceScatter})`, TP=`(6,{AllReduce})`, MoE=`(1,{AllReduce,Recv,Send})`
are pairwise distinct on this alone) are still matched exactly, no
tolerance, so different workloads can never blur together regardless of
message sizes. The message-size SET (the SCALE discriminator) is now
compared by Jaccard distance (symmetric difference over union), again
reusing `CALIB_MIN_FREQ_FRAC` (0.02) as the maximum tolerated fraction of
the union that may differ — two genuinely different workloads' real
tensor/gradient sizes are structurally almost entirely disjoint (this
file's own earlier ResNet-vs-nanoGPT signature-collision finding already
established this), so their Jaccard distance sits far above this floor;
the tolerance only ever absorbs a SAME workload's own natural
run-to-run variability in which of its rarest sizes had appeared by
stabilization time. **Implemented and code-reviewed this session but NOT
YET LIVE-VALIDATED** — the old cluster's worker-2/worker-3 became
occupied by unrelated external jobs before the decisive re-test (a real
MoE netfault run, confirmed to now match 3 real healthy reference
signatures and correctly engage the cross-job path) could be completed.
**This is the first thing to re-run on a new cluster once VM and the
pipeline are standing** — see `migration_readiness.md`'s new cluster
status section.

**Known limitations** (from this project's own prior reports/comments):
- Bucket calibration requires 10 occurrences of the same (bucket, coll)
  pair before it's "scored" — a communicator with fewer than 10 real
  occurrences of its own message size in a short job never gets scored.
- `agg_samples_seen` and similar per-sample pushes are pushed with the
  REAL sample ingestion timestamp (fixed this project's own
  query-eval-time-vs-real-timestamp bug class — see alert_engine.py below).

---

## `/root/P20c_alerting/alert_engine.py`

**What it does**: the live, real-time alerting loop. Polls
VictoriaMetrics every `poll_interval` (default 3.0s) seconds, discovers
live hosts/communicators/buckets, checks mean/CV/outlier-count statistics
for each, and for anything that fires, calls
`P18k_classifier.build_single_rank_finding` (via the `build_finding_for_alert`
adapter) to get a real tier + cause evidence, then formats and emits an
alert.

**Role**: the thing an operator actually watches; also runs the
network/NVLink/host-contention checks and the P21.6.1 2-member-communicator
DCGM fallback (for communicators too small — below `SELF_DETECTION_FLOOR`
— for peer-relative stats to work at all).

**Key configurable values (real, current)**:
- `SELF_DETECTION_FLOOR = 3` — communicators below this member count can't
  compute peer-relative mean/CV at all (matches
  `node_aggregator_ref.py`'s own guard).
- `DCGM_FALLBACK_CHECK_INTERVAL_S = 20.0`, `NETWORK_CHECK_INTERVAL_S = 60.0`,
  `HOST_CHECK_INTERVAL_S = 20.0`, `NVLINK_CHECK_INTERVAL_S = 60.0` — cadences
  for the out-of-band background checks (all run in separate threads so
  they don't block the 3s poll loop).
- `FRESH_THRESH_S = pipeline_health.HEARTBEAT_STALE_THRESH_S (90.0)` — how
  old a VictoriaMetrics row can be before it's treated as stale/not-current.
- `IOWAIT_LOG_DIR = "/root/P20c_alerting/iowait_logs"` (P26.5-maintenance,
  this session) — where `iowait_logger.py`'s per-host storage-evidence
  logs are read from for the live storage check.
- `PAGE_WORTHY_TIERS = {"CONFIRMED"}` — only CONFIRMED pages a human
  (real data basis: a 3-hour blind run showed 0 false positives at
  CONFIRMED vs. 22-25/hour false-positive rate at PROBABLE).

**Fixed this session (P27-maintenance)**:
- `_find_true_rank0_member` used plain `_query_instant` with no
  freshness check — the exact sibling bug already fixed in
  `_query_gpu_slot`/`_comm_cross_node_members` (P21.6.1), just not
  applied here yet. Now uses `_query_instant_real_ts` + `_is_fresh_row`,
  same fix shape. Validated: correctly returns `None` (not a stale
  cross-job answer) when nothing fresh exists.
- `build_finding_for_alert` no longer builds a "placeholder rank" to
  reverse-engineer the right node/GPU-slot through `worker_of()`/
  `rank % 8` — it now passes its own real hostname and real
  `gpu_slot_index`-derived slot straight through via
  `build_single_rank_finding`'s new `host=`/`local_idx=` parameters. This
  was the single most impactful finding of this session's cluster-
  topology audit: it's the one place the hardcoded 2-node/8-GPU
  arithmetic reached into the LIVE production path. Live-validated with
  a deliberately different-shaped hostname (`"gpu-node-3"`).

**New this session (P27-era) — `_timing_asymmetry_fallback_evaluate` /
`_emit_timing_fallback`**: a second, independent 2-member fallback,
running alongside (never replacing) the existing DCGM-based
`_dcgm_fallback_evaluate`. Closes a real gap that DCGM structurally
cannot: a pure software `time.sleep()`-based straggler produces NO DCGM
signature at all (both members read nominal `sm_clock`/power), so
`_dcgm_fallback_evaluate` correctly, honestly returns `None` for exactly
this fault class — not because the fault isn't real, but because it's
the wrong evidence type for it. Confirmed live, twice independently
(TP_SIZE=2 and TP-inference): the TRUE straggler's own
`agg_mean_exec_time_us` reads LOW (it sleeps BEFORE entering the
collective, so its own recorded collective-timing window is short/
normal), while its healthy PARTNER's reads elevated (the partner is the
one actually waiting on the late arrival) — the inverse of what a naive
"whichever member looks elevated is the straggler" rule would conclude.
- Sources evidence from `agg_mean_exec_time_us` — already pushed for
  every member regardless of comm size (no new metric invented).
  Compares each member's current mean against the CROSS-COMMUNICATOR
  peer median for the same `{hostname, bucket, coll}` (pooling every
  other same-shape communicator's members on the job), not a same-run
  self-history baseline — an earlier design iteration using self-history
  was found live to false-fire on ordinary warmup dynamics before this
  redesign.
- Reuses `_find_true_rank0_member` (real-identity lookup, not a static
  rule) to exclude the already-documented rank-0 background-bias
  artifact — an earlier design iteration without this exclusion was
  found live to false-fire specifically on rank-0's own TP pair, with
  near-identical numbers across independent runs, confirming it as a
  real, reproducible bias rather than noise.
- Reuses `p18k.PATH_B_AND_TIMING_SUPPRESS_RATIO` (0.6, the same
  suppression ratio DCGM's own Path B already uses) as the tolerance for
  "is this member suppressed relative to peers" — no new threshold
  invented. Persists via the existing `CVPersistenceTracker` (3-of-3
  consecutive real samples), same discipline as CV/mean/throughput
  detection.
- Wired into both the same cascade path (`_emit`, when a larger related
  communicator fires) and the same standalone periodic sweep
  (`_maybe_launch_dcgm_fallback_check`'s sibling) that
  `_dcgm_fallback_evaluate` already uses.
- **Live-validated**: TP2 (target rank3) and TP-inference (target rank2)
  each produce exactly one CONFIRMED/PAGE alert on the exact injected
  rank (cross-checked via `gpu_slot_index`), zero false positives, across
  the final validated design.

**New this session (P27.4) — `_check_mean`'s real poll-timing race**:
`_check_mean` used a bare instant query with no persistence/lookback at
all (unlike `_check_cv`, which already had `CVPersistenceTracker`) —
confirmed live via FSDP fault test #8 that node_aggregator's own
`agg_mean_fired` correctly flipped to 1 (z=149.93, a stronger signal
than the unrelated rank that got flagged instead) but this bare instant
check never caught it, pure poll-alignment luck. Fixing this surfaced
two further real, previously-undiscovered bugs, found only by testing
the fix against live data rather than trusting it once it looked
correct:
1. **VictoriaMetrics's comm/bucket label-discovery endpoints
   (`/api/v1/label/.../values`, `/api/v1/series`) don't reliably
   time-filter** — confirmed live: one FSDP poll cycle was checking 136
   distinct communicator IDs, including ones from workloads (ResNet,
   TP2) that had finished HOURS earlier the same session, with zero
   current `agg_samples_seen` data. Left unfixed, every poll re-checks
   dozens to hundreds of dead communicators (each a real VM round-trip
   via `_check_mean`/`_check_cv`/`_check_outlier_count`), inflating real
   poll wall-clock time far past `poll_interval` and starving the
   CURRENT job's own real buckets of timely checks — the true, deeper
   cause behind the race above. Fixed by adding a genuine per-candidate
   freshness check (reusing `_is_fresh_row`, the same staleness
   discipline every other live read in this file already uses) to both
   `_discover_comms` and `_discover_buckets`.
2. **`timestamp()` does not compose with `max_over_time()`** in this VM's
   PromQL/MetricsQL engine — confirmed live via a direct query:
   `timestamp(max_over_time(X[90s]))` returned empty at a timestamp where
   `max_over_time(X[90s])` itself (and a bare instant query) both
   returned a real value. This silently discarded every row through
   `_query_instant_real_ts`'s existing label-correlation logic (which
   drops any row with no timestamp match — correct behavior for the
   plain-selector case it was built for, wrong here). A
   `max_over_time(...)` result is a synthetic value anchored to the
   QUERY's own eval time, not one specific underlying raw sample, so
   there is no meaningful "real sample timestamp" to recover in the
   first place — fixed by using plain `_query_instant` (whose eval-time
   IS the correct, honest timestamp for this specific query) instead of
   `_query_instant_real_ts`, with no separate freshness gate needed
   (the bounded `max_over_time([lookback_s])` window itself is what
   limits how far back a real firing sample can be and still count).
   `lookback_s` uses `FRESH_THRESH_S` (90s) — reused, not a new
   constant; an earlier attempt with `poll_interval * 3` (10s) was
   confirmed live to be too narrow (a real firing sample was found via
   direct query to exist just outside that window).
- **Live-validated** (300s test duration — FSDP's smaller admin bucket
  needs close to the full window just to accumulate its first
  `MEAN_WINDOW=100`-sample window at all, a real, disclosed timing
  characteristic distinct from the race this fix closes): fault test #8
  now correctly flags the exact injected rank (rank0), healthy baseline
  #7 stays clean (1 background-noise alert, 0 CONFIRMED/PAGE). 0
  `[CHECK-FAILED]` across all validation runs.

**Known limitations**:
- The 2-member DCGM fallback only ever produces Path B (clock-suppression)
  evidence live — Path A (thermal) needs a rolling buffer this live engine
  doesn't keep. (The timing-asymmetry fallback above closes the
  complementary software-fault case DCGM can't see at all — the two
  together, not either alone, cover the real 2-member gap.)
- **A real, reproducible, unexplained finding (P27-era)**: the host-load
  check (`_maybe_launch_host_check`/`live_host_load_ratios`) fired a real
  `CONFIRMED/PAGE` alert on `worker-2` in 3 out of 3 independent MoE
  healthy-baseline test runs (ratio ~7-9x, but tiny absolute load —
  ~0.15 vs ~0.02 load/core), with no corresponding Slurm job visible to
  explain it. Not caused by this session's own changes to this file
  (the host-check code path itself was untouched) — looks like a real,
  small, persistent background process on that one physical node. Not
  investigated further this session; worth checking whether it recurs
  on a different cluster/node.

---

## `/root/P20c_alerting/coverage_guard.py`

**What it does**: decides whether an alert's confidence should be capped
because the underlying data pipeline itself looks degraded (peer coverage
too low, absolute ingest rate too low relative to a self-calibrated
baseline) — i.e., "was there even enough real data to trust this alert."

**Key configurable values (real, current)**:
- Self-calibrated throughput baseline (replaced the old hardcoded
  `CALIBRATED_RATE_PER_SEC=50.0`, which was silently wrong for any
  workload other than the one it was measured against) — keyed by
  `workload_signature()`.
- `PEER_COVERAGE_FLOOR`, `ABSOLUTE_RATE_FLOOR_FRAC` — fractional
  thresholds (workload-generic).

**Fixed this session (P27-maintenance)**: `dt = 2.0` used to be a
hardcoded assumption that exactly 2.000s of real wall-clock time elapsed
between the two rate-sample HTTP calls — never actually measured, so
network/VM response jitter on either call silently skewed the computed
rate. Now uses a new `_query_instant_real_ts` (same fix shape as
`alert_engine.py`'s/`pipeline_health.py`'s own, correctly excluding
`__name__` from the label-correlation key — a mistake this session's own
first draft of this fix reproduced and then caught by testing against a
real pushed metric before trusting it) and measures `dt` as the two
samples' own real timestamp difference. Also added a `dt <= 0` guard
that honestly refuses to compute a rate rather than divide-by-zero —
confirmed live: two samples resolving to the identical already-indexed
value produced `dt=0.000s` and a clean "cannot compute a real rate this
cycle" message, not a crash or a fabricated number.

**Known limitations** (this session's own Part 3 audit, not fixed there):
coverage_guard's own volume check still pools different collective types
(`coll`) at a shared bucket value (a smaller, separately-named,
already-documented gap from an earlier session, not fixed there either).

---

## `/root/P20c_alerting/pipeline_health.py`

**What it does**: the dead-man's-switch — distinguishes "genuinely
healthy, zero alerts" from "can't tell, no fresh data is reaching the
metrics store at all." Exists because of a real incident (a 3h10m run
with zero alerts, caused entirely by a broken `vm_url`, not a healthy
run).

**Key configurable values**: `HEARTBEAT_STALE_THRESH_S = 90.0` — the real,
measured staleness threshold (10s was tried first and found unsatisfiable
even in healthy runs).

---

## `/root/P20c_alerting/thresholds.py`

**What it does**: the shared numeric thresholds `alert_engine.py`'s
mean/CV checks fire against.

**Key configurable values (real, current)**: `CV_Z_THRESH = 60.0`,
`MEAN_Z_THRESH = 30.0`, `MEAN_MM_THRESH = 2.0`, `PERSIST_WINDOW = 3`,
`PERSIST_REQUIRED = 3` (3-of-3 consecutive windows). All re-validated
across multiple real workloads this project has tested (ResNet, ViT,
FSDP, MoE, TP at multiple sizes, DDP) — this session's own Part 3 audit
found these likely-fine, not workload-specific.

---

## `/root/P20c_alerting/persistence.py`

**What it does**: `CVPersistenceTracker` — the shared 3-of-3-consecutive-
window gating mechanism used by CV, host-load, and job-throughput checks,
so a single noisy window can't fire an alert alone.

---

## `/root/P20c_alerting/health_exclusions.py`

**What it does**: a small, explicit exclusion list (`EXCLUDE_ALWAYS`,
rank 0 in particular) for statistics known to have a persistent,
non-fault bias (rank 0's own bookkeeping overhead wins "worst" in most
healthy windows) — excluded from peer-relative "worst" selection so that
known bias can't manufacture a false alert.

---

## `/root/P20c_alerting/ras_alert.py`

**What it does**: a separate, narrower alerting path for hardware RAS
(Reliability, Availability, Serviceability) error events — distinct from
the compute-straggler detection path above.

---

## `/root/P20c_alerting/iowait_logger.py` (P26.5-maintenance, new this session)

**What it does**: wraps `iowait_agent.bt` (the eBPF storage-fault agent,
see below), persisting its real per-PID block-I/O-wait output to a
queryable, real-epoch-timestamped JSON-lines log per host —
`storage_evidence.py` reads this for both live and offline classifier
storage checks.

**Real, confirmed scope this session**: this whole approach (block layer
tracepoints) sees LOCAL DISK I/O only. Confirmed live: virtiofs (this
platform's own jail root and its `/data` submount) produces ZERO
`block_rq_issue`/`block_rq_complete` events for the reading process — a
real 400MB cold+cached read test showed nothing, over the same window a
genuine local-disk read on the same host showed millions of microseconds
of correctly-attributed real io-wait. S3-backed access is pure network
I/O (a TCP socket, not the block layer) — same null result, confirmed via
a live request against a real Nebius storage endpoint.

---

## `/root/P18k_classifier/classifier.py`

**What it does**: Stage 3/4 classification — decides confidence tier
(CONFIRMED/PROBABLE/UNCONFIRMED) and gathers real cause-evidence for a
single-rank finding. Three CONFIRMED paths, each requiring two
corroborating signals:
- **Path A** (thermal): cumulative SW thermal-slowdown counter ratio +
  TFLOPS deviation.
- **Path B** (clock suppression): `sm_clock < 0.6x` node peer median,
  gated on the target being genuinely active (`power > ACTIVE_POWER_PEER_FRAC
  (0.40) * peer_median_power` — peer-relative, replacing an old absolute
  `IDLE_POWER_W=150.0` that was silently wrong for lighter workloads like
  ResNet).
- **Path C** (storage, P26.5-maintenance, new this session): real eBPF
  block-I/O-wait for the rank's own real PID, requiring both an absolute
  floor (`IOWAIT_ABS_FLOOR_US = 500_000`) and a real fraction of the
  window's own duration (`IOWAIT_RATIO_MIN = 0.5`) — see
  `storage_evidence.py`. Live-validated end to end this session against a
  real local-disk fault (CONFIRMED fired correctly) and a real non-storage
  delay (correctly ruled out, not silenced — see `build_review_lists()`).

**Key configurable values**: `THERMAL_RATIO_MIN = 100.0`,
`TFLOPS_DEVIATION_PCT = 10.0`, `ACTIVE_POWER_PEER_FRAC = 0.40`,
`IOWAIT_ABS_FLOOR_US = 500_000`, `IOWAIT_RATIO_MIN = 0.5`,
`IOWAIT_LIVE_WINDOW_S = 10.0`.

**Fixed this session (P27-maintenance, cluster-topology audit)**:
- `BUCKET_B`/`BUCKET_C` (the prior session's own flagged finding) are no
  longer used as defaults anywhere in this file — `check_global_drift`
  and `classify_incremental` now discover the real primary/corroborating
  buckets for whatever `dump_dirs` they're actually given
  (`detection.select_primary_corroborating_buckets`), the same way
  `alert_engine.py`'s live path already discovers buckets dynamically.
  Live-validated: `classify()` against a real, complete 16-rank ResNet
  dump returns `status: 'clean'` using the real discovered bucket
  (2052000), not the old hardcoded 12295680.
- `build_single_rank_finding` now accepts real `host=`/`local_idx=`
  directly, bypassing `worker_of(rank)`/`rank % 8` entirely when
  supplied. `alert_engine.py`'s `build_finding_for_alert` (the live
  production path) now passes its own already-real hostname and real
  `gpu_slot_index`-derived slot straight through — no more "placeholder
  rank" arithmetic reverse-engineering a 2-node/8-GPU shape.
  Live-validated with a deliberately different-shaped hostname
  (`"gpu-node-3"`, not `worker-0`/`worker-1`): the finding correctly
  carries that real hostname through untouched. `rank%8`/`worker_of()`
  remain as a fallback ONLY for callers that still just pass a flat
  global rank (the offline/buffer-replay engine — see "Dynamic/generic
  guarantees" below for what that engine still assumes).
- Path A's rolling-buffer peer computation (`for i in range(8) if i !=
  local_idx`) now derives peer GPU indices from the buffer's own real
  recorded keys, not a hardcoded range — implemented, not independently
  live-validated this session (needs a real `gpu_buf` from a live
  rolling-buffer sampler run; GPU access was unavailable).

**Known limitations** (this session's own Part 3 audit, not fixed there
unless noted above):
- Path C's own thresholds are real but thin — calibrated from an earlier
  session's own n=1 real local-disk test, not yet cross-validated across
  multiple hosts/workloads the way the other thresholds have been.
- Path C is only wired for the live path (`alert_engine.py`'s
  `build_finding_for_alert` now passes `iowait_pid=member`) — the offline/
  buffer-replay path has no real PID-per-rank mapping available yet
  (an honest, disclosed gap: `cause["impossible"]` reports this rather
  than silently guessing an identity).

## `/root/P18k_classifier/detection.py`, `calibration.py`, `rolling_buffer.py`, `cause_metrics.py` (this session's other real fixes)

**What they do**: `detection.py` is the core offline per-rank statistics
engine (node-scoped/node-vs-node scoring, bucket discovery, rank0-specific
statistics) `classifier.py` builds on. `calibration.py` is the Stage-2
per-job calibration/contamination-screening workflow. `rolling_buffer.py`
is the continuous DCGM/host/IB sampler for offline fault-window lookback.
`cause_metrics.py` holds the live SSH-based DCGM/IB/thermal/TFLOPS query
functions `classifier.py` calls.

**Fixed this session**:
- `detection.py`: `N_RANKS=16` removed from `calibration.py`'s
  `screen_contamination` (now uses each real communicator's own real
  `n_ranks`, via `per_rank_series_by_comm` directly instead of the
  single-comm back-compat wrapper that discarded it) — live-validated
  against real recorded TP dump data (correctly discovers 4 real 2-member
  communicators, correctly flags a genuinely-incomplete one by its real
  count, not a hardcoded 16).
- `detection.py`: `score_node_scoped`'s peer computation used to derive
  candidates from `rank_node(worst)` (the hardcoded `NODE_A`/`NODE_B`
  0-7/8-15 range) regardless of which ranks the caller's own data
  actually contained — **confirmed live this session to crash with a real
  `KeyError`** the moment a real communicator's own membership doesn't
  span a full hardcoded 8-wide node (e.g. a real 2-member TP
  communicator). Fixed to derive peers from the real ranks actually
  present in the data — live-validated (no crash, correct real-coverage
  disqualification) against real TP dump data.
- `detection.py`: `RANK0_CV_PEERS` was a separately-hardcoded literal
  `{1,2,4,5,6,7}` duplicating `NODE_A`'s own range by hand — now derived
  from `rank_node(0)` directly (removes the duplication/drift risk;
  behavior-preserving, confirmed identical output).
- `detection.py`/`calibration.py`/`classifier.py`/`persistence.py`/
  `transient_latency.py`: `BUCKET_B`/`BUCKET_C` hardcoded defaults removed
  from `score_all`, `run_persistence_sweep`, and `windowed_mean_scores` —
  all now discover real buckets when not given an explicit one.
- `cause_metrics.py`/`rolling_buffer.py`: IB device enumeration
  (`query_ib_all_devices`, `_query_ib_all_devices_batched`) used to
  hardcode exactly 8 `mlx5_N` devices — now discovered live via
  `discover_ib_devices` (`ls /sys-host/class/infiniband/`). Validated for
  graceful degradation (returns `[]`, not a crash, on a host with none —
  confirmed on login-0); the real positive case (discovering a worker
  node's actual 8 devices) is **not yet live-re-validated** — GPU/worker
  access was unavailable this session.
- `cause_metrics.py`: `query_thermal_slowdown_all_gpus` (`range(8)`) and
  `query_matmul_tflops`/`health_exclusions.py`'s `degraded_gpus_live`
  (`--gpus-per-node=8`) now discover the real per-host GPU count via a
  new `discover_gpu_count` (`nvidia-smi -L | wc -l`) instead of assuming
  8 — **not yet live-re-validated**, same reason.

**Known, disclosed, NOT fixed this session** (a real, deeper structural
finding — see "Dynamic/generic guarantees" below for the full reasoning):
`detection.py`'s `NODE_A = set(range(0,8))` / `NODE_B = set(range(8,16))`
and `rank_node()` remain hardcoded to a 2-node/8-rank-per-node shape, and
are still used by `score_node_scoped_per_node`, `score_node_vs_node`/
`recheck_node_vs_node_excluding`'s own `rank<8`/`rank>=8` split,
`classifier.py`'s own node-grouping logic (lines ~710/719/736), and (in
an already-guarded, non-crashing form) `cv_fixed.py`/`decay_counter.py`/
`transient_latency.py`'s own peer computation. Confined entirely to the
OFFLINE/historical-replay analysis engine — the LIVE production path
(`alert_engine.py`) no longer depends on any of this, per the fix above.

## `/root/P18k_classifier/storage_evidence.py` (P26.5-maintenance)

**What it does**: reads `iowait_logger.py`'s persisted per-host log and
answers "was this real PID io-bound during this real time window" —
`query_iowait_window()` for the raw evidence, `determine_storage_path()`
for the two-signal CONFIRMED verdict. See classifier.py section above for
the real thresholds and their (thin, n=1) calibration basis.

---

## `/root/P18k_classifier/report.py`

**What it does**: renders a finding as one of the three tiers'
human-readable text. CONFIRMED/PROBABLE show `cause["class1"]` items
directly (plus, as of this session, a real storage-evidence line when
Path C fired); UNCONFIRMED renders the full structured
ruled-out/impossible/next-steps review via `build_unconfirmed_report()`.

---

## Inspector plugin

**Source location**: `/root/nccl-2.28-src/ext-profiler/inspector/`
(`inspector.cc`, `inspector.h`, `inspector_plugin.cc`, `json.cc/h`,
`version.cc/h`, `Makefile`). Built artifact:
`libnccl-profiler-inspector.so` in the same directory (a `.bak_p22` backup
of an earlier build is also present there).

**Real patches made this project** (both tagged in the source, both
confirmed via live testing, not just code review):
1. **`gpu_slot_index` (P21.7)** — `inspector.cc` lines 13, 71, 415,
   1008-1024. Captures the real physical CUDA device ordinal via a direct
   `cudaGetDevice()` call at communicator-init time, on the thread that
   actually has the right CUDA context bound (the internal dump thread,
   spawned later, does not) — a real, disclosed dependency: this capture
   is thread-specific, not something that transfers automatically to a
   different init flow. Reports `-1` (honestly) if the call fails, rather
   than assuming device 0.
2. **P2P/AllToAll visibility (P23)** — `inspector_plugin.cc` lines 111-117.
   Adds `ncclProfileP2p` to the requested activation mask (alongside the
   pre-existing `ncclProfileColl`) — confirmed via a live probe of 4000
   real `all_to_all_single`/`all_to_all` calls that without this flag,
   zero records are produced for Send/Recv/AllToAll-style point-to-point
   traffic (not mislabeled — literally absent).

---

## Dynamic/generic guarantees

A P27-maintenance-session audit specifically hunted for cluster-topology
assumptions (as opposed to the prior session's workload-calibration
assumptions) — hardcoded hostnames, rank/GPU-per-node counts, IB/NVLink
device counts, byte-size constants, and file paths, beyond this cluster's
own real 2-node/8-GPU-per-node/`worker-0`+`worker-1` shape. This is the
honest, current answer: what's confirmed dynamic, with the real evidence,
versus what remains cluster-specific or unconfirmed.

### Confirmed dynamic (real evidence, not just design intent)

- **Communicator/bucket discovery (live path)**: `alert_engine.py`'s own
  `_discover_hostnames`/`_discover_comms`/`_discover_buckets` were already
  fully dynamic before this session (P21.5) — proven across every real
  multi-communicator workload this project has tested (TP2/TP4/FSDP/MoE/
  DDP/inference at various sizes).
- **GPU-slot identity**: `gpu_slot_index`, via a direct `cudaGetDevice()`
  call at communicator-init time (Inspector plugin, P21.7) — works
  regardless of node/rank count, since it reads the real physical device
  ordinal directly from CUDA, never inferred from rank arithmetic.
- **The live production alerting path's host/GPU-slot targeting**
  (P27-maintenance, this session): `build_finding_for_alert` →
  `build_single_rank_finding` no longer derives host/GPU-slot from a
  hardcoded `worker_of(rank)`/`rank % 8` — it passes the real, already-
  discovered hostname and real `gpu_slot_index`-derived slot straight
  through. Proven with a deliberately different-shaped hostname
  (`"gpu-node-3"`) carried through correctly, untouched.
- **Message-size bucket selection** (P27-maintenance): `BUCKET_B`/
  `BUCKET_C` (one workload's own hardcoded byte sizes) no longer used as
  defaults anywhere in `detection.py`/`calibration.py`/`classifier.py`/
  `persistence.py`/`transient_latency.py` — real, per-run bucket
  discovery (`select_primary_corroborating_buckets`) is used instead,
  proven against real recorded ResNet (single-communicator) and TP
  (multi-communicator) dump data.
- **Per-communicator rank/member coverage** (P27-maintenance): real
  `n_ranks` per communicator (`per_rank_series_by_comm`'s own
  `header["n_ranks"]`, NCCL/Inspector's own self-reported size) replaces
  every hardcoded `N_RANKS=16` comparison found this session. Proven
  live: a real 2-member TP communicator's own real coverage (2/2) is now
  compared correctly, not against a hardcoded 16.
- **Node-scoped peer computation, when caller-scoped data is used**
  (P27-maintenance): `score_node_scoped`'s own peer set is now derived
  from whichever ranks the caller's data actually contains, not a
  hardcoded 8-wide range — proven live: this used to crash with a real
  `KeyError` the moment a real communicator didn't span a full
  hardcoded node; now doesn't.
- **IB device enumeration / per-host GPU count** (P27-maintenance):
  `discover_ib_devices`/`discover_gpu_count` replace hardcoded
  `mlx5_0..7`/`range(8)`/`--gpus-per-node=8` assumptions across
  `cause_metrics.py`, `rolling_buffer.py`, `health_exclusions.py`.
  Graceful-degradation confirmed live (a host with zero real IB devices
  correctly returns `[]`, not a crash); the real *positive* case (a
  worker node's actual device/GPU count) is implemented but **not yet
  live-re-validated** — GPU/worker access was down this session.
- **The 2-member DCGM fallback (P21.6.1, an earlier session)**: already
  confirmed fully generic — no hardcoded rank/host assumption anywhere in
  its own discovery or evaluation path, verified again this session (a
  targeted re-grep found zero `range(8)`/`% 8`/`== 16` patterns in that
  code).

### Remains cluster-specific or unconfirmed

- **The offline/historical-replay analysis engine's core data model**
  (`detection.py`'s `NODE_A = set(range(0,8))` / `NODE_B = set(range(8,16))`
  / `rank_node()`, and everything built on it — `score_node_scoped_per_node`,
  `score_node_vs_node`/`recheck_node_vs_node_excluding`'s own `rank<8`/
  `rank>=8` split, `classifier.py`'s node-grouping suppression logic,
  `cv_fixed.py`/`decay_counter.py`/`transient_latency.py`'s own peer
  narrowing). This is a real, hardcoded 2-node/8-rank-per-node assumption,
  confined entirely to the OFFLINE batch/replay tool (NOT the live
  production path, which this session's fix above already closed).
  Genericizing it properly means redesigning this engine's whole per-rank
  data model from "flat global rank + hardcoded 2-way split" to "real,
  host-keyed membership" — a substantial, separate undertaking, correctly
  scoped as its own future session rather than a partial, risky rewrite
  here.
- **`ras_alert.py`/`health_exclusions.py`'s `hosts=("worker-0","worker-1")`
  defaults** (`rendezvous_coordinator_rank`'s caller,
  `degraded_gpus_live`) — real hardcoding, but confined to a module that
  is NOT wired into the live production `alert_engine.py` pipeline at
  all (only exercised via its own standalone test script) — lower
  priority, disclosed, not fixed this session.
- **IB/thermal/TFLOPS/GPU-count real-discovery fixes** (see above) —
  implemented and safe (graceful degradation confirmed), but the real
  *positive* case (discovering a worker node's actual device/GPU count
  correctly) needs live re-validation once GPU/worker access returns.
- **Path A's rolling-buffer peer computation fix** (`classifier.py`,
  now derived from `gpu_buf`'s own real keys) — implemented, not
  independently live-validated this session (needs a real buffer object
  from a live rolling-buffer sampler run).

### The actual, final answer

This system's **live production alerting path is now genuinely
cluster-topology-agnostic** for the properties audited this session
(host naming, GPU-per-node count, bucket selection, IB device count) —
every hardcoding found in that path was fixed and, where GPU access
allowed, live-validated. The **offline/historical-replay analysis
package** (used for post-hoc dump analysis, calibration, and standalone
sweep tools — not live alerting) still has one real, deep, disclosed
2-node/8-GPU-per-node assumption baked into its core statistics engine
(`NODE_A`/`NODE_B`/`rank_node()`), deliberately not rewritten this
session. A differently-shaped cluster (different node count, different
GPUs-per-node, different network topology) would need: (1) nothing extra
for live alerting, (2) a real rewrite of `detection.py`'s per-rank data
model before trusting the offline analysis tools, and (3) live
re-validation of the IB/GPU-count discovery fixes and the storage-check's
per-host log wiring, none of which could be exercised against real
hardware this session.
