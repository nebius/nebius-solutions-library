#!/usr/bin/env python3
"""Combine bench_all_gpus.py (per-node JSON) + nvml_counters.sh (per-node CSV)
into one report per GPU, flagging >10% TFLOPS deviation from node median."""
import json, sys, statistics as st

def load(node_label, bench_json_path, nvml_csv_path):
    bench = json.load(open(bench_json_path))
    nvml = {}
    for line in open(nvml_csv_path):
        line = line.strip()
        if not line: continue
        parts = line.split(",")
        i, temp, power, appclk, maxclk, sw_t, hw_t, hw_p, sw_p = parts
        nvml[int(i)] = dict(temp=float(temp), power=float(power), appclk=float(appclk),
                             maxclk=float(maxclk), sw_thermal_s=int(sw_t)/1e6, hw_thermal_s=int(hw_t)/1e6,
                             hw_power_s=int(hw_p)/1e6, sw_power_s=int(sw_p)/1e6)
    tflops_vals = [bench[str(i)]["tflops"] for i in range(8)]
    med = st.median(tflops_vals)
    print(f"\n=== {node_label} (median TFLOPS = {med:.1f}) ===")
    for i in range(8):
        b = bench[str(i)]
        n = nvml[i]
        dev_pct = (b["tflops"] - med) / med * 100
        flag = "  <<< FLAG (>10% below median)" if dev_pct < -10 else ""
        print(f"GPU{i}: {b['tflops']:6.1f} TFLOPS ({dev_pct:+5.1f}%)  sleep_cal={b['cycles_per_ms']:>9d} cyc/ms  "
              f"temp={n['temp']:.0f}C power={n['power']:.1f}W appclk={n['appclk']:.0f}MHz maxclk={n['maxclk']:.0f}MHz  "
              f"sw_thermal={n['sw_thermal_s']:.1f}s hw_thermal={n['hw_thermal_s']:.1f}s hw_power={n['hw_power_s']:.1f}s{flag}")

if __name__ == "__main__":
    load("worker-0", sys.argv[1], sys.argv[2])
    load("worker-1", sys.argv[3], sys.argv[4])
