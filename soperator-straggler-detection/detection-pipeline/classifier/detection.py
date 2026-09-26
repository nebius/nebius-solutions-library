#!/usr/bin/env python3
"""Stage 1: detection layer. Three statistics against Inspector raw dumps,
with all established exclusions.

Exclusions (always):
  - rank 3 (physically degraded GPU, never a valid peer)
  - bucket A (2.36MB) -- contaminated by the rank-15 ring-position artifact
  - coll_sn == 0 (bootstrap collective, 1.4-2.1s startup skew)
  - partially-covered collectives (a coll_sn not seen on all 16 ranks)
  - startup artifacts: 4-byte bootstrap AllGather, 42.98MB one-time broadcast

Additional exclusion for outlier_count only:
  - rank 0 (master process has a structurally elevated baseline outlier
    count from its own bookkeeping/logging overhead)

Statistics:
  - mean exec time      -> sustained faults        (direction: min wins)
  - CV (stdev/mean)      -> frequent intermittent    (direction: max wins)
  - outlier_count(k=3)   -> medium/long-burst faults (direction: max wins,
                            counting the LOW tail -- the straggler's
                            affected collectives are anomalously SHORT,
                            not long; verified empirically against the
                            task's stated hint, which was backwards)
"""
import glob
import json
import statistics as st
from collections import Counter, defaultdict

N_RANKS = 16
NODE_A = set(range(0, 8))
NODE_B = set(range(8, 16))

# Cluster-topology-agnostic fix (this session): BUCKET_B/BUCKET_C used to
# be hardcoded exact byte counts (12295680/28323840) -- calibrated for
# ONE specific workload's own real AllReduce message sizes, the exact
# same failure pattern already found and fixed elsewhere in this project
# (workload_signature's collision, coverage_guard's CALIBRATED_RATE_
# PER_SEC). A different workload's real message sizes are silently NOT
# these two numbers, and every default parameter across this module
# (score_all, check_global_drift, classify_incremental) and its sibling
# standalone tools (persistence.py, transient_latency.py) used to
# default to them regardless. Kept ONLY as historical constants (a few
# already-recorded calibration/regression scripts elsewhere in this
# project's history may still reference these two specific values when
# replaying THEIR OWN specific historical dumps) -- no longer used as a
# default anywhere in this module. See select_primary_corroborating_
# buckets() below for the real, per-run replacement.
BUCKET_B = 12295680
BUCKET_C = 28323840
DIAGNOSTIC_BUCKETS = {BUCKET_B, BUCKET_C}  # bucket A permanently excluded

STARTUP_SIZES = {4, 42980352}  # bootstrap AllGather (4B) and one-time broadcast (42.98MB)


def discover_real_buckets(dump_dirs, min_count=100):
    """Real, dynamic message-size bucket discovery from a run's own raw
    dump data -- {msg_size_bytes: sample_count}, filtered to real,
    recurring per-step buckets (not startup artifacts, not a one-off).
    No assumption about which bucket byte value this specific run
    actually uses -- BUCKET_B/BUCKET_C above were never universal (see
    their own comment). Same scan calibration.py's own discover_buckets
    already performed for the calibration workflow -- factored out here
    (the lower-dependency module) so classify_incremental and the
    standalone sweep tools can reuse it directly instead of hand-rolling
    their own copy or importing calibration.py (which itself imports
    FROM this module, so the dependency can only run this direction)."""
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


def select_primary_corroborating_buckets(dump_dirs, min_count=100):
    """Real, per-run bucket selection, replacing the old universal
    BUCKET_B (primary)/BUCKET_C (corroborating) hardcoding: the real
    bucket with the most real samples in THIS run's own data becomes
    primary (this project's own established "biggest, most active real
    signal" convention, e.g. the same idea score_bucket's own per-comm
    design already uses), every other real bucket found becomes
    corroborating. Returns (None, ()) if nothing real is found (an
    honest, real "nothing to score" answer, not a silent fallback to the
    old hardcoded values)."""
    buckets = discover_real_buckets(dump_dirs, min_count)
    if not buckets:
        return None, ()
    ordered = sorted(buckets, key=lambda b: buckets[b], reverse=True)
    return ordered[0], tuple(ordered[1:])


def discover_rank_hosts(dump_dirs):
    """Real, dynamic rank->hostname identity, read directly from each
    record's own metadata.hostname field (Inspector's own real, self-
    reported identity per dump record) -- the offline package's
    equivalent of the live alerting path's real gpu_slot_index discovery
    (alert_engine.py's _query_gpu_slot), adapted to this package's dump-
    file data model instead of a live VictoriaMetrics query. Ported here
    (codebase audit, third pass) to replace NODE_A/NODE_B/rank_node()'s
    hardcoded 0-7/8-15 range and worker_of()'s hardcoded rank<8 split,
    both of which silently mis-attribute host identity on any cluster
    shaped differently than 2 nodes x 8 GPUs. Returns {rank: hostname},
    built from whichever ranks/hosts THIS run's own dump files actually
    contain -- no assumption anywhere about node count or GPUs-per-node.
    A rank whose own record(s) never carry a hostname (deliberately
    defensive -- every real record seen so far has one) is simply absent
    from the returned map, exactly like a rank with no data at all;
    callers already handle a missing rank honestly (rank_node's own
    single-rank fallback, `r in vals`-style guards throughout)."""
    hosts = {}
    for dd in dump_dirs:
        for f in sorted(glob.glob(f"{dd}/*.log")):
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rank = rec.get("header", {}).get("rank")
                    hostname = rec.get("metadata", {}).get("hostname")
                    if rank is not None and hostname is not None and rank not in hosts:
                        hosts[rank] = hostname
                    break  # one real record per file already identifies this rank's host
    return hosts


EXCLUDE_ALWAYS = {3}
EXCLUDE_OUTLIER_EXTRA = {0}
EXCLUDE_CV_EXTRA = {0}  # P18b finding: rank0's master-process overhead is a
                        # PERSISTENT (not spiky) source of elevated CV --
                        # wins "worst" in 72% of 100-sample healthy windows.
                        # Excluding it is necessary for CV windowing to work
                        # at all, same as the established outlier_count fix.

OUTLIER_K = 3.0


def rank_node(rank, rank_hosts):
    """Real node-membership: every rank (including `rank` itself) sharing
    `rank`'s own real, discovered hostname (rank_hosts, from discover_
    rank_hosts) -- replaces the old NODE_A/NODE_B hardcoded 0-7/8-15
    range entirely (codebase audit, third pass: this was a real,
    disclosed-but-still-live 2-node/8-per-node assumption, ported to real
    discovery the same way the live alerting path's own worker_of()/
    rank%8 was already fixed via gpu_slot_index). No assumption about
    node count or GPUs-per-node -- generalizes to however many real
    hosts/ranks this run's own dump files actually contain. `rank` alone
    (no peers) if its host is unknown -- an honest, non-crashing answer,
    matching every other missing-data case in this module."""
    host = rank_hosts.get(rank)
    if host is None:
        return {rank}
    return {r for r, h in rank_hosts.items() if h == host}


# Codebase audit, third pass -- RANK0_CV_PEERS used to be a module-level
# constant, computed once at import time from the old NODE_A-based
# rank_node(0). rank_node now requires real, discovered rank_hosts (only
# known once a run's dump files are actually scanned, never at import
# time), so this can no longer be a static constant -- score_rank0_cv/
# score_rank0_outlier_rate now compute it fresh, per call, from whatever
# real rank_hosts their own caller passes in. See score_rank0_cv below.


def load_records(dump_dirs):
    """Yields (comm_key, coll_sn, rank, record) for every AllReduce record,
    already filtered to startup-artifact sizes and the header/comm identity."""
    for dd in dump_dirs:
        for f in sorted(glob.glob(f"{dd}/*.log")):
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cp = rec["coll_perf"]
                    if cp["coll"] != "AllReduce":
                        continue
                    if cp["coll_msg_size_bytes"] in STARTUP_SIZES:
                        continue
                    if cp["coll_sn"] == 0:
                        continue
                    rank = rec["header"]["rank"]
                    comm_key = rec["header"]["id"]
                    yield comm_key, cp["coll_sn"], rank, rec


def per_rank_series_by_comm(dump_dirs, msg_sizes, field="coll_exec_time_us", require_full_coverage=True):
    """P21.5 -- comm-scoped, dynamically-covered replacement for the old
    per_rank_series. Two real, confirmed bugs fixed here, both from the
    same root cause (treating "coll_sn" and "rank" as globally meaningful
    without regard to which communicator they came from):

    1. The old code grouped purely by `sn` (coll_sn) across ALL
       communicators combined -- but each communicator has its own
       independently-numbered coll_sn counter starting near 0/1, so two
       DIFFERENT communicators' unrelated collectives sharing the same sn
       number would silently merge into one "collective" here. Fixed by
       grouping by (comm_key, sn) instead.
    2. The old coverage check used a hardcoded N_RANKS=16 -- confirmed
       broken the moment more than one communicator exists (no single
       communicator spans the full job once any additional parallelism
       dimension is introduced), and would break differently again for a
       job of any other total rank count. Fixed by reading each
       communicator's own real member count directly from its own
       records' header["n_ranks"] (NCCL/Inspector's own authoritative,
       self-reported communicator size -- confirmed accurate against real
       data: the TP communicator reported n_ranks=2, the DP communicator
       reported n_ranks=8, both correct) -- discovered per comm_key as
       records are scanned, never assumed ahead of time.

    Returns {comm_key: (per_rank_dict, n_ranks, dropped_partial)} -- one
    entry per communicator actually found in the data, whatever that
    number turns out to be."""
    by_comm_sn_rank = defaultdict(lambda: defaultdict(dict))
    comm_n_ranks = {}
    for comm_key, sn, rank, rec in load_records(dump_dirs):
        cp = rec["coll_perf"]
        if cp["coll_msg_size_bytes"] not in msg_sizes:
            continue
        comm_n_ranks.setdefault(comm_key, rec["header"].get("n_ranks"))
        by_comm_sn_rank[comm_key][sn][rank] = cp[field]

    out = {}
    for comm_key, by_sn in by_comm_sn_rank.items():
        expected = comm_n_ranks.get(comm_key)
        per_rank = defaultdict(list)
        dropped_partial = 0
        for sn, rank_vals in sorted(by_sn.items()):
            if require_full_coverage and expected is not None and len(rank_vals) < expected:
                dropped_partial += 1
                continue
            for rank, v in rank_vals.items():
                per_rank[rank].append(v)
        out[comm_key] = (dict(per_rank), expected, dropped_partial)
    return out


def per_rank_series(dump_dirs, msg_sizes, field="coll_exec_time_us", require_full_coverage=True):
    """Back-compat single-communicator view, kept for any caller that only
    ever expects one communicator to exist. NOT used by score_bucket/
    score_all any more (see per_rank_series_by_comm and score_bucket_by_comm
    below) -- those now go through the comm-scoped path unconditionally,
    the same code path for one communicator or many. This wrapper just
    picks the single discovered communicator (or the largest one, if
    somehow more than one exists) so old direct callers of this exact
    function keep working unchanged."""
    by_comm = per_rank_series_by_comm(dump_dirs, msg_sizes, field, require_full_coverage)
    if not by_comm:
        return {}, 0
    comm_key = max(by_comm, key=lambda k: len(by_comm[k][0]))
    per_rank, _n_ranks, dropped = by_comm[comm_key]
    return per_rank, dropped


def per_rank_series_with_ts(dump_dirs, msg_sizes, field="coll_exec_time_us", require_full_coverage=True):
    """Same as per_rank_series, but also returns each sample's wall-clock
    time (event_trace_ts.coll_start_ts, microseconds -> seconds) aligned
    by index -- needed to map a fired window back to a time range for a
    rolling cause-metric buffer lookup (P18c Stage 1).

    P21.5 -- same two fixes as per_rank_series_by_comm: grouped by
    (comm_key, sn) instead of bare sn (avoids cross-communicator coll_sn
    collisions), and coverage checked against each communicator's own
    real header["n_ranks"] instead of hardcoded N_RANKS=16. Still returns
    a single-communicator view (picks the largest one found) since every
    caller of this specific function expects a flat per-rank dict."""
    by_comm_sn_rank = defaultdict(lambda: defaultdict(dict))
    comm_n_ranks = {}
    for comm_key, sn, rank, rec in load_records(dump_dirs):
        cp = rec["coll_perf"]
        if cp["coll_msg_size_bytes"] not in msg_sizes:
            continue
        comm_n_ranks.setdefault(comm_key, rec["header"].get("n_ranks"))
        ts_us = rec["coll_perf"]["event_trace_ts"]["coll_start_ts"]
        by_comm_sn_rank[comm_key][sn][rank] = (cp[field], ts_us / 1e6)

    if not by_comm_sn_rank:
        return {}, {}, 0
    comm_key = max(by_comm_sn_rank, key=lambda k: sum(len(v) for v in by_comm_sn_rank[k].values()))
    expected = comm_n_ranks.get(comm_key)
    by_sn_rank = by_comm_sn_rank[comm_key]

    per_rank, per_rank_ts = defaultdict(list), defaultdict(list)
    dropped_partial = 0
    for sn, rank_vals in sorted(by_sn_rank.items()):
        if require_full_coverage and expected is not None and len(rank_vals) < expected:
            dropped_partial += 1
            continue
        for rank, (v, ts) in rank_vals.items():
            per_rank[rank].append(v)
            per_rank_ts[rank].append(ts)
    return dict(per_rank), dict(per_rank_ts), dropped_partial


def windowed(per_rank, window_size):
    """Split each rank's series into consecutive windows of window_size
    samples (rolling, non-overlapping for simplicity of reporting -- a
    real rolling-window implementation would slide by 1 and recompute;
    here we chunk, which is sufficient to validate persistence logic)."""
    n = min(len(v) for v in per_rank.values())
    windows = []
    for start in range(0, n - window_size + 1, window_size):
        windows.append({r: v[start:start + window_size] for r, v in per_rank.items()})
    return windows


# ---- statistics ----

def stat_mean(vals):
    return st.mean(vals)


CV_TRIM_EACH_SIDE = 4  # P18g Stage 3: raised from 2 to enable a smaller
                       # window (125, down from 250) without reopening
                       # the false-positive problem the trim originally
                       # fixed -- verified this stricter trim doesn't
                       # move the whole-run healthy ceiling meaningfully
                       # (9.7 -> 9.97, n=9) or the true-positive jitter
                       # signal (158.8 -> 159.2).


def stat_cv(vals, trim_each_side=CV_TRIM_EACH_SIDE):
    """P18e Stage 1: trimmed CV. Root cause of the stage3_baseline false
    positive (z=20.83 against a threshold of 20.0): a whole-run CV score
    is a coefficient of variation over the ENTIRE series, computed in
    one shot (classify() has never actually implemented rolling windows
    -- that only exists in standalone analysis scripts). A handful of
    one-off low samples (rank8 in stage3_baseline: 4 of 300 samples were
    255-1762 against a ~3684 median -- a one-time hiccup, not a
    population-wide bias, confirmed by re-scoring 11 healthy runs: the
    population median z is 3.13, stage3_baseline's 20.83 is the single
    outlier, not evidence the whole population sits near threshold) can
    inflate CV enough to false-fire, since CV's variance term has no
    outlier resistance at all. Dropping the 2 most extreme samples on
    each side before computing removes exactly this kind of one-off
    spike while preserving genuine, sustained intermittent variability
    (the true-positive jitter case still shows z=158.79, unaffected)."""
    s = sorted(vals)
    if len(s) > 2 * trim_each_side + 10:
        s = s[trim_each_side:-trim_each_side] if trim_each_side else s
    mu = st.mean(s)
    return (st.stdev(s) / mu) if mu and len(s) > 1 else 0.0


def stat_outlier_count(vals, k=OUTLIER_K):
    med = st.median(vals)
    thresh = med / k
    return sum(1 for v in vals if v < thresh)


STATS = {
    "mean":          (stat_mean, "min"),   # sustained: straggler shows LOW mean
    "cv":            (stat_cv, "max"),     # intermittent: straggler shows HIGH variability
    "outlier_count": (stat_outlier_count, "max"),  # medium/burst: straggler shows more LOW-tail events
}


def score_node_scoped(per_rank, stat_name):
    """Node-scoped: within worst rank's own real peer group (whatever
    ranks are actually present in per_rank -- the caller's own real,
    already-scoped membership, e.g. one specific communicator's real
    members, or a specific node's real ranks if the caller pre-filtered
    to one), excluding rank3 always (and rank0 additionally for
    outlier_count).

    Cluster-topology-agnostic fix (this session): peers used to come from
    rank_node(worst) -- NODE_A/NODE_B, a hardcoded 0-7/8-15 range --
    regardless of which ranks per_rank actually contained. Confirmed live
    this session: this crashes with a real KeyError the moment per_rank is
    a real communicator's own membership that doesn't span a full
    hardcoded 8-wide node (e.g. a real 2-member TP communicator's own
    per_rank, fed in via the same comm-scoped design
    per_rank_series_by_comm already established elsewhere in this file --
    rank_node() still hands back the full 8-wide range, so vals[r] fails
    for every rank in that range NOT actually in this specific
    communicator). Peers are now derived from vals' own real keys --
    exactly whatever this specific call was actually given data for, never
    an assumed range -- which also makes existing callers that
    pre-filter per_rank themselves (score_node_scoped_per_node's own
    NODE_A/NODE_B split, still a real, separate, disclosed cluster-shape
    assumption at THAT call site -- see this session's audit) behave
    identically to before, since their own pre-filtering already bounded
    per_rank to what should be compared."""
    stat_fn, direction = STATS[stat_name]
    exclude = set(EXCLUDE_ALWAYS)
    if stat_name == "outlier_count":
        exclude |= EXCLUDE_OUTLIER_EXTRA
    if stat_name == "cv":
        exclude |= EXCLUDE_CV_EXTRA

    vals = {r: stat_fn(v) for r, v in per_rank.items() if r not in exclude}
    if not vals:
        return None
    worst = max(vals, key=lambda r: vals[r]) if direction == "max" else min(vals, key=lambda r: vals[r])
    peers = [r for r in vals if r != worst]
    peer_vals = [vals[r] for r in peers]
    if len(peer_vals) < 2:
        return None
    mu, sd = st.mean(peer_vals), st.stdev(peer_vals)
    if direction == "max":
        z = (vals[worst] - mu) / sd if sd > 0 else (float("inf") if vals[worst] > mu else 0.0)
        med = st.median(peer_vals)
        mm = vals[worst] / med if med else float("inf")
    else:
        z = (mu - vals[worst]) / sd if sd > 0 else (float("inf") if vals[worst] < mu else 0.0)
        med = st.median(peer_vals)
        mm = med / vals[worst] if vals[worst] else float("inf")
    return {
        "stat": stat_name, "worst_rank": worst, "worst_val": vals[worst],
        "z_node": z, "maxmed_node": mm, "peer_mean": mu, "peer_sd": sd,
        "all_vals": vals,
    }


def score_node_scoped_per_node(per_rank, stat_name):
    """P18c Stage 4 fix: score_node_scoped picks ONE global worst rank per
    statistic across all 16 ranks -- even though peers are drawn only
    from the winner's own node, the winner-selection itself is global.
    When two DIFFERENT nodes each have a real, independent single-rank
    fault of the same statistic (found via the two-different-nodes test:
    ranks 4 and 12 both clock-locked, CV=0.381 and 0.242 respectively --
    comparably faulty), only the more extreme one is ever returned by
    score_node_scoped. The other rank's fault doesn't get downgraded to
    a lower tier -- it never enters the findings list at all, because
    classify() only ever looks at score_node_scoped's single winner.

    This computes the worst rank INDEPENDENTLY per node, so a real fault
    on worker-1 can't be shadowed by a more extreme one on worker-0."""
    return {
        "worker-0": score_node_scoped({r: v for r, v in per_rank.items() if r in NODE_A}, stat_name),
        "worker-1": score_node_scoped({r: v for r, v in per_rank.items() if r in NODE_B}, stat_name),
    }


def score_rank0_cv(per_rank, rank_hosts):
    """P18d Stage 2 (Option B): rank0 is excluded from the MAIN CV peer
    group (EXCLUDE_CV_EXTRA) -- its bookkeeping overhead is a PERSISTENT,
    not spiky, source of elevated CV that would otherwise win "worst" in
    ~72% of healthy windows. That exclusion fixed CV windowing for every
    OTHER rank, but as a side effect made rank0 structurally invisible
    to CV: a real intermittent fault ON rank0 was never even considered,
    not merely a candidate that failed to fire (P18c finding: rank0's
    own CV was 6x the winning rank's and was never looked at).

    This is a SEPARATE, additional check: rank0's own CV against its
    node's other ranks (excluding rank3, always invalid), independent of
    the main per-node CV check above -- which keeps excluding rank0 as a
    PEER for everyone else, unchanged. Different statistics get
    different peer groups; this doesn't have to be one-size-fits-all.

    Codebase audit, third pass -- rank0's own real node peers (was the
    module-level RANK0_CV_PEERS constant, computed once from the old
    hardcoded NODE_A; now computed fresh per call from rank_hosts, the
    real, discovered identity this run's own dump files actually
    contain -- see rank_node's own docstring)."""
    if 0 not in per_rank:
        return None
    rank0_peers = rank_node(0, rank_hosts) - {0, 3}
    peers = [r for r in rank0_peers if r in per_rank]
    if len(peers) < 2:
        return None
    worst_val = stat_cv(per_rank[0])
    peer_vals = [stat_cv(per_rank[r]) for r in peers]
    mu, sd = st.mean(peer_vals), (st.stdev(peer_vals) if len(peer_vals) > 1 else 0.0)
    z = (worst_val - mu) / sd if sd > 0 else (float("inf") if worst_val > mu else 0.0)
    med = st.median(peer_vals)
    mm = worst_val / med if med else float("inf")
    return {"stat": "cv", "worst_rank": 0, "worst_val": worst_val, "z_node": z,
            "maxmed_node": mm, "peer_mean": mu, "peer_sd": sd}


def score_rank0_outlier_rate(per_rank, rank_hosts):
    """P18f Stage 3: score_rank0_cv (Option B) is PROVEN, not just
    suspected, to be unable to separate rank0's genuine intermittent-
    only fault from healthy noise -- re-measured at n=20 healthy runs,
    the healthy max (51.49) now EXCEEDS the true positive (50.14). No
    threshold on that statistic can work; this is a different statistic
    entirely, applied to rank0 the same way CV got its own peer group.

    outlier_count's RAW count is confounded by run length (a healthy
    8000-iteration run naturally accumulates more low-tail events than a
    300-iteration one at the same underlying rate) -- normalize to a
    RATE (count / n_samples) first. z/mm still degenerate to +inf
    whenever a peer's rate happens to be exactly 0 (common over a short
    run), so the primary gate is an ABSOLUTE rate threshold (mirroring
    outlier_count's own original raw-count gate), with z/mm reported as
    corroborating context only.

    Codebase audit, third pass -- see score_rank0_cv's identical fix for
    why this now takes rank_hosts instead of the old RANK0_CV_PEERS
    constant."""
    if 0 not in per_rank:
        return None
    n0 = len(per_rank[0])
    if n0 == 0:
        return None
    worst_rate = stat_outlier_count(per_rank[0]) / n0
    rank0_peers = rank_node(0, rank_hosts) - {0, 3}
    peers = [r for r in rank0_peers if r in per_rank and len(per_rank[r]) > 0]
    if len(peers) < 2:
        return None
    peer_rates = [stat_outlier_count(per_rank[r]) / len(per_rank[r]) for r in peers]
    mu, sd = st.mean(peer_rates), (st.stdev(peer_rates) if len(peer_rates) > 1 else 0.0)
    z = (worst_rate - mu) / sd if sd > 0 else (float("inf") if worst_rate > mu else 0.0)
    med = st.median(peer_rates)
    mm = worst_rate / med if med else float("inf")
    return {"stat": "outlier_rate", "worst_rank": 0, "worst_rate": worst_rate, "worst_val": worst_rate,
            "z_node": z, "maxmed_node": mm, "peer_mean": mu, "peer_sd": sd, "n": n0}


STAT_WINDOW_SIZE = {"mean": 100, "cv": 125}  # P18g Stage 3: cv window
# halved from 250 -- combined with the stricter trim above and a 2-of-3
# (not strictly-consecutive) persistence rule in classifier.py, this
# recovers clean separation down to 300 samples (2 windows), the
# structural floor being 250 samples (window*2) below which fewer than
# 2 windows can exist at all. See classify_incremental's docstring for
# the "insufficient data" report below that floor.
# P18f Stage 1: the window
# sizes already validated in standalone analysis (P18b Stage 2b/2c),
# wired into the actual scoring path for the first time. outlier_count
# is deliberately NOT windowed here -- established finding (P18b/P18c)
# is that it needs ~60s+/most of a run to accumulate reliably, i.e. it
# stays whole-run/forensic-only by design, not by oversight.


def chunk_windows(per_rank, per_rank_ts, window_size, start_offset=0):
    """Non-overlapping windows of window_size samples per rank, starting
    at start_offset (in samples) -- lets a caller resume from where it
    left off instead of re-scoring windows already scored, which is what
    makes classify() genuinely incremental rather than replay-only.
    Returns (windows, new_offset) where windows is a list of
    (per_rank_window, t_start, t_end) and new_offset is how far
    processing reached (the start of the first incomplete window)."""
    n = min(len(v) for v in per_rank.values())
    windows = []
    start = start_offset
    while start + window_size <= n:
        w = {r: v[start:start + window_size] for r, v in per_rank.items()}
        wts = {r: per_rank_ts[r][start:start + window_size] for r in per_rank_ts}
        t0 = min(min(t) for t in wts.values())
        t1 = max(max(t) for t in wts.values())
        windows.append((w, t0, t1))
        start += window_size
    return windows, start


def windowed_scores_per_node(per_rank, per_rank_ts, stat_name, window_size, start_offset=0):
    """Runs score_node_scoped_per_node on each window in turn. Returns
    (list of {"t0","t1","per_node"} dicts, new_offset)."""
    windows, new_offset = chunk_windows(per_rank, per_rank_ts, window_size, start_offset)
    out = []
    for w, t0, t1 in windows:
        out.append({"t0": t0, "t1": t1, "per_node": score_node_scoped_per_node(w, stat_name)})
    return out, new_offset


def score_node_vs_node(per_rank, rank_hosts, stat_name="mean", extra_exclude=frozenset()):
    """Node-vs-node aggregate -- mean-of-means only. CV/percentiles don't
    apply to a 2-element (node0, node1) comparison; this is directionally
    right but statistically weak with only 2 nodes.

    extra_exclude: ranks to drop before aggregating (P18b addition) --
    used to re-test whether a node-level discrepancy survives removing a
    rank that already has its own per-rank finding. If the discrepancy
    disappears once that rank is excluded, the per-rank fault fully
    explains the aggregate shift (double-counting). If it SURVIVES,
    something else on that node -- a separate host-level fault -- is
    also elevating the node's aggregate, and must not be suppressed just
    because one rank on the same node happened to fire too.

    Codebase audit, third pass -- was a hardcoded r<8/r>=8 split (this
    session's audit: still live, still real, despite the P18c Stage 4
    fix below only ever having fixed the CRASH on a missing rank, never
    this node-attribution assumption itself). Now groups ranks by their
    own real, discovered hostname (rank_hosts, from discover_rank_hosts)
    -- no assumption about node count or GPUs-per-node. This statistic
    is still fundamentally a 2-GROUP comparison (mean-of-means between
    exactly two aggregates, a real, separate statistical-design
    limitation this fix doesn't remove) -- but the two groups compared
    are now whichever two real hosts this run's own data actually
    contains (sorted, for a deterministic rather than arbitrary group
    order), not an assumed rank<8/rank>=8 split. A run whose data
    contains something other than exactly 2 real hosts degrades to the
    same 'not enough data to compare' shape already returned when either
    side has under 2 members -- an honest 'not checkable', never a
    silently wrong answer. Field names (worker0_mean/worker1_mean) are
    kept unchanged for every existing consumer (report.py and friends)
    -- they now label the first/second real discovered host, which for
    this project's own actual 2-node cluster is still, and always will
    be, worker-0/worker-1 (sorted order), so this is a pure
    generalization with zero behavior change on the topology already
    tested."""
    # P18c Stage 4 fix: iterate the ranks actually PRESENT in per_rank, not
    # a hardcoded range(0,8)/range(8,16) -- found via the sub-99%-coverage
    # test, where a rank missing entirely (not just partially covered)
    # crashed this with KeyError. That crash happened INSIDE score_bucket,
    # before classify()'s coverage guard (checked on score_bucket's
    # return value) ever got a chance to run -- so the guard was
    # completely unreachable for this exact case, not merely untested.
    exclude = EXCLUDE_ALWAYS | set(extra_exclude)
    means = {r: stat_mean(v) for r, v in per_rank.items()}
    real_hosts = sorted({rank_hosts[r] for r in means if r in rank_hosts})
    if len(real_hosts) != 2:
        return {"worker0_mean": None, "worker1_mean": None, "diff_sd_units": 0.0,
                "worker0_host": None, "worker1_host": None}
    host0, host1 = real_hosts
    w0 = [means[r] for r in means if rank_hosts.get(r) == host0 and r not in exclude]
    w1 = [means[r] for r in means if rank_hosts.get(r) == host1 and r not in exclude]
    if len(w0) < 2 or len(w1) < 2:
        return {"worker0_mean": None, "worker1_mean": None, "diff_sd_units": 0.0,
                "worker0_host": host0, "worker1_host": host1}
    mu0, sd0 = st.mean(w0), (st.stdev(w0) if len(w0) > 1 else 0.0)
    mu1, sd1 = st.mean(w1), (st.stdev(w1) if len(w1) > 1 else 0.0)
    pooled_sd = ((sd0 ** 2 + sd1 ** 2) / 2) ** 0.5
    diff_sd = (mu0 - mu1) / pooled_sd if pooled_sd > 0 else 0.0
    return {
        "worker0_mean": mu0, "worker0_sd": sd0,
        "worker1_mean": mu1, "worker1_sd": sd1,
        "diff_sd_units": diff_sd,
        # Codebase audit, third pass -- real discovered host names behind
        # the worker0_mean/worker1_mean labels (kept unchanged for every
        # existing consumer), so a caller that needs to REPORT which real
        # node was elevated (classifier.py's own node-vs-node block) no
        # longer has to assume "worker-0"/"worker-1" by name.
        "worker0_host": host0, "worker1_host": host1,
    }


def recheck_node_vs_node_excluding(means_by_rank, extra_exclude, rank_hosts):
    """Same aggregate-shift test as score_node_vs_node, but starting from
    already-computed per-rank means (e.g. primary['mean']['all_vals'])
    instead of raw dumps -- cheap enough to call from classify() without
    re-scanning dump files.

    Codebase audit, third pass -- same real-host-grouping fix as
    score_node_vs_node above, for the identical reason (see its own
    docstring)."""
    exclude = EXCLUDE_ALWAYS | set(extra_exclude)
    real_hosts = sorted({rank_hosts[r] for r in means_by_rank if r in rank_hosts})
    if len(real_hosts) != 2:
        return None
    host0, host1 = real_hosts
    w0 = [v for r, v in means_by_rank.items() if rank_hosts.get(r) == host0 and r not in exclude]
    w1 = [v for r, v in means_by_rank.items() if rank_hosts.get(r) == host1 and r not in exclude]
    if len(w0) < 2 or len(w1) < 2:
        return None
    mu0, sd0 = st.mean(w0), st.stdev(w0)
    mu1, sd1 = st.mean(w1), st.stdev(w1)
    pooled_sd = ((sd0 ** 2 + sd1 ** 2) / 2) ** 0.5
    diff_sd = (mu0 - mu1) / pooled_sd if pooled_sd > 0 else 0.0
    return {"worker0_mean": mu0, "worker1_mean": mu1, "diff_sd_units": diff_sd}


def score_bucket(dump_dirs, bucket):
    """Run all three statistics, node-scoped and node-vs-node, over ONE
    message size. Buckets are never pooled together for scoring -- each
    is its own distribution with its own absolute scale (established:
    bucket B is primary/trusted, bucket C is corroborating only).

    P21.5 -- goes through the comm-scoped path unconditionally now (the
    exact same code whether one communicator exists or several): scores
    EACH communicator found for this message size separately (its own
    coverage, computed against ITS OWN real member count, never a
    hardcoded N_RANKS), and returns one result per discovered
    communicator. The old flat, single-communicator-shaped return (a bare
    dict of stat_name -> result) is preserved as the value for whichever
    communicator is reported when exactly one exists, via
    _score_one_comm_result plus the back-compat unwrap below -- so a
    single-communicator job's caller sees literally the same shape as
    before, while a multi-communicator job's caller sees one such shape
    per comm_key. Nothing about score_node_scoped/score_node_vs_node/
    score_rank0_cv's own math changed -- only how many times, and over
    what per-communicator rank space, they're invoked.

    Codebase audit, third pass -- rank_hosts (real, discovered rank-
    >hostname identity, see discover_rank_hosts) is now computed once
    per call here and threaded down to score_node_vs_node/score_rank0_cv,
    replacing their old NODE_A/NODE_B-based node attribution."""
    by_comm = per_rank_series_by_comm(dump_dirs, {bucket})
    rank_hosts = discover_rank_hosts(dump_dirs)
    per_comm_results = {}
    for comm_key, (per_rank, n_ranks, dropped) in by_comm.items():
        per_comm_results[comm_key] = _score_one_comm_result(per_rank, n_ranks, dropped, rank_hosts)
    if not per_comm_results:
        # No data at all for this bucket, from any communicator -- same
        # "nothing to calibrate" shape callers already check for
        # (_coverage == 0.0), just without a comm_key to report it under.
        return _score_one_comm_result({}, None, 0, rank_hosts)
    if len(per_comm_results) == 1:
        # Single-communicator case: unwrap so existing callers (that only
        # ever knew one communicator existed) see the exact same flat
        # shape as before this fix.
        return next(iter(per_comm_results.values()))
    return {"_by_comm": per_comm_results}


def _score_one_comm_result(per_rank, n_ranks, dropped, rank_hosts):
    coverage = (len(per_rank) / n_ranks) if n_ranks else 0.0
    results = {}
    for stat_name in STATS:
        results[stat_name] = score_node_scoped(per_rank, stat_name)
        results[stat_name + "_per_node"] = score_node_scoped_per_node(per_rank, stat_name)
    results["node_vs_node"] = score_node_vs_node(per_rank, rank_hosts)
    results["cv_rank0_special"] = score_rank0_cv(per_rank, rank_hosts)
    results["_coverage"] = coverage
    results["_dropped_partial_sn"] = dropped
    results["_n_samples"] = {r: len(v) for r, v in per_rank.items()}
    return results


def score_all(dump_dirs, primary_bucket=None, corroborating_buckets=None):
    """Score the primary (trusted, calibration-selected) bucket, plus any
    corroborating buckets scored separately (not pooled) for cross-check.

    Cluster-topology-agnostic fix (this session): primary_bucket/
    corroborating_buckets used to default to BUCKET_B/(BUCKET_C,) -- one
    specific workload's own real message sizes, silently wrong for any
    other (the same failure pattern already fixed elsewhere in this
    project -- workload_signature, coverage_guard's rate baseline). None
    now means "discover THIS run's own real buckets"
    (select_primary_corroborating_buckets) -- an explicit bucket
    selection is still honored unchanged for callers that already know
    which one they want (e.g. a calibration workflow re-scoring a
    specific already-selected bucket)."""
    if primary_bucket is None and corroborating_buckets is None:
        primary_bucket, corroborating_buckets = select_primary_corroborating_buckets(dump_dirs)
        if primary_bucket is None:
            return {"primary": None, "corroborating": {}}
    elif corroborating_buckets is None:
        corroborating_buckets = ()
    out = {"primary": score_bucket(dump_dirs, primary_bucket)}
    out["corroborating"] = {b: score_bucket(dump_dirs, b) for b in corroborating_buckets}
    return out
