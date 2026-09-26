"""
P22 -- FSDP (Fully Sharded Data Parallel) training, using PyTorch's
native FSDP2 (fully_shard, torch.distributed._composable.fsdp) --
confirmed this is what's actually installed on this stack (torch
2.6.0a0+..., checked directly rather than assumed) and is the modern,
recommended API over classic FullyShardedDataParallel.

Shards real model parameters (per-transformer-block units, matching the
standard FSDP wrapping granularity) across the FULL world -- no TP, this
is FSDP alone, phase 2 of the multi-communicator investigation. Reuses
this project's existing nanoGPT model/data/config boilerplate unchanged.

Every rank uses ALL 8 local GPUs normally (no CUDA_VISIBLE_DEVICES
restriction) -- GPU3 participates as a normal (if already known-degraded)
member, exactly as in every prior real-training test in this project.
Fault injection targets are chosen from GPU4/GPU5 only, never GPU3,
enforced by simply never locking GPU3's clock, not by hiding it from
device enumeration (that CUDA_VISIBLE_DEVICES trick caused a real,
disclosed gpu_slot_index mistargeting bug in the P22-prereq session).
"""
import os
import time
import math
import pickle
import json
from contextlib import nullcontext

import numpy as np
import torch
import torch.distributed as dist
from torch.distributed import init_process_group, destroy_process_group
from torch.distributed._composable.fsdp import fully_shard

from model import GPTConfig, GPT

# -----------------------------------------------------------------------------
out_dir = 'out'
eval_interval = 3000
log_interval = 50
eval_iters = 200
always_save_checkpoint = False
init_from = 'scratch'
dataset = 'shakespeare_char'
gradient_accumulation_steps = 1  # kept at 1: every micro-step's backward triggers a real ReduceScatter
batch_size = 64
block_size = 256
n_layer = 6
n_head = 6
n_embd = 384
dropout = 0.2
bias = False
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
# P22 -- STRAGGLER_* kept for parity with this project's established
# software-fault-injection mechanism, unused by default (this session
# uses a real GPU clock-lock fault instead, injected externally via
# nvidia-smi, not from inside this script).
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
    ddp_rank = 0

# P27-hotfix -- same STRAGGLER_SLEEP_MS/STRAGGLER_TARGET_RANKS convention
# already proven in train_resnet.py/train_vit.py/train_tp_inference.py/
# nanoGPT_tp/train.py -- real, guaranteed-effective time.sleep(), not the
# previously-unused naming-only placeholder this file had before.
STRAGGLER_SLEEP_S = float(os.environ.get("STRAGGLER_SLEEP_MS", "0")) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get("STRAGGLER_TARGET_RANKS", "").split(",") if r.strip() != ""}
STRAGGLER_IS_TARGET = ddp_rank in STRAGGLER_TARGET_RANKS
STRAGGLER_MODE = os.environ.get("STRAGGLER_MODE", "constant")
STRAGGLER_PHASE = os.environ.get("STRAGGLER_PHASE", "backward")
# V1-beta sustained-run validation -- same file_trigger mechanism as
# nanoGPT_straggler/train.py's own _resolve_file_trigger (see its
# docstring): real, mid-run, unannounced-timing fault injection into an
# already-running job, via a shared, host-visible JSON file polled once
# per iteration, without restarting the process. No-op in every other
# mode.
STRAGGLER_TRIGGER_FILE = os.environ.get("STRAGGLER_TRIGGER_FILE", "/tmp/straggler_trigger.json")


def resolve_file_trigger(rank):
    if STRAGGLER_MODE != "file_trigger":
        return STRAGGLER_IS_TARGET, STRAGGLER_SLEEP_S, STRAGGLER_PHASE
    try:
        with open(STRAGGLER_TRIGGER_FILE) as f:
            cfg = json.load(f)
        targets = set(cfg.get("target_ranks", []))
        sleep_s = float(cfg.get("sleep_ms", 0)) / 1000.0
        phase = cfg.get("phase", "backward")
        return (rank in targets), sleep_s, phase
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return False, 0.0, STRAGGLER_PHASE


tokens_per_iter = gradient_accumulation_steps * ddp_world_size * batch_size * block_size
print(f"tokens per iteration will be: {tokens_per_iter:,}")

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

# P22 -- FSDP2: fully_shard applied per transformer block (the standard
# FSDP wrapping granularity -- each block becomes its own "FSDP unit",
# each with its own AllGather-before-forward / ReduceScatter-after-
# backward pair), then once more on the whole model (wraps whatever
# parameters aren't inside any block: wte, wpe, ln_f, lm_head). This is
# real per-communicator sharding of real parameters -- not TP's
# column/row-parallel sharding of individual matmuls, a structurally
# different sharding shape, which is exactly what this session is here
# to test.
if ddp:
    for block in model.transformer.h:
        fully_shard(block)
    fully_shard(model)

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
    # V1-beta sustained-run validation -- resolve this iteration's real
    # fault state from the shared trigger file when in file_trigger mode;
    # a no-op in every other mode (see resolve_file_trigger's docstring).
    STRAGGLER_IS_TARGET, STRAGGLER_SLEEP_S, STRAGGLER_PHASE = resolve_file_trigger(ddp_rank)

    lr = get_lr(iter_num) if decay_lr else learning_rate
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    # V1-beta sustained-run validation -- real, genuinely disk-bound
    # storage fault, the EXACT same mechanism already built and validated
    # for nanoGPT's checkpoint-phase test (P20k/P26.5-maintenance): a
    # real, fsync'd buffered write of a real 4MB-per-second-of-delay
    # buffer to the real host /tmp (ext4 on /dev/vda1, bind-mounted),
    # reused verbatim here, not reimplemented, gated on master_process
    # (mirrors nanoGPT's own real checkpoint-saving rank) and
    # STRAGGLER_PHASE == 'checkpoint'. Independent of eval_interval so it
    # can fire on any real iteration the trigger file names, not just an
    # eval boundary.
    if master_process and STRAGGLER_IS_TARGET and STRAGGLER_SLEEP_S > 0 and STRAGGLER_PHASE == 'checkpoint':
        _ckpt_fault_dir = "/tmp/p22_fsdp_ckpt_fault"
        os.makedirs(_ckpt_fault_dir, exist_ok=True)
        _ckpt_fault_path = os.path.join(_ckpt_fault_dir, f"real_disk_bound_write_rank{ddp_rank}.bin")
        _ckpt_fault_chunk = os.urandom(4 * 1024 * 1024)
        _ckpt_fault_n_chunks = max(1, int(STRAGGLER_SLEEP_S))
        with open(_ckpt_fault_path, "wb") as _f:
            for _ in range(_ckpt_fault_n_chunks):
                _f.write(_ckpt_fault_chunk)
                _f.flush()
                os.fsync(_f.fileno())

    # P22 -- FSDP fix: EVERY rank shares the SAME sharded parameters and
    # forward() unconditionally issues real AllGather calls to unshard
    # them, regardless of grad mode -- unlike plain DDP (where only
    # gradient sync needs symmetry), there is no "master's own subgroup"
    # here, it's the whole world. Gating estimate_loss() on master_process
    # alone caused exactly the call-order/count mismatch this project's
    # own TP script already hit and documented once before (rank 0 calls
    # 200 solo forward passes while every other rank skips straight to the
    # main loop's own forward call) -- confirmed directly this session via
    # the resulting NCCL watchdog timeout (ranks stuck at different
    # collective SeqNums: rank 4 at AllGather SeqNum=11 while others were
    # already at ReduceScatter SeqNum=20). Every rank now enters
    # estimate_loss() together; only rank 0 prints.
    if iter_num % eval_interval == 0:
        losses = estimate_loss()
        if master_process:
            print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    with ctx:
        logits, loss = model(X, Y)
    X, Y = get_batch('train')
    # P27-hotfix -- the STRAGGLER_* naming was previously kept "for
    # parity" only, unimplemented (this workload's real prior validation
    # used nvidia-smi clock-lock instead -- confirmed this session to
    # produce no measurable effect on this hardware for workloads with no
    # software injection alternative). Real time.sleep() on the target
    # rank(s), placed directly before loss.backward() -- the call that
    # triggers this rank's real contribution to FSDP's own
    # ReduceScatter-after-backward sync. No DDP require_backward_grad_sync
    # gate needed here (unlike train_resnet.py/train_vit.py): FSDP2's
    # fully_shard units sync on every backward pass unconditionally.
    if STRAGGLER_IS_TARGET and STRAGGLER_SLEEP_S > 0 and STRAGGLER_PHASE == 'backward':
        time.sleep(STRAGGLER_SLEEP_S)
    loss.backward()
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
