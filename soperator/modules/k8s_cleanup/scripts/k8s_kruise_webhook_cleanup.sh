#!/bin/bash
set -euo pipefail

retry_script="$(dirname "$0")/../../scripts/retry.sh"

context="${K8S_CLUSTER_CONTEXT:?context is required}"
prefix="${K8S_WEBHOOK_PREFIX:?webhook prefix is required}"

if [ -n "${K8S_CLUSTER_ID:-}" ]; then
  "$retry_script" -- nebius mk8s cluster get-credentials \
    --context-name "$context" \
    --external \
    --force \
    --id "$K8S_CLUSTER_ID"
fi

if ! "$retry_script" -- kubectl version --context "$context" >/dev/null 2>&1; then
  echo "Cluster unreachable for context $context; skipping kruise webhook cleanup."
  exit 0
fi

mutating_raw="$("$retry_script" -- kubectl get mutatingwebhookconfiguration \
  --context "$context" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .webhooks[*]}{.name}{" "}{end}{"\n"}{end}')"
mutating_configs="$(echo "$mutating_raw" | awk -v target="^" -v prefix="$prefix" '
    $1 ~ (target prefix) {print $1; next}
    $0 ~ (target ".*" prefix) {print $1}
  ' | sort -u)"

validating_raw="$("$retry_script" -- kubectl get validatingwebhookconfiguration \
  --context "$context" \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .webhooks[*]}{.name}{" "}{end}{"\n"}{end}')"
validating_configs="$(echo "$validating_raw" | awk -v target="^" -v prefix="$prefix" '
    $1 ~ (target prefix) {print $1; next}
    $0 ~ (target ".*" prefix) {print $1}
  ' | sort -u)"

if [ -z "$mutating_configs" ] && [ -z "$validating_configs" ]; then
  echo "No webhook configurations found with prefix $prefix; nothing to delete."
  exit 0
fi

for cfg in $mutating_configs; do
  echo "Deleting MutatingWebhookConfiguration $cfg (prefix $prefix)..."
  kubectl delete mutatingwebhookconfiguration "$cfg" --context "$context" --wait=true --timeout=2m || true
done

for cfg in $validating_configs; do
  echo "Deleting ValidatingWebhookConfiguration $cfg (prefix $prefix)..."
  kubectl delete validatingwebhookconfiguration "$cfg" --context "$context" --wait=true --timeout=2m || true
done
