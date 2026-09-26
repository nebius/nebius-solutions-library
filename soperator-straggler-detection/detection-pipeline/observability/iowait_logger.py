#!/usr/bin/env python3
"""P26.5-maintenance Part 2 -- persists iowait_agent.bt's real per-PID
block-I/O-wait output to a queryable, real-epoch-timestamped JSON-lines
log per host, so classifier.py's storage-evidence check (storage_
evidence.py) can look up "was rank X's real PID io-bound during this real
time window" AFTER the fact -- the agent's own bpftrace script is left
completely unchanged (it already validated correctly, both in the
original P20j session and re-confirmed this session); this wrapper only
adds the persistence layer that was genuinely missing before, since
build_single_rank_finding's cause-gathering runs against a historical
fault window (or, live, a recent rolling window), not "whatever bpftrace
happens to print at this exact instant" -- the same reason DCGM's own
rolling_buffer.py exists for GPU/host/IB cause metrics.

iowait_agent.bt's own `time("[%H:%M:%S] ")` builtin has no epoch/date --
deliberately NOT touched or reimplemented in bpftrace (out of scope, and
risks the already-validated eBPF logic); this wrapper instead tags each
completed interval block with the real wall-clock time (time.time()) at
the moment the block was fully received from bpftrace's stdout, which is
accurate to within the same ~2s interval granularity the agent itself
already prints at -- sufficient for the window-scale (multi-second)
before/after questions the classifier needs to answer, not for sub-second
correlation.

Usage: iowait_logger.py <hostname> <log_dir> [--duration SECS]
Writes/appends to <log_dir>/<hostname>.jsonl
"""
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time

# Stage 2 cluster-topology-agnostic fix: was a hardcoded absolute path
# into this project's original development-host layout
# (/root/P20d_e2e_validation/storage_ebpf/iowait_agent.bt) -- resolved
# relative to this package's own installed location instead
# (storage-ebpf/iowait_agent.bt, a sibling of observability/ in the
# packaged tree). BPFTRACE stays a real system path -- it's an installed
# system binary (see storage-ebpf/bpftrace-tracefs-wrapper.sh), not
# something this package ships itself.
AGENT_BT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "storage-ebpf", "iowait_agent.bt"))
BPFTRACE = "/usr/local/bin/bpftrace"

# P27.3-log-rotation-fix -- real, confirmed second instance of the same
# unbounded-growth bug class the sustained dry run found in
# alert_engine_supervised.log: this file's own real data log (opened via
# a plain open(path, "a"), held for this process's entire real,
# potentially-days-long lifetime, same as alert_engine.py's supervised
# stdout) had no rotation either. Unlike that file, THIS one is pure
# Python code with no external shell redirect holding a competing file
# descriptor -- Python's own standard logging.handlers.RotatingFileHandler
# is the correct, proven fit here (it owns reopening its own stream after
# rotation internally; no copytruncate/coordination trick needed, unlike
# the shell-redirect case).
#
# maxBytes=10MB: real reasoning -- this project's own real, observed
# write rate is ~0.16 rows/sec under normal/idle conditions (~592
# rows/hour, measured directly from this session's own real log: 9338
# rows over a real 56746s span), but rises to ~3.5 rows/sec during an
# ACTIVE real storage fault (measured directly: 209 real rows in one 60s
# query window during a real disk-bound fault test) -- at ~117 bytes/row
# observed, that's ~410 B/s in the worst real case, so 10MB gives ~6.75
# real hours of headroom even under SUSTAINED heavy fault conditions,
# comfortably longer than any single real fault this project has ever
# actually run, while staying small under normal operation (~592
# rows/hour x ~117B = ~69KB/hour, so 10MB would otherwise take ~145 real
# hours to fill on its own).
# backupCount=10: same total-disk-bound reasoning as alert_engine_
# supervised.log's own rotate=10 (~100MB worst case for this file,
# consistent across both fixes rather than picking a second, unrelated
# number).
IOWAIT_LOG_MAX_BYTES = 10 * 1024 * 1024
IOWAIT_LOG_BACKUP_COUNT = 10

TS_LINE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")
MAP_LINE_RE = re.compile(r"^@(iowait_us_by_pid|iowait_count_by_pid)\[(\d+),\s*([^\]]*)\]:\s*(\d+)")


def _make_rotating_logger(log_path):
    """Real, standard Python logging.handlers.RotatingFileHandler --
    see IOWAIT_LOG_MAX_BYTES/BACKUP_COUNT's own comment for the real
    sizing reasoning. Bare formatter (message only, no level/timestamp
    prefix logging would normally add) so rotated files stay genuinely
    valid JSON-lines -- this log's own real epoch `ts` field, already
    present in every record, is the timestamp; a second, logging-module-
    added one would be redundant and would break line-for-line JSON
    parsing for any consumer expecting exactly one JSON object per
    line."""
    logger = logging.getLogger(f"iowait_logger.{log_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=IOWAIT_LOG_MAX_BYTES, backupCount=IOWAIT_LOG_BACKUP_COUNT)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _flush_block(logger, hostname, block_ts, iowait_us, iowait_count):
    """One JSON line per (pid, comm) observed in this completed interval
    block -- both stats merged into a single record (they're always
    printed for the exact same key set within one block, by construction
    of the .bt script's own two parallel maps)."""
    for key in set(iowait_us) | set(iowait_count):
        pid, comm = key
        rec = {
            "ts": block_ts,
            "host": hostname,
            "pid": pid,
            "comm": comm,
            "iowait_us": iowait_us.get(key, 0),
            "count": iowait_count.get(key, 0),
        }
        logger.info(json.dumps(rec))


def run(hostname, log_dir, duration=None):
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{hostname}.jsonl")
    logger = _make_rotating_logger(log_path)
    proc = subprocess.Popen([BPFTRACE, AGENT_BT], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    start = time.time()
    iowait_us, iowait_count = {}, {}
    have_block = False
    for line in proc.stdout:
        line = line.rstrip("\n")
        if duration is not None and (time.time() - start) > duration:
            proc.terminate()
            break
        if TS_LINE_RE.match(line):
            # A new interval block is starting -- flush whatever the
            # PREVIOUS block accumulated (real receipt-time as ts),
            # then reset for the new one.
            if have_block:
                _flush_block(logger, hostname, time.time(), iowait_us, iowait_count)
            iowait_us, iowait_count = {}, {}
            have_block = True
            continue
        m = MAP_LINE_RE.match(line)
        if m:
            kind, pid, comm, val = m.groups()
            key = (int(pid), comm)
            if kind == "iowait_us_by_pid":
                iowait_us[key] = int(val)
            else:
                iowait_count[key] = int(val)
    # END block / process exit -- flush whatever's left.
    if have_block:
        _flush_block(logger, hostname, time.time(), iowait_us, iowait_count)
    return proc.wait()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: iowait_logger.py <hostname> <log_dir> [--duration SECS]", file=sys.stderr)
        sys.exit(1)
    hostname, log_dir = sys.argv[1], sys.argv[2]
    duration = None
    if "--duration" in sys.argv:
        duration = float(sys.argv[sys.argv.index("--duration") + 1])
    sys.exit(run(hostname, log_dir, duration))
