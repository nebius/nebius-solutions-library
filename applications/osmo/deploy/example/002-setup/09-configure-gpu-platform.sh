#!/bin/bash
# Configure OSMO GPU platform with tolerations via pod templates
# Based on OSMO documentation: https://nvidia.github.io/OSMO/main/deployment_guide/install_backend/resource_pools.html

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

OSMO_URL="${OSMO_URL:-http://localhost:8080}"
OSMO_NAMESPACE="${OSMO_NAMESPACE:-osmo}"

# Resolve the Object Storage settings from the bucket created by Terraform.
STORAGE_REGION=$(get_tf_output "storage_bucket.region" "../001-iac" 2>/dev/null || echo "")
STORAGE_ENDPOINT=$(get_tf_output "storage_bucket.endpoint" "../001-iac" 2>/dev/null || echo "")

if [[ -z "$STORAGE_REGION" || -z "$STORAGE_ENDPOINT" ]]; then
    log_error "Could not retrieve the Object Storage region and endpoint from Terraform"
    exit 1
fi

if [[ -n "${NEBIUS_REGION:-}" && "$NEBIUS_REGION" != "$STORAGE_REGION" ]]; then
    log_error "NEBIUS_REGION '${NEBIUS_REGION}' does not match the bucket region '${STORAGE_REGION}'"
    exit 1
fi

STORAGE_ENDPOINT=$(normalize_nebius_storage_endpoint "$STORAGE_ENDPOINT")

# -----------------------------------------------------------------------------
# Determine GPU platform name
# -----------------------------------------------------------------------------
# Try to read the GPU platform from Terraform output and derive a friendly name.
# Maps: gpu-h100-sxm -> H100, gpu-h200-sxm -> H200, gpu-b200-sxm-a -> B200, etc.
# Falls back to user input if Terraform is unavailable.
if [[ -z "${GPU_PLATFORM_NAME:-}" ]]; then
    TF_GPU_PLATFORM=$(get_tf_output "gpu_nodes_platform" "../001-iac" 2>/dev/null || echo "")
    if [[ -n "$TF_GPU_PLATFORM" ]]; then
        # Extract friendly name: gpu-h100-sxm -> H100, gpu-b200-sxm-a -> B200, gpu-l40s-a -> L40S
        GPU_PLATFORM_NAME=$(echo "$TF_GPU_PLATFORM" | sed -E 's/^gpu-([a-zA-Z0-9]+).*/\1/' | tr '[:lower:]' '[:upper:]')
        log_info "Auto-detected GPU platform from Terraform: ${TF_GPU_PLATFORM} -> ${GPU_PLATFORM_NAME}"
    else
        echo ""
        echo "Could not auto-detect GPU platform from Terraform."
        read -r -p "Enter GPU platform name (e.g. H100, H200, B200, L40S): " GPU_PLATFORM_NAME
        if [[ -z "$GPU_PLATFORM_NAME" ]]; then
            log_error "GPU platform name is required."
            exit 1
        fi
    fi
fi

echo ""
echo "========================================"
echo "  OSMO GPU Platform Configuration"
echo "  Platform name: ${GPU_PLATFORM_NAME}"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1

# -----------------------------------------------------------------------------
# Start port-forward
# -----------------------------------------------------------------------------
log_info "Starting port-forward to OSMO service..."

start_osmo_api_session "${OSMO_NAMESPACE}" 8080 30 || exit 1
OSMO_URL="${OSMO_API_URL}"

cleanup_port_forward() {
    stop_port_forward
}
trap cleanup_port_forward EXIT

log_success "Port-forward ready at ${OSMO_URL}"

# -----------------------------------------------------------------------------
# Step 1: Fix default_user pod template (remove GPU resources)
# -----------------------------------------------------------------------------
# The built-in default_user template includes nvidia.com/gpu which causes ALL
# workflows (including CPU-only) to request the nvidia RuntimeClass. This fails
# on CPU nodes. We move GPU resources to the gpu_tolerations template instead.
log_info "Updating default_user pod template (removing GPU resources)..."

RESPONSE=$(osmo_curl PUT "${OSMO_URL}/api/configs/pod_template/default_user" \
  -w "\n%{http_code}" \
  -d @"${SCRIPT_DIR}/default_user_pod_template.json")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
    log_success "default_user pod template updated (HTTP ${HTTP_CODE})"
else
    log_error "Failed to update default_user pod template (HTTP ${HTTP_CODE})"
    echo "Response: ${BODY}"
    exit 1
fi

# -----------------------------------------------------------------------------
# Step 2: Create GPU pod template
# -----------------------------------------------------------------------------
log_info "Creating gpu_tolerations pod template..."

# Substitute the authoritative bucket region and endpoint in the template.
GPU_POD_TEMPLATE_RESOLVED="/tmp/gpu_pod_template_resolved.json"
sed \
  -e "s|{{NEBIUS_REGION}}|${STORAGE_REGION}|g" \
  -e "s|{{NEBIUS_STORAGE_ENDPOINT}}|${STORAGE_ENDPOINT}|g" \
  "${SCRIPT_DIR}/gpu_pod_template.json" > "${GPU_POD_TEMPLATE_RESOLVED}"

RESPONSE=$(osmo_curl PUT "${OSMO_URL}/api/configs/pod_template/gpu_tolerations" \
  -w "\n%{http_code}" \
  -d @"${GPU_POD_TEMPLATE_RESOLVED}")
rm -f "${GPU_POD_TEMPLATE_RESOLVED}"
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
    log_success "Pod template created (HTTP ${HTTP_CODE})"
else
    log_error "Failed to create pod template (HTTP ${HTTP_CODE})"
    echo "Response: ${BODY}"
    exit 1
fi

# -----------------------------------------------------------------------------
# Step 2b: Create shared memory pod template
# -----------------------------------------------------------------------------
log_info "Creating shm pod template (shared memory for vLLM, PyTorch, etc.)..."

RESPONSE=$(osmo_curl PUT "${OSMO_URL}/api/configs/pod_template/shm" \
  -w "\n%{http_code}" \
  -d @"${SCRIPT_DIR}/shm_pod_template.json")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
    log_success "Shared memory pod template created (HTTP ${HTTP_CODE})"
else
    log_error "Failed to create shm pod template (HTTP ${HTTP_CODE})"
    echo "Response: ${BODY}"
    exit 1
fi

# -----------------------------------------------------------------------------
# Step 3: Create GPU platform
# -----------------------------------------------------------------------------
log_info "Creating platform '${GPU_PLATFORM_NAME}' in default pool..."

RESPONSE=$(osmo_curl PUT "${OSMO_URL}/api/configs/pool/default/platform/${GPU_PLATFORM_NAME}" \
  -w "\n%{http_code}" \
  -d @"${SCRIPT_DIR}/gpu_platform_update.json")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ge 200 && "$HTTP_CODE" -lt 300 ]]; then
    log_success "Platform '${GPU_PLATFORM_NAME}' created (HTTP ${HTTP_CODE})"
else
    log_error "Failed to create platform '${GPU_PLATFORM_NAME}' (HTTP ${HTTP_CODE})"
    echo "Response: ${BODY}"
    exit 1
fi

# -----------------------------------------------------------------------------
# Step 4: Verify configuration
# -----------------------------------------------------------------------------
log_info "Verifying configuration..."

echo ""
echo "Pod templates:"
osmo_curl GET "${OSMO_URL}/api/configs/pod_template" | jq 'keys'

echo ""
echo "Platform '${GPU_PLATFORM_NAME}' config:"
osmo_curl GET "${OSMO_URL}/api/configs/pool/default" | jq --arg platform "${GPU_PLATFORM_NAME}" '
  if (.platforms | type) == "object" then
    .platforms[$platform]
  else
    (.platforms[]? | select(.name == $platform))
  end
'

# -----------------------------------------------------------------------------
# Step 5: Check GPU resources
# -----------------------------------------------------------------------------
log_info "Checking GPU resources..."
sleep 3  # Wait for backend to pick up changes

RESOURCE_JSON=$(osmo_curl GET "${OSMO_URL}/api/resources" 2>/dev/null || echo '{}')
RESOURCE_COUNT=$(echo "$RESOURCE_JSON" | jq '
  (
    if type == "array" then
      .
    else
      (.resources // [])
    end
  )
  | map(select((.allocatable_fields.gpu // .gpu // null) != null))
  | length
' 2>/dev/null || echo "0")
echo "GPU nodes visible to OSMO: ${RESOURCE_COUNT}"

if [[ "$RESOURCE_COUNT" -gt 0 ]]; then
    echo ""
    echo "GPU resources:"
    echo "$RESOURCE_JSON" | jq '
      (
        if type == "array" then
          .
        else
          (.resources // [])
        end
      )[]
      | select((.allocatable_fields.gpu // .gpu // null) != null)
      | {
          name: .name,
          pool: (.pool_name // .pool // null),
          platform: (.platform_name // .platform // null),
          gpu: (.allocatable_fields.gpu // .gpu // null),
          cpu: (.allocatable_fields.cpu // .cpu // null),
          memory: (.allocatable_fields.memory // .memory // null)
        }
    '
fi

# -----------------------------------------------------------------------------
# Step 6: Set default pool profile
# -----------------------------------------------------------------------------
# Default pool is a per-user setting. When using port-forward with auth bypass,
# the OSMO CLI (osmo profile set pool) is not authenticated, so it may get 403.
# Try API first with auth bypass; fall back to CLI and then to a clear message.
log_info "Setting default pool to 'default'..."
_set_pool_ok=false
_resp=$(osmo_curl PATCH "${OSMO_URL}/api/users/me" -d '{"default_pool":"default"}' -w "\n%{http_code}" 2>/dev/null) || true
_http_code=$(echo "$_resp" | tail -n1)
if [[ "$_http_code" =~ ^2 ]]; then
    _set_pool_ok=true
fi
if [[ "$_set_pool_ok" != "true" ]]; then
    osmo profile set pool default 2>/dev/null && _set_pool_ok=true || true
fi
if [[ "$_set_pool_ok" == "true" ]]; then
    log_success "Default pool set"
else
    log_warning "Could not set default pool (403 is expected when using port-forward). Set once from the OSMO UI (Settings) or after logging in: osmo login <OSMO_URL> then osmo profile set pool default"
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
log_success "GPU platform configuration complete"
echo ""
echo "To submit a GPU workflow:"
echo "  osmo workflow submit workflows/osmo/gpu_test.yaml -p default --platform ${GPU_PLATFORM_NAME}"
echo ""
echo "Or test via curl:"
echo "  curl -X POST ${OSMO_URL}/api/workflow -H 'Content-Type: application/yaml' --data-binary @workflows/osmo/gpu_test.yaml"
