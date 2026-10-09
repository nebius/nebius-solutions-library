#!/bin/bash
set -euo pipefail

readonly kubectl_apply_with_retries_script="$(dirname "$0")/../../scripts/kubectl_apply_with_retries.sh"

kubectl create namespace "$NAMESPACE" --context "$K8S_CLUSTER_CONTEXT" --dry-run=client -o yaml \
  | "$kubectl_apply_with_retries_script" --context "$K8S_CLUSTER_CONTEXT"

AKID=$(nebius iam v2 access-key create --parent-id "$IAM_PROJECT_ID" \
  --account-service-account-id "$SERVICE_ACCOUNT_ID" \
  --format json | jq -r '.metadata.id')

"$kubectl_apply_with_retries_script" --server-side --context "$K8S_CLUSTER_CONTEXT" <<EOF
apiVersion: v1
kind: Secret
type: Opaque
metadata:
  name: $SECRET_NAME
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: soperator-terraform
  annotations:
    slurm.nebius.ai/service-account: $SERVICE_ACCOUNT_ID
data:
  aws-access-key-id: $(nebius iam v2 access-key get --id "$AKID" --format json | jq -r '.status.aws_access_key_id' | tr -d '\n' | base64)
  aws-access-secret-key: $(nebius iam v2 access-key get --id "$AKID" --format json | jq -r '.status.secret' | tr -d '\n' | base64)
  backup-password: $(echo -n "$BACKUPS_PASSWORD" | base64)
EOF
