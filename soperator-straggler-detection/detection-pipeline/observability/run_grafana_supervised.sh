#!/bin/bash
# V1 Beta Stage 6 -- real auto-restart supervisor for Grafana, mirroring
# run_vm_supervised.sh's own already-established supervision/log-rotation
# pattern exactly (same structure, same copytruncate reasoning).
#
# Launched as a genuinely persistent background process directly on
# whichever host it's run from (the login node, via grafana-setup.sh),
# matching this project's own real, confirmed-working launch pattern:
# `cd` into Grafana's own homepath first, then `--homepath=.` (relative)
# -- Grafana resolves its own public/ and conf/defaults.ini relative to
# this, so running it from anywhere else silently breaks asset serving.
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"
GRAFANA_HOMEPATH="${1:?usage: run_grafana_supervised.sh <grafana_homepath> <custom_ini> [log_file]}"
CUSTOM_INI="${2:?usage: run_grafana_supervised.sh <grafana_homepath> <custom_ini> [log_file]}"
LOG="${3:-$_PKG_ROOT/var/grafana_supervised.log}"
mkdir -p "$(dirname "$LOG")"

if command -v logrotate >/dev/null 2>&1; then
  _GENERATED_LOGROTATE="$_PKG_ROOT/var/grafana_supervised.logrotate.generated"
  sed "s|^/root/P20c_alerting/grafana_supervised.log {|$LOG {|" \
    "$_HERE/grafana_supervised.logrotate" > "$_GENERATED_LOGROTATE"
  (
    while true; do
      logrotate --state "$_PKG_ROOT/var/.logrotate_state_grafana" "$_GENERATED_LOGROTATE" 2>>"$_PKG_ROOT/var/logrotate_errors.log"
      sleep 300
    done
  ) &
  echo "[supervisor:grafana] log-rotation loop started (pid $!), checking every 300s" | tee -a "$LOG"
else
  echo "[supervisor:grafana] WARNING: logrotate not found -- $LOG will grow unbounded" | tee -a "$LOG"
fi

echo "[supervisor:grafana] starting, homepath=$GRAFANA_HOMEPATH config=$CUSTOM_INI log=$LOG" | tee -a "$LOG"
while true; do
  START_TS=$(date -u +%FT%TZ)
  echo "[supervisor:grafana] launching grafana at $START_TS" | tee -a "$LOG"
  ( cd "$GRAFANA_HOMEPATH" && ./bin/grafana server --config="$CUSTOM_INI" --homepath=. ) >> "$LOG" 2>&1
  EC=$?
  END_TS=$(date -u +%FT%TZ)
  echo "[supervisor:grafana] grafana EXITED exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
  sleep 3
done
