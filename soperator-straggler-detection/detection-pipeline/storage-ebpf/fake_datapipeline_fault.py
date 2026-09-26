#!/usr/bin/env python3
"""P20j Step 4b -- fake data-pipeline-only slowdown: a plain sleep() loop,
NO real file I/O at all. This is the negative control -- the eBPF agent
must show ZERO io-wait attributed to this process's pid, since it never
issues a single real disk read."""
import os
import time

if __name__ == "__main__":
    print(f"pid={os.getpid()} comm=fake_datapipeline_fault", flush=True)
    for i in range(8):
        time.sleep(1.5)
        print(f"fake stall iter {i}", flush=True)
