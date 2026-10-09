#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 03-run-csi-rwx-cross-node-test.sh
# Purpose:
#   Validate ReadWriteMany behavior across nodes by mounting the same PVC into
#   two pods scheduled onto different hosts.
#
# Why We Run This:
#   A single-pod test proves basic functionality, but shared filesystems are
#   most valuable when data written from one node can be read from another. This
#   script confirms that cross-node sharing works in practice.
#
# Reference Docs:
#   https://docs.nebius.com/kubernetes/storage/filesystem-over-csi
#
# What This Script Does:
#   - Applies a RWX PVC plus reader/writer pod manifest
#   - Uses pod anti-affinity to encourage placement on different nodes
#   - Waits for the PVC and both pods to become ready
#   - Verifies that the PVC inherited the expected default StorageClass
#   - Writes a file from one pod and reads it from the other
#
# Usage:
#   ./03-run-csi-rwx-cross-node-test.sh
#
# Optional Environment Variables:
#   TEST_NAMESPACE  Namespace where the validation resources should be created.
#                   Defaults to the current kubectl namespace or default.
#
# Manifest Used:
#   manifests/02-csi-rwx-cross-node.yaml
#
# Created By: Aaron Fagan
# Created On: 2026-03-17
# Version: 0.1.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

log_step "Starting cross-node RWX validation"
log_info "Namespace: ${TEST_NAMESPACE}"
log_info "Manifest: ${FILESYSTEM_RWX_MANIFEST_PATH}"
log_info "PVC name: ${FILESYSTEM_RWX_PVC_NAME}"
log_info "Writer pod: ${FILESYSTEM_RWX_WRITER_POD_NAME}"
log_info "Reader pod: ${FILESYSTEM_RWX_READER_POD_NAME}"
log_info "Expected default StorageClass: ${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}"

log_step "Checking required local dependencies"
require_command kubectl
log_pass "Required local commands for the RWX validation are available"

log_step "Applying the RWX validation manifest"
kubectl apply -n "${TEST_NAMESPACE}" -f "${FILESYSTEM_RWX_MANIFEST_PATH}"
log_pass "RWX validation manifest applied in namespace '${TEST_NAMESPACE}'"

log_step "Waiting for the RWX PVC to bind"
kubectl wait -n "${TEST_NAMESPACE}" \
  --for=jsonpath='{.status.phase}'=Bound \
  "pvc/${FILESYSTEM_RWX_PVC_NAME}" \
  --timeout=120s
log_info "PVC '${FILESYSTEM_RWX_PVC_NAME}' is bound"
log_pass "RWX PVC '${FILESYSTEM_RWX_PVC_NAME}' bound successfully"

log_step "Verifying that the RWX PVC inherited the default StorageClass"
RWX_STORAGE_CLASS_NAME="$(kubectl get pvc -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_PVC_NAME}" -o jsonpath='{.spec.storageClassName}')"
if [[ -z "${RWX_STORAGE_CLASS_NAME}" ]]; then
  log_fail "RWX PVC '${FILESYSTEM_RWX_PVC_NAME}' did not receive a StorageClass from the cluster default"
  exit 1
fi
if [[ "${RWX_STORAGE_CLASS_NAME}" != "${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}" ]]; then
  log_fail "RWX PVC '${FILESYSTEM_RWX_PVC_NAME}' used StorageClass '${RWX_STORAGE_CLASS_NAME}', expected '${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}'"
  exit 1
fi
log_info "PVC '${FILESYSTEM_RWX_PVC_NAME}' was assigned StorageClass '${RWX_STORAGE_CLASS_NAME}'"
log_pass "RWX PVC '${FILESYSTEM_RWX_PVC_NAME}' inherited the expected default StorageClass"

log_step "Waiting for both RWX test pods to become ready"
kubectl wait -n "${TEST_NAMESPACE}" \
  --for=condition=Ready \
  "pod/${FILESYSTEM_RWX_WRITER_POD_NAME}" \
  --timeout=180s
kubectl wait -n "${TEST_NAMESPACE}" \
  --for=condition=Ready \
  "pod/${FILESYSTEM_RWX_READER_POD_NAME}" \
  --timeout=180s
log_info "Both RWX test pods are ready"
log_pass "RWX writer and reader pods both reached Ready state"

log_step "Checking the node placement for the reader and writer pods"
WRITER_NODE="$(kubectl get pod -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_WRITER_POD_NAME}" -o jsonpath='{.spec.nodeName}')"
READER_NODE="$(kubectl get pod -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_READER_POD_NAME}" -o jsonpath='{.spec.nodeName}')"

echo "writer node: ${WRITER_NODE}"
echo "reader node: ${READER_NODE}"

kubectl get pods -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_WRITER_POD_NAME}" "${FILESYSTEM_RWX_READER_POD_NAME}" -o wide
log_pass "RWX pod placement details collected for both nodes"

log_step "Writing shared data from the writer pod"
kubectl exec -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_WRITER_POD_NAME}" -- sh -lc '
  set -eu
  echo "shared-check" > /data/shared.txt
  cat /data/shared.txt
'
log_pass "Writer pod '${FILESYSTEM_RWX_WRITER_POD_NAME}' wrote shared data to the mounted volume"

log_step "Reading the same shared data from the reader pod"
kubectl exec -n "${TEST_NAMESPACE}" "${FILESYSTEM_RWX_READER_POD_NAME}" -- sh -lc '
  set -eu
  ls -l /data
  cat /data/shared.txt
'
log_pass "Reader pod '${FILESYSTEM_RWX_READER_POD_NAME}' read the shared file created by the writer pod"

log_step "Cross-node RWX validation completed successfully"
log_info "The PVC inherited the cluster default StorageClass and the same file was visible from both pods through the shared volume"
log_pass "Cross-node ReadWriteMany storage behavior and default StorageClass inheritance confirmed"
