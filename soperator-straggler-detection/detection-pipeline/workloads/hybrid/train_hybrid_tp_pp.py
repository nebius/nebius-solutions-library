import os, time
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor.parallel import parallelize_module, ColwiseParallel, RowwiseParallel

# P28 -- hybrid TP+PP: the first workload in this project combining two
# parallelism strategies at once. Reuses BOTH already-validated pieces
# unmodified in structure, not reinvented:
#   - PP: train_pp_manual.py's exact 2-stage hand-rolled dist.send/recv
#     pattern (confirmed real gradient flow, confirmed real 2.2x stage
#     asymmetry) -- STAGE0_LAYERS/STAGE1_LAYERS split, per-iteration
#     dist.barrier() lockstep fix, unchanged.
#   - TP: train_tp_inference.py's exact parallelize_module(ColwiseParallel/
#     RowwiseParallel) sharding of each block's own MLP (c_fc/c_proj),
#     applied here to each PIPELINE STAGE's own local blocks instead of
#     the whole (unsharded-by-stage) model.
#
# Layout: world_size = PP_STAGES * TP_SIZE = 2 * 2 = 4. Rank layout is
# row-major over a (pp=2, tp=2) DeviceMesh -- rank 0,1 = stage 0's TP
# pair; rank 2,3 = stage 1's TP pair (init_device_mesh's own convention,
# not a new one invented here). The PP boundary is crossed by each TP-
# rank talking ONLY to its own same-tp-rank counterpart in the
# neighboring stage (rank i <-> rank i+PP_TP_SIZE) -- two independent,
# genuinely 2-member Send/Recv comms (mirroring PP's already-validated
# single-pair case exactly, just instantiated twice), each carrying the
# SAME real activation tensor (already replicated across the TP group by
# RowwiseParallel's own internal AllReduce, so both TP ranks in a stage
# have identical local data to hand across the boundary -- no extra
# broadcast needed).
N_LAYER = 8
N_EMBD = 512
N_HEAD = 8
BLOCK_SIZE = 256
VOCAB_SIZE = 50304
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '8'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '20'))
STAGE0_LAYERS = N_LAYER // 2
PP_TP_SIZE = int(os.environ.get('TP_SIZE', '2'))

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


class MLP(nn.Module):
    """Same c_fc/c_proj naming as model.py's own MLP -- required by
    parallelize_module's dict keys below, not a style choice."""
    def __init__(self):
        super().__init__()
        self.c_fc = nn.Linear(N_EMBD, 4 * N_EMBD)
        self.gelu = nn.GELU()
        self.c_proj = nn.Linear(4 * N_EMBD, N_EMBD)

    def forward(self, x):
        return self.c_proj(self.gelu(self.c_fc(x)))


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.ln1 = nn.LayerNorm(N_EMBD)
        self.attn = nn.MultiheadAttention(N_EMBD, N_HEAD, batch_first=True)
        self.ln2 = nn.LayerNorm(N_EMBD)
        self.mlp = MLP()

    def forward(self, x):
        h = self.ln1(x)
        mask = torch.triu(torch.ones(x.size(1), x.size(1), device=x.device, dtype=torch.bool), diagonal=1)
        a, _ = self.attn(h, h, h, attn_mask=mask, need_weights=False)
        x = x + a
        x = x + self.mlp(self.ln2(x))
        return x


class Stage0(nn.Module):
    def __init__(self):
        super().__init__()
        self.tok_emb = nn.Embedding(VOCAB_SIZE, N_EMBD)
        self.pos_emb = nn.Embedding(BLOCK_SIZE, N_EMBD)
        self.blocks = nn.ModuleList([Block() for _ in range(STAGE0_LAYERS)])

    def forward(self, idx):
        b, t = idx.shape
        pos = torch.arange(t, device=idx.device)
        x = self.tok_emb(idx) + self.pos_emb(pos)[None, :, :]
        for blk in self.blocks:
            x = blk(x)
        return x


class Stage1(nn.Module):
    def __init__(self):
        super().__init__()
        self.blocks = nn.ModuleList([Block() for _ in range(N_LAYER - STAGE0_LAYERS)])
        self.ln_f = nn.LayerNorm(N_EMBD)
        self.head = nn.Linear(N_EMBD, VOCAB_SIZE, bias=False)

    def forward(self, x):
        for blk in self.blocks:
            x = blk(x)
        x = self.ln_f(x)
        return self.head(x)


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    assert world_size == 2 * PP_TP_SIZE, f"expected world_size={2*PP_TP_SIZE} (2 PP stages x TP_SIZE={PP_TP_SIZE})"
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    pp_stage = rank // PP_TP_SIZE
    tp_rank_local = rank % PP_TP_SIZE
    pp_peer = rank + PP_TP_SIZE if pp_stage == 0 else rank - PP_TP_SIZE

    device_mesh = init_device_mesh("cuda", (2, PP_TP_SIZE), mesh_dim_names=("pp", "tp"))
    tp_mesh = device_mesh["tp"]

    torch.manual_seed(1337)
    if pp_stage == 0:
        model = Stage0().to(device)
    else:
        model = Stage1().to(device)

    if PP_TP_SIZE > 1:
        for blk in model.blocks:
            parallelize_module(blk.mlp, tp_mesh, {
                "c_fc": ColwiseParallel(),
                "c_proj": RowwiseParallel(),
            })

    # P28 -- same real, confirmed limitation as nanoGPT_tp/train.py's own
    # P21 comment: mixing plain Tensor and DTensor (TP-sharded) params in
    # one fused/foreach AdamW call raises "got mixed torch.Tensor and
    # DTensor" (aten._fused_adamw_.default). This stage's params are now
    # genuinely mixed (attention/LayerNorm/embedding = plain, mlp.c_fc/
    # c_proj = DTensor post-parallelize_module) -- fused=False,
    # foreach=False forces the plain per-parameter dispatch DTensor
    # supports, the documented fallback, not a new workaround.
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, fused=False, foreach=False)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[rank{rank}] pp_stage={pp_stage} tp_rank={tp_rank_local} pp_peer={pp_peer} real params: {n_params}", flush=True)

    act_shape = (BATCH_SIZE, BLOCK_SIZE, N_EMBD)

    for it in range(MAX_ITERS):
        t0 = time.time()
        opt.zero_grad()

        if pp_stage == 0:
            idx = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
            act = model(idx)
            # P28 -- sleep placed AFTER model(idx) (i.e. after this
            # stage's own TP AllReduce has already completed), not
            # before it as every prior single-strategy PP script placed
            # it. Placing it before model() would delay THIS rank's
            # arrival at its own stage's TP AllReduce too, confounding a
            # PP-only fault with a real TP disruption -- exactly the
            # cross-contamination Step 4 needs to rule OUT, not
            # accidentally manufacture. Here it only delays this rank's
            # arrival at the PP send, isolating the fault to the PP
            # portion cleanly.
            if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
                time.sleep(STRAGGLER_SLEEP_S)
            dist.send(act.detach().contiguous(), dst=pp_peer)

            grad_in = torch.empty(act_shape, device=device)
            dist.recv(grad_in, src=pp_peer)
            act.backward(gradient=grad_in)
            opt.step()
            dist.barrier()
            dt = (time.time() - t0) * 1000
            if it % 2 == 0:
                gnorm = sum(p.grad.norm().item() for p in model.parameters() if p.grad is not None)
                print(f"[rank{rank}] iter {it}: time {dt:.2f}ms, real grad_norm_sum={gnorm:.6f}", flush=True)
        else:
            act = torch.empty(act_shape, device=device, requires_grad=True)
            dist.recv(act, src=pp_peer)
            if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
                time.sleep(STRAGGLER_SLEEP_S)
            logits = model(act)
            y = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
            loss = nn.functional.cross_entropy(logits.view(-1, VOCAB_SIZE), y.view(-1))
            loss.backward()
            dist.send(act.grad.contiguous(), dst=pp_peer)
            opt.step()
            dist.barrier()
            dt = (time.time() - t0) * 1000
            if it % 2 == 0:
                gnorm = sum(p.grad.norm().item() for p in model.parameters() if p.grad is not None)
                print(f"[rank{rank}] iter {it}: loss {loss.item():.4f}, time {dt:.2f}ms, real grad_norm_sum={gnorm:.6f}", flush=True)

    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
