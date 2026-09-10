#!/bin/bash

set -euo pipefail

log() {
    printf '[INFO] %s\n' "$*"
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

get_ingress_public_ip() {
    kubectl_cmd get svc -n "${INGRESS_NAMESPACE}" "${INGRESS_SERVICE_NAME}" \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
}

hostname_has_expected_ip() {
    local hostname="$1"
    local expected_ip="$2"
    local resolved_ips=""

    resolved_ips="$(dig +short "${hostname}" A | tr -d '\r' || true)"
    [[ -n "${resolved_ips}" ]] || return 1
    grep -Fxq "${expected_ip}" <<<"${resolved_ips}"
}

wait_for_hostname() {
    local hostname="$1"
    local expected_ip="$2"
    local timeout_seconds="$3"
    local end_time

    end_time=$((SECONDS + timeout_seconds))

    while (( SECONDS < end_time )); do
        if hostname_has_expected_ip "${hostname}" "${expected_ip}"; then
            log "Verified public DNS: ${hostname} -> ${expected_ip}"
            return 0
        fi
        sleep 5
    done

    die "Public DNS is not ready for ${hostname}. Expected A record -> ${expected_ip} before terraform apply."
}

main() {
    local expected_ip=""
    local timeout_seconds

    require_command kubectl
    require_command dig

    [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is required"
    [[ -n "${INGRESS_NAMESPACE:-}" ]] || die "INGRESS_NAMESPACE is required"
    [[ -n "${INGRESS_SERVICE_NAME:-}" ]] || die "INGRESS_SERVICE_NAME is required"
    [[ -n "${OSMO_HOSTNAME:-}" ]] || die "OSMO_HOSTNAME is required"
    [[ -n "${KEYCLOAK_HOSTNAME:-}" ]] || die "KEYCLOAK_HOSTNAME is required"

    timeout_seconds="${WAIT_TIMEOUT_SECONDS:-300}"
    expected_ip="$(get_ingress_public_ip)"
    [[ -n "${expected_ip}" ]] || die "Could not determine ingress public IP from ${INGRESS_NAMESPACE}/${INGRESS_SERVICE_NAME}"

    log "Expecting public DNS to resolve to ingress IP ${expected_ip}"
    wait_for_hostname "${OSMO_HOSTNAME}" "${expected_ip}" "${timeout_seconds}"
    wait_for_hostname "${KEYCLOAK_HOSTNAME}" "${expected_ip}" "${timeout_seconds}"
}

main "$@"
