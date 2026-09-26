#!/usr/bin/env python3
"""CV windowed scoring with rank0 ALSO excluded (found to be the dominant,
persistent source of healthy-data instability -- not spiky noise)."""
import statistics as st
from detection import rank_node, stat_cv, EXCLUDE_ALWAYS

CV_EXCLUDE_EXTRA = {0}


def score_cv_fixed(w, rank_hosts):
    """Codebase audit, third pass -- rank_hosts (real, discovered
    rank->hostname identity; see detection.discover_rank_hosts) now
    required, replacing rank_node's old implicit NODE_A/NODE_B
    dependency."""
    exclude = EXCLUDE_ALWAYS | CV_EXCLUDE_EXTRA
    vals = {r: stat_cv(v) for r, v in w.items() if r not in exclude}
    worst = max(vals, key=lambda r: vals[r])
    peers = [r for r in rank_node(worst, rank_hosts) if r != worst and r in vals]
    peer_vals = [vals[r] for r in peers]
    mu, sd = st.mean(peer_vals), st.stdev(peer_vals)
    z = (vals[worst] - mu) / sd if sd > 0 else (float("inf") if vals[worst] > mu else 0.0)
    med = st.median(peer_vals)
    mm = vals[worst] / med if med else float("inf")
    return {"worst_rank": worst, "z_node": z, "maxmed_node": mm, "worst_val": vals[worst]}


def persistence_fire(window_scores, threshold, n_required):
    streak = 0
    first_fire = None
    for i, r in enumerate(window_scores):
        fires = r["z_node"] > threshold
        streak = streak + 1 if fires else 0
        if streak >= n_required and first_fire is None:
            first_fire = i
    return first_fire
