#!/bin/bash
set -u
TRAIN_ITERS=$1
OUTDIR=$2
PORT=$3
DUMPDIR=$4   # absolute, host-visible path -- a relative
             # NCCL_INSPECTOR_DUMP_DIR silently breaks dump output once the
             # training process chdir's (real bug found in the nanoGPT
             # workload) -- this is always passed in absolute already.

# Resolve both to absolute via this script's OWN known location, not the
# ambient cwd -- cwd at this point is not reliably the caller's launch dir
# (varies by container/pyxis behavior per node; confirmed live, not just
# theoretical: a relative OUTDIR produced zero rank-scoped alerts for an
# entire fault trial on one run, then silently vanishing output entirely
# -- different failure, same root cause -- on a second, otherwise identical
# run). A relative OUTDIR/DUMPDIR also breaks once this script cd's into
# $MEGATRON_DIR below regardless of cwd at start.
_PKG_ROOT_FOR_OUTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
case "$OUTDIR" in /*) ;; *) OUTDIR="$_PKG_ROOT_FOR_OUTDIR/$OUTDIR" ;; esac
case "$DUMPDIR" in /*) ;; *) DUMPDIR="$_PKG_ROOT_FOR_OUTDIR/$DUMPDIR" ;; esac

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_job_nodes
cluster_topology_discover_rank
cluster_topology_discover_rdzv_host
cluster_topology_discover_gpu_count
HN=$(hostname)
RANK=$NODE_RANK

mkdir -p "$DUMPDIR" "$OUTDIR"

# Repo-relative NCCL Inspector wiring (install.sh's own real build output),
# not a hardcoded /root path -- same convention every other workload here
# uses. NCCL_INSPECTOR_DUMP_VERBOSE defaults to lean mode (0) now, same as
# the other 17 launch scripts in this repo -- verbose=1 at this cluster's
# scale (48 ranks) fills a shared disk volume in well under an hour.
_PKG_ROOT_FOR_PLUGIN_FIX="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# INSPECTOR=off (A/B control arm): no profiler plugin loaded at all. Default on.
if [ "${INSPECTOR:-on}" = "on" ]; then
export NCCL_PROFILER_PLUGIN="$_PKG_ROOT_FOR_PLUGIN_FIX/inspector-plugin/libnccl-profiler-inspector.so"
export NCCL_INSPECTOR_ENABLE=1
fi
export NCCL_INSPECTOR_DUMP_VERBOSE="${NCCL_INSPECTOR_DUMP_VERBOSE:-0}"
export NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS=500
export NCCL_INSPECTOR_DUMP_DIR="$DUMPDIR"
export NCCL_INSPECTOR_PROM_DUMP=0

# STRAGGLER_SLEEP_MS / STRAGGLER_TARGET_RANKS / STORAGE_FAULT_FILE_MB /
# STORAGE_FAULT_TARGET_RANKS read directly by pretrain_gpt_straggler.py
# (own copy of upstream pretrain_gpt.py, same convention as
# workloads/nanogpt/train.py) -- passed through via
# --export=ALL from the launching srun command.

# Pre-Blackwell (H200/Hopper) with TP>1, non-FSDP: mcore-run-on-slurm
# skill confirms this asserts (not a silent deadlock) if unset.
export CUDA_DEVICE_MAX_CONNECTIONS=1

# Real bug found live (this session, a THIRD race in the same class as
# helpers_cpp and the .pyc bytecode cache above): Triton JIT-compiles
# each kernel to a content-hashed dir under ~/.triton/cache/<hash>/ on
# first use, writing cuda_utils.so non-atomically. With the default
# cache dir living under /root (shared across every rank on every node
# via this script's own --container-mounts="...,/root:/root,..."),
# multiple ranks racing to compile the SAME kernel hash for the first
# time can leave one rank reading another's partially-written .so --
# confirmed live: "ImportError: .../cuda_utils.so: cannot open shared
# object file" on ranks 0,1,4,5 (a different subset each run), the same
# race signature as the two bugs above. NOTE this can't be fixed here
# with a single export: this shell is one process per NODE, and
# torchrun below forks GPUS_PER_NODE (8) local-rank children from it
# that would all inherit one identical TRITON_CACHE_DIR, still racing
# with each other. The real per-local-rank split is done in
# pretrain_gpt_straggler.py itself (reads $LOCAL_RANK, which torchrun
# sets uniquely per child, before any triton-using import) -- see the
# patch at the top of that file.

MEGATRON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/Megatron-LM" && pwd)"
cd "$MEGATRON_DIR"

# Tiny GPT config, chosen to keep a full TP4/PP4/DP3 run to a few
# minutes: num-layers=8 (2/PP-stage), hidden-size=512 (128/TP-shard),
# num-attention-heads=8 (2/TP-shard), seq-length=512. global-batch-size
# =12 is divisible by data-parallel-size(3)*micro-batch-size(1)=3.
# NullTokenizer + --mock-data avoids needing a real preprocessed
# dataset/vocab on disk -- --vocab-size=4095 (not a round 4096) because
# Megatron's own MockGPTLowLevelDataset generates token ids up to
# ~4094 regardless of --vocab-size (hardcoded max_sequence_length=4096);
# a smaller vocab causes silent out-of-bounds embedding lookups
# (device-side assert). --transformer-impl=local (not transformer_engine)
# because this repo's own established MOUNTS (bind-mounting host
# /usr/lib/x86_64-linux-gnu into the container) shadows the container's
# own libcublasLt.so, breaking TE's cublasLtGetVersion symbol lookup --
# the bundled nanoGPT workloads never hit this since they don't use TE.
# Shape/pinning knobs -- defaults reproduce the original TP4/PP4/DP3 tiny config.
# MEGA_TP/PP/LAYERS/HIDDEN/HEADS/SEQ/MBS/GBS override it (e.g. 16-GPU 8B-class run).
# CPU pinning of Inspector's threads (NEW, UNVALIDATED) via the shared wrapper pinning/pin_env.sh.
#   PIN_MODE=auto|full|dump|off   default: auto when INSPECTOR is on, off when INSPECTOR=off.
#   auto = pin NCCL's profiler thread too iff the NCCL actually in use supports it (>= 2.32.x), else dump thread only.
#   NCCL_OVERRIDE_DIR (or the dir recorded by pinning/nccl/fetch_build_nccl.sh) selects a newer NCCL.
# Any problem degrades to an unpinned run with a [PIN][WARN] line -- see pinning/README.md.
if [ "${INSPECTOR:-on}" = "off" ]; then PIN_MODE="${PIN_MODE:-off}"; else PIN_MODE="${PIN_MODE:-auto}"; fi
export PIN_MODE
export PIN_LOG_DIR="${PIN_LOG_DIR:-$OUTDIR/pinlogs}"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../pinning" && pwd)/pin_env.sh"
pin_prepare            # sets PIN_WRAP (empty when pinning is off/unavailable); also starts the background verifier

# Env-robustness closeout -- confirms the real, currently active NCCL/
# RDMA networking config survives MULTIPLE concurrent communicators
# (what every real TP>1-or-PP>1 job, including this one, already
# creates) before trusting it with the real job. Same "never abort,
# degrade loudly" contract as pin_prepare above. NET_SELFCHECK=0 skips it.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../self-checks" && pwd)/net_selfcheck.sh"
net_selfcheck_run "$((PORT + 7))"

# NCCL_VERSION_PROBE=1: each rank prints ncclGetVersion() of the NCCL actually loaded (runtime proof).
torchrun --nnodes="$NUM_NODES" --nproc_per_node="$GPUS_PER_NODE" --node_rank=$RANK \
  --rdzv_id=megatron_run --rdzv_backend=c10d --rdzv_endpoint=$RDZV_HOST:$PORT \
  "${PIN_WRAP[@]}" \
  pretrain_gpt_straggler.py \
  --tensor-model-parallel-size=${MEGA_TP:-4} --pipeline-model-parallel-size=${MEGA_PP:-4} --use-tp-pp-dp-mapping \
  --num-layers=${MEGA_LAYERS:-8} --hidden-size=${MEGA_HIDDEN:-512} --num-attention-heads=${MEGA_HEADS:-8} \
  --seq-length=${MEGA_SEQ:-512} --max-position-embeddings=${MEGA_SEQ:-512} \
  --micro-batch-size=${MEGA_MBS:-1} --global-batch-size=${MEGA_GBS:-12} \
  --train-iters=$TRAIN_ITERS --lr-decay-iters=$TRAIN_ITERS --lr-warmup-fraction=.01 \
  --lr=0.00015 --lr-decay-style=cosine --min-lr=1.0e-5 --weight-decay=1e-2 --clip-grad=1.0 \
  --tokenizer-type=NullTokenizer --vocab-size=4095 --mock-data --split=100,0,0 \
  --distributed-backend=nccl --transformer-impl=local --use-mcore-models \
  --use-distributed-optimizer --no-gradient-accumulation-fusion --attention-softmax-in-fp32 \
  --bf16 --log-interval=1 --eval-iters=0 --eval-interval=100000 \
  > $OUTDIR/train_$HN.log 2>&1
EC=$?
echo "[$HN] TORCHRUN_EXIT: $EC"
