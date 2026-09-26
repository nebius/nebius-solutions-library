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
