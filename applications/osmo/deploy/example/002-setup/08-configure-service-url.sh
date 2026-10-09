#!/bin/bash
#
# Configure OSMO Service URL
# Required for osmo-ctrl sidecar to communicate with OSMO service
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  OSMO Service URL Configuration"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1

if [[ -z "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
    log_error "OSMO_INGRESS_HOSTNAME is not set."
    echo "  Source your environment first: source ../000-prerequisites/nebius-env-init.sh"
    echo "  Or set it manually: export OSMO_INGRESS_HOSTNAME=<your-domain>"
    exit 1
fi

# -----------------------------------------------------------------------------
# Start port-forward
# -----------------------------------------------------------------------------
log_info "Starting port-forward to OSMO service..."

OSMO_NS="${OSMO_NAMESPACE:-osmo}"

start_osmo_api_session "${OSMO_NS}" 8080 30 || exit 1
OSMO_URL="${OSMO_API_URL}"

cleanup_port_forward() {
    stop_port_forward
}
trap cleanup_port_forward EXIT

log_success "Port-forward ready at ${OSMO_URL}"

# Login (no-op when bypassing Envoy -- curl headers handle auth)
osmo_login "${OSMO_API_PORT}" || exit 1

# -----------------------------------------------------------------------------
# Determine the target service URL
# -----------------------------------------------------------------------------
log_info "Determining target service URL..."

# Priority:
#   1. Explicit OSMO_INGRESS_BASE_URL (user override)
#   2. Auto-detect from NGINX Ingress Controller LoadBalancer
if [[ -n "${OSMO_INGRESS_BASE_URL:-}" ]]; then
    SERVICE_URL="${OSMO_INGRESS_BASE_URL}"
    log_info "Using explicit Ingress base URL: ${SERVICE_URL}"
elif DETECTED_URL=$(detect_service_url 2>/dev/null) && [[ -n "$DETECTED_URL" ]]; then
    SERVICE_URL="${DETECTED_URL}"
    log_info "Auto-detected service URL: ${SERVICE_URL}"
else
    log_error "Could not detect NGINX Ingress Controller URL."
    log_error "Ensure 03-deploy-nginx-ingress.sh was run and the LoadBalancer has an IP."
    if [[ "${OSMO_TLS_ENABLED:-false}" == "true" ]]; then
        log_error "Or set OSMO_INGRESS_BASE_URL manually: export OSMO_INGRESS_BASE_URL=https://<your-domain>"
    else
        log_error "Or set OSMO_INGRESS_BASE_URL manually: export OSMO_INGRESS_BASE_URL=http://<lb-ip>"
    fi
    exit 1
fi

# -----------------------------------------------------------------------------
# Check current service_base_url
# -----------------------------------------------------------------------------
log_info "Checking current service_base_url..."

CURRENT_URL=$(osmo_curl GET "${OSMO_URL}/api/configs/service" 2>/dev/null | jq -r '.service_base_url // ""')
echo "Current service_base_url: '${CURRENT_URL}'"

if [[ -n "$CURRENT_URL" && "$CURRENT_URL" != "null" && "$CURRENT_URL" == "$SERVICE_URL" ]]; then
    log_success "service_base_url is already correctly configured: ${CURRENT_URL}"
    cleanup_port_forward
    trap - EXIT
    exit 0
elif [[ -n "$CURRENT_URL" && "$CURRENT_URL" != "null" ]]; then
    log_warning "service_base_url is set to '${CURRENT_URL}' but should be '${SERVICE_URL}'"
    log_info "Updating service_base_url..."
fi

# -----------------------------------------------------------------------------
# Configure service_base_url
# -----------------------------------------------------------------------------
log_info "Configuring service_base_url to: ${SERVICE_URL}"

if upsert_osmo_service_base_url_db "${OSMO_NS}" "${SERVICE_URL}"; then
    log_success "service_base_url configured"
else
    log_error "Failed to configure service_base_url"
    exit 1
fi

# -----------------------------------------------------------------------------
# Verify Configuration
# -----------------------------------------------------------------------------
log_info "Verifying configuration..."

NEW_URL=$(osmo_curl GET "${OSMO_URL}/api/configs/service" 2>/dev/null | jq -r '.service_base_url // ""')

if [[ "$NEW_URL" == "$SERVICE_URL" ]]; then
    log_success "service_base_url verified: ${NEW_URL}"
else
    log_error "Verification failed. Expected: ${SERVICE_URL}, Got: ${NEW_URL}"
    exit 1
fi

# Cleanup
cleanup_port_forward
trap - EXIT

echo ""
echo "========================================"
log_success "OSMO Service URL configuration complete!"
echo "========================================"
echo ""
echo "Service URL: ${SERVICE_URL}"
echo ""
echo "This URL is used by the osmo-ctrl sidecar container to:"
echo "  - Stream workflow logs to the OSMO service"
echo "  - Report task status and completion"
echo "  - Fetch authentication tokens"
echo ""
