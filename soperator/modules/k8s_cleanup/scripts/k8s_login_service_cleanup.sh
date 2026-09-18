#!/bin/bash
set -euo pipefail

retry_script="$(dirname "$0")/../../scripts/retry.sh"

context="${K8S_CLUSTER_CONTEXT:?context is required}"
namespace="${SOPERATOR_NAMESPACE:?namespace is required}"
service="${LOGIN_SERVICE_NAME:?service name is required}"

if [ -n "${K8S_CLUSTER_ID:-}" ]; then
  "$retry_script" -- nebius mk8s cluster get-credentials \
    --context-name "$context" \
    --external \
    --force \
    --id "$K8S_CLUSTER_ID"
fi

system_namespace="${namespace}-system"

if ! "$retry_script" -- kubectl get namespace "$namespace" --context "$context" >/dev/null 2>&1; then
  echo "Namespace $namespace not found or cluster unreachable; skipping login service cleanup."
  exit 0
fi

if ! "$retry_script" -- kubectl get namespace "$system_namespace" --context "$context" >/dev/null 2>&1; then
  echo "Namespace $namespace not found or cluster unreachable; skipping login service cleanup."
  exit 0
fi

echo "Attempting to stop soperator controller to prevent service recreation..."
if kubectl get deployment soperator-controller-manager -n "$system_namespace" --context "$context" >/dev/null 2>&1; then
  "$retry_script" -- kubectl scale deployment soperator-controller-manager -n "$system_namespace" --context "$context" --replicas=0
  # kubectl scale returns before pods terminate; wait so the controller can't recreate the Service.
  echo "Waiting for soperator controller pod to terminate..."
  kubectl wait --for=delete pod -l control-plane=controller-manager -n "$system_namespace" --context "$context" --timeout=60s || true
fi

echo "Deleting service $namespace/$service..."
"$retry_script" -- kubectl delete service "$service" -n "$namespace" --context "$context" --ignore-not-found --wait=true --timeout=5m
