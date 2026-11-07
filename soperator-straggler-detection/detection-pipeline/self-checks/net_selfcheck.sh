#!/bin/bash
# net_selfcheck.sh -- SOURCE this from any multi-node launcher that is
# about to run a real job with >1 concurrent NCCL communicator (TP>1 or
# PP>1). Mirrors pinning/pin_env.sh's own contract: never aborts the
# real job, degrades to a safer config with a loud log line on failure.
#
#   source /path/to/self-checks/net_selfcheck.sh   # needs: NUM_NODES, GPUS_PER_NODE, RDZV_HOST, RANK, HN
#   net_selfcheck_run <port>                        # runs the probe; on failure, exports the RDMA-disabling fallback vars
#
# Inputs (env): NET_SELFCHECK=1|0 (default 1)   NET_SELFCHECK_TIMEOUT_S (default 60)
# Every node's copy of the calling launcher invokes this at the same
# point (same outer srun that starts the real job), so the probe's own
# torchrun rendezvous works exactly like the real training torchrun's
# does -- no separate coordination needed.
_NETCHK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

net_selfcheck_run() {
  local port="$1" hn="${HN:-$(hostname)}"
  if [ "${NET_SELFCHECK:-1}" = "0" ]; then
    echo "[NET-PREFLIGHT] host=$hn skipped (NET_SELFCHECK=0) -- assuming current networking config is safe, unverified this run"
    return 0
  fi
  local out; out="$(mktemp)"
  timeout "${NET_SELFCHECK_TIMEOUT_S:-60}" torchrun \
    --nnodes="$NUM_NODES" --nproc_per_node="${GPUS_PER_NODE:-1}" --node_rank="$RANK" \
    --rdzv_id=net_selfcheck --rdzv_backend=c10d --rdzv_endpoint="$RDZV_HOST:$port" \
    "$_NETCHK_DIR/nccl_multigroup_probe.py" > "$out" 2>&1
  local ec=$?
  if [ "$ec" -eq 0 ]; then
    echo "[NET-PREFLIGHT] host=$hn PASS -- concurrent multi-communicator create/use/destroy survived with the real, currently active networking config (see $out for per-rank detail)"
    rm -f "$out"
    return 0
  fi
  # ec=124 is `timeout`'s own code for "killed after deadline" -- a hang,
  # not necessarily the same failure mode as an immediate crash, but still
  # something the real job should not inherit blind.
  local how="exit $ec"
  [ "$ec" -eq 124 ] && how="TIMED OUT after ${NET_SELFCHECK_TIMEOUT_S:-60}s (hang, not necessarily a crash)"
  echo "[NET-PREFLIGHT] host=$hn FAIL ($how) -- concurrent multi-communicator test did NOT survive with the current networking config. Probe output:"
  sed 's/^/[NET-PREFLIGHT]   /' "$out"
  echo "[NET-PREFLIGHT] host=$hn falling back to NCCL_SHARP_DISABLE=1 NCCL_NET_GDR_DISABLE=1 NCCL_NET=Socket for the real job -- slower, but avoids the crash this probe just reproduced. This is the SAME workaround already in use on the Kubernetes side for this exact failure mode."
  export NCCL_SHARP_DISABLE=1
  export NCCL_NET_GDR_DISABLE=1
  export NCCL_NET=Socket
  export NET_SELFCHECK_FELL_BACK=1
  rm -f "$out"
  return 1
}
