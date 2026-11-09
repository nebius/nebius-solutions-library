#!/usr/bin/env bash
set -euo pipefail

: "${FLUX_K8S_CONTEXT:?FLUX_K8S_CONTEXT must be set}"
: "${FLUX_VERSION:?FLUX_VERSION must be set}"

readonly module_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly work_dir="$(mktemp -d)"
trap 'rm -rf -- "${work_dir}"' EXIT

curl --fail --silent --show-error --location \
  "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/install.yaml" \
  --output "${work_dir}/install.yaml"
cp "${module_dir}/templates/kustomization.yaml" "${work_dir}/kustomization.yaml"

# Flux v2.8+ removes deprecated API versions. Migrate objects and CRD
# storedVersions before updating an existing installation.
if kubectl --context "${FLUX_K8S_CONTEXT}" get \
  customresourcedefinition kustomizations.kustomize.toolkit.fluxcd.io \
  >/dev/null 2>&1; then
  sed "s/FLUX_VERSION/${FLUX_VERSION}/g" \
    "${module_dir}/templates/migration-job.yaml" \
    >"${work_dir}/migration-job.yaml"

  kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete \
    job flux-api-migration --ignore-not-found
  kubectl --context "${FLUX_K8S_CONTEXT}" apply \
    --filename "${work_dir}/migration-job.yaml"
  kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system wait \
    --for=condition=complete job/flux-api-migration --timeout=10m
  kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete \
    job flux-api-migration
fi

kubectl --context "${FLUX_K8S_CONTEXT}" apply -k "${work_dir}"

# Remove controllers that may be left from an older full-manifest installation.
kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete deployment \
  image-automation-controller \
  image-reflector-controller \
  notification-controller \
  source-watcher \
  --ignore-not-found
kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete serviceaccount \
  image-automation-controller \
  image-reflector-controller \
  notification-controller \
  source-watcher \
  --ignore-not-found
kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete service \
  notification-controller \
  source-watcher \
  webhook-receiver \
  --ignore-not-found
kubectl --context "${FLUX_K8S_CONTEXT}" --namespace flux-system delete networkpolicy \
  allow-webhooks \
  --ignore-not-found
