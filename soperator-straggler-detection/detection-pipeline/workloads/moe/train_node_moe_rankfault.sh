#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # host-visible path (under /root, bind-mounted)

HN=$(hostname)
if [ "$HN" = "worker-0" ]; then NODE_RANK=0; else NODE_RANK=1; fi

mkdir -p "$DUMPDIR" "$OUTDIR"

export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

# P23 (single-rank AllToAll fault test) -- torchrun runs
# rank_fault_wrapper.sh instead of train_moe.py directly; the wrapper
# reads its own process's real, torchrun-assigned $RANK (global rank,
# 0-15) and conditionally sets NCCL_MAX_NCHANNELS=1 for only the target
# rank (default rank 4, override via FAULT_TARGET_RANK) before exec-ing
# train_moe.py unchanged. No CUDA_VISIBLE_DEVICES restriction, same
# established precedent as every prior real-training test in this
# project -- GPU3 participates normally, never excluded, only never a
# fault-injection TARGET.
cd /root/P23_moe
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$NODE_RANK \
  --rdzv_id=p23_moe_rf --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  --no-python \
  ./rank_fault_wrapper.sh \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=100 \
  --eval_interval=3000 --eval_iters=200 --log_interval=50 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
