#!/bin/bash
# K8s-native install -- sibling to install.sh (Slurm).
# Discovers the real K8s cluster shape (GPU nodes, GPU count) via kubectl,
# creates the straggler-detection namespace + NFS workspace PVC, runs a one-shot
# init job inside the training container to build the Inspector plugin and
# (optionally) NCCL 2.32.3, deploys VictoriaMetrics, and writes cluster_k8s.env.
#
# Run from the control machine where kubectl is configured. Never modifies any
# existing Slurm/soperator resources -- purely additive K8s namespace.
#
# Optional env vars:
#   INSTALL_K8S_BUILD_NCCL=1   -- build NCCL 2.32.3 in the init job (~15 min)
#   INSTALL_K8S_VM_URL=http://  -- reuse an existing VictoriaMetrics instance
#   INSTALL_K8S_NAMESPACE=...   -- override default namespace (straggler-detection)
set -uo pipefail
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="$K8S_DIR/manifests"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[install_k8s.sh] $*"; }

NS="${INSTALL_K8S_NAMESPACE:-straggler-detection}"

# =========================================================================
# Step 1 -- verify kubectl access and discover GPU cluster shape
# =========================================================================
info "Step 1: verifying kubectl access and discovering real cluster shape..."

command -v kubectl >/dev/null 2>&1 || fail "kubectl not found -- this script must run on a machine with kubectl configured."
kubectl cluster-info >/dev/null 2>&1 || fail "kubectl cluster-info failed -- check your kubeconfig."
[ "$FAIL" -eq 1 ] && exit 1

# Real GPU nodes: label nebius.com/gpu=true (confirmed on this cluster)
GPU_NODES="$(kubectl get nodes -l nebius.com/gpu=true -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
if [ -z "$GPU_NODES" ]; then
    fail "No nodes with label nebius.com/gpu=true found. This script targets Nebius H200 GPU nodes."
    exit 1
fi
NUM_GPU_NODES="$(echo "$GPU_NODES" | wc -w)"
info "Real GPU nodes discovered ($NUM_GPU_NODES): $GPU_NODES"

# GPU count per node: from nebius.com/resource-preset label (e.g. 8gpu-128vcpu-1600gb -> 8)
# or from nebius.com/gpu-name. Fall back to 8 for H200 (confirmed on this cluster).
_FIRST_GPU_NODE="$(echo "$GPU_NODES" | awk '{print $1}')"
_GPU_PRESET="$(kubectl get node "$_FIRST_GPU_NODE" -o jsonpath='{.metadata.labels.nebius\.com/resource-preset}' 2>/dev/null)"
GPUS_PER_NODE="$(echo "$_GPU_PRESET" | grep -oP '^\d+(?=gpu)' || echo "")"
if [ -z "$GPUS_PER_NODE" ] || [ "$GPUS_PER_NODE" -eq 0 ] 2>/dev/null; then
    warn "Could not parse GPU count from resource-preset label '$_GPU_PRESET' -- defaulting to 8 (H200 node shape confirmed on this cluster). Set GPUS_PER_NODE env var to override."
    GPUS_PER_NODE="${GPUS_PER_NODE:-8}"
fi
info "GPUS_PER_NODE=$GPUS_PER_NODE (from node label '$_GPU_PRESET')"

# =========================================================================
# Step 2 -- create namespace and workspace PVC
# =========================================================================
info "Step 2: creating namespace $NS and workspace PVC..."
kubectl apply -f "$MANIFESTS_DIR/00-namespace.yaml"
kubectl apply -f "$MANIFESTS_DIR/01-workspace-pvc.yaml"
# Wait for PVC to be bound (NFS provisioner is fast but not instant)
info "  Waiting for workspace PVC to be Bound..."
for i in $(seq 1 30); do
    _status="$(kubectl get pvc straggler-workspace -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [ "$_status" = "Bound" ] && { info "  straggler-workspace PVC Bound."; break; }
    sleep 5
    [ "$i" -eq 30 ] && fail "PVC straggler-workspace did not become Bound within 150s. Check the nfs storage class."
done
[ "$FAIL" -eq 1 ] && exit 1

# =========================================================================
# Step 3 -- run workspace init job (clone repo + build Inspector + optionally NCCL)
# =========================================================================
info "Step 3: running workspace init job (clones repo, builds Inspector plugin, builds NCCL if INSTALL_K8S_BUILD_NCCL=1)..."

# Delete any previous init job before reapplying
kubectl delete job straggler-init-build -n "$NS" --ignore-not-found=true

# Apply the init job, optionally enabling NCCL 2.32.3 build via envsubst into env
if [ "${INSTALL_K8S_BUILD_NCCL:-0}" = "1" ]; then
    info "  INSTALL_K8S_BUILD_NCCL=1 -- will build NCCL 2.32.3 in the init job (~15 min)"
    # Patch the init job YAML to set BUILD_NCCL_232=1 by substitution
    sed 's/BUILD_NCCL_232=0/BUILD_NCCL_232=1/' "$MANIFESTS_DIR/02-workspace-init-job.yaml" | \
        kubectl apply -f -
else
    kubectl apply -f "$MANIFESTS_DIR/02-workspace-init-job.yaml"
fi

info "  Waiting for init job to complete (may take 5-20 min depending on network + NCCL build)..."
kubectl wait job/straggler-init-build -n "$NS" --for=condition=Complete --timeout=1800s
EC=$?
if [ $EC -ne 0 ]; then
    warn "init job did not complete successfully within 30 min. Logs:"
    kubectl logs -n "$NS" -l job-name=straggler-init-build --tail=50
    fail "Init job failed. See logs above. Fix and re-run install_k8s.sh."
    exit 1
fi
info "  Init job completed. Verifying Inspector plugin..."
_POD="$(kubectl get pods -n "$NS" -l job-name=straggler-init-build -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
if [ -n "$_POD" ]; then
    kubectl exec -n "$NS" "$_POD" -- ls -la \
        /workspace/nebius-solutions-library/soperator-straggler-detection/detection-pipeline/inspector-plugin/libnccl-profiler-inspector.so \
        2>/dev/null && info "  Inspector plugin confirmed in workspace." || \
        warn "  Could not confirm Inspector plugin in workspace pod -- check init job logs."
fi

# =========================================================================
# Step 4 -- deploy VictoriaMetrics
# =========================================================================
info "Step 4: deploying VictoriaMetrics..."
VM_URL_SVC="victoriametrics.${NS}.svc.cluster.local:8428"
if [ -n "${INSTALL_K8S_VM_URL:-}" ]; then
    info "  Reusing existing VM at INSTALL_K8S_VM_URL=$INSTALL_K8S_VM_URL (not deploying K8s VM pod)"
    VM_URL="$INSTALL_K8S_VM_URL"
else
    kubectl apply -f "$MANIFESTS_DIR/03-victoriametrics.yaml"
    info "  Waiting for VictoriaMetrics pod to be ready..."
    kubectl rollout status deployment/victoriametrics -n "$NS" --timeout=120s || \
        fail "VictoriaMetrics deployment did not become ready within 120s."
    VM_URL="http://$VM_URL_SVC"
    info "  VictoriaMetrics deployed. ClusterIP Service: $VM_URL_SVC"
fi

# Determine NCCL 2.32.3 lib path in workspace (if built)
NCCL_232_DIR="/workspace/nccl-2.32.3"
NCCL_232_LIB_PATH=""
if [ "${INSTALL_K8S_BUILD_NCCL:-0}" = "1" ]; then
    # Verify inside the init job pod that the NCCL lib exists
    if [ -n "$_POD" ]; then
        kubectl exec -n "$NS" "$_POD" -- test -f "$NCCL_232_DIR/build/lib/libnccl.so.2" 2>/dev/null && \
            NCCL_232_LIB_PATH="$NCCL_232_DIR/build/lib" && \
            info "  NCCL 2.32.3 confirmed at $NCCL_232_LIB_PATH in workspace." || \
            warn "  NCCL 2.32.3 not found in workspace despite INSTALL_K8S_BUILD_NCCL=1 -- check init job logs."
    fi
fi

# =========================================================================
# Step 5 -- write cluster_k8s.env
# =========================================================================
info "Step 5: writing cluster_k8s.env..."
cat > "$PKG_ROOT/cluster_k8s.env" <<EOF
# Generated by install_k8s.sh -- K8s-native cluster discovery.
# Do not hand-edit; re-run install_k8s.sh instead.
# Source this file before running k8s/run_k8s.sh.
K8S_NAMESPACE="${NS}"
NUM_GPU_NODES="${NUM_GPU_NODES}"
GPU_NODES="${GPU_NODES// /,}"
GPUS_PER_NODE="${GPUS_PER_NODE}"
VM_URL="${VM_URL}"
VM_URL_SVC="${VM_URL_SVC}"
NCCL_232_LIB_PATH="${NCCL_232_LIB_PATH}"
# Workspace path INSIDE pods (NFS PVC mounted at /workspace)
WORKSPACE_PKG_DIR="/workspace/nebius-solutions-library/soperator-straggler-detection/detection-pipeline"
EOF
info "Wrote $PKG_ROOT/cluster_k8s.env:"
sed 's/^/  /' "$PKG_ROOT/cluster_k8s.env"

if [ "$FAIL" -eq 1 ]; then
    echo "[install_k8s.sh] Completed WITH failures -- environment is NOT fully ready. See FATAL lines above." >&2
    exit 1
fi
info "install_k8s.sh completed. K8s environment is ready."
info "Next: source $PKG_ROOT/cluster_k8s.env, then run k8s/run_k8s.sh to start a training job."
