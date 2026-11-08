#!/usr/bin/env python3
"""readable_alerts.py -- operator-friendly rendering of alert_engine's output (presentation only).

Reads the engine's detailed log (default var/alert_engine_supervised.log, or any slice of it) and,
optionally, var/alert_summary.log for timestamps. It NEVER changes detection, alert text, or any
file the engine/self_test parse; it only prints a plain-language view with units, alerts grouped by
communicator, and a one-line verdict.

  tools/readable_alerts.py [--log FILE] [--summary FILE] [--since ISO8601]

Reading guide (what the raw fields mean):
  pid=<N>       OS PROCESS ID of the training process on that node -- NOT the training rank
                (older log lines from a not-yet-restarted engine instance may still say rank=;
                same value, same meaning). Pids repeat across nodes/runs; always read it with node=.
  gpu_slot      GPU index on that node.          comm=0x..  an NCCL communicator (a group of ranks).
  role_rank/n   this member's position / size within its communicator's role (na = not resolved) --
                scoped to the comm= on the SAME line: the same pid can legitimately show a
                different role_rank/role_n under a different comm= (it belongs to multiple real
                communicators at once), never a contradiction or a bug.
  confidence    CONFIRMED (independent cause corroborated) > PROBABLE > UNCONFIRMED. LOG-ONLY never pages.
  Times are microseconds in the raw log; shown here as us/ms."""
import argparse, collections, re, sys, os

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HDR = re.compile(r"^\[((?:[A-Z]+-)*(?:ALERT|INCIDENT)(?:-[A-Z]+)*)\] (.*)")

def us(v):
    v = float(v); return f"{v/1000:.2f} ms" if v >= 1000 else f"{v:.0f} us"

def kv(s): return dict(re.findall(r"(\w+)=(\S+)", s))

def blocks(path):
    cur = None
    for l in open(path, errors="ignore"):
        l = l.rstrip("\n")
        m = HDR.match(l)
        if m and m[1].endswith("ALERT") or (m and "ALERT" in m[1]):
            if cur: yield cur
            cur = {"type": m[1], "head": l, "f": kv(m[2]), "body": []}
        elif cur is not None:
            if l.startswith("====="): yield cur; cur = None
            else: cur["body"].append(l)
    if cur: yield cur

def describe(b):
    f, body = b["f"], "\n".join(b["body"])
    t = b["type"]
    if "scope" in f:
        return f"cluster-wide {f.get('type','?')} signal (not tied to one rank)", "", "cluster-scope"
    if t == "WAIT-INDUCED-ALERT":
        own = re.search(r"ratio=([\d.]+)x its own real historical baseline, baseline_us=(\d+)", body)
        peers = re.findall(r"member=(\d+) host=(\S+) slot=(\d+).*?ratio=([\d.]+) current_mean_us=(\d+) \(baseline_us=(\d+)", body)
        why = (f"{len(peers)} peer(s) in the same {f.get('role_n','?')}-GPU group are waiting on it: "
               + ", ".join(f"GPU {p[2]} {us(p[4])} vs {us(p[5])} normal ({float(p[3]):.1f}x)" for p in peers[:4])
               + (f"; its own time is normal ({float(own[1]):.2f}x its baseline of {us(own[2])}), i.e. it arrives last" if own else ""))
        return "STRAGGLER CULPRIT (peers are waiting on this rank)", why, "culprit"
    if t.startswith("ROLE-BASELINE"):
        m = re.search(r"current_mean_us=(\d+)\) is ([\d.]+)x its OWN real historical baseline.*?baseline_us=(\d+)", body)
        why = f"its time {us(m[1])} is {float(m[2]):.1f}x its own historical {us(m[3])} for this role" if m else "deviates from its own historical baseline for this role"
        return "slow vs its own history" + (" (role not validated for this workload shape)" if "UNVALIDATED" in t else ""), why, "history"
    if t == "STRAGGLER-INCIDENT":
        return "incident record", "", "incident"
    m = re.search(r"exec-time mean is ([\d.]+)x its node peers.*?z=([\d.]+)", body)
    own = re.search(r"own real exec time: ([\d.]+)us vs\. peer reference: ([\d.]+)us", body)
    if m and own:
        return "deviates from its peers in the same group", (f"windowed mean is {float(m[1]):.1f}x its peers' (z={float(m[2]):.0f}); "
               f"latest single sample {us(own[1])} vs peers {us(own[2])}" + (" -- the latest sample alone is not slower; the windowed statistic reflects earlier readings" if float(own[1]) < float(own[2]) else "")), "peer"
    m = re.search(r"member=(\d+) \(real GPU slot (\d+), host=(\S+)\) is flagged .*?partner.*?member=(\d+) host=(\S+) slot=(\d+) ratio=([\d.]+) current_mean_us=(\d+) \(peer_median_us=(\d+)", body, re.S)
    if m:
        return "2-member pair asymmetry", f"its only partner (GPU {m[6]} on {m[5]}) runs {us(m[8])} vs normal {us(m[9])} ({float(m[7]):.1f}x) -> this side may be the slow one", "pair"
    return "timing deviation", "", "other"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=f"{PKG}/var/alert_engine_supervised.log")
    ap.add_argument("--summary", default=f"{PKG}/var/alert_summary.log")
    ap.add_argument("--since", default="")
    a = ap.parse_args()
    ts = collections.defaultdict(list)
    if os.path.exists(a.summary):
        for l in open(a.summary, errors="ignore"):
            m = re.match(r"^(\S+Z) (\[.*)", l.rstrip("\n"))
            if m: ts[m[2]].append(m[1])
    groups = collections.OrderedDict(); counts = collections.Counter(); culprits = []
    for b in blocks(a.log):
        if b["type"] == "STRAGGLER-INCIDENT": continue
        when = (ts[b["head"]].pop(0) if ts.get(b["head"]) else "")
        if a.since and when and when < a.since: continue
        what, why, kind = describe(b); f = b["f"]
        counts[kind] += 1
        # Real bug found live (independent item-4 re-review, 2026-11): hardcoded
        # f.get('rank', ...) -- the OLD field name, before the rank=->pid= rename
        # (08d7fa01). kv() stores whatever key literally appears on the line, so a
        # current-format line (pid=) was silently rendering "(pid ?)" here, never
        # caught because every log this tool had been tested against so far
        # happened to come from a stale, not-yet-restarted engine instance still
        # emitting the old rank= name. Accepts either key; real current-format
        # lines use pid=, rank= is kept as a fallback for old pasted/stale lines.
        who = "cluster" if kind == "cluster-scope" else f"{f.get('node','?')} GPU {f.get('gpu_slot','?')} (pid {f.get('pid', f.get('rank','?'))})"
        key = f.get("comm", "cluster")
        groups.setdefault(key, []).append((when, f.get("confidence", "-"), f.get("severity", "-"), kind, who, what, why, f))
        if kind == "culprit": culprits.append(who)
    print("ALERTS, grouped by communicator (pid= in raw logs is an OS pid, not the training rank)\n")
    for comm, rows in groups.items():
        print(f"communicator {comm if comm != 'cluster' else '(cluster-wide)'}")
        for when, conf, sev, kind, who, what, why, f in rows:
            print(f"  {when or '--:--':<21} {conf:<11} {sev:<9} {who}\n      -> {what}" + (f"\n      why: {why}" if why else ""))
        print()
    n = sum(counts.values())
    print("VERDICT:", (f"{len(set(culprits))} probable straggler culprit(s): " + "; ".join(sorted(set(culprits))) + ". " if culprits else "no straggler culprit identified. ")
          + f"{n - len(culprits)} other lower-confidence finding(s) (history/peer/pair deviations and cluster signals) -- all LOG-ONLY, none page.")

if __name__ == "__main__": main()
