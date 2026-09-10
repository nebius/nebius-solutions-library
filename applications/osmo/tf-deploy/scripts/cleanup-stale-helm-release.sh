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

helm_cmd() {
    if [[ -n "${KUBECTL_CONTEXT:-}" ]]; then
        helm --kube-context "${KUBECTL_CONTEXT}" "$@"
    else
        helm "$@"
    fi
}

main() {
    local status=""

    require_command helm
    require_command jq

    [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is required"
    [[ -n "${NAMESPACE:-}" ]] || die "NAMESPACE is required"
    [[ -n "${RELEASE_NAME:-}" ]] || die "RELEASE_NAME is required"

    status="$(
        helm_cmd list -n "${NAMESPACE}" -o json \
        | jq -r --arg name "${RELEASE_NAME}" 'map(select(.name == $name)) | .[0].status // empty'
    )"

    if [[ -z "${status}" ]]; then
        log "No existing Helm release '${RELEASE_NAME}' found in namespace '${NAMESPACE}'"
        exit 0
    fi

    case "${status}" in
        deployed|superseded)
            log "Helm release '${RELEASE_NAME}' is '${status}'; leaving it in place"
            ;;
        failed|pending-install|pending-upgrade|pending-rollback|uninstalling)
            warn "Removing stale Helm release '${RELEASE_NAME}' in status '${status}'"
            helm_cmd uninstall "${RELEASE_NAME}" -n "${NAMESPACE}" --wait >/dev/null || true
            ;;
        *)
            warn "Encountered Helm release '${RELEASE_NAME}' in unexpected status '${status}'; leaving it in place"
            ;;
    esac
}

main "$@"
