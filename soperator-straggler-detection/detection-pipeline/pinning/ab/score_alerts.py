#!/usr/bin/env python3
"""score_alerts.py RUN [TRUTH_PID] -- score one run's alert_engine_slice.log against ground truth.
Counts EVERY alert header type ([ALERT], [WAIT-INDUCED-ALERT], [ROLE-BASELINE-ALERT-UNVALIDATED], ...)
by whether `rank=` (an OS PID, not the training rank) equals the injected rank's pid. The primary
culprit signal is [WAIT-INDUCED-ALERT]; everything else is reported as noise/secondary."""
import re, collections, sys
RUNS = "/root/inspector-audit/runs"
def score(run, truth=None):
    c = collections.Counter()
    for l in open(f"{RUNS}/{run}/alert_engine_slice.log", errors="ignore"):
        m = re.match(r"^\[((?:[A-Z]+-)*ALERT(?:-[A-Z]+)*)\] (.*)", l)
        if not m: continue
        typ, rest = m[1], m[2]
        if "scope=cluster" in rest: c[(typ, "cluster-scope", "-")] += 1; continue
        r = re.search(r"rank=(\d+)", rest); conf = re.search(r"confidence=(\w+)", rest)
        c[(typ, "TRUTH" if (truth and r and r[1] == truth) else "other", conf[1] if conf else "-")] += 1
    return c
if __name__ == "__main__":
    run = sys.argv[1]; t = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] not in ("", "UNKNOWN") else None
    c = score(run, t)
    wt = sum(v for k, v in c.items() if k[0] == "WAIT-INDUCED-ALERT" and k[1] == "TRUTH")
    wo = sum(v for k, v in c.items() if k[0] == "WAIT-INDUCED-ALERT" and k[1] == "other")
    print(f"WAIT-INDUCED-ALERT: truth={wt} wrong-rank={wo}")
    for k, v in sorted(c.items()): print(f"  {v:3d} {k}")
