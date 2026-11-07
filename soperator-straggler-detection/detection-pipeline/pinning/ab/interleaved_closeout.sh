#!/bin/bash
# interleaved_closeout.sh -- genuinely interleaved pin ON/OFF A/B for the final pre-POC closeout.
# Fixes the prior two-block design (healthy1-3 vs pinoff1-3, run hours apart) by alternating
# ON/OFF/ON/OFF/ON/OFF in actual launch sequence, for both healthy and under-fault conditions.
# 150 iterations/run (not 300): a deliberate, disclosed reduction from the two-block run's 300 to
# keep 12 real sequential runs tractable in one closeout pass; still well above this project's own
# stated "150+ iterations/run" A/B minimum, skip-first-10 warmup leaves 140 steady-state samples/run.
set -u
cd "$(dirname "$0")/../.."
DP="$(pwd)"
PORT_BASE=30001
RUN_BASE="$DP/var/runs"
IMG="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp,$DP:$DP,$DP/../../nccl-2.28-src:$DP/../../nccl-2.28-src"
NODES="worker-0,worker-1,worker-2,worker-3,worker-4,worker-5"

wait_for_queue() {
  while [ -n "$(squeue -u "$USER" -h)" ]; do sleep 15; done
}

run_one() {
  local name=$1 pin=$2 sleep_ms=$3 target=$4 port=$5
  wait_for_queue
  local dd="$RUN_BASE/$name/dump"
  mkdir -p "$RUN_BASE/$name"
  echo "$(date -u +%FT%TZ) START $name pin=$pin sleep_ms=$sleep_ms target=$target"
  srun --nodes=6 --ntasks=6 --ntasks-per-node=1 --gpus-per-node=8 --exclusive \
    --cpus-per-task=128 --cpu-bind=none -w "$NODES" \
    --container-image="$IMG" --container-mounts="$MOUNTS" \
    --export=ALL,STRAGGLER_SLEEP_MS=$sleep_ms,STRAGGLER_TARGET_RANKS=$target,STORAGE_FAULT_FILE_MB=0,STORAGE_FAULT_TARGET_RANKS=,PIN_MODE=$pin \
    bash -c "DD=\"$RUN_BASE/$name/\$(hostname)\"; bash \"$DP/workloads/megatron/train_node_megatron.sh\" 150 \"$RUN_BASE/$name\" $port \$DD" \
    > "$RUN_BASE/$name/full_output.log" 2>&1
  echo "$(date -u +%FT%TZ) END $name exit=$?"
}

# Healthy condition: 3 interleaved pairs (ON then OFF each round)
for r in 1 2 3; do
  run_one "closeout_healthy_on${r}"  full 0 ""  $((PORT_BASE + r*2))
  run_one "closeout_healthy_off${r}" off  0 ""  $((PORT_BASE + r*2 + 1))
done

# Under-fault condition: same 100ms/rank-8 fault as the rest of this project, 3 interleaved pairs
for r in 1 2 3; do
  run_one "closeout_fault_on${r}"  full 100 8  $((PORT_BASE + 20 + r*2))
  run_one "closeout_fault_off${r}" off  100 8  $((PORT_BASE + 20 + r*2 + 1))
done

echo "ALL_CLOSEOUT_PAIRS_DONE"
