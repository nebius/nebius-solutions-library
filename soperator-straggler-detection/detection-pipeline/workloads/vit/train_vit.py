import os
import time
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import torchvision.models as models

def main():
    torch.backends.cudnn.enabled = False
    torch.backends.cuda.matmul.allow_tf32 = True
    dist.init_process_group(backend="nccl")
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)

    max_iters = int(os.environ.get("MAX_ITERS", "300"))
    batch_size = int(os.environ.get("BATCH_SIZE", "16"))
    log_interval = int(os.environ.get("LOG_INTERVAL", "10"))
    image_size = int(os.environ.get("IMAGE_SIZE", "224"))

    # ViT-B/16 -- real torchvision transformer encoder (patch embed + 12
    # transformer encoder blocks, real multi-head self-attention), not a
    # from-scratch approximation. num_classes=1000 (ImageNet-shaped head,
    # matching ResNet's own synthetic-data convention in this project).
    model = models.vit_b_16(weights=None, num_classes=1000).to(device)
    model = DDP(model, device_ids=[local_rank])
    opt = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    criterion = nn.CrossEntropyLoss()

    # P27-hotfix -- same STRAGGLER_SLEEP_MS/STRAGGLER_TARGET_RANKS
    # convention as train_resnet.py's own P27-hotfix (mirroring
    # nanoGPT_tp/train.py's proven placement) -- a real time.sleep() on
    # the target rank(s), gated on model.require_backward_grad_sync,
    # placed directly before loss.backward(). Replaces this workload's
    # prior sole reliance on nvidia-smi -lgc, confirmed this session to
    # produce no measurable effect here (ratio 1.064, within noise).
    STRAGGLER_SLEEP_S = float(os.environ.get("STRAGGLER_SLEEP_MS", "0")) / 1000.0
    STRAGGLER_TARGET_RANKS = {int(r) for r in os.environ.get("STRAGGLER_TARGET_RANKS", "").split(",") if r.strip() != ""}
    STRAGGLER_IS_TARGET = rank in STRAGGLER_TARGET_RANKS

    torch.manual_seed(1234 + rank)
    for it in range(max_iters):
        x = torch.randn(batch_size, 3, image_size, image_size, device=device)
        y = torch.randint(0, 1000, (batch_size,), device=device)
        t0 = time.time()
        opt.zero_grad()
        out = model(x)
        loss = criterion(out, y)
        if STRAGGLER_IS_TARGET and model.require_backward_grad_sync and STRAGGLER_SLEEP_S > 0:
            time.sleep(STRAGGLER_SLEEP_S)
        loss.backward()
        opt.step()
        torch.cuda.synchronize()
        dt = time.time() - t0
        if rank == 0 and it % log_interval == 0:
            print(f"iter {it}: loss {loss.item():.4f}, time {dt*1000:.2f}ms", flush=True)

    if rank == 0:
        print(f"FINAL_LOSS: {loss.item():.4f}", flush=True)
    dist.barrier()
    dist.destroy_process_group()

if __name__ == "__main__":
    main()
