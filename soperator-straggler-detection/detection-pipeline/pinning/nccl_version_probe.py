#!/usr/bin/env python3
"""Independent runtime NCCL version probe: loads libnccl the same way a rank would and
calls ncclGetVersion() directly, plus reports which libnccl file is mapped into the process."""
import ctypes, os, sys
import torch
v = ctypes.c_int(0)
lib = ctypes.CDLL(os.environ.get("PROBE_LIB", "libnccl.so.2"))
lib.ncclGetVersion(ctypes.byref(v))
n = v.value
maps = sorted({l.split()[-1] for l in open("/proc/self/maps") if "libnccl" in l})
print(f"ncclGetVersion()={n // 10000}.{(n % 10000) // 100}.{n % 100}  torch.cuda.nccl.version()={torch.cuda.nccl.version()}  mapped={maps}")
