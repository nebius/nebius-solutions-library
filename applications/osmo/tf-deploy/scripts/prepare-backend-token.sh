#!/bin/bash
#
# Ensure the backend operator token secret exists for auth-disabled installs.
#

set -euo pipefail

PORT_FORWARD_PID=""
PORT_FORWARD_LOG=""
PORT_FORWARD_PORT=""
OSMO_API_URL=""

log() {
    printf '[INFO] %s\n' "$*"
}

warn() {
    printf '[WARN] %s\n' "$*" >&2
}

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

make_temp_file() {
    local prefix="$1"
    local suffix="${2:-}"
    local tmp_dir="${TMPDIR:-/tmp}"
    local tmp_path=""

    tmp_dir="${tmp_dir%/}"
    tmp_path="$(mktemp "${tmp_dir}/${prefix}.XXXXXX")"
    if [[ -n "${suffix}" ]]; then
        mv "${tmp_path}" "${tmp_path}${suffix}"
        tmp_path="${tmp_path}${suffix}"
        : >"${tmp_path}"
    fi

    printf '%s\n' "${tmp_path}"
}

kubectl_cmd() {
    if [[ -n "${KUBECTL_CONTEXT:-}" ]]; then
        kubectl --context "${KUBECTL_CONTEXT}" "$@"
    else
        kubectl "$@"
    fi
}

find_free_port() {
    local port="${1:-8080}"

    while :; do
        if ! lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "${port}"
            return 0
        fi
        port=$((port + 1))
    done
}

start_kubectl_port_forward() {
    local namespace="$1"
    local target="$2"
    local remote_port="$3"
    local preferred_local_port="${4:-8080}"
    local description="${5:-port-forward}"
    local local_port

    local_port="$(find_free_port "${preferred_local_port}")"
    PORT_FORWARD_LOG="$(make_temp_file "backend-token-pf" ".log")"

    kubectl_cmd -n "${namespace}" port-forward "${target}" "${local_port}:${remote_port}" >"${PORT_FORWARD_LOG}" 2>&1 &
    PORT_FORWARD_PID=$!
    PORT_FORWARD_PORT="${local_port}"

    for _ in $(seq 1 50); do
        if ! kill -0 "${PORT_FORWARD_PID}" >/dev/null 2>&1; then
            cat "${PORT_FORWARD_LOG}" >&2 || true
            die "Failed to start ${description} on localhost:${local_port}"
        fi
        if grep -q "Forwarding from" "${PORT_FORWARD_LOG}" 2>/dev/null; then
            return 0
        fi
        sleep 0.2
    done

    cat "${PORT_FORWARD_LOG}" >&2 || true
    die "${description} did not become ready on localhost:${local_port}"
}

stop_port_forward() {
    if [[ -n "${PORT_FORWARD_PID}" ]] && kill -0 "${PORT_FORWARD_PID}" >/dev/null 2>&1; then
        kill "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
        wait "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
    fi

    if [[ -n "${PORT_FORWARD_LOG}" && -f "${PORT_FORWARD_LOG}" ]]; then
        rm -f "${PORT_FORWARD_LOG}"
    fi

    PORT_FORWARD_PID=""
    PORT_FORWARD_LOG=""
    PORT_FORWARD_PORT=""
}

cleanup() {
    stop_port_forward
}

trap cleanup EXIT

wait_for_http_ready() {
    local url="$1"
    local timeout="${2:-30}"
    local description="${3:-endpoint}"
    local end_time

    end_time=$((SECONDS + timeout))
    while (( SECONDS < end_time )); do
        if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done

    die "${description} did not become ready at ${url}"
}

start_osmo_api_session() {
    start_kubectl_port_forward "${OSMO_NAMESPACE}" "svc/osmo-service" 80 8080 "OSMO API"
    wait_for_http_ready "http://localhost:${PORT_FORWARD_PORT}/api/version" 60 "OSMO API"
    OSMO_API_URL="http://localhost:${PORT_FORWARD_PORT}"
}

read_existing_token() {
    kubectl_cmd get secret osmo-operator-token -n "${BACKEND_OPERATOR_NAMESPACE}" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true
}

write_token_secret() {
    local token="$1"

    kubectl_cmd create namespace "${BACKEND_OPERATOR_NAMESPACE}" --dry-run=client -o yaml | kubectl_cmd apply -f - >/dev/null
    kubectl_cmd create secret generic osmo-operator-token \
        --namespace "${BACKEND_OPERATOR_NAMESPACE}" \
        --from-literal=token="${token}" \
        --dry-run=client -o yaml | kubectl_cmd apply -f - >/dev/null
}

create_token_via_api() {
    local token_name="$1"
    local expiry_date="$2"
    local response_file
    local http_code
    local token

    response_file="$(make_temp_file "backend-token-api")"
    http_code="$(curl -sS -X POST \
        "${OSMO_API_URL}/api/auth/access_token/service/${token_name}?expires_at=${expiry_date}&roles=osmo-backend" \
        -H "Content-Type: application/json" \
        -d '{}' \
        -o "${response_file}" -w "%{http_code}" || true)"

    if [[ "${http_code}" =~ ^20(0|1)$ ]]; then
        token="$(jq -r '.token // . // empty' "${response_file}" 2>/dev/null || true)"
        if [[ -z "${token}" ]]; then
            token="$(tr -d '"' <"${response_file}" | tr -d '\r' | xargs)"
        fi
        rm -f "${response_file}"
        if [[ -n "${token}" ]]; then
            printf '%s\n' "${token}"
            return 0
        fi
    fi

    cat "${response_file}" >&2 || true
    rm -f "${response_file}"
    return 1
}

create_token_via_osmo_cli() {
    local token_name="$1"
    local expiry_date="$2"
    local token_json
    local token

    command -v osmo >/dev/null 2>&1 || return 1

    osmo login "${OSMO_API_URL}" --method dev --username admin >/dev/null 2>&1 || return 1
    token_json="$(osmo token set "${token_name}" \
        --expires-at "${expiry_date}" \
        --description "Backend Operator Token (auto-generated)" \
        --service --roles osmo-backend -t json 2>/dev/null || true)"
    token="$(printf '%s' "${token_json}" | jq -r '.token // empty' 2>/dev/null || true)"

    [[ -n "${token}" ]] || return 1
    printf '%s\n' "${token}"
}

main() {
    local token=""
    local token_name
    local expiry_date

    require_command kubectl
    require_command curl
    require_command jq
    require_command lsof

    [[ -n "${OSMO_NAMESPACE:-}" ]] || die "OSMO_NAMESPACE is required"
    [[ -n "${BACKEND_OPERATOR_NAMESPACE:-}" ]] || die "BACKEND_OPERATOR_NAMESPACE is required"

    if [[ -n "${BACKEND_OPERATOR_SERVICE_TOKEN:-}" ]]; then
        log "Using provided backend operator service token"
        write_token_secret "${BACKEND_OPERATOR_SERVICE_TOKEN}"
        exit 0
    fi

    token="$(read_existing_token)"
    if [[ -n "${token}" ]]; then
        log "Reusing existing osmo-operator-token secret"
        exit 0
    fi

    start_osmo_api_session

    token_name="backend-token-$(date -u +%Y%m%d%H%M%S)"
    expiry_date="$(date -u -v+1y +%F 2>/dev/null || date -u -d '+1 year' +%F 2>/dev/null || echo '2027-01-01')"

    if [[ "${ENABLE_AUTH:-false}" == "true" ]]; then
        die "Automatic backend operator token creation is not supported when ENABLE_AUTH=true. Use password login (default) or provide BACKEND_OPERATOR_SERVICE_TOKEN explicitly."
    fi

    log "Creating backend operator service token via OSMO API"
    token="$(create_token_via_api "${token_name}" "${expiry_date}" || true)"
    if [[ -z "${token}" ]]; then
        warn "Direct API token creation failed; trying osmo CLI fallback"
        token="$(create_token_via_osmo_cli "${token_name}" "${expiry_date}" || true)"
    fi

    [[ -n "${token}" ]] || die "Failed to create backend operator service token automatically"

    write_token_secret "${token}"
    log "Created osmo-operator-token secret for backend operator"
}

main "$@"
