import os, time
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.distributed.pipelining import pipeline, SplitPoint, ScheduleGPipe

# P24 -- minimal, real 2-stage pipeline-parallel job. Reuses this
# project's own toy nanoGPT-scale transformer block shape (not a copy of
# nanoGPT_tp/model.py directly -- that file's internals are TP-specific,
# entangling PP with TP would confound this investigation's own real
# question: PP's OWN legitimate stage-to-stage timing asymmetry).
N_LAYER = 8
N_EMBD = 512
N_HEAD = 8
BLOCK_SIZE = 256
VOCAB_SIZE = 50304
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '8'))
N_MICROBATCHES = int(os.environ.get('N_MICROBATCHES', '4'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '50'))

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


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


class ToyGPT(nn.Module):
    def __init__(self):
        super().__init__()
        self.tok_emb = nn.Embedding(VOCAB_SIZE, N_EMBD)
        self.pos_emb = nn.Embedding(BLOCK_SIZE, N_EMBD)
        self.blocks = nn.ModuleList([Block() for _ in range(N_LAYER)])
        self.ln_f = nn.LayerNorm(N_EMBD)
        self.head = nn.Linear(N_EMBD, VOCAB_SIZE, bias=False)

    def forward(self, idx):
        b, t = idx.shape
        pos = torch.arange(t, device=idx.device)
        x = self.tok_emb(idx) + self.pos_emb(pos)[None, :, :]
        for blk in self.blocks:
            x = blk(x)
        x = self.ln_f(x)
        return self.head(x)


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    torch.manual_seed(1337)
    model = ToyGPT().to(device)

    # P24 -- straggler sleep placed inside the model's OWN forward, gated
    # on this rank's real pipeline-stage identity (rank == PP stage here,
    # one stage per rank -- no DP replication in this minimal test), same
    # STRAGGLER_SLEEP_MS/STRAGGLER_TARGET_RANKS convention already
    # validated everywhere else in this project. Placed before the
    # blocks run so it delays this stage's real contribution to the
    # pipeline the same principled way every other workload's sleep
    # delays its own real collective contribution.
    orig_forward = model.forward
    def forward_with_straggler(idx):
        if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        return orig_forward(idx)
    model.forward = forward_with_straggler

    split_spec = {f"blocks.{N_LAYER // 2}": SplitPoint.BEGINNING}
    # P24 -- pipeline() traces against ONE MICROBATCH's real shape, not
    # the full batch -- ScheduleGPipe.step() itself takes the full batch
    # and splits it into N_MICROBATCHES chunks internally (confirmed live
    # via a real PipeliningShapeError: full-batch example_input traced a
    # stage graph expecting (BATCH_SIZE, ...), which then mismatched the
    # real (BATCH_SIZE//N_MICROBATCHES, ...) chunks step() actually feeds
    # it at runtime).
    assert BATCH_SIZE % N_MICROBATCHES == 0
    example_input = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE // N_MICROBATCHES, BLOCK_SIZE), device=device)

    pipe = pipeline(model, mb_args=(example_input,), split_spec=split_spec)
    stage_mod = pipe.get_stage_module(rank)
    stage = pipe.build_stage(rank, device=device)
    opt = torch.optim.AdamW(stage_mod.parameters(), lr=3e-4)
    schedule = ScheduleGPipe(stage, N_MICROBATCHES, loss_fn=None if rank != world_size - 1 else
                              (lambda out, tgt: nn.functional.cross_entropy(out.view(-1, VOCAB_SIZE), tgt.view(-1))))

    print(f"[rank{rank}] PP stage {rank}/{world_size} real params: {sum(p.numel() for p in stage_mod.parameters())}", flush=True)

    for it in range(MAX_ITERS):
        print(f"[rank{rank}] DEBUG iter {it} starting", flush=True)
        x = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
        y = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, BLOCK_SIZE), device=device)
        print(f"[rank{rank}] DEBUG iter {it} pre zero_grad", flush=True)
        opt.zero_grad()
        print(f"[rank{rank}] DEBUG iter {it} post zero_grad, pre step()", flush=True)
        t0 = time.time()
        if rank == 0:
            schedule.step(x)
        elif rank == world_size - 1:
            losses = []
            schedule.step(target=y, losses=losses)
        else:
            schedule.step()
        print(f"[rank{rank}] DEBUG iter {it} schedule.step() returned", flush=True)
        gnorm = sum(p.grad.norm().item() for p in stage_mod.parameters() if p.grad is not None)
        print(f"[rank{rank}] DEBUG iter {it} real grad norm sum: {gnorm:.4f}, pre opt.step()", flush=True)
        opt.step()
        print(f"[rank{rank}] DEBUG iter {it} opt.step() returned", flush=True)
        # P24 -- real, required per-iteration barrier, confirmed live this
        # session: WITHOUT it, rank0 (first stage, forward-only for it)
        # races far ahead of rank1 (last stage, forward+backward+loss per
        # microbatch, structurally slower) and finishes ALL its loop
        # iterations while rank1 is still mid-loop -- rank0 then stops
        # calling step() entirely, so rank1's later iterations hang
        # waiting for pipeline messages that will never arrive. A per-
        # iteration barrier (not the removed per-iteration
        # cuda.synchronize(), which hung deterministically on the LAST
        # iteration specifically -- a different, narrower bug) keeps both
        # real stages progressing in lockstep, the correct fix for THIS
        # real failure mode.
        dist.barrier()
        print(f"[rank{rank}] DEBUG iter {it} opt.step()+barrier done", flush=True)
        dt = (time.time() - t0) * 1000
        if rank == world_size - 1 and it % 5 == 0:
            print(f"iter {it}: loss {losses[-1].item() if losses else float('nan'):.4f}, time {dt:.2f}ms", flush=True)
        elif it % 5 == 0:
            print(f"iter {it}: (stage {rank}) time {dt:.2f}ms", flush=True)

    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
