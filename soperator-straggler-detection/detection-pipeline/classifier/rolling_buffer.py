#!/usr/bin/env python3
"""P18c Stage 1: continuous rolling-history sampler for per-rank cause
metrics, replacing the one-shot post-hoc query that reads idle state when
queried after a transient fault has already ended.

Root problem (P18b, hit twice independently -- a cold-start test and a
sustained-fault retest): power and SM clock REVERT to idle within seconds
of the workload stopping (or, for cold-start, may never have been sampled
during the only window that mattered). A one-shot query issued after
detection/scoring completes can land on either side of that reversion by
pure timing luck. The thermal-slowdown counter (Path A) does NOT have
this problem -- it's cumulative/monotonic, so a post-hoc query already
sees the fault's full contribution regardless of when it's queried. This
fix therefore only matters for Path B (clock+power) and Path C (host CPU
load, which also reverts once the contention source stops).

Design: sample sm_clock + power_usage (cheap: one dcgmi dmon call, all 8
GPUs, per host) continuously at a fixed interval from BEFORE job launch
until AFTER it completes, independent of whether any detection has fired.
Host CPU load is sampled the same way. When a detection later fires on a
rank, look back into THAT rank's recorded history over the fault's
wall-clock window (derived from the dump's own coll_start_ts fields) --
not at whatever the live state happens to be at query time.
"""
import json
import time
import threading
import subprocess
import sys

from cause_metrics import query_dcgm_all_gpus, query_host_cpu, ssh, NETWORK_ERROR_FIELDS, discover_ib_devices

_IB_COUNTER_FIELDS = ["port_xmit_data", "port_rcv_errors", "symbol_error", "link_error_recovery",
                      "port_xmit_discards", "port_xmit_wait", "VL15_dropped"]

# Cluster-topology-agnostic fix (this session): _IB_DEVICES used to be a
# module-level hardcoded [mlx5_0..mlx5_7] list. This sampler's own
# _sample_host_ib runs on every tick of a continuous, whole-job-duration
# sampling loop (sample_once -> run_sampler), so real per-host discovery
# (cause_metrics.discover_ib_devices, a single `ls`) is cached here,
# per host, on first use -- real devices, discovered once, not
# re-discovered every ~1s tick for a job that could run for hours.
_ib_devices_cache = {}


def _query_ib_all_devices_batched(host):
    """Same data as cause_metrics.query_ib_all_devices, but ONE ssh call
    covering all real devices instead of one sequential round-trip per
    device (~0.6s each -- measured 5.2s/cycle for a naive per-device loop,
    which broke the ~1Hz sampling cadence the rest of the buffer holds).
    Needed only for the sampler's tight loop; cause_metrics's original
    per-device version is unchanged and still used by the one-shot
    live-query path."""
    if host not in _ib_devices_cache:
        _ib_devices_cache[host] = discover_ib_devices(host)
    devices = _ib_devices_cache[host]
    if not devices:
        return None
    cmd_parts = []
    for dev in devices:
        for c in _IB_COUNTER_FIELDS:
            cmd_parts.append(f"echo {dev}:{c}=$(cat /sys-host/class/infiniband/{dev}/ports/1/counters/{c} 2>/dev/null || echo NA)")
    out, err, rc = ssh(host, " ; ".join(cmd_parts))
    if "sys-host" in err or (rc != 0 and not out):
        return None
    results = {dev: {} for dev in devices}
    for line in out.splitlines():
        if "=" not in line or ":" not in line:
            continue
        devfield, v = line.split("=", 1)
        dev, field = devfield.split(":", 1)
        if dev in results:
            results[dev][field] = None if v == "NA" else int(v)
    return results


def _sample_host_dcgm(host, t, rows, lock):
    dcgm, err = query_dcgm_all_gpus(host, fields=("sm_clock", "power_usage"))
    if err:
        return
    for gpu_idx, vals in dcgm.items():
        try:
            row = {"t": t, "host": host, "gpu": gpu_idx,
                   "sm_clock": float(vals.get("sm_clock", "nan")),
                   "power": float(vals.get("power_usage", "nan"))}
        except (TypeError, ValueError):
            continue
        with lock:
            rows.append(row)


def _sample_host_cpu(host, t, rows, lock):
    cpu, err = query_host_cpu(host)
    if err:
        return
    with lock:
        rows.append({"t": t, "host": host, "gpu": None, "load_per_core": cpu["load_per_core"]})


def _sample_host_ib(host, t, rows, lock):
    """P18d Stage 1: extends the buffer to IB counters, closing the same
    post-hoc timing gap already fixed for Path B/C -- check_global_drift's
    network corroboration was still a live query, which reads whatever
    the cluster looks like AT CLASSIFY()-CALL TIME (idle, if the job has
    already finished), not what it looked like during the fault. Records
    RAW cumulative counters per device; deltas are computed at lookback
    time (query_ib_window), same two-point-difference math as the old
    live two-snapshot query, just against buffered history instead of a
    fresh poll."""
    devs = _query_ib_all_devices_batched(host)
    if not devs:
        return
    with lock:
        for dev, vals in devs.items():
            if vals is None:
                continue
            rows.append({"t": t, "host": host, "gpu": None, "ib_dev": dev,
                         "xmit": vals.get("port_xmit_data"),
                         "errors": sum((vals.get(f) or 0) for f in NETWORK_ERROR_FIELDS),
                         "congestion": vals.get("port_xmit_wait")})


def sample_once(hosts):
    """One sampling round across all hosts, run CONCURRENTLY (threads) so
    N hosts' SSH round-trips overlap instead of stacking sequentially --
    measured ~0.63s per single-host dcgmi call, which would make a naive
    sequential 2-host loop take ~1.3s/cycle against a 1s target interval."""
    t = time.time()
    rows = []
    lock = threading.Lock()
    threads = []
    for h in hosts:
        threads.append(threading.Thread(target=_sample_host_dcgm, args=(h, t, rows, lock)))
        threads.append(threading.Thread(target=_sample_host_cpu, args=(h, t, rows, lock)))
        threads.append(threading.Thread(target=_sample_host_ib, args=(h, t, rows, lock)))
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return rows


def run_sampler(hosts, outfile, interval=1.0, duration=None, stop_flag_path=None):
    start = time.time()
    with open(outfile, "a") as f:
        while True:
            if duration is not None and time.time() - start >= duration:
                break
            if stop_flag_path:
                try:
                    with open(stop_flag_path) as sf:
                        if sf.read().strip() == "stop":
                            break
                except FileNotFoundError:
                    pass
            cycle_start = time.time()
            for row in sample_once(hosts):
                f.write(json.dumps(row) + "\n")
            f.flush()
            elapsed = time.time() - cycle_start
            time.sleep(max(0.0, interval - elapsed))


def load_buffer(outfile):
    """Returns {(host, gpu): [(t, sm_clock, power), ...]} for GPU rows,
    {host: [(t, load_per_core), ...]} for host-CPU rows, and
    {(host, ib_dev): [(t, xmit, errors, congestion), ...]} for IB rows."""
    gpu_buf, host_buf, ib_buf = {}, {}, {}
    with open(outfile) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "ib_dev" in r:
                ib_buf.setdefault((r["host"], r["ib_dev"]), []).append(
                    (r["t"], r["xmit"], r["errors"], r["congestion"]))
            elif r.get("gpu") is None:
                host_buf.setdefault(r["host"], []).append((r["t"], r["load_per_core"]))
            else:
                gpu_buf.setdefault((r["host"], r["gpu"]), []).append((r["t"], r["sm_clock"], r["power"]))
    return gpu_buf, host_buf, ib_buf


def query_gpu_window(gpu_buf, host, gpu, t_start, t_end):
    rows = [r for r in gpu_buf.get((host, gpu), []) if t_start <= r[0] <= t_end]
    if not rows:
        return None
    powers = [r[2] for r in rows if r[2] == r[2]]
    clocks = [r[1] for r in rows if r[1] == r[1]]
    return {
        "n": len(rows),
        "max_power": max(powers) if powers else None,
        "mean_power": (sum(powers) / len(powers)) if powers else None,
        "min_sm_clock": min(clocks) if clocks else None,
        "median_sm_clock": sorted(clocks)[len(clocks) // 2] if clocks else None,
    }


def query_host_window(host_buf, host, t_start, t_end):
    rows = [r for r in host_buf.get(host, []) if t_start <= r[0] <= t_end]
    if not rows:
        return None
    loads = [r[1] for r in rows]
    return {"n": len(rows), "max_load_per_core": max(loads), "mean_load_per_core": sum(loads) / len(loads)}


def query_ib_window(ib_buf, hosts, t_start, t_end):
    """P18d Stage 1: same participation/throughput/error-delta computation
    query_network_snapshot did with a live two-snapshot poll, but against
    BUFFERED history within [t_start, t_end] -- the first and last sample
    recorded for each device in that window stand in for the old "before"
    and "after" live reads. Returns the same per-host shape
    query_network_snapshot did, so build_global_drift_finding needs no
    changes downstream."""
    result = {}
    for h in hosts:
        devs_in_window = {}
        for (host, dev), rows in ib_buf.items():
            if host != h:
                continue
            in_range = [r for r in rows if t_start <= r[0] <= t_end]
            if len(in_range) < 2:
                continue
            devs_in_window[dev] = (in_range[0], in_range[-1])
        if not devs_in_window:
            result[h] = None
            continue
        active, total = 0, 0
        xmit_delta_total, errors_delta, congestion_delta = 0, 0, 0
        dt = t_end - t_start
        for dev, (first, last) in devs_in_window.items():
            total += 1
            _, x0, e0, c0 = first
            _, x1, e1, c1 = last
            if x0 is None or x1 is None:
                continue
            xd = x1 - x0
            xmit_delta_total += max(0, xd)
            if xd > 0:
                active += 1
            if e0 is not None and e1 is not None:
                errors_delta += max(0, e1 - e0)
            if c0 is not None and c1 is not None:
                congestion_delta += max(0, c1 - c0)
        result[h] = {
            "active_devices": active, "total_devices": total,
            "participation_frac": (active / total) if total else None,
            "xmit_rate_bytes_s": xmit_delta_total / dt if dt > 0 else 0.0,
            "errors_delta": errors_delta, "congestion_delta": congestion_delta,
        }
    if all(v is None for v in result.values()):
        return None, "no IB samples recorded in this window (sys-host not mounted, or buffer doesn't cover this range)"
    return result, None


if __name__ == "__main__":
    hosts = sys.argv[1].split(",")
    outfile = sys.argv[2]
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    stop_flag = sys.argv[4] if len(sys.argv) > 4 else None
    run_sampler(hosts, outfile, interval, stop_flag_path=stop_flag)
