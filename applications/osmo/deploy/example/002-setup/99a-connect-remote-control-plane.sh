#!/bin/bash
#
# Connect OSMO Backend to a Remote Control Plane
#
# Reconfigures an already-deployed backend operator to point to a remote
# control plane at a different URL (e.g. in another K8s cluster).
#
# Required inputs (env vars or positional args):
#   REMOTE_CONTROL_PLANE_URL  — external HTTPS URL of the remote control plane
#   REMOTE_SERVICE_TOKEN      — service token from the remote control plane
#
# Usage:
#   export REMOTE_CONTROL_PLANE_URL=https://os1.eu-north1.osmo.nebius.cloud
#   export REMOTE_SERVICE_TOKEN=<token>
#   ./99a-connect-remote-control-plane.sh
#
#   Or with positional args:
#   ./99a-connect-remote-control-plane.sh <url> <token>
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  Connect Backend to Remote Control Plane"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1
check_helm || exit 1

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OSMO_OPERATOR_NAMESPACE="${OSMO_OPERATOR_NAMESPACE:-osmo-operator}"
OSMO_WORKFLOWS_NAMESPACE="${OSMO_WORKFLOWS_NAMESPACE:-osmo-workflows}"
OSMO_IMAGE_TAG="${OSMO_IMAGE_TAG:-6.0.0}"
BACKEND_NAME="${OSMO_BACKEND_NAME:-default}"

# Accept positional args or env vars
REMOTE_CONTROL_PLANE_URL="${1:-${REMOTE_CONTROL_PLANE_URL:-}}"
REMOTE_SERVICE_TOKEN="${2:-${REMOTE_SERVICE_TOKEN:-}}"

# Validate required inputs
if [[ -z "$REMOTE_CONTROL_PLANE_URL" ]]; then
    log_error "REMOTE_CONTROL_PLANE_URL is required."
    echo ""
    echo "Usage:"
    echo "  export REMOTE_CONTROL_PLANE_URL=https://os1.eu-north1.osmo.nebius.cloud"
    echo "  export REMOTE_SERVICE_TOKEN=<token>"
    echo "  ./99a-connect-remote-control-plane.sh"
    echo ""
    echo "  Or: ./99a-connect-remote-control-plane.sh <url> <token>"
    exit 1
fi

if [[ -z "$REMOTE_SERVICE_TOKEN" ]]; then
    log_error "REMOTE_SERVICE_TOKEN is required."
    echo ""
    echo "Generate a service token on the remote control plane:"
    echo "  osmo token set backend-token-\$(date +%s) --service --roles osmo-backend --expires-at 2027-01-01"
    echo ""
    echo "Then export it:"
    echo "  export REMOTE_SERVICE_TOKEN=<token>"
    exit 1
fi

# Strip trailing slash from URL
REMOTE_CONTROL_PLANE_URL="${REMOTE_CONTROL_PLANE_URL%/}"

log_info "Remote control plane URL: ${REMOTE_CONTROL_PLANE_URL}"
log_info "Backend name: ${BACKEND_NAME}"
log_info "Operator namespace: ${OSMO_OPERATOR_NAMESPACE}"

# -----------------------------------------------------------------------------
# Verify kubectl is connected
# -----------------------------------------------------------------------------
log_info "Current kubectl context:"
kubectl config current-context
echo ""

# -----------------------------------------------------------------------------
# Test remote control plane reachability
# -----------------------------------------------------------------------------
log_info "Testing remote control plane reachability..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "${REMOTE_CONTROL_PLANE_URL}/api/version" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "000" ]]; then
    log_error "Cannot reach ${REMOTE_CONTROL_PLANE_URL}/api/version (connection failed)"
    log_error "Check the URL and ensure the remote control plane is accessible from this network."
    exit 1
elif [[ "$HTTP_CODE" =~ ^(200|401|403)$ ]]; then
    log_success "Remote control plane reachable (HTTP ${HTTP_CODE})"
else
    log_warning "Remote control plane returned HTTP ${HTTP_CODE} — proceeding anyway"
fi

# -----------------------------------------------------------------------------
# Check that osmo-operator release exists
# -----------------------------------------------------------------------------
log_info "Checking for existing osmo-operator Helm release..."

if ! helm status osmo-operator -n "${OSMO_OPERATOR_NAMESPACE}" &>/dev/null; then
    log_error "No osmo-operator Helm release found in namespace ${OSMO_OPERATOR_NAMESPACE}"
    log_error "Deploy the backend operator first: ./06-deploy-osmo-backend.sh"
    exit 1
fi
log_success "osmo-operator release found"

# -----------------------------------------------------------------------------
# Create/update the osmo-operator-token secret
# -----------------------------------------------------------------------------
log_info "Updating osmo-operator-token secret..."

kubectl create secret generic osmo-operator-token \
    --namespace "${OSMO_OPERATOR_NAMESPACE}" \
    --from-literal=token="${REMOTE_SERVICE_TOKEN}" \
    --dry-run=client -o yaml | kubectl apply -f -

log_success "osmo-operator-token secret updated"

# -----------------------------------------------------------------------------
# Helm upgrade — update global.serviceUrl, keep everything else
# -----------------------------------------------------------------------------
log_info "Updating osmo-operator Helm release with remote service URL..."

helm upgrade osmo-operator osmo/backend-operator \
    --namespace "${OSMO_OPERATOR_NAMESPACE}" \
    --reuse-values \
    --set "global.serviceUrl=${REMOTE_CONTROL_PLANE_URL}" \
    --wait \
    --timeout 5m

log_success "Helm release updated with serviceUrl=${REMOTE_CONTROL_PLANE_URL}"

# -----------------------------------------------------------------------------
# Wait for backend-listener pod to restart
# -----------------------------------------------------------------------------
log_info "Waiting for backend-listener pod to be ready..."

# Give the rollout a moment to start
sleep 3

# Wait for all pods in the operator namespace to be ready
kubectl rollout status deployment -n "${OSMO_OPERATOR_NAMESPACE}" --timeout=120s 2>/dev/null || true

# Check backend-listener pod status
LISTENER_POD=$(kubectl get pods -n "${OSMO_OPERATOR_NAMESPACE}" -l app=backend-listener -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [[ -n "$LISTENER_POD" ]]; then
    log_info "Checking backend-listener logs for connection status..."
    # Wait a few seconds for connection attempt
    sleep 5
    kubectl logs -n "${OSMO_OPERATOR_NAMESPACE}" "$LISTENER_POD" --tail=20 2>/dev/null || true
    echo ""
else
    log_warning "No backend-listener pod found — check deployment status"
fi

# -----------------------------------------------------------------------------
# Print status
# -----------------------------------------------------------------------------
echo ""
kubectl get pods -n "${OSMO_OPERATOR_NAMESPACE}"

echo ""
echo "========================================"
log_success "Backend connected to remote control plane!"
echo "========================================"
echo ""
echo "Remote Control Plane: ${REMOTE_CONTROL_PLANE_URL}"
echo "Backend Name: ${BACKEND_NAME}"
echo "Operator Namespace: ${OSMO_OPERATOR_NAMESPACE}"
echo ""
echo "To verify the backend is online on the remote control plane:"
echo ""
echo "  curl ${REMOTE_CONTROL_PLANE_URL}/api/configs/backend"
echo ""
echo "  Or using osmo CLI (logged into the remote control plane):"
echo "  osmo config show BACKEND ${BACKEND_NAME}"
echo ""
echo "To check backend-listener logs:"
echo "  kubectl logs -n ${OSMO_OPERATOR_NAMESPACE} -l app=backend-listener -f"
echo ""
