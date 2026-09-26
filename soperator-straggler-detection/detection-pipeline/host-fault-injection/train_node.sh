#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
INJECT_ENABLE=${4:-0}
INJECT_RANK=${5:-4}
INJECT_BURST_MS=${6:-1.0}
INJECT_PERIOD_MS=${7:-20.0}

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

DUMPDIR=/tmp/p4d_inspector_dumps
rm -rf "$DUMPDIR"; mkdir -p "$DUMPDIR"; mkdir -p "$OUTDIR"

export NCCL_PROFILER_PLUGIN=/root/nccl-2.28-src/ext-profiler/inspector/libnccl-profiler-inspector.so
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

export INJECT_ENABLE INJECT_RANK INJECT_BURST_MS INJECT_PERIOD_MS

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p4dclean --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train.py config/train_shakespeare_char.py \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=20 \
  --eval_interval=100000 --log_interval=10 --always_save_checkpoint=False \
  --compile=False --gradient_accumulation_steps=16 --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"

mkdir -p "$OUTDIR/dumps_$HN"
cp "$DUMPDIR"/*.log "$OUTDIR/dumps_$HN/" 2>/dev/null
echo "[$HN] nfiles=$(ls "$OUTDIR/dumps_$HN" 2>/dev/null | wc -l)"
rm -rf "$DUMPDIR"
