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

# Real bug found live (this session): MOUNTS above hardcodes /root:/root,
# assuming this project's own original dev-cluster convention (this
# package and its nccl-2.28-src sibling both live under /root). A real,
# different clone location (e.g. under /home/<user>/...) is completely
# invisible inside the container with only that mount -- every rank
# fails immediately with "No such file or directory" trying to exec
# this script's own train_node_*.sh path. Adds whichever of this
# package's own real root (PKG_ROOT, same 2-levels-up convention
# install.sh/environment.sh already use) and the real NCCL source dir
# (derived from cluster.env's own NCCL_LIB_PATH, already loaded above)
# aren't already covered by the existing /root:/root mount -- dynamic,
# not assumed, and skips anything already redundant with /root:/root so
# a cluster still using the original /root convention sees no behavior
# change at all. Both fully resolved (cd+pwd, not string-only dirname)
# so neither ends up as a literal, unresolved "../.." path segment in
# the actual --container-mounts argument.
_SCRIPT_DIR_FOR_MOUNT_FIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT_FOR_MOUNT_FIX="$(cd "$_SCRIPT_DIR_FOR_MOUNT_FIX/../.." && pwd)"
_NCCL_SRC_DIR_FOR_MOUNT_FIX="$(cd "$(dirname "$(dirname "$NCCL_LIB_PATH")")" && pwd)"
for _p in "$_PKG_ROOT_FOR_MOUNT_FIX" "$_NCCL_SRC_DIR_FOR_MOUNT_FIX"; do
  case "$_p" in
    /root|/root/*) ;;  # already covered by the existing /root:/root mount
    *) MOUNTS="$MOUNTS,$_p:$_p" ;;
  esac
done
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
