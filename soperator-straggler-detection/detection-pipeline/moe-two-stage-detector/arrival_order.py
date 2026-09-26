#!/usr/bin/env python3
"""Step 2 -- real "who arrived last at the collective" signal, built from
Inspector's own real per-record timestamps (event_trace_ts.coll_start_ts
and the kernel_events' own kernel_start_ts), NOT from coll_exec_time_us
(the metric already proven unreliable for single-rank localization inside
a synchronous collective).

Real, generic grouping: MoE's dispatch/combine are each one
all_to_all_single call, which NCCL's profiler records as one Send (and/or
Recv) record per destination peer, per rank, all sharing the same real
coll_msg_size_bytes for a given call (since a single all_to_all_single
call splits one tensor by peer -- the split SIZES vary per destination,
but which CALL a record belongs to is identified by (coll type, an
ordinal position among that rank's own records with a message size in
the same "wave"), not by a shared coll_sn across ranks (coll_sn is a
per-rank-local counter, not a cross-rank correlation id).

Simpler, robust, real approach used here: for a rank's OWN dump file, its
records naturally arrive in real chronological (dump) order. Consecutive
records whose coll_msg_size_bytes stays in the "same call" size regime
(tiny fixed count-exchange vs the real, larger, per-round-varying token
dispatch) are grouped by proximity in real wall-clock time (coll_start_ts
gaps): a genuine gap of, say, several times the per-round Send/Recv
spacing marks a new forward-pass round starting. This needs no assumption
about exact NCCL send-count per call -- it reads real recorded structure.
"""
import json
import glob
import sys
from collections import defaultdict

TINY_MSG_BYTES = 64  # a real, tiny (~world_size int64) count-exchange call
                      # stays well under this; the real token dispatch is
                      # always vastly larger (thousands of bytes+) for any
                      # non-trivial batch/embedding size.


def load_records(dump_dirs):
    """Real records, one rank's own file at a time, in real file (=real
    chronological dump) order -- Inspector writes append-only per pid."""
    per_rank = defaultdict(list)
    for d in dump_dirs:
        for fp in glob.glob(d + "/*.log"):
            with open(fp) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            if not lines:
                continue
            rank = lines[0]["header"]["rank"]
            for rec in lines:
                cp = rec["coll_perf"]
                if cp["coll"] not in ("Send", "Recv"):
                    continue
                per_rank[rank].append({
                    "coll": cp["coll"],
                    "msg_size": cp["coll_msg_size_bytes"],
                    "coll_start_ts": cp["event_trace_ts"]["coll_start_ts"],
                    "kernel_start_ts": cp["event_trace_ts"]["kernel_events"][0]["kernel_start_ts"]
                                       if cp["event_trace_ts"].get("kernel_events") else None,
                })
    return per_rank


def find_dispatch_rounds(per_rank):
    """For each rank, split its real Send/Recv record stream into rounds:
    a new round starts at each TINY (count-exchange) record following a
    non-tiny one, or at the first record. Returns, per rank, a list of
    rounds, each round = list of its real "dispatch-phase" (non-tiny)
    records with their real timestamps."""
    rounds_per_rank = {}
    for rank, recs in per_rank.items():
        rounds = []
        current = []
        in_dispatch = False
        for r in recs:
            is_tiny = r["msg_size"] <= TINY_MSG_BYTES
            if is_tiny:
                if current:
                    rounds.append(current)
                    current = []
                in_dispatch = False
            else:
                current.append(r)
                in_dispatch = True
        if current:
            rounds.append(current)
        rounds_per_rank[rank] = rounds
    return rounds_per_rank


def real_round_start_ts(round_records):
    """This rank's real entry time into this round's dispatch phase: the
    EARLIEST coll_start_ts among its own Send/Recv records for the round
    (the first thing it does once ready to participate)."""
    return min(r["coll_start_ts"] for r in round_records)


def last_arriving_rank(rounds_per_rank, round_index):
    """Across all ranks that have a round at round_index, real evidence:
    which rank's real coll_start_ts for that round is LATEST (arrived
    last)? Returns (rank, sorted_list_of_(rank, start_ts))."""
    entries = []
    for rank, rounds in rounds_per_rank.items():
        if round_index < len(rounds):
            entries.append((rank, real_round_start_ts(rounds[round_index])))
    if not entries:
        return None, []
    entries.sort(key=lambda kv: kv[1])
    return entries[-1][0], entries


if __name__ == "__main__":
    dump_dirs = sys.argv[1:]
    per_rank = load_records(dump_dirs)
    print("ranks found:", sorted(per_rank.keys()))
    rounds_per_rank = find_dispatch_rounds(per_rank)
    for r in sorted(rounds_per_rank.keys()):
        print(f"rank {r}: {len(rounds_per_rank[r])} dispatch rounds found")
    n_rounds = min(len(v) for v in rounds_per_rank.values())
    print(f"\ncommon rounds across all ranks: {n_rounds}")
    for ri in range(min(5, n_rounds)):
        worst, entries = last_arriving_rank(rounds_per_rank, ri)
        spread_us = (entries[-1][1] - entries[0][1])
        print(f"round {ri}: last_arriving_rank={worst}  spread_us={spread_us:.0f}  "
              f"order(earliest->latest)={[e[0] for e in entries]}")
