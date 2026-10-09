#!/bin/bash
# Uninstall Keycloak and disable OSMO authentication
# This removes Keycloak and related secrets. After running this, re-deploy
# OSMO control plane without DEPLOY_KEYCLOAK to switch back to open API mode.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

OSMO_NS="${OSMO_NAMESPACE:-osmo}"
KC_TLS_SECRET="${KEYCLOAK_TLS_SECRET_NAME:-osmo-tls-auth}"
INGRESS_NS="${INGRESS_NAMESPACE:-ingress-nginx}"

echo ""
echo "========================================"
echo "  Uninstall Keycloak"
echo "========================================"
echo ""

check_kubectl || exit 1
check_helm || exit 1

# Step 1: Uninstall Keycloak Helm release
log_info "Uninstalling Keycloak Helm release..."
helm uninstall keycloak --namespace "${OSMO_NS}" 2>/dev/null || log_info "Keycloak Helm release not found (already removed)"

# Step 2: Delete Keycloak config job and realm ConfigMap
log_info "Cleaning up Keycloak configuration job and ConfigMap..."
kubectl delete job keycloak-osmo-setup -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
kubectl delete configmap keycloak-realm-json -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true

# Step 3: Delete Keycloak-related secrets
log_info "Deleting Keycloak secrets..."
kubectl delete secret keycloak-admin-secret -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
kubectl delete secret keycloak-db-secret -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
kubectl delete secret keycloak-nebius-sso-secret -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
kubectl delete secret oidc-secrets -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
log_success "Keycloak secrets deleted"

# Step 4: Delete Keycloak TLS secret
log_info "Deleting Keycloak TLS secret (${KC_TLS_SECRET})..."
kubectl delete secret "${KC_TLS_SECRET}" -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
kubectl delete secret "${KC_TLS_SECRET}" -n "${INGRESS_NS}" --ignore-not-found 2>/dev/null || true
log_success "Keycloak TLS secrets deleted"

# Step 5: Delete Keycloak PVCs (if any)
log_info "Cleaning up Keycloak PVCs..."
kubectl delete pvc -l app.kubernetes.io/name=keycloak -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true

echo ""
log_success "Keycloak uninstalled"
echo ""
echo "Next steps:"
echo "  1. Re-deploy OSMO control plane without authentication:"
echo "     unset DEPLOY_KEYCLOAK"
echo "     ./04-deploy-osmo-control-plane.sh"
echo ""
echo "  2. (Optional) Drop the Keycloak database from PostgreSQL:"
echo "     Connect to your Managed PostgreSQL and run:"
echo "     DROP DATABASE IF EXISTS keycloak;"
echo ""
echo "  3. (Optional) Remove the DNS A record for the auth subdomain"
echo ""
