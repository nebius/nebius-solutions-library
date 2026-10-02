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

require_command kubectl

: "${NAMESPACE:?NAMESPACE is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${STORAGE_ACCESS_KEY_ID:?STORAGE_ACCESS_KEY_ID is required}"
: "${STORAGE_SECRET_ACCESS_KEY:?STORAGE_SECRET_ACCESS_KEY is required}"

log "Applying runtime secrets in namespace ${NAMESPACE}"

kubectl_cmd create secret generic db-secret \
  --namespace "${NAMESPACE}" \
  --from-literal=db-password="${POSTGRES_PASSWORD}" \
  --dry-run=client -o yaml | kubectl_cmd apply -f - >/dev/null

kubectl_cmd create secret generic osmo-storage \
  --namespace "${NAMESPACE}" \
  --from-literal=access-key-id="${STORAGE_ACCESS_KEY_ID}" \
  --from-literal=secret-access-key="${STORAGE_SECRET_ACCESS_KEY}" \
  --dry-run=client -o yaml | kubectl_cmd apply -f - >/dev/null

log "Runtime secrets applied"
