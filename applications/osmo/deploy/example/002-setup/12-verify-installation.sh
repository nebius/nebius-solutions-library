#!/bin/bash
# =============================================================================
# OSMO Installation Verification Script
# =============================================================================
# Checks that all required components are properly configured:
#   1. GPU Operator running with driver enabled, matching the configured driver.version
#   2. /mnt/data mounted on all nodes
#   3. 64Gi /dev/shm pod template configured in OSMO
#   4. Redis sized correctly (8 vCPU, ~52.82Gi mem, 50Gi PVC)
#   5. max_num_tasks >= 200 in WORKFLOW config
#   6. Platform name is not the default "gpu"
#
# Prerequisites:
#   - kubectl configured and connected to the target cluster
#     (run: nebius mk8s cluster get-credentials --id <cluster-id> --external)
#   - helm CLI installed (for GPU Operator checks)
#   - jq installed
#   - curl installed
#   - OSMO CLI installed and accessible (for osmo login)
#   - No fixed local port requirement; the script auto-selects a free port for OSMO port-forwarding
#   - NEBIUS_REGION set (run: source ../000-prerequisites/nebius-env-init.sh)
#
# Usage:
#   ./12-verify-installation.sh
#
# Environment variables (optional overrides):
#   OSMO_URL                 OSMO API URL (default: http://localhost:8080)
#   OSMO_NAMESPACE           Namespace where OSMO is deployed (default: osmo)
#   GPU_OPERATOR_NAMESPACE   Namespace for GPU Operator (default: gpu-operator)
#   EXPECTED_DRIVER_VERSION  Expected NVIDIA driver version override (default: use GPU Operator driver.version)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

OSMO_URL="${OSMO_URL:-http://localhost:8080}"
OSMO_NAMESPACE="${OSMO_NAMESPACE:-osmo}"
EXPECTED_DRIVER_VERSION="${EXPECTED_DRIVER_VERSION:-}"
MIN_REDIS_CPU=8
MIN_REDIS_MEMORY_GI=50
MIN_REDIS_PVC_GI=50
MIN_MAX_NUM_TASKS=200
EXPECTED_SHM_SIZE="64Gi"

PASS=0
FAIL=0
WARN=0

check_pass() {
    ((PASS++))
    log_success "$1"
}

check_fail() {
    ((FAIL++))
    log_error "$1"
}

check_warn() {
    ((WARN++))
    log_warning "$1"
}

echo ""
echo "========================================"
echo "  OSMO Installation Verification"
echo "========================================"
echo ""

# -----------------------------------------------------------------------------
# Prerequisite checks
# -----------------------------------------------------------------------------
log_info "Checking prerequisites..."

PREREQ_OK=true

if ! command -v kubectl &>/dev/null; then
    log_error "kubectl not found. Install it and configure cluster access first."
    PREREQ_OK=false
fi

if ! command -v helm &>/dev/null; then
    log_error "helm not found. Install helm to check GPU Operator configuration."
    PREREQ_OK=false
fi

if ! command -v jq &>/dev/null; then
    log_error "jq not found. Install jq for JSON parsing."
    PREREQ_OK=false
fi

if ! command -v curl &>/dev/null; then
    log_error "curl not found."
    PREREQ_OK=false
fi

if [[ "$PREREQ_OK" != "true" ]]; then
    log_error "Missing prerequisites. Fix the above and re-run."
    return 1 2>/dev/null || exit 1
fi

# Verify kubectl can reach the cluster
if ! kubectl cluster-info &>/dev/null; then
    log_error "kubectl cannot reach the cluster. Connect first:"
    echo "  nebius mk8s cluster get-credentials --id <cluster-id> --external"
    return 1 2>/dev/null || exit 1
fi

CLUSTER_CONTEXT=$(kubectl config current-context 2>/dev/null || echo "unknown")
log_info "Connected to cluster: ${CLUSTER_CONTEXT}"
echo ""

# =============================================================================
# Check 1: GPU Operator with driver
# =============================================================================
log_info "--- Check 1: GPU Operator & Driver ---"

# Check GPU Operator is deployed
if helm list -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" 2>/dev/null | grep -q gpu-operator; then
    check_pass "GPU Operator helm release found"
else
    check_fail "GPU Operator helm release NOT found in namespace ${GPU_OPERATOR_NAMESPACE:-gpu-operator}"
fi

# Check driver is enabled (not disabled via --set driver.enabled=false)
GPU_OPERATOR_VALUES=$(helm get values gpu-operator -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" -a -o json 2>/dev/null || echo '{}')
DRIVER_ENABLED=$(echo "$GPU_OPERATOR_VALUES" | jq -r '.driver.enabled // empty' 2>/dev/null || echo "unknown")
CONFIGURED_DRIVER_VERSION=$(echo "$GPU_OPERATOR_VALUES" | jq -r '.driver.version // empty' 2>/dev/null || echo "")

if [[ -z "$EXPECTED_DRIVER_VERSION" && -n "$CONFIGURED_DRIVER_VERSION" ]]; then
    EXPECTED_DRIVER_VERSION="$CONFIGURED_DRIVER_VERSION"
fi

if [[ "$DRIVER_ENABLED" == "true" ]]; then
    check_pass "GPU driver is enabled in GPU Operator"
elif [[ "$DRIVER_ENABLED" == "false" ]]; then
    check_fail "GPU driver is DISABLED (driver.enabled=false) — driverless images need the operator to manage the driver"
else
    check_warn "Could not determine if GPU driver is enabled"
fi

# Check driver version by running nvidia-smi inside a nvidia-driver-daemonset pod
DRIVER_POD=$(kubectl get pods -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" \
    -l app=nvidia-driver-daemonset --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [[ -z "$DRIVER_POD" ]]; then
    check_warn "No running nvidia-driver-daemonset pod found — cannot check driver version"
else
    DRIVER_NODE=$(kubectl get pod -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" "$DRIVER_POD" \
        -o jsonpath='{.spec.nodeName}' 2>/dev/null || echo "unknown")
    log_info "Running nvidia-smi in pod ${DRIVER_POD} (node ${DRIVER_NODE})..."

    NVIDIA_SMI_OUTPUT=$(kubectl exec -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" "$DRIVER_POD" -- \
        nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]' || echo "")

    if [[ -z "$NVIDIA_SMI_OUTPUT" ]]; then
        check_fail "Could not run nvidia-smi in pod ${DRIVER_POD}"
    elif [[ -n "$EXPECTED_DRIVER_VERSION" && "$NVIDIA_SMI_OUTPUT" == "$EXPECTED_DRIVER_VERSION" ]]; then
        check_pass "nvidia-smi driver version: ${NVIDIA_SMI_OUTPUT} (on ${DRIVER_NODE})"
    elif [[ -z "$EXPECTED_DRIVER_VERSION" ]]; then
        check_warn "nvidia-smi driver version: ${NVIDIA_SMI_OUTPUT} (no expected version configured)"
    else
        check_fail "nvidia-smi driver version: ${NVIDIA_SMI_OUTPUT} (expected ${EXPECTED_DRIVER_VERSION}, on ${DRIVER_NODE})"
    fi
fi

# Check nvidia-driver-daemonset pods are running
DRIVER_PODS=$(kubectl get pods -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" -l app=nvidia-driver-daemonset --no-headers 2>/dev/null | wc -l | tr -d ' ')
DRIVER_PODS_READY=$(kubectl get pods -n "${GPU_OPERATOR_NAMESPACE:-gpu-operator}" -l app=nvidia-driver-daemonset --no-headers 2>/dev/null | grep -c "Running" || echo "0")
if [[ "$DRIVER_PODS" -gt 0 ]]; then
    if [[ "$DRIVER_PODS_READY" -eq "$DRIVER_PODS" ]]; then
        check_pass "nvidia-driver-daemonset: ${DRIVER_PODS_READY}/${DRIVER_PODS} pods Running"
    else
        check_fail "nvidia-driver-daemonset: ${DRIVER_PODS_READY}/${DRIVER_PODS} pods Running"
    fi
else
    check_warn "No nvidia-driver-daemonset pods found (expected when driver.enabled=true)"
fi

# =============================================================================
# Check 2: /mnt/data mounted on a GPU node
# =============================================================================
echo ""
log_info "--- Check 2: /mnt/data on GPU node ---"

GPU_NODE=$(kubectl get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$GPU_NODE" ]]; then
    check_warn "No GPU nodes found — cannot verify /mnt/data"
else
    POD_NAME="mnt-check-verify"
    kubectl delete pod "$POD_NAME" --force --grace-period=0 &>/dev/null || true
    kubectl run "$POD_NAME" --image=busybox --restart=Never \
        --overrides="{
            \"spec\":{
                \"nodeName\":\"${GPU_NODE}\",
                \"containers\":[{
                    \"name\":\"check\",
                    \"image\":\"busybox\",
                    \"command\":[\"sh\",\"-c\",\"grep -q ' /host-mnt/data ' /host-proc/mounts && echo MOUNTED || echo NOT_MOUNTED\"],
                    \"volumeMounts\":[
                        {\"name\":\"host-proc\",\"mountPath\":\"/host-proc\",\"readOnly\":true},
                        {\"name\":\"host-mnt\",\"mountPath\":\"/host-mnt\",\"readOnly\":true}
                    ]
                }],
                \"volumes\":[
                    {\"name\":\"host-proc\",\"hostPath\":{\"path\":\"/proc\",\"type\":\"Directory\"}},
                    {\"name\":\"host-mnt\",\"hostPath\":{\"path\":\"/mnt\",\"type\":\"Directory\"}}
                ],
                \"tolerations\":[{\"operator\":\"Exists\"}],
                \"restartPolicy\":\"Never\"
            }
        }" &>/dev/null

    kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD_NAME" --timeout=30s &>/dev/null

    RESULT=$(kubectl logs "$POD_NAME" 2>/dev/null | tail -1)
    kubectl delete pod "$POD_NAME" --force --grace-period=0 &>/dev/null || true

    if [[ "$RESULT" == "MOUNTED" ]]; then
        check_pass "GPU node ${GPU_NODE}: /mnt/data mounted"
    elif [[ "$RESULT" == "NOT_MOUNTED" ]]; then
        check_fail "GPU node ${GPU_NODE}: /mnt/data NOT mounted"
    else
        check_warn "GPU node ${GPU_NODE}: could not verify /mnt/data"
    fi
fi

# =============================================================================
# Check 3–6 require OSMO API access via port-forward
# =============================================================================
echo ""
log_info "--- Setting up OSMO API access ---"

# Start port-forward for OSMO API checks
if ! start_osmo_api_session "${OSMO_NAMESPACE}" 8080 15; then
    log_error "Skipping OSMO API checks (3–6). Ensure OSMO is running in namespace '${OSMO_NAMESPACE}'."
    echo ""
    echo "========================================"
    echo "  Verification Summary (partial)"
    echo "========================================"
    echo ""
    echo -e "  ${GREEN}Passed:   ${PASS}${NC}"
    echo -e "  ${RED}Failed:   ${FAIL}${NC}"
    echo -e "  ${YELLOW}Warnings: ${WARN}${NC}"
    echo -e "  Skipped: checks 3–6 (OSMO API unreachable)"
    echo ""
    return 0 2>/dev/null || exit 0
fi
OSMO_URL="${OSMO_API_URL}"

cleanup_port_forward() {
    stop_port_forward
}
trap cleanup_port_forward EXIT RETURN

osmo_login "${OSMO_API_PORT}" || true

# =============================================================================
# Check 3: Shared memory pod template (64Gi /dev/shm)
# =============================================================================
echo ""
log_info "--- Check 3: Shared memory pod template ---"

SHM_TEMPLATE=$(osmo_curl GET "${OSMO_URL}/api/configs/pod_template/shm" 2>/dev/null || echo "")
if [[ -n "$SHM_TEMPLATE" && "$SHM_TEMPLATE" != "null" && "$SHM_TEMPLATE" != "{}" ]]; then
    # Check sizeLimit (API may return under .configs.spec or .spec)
    SHM_SIZE=$(echo "$SHM_TEMPLATE" | jq -r '(.configs.spec // .spec).volumes[]? | select(.name=="shm") | .emptyDir.sizeLimit // empty' 2>/dev/null || echo "")
    if [[ "$SHM_SIZE" == "$EXPECTED_SHM_SIZE" ]]; then
        check_pass "shm pod template: sizeLimit=${SHM_SIZE}"
    elif [[ -n "$SHM_SIZE" ]]; then
        check_fail "shm pod template: sizeLimit=${SHM_SIZE} (expected ${EXPECTED_SHM_SIZE})"
    else
        check_warn "shm pod template exists but could not read sizeLimit"
    fi

    # Check /dev/shm mount
    SHM_MOUNT=$(echo "$SHM_TEMPLATE" | jq -r '(.configs.spec // .spec).containers[]?.volumeMounts[]? | select(.mountPath=="/dev/shm") | .name // empty' 2>/dev/null || echo "")
    if [[ "$SHM_MOUNT" == "shm" ]]; then
        check_pass "shm pod template: /dev/shm volumeMount configured"
    else
        check_fail "shm pod template: /dev/shm volumeMount NOT found"
    fi
else
    check_fail "shm pod template NOT found in OSMO"
fi

# =============================================================================
# Check 4: Redis configuration (8 vCPU, ~52.82Gi mem, 50Gi PVC)
# =============================================================================
echo ""
log_info "--- Check 4: Redis resources ---"

REDIS_STS=$(kubectl get statefulset redis-master -n "${OSMO_NAMESPACE}" -o json 2>/dev/null || echo "")
if [[ -z "$REDIS_STS" || "$REDIS_STS" == "" ]]; then
    check_fail "Redis statefulset 'redis-master' not found in namespace ${OSMO_NAMESPACE}"
else
    # CPU requests
    REDIS_CPU=$(echo "$REDIS_STS" | jq -r '.spec.template.spec.containers[] | select(.name=="redis") | .resources.requests.cpu // empty' 2>/dev/null || echo "")
    REDIS_CPU_NUM=$(echo "$REDIS_CPU" | sed 's/m$//' || echo "0")
    if [[ "$REDIS_CPU" =~ m$ ]]; then
        REDIS_CPU_CORES=$((REDIS_CPU_NUM / 1000))
    else
        REDIS_CPU_CORES=$REDIS_CPU_NUM
    fi

    if [[ "$REDIS_CPU_CORES" -ge "$MIN_REDIS_CPU" ]] 2>/dev/null; then
        check_pass "Redis CPU requests: ${REDIS_CPU} (>= ${MIN_REDIS_CPU} cores)"
    else
        check_fail "Redis CPU requests: ${REDIS_CPU} (expected >= ${MIN_REDIS_CPU} cores)"
    fi

    # Memory requests
    REDIS_MEM=$(echo "$REDIS_STS" | jq -r '.spec.template.spec.containers[] | select(.name=="redis") | .resources.requests.memory // empty' 2>/dev/null || echo "")
    REDIS_MEM_NUM=$(echo "$REDIS_MEM" | sed -E 's/[A-Za-z]+$//')
    REDIS_MEM_UNIT=$(echo "$REDIS_MEM" | sed -E 's/^[0-9.]+//')
    case "$REDIS_MEM_UNIT" in
        Gi) REDIS_MEM_GI=$REDIS_MEM_NUM ;;
        Mi) REDIS_MEM_GI=$((REDIS_MEM_NUM / 1024)) ;;
        *) REDIS_MEM_GI=0 ;;
    esac

    if [[ "$REDIS_MEM_GI" -ge "$MIN_REDIS_MEMORY_GI" ]] 2>/dev/null; then
        check_pass "Redis memory requests: ${REDIS_MEM} (>= ${MIN_REDIS_MEMORY_GI}Gi)"
    else
        check_fail "Redis memory requests: ${REDIS_MEM} (expected >= ${MIN_REDIS_MEMORY_GI}Gi)"
    fi

    # PVC size
    REDIS_PVC_SIZE=$(kubectl get pvc -n "${OSMO_NAMESPACE}" -l app.kubernetes.io/name=redis --no-headers -o jsonpath='{.items[0].spec.resources.requests.storage}' 2>/dev/null || echo "")
    REDIS_PVC_NUM=$(echo "$REDIS_PVC_SIZE" | sed -E 's/[A-Za-z]+$//')
    REDIS_PVC_UNIT=$(echo "$REDIS_PVC_SIZE" | sed -E 's/^[0-9.]+//')
    case "$REDIS_PVC_UNIT" in
        Gi) REDIS_PVC_GI=$REDIS_PVC_NUM ;;
        Ti) REDIS_PVC_GI=$((REDIS_PVC_NUM * 1024)) ;;
        *) REDIS_PVC_GI=0 ;;
    esac

    if [[ "$REDIS_PVC_GI" -ge "$MIN_REDIS_PVC_GI" ]] 2>/dev/null; then
        check_pass "Redis PVC size: ${REDIS_PVC_SIZE} (>= ${MIN_REDIS_PVC_GI}Gi)"
    else
        check_fail "Redis PVC size: ${REDIS_PVC_SIZE:-unknown} (expected >= ${MIN_REDIS_PVC_GI}Gi)"
    fi
fi

# =============================================================================
# Check 5: WORKFLOW config
# =============================================================================
echo ""
log_info "--- Check 5: WORKFLOW config ---"

WORKFLOW_CONFIG=$(osmo_curl GET "${OSMO_URL}/api/configs/workflow" 2>/dev/null || echo "")
if [[ -n "$WORKFLOW_CONFIG" && "$WORKFLOW_CONFIG" != "null" ]]; then
    WORKFLOW_CFG=$(echo "$WORKFLOW_CONFIG" | jq '.configs_dict // .' 2>/dev/null || echo "")
    EXPECTED_STORAGE_OVERRIDE=$(get_tf_output "storage_bucket.endpoint" "${SCRIPT_DIR}/../001-iac" 2>/dev/null || echo "")
    EXPECTED_STORAGE_REGION=$(get_tf_output "storage_bucket.region" "${SCRIPT_DIR}/../001-iac" 2>/dev/null || echo "")

    if [[ -n "$EXPECTED_STORAGE_OVERRIDE" ]]; then
        EXPECTED_STORAGE_OVERRIDE=$(normalize_nebius_storage_endpoint "$EXPECTED_STORAGE_OVERRIDE")
    fi

    MAX_NUM_TASKS=$(echo "$WORKFLOW_CFG" | jq -r '.max_num_tasks // empty' 2>/dev/null || echo "")
    if [[ -z "$MAX_NUM_TASKS" ]]; then
        check_fail "max_num_tasks not set in WORKFLOW config (default is too low)"
    elif [[ "$MAX_NUM_TASKS" -ge "$MIN_MAX_NUM_TASKS" ]] 2>/dev/null; then
        check_pass "max_num_tasks: ${MAX_NUM_TASKS} (>= ${MIN_MAX_NUM_TASKS})"
    else
        check_fail "max_num_tasks: ${MAX_NUM_TASKS} (expected >= ${MIN_MAX_NUM_TASKS})"
    fi

    for STORAGE_KEY in workflow_data workflow_log; do
        STORAGE_ENDPOINT=$(echo "$WORKFLOW_CFG" | jq -r --arg key "$STORAGE_KEY" '.[$key].credential.endpoint // empty' 2>/dev/null || echo "")
        STORAGE_OVERRIDE_URL=$(echo "$WORKFLOW_CFG" | jq -r --arg key "$STORAGE_KEY" '.[$key].credential.override_url // empty' 2>/dev/null || echo "")
        STORAGE_REGION=$(echo "$WORKFLOW_CFG" | jq -r --arg key "$STORAGE_KEY" '.[$key].credential.region // empty' 2>/dev/null || echo "")
        STORAGE_ACCESS_KEY_ID=$(echo "$WORKFLOW_CFG" | jq -r --arg key "$STORAGE_KEY" '.[$key].credential.access_key_id // empty' 2>/dev/null || echo "")
        STORAGE_ACCESS_KEY=$(echo "$WORKFLOW_CFG" | jq -r --arg key "$STORAGE_KEY" '.[$key].credential.access_key // empty' 2>/dev/null || echo "")

        if [[ -z "$STORAGE_ENDPOINT" ]]; then
            check_fail "${STORAGE_KEY}: credential.endpoint missing"
        else
            check_pass "${STORAGE_KEY}: credential.endpoint set"
        fi

        if [[ -z "$STORAGE_OVERRIDE_URL" ]]; then
            check_fail "${STORAGE_KEY}: credential.override_url missing (Nebius Object Storage needs the HTTPS endpoint)"
        elif [[ -n "$EXPECTED_STORAGE_OVERRIDE" && "$STORAGE_OVERRIDE_URL" != "$EXPECTED_STORAGE_OVERRIDE" ]]; then
            check_fail "${STORAGE_KEY}: credential.override_url=${STORAGE_OVERRIDE_URL} (expected ${EXPECTED_STORAGE_OVERRIDE})"
        else
            check_pass "${STORAGE_KEY}: credential.override_url=${STORAGE_OVERRIDE_URL}"
        fi

        if [[ -z "$STORAGE_REGION" ]]; then
            check_fail "${STORAGE_KEY}: credential.region missing (workflow uploads will fail with Invalid region)"
        elif [[ -n "$EXPECTED_STORAGE_REGION" && "$STORAGE_REGION" != "$EXPECTED_STORAGE_REGION" ]]; then
            check_fail "${STORAGE_KEY}: credential.region=${STORAGE_REGION} (expected ${EXPECTED_STORAGE_REGION})"
        else
            check_pass "${STORAGE_KEY}: credential.region=${STORAGE_REGION}"
        fi

        if [[ -n "$STORAGE_ACCESS_KEY_ID" && -n "$STORAGE_ACCESS_KEY" ]]; then
            check_pass "${STORAGE_KEY}: inline access keys present"
        elif [[ -n "$STORAGE_ACCESS_KEY_ID" || -n "$STORAGE_ACCESS_KEY" ]]; then
            check_fail "${STORAGE_KEY}: inline access keys are only partially configured"
        else
            check_warn "${STORAGE_KEY}: inline access keys absent; this only works on OSMO builds that support env-backed workflow credentials"
        fi
    done

    for deploy in osmo-service osmo-worker osmo-logger osmo-agent; do
        DEPLOYMENT_JSON=$(kubectl get deployment "$deploy" -n "${OSMO_NAMESPACE}" -o json 2>/dev/null || echo "")
        if [[ -z "$DEPLOYMENT_JSON" ]]; then
            check_fail "Deployment ${deploy} not found"
            continue
        fi

        DEPLOY_ENV_NAMES=$(echo "$DEPLOYMENT_JSON" | jq -r '
            .spec.template.spec.containers[]
            | select(.name == "'"${deploy}"'")
            | .env[]?.name
        ' 2>/dev/null || echo "")

        if echo "$DEPLOY_ENV_NAMES" | grep -qx "AWS_ACCESS_KEY_ID" &&
           echo "$DEPLOY_ENV_NAMES" | grep -qx "AWS_SECRET_ACCESS_KEY" &&
           echo "$DEPLOY_ENV_NAMES" | grep -qx "AWS_ENDPOINT_URL_S3" &&
           echo "$DEPLOY_ENV_NAMES" | grep -qx "AWS_DEFAULT_REGION"; then
            check_pass "${deploy}: AWS storage env wiring present"
        else
            check_fail "${deploy}: missing AWS storage env wiring from osmo-storage"
        fi
    done

    VERIFY_BUCKET=$(get_tf_output "storage_bucket.name" "${SCRIPT_DIR}/../001-iac" 2>/dev/null || echo "")
    VERIFY_ENDPOINT=$(get_tf_output "storage_bucket.endpoint" "${SCRIPT_DIR}/../001-iac" 2>/dev/null || echo "")
    VERIFY_REGION=$(get_tf_output "storage_bucket.region" "${SCRIPT_DIR}/../001-iac" 2>/dev/null || echo "")
    if [[ -n "$VERIFY_ENDPOINT" ]]; then
        VERIFY_ENDPOINT=$(normalize_nebius_storage_endpoint "$VERIFY_ENDPOINT")
    fi

    if [[ -z "$VERIFY_BUCKET" || -z "$VERIFY_ENDPOINT" || -z "$VERIFY_REGION" ]]; then
        check_warn "Skipping live bucket probe (Terraform bucket name / endpoint / region missing)"
    elif probe_nebius_bucket_rw "${OSMO_NAMESPACE}" "${VERIFY_BUCKET}" "${VERIFY_ENDPOINT}" "${VERIFY_REGION}"; then
        check_pass "osmo-storage secret can read/write ${VERIFY_BUCKET}"
    else
        check_fail "osmo-storage secret cannot read/write ${VERIFY_BUCKET}"
    fi
else
    check_fail "Could not retrieve WORKFLOW config from OSMO API"
fi

# =============================================================================
# Check 6: Platform name is not default "gpu"
# =============================================================================
echo ""
log_info "--- Check 6: Platform naming ---"

POOL_CONFIG=$(osmo_curl GET "${OSMO_URL}/api/configs/pool/default" 2>/dev/null || echo "")
if [[ -n "$POOL_CONFIG" && "$POOL_CONFIG" != "null" ]]; then
    PLATFORM_NAMES=$(echo "$POOL_CONFIG" | jq -r '.platforms // {} | keys[]' 2>/dev/null || echo "")
    if [[ -z "$PLATFORM_NAMES" ]]; then
        check_fail "No platforms found in default pool"
    else
        GPU_TYPE_FOUND=false
        GENERIC_LIST=""
        for NAME in $PLATFORM_NAMES; do
            # Platform name must identify the GPU type (e.g. H100, H200, B200, L40S)
            if echo "$NAME" | grep -qiE '^(h100|h200|b200|b300|l40s|a100|a10)'; then
                check_pass "Platform '${NAME}': name identifies GPU type"
                GPU_TYPE_FOUND=true
            else
                GENERIC_LIST="${GENERIC_LIST} ${NAME}"
            fi
        done
        if [[ "$GPU_TYPE_FOUND" == "false" ]]; then
            check_fail "No GPU-type platform found (only generic:${GENERIC_LIST}) — create one named after the GPU (e.g. H100)"
        elif [[ -n "$GENERIC_LIST" ]]; then
            log_info "Also found generic platforms:${GENERIC_LIST} (cannot be deleted, ignored)"
        fi
    fi
else
    check_fail "Could not retrieve pool config from OSMO API"
fi

# =============================================================================
# Summary
# =============================================================================
cleanup_port_forward
trap - EXIT RETURN

echo ""
echo "========================================"
echo "  Verification Summary"
echo "========================================"
echo ""
echo -e "  ${GREEN}Passed:   ${PASS}${NC}"
echo -e "  ${RED}Failed:   ${FAIL}${NC}"
echo -e "  ${YELLOW}Warnings: ${WARN}${NC}"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    log_error "Installation has ${FAIL} issue(s) that need to be fixed."
elif [[ "$WARN" -gt 0 ]]; then
    log_warning "Installation looks OK but has ${WARN} warning(s) to review."
else
    log_success "All checks passed!"
fi
