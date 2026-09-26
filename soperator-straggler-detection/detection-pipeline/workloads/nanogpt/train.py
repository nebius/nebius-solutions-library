"""
This training script can be run both on a single gpu in debug mode,
and also in a larger training run with distributed data parallel (ddp).

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False

To run with DDP on 4 gpus on 1 node, example:
$ torchrun --standalone --nproc_per_node=4 train.py

To run with DDP on 4 gpus across 2 nodes, example:
- Run on the first (master) node with example IP 123.456.123.456:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
- Run on the worker node:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
(If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
"""

import os
import sys
import time
import math
import pickle
import json
from contextlib import nullcontext

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

# Stage 2 cluster-topology-agnostic fix: model.py/configurator.py used to
# be duplicated into this directory because a bare `from model import`
# and `exec(open('configurator.py'))` both only resolve relative to the
# process's own CWD, not this script's real location -- and this
# package's launch scripts `cd` elsewhere before running. Both now
# resolve relative to the shared ../nanogpt-base/ directory explicitly,
# regardless of CWD -- one real copy, not a duplicate per shape.
_NANOGPT_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nanogpt-base")
sys.path.insert(0, _NANOGPT_BASE)
from model import GPTConfig, GPT

# -----------------------------------------------------------------------------
# default config values designed to train a gpt2 (124M) on OpenWebText
# I/O
out_dir = 'out'
eval_interval = 2000
log_interval = 1
eval_iters = 200
eval_only = False # if True, script exits right after the first eval
always_save_checkpoint = True # if True, always save a checkpoint after each eval
init_from = 'scratch' # 'scratch' or 'resume' or 'gpt2*'
# wandb logging
wandb_log = False # disabled by default
wandb_project = 'owt'
wandb_run_name = 'gpt2' # 'run' + str(time.time())
# data
dataset = 'openwebtext'
gradient_accumulation_steps = 5 * 8 # used to simulate larger batch sizes
batch_size = 12 # if gradient_accumulation_steps > 1, this is the micro-batch size
block_size = 1024
# model
n_layer = 12
n_head = 12
n_embd = 768
dropout = 0.0 # for pretraining 0 is good, for finetuning try 0.1+
bias = False # do we use bias inside LayerNorm and Linear layers?
# adamw optimizer
learning_rate = 6e-4 # max learning rate
max_iters = 600000 # total number of training iterations
weight_decay = 1e-1
beta1 = 0.9
beta2 = 0.95
grad_clip = 1.0 # clip gradients at this value, or disable if == 0.0
# learning rate decay settings
decay_lr = True # whether to decay the learning rate
warmup_iters = 2000 # how many steps to warm up for
lr_decay_iters = 600000 # should be ~= max_iters per Chinchilla
min_lr = 6e-5 # minimum learning rate, should be ~= learning_rate/10 per Chinchilla
# DDP settings
backend = 'nccl' # 'nccl', 'gloo', etc.
# system
device = 'cuda' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16' # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
compile = True # use PyTorch 2.0 to compile the model to be faster
# -----------------------------------------------------------------------------
config_keys = [k for k,v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
exec(open(os.path.join(_NANOGPT_BASE, 'configurator.py')).read()) # overrides from command line or config file
config = {k: globals()[k] for k in config_keys} # will be useful for logging
# -----------------------------------------------------------------------------

# various inits, derived attributes, I/O setup
ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
if ddp:
    init_process_group(backend=backend)
    ddp_rank = int(os.environ['RANK'])
    ddp_local_rank = int(os.environ['LOCAL_RANK'])
    ddp_world_size = int(os.environ['WORLD_SIZE'])
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
    seed_offset = ddp_rank # each process gets a different seed
    # P20k Test 1/2 -- controlled, rank-gated delay inserted directly before
    # the collective call the gradient sync all_reduce triggers (see the
    # backward() call below, gated on require_backward_grad_sync). Not a
    # sleep anywhere else in the step -- specifically here, so the delay is
    # the direct, software-only mechanism the whole detection system is
    # built on (early arrivers wait inside the collective; the straggler
    # arrives late and shows the shortest exec time).
    _straggler_sleep_s = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
    _straggler_targets_env = os.environ.get('STRAGGLER_TARGET_RANKS', '')
    _straggler_target_ranks = {int(r) for r in _straggler_targets_env.split(',') if r.strip() != ''}
    _straggler_is_target = ddp_rank in _straggler_target_ranks
    # Dedup-vs-fault-shape follow-up session -- constant-every-iteration
    # sleep (the only mode this file used to support) is the easy case for
    # coarse dedup, since it never has a calm gap for dedup to land on.
    # Two new modes, gated by wall-clock/iteration-count rather than firing
    # unconditionally every iteration, to test the cases dedup can
    # actually hide: 'medium' (sustained slowness for a real on/off
    # wall-clock window, repeating) and 'jitter' (short bursts on only
    # every Nth iteration, real calm gaps in between).
    _straggler_mode = os.environ.get('STRAGGLER_MODE', 'constant')
    _straggler_on_s = float(os.environ.get('STRAGGLER_ON_S', '8'))
    _straggler_cycle_s = float(os.environ.get('STRAGGLER_CYCLE_S', '20'))
    _straggler_every_n = int(os.environ.get('STRAGGLER_EVERY_N', '7'))
    _straggler_t0 = time.time()
    _straggler_iter_count = 0
    # Phase-boundary stress test (this session) -- WHERE in the step the
    # sleep is placed, reusing the exact same STRAGGLER_SLEEP_MS/
    # TARGET_RANKS knob and time.sleep() mechanism, not a new injection
    # method. 'backward' (default) is this file's original, existing call
    # site (after forward, before backward/the gradient-sync collective).
    _straggler_phase = os.environ.get('STRAGGLER_PHASE', 'backward')
    # 'warmup' mode (new): fires every iteration like 'constant', but
    # ONLY while iter_num < STRAGGLER_WARMUP_ITERS, then never again --
    # tests whether a fault confined to the pre-calibration window is
    # correctly silent-then-clean rather than a false positive once
    # calibration completes on now-healthy data.
    _straggler_warmup_iters = int(os.environ.get('STRAGGLER_WARMUP_ITERS', '50'))

    def _straggler_should_fire(current_iter_num):
        nonlocal_fire = True
        if _straggler_mode == 'medium':
            phase = (time.time() - _straggler_t0) % _straggler_cycle_s
            nonlocal_fire = phase < _straggler_on_s
        elif _straggler_mode == 'jitter':
            pass  # handled by caller (needs a persistent counter) -- unused for phase-boundary tests
        elif _straggler_mode == 'warmup':
            nonlocal_fire = current_iter_num < _straggler_warmup_iters
        return nonlocal_fire

    # V1-beta sustained-run validation -- 'file_trigger' mode: real,
    # mid-run, unannounced-timing fault injection into an ALREADY-RUNNING
    # job, without restarting it (env vars are fixed at process launch,
    # so they can't express "inject now, at a moment decided after the
    # job is already underway"). Reuses every existing fault mechanism
    # (sleep-based delay, real fsync'd disk write) completely unchanged --
    # the only new piece is the trigger SOURCE: a shared, host-visible
    # JSON file this rank polls once per iteration (cheap os.path.exists),
    # instead of a fixed env var read once at startup. The orchestrator
    # writes {"target_ranks": [...], "sleep_ms": N, "phase": "backward"|
    # "forward"|"optimizer"|"checkpoint"} at whatever real moment it
    # chooses, and deletes it to end the fault -- full external control,
    # zero change to the job's own process.
    _straggler_trigger_file = os.environ.get('STRAGGLER_TRIGGER_FILE', '/tmp/straggler_trigger.json')

    def _resolve_file_trigger(rank):
        if _straggler_mode != 'file_trigger':
            return _straggler_is_target, _straggler_sleep_s, _straggler_phase
        try:
            with open(_straggler_trigger_file) as _f:
                _cfg = json.load(_f)
            _targets = set(_cfg.get('target_ranks', []))
            _sleep_s = float(_cfg.get('sleep_ms', 0)) / 1000.0
            _phase = _cfg.get('phase', 'backward')
            return (rank in _targets), _sleep_s, _phase
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return False, 0.0, _straggler_phase
    # world_size number of processes will be training simultaneously, so we can scale
    # down the desired gradient accumulation iterations per process proportionally
    assert gradient_accumulation_steps % ddp_world_size == 0
    gradient_accumulation_steps //= ddp_world_size
else:
    # if not ddp, we are running on a single gpu, and one process
    master_process = True
    seed_offset = 0
    ddp_world_size = 1
tokens_per_iter = gradient_accumulation_steps * ddp_world_size * batch_size * block_size
print(f"tokens per iteration will be: {tokens_per_iter:,}")

if master_process:
    os.makedirs(out_dir, exist_ok=True)
torch.manual_seed(1337 + seed_offset)
torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
# note: float16 data type will automatically use a GradScaler
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

# poor man's data loader
# Stage 2 cluster-topology-agnostic fix: 'data' used to be a bare
# CWD-relative string -- now resolved against this package's real,
# shared dataset location (../shared-data/), regardless of CWD.
data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared-data', dataset)
def get_batch(split):
    # We recreate np.memmap every batch to avoid a memory leak, as per
    # https://stackoverflow.com/questions/45132940/numpy-memmap-memory-usage-want-to-iterate-once/61472122#61472122
    if split == 'train':
        data = np.memmap(os.path.join(data_dir, 'train.bin'), dtype=np.uint16, mode='r')
    else:
        data = np.memmap(os.path.join(data_dir, 'val.bin'), dtype=np.uint16, mode='r')
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([torch.from_numpy((data[i:i+block_size]).astype(np.int64)) for i in ix])
    y = torch.stack([torch.from_numpy((data[i+1:i+1+block_size]).astype(np.int64)) for i in ix])
    if device_type == 'cuda':
        # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
        x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
    else:
        x, y = x.to(device), y.to(device)
    return x, y

# init these up here, can override if init_from='resume' (i.e. from a checkpoint)
iter_num = 0
best_val_loss = 1e9

# attempt to derive vocab_size from the dataset
meta_path = os.path.join(data_dir, 'meta.pkl')
meta_vocab_size = None
if os.path.exists(meta_path):
    with open(meta_path, 'rb') as f:
        meta = pickle.load(f)
    meta_vocab_size = meta['vocab_size']
    print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

# model init
model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=block_size,
                  bias=bias, vocab_size=None, dropout=dropout) # start with model_args from command line
if init_from == 'scratch':
    # init a new model from scratch
    print("Initializing a new model from scratch")
    # determine the vocab size we'll use for from-scratch training
    if meta_vocab_size is None:
        print("defaulting to vocab_size of GPT-2 to 50304 (50257 rounded up for efficiency)")
    model_args['vocab_size'] = meta_vocab_size if meta_vocab_size is not None else 50304
    gptconf = GPTConfig(**model_args)
    model = GPT(gptconf)
elif init_from == 'resume':
    print(f"Resuming training from {out_dir}")
    # resume training from a checkpoint.
    ckpt_path = os.path.join(out_dir, 'ckpt.pt')
    checkpoint = torch.load(ckpt_path, map_location=device)
    checkpoint_model_args = checkpoint['model_args']
    # force these config attributes to be equal otherwise we can't even resume training
    # the rest of the attributes (e.g. dropout) can stay as desired from command line
    for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
        model_args[k] = checkpoint_model_args[k]
    # create the model
    gptconf = GPTConfig(**model_args)
    model = GPT(gptconf)
    state_dict = checkpoint['model']
    # fix the keys of the state dictionary :(
    # honestly no idea how checkpoints sometimes get this prefix, have to debug more
    unwanted_prefix = '_orig_mod.'
    for k,v in list(state_dict.items()):
        if k.startswith(unwanted_prefix):
            state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
    model.load_state_dict(state_dict)
    iter_num = checkpoint['iter_num']
    best_val_loss = checkpoint['best_val_loss']
elif init_from.startswith('gpt2'):
    print(f"Initializing from OpenAI GPT-2 weights: {init_from}")
    # initialize from OpenAI GPT-2 weights
    override_args = dict(dropout=dropout)
    model = GPT.from_pretrained(init_from, override_args)
    # read off the created config params, so we can store them into checkpoint correctly
    for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
        model_args[k] = getattr(model.config, k)
# crop down the model block size if desired, using model surgery
if block_size < model.config.block_size:
    model.crop_block_size(block_size)
    model_args['block_size'] = block_size # so that the checkpoint will have the right value
model.to(device)

# initialize a GradScaler. If enabled=False scaler is a no-op
scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

# optimizer
optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
if init_from == 'resume':
    optimizer.load_state_dict(checkpoint['optimizer'])
checkpoint = None # free up memory

# compile the model
if compile:
    print("compiling the model... (takes a ~minute)")
    unoptimized_model = model
    model = torch.compile(model) # requires PyTorch 2.0

# wrap model into DDP container
if ddp:
    model = DDP(model, device_ids=[ddp_local_rank])

# helps estimate an arbitrarily accurate loss over either split using many batches
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

# learning rate decay scheduler (cosine with warmup)
def get_lr(it):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * (it + 1) / (warmup_iters + 1)
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)

# logging
if wandb_log and master_process:
    import wandb
    wandb.init(project=wandb_project, name=wandb_run_name, config=config)

# training loop
X, Y = get_batch('train') # fetch the very first batch
t0 = time.time()
local_iter_num = 0 # number of iterations in the lifetime of this process
raw_model = model.module if ddp else model # unwrap DDP container if needed
running_mfu = -1.0
while True:
    # V1-beta sustained-run validation -- resolve this iteration's real
    # fault state from the shared trigger file when in file_trigger mode
    # (see _resolve_file_trigger's own docstring); a no-op (returns the
    # same static values unchanged) in every other mode, so no prior
    # test's behavior changes.
    _straggler_is_target, _straggler_sleep_s, _straggler_phase = _resolve_file_trigger(ddp_rank)

    # determine and set the learning rate for this iteration
    lr = get_lr(iter_num) if decay_lr else learning_rate
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    # evaluate the loss on train/val sets and write checkpoints
    if iter_num % eval_interval == 0 and master_process:
        losses = estimate_loss()
        print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
        if wandb_log:
            wandb.log({
                "iter": iter_num,
                "train/loss": losses['train'],
                "val/loss": losses['val'],
                "lr": lr,
                "mfu": running_mfu*100, # convert to percentage
            })
        if losses['val'] < best_val_loss or always_save_checkpoint:
            best_val_loss = losses['val']
            if iter_num > 0:
                checkpoint = {
                    'model': raw_model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'model_args': model_args,
                    'iter_num': iter_num,
                    'best_val_loss': best_val_loss,
                    'config': config,
                }
                print(f"saving checkpoint to {out_dir}")
                # Phase-boundary follow-up (this session) -- replaces the
                # earlier time.sleep()-based checkpoint fault, which only
                # ever delayed WHEN torch.save() started, not the disk
                # throughput during the write itself (confirmed: rank5
                # was never even the right target -- this whole block
                # only runs on master_process, i.e. rank 0 -- so the
                # original test's fault, targeting rank5, never fired at
                # all). Real, disk-bound slowdown: a real, fsync'd
                # buffered write of a real 512MB buffer to the real host
                # /tmp (ext4 on /dev/vda1, bind-mounted into this
                # container -- confirmed empirically THIS session that
                # writes here correctly attribute real, substantial
                # per-pid iowait via the existing eBPF agent once the
                # agent itself runs on the same real machine as this
                # process, not a different one). Runs on the SAME rank
                # that's about to call torch.save(), immediately before
                # it, so the real io-wait falls inside the checkpoint
                # window this test measures.
                if (ddp and _straggler_is_target and _straggler_sleep_s > 0 and _straggler_phase == 'checkpoint'):
                    _ckpt_fault_dir = "/tmp/p20k_ckpt_fault"
                    os.makedirs(_ckpt_fault_dir, exist_ok=True)
                    _ckpt_fault_path = os.path.join(_ckpt_fault_dir, f"real_disk_bound_write_rank{ddp_rank}.bin")
                    _ckpt_fault_chunk = os.urandom(4 * 1024 * 1024)
                    _ckpt_fault_n_chunks = max(1, int(_straggler_sleep_s))  # ~1 real 4MB fsync'd write per requested "second" of delay
                    with open(_ckpt_fault_path, "wb") as _f:
                        for _ in range(_ckpt_fault_n_chunks):
                            _f.write(_ckpt_fault_chunk)
                            _f.flush()
                            os.fsync(_f.fileno())
                torch.save(checkpoint, os.path.join(out_dir, 'ckpt.pt'))
    if iter_num == 0 and eval_only:
        break

    # forward backward update, with optional gradient accumulation to simulate larger batch size
    # and using the GradScaler if data type is float16
    for micro_step in range(gradient_accumulation_steps):
        if ddp:
            # in DDP training we only need to sync gradients at the last micro step.
            # the official way to do this is with model.no_sync() context manager, but
            # I really dislike that this bloats the code and forces us to repeat code
            # looking at the source of that context manager, it just toggles this variable
            model.require_backward_grad_sync = (micro_step == gradient_accumulation_steps - 1)
        if (ddp and _straggler_is_target and model.require_backward_grad_sync and _straggler_sleep_s > 0
                and _straggler_phase == 'forward'):
            _straggler_fire = True
            if _straggler_mode == 'warmup':
                _straggler_fire = _straggler_should_fire(iter_num)
            if _straggler_fire:
                time.sleep(_straggler_sleep_s)
        with ctx:
            logits, loss = model(X, Y)
            loss = loss / gradient_accumulation_steps # scale the loss to account for gradient accumulation
        # immediately async prefetch next batch while model is doing the forward pass on the GPU
        X, Y = get_batch('train')
        # P20k Test 1/2: delay right before the backward() call that
        # actually triggers the gradient-sync all_reduce (only true on the
        # last micro_step, when require_backward_grad_sync is True) -- not
        # on every micro_step, and not anywhere else in the step.
        if (ddp and _straggler_is_target and model.require_backward_grad_sync and _straggler_sleep_s > 0
                and _straggler_phase == 'backward'):
            _straggler_fire = True
            if _straggler_mode == 'medium':
                _straggler_phase_pos = (time.time() - _straggler_t0) % _straggler_cycle_s
                _straggler_fire = _straggler_phase_pos < _straggler_on_s
            elif _straggler_mode == 'jitter':
                _straggler_iter_count += 1
                _straggler_fire = (_straggler_iter_count % _straggler_every_n) == 0
            elif _straggler_mode == 'warmup':
                _straggler_fire = _straggler_should_fire(iter_num)
            if _straggler_fire:
                time.sleep(_straggler_sleep_s)
        # backward pass, with gradient scaling if training in fp16
        scaler.scale(loss).backward()
    # clip the gradient
    if grad_clip != 0.0:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
    if (ddp and _straggler_is_target and _straggler_sleep_s > 0 and _straggler_phase == 'optimizer'):
        _straggler_fire = True
        if _straggler_mode == 'warmup':
            _straggler_fire = _straggler_should_fire(iter_num)
        if _straggler_fire:
            time.sleep(_straggler_sleep_s)
    # step the optimizer and scaler if training in fp16
    scaler.step(optimizer)
    scaler.update()
    # flush the gradients as soon as we can, no need for this memory anymore
    optimizer.zero_grad(set_to_none=True)

    # timing and logging
    t1 = time.time()
    dt = t1 - t0
    t0 = t1
    if iter_num % log_interval == 0 and master_process:
        # get loss as float. note: this is a CPU-GPU sync point
        # scale up to undo the division above, approximating the true total loss (exact would have been a sum)
        lossf = loss.item() * gradient_accumulation_steps
        if local_iter_num >= 5: # let the training loop settle a bit
            mfu = raw_model.estimate_mfu(batch_size * gradient_accumulation_steps, dt)
            running_mfu = mfu if running_mfu == -1.0 else 0.9*running_mfu + 0.1*mfu
        print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")
    iter_num += 1
    local_iter_num += 1

    # termination conditions
    if iter_num > max_iters:
        break

if ddp:
    # Ensure every rank across both nodes has actually finished training
    # before any of them starts tearing down its NCCL communicator --
    # without this, one node's ranks can call destroy_process_group()
    # while the other node's ranks are still mid-collective, and the
    # watchdog times out after 600s waiting for a peer that already left.
    torch.distributed.barrier()
    destroy_process_group()
