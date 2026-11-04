#!/usr/bin/env python3
"""analyze_ab.py TAG -- per-run mean iteration time (first 20 iterations dropped as warmup),
Welch's t-test of each arm vs 'off' on per-run means, per-round paired overhead, contamination
check against soperatorchecks jobs (sacct), and pinned-core utilization from the 1 Hz CPU samplers."""
import glob, json, os, re, statistics as st, subprocess, sys
from scipy import stats
TAG = sys.argv[1]; WARM = 20; R = "/root/inspector-audit/runs"
def iters(d):
    best = []
    for f in glob.glob(f"{d}/train_worker-*.log"):
        v = [float(m[1]) for l in open(f, errors="ignore") if (m := re.search(r"elapsed time per iteration \(ms\): ([\d.]+)", l))]
        if len(v) > len(best): best = v
    return best
def contaminated(d):
    try:
        s, e = int(open(f"{d}/t_start").read()), int(open(f"{d}/t_end").read())
        out = subprocess.run(["sacct", "-X", "-n", "-P", "-S", f"@{s}", "-E", f"@{e}", "-u", "soperatorchecks", "-o", "JobID,Start,End"], capture_output=True, text=True).stdout
        return bool(out.strip())
    except Exception: return None
runs = {}
for d in sorted(glob.glob(f"{R}/{TAG}_r*_*")):
    m = re.match(rf".*{TAG}_r(\d+)_(\w+)$", d); v = iters(d)
    if len(v) <= WARM: print(f"SKIP {d}: only {len(v)} iterations"); continue
    m_ = v[WARM:]; runs.setdefault(m[2], {})[int(m[1])] = {"mean": st.mean(m_), "median": st.median(m_), "n": len(m_), "cont": contaminated(d), "dir": d}
print(f"{'arm':10} {'runs':>4} {'iters/run':>9} {'mean ms':>9} {'sd(runs)':>9} {'median ms':>10}  contaminated")
for a, rr in runs.items():
    ms = [x["mean"] for x in rr.values()]
    print(f"{a:10} {len(rr):>4} {rr[min(rr)]['n']:>9} {st.mean(ms):9.2f} {st.pstdev(ms) if len(ms)>1 else 0:9.2f} {st.mean(x['median'] for x in rr.values()):10.2f}  {[r for r,x in rr.items() if x['cont']]}")
if "off" in runs:
    base = [x["mean"] for x in runs["off"].values()]
    print("\nOverhead vs OFF (per-run means; Welch two-sided; paired per-round in brackets):")
    for a, rr in runs.items():
        if a == "off": continue
        ms = [x["mean"] for x in rr.values()]
        t, p = stats.ttest_ind(ms, base, equal_var=False)
        pr = [100 * (rr[r]["mean"] / runs["off"][r]["mean"] - 1) for r in rr if r in runs["off"]]
        print(f"  {a:9} overhead {100*(st.mean(ms)/st.mean(base)-1):+6.2f}%  Welch t={t:6.2f} p={p:.3f}  paired per-round %: {[round(x,2) for x in pr]}")
# pinned-core utilization
print("\nCPU utilization (host view, 1 Hz, steady state = middle 60% of samples):")
for a in runs:
    for r, x in runs[a].items():
        for w in ("worker-0", "worker-1"):
            f = f"{x['dir']}/cpu_{w}.jsonl"
            if not os.path.exists(f): continue
            rows = [json.loads(l)["busy"] for l in open(f)]; n = len(rows); rows = rows[int(n*.2):int(n*.8)]
            if not rows: continue
            avg = {int(c): st.mean(r_[c] for r_ in rows) for c in rows[0]}
            numa_train = [c for c in avg if c < 48 or 64 <= c < 112]; resv = [c for c in avg if 48 <= c < 64 or c >= 112]
            print(f"  {a:9} r{r} {w}: train-cores mean {st.mean(avg[c] for c in numa_train):5.1f}%  | cpus48-63,112-127 mean {st.mean(avg[c] for c in resv):5.1f}% max-core {max(avg[c] for c in resv):5.1f}%")
            break
