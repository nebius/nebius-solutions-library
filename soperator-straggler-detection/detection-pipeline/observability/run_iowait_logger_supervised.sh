#!/bin/bash
# V1-beta P0 fix -- real auto-restart supervisor for iowait_logger.py,
# mirroring run_alert_engine_supervised.sh's own already-established
# supervision/log-rotation pattern exactly.
#
# Real, confirmed-live gap this closes: iowait_logger.py (the real eBPF
# io-wait data producer Path C storage-fault detection depends on --
# alert_engine.py's own IOWAIT_LOG_DIR already expects its output) had NO
# launcher anywhere in this project's install.sh/run.sh -- unlike every
# other process this pipeline depends on (the aggregator, alert_engine.py
# itself, VictoriaMetrics, Grafana), nothing ever started it. This means
# Path C was silently non-functional on every real deployment regardless
# of whether bpftrace/tracefs itself worked, simply because the producer
# process never ran. This is the one place that actually brings it up,
# same auto-restart discipline as every other supervised process here.
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"
HOSTNAME_ARG="${1:?usage: run_iowait_logger_supervised.sh <hostname> [iowait_log_dir] [supervisor_log_file]}"
# Same default IOWAIT_LOG_DIR convention alert_engine.py's own
# IOWAIT_LOG_DIR already uses (os.path.join(_PKG_ROOT, "var",
# "iowait_logs")) -- must match exactly, or alert_engine.py would be
# reading a different directory than this actually writes to.
IOWAIT_LOG_DIR="${2:-$_PKG_ROOT/var/iowait_logs}"
LOG="${3:-$_PKG_ROOT/var/iowait_logger_supervised.log}"
mkdir -p "$IOWAIT_LOG_DIR" "$(dirname "$LOG")"

if command -v logrotate >/dev/null 2>&1; then
  _GENERATED_LOGROTATE="$_PKG_ROOT/var/iowait_logger_supervised_$HOSTNAME_ARG.logrotate.generated"
  sed "s|^/root/P20c_alerting/iowait_logger_supervised.log {|$LOG {|" \
    "$_HERE/iowait_logger_supervised.logrotate" > "$_GENERATED_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state_iowait_$HOSTNAME_ARG" "$_GENERATED_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
      sleep 300
    done
  ) &
  echo "[supervisor:iowait:$HOSTNAME_ARG] log-rotation loop started (pid $!), checking every 300s" | tee -a "$LOG"
else
  echo "[supervisor:iowait:$HOSTNAME_ARG] WARNING: logrotate not found -- $LOG will grow unbounded" | tee -a "$LOG"
fi

echo "[supervisor:iowait:$HOSTNAME_ARG] starting, iowait_log_dir=$IOWAIT_LOG_DIR log=$LOG" | tee -a "$LOG"
while true; do
  START_TS=$(date -u +%FT%TZ)
  echo "[supervisor:iowait:$HOSTNAME_ARG] launching iowait_logger.py at $START_TS" | tee -a "$LOG"
  python3 "$_PKG_ROOT/observability/iowait_logger.py" "$HOSTNAME_ARG" "$IOWAIT_LOG_DIR" >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor:iowait:$HOSTNAME_ARG] iowait_logger.py EXITED exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
