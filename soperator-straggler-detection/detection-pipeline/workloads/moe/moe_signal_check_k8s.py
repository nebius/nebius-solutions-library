#!/usr/bin/env python3
"""moe_signal_check_k8s.py -- K8s counterpart to the Slurm moe_signal_check.py.

Reads Inspector JSONL dump files from a directory (copied from K8s training pod
/inspector-dumps via kubectl cp) and computes per-rank weighted-mean recv-wait
and send-wait intervals from the proxy_peer_wait blocks added by the §6.41 fix.

The straggler appears with the LOWEST recv-wait (z-score -8 to -13 at 20ms/100ms
fault, vs ±1.1 healthy noise band on Slurm). This script reproduces that analysis
on K8s dump data to validate the signal transfers across platforms.

Usage:
  python3 moe_signal_check_k8s.py <dump_dir> [--target-rank N] [--verbose]

  <dump_dir>: directory containing *.log Inspector dump files, as copied from pods.
              Can also be a parent directory containing per-node subdirectories.
  --target-rank N: highlight this rank's stats (the injected fault rank); default auto-detect.
  --verbose: print per-peer breakdown for the target rank.

Slurm reference (from DESIGN_NOTES §6.41.4):
  Healthy: z ≈ -1.1 to +0.06
  20ms fault: z ≈ -8 to -13
  100ms fault: z ≈ -10 to -11
"""
import argparse, glob, json, math, os, statistics as st, sys
from collections import defaultdict


def find_dumps(dump_dir):
    """Find all *.log Inspector dump files under dump_dir (recursive)."""
    files = []
    for root, _, fnames in os.walk(dump_dir):
        for f in fnames:
            if f.endswith(".log"):
                files.append(os.path.join(root, f))
    return sorted(files)


def parse_dumps(files, verbose=False):
    """
    Returns: {rank: {'recv': [durUsec, ...], 'send': [durUsec, ...]}}
    aggregated over all files and all collectives.
    """
    rank_wait = defaultdict(lambda: {'recv': [], 'send': []})
    proxy_records = 0
    total_records = 0
    pid_skip_counts = []

    for fpath in files:
        with open(fpath, errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_records += 1

                # Cross-process skip count (from Inspector logs, not dump files)
                if 'cross_process_skip_count' in rec:
                    pid_skip_counts.append(rec['cross_process_skip_count'])
                    continue

                ppw = rec.get('proxy_peer_wait')
                if not ppw:
                    continue
                proxy_records += 1

                # rank is the recorder (the local rank that produced this dump)
                recorder_rank = rec.get('rank', rec.get('nrank'))
                if recorder_rank is None:
                    continue

                send_list = ppw.get('send', [])
                recv_list = ppw.get('recv', [])

                # send_list[peer] = {count, sum_us, max_us}
                for peer_idx, peer_data in enumerate(send_list):
                    if isinstance(peer_data, dict):
                        s = peer_data.get('sum_us', 0)
                        c = peer_data.get('count', 0)
                        if c > 0:
                            rank_wait[recorder_rank]['send'].append(s / c)
                    elif isinstance(peer_data, (int, float)) and peer_data > 0:
                        rank_wait[recorder_rank]['send'].append(float(peer_data))

                for peer_idx, peer_data in enumerate(recv_list):
                    if isinstance(peer_data, dict):
                        s = peer_data.get('sum_us', 0)
                        c = peer_data.get('count', 0)
                        if c > 0:
                            rank_wait[recorder_rank]['recv'].append(s / c)
                    elif isinstance(peer_data, (int, float)) and peer_data > 0:
                        rank_wait[recorder_rank]['recv'].append(float(peer_data))

    if verbose:
        print(f"  Files: {len(files)}, total records: {total_records}, "
              f"proxy_peer_wait records: {proxy_records}")
    return dict(rank_wait)


def parse_dumps_v2(files, verbose=False):
    """
    Alternative parser for the actual dump format from inspector.cc's
    inspectorDumpProxyPeerWait: looks for arrays of {count, sum_us, max_us}
    per peer indexed by peer rank.
    """
    rank_recv_means = defaultdict(list)   # rank -> [mean_recv_us_per_collective]
    rank_send_means = defaultdict(list)
    proxy_records = 0
    total_records = 0
    cross_process_skips = 0

    for fpath in files:
        with open(fpath, errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Check for cross-process skip log lines (plain text, not JSON)
                if 'cross-process context skipped' in line or 'ProxyOp cross-process' in line:
                    cross_process_skips += 1
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_records += 1

                ppw = rec.get('proxy_peer_wait')
                if not ppw:
                    continue
                proxy_records += 1

                # rank is in header (confirmed from actual dump format)
                recorder_rank = rec.get('header', {}).get('rank')
                if recorder_rank is None:
                    continue

                # Aggregate: mean wait per collective across all peers
                send_data = ppw.get('send', [])
                recv_data = ppw.get('recv', [])

                for entry in send_data:
                    if isinstance(entry, dict):
                        # Actual format from inspector.cc: {peer, count, mean_us, max_us}
                        m = entry.get('mean_us', entry.get('sum_us', 0) / max(1, entry.get('count', 1)))
                        if m and m > 0:
                            rank_send_means[recorder_rank].append(float(m))
                    elif isinstance(entry, (int, float)) and entry > 0:
                        rank_send_means[recorder_rank].append(float(entry))

                for entry in recv_data:
                    if isinstance(entry, dict):
                        m = entry.get('mean_us', entry.get('sum_us', 0) / max(1, entry.get('count', 1)))
                        if m and m > 0:
                            rank_recv_means[recorder_rank].append(float(m))
                    elif isinstance(entry, (int, float)) and entry > 0:
                        rank_recv_means[recorder_rank].append(float(entry))

    if verbose:
        print(f"  Files: {len(files)}, total JSON records: {total_records}, "
              f"proxy_peer_wait records: {proxy_records}")
        if cross_process_skips > 0:
            print(f"  Cross-process skip log lines: {cross_process_skips} "
                  f"(PID-mismatch guard firing -- expected on K8s)")

    return dict(rank_recv_means), dict(rank_send_means)


def zscore_analysis(rank_means, label, target_rank=None):
    """Compute per-rank mean, then z-score of each rank vs peer group."""
    if not rank_means:
        print(f"  {label}: no data found")
        return

    # Per-rank mean
    per_rank_mean = {r: st.mean(vals) for r, vals in rank_means.items() if vals}
    if len(per_rank_mean) < 2:
        print(f"  {label}: only {len(per_rank_mean)} rank(s) with data -- need ≥2 for z-score")
        return

    all_means = list(per_rank_mean.values())
    group_mean = st.mean(all_means)
    group_sd = st.stdev(all_means) if len(all_means) > 1 else 0

    print(f"\n  {label} (per-rank mean wait, group mean={group_mean:.1f}µs, "
          f"sd={group_sd:.1f}µs, n_ranks={len(per_rank_mean)}):")

    sorted_ranks = sorted(per_rank_mean)
    for rank in sorted_ranks:
        mean_us = per_rank_mean[rank]
        z = (mean_us - group_mean) / group_sd if group_sd > 0 else 0
        marker = ""
        if target_rank is not None and rank == target_rank:
            marker = " <-- TARGET RANK"
        elif group_sd > 0 and abs(z) > 3:
            marker = " <-- OUTLIER"
        print(f"    rank {rank:3d}: mean={mean_us:8.1f}µs  z={z:+6.2f}{marker}")

    if target_rank is not None and target_rank in per_rank_mean:
        tmean = per_rank_mean[target_rank]
        tz = (tmean - group_mean) / group_sd if group_sd > 0 else 0
        ratio = tmean / group_mean if group_mean > 0 else 0
        print(f"\n  TARGET RANK {target_rank}: mean={tmean:.1f}µs  "
              f"z={tz:+.2f}  ratio_to_group={ratio:.3f}")
        print(f"  Slurm reference: z=-8 to -13 at 20ms/100ms fault, z≈±1.1 healthy")
        if abs(tz) > 5:
            print(f"  ** STRONG SIGNAL: z={tz:+.2f} is well below the ±1.1 healthy band **")
        elif abs(tz) > 2:
            print(f"  ** MODERATE SIGNAL: z={tz:+.2f} -- above noise floor, below strong threshold **")
        else:
            print(f"  ** WITHIN HEALTHY NOISE BAND: z={tz:+.2f} **")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump_dir", help="Directory containing Inspector dump .log files")
    ap.add_argument("--target-rank", type=int, default=None,
                    help="Rank to highlight as the fault target (default: auto-detect lowest recv-wait)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.dump_dir):
        print(f"ERROR: {args.dump_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    files = find_dumps(args.dump_dir)
    if not files:
        print(f"ERROR: no .log files found under {args.dump_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {len(files)} dump files from: {args.dump_dir}")
    if args.verbose:
        for f in files[:5]:
            print(f"  {f}")
        if len(files) > 5:
            print(f"  ... and {len(files)-5} more")

    # Check for PID-mismatch guard firing in dump file content
    cross_skip = 0
    for f in files:
        try:
            content = open(f, errors='replace').read()
            if 'ProxyOp cross-process' in content or 'cross-process context skipped' in content:
                cross_skip += 1
        except Exception:
            pass
    if cross_skip > 0:
        print(f"\nPID-mismatch guard: firing in {cross_skip}/{len(files)} dump files")
        print("  (expected on K8s -- confirms S6.41 guard is active and load-bearing)")
    else:
        print("\nPID-mismatch guard: not observed in dump file content")
        print("  (may be in stdout logs rather than dump files; check kubectl logs)")

    rank_recv, rank_send = parse_dumps_v2(files, verbose=args.verbose)

    if not rank_recv and not rank_send:
        print("\nNo proxy_peer_wait data found in dump files.")
        print("Possible causes:")
        print("  1. Plugin was not rebuilt with proxy-event code (check nm for ProxyPeerWait symbols)")
        print("  2. NCCL_IB_DISABLE=1 is set but proxy events require IB transport (check if TCP-only)")
        print("  3. MoE AllToAll not running (wrong workload or model config)")
        print("  4. Dump files are from a run without the patched plugin")
        sys.exit(1)

    # Auto-detect target rank if not specified
    target_rank = args.target_rank
    if target_rank is None and rank_recv:
        per_rank_means = {r: st.mean(vals) for r, vals in rank_recv.items() if vals}
        if per_rank_means:
            target_rank = min(per_rank_means, key=per_rank_means.get)
            print(f"\nAuto-detected lowest recv-wait rank: {target_rank} "
                  f"(mean={per_rank_means[target_rank]:.1f}µs)")

    zscore_analysis(rank_recv, "recv-wait (straggler = LOWEST)", target_rank)
    zscore_analysis(rank_send, "send-wait", target_rank)


if __name__ == "__main__":
    main()
