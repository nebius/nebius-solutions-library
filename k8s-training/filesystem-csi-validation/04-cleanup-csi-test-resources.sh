#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 04-cleanup-csi-test-resources.sh
# Purpose:
#   Remove only the temporary validation resources created by the Nebius Shared
#   Filesystem CSI test workflow after testing is complete.
#
# Why We Run This:
#   The smoke and RWX tests are disposable validation resources. Cleaning them
#   up keeps the cluster tidy while leaving the CSI driver and host mounts in
#   place for ongoing cluster use.
#
# Reference Docs:
#   https://docs.nebius.com/kubernetes/storage/filesystem-over-csi
#
# What This Script Does:
#   - Removes the known test files written by the validation workflow
#   - Deletes the cross-node RWX validation manifest resources
#   - Deletes the single-pod smoke test manifest resources
#   - Deletes any PVs still associated with the test PVCs
#   - Deletes any remaining labeled validation pods and PVCs as a cleanup
#     fallback
#   - Deletes leftover node-debugger pods created during node verification
#     even if they finished in an error state
#   - Ignores already-deleted resources so reruns stay safe
#
# What This Script Does Not Do:
#   - It does not uninstall the CSI Helm release
#   - It does not delete the CSI StorageClass
#   - It does not destroy the Terraform-managed cluster, node groups, or shared
#     filesystem
#   - It does not remove the host-level shared filesystem mount configured on
#     nodes
#
# Usage:
#   ./04-cleanup-csi-test-resources.sh
#
# Optional Environment Variables:
#   TEST_NAMESPACE  Namespace where the validation resources were created.
#                   Defaults to the current kubectl namespace or default.
#
# Created By: Aaron Fagan
# Created On: 2026-03-17
# Version: 0.1.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

best_effort_exec() {
  local pod_name="$1"
  local command="$2"

  if kubectl get pod "${pod_name}" -n "${TEST_NAMESPACE}" >/dev/null 2>&1; then
    kubectl exec -n "${TEST_NAMESPACE}" "${pod_name}" -- sh -lc "${command}" || true
  fi
}

log_step "Starting Nebius Shared Filesystem CSI validation cleanup"
log_info "Namespace: ${TEST_NAMESPACE}"
log_info "Smoke test pod: ${FILESYSTEM_SMOKE_POD_NAME}"
log_info "RWX writer pod: ${FILESYSTEM_RWX_WRITER_POD_NAME}"
log_info "RWX reader pod: ${FILESYSTEM_RWX_READER_POD_NAME}"
log_info "Validation label selector: ${FILESYSTEM_VALIDATION_LABEL_SELECTOR}"

log_step "Checking required local dependencies"
require_command kubectl
log_pass "Required local commands for cleanup are available"

log_step "Removing validation test files from mounted volumes"
best_effort_exec "${FILESYSTEM_SMOKE_POD_NAME}" 'rm -f /data/probe.txt'
best_effort_exec "${FILESYSTEM_RWX_WRITER_POD_NAME}" 'rm -f /data/shared.txt'
best_effort_exec "${FILESYSTEM_RWX_READER_POD_NAME}" 'rm -f /data/shared.txt'
log_pass "Validation probe files were removed or already absent"

log_step "Deleting the validation manifests"
kubectl delete -n "${TEST_NAMESPACE}" -f "${FILESYSTEM_RWX_MANIFEST_PATH}" --ignore-not-found=true || true
kubectl delete -n "${TEST_NAMESPACE}" -f "${FILESYSTEM_SMOKE_MANIFEST_PATH}" --ignore-not-found=true || true
log_pass "Validation manifests were deleted or already absent"

log_step "Deleting any remaining validation pods and PVCs"
kubectl delete pod -n "${TEST_NAMESPACE}" \
  "${FILESYSTEM_SMOKE_POD_NAME}" \
  "${FILESYSTEM_RWX_WRITER_POD_NAME}" \
  "${FILESYSTEM_RWX_READER_POD_NAME}" \
  --ignore-not-found=true || true
kubectl delete pvc -n "${TEST_NAMESPACE}" \
  "${FILESYSTEM_SMOKE_PVC_NAME}" \
  "${FILESYSTEM_RWX_PVC_NAME}" \
  --ignore-not-found=true || true
log_pass "Validation pods and PVCs were deleted or already absent"

log_step "Deleting any remaining labeled validation pods and PVCs as a fallback"
kubectl delete pod,pvc -n "${TEST_NAMESPACE}" \
  -l "${FILESYSTEM_VALIDATION_LABEL_SELECTOR}" \
  --ignore-not-found=true || true
log_pass "Labeled validation pods and PVCs were deleted or already absent"

log_step "Deleting any PVs still associated with the validation PVCs"
while read -r pv_name claim_namespace claim_name; do
  [[ -z "${pv_name}" ]] && continue
  if [[ "${claim_namespace}" == "${TEST_NAMESPACE}" ]] && \
     [[ "${claim_name}" == "${FILESYSTEM_SMOKE_PVC_NAME}" || "${claim_name}" == "${FILESYSTEM_RWX_PVC_NAME}" ]]; then
    kubectl delete pv "${pv_name}" --ignore-not-found=true || true
  fi
done < <(
  kubectl get pv -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.claimRef.namespace}{" "}{.spec.claimRef.name}{"\n"}{end}' \
    2>/dev/null || true
)
log_pass "Validation PV cleanup completed"

log_step "Deleting recorded node debugger pods from the verification step"
if [[ -f "${DEBUG_POD_RECORD_FILE}" ]]; then
  while read -r namespace pod_name; do
    [[ -z "${namespace}" || -z "${pod_name}" ]] && continue
    kubectl delete pod -n "${namespace}" "${pod_name}" --ignore-not-found=true || true
  done < "${DEBUG_POD_RECORD_FILE}"
  rm -f "${DEBUG_POD_RECORD_FILE}"
  log_pass "Recorded node debugger pods were deleted or already absent"
else
  log_info "No recorded node debugger pods found at ${DEBUG_POD_RECORD_FILE}; skipping"
  log_pass "No recorded node debugger pods required cleanup"
fi

log_step "Deleting any remaining node debugger pods in the test namespace as a fallback"
while read -r pod_name phase; do
  [[ -z "${pod_name}" ]] && continue
  if [[ "${pod_name}" == node-debugger-* ]]; then
    kubectl delete pod -n "${TEST_NAMESPACE}" "${pod_name}" --ignore-not-found=true || true
  fi
done < <(
  kubectl get pods -n "${TEST_NAMESPACE}" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' \
    2>/dev/null || true
)
log_pass "Node debugger pod fallback cleanup finished for namespace '${TEST_NAMESPACE}'"

log_step "Validation cleanup completed"
log_info "CSI driver, StorageClass, cluster, and host mounts were left in place"
log_pass "Validation cleanup finished without removing the installed CSI platform components"
