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

cd /root/P3_real_workload/nanoGPT
# eval_interval=3000 and always_save_checkpoint=True (deliberate deviation
# from P19's short-test config): forces a repeated, guaranteed rank-0
# checkpoint write every ~60-70s throughout the whole long run, instead of
# at most once at the very end -- needed to characterize whether the
# rank-0 checkpoint-I/O signal recurs, not just observe it once.
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=p20alongrun --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train.py config/train_shakespeare_char.py \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=100 \
  --eval_interval=3000 --eval_iters=200 --log_interval=200 \
  --always_save_checkpoint=True \
  --compile=False --gradient_accumulation_steps=16 --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
