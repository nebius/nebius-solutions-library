#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPBASE=$4
_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK
DUMPDIR="$DUMPBASE/dump_w$RANK"
mkdir -p "$DUMPDIR" "$OUTDIR"
export LD_LIBRARY_PATH="${NCCL_LIB_PATH:-}:${LD_LIBRARY_PATH:-}"
export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0
export MAX_ITERS=$STEPS
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p32_repro --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  repro_alltoall.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
