#!/usr/bin/env bash
set -euo pipefail

: "${K8S_CONTEXT:?}"
: "${NAMESPACE:?}"
: "${STORAGE_NAMESPACE:?}"
: "${CHART_REPOSITORY:?}"
: "${CHART_VERSION:?}"
: "${VALUES_YAML:?}"

kubectl=(kubectl --context "$K8S_CONTEXT" --namespace "$NAMESPACE" --request-timeout=30s)

# A successful full-values Helm release means the installation has already
# passed the foundation gate. Never downgrade that cluster to the minimal set.
full_values=$("${kubectl[@]}" get secrets \
  -l owner=helm,name=terraform-fluxcd-values,status=deployed -o name)
if [[ -n "$full_values" ]]; then
  echo "Full Terraform values already installed; preserving the running stack."
  exit 0
fi

command -v helm >/dev/null
command -v jq >/dev/null
command -v yq >/dev/null

module_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
printf '%s\n' "$VALUES_YAML" >"$work_dir/values.yaml"
export FOUNDATION_RELEASE_NAME="$NAMESPACE-soperator-fluxcd"

# Also recover an installation started with the old bootstrap workflow. Suspend
# its umbrella before Helm repairs the initial set, preventing competing writes.
umbrella=$("${kubectl[@]}" get helmrelease soperator-fluxcd --ignore-not-found -o name)
if [[ -n "$umbrella" ]]; then
  "${kubectl[@]}" patch helmrelease soperator-fluxcd --type=merge \
    -p '{"spec":{"suspend":true}}'
fi

# main does not create this namespace. Keep it outside the umbrella manifest so
# Flux takeover cannot remove it when it renders the unmodified chart later.
"${kubectl[@]}" create namespace "$STORAGE_NAMESPACE" --dry-run=client -o yaml |
  "${kubectl[@]}" apply -f -

bash "$module_dir/scripts/helm_with_filter.sh" upgrade --install "$FOUNDATION_RELEASE_NAME" \
  "$CHART_REPOSITORY/helm-soperator-fluxcd" \
  --version "$CHART_VERSION" --namespace "$NAMESPACE" \
  --kube-context "$K8S_CONTEXT" --reset-values --no-hooks \
  --values "$work_dir/values.yaml"
