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

main() {
    local timeout
    local action="${ACTION:-apply}"

    require_command kubectl

    [[ -n "${KUBECONFIG:-}" ]] || die "KUBECONFIG is required"
    [[ -n "${CLUSTER_ISSUER_NAME:-}" ]] || die "CLUSTER_ISSUER_NAME is required"

    timeout="${WAIT_TIMEOUT:-300s}"

    case "${action}" in
        apply)
            [[ -n "${CERT_MANAGER_EMAIL:-}" ]] || die "CERT_MANAGER_EMAIL is required"
            [[ -n "${CERT_MANAGER_ACME_SERVER:-}" ]] || die "CERT_MANAGER_ACME_SERVER is required"
            [[ -n "${CERT_MANAGER_HTTP01_INGRESS_CLASS:-}" ]] || die "CERT_MANAGER_HTTP01_INGRESS_CLASS is required"

            log "Applying ClusterIssuer ${CLUSTER_ISSUER_NAME}"
            cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${CLUSTER_ISSUER_NAME}
spec:
  acme:
    server: ${CERT_MANAGER_ACME_SERVER}
    email: ${CERT_MANAGER_EMAIL}
    privateKeySecretRef:
      name: ${CLUSTER_ISSUER_NAME}-account-key
    solvers:
    - http01:
        ingress:
          class: ${CERT_MANAGER_HTTP01_INGRESS_CLASS}
EOF

            log "Waiting for ClusterIssuer ${CLUSTER_ISSUER_NAME} to become Ready"
            kubectl_cmd wait --for=condition=Ready --timeout="${timeout}" "clusterissuer/${CLUSTER_ISSUER_NAME}" >/dev/null
            ;;
        delete)
            [[ -n "${CERT_MANAGER_NAMESPACE:-}" ]] || die "CERT_MANAGER_NAMESPACE is required for delete"

            if kubectl_cmd get "clusterissuer/${CLUSTER_ISSUER_NAME}" >/dev/null 2>&1; then
                log "Deleting ClusterIssuer ${CLUSTER_ISSUER_NAME}"
                kubectl_cmd delete "clusterissuer/${CLUSTER_ISSUER_NAME}" --ignore-not-found >/dev/null
                kubectl_cmd wait --for=delete --timeout="${timeout}" "clusterissuer/${CLUSTER_ISSUER_NAME}" >/dev/null 2>&1 || true
            else
                log "ClusterIssuer ${CLUSTER_ISSUER_NAME} not found, skipping delete"
            fi

            kubectl_cmd delete secret -n "${CERT_MANAGER_NAMESPACE}" "${CLUSTER_ISSUER_NAME}-account-key" --ignore-not-found >/dev/null 2>&1 || true
            ;;
        *)
            die "Unsupported ACTION=${action}. Expected one of: apply, delete"
            ;;
    esac
}

main "$@"
