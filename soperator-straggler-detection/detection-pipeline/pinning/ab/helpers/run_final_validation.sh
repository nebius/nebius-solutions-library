#!/bin/bash
# Final validation with BOTH DCGM false-page patches active (full pin, NCCL 2.32.3, PIN_HOUSEKEEPING stack).
# Step 3: 2 healthy; Step 7: 3x 100ms fault on rank 8; Steps 5/6: ~105 min healthy soak with per-thread sampling.
cd /root/inspector-audit
F=repo/soperator-straggler-detection/detection-pipeline/pinning/ab/fault_trial.sh
export PIN_VERIFY_SAMPLE=120 PIN_VERIFY_SAMPLE_AFTER=240
$F f_healthyA 300 0 8 > logs/f_healthyA.log 2>&1
$F f_healthyB 300 0 8 > logs/f_healthyB.log 2>&1
$F f_fault1 300 100 8 > logs/f_fault1.log 2>&1
$F f_fault2 300 100 8 > logs/f_fault2.log 2>&1
$F f_fault3 300 100 8 > logs/f_fault3.log 2>&1
export PIN_VERIFY_SAMPLE=600 PIN_VERIFY_SAMPLE_AFTER=900
$F f_soak 4500 0 8 > logs/f_soak.log 2>&1
echo FINAL_DONE >> logs/f_soak.log
