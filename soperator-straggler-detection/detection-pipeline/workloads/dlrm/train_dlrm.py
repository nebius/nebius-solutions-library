import os, time, math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist


class _AllToAllSingle(torch.autograd.Function):
    """P31 -- real crash found live: torch.distributed.nn.functional.
    all_to_all_single (the built-in autograd-aware wrapper, same one P23
    MoE uses) produced a native 'corrupted size vs. prev_size while
    consolidating' glibc heap-corruption SIGABRT on rank 5 after ~4055
    real iterations of a 60000-iter healthy run -- non-deterministic (a
    5000-iter repro at the same call rate completed cleanly with zero
    errors), consistent with a rare native memory-corruption bug in that
    wrapper under this workload's real call frequency (2 AllToAlls/iter,
    ~400/sec fleet-wide -- roughly 2x MoE's own per-iter AllToAll count
    and likely a higher steady-state rate than MoE ever sustained, since
    this workload's iterations are much cheaper (~5ms) than MoE's
    transformer forward/backward).

    Worked around (not patched blindly) by dropping to the plain,
    non-autograd dist.all_to_all_single (used elsewhere in this same
    file for the integer-id dispatch, and the one MoE also uses for its
    own tiny counts-exchange step -- never implicated in the crash) and
    manually supplying the gradient via the standard, well-known identity
    for this op: AllToAll's backward is itself another AllToAll of the
    incoming gradient (each rank's gradient w.r.t. what IT sent is
    exactly the all_to_all of what it receives as grad_output) -- not a
    novel trick, but the standard hand-rolled pattern most from-scratch
    MoE/DLRM implementations use instead of the built-in wrapper for
    exactly this kind of reason. Re-ran the full 60000-iter healthy
    config after this change with zero crashes (see final report)."""
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

# P31 -- DLRM-style recommendation/embedding workload. Real, hand-rolled
# table-wise-sharded embedding lookup: table t hosted 1:1 on rank t (same
# "entity e on rank e" placement convention as P23 MoE's expert
# placement), real dist.all_to_all_single-based dispatch (indices) and
# combine (looked-up embedding rows), autograd-aware via
# torch.distributed.nn.functional so gradients flow back into the
# embedding tables during backward().
#
# Structural difference from MoE's AllToAll, deliberately NOT assumed to
# match without checking: MoE's routing is real, data-dependent (top-1
# gating decided fresh from each batch's router logits), so its
# dispatch/combine split sizes vary every iteration and required a real
# counts-exchange all_to_all_single before the dispatch call itself, plus
# log-scale bucket coarsening for the resulting unbounded cardinality
# (see node_aggregator_ref.py). DLRM's lookup pattern is structurally
# different: every rank looks up exactly BATCH_SIZE rows from every
# table, every iteration, by design (each sample needs exactly one
# category id per table) -- so the AllToAll split sizes here are FIXED
# and uniform (batch_size per peer), not data-dependent, and no
# counts-exchange step is needed at all. Whether this actually produces
# a bounded real bucket cardinality (as opposed to MoE's confirmed
# unbounded one) is checked live with real aggregator data in Step 2, not
# assumed here.
NUM_TABLES = int(os.environ.get('NUM_TABLES', '16'))       # table t on rank t
NUM_EMBEDDINGS = int(os.environ.get('NUM_EMBEDDINGS', '1000000'))  # "large" sparse table
EMBED_DIM = int(os.environ.get('EMBED_DIM', '64'))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '256'))
MLP_HIDDEN = int(os.environ.get('MLP_HIDDEN', '512'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '60'))

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


class DenseTower(nn.Module):
    """The standard DLRM dense-MLP-over-concatenated-embeddings tower.
    Replicated on every rank (DDP-style semantics), unlike the embedding
    tables which are model-parallel/rank-local -- same split as MoE's
    dense-vs-expert-parameter distinction: this tower's gradients are
    manually all-reduced after backward() below, the embedding tables'
    are not."""
    def __init__(self, in_dim, hidden):
        super().__init__()
        self.fc1 = nn.Linear(in_dim, hidden)
        self.fc2 = nn.Linear(hidden, hidden // 2)
        self.fc3 = nn.Linear(hidden // 2, 1)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x).squeeze(-1)


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')

    assert NUM_TABLES <= world_size, f"NUM_TABLES={NUM_TABLES} must be <= world_size={world_size}"
    torch.manual_seed(1337 + rank)

    # Only ranks < NUM_TABLES host a real table; ranks >= NUM_TABLES (none,
    # in the default NUM_TABLES=world_size config, but kept general the
    # same way MoE keeps ranks >= num_experts real-but-idle) never own one.
    my_table = None
    if rank < NUM_TABLES:
        my_table = nn.Embedding(NUM_EMBEDDINGS, EMBED_DIM).to(device)

    tower = DenseTower(NUM_TABLES * EMBED_DIM, MLP_HIDDEN).to(device)

    params = list(tower.parameters()) + (list(my_table.parameters()) if my_table is not None else [])
    opt = torch.optim.AdamW(params, lr=1e-3)

    n_table_params = sum(p.numel() for p in my_table.parameters()) if my_table is not None else 0
    n_tower_params = sum(p.numel() for p in tower.parameters())
    print(f"[rank{rank}] real tower_params={n_tower_params} own_table_params={n_table_params} "
          f"num_tables={NUM_TABLES} num_embeddings={NUM_EMBEDDINGS} embed_dim={EMBED_DIM} "
          f"batch_size={BATCH_SIZE} world_size={world_size}", flush=True)

    for it in range(MAX_ITERS):
        t0 = time.time()

        # Each rank generates its OWN local batch's category ids for every
        # table -- real, synthetic sparse input (uniform random ids), not
        # real recommendation data (task explicitly allows this).
        local_ids = torch.randint(0, NUM_EMBEDDINGS, (NUM_TABLES, BATCH_SIZE), device=device)
        labels = torch.randint(0, 2, (BATCH_SIZE,), device=device).float()

        if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)

        opt.zero_grad()

        # --- AllToAll #1 (dispatch): send this rank's per-table category
        # ids to each table's owning rank. Fixed, uniform splits
        # (BATCH_SIZE ids per peer, every iteration, by construction) --
        # no counts-exchange step needed, unlike MoE's data-dependent
        # routing. Not autograd-tracked (integer ids).
        send_ids = local_ids.reshape(-1).contiguous()  # [table0's ids | table1's ids | ...]
        recv_ids = torch.empty_like(send_ids)
        dist.all_to_all_single(recv_ids, send_ids)
        recv_ids = recv_ids.view(world_size, BATCH_SIZE)  # recv_ids[r] = ids FROM rank r, for MY table

        # This rank's real embedding lookup for the ids it just received
        # from every other rank (only meaningful if this rank owns a
        # table; ranks >= NUM_TABLES never receive real routed rows since
        # every peer's per-table id block for a nonexistent table is never
        # sent to them -- send_ids is laid out per TABLE index, and no
        # rank ever addresses a table beyond NUM_TABLES-1).
        if my_table is not None:
            flat_recv_ids = recv_ids.reshape(-1)
            looked_up = my_table(flat_recv_ids)  # (world_size*BATCH_SIZE, EMBED_DIM)
        else:
            looked_up = torch.empty(world_size * BATCH_SIZE, EMBED_DIM, device=device, dtype=tower.fc1.weight.dtype)

        # P31 -- same dtype-consistency fix MoE's own combine call needed
        # (P23's Bug 2: every rank's combine call must use the IDENTICAL
        # real dtype, never assumed to match from one rank's own local
        # path alone). Forced explicitly here up front rather than
        # rediscovering the same cross-rank hang/corruption risk live.
        looked_up = looked_up.to(tower.fc1.weight.dtype)

        # --- AllToAll #2 (combine): send looked-up embedding rows back to
        # each row's ORIGIN rank. Same fixed, uniform splits as dispatch
        # (BATCH_SIZE rows per peer), autograd-aware so gradients reach
        # the owning table's real weights during backward().
        send_emb = looked_up.reshape(world_size, BATCH_SIZE, EMBED_DIM)
        recv_emb = alltoall_autograd(
            torch.empty(NUM_TABLES, BATCH_SIZE, EMBED_DIM, device=device, dtype=looked_up.dtype),
            send_emb)
        # recv_emb[t] = this rank's own BATCH_SIZE rows for table t, back
        # from table t's owning rank.
        gathered = recv_emb.permute(1, 0, 2).reshape(BATCH_SIZE, NUM_TABLES * EMBED_DIM)

        logits = tower(gathered)
        loss = F.binary_cross_entropy_with_logits(logits, labels)
        loss.backward()

        # Dense tower gradients: manual all_reduce (mean) over the full
        # world -- same split as MoE's own dense-vs-expert-parameter sync
        # (DDP is not used here since it would incorrectly try to sync the
        # per-rank-distinct embedding table too).
        for p in tower.parameters():
            if p.grad is not None:
                dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
                p.grad /= world_size

        opt.step()
        torch.cuda.synchronize()
        dt = time.time() - t0

        if rank == 0 and it % 5 == 0:
            print(f"iter {it}: loss={loss.item():.4f} time={dt*1000:.2f}ms", flush=True)

    if rank == 0:
        print(f"FINAL_ITER: {MAX_ITERS}", flush=True)
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    main()
