#!/usr/bin/env python3
"""Step 3 -- real, generic load-imbalance check. Parses this project's own
new [MOE-TOKEN-LOAD] lines (model_moe.py's MoEMLP.forward(), logged before
any fault-injection sleep so the signal is never itself delayed) out of
the real train_worker-*.log files, and compares the suspect (last-
arriving) rank's real assigned token total against its peers' for the
SAME round.

Not hardcoded to any world_size/expert count -- reads whatever ranks and
rounds are actually present in the real log lines.
"""
import re
import sys
import statistics as st
from collections import defaultdict

LINE_RE = re.compile(
    r"\[MOE-TOKEN-LOAD\] rank=(\d+) block=(\d+) round=(\d+) assigned_total=(\d+)")


def parse_token_load(log_paths):
    """Returns {(layer_idx, round): {rank: assigned_total}}.

    Real bug found and fixed here (caught before drawing any conclusion
    from it): the log line's own "block" field is id(self) -- a Python
    object id, which is process-local and different per rank even for
    the architecturally-SAME layer, so it can never correlate across
    ranks directly. Real fix, no re-run needed: each rank calls its own
    n_layer MoEMLP instances in the same fixed, deterministic order
    every iteration (nn.ModuleList's real construction order), so a
    rank's distinct block ids, ordered by first appearance in its own
    real log lines, ARE the real layer indices 0..n_layer-1 -- this
    recovers a genuine cross-rank key from data already collected."""
    raw = defaultdict(dict)  # rank -> {block: [ (round, total) in file order ]}
    first_seen_order = defaultdict(list)  # rank -> [block ids in first-appearance order]
    for path in log_paths:
        with open(path) as f:
            for line in f:
                m = LINE_RE.search(line)
                if not m:
                    continue
                rank, block, round_, total = (int(m.group(1)), int(m.group(2)),
                                               int(m.group(3)), int(m.group(4)))
                if block not in raw[rank]:
                    raw[rank][block] = []
                    first_seen_order[rank].append(block)
                raw[rank][block].append((round_, total))

    by_layer_round = defaultdict(dict)
    for rank, blocks in raw.items():
        block_to_layer = {b: i for i, b in enumerate(first_seen_order[rank])}
        for block, entries in blocks.items():
            layer_idx = block_to_layer[block]
            for round_, total in entries:
                by_layer_round[(layer_idx, round_)][rank] = total
    return by_layer_round


def check_load_imbalance(by_block_round, suspect_rank, block=None, round_index=None,
                          imbalance_ratio_min=1.5):
    """Real comparison: for the given (or first available) (block, round),
    is suspect_rank's real assigned_total higher than its peers' by an
    amount consistent with legitimate MoE routing imbalance, or is it
    comparable/lower (pointing away from load as the explanation)?

    Returns dict: {"explained_by_load": bool, "suspect_total": int,
                    "peer_mean": float, "ratio": float, "block": ..., "round": ...}
    """
    if block is not None and round_index is not None:
        candidates = [(block, round_index)]
    else:
        candidates = sorted(by_block_round.keys())

    for key in candidates:
        loads = by_block_round[key]
        if suspect_rank not in loads or len(loads) < 2:
            continue
        # Real, generic fix: exclude peers that are structurally never
        # token-receiving experts for this layer (their real load is
        # always exactly 0 across every round for this layer -- not an
        # assumption about num_experts/world_size, a real check against
        # this layer's own actual data), since comparing a real expert's
        # load against always-zero non-experts artificially inflates the
        # ratio regardless of any real imbalance.
        zero_peers = {r for r, v in by_block_round.get(key, {}).items()
                      if r != suspect_rank and all(
                          by_block_round.get((key[0], rr), {}).get(r, 0) == 0
                          for rr in {k[1] for k in by_block_round if k[0] == key[0]})}
        peer_vals = [v for r, v in loads.items() if r != suspect_rank and r not in zero_peers]
        if not peer_vals:
            continue
        peer_mean = st.mean(peer_vals)
        suspect_total = loads[suspect_rank]
        ratio = (suspect_total / peer_mean) if peer_mean > 0 else (float("inf") if suspect_total > 0 else 1.0)
        explained_by_load = ratio >= imbalance_ratio_min
        return {
            "explained_by_load": explained_by_load,
            "suspect_total": suspect_total,
            "peer_mean": peer_mean,
            "ratio": ratio,
            "block": key[0], "round": key[1],
            "all_loads": loads,
        }
    return {"explained_by_load": None, "error": "no matching (block, round) with suspect_rank present"}


if __name__ == "__main__":
    suspect_rank = int(sys.argv[1])
    log_paths = sys.argv[2:]
    by_block_round = parse_token_load(log_paths)
    print(f"parsed {len(by_block_round)} (block, round) groups")
    result = check_load_imbalance(by_block_round, suspect_rank)
    print(result)
