#!/usr/bin/env bash
set -euo pipefail

: "${K8S_CONTEXT:?}"
: "${NAMESPACE:?}"
: "${VALUES_YAML:?}"

kubectl=(kubectl --context "$K8S_CONTEXT" --namespace "$NAMESPACE" --request-timeout=30s)
"${kubectl[@]}" wait --for=condition=Established \
  crd/helmreleases.helm.toolkit.fluxcd.io \
  crd/helmrepositories.source.toolkit.fluxcd.io --timeout=60s
existing=$("${kubectl[@]}" get configmap terraform-fluxcd-values --ignore-not-found -o name)
if [[ -n "$existing" ]]; then
  echo "Keeping existing terraform-fluxcd-values."
  exit 0
fi

# Match the ownership of the raw chart used for the final configuration so Helm
# can adopt the seed without deleting the ConfigMap watched by Flux.
"${kubectl[@]}" create configmap terraform-fluxcd-values \
  --from-literal="values.yaml=$VALUES_YAML" --dry-run=client -o json |
  jq --arg namespace "$NAMESPACE" '
    .metadata.labels = {
      "app.kubernetes.io/managed-by": "Helm",
      "reconcile.fluxcd.io/watch": "Enabled"
    } |
    .metadata.annotations = {
      "meta.helm.sh/release-name": "terraform-fluxcd-values",
      "meta.helm.sh/release-namespace": $namespace,
      "helm.sh/resource-policy": "keep"
    }' |
  "${kubectl[@]}" create -f -
