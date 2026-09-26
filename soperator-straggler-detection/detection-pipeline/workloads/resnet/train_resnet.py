import os
import time
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import torchvision.models as models

def main():
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
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
    image_size = int(os.environ.get("IMAGE_SIZE", "224"))  # P18j Stage 4:
    # synthetic third overlap-ratio point -- shrinking spatial size cuts
    # Conv2d compute cost roughly quadratically while leaving the
    # gradient-bucket AllReduce sizes (a property of parameter count, not
    # input size) unchanged, pushing R = burst/gap higher without
    # touching the model architecture.

    model = models.resnet18(weights=None, num_classes=1000).to(device)
    model = DDP(model, device_ids=[local_rank])
    opt = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    criterion = nn.CrossEntropyLoss()

    # in-process jitter injection, ported from P4b_jitter/nanogpt_fixed --
    # a calibrated torch.cuda._sleep() burst on the target rank's own
    # CUDA stream, at a configurable period.
    INJECT_ENABLE = int(os.environ.get("INJECT_ENABLE", "0"))
    INJECT_RANK = int(os.environ.get("INJECT_RANK", "4"))
    INJECT_BURST_MS = float(os.environ.get("INJECT_BURST_MS", "10.0"))
    INJECT_PERIOD_MS = float(os.environ.get("INJECT_PERIOD_MS", "100.0"))
    inject_active = bool(INJECT_ENABLE and (rank == INJECT_RANK))
    inject_cycles, inject_next_time = 0, 0.0
    if inject_active:
        torch.cuda.synchronize()
        cal_cycles = 800000
        for _ in range(4):
            _t0 = time.time()
            torch.cuda._sleep(cal_cycles)
            torch.cuda.synchronize()
            _got_ms = (time.time() - _t0) * 1000.0
            if _got_ms > 0.0001:
                cal_cycles = int(cal_cycles * (1.0 / _got_ms))
        inject_cycles = max(1, int(cal_cycles * INJECT_BURST_MS))
        inject_next_time = time.time()
        print(f"[inject] rank={rank} ENABLED burst={INJECT_BURST_MS}ms period={INJECT_PERIOD_MS}ms "
              f"cycles={inject_cycles}", flush=True)

    torch.manual_seed(1234 + rank)
    for it in range(max_iters):
        x = torch.randn(batch_size, 3, image_size, image_size, device=device)
        y = torch.randint(0, 1000, (batch_size,), device=device)
        t0 = time.time()
        if inject_active:
            _now = time.time()
            if _now >= inject_next_time:
                torch.cuda._sleep(inject_cycles)
                torch.cuda.synchronize()
                inject_next_time = time.time() + INJECT_PERIOD_MS / 1000.0
        opt.zero_grad()
        out = model(x)
        loss = criterion(out, y)
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
