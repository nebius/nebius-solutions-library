#!/bin/bash
# Launch an FSDP training job with the straggler-detection pipeline on Kubernetes.
# Source cluster_k8s.env first, then run this script.
#
# Usage:
#   source cluster_k8s.env
#   k8s/run_fsdp_k8s.sh [steps] [port] [pin_mode] [sleep_ms] [target_ranks] [straggler_mode] [straggler_phase]
#   Defaults: 500 steps, port 29510, pin_mode=dump, sleep_ms=0, targets="", mode=constant, phase=backward
#
# FSDP communicator structure on 2 nodes / 16 GPUs:
#   AllGather (parameter unsharding): 16-member, every forward pass
#   ReduceScatter (gradient sharding): 16-member, every backward pass
# Both are detectable by _check_mean and _check_mean_tiered (same as nanoGPT DDP).
set -uo pipefail
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="$K8S_DIR/manifests"

CLUSTER_ENV="$PKG_ROOT/cluster_k8s.env"
[ -f "$CLUSTER_ENV" ] || { echo "FATAL: source cluster_k8s.env first"; exit 1; }
source "$CLUSTER_ENV"

for v in K8S_NAMESPACE NUM_GPU_NODES GPUS_PER_NODE VM_URL_SVC; do
  [ -z "${!v:-}" ] && { echo "FATAL: cluster_k8s.env missing $v"; exit 1; }
done

TRAIN_STEPS="${1:-500}"
TRAIN_PORT="${2:-29510}"
PIN_MODE="${3:-dump}"
SLEEP_MS="${4:-0}"
TARGET_RANKS="${5:-}"
STRAGGLER_MODE="${6:-constant}"
STRAGGLER_PHASE="${7:-backward}"
NS="$K8S_NAMESPACE"
TRAIN_NUM_NODES="$NUM_GPU_NODES"
TRAIN_GPUS="$GPUS_PER_NODE"

_RUN_ID="$(date +%s)"
JOB_RUN_NAME="fsdp-${_RUN_ID}"

echo "[run_fsdp_k8s.sh] $JOB_RUN_NAME  steps=$TRAIN_STEPS nodes=$TRAIN_NUM_NODES sleep_ms=$SLEEP_MS targets='$TARGET_RANKS' mode=$STRAGGLER_MODE phase=$STRAGGLER_PHASE"

_ACTIVE="$(kubectl get jobs -n "$NS" -l straggler.nebius.ai/component=training \
    --field-selector='status.active=1' --no-headers 2>/dev/null | wc -l)"
[ "${_ACTIVE:-0}" -gt 0 ] && { echo "ABORT: active training job already running in $NS"; exit 1; }

kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${JOB_RUN_NAME}-headless
  namespace: ${NS}
  labels:
    straggler.nebius.ai/run: ${JOB_RUN_NAME}
spec:
  selector:
    straggler.nebius.ai/run: ${JOB_RUN_NAME}
  clusterIP: None
  ports:
    - port: ${TRAIN_PORT}
      targetPort: ${TRAIN_PORT}
EOF

# Use NCCL 2.32.3 if built (required for Inspector profiler_v5 support; NCCL 2.25.1 is v1/v2 only)
NCCL_LIB_PATH="${NCCL_232_LIB_PATH:-}"
export JOB_RUN_NAME TRAIN_STEPS TRAIN_PORT TRAIN_NUM_NODES TRAIN_GPUS VM_URL_SVC PIN_MODE NCCL_LIB_PATH
export SLEEP_MS TARGET_RANKS STRAGGLER_MODE STRAGGLER_PHASE

envsubst '${JOB_RUN_NAME} ${TRAIN_STEPS} ${TRAIN_PORT} ${TRAIN_NUM_NODES} ${TRAIN_GPUS} ${VM_URL_SVC} ${PIN_MODE} ${NCCL_LIB_PATH} ${SLEEP_MS} ${TARGET_RANKS} ${STRAGGLER_MODE} ${STRAGGLER_PHASE}' \
    < "$MANIFESTS_DIR/07-fsdp-job.yaml.tmpl" | kubectl apply -f -

echo "[run_fsdp_k8s.sh] Job $JOB_RUN_NAME applied."
echo "  Watch:  kubectl get pods -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -w"
echo "  Alerts: kubectl logs -n $NS deployment/alert-engine -f | grep -v DCGM"
echo "  Stop:   kubectl delete job $JOB_RUN_NAME svc/${JOB_RUN_NAME}-headless -n $NS"
echo ""
echo "JOB_RUN_NAME=$JOB_RUN_NAME"
