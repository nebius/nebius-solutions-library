#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # host-visible path (under /root, bind-mounted)

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=0
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

# V1-beta sustained dry run, Shape 1 -- GPT-2(124M)-scale model/batch/
# block settings, real (not toy-stretched) config, trained on the
# already-prepared shakespeare_char dataset (openwebtext was never
# prepared in this environment and would take far longer to fetch than
# this session's own real-time budget allows) -- same real model
# architecture/scale as train_gpt2.py's own config, different (smaller,
# already-available) real text corpus.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=v1beta_shape1 --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
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
