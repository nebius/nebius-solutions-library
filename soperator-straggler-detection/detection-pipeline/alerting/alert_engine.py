#!/usr/bin/env python3
"""P20c -- the alert layer.

Independently re-implements the CV persistence rule (and the single-window
mean/outlier_count/rank0_outlier_rate gates) against raw VM data. This does
NOT trust node_aggregator.py's internal agg_*_fired flags as ground truth
-- it is a second, decoupled implementation of the same rule, per this
task's explicit instruction ("confirm persistence requirement carries
through unchanged... for the alert layer, not just the classifier's
internal fire flag"). A bug in the aggregator's own internal state would
not silently become the alerting ground truth unchecked.

When this layer's OWN check confirms a fire, it:
  1. Runs the coverage guard (coverage_guard.py) and caps confidence if
     coverage is degraded, rather than firing/suppressing outright.
  2. Gathers live cause evidence and decides CONFIRMED/PROBABLE/UNCONFIRMED
     by calling into P18k_classifier's real classifier.py/cause_metrics.py
     (not a reimplementation) -- report.py's real formatter renders the
     final message.

Known, stated limitation (see build_finding_for_alert docstring): cause
gathering here uses only the LIVE-query DCGM path (classifier.py's Path B),
not the buffer-based lookback path (Path A thermal/TFLOPS), since this
alert engine does not maintain a continuous rolling GPU-telemetry buffer.
This is an accepted trade-off for a near-real-time alert (persistence
means the fault is normally still active when the alert fires, unlike the
offline classifier's use case of investigating a whole job after the fact)
-- but it means a fault that resolves in the few seconds between
persistence firing and cause-gathering could read weaker (PROBABLE/
UNCONFIRMED) than the offline classifier would find with its buffer.
"""
import sys
import time
import json
import statistics
import subprocess
import threading
import concurrent.futures
import urllib.request
import urllib.parse
from collections import deque, defaultdict

sys.path.insert(0, "/root/P18k_classifier")
import classifier as p18k  # noqa: E402
import storage_evidence  # noqa: E402
import report as p18k_report  # noqa: E402

sys.path.insert(0, "/root/P20c_alerting")
import thresholds as T  # noqa: E402
import coverage_guard  # noqa: E402
import pipeline_health  # noqa: E402
from persistence import CVPersistenceTracker  # noqa: E402

# P20d-closeout Part A/B -- live network (IB) and host (CPU) detection.
# Real measured cost matters here, not the docstring's nominal "2s
# interval": query_network_snapshot's own two-snapshot-per-host design
# does 8 sequential SSH round-trips per host per snapshot (one per mlx5
# device) -- measured live against this exact 2-node cluster at 19.94s
# for one call, dominated by SSH connection overhead, not the 2s sleep.
# Run this synchronously inside poll_once()'s 3s loop and it would stall
# every other check for ~20s each time it fires -- exactly the kind of
# poll_once() latency problem Part C exists to fix, so this must run out
# of band (its own background thread, on its own slower cadence) rather
# than add to it. query_host_cpu is much cheaper (2 SSH calls total) but
# gated the same way for consistency and because SSH itself can still
# hang under real network trouble.
NETWORK_CHECK_INTERVAL_S = 60.0
HOST_CHECK_INTERVAL_S = 20.0
# P25 Part 1 -- same interval as the IB check: real measured cost of
# query_nvlink_snapshot on this cluster is dominated by per-GPU SSH round
# trips (2 hosts x 8 GPUs x 2 error snapshots + 1 bandwidth poll), the
# same class of cost that made the IB check out-of-band in the first
# place -- reusing that interval and the same background-thread pattern
# rather than inventing a different cadence.
NVLINK_CHECK_INTERVAL_S = 60.0

# P21.6.1 -- standalone half of the 2-member-communicator DCGM fallback
# (see _dcgm_fallback_evaluate's own docstring for the full design). Real
# measured cost is ONE dcgmi call per host (query_dcgm_all_gpus does a
# single `dcgmi dmon -c 1` covering all 8 GPUs at once, not a per-GPU SSH
# round trip like the network/NVLink checks) -- the same cost class as
# HOST_CHECK_INTERVAL_S's query_host_cpu, not the ~20s network/NVLink
# checks, so it reuses that cadence rather than the slower one.
DCGM_FALLBACK_CHECK_INTERVAL_S = 20.0

# P27.2.7 -- real, confirmed gap this closes (regression-sweep investigation,
# TP-inference): _cross_comm_peer_median's peer pool requires every sibling
# comm to be reporting a currently-FRESH row, which real, direct live-trace
# evidence confirmed structurally fails for any below-floor workload whose
# dp-replica pairs finish asynchronously -- TP-inference's real case: pure
# forward-pass-only replicas with no backward-pass/gradient-sync coupling
# finish in ~10-15s while a faulted pair runs for minutes, so ALL of a
# host's OTHER same-shape siblings go permanently stale long before the
# fault's own persistence window can ever be satisfied, confirmed via two
# independent live fault runs (both showing role_baseline=cold-start AND
# cross_comm_peer=no_live_peer on EVERY cycle for the rest of each run).
# Bounded to this one job's own real duration (not unbounded, not the 30-
# day ROLE_XJOB_LOOKBACK_S cross-job window -- that's a different, already-
# guarded axis via _comm_slurm_job_id's own job-id filter): 3600s
# comfortably covers every real below-floor job's duration seen anywhere
# in this project (seconds to ~600s) while remaining a real, disclosable
# bound rather than an unbounded historical scan.
PEER_SIBLING_LOOKBACK_S = 3600.0

# P27-hotfix9 -- 3x DCGM_FALLBACK_CHECK_INTERVAL_S (the poll cadence this
# same fallback already runs on, reused rather than a new invented number)
# -- real margin for two genuinely related timing-fallback alerts to each
# independently satisfy their own 3-sample persistence requirement on
# their own real schedule, confirmed this session NOT to reliably land in
# the exact same 20s cycle even when directly causally connected.
RECENT_TIMING_CORRELATION_WINDOW_S = DCGM_FALLBACK_CHECK_INTERVAL_S * 3

# P27-hotfix9b -- real, measured need: hybrid TP+PP's own actual fault
# cascade (rank0's PP fault -> rank2's delay -> stage1's OWN TP AllReduce
# imbalance -> rank3's delay -> rank1's own elevated wait) is a REAL
# 2-hop chain confirmed live -- the direct alert's comm and the
# downstream echo alert's comm share NO member directly, only through
# one intermediate comm. 3 gives one hop of real margin beyond the
# deepest case this project has actually observed, while keeping the
# BFS bounded and cheap (below-floor comm graphs are sparse in every
# real job tested so far) -- not an unbounded search.
MAX_CORRELATION_HOPS = 3

# P27.2.6 -- minimum absolute gap (current - peer_median), in real peer-pool
# MADs, the 2-member timing-asymmetry fallback requires on top of its existing
# 1/PATH_B_AND_TIMING_SUPPRESS_RATIO ratio check, before flagging a member as
# elevated. Derived from real, live-measured data on the original 2-node
# cluster, not guessed: 5 independent, deliberately unfaulted TP2 runs (0
# faults injected) produced 8 real CONFIRMED/PAGE false positives via the
# ratio check alone, whose (current - peer_median) gaps, measured against one
# live peer-pool MAD sample (458us) from the exact bucket/coll they fired on,
# spanned ~1.0-3.6 MADs -- real, substantial peer-pool dispersion at
# sub-millisecond scale, not injected faults. This fallback's own real
# validated fault (see _timing_asymmetry_fallback_evaluate's P27.2.4 docstring:
# ~230x ratio, 24338us vs ~105us peer median) sits roughly two orders of
# magnitude beyond that noise band on the same MAD scale. 6 sits comfortably
# above the observed noise ceiling (3.6) with real margin, while remaining
# astronomically smaller than the real fault's own margin -- deliberately a
# multiplier of a LIVE, per-check peer-pool statistic, not an absolute
# microsecond value, so it self-scales to whatever a given cluster's own real
# peer-pool dispersion actually is (including a low-dispersion cluster where
# peer_mad is small and this gate is essentially always cleared).
TIMING_FALLBACK_MAD_MULTIPLE = 6

# P27-hotfix4 (bugfix) -- same real value/rationale as node_aggregator_ref.py's
# THROUGHPUT_XJOB_LOOKBACK_S (30 days: long enough to span realistic gaps
# between recurring runs), reused here for _member_role_baseline's own
# cross-JOB historical query. Confirmed live this session: the first version
# of that method copied _cross_comm_peer_median's instant-query + _fresh()
# pattern, which requires the OTHER job's data to be fresh RIGHT NOW --
# correct for _cross_comm_peer_median's real use case (a concurrently-running
# peer comm on the same host) but wrong for a cross-JOB historical baseline,
# whose whole point is comparing against a job that has usually already
# finished. That bug made _member_role_baseline silently degrade to "only
# works if another identical job happens to be running at this exact
# moment" -- collapsing it back to _cross_comm_peer_median's own limitation
# and defeating the reason this method exists. last_over_time with this
# long lookback (the query_throughput_history pattern) is the fix.
ROLE_XJOB_LOOKBACK_S = 30 * 86400

# P27-hotfix8 -- real, measured threshold replacing the old rank-0-identity
# exclusion entirely (see _timing_asymmetry_fallback_evaluate's own comment
# for the full story). Confirmed live via leave-one-out testing against 9
# real, genuinely healthy TP2 runs of rank0/1's own TP-pair (the pair
# carrying TP2's real, already-documented coordinator-overhead artifact,
# never fault-injected in any of the 9): that role's own historical
# role-baseline pool has a real, measured MAD/median ratio of 0.65-0.82
# across all 9 -- and 2/9 (~22%) would have false-elevated using the
# role-aware baseline alone, confirming the artifact is real and not
# already suppressed by role-awareness. By contrast, hybrid TP+PP's rank0
# PP pair (a genuine, direct, correctly-attributable fault target this
# session confirmed via its partner's clean 8.0x elevation) measured
# 0.28-0.36 on the same ratio, even including a real live fault run. 0.5
# sits with real margin above hybrid's observed max (0.36) and below
# TP2's observed min (0.65) -- a real, data-justified boundary, not a
# guess. A role whose own historical pool exceeds this is treated as "too
# noisy to trust for a ratio-based elevation check" and degrades to the
# existing _cross_comm_peer_median fallback, exactly like a genuine cold
# start already does -- generic, with no notion of "rank 0" needed at all.
ROLE_BASELINE_MAX_RELATIVE_MAD = 0.5

# P27.2.6 STOPGAP -- TEMPORARY, applied immediately and separately from the
# TIMING_FALLBACK_MAD_MULTIPLE fix above, before that fix had been live-
# validated. Real, live investigation on this cluster found the ratio-only
# P27.2 timing-asymmetry fallback firing CONFIRMED/PAGE on 5/5 genuinely
# healthy (unfaulted) TP2 runs -- an active false-page risk. Downgrades this
# ONE fallback's tier below PAGE-worthy while the MAD-based fix above is
# validated (Steps 2-4: false-positive gone on fresh healthy runs, real fault
# detection still fires with real margin, no regression vs. the interim
# 6-node cluster's own validated result). Does not touch _dcgm_fallback_
# evaluate's Path B half of this same fallback, or any other detector --
# scoped to exactly the mechanism found unsafe.
#
# REVERTED to False -- Steps 2-4 all passed (P27.2.6 validation): 0/5 fresh
# healthy TP2 runs false-fired under the MAD-gated check; the real injected
# fault (TP2, target rank3) still fired with correct rank attribution at
# gap/mad=1314.65, ~219x the required TIMING_FALLBACK_MAD_MULTIPLE=6; the
# 6-node cluster's own historical validated case reconstructs to gap/mad in
# the 16-1864x range under every realistic MAD bound available (no live
# re-test possible this session -- that cluster's own real peer_mad was never
# recorded, since this metric didn't exist yet when that validation ran).
TIMING_FALLBACK_STOPGAP_ACTIVE = False

# P26.5-maintenance -- where iowait_logger.py (run separately, one process
# per host, same convention as node_aggregator_ref.py) persists its real,
# per-host block-I/O-wait log. build_finding_for_alert reads from here via
# storage_evidence.py; a host with no {hostname}.jsonl file here (agent
# not deployed on that host yet) degrades honestly to "storage (eBPF
# io-wait): no persisted iowait log for this host", not a crash or a
# fabricated answer.
IOWAIT_LOG_DIR = "/root/P20c_alerting/iowait_logs"

# P20k-closeout-followup -- _fresh()'s real staleness threshold. Reused
# directly from pipeline_health.py's own already-measured, already-
# validated constant for this exact VM instance (not duplicated as a new
# number) -- see _fresh()'s own docstring for the real measurements that
# make 10s unsatisfiable and 90s correct.
FRESH_THRESH_S = pipeline_health.HEARTBEAT_STALE_THRESH_S


def _is_fresh_row(row):
    """P21.6.1 -- module-level counterpart to AlertEngine._fresh(), for the
    handful of module-level (no `self`) discovery/targeting helpers below
    that need the exact same real-staleness discipline. Real bug this
    closes, found live during this session's own validation: _comm_cross_
    node_members and _query_gpu_slot both used plain _query_instant (whose
    value[0] is the QUERY's own eval time, not the sample's real ingestion
    time -- the exact trap _query_instant_real_ts/_fresh() already exist to
    close everywhere else). Left unfixed, a physical GPU slot re-faulted
    within FRESH_THRESH_S of a PREVIOUS, now-dead job's last real push
    reads back that OLD job's stale (hostname, member) identity as if it
    were live -- confirmed directly: re-injecting the same clock-lock fault
    on the same GPU slot ~2-5 minutes after a prior job's aggregators were
    killed still resolved to that prior job's dead member/comm IDs instead
    of the new job's real ones, purely because both queries' instant reads
    fell inside VM's default (not this project's own, deliberately chosen)
    staleness window. Callers must switch to _query_instant_real_ts (whose
    value[0] genuinely IS the real sample timestamp) and filter through
    this before trusting a row."""
    try:
        ts = float(row["value"][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    return (time.time() - ts) < FRESH_THRESH_S

# P20d-hardening Step 2.2 -- a real, confirmed-twice, physically-grounded
# interaction limit, not a code bug. Reproduced live: NCCL_MAX_NCHANNELS=1
# + a simultaneous clock-lock on the same rank. Root cause confirmed at
# the RAW DATA level (not just z-scores): under a single-channel network
# constraint, the locked rank's own agg_mean_exec_time_us (2924-2926us
# while locked) was statistically indistinguishable from OTHER, unlocked
# ranks' own baseline values (2923-2927us) -- the collective's exec time
# is genuinely network-bound in this state, so GPU clock speed stops
# affecting it measurably. Compute detection only fired on the fault's
# release transient (z=2098.8), never during the sustained overlap, in
# both this session and the last. This cannot be fixed by adjusting CV's
# threshold/persistence/gating -- there is no real separation signal left
# in the underlying data to detect during the overlap; loosening the
# compute check specifically during a concurrent network condition would
# make it MORE trigger-happy exactly when its statistical basis is least
# reliable, the wrong direction. The correlating-not-fixing mitigation
# below is the one the task explicitly named as acceptable: make the
# ALREADY-firing network alert say so, rather than touch either
# detector's validated statistics.
NETWORK_MASKS_COMPUTE_CAVEAT = (
    "Known interaction limit (confirmed live, twice, root-caused at the raw exec-time "
    "level): while this network condition is active, a SIMULTANEOUS compute/clock fault "
    "on the same node(s) may be masked from CV-based compute detection -- the collective's "
    "exec time becomes network-bound, so a straggler rank's own clock speed stops "
    "producing a detectable timing difference from its peers. Compute detection may only "
    "recover once this network condition clears (observed: firing on the release transient, "
    "not during the overlap). Absence of a concurrent compute alert while this network "
    "alert is active is NOT evidence of compute health -- it is undetermined."
)


def _hosts_have_active_job(hostnames, timeout=5):
    """Reuses node_aggregator_ref.py's own squeue-based job-presence
    pattern (refresh_job_id()) rather than inventing a new one. Both live
    checks below are only meaningful while a real job is actively
    training across ALL of these hosts -- confirmed live against this
    project's own idle cluster: IB participation_frac reads 0.0 on both
    nodes with nothing running, which check_network_contention_direct's
    own logic would otherwise read as a capacity collapse (a real false-
    positive class caught before shipping, not a hypothetical one). A
    host-load ratio comparison between two idle nodes is equally
    meaningless. Requires ALL hosts to show a running job, since the
    comparison itself (this node vs. the others) needs every side of it
    to be real training activity."""
    try:
        for h in hostnames:
            out = subprocess.run(["squeue", "-w", h, "-h", "-o", "%i", "--states=R"],
                                  capture_output=True, text=True, timeout=timeout)
            if out.returncode != 0 or not out.stdout.strip():
                return False
        return True
    except Exception:
        return False


def _query_instant(vm_url, promql):
    qs = urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(f"{vm_url}/api/v1/query?{qs}", timeout=10) as resp:
        d = json.load(resp)
    return d.get("data", {}).get("result", [])


def _query_instant_real_ts(vm_url, promql):
    """P20k-closeout-followup fix -- the real bug, confirmed directly: a
    plain instant query's value[0] is the QUERY's own evaluation time
    ("now"), not the underlying sample's real ingestion time (confirmed:
    value[0] matched date +%s almost exactly, while timestamp(promql) on
    the identical query revealed a sample that was actually 41s old).
    Every _fresh() call site, plus the two places that separately extract
    ts to feed CVPersistenceTracker.observe() (_check_cv, _check_job_
    throughput -- whose own dedup-by-timestamp logic was therefore ALSO
    silently broken, not just _fresh()'s filtering), depended on this
    same wrong field.

    Fix: issue a SECOND query wrapping the same expression in PromQL's
    timestamp() function (the same real fix pipeline_health.py already
    validated for its own single-metric case), correlate its real
    per-series timestamps back onto the original value rows by label
    set, and return rows shaped exactly like _query_instant's own output
    -- value[0] now genuinely IS the sample's real timestamp, value[1]
    unchanged. This means _fresh() and every existing `ts = float(row
    ["value"][0])` call site work correctly with ZERO changes to their
    own logic; only the query call itself needed to change.

    Real, measured cost of this: one extra HTTP round-trip per existing
    call site -- see the P20k-closeout-followup report for the actual
    measured poll_once() cycle-time delta, not a guess."""
    val_res = _query_instant(vm_url, promql)
    if not val_res:
        return []
    ts_res = _query_instant(vm_url, f"timestamp({promql})")
    # timestamp()'s result vector drops __name__ from its label set (a
    # real PromQL/VM behavior, not a guess -- confirmed directly: the
    # first version of this fix silently dropped every row because it
    # compared label sets including __name__ on one side only). Excluded
    # from the correlation key on both sides so the two queries' rows
    # actually match.
    ts_by_labels = {}
    for row in ts_res:
        key = tuple(sorted((k, v) for k, v in row["metric"].items() if k != "__name__"))
        ts_by_labels[key] = float(row["value"][1])
    out = []
    for row in val_res:
        key = tuple(sorted((k, v) for k, v in row["metric"].items() if k != "__name__"))
        real_ts = ts_by_labels.get(key)
        if real_ts is None:
            continue  # shouldn't happen if both queries hit the same data, but never trust a made-up ts
        out.append({"metric": row["metric"], "value": [real_ts, row["value"][1]]})
    return out


def _discover_hostnames(vm_url, lookback_s=60):
    """Live node discovery: ask VM which hostnames have actually pushed
    agg_cv_z_worst in the last lookback_s, rather than trusting a
    hardcoded list. Scoped to a recent window (not all-time label
    history) so a decommissioned node or a stale prior job's leftover
    series doesn't linger forever in the polled set. Returns [] (not an
    exception) on any failure -- callers must treat empty as "nothing
    discovered yet," not as a crash."""
    now = time.time()
    qs = urllib.parse.urlencode({
        "match[]": "agg_cv_z_worst",
        "start": str(now - lookback_s),
        "end": str(now),
    })
    try:
        with urllib.request.urlopen(f"{vm_url}/api/v1/label/hostname/values?{qs}", timeout=10) as resp:
            d = json.load(resp)
        return sorted(d.get("data", []))
    except Exception:
        return []


def _discover_comms(vm_url, hostname, lookback_s=60):
    """P21.5 -- live communicator discovery, the same pattern as
    _discover_hostnames: ask VM which `comm` label values have actually
    been pushed for this hostname recently, rather than assuming a fixed
    bucket/rank space calibrated for one specific single-communicator
    workload (the exact bug this fix closes -- see node_aggregator_ref.py's
    own module docstring for the real data that confirmed it). Returns []
    on any failure -- callers must treat empty as "nothing discovered
    yet," same convention as hostname discovery."""
    now = time.time()
    qs = urllib.parse.urlencode({
        "match[]": f'agg_samples_seen{{hostname="{hostname}"}}',
        "start": str(now - lookback_s),
        "end": str(now),
    })
    try:
        with urllib.request.urlopen(f"{vm_url}/api/v1/label/comm/values?{qs}", timeout=10) as resp:
            d = json.load(resp)
        candidates = sorted(d.get("data", []))
    except Exception:
        return []
    # P27.4 -- real, confirmed bug: VictoriaMetrics's label-values endpoint
    # with start/end does NOT reliably restrict to comms with an actual
    # sample in that window -- confirmed live via a real FSDP test where
    # this returned 136 distinct comm values on one host, including comm
    # ids from workloads (ResNet, TP2) that finished HOURS earlier this
    # same session, with zero current agg_samples_seen data. Left
    # unfixed, every poll_once() cycle re-checks dozens to hundreds of
    # dead communicators via _check_mean/_check_cv/_check_outlier_count
    # (each a real VM round-trip), inflating real wall-clock poll time far
    # past poll_interval and starving the CURRENT job's own real buckets
    # of timely checks -- the true, deeper cause behind the poll-timing
    # gap this same P27.4 fix closes in _check_mean below. Corroborated
    # here with a genuine per-candidate freshness check (the same
    # _is_fresh_row discipline every other live read in this file already
    # uses), since the label-values query alone cannot be trusted for
    # recency.
    fresh = []
    for c in candidates:
        rows = _query_instant_real_ts(vm_url, f'agg_samples_seen{{hostname="{hostname}",comm="{c}"}}')
        if any(_is_fresh_row(r) for r in rows):
            fresh.append(c)
    return fresh


def _discover_buckets(vm_url, hostname, comm, lookback_s=60):
    """P21.5 -- same live-discovery pattern, scoped to one already-
    discovered communicator: which message-size buckets has THIS
    communicator actually reported recently. A different communicator on
    the same host gets its own, independently-discovered bucket set --
    two communicators are never assumed to share a bucket space.

    P22.2 -- returns (bucket, coll) pairs, not bare bucket values.
    Confirmed real and live (P22.1's own FSDP fault-injection test): once
    calibration can score two different collective types at the same
    message size on the same communicator (P22.1's own fix made this
    correct and real -- FSDP's ReduceScatter and AllGather genuinely
    share bucket=442560), discovering "bucket" alone collapses them back
    into one target here, one layer up -- silently re-introducing the
    exact conflation the aggregator-side fix just removed, just at the
    query/tracking layer instead of the calibration layer. Confirmed
    live: agg_mean_z_worst{bucket="442560"} matched BOTH AllGather's and
    ReduceScatter's independent series at once, and downstream per-member
    dicts/keys (keyed on member alone) silently mixed them.
    /api/v1/label/bucket/values (the old query) only returns each label's
    independent value set -- it cannot say WHICH bucket value went with
    WHICH coll value. /api/v1/series (used here instead) returns the
    real label sets that actually co-occur, so the true (bucket, coll)
    pairs are recovered together, not reconstructed by (wrongly) assuming
    every bucket value pairs with every coll value."""
    now = time.time()
    qs = urllib.parse.urlencode({
        "match[]": f'agg_samples_seen{{hostname="{hostname}",comm="{comm}"}}',
        "start": str(now - lookback_s),
        "end": str(now),
    })
    try:
        with urllib.request.urlopen(f"{vm_url}/api/v1/series?{qs}", timeout=10) as resp:
            d = json.load(resp)
        pairs = set()
        for series in d.get("data", []):
            b = series.get("bucket")
            c = series.get("coll")
            if b is not None and c is not None:
                pairs.add((b, c))
    except Exception:
        return []
    # P27.4 -- same real, confirmed VictoriaMetrics series/label-discovery
    # staleness bug as _discover_comms above (see its own comment for the
    # live evidence) -- /api/v1/series with start/end is equally untrustworthy
    # for recency, so each candidate (bucket, coll) pair gets the same
    # genuine per-candidate freshness corroboration before being trusted.
    fresh = []
    for b, c in sorted(pairs):
        rows = _query_instant_real_ts(vm_url, f'agg_samples_seen{{hostname="{hostname}",comm="{comm}",bucket="{b}",coll="{c}"}}')
        if any(_is_fresh_row(r) for r in rows):
            fresh.append((b, c))
    return fresh


def _comm_cross_node_members(vm_url, comm):
    """P21.6 -- a communicator's real, full membership across every
    currently-reporting host, recovered from data alone: the same comm_id
    string is used identically by every node's aggregator for the same
    real NCCL communicator (confirmed directly in P21.5's real data --
    e.g. one DP-scoped communicator's id appeared in both worker-0's and
    worker-1's own push streams). No assumption about which/how many
    hosts a communicator spans -- just asks VM which (hostname, member)
    pairs are currently reporting under this exact comm_id, anywhere.
    Uses _query_instant_real_ts + _is_fresh_row (P21.6.1 fix -- was plain
    _query_instant, which returns query-eval-time, not real sample age;
    see _is_fresh_row's own docstring for the real cross-job misattribution
    this let through), not the module's bare _query_instant. Callers here
    wrap in try/except themselves rather than each helper silently
    swallowing errors with its own, different semantics."""
    try:
        rows = _query_instant_real_ts(vm_url, f'agg_samples_seen{{comm="{comm}"}}')
    except Exception:
        return []
    return sorted({(r["metric"]["hostname"], r["metric"]["member"]) for r in rows if _is_fresh_row(r)})


def _comm_slurm_job_id(vm_url, comm):
    """P27-hotfix5 -- the real slurm_job_id this comm's own members are
    reporting under (same query shape as _comm_cross_node_members, just
    reading a different label off the identical rows) -- needed to look
    up that job's real TOTAL rank count (AlertEngine._job_total_member_
    count) when deciding whether the P27.2.5 rank-0 exclusion's own
    justification (rank 0 as one of MANY ranks) actually applies to this
    comm's job, or not. Returns None if zero or more than one distinct
    job id is seen (an honest "can't tell," never a guess)."""
    try:
        rows = _query_instant_real_ts(vm_url, f'agg_samples_seen{{comm="{comm}"}}')
    except Exception:
        return None
    ids = {r["metric"].get("slurm_job_id") for r in rows if _is_fresh_row(r)} - {None, ""}
    return next(iter(ids)) if len(ids) == 1 else None


def _member_other_comms(vm_url, hostname, member, exclude_comm, lookback_s=60):
    """P21.6 -- which OTHER communicators does this exact physical process
    (hostname, member -- a PID, stable per P21.5's identity mapping)
    report under, besides exclude_comm. This is the entire mechanism for
    discovering a "dependent" communicator relationship: two
    communicators are related if and only if they share a physical
    member, discovered from data, not from knowing in advance that "TP
    nests inside DP" or any other specific parallelism-strategy shape --
    the same query works unchanged for however P22/P23 structure their
    own communicators."""
    now = time.time()
    qs = urllib.parse.urlencode({
        "match[]": f'agg_samples_seen{{hostname="{hostname}",member="{member}"}}',
        "start": str(now - lookback_s),
        "end": str(now),
    })
    try:
        with urllib.request.urlopen(f"{vm_url}/api/v1/label/comm/values?{qs}", timeout=10) as resp:
            d = json.load(resp)
        return [c for c in d.get("data", []) if c != exclude_comm]
    except Exception:
        return []


def _comm_local_member_count(vm_url, hostname, comm):
    """P21.6 -- how many distinct physical members currently report under
    this communicator, ON THIS SPECIFIC HOST -- the exact same quantity
    node_aggregator_ref.py's own self-detection floor (score_cv_window's
    `len(cvs) < 3`, score_mean_window's `len(members_here) < 3`) is
    evaluated against, so "is this dependent communicator below the
    self-detection floor" means exactly the same thing here as it does
    inside the aggregator itself -- not a separately-invented notion.

    P21.6.1 fix -- the original query here was a bare `count(agg_samples_
    seen{...})`, which counts SERIES, not distinct members: agg_samples_
    seen carries a separate series per (member, bucket, coll), so any
    member reporting under more than one (bucket, coll) -- confirmed real,
    live, on this exact cluster's TP-pair comm (3 buckets active
    simultaneously: message-size-varying AllReduce buckets plus a tiny
    bucket=4 one) -- inflates the count well past its true member count
    (measured live: a genuine 2-member comm read as 6). Since
    SELF_DETECTION_FLOOR=3, that silently made a real, below-floor 2-
    member comm look like 6 -- >= floor -- so _find_dependent_small_comms
    (and, new this session, _dcgm_fallback_evaluate's own standalone
    sweep) would never even recognize it as needing the fallback at all,
    independent of anything the fallback itself gets right. Fixed by
    counting members via `count by (member)` first, THEN counting those
    groups -- the same two-step distinct-count VictoriaMetrics needs for
    this (no single instant-vector function collapses both dimensions at
    once)."""
    try:
        res = _query_instant(
            vm_url,
            f'count(count by (member) (agg_samples_seen{{hostname="{hostname}",comm="{comm}"}}))'
        )
    except Exception:
        return 0
    if not res:
        return 0
    return int(float(res[0]["value"][1]))


SELF_DETECTION_FLOOR = 3  # P21.6 -- matches node_aggregator_ref.py's own mean/CV member-count guard exactly (not a new number)


def _query_gpu_slot(vm_url, hostname, member):
    """P21.7 -- the real physical GPU slot index (0-7) for this (hostname,
    member), read from agg_member_gpu_slot_index -- pushed by
    node_aggregator_ref.py directly from Inspector's own new dump-schema
    field (gpu_slot_index, populated in the plugin via cudaGetDevice() on
    the correct thread at communicator-init time; see this session's
    inspector.cc changes). Returns None if not yet known (not pushed, or
    VM query failed), stale (P21.6.1 fix -- see _is_fresh_row's own
    docstring: a plain _query_instant read let a dead job's last-pushed
    slot value answer for a different, later job's member id of the same
    number, within VM's own default staleness window), or if the plugin
    itself couldn't capture it (-1, reported as-is, distinguished from
    "not yet queried")."""
    try:
        res = _query_instant_real_ts(vm_url, f'agg_member_gpu_slot_index{{hostname="{hostname}",member="{member}"}}')
    except Exception:
        return None
    if not res or not _is_fresh_row(res[0]):
        return None
    return int(float(res[0]["value"][1]))


def build_finding_for_alert(vm_url, hostname, comm, member, bucket, stat_name, z, mm, worst_val, peer_mean,
                             dcgm_host_map, anomaly_ts=None):
    """Adapter: shapes a single live reading into the exact `stat_hits`
    input P18k_classifier.build_single_rank_finding expects, then calls
    the REAL cause-gathering + tier-decision logic (not reimplemented
    here). corroborating is intentionally empty -- this alert engine reads
    one bucket at a time, live, and does not (yet) cross-reference the
    OTHER bucket's simultaneous reading the way the offline classifier's
    two-bucket replay does. Documented as a known simplification, not
    silently dropped.

    P21.5 found a real, precise gap here: build_single_rank_finding used
    to derive BOTH which node to query DCGM against (worker_of(rank), i.e.
    "rank < 8 -> worker-0 else worker-1") AND which GPU slot on that node
    (local_idx = rank % 8) from a single integer "rank" argument -- baked-
    in 8-ranks-per-node, stable-global-rank arithmetic. `member` (a PID)
    is not safe to feed through that arithmetic directly.

    P21.7 closed the GPU-slot half of that gap (P21.5 could only fix node-
    targeting): Inspector's dump schema carries a real gpu_slot_index
    field (populated in the plugin via cudaGetDevice() on the correct
    thread at communicator-init time -- see inspector.cc). Queried here
    via _query_gpu_slot.

    P26.5-maintenance -- cluster-topology-agnostic fix: the P21.7-era
    version of this comment (still worth reading for the historical
    reasoning) constructed a "placeholder rank" that made classifier.py's
    OWN worker_of()/rank%8 arithmetic reverse-engineer the right node and
    slot, which only worked because it still relied on this cluster's own
    2-node/8-GPU-per-node shape (baked into that arithmetic itself, not
    fixed by the placeholder trick). build_single_rank_finding now accepts
    real host/local_idx directly (host=, local_idx=) -- this adapter
    already HAS both real values (hostname is a real, live-discovered
    string; real_slot is real gpu_slot_index data), so they're passed
    straight through with no arithmetic translation, no reverse-
    engineering, and no dependency on this cluster's specific shape at
    all. Falls back to slot 0 only if the real slot genuinely isn't known
    yet (query failure, or the plugin itself couldn't capture it) --
    reported honestly via the returned finding's own dcgm_gpu_slot_known
    field rather than silently pretending slot 0 is confirmed."""
    # report.py's CONFIRMED/PROBABLE formatters unconditionally :.2f-format
    # mm once "statistic" is present; nan is an honest, non-crashing stand-in
    # for "could not be computed this cycle" (rare: needs all members'
    # agg_cv_exec_time present in the same freshness window).
    mm_safe = mm if mm is not None else float("nan")
    result = {"z_node": z, "maxmed_node": mm_safe, "worst_val": worst_val, "peer_mean": peer_mean}
    stat_hits = [(stat_name, result)]
    real_slot = _query_gpu_slot(vm_url, hostname, member)
    slot_known = real_slot is not None and real_slot >= 0
    slot = real_slot if slot_known else 0
    # P27.3-timing-gap fix -- real, secondary issue confirmed alongside
    # the type-mismatch root cause (see storage_evidence.query_iowait_
    # window's own docstring for that one): a bare "now" live window
    # (the old rank_ts_range=None default below) is anchored to WHEN
    # THIS FUNCTION HAPPENS TO RUN, not to when the anomaly it's
    # investigating actually occurred -- measured live, this engine's
    # own real detection latency (calibration + the deliberately-wide
    # FRESH_THRESH_S lookback _check_mean/_check_cv already use to
    # bridge node_aggregator_ref.py's irregular push cadence) is
    # routinely ~30-34s, well past IOWAIT_LIVE_WINDOW_S (10s) -- for a
    # SUSTAINED fault the live window still overlaps real activity by
    # luck (confirmed live), but a brief, already-ended one would be
    # missed by the time this runs, for any real customer fault,
    # regardless of when in a job it happens.
    #
    # Fix: reuse rank_ts_range exactly as build_single_rank_finding's
    # own offline/batch-replay callers already do -- no new, parallel
    # timing mechanism -- anchored on anomaly_ts, the SAME real sample
    # timestamp _check_mean/_check_cv already compute via
    # _query_instant_real_ts (existing infrastructure, not new) to know
    # this alert should fire at all. Padded backward by
    # IOWAIT_LIVE_WINDOW_S (catches a fault that started slightly
    # before the window that detected it closed) and extended forward
    # to real "now" (still catches a fault that's still ongoing) --
    # strictly a superset of the old bare-"now" window, never narrower.
    rank_ts_range = None
    if anomaly_ts is not None:
        rank_ts_range = {member: (anomaly_ts - p18k.IOWAIT_LIVE_WINDOW_S, time.time())}
    finding = p18k.build_single_rank_finding(
        member, stat_hits, primary=bucket, corroborating={},
        dcgm_host_map=dcgm_host_map, ib_hosts=None,
        buffer=None, rank_ts_range=rank_ts_range,
        # P26.5-maintenance -- `member` IS the real jailed PID iowait_
        # agent.bt already resolves to via its own curtask->thread_pid
        # fix, so this needs no new identity-resolution work at all,
        # unlike the GPU-slot problem P21.7 solved above: the live path
        # already has exactly what storage_evidence.py's check needs.
        iowait_pid=member, iowait_log_dir=IOWAIT_LOG_DIR,
        # P26.5-maintenance -- real host/slot, passed directly (see this
        # function's own docstring above); `member` above is now only
        # ever used as a display label (immediately overridden by
        # finding["rank"] = member below), never for node/slot arithmetic.
        host=hostname, local_idx=slot,
    )
    # Overridden with the real identity for display -- the DCGM query
    # itself already ran (correctly, against the right node, and now the
    # right GPU slot when known) above.
    finding["host"] = hostname
    finding["rank"] = member
    finding["comm"] = comm
    finding["dcgm_gpu_slot_known"] = slot_known
    finding["dcgm_gpu_slot_used"] = slot
    return finding


CONFIDENCE_HEADER = {
    "CONFIRMED": "CONFIRMED",
    "PROBABLE": "PROBABLE",
    "UNCONFIRMED": "UNCONFIRMED",
}

# P20d Part B -- paging-severity policy, decided from a real headroom
# analysis (not picked first and justified after), on the original P20d
# 3-hour blind run's real numbers:
#
#   62 false positives, ALL confidence=PROBABLE, ALL cv-mm in [0.14, 1.10]
#   (i.e. genuinely indistinguishable from peers -- classic base-rate CV
#   noise, not a real signal).
#   21 CONFIRMED alerts fired across the whole run. ALL 21 were real true
#   positives on an actually-injected fault. ZERO false CONFIRMED alerts.
#
# Two threshold-based fixes were evaluated against this same data and
# rejected:
#   - Raising CV_Z_THRESH alone: false positives and true positives overlap
#     heavily in z (FP max=449.3 exceeds 22 of the 31 real TP z-values), so
#     no single z cutoff cleanly separates them -- even z>300 still lets one
#     FP through while discarding 22/31 real detections.
#   - Adding an mm floor (e.g. mm>1.5) to the CV fire gate: this DOES
#     cleanly separate the two populations (0/62 FP survive at any floor
#     from 1.5 up) -- but it also discards the two real detections from
#     fault D, the short/marginal-duration case (P20c gap-closure): both of
#     D's only alerts have mm~1.0 (a real fire correctly triggered by CV's
#     z-only gate, exactly as CV was designed -- classifier.py's own
#     comment: "mm has almost no margin here... gate primarily on z").
#     Adding an mm floor would silently turn a real short-fault detection
#     into a false negative -- not an acceptable trade for closing the
#     noise problem.
#
# What the data actually supports: the false-positive problem lives
# entirely in the PROBABLE tier, and CONFIRMED already has a clean,
# zero-false-positive track record in this data. So the fix that matches
# the evidence is a PAGING-SEVERITY split, not a detection-threshold
# change: PROBABLE/UNCONFIRMED alerts still fire, still get logged and
# shown on the dashboard (nothing about detection sensitivity changes,
# fault D's short-fault case is still caught, exactly as before) -- they
# simply do not page a human. Only CONFIRMED pages. This is a severity
# distinction, not a suppression: every alert this engine ever decides to
# emit is still emitted and visible.
PAGE_WORTHY_TIERS = {"CONFIRMED"}


def severity_for_tier(tier):
    """Returns ('PAGE', ...) or ('LOG-ONLY', ...) -- see PAGE_WORTHY_TIERS
    docstring above for the real data this policy is based on."""
    if tier in PAGE_WORTHY_TIERS:
        return "PAGE", "pages a human -- CONFIRMED has a 0-false-positive track record in the validated data"
    return ("LOG-ONLY", "dashboard/log only, does not page -- PROBABLE/UNCONFIRMED carry the run's entire "
                         "measured false-positive rate (22-25/hour); still fully recorded, not suppressed")


def format_alert(finding, coverage):
    """Renders the final alert text: P18k_report's real per-tier
    formatting, prefixed with rank/node/fault-type/confidence-tier/severity
    and suffixed with the coverage-guard annotation (never silently
    swallowed -- always shown, healthy or degraded)."""
    rank = finding["rank"]
    host = finding["host"]
    comm = finding.get("comm")
    fault_type = finding.get("type_candidate", "compute")
    tier = finding["tier"]
    severity, severity_reason = severity_for_tier(tier)
    # P21.5 -- comm= shown alongside member= (still labeled "rank" here for
    # minimal disruption to existing log-scraping/tooling) since a bare
    # member id is only meaningful within the communicator it was reported
    # under, not as a standalone global rank number.
    header = (f"[ALERT] rank={rank} comm={comm} node={host} type={fault_type} confidence={tier} "
              f"severity={severity}")
    # P21.6 -- cascade-mislocalization fix: shown prominently, right after
    # the header, before any cause-evidence body -- either confirming a
    # real re-attribution (host/rank/comm above already reflect it) or
    # disclosing that the location above is provisional. Never silently
    # omitted when set.
    cascade_line = f"\n\n{finding['cascade_note']}" if finding.get("cascade_note") else ""
    # P21.7 -- DCGM cause-evidence (sm_clock, throttle_reasons, etc. in the
    # body below) is now targeted at the real physical GPU slot when
    # Inspector's gpu_slot_index was actually captured for this member;
    # disclosed honestly when it wasn't (falls back to slot 0, which may
    # not be this member's real GPU).
    if not finding.get("dcgm_gpu_slot_known", False):
        cascade_line += (
            f"\n\nDCGM TARGETING UNCERTAIN: this process's real GPU slot index "
            f"was not available (gpu_slot_index missing or capture failed at the "
            f"Inspector plugin level) -- the DCGM cause-evidence below was queried "
            f"against slot 0 as a fallback, which may not be this member's actual "
            f"GPU. Treat sm_clock/throttle_reasons/etc. below as unverified for "
            f"this specific alert."
        )

    if tier == "UNCONFIRMED":
        ruled_out, impossible, class2_flags, next_steps = p18k.build_review_lists(finding)
        body = p18k_report.build_unconfirmed_report(finding, ruled_out, impossible, class2_flags, next_steps)
    else:
        body = p18k_report.format_finding(finding)

    cov_line = (
        f"\nCoverage guard: {'DEGRADED' if coverage['degraded'] else 'full'} "
        f"(peer_fraction={coverage['peer_fraction']}, absolute_rate_frac={coverage['absolute_rate_frac']})"
    )
    if coverage["degraded"]:
        cov_line += "\n  " + "\n  ".join(coverage["reasons"])
        cov_line += ("\n  Confidence CAPPED at PROBABLE due to degraded coverage "
                      "regardless of cause-evidence tier." if tier == "CONFIRMED" else "")
    cov_line += f"\n  {coverage['blind_spot_note']}"
    severity_line = f"\nSeverity: {severity} -- {severity_reason}"

    return f"{header}{cascade_line}\n\n{body}\n{cov_line}{severity_line}"


class AlertEngine:
    def __init__(self, vm_url, hostnames=None,
                 dcgm_host_map=None, poll_interval=3.0,
                 hostname_refresh_s=30.0):
        """hostnames=None (the default) means dynamic: the engine discovers
        which nodes are actually live from VM itself and re-checks
        periodically, so a 100-node deployment doesn't require a
        hand-maintained list and picks up nodes joining/leaving without a
        restart -- this was a hardcoded ("worker-0", "worker-1") default
        before, which silently never polled any node outside that pair.
        Passing an explicit hostnames tuple still pins a fixed set (used by
        existing controlled tests) and disables discovery entirely.
        hostname_refresh_s bounds how often discovery re-queries VM --
        every poll_once() would just add one more query per host to the
        per-poll load; a periodic refresh is enough to track real
        node churn.

        P21.5 -- the old `buckets=("12295680","28323840")` constructor
        parameter is gone. Those were exact byte counts calibrated for one
        specific single-communicator workload -- confirmed directly this
        never matches a different workload or parallelism strategy's real
        message sizes. (comm, bucket) pairs are now ALWAYS discovered live
        per hostname (see _refresh_comm_buckets), the same dynamic pattern
        hostnames already used, with no static-override escape hatch --
        keeping one would just let a caller quietly re-hardcode the exact
        thing this fix removes."""
        self.vm_url = vm_url
        self._static_hostnames = tuple(hostnames) if hostnames else None
        self._explicit_dcgm_host_map = dcgm_host_map
        self.hostname_refresh_s = hostname_refresh_s
        self._hostnames_refreshed_at = 0.0
        self.hostnames = list(self._static_hostnames) if self._static_hostnames else []
        # P21.5 -- {hostname: [(comm, bucket), ...]}, discovered live;
        # refreshed on the same cadence as hostname discovery.
        self._comm_buckets = {}
        self._comm_buckets_refreshed_at = {}
        self.dcgm_host_map = dcgm_host_map or {h: True for h in self.hostnames}
        self.poll_interval = poll_interval
        self.cv_tracker = CVPersistenceTracker(T.CV_Z_THRESH, T.PERSIST_WINDOW, T.PERSIST_REQUIRED)
        # P20d-hardening Step 2.1 -- live host/CPU detection was a single
        # live snapshot with no persistence, unlike every other detector
        # in this system (a real, disclosed gap from the last session,
        # not observed live but architecturally real: one bad
        # /proc/loadavg read could false-fire CONFIRMED). Reuses
        # CVPersistenceTracker with this project's OWN existing
        # convention (thresholds.py: PERSIST_WINDOW=3/PERSIST_REQUIRED=3,
        # i.e. 3-of-3 consecutive, not an invented 2-of-3) rather than a
        # new mechanism. z_thresh is HOST_LOAD_RATIO_MIN=4.0, the same
        # validated (n=9) threshold this whole system already uses for
        # host contention -- unchanged, just now gated on persistence
        # the same way CV is.
        self.host_tracker = CVPersistenceTracker(p18k.HOST_LOAD_RATIO_MIN, T.PERSIST_WINDOW, T.PERSIST_REQUIRED)
        # P20k-closeout Part B -- job-wide throughput. z_thresh=0.0 by
        # design: observe() is fed a "deficit" (calibrated floor minus the
        # real measured rate), so "deficit > 0" means "below floor" -- this
        # reuses CVPersistenceTracker genuinely, not by hacky inversion,
        # and keeps the same 3-of-3 persistence convention as everything
        # else, so one transient blip doesn't fire this on its own.
        self.throughput_tracker = CVPersistenceTracker(0.0, T.PERSIST_WINDOW, T.PERSIST_REQUIRED)
        # P27.2 -- 2-member timing-asymmetry fallback (see
        # _timing_asymmetry_fallback_evaluate's own docstring). Same
        # z_thresh=0.0-as-deficit idiom as throughput_tracker just above:
        # observe() is fed (PATH_B_AND_TIMING_SUPPRESS_RATIO - ratio), so
        # "deficit > 0" means "this member's current mean is below its own
        # historical baseline by more than the suppression ratio allows" --
        # genuine reuse of CVPersistenceTracker, same 3-of-3 persistence
        # convention as every other detector here, not a one-off mechanism.
        self.timing_fallback_tracker = CVPersistenceTracker(0.0, T.PERSIST_WINDOW, T.PERSIST_REQUIRED)
        # P27-hotfix9 -- rolling pool of recently-fired timing-fallback
        # alerts, for _correlate_firing_timing_alerts. NOT scoped to a
        # single poll cycle: confirmed this session that two real,
        # genuinely related alerts (a direct fault and its own downstream
        # echo) don't reliably satisfy their independent 3-sample
        # persistence requirements in the exact same cycle -- each comm's
        # own data reaches its 3rd real sample on its own real schedule.
        # RECENT_TIMING_CORRELATION_WINDOW_S gives real margin (3x the
        # poll interval) for that natural drift.
        self._recent_timing_fires = []
        self.was_firing = defaultdict(bool)
        self.alerts = []  # list of rendered alert strings, in order
        # P20d-closeout dead-man's-switch state -- tracked separately from
        # self.alerts on purpose. "0 alerts emitted" must never be readable
        # as "healthy" without also checking this: it's exactly as true
        # during a genuinely healthy run as it was during the run3 vm_url
        # incident (3h10m, zero data ever reached VM, zero alerts as a
        # direct consequence of there being nothing to evaluate -- not of
        # there being nothing wrong).
        self.pipeline_down = {}
        self.n_pipeline_down_cycles = 0
        # P20d-closeout Part A/B state -- background-thread-driven, see
        # the module docstring above _hosts_have_active_job for why these
        # can't run synchronously inside the 3s poll loop.
        self._alerts_lock = threading.Lock()
        self._last_network_check_at = 0.0
        self._last_host_check_at = 0.0
        self._last_nvlink_check_at = 0.0
        self._network_thread = None
        self._host_thread = None
        self._nvlink_thread = None
        # P21.6.1 -- standalone 2-member-communicator DCGM fallback state.
        self._last_dcgm_fallback_check_at = 0.0
        self._dcgm_fallback_thread = None
        # P27-hotfix2 -- nv-hostengine liveness, same dead-man's-switch
        # discipline as pipeline_down/n_pipeline_down_cycles above: found
        # this session that nv-hostengine (the persistent DCGM daemon every
        # DCGM-sourced cause-check depends on) can be down with zero loud
        # signal anywhere -- every DCGM query just silently degrades to
        # "couldn't check" instead of "confirmed no thermal cause".
        self.dcgm_hostengine_down = {}
        self.n_dcgm_hostengine_down_cycles = 0
        self._last_dcgm_hostengine_check_at = 0.0
        self._dcgm_hostengine_thread = None
        # P20d-closeout Part C -- measured live against this exact engine
        # (2 real hosts + synthetic hostnames to simulate scale): at 0ms
        # added query latency the old sequential poll_once() already took
        # ~7ms/host (0.77s at n=100); at a modest, realistic 5ms/query VM
        # latency it broke the 3s budget between n=20-50 (3.95s at n=50);
        # at 20ms/query it broke between n=10-20. This pool runs each
        # host's per-bucket checks (and the pipeline-health check)
        # concurrently instead of sequentially -- same per-host query
        # count and logic, gathered in parallel. Sized once, reused across
        # every poll_once() call rather than rebuilt each cycle (thread
        # creation itself is not free at a 3s cadence).
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=64)
        self._refresh_hostnames(force=True)

    def _refresh_hostnames(self, force=False):
        if self._static_hostnames:
            return
        now = time.time()
        if not force and (now - self._hostnames_refreshed_at) < self.hostname_refresh_s:
            return
        discovered = _discover_hostnames(self.vm_url)
        if discovered:
            self.hostnames = discovered
            if not self._explicit_dcgm_host_map:
                self.dcgm_host_map = {h: True for h in discovered}
        self._hostnames_refreshed_at = now

    def _check_pipeline_health_one(self, hostname):
        health = pipeline_health.check_pipeline_health(self.vm_url, hostname)
        was_down = self.pipeline_down.get(hostname, False)
        if health["down"]:
            self.pipeline_down[hostname] = True
            self.n_pipeline_down_cycles += 1
            print(f"[PIPELINE-DOWN] hostname={hostname} :: " + " ; ".join(health["reasons"]),
                  flush=True)
        elif was_down:
            self.pipeline_down[hostname] = False
            age = health["last_heartbeat_age_s"]
            print(f"[PIPELINE-RECOVERED] hostname={hostname} "
                  f"heartbeat_age={age:.1f}s" if age is not None else
                  f"[PIPELINE-RECOVERED] hostname={hostname}", flush=True)
        else:
            self.pipeline_down[hostname] = False

    def _check_pipeline_health(self):
        """Runs every poll cycle, unconditionally, for every hostname --
        never gated on whether any rank-level check below finds anything.
        This is the fix for the run3 vm_url incident specifically: that
        failure produced zero alerts for 3h10m not because the system was
        healthy, but because there was no fresh data to evaluate at all,
        and nothing was unconditionally checking for that condition
        itself. Dispatched through the shared pool (Part C) -- each
        host's check is independent (its own VM queries, its own
        pipeline_down[hostname] entry), so concurrent execution changes
        nothing about correctness, only wall-clock time."""
        futures = [self._pool.submit(self._check_pipeline_health_one, h) for h in self.hostnames]
        for f in futures:
            f.result()

    def _refresh_comm_buckets(self, hostname, force=False):
        """P21.5 -- discovers this hostname's real (comm, bucket) pairs
        live from VM, replacing the old fixed `self.buckets` tuple. Two
        queries per newly-seen comm (comms, then that comm's own buckets)
        -- refreshed on the same periodic cadence as hostname discovery,
        not on every poll_once(), for the same reason hostnames aren't
        rediscovered every cycle either.

        P22.2 -- now (comm, bucket, coll) triples, since _discover_buckets
        itself now returns (bucket, coll) pairs -- see that function's own
        docstring for why bucket alone is no longer a safe target."""
        now = time.time()
        last = self._comm_buckets_refreshed_at.get(hostname, 0.0)
        if not force and (now - last) < self.hostname_refresh_s:
            return
        self._comm_buckets_refreshed_at[hostname] = now
        pairs = []
        for comm in _discover_comms(self.vm_url, hostname):
            for bucket, coll in _discover_buckets(self.vm_url, hostname, comm):
                pairs.append((comm, bucket, coll))
        if pairs or hostname not in self._comm_buckets:
            self._comm_buckets[hostname] = pairs

    def _run_check(self, label, fn, *args):
        """P27-hotfix defense-in-depth -- a check's own cause-evidence
        gathering can hit a real bug this project hasn't found yet (the
        DCGM-None crash was exactly this: an unhandled AttributeError deep
        in build_finding_for_alert). Without this, poll_once()'s
        f.result() re-raises that exception, killing the whole process
        silently -- no final summary, no indication detection stopped.
        This is NOT a substitute for fixing real bugs at their source
        (see the classifier.py/cause_metrics.py fixes this same session)
        -- it's a last-resort net so an unexpected failure in ONE check
        logs loudly and the poll loop keeps running, rather than the
        entire engine going dark."""
        try:
            fn(*args)
        except Exception as e:
            print(f"[CHECK-FAILED] {label} args={args}: {type(e).__name__}: {e}", file=sys.stderr)

    def _poll_host(self, hostname):
        self._refresh_comm_buckets(hostname)
        for comm, bucket, coll in self._comm_buckets.get(hostname, []):
            self._run_check("cv", self._check_cv, hostname, comm, bucket, coll)
            self._run_check("mean", self._check_mean, hostname, comm, bucket, coll)
            self._run_check("outlier_count", self._check_outlier_count, hostname, comm, bucket, coll)
        self._run_check("rank0_outlier_rate", self._check_rank0_outlier_rate, hostname)
        self._run_check("job_throughput", self._check_job_throughput, hostname)

    def poll_once(self):
        self._refresh_hostnames()
        # These run synchronously in the main poll thread (not inside the
        # ThreadPoolExecutor below) -- an unhandled exception in any of
        # them would kill run()'s loop just as directly as the per-check
        # crash this fix targets. Same defense-in-depth net.
        self._run_check("pipeline_health", self._check_pipeline_health)
        self._run_check("network_check", self._maybe_launch_network_check)
        self._run_check("nvlink_check", self._maybe_launch_nvlink_check)
        self._run_check("host_check", self._maybe_launch_host_check)
        self._run_check("dcgm_fallback_check", self._maybe_launch_dcgm_fallback_check)
        self._run_check("dcgm_hostengine_check", self._maybe_launch_dcgm_hostengine_check)
        futures = [self._pool.submit(self._poll_host, h) for h in self.hostnames]
        for f in futures:
            f.result()

    def _find_dependent_small_comms(self, hostname, comm):
        """P21.6 -- generic dependent-communicator discovery. A communicator
        is "dependent" on (hostname, comm) if it shares at least one
        physical member (a PID, P21.5's identity mapping) with it, AND is
        below SELF_DETECTION_FLOOR members on its own host -- discovered
        entirely from real shared physical membership, not from knowing
        in advance that "TP nests inside DP": the same lookup applies
        unchanged whatever nesting shape a future parallelism strategy
        (P22 FSDP, P23 MoE) creates. Returns {(dep_hostname, dep_comm):
        set of members shared with the alerting communicator}."""
        dependents = {}
        count_cache = {}
        for h, m in _comm_cross_node_members(self.vm_url, comm):
            for other_comm in _member_other_comms(self.vm_url, h, m, comm):
                key = (h, other_comm)
                if key not in count_cache:
                    count_cache[key] = _comm_local_member_count(self.vm_url, h, other_comm)
                if count_cache[key] < SELF_DETECTION_FLOOR:
                    dependents.setdefault(key, set()).add(m)
        return dependents

    def _correlate_firing_timing_alerts(self, newly_fired):
        """P27-hotfix9 -- real, generic cross-comm correlation for the
        timing-asymmetry fallback, closing the gap this session's own
        Hybrid TP+PP validation found: a single real fault on one rank
        produced genuine, physically real downstream effects on comms
        that never contained the faulted rank at all (a fault on rank0's
        PP handoff caused a real 25x elevation on stage1's OWN, unrelated
        TP comm, and separately caused the rank1<->rank3 PP pair -- never
        faulted -- to fire its own alert). Each comm's fallback evaluates
        in total isolation; there was no way to tell "these alerts are
        probably the same root cause echoing outward" from "these are two
        genuinely independent faults."

        Reuses _find_dependent_small_comms -- P21.6's own real shared-
        physical-member discovery, already validated for the DCGM cascade
        mechanism -- as the ONLY topology signal, rather than a second,
        parallel member-overlap graph. No hardcoded "PP causes TP effects"
        rule: the link this builds is purely "does comm B share a real
        physical member with comm A's own ELEVATED (waiting/echo) side" --
        the same real member-identity data this whole fallback already
        reads, applicable to any future comm shape or hybrid combination,
        not just this one.

        Directionality without inventing a new concept: this fallback's
        OWN established signature (confirmed throughout this project) is
        "the TARGET reads normal, its WAITING PARTNER reads elevated" --
        so a comm's real elevated member is structurally the one MORE
        LIKELY to be propagating a delay it picked up elsewhere (it's
        genuinely waiting on something), while the target reads its own
        normal execution time. If that SAME elevated member is a real
        physical member of ANOTHER comm that ALSO fired this cycle, this
        alert is flagged a POSSIBLE ECHO of that other alert -- never a
        confident reassignment (same discipline _rank_dependents_by_
        deviation already established for the structurally similar DCGM
        cascade problem: a confident wrong answer is worse than a
        disclosed "possibly related, unconfirmed"). An alert with no such
        incoming link is a ROOT_CAUSE_CANDIDATE -- still just a label for
        "nothing currently firing explains this one," not a claim of
        certainty.

        Real, measured timing is included as disclosed CORROBORATING
        evidence only, never a gate: each alert's own per_member[...]["ts"]
        (the real dump timestamp already used for persistence dedup) is
        compared, and whether the downstream alert's onset is at or after
        the candidate upstream alert's is reported honestly either way --
        confirmed this session that magnitude alone does NOT reliably
        separate cause from echo (downstream echoes measured 10-25x,
        sometimes larger than the direct 8.0x/7.54x cause), so magnitude
        is reported as evidence, never used as the discriminator.

        This is an ENRICHMENT layer only: newly_fired's own alerts have
        already been emitted via _emit_timing_fallback by the time this
        runs (see the caller) -- nothing here suppresses, delays, or
        alters any real alert. A genuinely independent second fault with
        no real shared-member link to anything else firing is reported
        as its own ROOT_CAUSE_CANDIDATE, exactly as loudly as before.

        Returns a list of annotation dicts (one per input alert), never
        raises -- a correlation failure degrades to "no correlation
        context available," not a blocked alert.

        P27-hotfix9b -- single-hop was confirmed, live, against hybrid's
        own real fault to MISS the actual case: the direct alert's comm
        (rank0<->rank2) and the downstream echo alert's comm (rank1<->
        rank3) share NO physical member with each other AT ALL (entirely
        disjoint 2-member sets) -- the real causal link only exists
        THROUGH an intermediate comm (stage1's own TP pair, containing
        BOTH rank2 and rank3) that wasn't itself a firing timing-fallback
        alert. Generalized to a real, bounded BFS over the SAME shared-
        physical-member primitive (_member_other_comms/_comm_cross_node_
        members -- no new query type), hopping through ANY real comm
        (firing or not) up to MAX_CORRELATION_HOPS, with the full real
        path (every intermediate comm and shared member) kept for
        disclosure -- never collapsed into an opaque "these are related"
        claim. Confirmed live this exact traversal reaches hybrid's real
        2-hop case (direct comm --[rank2]--> stage1 TP comm --[rank3]-->
        echo comm)."""
        if len(newly_fired) < 2:
            return [{"hostname": a["hostname"], "comm": a["comm"], "role": "root_cause_candidate",
                      "evidence": []} for a in newly_fired]

        by_key = {(a["hostname"], a["comm"]): a for a in newly_fired}
        echo_info = {}
        for a in newly_fired:
            key = (a["hostname"], a["comm"])
            echo_m = next((m for m in a["per_member"] if m != a["member"]), None)
            if echo_m is None:
                continue
            echo_info[key] = {
                "member": echo_m,
                "hostname": a["per_member"][echo_m]["hostname"],
                "ts": a["per_member"][echo_m]["ts"],
                "ratio": a["per_member"][echo_m]["ratio"],
            }

        incoming = defaultdict(list)
        for up_key, echo in echo_info.items():
            try:
                reachable = self._bfs_shared_member_comms(echo["hostname"], echo["member"],
                                                            max_hops=MAX_CORRELATION_HOPS)
            except Exception:
                continue
            for dn_key, path in reachable.items():
                if dn_key == up_key or dn_key not in by_key:
                    continue
                dn_a = by_key[dn_key]
                dn_echo = echo_info.get(dn_key)
                ts_order_consistent = (dn_echo is not None and dn_echo["ts"] >= echo["ts"])
                incoming[dn_key].append({
                    "upstream_hostname": up_key[0], "upstream_comm": up_key[1],
                    "shared_member": echo["member"], "shared_member_host": echo["hostname"],
                    "upstream_echo_ratio": echo["ratio"],
                    "upstream_echo_ts": echo["ts"],
                    "downstream_echo_ts": dn_echo["ts"] if dn_echo else None,
                    "ts_order_consistent": ts_order_consistent,
                    "path": path,
                })

        annotations = []
        for a in newly_fired:
            key = (a["hostname"], a["comm"])
            if key in incoming:
                annotations.append({"hostname": a["hostname"], "comm": a["comm"],
                                     "role": "possible_echo", "evidence": incoming[key]})
            else:
                annotations.append({"hostname": a["hostname"], "comm": a["comm"],
                                     "role": "root_cause_candidate", "evidence": []})
        return annotations

    def _bfs_shared_member_comms(self, start_hostname, start_member, max_hops):
        """P27-hotfix9b -- real, bounded breadth-first traversal of the
        shared-physical-member comm graph, starting from one real
        (hostname, member). Reuses ONLY existing primitives (_member_
        other_comms, _comm_cross_node_members -- the same ones _find_
        dependent_small_comms already uses for its own single-hop case)
        -- no new query type, no new notion of "topology" invented.

        Returns {(hostname, comm): path} for every comm reachable within
        max_hops, where path is the real, ordered list of hops taken
        (each hop: {hostname, comm, via_member}) -- kept so a caller can
        DISCLOSE the real intermediate comms/members a correlation
        passed through, never collapse a multi-hop link into an opaque
        claim. max_hops bounds this to a small, cheap search (below-
        floor comm graphs are sparse in every real job this project has
        tested) -- not an unbounded traversal."""
        visited_comms = {}
        visited_members = {(start_hostname, start_member)}
        frontier = [(start_hostname, start_member, [])]
        for _ in range(max_hops):
            next_frontier = []
            for hh, m, path in frontier:
                try:
                    other_comms = _member_other_comms(self.vm_url, hh, m, exclude_comm="")
                except Exception:
                    continue
                for oc in other_comms:
                    key = (hh, oc)
                    if key in visited_comms:
                        continue
                    new_path = path + [{"hostname": hh, "comm": oc, "via_member": m}]
                    visited_comms[key] = new_path
                    try:
                        comm_members = _comm_cross_node_members(self.vm_url, oc)
                    except Exception:
                        comm_members = []
                    for mh, mm in comm_members:
                        if (mh, mm) not in visited_members:
                            visited_members.add((mh, mm))
                            next_frontier.append((mh, mm, new_path))
            frontier = next_frontier
            if not frontier:
                break
        return visited_comms

    def _emit_correlation_report(self, newly_fired, annotations):
        """P27-hotfix9 -- disclosed-uncertainty rendering of _correlate_
        firing_timing_alerts' output, same "ranked real evidence, never a
        confident single winner" presentation _rank_dependents_by_
        deviation's own docstring established. Printed as its own labeled
        block, separate from each alert's own [ALERT] emission (already
        done by the time this runs) -- pure added context, never a
        replacement for or suppression of any real alert.

        P27-hotfix9b -- discloses the real, full path (every intermediate
        comm/shared-member hop _bfs_shared_member_comms found), not just
        an opaque "these are related" claim -- confirmed necessary live:
        hybrid's own real echo link passes through one intermediate comm
        neither alert's own comm ever mentions on its own."""
        if len(newly_fired) < 2:
            return
        lines = [f"[CORRELATION] multiple timing-fallback alerts within the recent "
                 f"{RECENT_TIMING_CORRELATION_WINDOW_S:.0f}s correlation window -- real shared-physical-member "
                 f"evidence below; ROOT_CAUSE_CANDIDATE is not a confirmed diagnosis and POSSIBLE_ECHO is not "
                 f"a confirmed dismissal -- every alert above already fired independently and remains fully "
                 f"valid regardless of this annotation."]
        for a, ann in zip(newly_fired, annotations):
            tag = f"comm={a['comm']} hostname={a['hostname']} member={a['member']} slot={a['slot']}"
            if ann["role"] == "root_cause_candidate":
                lines.append(f"  {tag}: ROOT_CAUSE_CANDIDATE (no other alert in this window is reachable, "
                              f"within {MAX_CORRELATION_HOPS} real shared-physical-member hops, from this "
                              f"one's own elevated/waiting side)")
            else:
                for ev in ann["evidence"]:
                    order_note = ("consistent with" if ev["ts_order_consistent"] else
                                  "NOT consistent with -- disclosed anyway")
                    dn_ts = ev["downstream_echo_ts"]
                    dn_ts_str = f"{dn_ts:.1f}" if dn_ts is not None else "unknown"
                    path = ev.get("path") or []
                    if len(path) <= 1:
                        path_str = "direct (1 hop)"
                    else:
                        hops = " -> ".join(f"comm={h['comm']} hostname={h['hostname']} "
                                            f"(via member={h['via_member']})" for h in path)
                        path_str = f"{len(path)} hops: {hops}"
                    lines.append(
                        f"  {tag}: POSSIBLE_ECHO of comm={ev['upstream_comm']} "
                        f"hostname={ev['upstream_hostname']} (real path: {path_str}; that upstream alert's "
                        f"own elevated/waiting member={ev['shared_member']} on {ev['shared_member_host']}, "
                        f"ratio={ev['upstream_echo_ratio']:.2f} at ts={ev['upstream_echo_ts']:.1f}; "
                        f"real timestamp order {order_note} this alert's own onset at ts={dn_ts_str}) -- "
                        f"possibly related, NOT confirmed; this alert stays fully valid on its own evidence."
                    )
        print("\n".join(lines), flush=True)

    def _find_true_rank0_member(self):
        """P21.7 -- identifies the physical process performing global rank
        0's role, using the REAL gpu_slot_index field (not exec-time-
        ratio inference -- P21.6 confirmed that's circular/confounded:
        rank 0's own persistent overhead bias is exactly what makes it
        LOOK like the answer via ratio-based inference, so ratio-based
        inference can never reliably rule it OUT). This project's own,
        consistently-observed launch convention (every launch script:
        RANK=0 iff hostname is the alphabetically-first of the node list
        given to `-w`, --node_rank=$RANK) means global rank 0 is always
        local_rank 0 -- gpu_slot_index==0 -- on the alphabetically-first
        currently-discovered hostname. A real, disclosed, project-
        specific convention (not a universal property of every possible
        launcher), consistent with classifier.py's own pre-existing
        worker_of()/rank%8 arithmetic, which already assumes this exact
        2-node layout and is not touched here. Returns (hostname, member)
        or None if not yet determinable (no hostnames discovered, or no
        member reporting slot 0 there yet).

        P26.5-maintenance fix -- this used plain _query_instant, whose
        value[0] is the QUERY's own eval time, not the sample's real
        ingestion time (the exact bug already fixed this project's own
        _comm_cross_node_members/_query_gpu_slot, in this same file, for
        the identical reason: a dead job's stale slot-0 member reads back
        as if it were live, misidentifying "rank 0" as a member from a
        PREVIOUS, unrelated job that happens to share the same physical
        slot-0 position). Now uses _query_instant_real_ts + _is_fresh_row,
        the same fix, applied to this sibling call site."""
        if not self.hostnames:
            return None
        first_host = sorted(self.hostnames)[0]
        try:
            res = _query_instant_real_ts(self.vm_url, f'agg_member_gpu_slot_index{{hostname="{first_host}"}}')
        except Exception:
            return None
        for row in res:
            if not _is_fresh_row(row):
                continue
            if float(row["value"][1]) == 0:
                return (first_host, row["metric"]["member"])
        return None

    def _rank_dependents_by_deviation(self, dependents):
        """P21.6 Step 2 -- REAL cross-communicator correlation, attempted
        as a path to confident localization; downgraded to ranked evidence
        only after direct testing showed picking a single "winner" is NOT
        reliable (see this session's closeout report). For each dependent
        small communicator, pulls its own members' real
        agg_mean_exec_time_us (already pushed unconditionally for every
        member of every window regardless of whether a z-score could be
        computed) and computes a maxmed-style ratio (max/median-of-peers)
        -- well-defined for any peer count >= 1, unlike stdev, so it works
        even for the 2-member communicators the self-detection floor
        exists because of.

        Originally this picked the single largest ratio as a confident
        re-attribution. Tested directly against the real GPU4 clock-lock
        fault: it worked in the sense that GPU4's own TP pair DID show a
        real, meaningfully elevated ratio (~12x) -- but a DIFFERENT,
        unrelated TP pair (the one containing global rank 0) consistently
        showed an even LARGER ratio (~25-39x), and was picked instead,
        every time. Confirmed this is not a fluke: rank 0's persistent
        master-process bookkeeping overhead is a real, ALREADY-DOCUMENTED
        artifact in this codebase (detection.py's own EXCLUDE_CV_EXTRA={0}
        comment: "wins 'worst' in 72% of 100-sample healthy windows") --
        it was excluded in the original single-communicator design, but
        P21.5's generic redesign could not carry that exclusion forward
        (Inspector's data has no way to identify "which physical member is
        rank 0" at all -- the same disclosed gap as the GPU-slot-index
        problem). Picking a single "winner" by raw ratio alone means this
        persistent, non-fault bias can and does outcompete a real,
        smaller-but-genuine fault signal. This is exactly the "fragile
        heuristic" the task warned against -- a confident wrong answer is
        worse than an honest "don't know," so no single winner is claimed.

        P21.7 update: previously, no single winner was claimed BECAUSE
        rank 0's bias couldn't be told apart from a real fault using
        exec-time ratios alone. Now that gpu_slot_index gives a real,
        independent way to identify rank 0 (_find_true_rank0_member, NOT
        exec-time inference), that member is excluded from consideration
        here entirely -- closing the confound this docstring describes,
        without needing to trust ratio magnitude to distinguish "real
        fault" from "known rank-0 artifact." A 2-member dependent
        containing rank 0 naturally drops out (only 1 member left,
        below the len(vals)<2 floor) rather than being force-ranked.

        Returns every REMAINING dependent with a computable ratio, sorted
        largest-first, as REFERENCE EVIDENCE for a human to reason with --
        still never presented as a confirmed re-attribution (other,
        undocumented persistent biases could exist for other members;
        only the ALREADY-DOCUMENTED rank-0 one is specifically closed)."""
        true_rank0 = self._find_true_rank0_member()
        ranked = []
        for (h, dep_comm) in dependents:
            members = sorted({m for hh, m in _comm_cross_node_members(self.vm_url, dep_comm) if hh == h})
            vals = {}
            for m in members:
                if true_rank0 is not None and (h, m) == true_rank0:
                    continue  # P21.7 -- known rank-0 artifact, excluded by real identity, not inferred
                res = _query_instant_real_ts(self.vm_url, f'agg_mean_exec_time_us{{hostname="{h}",comm="{dep_comm}",member="{m}"}}')
                for row in res:
                    if self._fresh(row):
                        vals[m] = float(row["value"][1])
            if len(vals) < 2:
                continue
            sorted_vals = sorted(vals.values())
            n = len(sorted_vals)
            median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
            worst_m = max(vals, key=lambda m: abs(vals[m] - median))
            peers = sorted(v for m, v in vals.items() if m != worst_m)
            peer_median = peers[len(peers) // 2] if len(peers) % 2 else \
                (peers[len(peers) // 2 - 1] + peers[len(peers) // 2]) / 2
            lo, hi = min(vals[worst_m], peer_median), max(vals[worst_m], peer_median)
            ratio = hi / lo if lo else float("inf")
            ranked.append((h, dep_comm, worst_m, ratio))
        ranked.sort(key=lambda t: t[3], reverse=True)
        return ranked

    def _local_comm_members(self, hostname, comm):
        """P21.6.1 -- this communicator's real physical members ON THIS
        SPECIFIC HOST, from the same cross-node-membership data
        _rank_dependents_by_deviation already reads (no new query type)."""
        return sorted({m for hh, m in _comm_cross_node_members(self.vm_url, comm) if hh == hostname})

    def _dcgm_fallback_evaluate(self, hostname, comm, dcgm_cache=None):
        """P21.6.1 -- the ONE shared core both the cascade-triggered path
        (called from _emit, below, when a larger related communicator just
        fired) and the standalone periodic path (_maybe_launch_dcgm_
        fallback_check) converge on. Neither path re-implements its own
        query or tier logic; both just decide WHEN to call this.

        The gap being closed: node_aggregator_ref.py's score_mean_window/
        score_cv_window (the ONLY thing that would normally trigger
        build_finding_for_alert's DCGM query at all) hard-gate on
        `len(members) < 3` -- SELF_DETECTION_FLOOR -- so for any
        communicator below that floor (2-member TP pairs, in every shape
        seen so far: TP training's own TP-internal comm, and TP-inference's
        ENTIRE communicator set, which has no larger comm to ride along
        with at all) the NCCL peer-relative z-score that would normally
        drive a DCGM lookup simply never gets computed, and the query never
        happens -- not because DCGM couldn't answer, but because nothing
        ever asked it.

        p18k.path_b_clock_cause (P21.6.1, factored out of
        build_single_rank_finding) was ALREADY, structurally, comparing a
        target GPU against every OTHER GPU on its own physical node via
        DCGM -- never against "the other member of this specific
        communicator" -- so it was never actually dependent on
        communicator size to begin with. This function just calls it
        directly, per real physical member (via _query_gpu_slot's already-
        validated gpu_slot_index targeting), without waiting for an NCCL
        stat that structurally cannot exist below the floor.

        Requires >=2 real, slot-resolved local members (a lone member with
        no counterpart to disagree with can't produce the asymmetry this
        fallback demands) and fires ONLY when EXACTLY ONE resolves
        CONFIRMED via determine_confirmed_path's Path B and the OTHERS do
        not -- if zero or more than one member show anomalous evidence,
        this returns None (stay silent/uncertain) rather than guess, the
        same discipline _rank_dependents_by_deviation's own docstring
        already established for ratio-based ranking (a confident wrong
        answer is worse than an honest "don't know").

        dcgm_cache: optional {hostname: (dcgm, err)} the CALLER maintains
        across multiple _dcgm_fallback_evaluate calls in the same sweep
        cycle (e.g. _maybe_launch_dcgm_fallback_check, which can call this
        once per below-floor comm -- 50 real comms measured live on this
        exact 2-node cluster under a real TP_SIZE=4 job, since P2P-style
        Send/Recv sub-communicators below the floor turned out to be far
        more numerous than the 2-member-TP-pair case this fallback was
        originally scoped for). Without this, EVERY call re-issues its own
        `dcgmi dmon` SSH round-trip even when several comms share the same
        host in the same cycle -- measured live: 50 targets x ~195ms =
        9.8s for what should be 2 real per-host queries. When omitted
        (the cascade path's own usage, called at most a few times per
        alert, not per-sweep), this fetches directly as before -- no
        caching needed there since it's never called at this multiplicity.

        Returns None, or (member, slot, per_member) where per_member is
        {member: {"slot": int, "tier": str, "path_b_clock": dict|None}}
        for every locally-resolved member (for reporting real DCGM values,
        including the non-firing member's own nominal readings)."""
        members = self._local_comm_members(hostname, comm)
        if len(members) < 2 or not self.dcgm_host_map.get(hostname, True):
            return None
        if dcgm_cache is not None:
            if hostname not in dcgm_cache:
                try:
                    dcgm_cache[hostname] = p18k.query_dcgm_all_gpus(hostname)
                except Exception:
                    dcgm_cache[hostname] = (None, "query_dcgm_all_gpus raised")
            dcgm, err = dcgm_cache[hostname]
        else:
            try:
                dcgm, err = p18k.query_dcgm_all_gpus(hostname)
            except Exception:
                return None
        if err or not dcgm:
            return None
        per_member = {}
        for m in members:
            slot = _query_gpu_slot(self.vm_url, hostname, m)
            if slot is None or slot < 0:
                continue
            pb = p18k.path_b_clock_cause(dcgm, slot)
            # determine_tier_single_rank indexes cause["class1"] directly
            # (not .get) -- matching build_single_rank_finding's own real
            # cause-dict shape (class1={} always present, even when this
            # fallback -- unlike that function -- never populates it,
            # since class1 there holds the DCGM class-2/class-1 raw-field
            # dump this fallback doesn't separately gather).
            tier = p18k.determine_tier_single_rank({"class1": {}, "path_b_clock": pb} if pb else {"class1": {}})
            per_member[m] = {"slot": slot, "tier": tier, "path_b_clock": pb}
        confirmed = [m for m, d in per_member.items() if d["tier"] == "CONFIRMED"]
        if len(per_member) >= 2 and len(confirmed) == 1:
            m = confirmed[0]
            return (m, per_member[m]["slot"], per_member)
        return None

    def _member_exec_time_current(self, hostname, comm, member, bucket, coll):
        """P27.2.3 -- one member's real, smoothed CURRENT exec-time
        reading for the 2-member timing-asymmetry fallback, sourced from
        node_aggregator_ref.py's own agg_mean_exec_time_us{comm=,member=,
        bucket=,coll=,...} series (already pushed for every member, not
        just node_aggregator's own internal "worst" pick -- no new
        metric). Scoped to ONE specific (bucket, coll) -- P27.2.1's own
        fix, unchanged: confirmed live this session that pooling every
        bucket a member reports under mixes a real substantial-payload
        collective with a trivial administrative one (a real 4-byte
        AllReduce, confirmed present) whose timing is dominated by
        scheduling jitter, not real communication cost.

        Smoothed via avg_over_time(...[DCGM_FALLBACK_CHECK_INTERVAL_S])
        rather than one bare instant sample -- P27.2.2's own fix,
        unchanged: confirmed live that individual real collectives on
        this hardware spike 100-1000x above their own typical value
        (100us -> 100,000-300,000us) unpredictably, for any member,
        independent of any fault, and a single such spike landing in one
        window with no damping at all produced a spurious reading on a
        completely healthy member. Freshness (is this member still
        actively, recently reporting at all) is checked first via a
        plain instant read, before trusting the smoothed value.

        Returns (current_mean, newest_real_ts) or None if there's no
        fresh data at all -- an honest "can't compute this," never a
        fabricated 0."""
        promql = (f'agg_mean_exec_time_us{{hostname="{hostname}",comm="{comm}",member="{member}",'
                  f'bucket="{bucket}",coll="{coll}"}}')
        fresh_rows = [r for r in _query_instant_real_ts(self.vm_url, promql) if self._fresh(r)]
        if not fresh_rows:
            return None
        newest_ts = max(float(r["value"][0]) for r in fresh_rows)
        cur_rows = _query_instant(self.vm_url, f'avg_over_time({promql}[{int(DCGM_FALLBACK_CHECK_INTERVAL_S)}s])')
        if not cur_rows:
            return None
        cur_vals = [float(r["value"][1]) for r in cur_rows]
        return (sum(cur_vals) / len(cur_vals), newest_ts)

    def _push_visibility_metric(self, metric_line):
        """V1-beta-dashboard-followup -- generic push helper, factored
        out of _push_role_baseline_exclusion's own already-established
        wire format (metric{labels} value timestamp_ms via /api/v1/
        import/prometheus) rather than duplicating that try/except/
        urlopen boilerplate for each new "just visibility" metric this
        session adds (host load ratio, nv-hostengine liveness, the
        storage Path C verdict) -- same discipline: a push failure here
        degrades to "this specific reading isn't recorded," never blocks
        the live detection path that already computed the value.
        `metric_line` is the metric{labels} portion only; timestamp is
        always real, current wall-clock time (these are live, per-cycle
        readings, not historical replay)."""
        try:
            now_ms = int(time.time() * 1000)
            body = f"{metric_line} {now_ms}\n"
            req = urllib.request.Request(f"{self.vm_url}/api/v1/import/prometheus",
                                          data=body.encode(), method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[visibility-push-failed] {metric_line}: {type(e).__name__}: {e}", file=sys.stderr)

    def _push_role_baseline_exclusion(self, hostname, comm, member, bucket, coll, role_rank, role_n):
        """P27-hotfix6 -- self-exclusion for the role-aware baseline pool.
        Real, confirmed vulnerability this closes: agg_mean_exec_time_us
        history has no way to tell "this was a healthy calibration
        reading" apart from "this was itself an anomalous/fault-condition
        reading" -- confirmed live this session (PP): 7 repeated fault-
        injection tests all targeting the same rank pushed their OWN
        elevated readings into role_rank=1's history, until the median
        of that history sat almost exactly on the fault's own value
        (~52363 vs current ~52427), making an ONGOING real fault read as
        statistically normal against its own poisoned baseline. Generic
        by construction -- fires for ANY below-floor workload using this
        fallback (TP2, TP-inference, PP), not a PP-specific check.

        Deliberately reuses the EXACT elevated_thresh + TIMING_FALLBACK_
        MAD_MULTIPLE gap check already computed live in _timing_
        asymmetry_fallback_evaluate for THIS SAME member/window (see its
        own call site below) -- no new anomaly-detection algorithm. A
        member found elevated even ONCE (not gated on the 3-sample
        persistence requirement that gates actually FIRING an alert) is
        excluded here, since a single elevated window is already real
        evidence this reading does not belong in "what normal looks
        like" -- waiting for persistence would let 1-2 poisoning
        readings through before the 3rd finally excludes them.

        AlertEngine has never pushed to VM before this (it was purely a
        reader); this reuses the aggregator's own exact push wire format
        (metric{labels} value timestamp_ms via /api/v1/import/
        prometheus) rather than inventing a new protocol. A push failure
        here degrades to "this specific poisoning event isn't recorded,"
        never blocks the live detection path that called it -- same
        never-let-cause-evidence-block-detection discipline as every
        other best-effort side-channel in this file."""
        try:
            now_ms = int(time.time() * 1000)
            body = (f'agg_role_baseline_excluded{{hostname="{hostname}",comm="{comm}",member="{member}",'
                    f'bucket="{bucket}",coll="{coll}",role_rank="{role_rank}",role_n="{role_n}"}} 1 {now_ms}\n')
            req = urllib.request.Request(f"{self.vm_url}/api/v1/import/prometheus",
                                          data=body.encode(), method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[{hostname}] role-baseline exclusion push failed for comm={comm} member={member}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

    def _excluded_role_pool_members(self, hostname, bucket, coll, role_rank, role_n):
        """P27-hotfix6 -- real (comm, member) pairs ever marked excluded
        (see _push_role_baseline_exclusion) for this exact role shape,
        over the same long lookback as the history query itself -- so a
        pair excluded at any point in the past stays excluded for as
        long as its own contaminated agg_mean_exec_time_us reading would
        otherwise still be found by last_over_time."""
        selector = (f'agg_role_baseline_excluded{{hostname="{hostname}",bucket="{bucket}",coll="{coll}",'
                    f'role_rank="{role_rank}",role_n="{role_n}"}}')
        try:
            rows = _query_instant(self.vm_url, f'last_over_time({selector}[{int(ROLE_XJOB_LOOKBACK_S)}s])')
        except Exception:
            return set()
        return {(r["metric"].get("comm"), r["metric"].get("member")) for r in rows}

    def _job_workload_sig(self, hostname, slurm_job_id):
        """P27-hotfix7 -- real workload_signature() this job pushed
        (node_aggregator_ref.py's own agg_job_workload_sig_info, pushed
        once per job at real throughput-stability time -- (n_comms,
        collective-type composition, message-size-set) fingerprint,
        the exact same discriminator query_throughput_history already
        uses for cross-job comparison, reused here directly rather than
        building a second, parallel signature scheme). Returns the sig
        string, or None if this job never reached stability (a real,
        genuine short-lived job) or reported more than one distinct sig
        (shouldn't happen, but an honest "can't tell" beats a guess)."""
        if not slurm_job_id:
            return None
        try:
            rows = _query_instant(self.vm_url,
                                   f'last_over_time(agg_job_workload_sig_info{{hostname="{hostname}",'
                                   f'slurm_job_id="{slurm_job_id}"}}[{int(ROLE_XJOB_LOOKBACK_S)}s])')
        except Exception:
            return None
        sigs = {r["metric"].get("sig") for r in rows} - {None}
        return next(iter(sigs)) if len(sigs) == 1 else None

    def _member_role_baseline(self, hostname, bucket, coll, role_rank, role_n, exclude_comm):
        """P27-hotfix4 -- real, per-ROLE cross-JOB historical baseline,
        for below-floor pairs whose two members' real expected work is
        PERMANENTLY different (confirmed live this session: PP's stage0
        Recv legitimately ~2.22x stage1's Recv, a real structural
        property, not noise or a fault -- exceeds this fallback's own
        elevated_thresh, a real, quantified false-positive risk against
        _cross_comm_peer_median's symmetric-peer assumption, which was
        only ever validated against TP2's genuinely symmetric case).

        Reuses agg_mean_exec_time_us -- already pushed for every member
        with real role_rank/role_n labels (see node_aggregator_ref.py's
        own P27-hotfix4 comment) -- no new metric. Same last_over_time +
        exclude-current-comm + Python-side filtering pattern as
        query_throughput_history (node_aggregator_ref.py) and
        _cross_comm_peer_median above, reused directly for consistency,
        not novelty. Compares THIS role's current reading only against
        OTHER JOBS' real historical readings for the SAME role on the
        SAME comm shape (role_rank, role_n) -- never against the OTHER
        member's current value, which is exactly the assumption that
        breaks on a structurally-asymmetric pair like PP.

        Returns (median, mad) of real historical values, or (None, None)
        if no cross-job history exists yet for this role (a real,
        honest cold start -- callers fall back to _cross_comm_peer_median,
        same graceful-degradation discipline as self-calibration
        elsewhere in this project)."""
        if role_rank in (None, "na") or role_n in (None, "na"):
            return None, None
        selector = (f'agg_mean_exec_time_us{{hostname="{hostname}",bucket="{bucket}",coll="{coll}",'
                    f'role_rank="{role_rank}",role_n="{role_n}"}}')
        # P27-hotfix4 (bugfix) -- last_over_time over a real, long lookback
        # (see ROLE_XJOB_LOOKBACK_S's own comment for why this replaced an
        # instant+_fresh() check), same query_throughput_history pattern:
        # finds each historical (comm,member) series' last real value
        # regardless of how long ago that job actually ran, rather than
        # only ones still reporting fresh data right now.
        hist_rows = _query_instant(self.vm_url, f'last_over_time({selector}[{int(ROLE_XJOB_LOOKBACK_S)}s])')
        # P27-hotfix7 -- real, confirmed cross-WORKLOAD contamination
        # this closes (Hybrid TP+PP validation session): plain PP and
        # hybrid TP+PP both produce a PP-shaped comm with the IDENTICAL
        # byte-size boundary tensor (TP shards weights, not activations,
        # so the PP boundary tensor size is unchanged by TP sharding),
        # so the (hostname,bucket,coll,role_rank,role_n) key alone
        # cannot tell them apart -- confirmed live: hybrid PP's real
        # ~11,700us baseline got pooled with plain PP's real ~5,500us
        # baseline, producing a false 2.13x "elevated" reading on a
        # genuinely healthy hybrid run. Fixed by additionally requiring
        # each candidate historical row's OWN job to share the CURRENT
        # job's real workload_signature (n_comms, collective-type
        # composition, message-size set) -- the exact discriminator
        # query_throughput_history already uses for the identical real
        # problem at the job-throughput level, reused here unchanged,
        # not a new parallel scheme. Strict on both sides: if the
        # current job's own sig can't be determined (never reached
        # throughput stability), OR a candidate row's job's sig can't be
        # determined, that filtering is skipped/that row is dropped
        # respectively -- never a guessed match, same discipline as
        # _job_total_member_count's own conservative "can't tell" default.
        current_job_id = _comm_slurm_job_id(self.vm_url, exclude_comm)
        current_sig = self._job_workload_sig(hostname, current_job_id) if current_job_id else None
        if current_sig is not None:
            sig_cache = {}
            filtered = []
            for r in hist_rows:
                jid = r["metric"].get("slurm_job_id")
                if jid not in sig_cache:
                    sig_cache[jid] = self._job_workload_sig(hostname, jid)
                if sig_cache[jid] == current_sig:
                    filtered.append(r)
            hist_rows = filtered
        # P27-hotfix6 -- real (comm, member) pairs this same role shape
        # has EVER had marked as an anomalous reading (see
        # _push_role_baseline_exclusion), dropped from the pool here so
        # a persistent/repeated fault can never poison its own future
        # baseline. Same exclude-current-comm filtering this already
        # did, just widened to also drop known-anomalous OTHER comms.
        excluded = self._excluded_role_pool_members(hostname, bucket, coll, role_rank, role_n)
        hist_keys = {(r["metric"].get("comm"), r["metric"].get("member"))
                     for r in hist_rows if r["metric"].get("comm") != exclude_comm} - excluded
        if not hist_keys:
            return None, None
        vals = [float(r["value"][1]) for r in hist_rows
                if (r["metric"].get("comm"), r["metric"].get("member")) in hist_keys]
        if not vals:
            return None, None
        median = statistics.median(vals)
        mad = statistics.median([abs(v - median) for v in vals])
        # P27-hotfix8 -- real, data-driven reliability gate replacing the
        # old rank-0-identity exclusion (see ROLE_BASELINE_MAX_RELATIVE_MAD's
        # own comment for the real measured values behind this threshold).
        # A role whose own historical pool is this internally noisy can't
        # be trusted for a ratio-based elevation check -- degrading to
        # (None, None) here routes the caller to the existing
        # _cross_comm_peer_median fallback, the same honest-degradation
        # path a genuine cold start already takes, not a new code path.
        if median > 0 and (mad / median) > ROLE_BASELINE_MAX_RELATIVE_MAD:
            return None, None
        return median, mad

    def _member_role_labels(self, hostname, comm, member, bucket, coll):
        """P27-hotfix4 -- reads this member's own real role_rank/role_n
        labels straight off its already-pushed agg_mean_exec_time_us
        series (set by node_aggregator_ref.py at push time) -- no new
        query pattern, just reading labels this file didn't read before.
        Returns (role_rank, role_n) strings, or (None, None) if no fresh
        row exists (honest, matches every other "can't compute this"
        return in this file)."""
        promql = (f'agg_mean_exec_time_us{{hostname="{hostname}",comm="{comm}",member="{member}",'
                  f'bucket="{bucket}",coll="{coll}"}}')
        rows = [r for r in _query_instant_real_ts(self.vm_url, promql) if self._fresh(r)]
        if not rows:
            return None, None
        met = rows[0]["metric"]
        return met.get("role_rank"), met.get("role_n")

    def _cross_comm_peer_median(self, hostname, bucket, coll, exclude_comm):
        """P27.2.3 -- real, EXTERNAL peer baseline for the 2-member
        timing-asymmetry fallback, replacing the earlier same-member
        self-history baseline entirely (see _timing_asymmetry_fallback_
        evaluate's own docstring for why: confirmed live, twice, that
        self-history is vulnerable to this hardware's own real per-
        collective timing spikes contaminating either side of the ratio,
        producing spurious CONFIRMED/PAGE alerts on completely healthy
        pairs -- a second reviewer's independent cross-check against raw
        NCCL-inspector data caught this before it reached production).

        The real insight this reuses, not invents: TP_SIZE=2 (and
        TP-inference's identical topology) creates ONE independent
        2-member communicator PER dp-shard, and every shard runs the
        SAME model-parallel operation on the SAME tensor shapes -- so
        every OTHER shard's TP-pair, on the SAME host, reporting the SAME
        (bucket, coll), is a real, live, external peer group for "what a
        normal value looks like on this job right now," discovered from
        data exactly the same way _discover_buckets/_comm_cross_node_
        members already discover comm membership -- no new query
        mechanism, no static assumption that TP nests inside DP or any
        other specific parallelism shape. This is this project's own
        foundational peer-relative principle (compare to OTHER members,
        never self), just applied ACROSS the multiple below-floor
        siblings a below-floor comm's own topology creates, since
        node_aggregator_ref.py's internal peer-relative check is blind to
        anything below SELF_DETECTION_FLOOR by construction.

        Queried WITHOUT a comm= filter (deliberately -- pools every
        currently-active comm reporting this exact hostname/bucket/coll),
        then rows belonging to exclude_comm (the pair actually under
        test) are dropped in Python, the same "do the stats here, not in
        PromQL" convention this file already uses throughout. Smoothed
        the same way _member_exec_time_current is (avg_over_time over
        DCGM_FALLBACK_CHECK_INTERVAL_S) for consistency, though pooling
        several DISTINCT members' values already gives real, independent
        cross-sectional robustness a single member's own self-history
        never had -- median, not mean, so even an unsmoothed stray spike
        on ONE peer member can't move the whole baseline.

        Returns (peer_median, peer_mad), or (None, None) if no OTHER
        same-shape comm is currently active on this host -- an honest,
        disclosable "no external peer group available right now" (e.g. a
        workload whose below-floor comm has no live same-shape sibling
        at this exact moment), never a fabricated answer from an empty
        set.

        P27.2.6 -- peer_mad (median absolute deviation of peer_vals
        around peer_median) added alongside the median, from the SAME
        pool already gathered here (no new query). Real, measured need:
        confirmed live on the 2-node original cluster's own real TP2
        traffic that peer_vals themselves carry substantial real spread
        at this timescale (one live measurement: 8 peers, bucket=25874004,
        median=628.8us, MAD=457.7us -- MAD at 73% of the median), the
        same real per-collective timing-spike phenomenon this function's
        own docstring already documents as having broken the earlier
        self-history design. A bare ratio-vs-median check has no way to
        tell "this comm's peer group is itself this noisy right now" from
        "this member is genuinely elevated" -- see _timing_asymmetry_
        fallback_evaluate's own P27.2.6 use of this value.

        P27.2.7 -- real, confirmed gap this closes: the live-only pool
        above structurally empties out for any below-floor workload whose
        siblings finish asynchronously (TP-inference's real, confirmed
        case -- see PEER_SIBLING_LOOKBACK_S's own comment for the live
        trace evidence). When zero siblings are currently live, this now
        falls back to each sibling's own LAST real value within
        PEER_SIBLING_LOOKBACK_S -- same last_over_time + Python-side
        filtering pattern _member_role_baseline already uses for its
        cross-JOB history, just scoped to siblings of THIS SAME job
        (via _comm_slurm_job_id, reused unchanged) instead of across
        jobs, so an unrelated older job's leftover stale data can never
        contaminate this pool. Only engaged when the live pool is
        completely empty -- a sibling that's still live is always
        preferred unchanged, same discipline as every other honest-
        degradation fallback in this file."""
        selector = f'agg_mean_exec_time_us{{hostname="{hostname}",bucket="{bucket}",coll="{coll}"}}'
        fresh_rows = [r for r in _query_instant_real_ts(self.vm_url, selector) if self._fresh(r)]
        fresh_keys = {(r["metric"].get("comm"), r["metric"].get("member"))
                      for r in fresh_rows if r["metric"].get("comm") != exclude_comm}
        if fresh_keys:
            smoothed_rows = _query_instant(self.vm_url, f'avg_over_time({selector}[{int(DCGM_FALLBACK_CHECK_INTERVAL_S)}s])')
            peer_vals = [float(r["value"][1]) for r in smoothed_rows
                         if (r["metric"].get("comm"), r["metric"].get("member")) in fresh_keys]
        else:
            job_id = _comm_slurm_job_id(self.vm_url, exclude_comm)
            if job_id is None:
                return None, None
            hist_rows = _query_instant(self.vm_url, f'last_over_time({selector}[{int(PEER_SIBLING_LOOKBACK_S)}s])')
            same_job_keys = {(r["metric"].get("comm"), r["metric"].get("member"))
                              for r in hist_rows
                              if r["metric"].get("comm") != exclude_comm and r["metric"].get("slurm_job_id") == job_id}
            if not same_job_keys:
                return None, None
            peer_vals = [float(r["value"][1]) for r in hist_rows
                         if (r["metric"].get("comm"), r["metric"].get("member")) in same_job_keys]
        if not peer_vals:
            return None, None
        peer_median = statistics.median(peer_vals)
        peer_mad = statistics.median([abs(v - peer_median) for v in peer_vals])
        return peer_median, peer_mad

    def _timing_asymmetry_fallback_evaluate(self, hostname, comm):
        """P27.2 -- the raw-timing half of the 2-member fallback,
        alongside (never replacing) _dcgm_fallback_evaluate's DCGM Path B
        half. Closes a real, disclosed gap _dcgm_fallback_evaluate's own
        docstring does NOT cover: DCGM's clock/power comparison only ever
        detects a hardware-level clock/thermal suppression -- confirmed
        this session (P27 sweep) that a pure software time.sleep()
        straggler produces NO DCGM signature at all (both members read
        nominal sm_clock/power), so _dcgm_fallback_evaluate correctly,
        honestly returns None for exactly this fault class, not because
        the fault isn't real but because it's the wrong evidence type for
        it. Confirmed live, twice independently this sweep (TP_SIZE=2 and
        TP-inference, both real 2-member communicators): the TRUE
        straggler's own agg_mean_exec_time_us reads LOW (it sleeps BEFORE
        entering the collective, so its own recorded collective-timing
        window is short/normal), while its healthy PARTNER's reads
        elevated (the partner is the one actually waiting on the late
        arrival) -- the inverse of what a naive "whichever member looks
        elevated is the straggler" rule would conclude.

        P27.2.3 -- REDESIGNED comparison axis, replacing self-history
        entirely. The original design (a member's current vs its OWN
        avg_over_time history) was confirmed live, via a second
        reviewer's independent raw-data cross-check, to false-fire
        CONFIRMED/PAGE on completely healthy pairs: this hardware's real
        per-collective timing spikes (100-1000x, unpredictable, both
        members, unrelated to any fault) can land in one side of a
        self-history ratio and not the other purely by chance -- and
        cross-member corroboration against the PARTNER's own concurrent
        (equally noisy) current value was checked and does NOT fix this
        (confirmed with the exact failing numbers: a 0.042 cross-member
        ratio on a pair whose full-run medians were statistically
        identical). The comparison axis now used instead:
        _cross_comm_peer_median -- an EXTERNAL, real, live peer group
        from every OTHER same-shape below-floor comm on this host,
        immune to any single comm's own internal noise by construction
        (a median across several independent members). This is the same
        self-relative-timing SIGNATURE as before (the true straggler
        reads low, its partner reads high) -- only what "low"/"high" are
        measured against has changed, from an internal, noise-vulnerable
        self-history to an external, cross-sectional ground truth.

        Deliberately scoped to EXACTLY 2 members, matching the only shape
        this signature has actually been confirmed in -- a 3+-member
        below-floor comm (if one ever exists) gets no fallback claim from
        this function; that would be extrapolating past real evidence.
        Also returns None, honestly, whenever no OTHER same-shape comm is
        currently active to serve as the peer group (see _cross_comm_
        peer_median) -- a real, disclosable residual gap for a workload
        whose below-floor comm has no live sibling at all, not a bug.

        P27.2.4 -- flip found via a second reviewer's live cross-check
        against this exact run's full-run raw medians: the injected
        target's own reading is NOT the statistically distinguishable
        side of this comparison. Confirmed directly (TP_SIZE=2, target
        rank3): the target's median (106us) sat well inside the SAME
        range most OTHER members on the host read (102-110us) -- a
        cross-comm peer median pools mostly these "normal" values, so the
        target's own ratio against it comes out near 1.0, not suppressed.
        The one statistically unambiguous outlier is the PARTNER, reading
        ~230x the peer median (24338us vs ~105us) -- the real, large,
        easy-to-detect signal here is elevation, not suppression, exactly
        because most members' typical reading is already low and a
        healthy partner only reads far ABOVE that norm while genuinely
        waiting on a real straggler. This function therefore finds
        whichever member is ELEVATED relative to the external peer median
        (ratio > 1/PATH_B_AND_TIMING_SUPPRESS_RATIO -- the same shared
        ratio constant, just inverted for the opposite comparison
        direction, not a new number) and flags its PARTNER (the other
        real physical member of this exact 2-member comm) as the
        straggler -- the same "waiting partner reveals a straggler it
        isn't itself" principle established throughout this whole
        session, now applied via external peer evidence instead of
        self-history.

        Fires only when EXACTLY ONE member is elevated (and, by
        construction of a 2-member comm, exactly one partner exists to
        flag) -- the same "asymmetric, not ambiguous" two-part
        corroboration _dcgm_fallback_evaluate already requires, just
        sourced from timing instead of DCGM. Also requires that elevation
        to PERSIST for T.PERSIST_REQUIRED consecutive real samples via
        self.timing_fallback_tracker (this project's own existing 3-of-3
        persistence convention, reused exactly) before firing.

        Known, disclosed overlap: global rank 0 (this job's torchrun
        rendezvous coordinator) carries real, already-documented
        structural overhead (health_exclusions.py) independent of any
        injected fault, and can show this exact elevated-partner
        signature on its own TP-pair -- confirmed live this session on an
        UNINJECTED pair (worker-2 gsi0/gsi1, ~204x split, n>1000 samples,
        clearly not noise). This project deliberately does NOT statically
        exclude rank 0 from its other peer-relative detectors either
        (node_aggregator_ref.py's EXCLUDE_ALWAYS is empty by design,
        real cause left to a separate RAS-layer mechanism) -- this
        fallback follows the same established convention rather than
        special-casing rank 0 inline, so an alert naming rank 0's TP
        partner is the same already-accepted background-noise class this
        whole project already discloses elsewhere, not a new false-
        positive this fallback introduces.

        Returns None (stay silent/uncertain) whenever evidence is
        missing, ambiguous (0 or 2+ members elevated), or not yet
        persistent -- never a guess. Otherwise returns (member, slot,
        per_member) shaped exactly like _dcgm_fallback_evaluate's own
        return, with per_member[m] = {"slot": int, "ratio": float,
        "current": float, "peer_median": float, "bucket": str,
        "coll": str} so _emit_timing_fallback can render both members'
        real numbers (and the external peer value they were judged
        against) for transparency.

        Bucket selection unchanged from P27.2.1: this comm's own
        (bucket, coll) pairs are discovered live via _discover_buckets,
        and the LARGEST by real message size is picked as the one real,
        substantial-payload collective to evaluate -- a data-derived
        choice, not a hardcoded workload-specific bucket value.

        P27.2.5 -- global rank 0 exclusion, wired in the same surgical
        way _rank_dependents_by_deviation already does (P21.7): confirmed
        live, twice, on separate runs (p1v2 and p1v5), that the real
        physical process performing global rank 0 shows the exact same
        ~1.8-1.9x split against its TP partner every single time
        (11141-11263us vs 19762-20304us, nearly identical numbers across
        independent runs), with zero fault ever injected there -- the
        already-documented rank-0 overhead artifact, not a real fault,
        not noise. Rather than leave this fallback to rediscover the same
        confound _find_true_rank0_member's own docstring already explains
        (ratio-based inference can never reliably rule rank 0 out, since
        its bias is exactly what makes it look like a real answer), this
        reuses that exact function -- real gpu_slot_index identity, not
        exec-time inference -- and skips the whole pair if either member
        IS that real physical identity, before any ratio math runs at
        all. Simpler and more robust than excluding rank 0 from only the
        peer pool or only the "elevated" role: the confound can in
        principle show up on either side of this specific pairing, so
        the whole below-floor comm is treated as unresolvable here,
        exactly as EXCLUDE_ALWAYS being deliberately empty elsewhere in
        this project reflects -- real identity, not a static rule."""
        # P27-hotfix4 (Part 1 fix) -- member discovery is now topology-
        # agnostic: _comm_cross_node_members already returns this comm's
        # real (hostname, member) pairs across EVERY host reporting it
        # (see its own docstring -- no assumption baked in about which or
        # how many hosts a communicator spans), so it works unchanged
        # whether the 2 members are co-located (TP2/TP-inference, every
        # case this fallback was originally validated against) or on
        # different physical nodes entirely (PP's Send/Recv comm --
        # confirmed live this session: _local_comm_members(hostname, comm)
        # returned only 1 member for PP because it filters to ONE host by
        # construction, silently discarding the other stage's real
        # member and making len(members) != 2 always true). Each member
        # now carries its OWN real host through every downstream query
        # instead of borrowing the single `hostname` this function was
        # called with -- that parameter is kept only as the per-host
        # trigger context the caller already iterates on (P21.6.1's own
        # per-host below-floor comm discovery loop), never as a stand-in
        # for "the" host of every member.
        # P27-hotfix8 -- the rank-0-identity exclusion this block used to
        # have (P27.2.5, then P27-hotfix5's job-rank-count rescoping) is
        # REMOVED here, not just rescoped again -- confirmed this session,
        # with real data, that job-level rank-count was never the right
        # axis at all. Hybrid TP+PP (world_size=4) proved job-rank-count
        # can't distinguish "rank 0 is one of many independent below-floor
        # pairs" (TP2's real case) from "rank 0 is a direct member of THIS
        # below-floor comm, whose own fault signal is genuine" (hybrid's
        # real case) -- n_ranks_total=4 incorrectly excluded hybrid's
        # genuine, correctly-attributable fault (rank2's own clean 8.0x
        # elevation, confirmed via its DIRECT partner rank0, never got to
        # fire). Re-investigated whether the role-aware baseline already
        # makes ANY rank-0 handling redundant: real leave-one-out testing
        # against 9 genuinely healthy TP2 runs of rank0/1's own TP-pair
        # (never fault-injected) found 2/9 (~22%) WOULD false-elevate on
        # the role-aware baseline alone -- TP2's real coordinator-overhead
        # artifact is confirmed still present and NOT already suppressed,
        # so real protection is still needed. But the discriminator that
        # actually separates the two real cases isn't rank-0 identity at
        # all: TP2's rank0/1 pair shows genuinely HIGH relative noise in
        # its OWN historical role-baseline pool (measured MAD/median
        # 0.65-0.82 across the 9 real jobs), while hybrid's rank0 PP pair
        # shows LOW relative noise (measured 0.28-0.36) even including a
        # real live fault run. That check -- "is this role's own
        # historical baseline too noisy to trust" -- is now done directly
        # in _member_role_baseline (ROLE_BASELINE_MAX_RELATIVE_MAD),
        # generically, for every below-floor workload, with no notion of
        # "rank 0" needed at all: an unreliable role baseline degrades to
        # the existing _cross_comm_peer_median fallback below, the same
        # honest-degradation path a cold-start role already takes.
        members_with_host = _comm_cross_node_members(self.vm_url, comm)
        if len(members_with_host) != 2:
            return None
        # _discover_buckets is comm-scoped, not member-scoped -- confirmed
        # live (PP) that either participating host already reports the
        # comm's full (bucket, coll) pair set on its own (each host's own
        # member performs BOTH directions across iterations -- e.g. PP's
        # stage0 both Sends its forward activation and Recvs its
        # gradient back on the same comm). Still unioned across every
        # member's distinct host here rather than trusting just one,
        # since nothing guarantees that holds for every future comm shape.
        pairs = set()
        for hh in {hh for hh, _ in members_with_host}:
            pairs.update(_discover_buckets(self.vm_url, hh, comm))
        if not pairs:
            return None
        # P27-hotfix6 (bugfix) -- real, previously-undiscovered root
        # cause behind this session's own intermittent, unexplained
        # non-firing on live PP tests (traced back through what first
        # looked like a persistence-timing problem): pairs is a set (a
        # deliberate union across every member's own host, above), so
        # max(pairs, key=lambda bc: int(bc[0])) ties whenever two DIFFERENT
        # coll types share the exact same message size -- confirmed live
        # this session, PP's Send and Recv are literally the same tensor
        # observed from opposite directions, so they always tie on byte
        # size. Python's max() breaks a tie by whichever candidate a
        # set's own hash-based iteration order happens to yield first,
        # which is NOT stable or meaningful -- confirmed directly: some
        # live polls picked ('4194304','Recv') (the real signal: Recv is
        # the receiving side of a point-to-point transfer, and genuinely
        # reflects real wait time -- this whole investigation's own
        # established "stage0's Recv waits for the full round trip"
        # finding), others silently picked ('4194304','Send') instead (an
        # enqueue-only operation, ~130us, structurally incapable of ever
        # showing a real wait-time fault) -- evaluating the wrong
        # collective type produced a real "no evidence found" every time
        # that happened, with no error or signal that anything was wrong.
        # Fixed with a real, deterministic tie-break: largest size first
        # (unchanged), Recv preferred on a size tie (the principled
        # choice -- Recv is the side that structurally carries wait-time
        # signal in a point-to-point pair; Send never does), coll name
        # alphabetically as a final deterministic tiebreak for any other
        # same-size pairing this fallback hasn't seen yet.
        bucket, coll = min(pairs, key=lambda bc: (-int(bc[0]), 0 if bc[1] == "Recv" else 1, bc[1]))
        per_member = {}
        for hh, m in members_with_host:
            slot = _query_gpu_slot(self.vm_url, hh, m)
            if slot is None or slot < 0:
                return None
            data = self._member_exec_time_current(hh, comm, m, bucket, coll)
            if data is None:
                return None
            current, ts = data
            role_rank, role_n = self._member_role_labels(hh, comm, m, bucket, coll)
            role_median, role_mad = self._member_role_baseline(hh, bucket, coll, role_rank, role_n, exclude_comm=comm)
            if role_median is not None and role_median > 0:
                baseline, mad, source = role_median, role_mad, "role"
            else:
                # P27-hotfix4 -- cross-comm peer fallback computed per-
                # member, using THAT member's own real host, not the
                # single shared `hostname` -- for a cross-node pair each
                # side can have a genuinely different local peer pool (or
                # none at all), same reasoning as the primary baseline
                # above.
                fallback_peer_median, fallback_peer_mad = self._cross_comm_peer_median(hh, bucket, coll, exclude_comm=comm)
                if fallback_peer_median is not None and fallback_peer_median > 0:
                    baseline, mad, source = fallback_peer_median, fallback_peer_mad, "cross_comm_peer"
                else:
                    return None
            per_member[m] = {"slot": slot, "current": current, "peer_median": baseline,
                              "peer_mad": mad, "ratio": current / baseline, "baseline_source": source,
                              "ts": ts, "bucket": bucket, "coll": coll, "hostname": hh,
                              "role_rank": role_rank, "role_n": role_n}
        elevated_thresh = 1.0 / p18k.PATH_B_AND_TIMING_SUPPRESS_RATIO
        # P27.2.6 -- real, live investigation on the original 2-node cluster found
        # this ratio-only check firing CONFIRMED/PAGE on 5/5 genuinely healthy TP2
        # runs (0 faults injected), root-caused to real peer-pool dispersion at
        # sub-millisecond scale (one live measurement: MAD 73% of the median) --
        # the ratio-only check can't distinguish "peer group is itself this noisy
        # right now" from "this member is genuinely elevated". Requiring the
        # absolute gap to also clear TIMING_FALLBACK_MAD_MULTIPLE peer MADs closes
        # this: the 8 real false positives found measured ~1.0-3.6 peer-MAD gaps,
        # while this fallback's own real validated fault (P27.2.4's docstring:
        # ~230x ratio, 24338us vs ~105us peer median) sits roughly two orders of
        # magnitude beyond that on the same MAD scale -- a real fault clears this
        # bar with enormous margin, pure noise does not. peer_mad of 0 (perfectly
        # uniform peers) makes this gate a no-op, same as the ratio check alone.
        # P27-hotfix4 -- reads EACH member's own d["peer_mad"]/d["peer_median"]
        # (its own role baseline, or the shared cross-comm-peer fallback --
        # see the per_member construction above), not one shared value for
        # both members -- required now that the two members can legitimately
        # have different baselines.
        elevated = [m for m, d in per_member.items() if d["ratio"] > elevated_thresh
                    and (d["peer_mad"] is None or d["peer_mad"] <= 0
                         or (d["current"] - d["peer_median"]) > TIMING_FALLBACK_MAD_MULTIPLE * d["peer_mad"])]
        # P27-hotfix6 -- push the self-exclusion marker for every elevated
        # member found THIS poll, before the ambiguous-count check below
        # (0 or 2+ elevated still returns None for firing purposes, but
        # an elevated reading is real evidence against belonging in
        # future "normal" history either way -- not gated on the 3-
        # sample persistence requirement that gates actually firing an
        # alert, since that would let 1-2 poisoning readings through
        # before the 3rd finally got excluded).
        for elevated_m in elevated:
            d = per_member[elevated_m]
            if d["role_rank"] not in (None, "na") and d["role_n"] not in (None, "na"):
                self._push_role_baseline_exclusion(d["hostname"], comm, elevated_m, bucket, coll,
                                                    d["role_rank"], d["role_n"])
        if len(elevated) != 1:
            return None
        waiting_partner = elevated[0]
        stragglers = [mid for mid in per_member if mid != waiting_partner]
        if len(stragglers) != 1:
            return None
        m = stragglers[0]
        surplus = per_member[waiting_partner]["ratio"] - elevated_thresh
        # P27-hotfix4 -- keyed on the flagged straggler's OWN real host
        # (per_member[m]["hostname"]), not the single `hostname` this
        # function was called with -- for a cross-node pair that's the
        # straggler's actual physical location, the real identity this
        # persistence key is meant to track.
        key = ("timing_fallback", per_member[m]["hostname"], comm, m, bucket, coll)
        fired = self.timing_fallback_tracker.observe(key, surplus, per_member[waiting_partner]["ts"])
        if not fired:
            return None
        return (m, per_member[m]["slot"], per_member)

    def _emit_timing_fallback(self, hostname, comm, member, slot, per_member, trigger,
                               path_c_storage=None, storage_verdict=None):
        """P27.2 -- dedicated formatter for _timing_asymmetry_fallback_
        evaluate's finding, same reasoning as _emit_dcgm_fallback: this
        evidence (a member's own current-vs-historical exec-time ratio)
        has no "evidence.statistic"/z/mm shape build_finding_for_alert
        expects, so it gets its own renderer rather than being forced
        through that path. Explicitly labeled as timing-sourced (not
        DCGM) so the two fallback evidence types are never confused when
        read side by side."""
        tier = "PROBABLE" if TIMING_FALLBACK_STOPGAP_ACTIVE else "CONFIRMED"
        severity, severity_reason = severity_for_tier(tier)
        d = per_member[member]
        # P27-hotfix4 -- each member's OWN real host (per_member[...]["hostname"],
        # set per-member in _timing_asymmetry_fallback_evaluate), not the single
        # `hostname` this function was called with -- that's just whichever
        # host's own per-host below-floor scan happened to discover this comm
        # this cycle, which for a cross-node pair like PP's is no more "the"
        # host of this finding than the other member's. Confirmed live this
        # session: PP's 2 members sit on two different physical nodes, so a
        # single hostname/"locally-reporting" framing would misdescribe the
        # topology outright, not just cosmetically.
        member_host = d["hostname"]
        other_lines = []
        for m, od in sorted(per_member.items()):
            if m == member:
                continue
            mad_note = (f", peer_mad_us={od['peer_mad']:.0f}, gap/mad="
                        f"{(od['current']-od['peer_median'])/od['peer_mad']:.2f}"
                        if od.get('peer_mad') else "")
            other_lines.append(
                f"  member={m} host={od['hostname']} slot={od['slot']} ratio={od['ratio']:.3f} "
                f"current_mean_us={od['current']:.0f} (peer_median_us={od['peer_median']:.0f}{mad_note})"
            )
        hosts = sorted({od["hostname"] for od in per_member.values()})
        topology_note = ("both members co-located on this host" if len(hosts) == 1
                          else f"members span {len(hosts)} hosts ({', '.join(hosts)}) -- a cross-node comm")
        lines = [
            f"[ALERT] rank={member} comm={comm} node={member_host} type=compute "
            f"confidence={tier} severity={severity}",
            "",
            f"P27.2 2-MEMBER TIMING-ASYMMETRY FALLBACK ({trigger}-triggered): comm={comm} "
            f"({topology_note}) has only {len(per_member)} real member(s) total -- below "
            f"SELF_DETECTION_FLOOR ({SELF_DETECTION_FLOOR}), so node_aggregator_ref.py's own "
            f"peer-relative mean/CV statistics structurally cannot compute here. This finding is "
            f"sourced from each member's OWN real baseline: PRIMARILY a per-ROLE cross-JOB "
            f"historical baseline (agg_mean_exec_time_us from OTHER completed jobs, same comm "
            f"shape/role -- see _member_role_baseline), falling back to an EXTERNAL cross-"
            f"communicator peer median (other currently-active same-shape below-floor comms on "
            f"that member's own host) only when no cross-job role history exists yet -- this "
            f"member used baseline_source={d['baseline_source']!r}. Scoped to this comm's own "
            f"largest real message-size bucket (bucket={d['bucket']} bytes, coll={d['coll']} -- "
            f"the dominant real-payload collective, not a trivial administrative one). "
            f"Deliberately NOT a same-member self-history comparison (P27.2.2's design): confirmed "
            f"live this session that self-history is vulnerable to this hardware's own real per-"
            f"collective timing spikes producing spurious suppression on completely healthy pairs; "
            f"an external baseline (role-based or cross-comm-peer) is immune to any single comm's "
            f"own internal noise by construction. Independent of both the self-detection floor and "
            f"of DCGM (which produces no signature at all for a software time.sleep() straggler -- "
            f"see _dcgm_fallback_evaluate for the DCGM-sourced half of this same fallback).",
            "",
            f"member={member} (real GPU slot {slot}, host={member_host}) is flagged as the "
            f"straggler because its ONLY real physical partner in this 2-member comm reads far "
            f"ABOVE that partner's own baseline (see below) -- P27.2.4's own direction: this "
            f"member's OWN reading looks statistically normal on its own (most members read low "
            f"most of the time), so the detectable evidence is its partner's elevation, not this "
            f"member's own value. The real straggler signature confirmed this sweep: the injected "
            f"target sleeps BEFORE entering the collective, so ITS OWN recorded exec time reads "
            f"short/normal, while its healthy partner (below) is the one left waiting and reads "
            f"far above its own baseline -- sustained for {T.PERSIST_REQUIRED} consecutive real "
            f"samples.",
        ]
        if other_lines:
            elevated_thresh = 1.0 / p18k.PATH_B_AND_TIMING_SUPPRESS_RATIO
            lines.append(f"Its partner in this comm, whose elevation (>{elevated_thresh:.3f}x its "
                          f"own baseline AND >{TIMING_FALLBACK_MAD_MULTIPLE} real baseline-pool "
                          "MADs above it -- P27.2.6) is the actual detected anomaly:")
            lines.extend(other_lines)
        lines.append("")
        # P27.3-followup -- real cause attribution for the flagged
        # straggler (member), reusing classifier.py's own Path C
        # (gather_path_c_storage + storage_evidence.determine_storage_path)
        # exactly as the above-floor live path already does -- this
        # fallback's own persistence gate already decided a real,
        # sustained asymmetry exists; this only answers WHY, never
        # changing whether/when the alert above fires.
        if path_c_storage is not None:
            if storage_verdict is True:
                lines.append(f"Cause (Path C, storage): CONFIRMED -- real eBPF block-I/O-wait for "
                              f"pid={member} on {member_host}: {path_c_storage.get('target_iowait_us', 0)}us "
                              f"aggregated over [{path_c_storage.get('t_start', 0):.1f},"
                              f"{path_c_storage.get('t_end', 0):.1f}] -- above the real threshold for a "
                              f"genuine storage stall.")
            elif storage_verdict is False:
                lines.append(f"Cause (Path C, storage): ruled out -- real eBPF block-I/O-wait for "
                              f"pid={member} on {member_host}: {path_c_storage.get('target_iowait_us', 0)}us "
                              f"aggregated over [{path_c_storage.get('t_start', 0):.1f},"
                              f"{path_c_storage.get('t_end', 0):.1f}] -- below the real threshold for a "
                              f"genuine storage stall. Data-pipeline (uneven shard sizes, a slow non-disk "
                              f"data source) remains the candidate this check cannot rule in or out.")
        else:
            lines.append("Cause (Path C, storage): not checked -- no persisted iowait log for this host "
                          "(agent not deployed/running there, or IOWAIT_LOG_DIR not configured).")
        lines.append("")
        lines.append(f"Severity: {severity} -- {severity_reason}")
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _emit_dcgm_fallback(self, hostname, comm, member, slot, per_member, trigger):
        """P21.6.1 -- dedicated formatter, same reasoning as _emit_host/
        _emit_network/_emit_nvlink: this finding's evidence is DCGM cause-
        evidence directly (path_b_clock), sourced independently of any
        NCCL peer-relative statistic -- format_alert()/build_finding_for_
        alert() assume an "evidence.statistic"/z/mm shape this doesn't
        have, so forcing it through that path would misrender rather than
        add real value.

        trigger is "cascade" (a larger related communicator just fired and
        _find_dependent_small_comms surfaced this comm as dependent) or
        "standalone" (this comm has no larger related communicator at all
        -- TP-inference's actual case -- found by this fallback's own
        periodic sweep, with no anomaly to ride along with)."""
        tier = "CONFIRMED"
        severity, severity_reason = severity_for_tier(tier)
        pb = per_member[member]["path_b_clock"] or {}
        other_lines = []
        for m, d in sorted(per_member.items()):
            if m == member:
                continue
            opb = d["path_b_clock"] or {}
            other_lines.append(
                f"  member={m} slot={d['slot']} tier={d['tier']} "
                f"sm_clock={opb.get('target_sm_clock')} power={opb.get('target_power')} "
                f"(peer_median_sm={opb.get('peer_median_sm_clock')}, peer_median_power={opb.get('peer_median_power')})"
            )
        lines = [
            f"[ALERT] rank={member} comm={comm} node={hostname} type=compute "
            f"confidence={tier} severity={severity}",
            "",
            f"P21.6.1 2-MEMBER DCGM FALLBACK ({trigger}-triggered): comm={comm} on {hostname} "
            f"has only {len(per_member)} locally-reporting member(s) -- below SELF_DETECTION_FLOOR "
            f"({SELF_DETECTION_FLOOR}), so node_aggregator_ref.py's own peer-relative mean/CV "
            f"statistics structurally cannot compute here (len(members) < 3 gate). This finding is "
            f"sourced directly from DCGM hardware telemetry via the real gpu_slot_index-targeted "
            f"physical GPU, independent of that gate.",
            "",
            f"member={member} (real GPU slot {slot}) Path B clock suppression, node-scoped: "
            f"sm_clock={pb.get('target_sm_clock')} vs peer_median_sm_clock={pb.get('peer_median_sm_clock')} "
            f"(<0.6x peer -> suppressed), power={pb.get('target_power')} vs "
            f"peer_median_power={pb.get('peer_median_power')} (genuinely_active="
            f"{pb.get('genuinely_active')}, gate=ACTIVE_POWER_PEER_FRAC={p18k.ACTIVE_POWER_PEER_FRAC}).",
        ]
        if other_lines:
            lines.append("Other locally-resolved member(s) of this same communicator, for comparison "
                          "(all show nominal Path B evidence -- this is what makes the finding above "
                          "asymmetric, not ambiguous):")
            lines.extend(other_lines)
        lines.append("")
        lines.append(f"Severity: {severity} -- {severity_reason}")
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _maybe_launch_dcgm_fallback_check(self):
        """P21.6.1 standalone path -- periodic, anomaly-independent sweep
        over every currently-discovered (hostname, comm) pair below
        SELF_DETECTION_FLOOR, for communicators with no larger related
        communicator to cascade an anomaly in from at all (TP-inference's
        real case: every communicator IS a 2-member TP pair, there is no
        DDP wrapper creating a larger comm alongside it, so the cascade
        path below in _emit() never gets a chance to run -- nothing ever
        fires on a larger comm to trigger it). Same out-of-band background-
        thread pattern as _maybe_launch_network_check/_nvlink/_host (must
        not block poll_once()'s 3s budget), same edge-triggered dedup via
        self.was_firing as _check_mean/_check_cv already use, so a
        sustained fault alerts once per onset, not once per 20s tick."""
        now = time.time()
        if now - self._last_dcgm_fallback_check_at < DCGM_FALLBACK_CHECK_INTERVAL_S:
            return
        if self._dcgm_fallback_thread is not None and self._dcgm_fallback_thread.is_alive():
            return
        self._last_dcgm_fallback_check_at = now
        # snapshot under the same data _poll_host iterates over -- (comm,
        # bucket, coll) triples collapse to distinct (hostname, comm) pairs
        # since this fallback doesn't care about bucket/coll at all (it's
        # DCGM-sourced, not NCCL-message-size-scoped).
        targets = []
        for hostname, pairs in self._comm_buckets.items():
            seen_comms = set()
            for comm, bucket, coll in pairs:
                if comm in seen_comms:
                    continue
                seen_comms.add(comm)
                if _comm_local_member_count(self.vm_url, hostname, comm) < SELF_DETECTION_FLOOR:
                    targets.append((hostname, comm))

        def _run():
            # P21.6.1 overhead fix -- one dcgmi call per DISTINCT host for
            # this whole sweep cycle, not one per (hostname, comm) target.
            # Measured live under a real TP_SIZE=4 job: 50 below-floor
            # targets on just 2 hosts took 9.8s before this cache (~195ms
            # x 50, each a redundant repeat of the SAME host's query);
            # with it, at most 2 real SSH round-trips total, independent
            # of how many small comms exist on a host.
            dcgm_cache = {}
            active_hosts = {h for h, _ in targets if _hosts_have_active_job([h])}
            for hostname, comm in targets:
                if hostname not in active_hosts:
                    continue
                result = self._dcgm_fallback_evaluate(hostname, comm, dcgm_cache=dcgm_cache)
                key = ("dcgm_fallback_standalone", hostname, comm)
                fired = result is not None
                if fired and not self.was_firing[key]:
                    member, slot, per_member = result
                    self._emit_dcgm_fallback(hostname, comm, member, slot, per_member, "standalone")
                self.was_firing[key] = fired
                # P27.2 -- timing-asymmetry half, tried independently of
                # the DCGM result above (not an else-branch): DCGM and
                # timing are two different evidence types that can each
                # resolve or stay silent on their own, exactly like Path
                # A/B/C never short-circuit each other in classifier.py's
                # determine_confirmed_path. Both get a real chance every
                # cycle.
                t_result = self._timing_asymmetry_fallback_evaluate(hostname, comm)
                t_key = ("timing_fallback_standalone", hostname, comm)
                t_fired = t_result is not None
                if t_fired and not self.was_firing[t_key]:
                    t_member, t_slot, t_per_member = t_result
                    # P27.3-followup -- real, GENERIC gap this closes: this
                    # fallback's own persistence gate (T.PERSIST_REQUIRED)
                    # already correctly decided a real, sustained below-
                    # floor asymmetry exists; until now it never asked WHY
                    # (no Path A/B/C cause-gathering at all, since this
                    # finding never goes through build_single_rank_finding
                    # -- confirmed this session, tracing every real caller
                    # of that function). Reuses classifier.py's own real
                    # gather_path_c_storage (the exact same code Path C
                    # already runs live, above floor) with this fallback's
                    # own already-resolved real identity (member IS the
                    # real jailed PID, member_host is that member's own
                    # real host) -- no new identity resolution, no change
                    # to the persistence gate above, this only adds cause
                    # attribution to a finding that already fired.
                    t_member_host = t_per_member[t_member]["hostname"]
                    _, _, t_path_c = p18k.gather_path_c_storage(t_member_host, t_member, IOWAIT_LOG_DIR)
                    t_storage_verdict = storage_evidence.determine_storage_path(t_path_c) if t_path_c else None
                    self._emit_timing_fallback(hostname, comm, t_member, t_slot, t_per_member, "standalone",
                                                path_c_storage=t_path_c, storage_verdict=t_storage_verdict)
                    # P27-hotfix9 -- cross-comm correlation enrichment,
                    # pure add-on: this alert is ALREADY emitted above,
                    # unconditionally, before any of this runs. Checked
                    # against the ROLLING RECENT window (not just this
                    # cycle -- see RECENT_TIMING_CORRELATION_WINDOW_S's
                    # own comment for why), so a cycle with 0 or 1 recent
                    # firings (every single-comm workload, always) makes
                    # the correlation call a no-op by construction (its
                    # own len(pool)<2 guard) -- provably inert there.
                    now = time.time()
                    self._recent_timing_fires = [a for a in self._recent_timing_fires
                                                  if now - a["fired_at"] <= RECENT_TIMING_CORRELATION_WINDOW_S]
                    new_alert = {"hostname": hostname, "comm": comm, "member": t_member,
                                 "slot": t_slot, "per_member": t_per_member, "fired_at": now}
                    pool = self._recent_timing_fires + [new_alert]
                    if len(pool) >= 2:
                        annotations = self._correlate_firing_timing_alerts(pool)
                        self._emit_correlation_report(pool, annotations)
                    self._recent_timing_fires.append(new_alert)
                self.was_firing[t_key] = t_fired

        self._dcgm_fallback_thread = threading.Thread(target=_run, daemon=True)
        self._dcgm_fallback_thread.start()

    def _emit(self, stat_name, hostname, comm, member, bucket, coll, z, mm, worst_val, peer_mean, slurm_job_id, anomaly_ts=None):
        # P22.2 -- coverage_guard.py and build_finding_for_alert's own
        # `primary=` display field are both out of this session's scope
        # (see this session's own report: coverage_guard's own volume
        # check still pools coll types at a shared bucket value, a real,
        # smaller, separately-named gap, not fixed here) -- bucket is
        # passed through unchanged, coll is not threaded into either.
        coverage = coverage_guard.check_coverage(self.vm_url, hostname, comm, bucket, member, slurm_job_id)
        finding = build_finding_for_alert(self.vm_url, hostname, comm, member, bucket, stat_name, z, mm, worst_val, peer_mean,
                                           self.dcgm_host_map, anomaly_ts=anomaly_ts)
        if coverage["degraded"] and finding["tier"] == "CONFIRMED":
            finding["tier"] = "PROBABLE"

        # V1-beta-dashboard-followup -- real visibility only: the SAME
        # already-computed Path C evidence/verdict this finding's own
        # rendered alert text discloses (or, for PROBABLE/UNCONFIRMED,
        # never surfaces at all -- report.py's own _format_probable
        # doesn't render path_c_storage even when it was checked), now
        # ALSO pushed as a real metric so it's queryable/graphable
        # without reading raw alert text. 1=confirmed, 0=checked and
        # ruled out, -1=genuinely not checked (no iowait log for this
        # host, or no real PID identity) -- storage_evidence.determine_
        # storage_path's own real three-way return, not a new verdict.
        pc = finding["cause"].get("path_c_storage")
        verdict = storage_evidence.determine_storage_path(pc) if pc else None
        verdict_num = 1 if verdict is True else (0 if verdict is False else -1)
        self._push_visibility_metric(
            f'agg_path_c_verdict{{hostname="{hostname}",member="{member}",comm="{comm}",bucket="{bucket}"}} {verdict_num}')

        # P21.6 -- cascade-mislocalization fix. An alert sourced from a
        # larger communicator has no way, by itself, to know whether the
        # anomaly it sees originated within that communicator or cascaded
        # in from a dependent, smaller communicator that structurally
        # can't self-detect (confirmed real and dangerous in P21.5: a
        # GPU4/worker-0 fault produced an alert naming worker-1, the
        # healthy node). Real "pick a single winner" localization was
        # tried and found unreliable (see _rank_dependents_by_deviation's
        # own docstring -- it gets outcompeted by rank 0's already-known,
        # un-excludable overhead bias). This alert therefore always
        # discloses the uncertainty (Step 1) and, when computable, shows
        # ranked real evidence (Step 2's salvageable half) -- but never
        # claims a single confirmed re-attribution.
        dependents = self._find_dependent_small_comms(hostname, comm)
        if dependents:
            # P21.6.1 -- cascade-triggered half of the 2-member DCGM
            # fallback (see _dcgm_fallback_evaluate's own docstring for the
            # shared core this converges on with the standalone periodic
            # path below). This alert firing on a LARGER communicator is
            # itself the trigger: rather than wait for a slower periodic
            # sweep, check DCGM for every dependent small comm's real
            # physical members RIGHT NOW, while we already know something
            # is wrong somewhere nearby. When it resolves to a real,
            # asymmetric answer, this both (a) emits its own independent
            # CONFIRMED finding naming the real physical GPU (real hardware
            # evidence, not a ranked guess) and (b) upgrades the disclosure
            # note below from "uncertain" to "resolved" for that specific
            # dependent -- other, unresolved dependents still get the
            # honest "uncertain" treatment, unchanged.
            resolved_deps = {}
            for (h, dep_comm) in dependents:
                key = ("dcgm_fallback_cascade", h, dep_comm)
                result = self._dcgm_fallback_evaluate(h, dep_comm)
                fired = result is not None
                if fired and not self.was_firing[key]:
                    dep_member, dep_slot, dep_per_member = result
                    self._emit_dcgm_fallback(h, dep_comm, dep_member, dep_slot, dep_per_member, "cascade")
                    resolved_deps[(h, dep_comm)] = (dep_member, dep_slot, "DCGM")
                self.was_firing[key] = fired
                # P27.2 -- timing-asymmetry half, tried independently
                # (not an elif) so it still gets a real chance even when
                # DCGM already resolved this dependent this cycle -- both
                # trackers must keep observing every cycle regardless, or
                # a later cycle where DCGM stops resolving would start
                # timing's persistence count from cold at the worst time.
                # Display only prefers whichever resolved first below.
                t_key = ("timing_fallback_cascade", h, dep_comm)
                t_result = self._timing_asymmetry_fallback_evaluate(h, dep_comm)
                t_fired = t_result is not None
                if t_fired and not self.was_firing[t_key]:
                    t_member, t_slot, t_per_member = t_result
                    # P27.3-followup, Part A -- same wiring as the standalone
                    # trigger site, applied here for symmetry: this call site
                    # was deliberately left unmodified in that earlier session
                    # pending its own dedicated validation. Same helper, same
                    # already-resolved real identity, no new logic.
                    t_member_host = t_per_member[t_member]["hostname"]
                    _, _, t_path_c = p18k.gather_path_c_storage(t_member_host, t_member, IOWAIT_LOG_DIR)
                    t_storage_verdict = storage_evidence.determine_storage_path(t_path_c) if t_path_c else None
                    self._emit_timing_fallback(h, dep_comm, t_member, t_slot, t_per_member, "cascade",
                                                path_c_storage=t_path_c, storage_verdict=t_storage_verdict)
                    resolved_deps.setdefault((h, dep_comm), (t_member, t_slot, "TIMING"))
                self.was_firing[t_key] = t_fired

            ranked = self._rank_dependents_by_deviation(dependents)
            dep_list = ", ".join(
                (f"comm={c} hostname={h} ({len(ms)} shared member(s)) "
                 f"[{resolved_deps[(h, c)][2]}-RESOLVED this cycle: member={resolved_deps[(h, c)][0]} "
                 f"real GPU slot={resolved_deps[(h, c)][1]} -- see its own CONFIRMED alert above]"
                 if (h, c) in resolved_deps else
                 f"comm={c} hostname={h} ({len(ms)} shared member(s))")
                for (h, c), ms in dependents.items()
            )
            note = (
                f"LOCATION UNCERTAIN: this alert's own communicator (comm={comm}, "
                f"hostname={hostname}) shares physical members with dependent "
                f"communicator(s) below the self-detection floor "
                f"(< {SELF_DETECTION_FLOOR} members) that cannot report their own "
                f"peer-relative anomalies: {dep_list}. If the true fault originates "
                f"in one of those smaller communicators, this alert's stated location "
                f"may NOT be the fault's actual physical location -- treat "
                f"hostname={hostname}/member={member} as provisional, not confirmed."
            )
            if ranked:
                top = ranked[:3]
                evidence_lines = "\n".join(
                    f"    {i+1}. comm={c} hostname={h} member={m} (internal deviation {r:.2f}x)"
                    for i, (h, c, m, r) in enumerate(top)
                )
                note += (
                    f"\n  Reference evidence only, NOT a confirmed answer (real "
                    f"internal deviation ratio per dependent, largest first -- "
                    f"confirmed directly this session that the single largest ratio "
                    f"is not reliably the true origin: a persistent, non-fault bias "
                    f"in one member can outrank a real, smaller fault signal in "
                    f"another):\n{evidence_lines}"
                )
            finding["cascade_note"] = note

        text = format_alert(finding, coverage)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _emit_network(self, net_check):
        """Separate from _emit()/format_alert() deliberately -- a network
        finding is cluster-wide, not single-rank/single-host, and has no
        "rank" key at all (build_global_drift_finding's own shape).
        format_alert()/build_finding_for_alert() assume a rank-scoped
        finding throughout (coverage_guard.check_coverage() takes a rank+
        bucket, report.py's _format_confirmed reads finding['cause']
        ['class1'], neither of which a network finding has). Forcing this
        through that path would either crash or silently misrender; a
        dedicated formatter is the zero-regression-risk choice for the
        already-validated rank-based path."""
        net, err, used_buffer = net_check
        tier = net["tier"]
        severity, severity_reason = severity_for_tier(tier)
        lines = [f"[ALERT] scope=cluster type=network confidence={tier} severity={severity}", ""]
        lines.append(f"IB evidence across {sorted(net['net'].keys())} (live_query, "
                      f"no rolling buffer in this alert engine):")
        for h, d in sorted(net["net"].items()):
            lines.append(f"  {h}: participation={d['participation_frac']:.2f} "
                          f"({d['active_devices']}/{d['total_devices']} devices active), "
                          f"xmit_rate={d['xmit_rate_bytes_s']:.0f} B/s, "
                          f"errors_delta={d['errors_delta']}, congestion_delta={d['congestion_delta']}")
        lines.append("")
        lines.append(p18k.NETWORK_ATTRIBUTION_CAVEAT)
        lines.append("")
        lines.append(NETWORK_MASKS_COMPUTE_CAVEAT)
        lines.append(f"\nSeverity: {severity} -- {severity_reason}")
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _emit_nvlink(self, net_check):
        """P25 Part 1 -- NVLink counterpart to _emit_network, same
        reasoning: a cluster-wide finding with no "rank" key, so it needs
        its own formatter rather than going through format_alert()'s
        rank-scoped path. `type=nvlink` in the header line is deliberate
        and load-bearing -- it's what makes this distinguishable from an
        IB-based `type=network` finding downstream (dashboards, log
        scraping, this project's own report.py conventions all key off
        that field)."""
        net, err, used_buffer = net_check
        tier = net["tier"]
        severity, severity_reason = severity_for_tier(tier)
        lines = [f"[ALERT] scope=cluster type=nvlink confidence={tier} severity={severity}", ""]
        lines.append(f"NVLink evidence across {sorted(net['net'].keys())} (live_query, "
                      f"no rolling buffer for NVLink yet):")
        for h, d in sorted(net["net"].items()):
            lines.append(f"  {h}: participation={d['participation_frac']:.2f} "
                          f"({d['active_devices']}/{d['total_devices']} links up), "
                          f"xmit_rate={d['xmit_rate_bytes_s']:.0f} B/s, "
                          f"errors_delta={d['errors_delta']} (CRC FLIT+Data/Replay/Recovery, summed)")
        lines.append("")
        lines.append("NVLink is intra-node only and this project's jobs always allocate a whole node "
                      "-- unlike IB's cluster-shared fabric, this reading has no cross-tenant "
                      "attribution ambiguity.")
        lines.append(f"\nSeverity: {severity} -- {severity_reason}")
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _emit_host(self, finding):
        """Same reasoning as _emit_network -- check_host_contention_direct's
        finding shape (host/type_candidate="host"/evidence.ratio) doesn't
        match the rank-scoped format_alert() path either; dedicated
        formatter, zero risk to the existing rank-based rendering."""
        tier = finding["tier"]
        severity, severity_reason = severity_for_tier(tier)
        node = finding["host"]
        ev = finding["evidence"]
        source = finding["cause"]["path_c_host"]["source"]
        lines = [
            f"[ALERT] node={node} type=host confidence={tier} severity={severity}", "",
            f"Host CPU load ratio {ev['ratio']:.2f}x other node(s) "
            f"({node}={ev['affected_load_per_core']:.3f} load/core, "
            f"other={ev['other_load_per_core']:.3f} load/core, source={source}).",
            "",
            f"Severity: {severity} -- {severity_reason}",
        ]
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _maybe_launch_network_check(self):
        now = time.time()
        if now - self._last_network_check_at < NETWORK_CHECK_INTERVAL_S:
            return
        if self._network_thread is not None and self._network_thread.is_alive():
            return  # previous check still running (its own ~20s cost) -- don't pile up
        self._last_network_check_at = now
        hosts = list(self.hostnames)

        def _run():
            if len(hosts) < 2 or not _hosts_have_active_job(hosts):
                return
            net_check = p18k.check_network_contention_direct(None, hosts, None)
            net, err, used_buffer = net_check
            if net is not None:
                self._emit_network(net_check)
            elif err:
                # P27-hotfix-stall -- err used to be captured and silently
                # discarded here: a genuinely degraded/timed-out check
                # (or, before the fix, one still mid-stall) read
                # indistinguishably from "checked, found nothing" -- the
                # same class of silent gap [PIPELINE-DOWN]/[DCGM-
                # HOSTENGINE-DOWN] already exist to close elsewhere in
                # this file. Loud and unconditional now, same discipline.
                print(f"[NETWORK-CHECK-DEGRADED] {err}", file=sys.stderr, flush=True)

        self._network_thread = threading.Thread(target=_run, daemon=True)
        self._network_thread.start()

    def _maybe_launch_nvlink_check(self):
        """P25 Part 1 -- same out-of-band pattern as _maybe_launch_
        network_check: real measured cost is dominated by per-GPU SSH
        round trips, so this must not block poll_once()'s 3s loop."""
        now = time.time()
        if now - self._last_nvlink_check_at < NVLINK_CHECK_INTERVAL_S:
            return
        if self._nvlink_thread is not None and self._nvlink_thread.is_alive():
            return  # previous check still running -- don't pile up
        self._last_nvlink_check_at = now
        hosts = list(self.hostnames)

        def _run():
            if len(hosts) < 2 or not _hosts_have_active_job(hosts):
                return
            net_check = p18k.check_nvlink_contention_direct(None, hosts, None)
            net, err, used_buffer = net_check
            if net is not None:
                self._emit_nvlink(net_check)
            elif err:
                # P27-hotfix-stall -- see _maybe_launch_network_check's
                # identical fix for why this can't stay silent.
                print(f"[NVLINK-CHECK-DEGRADED] {err}", file=sys.stderr, flush=True)

        self._nvlink_thread = threading.Thread(target=_run, daemon=True)
        self._nvlink_thread.start()

    def _maybe_launch_host_check(self):
        now = time.time()
        if now - self._last_host_check_at < HOST_CHECK_INTERVAL_S:
            return
        if self._host_thread is not None and self._host_thread.is_alive():
            return
        self._last_host_check_at = now
        hosts = list(self.hostnames)

        def _run():
            if len(hosts) < 2 or not _hosts_have_active_job(hosts):
                return
            # live_host_load_ratios (not check_host_contention_direct)
            # deliberately -- it returns EVERY host's raw ratio, not just
            # ones already over threshold, so a host that drops back
            # under threshold still gets a real "not fired" tick fed to
            # the tracker. Skipping that tick (as using the pre-filtered
            # finding list would) would let two separate above-threshold
            # episodes silently merge into a false "3 consecutive".
            ratios = p18k.live_host_load_ratios(hosts)
            now_ts = time.time()
            for node, r in ratios.items():
                # V1-beta-dashboard-followup -- real visibility only:
                # this ratio was already computed above for every real
                # host every cycle (not just ones over threshold, see
                # this function's own comment on why); pushing it here
                # is the SAME already-computed number, not a new check.
                self._push_visibility_metric(
                    f'agg_host_load_ratio{{hostname="{node}"}} {r["ratio"]}')
                if self.host_tracker.observe(node, r["ratio"], now_ts):
                    finding = {
                        "host": node, "type_candidate": "host", "timescale": "sustained/whole-node",
                        "evidence": {"affected_load_per_core": r["load"],
                                     "other_load_per_core": r["other_mean"], "ratio": r["ratio"]},
                        "cause": {"checked": [f"live query_host_cpu, {node} vs other node(s), "
                                               f"{T.PERSIST_REQUIRED} consecutive snapshots"],
                                  "impossible": [], "path_c_host": {"ratio": r["ratio"], "source": "live_query"}},
                        "tier": "CONFIRMED",
                    }
                    self._emit_host(finding)

        self._host_thread = threading.Thread(target=_run, daemon=True)
        self._host_thread.start()

    def _maybe_launch_dcgm_hostengine_check(self):
        """P27-hotfix2 -- nv-hostengine dead-man's-switch, same discipline
        as _check_pipeline_health_one's [PIPELINE-DOWN]: found this session
        that nv-hostengine can be down on a host with zero loud signal
        anywhere -- every DCGM-sourced cause-check on that host just
        silently degrades from "confirmed no thermal cause" to "couldn't
        check", indistinguishable from a real ambiguous DCGM read unless
        someone reads the per-alert text closely. This makes that
        distinction loud and unconditional, the same way pipeline_health
        made "no fresh data in VM" loud instead of silently reading as "a
        genuinely healthy zero-alert run".

        Threaded and interval-gated (DCGM_FALLBACK_CHECK_INTERVAL_S, this
        file's own existing DCGM-check cadence, reused rather than a new
        constant) for the same reason network/host/nvlink checks are:
        each host costs a real SSH round-trip, must not run on the 3s
        poll budget. Fully generic -- iterates self.hostnames exactly as
        discovered from live data, no hardcoded assumption about which
        node(s) should be running nv-hostengine."""
        now = time.time()
        if now - self._last_dcgm_hostengine_check_at < DCGM_FALLBACK_CHECK_INTERVAL_S:
            return
        if self._dcgm_hostengine_thread is not None and self._dcgm_hostengine_thread.is_alive():
            return
        self._last_dcgm_hostengine_check_at = now
        hosts = list(self.hostnames)

        def _run():
            for host in hosts:
                alive, reason = p18k.check_nv_hostengine_alive(host)
                # V1-beta-dashboard-followup -- real visibility only:
                # this is the SAME already-computed `alive` value the
                # loud [DCGM-HOSTENGINE-DOWN]/-RECOVERED text below
                # already reacts to; pushed every real cycle (not just
                # on state transitions) so the panel shows a continuous
                # real timeline, not just edge events.
                self._push_visibility_metric(
                    f'agg_nvhostengine_alive{{hostname="{host}"}} {1 if alive else 0}')
                was_down = self.dcgm_hostengine_down.get(host, False)
                if not alive:
                    self.n_dcgm_hostengine_down_cycles += 1
                    self.dcgm_hostengine_down[host] = True
                    print(f"[DCGM-HOSTENGINE-DOWN] hostname={host} :: {reason} -- every DCGM-sourced "
                          f"cause-check on this host is degraded to 'couldn't check' until this recovers",
                          file=sys.stderr, flush=True)
                elif was_down:
                    self.dcgm_hostengine_down[host] = False
                    print(f"[DCGM-HOSTENGINE-RECOVERED] hostname={host}", file=sys.stderr, flush=True)
                # else: alive and wasn't down -- no signal, matching pipeline_health's
                # own "a genuinely healthy read stays silent" discipline.

        self._dcgm_hostengine_thread = threading.Thread(target=_run, daemon=True)
        self._dcgm_hostengine_thread.start()

    def _fresh(self, result_row):
        """agg_*_worst metrics are labeled rank="{worst}" -- the rank that
        was worst AT THAT WINDOW. Over a job's life this accumulates one
        DISTINCT series per rank that has ever been worst at least once,
        not one series with a changing rank value (a real bug found and
        fixed during this session's own live validation: naively taking
        result[0] silently read whichever rank's series happened to sort
        first, missing the actual current fault entirely). A series
        belongs to the CURRENT moment only if its own sample timestamp is
        recent -- a rank that hasn't been "worst" again since some time
        ago keeps a frozen last value forever and must not be replayed as
        if it were still happening now.

        Threshold: FRESH_THRESH_S (see module constant below), not the
        original max(10.0, poll_interval*3) -- that 10s figure was NEVER
        actually enforced before this session's fix (result_row["value"][0]
        was always ~query-eval-time, i.e. always "fresh" regardless of
        true age, the exact bug this session closes). Once value[0]
        genuinely reflects real sample age, 10s turned out to be far too
        tight to ever pass: measured directly, under a real 300ms-sleep
        fault (which itself reduces the job's real sample-generation rate
        to ~3.1-3.2/sec, same throughput collapse characterized in the
        uniform-slowdown work), mean's own window-close cadence slows to
        roughly once per 25-30s, and the freshest that sample can ever
        report once visible is 35.6-59.6s old -- on top of this exact VM
        instance's own already-measured ~30-39s baseline visibility
        floor (pipeline_health.py's HEARTBEAT_STALE_THRESH_S=90.0,
        measured the same way). A 10s bar is unsatisfiable here even in
        the healthy case, let alone under the fault conditions an alert
        exists to catch. Reusing 90.0 unchanged (not inventing a new
        number) -- comfortably above the measured ~60s worst case with
        real margin, and already validated as this VM instance's correct
        order of magnitude for a different metric on the same instance."""
        ts = float(result_row["value"][0])
        return (time.time() - ts) < max(FRESH_THRESH_S, self.poll_interval * 3)

    def _cv_maxmed(self, hostname, comm, bucket, coll, worst_member):
        """node_aggregator.py never pushes a maxmed value for CV (its
        firing gate doesn't need one -- z alone is the CV gate, per
        classifier.py's own comment: 'mm has almost no margin here...
        gate primarily on z'). But report.py's CONFIRMED/PROBABLE
        formatter expects one to display. Rather than fabricate a value,
        compute the real thing from agg_cv_exec_time (which IS pushed for
        every member, every window) using the exact same maxmed() formula
        node_aggregator.py itself uses (worst / median(peers)).

        P21.5 -- scoped by comm now, not just bucket: two different
        communicators could in principle report the same bucket byte
        value (their message-size spaces are independently discovered,
        nothing prevents an incidental collision), so both the query and
        the peer set must stay within the SAME communicator's own
        membership -- exactly the fix for the confirmed real bug where a
        bare rank number meant different physical GPUs depending on which
        communicator's record it came from.

        P22.2 -- also scoped by coll now, same reasoning: two collective
        types can legitimately share a bucket value on the SAME
        communicator too (P22.1's own fix made this real), so without a
        coll filter this would pool AllGather's and ReduceScatter's own
        independent exec-time populations into one (wrong) peer set."""
        res = _query_instant_real_ts(self.vm_url, f'agg_cv_exec_time{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        by_member = {}
        for row in res:
            if not self._fresh(row):
                continue
            by_member[row["metric"]["member"]] = float(row["value"][1])
        if worst_member not in by_member or len(by_member) < 2:
            return None, None
        worst_val = by_member[worst_member]
        peers = [v for p, v in by_member.items() if p != worst_member]
        peer_median = sorted(peers)[len(peers) // 2] if len(peers) % 2 else \
            (sorted(peers)[len(peers) // 2 - 1] + sorted(peers)[len(peers) // 2]) / 2
        mm = worst_val / peer_median if peer_median else float("inf")
        return mm, worst_val

    def _check_cv(self, hostname, comm, bucket, coll):
        # P22.2 -- coll filter added: without it this query would match
        # every collective type sharing this bucket value on this
        # communicator at once (confirmed real and live in P22.1's FSDP
        # test -- AllGather and ReduceScatter both at bucket=442560).
        res = _query_instant_real_ts(self.vm_url, f'agg_cv_z_worst{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        for row in res:
            if not self._fresh(row):
                continue
            m = row["metric"]
            z = float(row["value"][1])
            ts = float(row["value"][0])
            member = m["member"]
            # P22.2 -- coll added to the persistence key: a persistence
            # window for one collective type must never be satisfied by
            # samples from another, the same principle already applied
            # to member/bucket -- two collective types on the same
            # member/bucket are two independent series, not one.
            key = (hostname, comm, member, bucket, coll)
            if self.cv_tracker.observe(key, z, ts):
                mm, worst_val = self._cv_maxmed(hostname, comm, bucket, coll, member)
                self._emit("cv", hostname, comm, member, bucket, coll, z, mm, worst_val, None, m.get("slurm_job_id"), anomaly_ts=ts)

    def _check_mean(self, hostname, comm, bucket, coll):
        # P22.2 -- coll filter added to both queries, same reasoning as
        # _check_cv. Since each call is now scoped to one specific
        # (bucket, coll) pair (via _poll_host's own iteration over
        # _discover_buckets' now-paired results), z_res/mm_res can only
        # ever contain rows for THIS coll -- but mm_by_member is still
        # keyed by (member, coll), not member alone, as explicit defense-
        # in-depth: if this function is ever called with multiple coll
        # rows present again (e.g. the coll filter is accidentally
        # dropped in a future edit), a member-only key would silently
        # reintroduce exactly this session's own bug instead of failing
        # loudly.
        #
        # P27.4 -- real poll-timing race, confirmed live via FSDP fault
        # test #8: this check used to read agg_mean_z_worst/agg_mean_mm_
        # worst as bare INSTANT queries, which only ever return VM's
        # CURRENT latest sample for a series. node_aggregator_ref.py
        # closes a new mean-window (and pushes a new agg_mean_fired/z/mm
        # sample) on its OWN cadence, entirely independent of this
        # engine's poll_interval -- when that cadence is faster than the
        # poll interval, more than one real window-close event can land
        # between two polls, and an instant query only ever sees the LAST
        # one, silently overwriting an earlier genuine firing before this
        # engine ever observes it. Confirmed exactly this in test #8: the
        # true injected rank's own agg_mean_fired flipped to 1 with
        # z=149.93 (a STRONGER signal than the unrelated rank that
        # happened to get flagged instead), but the old bare instant
        # check never caught it -- pure poll-alignment luck, not a real
        # detection gap.
        #
        # Fixed by widening the query to max_over_time(...) over a real
        # bridging window. First tried lookback_s = poll_interval * 3
        # (matching _fresh()'s OLD, since-abandoned scaling) -- confirmed
        # live this is NOT wide enough: a direct VM query showed
        # max_over_time(agg_mean_fired{...}[10s]) came back EMPTY at a
        # timestamp where the SAME query with [20s] (and a bare instant
        # query) correctly returned 1. The relevant cadence to bridge
        # isn't this engine's own poll_interval at all -- it's node_
        # aggregator_ref.py's independent window-close/push cadence,
        # which this file has no direct visibility into and which
        # apparently jitters past 10s in practice. FRESH_THRESH_S (see
        # its own docstring/history above: poll_interval*3 was already
        # tried and found "far too tight" for this exact same class of
        # real push-cadence gap, for the identical reason) is this
        # project's own already-measured, already-correct scale for
        # exactly this uncertainty -- reused directly rather than
        # re-guessing a second workload-agnostic constant. agg_mean_fired
        # is node_aggregator's own pre-combined z-AND-mm decision (both
        # computed from the exact same window-close event) -- reused
        # directly here rather than separately max_over_time-ing z and mm
        # on their own, which could silently AND together two DIFFERENT
        # moments' peaks into a spurious, uncorroborated firing. z_res/
        # mm_res are still queried as before, but now purely for the
        # alert's own reported numbers (best-effort "worst seen
        # recently"), never as the fire condition itself.
        #
        # P27.4 real bug #2, found live via targeted debug tracing after
        # the lookback widening ALONE still produced zero alerts against a
        # confirmed, continuously-firing real signal (94 consecutive
        # agg_mean_fired=1 samples spanning ~186s): _query_instant_real_ts
        # correlates a value query against a SEPARATE timestamp(<same
        # query>) query by label set, and silently drops any row with no
        # match (by design, for the plain-vector-selector case this
        # function was written for). Confirmed directly against live VM
        # data that timestamp(max_over_time(X[90s])) returns EMPTY even
        # while max_over_time(X[90s]) itself returns a real value at the
        # identical timestamp -- MetricsQL's timestamp() does not compose
        # with an *_over_time aggregation the way it does with a plain
        # selector, so every fired_res row was being silently discarded
        # before ever reaching the fired-check below. A max_over_time(...)
        # result is a synthetic value anchored to the QUERY's own eval
        # time, not to one specific underlying raw sample, so there is no
        # meaningful "real sample timestamp" to recover here in the first
        # place -- plain _query_instant (eval time itself) is the correct,
        # honest timestamp for this specific query, not a workaround.
        lookback_s = FRESH_THRESH_S
        fired_promql = (f'max_over_time(agg_mean_fired{{hostname="{hostname}",comm="{comm}",'
                         f'bucket="{bucket}",coll="{coll}"}}[{int(lookback_s)}s])')
        fired_res = _query_instant(self.vm_url, fired_promql)
        z_res = _query_instant_real_ts(self.vm_url, f'agg_mean_z_worst{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        mm_res = _query_instant_real_ts(self.vm_url, f'agg_mean_mm_worst{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        mm_by_member = {(r["metric"]["member"], r["metric"].get("coll")): float(r["value"][1])
                        for r in mm_res if self._fresh(r)}
        z_by_member = {(r["metric"]["member"], r["metric"].get("coll")): float(r["value"][1])
                       for r in z_res if self._fresh(r)}
        # No _fresh() gate here: fired_res's own value[0] is _query_instant's
        # query-eval timestamp (always "now", by construction of a plain
        # instant query with no explicit time= param) -- it can never be
        # stale, so a staleness check on it would be checking nothing.
        # The real staleness guarantee comes from the query itself: the
        # bounded max_over_time([lookback_s]) window is what limits how
        # far back a real firing sample can be and still count.
        for row in fired_res:
            m = row["metric"]
            member = m["member"]
            row_coll = m.get("coll")
            fired = float(row["value"][1]) >= 1.0
            # P22.2 -- coll added to the persistence key, same reasoning
            # as _check_cv's own key.
            key = ("mean", hostname, comm, member, bucket, coll)
            if fired and not self.was_firing[key]:
                z = z_by_member.get((member, row_coll), 0.0)
                mm = mm_by_member.get((member, row_coll), 0.0)
                # P27.3-timing-gap investigation -- real, explicit trace of
                # the gap between "the underlying anomaly's own real sample
                # timestamp" (z_res's real ts, via _query_instant_real_ts,
                # already-existing infrastructure -- not a new mechanism)
                # and "the real moment this engine actually decides to act
                # on it". This is the direct measurement Path C's own live
                # window (IOWAIT_LIVE_WINDOW_S) gets evaluated against.
                z_row_ts = next((float(r["value"][0]) for r in z_res
                                  if r["metric"].get("member") == member and r["metric"].get("coll") == row_coll), None)
                now_ts = time.time()
                age_s = (now_ts - z_row_ts) if z_row_ts is not None else None
                print(f"[EMIT_TRACE] decision_time={now_ts:.3f} member={member} bucket={bucket} coll={row_coll} "
                      f"z_sample_real_ts={z_row_ts} age_s={age_s}", flush=True)
                self._emit("mean", hostname, comm, member, bucket, coll, z, mm, None, None, m.get("slurm_job_id"), anomaly_ts=z_row_ts)
            self.was_firing[key] = fired

    def _check_outlier_count(self, hostname, comm, bucket, coll):
        # P22.2 -- coll filter added, same reasoning as _check_cv/_check_mean.
        mm_res = _query_instant_real_ts(self.vm_url, f'agg_outlier_count_mm_worst{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        fired_res = _query_instant_real_ts(self.vm_url, f'agg_outlier_count_fired{{hostname="{hostname}",comm="{comm}",bucket="{bucket}",coll="{coll}"}}')
        fired_by_member = {(r["metric"]["member"], r["metric"].get("coll")): float(r["value"][1]) == 1.0
                           for r in fired_res if self._fresh(r)}
        for row in mm_res:
            if not self._fresh(row):
                continue
            m = row["metric"]
            mm = float(row["value"][1])
            member = m["member"]
            row_coll = m.get("coll")
            fired_flag = fired_by_member.get((member, row_coll), False)
            # P22.2 -- coll added to the tracking key, same reasoning as
            # the other two checks' persistence/tracking keys.
            key = ("outlier_count", hostname, comm, member, bucket, coll)
            # outlier_count is corroborating-only (known nonzero healthy
            # rate, thresholds.py) -- never emitted as its own top-level
            # alert, only ever attached as additional_signatures when it
            # co-fires alongside cv/mean inside build_single_rank_finding.
            # State tracked here for visibility/audit, not dispatched alone.
            self.was_firing[key] = fired_flag

    def _check_job_throughput(self, hostname):
        """P20k-closeout Part B -- the one absolute, non-peer-relative
        check in this pipeline. Gated on 3-of-3 persistence (same
        convention as CV/host) via self.throughput_tracker.

        P23 step 3 -- was: query agg_job_throughput_per_rank_bucket_
        per_sec (normalized by live bucket-cardinality) and compare
        against coverage_guard.CALIBRATED_RATE_PER_SEC * ABSOLUTE_RATE_
        FLOOR_FRAC, a constant measured against small-bucket-count shapes
        (DDP/TP/FSDP, ~5-88 real buckets). Confirmed live this doesn't
        generalize: a real MoE workload with ~202 real buckets spreads
        the SAME healthy total event rate across far more buckets,
        making the per-bucket rate structurally, permanently lower for a
        completely healthy job -- CALIBRATED_RATE_PER_SEC was never a
        valid floor for that, no matter how stable the bucket count is.
        node_aggregator_ref.py now pushes agg_job_throughput_ratio_to_
        baseline instead: a plain ratio to THIS job's own self-
        calibrated healthy rate (established once its bucket count has
        stabilized -- see its own maybe_check_job_throughput comment),
        not an absolute rate at all. ABSOLUTE_RATE_FLOOR_FRAC (already
        existing, reused unchanged) is now directly the ratio floor --
        no cross-job, cross-shape constant is needed for this check.

        P22.5 -- additionally gated on this host's OWN currently-known
        pipeline-health state (self.pipeline_down, checked THIS SAME
        cycle by _check_pipeline_health() before _poll_host() dispatches
        here). A genuinely down/degraded pipeline (heartbeat itself
        stale) means ANY reading from this host is unreliable regardless,
        and P22.4 confirmed live that exactly this happened: 2 CONFIRMED/
        PAGE "uniform_slowdown" alerts fired during a period this SAME
        pipeline-health check was already printing [PIPELINE-DOWN] for.
        Deferring to the pipeline-health path here (already correct,
        already wall-clock-based) rather than ALSO firing a separate,
        confident "job slowed down" page is the right response to "the
        monitoring pipeline itself is behind" -- a real job slowdown
        should page; the pipeline/aggregator falling behind should not."""
        if self.pipeline_down.get(hostname, False):
            return
        res = _query_instant_real_ts(self.vm_url, f'agg_job_throughput_ratio_to_baseline{{hostname="{hostname}"}}')
        floor = coverage_guard.ABSOLUTE_RATE_FLOOR_FRAC
        for row in res:
            if not self._fresh(row):
                continue
            rate = float(row["value"][1])
            ts = float(row["value"][0])
            deficit = floor - rate
            if self.throughput_tracker.observe(hostname, deficit, ts):
                self._emit_uniform_slowdown(hostname, rate, floor)

    def _emit_uniform_slowdown(self, hostname, rate, floor):
        """Separate from _emit()/_emit_network()/_emit_host() deliberately
        -- this finding has no rank and no bucket at all (it's job-wide,
        not per-rank), a fundamentally different shape from every other
        finding type this engine produces. type_candidate="uniform_
        slowdown" names it as its own class of problem, distinct from the
        existing per-rank compute/host/network types -- not routed through
        the coverage guard or the P18k tier machinery, both of which
        assume a single-rank or cluster-wide-drift finding shape neither
        of which this is."""
        lines = [
            f"[ALERT] node={hostname} type=uniform_slowdown confidence=CONFIRMED severity=PAGE", "",
            f"Job-wide throughput at {rate:.2f}x its healthy reference rate (established once "
            f"this job's real bucket structure stabilized -- from cross-job history when enough "
            f"exists for this workload's shape, self-calibrated only as a cold-start fallback), "
            f"below the {floor:.2f}x floor, sustained for {T.PERSIST_REQUIRED} consecutive checks.",
            "",
            "This is a job-wide finding, not a single-rank one -- every peer-relative check "
            "(CV, mean, outlier_count) can legitimately stay quiet during a uniform slowdown, "
            "since nothing looks anomalous relative to peers. This check exists specifically "
            "because that peer-relative blind spot is real (confirmed live: a uniform slowdown "
            "produced zero per-rank alerts while job throughput collapsed ~16x).",
            "",
            "Severity: PAGE -- a real, sustained, job-wide throughput collapse below half this "
            "job's own established healthy baseline, held for 3 consecutive checks, is not "
            "something to log quietly.",
        ]
        text = "\n".join(lines)
        with self._alerts_lock:
            self.alerts.append(text)
            print(text, flush=True)
            print("=" * 70, flush=True)

    def _check_rank0_outlier_rate(self, hostname):
        """P21.5 -- no-op. agg_rank0_outlier_rate depended on "rank 0"
        being a stable, externally-known global identity (the master
        process's known bookkeeping overhead) -- node_aggregator_ref.py no
        longer privileges any specific member as "rank 0" (see that file's
        own module docstring for why: Inspector's dump schema has no way
        to recover which physical GPU slot a given member/PID occupies),
        so this metric is never pushed any more and this check would only
        ever see empty results. Left as an explicit no-op, not silently
        deleted, so its absence is visible in the code rather than a
        quietly-vanished check someone has to rediscover later."""
        return

    def run(self, duration_s, poll_interval=None):
        """P27-hotfix4 (bugfix) -- duration_s <= 0 means run forever. Root
        cause of this session's own recurring "alert_engine.py is dead"
        false alarms (5 times): every relaunch passed a finite, test-
        scoped --duration (300s/3000s) appropriate for a one-off
        validation run, then a later mandatory liveness recheck found the
        process gone and treated that as an unexplained crash. It was
        never a crash -- run() has no exception handling around it, and
        confirmed directly: the clean, fully-computed "total alerts
        emitted" summary in main() only prints AFTER this loop returns
        normally, which cannot happen from an unhandled exception or an
        OS-level kill. Every "death" this session printed that exact
        summary, proving the timer simply expired as designed each time.
        [CHECK-FAILED] (_run_check) was never relevant here either -- it
        guards one check's own exception, not this top-level timer.
        A real persistent monitoring process needs no artificial ceiling
        at all; duration_s<=0 is the honest way to say that, rather than
        picking some large-but-still-finite number that just moves the
        same false alarm further out."""
        poll_interval = poll_interval or self.poll_interval
        forever = duration_s <= 0
        t_end = None if forever else time.time() + duration_s
        while forever or time.time() < t_end:
            self.poll_once()
            time.sleep(poll_interval)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("vm_url")
    ap.add_argument("--duration", type=float, default=120,
                     help="Seconds to run. <=0 means run forever (for persistent "
                          "monitoring deployment, as opposed to a bounded test run).")
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--hostnames", default=None,
                     help="Comma-separated list to pin explicitly (disables discovery). "
                          "Omit for dynamic VM-based node discovery (default).")
    ap.add_argument("--hostname-refresh-s", type=float, default=30.0)
    args = ap.parse_args()
    hostnames = tuple(args.hostnames.split(",")) if args.hostnames else None
    eng = AlertEngine(args.vm_url, hostnames=hostnames, poll_interval=args.poll_interval,
                       hostname_refresh_s=args.hostname_refresh_s)
    eng.run(args.duration, args.poll_interval)
    print(f"\ntotal alerts emitted: {len(eng.alerts)}")
    print(f"pipeline_down_cycles: {eng.n_pipeline_down_cycles} "
          f"({'PIPELINE WAS DOWN AT SOME POINT -- alert count above is NOT trustworthy for the down period(s)' if eng.n_pipeline_down_cycles else 'pipeline healthy for the entire run -- 0 alerts above is a genuine result'})")
    print(f"dcgm_hostengine_down_cycles: {eng.n_dcgm_hostengine_down_cycles} "
          f"({'nv-hostengine WAS DOWN on at least one host at some point -- DCGM-sourced cause-evidence for that period is degraded, not a genuine ambiguous read' if eng.n_dcgm_hostengine_down_cycles else 'nv-hostengine reachable on every host for the entire run'})")
