#!/usr/bin/env python3
"""P20c Step 3 -- RAS-based fail-stop alert path, separate from the
classifier's fail-slow path (different failure class: RAS observes
whether ranks are still alive/progressing at all, not whether a live rank
is running measurably slower than its peers).

Timing-constraint finding (validated live, not assumed): `ncclras --help`
advertises `-m/--monitor[=GROUPS]` ("continuously watch for peer changes",
GROUPS: lifecycle/trace/all) -- a genuine event-driven mode, not just
polling. Verified against a real job: running `ncclras -m lifecycle` in
the background and then SIGKILLing one rank's training process produced
"Connection closed by the NCCL job." within ~8s, with NO polling interval
to miss -- the monitor process is itself a live listener on the job's RAS
socket. This directly addresses the P20c-prerequisite finding that a
polling-based check can miss a job that crashes or completes fast.

Real, honestly-scoped limitation found in the same test: the monitor
process exits once the socket closes (it does not persist across job
restarts), and its message does not attribute WHICH rank triggered the
closure -- killing rank 4 produced the exact same generic "Connection
closed by the NCCL job." that a clean job completion would produce. Rank
attribution, when available, actually comes from torchrun's own elastic-
agent crash log (a different, existing signal, not something RAS exposes)
-- this module treats the last MISMATCH snapshot (via `ncclras -v`,
excluded per the positional/structural rule below) as a best-effort,
explicitly-caveated hint only, never as authoritative attribution.

Design: one ncclras -m lifecycle subprocess per job, supervised. Its exit
(clean "Connection closed" message, OR the subprocess dying) is the
fail-stop trigger. A `ncclras -v` snapshot taken just before/at trigger
time supplies best-effort context.
"""
import subprocess
import re
import sys
import time

sys.path.insert(0, "/root/P20c_alerting")
import health_exclusions  # noqa: E402

MISMATCH_RANK_RE = re.compile(r"^\s*Rank (\d+) (?:--|has launched)")


def parse_mismatch_ranks(ras_v_output):
    """Extracts every rank named in a MISMATCH warning block (the
    'behind' or 'ahead' groups), regardless of grouping -- exclusion
    filtering happens separately in exclude_known_benign_ranks()."""
    if "MISMATCH" not in ras_v_output:
        return []
    return [int(m.group(1)) for m in MISMATCH_RANK_RE.finditer(ras_v_output)]


def exclude_known_benign_ranks(ranks, coordinator_rank, degraded_ranks):
    """Mechanism-based filter -- NOT a hardcoded rank list. coordinator_rank
    is computed fresh from job topology (health_exclusions.
    rendezvous_coordinator_rank); degraded_ranks is computed fresh from a
    live TFLOPS health-check (health_exclusions.degraded_gpus_live), not a
    remembered/stale list."""
    excluded = {coordinator_rank} | degraded_ranks
    return [r for r in ranks if r not in excluded]


def query_ras_snapshot(host="worker-0"):
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "/usr/bin/ncclras -v 2>&1"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout


def compute_current_exclusions(local_ranks_per_host, coordinator_host="worker-0",
                                hosts=("worker-0", "worker-1")):
    coordinator_rank = health_exclusions.rendezvous_coordinator_rank(
        node_rank_of_host=None, local_ranks_per_host=local_ranks_per_host,
        coordinator_host=coordinator_host,
    )
    degraded_by_host = health_exclusions.degraded_gpus_live(hosts=hosts)
    degraded_ranks = set()
    for global_rank, (host, local_rank) in local_ranks_per_host.items():
        flagged = degraded_by_host.get(host) or set()
        if local_rank in flagged:
            degraded_ranks.add(global_rank)
    return coordinator_rank, degraded_ranks


class RASFailStopWatcher:
    """Launches and supervises one `ncclras -m lifecycle` subprocess for
    the current job. .wait_for_event(timeout) blocks until the monitor
    reports closure or dies on its own; returns a dict describing what
    was observed, including a best-effort (never authoritative) rank hint
    from the last `ncclras -v` snapshot, with the known-benign ranks
    already excluded via the mechanism-based rule.
    """

    def __init__(self, host="worker-0", local_ranks_per_host=None):
        self.host = host
        self.local_ranks_per_host = local_ranks_per_host or {}
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host,
             "/usr/bin/ncclras -m lifecycle 2>&1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        return self.proc

    def wait_for_event(self, timeout=None):
        assert self.proc is not None, "call start() first"
        t0 = time.time()
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if line:
                lines.append(line.rstrip("\n"))
                if "Connection closed" in line or "connection closed" in line.lower():
                    break
            elif self.proc.poll() is not None:
                break  # subprocess died on its own
            if timeout is not None and time.time() - t0 > timeout:
                return {"event": "timeout", "lines": lines}

        snapshot = query_ras_snapshot(self.host)
        raw_ranks = parse_mismatch_ranks(snapshot)
        coordinator_rank, degraded_ranks = compute_current_exclusions(self.local_ranks_per_host)
        candidate_ranks = exclude_known_benign_ranks(raw_ranks, coordinator_rank, degraded_ranks)
        return {
            "event": "connection_closed",
            "lines": lines,
            "last_snapshot_raw_mismatch_ranks": raw_ranks,
            "excluded_coordinator_rank": coordinator_rank,
            "excluded_degraded_ranks": sorted(degraded_ranks),
            "candidate_ranks_after_exclusion": candidate_ranks,
            "caveat": (
                "This rank list is a best-effort hint from the LAST snapshot before "
                "closure, not authoritative attribution -- RAS's own lifecycle event "
                "does not name which rank triggered it. Cross-reference against the "
                "job's own torchrun/elastic-agent crash log (rank/exitcode) when available; "
                "that log, not RAS, is the authoritative source for which rank actually failed."
            ),
        }


def format_ras_alert(event, host, job_id=None):
    """Distinct format/severity from fail-slow alerts (different failure
    class -- a rank/job that stopped responding entirely, not one running
    measurably slower)."""
    header = f"[ALERT] type=fail-stop node={host} job={job_id or '?'} severity=CRITICAL"
    lines = [header, "", "RAS reported the job's RAS connection closed "
             "(job crashed, a rank died, or the job completed)."]
    lines.append("")
    if event.get("candidate_ranks_after_exclusion"):
        lines.append(f"Best-effort rank hint (post-exclusion): "
                      f"{event['candidate_ranks_after_exclusion']}")
    else:
        lines.append("No non-excluded rank showed lag in the last snapshot before "
                      "closure -- no rank hint available from RAS.")
    lines.append(f"Excluded (rendezvous coordinator, mechanism-based): "
                 f"rank {event.get('excluded_coordinator_rank')}")
    lines.append(f"Excluded (live-confirmed degraded GPU, mechanism-based): "
                 f"ranks {event.get('excluded_degraded_ranks')}")
    lines.append("")
    lines.append(event["caveat"])
    return "\n".join(lines)
