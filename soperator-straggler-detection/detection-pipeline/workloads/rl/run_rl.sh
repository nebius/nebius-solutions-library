#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_W0=$4
DUMPDIR_W1=$5
SLEEP_MS=${6:-0}
TARGET_RANKS=${7:-}
PHASE=${8:-none}
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

echo "### p31-rl steps=$STEPS outdir=$OUTDIR port=$PORT sleep_ms=$SLEEP_MS target_ranks=$TARGET_RANKS phase=$PHASE"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w worker-0,worker-1 \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_SLEEP_MS="$SLEEP_MS",STRAGGLER_TARGET_RANKS="$TARGET_RANKS",STRAGGLER_PHASE="$PHASE" \
  bash -c 'if [ "$(hostname)" = "worker-0" ]; then DD='"$DUMPDIR_W0"'; else DD='"$DUMPDIR_W1"'; fi; bash /root/P31_rl/train_node_rl.sh '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
