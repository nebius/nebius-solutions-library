#!/usr/bin/env python3
"""Compact transient-fault latency analysis: windowed mean scoring (100
samples/window) across the whole run, mapped to wall-clock time via each
window's own timestamps, reporting detection latency (first window after
fault_start to show a strong rank4 signature) and recovery latency (first
window after fault_end back to the clean healthy null)."""
import sys
import statistics as st
from detection import (per_rank_series_with_ts, rank_node, EXCLUDE_ALWAYS, EXCLUDE_CV_EXTRA, stat_mean,
                        select_primary_corroborating_buckets, discover_rank_hosts)


def windowed_mean_scores(dirs, fault_start, fault_end, window=100, bucket=None):
    """Cluster-topology-agnostic fix (this session): bucket used to
    default to BUCKET_B -- one specific workload's own real message size.
    bucket=None now discovers this run's own real primary bucket; an
    explicit bucket is still honored for re-running against a specific
    one.

    Codebase audit, third pass -- peers below used to use rank_node()'s
    own real, disclosed-but-still-live 2-node/8-per-node assumption
    (NODE_A/NODE_B); now uses this run's own real, discovered rank_hosts
    (discover_rank_hosts) instead -- no assumption about node count or
    GPUs-per-node."""
    if bucket is None:
        bucket, _ = select_primary_corroborating_buckets(dirs)
        if bucket is None:
            return []
    per_rank, per_rank_ts, _ = per_rank_series_with_ts(dirs, {bucket})
    rank_hosts = discover_rank_hosts(dirs)
    n = min(len(v) for v in per_rank.values())
    results = []
    for start in range(0, n - window + 1, window):
        vals = {r: stat_mean(v[start:start + window]) for r, v in per_rank.items() if r not in EXCLUDE_ALWAYS}
        ts = {r: per_rank_ts[r][start:start + window] for r in per_rank_ts if r not in EXCLUDE_ALWAYS}
        t0 = min(min(t) for t in ts.values())
        t1 = max(max(t) for t in ts.values())
        worst = min(vals, key=lambda r: vals[r])
        peers = [r for r in rank_node(worst, rank_hosts) if r != worst and r in vals]
        peer_vals = [vals[r] for r in peers]
        mu, sd = st.mean(peer_vals), (st.stdev(peer_vals) if len(peer_vals) > 1 else 0.0)
        z = (mu - vals[worst]) / sd if sd > 0 else (float("inf") if vals[worst] < mu else 0.0)
        med = st.median(peer_vals)
        mm = med / vals[worst] if vals[worst] else float("inf")
        results.append({"t0": t0, "t1": t1, "worst": worst, "z": z, "mm": mm,
                         "rel_start": t0 - fault_start, "rel_end": t1 - fault_start})
    return results


if __name__ == "__main__":
    d0, d1, fault_start, fault_end = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    windows = windowed_mean_scores([d0, d1], fault_start, fault_end)
    first_fire, last_fire = None, None
    for w in windows:
        fires = (w["worst"] == 4 and w["mm"] > 2.0 and w["z"] > 30.0)
        if fires:
            if first_fire is None:
                first_fire = w
            last_fire = w
    print("fault_duration_measured:", fault_end - fault_start)
    if first_fire:
        print("detect_latency_s:", first_fire["rel_start"], "to", first_fire["rel_end"])
    else:
        print("NEVER DETECTED")
    if last_fire:
        recovery_rel = last_fire["t1"] - fault_end
        print("last_firing_window_end_rel_to_fault_end:", recovery_rel)
    # print full table compactly
    for w in windows:
        if -5 <= w["rel_start"] <= (fault_end - fault_start) + 15:
            print(f"  t={w['rel_start']:.1f}..{w['rel_end']:.1f} worst={w['worst']} z={w['z']:.1f} mm={w['mm']:.2f}")
