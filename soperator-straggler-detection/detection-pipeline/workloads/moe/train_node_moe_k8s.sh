#!/bin/bash
# K8s-native per-pod training script for MoE (Mixture of Experts).
# Sibling to train_node_moe.sh (Slurm) -- identical except it sources
# cluster_topology_k8s.sh instead of cluster_topology.sh. Zero changes to
# the detection logic, Inspector wiring, or training configuration.
#
# MoE communicator structure (16 GPUs / 2 nodes):
#   - AllToAll (expert dispatch/combine): 16-member, every forward pass
#   - AllReduce (non-expert grad sync, sync_grads()): 16-member, every backward
# Both are 16-member communicators detectable by _check_mean and _check_mean_tiered.
#
# Required env vars (set in Job pod template):
#   JOB_COMPLETION_INDEX       -- pod rank (0-based, set by K8s indexed Job)
#   TRAINING_NUM_NODES         -- total pod count
#   TRAINING_RDZV_HOST         -- rendezvous ClusterIP Service DNS
#   TRAINING_GPUS_PER_NODE     -- GPUs per node (8 for H200)
#   STRAGGLER_SLEEP_MS         -- fault magnitude in ms (0 = healthy)
#   STRAGGLER_TARGET_RANKS     -- comma-separated global ranks to fault
#   PIN_MODE                   -- auto|full|dump|off (default auto)
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4

_PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

_CHECKS_SCRIPT="$_PKG_ROOT/k8s/startup_checks.sh"
if [ -f "$_CHECKS_SCRIPT" ]; then source "$_CHECKS_SCRIPT"; fi

_LIB="$_PKG_ROOT/lib/cluster_topology_k8s.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

_PLUGIN_PATH="$_PKG_ROOT/inspector-plugin/libnccl-profiler-inspector.so"
stat "$_PLUGIN_PATH" > /dev/null 2>&1 || true
export NCCL_PROFILER_PLUGIN="$_PLUGIN_PATH"
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR="$DUMPDIR"
export NCCL_INSPECTOR_PROM_DUMP=0

PIN_MODE="${PIN_MODE:-auto}"
export PIN_MODE
export PIN_LOG_DIR="${PIN_LOG_DIR:-$OUTDIR/pinlogs}"
source "$_PKG_ROOT/pinning/pin_env.sh"
pin_prepare

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=k8s_moe --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "${PIN_WRAP[@]}" \
  train_moe.py \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=100 \
  --eval_interval=3000 --eval_iters=200 --log_interval=50 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  2>&1 | tee $OUTDIR/train_${HN}.log
EC=${PIPESTATUS[0]}
echo "[$HN] TORCHRUN_EXIT: $EC"
