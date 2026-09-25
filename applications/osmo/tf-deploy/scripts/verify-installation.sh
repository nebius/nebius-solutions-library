#!/bin/bash
#
# OSMO tf-deploy verification script
#
# Manual post-deploy verification for the Terraform-first workflow.
#
# Usage:
#   ./scripts/verify-installation.sh
#

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
TF_DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${TF_DEPLOY_DIR}/../../.." && pwd)"

cd "${REPO_ROOT}"

GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
NC=$'\033[0m'

PASS=0
FAIL=0
WARN=0

MIN_REDIS_CPU=8
MIN_REDIS_MEMORY_GI=50
MIN_REDIS_PVC_GI=50
EXPECTED_DRIVER_VERSION="${EXPECTED_DRIVER_VERSION:-}"

PORT_FORWARD_PID=""
PORT_FORWARD_LOG=""
OSMO_API_URL=""
OSMO_AUTH_BYPASS="false"

APP_OUTPUTS_JSON='{}'
INFRA_OUTPUTS_JSON='{}'

log_info() {
    printf '%s[INFO]%s %s\n' "${BLUE}" "${NC}" "$*"
}

log_success() {
    printf '%s[PASS]%s %s\n' "${GREEN}" "${NC}" "$*"
}

log_warn() {
    printf '%s[WARN]%s %s\n' "${YELLOW}" "${NC}" "$*" >&2
}

log_error() {
    printf '%s[FAIL]%s %s\n' "${RED}" "${NC}" "$*" >&2
}

check_pass() {
    PASS=$((PASS + 1))
    log_success "$1"
}

check_fail() {
    FAIL=$((FAIL + 1))
    log_error "$1"
}

check_warn() {
    WARN=$((WARN + 1))
    log_warn "$1"
}

die() {
    log_error "$*"
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

cleanup() {
    if [[ -n "${PORT_FORWARD_PID}" ]] && kill -0 "${PORT_FORWARD_PID}" >/dev/null 2>&1; then
        kill "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
        wait "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
    fi

    if [[ -n "${PORT_FORWARD_LOG}" && -f "${PORT_FORWARD_LOG}" ]]; then
        rm -f "${PORT_FORWARD_LOG}"
    fi
}

trap cleanup EXIT

tf_output_json() {
    local dir="$1"
    terraform -chdir="${dir}" output -json 2>/dev/null || printf '{}'
}

tf_output_value() {
    local json="$1"
    local key="$2"

    printf '%s' "${json}" | jq -r --arg key "${key}" '.[$key].value // empty' 2>/dev/null || true
}

kubectl_cmd() {
    if [[ -n "${KUBECTL_CONTEXT:-}" ]]; then
        kubectl --context "${KUBECTL_CONTEXT}" "$@"
    else
        kubectl "$@"
    fi
}

helm_cmd() {
    helm "$@"
}

load_local_context() {
    if [[ -n "${KUBECONFIG:-}" ]]; then
        return 0
    fi

    if kubectl get ns >/dev/null 2>&1; then
        return 0
    fi

    if [[ -f "${TF_DEPLOY_DIR}/cluster-access.env" ]]; then
        # shellcheck disable=SC1091
        source "${TF_DEPLOY_DIR}/cluster-access.env"
    elif [[ -z "${KUBECONFIG:-}" && -f "${TF_DEPLOY_DIR}/generated/kubeconfig" ]]; then
        export KUBECONFIG="${TF_DEPLOY_DIR}/generated/kubeconfig"
    fi
}

load_outputs() {
    APP_OUTPUTS_JSON="$(tf_output_json "${TF_DEPLOY_DIR}")"
    INFRA_OUTPUTS_JSON="$(tf_output_json "${TF_DEPLOY_DIR}/infra")"
}

find_free_port() {
    local port="${1:-18080}"

    while :; do
        if ! lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
            printf '%s\n' "${port}"
            return 0
        fi
        port=$((port + 1))
    done
}

start_osmo_api_session() {
    local namespace="$1"
    local pod_name=""
    local port=""
    local use_pod="false"

    pod_name="$(kubectl_cmd get pod -n "${namespace}" -l app=osmo-service --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    [[ -n "${pod_name}" ]] || die "Could not find a running osmo-service pod"

    if kubectl_cmd get pod -n "${namespace}" "${pod_name}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null | grep -q 'envoy'; then
        use_pod="true"
        OSMO_AUTH_BYPASS="true"
    fi

    port="$(find_free_port 18080)"
    PORT_FORWARD_LOG="$(mktemp "${TMPDIR:-/tmp}/osmo-verify-pf.XXXXXX.log")"

    if [[ "${use_pod}" == "true" ]]; then
        kubectl_cmd -n "${namespace}" port-forward "pod/${pod_name}" "${port}:8000" >"${PORT_FORWARD_LOG}" 2>&1 &
    else
        kubectl_cmd -n "${namespace}" port-forward "svc/osmo-service" "${port}:80" >"${PORT_FORWARD_LOG}" 2>&1 &
    fi
    PORT_FORWARD_PID=$!

    for _ in $(seq 1 60); do
        if ! kill -0 "${PORT_FORWARD_PID}" >/dev/null 2>&1; then
            cat "${PORT_FORWARD_LOG}" >&2 || true
            die "Failed to start OSMO API port-forward"
        fi
        if grep -q "Forwarding from" "${PORT_FORWARD_LOG}" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done

    OSMO_API_URL="http://127.0.0.1:${port}"
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 5 "${OSMO_API_URL}/api/version" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    cat "${PORT_FORWARD_LOG}" >&2 || true
    die "OSMO API did not become reachable at ${OSMO_API_URL}"
}

osmo_curl() {
    local method="$1"
    local path="$2"
    shift 2

    local headers=()
    if [[ "${OSMO_AUTH_BYPASS}" == "true" ]]; then
        headers+=(-H "x-osmo-user: osmo-admin" -H "x-osmo-roles: osmo-admin,osmo-user")
    fi

    curl -sS --max-time 30 \
        -X "${method}" \
        -H "Content-Type: application/json" \
        "${headers[@]}" \
        "${OSMO_API_URL}${path}" \
        "$@"
}

normalize_storage_endpoint() {
    local endpoint="$1"

    endpoint="${endpoint%/}"
    if [[ "${endpoint}" =~ ^https://[^/:]+$ ]]; then
        printf '%s:443\n' "${endpoint}"
    else
        printf '%s\n' "${endpoint}"
    fi
}

probe_bucket_rw() {
    local namespace="$1"
    local bucket="$2"
    local endpoint="$3"
    local region="$4"
    local access_key_id="$5"
    local secret_access_key="$6"
    local probe_key=""
    local run_name=""
    local probe_script=""
    local output=""

    probe_key="osmo-verify-$(date +%s)-${RANDOM}"
    run_name="osmo-verify-s3-${RANDOM}${RANDOM}"
    probe_script=$(cat <<EOF
set -e
aws --endpoint-url "${endpoint}" s3api head-bucket --bucket "${bucket}" >/dev/null
aws --endpoint-url "${endpoint}" s3api put-object --bucket "${bucket}" --key "${probe_key}" --body /etc/hosts >/dev/null
aws --endpoint-url "${endpoint}" s3api delete-object --bucket "${bucket}" --key "${probe_key}" >/dev/null
echo S3_VERIFY_OK
EOF
)

    if output="$(kubectl_cmd run "${run_name}" \
        --rm --restart=Never -i \
        -n "${namespace}" \
        --image=amazon/aws-cli:2.15.0 \
        --env="AWS_ACCESS_KEY_ID=${access_key_id}" \
        --env="AWS_SECRET_ACCESS_KEY=${secret_access_key}" \
        --env="AWS_DEFAULT_REGION=${region}" \
        --env="AWS_EC2_METADATA_DISABLED=true" \
        --command -- sh -lc "${probe_script}" 2>&1)"; then
        grep -q "S3_VERIFY_OK" <<<"${output}"
        return $?
    fi

    return 1
}

check_http_reachable() {
    local url="$1"
    local description="$2"
    local code=""

    code="$(curl -skI -o /dev/null -w '%{http_code}' --max-time 20 "${url}" 2>/dev/null || true)"
    if [[ "${code}" =~ ^2|^3 ]]; then
        check_pass "${description}: HTTP ${code}"
    else
        check_fail "${description}: HTTP ${code:-unreachable}"
    fi
}

check_rollout() {
    local namespace="$1"
    local kind="$2"
    local name="$3"

    if ! kubectl_cmd get "${kind}" "${name}" -n "${namespace}" >/dev/null 2>&1; then
        check_fail "${kind}/${name} not found in namespace ${namespace}"
        return 0
    fi

    if [[ "${kind}" == "deployment" ]]; then
        if kubectl_cmd rollout status "deployment/${name}" -n "${namespace}" --timeout=10s >/dev/null 2>&1; then
            check_pass "deployment/${name} is ready"
        else
            check_fail "deployment/${name} is not ready"
        fi
        return 0
    fi

    local ready replicas
    ready="$(kubectl_cmd get "${kind}" "${name}" -n "${namespace}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    replicas="$(kubectl_cmd get "${kind}" "${name}" -n "${namespace}" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
    if [[ -n "${replicas}" && "${ready:-0}" == "${replicas}" ]]; then
        check_pass "${kind}/${name} is ready (${ready}/${replicas})"
    else
        check_fail "${kind}/${name} is not ready (${ready:-0}/${replicas:-0})"
    fi
}

echo ""
echo "========================================"
echo "  OSMO tf-deploy Verification"
echo "========================================"
echo ""

require_command kubectl
require_command helm
require_command jq
require_command curl
require_command lsof
require_command terraform

load_local_context
load_outputs

OSMO_NAMESPACE="${OSMO_NAMESPACE:-$(tf_output_value "${APP_OUTPUTS_JSON}" namespace)}"
MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-$(tf_output_value "${APP_OUTPUTS_JSON}" monitoring_namespace)}"
BACKEND_OPERATOR_NAMESPACE="${BACKEND_OPERATOR_NAMESPACE:-$(tf_output_value "${APP_OUTPUTS_JSON}" backend_operator_namespace)}"
WORKFLOWS_NAMESPACE="${WORKFLOWS_NAMESPACE:-$(tf_output_value "${APP_OUTPUTS_JSON}" workflows_namespace)}"
BACKEND_NAME="${BACKEND_NAME:-$(tf_output_value "${APP_OUTPUTS_JSON}" backend_name)}"
INGRESS_HOSTNAME="${INGRESS_HOSTNAME:-$(tf_output_value "${APP_OUTPUTS_JSON}" ingress_hostname)}"
AUTH_DOMAIN="${AUTH_DOMAIN:-$(tf_output_value "${APP_OUTPUTS_JSON}" auth_domain)}"
EXPECTED_SERVICE_BASE_URL="${EXPECTED_SERVICE_BASE_URL:-$(tf_output_value "${APP_OUTPUTS_JSON}" service_base_url)}"
STORAGE_BUCKET_NAME="${STORAGE_BUCKET_NAME:-$(printf '%s' "${INFRA_OUTPUTS_JSON}" | jq -r '.storage_bucket.value.name // empty' 2>/dev/null || true)}"
STORAGE_ENDPOINT="${STORAGE_ENDPOINT:-$(printf '%s' "${INFRA_OUTPUTS_JSON}" | jq -r '.storage_bucket.value.endpoint // empty' 2>/dev/null || true)}"
NEBIUS_REGION="${NEBIUS_REGION:-$(printf '%s' "${INFRA_OUTPUTS_JSON}" | jq -r '.region.value // empty' 2>/dev/null || true)}"
EXPECTED_GPU_PLATFORM="${EXPECTED_GPU_PLATFORM:-$(printf '%s' "${INFRA_OUTPUTS_JSON}" | jq -r '.gpu_nodes_platform.value // empty' 2>/dev/null | sed -E 's/^gpu-([^-]+).*/\1/' | tr '[:lower:]' '[:upper:]')}"
GPU_OPERATOR_NAMESPACE="${GPU_OPERATOR_NAMESPACE:-gpu-operator}"
KAI_SCHEDULER_NAMESPACE="${KAI_SCHEDULER_NAMESPACE:-kai-scheduler}"

: "${OSMO_NAMESPACE:=osmo}"
: "${MONITORING_NAMESPACE:=monitoring}"
: "${BACKEND_OPERATOR_NAMESPACE:=osmo-operator}"
: "${WORKFLOWS_NAMESPACE:=osmo-workflows}"
: "${BACKEND_NAME:=default}"

log_info "Checking kubectl connectivity..."
if kubectl_cmd get ns >/dev/null 2>&1; then
    check_pass "kubectl can reach the cluster"
else
    die "kubectl cannot reach the cluster"
fi

echo ""
log_info "--- Core namespaces and workloads ---"
for ns in "${OSMO_NAMESPACE}" "${WORKFLOWS_NAMESPACE}"; do
    if kubectl_cmd get ns "${ns}" >/dev/null 2>&1; then
        check_pass "namespace/${ns} exists"
    else
        check_fail "namespace/${ns} missing"
    fi
done

check_rollout "${OSMO_NAMESPACE}" deployment osmo-service
check_rollout "${OSMO_NAMESPACE}" deployment osmo-router
check_rollout "${OSMO_NAMESPACE}" deployment osmo-ui
check_rollout "${OSMO_NAMESPACE}" deployment osmo-worker
check_rollout "${OSMO_NAMESPACE}" deployment osmo-agent
check_rollout "${OSMO_NAMESPACE}" deployment osmo-logger
check_rollout "${OSMO_NAMESPACE}" statefulset redis-master
check_rollout "${OSMO_NAMESPACE}" statefulset keycloak
check_rollout "${OSMO_NAMESPACE}" statefulset keycloak-postgresql

echo ""
log_info "--- Ingress and public endpoints ---"
if kubectl_cmd get ingress -n "${OSMO_NAMESPACE}" osmo-service >/dev/null 2>&1; then
    check_pass "ingress/osmo-service exists"
else
    check_fail "ingress/osmo-service missing"
fi
if kubectl_cmd get ingress -n "${OSMO_NAMESPACE}" keycloak >/dev/null 2>&1; then
    check_pass "ingress/keycloak exists"
else
    check_fail "ingress/keycloak missing"
fi
if [[ -n "${INGRESS_HOSTNAME}" ]]; then
    check_http_reachable "https://${INGRESS_HOSTNAME}" "OSMO ingress"
else
    check_warn "INGRESS_HOSTNAME not available; skipping external OSMO probe"
fi
if [[ -n "${AUTH_DOMAIN}" ]]; then
    check_http_reachable "https://${AUTH_DOMAIN}" "Keycloak ingress"
else
    check_warn "AUTH_DOMAIN not available; skipping external Keycloak probe"
fi

echo ""
log_info "--- Observability and backend operator ---"
if kubectl_cmd get ns "${MONITORING_NAMESPACE}" >/dev/null 2>&1; then
    if helm_cmd status prometheus -n "${MONITORING_NAMESPACE}" >/dev/null 2>&1; then
        check_pass "helm release/prometheus is deployed"
    else
        check_warn "prometheus helm release not found in ${MONITORING_NAMESPACE}"
    fi
    if helm_cmd status loki -n "${MONITORING_NAMESPACE}" >/dev/null 2>&1; then
        check_pass "helm release/loki is deployed"
    else
        check_warn "loki helm release not found in ${MONITORING_NAMESPACE}"
    fi
    check_rollout "${MONITORING_NAMESPACE}" deployment prometheus-grafana
    check_rollout "${MONITORING_NAMESPACE}" deployment prometheus-kube-prometheus-operator
else
    check_warn "namespace/${MONITORING_NAMESPACE} not found; observability appears disabled"
fi

if kubectl_cmd get ns "${BACKEND_OPERATOR_NAMESPACE}" >/dev/null 2>&1; then
    check_rollout "${BACKEND_OPERATOR_NAMESPACE}" deployment osmo-operator-osmo-backend-listener
    check_rollout "${BACKEND_OPERATOR_NAMESPACE}" deployment osmo-operator-osmo-backend-worker
else
    check_warn "namespace/${BACKEND_OPERATOR_NAMESPACE} not found; backend operator appears disabled"
fi

echo ""
log_info "--- GPU infrastructure ---"
GPU_NODE="$(kubectl_cmd get nodes -l nvidia.com/gpu.present=true -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${GPU_NODE}" ]]; then
    check_warn "No GPU nodes detected; skipping GPU-specific checks"
else
    check_pass "GPU node detected: ${GPU_NODE}"

    if helm_cmd status gpu-operator -n "${GPU_OPERATOR_NAMESPACE}" >/dev/null 2>&1; then
        check_pass "helm release/gpu-operator is deployed"
    else
        check_fail "helm release/gpu-operator missing"
    fi

    GPU_OPERATOR_VALUES="$(helm_cmd get values gpu-operator -n "${GPU_OPERATOR_NAMESPACE}" -a -o json 2>/dev/null || printf '{}')"
    DRIVER_ENABLED="$(printf '%s' "${GPU_OPERATOR_VALUES}" | jq -r '.driver.enabled // empty' 2>/dev/null || true)"
    CONFIGURED_DRIVER_VERSION="$(printf '%s' "${GPU_OPERATOR_VALUES}" | jq -r '.driver.version // empty' 2>/dev/null || true)"

    if [[ -z "${EXPECTED_DRIVER_VERSION}" && -n "${CONFIGURED_DRIVER_VERSION}" ]]; then
        EXPECTED_DRIVER_VERSION="${CONFIGURED_DRIVER_VERSION}"
    fi

    if [[ "${DRIVER_ENABLED}" == "true" ]]; then
        check_pass "GPU driver is enabled in GPU Operator"
    elif [[ "${DRIVER_ENABLED}" == "false" ]]; then
        check_warn "GPU driver is disabled in GPU Operator; assuming driver-full node images"
    else
        check_warn "Could not determine if GPU driver is enabled in GPU Operator"
    fi

    DRIVER_POD="$(kubectl_cmd get pods -n "${GPU_OPERATOR_NAMESPACE}" -l app=nvidia-driver-daemonset --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    DRIVER_PODS="$(kubectl_cmd get pods -n "${GPU_OPERATOR_NAMESPACE}" -l app=nvidia-driver-daemonset --no-headers 2>/dev/null | wc -l | tr -d ' ' || true)"
    DRIVER_PODS_READY="$(kubectl_cmd get pods -n "${GPU_OPERATOR_NAMESPACE}" -l app=nvidia-driver-daemonset --no-headers 2>/dev/null | grep -c "Running" || true)"

    if [[ "${DRIVER_PODS:-0}" -gt 0 ]] 2>/dev/null; then
        if [[ "${DRIVER_PODS_READY:-0}" -eq "${DRIVER_PODS}" ]]; then
            check_pass "nvidia-driver-daemonset: ${DRIVER_PODS_READY}/${DRIVER_PODS} pods Running"
        else
            check_fail "nvidia-driver-daemonset: ${DRIVER_PODS_READY:-0}/${DRIVER_PODS} pods Running"
        fi
    elif [[ "${DRIVER_ENABLED}" == "true" ]]; then
        check_fail "No nvidia-driver-daemonset pods found while driver.enabled=true"
    else
        check_warn "No nvidia-driver-daemonset pods found"
    fi

    if [[ -n "${DRIVER_POD}" ]]; then
        DRIVER_NODE="$(kubectl_cmd get pod -n "${GPU_OPERATOR_NAMESPACE}" "${DRIVER_POD}" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true)"
        NVIDIA_SMI_OUTPUT="$(kubectl_cmd exec -n "${GPU_OPERATOR_NAMESPACE}" "${DRIVER_POD}" -- nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]' || true)"

        if [[ -z "${NVIDIA_SMI_OUTPUT}" ]]; then
            check_fail "Could not run nvidia-smi in ${DRIVER_POD}"
        elif [[ -n "${EXPECTED_DRIVER_VERSION}" && "${NVIDIA_SMI_OUTPUT}" == "${EXPECTED_DRIVER_VERSION}" ]]; then
            check_pass "nvidia-smi driver version=${NVIDIA_SMI_OUTPUT} (node ${DRIVER_NODE})"
        elif [[ -z "${EXPECTED_DRIVER_VERSION}" ]]; then
            check_warn "nvidia-smi driver version=${NVIDIA_SMI_OUTPUT} (no expected version configured)"
        else
            check_fail "nvidia-smi driver version=${NVIDIA_SMI_OUTPUT} (expected ${EXPECTED_DRIVER_VERSION}, node ${DRIVER_NODE})"
        fi
    elif [[ "${DRIVER_ENABLED}" == "true" ]]; then
        check_warn "Skipping nvidia-smi driver version check because no running nvidia-driver-daemonset pod was found"
    fi

    if helm_cmd status kai-scheduler -n "${KAI_SCHEDULER_NAMESPACE}" >/dev/null 2>&1; then
        check_pass "helm release/kai-scheduler is deployed"
    else
        check_fail "helm release/kai-scheduler missing"
    fi

    POD_NAME="mnt-check-verify"
    kubectl_cmd delete pod "${POD_NAME}" --force --grace-period=0 >/dev/null 2>&1 || true
    kubectl_cmd run "${POD_NAME}" --image=busybox --restart=Never \
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
        }" >/dev/null 2>&1 || true
    kubectl_cmd wait --for=jsonpath='{.status.phase}'=Succeeded "pod/${POD_NAME}" --timeout=30s >/dev/null 2>&1 || true
    MNT_RESULT="$(kubectl_cmd logs "${POD_NAME}" 2>/dev/null | tail -1 || true)"
    kubectl_cmd delete pod "${POD_NAME}" --force --grace-period=0 >/dev/null 2>&1 || true
    if [[ "${MNT_RESULT}" == "MOUNTED" ]]; then
        check_pass "GPU node ${GPU_NODE}: /mnt/data mounted"
    else
        check_fail "GPU node ${GPU_NODE}: /mnt/data not mounted"
    fi
fi

echo ""
log_info "--- OSMO API configuration ---"
start_osmo_api_session "${OSMO_NAMESPACE}"
check_pass "OSMO API reachable at ${OSMO_API_URL}"

SERVICE_CONFIG="$(osmo_curl GET "/api/configs/service" 2>/dev/null || true)"
SERVICE_BASE_URL="$(printf '%s' "${SERVICE_CONFIG}" | jq -r '.service_base_url // .configs_dict.service_base_url // empty' 2>/dev/null || true)"
if [[ -n "${EXPECTED_SERVICE_BASE_URL}" && "${SERVICE_BASE_URL}" == "${EXPECTED_SERVICE_BASE_URL}" ]]; then
    check_pass "SERVICE.service_base_url=${SERVICE_BASE_URL}"
elif [[ -n "${SERVICE_BASE_URL}" ]]; then
    check_fail "SERVICE.service_base_url=${SERVICE_BASE_URL} (expected ${EXPECTED_SERVICE_BASE_URL:-<unknown>})"
else
    check_fail "SERVICE.service_base_url not found"
fi

WORKFLOW_CONFIG="$(osmo_curl GET "/api/configs/workflow" 2>/dev/null || true)"
WORKFLOW_CFG="$(printf '%s' "${WORKFLOW_CONFIG}" | jq '.configs_dict // .' 2>/dev/null || printf '{}')"
MAX_NUM_TASKS="$(printf '%s' "${WORKFLOW_CFG}" | jq -r '.max_num_tasks // empty' 2>/dev/null || true)"
if [[ -n "${MAX_NUM_TASKS}" && "${MAX_NUM_TASKS}" -ge 200 ]] 2>/dev/null; then
    check_pass "WORKFLOW.max_num_tasks=${MAX_NUM_TASKS}"
else
    check_fail "WORKFLOW.max_num_tasks=${MAX_NUM_TASKS:-unset} (expected >= 200)"
fi

for storage_key in workflow_data workflow_log; do
    storage_endpoint="$(printf '%s' "${WORKFLOW_CFG}" | jq -r --arg key "${storage_key}" '.[$key].credential.override_url // empty' 2>/dev/null || true)"
    storage_region="$(printf '%s' "${WORKFLOW_CFG}" | jq -r --arg key "${storage_key}" '.[$key].credential.region // empty' 2>/dev/null || true)"
    if [[ -n "${storage_endpoint}" ]]; then
        check_pass "${storage_key}: credential.override_url=${storage_endpoint}"
    else
        check_fail "${storage_key}: credential.override_url missing"
    fi
    if [[ -n "${storage_region}" ]]; then
        check_pass "${storage_key}: credential.region=${storage_region}"
    else
        check_fail "${storage_key}: credential.region missing"
    fi
done

DATASET_CONFIG="$(osmo_curl GET "/api/configs/dataset" 2>/dev/null || true)"
DEFAULT_BUCKET="$(printf '%s' "${DATASET_CONFIG}" | jq -r '.configs_dict.default_bucket // .default_bucket // empty' 2>/dev/null || true)"
if [[ -n "${DEFAULT_BUCKET}" ]]; then
    check_pass "DATASET.default_bucket=${DEFAULT_BUCKET}"
else
    check_fail "DATASET.default_bucket not configured"
fi

BACKEND_CONFIG="$(osmo_curl GET "/api/configs/backend" 2>/dev/null || true)"
BACKEND_OBJECT="$(printf '%s' "${BACKEND_CONFIG}" | jq -c --arg name "${BACKEND_NAME}" '.backends[]? | select(.name == $name)' 2>/dev/null || true)"
if [[ -n "${BACKEND_OBJECT}" && "${BACKEND_OBJECT}" != "null" ]]; then
    check_pass "Backend '${BACKEND_NAME}' is registered"
else
    check_fail "Backend '${BACKEND_NAME}' is not registered"
fi

if [[ -n "${GPU_NODE}" ]]; then
    SHM_TEMPLATE="$(osmo_curl GET "/api/configs/pod_template/shm" 2>/dev/null || true)"
    SHM_SIZE="$(printf '%s' "${SHM_TEMPLATE}" | jq -r '(.configs.spec // .spec).volumes[]? | select(.name=="shm") | .emptyDir.sizeLimit // empty' 2>/dev/null || true)"
    if [[ "${SHM_SIZE}" == "64Gi" ]]; then
        check_pass "shm pod template sizeLimit=64Gi"
    else
        check_fail "shm pod template sizeLimit=${SHM_SIZE:-unset} (expected 64Gi)"
    fi

    GPU_TEMPLATE="$(osmo_curl GET "/api/configs/pod_template/gpu_tolerations" 2>/dev/null || true)"
    GPU_SELECTOR="$(printf '%s' "${GPU_TEMPLATE}" | jq -r '(.configs.spec // .spec).nodeSelector["nvidia.com/gpu.present"] // empty' 2>/dev/null || true)"
    if [[ "${GPU_SELECTOR}" == "true" ]]; then
        check_pass "gpu_tolerations pod template selects GPU nodes"
    else
        check_fail "gpu_tolerations pod template missing GPU node selector"
    fi

    POOL_CONFIG="$(osmo_curl GET "/api/configs/pool/default" 2>/dev/null || true)"
    PLATFORM_NAMES="$(printf '%s' "${POOL_CONFIG}" | jq -r '.platforms // {} | keys[]' 2>/dev/null || true)"
    if [[ -n "${EXPECTED_GPU_PLATFORM}" ]]; then
        if printf '%s\n' "${PLATFORM_NAMES}" | grep -qx "${EXPECTED_GPU_PLATFORM}"; then
            check_pass "Default pool contains platform ${EXPECTED_GPU_PLATFORM}"
        else
            check_fail "Default pool is missing expected platform ${EXPECTED_GPU_PLATFORM}"
        fi
    elif [[ -n "${PLATFORM_NAMES}" ]]; then
        check_pass "Default pool has platform entries: $(printf '%s\n' "${PLATFORM_NAMES}" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    else
        check_fail "Default pool has no platform entries"
    fi
fi

echo ""
log_info "--- Redis and storage runtime checks ---"
REDIS_STS="$(kubectl_cmd get statefulset redis-master -n "${OSMO_NAMESPACE}" -o json 2>/dev/null || true)"
REDIS_CPU="$(printf '%s' "${REDIS_STS}" | jq -r '.spec.template.spec.containers[] | select(.name=="redis") | .resources.requests.cpu // empty' 2>/dev/null || true)"
REDIS_MEM="$(printf '%s' "${REDIS_STS}" | jq -r '.spec.template.spec.containers[] | select(.name=="redis") | .resources.requests.memory // empty' 2>/dev/null || true)"
REDIS_PVC="$(kubectl_cmd get pvc -n "${OSMO_NAMESPACE}" -l app.kubernetes.io/name=redis --no-headers -o jsonpath='{.items[0].spec.resources.requests.storage}' 2>/dev/null || true)"
REDIS_CPU_NUM="$(printf '%s' "${REDIS_CPU}" | sed 's/m$//')"
if [[ "${REDIS_CPU}" =~ m$ ]]; then
    REDIS_CPU_CORES=$((REDIS_CPU_NUM / 1000))
else
    REDIS_CPU_CORES="${REDIS_CPU_NUM:-0}"
fi

REDIS_MEM_NUM="$(printf '%s' "${REDIS_MEM}" | sed -E 's/[A-Za-z]+$//')"
REDIS_MEM_UNIT="$(printf '%s' "${REDIS_MEM}" | sed -E 's/^[0-9.]+//')"
case "${REDIS_MEM_UNIT}" in
    Gi) REDIS_MEM_GI="${REDIS_MEM_NUM}" ;;
    Mi) REDIS_MEM_GI=$((REDIS_MEM_NUM / 1024)) ;;
    *) REDIS_MEM_GI=0 ;;
esac

REDIS_PVC_NUM="$(printf '%s' "${REDIS_PVC}" | sed -E 's/[A-Za-z]+$//')"
REDIS_PVC_UNIT="$(printf '%s' "${REDIS_PVC}" | sed -E 's/^[0-9.]+//')"
case "${REDIS_PVC_UNIT}" in
    Gi) REDIS_PVC_GI="${REDIS_PVC_NUM}" ;;
    Ti) REDIS_PVC_GI=$((REDIS_PVC_NUM * 1024)) ;;
    *) REDIS_PVC_GI=0 ;;
esac

if [[ -n "${REDIS_CPU}" && "${REDIS_CPU_CORES}" -ge "${MIN_REDIS_CPU}" ]] 2>/dev/null; then
    check_pass "Redis CPU request=${REDIS_CPU} (>= ${MIN_REDIS_CPU} cores)"
else
    check_fail "Redis CPU request=${REDIS_CPU:-unset} (expected >= ${MIN_REDIS_CPU} cores)"
fi

if [[ -n "${REDIS_MEM}" && "${REDIS_MEM_GI}" -ge "${MIN_REDIS_MEMORY_GI}" ]] 2>/dev/null; then
    check_pass "Redis memory request=${REDIS_MEM} (>= ${MIN_REDIS_MEMORY_GI}Gi)"
else
    check_fail "Redis memory request=${REDIS_MEM:-unset} (expected >= ${MIN_REDIS_MEMORY_GI}Gi)"
fi

if [[ -n "${REDIS_PVC}" && "${REDIS_PVC_GI}" -ge "${MIN_REDIS_PVC_GI}" ]] 2>/dev/null; then
    check_pass "Redis PVC request=${REDIS_PVC} (>= ${MIN_REDIS_PVC_GI}Gi)"
else
    check_fail "Redis PVC request=${REDIS_PVC:-unset} (expected >= ${MIN_REDIS_PVC_GI}Gi)"
fi

STORAGE_SECRET_JSON="$(kubectl_cmd get secret osmo-storage -n "${OSMO_NAMESPACE}" -o json 2>/dev/null || true)"
STORAGE_ACCESS_KEY_ID="$(printf '%s' "${STORAGE_SECRET_JSON}" | jq -r '.data["access-key-id"] // empty' 2>/dev/null | base64 -d 2>/dev/null || true)"
STORAGE_SECRET_ACCESS_KEY="$(printf '%s' "${STORAGE_SECRET_JSON}" | jq -r '.data["secret-access-key"] // empty' 2>/dev/null | base64 -d 2>/dev/null || true)"
if [[ -n "${STORAGE_BUCKET_NAME}" && -n "${STORAGE_ENDPOINT}" && -n "${NEBIUS_REGION}" && -n "${STORAGE_ACCESS_KEY_ID}" && -n "${STORAGE_SECRET_ACCESS_KEY}" ]]; then
    if probe_bucket_rw "${OSMO_NAMESPACE}" "${STORAGE_BUCKET_NAME}" "$(normalize_storage_endpoint "${STORAGE_ENDPOINT}")" "${NEBIUS_REGION}" "${STORAGE_ACCESS_KEY_ID}" "${STORAGE_SECRET_ACCESS_KEY}"; then
        check_pass "osmo-storage secret can read/write ${STORAGE_BUCKET_NAME}"
    else
        check_fail "osmo-storage secret cannot read/write ${STORAGE_BUCKET_NAME}"
    fi
else
    check_warn "Skipping live bucket probe (bucket, endpoint, region, or osmo-storage secret missing)"
fi

echo ""
echo "========================================"
echo "  Verification Summary"
echo "========================================"
echo ""
printf '  %sPassed:%s   %s\n' "${GREEN}" "${NC}" "${PASS}"
printf '  %sFailed:%s   %s\n' "${RED}" "${NC}" "${FAIL}"
printf '  %sWarnings:%s %s\n' "${YELLOW}" "${NC}" "${WARN}"
echo ""

if [[ "${FAIL}" -gt 0 ]]; then
    log_error "Verification failed with ${FAIL} issue(s)."
    exit 1
fi

if [[ "${WARN}" -gt 0 ]]; then
    log_warn "Verification passed with ${WARN} warning(s)."
else
    log_success "All verification checks passed."
fi

echo ""
echo "Optional smoke tests:"
echo "  osmo workflow submit ${TF_DEPLOY_DIR}/../workflows/osmo/hello_nebius.yaml"
echo "  osmo workflow submit ${TF_DEPLOY_DIR}/../workflows/osmo/gpu_test.yaml"
