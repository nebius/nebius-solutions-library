#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
HN=$(hostname)
if [ "$HN" = "worker-0" ]; then RANK=0; else RANK=1; fi
mkdir -p "$OUTDIR"
export LD_LIBRARY_PATH=/root/nccl-2.28-src/build/lib:${LD_LIBRARY_PATH:-}
export MAX_ITERS=$STEPS
cd /root/P31_dlrm
torchrun --nnodes=2 --nproc_per_node=8 --node_rank=$RANK \
  --rdzv_id=p31_dlrm_noinsp --rdzv_backend=c10d --rdzv_endpoint=worker-0:$PORT \
  train_dlrm.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
