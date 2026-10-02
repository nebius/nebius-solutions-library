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

wait_for_crd() {
    local crd_name="$1"
    local timeout="$2"

    log "Waiting for CRD ${crd_name} to become established"
    kubectl_cmd wait --for=condition=Established --timeout="${timeout}" "crd/${crd_name}" >/dev/null
}

wait_for_deployment() {
    local namespace="$1"
    local deployment_name="$2"
    local timeout="$3"

    log "Waiting for deployment ${namespace}/${deployment_name} to become available"
    kubectl_cmd rollout status "deployment/${deployment_name}" -n "${namespace}" --timeout="${timeout}" >/dev/null
}

main() {
    local timeout
    local crd_name
    local deployment_name
    local crd_names=(
        "certificaterequests.cert-manager.io"
        "certificates.cert-manager.io"
        "challenges.acme.cert-manager.io"
        "clusterissuers.cert-manager.io"
        "issuers.cert-manager.io"
        "orders.acme.cert-manager.io"
    )
    local deployments=(
        "cert-manager"
        "cert-manager-cainjector"
        "cert-manager-webhook"
    )

    require_command kubectl

    [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is required"
    [[ -n "${CERT_MANAGER_NAMESPACE:-}" ]] || die "CERT_MANAGER_NAMESPACE is required"

    timeout="${WAIT_TIMEOUT:-300s}"

    for crd_name in "${crd_names[@]}"; do
        wait_for_crd "${crd_name}" "${timeout}"
    done

    for deployment_name in "${deployments[@]}"; do
        wait_for_deployment "${CERT_MANAGER_NAMESPACE}" "${deployment_name}" "${timeout}"
    done
}

main "$@"
