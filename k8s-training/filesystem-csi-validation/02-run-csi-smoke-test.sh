#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 02-run-csi-smoke-test.sh
# Purpose:
#   Run a minimal end-to-end validation using one PVC and one pod that mounts
#   the shared volume at /data.
#
# Why We Run This:
#   This is the fastest proof that the Terraform-managed default StorageClass
#   works, the PVC binds, and a pod can read and write data through the
#   shared filesystem exposed through CSI.
#
# Reference Docs:
#   https://docs.nebius.com/kubernetes/storage/filesystem-over-csi
#
# What This Script Does:
#   - Applies the single-pod smoke test manifest
#   - Waits for the PVC to bind
#   - Verifies that the PVC inherited the expected default StorageClass
#   - Waits for the pod to become ready
#   - Writes and reads a small probe file inside /data
#
# Usage:
#   ./02-run-csi-smoke-test.sh
#
# Optional Environment Variables:
#   TEST_NAMESPACE  Namespace where the validation resources should be created.
#                   Defaults to the current kubectl namespace or default.
#
# Manifest Used:
#   manifests/01-csi-smoke-test.yaml
#
# Created By: Aaron Fagan
# Created On: 2026-03-17
# Version: 0.1.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

log_step "Starting single-pod shared filesystem smoke test"
log_info "Namespace: ${TEST_NAMESPACE}"
log_info "Manifest: ${FILESYSTEM_SMOKE_MANIFEST_PATH}"
log_info "PVC name: ${FILESYSTEM_SMOKE_PVC_NAME}"
log_info "Pod name: ${FILESYSTEM_SMOKE_POD_NAME}"
log_info "Expected default StorageClass: ${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}"

log_step "Checking required local dependencies"
require_command kubectl
log_pass "Required local commands for the smoke test are available"

log_step "Applying the smoke test manifest"
kubectl apply -n "${TEST_NAMESPACE}" -f "${FILESYSTEM_SMOKE_MANIFEST_PATH}"
log_pass "Smoke test manifest applied in namespace '${TEST_NAMESPACE}'"

log_step "Waiting for the smoke test PVC to bind"
kubectl wait -n "${TEST_NAMESPACE}" \
  --for=jsonpath='{.status.phase}'=Bound \
  "pvc/${FILESYSTEM_SMOKE_PVC_NAME}" \
  --timeout=120s
log_info "PVC '${FILESYSTEM_SMOKE_PVC_NAME}' is bound"
log_pass "Smoke test PVC '${FILESYSTEM_SMOKE_PVC_NAME}' bound successfully"

log_step "Verifying that the smoke test PVC inherited the default StorageClass"
SMOKE_STORAGE_CLASS_NAME="$(kubectl get pvc -n "${TEST_NAMESPACE}" "${FILESYSTEM_SMOKE_PVC_NAME}" -o jsonpath='{.spec.storageClassName}')"
if [[ -z "${SMOKE_STORAGE_CLASS_NAME}" ]]; then
  log_fail "Smoke test PVC '${FILESYSTEM_SMOKE_PVC_NAME}' did not receive a StorageClass from the cluster default"
  exit 1
fi
if [[ "${SMOKE_STORAGE_CLASS_NAME}" != "${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}" ]]; then
  log_fail "Smoke test PVC '${FILESYSTEM_SMOKE_PVC_NAME}' used StorageClass '${SMOKE_STORAGE_CLASS_NAME}', expected '${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}'"
  exit 1
fi
log_info "PVC '${FILESYSTEM_SMOKE_PVC_NAME}' was assigned StorageClass '${SMOKE_STORAGE_CLASS_NAME}'"
log_pass "Smoke test PVC '${FILESYSTEM_SMOKE_PVC_NAME}' inherited the expected default StorageClass"

log_step "Waiting for the smoke test pod to become ready"
kubectl wait -n "${TEST_NAMESPACE}" \
  --for=condition=Ready \
  "pod/${FILESYSTEM_SMOKE_POD_NAME}" \
  --timeout=120s
log_info "Pod '${FILESYSTEM_SMOKE_POD_NAME}' is ready"
log_pass "Smoke test pod '${FILESYSTEM_SMOKE_POD_NAME}' reached Ready state"

log_step "Writing and reading a probe file through the mounted volume"
kubectl exec -n "${TEST_NAMESPACE}" "${FILESYSTEM_SMOKE_POD_NAME}" -- sh -lc '
  set -eu
  df -h /data
  echo ok > /data/probe.txt
  ls -l /data
  cat /data/probe.txt
'
log_pass "Pod '${FILESYSTEM_SMOKE_POD_NAME}' successfully wrote and read the probe file on the shared volume"

log_step "Smoke test completed successfully"
log_info "The PVC inherited the cluster default StorageClass and the mounted shared filesystem accepted a write and returned the probe file"
log_pass "Single-pod shared filesystem smoke test confirmed default StorageClass behavior and working storage access"
