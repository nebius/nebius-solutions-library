"""
Full definition of a GPT Language Model, all of it in this single file.
References:
1) the official GPT-2 TensorFlow implementation released by OpenAI:
https://github.com/openai/gpt-2/blob/master/src/model.py
2) huggingface/transformers PyTorch implementation:
https://github.com/huggingface/transformers/blob/main/src/transformers/models/gpt2/modeling_gpt2.py
"""

import math
import os
import time
import inspect
from dataclasses import dataclass

import torch
import torch.nn as nn
from torch.nn import functional as F
import torch.distributed as dist
import torch.distributed.nn.functional as dist_nn_func

# P23 -- set by train_moe.py after init_process_group(), before GPT(config)
# is constructed. Module-level globals (not threaded through GPTConfig/
# Block/GPT's constructors) -- simplest correct option for a toy, single-
# purpose copy of this file, not a shared module.
MOE_RANK = 0
MOE_WORLD_SIZE = 1
MOE_NUM_EXPERTS = 8

# P23 (single-rank AllToAll fault, Candidate C) -- a real, plain
# application-level delay held immediately before ONE rank's own
# dispatch all_to_all_single call, gated purely on this process's own
# rank via a read-once-at-import env var (no threading through
# train_moe.py needed for a test-only mechanism like this). NOT an NCCL-
# or RDMA-level change -- every rank still calls the exact same fixed
# sequence of collectives, in the same logical order, on the same
# iteration-count-gated schedule; this only makes rank
# MOE_FAULT_TARGET_RANK real-world SLOWER to reach its own call, which
# every other rank simply waits for at that synchronization point --
# exactly how a genuine hardware/network straggler behaves, not a
# divergence in which/how-many collectives get called. time.sleep()
# chosen over a busy-wait so the fault doesn't itself burn CPU/GPU
# cycles that a real network/host straggler wouldn't.
MOE_FAULT_TARGET_RANK = int(os.environ.get("MOE_FAULT_TARGET_RANK", "-1"))
MOE_FAULT_DELAY_US = float(os.environ.get("MOE_FAULT_DELAY_US", "0"))

class LayerNorm(nn.Module):
    """ LayerNorm but with an optional bias. PyTorch doesn't support simply bias=False """

    def __init__(self, ndim, bias):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(ndim))
        self.bias = nn.Parameter(torch.zeros(ndim)) if bias else None

    def forward(self, input):
        return F.layer_norm(input, self.weight.shape, self.weight, self.bias, 1e-5)

class CausalSelfAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd, bias=config.bias)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd, bias=config.bias)
        # regularization
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        self.dropout = config.dropout
        # flash attention make GPU go brrrrr but support is only in PyTorch >= 2.0
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention')
        if not self.flash:
            print("WARNING: using slow attention. Flash Attention requires PyTorch >= 2.0")
            # causal mask to ensure that attention is only applied to the left in the input sequence
            self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size))
                                        .view(1, 1, config.block_size, config.block_size))

    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k, v  = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        if self.flash:
            # efficient attention using Flash Attention CUDA kernels
            y = torch.nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=None, dropout_p=self.dropout if self.training else 0, is_causal=True)
        else:
            # manual implementation of attention
            att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
            att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
            att = F.softmax(att, dim=-1)
            att = self.attn_dropout(att)
            y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y

class MLP(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.c_fc    = nn.Linear(config.n_embd, 4 * config.n_embd, bias=config.bias)
        self.gelu    = nn.GELU()
        self.c_proj  = nn.Linear(4 * config.n_embd, config.n_embd, bias=config.bias)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        x = self.dropout(x)
        return x

class MoEMLP(nn.Module):
    """P23 -- toy MoE MLP replacing the standard dense MLP block. Top-1
    gating over MOE_NUM_EXPERTS experts; expert e is hosted 1:1 on rank e
    (e in [0, MOE_NUM_EXPERTS)). Every rank in the world still
    participates symmetrically in the same world-spanning
    all_to_all_single calls (this is the fixed, per-rank-identical
    collective schedule every rank enters -- no rank ever conditionally
    skips a collective based on its own data, only the SPLIT SIZES within
    a call vary, which is the legitimate, real MoE load-imbalance this
    session is here to produce and measure); ranks >= MOE_NUM_EXPERTS
    always send/receive zero-sized chunks to/from every peer (the router
    can never emit a class for them), never actually running their own
    identically-shaped expert weights on real data.

    Real, data-dependent per-rank/per-expert routing counts -- computed
    fresh from THIS batch's router logits every forward pass, not a
    synthetic uniform-split assumption. A tiny all_to_all_single of the
    resulting counts happens first (every peer needs to know how many
    rows to allocate for the real dispatch/combine calls that follow) --
    the standard two-step pattern real MoE dispatch requires when routing
    is data-dependent.
    """

    def __init__(self, config):
        super().__init__()
        self.num_experts = MOE_NUM_EXPERTS
        self.rank = MOE_RANK
        self.world_size = MOE_WORLD_SIZE
        self.router = nn.Linear(config.n_embd, self.num_experts, bias=False)
        self._fwd_idx = 0
        self.c_fc    = nn.Linear(config.n_embd, 4 * config.n_embd, bias=config.bias)
        self.gelu    = nn.GELU()
        self.c_proj  = nn.Linear(4 * config.n_embd, config.n_embd, bias=config.bias)
        self.dropout = nn.Dropout(config.dropout)

    def expert(self, x):
        return self.dropout(self.c_proj(self.gelu(self.c_fc(x))))

    def forward(self, x):
        B, T, C = x.shape
        x_flat = x.reshape(-1, C)

        logits = self.router(x_flat)                       # (N, num_experts)
        top1 = logits.argmax(dim=-1)                        # (N,)
        gate = F.softmax(logits, dim=-1).gather(1, top1.unsqueeze(1)).squeeze(1)  # (N,)

        counts = torch.zeros(self.world_size, dtype=torch.long, device=x.device)
        counts[:self.num_experts] = torch.bincount(top1, minlength=self.num_experts)

        order = torch.argsort(top1)
        x_sorted = x_flat[order]
        input_splits = counts.tolist()

        # step 1: exchange real per-rank routed-token counts (tiny, fixed
        # world_size-length tensor -- not autograd-tracked, integers only)
        out_counts = torch.empty_like(counts)
        dist.all_to_all_single(out_counts, counts)
        output_splits = out_counts.tolist()

        # Stage-2 localization (P30) -- real, generic per-rank assigned-
        # token load for this round: output_splits[j] is how many tokens
        # peer j actually sent to THIS rank (all_to_all_single's real
        # exchange, not an assumption), so sum(output_splits) is this
        # rank's real total workload as an expert this round. Logged
        # here, before the fault-injection sleep below, so the load
        # signal itself is never affected by an injected delay -- not
        # hardcoded to any world_size/expert count.
        self._fwd_idx += 1
        print(f"[MOE-TOKEN-LOAD] rank={self.rank} block={id(self)} round={self._fwd_idx} "
              f"assigned_total={sum(output_splits)} output_splits={output_splits}",
              flush=True)

        # P23 (Candidate C) -- real delay for ONLY this rank, immediately
        # before its own dispatch call; every other rank's code path
        # (including every OTHER block's identical call on this SAME
        # rank) is untouched.
        if self.rank == MOE_FAULT_TARGET_RANK and MOE_FAULT_DELAY_US > 0:
            time.sleep(MOE_FAULT_DELAY_US / 1e6)

        # step 2: dispatch -- real tokens, autograd-aware so gradients
        # flow back through routing during backward()
        recv = dist_nn_func.all_to_all_single(
            torch.empty(sum(output_splits), C, device=x.device, dtype=x.dtype),
            x_sorted, output_splits, input_splits)

        processed = self.expert(recv) if self.rank < self.num_experts else recv

        # P23 -- two real, related bugs found and fixed live, both from
        # the same root cause: under autocast, LayerNorm's output (this
        # block's ln_2(x), i.e. the x this forward() receives) is forced
        # to float32 (a standard autocast policy), so recv/x_sorted are
        # float32 -- but self.expert()'s own Linear/GELU/Linear chain,
        # also autocast-governed, casts its OUTPUT back to bfloat16
        # regardless of its float32 input.
        #   Bug 1 (this rank's own dtype mismatch): the combine call's
        #   output template must match processed's REAL dtype, not be
        #   assumed to equal x_sorted's -- confirmed via a real crash:
        #   "Invalid usage of tensors with different dtypes... float32
        #   and bfloat16" raised by dist.all_to_all_single's own local
        #   (this-rank-only) dtype check.
        #   Bug 2 (cross-rank dtype disagreement, more serious): ranks
        #   < num_experts apply self.expert() and get bfloat16 back;
        #   ranks >= num_experts skip it entirely (`processed = recv`)
        #   and stay float32. The SAME combine collective would then be
        #   called with DIFFERENT dtypes on different ranks -- invisible
        #   to any single rank's own local dtype check (each rank's own
        #   output/input pair matches locally), but a real cross-rank
        #   protocol mismatch that can silently corrupt data or hang.
        #   Suspected (not fully confirmed) to be the cause of a real
        #   NCCL watchdog hang observed live at block index 1's first
        #   collective. Fixed by forcing processed back to recv's dtype
        #   on every rank unconditionally, so every rank's combine call
        #   uses the identical, real dtype regardless of whether
        #   self.expert() ran.
        processed = processed.to(recv.dtype)

        # step 3: combine -- send processed tokens back to their origin rank
        combined = dist_nn_func.all_to_all_single(
            torch.empty(x_sorted.shape[0], C, device=x.device, dtype=recv.dtype),
            processed, input_splits, output_splits)

        out = torch.empty_like(combined)
        out[order] = combined
        out = out * gate.unsqueeze(1)
        return out.reshape(B, T, C)

class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        self.ln_1 = LayerNorm(config.n_embd, bias=config.bias)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = LayerNorm(config.n_embd, bias=config.bias)
        self.mlp = MoEMLP(config)

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

@dataclass
class GPTConfig:
    block_size: int = 1024
    vocab_size: int = 50304 # GPT-2 vocab_size of 50257, padded up to nearest multiple of 64 for efficiency
    n_layer: int = 12
    n_head: int = 12
    n_embd: int = 768
    dropout: float = 0.0
    bias: bool = True # True: bias in Linears and LayerNorms, like GPT-2. False: a bit better and faster

class GPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        assert config.vocab_size is not None
        assert config.block_size is not None
        self.config = config

        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd),
            wpe = nn.Embedding(config.block_size, config.n_embd),
            drop = nn.Dropout(config.dropout),
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
            ln_f = LayerNorm(config.n_embd, bias=config.bias),
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        # with weight tying when using torch.compile() some warnings get generated:
        # "UserWarning: functional_call was passed multiple values for tied weights.
        # This behavior is deprecated and will be an error in future versions"
        # not 100% sure what this is, so far seems to be harmless. TODO investigate
        self.transformer.wte.weight = self.lm_head.weight # https://paperswithcode.com/method/weight-tying

        # init all weights
        self.apply(self._init_weights)
        # apply special scaled init to the residual projections, per GPT-2 paper
        for pn, p in self.named_parameters():
            if pn.endswith('c_proj.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=0.02/math.sqrt(2 * config.n_layer))

        # report number of parameters
        print("number of parameters: %.2fM" % (self.get_num_params()/1e6,))

    def get_num_params(self, non_embedding=True):
        """
        Return the number of parameters in the model.
        For non-embedding count (default), the position embeddings get subtracted.
        The token embeddings would too, except due to the parameter sharing these
        params are actually used as weights in the final layer, so we include them.
        """
        n_params = sum(p.numel() for p in self.parameters())
        if non_embedding:
            n_params -= self.transformer.wpe.weight.numel()
        return n_params

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        device = idx.device
        b, t = idx.size()
        assert t <= self.config.block_size, f"Cannot forward sequence of length {t}, block size is only {self.config.block_size}"
        pos = torch.arange(0, t, dtype=torch.long, device=device) # shape (t)

        # forward the GPT model itself
        tok_emb = self.transformer.wte(idx) # token embeddings of shape (b, t, n_embd)
        pos_emb = self.transformer.wpe(pos) # position embeddings of shape (t, n_embd)
        x = self.transformer.drop(tok_emb + pos_emb)
        for block in self.transformer.h:
            x = block(x)
        x = self.transformer.ln_f(x)

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.lm_head(x)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        else:
            # inference-time mini-optimization: only forward the lm_head on the very last position
            logits = self.lm_head(x[:, [-1], :]) # note: using list [-1] to preserve the time dim
            loss = None

        return logits, loss

    def crop_block_size(self, block_size):
        # model surgery to decrease the block size if necessary
        # e.g. we may load the GPT2 pretrained model checkpoint (block size 1024)
        # but want to use a smaller block size for some smaller, simpler model
        assert block_size <= self.config.block_size
        self.config.block_size = block_size
        self.transformer.wpe.weight = nn.Parameter(self.transformer.wpe.weight[:block_size])
        for block in self.transformer.h:
            if hasattr(block.attn, 'bias'):
                block.attn.bias = block.attn.bias[:,:,:block_size,:block_size]

    @classmethod
    def from_pretrained(cls, model_type, override_args=None):
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        override_args = override_args or {} # default to empty dict
        # only dropout can be overridden see more notes below
        assert all(k == 'dropout' for k in override_args)
        from transformers import GPT2LMHeadModel
        print("loading weights from pretrained gpt: %s" % model_type)

        # n_layer, n_head and n_embd are determined from model_type
        config_args = {
            'gpt2':         dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
            'gpt2-medium':  dict(n_layer=24, n_head=16, n_embd=1024), # 350M params
            'gpt2-large':   dict(n_layer=36, n_head=20, n_embd=1280), # 774M params
            'gpt2-xl':      dict(n_layer=48, n_head=25, n_embd=1600), # 1558M params
        }[model_type]
        print("forcing vocab_size=50257, block_size=1024, bias=True")
        config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GPT model checkpoints
        config_args['bias'] = True # always True for GPT model checkpoints
        # we can override the dropout rate, if desired
        if 'dropout' in override_args:
            print(f"overriding dropout rate to {override_args['dropout']}")
            config_args['dropout'] = override_args['dropout']
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer, not a param

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in names and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignore these, just a buffer
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # same, just the mask (buffer)
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                # special treatment for the Conv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model

    def configure_optimizers(self, weight_decay, learning_rate, betas, device_type):
        # start with all of the candidate parameters
        param_dict = {pn: p for pn, p in self.named_parameters()}
        # filter out those that do not require grad
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        # create optim groups. Any parameters that is 2D will be weight decayed, otherwise no.
        # i.e. all weight tensors in matmuls + embeddings decay, all biases and layernorms don't.
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
        print(f"num non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")
        # Create AdamW optimizer and use the fused version if it is available
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and device_type == 'cuda'
        extra_args = dict(fused=True) if use_fused else dict()
        optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=betas, **extra_args)
        print(f"using fused AdamW: {use_fused}")

        return optimizer

    def estimate_mfu(self, fwdbwd_per_iter, dt):
        """ estimate model flops utilization (MFU) in units of A100 bfloat16 peak FLOPS """
        # first estimate the number of flops we do per iteration.
        # see PaLM paper Appendix B as ref: https://arxiv.org/abs/2204.02311
        N = self.get_num_params()
        cfg = self.config
        L, H, Q, T = cfg.n_layer, cfg.n_head, cfg.n_embd//cfg.n_head, cfg.block_size
        flops_per_token = 6*N + 12*L*H*Q*T
        flops_per_fwdbwd = flops_per_token * T
        flops_per_iter = flops_per_fwdbwd * fwdbwd_per_iter
        # express our flops throughput as ratio of A100 bfloat16 peak flops
        flops_achieved = flops_per_iter * (1.0/dt) # per second
        flops_promised = 312e12 # A100 GPU bfloat16 peak flops is 312 TFLOPS
        mfu = flops_achieved / flops_promised
        return mfu

    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        Most likely you'll want to make sure to be in model.eval() mode of operation for this.
        """
        for _ in range(max_new_tokens):
            # if the sequence context is growing too long we must crop it at block_size
            idx_cond = idx if idx.size(1) <= self.config.block_size else idx[:, -self.config.block_size:]
            # forward the model to get the logits for the index in the sequence
            logits, _ = self(idx_cond)
            # pluck the logits at the final step and scale by desired temperature
            logits = logits[:, -1, :] / temperature
            # optionally crop the logits to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')
            # apply softmax to convert logits to (normalized) probabilities
            probs = F.softmax(logits, dim=-1)
            # sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)
            # append sampled index to the running sequence and continue
            idx = torch.cat((idx, idx_next), dim=1)

        return idx
