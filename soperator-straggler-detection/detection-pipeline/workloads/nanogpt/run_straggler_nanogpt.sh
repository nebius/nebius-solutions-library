#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_BASE=$4
SLEEP_MS=$5
TARGET_RANKS=$6
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
# /tmp added (this session, disk-bound checkpoint-fault follow-up) -- the
# container's own base-image /tmp is tmpfs (RAM-backed, confirmed via a
# direct mount check), not the real host ext4 /dev/vda1 /tmp the eBPF
# storage detector's own validated fault mechanism requires; without this
# explicit bind, any real disk-bound write/read targeting /tmp from
# inside this container silently never touches a real block device.
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "### straggler-nanogpt steps=$STEPS outdir=$OUTDIR port=$PORT sleep_ms=$SLEEP_MS target_ranks=$TARGET_RANKS"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_SLEEP_MS="$SLEEP_MS",STRAGGLER_TARGET_RANKS="$TARGET_RANKS" \
  bash -c 'DD="'"$DUMPDIR_BASE"'/$(hostname)"; bash "'"$SCRIPT_DIR"'/train_node_straggler.sh" '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
