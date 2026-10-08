#!/usr/bin/env bash
# kubectl against a cluster whose kubeconfig arrives in the environment (KUBECONFIG_CONTENT, the same
# exec-based kubeconfig the stage's providers use). For Terraform local-exec provisioners that need
# kubectl at apply or destroy time (stack/platform/charts.tf).
#
#   KUBECONFIG_CONTENT=<yaml> stack/scripts/kube.sh <kubectl args...>
set -euo pipefail
[ -n "${KUBECONFIG_CONTENT:-}" ] || { echo "kube.sh: KUBECONFIG_CONTENT is empty" >&2; exit 2; }
f=$(mktemp); trap 'rm -f "$f"' EXIT
printf '%s' "$KUBECONFIG_CONTENT" > "$f"
exec kubectl --kubeconfig "$f" "$@"
