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
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

# P22 -- no CUDA_VISIBLE_DEVICES restriction. All 8 local GPUs used
# normally, local_rank N == physical GPU N directly, so gpu_slot_index
# (captured via cudaGetDevice() in the Inspector plugin) reports the true
# physical slot -- exactly the methodology fix carried forward from the
# P22-prereq session's disclosed CUDA_VISIBLE_DEVICES mistake.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p22_fsdp --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train_fsdp.py \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=100 \
  --eval_interval=3000 --eval_iters=200 --log_interval=50 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
