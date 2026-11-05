#!/bin/bash
# K8s-native aggregator supervisor -- sibling to observability/run_aggregator_supervised.sh.
# Differences from the Slurm version:
#   1. Uses NODE_NAME (K8s Downward API spec.nodeName) as the hostname label in
#      VictoriaMetrics metrics -- the node's real hostname, not the ephemeral pod name.
#   2. Accepts an explicit job ID (--slurm-job-id) so the aggregator can scope
#      metrics by K8s Job name instead of calling squeue (unavailable in pods).
#   3. Appends /api/v1/import/prometheus to the VM base URL (same as Slurm version).
#   4. Runs in a supervisor restart loop identical to the Slurm version.
#
# Usage (from aggregator sidecar container):
#   run_aggregator_k8s.sh <dump_dir> <vm_base_url> <k8s_job_name> [log_file]
#   Environment: NODE_NAME must be set (Downward API fieldRef: spec.nodeName).
set -u
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PKG_ROOT="$(cd "$_HERE/.." && pwd)"

DUMP_DIR="${1:?usage: run_aggregator_k8s.sh <dump_dir> <vm_base_url> <k8s_job_name> [log_file]}"
VM_URL_BASE="${2:?usage: run_aggregator_k8s.sh <dump_dir> <vm_base_url> <k8s_job_name> [log_file]}"
K8S_JOB_NAME="${3:?usage: run_aggregator_k8s.sh <dump_dir> <vm_base_url> <k8s_job_name> [log_file]}"
LOG="${4:-/dev/stderr}"
# NODE_NAME is injected via Downward API in the pod spec.
HOSTNAME_ARG="${NODE_NAME:?NODE_NAME env var not set -- ensure it is injected via Downward API (fieldRef: spec.nodeName) in the pod spec.}"

# Append the VictoriaMetrics ingestion path (same as Slurm version's convention).
VM_URL="${VM_URL_BASE%/}/api/v1/import/prometheus"

mkdir -p "$DUMP_DIR"

AGG_DURATION_S=315360000  # 10 years, same as Slurm version

echo "[agg-k8s:$HOSTNAME_ARG] starting, dump_dir=$DUMP_DIR vm_url=$VM_URL job=$K8S_JOB_NAME" | tee -a "$LOG"
while true; do
    START_TS=$(date -u +%FT%TZ)
    echo "[agg-k8s:$HOSTNAME_ARG] launching node_aggregator_ref.py at $START_TS" | tee -a "$LOG"
    python3 "$_PKG_ROOT/aggregator/node_aggregator_ref.py" \
        "$DUMP_DIR" \
        "$HOSTNAME_ARG" \
        "$VM_URL" \
        --duration "$AGG_DURATION_S" \
        --slurm-job-id "$K8S_JOB_NAME" \
        >> "$LOG" 2>&1
    EC=$?
    END_TS=$(date -u +%FT%TZ)
    echo "[agg-k8s:$HOSTNAME_ARG] node_aggregator_ref.py EXITED exit_code=$EC start=$START_TS end=$END_TS -- relaunching in 3s" | tee -a "$LOG"
    sleep 3
done
