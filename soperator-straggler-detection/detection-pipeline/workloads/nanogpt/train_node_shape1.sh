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
# Real bug found live (this session, a 6-node/48-GPU cluster):
# --gradient_accumulation_steps below was hardcoded to 32, assuming
# this project's own original 2-node/8-GPU (world_size=16) dev cluster
# -- train.py's own assertion (gradient_accumulation_steps %
# ddp_world_size == 0) fails on any real world size that doesn't
# happen to divide it evenly (32 % 48 != 0 here). 32 was exactly 2x
# the original world_size (16) -- preserved as the same real,
# live-discovered multiple of the ACTUAL world size instead (using
# NUM_NODES/GPUS_PER_NODE, already discovered above), so this holds
# for any real cluster shape, not just the one it was hardcoded for.
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=v1beta_shape1 --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  train.py "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../nanogpt-base/config/train_shakespeare_char.py" \
  --n_layer=12 --n_head=12 --n_embd=768 --block_size=512 --dropout=0.0 \
  --batch_size=12 --gradient_accumulation_steps=$((2 * NUM_NODES * GPUS_PER_NODE)) \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=200 \
  --eval_interval=2000 --eval_iters=50 --log_interval=20 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
