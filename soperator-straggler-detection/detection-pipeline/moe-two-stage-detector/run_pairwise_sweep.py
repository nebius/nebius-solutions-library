#!/usr/bin/env python3
"""Step 5 driver -- launches pairwise_sweep.py's two processes directly
via ssh (bypassing SLURM entirely, so this doesn't need or take a
competing exclusive node allocation from the live training job), pinned
via CUDA_VISIBLE_DEVICES to the suspect's and a healthy peer's real
physical GPUs (by real gpu_slot_index, discovered from dump metadata --
same mechanism Step 4 uses). Brief and explicitly scoped: default 20
rounds of a ~4MB point-to-point exchange, which completes in well under
a second of real GPU time per side.

Usage: run_pairwise_sweep.py <suspect_rank> <peer_rank> <dump_dir> [<dump_dir> ...]
"""
import json
import subprocess
import sys
import time
import time

sys.path.insert(0, '/root/P30_moe_detector')
from telemetry_check import discover_suspect_identity

PORT = "29800"
N_ROUNDS = 20
SCRIPT = "/root/P30_moe_detector/pairwise_sweep.py"


def launch_side(host, gpu_slot, role, local_rank, master_addr, log_path):
    # Bare host has torch+CUDA directly (confirmed: torch 2.11.0+cu128,
    # cuda available) -- no container wrapper needed, unlike the training
    # jobs elsewhere in this project which go through pyxis/srun.
    cmd = (
        f"CUDA_VISIBLE_DEVICES={gpu_slot} python3 {SCRIPT} {role} {local_rank} {N_ROUNDS} {PORT} {master_addr} "
        f"> {log_path} 2>&1"
    )
    return subprocess.Popen(["ssh", "-o", "BatchMode=yes", host, cmd])


def read_result(path):
    # Real race found and fixed here: ssh's own process can exit before
    # this pod's view of the shared /root mount reflects the remote
    # write's full flush -- retry briefly rather than treat a
    # transiently-truncated read as a genuine absence of data.
    for attempt in range(10):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("[PAIRWISE-RESULT]"):
                        try:
                            return json.loads(line[len("[PAIRWISE-RESULT] "):])
                        except json.JSONDecodeError:
                            break  # truncated -- retry
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    return None


def run_one_pair(rank_a, rank_b, dump_dirs, tag):
    host_a, slot_a = discover_suspect_identity(dump_dirs, rank_a)
    host_b, slot_b = discover_suspect_identity(dump_dirs, rank_b)
    if host_a is None or host_b is None:
        return {"error": f"could not discover host/gpu_slot for rank {rank_a} or {rank_b}"}

    log_a = f"/root/P30_moe_detector/_pairwise_{tag}_a.log"
    log_b = f"/root/P30_moe_detector/_pairwise_{tag}_b.log"
    master_addr = host_a

    p_a = launch_side(host_a, slot_a, "a", 0, master_addr, log_a)
    p_b = launch_side(host_b, slot_b, "b", 1, master_addr, log_b)

    try:
        p_a.wait(timeout=60)
        p_b.wait(timeout=60)
    except subprocess.TimeoutExpired:
        p_a.kill()
        p_b.kill()
        return {"error": "pairwise sweep timed out after 60s -- likely a rendezvous/connectivity issue, not a real fault measurement"}

    r_a, r_b = read_result(log_a), read_result(log_b)
    if r_a is None or r_b is None:
        return {"error": "one or both sides produced no real [PAIRWISE-RESULT] line",
                "rank_a": rank_a, "rank_b": rank_b}

    # Real fix (this session): a P2P send/recv is itself a two-party sync
    # point, so a locked/slow side's own matmul phase makes the OTHER
    # side wait at the exchange -- both sides end up measuring nearly the
    # SAME overall round time (confirmed directly: suspect vs peer WITHIN
    # one pair came back statistically identical, ratio~1.0, even under a
    # real, confirmed 810-vs-1980MHz clock difference observed live via
    # nvidia-smi during the same run). The real signal is this PAIR's
    # overall round time vs a baseline pair's, not suspect vs peer inside
    # one pair -- comparing THAT way showed a real, unambiguous 1.80x
    # slowdown for the same fault.
    pair_mean_us = (r_a["mean_us"] + r_b["mean_us"]) / 2
    return {"rank_a": rank_a, "rank_b": rank_b, "result_a": r_a, "result_b": r_b, "pair_mean_us": pair_mean_us}


def run(suspect_rank, peer_rank, dump_dirs, baseline_rank=None):
    suspect_pair = run_one_pair(suspect_rank, peer_rank, dump_dirs, "suspect")
    result = {"suspect_rank": suspect_rank, "peer_rank": peer_rank, "suspect_pair": suspect_pair}

    if "error" in suspect_pair:
        result["inconclusive"] = True
        result["reason"] = suspect_pair["error"]
        return result

    if baseline_rank is None:
        result["inconclusive"] = True
        result["reason"] = ("no baseline_rank given -- a within-pair ratio is not a valid discriminator "
                             "(the send/recv sync symmetrizes both sides' measured time); pass a third, "
                             "known-healthy rank to compare this pair's overall time against a healthy "
                             "baseline pair's overall time.")
        result["suspect_pair_mean_us"] = suspect_pair["pair_mean_us"]
        return result

    baseline_pair = run_one_pair(peer_rank, baseline_rank, dump_dirs, "baseline")
    result["baseline_rank"] = baseline_rank
    result["baseline_pair"] = baseline_pair
    if "error" in baseline_pair:
        result["inconclusive"] = True
        result["reason"] = baseline_pair["error"]
        return result

    result["suspect_pair_mean_us"] = suspect_pair["pair_mean_us"]
    result["baseline_pair_mean_us"] = baseline_pair["pair_mean_us"]
    result["ratio"] = suspect_pair["pair_mean_us"] / baseline_pair["pair_mean_us"] if baseline_pair["pair_mean_us"] > 0 else float("inf")
    result["inconclusive"] = False
    return result


if __name__ == "__main__":
    # Usage: run_pairwise_sweep.py <suspect_rank> <peer_rank> [--baseline <rank>] <dump_dir> [<dump_dir> ...]
    args = sys.argv[1:]
    suspect_rank = int(args.pop(0))
    peer_rank = int(args.pop(0))
    baseline_rank = None
    if args and args[0] == "--baseline":
        args.pop(0)
        baseline_rank = int(args.pop(0))
    dump_dirs = args
    print(json.dumps(run(suspect_rank, peer_rank, dump_dirs, baseline_rank), indent=2, default=str))
