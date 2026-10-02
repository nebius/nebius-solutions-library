#!/bin/bash
set -euo pipefail

retry_script="$(dirname "$0")/../../scripts/retry.sh"

# Set up kubectl context if the cluster ID is available (absent in old Terraform state).
if [ -n "${K8S_CLUSTER_ID:-}" ]; then
  "$retry_script" -- nebius mk8s cluster get-credentials \
    --context-name "$K8S_CLUSTER_CONTEXT" \
    --external \
    --force \
    --id "$K8S_CLUSTER_ID"
fi

# Delete all IAM access keys for the service account.
# Skip the IAM block if the SA was already removed out-of-band (e.g. by force-cleanup).
if "$retry_script" -- nebius iam service-account get --id "$SERVICE_ACCOUNT_ID" >/dev/null 2>&1; then
  for AKID in $("$retry_script" -- nebius iam v2 access-key list-by-account \
    --account-service-account-id "$SERVICE_ACCOUNT_ID" \
    --format json | jq -r '.items[].metadata.id'); do
    "$retry_script" -- nebius iam v2 access-key delete --id "$AKID"
  done
else
  echo "Service account $SERVICE_ACCOUNT_ID already gone, skipping access-key cleanup"
fi

# Delete the k8s secret only if the context is available.
if kubectl config get-contexts "$K8S_CLUSTER_CONTEXT" &>/dev/null; then
  "$retry_script" -- bash -c "kubectl get --context '$K8S_CLUSTER_CONTEXT' -n '$NAMESPACE' secret '$SECRET_NAME' -oyaml | kubectl delete --context '$K8S_CLUSTER_CONTEXT' -f -"
else
  echo "kubectl context '$K8S_CLUSTER_CONTEXT' not found, skipping secret cleanup"
fi
