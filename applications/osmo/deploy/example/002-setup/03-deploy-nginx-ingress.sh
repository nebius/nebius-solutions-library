#!/bin/bash
#
# Deploy NGINX Ingress Controller (community)
# Provides path-based routing for all OSMO services (API, router, Web UI).
#
# This installs the same controller OSMO uses elsewhere:
# - OSMO quick-start chart (Chart.yaml) depends on ingress-nginx from the same Helm repo.
# - OSMO Kind runner (run/start_service_kind.py) installs ingress-nginx the same way.
# We do not use the quick-start umbrella chart here (Nebius uses managed DB, etc.),
# so we install the controller explicitly. Not a duplicate of OSMO—same upstream chart.
#
# Run before 05-deploy-osmo-control-plane.sh (and optionally 04-enable-tls.sh).
# See: https://kubernetes.github.io/ingress-nginx/deploy/

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

INGRESS_NAMESPACE="${INGRESS_NAMESPACE:-ingress-nginx}"
INGRESS_RELEASE_NAME="${INGRESS_RELEASE_NAME:-ingress-nginx}"

echo ""
echo "========================================"
echo "  NGINX Ingress Controller Deployment"
echo "========================================"
echo ""

check_kubectl || exit 1
check_helm || exit 1

# -----------------------------------------------------------------------------
# Add Helm repo
# -----------------------------------------------------------------------------
log_info "Adding ingress-nginx Helm repository..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx --force-update
helm repo update

# -----------------------------------------------------------------------------
# Create namespace and install
# -----------------------------------------------------------------------------
log_info "Creating namespace ${INGRESS_NAMESPACE}..."
kubectl create namespace "${INGRESS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

log_info "Installing NGINX Ingress Controller..."
# --set controller.progressDeadlineSeconds=600: chart v4.14+ defaults to 0 which
# K8s 1.32+ rejects ("must be greater than minReadySeconds"). Without this fix the
# Deployment is invalid, the controller never starts, and the admission webhook
# blocks all Ingress resource creation in downstream scripts.
helm upgrade --install "${INGRESS_RELEASE_NAME}" ingress-nginx/ingress-nginx \
    --namespace "${INGRESS_NAMESPACE}" \
    --set controller.service.type=LoadBalancer \
    --set controller.progressDeadlineSeconds=600 \
    --wait --timeout 5m || {
    log_warning "Helm install returned non-zero; controller may still be starting."
}

log_success "NGINX Ingress Controller deployed"

# -----------------------------------------------------------------------------
# Wait for LoadBalancer IP (optional; may take 1–2 min on cloud)
# -----------------------------------------------------------------------------
log_info "Waiting for LoadBalancer IP (up to 120s)..."
for i in $(seq 1 24); do
    LB_IP=$(kubectl get svc -n "${INGRESS_NAMESPACE}" -l app.kubernetes.io/name=ingress-nginx -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "$LB_IP" ]]; then
        log_success "LoadBalancer IP: ${LB_IP}"
        echo ""
        echo "OSMO will be accessible at:"
        echo "  http://${LB_IP}"
        echo ""
        echo "This URL is auto-detected by 05-deploy-osmo-control-plane.sh."
        echo ""
        break
    fi
    sleep 5
done
if [[ -z "${LB_IP:-}" ]]; then
    log_warning "LoadBalancer IP not yet assigned. Check: kubectl get svc -n ${INGRESS_NAMESPACE}"
fi

echo "========================================"
log_success "NGINX Ingress deployment complete"
echo "========================================"
echo ""
echo "Next: run 04-enable-tls.sh <hostname>  (optional, recommended)"
echo "      - If you omit <hostname>, it will use OSMO_INGRESS_HOSTNAME."
echo "Then: run 05-deploy-osmo-control-plane.sh"
echo ""
