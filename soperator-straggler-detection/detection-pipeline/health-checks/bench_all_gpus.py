import torch, time, json, sys

results = {}
for dev in range(8):
    torch.cuda.set_device(dev)
    a = torch.randn(8192, 8192, device=f'cuda:{dev}', dtype=torch.float16)
    b = torch.randn(8192, 8192, device=f'cuda:{dev}', dtype=torch.float16)
    torch.cuda.synchronize()
    for _ in range(5):
        c = a @ b
    torch.cuda.synchronize()
    N = 30
    t0 = time.time()
    for _ in range(N):
        c = a @ b
    torch.cuda.synchronize()
    t1 = time.time()
    per_matmul_ms = (t1 - t0) * 1000.0 / N
    flops = 2 * 8192**3
    tflops = flops / (per_matmul_ms / 1000.0) / 1e12

    torch.cuda.synchronize()
    cal_cycles = 800000
    for _ in range(4):
        ct0 = time.time()
        torch.cuda._sleep(cal_cycles)
        torch.cuda.synchronize()
        got_ms = (time.time() - ct0) * 1000.0
        if got_ms > 0.0001:
            cal_cycles = int(cal_cycles * (1.0 / got_ms))

    del a, b, c
    torch.cuda.empty_cache()
    results[dev] = {"tflops": tflops, "cycles_per_ms": cal_cycles}

print(json.dumps(results))
