#!/bin/bash
# interleaved_baseline.sh -- genuinely interleaved "no Inspector at all" vs "Inspector ON,
# full pinning" A/B, healthy condition only, at full 6-node/48-GPU scale. Answers: what is the
# total real-world cost of running Inspector (with full pinning) vs not running Inspector at all,
# at production scale -- separate question from interleaved_closeout.sh's pinned-vs-unpinned (which
# always has Inspector running in both arms). Modeled directly on interleaved_closeout.sh's
# run_one(); same shape, same cluster, 150 iters/run (140 steady-state after skip-first-10).
set -u
cd "$(dirname "$0")/../.."
DP="$(pwd)"
PORT_BASE=30101
RUN_BASE="$DP/var/runs"
IMG="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp,$DP:$DP,$DP/../../nccl-2.28-src:$DP/../../nccl-2.28-src"
NODES="worker-0,worker-1,worker-2,worker-3,worker-4,worker-5"

wait_for_queue() {
  while [ -n "$(squeue -u "$USER" -h)" ]; do sleep 15; done
}

run_one() {
  local name=$1 inspector=$2 pin_export=$3 port=$4
  wait_for_queue
  local dd="$RUN_BASE/$name/dump"
  mkdir -p "$RUN_BASE/$name"
  echo "$(date -u +%FT%TZ) START $name inspector=$inspector pin_export=$pin_export"
  srun --nodes=6 --ntasks=6 --ntasks-per-node=1 --gpus-per-node=8 --exclusive \
    --cpus-per-task=128 --cpu-bind=none -w "$NODES" \
    --container-image="$IMG" --container-mounts="$MOUNTS" \
    --export=ALL,STRAGGLER_SLEEP_MS=0,STRAGGLER_TARGET_RANKS=,STORAGE_FAULT_FILE_MB=0,STORAGE_FAULT_TARGET_RANKS=,INSPECTOR=$inspector$pin_export \
    bash -c "DD=\"$RUN_BASE/$name/\$(hostname)\"; bash \"$DP/workloads/megatron/train_node_megatron.sh\" 150 \"$RUN_BASE/$name\" $port \$DD" \
    > "$RUN_BASE/$name/full_output.log" 2>&1
  echo "$(date -u +%FT%TZ) END $name exit=$?"
}

# 3 interleaved pairs: Inspector fully off (PIN_MODE left to its own off-when-no-Inspector default)
# vs Inspector on + full pinning (the realistic prod config) -- the only two arms being compared here.
for r in 1 2 3; do
  run_one "closeout_baseline_noinsp${r}" off ""            $((PORT_BASE + r*2))
  run_one "closeout_baseline_pinned${r}"  on  ",PIN_MODE=full" $((PORT_BASE + r*2 + 1))
done

echo "ALL_BASELINE_PAIRS_DONE"
