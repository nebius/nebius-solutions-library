#!/usr/bin/env python3
"""Stage 5: persistence requirement -- how many consecutive rolling windows
must exceed threshold before firing, trading detection latency against
false-positive rate (especially for CV, the noisy statistic)."""
from detection import per_rank_series, windowed, score_node_scoped, select_primary_corroborating_buckets
from classifier import fired


def run_persistence_sweep(dump_dirs, stat_name, window_size=100, persistence_options=(1, 2, 3, 4), bucket=None):
    """Splits the run into windows, scores each, and reports for each
    persistence requirement N: does an N-in-a-row streak ever occur, and
    at which window index (-> detection latency in samples).

    Cluster-topology-agnostic fix (this session): bucket used to default
    to BUCKET_B -- one specific workload's own real message size, so this
    sweep silently scored nothing real for any other workload's dump
    dirs. bucket=None now discovers THIS run's own real primary bucket;
    an explicit bucket is still honored for re-running the sweep against
    a specific one."""
    if bucket is None:
        bucket, _ = select_primary_corroborating_buckets(dump_dirs)
        if bucket is None:
            return {"per_window_fired": [], "n_windows": 0, "by_persistence": {}}
    per_rank, _ = per_rank_series(dump_dirs, {bucket})
    windows = windowed(per_rank, window_size)
    fire_flags = []
    for w in windows:
        r = score_node_scoped(w, stat_name)
        fire_flags.append(fired(stat_name, r) if r else False)

    results = {}
    for n in persistence_options:
        streak = 0
        first_fire_window = None
        for i, f in enumerate(fire_flags):
            streak = streak + 1 if f else 0
            if streak >= n and first_fire_window is None:
                first_fire_window = i
        results[n] = {
            "ever_fires": first_fire_window is not None,
            "first_fire_at_window": first_fire_window,
            "detection_latency_samples": (first_fire_window + 1) * window_size if first_fire_window is not None else None,
        }
    return {"per_window_fired": fire_flags, "n_windows": len(windows), "by_persistence": results}
