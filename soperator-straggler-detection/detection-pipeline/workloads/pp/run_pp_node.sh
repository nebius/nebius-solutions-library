#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPBASE=$4
HN=$(hostname)
if [ "$HN" = "worker-0" ]; then RANK=0; else RANK=1; fi
DUMPDIR="$DUMPBASE/dump_w$RANK"
mkdir -p "$DUMPDIR" "$OUTDIR"
export LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:${LD_LIBRARY_PATH:-}
export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0
export MAX_ITERS=$STEPS
cd /root/P24_pipeline
torchrun --nnodes=2 --nproc_per_node=1 --node_rank=$RANK \
  --rdzv_id=p24_pp --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train_pp_manual.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
