#!/bin/bash
# P27-hotfix4 (Part 2 fix) -- real auto-restart supervisor for alert_engine.py.
#
# Root cause this replaces: every one of this session's 5 "alert_engine.py is
# dead" incidents was the process's own finite --duration expiring exactly as
# designed (confirmed via its clean, fully-computed "total alerts emitted"
# summary, which only prints after run()'s loop returns normally -- not
# reachable from a crash). alert_engine.py now supports --duration<=0 for a
# genuine persistent run, removing that self-inflicted timer entirely.
#
# This wrapper covers the OTHER, real, distinct failure mode Part 2 was asked
# to consider: an actual unexpected exit (an OS-level kill, e.g. an oversized
# process getting reaped, or a top-level unhandled exception outside any
# single check -- [CHECK-FAILED]'s _run_check only guards exceptions INSIDE
# one check, never a crash in poll_once's own surrounding code or a signal).
# If that ever genuinely happens, this loop notices the real exit and
# relaunches within a few seconds, logging it loudly -- instead of relying on
# a human's next manual liveness recheck to notice at all.
set -u
VM_URL="${1:-http://worker-0:8428}"
LOG="${2:-/root/P20c_alerting/alert_engine_supervised.log}"

# P27.3-log-rotation-fix -- real, standard logrotate (see alert_engine_
# supervised.logrotate's own comments for the size/retention/copytruncate
# reasoning), triggered periodically. A real systemd timer or cron would
# normally do this triggering; neither is available in this container
# (confirmed directly: pid 1 is sshd, "running in chroot"; no crontab
# binary) -- this loop is the minimal, standard substitute for that
# missing scheduler, not a reimplementation of rotation itself, which
# logrotate alone still fully owns. 300s: frequent enough that a size
# threshold gets caught promptly under real heavy load, without adding
# meaningful overhead (logrotate's own state-file check is a cheap stat,
# not a real cost, when the file is still under threshold).
if command -v logrotate >/dev/null 2>&1; then
  (
    while true; do
      logrotate --state /root/P20c_alerting/.logrotate_state /root/P20c_alerting/alert_engine_supervised.logrotate 2>>/root/P20c_alerting/logrotate_errors.log
      sleep 300
    done
  ) &
  echo "[supervisor] log-rotation loop started (pid $!), checking every 300s" | tee -a "$LOG"
else
  echo "[supervisor] WARNING: logrotate not found -- alert_engine_supervised.log will grow unbounded" | tee -a "$LOG"
fi

echo "[supervisor] starting, vm_url=$VM_URL log=$LOG" | tee -a "$LOG"
while true; do
  START_TS=$(date -u +%FT%TZ)
  echo "[supervisor] launching alert_engine.py at $START_TS" | tee -a "$LOG"
  python3 /root/P20c_alerting/alert_engine.py "$VM_URL" --duration 0 >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor] alert_engine.py EXITED unexpectedly (--duration 0 means it should never return on its own) exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
