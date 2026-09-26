"""P26 -- tensor-parallel INFERENCE-only test: real forward passes through
a TP-sharded model (reusing P21's own model.py + parallelize_module
setup), with NO backward pass, NO optimizer, NO gradient sync of any
kind. The whole point: TP's own forward-pass AllReduce (c_proj's
RowwiseParallel combining sharded partial outputs -- see P21's train.py
comment on this exact mechanism) is structurally required regardless of
training vs inference, so this tests whether that alone produces real,
sustained collective traffic worth calibrating against, with NOTHING
manufactured -- no fake collective, no synthetic AllReduce inserted for
its own sake. If TP_SIZE=1, there is no cross-rank communication AT ALL
(pure data-parallel inference, each rank independent) -- the genuine
negative case Step 1(a) describes.
"""
import os
import time
import torch
import torch.distributed as dist
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor.parallel import parallelize_module, ColwiseParallel, RowwiseParallel

from model import GPTConfig, GPT

def main():
    dist.init_process_group(backend="nccl")
    ddp_rank = dist.get_rank()
    ddp_world_size = dist.get_world_size()

    # P27-hotfix -- same STRAGGLER_SLEEP_MS/STRAGGLER_TARGET_RANKS
    # convention already established and confirmed guaranteed-effective
    # in nanoGPT_tp/train.py and train_fsdp.py -- a real time.sleep() on
    # the target rank(s), not a new pattern. Placed directly before this
    # rank's own forward() call below (the call that contains TP's
    # c_proj RowwiseParallel AllReduce) so it delays this rank's actual
    # arrival at the collective, the same principle that makes the
    # sleep-based mechanism reliable elsewhere in this project --
    # replacing this workload's prior sole reliance on nvidia-smi -lgc,
    # confirmed this session to produce no measurable effect here.
    STRAGGLER_SLEEP_S = float(os.environ.get("STRAGGLER_SLEEP_MS", "0")) / 1000.0
    STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get("STRAGGLER_TARGET_RANKS", "").split(",") if r.strip() != ""}
    STRAGGLER_IS_TARGET = ddp_rank in STRAGGLER_TARGET_RANKS
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)

    tp_size = int(os.environ.get("TP_SIZE", "1"))
    assert ddp_world_size % tp_size == 0
    dp_world_size = ddp_world_size // tp_size
    if tp_size > 1:
        device_mesh = init_device_mesh("cuda", (dp_world_size, tp_size), mesh_dim_names=("dp", "tp"))
        tp_rank = ddp_rank % tp_size
        dp_rank = ddp_rank // tp_size
    else:
        device_mesh = None
        tp_rank = 0
        dp_rank = ddp_rank

    max_iters = int(os.environ.get("MAX_ITERS", "300"))
    batch_size = int(os.environ.get("BATCH_SIZE", "8"))
    block_size = int(os.environ.get("BLOCK_SIZE", "256"))
    log_interval = int(os.environ.get("LOG_INTERVAL", "10"))
    vocab_size = 50304

    # P21 -- same TP group shares the same input (a real batch of "requests"
    # this TP-sharded model instance is serving) -- seed by dp_rank so both
    # ranks in a TP group draw the identical synthetic token sequence,
    # exactly like train.py's own seed_offset=dp_rank reasoning.
    torch.manual_seed(1337 + dp_rank)

    gptconf = GPTConfig(n_layer=6, n_head=6, n_embd=384, block_size=block_size,
                        bias=False, vocab_size=vocab_size, dropout=0.0)
    model = GPT(gptconf).to(device)
    model.eval()

    if tp_size > 1:
        tp_mesh = device_mesh["tp"]
        for block in model.transformer.h:
            parallelize_module(block.mlp, tp_mesh, {
                "c_fc": ColwiseParallel(),
                "c_proj": RowwiseParallel(),
            })
    # deliberately NOT wrapped in DDP -- pure inference, no gradient sync
    # of any kind is needed or performed.

    for it in range(max_iters):
        idx = torch.randint(0, vocab_size, (batch_size, block_size), device=device)
        t0 = time.time()
        if STRAGGLER_IS_TARGET and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        with torch.no_grad():
            logits, loss = model(idx, targets=None)  # real inference-mode forward: last-token logits only
        torch.cuda.synchronize()
        dt = time.time() - t0
        if ddp_rank == 0 and it % log_interval == 0:
            print(f"iter {it}: logits_shape={tuple(logits.shape)}, time {dt*1000:.2f}ms", flush=True)

    if ddp_rank == 0:
        print(f"FINAL_ITER: {max_iters}", flush=True)
    dist.barrier()
    dist.destroy_process_group()

if __name__ == "__main__":
    main()
