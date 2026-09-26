#!/bin/bash
set -u
# P26 -- ViT-B/16 (real torchvision transformer encoder), same DDP/
# dump-dir/NCCL-Inspector wiring as train_node_resnet_p26.sh.
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
export BATCH_SIZE=${BATCH_SIZE_OVERRIDE:-16}
export IMAGE_SIZE=${IMAGE_SIZE:-224}
export LOG_INTERVAL=10

cd /root/P26_vit
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=p26_vit --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  /root/P26_vit/train_vit.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
