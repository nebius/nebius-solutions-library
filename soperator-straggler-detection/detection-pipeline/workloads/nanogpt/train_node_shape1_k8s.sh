#!/bin/bash
# K8s-native per-pod training script for Shape 1 (nanoGPT).
# Sibling to train_node_shape1.sh (Slurm) -- identical except it sources
# cluster_topology_k8s.sh instead of cluster_topology.sh. Zero changes to
# the detection logic, Inspector wiring, or training configuration.
#
# Invoked by the K8s training Job's entrypoint command per pod.
# Required env vars (set in Job pod template):
#   JOB_COMPLETION_INDEX       -- pod rank (0-based, set by K8s indexed Job)
#   TRAINING_NUM_NODES         -- total pod count
#   TRAINING_RDZV_HOST         -- rendezvous ClusterIP Service DNS
#   TRAINING_GPUS_PER_NODE     -- GPUs per node (8 for H200)
#   NCCL_INSPECTOR_DUMP_DIR    -- dump dir (EmptyDir mount shared with aggregator sidecar)
#   TRAINING_NCCL_LIB_PATH     -- NCCL 2.32.3 lib dir for full pinning (optional)
#   PIN_MODE                   -- auto|full|dump|off (default auto)
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # inside-pod path; shared with aggregator sidecar via EmptyDir volume

# Resolve both to absolute via this script's OWN known location, not the
# ambient cwd -- same real bug fix as the Slurm sibling (train_node_shape1.sh):
# cwd here is not reliably the caller's launch dir, and a relative OUTDIR/
# DUMPDIR also breaks once this script cd's into its own work dir below.
# DUMPDIR here is already an absolute EmptyDir mount path, so this is a
# no-op for it in practice -- it only guards a future caller passing it relative.
_PKG_ROOT_FOR_OUTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$OUTDIR" in /*) ;; *) OUTDIR="$_PKG_ROOT_FOR_OUTDIR/$OUTDIR" ;; esac
case "$DUMPDIR" in /*) ;; *) DUMPDIR="$_PKG_ROOT_FOR_OUTDIR/$DUMPDIR" ;; esac

# Environment self-checks (NCCL plugin, rank uniqueness, DCGM availability)

_CHECKS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/k8s/startup_checks.sh"
_cw=0
while [ $_cw -lt 60 ] && [ ! -f "$_CHECKS_SCRIPT" ]; do sleep 2; _cw=$((_cw + 2)); done
if [ -f "$_CHECKS_SCRIPT" ]; then
    source "$_CHECKS_SCRIPT"
else
    echo "[STARTUP] [$(hostname)] WARNING: $_CHECKS_SCRIPT not found -- NCCL plugin," \
         "rank-uniqueness, and hardware-diagnostic self-checks did NOT run this job." >&2
fi

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology_k8s.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

# Inspector plugin: same repo-relative path as Slurm version -- the workspace
# PVC mounts the full pipeline tree at /workspace, so the .so is at the same
# relative path regardless of where it was built.
_PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# NFS stale-cache fix: stat the plugin file before export to force the NFS client
# to fetch fresh directory metadata from the server. Without this, a fresh workspace
# (init build just completed) can show a stale cached listing without the .so, causing
# NCCL to silently skip the plugin (confirmed on a fresh-clone README self-run).
_PLUGIN_PATH="$_PKG_ROOT/inspector-plugin/libnccl-profiler-inspector.so"
if [ "${INSPECTOR:-on}" = "off" ]; then
    # True baseline arm: Inspector completely disabled. No plugin, no dump threads.
    # Used by pinning A/B harness (ab_driver_k8s.sh) for the off condition.
    export NCCL_INSPECTOR_ENABLE=0
    PIN_MODE="${PIN_MODE:-off}"
    echo "[STARTUP] Inspector=off: plugin NOT loaded (A/B baseline arm)" >&2
else
    # NFS stale-cache fix: poll until the plugin file is visible (NFS ac-cache can lag
    # behind a fresh init build by up to 30s on newly mounted PVCs). Without this,
    # NCCL silently skips the plugin (dlopen ENOENT → no dumps → no detection).
    _pi=0
    while [ $_pi -lt 30 ] && [ ! -f "$_PLUGIN_PATH" ]; do sleep 1; _pi=$((_pi+1)); done
    if [ -f "$_PLUGIN_PATH" ]; then
        export NCCL_PROFILER_PLUGIN="$_PLUGIN_PATH"
        echo "[STARTUP] Inspector plugin found after ${_pi}s: $_PLUGIN_PATH" >&2
    else
        echo "[STARTUP] [WARN] Inspector plugin not visible after 30s (NFS stale cache?); running without plugin" >&2
    fi
    export NCCL_INSPECTOR_ENABLE=1
    export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"
    export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
    export NCCL_INSPECTOR_DUMP_DIR="$DUMPDIR"
    export NCCL_INSPECTOR_PROM_DUMP=0
fi

# CPU pinning -- same pin_env.sh/pin_prepare() pattern as train_node_megatron.sh.
# rank_setup.py reads /sys/devices/system/cpu (accessible in K8s pods via sysfs
# bind-mount) and nvidia-smi PCI bus IDs (requires nvidia.com/gpu resource in pod).
# os.sched_getaffinity(0) returns all CPUs when no CPU limits are set (our job
# spec uses requests only), identical to the Slurm --cpus-per-task=128 convention.
# taskset on own process needs no special capability.
# Degrades to unpinned with [PIN][WARN] on any failure -- never blocks the job.
PIN_MODE="${PIN_MODE:-auto}"
export PIN_MODE
export PIN_LOG_DIR="${PIN_LOG_DIR:-$OUTDIR/pinlogs}"
source "$_PKG_ROOT/pinning/pin_env.sh"
pin_prepare

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# NCCL_VERSION_PROBE: each rank prints the NCCL version actually loaded at runtime
# (confirms NCCL 2.32.3 override is active for full pinning).
export NCCL_VERSION_PROBE="${NCCL_VERSION_PROBE:-1}"

torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=k8s_shape1 --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "${PIN_WRAP[@]}" \
  train.py "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../nanogpt-base/config/train_shakespeare_char.py" \
  --n_layer=12 --n_head=12 --n_embd=768 --block_size=512 --dropout=0.0 \
  --batch_size=12 --gradient_accumulation_steps=$((2 * NUM_NODES * GPUS_PER_NODE)) \
  --max_iters=$STEPS --lr_decay_iters=$STEPS --warmup_iters=200 \
  --eval_interval=2000 --eval_iters=50 --log_interval=20 \
  --always_save_checkpoint=False \
  --compile=False --out_dir=$OUTDIR/ckpt \
  2>&1 | tee $OUTDIR/train_${HN}.log
EC=${PIPESTATUS[0]}
echo "[$HN] TORCHRUN_EXIT: $EC"
