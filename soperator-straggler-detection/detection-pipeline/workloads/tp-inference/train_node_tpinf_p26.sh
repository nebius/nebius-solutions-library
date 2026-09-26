#!/bin/bash
set -u
# P26 -- TP-style inference-only test: real forward-pass-only traffic
# through a TP-sharded model, no backward, no gradient sync.
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # host-visible path (under /root, bind-mounted)

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

export MAX_ITERS=$STEPS
export BATCH_SIZE=${BATCH_SIZE_OVERRIDE:-8}
export BLOCK_SIZE=${BLOCK_SIZE_OVERRIDE:-256}
export LOG_INTERVAL=10
# TP_SIZE passed through via --export=ALL from the launching srun command.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p26_tpinf --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"/train_tp_inference.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
