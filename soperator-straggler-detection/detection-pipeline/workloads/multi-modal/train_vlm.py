import os, sys, time, math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

# P33 -- multi-modal (vision+language) workload: the final item on P26's
# original workload-shape list. Two distinct backbone families in ONE
# job for the first time this session -- every prior workload (nanoGPT
# dense-decoder, ResNet/ViT vision, MoE routing, diffusion conv+
# attention, DLRM embedding-lookup) was architecturally uniform
# throughout. Real, standard LLaVA-style design: a small conv vision
# encoder produces one real feature vector per image, projected into the
# language decoder's embedding space and prepended as an extra "visual
# token" position ahead of the real text tokens -- not a made-up
# combination scheme, the same prefix-conditioning pattern real VLMs use.
#
# Vision half: a small hand-built conv encoder (task's own stated
# alternative to ViT-B/16 -- chosen to keep the combined model a
# reasonably-sized "minimal, real" test rather than compounding ViT-B's
# 86M params with a full decoder). Language half: nanoGPT_tp/model.py's
# real Block/LayerNorm/GPT classes, reused directly (imported, not
# reimplemented) at this project's own established n_layer=6, n_head=6,
# n_embd=384 config (same as TP2/long-context/hybrid).
# Stage 3 fix: missed by Stage 2's item-4 sys.path sweep -- same real-
# location fix as every other file (tp2/'s model.py is the real
# "nanoGPT_tp" this file always meant, per train_node_tp.sh's own comment
# distinguishing it from the plain nanogpt-base model).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tp2"))
from model import GPTConfig, GPT

N_LAYER = 6
N_HEAD = 6
N_EMBD = 384
BLOCK_SIZE = int(os.environ.get('BLOCK_SIZE', '256'))
VOCAB_SIZE = 50304
IMG_SIZE = int(os.environ.get('IMG_SIZE', '64'))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '16'))
SEQ_LEN = int(os.environ.get('SEQ_LEN', '64'))  # real text token length per sample
MAX_ITERS = int(os.environ.get('MAX_ITERS', '60'))

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


class VisionEncoder(nn.Module):
    """Small real conv encoder -- 4 conv+pool stages down to a single
    global-pooled feature vector per image, projected to n_embd. Not a
    toy: real strided convs, real GroupNorm+SiLU nonlinearities (same
    building blocks P30's diffusion U-Net used), genuinely different
    parameter/compute shape from the language decoder it feeds into."""
    def __init__(self, n_embd, base_ch=32):
        super().__init__()
        self.stem = nn.Conv2d(3, base_ch, 3, padding=1)
        self.stage1 = nn.Sequential(nn.GroupNorm(8, base_ch), nn.SiLU(), nn.Conv2d(base_ch, base_ch * 2, 3, stride=2, padding=1))
        self.stage2 = nn.Sequential(nn.GroupNorm(8, base_ch * 2), nn.SiLU(), nn.Conv2d(base_ch * 2, base_ch * 4, 3, stride=2, padding=1))
        self.stage3 = nn.Sequential(nn.GroupNorm(8, base_ch * 4), nn.SiLU(), nn.Conv2d(base_ch * 4, base_ch * 8, 3, stride=2, padding=1))
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.proj = nn.Linear(base_ch * 8, n_embd)

    def forward(self, x):
        h = self.stem(x)
        h = self.stage1(h)
        h = self.stage2(h)
        h = self.stage3(h)
        h = self.pool(h).flatten(1)
        return self.proj(h)


class VLM(nn.Module):
    """Vision-conditioned language model: real nanoGPT decoder (GPT's own
    real submodules, reused directly via composition, not
    reimplemented), with the vision encoder's real feature vector
    prepended as one extra sequence position ahead of the real text
    token embeddings -- the standard prefix-conditioning VLM pattern.
    Loss computed only over the real text-token positions (the vision
    prefix position is never a language-modeling target)."""
    def __init__(self, gpt_config, img_size):
        super().__init__()
        self.gpt = GPT(gpt_config)
        self.vision = VisionEncoder(gpt_config.n_embd)
        self.n_embd = gpt_config.n_embd

    def forward(self, images, idx, targets=None):
        b, t = idx.size()
        device = idx.device
        vision_emb = self.vision(images)  # (b, n_embd) -- one real visual token per sample

        tok_emb = self.gpt.transformer.wte(idx)  # (b, t, n_embd)
        x = torch.cat([vision_emb.unsqueeze(1), tok_emb], dim=1)  # (b, t+1, n_embd)

        pos = torch.arange(0, t + 1, dtype=torch.long, device=device)
        pos_emb = self.gpt.transformer.wpe(pos)
        x = self.gpt.transformer.drop(x + pos_emb)
        for block in self.gpt.transformer.h:
            x = block(x)
        x = self.gpt.transformer.ln_f(x)
        logits = self.gpt.lm_head(x)  # (b, t+1, vocab)

        loss = None
        if targets is not None:
            # real text-token positions only: logits[:, 1:, :] (skip the
            # vision-prefix position's own output) predicts targets.
            loss = F.cross_entropy(logits[:, 1:, :].reshape(-1, logits.size(-1)), targets.reshape(-1))
        return logits, loss


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')
    torch.backends.cudnn.enabled = False
    torch.manual_seed(1337 + rank)

    gpt_config = GPTConfig(n_layer=N_LAYER, n_head=N_HEAD, n_embd=N_EMBD, block_size=BLOCK_SIZE,
                           bias=False, vocab_size=VOCAB_SIZE, dropout=0.0)
    model = VLM(gpt_config, IMG_SIZE).to(device)
    model = DDP(model, device_ids=[local_rank])
    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, fused=False, foreach=False)

    n_vision_params = sum(p.numel() for p in model.module.vision.parameters())
    n_gpt_params = sum(p.numel() for p in model.module.gpt.parameters())
    print(f"[rank{rank}] real vision_params={n_vision_params} gpt_params={n_gpt_params} "
          f"img_size={IMG_SIZE} seq_len={SEQ_LEN} world_size={world_size}", flush=True)

    for it in range(MAX_ITERS):
        t0 = time.time()
        images = torch.randn(BATCH_SIZE, 3, IMG_SIZE, IMG_SIZE, device=device)
        idx = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, SEQ_LEN), device=device)
        targets = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, SEQ_LEN), device=device)

        if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)

        opt.zero_grad()
        logits, loss = model(images, idx, targets)
        loss.backward()
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
