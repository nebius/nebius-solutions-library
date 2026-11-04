#!/usr/bin/env python3
"""verify_pinning.py -- NEW, UNVALIDATED runtime health check for thread pinning.
Waits for pin_*.log records (written by thread_pin_new.c at actual thread creation),
then INDEPENDENTLY re-reads each recorded thread's current affinity from
/proc/<pid>/task/<tid>/status and checks the training (main) thread excludes the
reserved cores. Verdict: FULLY_DEDICATED (both roles) / DUMP_ONLY_DEDICATED (dump role only) only if every expected rank has >=1 verified
dump thread AND >=1 verified profiler thread (roles per --roles); else PARTIAL with reasons.
Run inside the same pid namespace as the training processes (e.g. from train_node_*.sh)."""
import argparse, glob, json, os, re, socket, sys, time

def cpus(s):
    out = set()
    for p in filter(None, s.strip().split(",")):
        if "-" in p:
            a, b = p.split("-"); out |= set(range(int(a), int(b) + 1))
        else: out.add(int(p))
    return out

def proc_allowed(pid, tid):
    try:
        for l in open(f"/proc/{pid}/task/{tid}/status"):
            if l.startswith("Cpus_allowed_list:"): return cpus(l.split(":")[1])
    except OSError: return None

def collect(d):
    ranks, threads = {}, []
    launch = set()
    for f in glob.glob(f"{d}/launch_{socket.gethostname()}.log"):
        for l in open(f):
            m = re.search(r" pid=(\d+) local_rank", l)
            if m: launch.add(int(m[1]))
    for f in glob.glob(f"{d}/pin_{socket.gethostname()}_*.log"):
        try: lines = open(f).read().splitlines()
        except OSError: continue
        for l in lines:
            if l.startswith("INIT"):
                m = re.search(r"pid=(\d+) dump_cpus=\[(.*?)\] profiler_cpus=\[(.*?)\] profiler_ranges=(\d+)", l)
                ranks[int(m[1])] = {"dump": m[2], "profiler": m[3], "ranges": int(m[4])}
            elif l.startswith("THREAD"):
                m = re.search(r"role=(\w+) pid=(\d+) tid=(\d+) .*wanted=\[(.*?)\] readback=\[(.*?)\] match=(\d)", l)
                threads.append({"role": m[1], "pid": int(m[2]), "tid": int(m[3]), "wanted": m[4], "readback": m[5], "match": m[6] == "1"})
    ranks = {p: v for p, v in ranks.items() if p in launch}  # only real rank processes, not their child processes
    threads = [t for t in threads if t['pid'] in launch]
    return ranks, threads

def mapped_nccl(pid):
    """realpath of the libnccl actually mapped into this process, or None."""
    try:
        for l in open(f"/proc/{pid}/maps"):
            if "libnccl.so" in l:
                return os.path.realpath(l.split()[-1])
    except Exception:
        pass
    return None


def preflight_nccl(log_dir):
    """NCCL path the launcher PREDICTED (last [PIN-PREFLIGHT] lib=... line in this host's launch log), or None."""
    exp = None
    try:
        for l in open(f"{log_dir}/launch_{socket.gethostname()}.log", errors="ignore"):
            m = re.search(r"\[PIN-PREFLIGHT\].* lib=(\S+)", l)
            if m and m.group(1) != "none":
                exp = os.path.realpath(m.group(1))
    except Exception:
        pass
    return exp


def thread_stat(pid, tid):
    try:
        f = open(f"/proc/{pid}/task/{tid}/stat").read().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12]), int(f[36])  # utime+stime (ticks), CPU last run on
    except (OSError, IndexError, ValueError): return None

def sample_threads(rep, secs, reasons):
    hz = os.sysconf("SC_CLK_TCK"); ths = [(pid, t) for pid, r in rep.items() for t in r["threads"] if t["verified"]]
    first = {(p, t["tid"]): thread_stat(p, t["tid"]) for p, t in ths}; seen = {k: [] for k in first}; t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(1)
        for (p, t) in ths:
            st = thread_stat(p, t["tid"])
            if st: seen[(p, t["tid"])].append(st[1])
    by = {}
    for (p, t) in ths:
        k = (p, t["tid"]); last = thread_stat(p, t["tid"]); a0 = first[k]
        if not last or not a0: continue
        util = 100.0 * (last[0] - a0[0]) / hz / max(1e-9, time.time() - t0)
        want = cpus(t["wanted"]); on = sum(1 for c in seen[k] if c in want) / max(1, len(seen[k]))
        now = proc_allowed(p, t["tid"]); drift = (now != want)
        if drift: reasons.append(f"pid {p} {t['role']} tid {t['tid']}: affinity DRIFTED to {sorted(now) if now else now} (wanted {sorted(want)})")
        b = by.setdefault(t["role"], {"u": [], "on": [], "drift": 0}); b["u"].append(util); b["on"].append(on); b["drift"] += drift
    return {"seconds": secs, "by_role": {r: {"n": len(b["u"]), "mean": sum(b["u"]) / len(b["u"]), "max": max(b["u"]), "on_cpu_frac": sum(b["on"]) / len(b["on"]), "drift": b["drift"]} for r, b in by.items()}}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", required=True); ap.add_argument("--expect-ranks", type=int, required=True)
    ap.add_argument("--roles", default="dump,profiler"); ap.add_argument("--wait", type=int, default=180)
    ap.add_argument("--reserved", default=os.environ.get("PIN_RESERVED_CPUS", ""))
    ap.add_argument("--sample-after", type=int, default=0, help="seconds to wait (training reaching steady state) before the --sample window starts")
    ap.add_argument("--sample", type=int, default=0, help="after the startup verdict, sample each pinned thread for N seconds: per-thread CPU%%, CPU last run on, and re-verify affinity at the end (drift check)")
    a = ap.parse_args(); roles = a.roles.split(","); t0 = time.time()
    os.makedirs(a.log_dir, exist_ok=True)
    while time.time() - t0 < a.wait:  # wait until every rank has at least one thread record per role
        ranks, th = collect(a.log_dir)
        have = {p: {t["role"] for t in th if t["pid"] == p} for p in ranks}
        if len(ranks) >= a.expect_ranks and all(set(roles) <= h for h in have.values()): break
        time.sleep(3)
    ranks, th = collect(a.log_dir); reasons = []; rep = {}
    if len(ranks) < a.expect_ranks: reasons.append(f"only {len(ranks)}/{a.expect_ranks} ranks loaded the interposer")
    res = cpus(a.reserved)
    for pid, info in ranks.items():
        r = {"threads": [], "main_overlap_with_reserved": None}
        for role in roles:
            mine = [t for t in th if t["pid"] == pid and t["role"] == role]
            ok = 0
            for t in mine:
                live = proc_allowed(pid, t["tid"])  # independent of the interposer's own readback
                good = t["match"] and live is not None and live == cpus(t["wanted"])
                ok += good
                r["threads"].append({**t, "proc_affinity": sorted(live) if live is not None else "thread-gone", "verified": good})
            if role == "profiler" and info["ranges"] == 0:
                reasons.append(f"pid {pid}: profiler UNAVAILABLE (loaded NCCL has no ncclProfilerThreadCreate symbol range)")
            elif not mine: reasons.append(f"pid {pid}: no {role} thread creation record")
            elif ok < len(mine): reasons.append(f"pid {pid}: {len(mine)-ok}/{len(mine)} {role} threads NOT verified pinned")
        got = mapped_nccl(pid); exp = preflight_nccl(a.log_dir)
        r["nccl_mapped"] = got; r["nccl_predicted"] = exp
        if got and exp and got != exp:
            msg = f"pid {pid}: loaded NCCL {got} differs from the launcher's preflight prediction {exp}"
            if "profiler" in roles: reasons.append(msg + " (profiler ranges came from the wrong library)")
            else: print(f"[PIN-VERIFY][WARN] {msg}")
        main = proc_allowed(pid, pid)
        if main is not None and res:
            r["main_overlap_with_reserved"] = sorted(main & res)
            if main & res: reasons.append(f"pid {pid}: training main thread can still run on reserved cores {sorted(main & res)}")
        rep[pid] = r
    samples = None
    if a.sample > 0 and not reasons:
        time.sleep(a.sample_after)
        samples = sample_threads(rep, a.sample, reasons)
    # FULLY_DEDICATED is reserved for BOTH roles verified; dump-only runs (older NCCL, or PIN_MODE=dump) get an honest,
    # distinct label so a dump-only result can never be read as full dedication.
    if reasons:
        verdict = "PARTIAL_OR_NOT_DEDICATED"
    elif set(roles) >= {"dump", "profiler"}:
        verdict = "FULLY_DEDICATED"
    else:
        verdict = "DUMP_ONLY_DEDICATED"
    out = {"host": socket.gethostname(), "verdict": verdict, "roles_required": roles, "ranks_seen": len(ranks), "reasons": reasons, "per_pid": rep, "sampling": samples}
    json.dump(out, open(f"{a.log_dir}/verify_{socket.gethostname()}.json", "w"), indent=1)
    print(f"[PIN-VERIFY] host={socket.gethostname()} verdict={verdict} ranks={len(ranks)}/{a.expect_ranks} roles={','.join(roles)}")
    if samples:
        for role, v in samples["by_role"].items():
            print(f"[PIN-VERIFY] sampled {a.sample}s role={role}: threads={v['n']} cpu_util_pct mean={v['mean']:.1f} max={v['max']:.1f}; ran_on_assigned_cpu={v['on_cpu_frac']*100:.0f}% of samples; affinity_drift={v['drift']}")
    for x in reasons[:12]: print(f"[PIN-VERIFY][WARN] {x}")
    sys.exit(0 if not reasons else 3)
if __name__ == "__main__": main()
