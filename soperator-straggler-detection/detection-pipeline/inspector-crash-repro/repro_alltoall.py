import os, time
import torch
import torch.distributed as dist

# P32 -- minimal, isolated repro for the real Inspector-plugin heap
# corruption crash found during DLRM validation. No model, no gradients,
# no embeddings -- just raw dist.all_to_all_single calls at a rate
# matching (and exceeding, to trigger faster) DLRM's real ~400-500
# AllToAll-decomposed-P2P-calls/sec fleet-wide. Confirms the crash is in
# the Inspector plugin's P2P capture path itself, not in DLRM's model
# code (already independently confirmed via the DLRM A/B test: disabled
# plugin survives 60000 iters clean).
WORLD_SIZE_LOCAL = int(os.environ.get('WORLD_SIZE', '16'))
BATCH = int(os.environ.get('BATCH', '256'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '50000'))
CALLS_PER_ITER = int(os.environ.get('CALLS_PER_ITER', '2'))  # match DLRM's dispatch+combine


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    send = torch.randint(0, 1000000, (world_size * BATCH,), dtype=torch.int64, device=device)
    recv = torch.empty_like(send)

    t0 = time.time()
    for it in range(MAX_ITERS):
        for _ in range(CALLS_PER_ITER):
            dist.all_to_all_single(recv, send)
        if rank == 0 and it % 2000 == 0:
            dt = time.time() - t0
            rate = (it + 1) * CALLS_PER_ITER * world_size / dt if dt > 0 else 0
            print(f"iter {it}: elapsed={dt:.1f}s approx_fleet_calls_per_sec={rate:.1f}", flush=True)

    if rank == 0:
        print(f"FINAL_ITER: {MAX_ITERS}", flush=True)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
