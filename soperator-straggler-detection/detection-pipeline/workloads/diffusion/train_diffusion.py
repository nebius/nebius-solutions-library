import os, math, time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

# P30 -- diffusion/image-generation: the most structurally different
# workload since MoE. Hand-built (no diffusers -- confirmed not
# installed on this stack this session; torch/torchvision are, and this
# project's own established convention throughout has been a hand-built,
# zero-external-ML-dependency toy model for every workload so far, not
# an installed library). A compact conv+self-attention U-Net, standard
# DDPM training objective (predict the real added noise, MSE loss) --
# genuinely different compute/communication profile from every prior
# workload: conv layers (not just Linear/attention), a mixed parameter
# distribution (conv kernels + one self-attention block + a timestep-
# embedding MLP), and plain DDP (not TP/PP/hybrid) as the communication
# pattern -- deliberately NOT assumed to be "DDP-shaped like ResNet/ViT"
# without confirming: this project's own DDP AllReduce comm count/
# composition is discovered live, the same way every other workload's
# real structure has been confirmed rather than assumed.
IMG_SIZE = int(os.environ.get('IMG_SIZE', '64'))
BASE_CH = int(os.environ.get('BASE_CH', '64'))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '16'))
MAX_ITERS = int(os.environ.get('MAX_ITERS', '60'))
T_STEPS = 1000  # real DDPM diffusion step count (standard convention)

STRAGGLER_SLEEP_S = float(os.environ.get('STRAGGLER_SLEEP_MS', '0')) / 1000.0
STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get('STRAGGLER_TARGET_RANKS', '').split(',') if r.strip() != ''}


def timestep_embedding(t, dim):
    """Standard sinusoidal timestep embedding (real DDPM convention)."""
    half = dim // 2
    freqs = torch.exp(-math.log(10000) * torch.arange(half, device=t.device).float() / half)
    args = t[:, None].float() * freqs[None]
    return torch.cat([torch.cos(args), torch.sin(args)], dim=-1)


class ResBlock(nn.Module):
    """Conv + GroupNorm + SiLU, timestep-conditioned -- the real, standard
    DDPM residual block, genuinely different parameter shape from every
    prior workload's Linear-dominated blocks."""
    def __init__(self, in_ch, out_ch, temb_dim):
        super().__init__()
        self.norm1 = nn.GroupNorm(8, in_ch)
        self.conv1 = nn.Conv2d(in_ch, out_ch, 3, padding=1)
        self.temb_proj = nn.Linear(temb_dim, out_ch)
        self.norm2 = nn.GroupNorm(8, out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, 3, padding=1)
        self.skip = nn.Conv2d(in_ch, out_ch, 1) if in_ch != out_ch else nn.Identity()

    def forward(self, x, temb):
        h = self.conv1(F.silu(self.norm1(x)))
        h = h + self.temb_proj(temb)[:, :, None, None]
        h = self.conv2(F.silu(self.norm2(h)))
        return h + self.skip(x)


class SelfAttention2d(nn.Module):
    """Real self-attention over spatial positions at the bottleneck --
    the conv+attention HYBRID this task explicitly flags as a genuinely
    new parameter/compute distribution vs. every prior workload."""
    def __init__(self, ch):
        super().__init__()
        self.norm = nn.GroupNorm(8, ch)
        self.qkv = nn.Conv2d(ch, ch * 3, 1)
        self.proj = nn.Conv2d(ch, ch, 1)

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.qkv(self.norm(x)).reshape(b, 3, c, h * w).permute(1, 0, 3, 2)
        q, k, v = qkv[0], qkv[1], qkv[2]
        attn = torch.softmax((q @ k.transpose(-2, -1)) / math.sqrt(c), dim=-1)
        out = (attn @ v).permute(0, 2, 1).reshape(b, c, h, w)
        return x + self.proj(out)


class UNet(nn.Module):
    def __init__(self, base_ch=64):
        super().__init__()
        temb_dim = base_ch * 4
        self.temb_mlp = nn.Sequential(nn.Linear(base_ch, temb_dim), nn.SiLU(), nn.Linear(temb_dim, temb_dim))
        self.base_ch = base_ch

        self.in_conv = nn.Conv2d(3, base_ch, 3, padding=1)
        self.down1 = ResBlock(base_ch, base_ch, temb_dim)
        self.down_pool1 = nn.Conv2d(base_ch, base_ch * 2, 3, stride=2, padding=1)
        self.down2 = ResBlock(base_ch * 2, base_ch * 2, temb_dim)
        self.down_pool2 = nn.Conv2d(base_ch * 2, base_ch * 4, 3, stride=2, padding=1)

        self.mid1 = ResBlock(base_ch * 4, base_ch * 4, temb_dim)
        self.mid_attn = SelfAttention2d(base_ch * 4)
        self.mid2 = ResBlock(base_ch * 4, base_ch * 4, temb_dim)

        self.up_conv2 = nn.ConvTranspose2d(base_ch * 4, base_ch * 2, 4, stride=2, padding=1)
        self.up2 = ResBlock(base_ch * 4, base_ch * 2, temb_dim)
        self.up_conv1 = nn.ConvTranspose2d(base_ch * 2, base_ch, 4, stride=2, padding=1)
        self.up1 = ResBlock(base_ch * 2, base_ch, temb_dim)

        self.out_norm = nn.GroupNorm(8, base_ch)
        self.out_conv = nn.Conv2d(base_ch, 3, 3, padding=1)

    def forward(self, x, t):
        temb = self.temb_mlp(timestep_embedding(t, self.base_ch))
        h0 = self.in_conv(x)
        h1 = self.down1(h0, temb)
        h2 = self.down_pool1(h1)
        h2 = self.down2(h2, temb)
        h3 = self.down_pool2(h2)

        h3 = self.mid1(h3, temb)
        h3 = self.mid_attn(h3)
        h3 = self.mid2(h3, temb)

        u2 = self.up_conv2(h3)
        u2 = self.up2(torch.cat([u2, h2], dim=1), temb)
        u1 = self.up_conv1(u2)
        u1 = self.up1(torch.cat([u1, h1], dim=1), temb)

        return self.out_conv(F.silu(self.out_norm(u1)))


def main():
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)
    dist.init_process_group('nccl')
    torch.backends.cudnn.enabled = False

    torch.manual_seed(1337)
    model = UNet(BASE_CH).to(device)
    model = DDP(model, device_ids=[local_rank])
    opt = torch.optim.AdamW(model.parameters(), lr=2e-4)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[rank{rank}] real params: {n_params} img_size={IMG_SIZE} base_ch={BASE_CH} "
          f"world_size={world_size}", flush=True)

    # real, standard linear beta schedule (DDPM convention)
    betas = torch.linspace(1e-4, 0.02, T_STEPS, device=device)
    alphas_cumprod = torch.cumprod(1.0 - betas, dim=0)

    for it in range(MAX_ITERS):
        t0 = time.time()
        x0 = torch.randn(BATCH_SIZE, 3, IMG_SIZE, IMG_SIZE, device=device)
        t = torch.randint(0, T_STEPS, (BATCH_SIZE,), device=device)
        noise = torch.randn_like(x0)
        sqrt_ac = alphas_cumprod[t].sqrt()[:, None, None, None]
        sqrt_1mac = (1 - alphas_cumprod[t]).sqrt()[:, None, None, None]
        x_noisy = sqrt_ac * x0 + sqrt_1mac * noise

        if rank in STRAGGLER_TARGET_RANKS and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)

        opt.zero_grad()
        pred_noise = model(x_noisy, t)
        loss = F.mse_loss(pred_noise, noise)
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
