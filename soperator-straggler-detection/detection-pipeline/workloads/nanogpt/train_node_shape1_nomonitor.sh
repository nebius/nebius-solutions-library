#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # unused, kept for arg-position parity

HN=$(hostname)
if [ "$HN" = "worker-0" ]; then RANK=0; else RANK=1; fi

mkdir -p "$OUTDIR"

# Monitoring-OFF A/B comparison -- Inspector plugin NOT loaded at all
# (no NCCL_PROFILER_PLUGIN, no NCCL_INSPECTOR_ENABLE), same real model/
# batch/block config as the monitored Shape 1 run, same log_interval so
# the two real per-iteration time samples are directly comparable.
cd /root/P20d_e2e_validation/p20k_mean_test/nanoGPT_straggler
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=v1beta_shape1_nomon --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train.py config/train_shakespeare_char.py \
  --n_layer=12 --n_head=12 --n_embd=768 --block_size=512 --dropout=0.0 \
  --batch_size=12 --gradient_accumulation_steps=32 \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=200 \
  --eval_interval=2000 --eval_iters=50 --log_interval=20 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
