#!/bin/bash
# Launch a MoE training job with the straggler-detection pipeline on Kubernetes.
# Source cluster_k8s.env first, then run this script.
#
# Usage:
#   source cluster_k8s.env
#   k8s/run_moe_k8s.sh [steps] [port] [pin_mode] [sleep_ms] [target_ranks]
#   Defaults: 500 steps, port 29520, pin_mode=dump, sleep_ms=0, targets=""
#
# MoE communicator structure on 2 nodes / 16 GPUs:
#   AllToAll (expert dispatch/combine): 16-member, every forward pass
#   AllReduce (non-expert grad sync): 16-member, every backward pass
# Both are detectable by _check_mean and _check_mean_tiered.
# Note: AllToAll straggler signal is structurally different from AllReduce
# (non-uniform send, no hierarchical reduction). The AllReduce grad-sync path
# produces the same tiered signal as nanoGPT DDP.
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
TRAIN_PORT="${2:-29520}"
PIN_MODE="${3:-dump}"
SLEEP_MS="${4:-0}"
TARGET_RANKS="${5:-}"
# AllToAll-targeted fault injection (correct for MoE detection):
# MOE_FAULT_TARGET_RANK=N MOE_FAULT_DELAY_US=100000 injects 100ms before the
# AllToAll routing call in the FORWARD pass — this is what the tiered check detects.
MOE_FAULT_TARGET_RANK="${MOE_FAULT_TARGET_RANK:-}"
MOE_FAULT_DELAY_US="${MOE_FAULT_DELAY_US:-0}"
NS="$K8S_NAMESPACE"
TRAIN_NUM_NODES="$NUM_GPU_NODES"
TRAIN_GPUS="$GPUS_PER_NODE"

_RUN_ID="$(date +%s)"
JOB_RUN_NAME="moe-${_RUN_ID}"

echo "[run_moe_k8s.sh] $JOB_RUN_NAME  steps=$TRAIN_STEPS nodes=$TRAIN_NUM_NODES sleep_ms=$SLEEP_MS targets='$TARGET_RANKS'"

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

export JOB_RUN_NAME TRAIN_STEPS TRAIN_PORT TRAIN_NUM_NODES TRAIN_GPUS VM_URL_SVC PIN_MODE
export SLEEP_MS TARGET_RANKS

export JOB_RUN_NAME TRAIN_STEPS TRAIN_PORT TRAIN_NUM_NODES TRAIN_GPUS VM_URL_SVC PIN_MODE SLEEP_MS TARGET_RANKS MOE_FAULT_TARGET_RANK MOE_FAULT_DELAY_US
envsubst '${JOB_RUN_NAME} ${TRAIN_STEPS} ${TRAIN_PORT} ${TRAIN_NUM_NODES} ${TRAIN_GPUS} ${VM_URL_SVC} ${PIN_MODE} ${SLEEP_MS} ${TARGET_RANKS} ${MOE_FAULT_TARGET_RANK} ${MOE_FAULT_DELAY_US}' \
    < "$MANIFESTS_DIR/08-moe-job.yaml.tmpl" | kubectl apply -f -

echo "[run_moe_k8s.sh] Job $JOB_RUN_NAME applied."
echo "  Watch:  kubectl get pods -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -w"
echo "  Alerts: kubectl logs -n $NS deployment/alert-engine -f | grep -v DCGM"
echo "  Stop:   kubectl delete job $JOB_RUN_NAME svc/${JOB_RUN_NAME}-headless -n $NS"
echo ""
echo "JOB_RUN_NAME=$JOB_RUN_NAME"
