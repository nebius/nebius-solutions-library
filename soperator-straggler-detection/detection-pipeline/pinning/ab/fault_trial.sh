#!/bin/bash
# fault_trial.sh TAG ITERS SLEEP_MS TARGET_RANK  -- one Megatron (16 GPU, TP4/PP2/DP2) run on NCCL 2.32.3 + PIN_MODE=full
# against the LIVE run.sh stack (aggregators + alert engine). SLEEP_MS=0 => healthy run.
# Ground truth (global rank -> pid/host) is read from the run's own dump files BEFORE any alert is examined.
TAG=$1; ITERS=$2; SLEEP=$3; TRank=$4
PKG=/root/inspector-audit/repo/soperator-straggler-detection/detection-pipeline
source /root/inspector-audit/run_cfg.sh
export DUMP_BASE=$PKG/var/dump_$TAG   # per-trial dir so ground truth/aggregation never mixes trials
ALOG=$PKG/var/alert_engine_supervised.log; OUT=/root/inspector-audit/runs/$TAG
# aggregators watch var/dump/<host>; point them via a symlink per trial is not possible, so use the shared dir:
export DUMP_BASE=$PKG/var/dump
rss() { for p in $(pgrep -f "alert_engine.py|vm-standalone/victoria-metrics-prod"); do echo "$(ps -o rss= -p $p) KB $(ps -o args= -p $p | cut -c1-60)"; done; for w in worker-0 worker-1; do ssh -o BatchMode=yes $w 'for p in $(pgrep -f node_aggregator_ref.py); do echo "$(ps -o rss= -p $p) KB aggregator@$(hostname)"; done'; done; }
while [ -n "$(squeue -h)" ]; do sleep 10; done
A0=$(stat -c %s $ALOG); echo "[trial $TAG] start $(date -u +%FT%TZ) sleep_ms=$SLEEP target_rank=$TRank iters=$ITERS"; rss > /root/inspector-audit/runs_${TAG}_rss_before.txt
( MEGA_GBS=32 run_one $TAG $ITERS on full 2323 STRAGGLER_SLEEP_MS=$SLEEP STRAGGLER_TARGET_RANKS=$( [ "$SLEEP" = 0 ] && echo "" || echo $TRank ) ) &
RP=$!
# ground truth: world-comm (n_ranks=16) header rank == TRank -> pid, host
TRUTH=""; for i in $(seq 1 120); do
  for h in worker-0 worker-1; do for f in $(ls -t $PKG/var/dump/$h/*.log 2>/dev/null | head -20); do
    [ "$(stat -c %Y $f)" -ge "$(cat $OUT/t_start 2>/dev/null || echo 9999999999)" ] || continue
    l=$(head -1 $f); r=$(echo "$l" | grep -o '"rank":[0-9]*,"n_ranks":16' | grep -o '"rank":[0-9]*' | cut -d: -f2)
    [ "$r" = "$TRank" ] && TRUTH="$(echo $f | grep -o 'pid[0-9]*' | tr -d pid) $h" && break 2; done; done
  [ -n "$TRUTH" ] && break; sleep 5; done
echo "[trial $TAG] GROUND_TRUTH target_rank=$TRank -> pid/host: ${TRUTH:-UNKNOWN}"
T0=$(date +%s); DET=""
while kill -0 $RP 2>/dev/null; do
  if [ "$SLEEP" != 0 ] && [ -n "$TRUTH" ] && [ -z "$DET" ] && tail -c +$((A0+1)) $ALOG | grep -E "^\[WAIT-INDUCED-ALERT\] " | grep -q "rank=${TRUTH%% *} .*node=${TRUTH##* } "; then DET=$(( $(date +%s) - T0 )); echo "[trial $TAG] WAIT-INDUCED-ALERT named the true rank after ${DET}s"; fi
  sleep 5; done
wait $RP
tail -c +$((A0+1)) $ALOG > $OUT/alert_engine_slice.log; rss > /root/inspector-audit/runs_${TAG}_rss_after.txt
echo "[trial $TAG] END $(date -u +%FT%TZ) detect_s=${DET:-NONE}"
python3 "$(dirname "$0")/score_alerts.py" $TAG "${TRUTH%% *}" | sed "s/^/[trial $TAG] /"
echo "[trial $TAG] CHECK-FAILED: $(grep -c CHECK-FAILED $OUT/alert_engine_slice.log)  TORCHRUN_EXIT: $(grep TORCHRUN_EXIT $OUT/full_output.log | tr '\n' ' ')"
cat $OUT/pin_verify_*.log | grep PIN-VERIFY
