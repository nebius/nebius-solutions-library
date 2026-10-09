#!/bin/bash
#
# Deploy GPU Infrastructure (GPU Operator, Network Operator, KAI Scheduler)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  GPU Infrastructure Deployment"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1
check_helm || exit 1

# Add Helm repos
log_info "Adding Helm repositories..."
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
helm repo update

# Auto-detect driverfull images from Terraform config
if [[ -z "${USE_DRIVERFULL_IMAGES:-}" ]]; then
    TF_DRIVERFULL=$(get_tf_output "gpu_nodes_driverfull_image" "../001-iac" || echo "")
    if [[ "$TF_DRIVERFULL" == "true" ]]; then
        USE_DRIVERFULL_IMAGES="true"
        log_info "Auto-detected driverfull images from Terraform"
    fi
fi

# -----------------------------------------------------------------------------
# Deploy GPU Operator (skipped when using driverfull images)
# -----------------------------------------------------------------------------
if [[ "${USE_DRIVERFULL_IMAGES:-false}" == "true" ]]; then
    log_info "Skipping GPU Operator (using Nebius driverfull images with pre-installed drivers)"
    log_info "Installing NVIDIA device plugin for driverfull mode..."

    kubectl create namespace "${GPU_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    # With driverfull images, we still need the GPU Operator for toolkit, device-plugin,
    # dcgm, etc. - but driver installation is disabled.
    helm upgrade --install gpu-operator nvidia/gpu-operator \
        --namespace "${GPU_OPERATOR_NAMESPACE}" \
        --values "${VALUES_DIR}/gpu-operator.yaml" \
        --set driver.enabled=false \
        --timeout 10m

    log_success "GPU Operator deployed (driver disabled - using driverfull images)"
else
    log_info "Deploying NVIDIA GPU Operator (with driver installation)..."

    kubectl create namespace "${GPU_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    DRIVER_VERSION_SET=()
    if [[ -n "${GPU_DRIVER_VERSION:-}" ]]; then
        log_info "Using pinned driver version: ${GPU_DRIVER_VERSION}"
        DRIVER_VERSION_SET=(--set "driver.version=${GPU_DRIVER_VERSION}")
    fi

    # shellcheck disable=SC2086
    helm upgrade --install gpu-operator nvidia/gpu-operator \
        --namespace "${GPU_OPERATOR_NAMESPACE}" \
        --values "${VALUES_DIR}/gpu-operator.yaml" \
        ${DRIVER_VERSION_SET[@]:+${DRIVER_VERSION_SET[@]}} \
        --timeout 10m

    log_success "GPU Operator deployed (pods will become ready when GPU nodes are available)"
fi

# Brief wait for core operator pod only (not GPU node components)
sleep 10
kubectl get pods -n "${GPU_OPERATOR_NAMESPACE}" --no-headers 2>/dev/null | head -5 || true

# -----------------------------------------------------------------------------
# Deploy Network Operator (for InfiniBand) - OPTIONAL
# -----------------------------------------------------------------------------
if [[ "${ENABLE_NETWORK_OPERATOR:-false}" == "true" ]]; then
    log_info "Deploying NVIDIA Network Operator (InfiniBand support)..."

    kubectl create namespace "${NETWORK_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

    helm upgrade --install network-operator nvidia/network-operator \
        --namespace "${NETWORK_OPERATOR_NAMESPACE}" \
        --values "${VALUES_DIR}/network-operator.yaml" \
        --timeout 10m

    log_success "Network Operator deployed"
    
    # Brief wait and show status
    sleep 5
    kubectl get pods -n "${NETWORK_OPERATOR_NAMESPACE}" --no-headers 2>/dev/null | head -5 || true
else
    log_info "Skipping Network Operator (set ENABLE_NETWORK_OPERATOR=true to install)"
fi

# -----------------------------------------------------------------------------
# Deploy KAI Scheduler (from NVIDIA OCI registry)
# https://nvidia.github.io/OSMO/main/deployment_guide/install_backend/dependencies/dependencies.html
# -----------------------------------------------------------------------------
log_info "Deploying KAI Scheduler..."

kubectl create namespace "${KAI_SCHEDULER_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# Install directly from OCI registry
KAI_VERSION="${KAI_SCHEDULER_VERSION:-0.4.0}"
helm upgrade --install kai-scheduler \
    oci://ghcr.io/nvidia/kai-scheduler/kai-scheduler \
    --version "${KAI_VERSION}" \
    --namespace "${KAI_SCHEDULER_NAMESPACE}" \
    --values "${VALUES_DIR}/kai-scheduler.yaml" \
    --timeout 5m

log_success "KAI Scheduler deployed"

# Brief wait and show status
sleep 5
kubectl get pods -n "${KAI_SCHEDULER_NAMESPACE}" --no-headers 2>/dev/null | head -5 || true

# -----------------------------------------------------------------------------
# Verify Installation
# -----------------------------------------------------------------------------
echo ""
log_info "Verifying GPU infrastructure..."

# Check GPU nodes
GPU_NODES=$(kubectl get nodes -l node-type=gpu -o name 2>/dev/null | wc -l)
if [[ $GPU_NODES -gt 0 ]]; then
    log_success "Found $GPU_NODES GPU node(s)"
    kubectl get nodes -l node-type=gpu -o wide
else
    log_warning "No GPU nodes found yet (they may still be provisioning)"
fi

echo ""
echo "========================================"
log_success "GPU Infrastructure deployment complete!"
echo "========================================"
echo ""
echo "Next step: ./02-deploy-observability.sh"
echo ""
