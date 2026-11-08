#!/bin/bash
# K8s per-pod Megatron train script — sibling to train_node_megatron.sh (Slurm).
# Sources cluster_topology_k8s.sh instead of cluster_topology.sh; everything
# else is identical. Do NOT modify _check_mean, classifier.py, or bug fixes.
set -u
TRAIN_ITERS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4

# Resolve both to absolute via this script's OWN known location, not the
# ambient cwd -- same real bug fix as the Slurm sibling (train_node_megatron.sh):
# cwd here is not reliably the caller's launch dir, and a relative OUTDIR/
# DUMPDIR also breaks once this script cd's into its own work dir below.
_PKG_ROOT_FOR_OUTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$OUTDIR" in /*) ;; *) OUTDIR="$_PKG_ROOT_FOR_OUTDIR/$OUTDIR" ;; esac
case "$DUMPDIR" in /*) ;; *) DUMPDIR="$_PKG_ROOT_FOR_OUTDIR/$DUMPDIR" ;; esac

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology_k8s.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

# Run environment self-checks before training.
# Detects and adapts to: broken NCCL plugin, PID collisions, DCGM availability.
_CHECKS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/k8s/startup_checks.sh"
if [ -f "$_CHECKS_SCRIPT" ]; then
  source "$_CHECKS_SCRIPT"
else
  # startup_checks.sh's own header says "No check silently fails" -- this
  # conditional source was the one way it actually could (confirmed: a fresh
  # namespace where the workspace PVC isn't mounted yet hits this). Loud
  # warning instead of silent skip, consistent with that stated contract.
  echo "[STARTUP] [$(hostname)] WARNING: $_CHECKS_SCRIPT not found -- NCCL plugin," \
       "rank-uniqueness, and hardware-diagnostic self-checks did NOT run this job." >&2
fi

# Wait for the rendezvous DNS entry to be resolvable before starting torchrun.
# Non-rank-0 pods must wait for rank-0's DNS to appear in the headless service;
# rank-0 also waits briefly to ensure the headless service endpoint is registered.
# Without this, pods starting simultaneously race to connect before DNS propagates.
_RDZV_FQDN="$(echo "$RDZV_HOST" | cut -d: -f1)"
# Megatron with PIN_MODE=off starts in < 2s — before headless-service
# DNS (pod-name.headless-svc.ns.svc.cluster.local) propagates from the
# K8s endpoint controller. nanoGPT's natural delay (pin_resolve.py +
# build.sh) is 15-30s, which is enough. Megatron needs an explicit wait.
echo "[$HN] Waiting 20s for headless-service DNS to propagate (rank $RANK)..."
sleep $((15 + RANK * 20))

_PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ "${INSPECTOR:-on}" = "on" ]; then
  export NCCL_PROFILER_PLUGIN="$_PKG_ROOT/inspector-plugin/libnccl-profiler-inspector.so"
  export NCCL_INSPECTOR_ENABLE=1
fi
export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR="$DUMPDIR"
export NCCL_INSPECTOR_PROM_DUMP=0
export CUDA_DEVICE_MAX_CONNECTIONS=1

# CPU pinning (same as Slurm path — PIN_MODE=dump or full per env var)
if [ "${INSPECTOR:-on}" = "off" ]; then PIN_MODE="${PIN_MODE:-off}"; else PIN_MODE="${PIN_MODE:-dump}"; fi
export PIN_MODE
export PIN_LOG_DIR="${PIN_LOG_DIR:-$OUTDIR/pinlogs}"
source "$_PKG_ROOT/pinning/pin_env.sh"
pin_prepare

# Triton per-local-rank cache (same race guard as Slurm path)
export TRITON_CACHE_DIR="${TRITON_CACHE_DIR:-/tmp/triton_cache_rank${LOCAL_RANK:-0}}"
mkdir -p "$TRITON_CACHE_DIR"

MEGATRON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/Megatron-LM" && pwd)"
cd "$MEGATRON_DIR"

SLEEP_MS="${STRAGGLER_SLEEP_MS:-0}"
TARGET_RANKS="${STRAGGLER_TARGET_RANKS:-}"

torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=k8s_megatron --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "${PIN_WRAP[@]}" \
  pretrain_gpt_straggler.py \
  --tensor-model-parallel-size=${MEGA_TP:-4} --pipeline-model-parallel-size=${MEGA_PP:-2} --use-tp-pp-dp-mapping \
  --num-layers=${MEGA_LAYERS:-40} --hidden-size=${MEGA_HIDDEN:-4096} --num-attention-heads=${MEGA_HEADS:-32} \
  --seq-length=${MEGA_SEQ:-2048} --max-position-embeddings=${MEGA_SEQ:-2048} \
  --micro-batch-size=${MEGA_MBS:-1} --global-batch-size=${MEGA_GBS:-32} \
  --train-iters=$TRAIN_ITERS --lr-decay-iters=$TRAIN_ITERS --lr-warmup-fraction=.01 \
  --lr=0.00015 --lr-decay-style=cosine --min-lr=1.0e-5 --weight-decay=1e-2 --clip-grad=1.0 \
  --tokenizer-type=NullTokenizer --vocab-size=4095 --mock-data --split=100,0,0 \
  --distributed-backend=nccl --transformer-impl=local --use-mcore-models \
  --use-distributed-optimizer --no-gradient-accumulation-fusion --attention-softmax-in-fp32 \
  --bf16 --log-interval=1 --eval-iters=0 --eval-interval=100000 \
  2>&1 | tee "$OUTDIR/train_${HN}.log"
EC=${PIPESTATUS[0]}
echo "[$HN] TORCHRUN_EXIT: $EC"
