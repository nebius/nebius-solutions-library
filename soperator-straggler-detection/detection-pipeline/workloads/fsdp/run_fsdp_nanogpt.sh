#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_W0=$4
DUMPDIR_W1=$5
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

echo "### fsdp-nanogpt steps=$STEPS outdir=$OUTDIR port=$PORT"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w worker-0,worker-1 \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  bash -c 'if [ "$(hostname)" = "worker-0" ]; then DD='"$DUMPDIR_W0"'; else DD='"$DUMPDIR_W1"'; fi; bash /root/P22_fsdp/train_node_fsdp.sh '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
