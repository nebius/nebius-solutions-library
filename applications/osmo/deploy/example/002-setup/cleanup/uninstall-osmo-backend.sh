#!/bin/bash
#
# Uninstall OSMO Backend Operator
# Reverses everything deployed by 05-deploy-osmo-backend.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/../lib/common.sh"
source "${SCRIPT_DIR}/../defaults.sh"

OSMO_OPERATOR_NAMESPACE="osmo-operator"
OSMO_WORKFLOWS_NAMESPACE="osmo-workflows"

echo ""
echo "========================================"
echo "  Uninstalling OSMO Backend Operator"
echo "========================================"
echo ""

log_warning "This will remove:"
echo "  - Helm release: osmo-operator (namespace: ${OSMO_OPERATOR_NAMESPACE})"
echo "  - Secret: osmo-operator-token (namespace: ${OSMO_OPERATOR_NAMESPACE})"
echo "  - Namespace: ${OSMO_OPERATOR_NAMESPACE}"
echo "  - Namespace: ${OSMO_WORKFLOWS_NAMESPACE} (and all workflow pods)"
echo ""
read_prompt_var "Continue? (y/N)" confirm ""
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    log_info "Cancelled"
    exit 0
fi

# Uninstall Helm release
if helm status osmo-operator -n "${OSMO_OPERATOR_NAMESPACE}" &>/dev/null; then
    log_info "Uninstalling Helm release: osmo-operator..."
    helm uninstall osmo-operator -n "${OSMO_OPERATOR_NAMESPACE}" --wait --timeout 5m
    log_success "Helm release uninstalled"
else
    log_info "Helm release osmo-operator not found — skipping"
fi

# Delete secrets
log_info "Removing secrets..."
kubectl delete secret osmo-operator-token -n "${OSMO_OPERATOR_NAMESPACE}" --ignore-not-found

# Delete the internal agent service (created by 04-deploy-osmo-control-plane.sh for backend operator)
log_info "Removing osmo-agent-internal service..."
kubectl delete svc osmo-agent-internal -n "${OSMO_NAMESPACE}" --ignore-not-found

# Delete namespaces (this also removes any remaining resources inside them)
log_info "Deleting namespace: ${OSMO_WORKFLOWS_NAMESPACE}..."
kubectl delete namespace "${OSMO_WORKFLOWS_NAMESPACE}" --ignore-not-found --wait=false

log_info "Deleting namespace: ${OSMO_OPERATOR_NAMESPACE}..."
kubectl delete namespace "${OSMO_OPERATOR_NAMESPACE}" --ignore-not-found --wait=false

echo ""
log_success "OSMO Backend Operator uninstalled"
echo ""
echo "Note: Namespace deletion may continue in the background."
echo "  kubectl get ns ${OSMO_OPERATOR_NAMESPACE} ${OSMO_WORKFLOWS_NAMESPACE} 2>/dev/null"
echo ""
