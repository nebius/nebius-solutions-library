#!/bin/bash
set -u
# P23 step 5 -- same launch pattern as run_moe_nanogpt.sh, with
# --export=ALL,NCCL_MAX_NCHANNELS=1 added -- the exact original-feasibility-
# phase network-fault mechanism (P18h's run_netfault.sh / P20d's
# run_netfault_nanogpt.sh), applied to the toy MoE training script instead.
# This env var is job-wide by construction (NCCL negotiates channel count
# once per communicator, symmetrically across all its members), matching
# how this mechanism has always been used in this project -- not a
# per-rank injection.
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_W0=$4
DUMPDIR_W1=$5
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

echo "### moe-netfault steps=$STEPS outdir=$OUTDIR port=$PORT NCCL_MAX_NCHANNELS=1"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w worker-0,worker-1 \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,NCCL_MAX_NCHANNELS=1 \
  bash -c 'if [ "$(hostname)" = "worker-0" ]; then DD='"$DUMPDIR_W0"'; else DD='"$DUMPDIR_W1"'; fi; bash /root/P23_moe/train_node_moe.sh '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
