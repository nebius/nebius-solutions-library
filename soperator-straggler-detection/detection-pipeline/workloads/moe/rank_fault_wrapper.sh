#!/bin/bash
# P23 (single-rank AllToAll fault test) -- torchrun injects RANK/LOCAL_RANK
# into each child process's environment BEFORE exec-ing it; this wrapper
# is what torchrun execs INSTEAD OF train_moe.py directly, so it can read
# its own (global) $RANK and conditionally apply a fault to ONLY the
# target rank, leaving every other rank's process completely unmodified.
#
# Deliberately NOT time-based or I/O-based -- a single string-equality
# check against an already-set environment variable, negligible and
# identical in cost regardless of which rank evaluates it, so this
# introduces no asymmetric timing into collective entry, only an
# asymmetric FAULT (the thing under test).
#
# Candidate A (per-rank NCCL_MAX_NCHANNELS) was tried first and found to
# be a real dead end, not just insufficient: NCCL rejects an asymmetric
# channel-count request across ranks of the SAME communicator outright
# (confirmed live: "NCCL Error 3: internal error" at the target rank's
# own all_to_all_single call, not a graceful per-rank negotiation).
#
# Candidate B (this file, active): LD_PRELOAD the QP rate-limit shim
# (qp_rate_limit_shim.so) for ONLY the target rank's process. This
# throttles at the RDMA layer (real ibv_modify_qp_rate_limit(), applied
# the instant this rank's own QPs reach RTS) -- entirely within this
# one process, no cross-rank NCCL negotiation involved, so it carries
# none of Candidate A's failure mode.
# Stage 2 cluster-topology-agnostic fix: both absolute paths below used
# to hardcode this project's original development-host layout
# (/root/P23_moe) -- resolved relative to this script's own real,
# installed location instead.
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_RANK="${FAULT_TARGET_RANK:-4}"
if [ "${RANK:-}" = "$TARGET_RANK" ]; then
  export LD_PRELOAD="$_HERE/qp_rate_limit_shim.so${LD_PRELOAD:+:$LD_PRELOAD}"
fi
exec python3 "$_HERE/train_moe.py" "$@"
