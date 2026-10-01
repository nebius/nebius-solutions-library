#!/bin/bash
set -u
# Real customer-shaped workload for straggler-detection validation:
# Megatron-LM GPT pretraining, TP=4/PP=4/DP=3 (48 GPUs, 6 nodes, 2 nodes
# per DP replica, 2 PP stages per node, each stage a TP4 group -- uses
# Megatron-core's --use-tp-pp-dp-mapping so TP is fastest-varying and PP
# is next-fastest, giving exactly this node layout with this repo's own
# NODE_RANK-by-position-in-NODE_LIST convention, one task/node, 8
# ranks/node).
TRAIN_ITERS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_BASE=$4
SLEEP_MS=${5:-0}
TARGET_RANKS=${6:-}
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes

# Same dynamic container-mount fix every other workload in this repo
# already uses (see workloads/nanogpt/run_straggler_nanogpt.sh) -- the
# container only ships /root, /usr/lib*, /tmp by default, so this
# repo's own real clone location and the Megatron-LM checkout living
# inside it must be added explicitly, or every rank fails immediately
# with "No such file or directory".
_SCRIPT_DIR_FOR_MOUNT_FIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT_FOR_MOUNT_FIX="$(cd "$_SCRIPT_DIR_FOR_MOUNT_FIX/../.." && pwd)"
_NCCL_SRC_DIR_FOR_MOUNT_FIX="$(cd "$(dirname "$(dirname "$NCCL_LIB_PATH")")" && pwd)"
for _p in "$_PKG_ROOT_FOR_MOUNT_FIX" "$_NCCL_SRC_DIR_FOR_MOUNT_FIX"; do
  case "$_p" in
    /root|/root/*) ;;
    *) MOUNTS="$MOUNTS,$_p:$_p" ;;
  esac
done
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "### megatron-tp4pp4dp3 train_iters=$TRAIN_ITERS outdir=$OUTDIR port=$PORT sleep_ms=$SLEEP_MS target_ranks=$TARGET_RANKS"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

# Real bug found live (this session): megatron.core.datasets.helpers_cpp
# is JIT-compiled via a plain `make` on first import (see Megatron-LM's
# own compile_helpers() docstring: "Make sure this is invoked on a
# single process" -- an acknowledged hazard Megatron's own launcher
# never actually enforces). With multiple ranks/nodes importing it
# simultaneously against the SAME shared NFS checkout, one rank's
# partially-written .so can crash another rank's import
# (ModuleNotFoundError: No module named 'megatron.core.datasets.
# helpers_cpp') -- a real race, not a flaky one-off, and it reproduces
# again any time this .so doesn't already exist (a fresh checkout, or
# this environment resetting). Fixed the same way install.sh already
# builds the NCCL Inspector plugin once, centrally, before any rank
# needs it: a single, blocking, single-task `make` run here, BEFORE the
# real multi-rank job launches below -- by the time any rank imports
# helpers_cpp, the .so already exists and every import is just a safe
# read of a complete file. `make`'s own dependency-timestamp check
# makes this a no-op (a few ms) on every run after the first real
# build, so it's always safe to run unconditionally, not just
# conditionally on first use.
MEGATRON_DIR_FOR_PREBUILD="$(cd "$(dirname "${BASH_SOURCE[0]}")/Megatron-LM" && pwd)"
_PREBUILD_NODE="$(echo "$NODE_LIST" | cut -d, -f1)"
echo "--- pre-building megatron.core.datasets.helpers_cpp + precompiling all .pyc bytecode once on $_PREBUILD_NODE (single process, before any rank races to do either) ---"
# Real bug found live (this session, a SECOND, related race -- not just
# helpers_cpp): with a fresh/reset checkout (no .pyc cache yet), all 16
# ranks import the entire megatron.core/megatron.training package tree
# for the first time nearly simultaneously, each racing to write its OWN
# compiled bytecode cache (__pycache__/*.pyc) to the SAME shared NFS
# path. Confirmed directly: a fresh checkout failed with "cannot import
# name 'GlobalMemoryBuffer' from partially initialized module
# 'megatron.core.utils' (most likely due to a circular import)" -- but
# on a DIFFERENT rank each run (0/2 one run, 7 the next), not the same
# one every time, which is the real signature of a race, not a genuine
# circular-import logic bug (that would fail identically every time
# regardless of which process hits it first). `python3 -m compileall`
# run once here, single-process, writes every real .pyc ahead of time --
# by the time any rank imports anything, the cache is already complete,
# same "build once, centrally, before concurrent access" fix as
# helpers_cpp above, same single pre-build step.
srun --nodes=1 --ntasks=1 -w "$_PREBUILD_NODE" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  bash -c "make -C '$MEGATRON_DIR_FOR_PREBUILD/megatron/core/datasets' && python3 -m compileall -q '$MEGATRON_DIR_FOR_PREBUILD'" \
  || { echo "FATAL: megatron pre-build (helpers_cpp and/or bytecode precompile) failed -- see output above." >&2; exit 1; }
echo "--- megatron pre-build confirmed (helpers_cpp + bytecode cache, or already up to date) ---"

srun --nodes="$NUM_NODES" --ntasks="$NUM_NODES" --ntasks-per-node=1 --gpus-per-node="$GPUS_PER_NODE" -w "$NODE_LIST" \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_SLEEP_MS="$SLEEP_MS",STRAGGLER_TARGET_RANKS="$TARGET_RANKS" \
  bash -c 'DD="'"$DUMPDIR_BASE"'/$(hostname)"; bash "'"$SCRIPT_DIR"'/train_node_megatron.sh" '"$TRAIN_ITERS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
