#!/bin/bash
set -u
# P26 Step 0 -- same train_resnet.py (unmodified: ResNet18, DDP, synthetic
# data, batch=32, image=224, cudnn disabled) as the original P18i/j/k
# feasibility-phase validation, wired to THIS project's current dump-dir/
# NCCL-Inspector convention (node_aggregator_ref.py-compatible), instead
# of P18i's old hardcoded /tmp dump dir + local-buffer classifier.py path.
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

export CUDA_MODULE_LOADING=EAGER
# Real bug found live (this session): this hardcoded /root/nccl-2.28-src
# path assumed this project's own original dev-cluster convention -- a
# real, different clone location (e.g. under /home/<user>/...) never has
# anything at that literal path, so NCCL_PROFILER_PLUGIN pointed at a
# file that doesn't exist and no dump files were ever produced. Derived
# from this script's own real location instead (same 2-levels-up
# PKG_ROOT convention install.sh/environment.sh already use) -- this
# always matches install.sh's own real build output
# ($PKG_ROOT/inspector-plugin/libnccl-profiler-inspector.so, confirmed
# live by install.sh's own "Inspector plugin built: ..." message),
# regardless of where this package was actually cloned.
_PKG_ROOT_FOR_PLUGIN_FIX="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export NCCL_PROFILER_PLUGIN="$_PKG_ROOT_FOR_PLUGIN_FIX/inspector-plugin/libnccl-profiler-inspector.so"
export NCCL_INSPECTOR_ENABLE=1
export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
export NCCL_INSPECTOR_PROM_DUMP=0

export MAX_ITERS=$STEPS
export BATCH_SIZE=${BATCH_SIZE_OVERRIDE:-32}
export IMAGE_SIZE=${IMAGE_SIZE:-224}
export LOG_INTERVAL=10
# INJECT_* left at their env defaults (INJECT_ENABLE=0) for the healthy/
# clock-lock runs; a separate invocation sets INJECT_ENABLE=1 explicitly
# for the burst/jitter-blind-spot re-check.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=p26_resnet --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"/train_resnet.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
