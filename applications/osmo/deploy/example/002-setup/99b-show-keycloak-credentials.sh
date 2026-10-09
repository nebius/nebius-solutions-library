#!/bin/bash
# Show Keycloak admin credentials
# Retrieves the admin password from the keycloak-admin-secret Kubernetes secret.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

OSMO_NAMESPACE="${OSMO_NAMESPACE:-osmo}"

echo ""
echo "========================================"
echo "  Keycloak Admin Credentials"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1

# Retrieve admin password from Kubernetes secret
log_info "Retrieving Keycloak admin password..."

ADMIN_PASSWORD=$(kubectl get secret keycloak-admin-secret -n "${OSMO_NAMESPACE}" \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null) || true

if [[ -z "${ADMIN_PASSWORD}" ]]; then
    log_error "Could not retrieve Keycloak admin password from secret 'keycloak-admin-secret' in namespace '${OSMO_NAMESPACE}'."
    echo "  Make sure Keycloak has been deployed (04-deploy-osmo-control-plane.sh)."
    exit 1
fi

# Determine Keycloak URL
if [[ -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
    KEYCLOAK_URL="https://${KEYCLOAK_HOSTNAME}"
elif [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
    KEYCLOAK_URL="https://auth-${OSMO_INGRESS_HOSTNAME}"
else
    KEYCLOAK_URL="(unknown — set KEYCLOAK_HOSTNAME or OSMO_INGRESS_HOSTNAME)"
fi

echo ""
echo "  URL:      ${KEYCLOAK_URL}"
echo "  Username: admin"
echo "  Password: ${ADMIN_PASSWORD}"
echo ""
