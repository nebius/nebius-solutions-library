#!/usr/bin/env python3
# =============================================================================
# GPU Soak Test — distributed workload (one process per GPU, via torchrun)
#
# Each rank runs THREE stresses on its GPU, concurrently:
#   1. HBM occupancy — a large resident tensor fills a configurable fraction
#      of device memory (default 75%), leaving headroom for NCCL buffers.
#   2. Sustained compute — a tight FP16 matmul loop keeps the SMs ~100% busy.
#   3. InfiniBand / NVLink stress — a timed dist.all_reduce every iteration,
#      whose result is verified for correctness and whose bus bandwidth is
#      measured (NCCL busbw convention).
#
# Ranks are launched by torchrun; RANK / LOCAL_RANK / WORLD_SIZE come from the
# environment torchrun sets. The PyTorchJob operator provides MASTER_ADDR /
# MASTER_PORT / (node) RANK / WORLD_SIZE, which the launch command feeds to
# torchrun as --node_rank / --nnodes.
#
# Exit code: rank 0 exits non-zero if ANY rank saw an incorrect all_reduce
# result, so the PyTorchJob master pod (and thus the job) fails loudly.
# =============================================================================
import os
import sys
import time

import torch
import torch.distributed as dist

FP16 = torch.float16
GiB = 1024 ** 3


def env_int(name, default):
    return int(os.environ.get(name, str(default)))


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


def main():
    rank = env_int("RANK", 0)
    local_rank = env_int("LOCAL_RANK", 0)
    world = env_int("WORLD_SIZE", 1)

    duration = env_int("SOAK_DURATION_SECONDS", 3600)
    fill_fraction = env_float("HBM_FILL_FRACTION", 0.75)
    allreduce_gb = env_float("ALLREDUCE_GB", 2.0)
    matmuls_per_iter = env_int("MATMULS_PER_ITER", 100)
    matmul_side = env_int("MATMUL_SIDE", 8192)

    is_master = rank == 0

    if not torch.cuda.is_available():
        print("[FAIL] CUDA not available in worker", flush=True)
        sys.exit(1)

    torch.cuda.set_device(local_rank)
    dev = torch.device(f"cuda:{local_rank}")

    # NCCL backend; env:// rendezvous is populated by torchrun.
    dist.init_process_group(backend="nccl", init_method="env://")

    gpu_name = torch.cuda.get_device_name(local_rank)
    total_mem = torch.cuda.get_device_properties(local_rank).total_memory

    # ---- Allocate in headroom-safe order -----------------------------------
    # 1) all_reduce buffer (must not be squeezed out by the HBM hog)
    ar_elems = int(allreduce_gb * GiB / 2)  # fp16 = 2 bytes
    ar = torch.empty(ar_elems, device=dev, dtype=FP16)
    ar_bytes = ar.numel() * 2

    # 2) matmul working set (small, fast — drives utilization, not memory)
    ma = torch.randn(matmul_side, matmul_side, device=dev, dtype=FP16)
    mb = torch.randn(matmul_side, matmul_side, device=dev, dtype=FP16)
    mc = torch.empty(matmul_side, matmul_side, device=dev, dtype=FP16)
    matmul_bytes = 3 * matmul_side * matmul_side * 2

    # 3) HBM hog — occupy the rest up to fill_fraction of total memory.
    #    Reserve ~2GB extra for NCCL internal buffers / CUDA context / workspace.
    reserve_bytes = ar_bytes + matmul_bytes + 2 * GiB
    hog_bytes = int(total_mem * fill_fraction) - reserve_bytes
    hog = None
    if hog_bytes > 0:
        hog = torch.empty(hog_bytes // 2, device=dev, dtype=FP16)
        hog.fill_(1.0)  # touch pages so the memory is truly committed

    torch.cuda.synchronize()
    allocated_gb = torch.cuda.memory_allocated(local_rank) / GiB

    if is_master:
        print(
            f"[soak] world={world} gpus/node={env_int('NPROC_PER_NODE', 8)} "
            f"gpu='{gpu_name}' total={total_mem / GiB:.1f}GB",
            flush=True,
        )
        print(
            f"[soak] per-rank: fill={fill_fraction * 100:.0f}% "
            f"allreduce={allreduce_gb:.1f}GB matmul={matmul_side}x{matmul_side} "
            f"allocated={allocated_gb:.1f}GB duration={duration}s",
            flush=True,
        )
        print("SOAK_CONFIG_OK", flush=True)

    # ---- Main soak loop ----------------------------------------------------
    # Termination is decided collectively: rank 0 raises the stop flag when the
    # clock expires, and an all_reduce(MAX) makes every rank break on the SAME
    # iteration. Without this, ranks could desync and deadlock in a collective.
    end = time.time() + duration
    stop = torch.zeros(1, device=dev)
    expected = float(world)  # all_reduce(SUM) of ones over `world` ranks
    iterations = 0
    failures = 0
    busbw_sum = 0.0
    peak_busbw = 0.0

    while True:
        if is_master and time.time() >= end:
            stop.fill_(1.0)
        dist.all_reduce(stop, op=dist.ReduceOp.MAX)
        if stop.item() > 0:
            break

        iterations += 1

        # Sustained compute — keeps GPU utilization pinned high.
        for _ in range(matmuls_per_iter):
            torch.matmul(ma, mb, out=mc)

        # IB/NVLink stress — timed, verified all_reduce.
        ar.fill_(1.0)
        torch.cuda.synchronize()
        dist.barrier()
        t0 = time.time()
        dist.all_reduce(ar, op=dist.ReduceOp.SUM)
        torch.cuda.synchronize()
        dt = time.time() - t0

        # busbw (NCCL convention): algbw * 2*(n-1)/n
        if dt > 0:
            algbw = ar_bytes / dt
            busbw = algbw * 2 * (world - 1) / world / 1e9
            busbw_sum += busbw
            peak_busbw = max(peak_busbw, busbw)

        # Correctness: every element should equal `world`. Use a tolerance that
        # scales with `world` — fp16 represents integers exactly only up to 2048,
        # so a fixed 1e-3 would false-fail at large scale (>256 nodes). A genuine
        # corrupt all_reduce is off by whole multiples, well outside 0.5%.
        result = ar[0].item()
        if abs(result - expected) > max(1e-3, expected * 5e-3):
            failures += 1
            print(
                f"[WARN] rank {rank} iter {iterations}: all_reduce result "
                f"{result} != expected {expected}",
                flush=True,
            )

        if is_master and iterations % 20 == 0:
            remaining = int(end - time.time())
            avg = busbw_sum / iterations if iterations else 0.0
            print(
                f"[soak] iter {iterations} | busbw={busbw:.1f} GB/s "
                f"(avg {avg:.1f}) | all_reduce {allreduce_gb:.1f}GB in "
                f"{dt * 1000:.1f}ms | remaining {remaining}s",
                flush=True,
            )

    # ---- Aggregate results across all ranks --------------------------------
    fail_tensor = torch.tensor([float(failures)], device=dev, dtype=torch.float64)
    peak_tensor = torch.tensor([peak_busbw], device=dev, dtype=torch.float64)
    dist.all_reduce(fail_tensor, op=dist.ReduceOp.SUM)
    dist.all_reduce(peak_tensor, op=dist.ReduceOp.MAX)
    total_failures = int(fail_tensor.item())
    global_peak_busbw = peak_tensor.item()

    if is_master:
        avg = busbw_sum / iterations if iterations else 0.0
        print("=== GPU Soak Test Complete ===", flush=True)
        print(f"Total iterations: {iterations}", flush=True)
        print(f"Failed iterations (all ranks): {total_failures}", flush=True)
        print(f"BUSBW_GBPS: {global_peak_busbw:.1f}", flush=True)
        print(f"BUSBW_AVG_GBPS: {avg:.1f}", flush=True)
        if total_failures == 0:
            print("[PASS] All all_reduce iterations completed with correct results", flush=True)
        else:
            print(f"[FAIL] {total_failures} all_reduce iteration(s) returned incorrect data", flush=True)

    dist.destroy_process_group()

    # Fail the job (via the master rank's exit code) on any incorrect result.
    if is_master and total_failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
