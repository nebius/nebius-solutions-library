#!/bin/bash
# Validation matrix (full stack, PIN_MODE=full, NCCL 2.32.3, PIN_HOUSEKEEPING stack). Args: TAG ITERS SLEEP_MS TARGET_RANK
cd /root/inspector-audit
export PIN_VERIFY_SAMPLE=120 PIN_VERIFY_SAMPLE_AFTER=240
F=repo/soperator-straggler-detection/detection-pipeline/pinning/ab/fault_trial.sh
$F v_healthyA   300 0   8  > logs/v_healthyA.log 2>&1      # healthy + per-thread sampling + housekeeping
$F v_rank2_100  300 100 2  > logs/v_rank2_100.log 2>&1      # worker-0, replica 0, PP stage 1
$F v_rank13_100 300 100 13 > logs/v_rank13_100.log 2>&1     # worker-1, replica 1, PP stage 2
$F v_rank8_25   300 25  8  > logs/v_rank8_25.log 2>&1       # lower magnitude on the original target
$F v_healthyB   300 0   8  > logs/v_healthyB.log 2>&1       # second healthy repeat
echo MATRIX_DONE >> logs/v_healthyB.log
