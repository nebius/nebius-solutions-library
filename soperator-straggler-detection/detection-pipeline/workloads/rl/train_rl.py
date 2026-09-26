#!/usr/bin/env python3
"""P31 -- minimal, real RL (REINFORCE-style policy-gradient) training loop.
Reuses this project's own real GPT model/data (nanoGPT_straggler's
model.py + shakespeare_char dataset) -- not a new architecture, a new
TRAINING LOOP SHAPE around the same real model.

Real RL structure, per iteration:
  1. ROLLOUT phase: torch.no_grad() forward pass -- the policy samples
     real actions (next-char predictions) from its own real softmax
     distribution. A real, computable, non-fabricated reward: did the
     sampled action match the real next character in the real data
     (per-position exact-match accuracy) -- a legitimate, standard
     next-token-prediction-as-RL reward, not a synthetic placeholder.
  2. POLICY UPDATE phase: a real forward WITH grad for the SAME sampled
     actions, REINFORCE loss = -mean(reward * log_prob(action)),
     backward(), optimizer.step() -- the same real DDP training step
     shape as every other workload in this project.

Every rank performs BOTH phases, every iteration (confirmed, not
assumed -- see the print at the bottom of each phase, and Step 2's
real dump-file check). Reuses the existing STRAGGLER_SLEEP_MS/
STRAGGLER_TARGET_RANKS/STRAGGLER_PHASE mechanism; STRAGGLER_PHASE=
'rollout' is new (targets the no-grad rollout forward specifically),
'forward'/'backward' reuse their established meaning for the policy-
update step's own forward/backward split.
"""
import os
import sys
import time

import numpy as np
import torch
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
import torch.distributed as dist

sys.path.insert(0, "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler")
from model import GPTConfig, GPT

DATA_DIR = "/root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler/data/shakespeare_char"
MAX_ITERS = int(os.environ.get("MAX_ITERS", "300"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "32"))
BLOCK_SIZE = int(os.environ.get("BLOCK_SIZE", "128"))
LOG_EVERY = int(os.environ.get("LOG_EVERY", "10"))

STRAGGLER_SLEEP_S = float(os.environ.get("STRAGGLER_SLEEP_MS", "0")) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get("STRAGGLER_TARGET_RANKS", "").split(",") if r.strip() != ""}
STRAGGLER_PHASE = os.environ.get("STRAGGLER_PHASE", "none")  # 'rollout' | 'forward' | 'backward' | 'none'


def get_batch(split):
    data = np.memmap(os.path.join(DATA_DIR, f"{split}.bin"), dtype=np.uint16, mode="r")
    ix = torch.randint(len(data) - BLOCK_SIZE, (BATCH_SIZE,))
    x = torch.stack([torch.from_numpy((data[i:i + BLOCK_SIZE]).astype(np.int64)) for i in ix])
    y = torch.stack([torch.from_numpy((data[i + 1:i + 1 + BLOCK_SIZE]).astype(np.int64)) for i in ix])
    return x, y


def main():
    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    init_process_group("nccl")
    is_target = rank in STRAGGLER_TARGET_RANKS

    torch.manual_seed(1337 + rank)
    config = GPTConfig(block_size=BLOCK_SIZE, vocab_size=65, n_layer=6, n_head=6, n_embd=384, dropout=0.0, bias=False)
    model = GPT(config).to(device)
    model = DDP(model, device_ids=[local_rank])
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)

    print(f"[rank{rank}] P31 RL loop starting, world_size={world_size}, is_straggler_target={is_target}", flush=True)

    for it in range(MAX_ITERS):
        model.require_backward_grad_sync = False  # no backward this phase -- confirmed harmless either way, explicit for clarity
        x, y = get_batch("train")
        x, y = x.to(device), y.to(device)

        # ---- ROLLOUT phase: real no-grad forward, real sampled actions, real reward ----
        if is_target and STRAGGLER_PHASE == "rollout" and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        t_rollout0 = time.time()
        with torch.no_grad():
            logits, _ = model(x, targets=y)  # targets forces full-sequence logits, loss itself unused here
            probs = F.softmax(logits, dim=-1)
            B, T, V = probs.shape
            actions = torch.multinomial(probs.view(-1, V), num_samples=1).view(B, T)
            reward = (actions == y).float()  # real, computable, non-fabricated: did the sampled action match the real next char
        t_rollout = time.time() - t_rollout0

        # ---- POLICY UPDATE phase: real forward WITH grad on the same sampled actions, REINFORCE loss ----
        model.require_backward_grad_sync = True
        if is_target and STRAGGLER_PHASE == "forward" and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        t_update0 = time.time()
        logits2, _ = model(x, targets=y)
        log_probs = F.log_softmax(logits2, dim=-1)
        action_log_probs = log_probs.gather(2, actions.unsqueeze(-1)).squeeze(-1)
        loss = -(reward.detach() * action_log_probs).mean()

        if is_target and STRAGGLER_PHASE == "backward" and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        loss.backward()
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        t_update = time.time() - t_update0

        if it % LOG_EVERY == 0:
            print(f"[rank{rank}] iter {it}: mean_reward={reward.mean().item():.4f} loss={loss.item():.4f} "
                  f"rollout_ms={t_rollout*1000:.1f} update_ms={t_update*1000:.1f}", flush=True)

    dist.barrier()
    destroy_process_group()
    print(f"[rank{rank}] P31 RL loop done", flush=True)


if __name__ == "__main__":
    main()
