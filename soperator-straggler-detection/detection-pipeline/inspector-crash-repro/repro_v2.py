import os, time
import torch
import torch.distributed as dist

# P32 step 1 (v2) -- repro_alltoall.py's pure back-to-back AllToAll loop
# (no model, no autograd, no AllReduce mix) ran cleanly at ~77,000 fleet
# calls/sec for 50000 iterations with ZERO crashes -- disproving "raw
# AllToAll call rate alone" as the trigger. This version reintroduces the
# specific structural elements repro v1 omitted, one file, to find which
# actually matters: (a) a custom autograd.Function around the AllToAll
# (like DLRM's real alltoall_autograd), so backward() drives it from
# PyTorch's autograd engine thread, not directly from the main Python
# thread; (b) a REAL mix of AllReduce calls at DLRM's real tower
# parameter byte sizes (4, 1024, 2048, 524288, 2097152) interleaved with
# the AllToAll calls every iteration, since NCCL's channel-count
# selection depends on message size and a mix of very-small and
# 2MB-scale messages might exercise a different multi-channel
# completion path than repro v1's single fixed small size; (c) still NO
# real embedding/model compute -- synthetic tensors only.
WORLD_SIZE_LOCAL = int(os.environ.get('WORLD_SIZE', '16'))
BATCH = int(os.environ.get('BATCH', '256'))
EMBED_DIM = int(os.environ.get('EMBED_DIM', '64'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '50000'))

# Real DLRM tower parameter byte sizes (fc1.weight, fc1.bias, fc2.weight,
# fc2.bias, fc3.weight, fc3.bias), as float32 element counts.
AR_SIZES_ELEMS = [1024 * 512, 512, 512 * 256, 256, 256, 1]


class _AllToAllSingle(torch.autograd.Function):
    """Identical to train_dlrm.py's own wrapper -- reused verbatim, not
    reimplemented, so this repro genuinely exercises the same autograd/
    NCCL call path DLRM uses, not a lookalike."""
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
        # plain AllToAll (ids-shaped, no grad) -- matches DLRM's dispatch call
        send_ids = torch.randint(0, 1000000, (world_size * BATCH,), dtype=torch.int64, device=device)
        recv_ids = torch.empty_like(send_ids)
        dist.all_to_all_single(recv_ids, send_ids)

        # autograd-wrapped AllToAll (embeddings-shaped) -- matches DLRM's combine call
        x = torch.randn(world_size, BATCH, EMBED_DIM, device=device, requires_grad=True)
        y = alltoall_autograd(torch.empty(world_size, BATCH, EMBED_DIM, device=device), x)
        loss = y.sum()
        loss.backward()  # drives the AllToAll backward from autograd's own engine thread

        # AllReduce mix at DLRM's real tower parameter sizes
        for t in ar_tensors:
            dist.all_reduce(t)

        if rank == 0 and it % 2000 == 0:
            dt = time.time() - t0
            print(f"iter {it}: elapsed={dt:.1f}s", flush=True)

    if rank == 0:
        print(f"FINAL_ITER: {MAX_ITERS}", flush=True)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
