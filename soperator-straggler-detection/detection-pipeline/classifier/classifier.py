#!/usr/bin/env python3
"""Stage 3/4: classification logic, confidence tiers, manual-review output.

Decision order:
  1. Global drift check first (network defeats every peer comparison --
     if the fault affects ALL ranks equally, node-scoped/node-vs-node
     comparisons see nothing, since there's no outlier relative to peers).
  2. Node-scoped (always on) -- single-rank fault. Which statistic fired
     names the time-scale: mean->sustained, cv->intermittent,
     outlier_count->medium/long-burst.
  3. Node-vs-node (parallel, not gated) -- whole-node host fault.

Three tiers:
  CONFIRMED    -- pattern detected AND cause metric corroborates.
  PROBABLE     -- pattern detected, cause partially available/ambiguous.
  UNCONFIRMED  -- pattern detected, cause not determinable -> manual review.
"""
from detection import (score_all, per_rank_series, per_rank_series_with_ts, stat_mean,
                       recheck_node_vs_node_excluding, EXCLUDE_ALWAYS,
                       STAT_WINDOW_SIZE, chunk_windows, windowed_scores_per_node, score_rank0_outlier_rate,
                       select_primary_corroborating_buckets, discover_rank_hosts)
from cause_metrics import (query_dcgm_all_gpus, decode_throttle, query_ib_all_devices, query_host_cpu,
                            query_thermal_slowdown_all_gpus, query_matmul_tflops, query_network_snapshot,
                            query_nvlink_snapshot, check_nv_hostengine_alive)
from rolling_buffer import query_gpu_window, query_host_window, query_ib_window
import storage_evidence
import statistics as _st
import time as _time

NETWORK_ATTRIBUTION_CAVEAT = (
    "IB/pkey counter visibility does not isolate THIS job's traffic from other "
    "tenants sharing the fabric. This can say the fabric is unhealthy -- it "
    "cannot say this job is the one being hurt by it."
)
GLOBAL_DRIFT_RANK_DEV_PCT = 10.0  # a rank counts as "elevated" if its mean
                                  # exec time is >10% worse than ITS OWN
                                  # stored healthy baseline (not vs peers --
                                  # peers can't see a uniform shift).
NETWORK_XMIT_NEAR_ZERO_BPS = 1e6  # <1MB/s aggregate during active training
# P25 -- same "basically nothing is moving" floor as IB's, not a NVLink-
# generation-specific expected-bandwidth assumption: real healthy per-host
# NVLink throughput measured live on this hardware ranged from ~2.4MB/s
# (per-GPU idle-ish) up to 100+MB/s aggregate under real TP+DDP training --
# 1MB/s stays far below that regardless of NVLink generation or link count.
NVLINK_XMIT_NEAR_ZERO_BPS = NETWORK_XMIT_NEAR_ZERO_BPS
                                  # is the only "throughput drop" callable
                                  # without a stored per-job throughput
                                  # baseline (none exists yet -- same
                                  # disclosed-limitation shape as Path C).

IDLE_POWER_W = 150.0  # idle GPUs on this hardware draw ~75W; a genuinely
                      # busy H200 draws 200W+ under real compute.
                      # P26 fix -- kept only as a historical record of the
                      # measurement that produced it (nanoGPT/TP/FSDP/MoE,
                      # all heavier compute than ResNet); no longer used by
                      # Path B's activity gate below, see ACTIVE_POWER_
                      # PEER_FRAC's own comment for why.
# P26 fix -- ResNet's real, healthy power draw under this EXACT clock-lock
# fault measured live at 121.21W (peer median 253.13W, ratio 0.479) --
# BELOW the old absolute IDLE_POWER_W=150.0, despite the GPU being
# genuinely, provably active (training loop running, real z=520+ signal).
# That constant was calibrated only against nanoGPT/TP/FSDP/MoE (all
# heavier compute, 220-240W active), never revisited for a lighter
# workload -- the third instance of this exact project's recurring root
# habit. Fixed by comparing against THIS SAME job's OTHER (unfaulted)
# ranks on the SAME node instead of a global absolute constant -- exactly
# the same design this file's Path B already uses for sm_clock (`sm <
# 0.6 * peer_sm`) just to the left of this fix, which never needed a
# workload-specific absolute clock-speed number either. Requires zero
# new hardware- or workload-specific constants: whatever THIS workload's
# real active power level currently is, its own peers measure it live,
# every single time, automatically adapting to any future workload shape.
# 0.40 chosen from real measured ratios on both sides: idle/active power
# ratio for every heavier workload tested is ~0.31-0.36 (75-79W idle /
# 220-240W active); ResNet's degraded-but-genuinely-active ratio measured
# live at 0.479 -- 0.40 sits with real margin on both sides of that gap.
ACTIVE_POWER_PEER_FRAC = 0.40
TFLOPS_DEVIATION_PCT = 10.0   # from run_health_check.sh's own flag threshold
THERMAL_RATIO_MIN = 100.0    # target counter must be >>100x the node median
                              # of its peers to count as "thermal degradation"
                              # (GPU4's 2847s vs mostly-0s peers would trip a
                              # naive >0 check; this requires real separation)
# P27.2 -- named out of determine_confirmed_path's own Path B literal
# (was inline `0.6`) so alert_engine.py's new 2-member timing-asymmetry
# fallback (_timing_asymmetry_fallback_evaluate) can reuse the exact same
# already-validated suppression ratio for its OWN comparison axis (a
# member's current agg_mean_exec_time_us vs. its own recent historical
# baseline, not vs. a DCGM clock peer) instead of inventing a second,
# undifferentiated "0.6"-shaped number. Same magnitude of real,
# already-accepted significance; same name would be misleading (this is
# now used for two different comparison axes), so PATH_B_AND_TIMING_
# SUPPRESS_RATIO names what's actually shared: the ratio itself, not
# what it's compared against.
PATH_B_AND_TIMING_SUPPRESS_RATIO = 0.6
IOWAIT_LIVE_WINDOW_S = 10.0  # P26.5-maintenance -- Path C's live (no
                              # rank_ts_range) fallback window: how far
                              # back from "now" to look for real io-wait,
                              # mirroring Path B's own live-query
                              # assumption that a fault an alert just
                              # fired on is still ongoing. Matches this
                              # alerting pipeline's own established poll
                              # cadence order of magnitude (alert_engine.py
                              # polls every few seconds), not an arbitrary
                              # number -- wide enough to not miss the
                              # agent's own 2s-granularity print interval,
                              # narrow enough to not pull in an unrelated,
                              # much-earlier spike as if it were current.

# thresholds -- set from measured healthy nulls (Stage 5), not intuition.
THRESH = {
    "mean_z": 30.0,      # healthy null: z 2.99-3.14. 10x margin.
    "mean_mm": 2.0,      # healthy null: mm 1.016-1.017.
    "cv_z": 20.0,        # P18e Stage 1 FIXED: was false-firing on
                         # stage3_baseline (z=20.83, a healthy run) --
                         # root cause was stat_cv's total lack of outlier
                         # resistance (4/300 one-off low samples in one
                         # rank inflated its whole-run CV). Confirmed the
                         # OUTLIER is that one run, not the population
                         # (n=11 healthy: median z=3.13, stage3_baseline
                         # was the single outlier at 20.83, next-highest
                         # was 9.26). Fixed at the statistic level
                         # (stat_cv now trims 2 samples/side) rather than
                         # by raising this threshold. Re-verified: n=11
                         # healthy max is now 9.7, true-positive jitter
                         # still fires at z=158.8. VALIDATED, n=11.
    "cv_mm": 1.9,        # healthy null: mm 1.34-1.61.
    "rank0_cv_z": None,  # P18f Stage 3: RETIRED as a firing gate. Proven
                         # (not just suspected) that this statistic cannot
                         # separate rank0's genuine intermittent-only
                         # fault from healthy noise -- re-measured at
                         # n=20 healthy runs, the healthy max (51.49)
                         # EXCEEDS the true positive (50.14). No threshold
                         # works. score_rank0_cv is still computed and
                         # reported as corroborating context on a finding
                         # that fires via rank0_outlier_rate instead, but
                         # it no longer gates anything by itself.
    "rank0_outlier_rate": 0.03,  # P18f Stage 3 replacement statistic --
                         # see score_rank0_outlier_rate. Validated n=19
                         # healthy runs (excluded P4_jitter/healthy: a
                         # contaminated run with elevated outlier rates
                         # on MULTIPLE ranks, not rank0-specific -- same
                         # exclusion logic as bucket-A contamination
                         # screening). Healthy max rate 0.0052, true
                         # fault rate 0.1533 -- ~5.8x margin above
                         # healthy, ~5.1x margin below the fault.
    "rank0_cv_z_STALE": 65.0,  # P18e Stage 1 re-calibration -- P18e's stat_cv
                         # trim fix (2 samples each side, added to fix the
                         # main CV check's false positive) has a side
                         # effect HERE: trimming shrinks peer CV variance
                         # more than it shrinks rank0's own (persistent,
                         # not spiky) elevated CV, which INFLATES rank0's
                         # z. Re-measured across n=11 healthy runs: max
                         # healthy z rose from 37.0 (untrimmed, n=5) to
                         # 48.17 (trimmed, n=11) -- the OLD threshold of
                         # 50.0 left only a 3.8% margin, unsafe. The
                         # genuine rank0 fault ALSO dropped under trimming
                         # (119.9 -> 82.7), so the healthy/fault gap is
                         # genuinely narrower now, not just measurement
                         # noise. 65.0 sits ~35% above the new healthy
                         # max and ~21% below the fault -- a real but
                         # thinner margin than before, disclosed as such.
    "outlier_count": 4,  # healthy null: counts 1-2. Fault: count=5.
    "outlier_count_mm": 2.0,  # P18c Stage 2 addition -- see fired().
    "node_vs_node_sd": 2.0,   # healthy null: -0.47 to -0.71 SD. Needs real margin.
    "global_drift_frac": 0.8,  # fraction of ranks that must show elevated
                                # exec time relative to their OWN historical
                                # baseline for a "global drift" call.
}
COVERAGE_MIN = 0.99  # below this, degrade to a coverage warning, don't fire.
HOST_CHECK_RECENT_WINDOW_S = 90.0  # P18h Stage 3 fix -- see
    # check_host_contention_direct. Both tested burner durations (60-90s)
    # fit inside this window with margin, while a fault that ended more
    # than 90s ago correctly ages out of the check.
NETWORK_CHECK_RECENT_WINDOW_S = 90.0  # same fix, same reasoning, for
    # check_network_contention_direct's ts_range (see classify_incremental).
CV_PERSISTENCE_WINDOW = 3  # P18g Stage 3: "2 of the last N windows fired"
                           # persistence rule for CV -- see the windowed-
                           # scoring loop. Replaced P18f's strictly-
                           # consecutive persistence-2, which broke on a
                           # single dip in an otherwise strong fault.
CV_PERSISTENCE_REQUIRED = 2

# ---------------------------------------------------------------------------
# P18e Stage 2: structural safeguard against the recurring failure pattern
# named explicitly in this session's prompt -- THREE separate times now
# (rank0's CV threshold, then the main CV threshold) a threshold was set,
# looked solid, and then failed on the NEXT healthy sample nobody had
# checked yet. That is a process gap, not a coincidence.
#
# RULE: any threshold gating a fault-detection statistic must be checked
# against >=MIN_HEALTHY_N_FOR_VALIDATED independent healthy runs before
# it is considered VALIDATED. Fewer than that, and it must be labeled
# "provisional" wherever it's reported -- not silently treated as settled.
#
# This registry is the checklist. When you add or change a threshold:
#   1. Add/update its entry here with the ACTUAL n of healthy runs you
#      checked it against (not the n of runs that exist somewhere --
#      the n you personally re-scored against THIS threshold, now).
#   2. If n < MIN_HEALTHY_N_FOR_VALIDATED, set status="provisional" and
#      say what's missing (e.g. "never measured against a healthy run
#      with the job actively training" for the network branch).
#   3. Re-run audit_thresholds() and paste its output into the report.
# The next session should extend this table, not skip it.
# ---------------------------------------------------------------------------
MIN_HEALTHY_N_FOR_VALIDATED = 5

THRESHOLD_REGISTRY = {
    "mean_z/mean_mm": {
        "peer_group": "node-scoped, excludes rank3 only", "n_healthy": 11,
        "status": "validated", "note": "n=11 healthy: z max 3.49, mm max 1.018 (threshold 30.0/2.0)."},
    "cv_z (main)": {
        "peer_group": "node-scoped, excludes rank3 + rank0", "n_healthy": 11,
        "status": "validated", "note": "P18e-fixed (trimmed stat_cv). n=11 healthy max z=9.7 (threshold 20.0)."},
    "rank0_cv_z": {
        "peer_group": "score_rank0_cv, rank0 vs {1,2,4,5,6,7}", "n_healthy": 11,
        "status": "validated", "note": "n=11 healthy max z=48.17 (threshold 65.0) -- thinner margin than "
                                        "the main CV check; disclosed, not hidden."},
    "outlier_count/outlier_count_mm": {
        "peer_group": "node-scoped, excludes rank3 + rank0", "n_healthy": 11,
        "status": "validated", "note": "n=11 healthy: raw count max=2 (threshold 4), mm always inf on healthy."},
    "node_vs_node_sd": {
        "peer_group": "whole-node mean-of-means, excludes rank3", "n_healthy": 11,
        "status": "validated", "note": "n=11 healthy: |diff_sd| max 0.468 (threshold 2.0)."},
    "global_drift_frac": {
        "peer_group": "per-rank mean vs stored healthy baseline", "n_healthy": 2,
        "status": "PROVISIONAL", "note": "Baseline built from only 2 healthy runs (healthy4+healthy5); "
                                          "tested against only 1-2 held-out healthy runs, not 5 independent "
                                          "ones. False-positive-safe on what was checked, but below the bar."},
    "IDLE_POWER_W (Path B activity gate)": {
        "peer_group": "not a per-run statistic -- physical power threshold", "n_healthy": "n/a (10+ direct runs)",
        "status": "validated (different kind)", "note": "Not a healthy-run false-positive question -- "
                                                          "repeatedly measured directly: idle ~75-79W, active "
                                                          "~220-240W under load, every clock-lock test this "
                                                          "investigation has run. Large, consistent separation."},
    "THERMAL_RATIO_MIN / MIN_ABS_THERMAL_S (Path A)": {
        "peer_group": "not a per-run statistic -- cross-GPU physical comparison", "n_healthy": "n=3 distinct GPUs",
        "status": "PROVISIONAL", "note": "Only 3 distinct GPUs ever exercised this: GPU3 (real degradation, "
                                          "fires), GPU4 (elevated counter/normal throughput, correctly doesn't "
                                          "fire), rank11 (same pattern as GPU4, correctly doesn't fire). "
                                          "Never seen a 4th/5th distinct case."},
    "HOST_LOAD_RATIO_MIN (Path C)": {
        "peer_group": "node-to-node CPU load ratio", "n_healthy": 0,
        "status": "PROVISIONAL", "note": "Never measured on a HEALTHY run at all -- only ever compared "
                                          "during an active host-fault injection. No healthy node-to-node "
                                          "load-ratio distribution exists. Also: the only host-fault injection "
                                          "tool available (cpu_burn.sh) is duty-cycled (5s on/5s off), and this "
                                          "path has never once reached CONFIRMED across any test in this "
                                          "investigation -- see Stage 3."},
    "NETWORK_XMIT_NEAR_ZERO_BPS / participation<0.8": {
        "peer_group": "IB device participation/throughput, per host", "n_healthy": 0,
        "status": "PROVISIONAL", "note": "Never measured against a HEALTHY run WHILE ACTIVELY TRAINING -- "
                                          "every 'healthy' network reading on record is from an IDLE cluster "
                                          "(0% participation), which is what made the coincidental-pass finding "
                                          "possible in the first place. True healthy-under-load IB baseline "
                                          "does not exist yet."},
}


def audit_thresholds():
    """Prints the table Stage 2 asks for. Run this, don't hand-copy it."""
    print(f"{'threshold':<40} {'peer group':<45} {'n':<8} status")
    for name, info in THRESHOLD_REGISTRY.items():
        print(f"{name:<40} {info['peer_group']:<45} {str(info['n_healthy']):<8} {info['status']}")

STAT_TO_TIMESCALE = {"mean": "sustained", "cv": "intermittent", "outlier_count": "medium/long-burst",
                     "outlier_rate": "intermittent"}  # P18f Stage 3: rank0's replacement stat


def worker_of(rank, rank_hosts):
    """Real, discovered host for `rank` (rank_hosts, from detection.
    discover_rank_hosts) -- replaces the old hardcoded `rank < 8` split
    (codebase audit, third pass: this was still live and active here,
    despite the live alerting path's own identical worker_of()/rank%8
    pattern already being fixed via real gpu_slot_index discovery).
    Returns None if this rank's host genuinely isn't known (no dump data
    for it) -- an honest 'can't tell', matching every other missing-data
    case in this module, never a guessed node."""
    return rank_hosts.get(rank)


def fired(stat_name, result):
    if result is None:
        return False
    z, mm = result["z_node"], result["maxmed_node"]
    if stat_name == "mean":
        return z > THRESH["mean_z"] and mm > THRESH["mean_mm"]
    if stat_name == "cv":
        # mm has almost no margin here: healthy tops out at 1.61, the
        # 10ms/100ms reference detection is only 1.682 -- 0.07 apart.
        # z separates much better (healthy max 14.41 vs detection 27.7-47.9),
        # so gate primarily on z; mm is corroborating only, not required.
        return z > THRESH["cv_z"]
    if stat_name == "outlier_count":
        # P18c Stage 2 fix: raw count alone (>=4) misattributes under
        # whole-node contention -- a CPU burner elevates outlier_count on
        # EVERY rank on the node (observed 9-15 vs the healthy 1-2
        # baseline), and whichever rank happens to have the highest raw
        # count "wins" even though it shows no real separation from its
        # now-also-elevated peers (mm=1.25, z=0.998 in the case that
        # exposed this). Add the same peer-relative mm gate mean/cv
        # already use. This works cleanly because of how mm degenerates:
        # on clean data (healthy, or a genuine single-rank fault), peers'
        # outlier counts cluster at 0-1, so peer_median is usually 0 and
        # mm is +inf -- always > any finite threshold, so a real fault is
        # never blocked by this gate. Under node-wide contamination, ALL
        # ranks' counts rise together, peer_median becomes nonzero, and
        # mm collapses to a small finite ratio -- exactly what should be
        # suppressed. One gate, both regimes.
        return result["worst_val"] >= THRESH["outlier_count"] and mm > THRESH["outlier_count_mm"]
    return False


def check_global_drift(dump_dirs, calibration=None, bucket=None):
    """All ranks moved together relative to a stored healthy baseline?
    Peer-relative comparisons (node-scoped, node-vs-node) are structurally
    blind to this -- if every rank slows down together there is no
    outlier relative to anyone else. The only way to see it is to compare
    against an ABSOLUTE reference from a healthy run, not against peers
    in the same run.

    Requires calibration['healthy_per_rank_mean'][bucket] (P18b addition
    to calibration.py, compute_healthy_per_rank_mean()). Without a
    calibration object carrying this, returns None honestly rather than
    fabricating a same-run comparison (comparing ranks to each other in
    the same run is exactly what node-scoped/node-vs-node already do).

    Cluster-topology-agnostic fix (this session): bucket used to default
    to BUCKET_B -- one specific workload's own real message size,
    silently wrong for any other. bucket=None now means "discover THIS
    run's own real primary bucket" (select_primary_corroborating_
    buckets), matching whatever calibration['healthy_per_rank_mean'] was
    actually keyed by for a real calibration run against this same
    dump_dirs. An explicit bucket is still honored unchanged if a caller
    already knows which one it wants."""
    if not calibration:
        return None
    if bucket is None:
        bucket, _ = select_primary_corroborating_buckets(dump_dirs)
        if bucket is None:
            return None
    baseline = calibration.get("healthy_per_rank_mean", {}).get(bucket)
    if not baseline:
        return None
    per_rank, _ = per_rank_series(dump_dirs, {bucket})
    now_means = {r: stat_mean(v) for r, v in per_rank.items() if r not in EXCLUDE_ALWAYS}
    per_rank_dev, elevated, total = {}, 0, 0
    for r, now_mean in now_means.items():
        base = baseline.get(r)
        if base is None:
            continue
        total += 1
        dev_pct = (now_mean - base) / base * 100
        per_rank_dev[r] = dev_pct
        # P18c bug fix: this used to check dev_pct > +10 ("elevated" =
        # higher exec time = worse). That inverts the sign convention
        # already established for this exact statistic everywhere else
        # in this codebase (detection.py STATS["mean"] direction="min" --
        # "sustained: straggler shows LOW mean", verified early in this
        # investigation and never revisited since). Found via a real
        # NCCL_MAX_NCHANNELS=1 test: it produced a uniform ~22% DECREASE
        # in bucket-B exec time across all 15 ranks -- a genuine global
        # effect that the wrong-signed check silently missed entirely
        # (fraction_elevated read 0.0 despite every rank moving together).
        if dev_pct < -GLOBAL_DRIFT_RANK_DEV_PCT:
            elevated += 1
    if total == 0:
        return None
    return {"fraction_elevated": elevated / total, "per_rank_dev_pct": per_rank_dev, "n_ranks": total}


def check_network_contention_direct(buffer, ib_hosts, ts_range):
    """P18h Stage 1: IB-evidence-based network detection, checked
    INDEPENDENTLY of any arrival-lag statistic -- same pattern
    check_host_contention_direct already established for host faults.

    Root problem this replaces: the old design gated the network finding
    behind check_global_drift's fraction_elevated (an exec-time-based
    measure) crossing 0.8. P18g's three-simultaneous-faults test found
    this can mask a real, independently-confirmable network fault: IB
    participation showed an unambiguous 0.25 collapse while
    fraction_elevated only reached 0.333 (diluted by the compute+host
    faults ALSO present distorting the per-rank exec-time baseline that
    statistic depends on). A signal this direct and this unambiguous
    should never be gated behind a DIFFERENT statistic's threshold.

    Uses the n=28-validated (P18g) healthy-under-load thresholds:
    participation always 1.0 healthy (gate at 0.8), throughput 0.79-1.08
    GB/s healthy (gate at 1MB/s), zero errors/congestion ever healthy."""
    net, err = None, None
    if buffer is not None and ts_range and ib_hosts:
        _, _, ib_buf = buffer
        net, err = query_ib_window(ib_buf, ib_hosts, ts_range[0], ts_range[1])
    elif ib_hosts:
        net, err = query_network_snapshot(ib_hosts)
    if not ib_hosts or err or not net:
        return None, err, (buffer is not None and ts_range)
    any_errors_or_congestion = any(h["errors_delta"] > 0 or h["congestion_delta"] > 0 for h in net.values())
    any_participation_collapse = any(
        h["total_devices"] > 0 and h["participation_frac"] < 0.8 for h in net.values())
    any_throughput_near_zero = any(h["xmit_rate_bytes_s"] < NETWORK_XMIT_NEAR_ZERO_BPS for h in net.values())
    capacity_signal = any_participation_collapse or any_throughput_near_zero
    if not capacity_signal and not any_errors_or_congestion:
        return None, None, (buffer is not None and ts_range)  # clean IB evidence -- no network finding
    tier = "CONFIRMED" if (any_errors_or_congestion and capacity_signal) else "PROBABLE"
    return {"net": net, "tier": tier}, None, (buffer is not None and ts_range)


def check_nvlink_contention_direct(buffer, nvlink_hosts, ts_range):
    """P25 Part 1 -- NVLink counterpart to check_network_contention_direct,
    same structure deliberately (participation + throughput + error/
    congestion cross-check), same tiering rule (CONFIRMED needs BOTH a
    capacity signal and an error/congestion signal; PROBABLE needs
    either alone). Closes a real, confirmed gap: TP's traffic is
    exclusively NVLink (TP pairs are always co-located on one node, see
    P21's device_mesh comments) -- check_network_contention_direct only
    ever reads IB counters, so a real NVLink fault on any TP job was
    completely undetectable before this, not just harder to catch.

    Only the live-query path (query_nvlink_snapshot) is wired up -- no
    rolling-buffer NVLink sampler exists yet (mirroring query_ib_window
    would need one), so a buffer+ts_range call returns None/reason
    rather than silently guessing; this matches how alert_engine.py
    only ever calls check_network_contention_direct live (buffer=None,
    ts_range=None) too, so it's not a real capability gap in practice."""
    if buffer is not None and ts_range:
        return None, "NVLink rolling-buffer path not implemented -- live-query only", True
    nv, err = None, None
    if nvlink_hosts:
        nv, err = query_nvlink_snapshot(nvlink_hosts)
    if not nvlink_hosts or err or not nv:
        return None, err, False
    any_errors_or_congestion = any(h["errors_delta"] > 0 or h["congestion_delta"] > 0 for h in nv.values())
    any_participation_collapse = any(
        h["total_devices"] > 0 and h["participation_frac"] < 0.8 for h in nv.values())
    any_throughput_near_zero = any(h["xmit_rate_bytes_s"] < NVLINK_XMIT_NEAR_ZERO_BPS for h in nv.values())
    capacity_signal = any_participation_collapse or any_throughput_near_zero
    if not capacity_signal and not any_errors_or_congestion:
        return None, None, False  # clean NVLink evidence -- no finding
    tier = "CONFIRMED" if (any_errors_or_congestion and capacity_signal) else "PROBABLE"
    return {"net": nv, "tier": tier}, None, False


def build_global_drift_finding(drift, ib_hosts, buffer=None, ts_range=None, net_check=None):
    """Cause-side corroboration for a global-drift finding, using P6's
    network finding set: device participation, aggregate throughput rate,
    error/congestion counter cross-check. The pkey-attribution caveat is
    attached unconditionally -- it applies to every network finding this
    classifier can produce, confirmed or not.

    P18h Stage 1: net_check (from check_network_contention_direct) is
    now the PRIMARY trigger and evidence source. `drift` (arrival-lag
    fraction_elevated) is corroborating context only -- reported, never
    gating. This function is called whenever check_network_contention_
    direct fires, drift or no drift."""
    cause = {"checked": [], "impossible": [], "network": {}}
    tier = "UNCONFIRMED"
    net, err, used_buffer = net_check if net_check is not None else \
        check_network_contention_direct(buffer, ib_hosts, ts_range)
    if not ib_hosts:
        cause["impossible"].append("IB counters not queried -- no ib_hosts provided")
    elif err:
        cause["impossible"].append(err)
    elif net is None:
        cause["impossible"].append("IB counters clean -- no network-specific evidence at this ts_range")
    else:
        source = "rolling_buffer" if used_buffer else "live_query"
        cause["checked"].append(f"IB device participation, throughput rate, error/congestion counters ({source})")
        cause["network"] = net["net"]
        tier = net["tier"]
    # arrival-lag drift is corroborating context, NEVER a gate (P18h fix)
    cause["arrival_lag_corroboration"] = (
        f"fraction_elevated={drift['fraction_elevated']:.3f}" if drift else "not computed"
    )
    if drift and drift["fraction_elevated"] < THRESH["global_drift_frac"] and tier != "UNCONFIRMED":
        cause["arrival_lag_corroboration"] += (
            " -- BELOW its own threshold despite confirmed network evidence. This is expected, not "
            "contradictory, when a concurrent compute/host fault also distorts the per-rank exec-time "
            "baseline (P18g Finding 1/2), or when the network fault itself doesn't slow exec time enough "
            "to trip that separate statistic. Do not read this as the network finding being unreliable."
        )
    return {
        "type_candidate": "network", "timescale": "sustained/global",
        "evidence": drift, "cause": cause, "tier": tier,
        "attribution_caveat": NETWORK_ATTRIBUTION_CAVEAT,
    }


def classify(dump_dirs, dcgm_host_map=None, ib_hosts=None, calibration=None, buffer=None):
    """Batch convenience wrapper -- runs classify_incremental once over
    the whole dump set with no prior state, and returns just the result
    (state discarded). Every existing caller of classify() keeps working
    unchanged; this is what makes P18f's windowing rewrite provably
    non-regressive against every previously-validated case."""
    result, _state = classify_incremental(dump_dirs, state=None, dcgm_host_map=dcgm_host_map,
                                           ib_hosts=ib_hosts, calibration=calibration, buffer=buffer)
    return result


def classify_incremental(dump_dirs, state=None, dcgm_host_map=None, ib_hosts=None, calibration=None, buffer=None):
    """P18f Stage 1: classify(), but genuinely windowed and incrementally
    callable. Prior sessions validated window sizes (mean~100, cv~250)
    in STANDALONE scripts only -- this is the first time that windowing
    is wired into the actual detection path. A live deployment would
    call this repeatedly as new Inspector records arrive, passing back
    the `state` this call returns; `state=None` scores from the start
    (which is what the classify() wrapper above does, for batch/replay
    use against a completed dump set).

    state (opaque dict, pass back verbatim): {"mean_offset", "cv_offset",
    "rank0_cv_offset", "fired_windows": {rank: [(stat, t0, t1, result), ...]}}
    -- fired_windows accumulates every window that ever fired for a rank,
    across calls, so a fault spanning several increments still produces
    ONE consolidated finding with a ts_range covering only the windows
    that actually fired (not the whole job) -- this is the fix for the
    transient-case dilution P18e found: Path B's buffer lookback used to
    span the rank's ENTIRE timestamp range, diluting the clock-ratio
    signal for a fault that was only ~35% of the run. Now it spans only
    the windows that actually fired.

    outlier_count and node_vs_node remain whole-run, recomputed fresh
    each call -- established finding (P18b/c) is outlier_count needs
    ~60s+/most of a run to accumulate, so windowing it would only ever
    see healthy-looking sub-windows; node_vs_node is being replaced
    entirely in Stage 2, not windowed here."""
    if state is None:
        state = {"mean_offset": 0, "cv_offset": 0, "rank0_cv_offset": 0, "fired_windows": {}}

    # Cluster-topology-agnostic fix (this session): primary/corroborating
    # buckets used to be hardcoded BUCKET_B/BUCKET_C -- one specific
    # workload's own real message sizes. Discovered once, here, from THIS
    # run's own real data, and reused for both calls below so per_rank_ts
    # and scored["primary"] stay consistent with each other (both must
    # reflect the SAME real bucket).
    primary_bucket, corroborating_buckets = select_primary_corroborating_buckets(dump_dirs)
    if primary_bucket is None:
        return {"status": "coverage_warning", "coverage": 0.0}, state
    per_rank, per_rank_ts, _ = per_rank_series_with_ts(dump_dirs, {primary_bucket})
    scored = score_all(dump_dirs, primary_bucket=primary_bucket, corroborating_buckets=corroborating_buckets)
    primary = scored["primary"]
    findings = []
    # Codebase audit, third pass -- real, discovered rank->hostname
    # identity (see discover_rank_hosts), replacing worker_of()'s old
    # hardcoded rank<8 split and recheck_node_vs_node_excluding/score_
    # rank0_outlier_rate's own old NODE_A/NODE_B dependency below.
    rank_hosts = discover_rank_hosts(dump_dirs)

    rank_ts_range = None
    if per_rank_ts:
        rank_ts_range = {r: (min(ts), max(ts)) for r, ts in per_rank_ts.items() if ts}

    if primary["_coverage"] < COVERAGE_MIN:
        return {"status": "coverage_warning", "coverage": primary["_coverage"]}, state

    # --- network: IB-evidence-first, checked independently of arrival-lag
    # (P18h Stage 1 -- see check_network_contention_direct). Fires on its
    # own IB evidence; check_global_drift's fraction_elevated is computed
    # too and attached as corroborating context, but is NEVER a gate that
    # can suppress an independently-confirmed network finding. ---
    whole_run_ts_range = None
    if rank_ts_range:
        whole_run_ts_range = (min(r[0] for r in rank_ts_range.values()),
                               max(r[1] for r in rank_ts_range.values()))
    # P18h Stage 3 fix: same bug as check_host_contention_direct -- using
    # the WHOLE job's ts_range means a network fault that ended minutes
    # ago never ages out (IB counters are cumulative, so a delta over
    # "everything so far" still reflects the fault period). Scope the
    # NETWORK CHECK specifically to a recent window; whole_run_ts_range
    # is still used elsewhere (e.g. global drift corroboration context).
    recent_net_ts_range = whole_run_ts_range
    if whole_run_ts_range:
        recent_net_ts_range = (max(whole_run_ts_range[0], whole_run_ts_range[1] - NETWORK_CHECK_RECENT_WINDOW_S),
                                whole_run_ts_range[1])
    drift = check_global_drift(dump_dirs, calibration=calibration)
    net_check, net_err, net_used_buffer = check_network_contention_direct(buffer, ib_hosts, recent_net_ts_range)
    network_finding = None
    if net_check is not None:
        network_finding = build_global_drift_finding(drift, ib_hosts, buffer=buffer, ts_range=recent_net_ts_range,
                                                       net_check=(net_check, net_err, net_used_buffer))
        # appended AFTER the per-rank loop below, once we know whether a
        # compute finding also exists on this job -- see the annotation
        # logic there (P18h Stage 2).

    # --- windowed node-scoped: single-rank fault, mean/cv genuinely
    # rolling now, only NEW windows scored each call ---
    fired_windows = state["fired_windows"]
    for stat_name in ("mean", "cv"):
        offset_key = f"{stat_name}_offset"
        window_size = STAT_WINDOW_SIZE[stat_name]
        window_results, new_offset = windowed_scores_per_node(per_rank, per_rank_ts, stat_name,
                                                                window_size, state[offset_key])
        state[offset_key] = new_offset
        # P18f Stage 1 fix, found via Stage 3's healthy re-check: a
        # single noisy CV window can cross the whole-run-calibrated
        # threshold on genuinely healthy data (measured: 4/19 healthy
        # runs false-fired on a lone window, z up to 32.96) -- whole-run
        # validation doesn't validate per-window noise. Mean's windows
        # showed no such false positives (checked the same 19 runs), so
        # persistence applies to CV only, preserving mean's fast single-
        # window detection (the validated ~0.72s transient latency).
        # P18g Stage 3: strictly-CONSECUTIVE persistence-2 turned out too
        # fragile even on a genuine, strong fault -- a single window with
        # a lower (but still real) z broke the streak entirely (measured:
        # a sustained fault's windows [23.8, 18.3, 87.8] against
        # threshold 20 never satisfied "2 IN A ROW"). Switched to "2 of
        # the last 3 windows fired", which tolerates one dip without
        # losing detection, while still requiring more than a single
        # lone spike to fire on healthy data. Also fixes P18f's short-
        # run blind spot: combined with window=125 (half the old 250)
        # and a stricter trim (4, up from 2), this recovers clean
        # separation down to 300 samples -- verified against 4 healthy
        # runs (max z 5.9) and 2 true positives (both fire cleanly) at
        # exactly that length. Mean is unaffected (no persistence
        # needed, no false positives found at any tested length).
        history_key = f"{stat_name}_history"
        histories = state.setdefault(history_key, {})
        for win in window_results:
            fired_ranks_this_window = set()
            all_ranks_this_window = set()
            for node_result in win["per_node"].values():
                if node_result is None:
                    continue
                rank = node_result["worst_rank"]
                all_ranks_this_window.add(rank)
                if fired(stat_name, node_result):
                    fired_ranks_this_window.add(rank)
                if stat_name != "cv":
                    if rank in fired_ranks_this_window:
                        fired_windows.setdefault(rank, []).append((stat_name, win["t0"], win["t1"], node_result))
                    continue
                hist = histories.setdefault(rank, [])
                hist.append((rank in fired_ranks_this_window, node_result))
                if len(hist) > CV_PERSISTENCE_WINDOW:
                    hist.pop(0)
                if sum(1 for fired_flag, _ in hist if fired_flag) >= CV_PERSISTENCE_REQUIRED:
                    # report using the strongest window's result among
                    # the ones that actually fired in this short history
                    best = max((r for f, r in hist if f), key=lambda r: r["z_node"] if r["z_node"] != float("inf") else 1e18)
                    fired_windows.setdefault(rank, []).append((stat_name, win["t0"], win["t1"], best))

    # outlier_count: deliberately whole-run, not windowed (see docstring).
    # Uses the whole job's ts_range as its "window" since that's the
    # scope it was actually scored over.
    whole_run_range = (min(r[0] for r in rank_ts_range.values()), max(r[1] for r in rank_ts_range.values())) \
        if rank_ts_range else (0, 0)
    # outlier_count is recomputed whole-run on EVERY call (not offset-
    # tracked like mean/cv), so a naive append would duplicate the same
    # firing across successive incremental calls -- strip any prior
    # outlier_count entry for this rank before adding the current one.
    for rank in fired_windows:
        fired_windows[rank] = [h for h in fired_windows[rank] if h[0] != "outlier_count"]
    for node_result in primary["outlier_count_per_node"].values():
        if node_result is None or not fired("outlier_count", node_result):
            continue
        rank = node_result["worst_rank"]
        fired_windows.setdefault(rank, []).append(("outlier_count", whole_run_range[0], whole_run_range[1], node_result))

    # P18f Stage 3: rank0's dedicated statistic is now outlier_rate, not
    # CV (score_rank0_cv is proven unable to separate the true positive
    # from healthy noise -- see THRESH["rank0_cv_z"]'s comment). Whole-
    # run, like outlier_count, not windowed (it's the same rare-event-
    # rate statistic, just with rank0's own peer group).
    for rank in fired_windows:
        fired_windows[rank] = [h for h in fired_windows[rank] if h[0] != "outlier_rate"]
    r0_rate = score_rank0_outlier_rate(per_rank, rank_hosts)
    if r0_rate is not None and r0_rate["worst_rate"] > THRESH["rank0_outlier_rate"]:
        fired_windows.setdefault(0, []).append(("outlier_rate", whole_run_range[0], whole_run_range[1], r0_rate))

    # --- consolidate: one finding per rank, ts_range = only the windows
    # that fired (the actual fix for the transient-dilution problem) ---
    for rank, hits in fired_windows.items():
        # P18f: a sustained fault fires MANY windows of the same stat --
        # collapse to one entry per stat_name (keep that stat's strongest
        # window) so all_fired_stats reports unique stats, not one entry
        # per firing window.
        best_by_stat = {}
        for stat, t0, t1, result in hits:
            prev = best_by_stat.get(stat)
            if prev is None or result["z_node"] > prev["z_node"]:
                best_by_stat[stat] = result
        stat_hits = list(best_by_stat.items())
        # P18f bug fix: outlier_count's hit uses the WHOLE run as its
        # "window" (it's deliberately unwindowed) -- letting it into the
        # min/max here would re-widen the tight ts_range right back out
        # to the whole job whenever outlier_count also fires alongside
        # mean/cv, defeating the entire point of windowing for a
        # transient fault. Only mean/cv windows narrow the ts_range.
        windowed_hits = [(t0, t1) for stat, t0, t1, _ in hits if stat != "outlier_count"]
        if windowed_hits:
            fire_t0 = min(t0 for t0, _ in windowed_hits)
            fire_t1 = max(t1 for _, t1 in windowed_hits)
        else:
            fire_t0 = min(t0 for _, t0, _, _ in hits)
            fire_t1 = max(t1 for _, _, t1, _ in hits)
        tight_ts_range = {rank: (fire_t0, fire_t1)}
        finding = build_single_rank_finding(rank, stat_hits, primary, scored["corroborating"],
                                             dcgm_host_map, ib_hosts, buffer=buffer,
                                             rank_ts_range=tight_ts_range, rank_hosts=rank_hosts)
        findings.append(finding)

    # P18h Stage 2: when network detection fires, annotate rather than
    # silently trust any concurrent compute-fault NON-detection on this
    # same job. Physical limitation (P18g Finding 1, confirmed and
    # bounded in this session): under a severe-enough network
    # constraint, collectives become network-bound rather than compute-
    # bound, so a real compute fault produces no exec-time separation at
    # all -- there is nothing wrong with the detection logic, the signal
    # it looks for genuinely isn't there to find. A clean "no compute
    # finding" result on a job with a confirmed network fault must not
    # be presented as "no compute fault" without this caveat.
    if network_finding is not None:
        compute_findings_exist = any("rank" in f for f in findings)
        if not compute_findings_exist:
            network_finding["compute_fault_caveat"] = (
                "No compute-fault finding on this job, but a network fault is confirmed. Per P18g's "
                "three-simultaneous-faults test: under a severe enough network constraint, collectives "
                "become network-bound rather than compute-bound, and a real compute fault (e.g. a "
                "clock-locked rank) can produce NO exec-time separation at all. Treat compute-fault "
                "non-detection on this job as INCONCLUSIVE, not as a clean bill of health, until the "
                "network fault is resolved and compute detection can be re-run under normal conditions."
            )
        findings.append(network_finding)

    # --- host: cause-metric-first, checked independently of any
    # exec-time signal (P18f Stage 2 -- see check_host_contention_direct
    # for why: node_vs_node genuinely cannot see this fault class). When
    # this fires, drop any non-CONFIRMED per-rank finding on the SAME
    # node -- P18e found the continuous-burner test produces a spurious
    # "rank0, compute, PROBABLE" finding (rank0's own host-sensitive
    # bookkeeping overhead reacting to the contention), which is really
    # the same root cause misattributed, not a second independent fault.
    host_findings = check_host_contention_direct(buffer)
    for hf in host_findings:
        node = hf["host"]
        findings[:] = [f for f in findings
                        if not ("rank" in f and worker_of(f["rank"], rank_hosts) == node and f["tier"] != "CONFIRMED")]
        findings.append(hf)

    # --- node-vs-node: whole-node fault, in parallel, not gated. Kept as
    # a secondary signal only -- see check_host_contention_direct's
    # diagnosis for why it is not the primary host-detection path. ---
    nvn = primary["node_vs_node"]
    if abs(nvn["diff_sd_units"]) > THRESH["node_vs_node_sd"]:
        # Codebase audit, third pass -- was a hardcoded "worker-0"/
        # "worker-1" literal; score_node_vs_node now reports which REAL
        # discovered host each side of the comparison actually is
        # (worker0_host/worker1_host), so this names the real elevated
        # node instead of assuming a name.
        node = nvn["worker0_host"] if nvn["diff_sd_units"] < 0 else nvn["worker1_host"]
        flagged_ranks_on_node = {f["rank"] for f in findings if "rank" in f and worker_of(f["rank"], rank_hosts) == node}
        if flagged_ranks_on_node:
            # P18b fix: don't blanket-suppress just because SOME rank on
            # this node already has a per-rank finding -- re-test whether
            # the aggregate shift survives excluding that rank's own mean.
            # If it does, a separate host-level fault is also present on
            # this node and must be reported, not silently absorbed into
            # the per-rank finding (this is exactly the two-faults-one-
            # node case: a per-rank compute fault does not preclude an
            # independent host-level fault on the same node).
            recheck = recheck_node_vs_node_excluding(primary["mean"]["all_vals"], flagged_ranks_on_node, rank_hosts)
            already_explained = recheck is None or abs(recheck["diff_sd_units"]) <= THRESH["node_vs_node_sd"]
        else:
            already_explained = False
        if not already_explained:
            node_ts_range = None
            if rank_ts_range:
                node_ranks = [r for r in rank_ts_range if worker_of(r, rank_hosts) == node]
                if node_ranks:
                    node_ts_range = (min(rank_ts_range[r][0] for r in node_ranks),
                                      max(rank_ts_range[r][1] for r in node_ranks))
            findings.append(build_host_finding(node, nvn, dcgm_host_map, buffer=buffer, node_ts_range=node_ts_range))

    # P18g Stage 3: explicit data-insufficiency flag for CV below its
    # structural floor (2 windows' worth -- 250 samples at window=125).
    # Below that, CV's 2-of-3 persistence can mathematically never fire
    # regardless of signal strength, which must be reported as "not
    # enough data to trust this statistic," not silently returned as a
    # clean/no-finding result indistinguishable from a genuinely healthy
    # run with plenty of data. Mean has no such floor (fires off a
    # single 100-sample window) and is unaffected.
    cv_floor = 2 * STAT_WINDOW_SIZE["cv"]
    n_samples = min((len(v) for v in per_rank.values()), default=0)
    cv_status = "insufficient_data" if n_samples < cv_floor else "ok"
    cv_status_detail = None
    if cv_status == "insufficient_data":
        cv_status_detail = (f"only {n_samples} samples available, need >={cv_floor} for CV's "
                             "persistence check to be meaningful -- mean-based sustained-fault "
                             "detection still applies and is unaffected by this floor.")

    if not findings:
        result = {"status": "clean", "primary_scores": {k: primary[k] for k in ["mean", "cv", "outlier_count"]},
                  "cv_status": cv_status}
        if cv_status_detail:
            result["cv_status_detail"] = cv_status_detail
        return result, state

    # P18h Stage 3: cv_status_detail belongs here too -- found via the
    # live short-run test that a finding fired via mean alone (rank4,
    # 200 samples) reported cv_status="insufficient_data" but no detail
    # string, leaving the caller to guess why CV didn't also weigh in
    # instead of stating it plainly alongside the confirmed finding.
    result = {"status": "findings", "findings": findings, "cv_status": cv_status}
    if cv_status_detail:
        result["cv_status_detail"] = cv_status_detail
    return result, state


HEALTHY_MAX_Z = {
    # P18f Stage 4: raw z is NOT comparable across statistics with
    # different distributions -- outlier_count's z routinely reads
    # "inf" on clean healthy data (peer count=0), which would always
    # numerically dominate mean/cv's raw z regardless of which pattern
    # actually fits the fault. Normalize each stat's z by how far its
    # OWN healthy population sits from zero, so "how many multiples of
    # this stat's own noise floor" is what gets compared, not raw
    # magnitude. Values are each stat's measured n>=11 healthy max.
    "mean": 3.49, "cv": 9.7, "outlier_count": 5.0, "outlier_rate": 1.0,
    # outlier_count's z-SCORE healthy max (measured n=9: 4.91) is what
    # belongs here, NOT its raw-count healthy max (~2) -- found via the
    # transient case still mislabeling "medium/long-burst": outlier_count
    # can reach a large FINITE z (8487 observed) whenever peer_sd is
    # small-but-nonzero rather than exactly 0, which the raw-count-based
    # reference badly underestimated, letting it dominate mean/cv again.
}


def _normalized_margin(stat_name, result):
    z = result["z_node"]
    if z == float("inf"):
        # degenerate (peer variance/median exactly 0, common for rare-
        # event stats on clean data) -- fall back to the raw-value ratio
        # over this stat's own established absolute gate instead of an
        # incomparable infinity.
        raw = result.get("worst_val", result.get("worst_rate", 0))
        gate = THRESH.get(stat_name, THRESH.get(f"{stat_name}_mm", 1.0)) or 1.0
        return raw / gate if gate else raw
    return z / HEALTHY_MAX_Z.get(stat_name, 1.0)


def path_b_clock_cause(dcgm, local_idx):
    """Path B cause-dict construction (clock suppression, node-scoped: target
    GPU's sm_clock vs. the OTHER GPUs on the SAME NODE's own median, gated
    on the target being genuinely active by the same peer-relative power
    check ACTIVE_POWER_PEER_FRAC already uses). Factored out of
    build_single_rank_finding's live-query branch (P21.6.1) so a caller with
    NO NCCL peer-relative stat_hit at all -- the 2-member-communicator DCGM
    fallback, alert_engine.py's _dcgm_fallback_evaluate -- can reach the
    exact same, already-validated comparison instead of re-deriving it.
    Notably this comparison was NEVER actually scoped to "the other member
    of a specific communicator" -- it always compared against every other
    GPU on the physical node, via dcgm (all 8 GPUs, queried once per host).
    That means it was never structurally dependent on communicator size in
    the first place; the only real gap was that nothing ever CALLED it for
    a rank whose own communicator has too few members to produce the
    peer-relative NCCL z-score that normally triggers this call.
    Returns the path_b_clock sub-dict, or None if the comparison couldn't
    be computed (missing/non-numeric fields for this dcgm snapshot)."""
    try:
        sm_vals = {i: float(g["sm_clock"]) for i, g in dcgm.items() if g.get("sm_clock") not in (None, "N/A")}
        peers_sm = [v for i, v in sm_vals.items() if i != local_idx]
        power = float(dcgm.get(local_idx, {}).get("power_usage", "nan"))
        power_vals = {i: float(g["power_usage"]) for i, g in dcgm.items()
                      if g.get("power_usage") not in (None, "N/A")}
        peers_power = [v for i, v in power_vals.items() if i != local_idx]
        peer_median_power = _st.median(peers_power) if peers_power else None
        # P26 fix -- peer-relative, not the old absolute IDLE_POWER_W
        # (see ACTIVE_POWER_PEER_FRAC's own comment).
        genuinely_active = (peer_median_power is not None and
                             power > ACTIVE_POWER_PEER_FRAC * peer_median_power)
        return {
            "target_sm_clock": sm_vals.get(local_idx), "peer_median_sm_clock": _st.median(peers_sm) if peers_sm else None,
            "target_power": power, "peer_median_power": peer_median_power,
            "genuinely_active": genuinely_active,
            "source": "live_query",
        }
    except (TypeError, ValueError, KeyError):
        return None


def gather_path_c_storage(host, iowait_pid, iowait_log_dir, t_start=None, t_end=None):
    """Path C: storage, real eBPF io-wait evidence (P26.5-maintenance),
    extracted (P27.3-followup) from build_single_rank_finding so a caller
    with real per-member identity (host, real PID) but no rank/rank_ts_range
    of that function's own shape -- the below-floor P27.2 fallback in
    alert_engine.py, specifically -- can gather this SAME real evidence
    directly, rather than Path C staying reachable only through
    build_single_rank_finding's own entry point (which the below-floor
    fallback never calls, since it has no NCCL peer-relative stat_hits to
    feed it).

    Fully generic: no assumption about which rank/host this is, only that
    the caller knows this member's real (jailed) PID. Window: uses the
    real (t_start, t_end) fault window when both are given (the offline-
    replay / already-known-window case). Otherwise (t_start or t_end is
    None) falls back to a rolling recent window ending now -- the same
    "assume the fault is still ongoing" convention Path B's own live-query
    branch already uses for DCGM.

    Returns (checked_note_or_None, impossible_note_or_None,
    path_c_storage_dict_or_None) -- never raises; a caller merges these
    into its own cause["checked"]/["impossible"]/["path_c_storage"], the
    same three-part shape build_single_rank_finding already produces."""
    if iowait_pid is None:
        return None, ("storage (eBPF io-wait): no real PID identity available for this rank "
                       "-- caller did not supply iowait_pid"), None
    if t_start is None or t_end is None:
        _now = _time.time()
        t_start, t_end = _now - IOWAIT_LIVE_WINDOW_S, _now
    # P27.3-timing-gap investigation -- real, explicit trace of exactly
    # when and what window this live call queries, so a real gap between
    # "evidence became available" and "evidence was actually checked" can
    # be measured directly against iowait_logger.py's own real, persisted
    # timestamps, rather than inferred.
    print(f"[PATH_C_TRACE] query_time={_time.time():.3f} host={host} pid={iowait_pid} "
          f"window=[{t_start:.3f},{t_end:.3f}] live={t_start is not None}", flush=True)
    io_ev = storage_evidence.query_iowait_window(iowait_log_dir, host, iowait_pid, t_start, t_end) \
        if iowait_log_dir else None
    print(f"[PATH_C_TRACE] result_time={_time.time():.3f} host={host} pid={iowait_pid} "
          f"io_ev={io_ev}", flush=True)
    if io_ev is None:
        return None, (f"storage (eBPF io-wait) on {host}: no persisted iowait log for this "
                       f"host -- agent not deployed/running there, or iowait_log_dir not "
                       f"configured for this caller"), None
    checked_note = f"real eBPF block-I/O-wait for pid={iowait_pid} on {host} over [{t_start:.1f},{t_end:.1f}]"
    path_c = dict(io_ev, pid=iowait_pid, host=host, t_start=t_start, t_end=t_end)
    return checked_note, None, path_c


def build_single_rank_finding(rank, stat_hits, primary, corroborating, dcgm_host_map, ib_hosts,
                               buffer=None, rank_ts_range=None, iowait_pid=None, iowait_log_dir=None,
                               host=None, local_idx=None, rank_hosts=None):
    # P18f Stage 4 fix: rank fired statistics by how many multiples of
    # EACH statistic's OWN healthy noise floor it cleared, not by raw
    # z-score -- raw z compared outlier_count's near-degenerate "inf"
    # against mean/cv's normal-distributed z, which isn't a meaningful
    # comparison and produced "medium/long-burst" labels for faults
    # that were conceptually intermittent whenever outlier_count merely
    # CO-FIRED, even weakly, alongside a much more decisively-crossed CV.
    stat_hits = sorted(stat_hits, key=lambda sh: _normalized_margin(sh[0], sh[1]), reverse=True)
    stat_name, result = stat_hits[0]
    timescale = STAT_TO_TIMESCALE[stat_name]
    # Report every OTHER stat that fired as its own additional label --
    # fired() already gates each stat on its own validated threshold, so
    # any stat in stat_hits at all is independently real signal, not
    # noise riding along with the primary one. (An earlier attempt
    # required each additional stat's margin to be within 50% of the
    # top one's -- too strict: it silently dropped mean+cv from the
    # transient case even though both are real, just each less extreme,
    # in normalized terms, than outlier_count's genuinely stronger
    # signal for that specific fault.) A fault can be both intermittent
    # and produce severe outliers; report both labels when both fired.
    additional_signatures = []
    seen = {timescale}
    for sn, r in stat_hits[1:]:
        label = STAT_TO_TIMESCALE[sn]
        if label not in seen:
            additional_signatures.append(label)
            seen.add(label)
    # Cluster-topology-agnostic fix (this session): host/local_idx used
    # to ALWAYS be derived from `rank` via worker_of(rank)/rank%8 --
    # hardcoded to this cluster's own 2-node/8-GPU-per-node shape, and
    # (via alert_engine.py's build_finding_for_alert, the LIVE production
    # path) the ONLY thing standing between a real (hostname, real GPU
    # slot) identity that adapter ALREADY has and a hardcoded arithmetic
    # translation of it into a synthetic "rank" that could silently
    # target the wrong node/slot on any cluster shaped differently than
    # 2x8. A caller who already has the real host/slot (host=, local_idx=)
    # now passes them straight through, no translation needed at all.
    # rank%8/worker_of(rank) remain the fallback ONLY for callers that
    # still only have a flat global rank and no direct identity (the
    # offline/buffer-replay engine's own rank-based data model).
    #
    # Codebase audit, third pass -- worker_of(rank) used to always fall
    # back to the hardcoded rank<8 split here; now uses this call's own
    # real, discovered rank_hosts (classify_incremental's own top-level
    # discover_rank_hosts call, threaded through) when the caller has it,
    # closing this specific gap the same way alert_engine.py's own
    # build_finding_for_alert already closes it for the live path (by
    # passing real host= directly rather than relying on this fallback at
    # all). local_idx=rank%8 is intentionally NOT touched here -- a
    # separate, narrower GPU-slot identity question this pass didn't
    # scope in (this offline package's own dump records don't uniformly
    # carry gpu_slot_index the way the live path's do; see this session's
    # audit for why that's a distinct, separately-tracked gap).
    if host is None:
        host = worker_of(rank, rank_hosts) if rank_hosts else None
    if local_idx is None:
        local_idx = rank % 8
    evidence = {
        "statistic": stat_name, "z": result["z_node"], "mm": result["maxmed_node"],
        "worst_val": result["worst_val"], "peer_mean": result["peer_mean"],
        "all_fired_stats": [sn for sn, _ in stat_hits],
    }
    corroborating_evidence = {}
    for bucket, cr in corroborating.items():
        # P18c: look up THIS rank's own node in the corroborating bucket,
        # not its global winner -- same fix as the primary-bucket one
        # above. Otherwise a second, independent finding on the other
        # node (e.g. rank12 when rank4 also fired) would display rank4's
        # bucket-C reading as its own "corroborating" evidence.
        cs = cr.get(stat_name + "_per_node", {}).get(host)
        if cs:
            corroborating_evidence[bucket] = {"z": cs["z_node"], "mm": cs["maxmed_node"], "worst_rank": cs["worst_rank"]}

    cause = {"class1": {}, "class2": {}, "checked": [], "impossible": [],
             "path_a_thermal": {}, "path_b_clock": {}, "path_c_storage": {}}

    # --- Path C: storage, real eBPF io-wait evidence (P26.5-maintenance)
    # ---
    # P27.3-followup -- extracted into gather_path_c_storage() (pure
    # extraction, no behavior change here) so the below-floor P27.2
    # fallback in alert_engine.py -- which has real per-member identity
    # (host, real PID) but no `rank`/`rank_ts_range` of this function's
    # own shape -- can call the exact same real evidence-gathering code
    # directly, instead of Path C staying reachable only through this
    # rank/rank_ts_range-shaped entry point. See that function's own
    # docstring for the full reasoning this comment used to carry.
    t_start, t_end = (rank_ts_range[rank] if rank_ts_range and rank in rank_ts_range else (None, None))
    checked_note, impossible_note, path_c = gather_path_c_storage(host, iowait_pid, iowait_log_dir, t_start, t_end)
    if checked_note:
        cause["checked"].append(checked_note)
    if impossible_note:
        cause["impossible"].append(impossible_note)
    if path_c:
        cause["path_c_storage"] = path_c
    # Codebase audit, third pass -- host can now genuinely be None (this
    # rank's real host wasn't in the caller's own discovered rank_hosts,
    # e.g. no dump data for it at all) where it never was before (the old
    # worker_of(rank) fallback always returned a real string, right or
    # wrong). Guarded here so that missing-identity case degrades to the
    # same honest "impossible" reason every other unobtainable-cause case
    # in this function already uses, instead of crashing inside ssh()
    # with a None host argument.
    if host is None:
        cause["impossible"].append("DCGM: this rank's real host is not known (no discovered identity)")
    elif dcgm_host_map:
        dcgm, err = query_dcgm_all_gpus(host)
        # P27-hotfix -- query_dcgm_all_gpus can return dcgm=None with err
        # falsy (confirmed live: ssh() succeeds, but dcgmi's own failure
        # text lands on stdout, not stderr, so `err` alone doesn't catch
        # every failure). Matching the same `err or not <data>` guard
        # already used elsewhere in this file (build_finding_for_alert's
        # IB/NVLink checks, alert_engine.py's own DCGM fallback) --
        # degrading to an honest UNCONFIRMED "impossible" reason instead
        # of crashing on dcgm.get(...) when dcgm is genuinely unavailable.
        # No change in behavior when DCGM IS available: dcgm is then a
        # real non-empty dict and err is empty, so this still takes the
        # else branch exactly as before.
        if err or not dcgm:
            cause["impossible"].append(f"DCGM on {host}: {err or 'no data returned'}")
        else:
            gpu = dcgm.get(local_idx, {})
            cause["checked"].append("DCGM clocks/throttle/thermal/power")
            cause["class1"]["sm_clock"] = gpu.get("sm_clock")
            cause["class1"]["throttle_reasons"] = decode_throttle(int(gpu.get("throttle_reasons", "0") or 0))
            for f in ["gpu_temp", "mem_temp", "power_usage", "pcie_replay", "xid_errors",
                      "ecc_sbe_vol", "ecc_dbe_vol", "retired_sbe", "retired_dbe", "retired_pending",
                      "nvlink_crc_total"]:
                val = gpu.get(f)
                if val in (None, "N/A"):
                    cause["impossible"].append(f"{f} on {host} GPU{local_idx}")
                else:
                    cause["class2"][f] = val

            # --- Path B: clock suppression, node-scoped ---
            pb = path_b_clock_cause(dcgm, local_idx)
            if pb is not None:
                cause["path_b_clock"] = pb
            else:
                cause["impossible"].append(f"path B clock comparison on {host}")

    # P18c Stage 1: rolling-buffer lookback OVERRIDES the live-query Path B
    # reading above when available -- power/clock revert to idle within
    # seconds of the workload stopping, so a live query issued after
    # classify() is called (i.e. after the whole job, and the fault
    # within it, have ended) reliably misses a real fault. The buffer was
    # recorded continuously throughout the run, so it sees the fault
    # regardless of when classify() itself happens to run.
    if buffer is not None and rank_ts_range and rank in rank_ts_range:
        gpu_buf, host_buf, _ib_buf = buffer
        t_start, t_end = rank_ts_range[rank]
        win = query_gpu_window(gpu_buf, host, local_idx, t_start, t_end)
        # Cluster-topology-agnostic fix (this session): peer GPU indices
        # used to be a hardcoded range(8) -- silently missing real GPUs
        # beyond index 7 on a node with more, silently querying
        # nonexistent indices on a node with fewer. gpu_buf's own real
        # keys (populated by rolling_buffer.py's _sample_host_dcgm, which
        # already discovers real GPU indices live via query_dcgm_all_gpus
        # -- never a hardcoded range itself) are this host's own real,
        # actually-recorded GPU set -- used directly instead.
        real_gpu_idxs = {g for (h, g) in gpu_buf if h == host}
        peer_wins = {i: query_gpu_window(gpu_buf, host, i, t_start, t_end) for i in real_gpu_idxs if i != local_idx}
        peer_clocks = [w["median_sm_clock"] for w in peer_wins.values() if w and w["median_sm_clock"] is not None]
        # P26 fix -- peer-relative, not the old absolute IDLE_POWER_W (see
        # ACTIVE_POWER_PEER_FRAC's own comment). MAX power per peer window,
        # same reasoning as the target's own max-over-window below.
        peer_powers = [w["max_power"] for w in peer_wins.values() if w and w["max_power"] is not None]
        peer_median_power = _st.median(peer_powers) if peer_powers else None
        if win is not None:
            cause["checked"].append(f"rolling buffer lookback ({win['n']} samples over [{t_start:.1f},{t_end:.1f}])")
            cause["path_b_clock"] = {
                "target_sm_clock": win["median_sm_clock"],
                "peer_median_sm_clock": _st.median(peer_clocks) if peer_clocks else None,
                # MAX power over the window, not mean -- a fault window
                # spans idle-startup + active-compute + (for a released
                # transient fault) idle-again; max power catches whether
                # the GPU was EVER genuinely active in that window, which
                # a mean would dilute exactly like the whole-run mm dilution
                # problem found in P18b Stage 2c.
                "target_power": win["max_power"], "peer_median_power": peer_median_power,
                "genuinely_active": (win["max_power"] is not None and peer_median_power is not None and
                                      win["max_power"] > ACTIVE_POWER_PEER_FRAC * peer_median_power),
                "source": "rolling_buffer",
            }
        else:
            cause["impossible"].append(f"rolling buffer has no samples for {host} GPU{local_idx} in fault window")

        # --- Path A: thermal degradation, node-scoped ---
        thermal, terr = query_thermal_slowdown_all_gpus(host)
        if terr:
            cause["impossible"].append(f"thermal-slowdown counters on {host}: {terr}")
        else:
            cause["checked"].append("cumulative SW thermal-slowdown counters (all node GPUs)")
            # P27-hotfix -- query_thermal_slowdown_all_gpus stores an
            # explicit None (not a missing key) for a GPU whose per-GPU
            # nvidia-smi query failed (rc != 0). dict.get(key, {}) only
            # falls back to {} when the key is ABSENT -- a present key
            # with value None still returns None here, which would then
            # crash on the next .get() the same way the DCGM site did.
            target_sw = (thermal.get(local_idx) or {}).get("sw_thermal_us")
            # Same present-but-None exposure for peer entries -- v can be
            # None for any peer GPU whose own nvidia-smi query failed.
            peer_sw = [(v or {})["sw_thermal_us"] for i, v in thermal.items()
                       if i != local_idx and (v or {}).get("sw_thermal_us") is not None]
            peer_median = _st.median(peer_sw) if peer_sw else 0
            cause["path_a_thermal"] = {
                "target_sw_thermal_s": (target_sw or 0) / 1e6,
                "peer_median_sw_thermal_s": peer_median / 1e6,
                "peer_max_sw_thermal_s": max(peer_sw) / 1e6 if peer_sw else 0,
            }

        # TFLOPS cross-check (needed by both paths, esp. to reject GPU4-like
        # false positives: nonzero thermal counter but normal throughput)
        tflops, ferr = query_matmul_tflops(host)
        if ferr:
            cause["impossible"].append(f"isolated matmul TFLOPS on {host}: {ferr}")
        else:
            cause["checked"].append("isolated matmul TFLOPS (all node GPUs)")
            vals = {int(k): v["tflops"] for k, v in tflops.items()}
            median = _st.median(vals.values())
            target_tf = vals.get(local_idx)
            dev_pct = (target_tf - median) / median * 100 if target_tf else None
            cause["tflops"] = {"target": target_tf, "node_median": median, "deviation_pct": dev_pct}

    return {
        "rank": rank, "host": host, "type_candidate": "compute", "timescale": timescale,
        "additional_signatures": additional_signatures,
        "evidence": evidence, "corroborating": corroborating_evidence, "cause": cause,
        "tier": determine_tier_single_rank(cause),
    }


def determine_tier_single_rank(cause):
    """Multi-path CONFIRMED logic (P18b, +Path C P26.5-maintenance). Each
    path requires TWO corroborating signals -- neither the thermal counter
    nor the clock reading alone is sufficient (GPU4 has a nonzero thermal
    counter with completely normal throughput; that must NOT confirm on
    its own).

    Path C (storage) is deliberately NOT added to the PROBABLE-eligibility
    check below, unlike Path A/tflops: those two are genuinely partial/
    incomplete signals even when "checked" (Path A needs a SEPARATE tflops
    cross-check to confirm, so "thermal data present, tflops missing" is a
    real ambiguous middle state) -- storage's own check
    (query_iowait_window + determine_storage_path) always returns a
    definitive verdict once real data exists: either both signals clear
    (CONFIRMED, handled above) or they don't, which is a clean, real
    RULING OUT, not an ambiguous partial result. Elevating a clean rule-out
    to PROBABLE would also silently defeat this fix's whole point: it
    would skip build_review_lists() entirely (format_finding only calls it
    for UNCONFIRMED), burying the explicit "storage ruled out" disclosure
    this fix exists to surface."""
    path = determine_confirmed_path(cause)
    if path is not None:
        return "CONFIRMED"
    if cause["class1"] or cause.get("path_a_thermal") or cause.get("tflops"):
        return "PROBABLE"
    return "UNCONFIRMED"


def determine_confirmed_path(cause):
    """Returns the path name ('A'/'B'/'C') if a CONFIRMED path fires, else None."""
    # --- Path A: thermal degradation ---
    pa = cause.get("path_a_thermal", {})
    tf = cause.get("tflops", {})
    if pa and tf:
        target_sw = pa.get("target_sw_thermal_s", 0)
        peer_median_sw = pa.get("peer_median_sw_thermal_s", 0)
        MIN_ABS_THERMAL_S = 60.0  # bug fix: when peer_median is 0 (the
        # common case -- most GPUs never accumulate this counter), a
        # ratio check alone is trivially satisfied by ANY nonzero target
        # value. Require a real absolute accumulation too (GPU4's 2847s
        # would still pass this alone -- it's the TFLOPS gate below that
        # correctly excludes it, but this closes the ratio loophole).
        ratio_ok = (target_sw > MIN_ABS_THERMAL_S and
                    (peer_median_sw == 0 or target_sw / max(peer_median_sw, 1e-9) > THERMAL_RATIO_MIN))
        dev_pct = tf.get("deviation_pct")
        tflops_ok = dev_pct is not None and dev_pct < -TFLOPS_DEVIATION_PCT
        if ratio_ok and tflops_ok:
            return "A"

    # --- Path B: clock suppression (throttle reasons NOT used -- verified
    # unusable: a manual clock lock shows every reason as "Not Active"
    # even under active load) ---
    pb = cause.get("path_b_clock", {})
    if pb:
        sm, peer_sm = pb.get("target_sm_clock"), pb.get("peer_median_sm_clock")
        active = pb.get("genuinely_active")
        if sm is not None and peer_sm and active:
            if sm < PATH_B_AND_TIMING_SUPPRESS_RATIO * peer_sm:  # target clock well below its own node peers
                return "B"

    # --- Path C: storage, real eBPF block-I/O-wait evidence
    # (P26.5-maintenance) -- reuses storage_evidence's own two-signal
    # check (real absolute floor AND substantial relative to the window's
    # own duration) rather than reimplementing it here, same as this
    # function calls out to path_b_clock_cause elsewhere. ---
    pc = cause.get("path_c_storage")
    if pc and storage_evidence.determine_storage_path(pc) is True:
        return "C"
    return None


def build_review_lists(finding):
    """Derives ruled_out / impossible / class2_flags / next_steps from what
    was actually checked, for an UNCONFIRMED (or downgraded) finding. Never
    invents a check that wasn't run."""
    ruled_out, class2_flags = [], []
    c1, c2 = finding["cause"]["class1"], finding["cause"]["class2"]
    impossible = list(finding["cause"]["impossible"])

    is_single_rank = "rank" in finding
    if is_single_rank:
        sm_clock = c1.get("sm_clock")
        throttle = c1.get("throttle_reasons")
        if sm_clock is not None:
            try:
                if int(sm_clock) > 1000 and (not throttle or throttle == ["gpu_idle"] or throttle == ["none"]):
                    ruled_out.append("DCGM: clocks nominal, no throttle reasons set -> not thermal or power (at query time)")
            except (TypeError, ValueError):
                pass
        ruled_out.append("Pattern is single-rank, not whole-node -> not a node-level fault")
        ruled_out.append("No global drift observed -> not network")
    else:
        load = c1.get("load_snapshot")
        if load:
            ruled_out.append(f"Host load snapshot captured ({load.splitlines()[0] if load else 'n/a'}) -- "
                              f"compare against known-good baseline, not yet a stored range")

    for field, label in [("gpu_temp", "elevated GPU temperature"), ("mem_temp", "elevated memory temperature"),
                          ("pcie_replay", "PCIe replay events"), ("ecc_sbe_vol", "ECC single-bit errors"),
                          ("ecc_dbe_vol", "ECC double-bit errors")]:
        val = c2.get(field)
        if val is not None:
            try:
                if float(val) > 0 and field not in ("gpu_temp", "mem_temp"):
                    class2_flags.append(f"{label}: {val} (not validated as a cause -- worth a look)")
            except (TypeError, ValueError):
                pass

    next_steps = []
    if is_single_rank:
        # P26.5-maintenance -- real eBPF storage-vs-data-pipeline check,
        # replacing the old blind punt ("eBPF unavailable in-container",
        # no longer true -- confirmed working again this session, and
        # actually wired to a real answer here) with the real,
        # checked verdict. Path C is never CONFIRMED by the time we reach
        # here (determine_tier_single_rank would have returned CONFIRMED
        # instead, skipping build_review_lists entirely) -- so
        # determine_storage_path's result here is only ever None (not
        # checkable) or False (checked, ruled out), never True.
        pc = finding["cause"].get("path_c_storage")
        verdict = storage_evidence.determine_storage_path(pc) if pc else None
        if verdict is False:
            ruled_out.append(
                f"Real eBPF block-I/O-wait for rank {finding['rank']}'s own process "
                f"(pid={pc.get('pid')}) on {pc.get('host')}: "
                f"{pc.get('target_iowait_us', 0)}us aggregated over the "
                f"[{pc.get('t_start', 0):.1f},{pc.get('t_end', 0):.1f}] window -- "
                f"below the real threshold for a genuine storage stall -> not local-disk "
                f"storage. Data-pipeline (uneven shard sizes, a slow non-disk data source) "
                f"remains the candidate this check cannot rule in or out."
            )
        else:
            reason = ("no persisted iowait log for this host (agent not deployed/running "
                      "there, or this caller didn't supply iowait_log_dir)" if pc is None and
                      any("storage" in s for s in finding["cause"].get("impossible", [])) else
                      "no real PID identity available for this rank")
            next_steps.append(
                f"Storage could not be checked directly for rank {finding['rank']} ({reason}). "
                f"This eBPF approach only sees LOCAL DISK I/O (block_rq_issue/complete "
                f"tracepoints) -- confirmed this session that it produces ZERO events for "
                f"virtiofs or S3-backed reads, by design (neither goes through the kernel "
                f"block layer), so a negative result there would not be meaningful anyway; "
                f"only a genuinely local-disk-backed rank can be ruled in or out this way."
            )
            next_steps.append("If the workload does per-rank data loading, check for uneven shard sizes "
                               "or a slow data source on that rank -- indistinguishable from storage stalls "
                               "without a real eBPF answer; report BOTH as candidates")
        next_steps.append(f"`nvidia-smi -q -d PERFORMANCE` on rank {finding['rank']}'s GPU for anything DCGM didn't surface")
        next_steps.append("If it recurs, enable verbose Inspector dumps on that rank for per-collective detail")
    else:
        next_steps.append(f"Check for co-tenant processes on {finding['host']} (load average, process count)")
        next_steps.append(f"Check host memory pressure / swap / page-cache behaviour on {finding['host']}")
        next_steps.append(f"Check IRQ/softirq time on {finding['host']} to distinguish host-busy from host-busy-because-of-network")

    return ruled_out, impossible, class2_flags, next_steps


HOST_LOAD_RATIO_MIN = 4.0  # P18f Stage 2 recalibration. Was 2.0, n=0
                           # healthy coverage (never measured on a healthy
                           # run at all). Re-measured across n=9 healthy-
                           # host buffer samples (runs with a compute-only
                           # or network-only fault, or fully healthy, but
                           # NO host contention): max observed ratio 2.37
                           # (rank0_test) -- ABOVE the old threshold of
                           # 2.0, which would have false-fired. True
                           # positives (continuous burner: 9.95, duty-
                           # cycled burner: 7.58) sit comfortably above
                           # 4.0. VALIDATED, n=9.


def check_host_contention_direct(buffer, hosts=None):
    """P18f Stage 2: cause-metric-FIRST host detection, replacing the
    node_vs_node-gated design entirely for the purpose of NAMING a host
    fault (node_vs_node is kept below only as a secondary, independent
    signal -- it has never once separated a host fault from healthy, at
    any burner intensity tested, so it cannot be the primary trigger).

    Diagnosis (P18f): raw per-rank exec-time means during the continuous-
    burner test were statistically indistinguishable between the
    contended node and the clean one (~3600 vs ~3600), and inter-
    collective gap time showed the same non-result (~19810us both
    nodes). Host CPU contention on this hardware/workload genuinely does
    not perturb NCCL-observable timing -- not the collective's own exec
    time, not the gap between collectives. It DOES show up directly and
    strongly in host CPU load (ratio >7x for both burner shapes tested),
    so that is now the primary signal, checked independently of any
    arrival-lag statistic, exactly the way Path A's thermal counter is
    checked independently of arrival-lag.

    P20d-closeout Part B: added a live-query fallback (hosts=), mirroring
    check_network_contention_direct's buffer-or-live design exactly. A
    caller with no rolling buffer (e.g. alert_engine.py, which never
    maintained one) can still run this validated, primary-signal check
    live via query_host_cpu -- the SAME live-capable primitive
    build_host_finding's dcgm_host_map branch already used, reused
    unchanged here rather than reinvented, gated by the SAME validated
    HOST_LOAD_RATIO_MIN=4.0 threshold this function already applies in
    its buffer path. This is a single live snapshot comparison per node
    (no windowed max/mean smoothing the way the buffer path gets) -- a
    real, disclosed simplification, not a hidden one."""
    if buffer is not None:
        _, host_buf, _ = buffer
        findings = []
        # Codebase audit, third pass -- was a hardcoded ("worker-0",
        # "worker-1") literal; now the real, discovered host list this
        # buffer actually contains (rolling_buffer.py's own live-
        # discovered keys), sorted for a deterministic order. The "other"
        # comparison below is inherently pairwise (one node vs. the
        # other) -- a real, separate statistical-design question this
        # fix doesn't answer for a cluster with more than 2 real hosts
        # (same disclosed scope as score_node_vs_node's identical fix in
        # detection.py) -- a node with anything other than exactly one
        # real "other" host is honestly skipped, never mis-paired.
        real_hosts = sorted(host_buf.keys())
        for node in real_hosts:
            others = [h for h in real_hosts if h != node]
            other = others[0] if len(others) == 1 else None
            if other is None or node not in host_buf or other not in host_buf or not host_buf[node]:
                continue
            # P18h Stage 3 bug fix: was scoped to the WHOLE recorded buffer
            # range (job start to now). Found via the mid-run fault-type-
            # change test: max_load_per_core over "everything so far" never
            # decreases once a real spike occurs, so a host fault that ended
            # minutes earlier was STILL reported CONFIRMED at every later
            # check -- never transitioning away from a resolved fault.
            # Scope to a recent window instead.
            t1 = max(r[0] for r in host_buf[node])
            t0 = max(min(r[0] for r in host_buf[node]), t1 - HOST_CHECK_RECENT_WINDOW_S)
            win_a = query_host_window(host_buf, node, t0, t1)
            win_b = query_host_window(host_buf, other, t0, t1)
            if not win_a or not win_b or win_b["mean_load_per_core"] <= 0:
                continue
            ratio = win_a["max_load_per_core"] / win_b["mean_load_per_core"]
            if ratio > HOST_LOAD_RATIO_MIN:
                findings.append({
                    "host": node, "type_candidate": "host", "timescale": "sustained/whole-node",
                    "evidence": {"affected_load_per_core": win_a["max_load_per_core"],
                                 "other_load_per_core": win_b["mean_load_per_core"], "ratio": ratio},
                    "cause": {"checked": [f"rolling buffer host-load, {node} vs {other} "
                                           f"({win_a['n']}/{win_b['n']} samples)"],
                              "impossible": [], "path_c_host": {"ratio": ratio, "source": "rolling_buffer"}},
                    "tier": "CONFIRMED",
                })
        return findings

    if not hosts or len(hosts) < 2:
        return []
    ratios = live_host_load_ratios(hosts)
    findings = []
    for node, r in ratios.items():
        if r["ratio"] > HOST_LOAD_RATIO_MIN:
            findings.append({
                "host": node, "type_candidate": "host", "timescale": "sustained/whole-node",
                "evidence": {"affected_load_per_core": r["load"], "other_load_per_core": r["other_mean"],
                             "ratio": r["ratio"]},
                "cause": {"checked": [f"live query_host_cpu, {node} vs other node(s), single snapshot"],
                          "impossible": [], "path_c_host": {"ratio": r["ratio"], "source": "live_query"}},
                "tier": "CONFIRMED",
            })
    return findings


def live_host_load_ratios(hosts):
    """P20d-hardening: factored out of check_host_contention_direct's live
    branch so a persistence-aware caller (alert_engine.py) can get every
    host's raw ratio every cycle -- not just the ones already over
    threshold -- which check_host_contention_direct's own filtered
    return value can't provide. A host that's genuinely under threshold
    needs its OWN ratio recorded as a real "not fired" tick for
    persistence tracking to correctly distinguish two separate above-
    threshold episodes from one continuous one; silently skipping it
    (as omission from a pre-filtered list would) loses that distinction.
    check_host_contention_direct's own behavior/contract is unchanged --
    this is a new, additional entry point, not a replacement."""
    loads = {}
    for h in hosts:
        cpu, err = query_host_cpu(h)
        if err or cpu is None:
            continue
        loads[h] = cpu["load_per_core"]
    ratios = {}
    for node, load in loads.items():
        others = [v for h, v in loads.items() if h != node]
        if not others:
            continue
        other_mean = sum(others) / len(others)
        if other_mean <= 0:
            continue
        ratios[node] = {"load": load, "other_mean": other_mean, "ratio": load / other_mean}
    return ratios


def build_host_finding(node, nvn, dcgm_host_map, buffer=None, node_ts_range=None):
    """Path C: node aggregate elevated + no within-node outlier (already
    established by the caller not double-counting) + the affected node's
    CPU load is elevated relative to the OTHER node -> CONFIRMED.
    Evidence needed and NOT currently obtainable: a stored per-node
    baseline load range (what's "normal" for THIS node during THIS
    workload's data-loading phases) -- without it we can only compare
    node-to-node, which conflates "busier because of a host fault" with
    "busier because that node happens to do more I/O for this job shape."
    That's a real, disclosed limitation, not a solved problem.

    P18c Stage 1: prefers a rolling-buffer lookback over the fault's own
    wall-clock window (host load reverts to idle just like GPU power once
    the contention source stops) -- falls back to a live snapshot only
    when no buffer was supplied.

    Codebase audit, third pass -- other_node used to be a hardcoded
    "worker-1" if node=="worker-0" else "worker-0" literal; now derived
    from whichever real host list this call actually has available (the
    buffer's own host_buf keys, or dcgm_host_map's keys for the live-
    query path) -- the same "exactly one other real host, or honestly
    not checkable" pattern already used elsewhere in this pass's fix
    (score_node_vs_node, check_host_contention_direct)."""
    real_hosts = set(buffer[1].keys()) if buffer is not None else (set(dcgm_host_map.keys()) if dcgm_host_map else set())
    other_candidates = [h for h in real_hosts if h != node]
    other_node = other_candidates[0] if len(other_candidates) == 1 else None
    cause = {"class1": {}, "class2": {}, "checked": [], "impossible": [], "path_c_host": {}}
    if other_node is None:
        cause["impossible"].append("host CPU load comparison: could not determine a single real 'other' "
                                     "host to compare against from the data available")
    elif buffer is not None and node_ts_range:
        _, host_buf, _ib_buf2 = buffer
        t_start, t_end = node_ts_range
        win_a = query_host_window(host_buf, node, t_start, t_end)
        win_b = query_host_window(host_buf, other_node, t_start, t_end)
        if win_a and win_b:
            cause["checked"].append(f"rolling buffer host-load lookback, {node} vs {other_node} "
                                     f"({win_a['n']}/{win_b['n']} samples over [{t_start:.1f},{t_end:.1f}])")
            cause["path_c_host"] = {
                # MAX load-per-core over the window, not mean -- same
                # dilution concern as Path B's power: the fault window
                # spans idle-before/active-during/idle-after, and a mean
                # would understate the contention that was actually there.
                "affected_load_per_core": win_a["max_load_per_core"],
                "other_load_per_core": win_b["mean_load_per_core"],
                "ratio": (win_a["max_load_per_core"] / win_b["mean_load_per_core"]
                          if win_b["mean_load_per_core"] > 0 else float("inf")),
                "source": "rolling_buffer",
            }
        else:
            cause["impossible"].append("rolling buffer has no host-load samples in fault window")
    elif dcgm_host_map:
        cpu_a, err_a = query_host_cpu(node)
        cpu_b, err_b = query_host_cpu(other_node)
        # P27-hotfix -- same class of bug as the DCGM crash: query_host_cpu
        # returns (None, err) on ssh()/parse failure, and err can be
        # falsy even when the query genuinely failed. Guard on the data
        # itself, not just the error string, before subscripting it below.
        if err_a or err_b or cpu_a is None or cpu_b is None:
            cause["impossible"].append(f"host CPU load comparison: {err_a or err_b or 'no data returned'}")
        else:
            cause["checked"].append(f"host load average, {node} vs {other_node} (no stored baseline)")
            cause["class1"]["load_snapshot"] = cpu_a
            cause["path_c_host"] = {
                "affected_load_per_core": cpu_a["load_per_core"],
                "other_load_per_core": cpu_b["load_per_core"],
                "ratio": cpu_a["load_per_core"] / cpu_b["load_per_core"] if cpu_b["load_per_core"] > 0 else float("inf"),
                "source": "live_query",
            }
    tier = "UNCONFIRMED"
    if cause["path_c_host"]:
        if cause["path_c_host"]["ratio"] > HOST_LOAD_RATIO_MIN:
            tier = "CONFIRMED"
        else:
            tier = "PROBABLE"
    elif cause["class1"]:
        tier = "PROBABLE"
    return {
        "host": node, "type_candidate": "host", "timescale": "sustained/whole-node",
        "evidence": {"diff_sd_units": nvn["diff_sd_units"], "worker0_mean": nvn["worker0_mean"],
                     "worker1_mean": nvn["worker1_mean"]},
        "cause": cause,
        "tier": tier,
    }
