#!/bin/bash
cd /root/inspector-audit
for t in 1 2 3; do repo/soperator-straggler-detection/detection-pipeline/pinning/ab/fault_trial.sh s6b_fault$t 300 100 8 > logs/s6b_fault$t.log 2>&1; done
echo ALL_FAULT_DONE >> logs/s6b_fault3.log
