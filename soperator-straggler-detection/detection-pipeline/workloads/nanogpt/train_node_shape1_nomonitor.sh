#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # unused, kept for arg-position parity

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$OUTDIR"

# Monitoring-OFF A/B comparison -- Inspector plugin NOT loaded at all
# (no NCCL_PROFILER_PLUGIN, no NCCL_INSPECTOR_ENABLE), same real model/
# batch/block config as the monitored Shape 1 run, same log_interval so
# the two real per-iteration time samples are directly comparable.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=v1beta_shape1_nomon --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train.py "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../nanogpt-base/config/train_shakespeare_char.py" \
  --n_layer=12 --n_head=12 --n_embd=768 --block_size=512 --dropout=0.0 \
  --batch_size=12 --gradient_accumulation_steps=32 \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=200 \
  --eval_interval=2000 --eval_iters=50 --log_interval=20 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
