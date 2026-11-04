#!/bin/bash
# Real clock-throttle ground-truth test (approved by user): lock worker-1 GPU 0 to 600 MHz for 240 s
# mid-run, ALWAYS restore (-rgc) and verify. Both DCGM fallback paths active (patched engine).
cd /root/inspector-audit
export PIN_VERIFY_SAMPLE=0 PIN_VERIFY_SAMPLE_AFTER=0
L=logs/p_clock_lock.log
F=repo/soperator-straggler-detection/detection-pipeline/pinning/ab/fault_trial.sh
restore(){
  echo "RESTORE $(date -u +%FT%TZ)" >> $L
  ssh worker-1 'nvidia-smi -i 0 -rgc' >> $L 2>&1
  ssh worker-1 'nvidia-smi -i 0 --query-gpu=clocks.sm,clocks.max.sm,clocks_event_reasons.sw_thermal_slowdown --format=csv' >> $L 2>&1
}
trap restore EXIT
( sleep ${LOCK_AFTER_S:-330}
  echo "LOCK $(date -u +%FT%TZ)" >> $L
  ssh worker-1 'nvidia-smi -i 0 -lgc 600,600' >> $L 2>&1
  sleep ${LOCK_HOLD_S:-240}
  restore ) &
$F ${1:-p_clock} 300 0 8 > logs/${1:-p_clock}.log 2>&1
wait
restore
echo CLOCK_TEST_DONE >> logs/${1:-p_clock}.log
