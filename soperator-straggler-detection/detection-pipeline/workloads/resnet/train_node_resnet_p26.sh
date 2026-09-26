#!/bin/bash
set -u
# P26 Step 0 -- same train_resnet.py (unmodified: ResNet18, DDP, synthetic
# data, batch=32, image=224, cudnn disabled) as the original P18i/j/k
# feasibility-phase validation, wired to THIS project's current dump-dir/
# NCCL-Inspector convention (node_aggregator_ref.py-compatible), instead
# of P18i's old hardcoded /tmp dump dir + local-buffer classifier.py path.
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # host-visible path (under /root, bind-mounted)

HN=$(hostname)
if [ "$HN" = "worker-0" ]; then RANK=0; else RANK=1; fi

mkdir -p "$DUMPDIR" "$OUTDIR"

export CUDA_MODULE_LOADING=EAGER
export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

export MAX_ITERS=$STEPS
export BATCH_SIZE=${BATCH_SIZE_OVERRIDE:-32}
export IMAGE_SIZE=${IMAGE_SIZE:-224}
export LOG_INTERVAL=10
# INJECT_* left at their env defaults (INJECT_ENABLE=0) for the healthy/
# clock-lock runs; a separate invocation sets INJECT_ENABLE=1 explicitly
# for the burst/jitter-blind-spot re-check.

cd /root/P18i_classifier
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=p26_resnet --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  /root/P18i_classifier/train_resnet.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
