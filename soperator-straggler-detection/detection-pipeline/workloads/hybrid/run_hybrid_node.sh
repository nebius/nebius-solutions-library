#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPBASE=$4
HN=$(hostname)
if [ "$HN" = "worker-0" ]; then NODE_RANK=0; else NODE_RANK=1; fi
mkdir -p "$OUTDIR"
export LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:${LD_LIBRARY_PATH:-}
export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_PROM_DUMP=0
export MAX_ITERS=$STEPS
cd /root/P28_hybrid
# P28 -- 2 ranks per node (rank0,1=worker-0=PP stage0's TP pair;
# rank2,3=worker-1=PP stage1's TP pair), each rank needs its OWN dump
# dir (node_aggregator_ref.py's convention: one aggregator per node,
# reading every rank co-located on that node from one shared dump dir --
# unlike PP's earlier 1-rank-per-node script, here BOTH local ranks'
# Inspector dumps land in the SAME per-node dir, exactly matching how
# every 8-rank-per-node workload in this project already works).
DUMPDIR="$DUMPBASE/dump_w$NODE_RANK"
mkdir -p "$DUMPDIR"
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
torchrun --nnodes=2 --nproc_per_node=2 --node_rank=$NODE_RANK \
  --rdzv_id=p28_hybrid --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train_hybrid_tp_pp.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
