#!/usr/bin/env python3
"""analyze_ab_k8s.py TAG -- K8s counterpart to analyze_ab.py for the nanoGPT A/B runs.

Reads iters.txt (one ms/iter float per line) written by ab_driver_k8s.sh,
drops WARM leading warmup iterations, computes per-round mean, then:
  - per-arm summary table (mean, sd across rounds, median)
  - Welch two-sided t-test of each arm vs 'off' on per-round means
  - per-round paired overhead %
  - CPU utilization from cpu_*.jsonl if present (on_full arm only)
  - PIN-VERIFY verdict from pin_verify_log.txt (on_full arm only)

Parity with analyze_ab.py (Slurm):
  SAME: Welch t-test (scipy.stats.ttest_ind, equal_var=False), WARM=20 skip,
        per-round mean, overhead reported as % relative to 'off' arm
  DIFFERENT: log format is nanoGPT "time Nms" (not Megatron "elapsed time per
             iteration (ms): N"); contamination check uses kubectl (not sacct);
             CPU utilization parsing uses RESERVED_CPU_RANGE from pin_verify_log.txt
             (not hardcoded cpus 48-63,112-127 from the Slurm cluster's topology).
"""
import glob, json, os, re, statistics as st, subprocess, sys
from scipy import stats

TAG = sys.argv[1]; WARM = 1  # nanoGPT logs every log_interval=20 iters; first record is CUDA/NCCL init spike
RUNDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "../../var/ab_k8s", TAG)

ARMS = ["off", "on_nopin", "on_full"]


def load_iters(d):
    f = os.path.join(d, "iters.txt")
    if not os.path.exists(f):
        return []
    with open(f) as fh:
        vals = []
        for l in fh:
            l = l.strip()
            if l:
                try:
                    vals.append(float(l))
                except ValueError:
                    pass
    return vals


def contaminated(d):
    """Check if another training job was running during this window (kubectl-based)."""
    try:
        t_start = int(open(os.path.join(d, "t_start")).read().strip())
        t_end   = int(open(os.path.join(d, "t_end"  )).read().strip())
        job_name = open(os.path.join(d, "job_name")).read().strip()
        # List all training jobs in the namespace that were active during the window.
        out = subprocess.run(
            ["kubectl", "get", "jobs", "-n", "straggler-detection",
             "-l", "straggler.nebius.ai/component=training",
             "-o", "jsonpath={range .items[*]}{.metadata.name},{.metadata.creationTimestamp}\\n{end}"],
            capture_output=True, text=True, timeout=15).stdout
        overlapping = []
        for line in out.strip().splitlines():
            if not line:
                continue
            parts = line.split(",", 1)
            if len(parts) < 2:
                continue
            jname, ts = parts
            if jname == job_name:
                continue
            overlapping.append(jname)
        return overlapping if overlapping else None
    except Exception:
        return None


def parse_reserved_cpus(d):
    """Extract reserved CPU list from pin_verify_log.txt (on_full arm)."""
    f = os.path.join(d, "pin_verify_log.txt")
    if not os.path.exists(f):
        return set()
    with open(f) as fh:
        for l in fh:
            m = re.search(r"reserved_cpus=([\d,\-]+)", l)
            if m:
                cpus = set()
                for part in m.group(1).split(","):
                    part = part.strip()
                    if "-" in part:
                        a, b = part.split("-")
                        cpus |= set(range(int(a), int(b)+1))
                    elif part:
                        cpus.add(int(part))
                return cpus
    return set()


# Collect all data
runs = {}  # arm -> {round -> {mean, median, n, cont}}
for arm in ARMS:
    for d in sorted(glob.glob(os.path.join(RUNDIR, f"{arm}_r*"))):
        m = re.search(rf"{re.escape(arm)}_r(\d+)$", d)
        if not m:
            continue
        r = int(m.group(1))
        vals = load_iters(d)
        if len(vals) <= WARM:
            print(f"SKIP {d}: only {len(vals)} iterations (need >{WARM})")
            continue
        steady = vals[WARM:]
        runs.setdefault(arm, {})[r] = {
            "mean": st.mean(steady),
            "median": st.median(steady),
            "n": len(steady),
            "cont": contaminated(d),
            "dir": d,
        }

# Summary table
print(f"\n{'arm':10} {'rounds':>6} {'iters/r':>7} {'mean ms':>9} {'sd(rounds)':>10} {'median ms':>10}  contaminated")
for arm in ARMS:
    if arm not in runs:
        print(f"{arm:10}  (no data)")
        continue
    rr = runs[arm]
    ms = [x["mean"] for x in rr.values()]
    cont = [r for r, x in rr.items() if x["cont"]]
    print(f"{arm:10} {len(rr):>6} {rr[min(rr)]['n']:>7} {st.mean(ms):9.2f} "
          f"{st.pstdev(ms) if len(ms)>1 else 0:10.2f} "
          f"{st.mean(x['median'] for x in rr.values()):10.2f}  {cont if cont else ''}")

# Overhead vs OFF
if "off" in runs:
    base = [x["mean"] for x in runs["off"].values()]
    print("\nOverhead vs OFF (per-round means; Welch two-sided; paired per-round in brackets):")
    for arm in ARMS:
        if arm == "off" or arm not in runs:
            continue
        rr = runs[arm]
        ms = [x["mean"] for x in rr.values()]
        t, p = stats.ttest_ind(ms, base, equal_var=False)
        common_rounds = sorted(set(rr) & set(runs["off"]))
        paired = [100 * (rr[r]["mean"] / runs["off"][r]["mean"] - 1) for r in common_rounds]
        print(f"  {arm:9} overhead {100*(st.mean(ms)/st.mean(base)-1):+6.2f}%  "
              f"Welch t={t:6.2f} p={p:.4f}  n={len(ms)}/{len(base)}  "
              f"paired %: {[round(x, 2) for x in paired]}")

# PIN-VERIFY verdicts (on_full arm)
if "on_full" in runs:
    print("\nPIN-VERIFY verdicts (on_full arm):")
    for r, x in sorted(runs["on_full"].items()):
        f = os.path.join(x["dir"], "pin_verify_log.txt")
        if os.path.exists(f):
            verdict_lines = [l.strip() for l in open(f) if "PIN-VERIFY" in l and "verdict=" in l]
            if verdict_lines:
                for vl in verdict_lines[:4]:
                    print(f"  r{r}: {vl}")
            else:
                print(f"  r{r}: (no PIN-VERIFY verdict line found in log)")
        else:
            print(f"  r{r}: (no pin_verify_log.txt)")

# CPU utilization (on_full arm, cpu_sampler.py output)
print("\nCPU utilization (on_full arm, 1Hz sampler, steady-state = middle 60%):")
if "on_full" in runs:
    for r, x in sorted(runs["on_full"].items()):
        reserved = parse_reserved_cpus(x["dir"])
        for f in sorted(glob.glob(os.path.join(x["dir"], "cpu_*.jsonl"))):
            pod = os.path.basename(f).replace(".jsonl", "")
            rows = []
            with open(f) as fh:
                for l in fh:
                    try:
                        rows.append(json.loads(l)["busy"])
                    except Exception:
                        pass
            n = len(rows)
            if n < 5:
                print(f"  r{r} {pod}: only {n} samples")
                continue
            rows = rows[int(n*0.2):int(n*0.8)]
            avg = {int(c): st.mean(row[c] for row in rows) for c in rows[0]}
            if reserved:
                res_in_data = [c for c in avg if c in reserved]
                train_cpus = [c for c in avg if c not in reserved]
                if res_in_data:
                    print(f"  r{r} {pod}: train-cores mean {st.mean(avg[c] for c in train_cpus):5.1f}%  "
                          f"| reserved-cores mean {st.mean(avg[c] for c in res_in_data):5.1f}%  "
                          f"max-core {max(avg[c] for c in res_in_data):5.1f}%")
                else:
                    print(f"  r{r} {pod}: reserved CPUs {sorted(reserved)} not in sampler data")
            else:
                print(f"  r{r} {pod}: {n} samples (no reserved CPU info, raw mean all CPUs: "
                      f"{st.mean(avg.values()):.1f}%)")
            break  # one pod per round is enough
