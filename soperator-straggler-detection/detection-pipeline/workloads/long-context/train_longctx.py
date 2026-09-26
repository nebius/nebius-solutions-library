import os, sys, time, random
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor.parallel import parallelize_module, ColwiseParallel, RowwiseParallel

sys.path.insert(0, '/root/P21_multicomm/nanoGPT_tp')
from model import GPTConfig, GPT

# P29 -- long-context training: the same TP2 model config/sharding this
# project already validated (nanoGPT_tp's own train_shakespeare_char.py:
# n_layer=6, n_head=6, n_embd=384), unchanged, so the ONLY real variable
# under test is sequence length -- not a different model, not a new
# communicator topology (same DDP-free single-TP-group structure TP2/
# TP-inference already use). MAX_BLOCK_SIZE=16384 is 64x this project's
# established block_size=256 -- a genuine long-context scale (matching
# real long-context LLM training's 8K-128K token context windows), not a
# token-count bump. Real message-size VARIABILITY (the actual concern
# P21.5/P23's coarsen_msg_size was designed against, confirmed so far
# only against MoE's routed-token-count variability) is stressed
# directly: each iteration samples a genuinely different real sequence
# length from a wide real range, rather than testing one single large
# fixed size.
N_LAYER = 6
N_HEAD = 6
N_EMBD = 384
MAX_BLOCK_SIZE = int(os.environ.get('MAX_BLOCK_SIZE', '16384'))
VOCAB_SIZE = 50304
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '2'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '60'))
# P29 -- real, varying sequence lengths, sampled per-iteration from a
# wide real range spanning two full octaves (2048 to 16384) -- directly
# stresses bucket coarsening/calibration against genuinely varying
# message sizes, not one fixed size.
SEQ_LEN_CHOICES = [int(x) for x in os.environ.get('SEQ_LEN_CHOICES', '2048,4096,8192,12288,16384').split(',')]

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


def main():
    dist.init_process_group(backend="nccl")
    ddp_rank = dist.get_rank()
    ddp_world_size = dist.get_world_size()
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

    torch.manual_seed(1337 + dp_rank)
    random.seed(4242 + dp_rank)

    gptconf = GPTConfig(n_layer=N_LAYER, n_head=N_HEAD, n_embd=N_EMBD, block_size=MAX_BLOCK_SIZE,
                        bias=False, vocab_size=VOCAB_SIZE, dropout=0.0)
    model = GPT(gptconf).to(device)

    if tp_size > 1:
        tp_mesh = device_mesh["tp"]
        for block in model.transformer.h:
            parallelize_module(block.mlp, tp_mesh, {
                "c_fc": ColwiseParallel(),
                "c_proj": RowwiseParallel(),
            })

    # P28/P21 -- same real, confirmed DTensor/plain-Tensor optimizer
    # limitation (mixing them in one fused/foreach AdamW call raises "got
    # mixed torch.Tensor and DTensor"): fused=False, foreach=False forces
    # the plain per-parameter dispatch DTensor supports.
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, fused=False, foreach=False)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[rank{ddp_rank}] tp_rank={tp_rank} dp_rank={dp_rank} real params: {n_params} "
          f"max_block_size={MAX_BLOCK_SIZE} seq_len_choices={SEQ_LEN_CHOICES}", flush=True)

    for it in range(MAX_ITERS):
        t0 = time.time()
        # P29 -- same TP group (same dp_rank) must use the IDENTICAL real
        # sequence length this iteration -- otherwise the TP AllReduce's
        # own tensor shapes would mismatch across tp_rank peers. Seeded by
        # dp_rank alone (not ddp_rank), matching train_tp_inference.py's
        # own "same TP group processes the same real request" convention.
        rng = random.Random(4242 + dp_rank * 100003 + it)
        seq_len = rng.choice(SEQ_LEN_CHOICES)
        idx = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, seq_len), device=device)
        y = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, seq_len), device=device)

        if STRAGGLER_IS_TARGET and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)

        opt.zero_grad()
        logits, loss = model(idx, targets=y)
        loss.backward()
        opt.step()
        torch.cuda.synchronize()
        dt = time.time() - t0

        if ddp_rank == 0:
            print(f"iter {it}: seq_len={seq_len} loss={loss.item():.4f} time={dt*1000:.2f}ms", flush=True)

    if ddp_rank == 0:
        print(f"FINAL_ITER: {MAX_ITERS}", flush=True)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
