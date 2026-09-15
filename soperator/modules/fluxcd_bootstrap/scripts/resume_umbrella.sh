#!/usr/bin/env bash
set -euo pipefail

: "${K8S_CONTEXT:?}"
: "${NAMESPACE:?}"

kubectl=(kubectl --context "$K8S_CONTEXT" --namespace "$NAMESPACE" --request-timeout=30s)
"${kubectl[@]}" patch helmrelease soperator-fluxcd --type=merge \
  -p '{"spec":{"suspend":false}}'
token="$(date +%s)-$$-$RANDOM"
"${kubectl[@]}" annotate helmrelease soperator-fluxcd --overwrite \
  "reconcile.fluxcd.io/requestedAt=$token" \
  "reconcile.fluxcd.io/resetAt=$token"
