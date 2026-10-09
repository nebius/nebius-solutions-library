#!/bin/bash
# Configure BACKEND scheduler_settings (KAI scheduler) for Nebius OSMO.
# Run after 05-deploy-osmo-backend.sh once the backend is ONLINE.
# Option A: Patch existing backend (keeps router_address, etc.) – default.
# Option B: Apply config from config/scheduler-config.template.json (set ROUTER_ADDRESS, etc.).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CONFIG_DIR="${SCRIPT_DIR}/config"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

OSMO_URL="${OSMO_URL:-http://localhost:8080}"
OSMO_NAMESPACE="${OSMO_NAMESPACE:-osmo}"
BACKEND_NAME="${OSMO_BACKEND_NAME:-default}"
K8S_NAMESPACE="${OSMO_WORKFLOWS_NAMESPACE:-osmo-workflows}"

# Use template (Option B) if --from-template and template exists
USE_TEMPLATE=false
[[ "${1:-}" == "--from-template" ]] && USE_TEMPLATE=true

echo ""
echo "========================================"
echo "  Configure BACKEND scheduler (KAI)"
echo "========================================"
echo ""

check_kubectl || exit 1
command -v jq &>/dev/null || { log_error "jq is required"; exit 1; }

# -----------------------------------------------------------------------------
# Start port-forward and login
# -----------------------------------------------------------------------------
log_info "Starting port-forward to OSMO service..."
start_osmo_api_session "${OSMO_NAMESPACE}" 8080 30 || exit 1
OSMO_URL="${OSMO_API_URL}"

cleanup_port_forward() {
    stop_port_forward
}
trap cleanup_port_forward EXIT

log_success "Port-forward ready at ${OSMO_URL}"

osmo_login "${OSMO_API_PORT}" || true

# -----------------------------------------------------------------------------
# Build backend config and apply
# -----------------------------------------------------------------------------
if [[ "$USE_TEMPLATE" == "true" && -f "${CONFIG_DIR}/scheduler-config.template.json" ]]; then
    # Option B: Render template and apply (set ROUTER_ADDRESS before running)
    log_info "Using config from scheduler-config.template.json..."
    if [[ -z "${ROUTER_ADDRESS:-}" ]]; then
        # Derive from ingress: https://host -> wss://host
        INGRESS_URL=$(detect_service_url 2>/dev/null || true)
        if [[ -n "$INGRESS_URL" ]]; then
            ROUTER_ADDRESS="wss://$(echo "$INGRESS_URL" | sed -e 's|https\?://||' -e 's|/.*||')"
            log_info "Derived ROUTER_ADDRESS from ingress: ${ROUTER_ADDRESS}"
        else
            log_error "Set ROUTER_ADDRESS (e.g. wss://your-osmo-host) or run without --from-template to patch existing backend"
            exit 1
        fi
    fi
    export BACKEND_NAME
    export K8S_NAMESPACE
    export ROUTER_ADDRESS
    mkdir -p "${CONFIG_DIR}/out"
    envsubst < "${CONFIG_DIR}/scheduler-config.template.json" > "${CONFIG_DIR}/out/scheduler-config.json"
    BACKEND_FILE="${CONFIG_DIR}/out/scheduler-config.json"
    if ! osmo config update BACKEND "$BACKEND_NAME" --file "$BACKEND_FILE" --description "Backend $BACKEND_NAME scheduler (KAI)"; then
        log_error "Failed to apply backend config from template"
        exit 1
    fi
else
    # Option A: Patch existing backend (keep router_address and other fields)
    log_info "Patching existing backend '$BACKEND_NAME' scheduler_settings (KAI)..."
    BACKEND_JSON=$(osmo_curl GET "${OSMO_URL}/api/configs/backend" 2>/dev/null || true)
    if [[ -z "$BACKEND_JSON" ]]; then
        log_error "Could not get backend config. Is the backend registered? Run: osmo config show BACKEND"
        exit 1
    fi
    BACKEND_OBJECT=$(echo "$BACKEND_JSON" | jq -c --arg name "$BACKEND_NAME" \
        '.backends[] | select(.name == $name) | . + {scheduler_settings: {"scheduler_type":"kai","scheduler_name":"kai-scheduler","scheduler_timeout":30}}')
    if [[ -z "$BACKEND_OBJECT" || "$BACKEND_OBJECT" == "null" ]]; then
        log_error "Backend '$BACKEND_NAME' not found in config. Available: $(echo "$BACKEND_JSON" | jq -r '.backends[].name' 2>/dev/null | tr '\n' ' ')"
        exit 1
    fi
    TMP_FILE=$(mktemp)
    echo "$BACKEND_OBJECT" > "$TMP_FILE"
    if ! osmo config update BACKEND "$BACKEND_NAME" --file "$TMP_FILE" --description "Backend $BACKEND_NAME scheduler (KAI)"; then
        rm -f "$TMP_FILE"
        log_error "Failed to update backend config"
        exit 1
    fi
    rm -f "$TMP_FILE"
fi

# -----------------------------------------------------------------------------
# Verify supported scheduler fields
# -----------------------------------------------------------------------------
UPDATED_BACKEND_JSON=$(osmo_curl GET "${OSMO_URL}/api/configs/backend" 2>/dev/null || true)
UPDATED_BACKEND=$(echo "$UPDATED_BACKEND_JSON" | jq -c --arg name "$BACKEND_NAME" '.backends[] | select(.name == $name)' 2>/dev/null || true)

if [[ -z "$UPDATED_BACKEND" || "$UPDATED_BACKEND" == "null" ]]; then
    log_error "Backend '$BACKEND_NAME' not found after update"
    exit 1
fi

UPDATED_SCHEDULER_TYPE=$(echo "$UPDATED_BACKEND" | jq -r '.scheduler_settings.scheduler_type // ""')
UPDATED_SCHEDULER_NAME=$(echo "$UPDATED_BACKEND" | jq -r '.scheduler_settings.scheduler_name // ""')
UPDATED_SCHEDULER_TIMEOUT=$(echo "$UPDATED_BACKEND" | jq -r '.scheduler_settings.scheduler_timeout // ""')

if [[ "$UPDATED_SCHEDULER_TYPE" != "kai" || "$UPDATED_SCHEDULER_NAME" != "kai-scheduler" || "$UPDATED_SCHEDULER_TIMEOUT" != "30" ]]; then
    log_error "Backend scheduler verification failed"
    echo "$UPDATED_BACKEND" | jq '.scheduler_settings'
    exit 1
fi

log_success "BACKEND scheduler configuration applied"
echo ""
echo "Verify:"
echo "  osmo config show BACKEND ${BACKEND_NAME}"
echo ""
echo "You should see scheduler_settings: scheduler_type=kai, scheduler_name=kai-scheduler, scheduler_timeout=30"
echo "Note: current OSMO 6.2.x backend config does not persist a separate coscheduling field."
echo ""
