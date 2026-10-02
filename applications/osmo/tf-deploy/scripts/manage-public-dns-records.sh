#!/bin/bash

set -euo pipefail

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

kubectl_cmd() {
    if [[ -n "${KUBECTL_CONTEXT:-}" ]]; then
        kubectl --context "${KUBECTL_CONTEXT}" "$@"
    else
        kubectl "$@"
    fi
}

hostname_relative_to_base_domain() {
    local hostname="$1"
    local base_domain="$2"
    local suffix=".${base_domain}"

    if [[ "${hostname}" == "${base_domain}" ]]; then
        printf '@\n'
        return 0
    fi

    [[ "${hostname}" == *"${suffix}" ]] || die "Hostname ${hostname} does not fall under base domain ${base_domain}"
    printf '%s\n' "${hostname%"${suffix}"}"
}

npc_cmd() {
    npc --profile "${DNS_NPC_PROFILE}" "$@"
}

recordset_exists() {
    local name="$1"
    local output=""

    if output="$(npc_cmd dns infra record get-recordset \
        --parent-id "${DNS_ZONE_ID}" \
        --name "${name}" \
        --type a \
        --format json 2>&1)"; then
        return 0
    fi

    if grep -Eqi 'not found|not_found|NotFound|FAILED_PRECONDITION|failed precondition' <<<"${output}"; then
        return 1
    fi

    die "Failed to query DNS recordset ${name}. npc output: ${output}"
}

get_target_ip() {
    local expected_ip="${PUBLIC_DNS_TARGET_IP:-}"
    local attempts=0

    if [[ -n "${expected_ip}" ]]; then
        printf '%s\n' "${expected_ip}"
        return 0
    fi

    require_command kubectl
    [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is required when PUBLIC_DNS_TARGET_IP is unset"
    [[ -n "${INGRESS_NAMESPACE:-}" ]] || die "INGRESS_NAMESPACE is required when PUBLIC_DNS_TARGET_IP is unset"
    [[ -n "${INGRESS_SERVICE_NAME:-}" ]] || die "INGRESS_SERVICE_NAME is required when PUBLIC_DNS_TARGET_IP is unset"

    while (( attempts < 60 )); do
        expected_ip="$(
            kubectl_cmd get svc -n "${INGRESS_NAMESPACE}" "${INGRESS_SERVICE_NAME}" \
                -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true
        )"

        if [[ -n "${expected_ip}" ]]; then
            printf '%s\n' "${expected_ip}"
            return 0
        fi

        attempts=$((attempts + 1))
        sleep 5
    done

    die "Could not determine ingress public IP from ${INGRESS_NAMESPACE}/${INGRESS_SERVICE_NAME}"
}

upsert_recordset() {
    local hostname="$1"
    local relative_name="$2"
    local target_ip="$3"

    log "Upserting DNS A recordset: ${hostname} -> ${target_ip}"
    npc_cmd dns infra record upsert-recordset \
        --parent-id "${DNS_ZONE_ID}" \
        --name "${relative_name}" \
        --type a \
        --data "${target_ip}" \
        --ttl 300 \
        --format json >/dev/null
}

delete_recordset() {
    local hostname="$1"
    local relative_name="$2"

    if ! recordset_exists "${relative_name}"; then
        log "DNS A recordset does not exist, skipping: ${hostname}"
        return 0
    fi

    log "Deleting DNS A recordset: ${hostname}"
    npc_cmd dns infra record delete-recordset \
        --parent-id "${DNS_ZONE_ID}" \
        --name "${relative_name}" \
        --type a \
        --format json >/dev/null
}

main() {
    local action="${ACTION:-upsert}"
    local osmo_relative_name=""
    local keycloak_relative_name=""
    local target_ip=""

    require_command npc

    [[ -n "${OSMO_BASE_DOMAIN:-}" ]] || die "OSMO_BASE_DOMAIN is required"
    [[ -n "${OSMO_HOSTNAME:-}" ]] || die "OSMO_HOSTNAME is required"
    [[ -n "${KEYCLOAK_HOSTNAME:-}" ]] || die "KEYCLOAK_HOSTNAME is required"

    if [[ "${OSMO_BASE_DOMAIN}" == "nip.io" ]]; then
        log "OSMO_BASE_DOMAIN=nip.io; skipping public DNS record management"
        return 0
    fi

    [[ -n "${DNS_NPC_PROFILE:-}" ]] || die "DNS_NPC_PROFILE is required"
    [[ -n "${DNS_ZONE_ID:-}" ]] || die "DNS_ZONE_ID is required"

    osmo_relative_name="$(hostname_relative_to_base_domain "${OSMO_HOSTNAME}" "${OSMO_BASE_DOMAIN}")"
    keycloak_relative_name="$(hostname_relative_to_base_domain "${KEYCLOAK_HOSTNAME}" "${OSMO_BASE_DOMAIN}")"

    case "${action}" in
        upsert)
            target_ip="$(get_target_ip)"
            upsert_recordset "${OSMO_HOSTNAME}" "${osmo_relative_name}" "${target_ip}"
            upsert_recordset "${KEYCLOAK_HOSTNAME}" "${keycloak_relative_name}" "${target_ip}"
            ;;
        delete)
            delete_recordset "${OSMO_HOSTNAME}" "${osmo_relative_name}"
            delete_recordset "${KEYCLOAK_HOSTNAME}" "${keycloak_relative_name}"
            ;;
        *)
            die "Unsupported ACTION=${action}. Expected one of: upsert, delete"
            ;;
    esac
}

main "$@"
