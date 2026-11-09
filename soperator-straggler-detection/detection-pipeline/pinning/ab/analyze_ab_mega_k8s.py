#!/usr/bin/env python3
"""analyze_ab_mega_k8s.py TAG -- Megatron counterpart to analyze_ab_k8s.py.

Reads iters.txt written by ab_driver_mega_k8s.sh (one "elapsed time per iteration
(ms)" float per line), drops WARM leading warmup iterations, then:
  - per-arm summary (mean, sd, median)
  - Welch two-sided t-test of each arm vs 'off' on per-round means
  - per-round paired overhead %
  - PIN-VERIFY verdicts

Methodology differences vs analyze_ab_k8s.py (nanoGPT):
  SAME: Welch t-test, per-round means, overhead vs off
  DIFFERENT: WARM=10 (Megatron has a 10-step CUDA/NCCL warmup in its own log);
             log format is Megatron's "elapsed time per iteration (ms): N" (log-interval=1,
             so 300 steps = 300 records vs nanoGPT's 15 records per 300 steps);
             all arms use NCCL 2.32.3 (no NCCL version confound)
"""
import glob, os, re, statistics as st, math, sys

TAG = sys.argv[1]; WARM = 10
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../..")
RUNDIR = os.path.join(BASE, "var/ab_k8s", TAG)
ARMS = ["off", "on_nopin", "on_full"]


def load(d):
    f = os.path.join(d, "iters.txt")
    if not os.path.exists(f):
        return []
    with open(f) as fh:
        vals = [float(l.strip()) for l in fh if l.strip()]
    return vals[WARM:]


def welch_t(a, b):
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return float('nan'), float('nan'), float('nan')
    m1, m2 = st.mean(a), st.mean(b)
    v1 = st.variance(a) / n1
    v2 = st.variance(b) / n2
    denom = math.sqrt(v1 + v2)
    if denom == 0:
        return 0.0, float('nan'), 1.0
    t = (m1 - m2) / denom
    df = (v1 + v2)**2 / (v1**2/(n1-1) + v2**2/(n2-1))
    p = math.erfc(abs(t) / math.sqrt(2))
    return t, df, p


runs = {}
for arm in ARMS:
    for d in sorted(glob.glob(os.path.join(RUNDIR, f"{arm}_r*"))):
        m = re.search(rf"{re.escape(arm)}_r(\d+)$", d)
        if not m:
            continue
        v = load(d)
        if not v:
            print(f"SKIP {d}: no data after WARM={WARM}")
            continue
        runs.setdefault(arm, {})[int(m.group(1))] = v

print(f"\nMethodology: Megatron TP=4/PP=1/DP=4, K8s H200, 2-node/16-GPU")
print(f"NCCL 2.32.3 on ALL arms (no version confound). WARM={WARM} steps dropped.")
print(f"\n{'arm':12} {'rounds':>6} {'n/round':>7} {'mean ms':>9} {'sd(within)':>11} "
      f"{'sd(rounds)':>11} {'median':>8}")

for arm in ARMS:
    if arm not in runs:
        print(f"  {arm:10}  (no data)")
        continue
    rr = runs[arm]
    all_v = [x for vs in rr.values() for x in vs]
    per_round = [st.mean(vs) for vs in rr.values()]
    print(f"  {arm:10} {len(rr):>6} {len(list(rr.values())[0]):>7} "
          f"{st.mean(all_v):9.3f} {st.stdev(all_v) if len(all_v)>1 else 0:11.3f} "
          f"{(st.stdev(per_round) if len(per_round)>1 else 0):11.3f} "
          f"{st.median(all_v):8.3f}")

if "off" in runs:
    base_means = [st.mean(vs) for vs in runs["off"].values()]
    print(f"\nOverhead vs OFF (Welch t on {len(base_means)} per-round means; "
          f"NCCL 2.32.3 on all arms):")
    for arm in ARMS:
        if arm == "off" or arm not in runs:
            continue
        rr = runs[arm]
        arm_means = [st.mean(vs) for vs in rr.values()]
        t, df, p = welch_t(arm_means, base_means)
        all_arm = [x for vs in rr.values() for x in vs]
        all_base = [x for vs in runs["off"].values() for x in vs]
        common = sorted(set(rr) & set(runs["off"]))
        paired = [100*(st.mean(rr[r])/st.mean(runs["off"][r])-1) for r in common]
        sig = "**SIGNIFICANT**" if (not math.isnan(p) and p < 0.05) else "n.s."
        print(f"  {arm:10}  {100*(st.mean(all_arm)/st.mean(all_base)-1):+6.3f}%  "
              f"t={t:+6.3f}  df={df:4.1f}  p={p:.4f}  {sig}  "
              f"paired={[round(x, 3) for x in paired]}")

print("\nPIN-VERIFY verdicts (on_full arm):")
if "on_full" in runs:
    for r in sorted(runs["on_full"]):
        d = os.path.join(RUNDIR, f"on_full_r{r}")
        pv = os.path.join(d, "pin_verify_log.txt")
        if os.path.exists(pv):
            lines = [l.strip() for l in open(pv)
                     if "PIN-PREFLIGHT" in l or "PIN-VERIFY" in l]
            for l in lines[:2]:
                print(f"  r{r}: {l}")
        else:
            print(f"  r{r}: (no pin_verify_log.txt)")
