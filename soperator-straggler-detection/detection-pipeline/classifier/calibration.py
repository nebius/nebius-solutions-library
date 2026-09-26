#!/usr/bin/env python3
"""Stage 2: per-job-shape calibration with contamination screening.

Procedure:
  1. Characterise the bucket profile from a healthy run.
  2. Screen EVERY bucket for contamination on healthy data, with every
     statistic. A bucket that fires strongly with nothing injected is
     disqualified -- this is what would have caught bucket A.
  3. (if available) run a known injection + clean null; score every
     surviving (non-contaminated) bucket with each statistic.
  4. Select a bucket+statistic pair PER FAULT CLASS (mean/CV/outlier_count
     are not interchangeable -- calibration output is a set).
  5. Remaining surviving buckets are recorded as corroborating, not trusted.
  6. Store a bucket signature (size, count, rate) so drift can be flagged.

Also: healthy-only calibration (no injection) -- rank buckets by null
noise alone and compare against the injection-calibrated answer.
"""
import glob
import json
import statistics as st
from collections import Counter, defaultdict

from detection import (per_rank_series_by_comm, score_node_scoped, STATS,
                        EXCLUDE_ALWAYS, EXCLUDE_OUTLIER_EXTRA, STARTUP_SIZES)

# a bucket "fires strongly" on healthy data (contamination) if any
# statistic's max/median exceeds this on a run with nothing injected.
CONTAMINATION_MM_THRESHOLD = 3.0


def discover_buckets(dump_dirs, min_count=100):
    """Characterise the message-size / bucket profile from a run. Filters
    out startup artifacts and anything with too few samples to be a real
    per-step bucket (vs. a one-off)."""
    sizes = Counter()
    for dd in dump_dirs:
        for f in sorted(glob.glob(f"{dd}/*.log")):
            with open(f) as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cp = rec["coll_perf"]
                    if cp["coll"] != "AllReduce" or cp["coll_sn"] == 0:
                        continue
                    if cp["coll_msg_size_bytes"] in STARTUP_SIZES:
                        continue
                    sizes[cp["coll_msg_size_bytes"]] += 1
    return {sz: cnt for sz, cnt in sizes.items() if cnt >= min_count}


def screen_contamination(dump_dirs, buckets):
    """Score every candidate bucket on HEALTHY data with every statistic.
    A bucket disqualifies itself if any statistic's mm exceeds threshold.

    Cluster-topology-agnostic fix (this session): the old coverage check
    compared against a hardcoded N_RANKS=16 -- the exact same bug class
    already fixed in per_rank_series_by_comm's own real coverage check
    (see that function's own docstring: "confirmed broken the moment more
    than one communicator exists... would break differently again for a
    job of any other total rank count"), just not yet applied here. Now
    goes through per_rank_series_by_comm directly (not the single-
    communicator per_rank_series back-compat wrapper, which discards the
    real n_ranks it already computes) and compares each discovered
    communicator's own real n_ranks (NCCL/Inspector's own self-reported
    communicator size, read from that communicator's own records) --
    never a hardcoded number. A bucket can span more than one real
    communicator (e.g. TP's own bucket vs DP's own bucket happening to
    share a message size) -- each is screened against ITS OWN real
    expected count, matching score_bucket's own established per-comm
    design elsewhere in this file's sibling module."""
    report = {}
    for bucket in buckets:
        by_comm = per_rank_series_by_comm(dump_dirs, {bucket})
        if not by_comm:
            report[bucket] = {"disqualified": True, "reason": "no communicator found for this bucket"}
            continue
        bucket_report = {"disqualified": False, "stats": {}, "comms": {}}
        for comm_key, (per_rank, n_ranks, _dropped) in by_comm.items():
            if n_ranks is None or len(per_rank) < n_ranks:
                bucket_report["disqualified"] = True
                bucket_report["reason"] = (f"comm={comm_key}: incomplete coverage "
                                            f"({len(per_rank)}/{n_ranks if n_ranks is not None else '?'} real ranks)")
                continue
            comm_stats = {}
            for stat_name in STATS:
                r = score_node_scoped(per_rank, stat_name)
                if r is None:
                    continue
                comm_stats[stat_name] = {"worst_rank": r["worst_rank"], "mm": r["maxmed_node"], "z": r["z_node"]}
                if r["maxmed_node"] not in (float("inf"),) and r["maxmed_node"] > CONTAMINATION_MM_THRESHOLD:
                    bucket_report["disqualified"] = True
                    bucket_report["reason"] = (f"comm={comm_key}: {stat_name} mm={r['maxmed_node']:.2f} "
                                                f"on healthy data (rank {r['worst_rank']})")
            bucket_report["comms"][comm_key] = comm_stats
        # back-compat: callers that only ever expected a flat "stats" dict
        # (single communicator) still get one, from whichever comm has the
        # most ranks -- the same "pick the largest" convention per_rank_
        # series' own back-compat wrapper already uses.
        if bucket_report["comms"]:
            largest = max(by_comm, key=lambda k: len(by_comm[k][0]))
            bucket_report["stats"] = bucket_report["comms"][largest]
        report[bucket] = bucket_report
    return report


def null_noise_ranking(dump_dirs, buckets):
    """Healthy-only calibration: rank buckets by peer-group CV of their
    per-rank means (rank 3 excluded), lowest noise = best candidate.
    No injection required -- this is the deployability test."""
    ranking = []
    for bucket in buckets:
        per_rank, _ = per_rank_series(dump_dirs, {bucket})
        means = {r: st.mean(v) for r, v in per_rank.items() if r not in EXCLUDE_ALWAYS}
        if len(means) < 2:
            continue
        vals = list(means.values())
        noise = st.stdev(vals) / st.mean(vals)
        ranking.append((bucket, noise))
    ranking.sort(key=lambda x: x[1])
    return ranking


def injection_calibrate(healthy_dirs, injected_dirs, surviving_buckets):
    """Score every surviving (non-contaminated) bucket against a known
    injection + its clean null. Returns per-bucket per-stat separation."""
    results = {}
    for bucket in surviving_buckets:
        healthy_pr, _ = per_rank_series(healthy_dirs, {bucket})
        inj_pr, _ = per_rank_series(injected_dirs, {bucket})
        bucket_result = {}
        for stat_name in STATS:
            rh = score_node_scoped(healthy_pr, stat_name)
            ri = score_node_scoped(inj_pr, stat_name)
            if rh is None or ri is None:
                continue
            bucket_result[stat_name] = {
                "healthy_mm": rh["maxmed_node"], "healthy_z": rh["z_node"],
                "injected_mm": ri["maxmed_node"], "injected_z": ri["z_node"],
                "injected_worst_rank": ri["worst_rank"],
            }
        results[bucket] = bucket_result
    return results


def compute_healthy_per_rank_mean(healthy_dirs, buckets):
    """Stored per-rank mean exec time from a healthy run, per bucket. This
    is the piece the original global-drift stub was missing: an absolute
    reference to compare a NEW run's per-rank means against, rather than
    only comparing ranks against each other (which sees nothing when every
    rank moves together)."""
    out = {}
    for bucket in buckets:
        per_rank, _ = per_rank_series(healthy_dirs, {bucket})
        out[bucket] = {r: st.mean(v) for r, v in per_rank.items() if r not in EXCLUDE_ALWAYS}
    return out


STANDARD_JITTER_BURST_MS = 10.0  # the burst magnitude used throughout this
    # whole investigation's intermittent-fault testing (10ms/100ms). The
    # self-diagnosis below is scoped to THIS reference magnitude -- a
    # workload flagged LOW confidence here is specifically saying "a
    # ~10ms straggler burst is unlikely to be visible," not making a
    # claim about bursts of a different size (see R_LOW_THRESHOLD note).
R_LOW_THRESHOLD = 0.3   # P18j Stage 1/3: predictive property boundary.
    # R = STANDARD_JITTER_BURST_MS / mean_gap_between_primary_bucket_
    # collectives_ms, computed from ONLY a healthy baseline (no
    # injection needed). Derived from exactly two measured points:
    # nanoGPT R=0.487 (intermittent detection WORKS, strong signal) and
    # ResNet R=0.111 (intermittent detection FAILS, no usable signal
    # even on the gap-time statistic). 0.3 sits between them with no
    # third data point to place it more precisely -- treat this boundary
    # as approximate, not exactly calibrated, until a third workload
    # provides a third measurement.


def compute_mean_collective_gap_ms(dump_dirs, bucket, reference_rank=None):
    """Mean wall-clock gap between consecutive firings of `bucket` on a
    single rank -- this is what a fault's absorption capacity is measured
    against: a burst much SMALLER than this gap has a full cycle's worth
    of surrounding compute to be absorbed into before the next collective
    fires; a burst comparable to or larger than the gap cannot be fully
    absorbed regardless of workload. Uses the lowest available rank not
    in EXCLUDE_ALWAYS if reference_rank isn't given, so this works
    without needing to know which rank is the eventual injection target."""
    import glob as _glob
    import json as _json
    by_rank = {}
    for dd in dump_dirs:
        for f in _glob.glob(f"{dd}/*.log"):
            with open(f) as fh:
                for line in fh:
                    try:
                        rec = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    cp = rec["coll_perf"]
                    if cp["coll"] != "AllReduce" or cp["coll_msg_size_bytes"] != bucket or cp["coll_sn"] == 0:
                        continue
                    r = rec["header"]["rank"]
                    by_rank.setdefault(r, []).append(cp["event_trace_ts"]["coll_start_ts"])
    if reference_rank is None:
        candidates = sorted(r for r in by_rank if r not in EXCLUDE_ALWAYS)
        if not candidates:
            return None
        reference_rank = candidates[0]
    starts = sorted(by_rank.get(reference_rank, []))
    if len(starts) < 2:
        return None
    gaps_ms = [(starts[i] - starts[i - 1]) / 1000.0 for i in range(1, len(starts))]
    return st.mean(gaps_ms)


def assess_intermittent_confidence(healthy_dirs, bucket, reference_rank=None,
                                     standard_fault_ms=STANDARD_JITTER_BURST_MS, low_threshold_R=R_LOW_THRESHOLD):
    """P18j Stage 3: the self-diagnosis check. Computed ENTIRELY from a
    healthy baseline (the run calibration already requires) plus a fixed
    reference fault magnitude -- no injection needed, so this fires
    automatically for a workload nobody has tested the actual fault
    injection on yet, which is the whole point.

    Returns a dict with the ratio, a HIGH/LOW confidence call, and a
    plain-language reason -- meant to be attached directly to calibration
    output, not interpreted only by someone who already knows this
    investigation's history."""
    mean_gap_ms = compute_mean_collective_gap_ms(healthy_dirs, bucket, reference_rank)
    if mean_gap_ms is None or mean_gap_ms <= 0:
        return {"ratio_R": None, "confidence": "UNKNOWN",
                "detail": "could not compute a mean inter-collective gap from this healthy baseline"}
    ratio_R = standard_fault_ms / mean_gap_ms
    confidence = "LOW" if ratio_R < low_threshold_R else "HIGH"
    detail = (
        f"mean gap between collectives on this bucket is {mean_gap_ms:.2f}ms; a {standard_fault_ms:.0f}ms "
        f"reference straggler burst is R={ratio_R:.3f} of that gap ({'below' if confidence=='LOW' else 'at or above'} "
        f"the {low_threshold_R} boundary). "
        + (f"This workload's compute/communication overlap likely has enough slack to absorb a burst this size "
           f"before it reaches a measurable collective boundary -- CV-based intermittent detection may not see "
           f"a real fault here even though mean-based sustained detection is unaffected."
           if confidence == "LOW" else
           f"A burst this size is a large enough fraction of the inter-collective interval that it's unlikely "
           f"to be fully absorbed -- CV-based intermittent detection should see a real fault of this magnitude.")
    )
    return {"ratio_R": ratio_R, "mean_gap_ms": mean_gap_ms, "confidence": confidence, "detail": detail}


def build_calibration_multi(healthy_dirs, injections_by_class, target_rank):
    """injections_by_class: {'sustained': dirs, 'intermittent': dirs,
    'medium_burst': dirs} -- maps fault class to its own injection dump
    dirs, since mean/CV need a sustained/intermittent reference and
    outlier_count needs a medium/burst reference (a sustained injection
    produces no internal outliers on the target rank -- it's constantly
    low, not intermittently low)."""
    buckets = discover_buckets(healthy_dirs)
    contamination = screen_contamination(healthy_dirs, buckets.keys())
    surviving = [b for b, r in contamination.items() if not r["disqualified"]]
    calib = {
        "bucket_signature": buckets,
        "contamination_screen": contamination,
        "surviving_buckets": surviving,
        "null_noise_ranking": null_noise_ranking(healthy_dirs, surviving),
        "injection_calibration": {}, "selected_bucket_per_stat": {},
        "healthy_per_rank_mean": compute_healthy_per_rank_mean(healthy_dirs, surviving),
        # P18j Stage 3: computed automatically for every surviving bucket,
        # using only this healthy baseline -- not gated on an injection
        # having been run, since the whole point is to work on a
        # workload nobody has fault-tested yet.
        "intermittent_detection_confidence": {
            b: assess_intermittent_confidence(healthy_dirs, b) for b in surviving
        },
    }
    stat_to_class = {"mean": "sustained", "cv": "intermittent", "outlier_count": "medium_burst"}
    for stat_name, fault_class in stat_to_class.items():
        inj_dirs = injections_by_class.get(fault_class)
        if inj_dirs is None:
            calib["selected_bucket_per_stat"][stat_name] = None
            continue
        best_bucket, best_sep = None, -1
        for bucket in surviving:
            healthy_pr, _ = per_rank_series(healthy_dirs, {bucket})
            inj_pr, _ = per_rank_series(inj_dirs, {bucket})
            rh = score_node_scoped(healthy_pr, stat_name)
            ri = score_node_scoped(inj_pr, stat_name)
            if rh is None or ri is None:
                continue
            calib["injection_calibration"].setdefault(bucket, {})[stat_name] = {
                "healthy_mm": rh["maxmed_node"], "injected_mm": ri["maxmed_node"],
                "healthy_worst_val": rh["worst_val"], "injected_worst_val": ri["worst_val"],
                "injected_worst_rank": ri["worst_rank"],
            }
            if ri["worst_rank"] != target_rank:
                continue
            # mm degenerates to inf/inf (NaN) when the peer group's own
            # count/median is 0 -- use the raw worst-value separation
            # instead, which stays well-defined for count-based stats.
            # Sign depends on direction: "min" stats (mean) separate by
            # going LOWER under fault; "max" stats (cv, outlier_count)
            # separate by going HIGHER.
            _, direction = STATS[stat_name]
            raw_sep = ri["worst_val"] - rh["worst_val"]
            sep = -raw_sep if direction == "min" else raw_sep
            if sep > best_sep:
                best_sep, best_bucket = sep, bucket
        calib["selected_bucket_per_stat"][stat_name] = best_bucket
    return calib


def build_calibration(healthy_dirs, injected_dirs=None, target_rank=None):
    """Full Stage 2 pipeline. Returns a stored calibration record."""
    buckets = discover_buckets(healthy_dirs)
    contamination = screen_contamination(healthy_dirs, buckets.keys())
    surviving = [b for b, r in contamination.items() if not r["disqualified"]]

    calib = {
        "bucket_signature": buckets,  # {size: count} -- drift reference
        "contamination_screen": contamination,
        "surviving_buckets": surviving,
        "null_noise_ranking": null_noise_ranking(healthy_dirs, surviving),
    }

    if injected_dirs is not None:
        inj_results = injection_calibrate(healthy_dirs, injected_dirs, surviving)
        calib["injection_calibration"] = inj_results
        # pick, per statistic, the surviving bucket with the best separation
        selection = {}
        for stat_name in STATS:
            best_bucket, best_sep = None, -1
            for bucket, stats in inj_results.items():
                if stat_name not in stats:
                    continue
                sep = stats[stat_name]["injected_mm"] - stats[stat_name]["healthy_mm"]
                if sep > best_sep and stats[stat_name]["injected_worst_rank"] == target_rank:
                    best_sep, best_bucket = sep, bucket
            selection[stat_name] = best_bucket
        calib["selected_bucket_per_stat"] = selection
    return calib
