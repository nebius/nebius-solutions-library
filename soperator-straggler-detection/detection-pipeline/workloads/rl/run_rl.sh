#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_BASE=$4
SLEEP_MS=${6:-0}
TARGET_RANKS=${7:-}
PHASE=${8:-none}
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "### p31-rl steps=$STEPS outdir=$OUTDIR port=$PORT sleep_ms=$SLEEP_MS target_ranks=$TARGET_RANKS phase=$PHASE"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_SLEEP_MS="$SLEEP_MS",STRAGGLER_TARGET_RANKS="$TARGET_RANKS",STRAGGLER_PHASE="$PHASE" \
  bash -c 'DD="'"$DUMPDIR_BASE"'/$(hostname)"; bash "'"$SCRIPT_DIR"'/train_node_rl.sh" '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
