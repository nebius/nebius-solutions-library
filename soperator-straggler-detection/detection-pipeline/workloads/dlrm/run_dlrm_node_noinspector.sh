#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK
mkdir -p "$OUTDIR"
export LD_LIBRARY_PATH="${NCCL_LIB_PATH:-}:${LD_LIBRARY_PATH:-}"
export MAX_ITERS=$STEPS
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p31_dlrm_noinsp --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train_dlrm.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
