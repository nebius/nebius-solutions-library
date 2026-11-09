#!/bin/bash
# Launch a Megatron TP/PP training job with the straggler-detection pipeline
# on Kubernetes. Source cluster_k8s.env first, then run this script.
#
# PREREQUISITE: k8s/deploy.sh must have been run first to create the namespace,
# workspace PVC, VictoriaMetrics, and alert engine Deployment. This script
# submits a training Job only — it does NOT start the alert engine. The alert
# engine runs as a K8s Deployment (deployed by deploy.sh); check its logs with:
#   kubectl logs -n straggler-detection deployment/alert-engine -f | grep -v DCGM
#
# Usage:
#   source cluster_k8s.env
#   k8s/run_megatron_k8s.sh [train_iters] [port] [pin_mode] [sleep_ms] [target_ranks]
#   Defaults: 100 iters, port 29500, pin_mode=dump, sleep_ms=0, target_ranks=""
#
# 2-node shape: TP4/PP2/DP2 on 16 GPUs (H200). This is the validated shape.
# Node count beyond 2 is NOT validated (see docs/DESIGN_NOTES.md § 6.6).
set -uo pipefail
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="$K8S_DIR/manifests"

CLUSTER_ENV="$PKG_ROOT/cluster_k8s.env"
[ -f "$CLUSTER_ENV" ] || { echo "FATAL: source cluster_k8s.env first"; exit 1; }
source "$CLUSTER_ENV"

for v in K8S_NAMESPACE NUM_GPU_NODES GPUS_PER_NODE VM_URL_SVC; do
  [ -z "${!v:-}" ] && { echo "FATAL: cluster_k8s.env missing $v — re-run k8s/install_k8s.sh"; exit 1; }
done

TRAIN_ITERS="${1:-100}"
TRAIN_PORT="${2:-29500}"
PIN_MODE="${3:-dump}"
SLEEP_MS="${4:-0}"
TARGET_RANKS="${5:-}"
NS="$K8S_NAMESPACE"

# 2-node Megatron shape: TP4/PP2/DP2 (validated; see DESIGN_NOTES §6.3/6.6)
MEGA_TP="${MEGA_TP:-4}"
MEGA_PP="${MEGA_PP:-2}"
MEGA_LAYERS="${MEGA_LAYERS:-40}"
MEGA_HIDDEN="${MEGA_HIDDEN:-4096}"
MEGA_HEADS="${MEGA_HEADS:-32}"
MEGA_SEQ="${MEGA_SEQ:-2048}"
MEGA_MBS="${MEGA_MBS:-1}"
MEGA_GBS="${MEGA_GBS:-32}"
TRAIN_NUM_NODES="$NUM_GPU_NODES"
TRAIN_GPUS="$GPUS_PER_NODE"

_RUN_ID="$(date +%s)"
JOB_RUN_NAME="mega-${_RUN_ID}"

echo "[run_megatron_k8s.sh] $JOB_RUN_NAME  iters=$TRAIN_ITERS TP=$MEGA_TP PP=$MEGA_PP nodes=$TRAIN_NUM_NODES sleep_ms=$SLEEP_MS targets='$TARGET_RANKS'"

# Guard: existing active training jobs
_ACTIVE="$(kubectl get jobs -n "$NS" -l straggler.nebius.ai/component=training \
    --field-selector='status.active=1' --no-headers 2>/dev/null | wc -l)"
[ "${_ACTIVE:-0}" -gt 0 ] && { echo "ABORT: active training job already running in $NS"; exit 1; }

# Headless service for pod DNS rendezvous
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

INSPECTOR="${INSPECTOR:-on}"
NCCL_LIB_PATH="${NCCL_LIB_PATH:-}"
export JOB_RUN_NAME TRAIN_ITERS TRAIN_PORT TRAIN_NUM_NODES TRAIN_GPUS VM_URL_SVC PIN_MODE
export MEGA_TP MEGA_PP MEGA_LAYERS MEGA_HIDDEN MEGA_HEADS MEGA_SEQ MEGA_MBS MEGA_GBS
export SLEEP_MS TARGET_RANKS INSPECTOR NCCL_LIB_PATH

envsubst '${JOB_RUN_NAME} ${TRAIN_ITERS} ${TRAIN_PORT} ${TRAIN_NUM_NODES} ${TRAIN_GPUS} ${VM_URL_SVC} ${PIN_MODE} ${MEGA_TP} ${MEGA_PP} ${MEGA_LAYERS} ${MEGA_HIDDEN} ${MEGA_HEADS} ${MEGA_SEQ} ${MEGA_MBS} ${MEGA_GBS} ${SLEEP_MS} ${TARGET_RANKS} ${INSPECTOR} ${NCCL_LIB_PATH}' \
    < "$MANIFESTS_DIR/06-megatron-job.yaml.tmpl" | kubectl apply -f -

echo "[run_megatron_k8s.sh] Job $JOB_RUN_NAME applied."
echo "  Watch:   kubectl get pods -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -w"
echo "  Logs:    kubectl logs -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -c training -f"
echo "  Alerts:  kubectl logs -n $NS deployment/alert-engine -f | grep -v DCGM"
echo "  Stop:    kubectl delete job $JOB_RUN_NAME svc/${JOB_RUN_NAME}-headless -n $NS"
echo ""
echo "JOB_RUN_NAME=$JOB_RUN_NAME"
