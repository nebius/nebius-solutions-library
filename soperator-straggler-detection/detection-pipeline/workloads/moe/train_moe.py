"""
P23 step 2 -- toy MoE (Mixture of Experts) training. Real, hand-rolled
top-1-gated MoE MLP (model_moe.MoEMLP) replacing nanoGPT's standard dense
MLP block in every transformer block, real dist.all_to_all_single-based
dispatch/combine (autograd-aware via torch.distributed.nn.functional, so
gradients really flow back through routing during backward()).

No native MoE support exists on this stack (confirmed in the P23 design
session: no torch.distributed.moe, no megablocks/tutel/fairscale/
deepspeed/torchtitan installed) -- this is a from-scratch implementation
using only dist.all_to_all_single, matching what the design session
identified as the realistic buildable path.

Expert placement: expert e hosted 1:1 on rank e, for e in
[0, NUM_EXPERTS). Every rank (all 16, spanning both nodes) participates
in the SAME world-spanning all_to_all_single calls every forward pass --
a fixed, per-rank-identical collective schedule, never conditional on a
rank's own local data (only the SPLIT SIZES within each call vary, which
is the real, legitimate load imbalance this test exists to produce).
Non-expert parameters (embeddings, attention, router, layernorms) are
gradient-synced with a manual all_reduce over the full world after
backward() -- no torch.nn.parallel.DistributedDataParallel here, since
DDP's automatic all-reduce would incorrectly try to sync the
per-rank-distinct expert weights too; expert-parameter gradients are
deliberately left unsynced (each of the 16 ranks' expert copy trains
independently) -- correctness of the trained model is not the goal, a
real, representative MoE communication pattern is.
"""
import os
import time
import math
import pickle
from contextlib import nullcontext

import numpy as np
import torch
import torch.distributed as dist
from torch.distributed import init_process_group, destroy_process_group

import model_moe
from model_moe import GPTConfig, GPT

# -----------------------------------------------------------------------------
out_dir = 'out'
eval_interval = 3000
log_interval = 50
eval_iters = 200
always_save_checkpoint = False
init_from = 'scratch'
dataset = 'shakespeare_char'
gradient_accumulation_steps = 1
batch_size = 64
block_size = 256
n_layer = 6
n_head = 6
n_embd = 384
dropout = 0.2
bias = False
num_experts = 8
learning_rate = 1e-3
max_iters = 200000
weight_decay = 1e-1
beta1 = 0.9
beta2 = 0.99
grad_clip = 1.0
decay_lr = True
warmup_iters = 100
lr_decay_iters = 200000
min_lr = 1e-4
backend = 'nccl'
device = 'cuda'
dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16'
compile = False
# -----------------------------------------------------------------------------
config_keys = [k for k, v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
exec(open('configurator.py').read())
config = {k: globals()[k] for k in config_keys}
# -----------------------------------------------------------------------------

ddp = int(os.environ.get('RANK', -1)) != -1
if ddp:
    init_process_group(backend=backend)
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE'])
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0
    seed_offset = ddp_rank
else:
    master_process = True
    seed_offset = 0
    ddp_world_size = 1
tokens_per_iter = gradient_accumulation_steps * ddp_world_size * batch_size * block_size
print(f"tokens per iteration will be: {tokens_per_iter:,}")

# P23 -- set BEFORE GPT(config) is constructed, so every Block's MoEMLP
# reads the real rank/world_size/expert-count at construction time.
model_moe.MOE_RANK = ddp_rank if ddp else 0
model_moe.MOE_WORLD_SIZE = ddp_world_size if ddp else 1
model_moe.MOE_NUM_EXPERTS = num_experts
if ddp and num_experts > ddp_world_size:
    raise ValueError(f"num_experts ({num_experts}) must be <= world_size ({ddp_world_size})")

if master_process:
    os.makedirs(out_dir, exist_ok=True)
torch.manual_seed(1337 + seed_offset)
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
device_type = 'cuda' if 'cuda' in device else 'cpu'
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

data_dir = os.path.join('data', dataset)
def get_batch(split):
    if split == 'train':
        data = np.memmap(os.path.join(data_dir, 'train.bin'), dtype=np.uint16, mode='r')
    else:
        data = np.memmap(os.path.join(data_dir, 'val.bin'), dtype=np.uint16, mode='r')
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([torch.from_numpy((data[i:i+block_size]).astype(np.int64)) for i in ix])
    y = torch.stack([torch.from_numpy((data[i+1:i+1+block_size]).astype(np.int64)) for i in ix])
    if device_type == 'cuda':
        x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
    else:
        x, y = x.to(device), y.to(device)
    return x, y

iter_num = 0
best_val_loss = 1e9

meta_path = os.path.join(data_dir, 'meta.pkl')
meta_vocab_size = None
if os.path.exists(meta_path):
    with open(meta_path, 'rb') as f:
        meta = pickle.load(f)
    meta_vocab_size = meta['vocab_size']
    print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=block_size,
                   bias=bias, vocab_size=None, dropout=dropout)
print("Initializing a new model from scratch")
model_args['vocab_size'] = meta_vocab_size if meta_vocab_size is not None else 50304
gptconf = GPTConfig(**model_args)
model = GPT(gptconf)
model.to(device)

# P23 -- manual gradient sync for non-expert params only (see module
# docstring for why plain DDP isn't used here). "Expert" params are any
# named parameter under a MoEMLP's c_fc/c_proj/gelu/dropout submodules --
# router is NOT an expert param (it's shared/replicated, DDP-synced
# normally like any other non-expert weight).
def is_expert_param(name):
    return '.mlp.c_fc.' in name or '.mlp.c_proj.' in name

if ddp:
    non_expert_params = [p for n, p in model.named_parameters() if not is_expert_param(n)]
    n_expert = sum(1 for n, _ in model.named_parameters() if is_expert_param(n))
    n_non_expert = len(non_expert_params)
    print(f"[rank {ddp_rank}] {n_non_expert} non-expert param tensors (world-synced), "
          f"{n_expert} expert param tensors (unsynced, local to this rank)")

def sync_grads():
    if not ddp:
        return
    for p in non_expert_params:
        if p.grad is not None:
            dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
            p.grad /= ddp_world_size

optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)

if compile:
    print("compiling the model... (takes a ~minute)")
    model = torch.compile(model)

def get_lr(it):
    if it < warmup_iters:
        return learning_rate * (it + 1) / (warmup_iters + 1)
    if it > lr_decay_iters:
        return min_lr
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))
    return min_lr + coeff * (learning_rate - min_lr)

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            with ctx:
                logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

X, Y = get_batch('train')
t0 = time.time()
local_iter_num = 0
raw_model = model
running_mfu = -1.0
while True:
    lr = get_lr(iter_num) if decay_lr else learning_rate
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    # Every rank enters estimate_loss() symmetrically on the SAME fixed,
    # iter_num-gated schedule -- the exact lesson from P22's estimate_loss
    # asymmetry bug and P23 step 1's own wall-clock-loop hang: no rank may
    # decide independently (via its own timing) whether to participate in
    # a collective; the schedule must be identical, deterministic, and
    # shared by construction (iter_num is advanced identically on every
    # rank, never via wall-clock time).
    if iter_num % eval_interval == 0:
        losses = estimate_loss()
        if master_process:
            print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    with ctx:
        logits, loss = model(X, Y)
    X, Y = get_batch('train')
    loss.backward()
    sync_grads()
    if grad_clip != 0.0:
        torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)

    t1 = time.time()
    dt = t1 - t0
    t0 = t1
    if iter_num % log_interval == 0 and master_process:
        lossf = loss.item()
        if local_iter_num >= 5:
            mfu = raw_model.estimate_mfu(batch_size * gradient_accumulation_steps, dt)
            running_mfu = mfu if running_mfu == -1.0 else 0.9 * running_mfu + 0.1 * mfu
        print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")
    iter_num += 1
    local_iter_num += 1

    if iter_num > max_iters:
        break

if ddp:
    torch.distributed.barrier()
    destroy_process_group()
