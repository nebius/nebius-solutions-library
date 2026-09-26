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
DUMPDIR_BASE=$4
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "### moe-netfault steps=$STEPS outdir=$OUTDIR port=$PORT NCCL_MAX_NCHANNELS=1"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,NCCL_MAX_NCHANNELS=1 \
  bash -c 'DD="'"$DUMPDIR_BASE"'/$(hostname)"; bash "'"$SCRIPT_DIR"'/train_node_moe.sh" '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
