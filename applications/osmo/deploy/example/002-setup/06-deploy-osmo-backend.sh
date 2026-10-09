#!/bin/bash
#
# Deploy OSMO Backend Operator
# https://nvidia.github.io/OSMO/main/deployment_guide/install_backend/deploy_backend.html
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  OSMO Backend Operator Deployment"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1
check_helm || exit 1

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OSMO_OPERATOR_NAMESPACE="osmo-operator"
OSMO_WORKFLOWS_NAMESPACE="osmo-workflows"
OSMO_IMAGE_TAG="${OSMO_IMAGE_TAG:-}"
OSMO_CHART_VERSION="${OSMO_CHART_VERSION:-}"
BACKEND_NAME="${OSMO_BACKEND_NAME:-default}"
OSMO_OPERATOR_PASSWORD_SECRET="${OSMO_OPERATOR_PASSWORD_SECRET:-osmo-operator-password}"
OSMO_OPERATOR_PASSWORD_KEY="${OSMO_OPERATOR_PASSWORD_KEY:-password}"
OSMO_OPERATOR_USERNAME="${OSMO_OPERATOR_USERNAME:-${OSMO_KC_ADMIN_USER:-osmo-admin}}"

# Detect whether the control plane is running with Keycloak-enabled auth.
KEYCLOAK_ENABLED="false"
if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
    KEYCLOAK_ENABLED="true"
elif kubectl get svc -n "${OSMO_NAMESPACE:-osmo}" keycloak &>/dev/null; then
    KEYCLOAK_ENABLED="true"
fi

# OSMO 6.2 backend-operator defaults to password auth. Token auth from the
# older script path now produces invalid access tokens against the 6.2 agent.
BACKEND_LOGIN_METHOD="${OSMO_BACKEND_LOGIN_METHOD:-}"
if [[ -z "$BACKEND_LOGIN_METHOD" ]]; then
    if [[ "$KEYCLOAK_ENABLED" == "true" ]]; then
        BACKEND_LOGIN_METHOD="password"
    else
        BACKEND_LOGIN_METHOD="token"
    fi
fi

cleanup_port_forwards() {
    local i
    for i in "${!PF_PIDS[@]}"; do
        stop_port_forward "${PF_PIDS[$i]}" "${PF_LOGS[$i]:-}"
    done
}

derive_backend_image_tag() {
    local chart_output=""

    if [[ -n "${OSMO_CHART_VERSION:-}" ]]; then
        chart_output=$(helm show chart osmo/backend-operator --version "${OSMO_CHART_VERSION}" 2>/dev/null || true)
    else
        chart_output=$(helm show chart osmo/backend-operator 2>/dev/null || true)
    fi

    echo "$chart_output" | awk -F': ' '/^appVersion:/ { gsub(/"/, "", $2); print $2; exit }'
}

resolve_keycloak_token_url() {
    KC_INGRESS_HOST=$(kubectl get ingress -n "${OSMO_NAMESPACE:-osmo}" keycloak -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || echo "")
    KC_CURL_TLS_OPT=""

    if [[ "${OSMO_KC_SKIP_TLS_VERIFY:-false}" == "true" ]]; then
        KC_CURL_TLS_OPT="-k"
        log_warning "TLS verification disabled for Keycloak token request (OSMO_KC_SKIP_TLS_VERIFY=true)"
    elif [[ "${KEYCLOAK_HOSTNAME:-}" == *.local || "${KC_INGRESS_HOST:-}" == *.local ]]; then
        KC_CURL_TLS_OPT="-k"
        log_warning "Self-signed cert detected (.local hostname) — skipping TLS verification for Keycloak token request"
    fi

    if [[ -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
        KEYCLOAK_TOKEN_URL="https://${KEYCLOAK_HOSTNAME}/realms/osmo/protocol/openid-connect/token"
        log_info "Keycloak token endpoint (KEYCLOAK_HOSTNAME): ${KEYCLOAK_TOKEN_URL}"
    elif [[ -n "$KC_INGRESS_HOST" ]]; then
        KEYCLOAK_TOKEN_URL="https://${KC_INGRESS_HOST}/realms/osmo/protocol/openid-connect/token"
        log_info "Keycloak token endpoint (ingress): ${KEYCLOAK_TOKEN_URL}"
    else
        local kc_pf_port

        kc_pf_port="${KC_PF_PORT:-8082}"
        log_info "No Keycloak ingress found; starting Keycloak port-forward..."
        start_kubectl_port_forward "${OSMO_NAMESPACE:-osmo}" svc/keycloak 80 "${kc_pf_port}" "Keycloak" || exit 1
        PF_PIDS+=("$PORT_FORWARD_PID")
        PF_LOGS+=("$PORT_FORWARD_LOG")
        kc_pf_port=$PORT_FORWARD_PORT
        if ! wait_for_http_ready "http://localhost:${kc_pf_port}/realms/osmo" 15 "Keycloak port-forward"; then
            log_error "Keycloak port-forward failed to respond in time"
            exit 1
        fi
        KEYCLOAK_TOKEN_URL="http://localhost:${kc_pf_port}/realms/osmo/protocol/openid-connect/token"
        log_info "Keycloak token endpoint (port-forward): ${KEYCLOAK_TOKEN_URL}"
    fi
}

validate_keycloak_operator_user() {
    local kc_response
    local kc_error

    if [[ "$KEYCLOAK_ENABLED" != "true" ]]; then
        log_error "Password auth for the backend operator requires Keycloak-enabled OSMO."
        log_error "Set OSMO_BACKEND_LOGIN_METHOD=token only for no-auth/dev installs."
        exit 1
    fi

    resolve_keycloak_token_url

    log_info "Authenticating backend operator user '${OSMO_OPERATOR_USERNAME}' with Keycloak..."
    kc_response=$(curl -s ${KC_CURL_TLS_OPT} -X POST "${KEYCLOAK_TOKEN_URL}" \
        -d "grant_type=password" \
        -d "client_id=osmo-device" \
        -d "username=${OSMO_OPERATOR_USERNAME}" \
        -d "password=${OSMO_OPERATOR_PASSWORD}")

    KC_JWT=$(echo "$kc_response" | jq -r '.access_token // empty' 2>/dev/null || echo "")
    if [[ -z "$KC_JWT" ]]; then
        kc_error=$(echo "$kc_response" | jq -r '.error_description // .error // empty' 2>/dev/null || echo "unknown error")
        log_error "Keycloak authentication failed: $kc_error"
        log_error "Ensure OSMO_OPERATOR_USERNAME / OSMO_OPERATOR_PASSWORD are valid local Keycloak credentials."
        log_error "If using Nebius SSO, create a local break-glass user (CREATE_OSMO_TEST_USER=true) or see AUTHENTICATION.md"
        exit 1
    fi

    log_success "Keycloak authentication successful for backend operator user '${OSMO_OPERATOR_USERNAME}'"
}

# Check for OSMO Service URL (in-cluster URL for the backend operator pods)
# IMPORTANT: Backend operators connect via WebSocket to osmo-agent, NOT osmo-service!
# The osmo-service handles REST API, osmo-agent handles WebSocket connections for backends
if [[ -z "${OSMO_SERVICE_URL:-}" ]]; then
    log_info "Auto-detecting in-cluster OSMO Agent URL..."
    
    # Backend operators MUST connect to osmo-agent for WebSocket connections
    # The osmo-service WebSocket routes only exist in dev mode
    OSMO_AGENT=$(kubectl get svc -n osmo osmo-agent -o jsonpath='{.metadata.name}' 2>/dev/null || echo "")
    
    if [[ -n "$OSMO_AGENT" ]]; then
        OSMO_SERVICE_URL="http://osmo-agent.osmo.svc.cluster.local:80"
        log_success "In-cluster Agent URL: ${OSMO_SERVICE_URL}"
    else
        # Fallback: try to detect from any osmo-agent service
        OSMO_AGENT=$(kubectl get svc -n osmo -l app.kubernetes.io/name=agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        if [[ -n "$OSMO_AGENT" ]]; then
            OSMO_SERVICE_URL="http://${OSMO_AGENT}.osmo.svc.cluster.local:80"
            log_success "In-cluster Agent URL: ${OSMO_SERVICE_URL}"
        else
            echo ""
            log_error "Could not detect OSMO Agent service. Deploy OSMO first: ./05-deploy-osmo-control-plane.sh"
            log_error "Note: Backend operators require osmo-agent service for WebSocket connections"
            exit 1
        fi
    fi
fi

# Check backend operator credentials
if [[ "$BACKEND_LOGIN_METHOD" == "password" ]]; then
    kubectl create namespace "${OSMO_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

    EXISTING_OPERATOR_PASSWORD=$(kubectl get secret "${OSMO_OPERATOR_PASSWORD_SECRET}" -n "${OSMO_OPERATOR_NAMESPACE}" -o "jsonpath={.data.${OSMO_OPERATOR_PASSWORD_KEY}}" 2>/dev/null | base64 -d || echo "")
    if [[ -z "${OSMO_OPERATOR_PASSWORD:-}" && -n "$EXISTING_OPERATOR_PASSWORD" ]]; then
        OSMO_OPERATOR_PASSWORD="$EXISTING_OPERATOR_PASSWORD"
        log_info "Using existing operator password from secret ${OSMO_OPERATOR_PASSWORD_SECRET}"
    fi
    OSMO_OPERATOR_PASSWORD="${OSMO_OPERATOR_PASSWORD:-${OSMO_KC_ADMIN_PASS:-osmo-admin}}"

    PF_PIDS=()
    PF_LOGS=()
    trap cleanup_port_forwards EXIT
    validate_keycloak_operator_user
    cleanup_port_forwards
    trap - EXIT
fi

if [[ "$BACKEND_LOGIN_METHOD" == "token" && -z "${OSMO_SERVICE_TOKEN:-}" ]]; then
    # First, ensure namespace exists so we can check for existing secret
    kubectl create namespace "${OSMO_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
    
    # Check if token secret already exists in cluster
    EXISTING_TOKEN=$(kubectl get secret osmo-operator-token -n "${OSMO_OPERATOR_NAMESPACE}" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || echo "")
    
    if [[ -n "$EXISTING_TOKEN" ]]; then
        log_info "Using existing token from secret osmo-operator-token"
        OSMO_SERVICE_TOKEN="$EXISTING_TOKEN"
    elif command -v osmo &>/dev/null; then
        # Check if osmo CLI is already logged in (don't try to login with in-cluster URL)
        log_info "Checking if OSMO CLI is already logged in..."
        
        # Try to generate token - this only works if CLI is already logged in
        TOKEN_NAME="backend-token-$(date -u +%Y%m%d%H%M%S)"
        EXPIRY_DATE=$(date -u -d "+1 year" +%F 2>/dev/null || date -u -v+1y +%F 2>/dev/null || echo "2027-01-01")
        
        TOKEN_JSON=$(osmo token set "$TOKEN_NAME" \
            --expires-at "$EXPIRY_DATE" \
            --description "Backend Operator Token" \
            --service --roles osmo-backend -t json 2>/dev/null || echo "")
        
        if [[ -n "$TOKEN_JSON" ]]; then
            OSMO_SERVICE_TOKEN=$(echo "$TOKEN_JSON" | jq -r '.token // empty' 2>/dev/null || echo "")
        fi
        
        if [[ -n "$OSMO_SERVICE_TOKEN" ]]; then
            log_success "Service token generated: $TOKEN_NAME (expires: $EXPIRY_DATE)"
        fi
    fi
    
    # If still no token, automatically create one using port-forward
    if [[ -z "$OSMO_SERVICE_TOKEN" ]]; then
        log_info "No token found - automatically creating service token..."

        TOKEN_NAME="backend-token-$(date -u +%Y%m%d%H%M%S)"
        EXPIRY_DATE=$(date -u -d "+1 year" +%F 2>/dev/null || date -u -v+1y +%F 2>/dev/null || echo "2027-01-01")

        PF_PIDS=()
        PF_LOGS=()
        trap cleanup_port_forwards EXIT

        if [[ "$KEYCLOAK_ENABLED" == "true" ]]; then
            # ---------------------------------------------------------------
            # Keycloak-enabled: use Resource Owner Password Grant to get JWT,
            # then call OSMO REST API with Bearer token
            # ---------------------------------------------------------------
            log_info "Keycloak detected - using password grant for token creation..."
            resolve_keycloak_token_url

            # Port-forward to OSMO service (for the token creation API)
            log_info "Starting port-forward to OSMO service..."
            start_kubectl_port_forward "${OSMO_NAMESPACE:-osmo}" svc/osmo-service 80 8080 "OSMO service" || exit 1
            SVC_PF_PID=$PORT_FORWARD_PID
            SVC_PF_PORT=$PORT_FORWARD_PORT
            SVC_PF_LOG=$PORT_FORWARD_LOG
            PF_PIDS+=("$SVC_PF_PID")
            PF_LOGS+=("$SVC_PF_LOG")
            if ! wait_for_http_ready "http://localhost:${SVC_PF_PORT}/api/version" 30 "OSMO API"; then
                log_error "Port-forward failed to start within 30s"
                exit 1
            fi
            log_success "Port-forward ready at http://localhost:${SVC_PF_PORT}"

            # Get Keycloak JWT via Resource Owner Password Grant
            # Uses osmo-device client (public, directAccessGrantsEnabled=true)
            # MUST use external Keycloak URL so the JWT issuer matches what Envoy expects
            KC_ADMIN_USER="${OSMO_OPERATOR_USERNAME}"
            KC_ADMIN_PASS="${OSMO_OPERATOR_PASSWORD:-${OSMO_KC_ADMIN_PASS:-osmo-admin}}"
            # When Nebius SSO is primary, the default osmo-admin user is not created; set CREATE_OSMO_TEST_USER=true
            # in 04-deploy-osmo-control-plane.sh or set OSMO_KC_ADMIN_USER/OSMO_KC_ADMIN_PASS to a valid local user.

            log_info "Authenticating with Keycloak as '${KC_ADMIN_USER}'..."
            KC_RESPONSE=$(curl -s ${KC_CURL_TLS_OPT} -X POST "${KEYCLOAK_TOKEN_URL}" \
                -d "grant_type=password" \
                -d "client_id=osmo-device" \
                -d "username=${KC_ADMIN_USER}" \
                -d "password=${KC_ADMIN_PASS}")

            KC_JWT=$(echo "$KC_RESPONSE" | jq -r '.access_token // empty' 2>/dev/null || echo "")
            if [[ -z "$KC_JWT" ]]; then
                KC_ERROR=$(echo "$KC_RESPONSE" | jq -r '.error_description // .error // empty' 2>/dev/null || echo "unknown error")
                log_error "Keycloak authentication failed: $KC_ERROR"
                log_error "Ensure OSMO_KC_ADMIN_USER and OSMO_KC_ADMIN_PASS are set. If using Nebius SSO, create a local user (e.g. CREATE_OSMO_TEST_USER=true) or see AUTHENTICATION.md"
                exit 1
            fi
            log_success "Keycloak authentication successful"

            # Create service token via OSMO REST API
            # NOTE: Must use "x-osmo-auth" header (not Authorization), because:
            #   1. Envoy's OAuth2 filter runs first and would redirect to Keycloak
            #      if it doesn't see OAuth cookies. The "x-osmo-auth" header triggers
            #      the pass_through_matcher, bypassing the OAuth2 redirect.
            #   2. Envoy's JWT filter reads from "x-osmo-auth" (not Authorization).
            #   3. No "Bearer " prefix -- the JWT filter has no value_prefix configured,
            #      so it expects the raw JWT directly.
            log_info "Creating service token: $TOKEN_NAME (expires: $EXPIRY_DATE)..."
            TOKEN_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
                "http://localhost:${SVC_PF_PORT}/api/auth/access_token/service/${TOKEN_NAME}?expires_at=${EXPIRY_DATE}&roles=osmo-backend" \
                -H "x-osmo-auth: ${KC_JWT}" \
                -H "Content-Type: application/json")

            # Separate response body from HTTP status code
            HTTP_CODE=$(echo "$TOKEN_RESPONSE" | tail -1)
            TOKEN_BODY=$(echo "$TOKEN_RESPONSE" | sed '$d')

            if [[ "$HTTP_CODE" != "200" && "$HTTP_CODE" != "201" ]]; then
                log_error "Token creation API returned HTTP $HTTP_CODE"
                log_error "Response: $TOKEN_BODY"
                exit 1
            fi

            # Response is the raw token string (quoted JSON string)
            OSMO_SERVICE_TOKEN=$(echo "$TOKEN_BODY" | jq -r '. // empty' 2>/dev/null || echo "")
            # If jq fails (response might be a plain string, not JSON), use raw
            if [[ -z "$OSMO_SERVICE_TOKEN" ]]; then
                OSMO_SERVICE_TOKEN=$(echo "$TOKEN_BODY" | tr -d '"' | tr -d '\r' | xargs)
            fi

        else
            # ---------------------------------------------------------------
            # No Keycloak: use dev auth method (original approach)
            # ---------------------------------------------------------------
            # Check if osmo CLI is available
            if ! command -v osmo &>/dev/null; then
                log_error "osmo CLI not found. Please install it first."
                exit 1
            fi

            # Start port-forward in background
            log_info "Starting port-forward to OSMO service..."
            start_kubectl_port_forward "${OSMO_NAMESPACE:-osmo}" svc/osmo-service 80 8080 "OSMO service" || exit 1
            SVC_PF_PID=$PORT_FORWARD_PID
            SVC_PF_PORT=$PORT_FORWARD_PORT
            SVC_PF_LOG=$PORT_FORWARD_LOG
            PF_PIDS+=("$SVC_PF_PID")
            PF_LOGS+=("$SVC_PF_LOG")
            if ! wait_for_http_ready "http://localhost:${SVC_PF_PORT}/api/version" 30 "OSMO API"; then
                log_error "Port-forward failed to start within 30s"
                exit 1
            fi
            log_success "Port-forward ready at http://localhost:${SVC_PF_PORT}"

            # Login with dev method (auth is disabled)
            log_info "Logging in to OSMO (dev method)..."
            if ! osmo login "http://localhost:${SVC_PF_PORT}" --method dev --username admin 2>/dev/null; then
                log_error "Failed to login to OSMO. If Keycloak is enabled, set DEPLOY_KEYCLOAK=true"
                exit 1
            fi
            log_success "Logged in successfully"

            # Create service token
            log_info "Creating service token: $TOKEN_NAME (expires: $EXPIRY_DATE)..."
            TOKEN_OUTPUT=$(osmo token set "$TOKEN_NAME" \
                --expires-at "$EXPIRY_DATE" \
                --description "Backend Operator Token (auto-generated)" \
                --service --roles osmo-backend 2>&1)

            # Extract token from output (format: "Access token: <token>")
            OSMO_SERVICE_TOKEN=$(echo "$TOKEN_OUTPUT" | sed -n 's/.*Access token: //p' | tr -d '\r' | xargs)
        fi

        if [[ -z "$OSMO_SERVICE_TOKEN" ]]; then
            log_error "Failed to create service token"
            echo "Response: ${TOKEN_RESPONSE:-$TOKEN_OUTPUT}"
            exit 1
        fi

        log_success "Service token created: $TOKEN_NAME (expires: $EXPIRY_DATE)"

        # Stop port-forwards
        cleanup_port_forwards
        trap - EXIT
    fi
fi

# -----------------------------------------------------------------------------
# Add OSMO Helm Repository
# -----------------------------------------------------------------------------
log_info "Adding OSMO Helm repository..."
helm repo add osmo https://helm.ngc.nvidia.com/nvidia/osmo --force-update
helm repo update

if [[ -z "$OSMO_IMAGE_TAG" ]]; then
    OSMO_IMAGE_TAG=$(derive_backend_image_tag)
    OSMO_IMAGE_TAG="${OSMO_IMAGE_TAG:-6.2}"
    log_info "Using backend image tag: ${OSMO_IMAGE_TAG}"
fi

# -----------------------------------------------------------------------------
# Create Namespaces
# -----------------------------------------------------------------------------
log_info "Creating namespaces..."
kubectl create namespace "${OSMO_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace "${OSMO_WORKFLOWS_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# -----------------------------------------------------------------------------
# Create Secrets
# -----------------------------------------------------------------------------
if [[ "$BACKEND_LOGIN_METHOD" == "password" ]]; then
    log_info "Creating operator password secret..."
    kubectl create secret generic "${OSMO_OPERATOR_PASSWORD_SECRET}" \
        --namespace "${OSMO_OPERATOR_NAMESPACE}" \
        --from-literal="${OSMO_OPERATOR_PASSWORD_KEY}=${OSMO_OPERATOR_PASSWORD}" \
        --dry-run=client -o yaml | kubectl apply -f -
else
    log_info "Creating operator token secret..."
    kubectl create secret generic osmo-operator-token \
        --namespace "${OSMO_OPERATOR_NAMESPACE}" \
        --from-literal=token="${OSMO_SERVICE_TOKEN}" \
        --dry-run=client -o yaml | kubectl apply -f -
fi

# -----------------------------------------------------------------------------
# Create Values File
# -----------------------------------------------------------------------------
log_info "Creating Helm values file..."

# Note: services.backendListener/Worker are at root level, not under global
# See: osmo-helm-charts/backend-operator/values.yaml
cat > /tmp/backend_operator_values.yaml <<EOF
global:
  osmoImageTag: "${OSMO_IMAGE_TAG}"
  serviceUrl: "${OSMO_SERVICE_URL}"
  agentNamespace: "${OSMO_OPERATOR_NAMESPACE}"
  backendNamespace: "${OSMO_WORKFLOWS_NAMESPACE}"
  backendName: "${BACKEND_NAME}"
$(if [[ "$BACKEND_LOGIN_METHOD" == "password" ]]; then
cat <<AUTH_BLOCK
  accountUsername: "${OSMO_OPERATOR_USERNAME}"
  accountPasswordSecret: "${OSMO_OPERATOR_PASSWORD_SECRET}"
  accountPasswordSecretKey: "${OSMO_OPERATOR_PASSWORD_KEY}"
  loginMethod: password
AUTH_BLOCK
else
cat <<AUTH_BLOCK
  accountTokenSecret: osmo-operator-token
  accountTokenSecretKey: token
  loginMethod: token
AUTH_BLOCK
fi)
  # Node selector to prefer system/CPU nodes (not GPU nodes)
  nodeSelector:
    kubernetes.io/os: linux
  
  # Logging configuration
  logs:
    logLevel: DEBUG
    k8sLogLevel: WARNING

# Service-specific configurations (at root level, not under global)
services:
  backendListener:
    resources:
      requests:
        cpu: "100m"
        memory: "256Mi"
      limits:
        memory: "1Gi"
  
  backendWorker:
    resources:
      requests:
        cpu: "100m"
        memory: "256Mi"
      limits:
        memory: "1Gi"

# Disable test runner for initial deployment
backendTestRunner:
  enabled: false
EOF

# -----------------------------------------------------------------------------
# Deploy Backend Operator
# -----------------------------------------------------------------------------
log_info "Deploying OSMO Backend Operator..."

HELM_ARGS=(
    --namespace "${OSMO_OPERATOR_NAMESPACE}"
    -f /tmp/backend_operator_values.yaml
    --wait
    --timeout 10m
)

if [[ -n "${OSMO_CHART_VERSION}" ]]; then
    HELM_ARGS+=(--version "${OSMO_CHART_VERSION}")
fi

helm upgrade --install osmo-operator osmo/backend-operator "${HELM_ARGS[@]}"

log_success "OSMO Backend Operator deployed"

# Cleanup temp file
rm -f /tmp/backend_operator_values.yaml

# -----------------------------------------------------------------------------
# Verify Deployment
# -----------------------------------------------------------------------------
echo ""
log_info "Verifying deployment..."

kubectl get pods -n "${OSMO_OPERATOR_NAMESPACE}"
kubectl get pods -n "${OSMO_WORKFLOWS_NAMESPACE}" 2>/dev/null || true

echo ""
echo "========================================"
log_success "OSMO Backend Operator deployment complete!"
echo "========================================"
echo ""
echo "Backend Name: ${BACKEND_NAME}"
echo "Agent URL (WebSocket): ${OSMO_SERVICE_URL}"
echo ""
# Detect Ingress URL for verification instructions
INGRESS_URL=$(detect_service_url 2>/dev/null || true)

echo "To verify the backend registration:"
echo ""
if [[ -n "$INGRESS_URL" ]]; then
    echo "  Check backend status:"
    echo "    osmo config show BACKEND ${BACKEND_NAME}"
    echo ""
    echo "  Or via curl (using NGINX Ingress LoadBalancer):"
    echo "    curl ${INGRESS_URL}/api/configs/backend"
else
    echo "  Terminal 1 - Start port-forward (keep running):"
    echo "    kubectl port-forward -n osmo svc/osmo-service 8080:80"
    echo ""
    echo "  Terminal 2 - Check backend status:"
    echo "    osmo config show BACKEND ${BACKEND_NAME}"
    echo ""
    echo "  Or via curl:"
    echo "    curl http://localhost:8080/api/configs/backend"
fi
echo ""
echo "Next step - Configure Storage:"
echo "  ./07-configure-storage.sh"
echo ""
