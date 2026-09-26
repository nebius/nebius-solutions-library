#!/usr/bin/env python3
"""Verify CV (window=125, trim=4, persistence 2-of-3) via PromQL export +
downstream scoring, matching detection.py/classifier.py exactly.

Stage 2 cluster-topology-agnostic fix: the VM endpoint used to default to
a hardcoded, stale, long-defunct local port (127.0.0.1:8610) from early in
this project's history -- silently pointing at a dead address on any
other host. There's no live pipeline caller to derive it from (this tool
is a standalone, manual CLI, only ever invoked directly -- confirmed via
INVENTORY.md Category A: node_aggregator_ref.py only imports stat_cv from
this file, never runs it as a script), so it's now a required CLI
argument instead of a silent default -- matching every other tool under
tools/."""
import urllib.request
import urllib.parse
import json
import statistics as st

WINDOW = 125
TRIM = 4
PERSIST_WINDOW = 3
PERSIST_REQUIRED = 2
CV_Z_THRESH = 20.0
EXCLUDE_ALWAYS = {"3"}
EXCLUDE_CV_EXTRA = {"0"}
RANK0_CV_PEERS = {"1", "2", "4", "5", "6", "7"}


def export_all_ranks(vm_url, bucket, start, end, hostname_filter=None):
    match = f'{{__name__="nccl_collective_exec_time_microseconds",collective="AllReduce",message_size_bytes="{bucket}"}}'
    if hostname_filter:
        match = match[:-1] + f',hostname="{hostname_filter}"}}'
    q = urllib.parse.urlencode({"match[]": match, "start": start, "end": end})
    url = f"{vm_url}/api/v1/export?{q}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        lines = [l for l in resp.read().decode().strip().split("\n") if l]
    out = {}
    rank_hosts = {}
    for l in lines:
        d = json.loads(l)
        rank = d["metric"]["rank"]
        rank_hosts[rank] = d["metric"].get("hostname")
        pairs = sorted(zip(d["timestamps"], d["values"]))
        out[rank] = [v for _, v in pairs]
    return out, rank_hosts


def stat_cv(vals, trim=TRIM):
    s = sorted(vals)
    if len(s) > 2 * trim + 10:
        s = s[trim:-trim] if trim else s
    mu = st.mean(s)
    return (st.stdev(s) / mu) if mu and len(s) > 1 else 0.0


def windows(vals, window_size):
    n = len(vals)
    return [vals[i:i + window_size] for i in range(0, n - window_size + 1, window_size)]


def cv_persistence_check(all_ranks, exclude, node_ranks):
    """Returns (fired, first_firing_window_idx, per_rank_history)."""
    per_rank_windows = {r: windows(v, WINDOW) for r, v in all_ranks.items() if r in node_ranks and r not in exclude}
    if not per_rank_windows:
        return False, None, None
    n_windows = min(len(w) for w in per_rank_windows.values())
    history = {r: [] for r in per_rank_windows}
    fired_at = None
    for i in range(n_windows):
        cvs = {r: stat_cv(per_rank_windows[r][i]) for r in per_rank_windows}
        worst = max(cvs, key=lambda r: cvs[r])
        peers = [r for r in per_rank_windows if r != worst]
        peer_vals = [cvs[r] for r in peers]
        mu, sd = st.mean(peer_vals), (st.stdev(peer_vals) if len(peer_vals) > 1 else 0)
        z = (cvs[worst] - mu) / sd if sd > 0 else (float("inf") if cvs[worst] > mu else 0.0)
        for r in per_rank_windows:
            history.setdefault(r, [])
        hist = history.setdefault(worst, [])
        hist.append(z > CV_Z_THRESH)
        if len(hist) > PERSIST_WINDOW:
            hist.pop(0)
        if sum(hist) >= PERSIST_REQUIRED and fired_at is None:
            fired_at = i
    return fired_at is not None, fired_at, history


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 6:
        print("usage: promql_cv_verify.py <vm_url> <bucket> <start> <end> <label>", file=sys.stderr)
        sys.exit(1)
    vm_url, bucket, start, end, label = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
    data, rank_hosts = export_all_ranks(vm_url, bucket, start, end)
    # Stage 2 cluster-topology-agnostic fix: "node A" used to be a
    # hardcoded `int(rank) < 8` split -- silently wrong on any cluster
    # with a different rank-to-node layout. Now the first real,
    # discovered hostname actually present in this export's own data
    # (sorted, for a deterministic choice), matching the same
    # first-real-host convention used elsewhere in this project
    # (e.g. alert_engine.py's _find_true_rank0_member).
    real_hosts = sorted({h for h in rank_hosts.values() if h})
    first_host = real_hosts[0] if real_hosts else None
    node_a = {r for r in data if rank_hosts.get(r) == first_host}
    fired, idx, _ = cv_persistence_check(data, EXCLUDE_ALWAYS | EXCLUDE_CV_EXTRA, node_a)
    print(f"[{label}] main CV check (host={first_host}): fired={fired} at window {idx}")

    # P19a correction: rank0's ACTUAL current detection statistic is
    # score_rank0_outlier_rate (rate of low-tail outliers, whole-run, NOT
    # CV -- score_rank0_cv was proven unable to separate the populations
    # and retired as a firing gate in P18f/P18g; using it here would be
    # verifying logic the classifier no longer actually runs).
    OUTLIER_K = 3.0
    RANK0_OUTLIER_RATE_THRESH = 0.03

    def stat_outlier_count(vals, k=OUTLIER_K):
        med = st.median(vals)
        thresh = med / k
        return sum(1 for v in vals if v < thresh)

    if "0" in data:
        n0 = len(data["0"])
        worst_rate = stat_outlier_count(data["0"]) / n0
        peer_data = {r: data[r] for r in RANK0_CV_PEERS if r in data}
        peer_rates = [stat_outlier_count(v) / len(v) for v in peer_data.values()]
        mu, sd = st.mean(peer_rates), (st.stdev(peer_rates) if len(peer_rates) > 1 else 0)
        z = (worst_rate - mu) / sd if sd > 0 else (float("inf") if worst_rate > mu else 0.0)
        fired_r0 = worst_rate > RANK0_OUTLIER_RATE_THRESH
        print(f"[{label}] rank0 outlier_rate check: rate={worst_rate:.4f} z={z:.2f} fired={fired_r0}")
