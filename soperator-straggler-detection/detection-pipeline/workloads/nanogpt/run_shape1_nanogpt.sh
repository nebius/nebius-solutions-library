#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_BASE=$4
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "### v1beta-shape1-nanogpt steps=$STEPS outdir=$OUTDIR port=$PORT"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

# Stage 2 cluster-topology-agnostic fix: STRAGGLER_TRIGGER_FILE used to
# be a hardcoded absolute path into this project's original development-
# host layout (/root/_v1beta_dryrun/trigger.json). This is real runtime
# state (a fault-trigger file written live during a test, not packaged
# source), so it belongs under this job's own real, always-provided
# OUTDIR rather than a fixed path.
TRIGGER_FILE="$OUTDIR/trigger.json"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_MODE=file_trigger,STRAGGLER_TRIGGER_FILE="$TRIGGER_FILE" \
  bash -c 'DD="'"$DUMPDIR_BASE"'/$(hostname)"; bash "'"$SCRIPT_DIR"'/train_node_shape1.sh" '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
