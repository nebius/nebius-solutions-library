#!/bin/bash
# V1 Beta Stage 6 -- real auto-restart supervisor for victoria-metrics-prod,
# mirroring run_alert_engine_supervised.sh's own already-established
# supervision/log-rotation pattern exactly (same structure, same
# copytruncate reasoning -- see that script's own comments for why).
#
# Launched as a genuinely persistent background process (matching this
# project's own already-established precedent for Grafana -- see
# grafana-standalone/README.md -- not a Slurm/srun allocation), directly
# on whichever host it's run from (the login node, via vm-setup.sh).
# Every real, load-bearing flag below (0s dedup, 100y retention) comes
# from vm-standalone/README.md's own documented launch command --
# reused here unchanged, not reinvented.
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"
VM_BIN="${1:?usage: run_vm_supervised.sh <vm_binary> <data_dir> <listen_addr> [log_file]}"
DATA_DIR="${2:?usage: run_vm_supervised.sh <vm_binary> <data_dir> <listen_addr> [log_file]}"
LISTEN_ADDR="${3:?usage: run_vm_supervised.sh <vm_binary> <data_dir> <listen_addr> [log_file]}"
LOG="${4:-$_PKG_ROOT/var/vm_supervised.log}"
mkdir -p "$DATA_DIR" "$(dirname "$LOG")"

if command -v logrotate >/dev/null 2>&1; then
  _GENERATED_LOGROTATE="$_PKG_ROOT/var/vm_supervised.logrotate.generated"
  sed "s|^/root/P20c_alerting/vm_supervised.log {|$LOG {|" \
    "$_HERE/vm_supervised.logrotate" > "$_GENERATED_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state_vm" "$_GENERATED_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
      sleep 300
    done
  ) &
  echo "[supervisor:vm] log-rotation loop started (pid $!), checking every 300s" | tee -a "$LOG"
else
  echo "[supervisor:vm] WARNING: logrotate not found -- $LOG will grow unbounded" | tee -a "$LOG"
fi

echo "[supervisor:vm] starting, bin=$VM_BIN data_dir=$DATA_DIR listen_addr=$LISTEN_ADDR log=$LOG" | tee -a "$LOG"
while true; do
  START_TS=$(date -u +%FT%TZ)
  echo "[supervisor:vm] launching victoria-metrics-prod at $START_TS" | tee -a "$LOG"
  "$VM_BIN" \
    -storageDataPath="$DATA_DIR" \
    -httpListenAddr="$LISTEN_ADDR" \
    -dedup.minScrapeInterval=0s \
    -retentionPeriod=100y >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor:vm] victoria-metrics-prod EXITED exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
