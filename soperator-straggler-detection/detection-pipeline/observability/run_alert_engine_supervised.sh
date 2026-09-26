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
# Stage 2 cluster-topology-agnostic fix: VM_URL's own default and every
# absolute path below used to hardcode this project's original
# development-host layout (http://worker-0:8428, /root/P20c_alerting/...).
# install.sh always passes the real, discovered VM_URL explicitly (see
# its own cluster.env generation) -- the default here is only a
# convenience fallback for manual invocation, not something to rely on.
# Every path is now resolved relative to this script's own real,
# installed location instead.
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"
VM_URL="${1:-http://worker-0:8428}"
LOG="${2:-$_PKG_ROOT/var/alert_engine_supervised.log}"
mkdir -p "$(dirname "$LOG")"

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
  # logrotate's own config format has no shell-variable interpolation --
  # it needs a real, resolved absolute path in the file itself, which
  # depends on where LOG actually ended up. Generate a real config with
  # that real path substituted in, rather than shipping one with this
  # project's original hardcoded path baked in statically.
  _GENERATED_LOGROTATE="$_PKG_ROOT/var/alert_engine_supervised.logrotate.generated"
  sed "s|^/root/P20c_alerting/alert_engine_supervised.log {|$LOG {|" \
    "$_HERE/alert_engine_supervised.logrotate" > "$_GENERATED_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state" "$_GENERATED_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
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
  python3 "$_PKG_ROOT/alerting/alert_engine.py" "$VM_URL" --duration 0 >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor] alert_engine.py EXITED unexpectedly (--duration 0 means it should never return on its own) exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
