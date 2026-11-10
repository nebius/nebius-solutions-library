#!/bin/bash
# Stage 3 -- real auto-restart supervisor for node_aggregator_ref.py, one
# per real discovered node, mirroring run_alert_engine_supervised.sh's own
# already-established supervision/log-rotation pattern exactly.
#
# Real, disclosed discrepancy found this session: alert_engine.py's own
# run() explicitly documents "duration_s <= 0 means run forever"
# (P27-hotfix4). node_aggregator_ref.py's run() has NO such special case
# -- `t_end = time.time() + duration_s` unconditionally, so --duration 0
# (or any non-positive value) means "run essentially zero time," not
# forever. There is no native persistent mode to fall back on here, so
# this wrapper launches it with a real, large-but-finite duration (see
# AGG_DURATION_S below) and, exactly like the alert_engine supervisor,
# relaunches it if it ever does exit (whether from that duration
# elapsing after a very long time, a crash, or anything else) --
# indistinguishable from "genuinely persistent" for any real deployment.
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"
DUMP_DIR="${1:?usage: run_aggregator_supervised.sh <dump_dir> <hostname> <vm_url> [log_file]}"
HOSTNAME_ARG="${2:?usage: run_aggregator_supervised.sh <dump_dir> <hostname> <vm_url> [log_file]}"
# Real bug found live (this session): cluster.env's VM_URL is the bare
# base URL (e.g. http://worker-0:8428) -- correct as-is for alert_engine.py
# and for a plain /health probe, both of which this project already uses
# it for. node_aggregator_ref.py's own flush() POSTs directly to whatever
# vm_url it's given with NO path appended -- it needs the real VictoriaMetrics
# ingestion endpoint, not the bare base (confirmed live: every push failed
# with HTTP 400 against the bare URL; the exact same POST against
# <base>/api/v1/import/prometheus returned 204). Appended here, not in
# cluster.env itself, so cluster.env's VM_URL stays correct for its other,
# already-working bare-URL uses.
VM_URL_BASE="${3:?usage: run_aggregator_supervised.sh <dump_dir> <hostname> <vm_url> [log_file]}"
VM_URL="${VM_URL_BASE%/}/api/v1/import/prometheus"
LOG="${4:-$_PKG_ROOT/var/aggregator_logs/$HOSTNAME_ARG.log}"
mkdir -p "$DUMP_DIR" "$(dirname "$LOG")"

# 10 years -- a real, finite value (see this file's own top comment for
# why a genuine "forever" mode doesn't exist here), effectively unbounded
# for any real job/cluster lifetime; the supervising loop below still
# relaunches it if it ever does return, so this number is not load-
# bearing for correctness, only for how rarely the loop below fires.
AGG_DURATION_S=315360000

if command -v logrotate >/dev/null 2>&1; then
  _GENERATED_LOGROTATE="$_PKG_ROOT/var/aggregator_supervised_$HOSTNAME_ARG.logrotate.generated"
  sed "s|^/root/P20c_alerting/aggregator_supervised.log {|$LOG {|" \
    "$_HERE/aggregator_supervised.logrotate" > "$_GENERATED_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state_$HOSTNAME_ARG" "$_GENERATED_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
      sleep 300
    done
  ) &
  echo "[supervisor:$HOSTNAME_ARG] log-rotation loop started (pid $!), checking every 300s" | tee -a "$LOG"
else
  echo "[supervisor:$HOSTNAME_ARG] WARNING: logrotate not found -- $LOG will grow unbounded" | tee -a "$LOG"
fi

# Real bug found live (this session): logrotate rejects a relative glob
# path outright ("keyword 'var' not properly separated") -- DUMP_DIR is
# passed in relative (e.g. "var/dump/worker-0") by run.sh, unlike $LOG
# above which is always already absolute ($_PKG_ROOT/var/...). Resolved
# here, rather than assuming the caller passes an absolute path.
# mkdir -p "$DUMP_DIR" above guarantees this cd succeeds. Computed
# unconditionally (moved out of the logrotate-only block below) because
# the orphan-cleanup loop further down needs it too and does not itself
# depend on logrotate being installed.
DUMP_DIR_ABS="$(cd "$DUMP_DIR" && pwd)"

# S6.42-dump-disk-fix -- separate rotation for the Inspector plugin's own
# raw dump files in $DUMP_DIR itself (previously never rotated at all --
# see dump_files.logrotate's own comment for the real incident and the
# real measured growth rate this is sized against). A SEPARATE, faster
# loop than the aggregator-log rotation above: dump files grow roughly
# 17x faster under real fault-trial load than the volume that justified
# the 300s loop above, so reusing that same slower cadence here would
# leave real, substantial overshoot before ever catching it.
if command -v logrotate >/dev/null 2>&1; then
  _GENERATED_DUMP_LOGROTATE="$_PKG_ROOT/var/dump_files_$HOSTNAME_ARG.logrotate.generated"
  sed "s|^__DUMP_GLOB__ {|$DUMP_DIR_ABS/*.log {|" \
    "$_HERE/dump_files.logrotate" > "$_GENERATED_DUMP_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state_dump_$HOSTNAME_ARG" "$_GENERATED_DUMP_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
      sleep 60
    done
  ) &
  echo "[supervisor:$HOSTNAME_ARG] dump-file rotation loop started (pid $!) for $DUMP_DIR, checking every 60s" | tee -a "$LOG"
else
  echo "[supervisor:$HOSTNAME_ARG] WARNING: logrotate not found -- $DUMP_DIR will grow unbounded" | tee -a "$LOG"
fi

# Capstone gap-fix #2 (2026-11): the rotation above only bounds the SIZE
# of a single still-growing dump file. It does nothing for files left
# behind by a job whose owning process has already exited -- Inspector
# names each dump file by PID (inspector-plugin/inspector.cc's own
# "%s-pid%d.log"; Option D's app-timing dumps follow the same convention,
# "apptiming-%s-pid%d.jsonl"), and a dead process's file never grows
# again, so logrotate's size trigger never touches it. These just
# accumulate, one set of files per job run, forever -- found live
# (2026-11, capstone validation): a single real worker-0 dump dir held
# 600 such files from past, long-dead jobs. This sweep removes a dump
# file only when BOTH (a) its owning PID is confirmed dead ("kill -0"
# fails -- a false "still alive" on a reused PID just means we wait
# longer, never an unsafe early delete) AND (b) its mtime is older than
# ORPHAN_GRACE_S, which exists so a job that just exited still gives
# node_aggregator_ref.py's own poll_files() time to drain it to EOF, and
# gives a human a window to grab a raw file for an immediate postmortem,
# before it is ever removed. By the time this sweep would delete
# anything, VM already holds this data durably (that is the entire point
# of the aggregator pipeline), so this never removes data this
# project's own detection path still depends on. Does not depend on
# logrotate being installed -- runs unconditionally.
ORPHAN_GRACE_S=600
(
  while true; do
    sleep 60
    now_s=$(date +%s)
    shopt -s nullglob
    for f in "$DUMP_DIR_ABS"/*-pid[0-9]*.log "$DUMP_DIR_ABS"/*-pid[0-9]*.jsonl; do
      [ -e "$f" ] || continue
      pid="$(basename "$f" | sed -n 's/.*-pid\([0-9][0-9]*\)\.[a-zA-Z]*$/\1/p')"
      [ -n "$pid" ] || continue
      kill -0 "$pid" 2>/dev/null && continue   # owning process still alive -- never touch
      mtime=$(stat -c %Y "$f" 2>/dev/null) || continue
      age=$(( now_s - mtime ))
      if [ "$age" -ge "$ORPHAN_GRACE_S" ]; then
        sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
        rm -f "$f"
        echo "[supervisor:$HOSTNAME_ARG] [ORPHAN-DUMP-CLEANUP] removed $f (dead pid=$pid, age=${age}s, size=${sz}B)" | tee -a "$LOG"
      fi
    done
  done
) &
echo "[supervisor:$HOSTNAME_ARG] orphaned dump-file cleanup loop started (pid $!) for $DUMP_DIR, checking every 60s, grace=${ORPHAN_GRACE_S}s" | tee -a "$LOG"

echo "[supervisor:$HOSTNAME_ARG] starting, dump_dir=$DUMP_DIR vm_url=$VM_URL log=$LOG" | tee -a "$LOG"
while true; do
  START_TS=$(date -u +%FT%TZ)
  echo "[supervisor:$HOSTNAME_ARG] launching node_aggregator_ref.py at $START_TS" | tee -a "$LOG"
  python3 "$_PKG_ROOT/aggregator/node_aggregator_ref.py" "$DUMP_DIR" "$HOSTNAME_ARG" "$VM_URL" --duration "$AGG_DURATION_S" >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor:$HOSTNAME_ARG] node_aggregator_ref.py EXITED exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
