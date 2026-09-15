#!/usr/bin/env bash
set -euo pipefail

: "${K8S_CONTEXT:?}"
: "${NAMESPACE:?}"
timeout_seconds=${TIMEOUT_SECONDS:-3600}
poll_seconds=${POLL_SECONDS:-10}
deadline=$((SECONDS + timeout_seconds))
kubectl=(kubectl --context "$K8S_CONTEXT" --namespace "$NAMESPACE" --request-timeout=30s)

diagnose() {
  "${kubectl[@]}" get helmreleases.helm.toolkit.fluxcd.io -o wide || true
  "${kubectl[@]}" get pods || true
}

wait_for_release() {
  local name=$1 json token="" last_status="" status
  echo "Checking HelmRelease $name..."
  while ((SECONDS < deadline)); do
    if ! json=$("${kubectl[@]}" get helmrelease "$name" --ignore-not-found -o json); then
      echo "Unable to read HelmRelease $name; retrying."
      sleep "$poll_seconds"
      continue
    fi
    if [[ -z "$json" ]]; then
      sleep "$poll_seconds"
      continue
    fi

    # Ready from an older generation is not proof that the current spec works.
    if jq -e '
      .metadata.generation as $generation |
      .status.observedGeneration == $generation and
      any(.status.conditions[]?; .type == "Ready" and .status == "True") and
      (any(.status.conditions[]?; (.type == "Reconciling" or .type == "Stalled") and .status == "True") | not)
    ' <<<"$json" >/dev/null; then
      echo "HelmRelease $name is Ready."
      return 0
    fi

    if [[ -z "$token" ]]; then
      token="$(date +%s)-$$-$RANDOM"
      # resetAt also recovers installs which exhausted retries while nodes were
      # unavailable. Do not force an upgrade or invoke the Flux CLI.
      "${kubectl[@]}" annotate helmrelease "$name" --overwrite \
        "reconcile.fluxcd.io/requestedAt=$token" \
        "reconcile.fluxcd.io/resetAt=$token"
    fi
    status=$(jq -r '[.status.conditions[]? | "\(.type)=\(.status): \(.reason)"] | join(", ")' <<<"$json")
    if [[ "$status" != "$last_status" ]]; then
      echo "$name: $status"
      last_status=$status
    fi
    sleep "$poll_seconds"
  done
  echo "Timed out waiting for HelmRelease $name." >&2
  diagnose
  return 1
}

# The umbrella was installed with Helm before the node groups. Its Flux
# HelmRelease is created only after this check and the full values ConfigMap.
# Namespaces may also need their retries reset after the nodes become available.
for release in \
  "$NAMESPACE-soperator-fluxcd-ns" \
  "$NAMESPACE-soperator-fluxcd-cert-manager" \
  "$NAMESPACE-soperator-fluxcd-storageclasses" \
  "$NAMESPACE-soperator-fluxcd-kruise"; do
  wait_for_release "$release"
done
