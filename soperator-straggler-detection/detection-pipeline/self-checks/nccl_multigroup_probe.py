#!/usr/bin/env python3
"""Minimal, isolated reproduction of the exact condition the task
description says crashes inside NCCL's HPC-X SHARP/IB RDMA plugin
(ncclIbDeregMr) on Kubernetes: MULTIPLE separate NCCL communicators
(process groups) existing and communicating AT THE SAME TIME, the way
Megatron's TP x PP parallelism always does (a rank belongs to both its
TP group and its PP group simultaneously -- this is not a contrived
edge case, it is what every real TP>1-or-PP>1 job already does).

No model, no Megatron, no training -- just torch.distributed group
creation/use/teardown, so this is cheap (a few seconds) and safe to run
as a preflight before every real job, exactly the way pinning/pin_env.sh's
pin_prepare() is already a cheap preflight run before every job.

Layout: arranges WORLD_SIZE ranks into a DIM0 x DIM1 grid (DIM0*DIM1 ==
WORLD_SIZE), creates one "row" group per row and one "col" group per
column -- the same overlapping-membership shape as Megatron's TP groups
(row) and PP/DP groups (col): every rank belongs to exactly one row group
AND one col group AT THE SAME TIME, so both communicators are alive
concurrently, matching the real trigger condition. Runs a few concurrent
allreduces on both, then explicitly destroys them (and the world group)
in an order deliberately interleaved rather than strictly nested, since
the task description's crash is inside ncclIbDeregMr -- a teardown-path
function -- not inside the collective call itself.

Exit code is the only contract the launcher cares about: 0 means this
cluster's real, currently active networking config (whatever NCCL/RDMA
plugin actually resolves in this container) survives concurrent multi-
group create/use/destroy. A non-zero exit, or death by signal (segfault/
abort inside the RDMA plugin would normally show up as SIGSEGV/SIGABRT,
not a clean Python exception), means it does not.
"""
import os
import sys
import torch
import torch.distributed as dist


def log(msg):
    print(f"[NET-SELFCHECK] rank={os.environ.get('RANK', '?')} {msg}", flush=True)


def main():
    dist.init_process_group(backend="nccl")
    world_size = dist.get_world_size()
    rank = dist.get_rank()
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(local_rank)

    # Pick a grid as close to square as possible; any WORLD_SIZE works
    # (falls back to a 1 x N row-only grid if WORLD_SIZE has no divisor
    # close to its square root -- still exercises >1 concurrent group as
    # long as WORLD_SIZE >= 4, which every real TP/PP job satisfies).
    dim0 = 1
    for d in range(int(world_size ** 0.5), 0, -1):
        if world_size % d == 0:
            dim0 = d
            break
    dim1 = world_size // dim0
    row = rank // dim1
    col = rank % dim1
    log(f"grid={dim0}x{dim1} row={row} col={col} world_size={world_size}")

    row_groups = [dist.new_group(ranks=list(range(r * dim1, (r + 1) * dim1))) for r in range(dim0)]
    col_groups = [dist.new_group(ranks=list(range(c, world_size, dim1))) for c in range(dim1)]
    my_row_group = row_groups[row]
    my_col_group = col_groups[col]
    log("row and col groups created -- both alive concurrently now")

    t = torch.ones(1024, device="cuda") * (rank + 1)
    for i in range(5):
        dist.all_reduce(t, group=my_row_group)
        dist.all_reduce(t, group=my_col_group)
    torch.cuda.synchronize()
    log(f"5 concurrent row+col allreduce rounds done, sample value={t[0].item()}")

    # Deliberately interleaved (not strictly inner-to-outer) teardown --
    # the task description's crash is inside a teardown-path function.
    for i, g in enumerate(row_groups):
        dist.destroy_process_group(g)
    for i, g in enumerate(col_groups):
        dist.destroy_process_group(g)
    log("all row/col groups destroyed cleanly")

    dist.destroy_process_group()
    log("world group destroyed cleanly -- PASS")


if __name__ == "__main__":
    main()
