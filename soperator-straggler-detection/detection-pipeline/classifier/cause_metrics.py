#!/usr/bin/env python3
"""Cause-metric collection: Class 1 (validated, may drive CONFIRMED) and
Class 2 (supporting evidence only, never drives a classification alone).

All queries run at the host/pod level via SSH (matching how GPU health has
been checked throughout this investigation), since dcgmi/nvidia-smi need
the host's driver stack and /sys-host needs an explicit container mount.
"""
import os
import subprocess
import json
import sys
import time
import concurrent.futures

# Stage 3 fix: query_matmul_tflops's own default path below was missed by
# Stage 2's item-4 sys.path/hardcoded-path sweep (that sweep fixed health_
# exclusions.py's degraded_gpus_live default the same way -- this is the
# same real script, same real fix, just a second hardcoded default for it
# that sweep didn't also catch).
_BENCH_SCRIPT_DEFAULT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "health-checks", "bench_all_gpus.py"))

# P27-hotfix-stall -- real, reproduced (n=2, confirmed via live gdb thread
# dumps) availability gap: every per-GPU/per-device/per-host query below
# used to issue its SSH round-trips SEQUENTIALLY (a plain `for`/dict-
# comprehension loop). Each individual call is genuinely fast when
# uncontended (~1.3-1.5s, confirmed via manual re-test during a live
# stall), but under real concurrent cluster load (confirmed correlating
# factor: a new training job's own container-pull/torchrun startup),
# each call slows down enough that 16-36 of them chained sequentially
# compound into a multi-minute total -- during which alert_engine.py's
# entire visible output went dark, twice, eating the persistence window
# a real fault needed to escalate to CONFIRMED/PAGE.
#
# _parallel_map is the direct fix for that root cause (sequential,
# unbounded-in-aggregate SSH calls), not a longer timeout papering over
# it: every item's ssh() round-trip fires concurrently via the same
# concurrency primitive alert_engine.py's own poll_once() already uses
# (concurrent.futures.ThreadPoolExecutor), so N calls cost ~1 call's
# latency instead of N calls' latency, with or without contention.
def _parallel_map(fn, items, max_workers=16):
    """Run fn(item) for every item in `items` concurrently, returning
    {item: fn(item)} -- items must be hashable and distinct (every
    caller here is a list of GPU ids, IB device names, or hostnames)."""
    items = list(items)
    if not items:
        return {}
    if len(items) == 1:
        return {items[0]: fn(items[0])}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        future_to_item = {ex.submit(fn, item): item for item in items}
        return {future_to_item[f]: f.result() for f in concurrent.futures.as_completed(future_to_item)}


# Real, enforced OVERALL deadline for the multi-phase nvlink/network
# checks (query_nvlink_snapshot/query_network_snapshot) -- separate from,
# and on top of, ssh()'s own per-call timeout. Parallelizing each phase
# (above) bounds each phase to ~1 call's latency, but a handful of
# sequential phases (link discovery -> errors snapshot 0 -> sleep ->
# errors snapshot 1 -> bandwidth) can still stack up if the cluster is
# under enough load that even single calls approach their own ~20s cap.
# This is the backstop: if the WHOLE check can't finish inside this
# budget, give up and report degraded/unavailable for this cycle --
# never block the caller indefinitely. 30s is a generous multiple of the
# ~1.5s uncontended baseline (comfortably covers real, non-pathological
# slowdown) while still being far short of the 60s NVLINK_CHECK_INTERVAL_S/
# NETWORK_CHECK_INTERVAL_S cadence these checks run on, so a degraded
# cycle doesn't visibly overlap the next one.
OVERALL_CHECK_DEADLINE_S = 30.0


def _with_overall_deadline(fn, deadline_s=OVERALL_CHECK_DEADLINE_S):
    """Runs fn() in its own thread with a real wall-clock ceiling. On
    timeout, returns (None, "<reason>") -- the same '(None, reason)'
    shape every other 'checked but unobtainable' path in this module
    already uses (e.g. query_network_snapshot's own sys-host-not-mounted
    case) -- never raises, never silently hangs the caller. The
    underlying thread is NOT forcibly killed (Python has no safe
    mechanism for that over a blocked subprocess) -- it's abandoned to
    finish or time out on its own via ssh()'s own per-call timeout; the
    caller only stops WAITING on it, which is the actual fix for
    "the whole pipeline blocks," not a claim that the orphaned SSH calls
    themselves vanish instantly.

    Deliberately NOT `with ThreadPoolExecutor(...) as ex:` -- caught live
    in this session's own testing: the context manager's __exit__ calls
    shutdown(wait=True), which blocks until the abandoned fn() thread
    actually finishes, silently reintroducing the exact multi-minute
    wait this function exists to bound (measured directly: a fn() that
    sleeps 100s made THIS function itself take the full 100s to return,
    even with deadline_s=2, because __exit__ was still waiting). shutdown
    (wait=False) in the finally block is the fix -- it stops accepting
    new work without blocking on work already in flight."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=deadline_s)
    except concurrent.futures.TimeoutError:
        return None, f"check exceeded its {deadline_s:.0f}s overall deadline -- degraded/not checked this cycle, not a claim of network/nvlink health"
    finally:
        ex.shutdown(wait=False)


DCGM_FIELDS = {
    "sm_clock": 100, "mem_clock": 101, "sm_max_clock": 113,
    "throttle_reasons": 112, "gpu_temp": 150, "mem_temp": 140,
    "power_usage": 155, "gpu_util": 203, "mem_copy_util": 204,
    "pcie_replay": 202, "pcie_link_gen": 237, "pcie_link_width": 238,
    "pcie_max_link_gen": 235, "pcie_max_link_width": 236,
    "xid_errors": 230,
    "ecc_sbe_vol": 310, "ecc_dbe_vol": 311,
    "retired_sbe": 390, "retired_dbe": 391, "retired_pending": 392,
    "nvlink_crc_total": 409, "nvlink_replay_total": 429 if False else 429,
    "nvlink_bandwidth_total": 449,
}

# NVML/DCGM current_clocks_event_reasons bitmask (subset relevant here)
THROTTLE_BITS = {
    0x0000000000000001: "gpu_idle",
    0x0000000000000002: "applications_clocks_setting",
    0x0000000000000004: "sw_power_cap",
    0x0000000000000008: "hw_slowdown",
    0x0000000000000010: "sync_boost",
    0x0000000000000020: "sw_thermal_slowdown",
    0x0000000000000040: "hw_thermal_slowdown",
    0x0000000000000080: "hw_power_brake_slowdown",
    0x0000000000000100: "display_clock_setting",
}


def decode_throttle(bitmask):
    reasons = [name for bit, name in THROTTLE_BITS.items() if bitmask & bit]
    return reasons or ["none"]


def ssh(host, cmd, timeout=15):
    """Real, confirmed gap this closes (constant-validation session,
    live 80-concurrent-SSH-connection stress test): subprocess.run's own
    timeout=timeout+5 bound was already real and enforced, but a call
    that actually HIT it raised subprocess.TimeoutExpired -- an
    exception, not a return value -- which every caller of this function
    was structurally unable to handle, since they all destructure a
    (stdout, stderr, returncode) TUPLE. Confirmed live: this propagated
    uncaught all the way through _parallel_map's f.result() and past
    _with_overall_deadline (which only catches concurrent.futures.
    TimeoutError, an unrelated exception class) -- on the nvlink/network
    path (raw background threads, wrapped by nothing) this was fully
    silent, swallowed by Python's default thread-exception hook; on the
    thermal path (reached via _check_mean/_check_cv, wrapped by _run_
    check) it surfaced only as a bare [CHECK-FAILED], with no honest
    reason recorded.

    Fixed at this one, lowest, most central point rather than in every
    caller: an exception here now becomes the exact same (stdout, stderr,
    returncode) shape a real nonzero-exit ssh already produces --
    returncode=1 (nonzero, so every existing `if rc != 0` guard already
    handles it correctly with zero caller changes), stderr carrying a
    real, honest, human-readable reason (used directly by callers that
    already report `err` as the failure reason, e.g. cause["impossible"].
    append(f"... on {host}: {err}")) -- never a silent failure, never an
    uncaught exception.

    Catches OSError too (e.g. the ssh binary itself missing or
    unspawnable) for the same reason, not just the one exception type
    this session's stress test happened to trigger -- any real failure
    to even run the subprocess should degrade the same honest way."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", host, cmd],
                            capture_output=True, text=True, timeout=timeout + 5)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        # Printed here, not just returned -- some callers (e.g. query_
        # thermal_slowdown_all_gpus' per-GPU _one()) only look at rc!=0
        # to decide "skip this one GPU" and never surface the specific
        # err string anywhere themselves, which would make this failure
        # mode quiet-but-not-crashing rather than genuinely honest/
        # logged. Printing directly here, at the one lowest, central
        # point every real ssh failure already passes through, is what
        # makes "never silent" true regardless of how any given caller
        # chooses to handle its own (out, err, rc) tuple.
        msg = f"ssh {host} timed out after {timeout + 5}s (cmd={cmd!r})"
        print(f"[SSH-TIMEOUT] {msg}", file=sys.stderr, flush=True)
        return "", msg, 1
    except OSError as e:
        msg = f"ssh {host} failed to run: {type(e).__name__}: {e}"
        print(f"[SSH-FAILED] {msg}", file=sys.stderr, flush=True)
        return "", msg, 1


def check_nv_hostengine_alive(host):
    """Real, live liveness check for nv-hostengine (the persistent DCGM
    daemon every query_dcgm_all_gpus/query_matmul_tflops/etc. call in this
    file depends on) -- found and fixed this session: nv-hostengine was
    never running on the original 2-node cluster, silently degrading
    every DCGM-sourced cause-check from "confirmed no thermal cause" down
    to "couldn't check" for the whole session, with no loud signal
    anywhere that this had happened.

    Deliberately the cheapest real check that actually distinguishes
    "daemon up" from "daemon down" -- `dcgmi discovery -l` needs no field
    IDs or group setup (unlike `dcgmi dmon`), just a live host engine to
    answer at all, so it can't false-degrade on a syntax/argument issue
    the way a more specific query could.

    Returns (alive: bool, reason: str|None) -- reason is None when alive,
    otherwise dcgmi's own real stdout/stderr (same stdout-first fallback
    as query_dcgm_all_gpus, since dcgmi puts its real error text on
    stdout, not stderr, confirmed live)."""
    out, err, rc = ssh(host, "dcgmi discovery -l")
    if rc != 0:
        return False, err or out.strip() or f"dcgmi discovery -l exited {rc} with no output"
    return True, None


def query_dcgm_all_gpus(host, fields=("sm_clock", "throttle_reasons", "gpu_temp", "mem_temp",
                                       "power_usage", "pcie_replay", "pcie_link_gen", "pcie_link_width",
                                       "xid_errors", "ecc_sbe_vol", "ecc_dbe_vol",
                                       "retired_sbe", "retired_dbe", "retired_pending",
                                       "nvlink_crc_total", "nvlink_bandwidth_total")):
    """Returns {gpu_index: {field_name: value}} for all 8 GPUs on host."""
    field_ids = ",".join(str(DCGM_FIELDS[f]) for f in fields)
    out, err, rc = ssh(host, f"dcgmi dmon -e {field_ids} -c 1")
    if rc != 0:
        # dcgmi's own error text (e.g. "Host engine connection
        # invalid/disconnected" when no DCGM hostengine is reachable from
        # this host -- the real, confirmed case on a cluster with no live
        # DCGM path) goes to STDOUT, not stderr -- ssh() itself succeeds
        # (rc reflects the REMOTE command's exit code), so `err` (stderr)
        # can come back empty even though the query genuinely failed.
        # Fall back to stdout so callers never get a blank reason.
        return None, err or out.strip() or f"dcgmi dmon exited {rc} with no output"
    lines = [l for l in out.splitlines() if l.strip() and l.strip()[0].isdigit() is False and "GPU" in l]
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "GPU":
            gpu_idx = int(parts[1])
            vals = parts[2:2 + len(fields)]
            result[gpu_idx] = dict(zip(fields, vals))
    return result, None


def query_ib_counters(host, device="mlx5_0", port=1):
    """Requires /sys-host/class/infiniband to exist on the host (it always
    does at the pod level -- the constraint is whether it's mounted into
    the JOB container, not whether it exists on the host used to check it)."""
    out, err, rc = ssh(host, f"cat /sys-host/class/infiniband/{device}/ports/{port}/counters/port_xmit_data 2>&1")
    if rc != 0 or "No such file" in out:
        return None, "sys-host not present on this host"
    return int(out.strip()), None


def discover_ib_devices(host):
    """Real, dynamic IB device discovery for this specific host -- lists
    /sys-host/class/infiniband/ directly (the same real path this
    module's own counter reads already use) rather than assuming exactly
    8 mlx5_N devices (cluster-topology-agnostic fix, this session: the
    old hardcoded range(8) default silently missed any device beyond
    index 7 on a node with more NICs, and silently queried nonexistent
    devices -- degrading gracefully via the existing "NA" fallback, but
    never actually seeing devices 8+ -- on a node with fewer). Returns []
    (not a crash) if the path can't be listed (SSH failure, or a node
    with no IB devices at all -- a real, valid answer, not an error)."""
    out, err, rc = ssh(host, "ls /sys-host/class/infiniband/ 2>/dev/null")
    if rc != 0 or not out.strip():
        return []
    return sorted(out.split())


def query_ib_all_devices(host, devices=None):
    """devices defaults to a fresh list per call -- NOT a generator. A
    generator expression as a default is created ONCE at function-def
    time and shared across every call; the second call in a process
    would get an already-exhausted generator and silently return {}.
    Found via query_network_snapshot's two-snapshot-per-host design,
    which is the first caller to invoke this more than once per process.

    Cluster-topology-agnostic fix (this session): devices used to default
    to a hardcoded [mlx5_0..mlx5_7] -- see discover_ib_devices' own
    docstring. None now means "discover this host's own real IB devices
    right now"; an explicit devices list is still honored unchanged for
    a caller that already knows which ones it wants (e.g. two-snapshot
    callers that discover once and reuse the same real list for both
    snapshots, avoiding a redundant second discovery call)."""
    if devices is None:
        devices = discover_ib_devices(host)

    def _one(dev):
        cmd = " ; ".join(
            f"echo {c}=$(cat /sys-host/class/infiniband/{dev}/ports/1/counters/{c} 2>/dev/null || echo NA)"
            for c in ["port_xmit_data", "port_rcv_errors", "symbol_error", "link_error_recovery",
                      "port_xmit_discards", "port_xmit_wait", "VL15_dropped"]
        )
        out, err, rc = ssh(host, cmd)
        if "sys-host" in err or rc != 0 and not out:
            return None
        d = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = None if v == "NA" else int(v)
        return d if d else None

    # P27-hotfix-stall -- was a sequential `for dev in devices` loop, one
    # SSH round-trip per device; see _parallel_map's own docstring.
    return _parallel_map(_one, devices)


NETWORK_ERROR_FIELDS = ["port_rcv_errors", "symbol_error", "link_error_recovery", "port_xmit_discards", "VL15_dropped"]


def query_network_snapshot(hosts, interval=2.0):
    """Real, enforced overall deadline (OVERALL_CHECK_DEADLINE_S) around
    _query_network_snapshot_inner -- see _with_overall_deadline's own
    docstring for why this exists on top of per-call parallelization."""
    return _with_overall_deadline(lambda: _query_network_snapshot_inner(hosts, interval))


def _query_network_snapshot_inner(hosts, interval=2.0):
    """P18b Stage 3: two-snapshot IB read per host, `interval` seconds
    apart -- device participation (devices with nonzero xmit delta),
    aggregate throughput rate, and error/congestion counter deltas. This
    is P6's finding set (device participation, throughput rate, error/
    congestion cross-check), not a new metric surface.

    Requires /sys-host:/sys-host in the job's --container-mounts.
    Without it, query_ib_all_devices returns all-None per host and this
    returns (None, reason) naming the mount requirement -- same shape as
    every other "checked but unobtainable" case in this classifier.

    Cluster-topology-agnostic fix (this session): device discovery
    (discover_ib_devices) now happens ONCE per host, here, and the same
    real list is reused for BOTH snapshots -- avoids a redundant second
    discovery SSH round-trip per host per cycle (each snapshot used to
    independently re-discover when query_ib_all_devices' own devices
    default was still a static hardcoded list; discovery is real work now,
    so doing it twice per cycle would be a real, avoidable cost).

    P27-hotfix-stall -- devices_by_host/snap0/snap1 were sequential dict
    comprehensions (one host at a time); now parallel across hosts, on
    top of query_ib_all_devices' own now-parallel per-device fan-out --
    every host's every device fires its SSH round-trip concurrently."""
    devices_by_host = _parallel_map(discover_ib_devices, hosts)
    snap0 = _parallel_map(lambda h: query_ib_all_devices(h, devices_by_host[h]), hosts)
    if all(v is None for devs in snap0.values() for v in devs.values()):
        return None, "sys-host not mounted -- requires /sys-host:/sys-host in --container-mounts"
    time.sleep(interval)
    snap1 = _parallel_map(lambda h: query_ib_all_devices(h, devices_by_host[h]), hosts)

    result = {}
    for h in hosts:
        devs0, devs1 = snap0[h], snap1[h]
        active, total = 0, 0
        xmit_delta_total, errors_delta, congestion_delta = 0, 0, 0
        for dev in devs0:
            a, b = devs0.get(dev), devs1.get(dev)
            if a is None or b is None:
                continue
            total += 1
            xd = (b.get("port_xmit_data") or 0) - (a.get("port_xmit_data") or 0)
            xmit_delta_total += max(0, xd)
            if xd > 0:
                active += 1
            for f in NETWORK_ERROR_FIELDS:
                errors_delta += max(0, (b.get(f) or 0) - (a.get(f) or 0))
            congestion_delta += max(0, (b.get("port_xmit_wait") or 0) - (a.get("port_xmit_wait") or 0))
        result[h] = {
            "active_devices": active, "total_devices": total,
            "participation_frac": (active / total) if total else None,
            "xmit_rate_bytes_s": xmit_delta_total / interval,
            "errors_delta": errors_delta, "congestion_delta": congestion_delta,
        }
    return result, None


# P25 Part 1 -- NVLink is the ONLY fabric TP traffic ever crosses (TP pairs
# are always co-located on one node, confirmed directly in P21's own
# device_mesh comments), and check_network_contention_direct only ever
# reads IB counters -- a real NVLink fault on any TP job is currently
# completely undetectable, not just harder to catch. Confirmed live on
# this hardware (H200, dcgmi 4.5.2) before writing a line of this:
#   - `dcgmi nvlink -s` reports per-GPU, per-link state (Up/Down/Disabled/
#     Not Supported) -- 18 links per GPU on this hardware, discovered from
#     that output each call, never assumed.
#   - `dcgmi nvlink -g <gpu> -e` reports per-link CRC FLIT/CRC Data/Replay/
#     Recovery error COUNTS -- confirmed these are CUMULATIVE-since-driver-
#     load, not deltas: an idle, healthy GPU4 showed real nonzero CRC Data
#     Error counts (325-493 per link) that stayed byte-for-byte identical
#     across a 5s idle re-check -- so, exactly like IB, this must be
#     compared as a DELTA across the observation window, never as a raw
#     value, or every check would misfire as CONFIRMED at rest.
#   - dmon's own precomputed `nvlink_bandwidth_total` field (449) works
#     live but its averaging window isn't independently verifiable, so
#     throughput here instead deltas the genuinely cumulative per-GPU
#     `nvlink_tx_bytes` counter (field 1011) over `interval`, exactly
#     mirroring port_xmit_data's role in query_network_snapshot above.
# Unlike IB (one shared cluster fabric -- NETWORK_ATTRIBUTION_CAVEAT
# applies), NVLink is intra-node only and this project's jobs always
# allocate a whole node (--gpus-per-node=8) -- so an NVLink reading has
# no cross-tenant attribution ambiguity at all; not carrying that caveat
# forward here is deliberate, not an oversight.
NVLINK_ERROR_LABELS = ["CRC FLIT Error", "CRC Data Error", "Replay Error", "Recovery Error"]


def query_nvlink_link_states(host, gpu_count=8):
    """Parses `dcgmi nvlink -s`'s per-GPU link-state line into
    {gpu_id: [state, state, ...]} -- link COUNT and STATE both come from
    this one real call, never assumed from a NVLink-generation constant.
    State chars: U=up, D=down, X=disabled, _=not supported (excluded)."""
    out, err, rc = ssh(host, "dcgmi nvlink -s")
    if rc != 0 or not out:
        return None, err or "dcgmi nvlink -s returned no output"
    states = {}
    in_gpus = False
    gpu_id = None
    for line in out.splitlines():
        s = line.strip()
        if s == "GPUs:":
            in_gpus = True
            continue
        if s == "NvSwitches:":
            break
        if not in_gpus:
            continue
        if s.startswith("gpuId "):
            gpu_id = int(s.split()[1].rstrip(":"))
            continue
        if gpu_id is not None and s:
            states[gpu_id] = [c for c in s.split() if c in ("U", "D", "X", "_")]
            gpu_id = None
    if not states:
        return None, "no GPU link-state rows parsed from `dcgmi nvlink -s`"
    return states, None


def query_nvlink_errors_all_gpus(host, gpu_ids):
    """Parses `dcgmi nvlink -g <gpu> -e` per GPU -- returns
    {gpu_id: total_error_count} summed across every link and every one of
    NVLINK_ERROR_LABELS. Cumulative-since-load, like IB's counters --
    caller must delta this across two snapshots, never read it raw.

    P27-hotfix-stall -- this was the SINGLE biggest contributor to the
    confirmed multi-minute stall: called twice per snapshot pair, once
    per host, each call a sequential `for gpu in gpu_ids` loop (8 SSH
    round-trips) -- 2 hosts x 8 gpus x 2 snapshots = 32 sequential SSH
    calls just for this one function, per check. Now all gpu_ids for one
    host fire concurrently; see _parallel_map's own docstring."""
    def _one(gpu):
        out, err, rc = ssh(host, f"dcgmi nvlink -g {gpu} -e")
        if rc != 0 or not out:
            return None
        total = 0
        for line in out.splitlines():
            for label in NVLINK_ERROR_LABELS:
                if label in line and "|" in line:
                    parts = line.split("|")
                    if len(parts) >= 3:
                        try:
                            total += int(parts[2].strip())
                        except ValueError:
                            pass
        return total
    return _parallel_map(_one, gpu_ids)


def query_nvlink_bandwidth_all_gpus(host):
    """Single dmon poll of `nvlink_bandwidth_total` (field 449) --
    {gpu_id: bytes_per_sec}. Deliberately NOT a two-snapshot delta of a
    cumulative counter the way IB's port_xmit_data is: field 1011
    (nvlink_tx_bytes), the obvious cumulative-counter candidate, was
    tested live and is NOT monotonic -- two independent one-shot polls
    2s apart on the SAME real, actively-training GPUs came back LOWER
    the second time (deltas of -2M to -58M bytes on 6 of 8 GPUs), so it
    is some kind of decaying/windowed internal value, not a lifetime
    counter, and diffing it silently produces a false near-zero-
    throughput reading. Field 449 by contrast is DCGM's own precomputed
    rate (unit column reads "MB/") and was confirmed stable and
    consistent with real load across 4 independent one-shot polls
    (~12,500-13,700 MB/s under real TP+DDP training on this hardware) --
    used directly as a rate, no delta math needed or safe to do here."""
    out, err, rc = ssh(host, "dcgmi dmon -e 449 -c 1")
    if rc != 0 or not out:
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "GPU":
            try:
                result[int(parts[1])] = None if parts[2] == "N/A" else int(parts[2]) * 1e6  # MB/s -> bytes/s
            except ValueError:
                pass
    return result


def query_nvlink_snapshot(hosts, interval=2.0, gpu_count=8):
    """Real, enforced overall deadline (OVERALL_CHECK_DEADLINE_S) around
    _query_nvlink_snapshot_inner -- see _with_overall_deadline's own
    docstring for why this exists on top of per-call parallelization."""
    return _with_overall_deadline(lambda: _query_nvlink_snapshot_inner(hosts, interval, gpu_count))


def _query_nvlink_snapshot_inner(hosts, interval=2.0, gpu_count=8):
    """NVLink counterpart to query_network_snapshot -- same three-part
    evidence (participation, throughput, errors), same per-host aggregate
    return shape, so check_nvlink_contention_direct can reuse check_
    network_contention_direct's exact thresholding logic. Participation
    here comes directly from real link STATE (Up/Down), not a throughput-
    delta proxy -- a strictly stronger signal than IB has, available
    because DCGM exposes it directly; still gated behind an active job by
    the caller for the throughput/error dimensions, same as IB.

    P27-hotfix-stall -- link_states/errors0/errors1/bw were sequential
    dict comprehensions (one host at a time, each itself now fanning out
    in parallel across GPUs via query_nvlink_errors_all_gpus); now
    parallel across hosts too, so every host's every GPU query for a
    given phase fires concurrently. The confirmed root cause of the
    multi-minute stall was this function's OWN structure: 2 hosts x 8
    GPUs x 2 error snapshots, each call sequential -- see this file's
    module-level comment above _parallel_map for the full live-diagnosed
    story (gdb thread dumps, ruled-out "remote command is just slow")."""
    link_states = _parallel_map(lambda h: query_nvlink_link_states(h, gpu_count), hosts)
    if all(v[0] is None for v in link_states.values()):
        return None, "no NVLink link-state data on any host (dcgmi unreachable?)"
    gpu_ids_by_host = {h: sorted((link_states[h][0] or {}).keys()) for h in hosts}

    errors0 = _parallel_map(lambda h: query_nvlink_errors_all_gpus(h, gpu_ids_by_host[h]), hosts)
    time.sleep(interval)
    errors1 = _parallel_map(lambda h: query_nvlink_errors_all_gpus(h, gpu_ids_by_host[h]), hosts)
    bw = _parallel_map(query_nvlink_bandwidth_all_gpus, hosts)  # instantaneous rate, no delta needed

    result = {}
    for h in hosts:
        states, lerr = link_states[h]
        if states is None:
            result[h] = None
            continue
        active_links, total_links = 0, 0
        for gpu, link_list in states.items():
            for st in link_list:
                total_links += 1
                if st == "U":
                    active_links += 1
        xmit_rate_total, errors_delta = 0, 0
        for gpu in gpu_ids_by_host[h]:
            r = bw[h].get(gpu)
            if r is not None:
                xmit_rate_total += r
            e0, e1 = errors0[h].get(gpu), errors1[h].get(gpu)
            if e0 is not None and e1 is not None:
                errors_delta += max(0, e1 - e0)
        result[h] = {
            "active_devices": active_links, "total_devices": total_links,
            "participation_frac": (active_links / total_links) if total_links else None,
            "xmit_rate_bytes_s": xmit_rate_total,
            "errors_delta": errors_delta, "congestion_delta": 0,
        }
    return result, None


def discover_gpu_count(host):
    """Real, dynamic per-host GPU count -- `nvidia-smi -L` lists exactly
    one line per real physical GPU on this host, counted directly rather
    than assumed (cluster-topology-agnostic fix, this session: query_
    thermal_slowdown_all_gpus/query_matmul_tflops both used to hardcode
    exactly 8, silently missing GPUs beyond index 7 on a node with more,
    or wasting queries against nonexistent indices on a node with fewer).
    Returns 0 (not a crash) if the host can't be reached or has no GPUs."""
    out, err, rc = ssh(host, "nvidia-smi -L 2>/dev/null | wc -l")
    if rc != 0 or not out.strip():
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def query_thermal_slowdown_all_gpus(host):
    """Real, enforced overall deadline (OVERALL_CHECK_DEADLINE_S) around
    _query_thermal_slowdown_all_gpus_inner -- see _with_overall_deadline's
    own docstring for why this exists on top of per-call parallelization,
    and this file's module-level comment above _parallel_map for the full
    live-diagnosed story (codebase audit, third pass) this specific
    function was found NOT to be covered by when that story was written:
    this one sits on the LIVE alert-construction path (build_single_rank_
    finding's Path A, called from build_finding_for_alert, dispatched via
    the SAME shared pool poll_once() blocks on) -- a WORSE blast radius
    than nvlink/network's own out-of-band thread, since a stall here can
    freeze the entire poll loop, not just one background check's output."""
    return _with_overall_deadline(lambda: _query_thermal_slowdown_all_gpus_inner(host))


def _query_thermal_slowdown_all_gpus_inner(host):
    """Cumulative SW/HW thermal slowdown counters, every real GPU on
    host (this cluster's own real count, discovered via discover_gpu_
    count -- not assumed). This is the proven signal that identified
    GPU 3's degradation -- NOT available via DCGM's dmon on this
    platform (fields 1422/1423 return N/A), so this falls back to the
    same nvidia-smi query established in run_health_check.sh.

    P27-hotfix-thermal -- was a sequential `for i in range(gpu_count)`
    loop, one SSH round-trip per GPU (up to 8); now all GPUs fire
    concurrently via _parallel_map, the exact same fix pattern already
    built and validated for query_nvlink_errors_all_gpus/query_ib_all_
    devices -- reused directly, not reimplemented."""
    gpu_count = discover_gpu_count(host)

    def _one(i):
        out, err, rc = ssh(host, f"nvidia-smi -i {i} -q -d PERFORMANCE")
        if rc != 0:
            return None
        sw, hw = None, None
        lines = out.splitlines()
        # counters block only -- parse after "Clocks Event Reasons Counters"
        in_counters = False
        for line in lines:
            if "Clocks Event Reasons Counters" in line:
                in_counters = True
                continue
            if in_counters:
                if "SW Thermal Slowdown" in line:
                    sw = int(line.split(":")[1].strip().split()[0])
                elif "HW Thermal Slowdown" in line:
                    hw = int(line.split(":")[1].strip().split()[0])
                elif "Sparse Operation Mode" in line or line.strip() == "":
                    if sw is not None and hw is not None:
                        break
        return {"sw_thermal_us": sw, "hw_thermal_us": hw}

    return _parallel_map(_one, range(gpu_count)), None


def _host_has_active_job(host, timeout=5):
    """Same real squeue-based job-presence technique already established
    in this project (alert_engine.py's _hosts_have_active_job, node_
    aggregator_ref.py's refresh_job_id) -- reimplemented here rather than
    imported, since cause_metrics.py has zero dependency on either of
    those modules and must stay that way: P18k_classifier is the lower-
    level, independently-importable package (classifier.py/detection.py's
    own standalone tools already rely on that), P20c_alerting is the live
    consumer that imports IT, never the reverse -- importing alert_
    engine.py from here would be a real circular dependency, not a
    reuse."""
    try:
        out = subprocess.run(["squeue", "-w", host, "-h", "-o", "%i", "--states=R"],
                              capture_output=True, text=True, timeout=timeout)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def query_matmul_tflops(host, run_health_check_path=_BENCH_SCRIPT_DEFAULT):
    """Isolated matmul TFLOPS, every real GPU on host -- reuses the exact
    benchmark from run_health_check.sh via a fresh container invocation.

    Cluster-topology-agnostic fix (this session): --gpus-per-node used to
    be hardcoded 8, silently requesting the wrong real GPU count on any
    node shaped differently. Now discovered live via discover_gpu_count
    (nvidia-smi -L) before the srun call. Returns (None, reason) if
    discovery itself fails, honestly, rather than falling back to a
    guessed number.

    P27-hotfix-thermal -- real, confirmed risk this closes (codebase
    audit, third pass): this benchmark REQUESTS the full real GPU count
    on `host` via a fresh srun job. A real fault-injection test, by
    construction, has already allocated that host's GPUs to the job
    under test -- so every time a real alert needed this corroboration
    (exactly the moment blocking matters most), this call was virtually
    guaranteed to QUEUE rather than run, for up to the full 120s timeout.
    Skipped entirely when the host already has an active job (same
    squeue technique already established elsewhere in this project),
    returning the same honest (None, reason) shape every other 'checked
    but unobtainable' case in this module already uses, rather than
    attempting a launch already known to be futile. The 120s subprocess
    timeout is now also caught explicitly (it previously wasn't) so an
    idle-host-but-still-slow benchmark degrades honestly too, instead of
    an uncaught TimeoutExpired propagating to the caller."""
    if _host_has_active_job(host):
        return None, (f"skipped: {host} already has an active job occupying its GPUs -- an "
                       f"additional --gpus-per-node srun benchmark here would very likely queue "
                       f"rather than run, the same real risk this session confirmed and fixed "
                       f"for the nvlink/network checks -- degraded/not checked this cycle, not "
                       f"a claim of GPU compute health")
    gpu_count = discover_gpu_count(host)
    if gpu_count <= 0:
        return None, f"could not discover a real GPU count on {host} (nvidia-smi -L failed or returned nothing)"
    cmd = (
        f"srun -N1 -w {host} --gpus-per-node={gpu_count} "
        "--container-image=nvcr.io#nvidia/pytorch:25.01-py3 "
        "--container-mounts=/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root "
        f"python3 {run_health_check_path}"
    )
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, f"matmul TFLOPS benchmark on {host} exceeded its 120s timeout -- degraded/not checked this cycle"
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line), None
    return None, r.stderr or "no JSON output"


def query_host_cpu(host):
    """Stand-in for node_cpu_seconds_total (no node_exporter wired yet --
    working from raw dumps per this task's scope). Returns 1-minute load
    average and core count from /proc/loadavg -- the same underlying
    signal node_cpu_seconds_total exposes, read directly."""
    out, err, rc = ssh(host, "cat /proc/loadavg; nproc")
    if rc != 0:
        return None, err
    lines = out.strip().splitlines()
    load1 = float(lines[0].split()[0])
    ncores = int(lines[1])
    return {"load1": load1, "ncores": ncores, "load_per_core": load1 / ncores}, None
