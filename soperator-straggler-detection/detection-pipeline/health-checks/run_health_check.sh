#!/bin/bash
set -u
LABEL=${1:-check}
# Stage 2 cluster-topology-agnostic fix: BASE/OUTDIR used to be hardcoded
# absolute paths into this project's original development-host layout
# (/root/P4d_clean/health) -- BASE now resolves to this script's own real
# location; OUTDIR defaults to a var/ directory alongside the package
# (overridable via HEALTH_CHECK_OUTDIR for a real deployment's own
# chosen data directory), matching the same convention already used for
# alert_engine.py's IOWAIT_LOG_DIR.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="${HEALTH_CHECK_OUTDIR:-$BASE/../var/health_check_runs}/$LABEL"
mkdir -p "$OUTDIR"
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/cluster_topology.sh"
source "$_LIB"
cluster_topology_available_nodes
# Real bash array from NODE_LIST (comma-separated) -- plain `for` below,
# not `| while read`, so nothing here runs in a subshell that would lose
# array/variable updates once the loop ends.
IFS=',' read -ra NODES <<< "$NODE_LIST"

echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then
  echo "ABORT: soperatorchecks job active"; exit 1
fi

# Stage 2 cluster-topology-agnostic fix: used to be exactly 2 hardcoded
# `srun ... -w worker-0`/`-w worker-1` invocations -- now one per real,
# discovered node, whatever the real count/names are.
echo "=== running matmul+sleep benchmark on every real discovered node ==="
for node in "${NODES[@]}"; do
  srun --nodes=1 --ntasks=1 --gpus-per-node="$GPUS_PER_NODE" -w "$node" \
    --container-image="$IMAGE" --container-mounts="$MOUNTS" \
    python3 "$BASE/bench_all_gpus.py" > "$OUTDIR/bench_$node.json" 2>"$OUTDIR/bench_$node.err" &
done
wait

echo "=== gathering NVML counters ==="
for node in "${NODES[@]}"; do
  bash "$BASE/nvml_counters.sh" "$node" > "$OUTDIR/nvml_$node.csv"
done

echo "=== REPORT: $LABEL ==="
REPORT_ARGS=()
for node in "${NODES[@]}"; do
  REPORT_ARGS+=("$OUTDIR/bench_$node.json" "$OUTDIR/nvml_$node.csv")
done
python3 "$BASE/gpu_health_check.py" "${REPORT_ARGS[@]}"

for node in "${NODES[@]}"; do
  scontrol update nodename="$node" state=resume >/dev/null 2>&1
done
