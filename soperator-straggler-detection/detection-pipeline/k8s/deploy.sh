#!/bin/bash
# One-command K8s deploy for the straggler-detection pipeline.
# From a fresh clone: k8s/deploy.sh [--namespace NS] [--build-nccl]
#
# This script is CLUSTER-AGNOSTIC: it uses only standard K8s primitives and
# reads cluster specifics from your kubeconfig. No hard-coded node names or
# cluster URLs. The only cluster-specific assumption is the GPU node label
# (nebius.com/gpu=true); override with --gpu-label if your cluster uses a
# different label.
#
# Prerequisites:
#   - kubectl configured to your cluster (any K8s >= 1.27)
#   - GPU nodes with nvidia.com/gpu taint: nvidia.com/gpu=<count>:NoSchedule
#   - StorageClass "nfs" providing ReadWriteMany PVCs (or override with --storage-class)
#   - nvidia/pytorch:25.01-py3 container image accessible from the cluster
#   - github.com reachable from pods (for initial workspace clone)
#
# After this script: source cluster_k8s.env, then run training jobs with
# k8s/run_k8s.sh (nanoGPT) or k8s/run_megatron_k8s.sh (Megatron).
set -uo pipefail

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS="$K8S_DIR/manifests"

NS="${STRAGGLER_NS:-straggler-detection}"
BUILD_NCCL=0
GPU_LABEL="${GPU_LABEL:-nebius.com/gpu=true}"
STORAGE_CLASS="${STORAGE_CLASS:-nfs}"
BRANCH="${BRANCH:-add/straggler-detection-v1-beta}"
REPO="${REPO:-https://github.com/nebius/nebius-solutions-library.git}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NS="$2"; shift 2 ;;
    --build-nccl) BUILD_NCCL=1; shift ;;
    --gpu-label) GPU_LABEL="$2"; shift 2 ;;
    --storage-class) STORAGE_CLASS="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

info()  { echo "[deploy] $*"; }
fatal() { echo "FATAL: $*" >&2; exit 1; }

command -v kubectl >/dev/null || fatal "kubectl not found"
kubectl cluster-info >/dev/null 2>&1 || fatal "kubectl cannot reach cluster (check kubeconfig)"

# --- Discover GPU nodes ---
GPU_NODES="$(kubectl get nodes -l "${GPU_LABEL}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
[ -z "$GPU_NODES" ] && fatal "No nodes with label ${GPU_LABEL}. Set --gpu-label to match your cluster's GPU node label."
NUM_GPU_NODES="$(echo "$GPU_NODES" | wc -w)"
_FIRST_GPU_NODE="$(echo "$GPU_NODES" | awk '{print $1}')"
GPUS_PER_NODE="$(kubectl get node "$_FIRST_GPU_NODE" -o jsonpath='{.metadata.labels.nebius\.com/resource-preset}' 2>/dev/null | grep -oP '^\d+(?=gpu)' || echo "")"
[ -z "$GPUS_PER_NODE" ] && GPUS_PER_NODE=8   # fall back to 8 if label not present
info "GPU nodes: $NUM_GPU_NODES (${GPU_NODES}) | GPUs/node: $GPUS_PER_NODE"

# --- Namespace ---
kubectl apply -f "$MANIFESTS/00-namespace.yaml" 2>/dev/null || kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# --- Workspace PVC (patch storage class if not 'nfs') ---
if [ "$STORAGE_CLASS" != "nfs" ]; then
  sed "s/storageClassName: nfs/storageClassName: $STORAGE_CLASS/" "$MANIFESTS/01-workspace-pvc.yaml" | kubectl apply -f -
else
  kubectl apply -f "$MANIFESTS/01-workspace-pvc.yaml"
fi
info "Waiting for workspace PVC..."
for i in $(seq 1 30); do
  [ "$(kubectl get pvc straggler-workspace -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Bound" ] && { info "PVC Bound"; break; }
  sleep 5
done

# --- Workspace init job (clone repo + build Inspector + optionally NCCL 2.32.3) ---
kubectl delete job straggler-init-build -n "$NS" --ignore-not-found=true 2>/dev/null
if [ "$BUILD_NCCL" = "1" ]; then
  info "NCCL 2.32.3 build enabled (--build-nccl) — this takes ~15-20 min"
  sed "s/BUILD_NCCL_232=0/BUILD_NCCL_232=1/" "$MANIFESTS/02-workspace-init-job.yaml" | kubectl apply -f -
else
  kubectl apply -f "$MANIFESTS/02-workspace-init-job.yaml"
fi
info "Waiting for workspace init job (clones repo + builds Inspector plugin)..."
kubectl wait job/straggler-init-build -n "$NS" --for=condition=Complete --timeout=1200s || {
  kubectl logs -n "$NS" -l job-name=straggler-init-build --tail=30
  fatal "Init job failed — see logs above"
}

# --- VictoriaMetrics ---
kubectl apply -f "$MANIFESTS/03-victoriametrics.yaml"
kubectl rollout status deployment/victoriametrics -n "$NS" --timeout=120s

VM_SVC_DNS="victoriametrics.${NS}.svc.cluster.local:8428"
info "VictoriaMetrics deployed at ClusterIP ${VM_SVC_DNS}"

# --- Alert engine (Deployment inside cluster, direct ClusterIP access to VM) ---
kubectl apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: alert-engine
  namespace: ${NS}
  labels:
    straggler.nebius.ai/component: alert-engine
spec:
  replicas: 1
  selector:
    matchLabels:
      straggler.nebius.ai/component: alert-engine
  template:
    metadata:
      labels:
        straggler.nebius.ai/component: alert-engine
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: nebius.com/gpu
                    operator: DoesNotExist
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: straggler-workspace
      containers:
        - name: alert-engine
          image: python:3.11-slim
          imagePullPolicy: IfNotPresent
          resources:
            requests:
              cpu: "2"
              memory: 4Gi
          volumeMounts:
            - name: workspace
              mountPath: /workspace
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
          command:
            - bash
            - -c
            - |
              set -e
              pip install pyyaml -q 2>/dev/null || true
              PKG=/workspace/nebius-solutions-library/soperator-straggler-detection/detection-pipeline
              for i in \$(seq 1 30); do [ -f "\$PKG/alerting/alert_engine.py" ] && break; sleep 5; done
              cd "\$PKG/alerting"
              exec python3 -I alert_engine.py \
                "http://victoriametrics.${NS}.svc.cluster.local:8428" \
                --duration 315360000 --poll-interval 2.0 --hostname-refresh-s 15
YAML
kubectl rollout status deployment/alert-engine -n "$NS" --timeout=120s

# --- Write cluster_k8s.env ---
NCCL_PATH=""
[ "$BUILD_NCCL" = "1" ] && NCCL_PATH="/workspace/nccl-2.32.3/build/lib"
cat > "$PKG_ROOT/cluster_k8s.env" <<ENV
# Generated by k8s/deploy.sh
K8S_NAMESPACE="${NS}"
NUM_GPU_NODES="${NUM_GPU_NODES}"
GPU_NODES="${GPU_NODES// /,}"
GPUS_PER_NODE="${GPUS_PER_NODE}"
VM_URL="http://${VM_SVC_DNS}"
VM_URL_SVC="${VM_SVC_DNS}"
NCCL_232_LIB_PATH="${NCCL_PATH}"
WORKSPACE_PKG_DIR="/workspace/nebius-solutions-library/soperator-straggler-detection/detection-pipeline"
ENV

info "=== Deploy complete ==="
info "Alert engine running: kubectl logs -n $NS deployment/alert-engine -f | grep -v DCGM"
info "Launch nanoGPT:    source cluster_k8s.env && k8s/run_k8s.sh"
info "Launch Megatron:   source cluster_k8s.env && k8s/run_megatron_k8s.sh"
info "Smoke test:        k8s/smoke_test.sh   (see Step 4 in README)"
