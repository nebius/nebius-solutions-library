import os, time
import torch
import torch.nn as nn
import torch.distributed as dist

# P24 -- hand-rolled 2-stage PP, replacing torch.distributed.pipelining
# after that library confirmed to hang inside its own step() call at
# iteration 2 across 2 full debugging sessions. Every send/recv here is
# explicit and under our own control -- no scheduler internals to hide
# inside if something goes wrong.
N_LAYER = 8
N_EMBD = 512
N_HEAD = 8
BLOCK_SIZE = 256
VOCAB_SIZE = 50304
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '8'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '20'))
STAGE0_LAYERS = N_LAYER // 2

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}
# Phase-boundary stress test (this session) -- reuses the same sleep
# knob, relocates WHERE it fires. This file's own original sleep sites
# (before each stage's forward call) are 'forward' (the default, kept
# unchanged); 'backward' and 'optimizer' are new placements, same
# mechanism. No checkpoint code path exists in this minimal script at
# all -- a real, structural, honest N/A for the checkpoint-phase test on
# this workload, not something to fabricate.
STRAGGLER_PHASE = os.environ.get('STRAGGLER_PHASE', 'forward')
STRAGGLER_MODE = os.environ.get('STRAGGLER_MODE', 'constant')
STRAGGLER_WARMUP_ITERS = int(os.environ.get('STRAGGLER_WARMUP_ITERS', '5'))


def _straggler_should_fire(rank, it):
    if rank not in STRAGGLER_TARGET_RANKS or STRAGGLER_SLEEP_S <= 0:
        return False
    if STRAGGLER_MODE == 'warmup':
        return it < STRAGGLER_WARMUP_ITERS
    return True


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.ln1 = nn.LayerNorm(N_EMBD)
        self.attn = nn.MultiheadAttention(N_EMBD, N_HEAD, batch_first=True)
        self.ln2 = nn.LayerNorm(N_EMBD)
        self.mlp = nn.Sequential(nn.Linear(N_EMBD, 4 * N_EMBD), nn.GELU(), nn.Linear(4 * N_EMBD, N_EMBD))

    def forward(self, x):
        h = self.ln1(x)
        mask = torch.triu(torch.ones(x.size(1), x.size(1), device=x.device, dtype=torch.bool), diagonal=1)
        a, _ = self.attn(h, h, h, attn_mask=mask, need_weights=False)
        x = x + a
        x = x + self.mlp(self.ln2(x))
        return x


class Stage0(nn.Module):
    """Embedding + first STAGE0_LAYERS blocks."""
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
    """Remaining blocks + final LN + head."""
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
    assert world_size == 2, "this minimal hand-rolled version is 2-stage only"
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    # P27-hotfix5 -- STAGE0_RANK decouples "which pipeline stage does the
    # real forward/backward work" from "which global rank this process
    # is" (default 0, matching every prior run: rank0=stage0). Added
    # specifically to test whether P27.2.5's rank-0 exclusion rule (skip
    # the whole below-floor comm if either member is the real global
    # rank 0 -- validated for TP2's 16-process job, where rank 0 is one
    # of many pairs and carries its own measured coordinator-overhead
    # artifact) actually applies to PP's 2-process shape, where rank 0 is
    # structurally unavoidable in its only comm. Setting STAGE0_RANK=1
    # swaps WHICH RANK does stage0's real work while peer targeting
    # (`peer = 1 - rank`, valid only because world_size==2) stays
    # correct regardless -- if the measured Recv/Send timing asymmetry
    # follows the STAGE (whichever rank now does stage0's work reads like
    # stage0 always has), that's real evidence the asymmetry is fully
    # explained by stage timing, not a hidden rank-0 bias riding along
    # with it.
    stage0_rank = int(os.environ.get('STAGE0_RANK', '0'))
    my_stage = 0 if rank == stage0_rank else 1
    peer = 1 - rank

    torch.manual_seed(1337)
    if my_stage == 0:
        model = Stage0().to(device)
    else:
        model = Stage1().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[rank{rank}] PP stage {my_stage}/2 (STAGE0_RANK={stage0_rank}) real params: {n_params}", flush=True)

    act_shape = (BATCH_SIZE, BLOCK_SIZE, N_EMBD)

    for it in range(MAX_ITERS):
        t0 = time.time()
        opt.zero_grad()

        if my_stage == 0:
            idx = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
            if STRAGGLER_PHASE == 'forward' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
            act = model(idx)
            # P24 -- real point-to-point send: this stage's real forward
            # output, the exact real payload a genuine PP boundary
            # transmits (Inspector should see this as a real Send/Recv
            # pair, distinct from any symmetric collective).
            dist.send(act.detach().contiguous(), dst=peer)

            grad_in = torch.empty(act_shape, device=device)
            dist.recv(grad_in, src=peer)
            if STRAGGLER_PHASE == 'backward' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
            # P24 -- explicit backward through the boundary: act itself
            # (a real leaf w.r.t. autograd on THIS rank, since it was
            # produced by this rank's own forward and never crossed a
            # torch.no_grad()) gets its .backward() driven by the REAL
            # gradient tensor received from stage 1, not a fabricated
            # placeholder -- this is what actually propagates gradients
            # back into stage 0's own parameters.
            act.backward(gradient=grad_in)
            if STRAGGLER_PHASE == 'optimizer' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
            opt.step()
            dist.barrier()
            dt = (time.time() - t0) * 1000
            if it % 2 == 0:
                gnorm = sum(p.grad.norm().item() for p in model.parameters() if p.grad is not None)
                print(f"[rank{rank}] iter {it}: time {dt:.2f}ms, real grad_norm_sum={gnorm:.6f}", flush=True)

        else:
            act = torch.empty(act_shape, device=device, requires_grad=True)
            dist.recv(act, src=peer)
            if STRAGGLER_PHASE == 'forward' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
            logits = model(act)
            y = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
            loss = nn.functional.cross_entropy(logits.view(-1, VOCAB_SIZE), y.view(-1))
            if STRAGGLER_PHASE == 'backward' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
            loss.backward()
            # P24 -- real gradient w.r.t. the RECEIVED activation tensor,
            # produced by autograd during loss.backward() above (act was
            # marked requires_grad=True at recv time specifically so this
            # exists) -- sent back as the real cross-boundary gradient,
            # not synthesized.
            dist.send(act.grad.contiguous(), dst=peer)
            if STRAGGLER_PHASE == 'optimizer' and _straggler_should_fire(rank, it):
                time.sleep(STRAGGLER_SLEEP_S)
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
