#!/bin/bash
#
# Configure OSMO Storage
# https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/configure_data_storage.html
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  OSMO Storage Configuration"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1

OSMO_NS="${OSMO_NAMESPACE:-osmo}"

# -----------------------------------------------------------------------------
# Get Storage Configuration from Terraform
# -----------------------------------------------------------------------------
log_info "Retrieving storage configuration from Terraform..."

S3_BUCKET=$(get_tf_output "storage_bucket.name" "../001-iac" 2>/dev/null || echo "")
S3_ENDPOINT=$(get_tf_output "storage_bucket.endpoint" "../001-iac" 2>/dev/null || echo "")
S3_REGION=$(get_tf_output "storage_bucket.region" "../001-iac" 2>/dev/null || echo "")

if [[ -z "$S3_BUCKET" || -z "$S3_ENDPOINT" || -z "$S3_REGION" ]]; then
    log_error "Could not retrieve the storage bucket name, endpoint, and region from Terraform"
    echo ""
    echo "Make sure you have run 'terraform apply' in deploy/001-iac"
    echo "and that storage is enabled in your terraform.tfvars"
    exit 1
fi

if [[ -n "${NEBIUS_REGION:-}" && "$NEBIUS_REGION" != "$S3_REGION" ]]; then
    log_error "NEBIUS_REGION '${NEBIUS_REGION}' does not match the bucket region '${S3_REGION}'"
    exit 1
fi

S3_ENDPOINT=$(normalize_nebius_storage_endpoint "${S3_ENDPOINT}")

log_success "Storage bucket: ${S3_BUCKET}"
log_success "Storage endpoint: ${S3_ENDPOINT}"
log_success "Storage region: ${S3_REGION}"

# -----------------------------------------------------------------------------
# Sync osmo-storage secret and verify direct bucket access
# -----------------------------------------------------------------------------
log_info "Syncing osmo-storage secret..."

if ! sync_osmo_storage_secret "${OSMO_NS}" "${SCRIPT_DIR}/../001-iac"; then
    echo ""
    echo "Re-run 05-deploy-osmo-control-plane.sh if Terraform / MysteryBox outputs are unavailable."
    exit 1
fi

log_info "Probing Nebius Object Storage with osmo-storage credentials..."
if probe_nebius_bucket_rw "${OSMO_NS}" "${S3_BUCKET}" "${S3_ENDPOINT}" "${S3_REGION}"; then
    log_success "Object Storage probe passed"
else
    log_error "Object Storage probe failed with the current osmo-storage credentials"
    exit 1
fi

# -----------------------------------------------------------------------------
# Start port-forward and configure storage
# -----------------------------------------------------------------------------
log_info "Starting port-forward to OSMO service..."

if ! start_osmo_api_session "${OSMO_NS}" 8080 60; then
    log_error "Port-forward failed to start within 60s"
    echo "  Check: kubectl get pods -n ${OSMO_NS} -l app=osmo-service"
    exit 1
fi
OSMO_URL="${OSMO_API_URL}"

# Cleanup function
cleanup_port_forward() {
    stop_port_forward
}
trap cleanup_port_forward EXIT

log_success "Port-forward ready at ${OSMO_URL}"

# Login (no-op when bypassing Envoy -- curl headers handle auth)
osmo_login "${OSMO_API_PORT}" || exit 1

# -----------------------------------------------------------------------------
# Ensure storage-capable deployments are wired to use osmo-storage
# -----------------------------------------------------------------------------
log_info "Checking deployment AWS credential wiring..."

for deploy in osmo-service osmo-worker; do
    if ! kubectl get deployment "$deploy" -n "${OSMO_NS}" -o json 2>/dev/null | \
        jq -e '
            .spec.template.spec.containers[]
            | select(.name == "'"${deploy}"'")
            | [.env[]? | .name]
            | index("AWS_ACCESS_KEY_ID") and index("AWS_SECRET_ACCESS_KEY")
        ' >/dev/null; then
        log_error "${deploy} is missing AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from osmo-storage. Re-run 05-deploy-osmo-control-plane.sh first."
        exit 1
    fi
done

# Nebius Object Storage uses an S3-compatible API. The workflow config should
# define the bucket and HTTPS endpoint. This OSMO build still validates
# workflow_data/workflow_log against a schema that requires inline access keys,
# so keep those fields populated from osmo-storage even though 05 also injects
# AWS_* env vars into the pods.
BACKEND_URI="s3://${S3_BUCKET}"
OVERRIDE_URL="${S3_ENDPOINT}"
REGION="${S3_REGION}"
S3_ACCESS_KEY=$(get_kubernetes_secret_value "${OSMO_NS}" osmo-storage "access-key-id")
S3_SECRET_KEY=$(get_kubernetes_secret_value "${OSMO_NS}" osmo-storage "secret-access-key")

if [[ -z "${S3_ACCESS_KEY}" || -z "${S3_SECRET_KEY}" ]]; then
    log_error "osmo-storage secret is missing access-key-id or secret-access-key"
    exit 1
fi

WORKFLOW_STORAGE_VALUE=$(jq -cn \
    --arg endpoint "${BACKEND_URI}" \
    --arg override_url "${OVERRIDE_URL}" \
    --arg access_key_id "${S3_ACCESS_KEY}" \
    --arg access_key "${S3_SECRET_KEY}" \
    --arg region "${REGION}" \
    '{credential: {endpoint: $endpoint, override_url: $override_url, access_key_id: $access_key_id, access_key: $access_key, region: $region}}')

log_success "Deployment AWS credential wiring verified"

# -----------------------------------------------------------------------------
# Configure Workflow Log Storage in OSMO
# -----------------------------------------------------------------------------
log_info "Configuring workflow log storage..."

if upsert_osmo_config_value_db "${OSMO_NS}" WORKFLOW workflow_log "${WORKFLOW_STORAGE_VALUE}"; then
    log_success "Workflow log storage configured"
else
    log_error "Failed to configure workflow log storage"
    exit 1
fi

# -----------------------------------------------------------------------------
# Configure Workflow Data Storage in OSMO
# -----------------------------------------------------------------------------
log_info "Configuring workflow data storage..."

if upsert_osmo_config_value_db "${OSMO_NS}" WORKFLOW workflow_data "${WORKFLOW_STORAGE_VALUE}"; then
    log_success "Workflow data storage configured"
else
    log_error "Failed to configure workflow data storage"
    exit 1
fi

# -----------------------------------------------------------------------------
# Configure Workflow Limits
# -----------------------------------------------------------------------------
log_info "Configuring workflow limits (max_num_tasks=200)..."

WORKFLOW_LIMITS_CONFIG=$(cat <<EOF
{
  "max_num_tasks": 200
}
EOF
)

echo "$WORKFLOW_LIMITS_CONFIG" > /tmp/workflow_limits_config.json

if osmo_config_update WORKFLOW /tmp/workflow_limits_config.json "Configure workflow limits" "${OSMO_API_PORT}"; then
    log_success "Workflow limits configured (max_num_tasks=200)"
else
    log_warning "Failed to configure workflow limits (may require newer OSMO version)"
fi

rm -f /tmp/workflow_limits_config.json

# -----------------------------------------------------------------------------
# Restart components that cache workflow storage config
# -----------------------------------------------------------------------------
# OSMO worker uploads workflow specs/logs to object storage. Service and logger
# read those artifacts back, and agent/logger also rely on the shared AWS_*
# env wiring from 05. Restart the storage-aware components so they all reload
# the current endpoint and credential path.
log_info "Restarting OSMO components to reload workflow storage config..."

for deploy in osmo-worker osmo-service osmo-logger osmo-agent; do
    if kubectl get deployment "$deploy" -n "${OSMO_NS}" >/dev/null 2>&1; then
        if kubectl rollout restart "deployment/${deploy}" -n "${OSMO_NS}" >/dev/null 2>&1; then
            log_info "Waiting for ${deploy} rollout..."
            if kubectl rollout status "deployment/${deploy}" -n "${OSMO_NS}" --timeout=300s >/dev/null 2>&1; then
                log_success "${deploy} restarted"
            else
                log_warning "${deploy} restart did not complete before timeout; verify with: kubectl rollout status deployment/${deploy} -n ${OSMO_NS}"
            fi
        else
            log_warning "Could not restart ${deploy}; verify it manually after storage changes"
        fi
    else
        log_warning "Deployment ${deploy} not found in namespace ${OSMO_NS}"
    fi
done

# -----------------------------------------------------------------------------
# Verify Configuration
# -----------------------------------------------------------------------------
log_info "Verifying storage configuration..."

echo ""
echo "Workflow configuration:"
osmo_curl GET "${OSMO_URL}/api/configs/workflow" 2>/dev/null | jq '.' || \
    log_warning "Could not retrieve workflow config for verification"

# Cleanup
cleanup_port_forward
trap - EXIT

echo ""
echo "========================================"
log_success "OSMO Storage configuration complete!"
echo "========================================"
echo ""
echo "Storage Details:"
echo "  Bucket: ${S3_BUCKET}"
echo "  Endpoint: ${S3_ENDPOINT}"
echo "  Backend URI: ${BACKEND_URI}"
echo "  Override URL: ${OVERRIDE_URL}"
echo "  Region: ${REGION}"
echo ""
echo "Configured:"
echo "  - workflow_log: For storing workflow logs"
echo "  - workflow_data: For storing intermediate task data"
echo ""
echo "OSMO can now store workflow artifacts in Nebius Object Storage."
echo ""
