#!/usr/bin/env bash
# kubectl against a cluster described by the environment, for Terraform local-exec provisioners that need
# kubectl at apply or destroy time (stack/platform/charts.tf). The cluster's address and CA (PEM) arrive as
# KUBE_SERVER and KUBE_CA; the IAM token is NEBIUS_IAM_TOKEN of the calling shell (stack.sh exports it), so
# no credential is ever a resource input or part of the state. The kubeconfig lives in a temporary file for
# the duration of one kubectl call.
#
#   KUBE_SERVER=https://<endpoint> KUBE_CA=<pem> NEBIUS_IAM_TOKEN=<token> stack/scripts/kube.sh <kubectl args...>
set -euo pipefail
for v in KUBE_SERVER KUBE_CA NEBIUS_IAM_TOKEN; do [ -n "${!v:-}" ] || { echo "kube.sh: $v is empty" >&2; exit 2; }; done
f=$(mktemp); trap 'rm -f "$f"' EXIT
printf 'apiVersion: v1\nkind: Config\ncurrent-context: target\nclusters: [{name: target, cluster: {server: "%s", certificate-authority-data: "%s"}}]\nusers: [{name: target, user: {token: "%s"}}]\ncontexts: [{name: target, context: {cluster: target, user: target}}]\n' \
  "$KUBE_SERVER" "$(printf '%s' "$KUBE_CA" | base64 | tr -d '\n')" "$NEBIUS_IAM_TOKEN" > "$f"
exec kubectl --kubeconfig "$f" "$@"
