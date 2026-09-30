#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPBASE=$4
_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
NODE_RANK=$NODE_RANK
mkdir -p "$OUTDIR"
export LD_LIBRARY_PATH="${NCCL_LIB_PATH:-}:${LD_LIBRARY_PATH:-}"
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
export NCCL_INSPECTOR_DUMP_VERBOSE=1
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_PROM_DUMP=0
export MAX_ITERS=$STEPS
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# P28 -- 2 ranks per node (rank0,1=worker-0=PP stage0's TP pair;
# rank2,3=worker-1=PP stage1's TP pair); BOTH local ranks' Inspector
# dumps land in the SAME per-node dir (one aggregator per node), which
# must be the real discovered hostname to match run.sh's own aggregator
# watch path ($DUMP_DIR_BASE/$node) -- a dump_w$NODE_RANK convention is
# never watched by anything and was a real routing bug.
DUMPDIR="$DUMPBASE/$HN"
mkdir -p "$DUMPDIR"
export NCCL_INSPECTOR_DUMP_DIR=$DUMPDIR
torchrun --nnodes="$NUM_NODES" --nproc_per_node=2 --node_rank=$NODE_RANK \
  --rdzv_id=p28_hybrid --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train_hybrid_tp_pp.py \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
