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
import time
import math
import pickle
from contextlib import nullcontext

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor.parallel import parallelize_module, ColwiseParallel, RowwiseParallel
from torch.distributed.tensor import DTensor
import torch.distributed as dist

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
exec(open('configurator.py').read()) # overrides from command line or config file
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
    # seed_offset set below (P21 -- must be dp_rank, not ddp_rank, once TP is present; see that block)
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
    # P21 -- tensor-parallel support. TP_SIZE=1 (default) reproduces the
    # exact original single-communicator DDP behavior unchanged. TP_SIZE>1
    # introduces a second, genuinely distinct NCCL communicator, scoped to
    # TP_SIZE-sized groups of CONSECUTIVE global ranks (mesh layout below
    # puts tp as the fastest-varying dimension, so a TP group is always
    # ranks [k*TP_SIZE, (k+1)*TP_SIZE) -- with nproc_per_node a multiple of
    # TP_SIZE, this keeps every TP group co-located on one node, using
    # NVLink, not cross-node IB -- the realistic placement, not an
    # arbitrary one), alongside the existing whole-job-spanning DP
    # communicator, now scoped to dp_world_size (= ddp_world_size //
    # TP_SIZE) members instead of the full ddp_world_size.
    tp_size = int(os.environ.get('TP_SIZE', '1'))
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
    # world_size number of processes will be training simultaneously, so we can scale
    # down the desired gradient accumulation iterations per process proportionally.
    # P21: scale by dp_world_size, not ddp_world_size -- TP shards ONE logical
    # replica's compute across tp_size ranks, it does not create additional
    # data-parallel replication, so the "how many independent replicas exist"
    # count for gradient-accumulation math is dp_world_size.
    assert gradient_accumulation_steps % dp_world_size == 0
    gradient_accumulation_steps //= dp_world_size
    # P21: both ranks in a TP group must see the IDENTICAL input batch (TP
    # splits the computation of one activation tensor across ranks -- it is
    # not meaningful if the two ranks are computing on different data). The
    # original seed_offset = ddp_rank gave every rank an independent stream;
    # seeding by dp_rank instead makes every rank within the same TP group
    # share the same seed and therefore draw the same batch sequence
    # (get_batch's torch.randint calls happen in the same deterministic
    # order on every rank).
    seed_offset = dp_rank
else:
    # if not ddp, we are running on a single gpu, and one process
    master_process = True
    seed_offset = 0
    ddp_world_size = 1
    tp_size = 1
    dp_world_size = 1
    device_mesh = None
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
data_dir = os.path.join('data', dataset)
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

# P21 -- apply tensor parallelism to each block's MLP (the textbook
# ColwiseParallel/RowwiseParallel target: c_fc shards its output dim across
# the TP group, c_proj shards its input dim and internally issues the
# AllReduce that combines the sharded partial outputs back together -- this
# AllReduce is the genuinely new, TP-scoped communicator this session is
# testing, distinct from the DP gradient-sync communicator below). Applied
# before torch.compile/DDP-wrapping, on the raw model.
if ddp and tp_size > 1:
    tp_mesh = device_mesh["tp"]
    for block in model.transformer.h:
        parallelize_module(block.mlp, tp_mesh, {
            "c_fc": ColwiseParallel(),
            "c_proj": RowwiseParallel(),
        })
    # P21 -- classic DistributedDataParallel's own parameter-broadcast-at-
    # construction (_sync_module_states) coalesces ALL parameters into one
    # buffer to broadcast efficiently, via torch.cat -- and torch.cat
    # cannot mix plain Tensor and DTensor in the same call (confirmed
    # directly: "got mixed torch.Tensor and DTensor, need to convert all
    # torch.Tensor to DTensor before calling distributed operators!").
    # DDP's own device_mesh= constructor argument does NOT avoid this --
    # checked its source directly: device_mesh is just sugar for deriving
    # self.process_group, it still funnels into the same
    # _sync_module_states/_broadcast_coalesced path. The correct, real fix
    # (not a workaround) is DDP's own documented mechanism for excluding
    # specific parameters from its automatic sync/reduction entirely
    # (_set_params_and_buffers_to_ignore_for_model) -- used here for every
    # DTensor (TP-sharded) parameter, which this code then handles itself:
    # broadcast once here (DDP would otherwise have done this at
    # construction) for DP-replica-consistent init, and all_reduce each
    # one's gradient across the dp_group after backward(), below, since
    # DDP's own backward hooks will now skip these parameters too.
    dp_group = device_mesh["dp"].get_group()
    dp_src_rank = ddp_rank % tp_size  # the dp_rank=0 member of this rank's own dp_group
    _tp_sharded_param_names = []
    with torch.no_grad():
        for name, p in model.named_parameters():
            if isinstance(p.data, DTensor):
                _tp_sharded_param_names.append(name)
                dist.broadcast(p.data.to_local(), src=dp_src_rank, group=dp_group)
    DDP._set_params_and_buffers_to_ignore_for_model(model, _tp_sharded_param_names)
    print(f"[rank {ddp_rank}] P21: {len(_tp_sharded_param_names)} TP-sharded (DTensor) "
          f"params excluded from DDP's own sync/reduction, handled manually "
          f"(dp_group broadcast done at init, all_reduce after each backward())")

# initialize a GradScaler. If enabled=False scaler is a no-op
scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

# optimizer
optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
if init_from == 'resume':
    optimizer.load_state_dict(checkpoint['optimizer'])
# P21 -- same mixed Tensor/DTensor limitation as DDP's broadcast and
# clip_grad_norm_, now in fused AdamW's vectorized kernel
# (aten._fused_adamw_.default: same "got mixed torch.Tensor and DTensor"
# error). The single optimizer built above still has DTensor (TP-sharded)
# params mixed into its param_groups. Split them into a second, separate
# optimizer with fused=False (DTensor supports the plain per-tensor op
# dispatch correctly, just not the fused/foreach batched kernel) -- this
# is the real, documented fallback for DTensor parameters with fused/
# foreach optimizers, not a workaround specific to this test. Every
# optimizer.step()/.zero_grad()/scaler.unscale_() call site below now
# iterates _optimizers instead of touching a single `optimizer` directly.
# Known, deliberate simplification: checkpoint save/resume below only
# persists the regular (non-DTensor) optimizer's state -- not needed for
# this validation run, which never resumes from a checkpoint.
if ddp and tp_size > 1:
    for _g in optimizer.param_groups:
        _g['params'] = [p for p in _g['params'] if not isinstance(p.data, DTensor)]
    _dt_decay = [p for p in model.parameters() if p.requires_grad and isinstance(p.data, DTensor) and p.dim() >= 2]
    _dt_nodecay = [p for p in model.parameters() if p.requires_grad and isinstance(p.data, DTensor) and p.dim() < 2]
    dtensor_optimizer = torch.optim.AdamW(
        [{'params': _dt_decay, 'weight_decay': weight_decay},
         {'params': _dt_nodecay, 'weight_decay': 0.0}],
        lr=learning_rate, betas=(beta1, beta2), fused=False)
    _optimizers = [optimizer, dtensor_optimizer]
    print(f"[rank {ddp_rank}] P21: split optimizer -- {sum(p.numel() for g in optimizer.param_groups for p in g['params'])} "
          f"regular params (fused AdamW), {sum(p.numel() for g in [{'params': _dt_decay}, {'params': _dt_nodecay}] for p in g['params'])} "
          f"DTensor params (non-fused AdamW)")
else:
    _optimizers = [optimizer]
checkpoint = None # free up memory

# compile the model
if compile:
    print("compiling the model... (takes a ~minute)")
    unoptimized_model = model
    model = torch.compile(model) # requires PyTorch 2.0

# wrap model into DDP container
if ddp:
    # P21 -- when TP is present, gradient sync must run over the "dp"
    # sub-mesh's process group (dp_world_size members), not the default
    # WORLD group (ddp_world_size members) -- DDP supports this directly
    # via process_group=. This is the existing whole-job AllReduce pattern,
    # now genuinely scoped smaller than the full job once TP exists.
    dp_group = device_mesh["dp"].get_group() if tp_size > 1 else None
    model = DDP(model, device_ids=[ddp_local_rank], process_group=dp_group)

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

    # determine and set the learning rate for this iteration
    lr = get_lr(iter_num) if decay_lr else learning_rate
    for _opt in _optimizers:
        for param_group in _opt.param_groups:
            param_group['lr'] = lr

    # evaluate the loss on train/val sets and write checkpoints
    # P21 -- real deadlock found and fixed here. The original condition
    # ("...and master_process") is safe under plain DDP: a gradient-free
    # (estimate_loss is @torch.no_grad()) forward call needs no cross-rank
    # coordination in DDP, since DDP only synchronizes ranks around
    # backward(). It is NOT safe once TP is present: forward computation
    # through a TP-sharded layer (RowwiseParallel's c_proj) issues a real
    # NCCL AllReduce scoped to the TP group, unconditionally, regardless of
    # grad mode -- so it requires EVERY rank in that TP group to call
    # forward together. With the original gating, master_process (rank 0)
    # entered estimate_loss()'s 400 solo forward passes while its TP
    # partner (rank 1, same tp mesh, dp_rank 0) skipped straight to the
    # main loop's own forward call -- a genuine call-count/order mismatch
    # inside the TP communicator, confirmed directly to hang (debug prints
    # showed no rank ever reaching the main loop's own forward call).
    # Fixed by gating on "this rank's OWN dp_rank is 0" (dp_rank==0 means
    # this rank IS master_process, or IS master_process's TP partner) --
    # every rank sharing master_process's TP group now enters
    # estimate_loss() together, keeping their shared TP communicator's
    # call sequence symmetric; other DP replicas' ranks (a completely
    # separate, independent TP communicator) still skip it entirely, which
    # is safe since it doesn't affect their own TP group's symmetry.
    # Printing/checkpointing remains gated on the true master_process only.
    _eval_participant = (dp_rank == 0) if (ddp and tp_size > 1) else master_process
    if iter_num % eval_interval == 0 and _eval_participant:
        losses = estimate_loss()
        if master_process:
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
        with ctx:
            logits, loss = model(X, Y)
            loss = loss / gradient_accumulation_steps # scale the loss to account for gradient accumulation
        # immediately async prefetch next batch while model is doing the forward pass on the GPU
        X, Y = get_batch('train')
        # P20k Test 1/2: delay right before the backward() call that
        # actually triggers the gradient-sync all_reduce (only true on the
        # last micro_step, when require_backward_grad_sync is True) -- not
        # on every micro_step, and not anywhere else in the step.
        if ddp and _straggler_is_target and model.require_backward_grad_sync and _straggler_sleep_s > 0:
            time.sleep(_straggler_sleep_s)
        # backward pass, with gradient scaling if training in fp16
        scaler.scale(loss).backward()
    # P21 -- DDP was told to ignore the TP-sharded (DTensor) parameters
    # entirely (see the model-construction comment above), so their
    # gradients never went through DDP's own backward hooks/bucket
    # all_reduce. Do that sync explicitly here, once per optimizer step
    # (matching where DDP's own all_reduce would have already completed by
    # this point for the non-TP parameters), only across the dp_group (the
    # TP-scoped combine for the *forward* activations already happened
    # inside RowwiseParallel automatically -- this is the separate,
    # DP-scoped gradient-averaging step for those same sharded weights).
    if ddp and tp_size > 1:
        with torch.no_grad():
            raw_model_for_grad_sync = model.module if hasattr(model, 'module') else model
            for name, p in raw_model_for_grad_sync.named_parameters():
                if name in _tp_sharded_param_names and p.grad is not None:
                    local_grad = p.grad.to_local() if isinstance(p.grad, DTensor) else p.grad
                    dist.all_reduce(local_grad, op=dist.ReduceOp.AVG, group=dp_group)
    # clip the gradient
    if grad_clip != 0.0:
        for _opt in _optimizers:
            scaler.unscale_(_opt)
        if ddp and tp_size > 1:
            # P21 -- same mixed Tensor/DTensor limitation as DDP's own
            # broadcast (see model-construction comment): clip_grad_norm_'s
            # vectorized _foreach_norm also can't take a parameter list
            # containing both types at once. Clipped as two separate
            # groups (each against its own local norm) rather than one
            # exact cross-shard-aware global norm -- a known, deliberate
            # simplification for this validation run, not a claim that
            # this is bit-for-bit what a production TP framework would do
            # for gradient clipping; it does not affect the aggregator/
            # classifier-pipeline questions this session is actually
            # testing.
            _regular_params = [p for n, p in model.named_parameters() if n.replace('module.', '', 1) not in _tp_sharded_param_names]
            _dtensor_params = [p for n, p in model.named_parameters() if n.replace('module.', '', 1) in _tp_sharded_param_names]
            torch.nn.utils.clip_grad_norm_(_regular_params, grad_clip)
            if _dtensor_params:
                torch.nn.utils.clip_grad_norm_(_dtensor_params, grad_clip)
        else:
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
    # step the optimizer and scaler if training in fp16
    for _opt in _optimizers:
        scaler.step(_opt)
    scaler.update()
    # flush the gradients as soon as we can, no need for this memory anymore
    for _opt in _optimizers:
        _opt.zero_grad(set_to_none=True)

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
