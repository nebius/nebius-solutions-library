#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # host-visible path (under /root, bind-mounted)

HN=$(hostname)
if [ "$HN" = "worker-0" ]; then RANK=0; else RANK=1; fi

mkdir -p "$DUMPDIR" "$OUTDIR"

export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

# P21 -- TP_SIZE is read directly by the patched train.py (own copy under
# /root/P21_multicomm/nanoGPT_tp/, not the P20d/p20k_mean_test one) --
# passed through via --export=ALL from the launching srun command.

cd /root/P21_multicomm/nanoGPT_tp
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=p21_tp --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train.py config/train_shakespeare_char.py \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=100 \
  --eval_interval=3000 --eval_iters=200 --log_interval=50 \
  --always_save_checkpoint=False \
  --compile=False --gradient_accumulation_steps=16 --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
