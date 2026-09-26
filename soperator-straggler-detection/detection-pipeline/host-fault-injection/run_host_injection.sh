#!/bin/bash
set -u
MODE=$1  # cpu_inject or healthy
STEPS=$2
OUTDIR=$3
PORT=$4
# Stage 2 cluster-topology-agnostic fix: was a hardcoded absolute path
# into this project's original development-host layout (/root/P4d_clean)
# -- resolved to this script's own real, installed location instead.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
cluster_topology_discover_rdzv_host
# Stage 2 cluster-topology-agnostic fix: the CPU-contention target used
# to be hardcoded to the literal "worker-0" -- now the same real,
# discovered first-node convention used throughout this package.
BURNER_HOST="$RDZV_HOST"

echo "### mode=$MODE steps=$STEPS outdir=$OUTDIR port=$PORT"

echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then
  echo "ABORT: soperatorchecks job active"; exit 1
fi
if [ -n "$(squeue -u "$USER" -h)" ]; then
  echo "ABORT: existing job for $USER already queued/running"; exit 1
fi

mkdir -p "$OUTDIR"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  bash "$BASE/train_node.sh" "$STEPS" "$OUTDIR" "$PORT" 0 4 \
  > "$OUTDIR/full_output.log" 2>&1 &
SRUN_PID=$!

if [ "$MODE" = "cpu_inject" ]; then
  sleep 10
  ssh -o BatchMode=yes "$BURNER_HOST" 'nohup /tmp/cpu_burn.sh 600 > /tmp/cpu_burn.log 2>&1 & echo $! > /tmp/cpu_burn.pid'
  echo "CPU burner started on $BURNER_HOST"
fi

wait "$SRUN_PID"
SRUN_EC=$?
echo "srun exit: $SRUN_EC"
grep -H TORCHRUN_EXIT "$OUTDIR/full_output.log"

if [ "$MODE" = "cpu_inject" ]; then
  ssh -o BatchMode=yes "$BURNER_HOST" 'kill $(cat /tmp/cpu_burn.pid) 2>/dev/null; pkill -f "while true" 2>/dev/null; rm -f /tmp/cpu_burn.pid'
  echo "CPU burner stopped"
fi

# Stage 2 cluster-topology-agnostic fix: used to hardcode exactly
# nodename=worker-0/worker-1 -- now every real, discovered node in
# NODE_LIST, whatever the real count/names are.
echo "$NODE_LIST" | tr ',' '\n' | while read -r node; do
  scontrol update nodename="$node" state=resume >/dev/null 2>&1
  ssh -o BatchMode=yes "$node" 'rm -rf /tmp/p4d_inspector_dumps' >/dev/null 2>&1
done
