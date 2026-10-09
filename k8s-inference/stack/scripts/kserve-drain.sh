#!/usr/bin/env bash
# Before the KServe releases are uninstalled at `destroy`: delete every InferenceService and LocalModelCache
# (endpoints defined through the API are not Terraform resources; the acceptance probe's example endpoint is
# one of them) while the KServe controller is still there to run their finalizers, then strip the finalizers
# of whatever is left. Without this the CRD deletion waits for ever on a finalizer nobody clears and the
# kserve-crd uninstall times out (measured 2026-10-09).
#
#   KUBE_SERVER=https://<endpoint> KUBE_CA=<pem> NEBIUS_IAM_TOKEN=<token> stack/scripts/kserve-drain.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
k() { "$here/kube.sh" "$@"; }
kinds="inferenceservices.serving.kserve.io localmodelcaches.serving.kserve.io services.serving.knative.dev"
for kind in $kinds; do
  k delete "$kind" --all --all-namespaces --wait=false --ignore-not-found > /dev/null 2>&1 || true
done
for i in $(seq 1 24); do
  left=0
  for kind in $kinds; do left=$((left + $(k get "$kind" --all-namespaces -o name 2> /dev/null | wc -l))); done
  [ "$left" = 0 ] && break
  sleep 5
done
for kind in $kinds routes.serving.knative.dev ingresses.networking.internal.knative.dev; do
  for obj in $(k get "$kind" --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2> /dev/null); do
    k patch "$kind" "${obj#*/}" -n "${obj%%/*}" --type=merge -p '{"metadata":{"finalizers":[]}}' > /dev/null 2>&1 || true
  done
done
echo "kserve-drain: endpoints removed (left after the wait: $left)"
