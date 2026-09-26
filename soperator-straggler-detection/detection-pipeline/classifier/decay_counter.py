#!/usr/bin/env python3
"""Stage 2a: decay-based cumulative outlier counter, as an alternative to
a hard window. Each new sample below the LOW-side outlier threshold adds
1.0 to a running score; the running score decays by a fixed factor at
every new sample (regardless of whether it's an outlier), so old events
fade rather than being sharply dropped at a window boundary."""
import statistics as st
from detection import per_rank_series, EXCLUDE_ALWAYS, EXCLUDE_OUTLIER_EXTRA, rank_node, OUTLIER_K


def decay_trace(vals, k=OUTLIER_K, decay=0.999):
    """Returns the running decayed-count trace for one rank's series."""
    med = st.median(vals)
    thresh = med / k
    score = 0.0
    trace = []
    for v in vals:
        score *= decay
        if v < thresh:
            score += 1.0
        trace.append(score)
    return trace


def score_decay_at_each_point(per_rank, rank_hosts, k=OUTLIER_K, decay=0.999, exclude_extra=True):
    """For every sample index, compute each rank's decayed score and the
    node-scoped z/worst-rank at that instant. Returns a list of per-index
    results (rank -> score) plus derived worst-rank/z per index.

    Codebase audit, third pass -- rank_hosts (real, discovered
    rank->hostname identity; see detection.discover_rank_hosts) now
    required, replacing rank_node's old implicit NODE_A/NODE_B
    dependency."""
    exclude = set(EXCLUDE_ALWAYS)
    if exclude_extra:
        exclude |= EXCLUDE_OUTLIER_EXTRA
    ranks = [r for r in per_rank if r not in exclude]
    traces = {r: decay_trace(per_rank[r], k, decay) for r in ranks}
    n = min(len(t) for t in traces.values())
    results = []
    for i in range(n):
        scores = {r: traces[r][i] for r in ranks}
        worst = max(scores, key=lambda r: scores[r])
        peers = [r for r in rank_node(worst, rank_hosts) if r != worst and r in scores]
        peer_scores = [scores[r] for r in peers]
        mu = st.mean(peer_scores)
        sd = st.stdev(peer_scores) if len(peer_scores) > 1 else 0.0
        z = (scores[worst] - mu) / sd if sd > 0 else (float("inf") if scores[worst] > mu else 0.0)
        results.append({"idx": i, "worst_rank": worst, "worst_score": scores[worst], "peer_mean": mu, "z": z})
    return results
