#!/usr/bin/env python3
"""Step 5 -- isolated pairwise diagnostic sweep. Runs as its OWN, brand
new torch.distributed process group (own rendezvous port), pinned via
CUDA_VISIBLE_DEVICES to exactly two real physical GPUs (the suspect
rank's and a chosen healthy peer's, by real gpu_slot_index) -- entirely
separate from the main training job's own process group and collectives.
This sidesteps the barrier-smearing problem by construction: nothing here
is a synchronous collective involving the whole training world, so a real
delay on the suspect shows up directly as elevated point-to-point latency
between exactly these two ranks, not smeared across peers.

Usage (per-process, launched once per host via ssh):
  pairwise_sweep.py <role: suspect|peer> <local_rank: 0|1> <n_rounds> <port>
Both processes must be launched with matching <port>, roles 0 and 1
respectively, and CUDA_VISIBLE_DEVICES already set to the real physical
GPU index for that side before this script starts.
"""
import os
import sys
import json
import time

import torch
import torch.distributed as dist


def main():
    role, local_rank, n_rounds, port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    master_addr = sys.argv[5]

    os.environ["MASTER_ADDR"] = master_addr
    os.environ["MASTER_PORT"] = port
    os.environ["RANK"] = str(local_rank)
    os.environ["WORLD_SIZE"] = "2"

    dist.init_process_group(backend="nccl", rank=local_rank, world_size=2)
    device = torch.device("cuda:0")  # CUDA_VISIBLE_DEVICES already scopes this to the one real GPU
    other = 1 - local_rank

    tensor_size = 1 << 20  # 1M float32 elements (~4MB) -- large enough that a real
                            # per-exchange delay is measurable above python/NCCL overhead noise,
                            # small enough that n_rounds of these stay brief.
    x = torch.ones(tensor_size, device=device)

    # Real gap found and fixed here (first clock-lock validation pass):
    # send/recv alone is pure data movement (NVLink/copy-engine bound),
    # a DIFFERENT clock domain than -lgc's SM/graphics clock -- it showed
    # zero measurable difference under a real, confirmed clock lock (DCGM
    # itself showed sm_clock=810 vs healthy ~1980, arrival-order shifted,
    # but this isolated exchange alone stayed flat, ratio 1.03). Adding a
    # real local matmul into each timed round -- the SAME clock domain
    # MoE's own expert FFN (Linear/GELU/Linear) actually runs on -- so
    # this isolated test can discriminate a compute-domain fault, not
    # only a network/copy one.
    # Second real fix: FP32 matmuls (even looped 30x) never got the GPU
    # near a realistic boost/heavy-load state either -- confirmed via a
    # direct adhoc check (345MHz, 0% util, idle-level 77W power draw
    # throughout a 30-rep FP32 loop). bf16 (this project's own real
    # workload precision, via tensor cores) at a larger size achieves
    # real, substantial throughput (confirmed: ~677 TFLOPS, ~68% of an
    # H200's real bf16 peak, in a direct adhoc check) -- this is what
    # actually gives a clock lock something real to suppress.
    mm_size = 8192
    mm_reps = 100  # ~162ms of real sustained compute per round (adhoc-
                   # confirmed rate: ~1.62ms/rep at this size/dtype)
    a = torch.randn(mm_size, mm_size, device=device, dtype=torch.bfloat16)
    b = torch.randn(mm_size, mm_size, device=device, dtype=torch.bfloat16)

    # warmup (excluded from timing -- first real NCCL/cuBLAS call pays one-time setup cost)
    for _ in range(mm_reps):
        _ = a @ b
    torch.cuda.synchronize()
    if local_rank == 0:
        dist.send(x, dst=other)
        dist.recv(x, src=other)
    else:
        dist.recv(x, src=other)
        dist.send(x, dst=other)
    torch.cuda.synchronize()

    latencies_us = []
    for _ in range(n_rounds):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range(mm_reps):
            _ = a @ b  # real local compute, same clock domain as the expert FFN this rank would run
        if local_rank == 0:
            dist.send(x, dst=other)
            dist.recv(x, src=other)
        else:
            dist.recv(x, src=other)
            dist.send(x, dst=other)
        torch.cuda.synchronize()
        t1 = time.perf_counter()
        latencies_us.append((t1 - t0) * 1e6)

    result = {
        "role": role, "local_rank": local_rank,
        "n_rounds": n_rounds,
        "latencies_us": latencies_us,
        "mean_us": sum(latencies_us) / len(latencies_us),
        "max_us": max(latencies_us),
        "min_us": min(latencies_us),
    }
    # print as the LAST line, tagged, so the driver can grep it out of
    # real captured stdout regardless of any NCCL init chatter above it.
    print("[PAIRWISE-RESULT] " + json.dumps(result), flush=True)
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
