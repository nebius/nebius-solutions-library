#!/usr/bin/env python3
"""1 Hz per-CPU busy% sampler from /proc/stat (host view). Usage: cpu_sampler.py OUT.jsonl"""
import json, sys, time
def snap():
    d = {}
    for l in open("/proc/stat"):
        if l.startswith("cpu") and l[3].isdigit():
            f = l.split(); v = list(map(int, f[1:9])); d[int(f[0][3:])] = (sum(v) - v[3] - v[4], sum(v))  # busy excl idle+iowait
    return d
prev = snap(); out = open(sys.argv[1], "w")
while True:
    time.sleep(1); cur = snap()
    row = {str(c): round(100 * (cur[c][0] - prev[c][0]) / max(1, cur[c][1] - prev[c][1]), 1) for c in cur}
    out.write(json.dumps({"t": time.time(), "busy": row}) + "\n"); out.flush(); prev = cur
