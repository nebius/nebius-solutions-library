import os, time
import torch
import torch.distributed as dist

# P32 step 1 (v3) -- repro v2 (matching collective mix + autograd
# backward + real AllReduce sizes) STILL ran 50000 iterations clean at
# ~1.35ms/iter. New, more targeted hypothesis, not yet tested: the
# Inspector dump thread polls every NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_
# MICROSECONDS=500us regardless of training speed. DLRM's real
# ~4.8-12.5ms/iter gives the dump thread ~10-25 real chances to run
# CONCURRENTLY with in-flight collective/kernelCh completion state per
# iteration; v1/v2's ~0.4-1.35ms/iter gives it only ~1-3. If the real bug
# is a race between the dump thread (reading commInfo/collInfo under
# their own locks) and the NCCL callback thread(s) manipulating the same
# structures, a higher relative dump-thread-to-NCCL-call ratio -- driven
# by real ITERATION WALL-CLOCK TIME, not call rate or model complexity
# -- would make the race far more likely. Tested directly and
# surgically: identical collective pattern to v2, with a single
# time.sleep() per iteration calibrated to DLRM's own real ~4.8ms/iter
# pace, and NO other change (no real embedding compute added) -- isolates
# wall-clock pacing as the one new variable.
WORLD_SIZE_LOCAL = int(os.environ.get('WORLD_SIZE', '16'))
BATCH = int(os.environ.get('BATCH', '256'))
EMBED_DIM = int(os.environ.get('EMBED_DIM', '64'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '50000'))
ITER_SLEEP_S = float(os.environ.get('ITER_SLEEP_MS', '4.5')) / 1000.0

AR_SIZES_ELEMS = [1024 * 512, 512, 512 * 256, 256, 256, 1]


class _AllToAllSingle(torch.autograd.Function):
    @staticmethod
    def forward(ctx, output_template, input_tensor):
        output = torch.empty_like(output_template)
        dist.all_to_all_single(output, input_tensor.contiguous())
        return output

    @staticmethod
    def backward(ctx, grad_output):
        grad_input = torch.empty_like(grad_output)
        dist.all_to_all_single(grad_input, grad_output.contiguous())
        return None, grad_input


def alltoall_autograd(output_template, input_tensor):
    return _AllToAllSingle.apply(output_template, input_tensor)


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    ar_tensors = [torch.randn(n, device=device, requires_grad=False) for n in AR_SIZES_ELEMS]

    t0 = time.time()
    for it in range(MAX_ITERS):
        send_ids = torch.randint(0, 1000000, (world_size * BATCH,), dtype=torch.int64, device=device)
        recv_ids = torch.empty_like(send_ids)
        dist.all_to_all_single(recv_ids, send_ids)

        x = torch.randn(world_size, BATCH, EMBED_DIM, device=device, requires_grad=True)
        y = alltoall_autograd(torch.empty(world_size, BATCH, EMBED_DIM, device=device), x)
        loss = y.sum()
        loss.backward()

        for t in ar_tensors:
            dist.all_reduce(t)

        torch.cuda.synchronize()
        # P32 v3 -- the one new variable under test: real wall-clock
        # pacing matching DLRM's own iteration time, giving the dump
        # thread the same relative number of real polling opportunities.
        time.sleep(ITER_SLEEP_S)

        if rank == 0 and it % 500 == 0:
            dt = time.time() - t0
            print(f"iter {it}: elapsed={dt:.1f}s", flush=True)

    if rank == 0:
        print(f"FINAL_ITER: {MAX_ITERS}", flush=True)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
