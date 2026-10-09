#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 01-verify-node-filesystem-mounts.sh
# Purpose:
#   Verify that the Nebius Shared Filesystem is mounted on every Kubernetes
#   node at the expected host path before any pod-level storage testing begins.
#
# Why We Run This:
#   The Nebius CSI workflow in this repo depends on the shared filesystem
#   already being attached and mounted on each node. If a node is missing the
#   host mount, later PVC or pod checks can fail in ways that are harder to
#   diagnose.
#
# Reference Docs:
#   https://docs.nebius.com/kubernetes/storage/filesystem-over-csi
#
# Repo Sources of Truth:
#   - ../../modules/cloud-init/k8s-cloud-init.tftpl
#   - ../main.tf
#
# What This Script Checks:
#   - The mount exists at /mnt/data (or the value of MOUNT_POINT)
#   - The mount is present in /etc/fstab
#   - The mounted filesystem reports capacity via df
#   - The target directory exists on the host
#
# Usage:
#   ./01-verify-node-filesystem-mounts.sh
#
# Optional Environment Variables:
#   TEST_NAMESPACE  Namespace used for the temporary node-debugger pods.
#                   Defaults to the current kubectl namespace or default.
#   MOUNT_POINT     Host path to validate. Defaults to the Terraform mount.
#   DEBUG_IMAGE     Image used by kubectl debug. Defaults to ubuntu.
#   VERIFY_ALL_NODES  When true, validates every node in the cluster. Defaults
#                     to false.
#   TARGET_NODE       Specific node to validate. Accepts either
#                     node/<name> or <name>. Overrides VERIFY_ALL_NODES.
#
# Created By: Aaron Fagan
# Created On: 2026-03-17
# Version: 0.1.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

DEBUG_IMAGE="${DEBUG_IMAGE:-ubuntu}"
VERIFY_ALL_NODES="${VERIFY_ALL_NODES:-false}"
TARGET_NODE="${TARGET_NODE:-}"
FAILED=0

normalize_node_name() {
  local node_name="$1"
  if [[ "${node_name}" == node/* ]]; then
    printf '%s\n' "${node_name}"
  else
    printf 'node/%s\n' "${node_name}"
  fi
}

log_step "Starting Nebius Shared Filesystem mount verification"
log_info "Namespace for temporary debug pods: ${TEST_NAMESPACE}"
log_info "Expected mount point: ${MOUNT_POINT}"
log_info "Debug image: ${DEBUG_IMAGE}"

log_step "Checking required local dependencies"
require_command kubectl
require_command awk
require_command mktemp
log_pass "Required local commands for node mount verification are available"

log_step "Preparing local state for debugger pod cleanup"
ensure_state_dir
touch "${DEBUG_POD_RECORD_FILE}"
log_info "Debugger pod record file: ${DEBUG_POD_RECORD_FILE}"
log_info "New debugger pods from this run will be appended for later cleanup"

log_step "Selecting which nodes to validate"
ALL_NODES=()
while IFS= read -r node; do
  [[ -n "${node}" ]] && ALL_NODES+=("${node}")
done < <(kubectl get nodes -o name)

if [[ "${#ALL_NODES[@]}" -eq 0 ]]; then
  log_fail "No Kubernetes nodes were returned by kubectl"
  exit 1
fi

if [[ -n "${TARGET_NODE}" ]]; then
  TARGET_NODE="$(normalize_node_name "${TARGET_NODE}")"
  NODES_TO_CHECK=("${TARGET_NODE}")
  log_info "Using explicitly requested node: ${TARGET_NODE}"
elif [[ "${VERIFY_ALL_NODES}" == "true" ]]; then
  NODES_TO_CHECK=("${ALL_NODES[@]}")
  log_info "VERIFY_ALL_NODES=true, so every node will be checked"
else
  NODES_TO_CHECK=("${ALL_NODES[0]}")
  log_info "Defaulting to a single-node validation using: ${NODES_TO_CHECK[0]}"
fi

log_pass "Selected ${#NODES_TO_CHECK[@]} node(s) for shared filesystem mount validation"

log_step "Checking Nebius Shared Filesystem mounts on the selected Kubernetes nodes"
for node in "${NODES_TO_CHECK[@]}"; do
  echo
  echo "------------------------------------------------------------"
  echo "=== ${node} ==="
  output_file="$(mktemp)"
  if ! kubectl debug -n "${TEST_NAMESPACE}" "${node}" \
    --attach=true \
    --quiet \
    --image="${DEBUG_IMAGE}" \
    --profile=sysadmin -- \
    chroot /host sh -lc "
      set -eu
      echo '[check] Verifying that the Nebius Shared Filesystem is actively mounted at ${MOUNT_POINT}'
      mount | awk '\$3 == \"${MOUNT_POINT}\" { print; found=1 } END { exit found ? 0 : 1 }'
      echo '[check] Verifying that the mount is persisted in /etc/fstab for node reboot safety'
      awk '\$2 == \"${MOUNT_POINT}\" { print; found=1 } END { exit found ? 0 : 1 }' /etc/fstab
      echo '[check] Verifying that the mounted filesystem reports capacity and is readable'
      df -h ${MOUNT_POINT}
      echo '[check] Verifying that the target directory exists on the host'
      test -d ${MOUNT_POINT}
      echo '[result] PASS: shared filesystem host mount is active and healthy at ${MOUNT_POINT} on this node'
    " 2>&1 | tee "${output_file}"; then
    FAILED=1
    echo "[result] FAIL: ${node} does not have a healthy shared filesystem mount at ${MOUNT_POINT}" >&2
  fi

  debug_pod_name="$(awk '/Creating debugging pod / { print $4 }' "${output_file}" | tail -n 1)"
  if [[ -n "${debug_pod_name}" ]]; then
    printf '%s %s\n' "${TEST_NAMESPACE}" "${debug_pod_name}" >> "${DEBUG_POD_RECORD_FILE}"
  fi
  rm -f "${output_file}"
done

if [[ "${FAILED}" -eq 0 ]]; then
  log_step "Shared filesystem mount verification completed successfully"
  log_info "All checked nodes reported a healthy mount at ${MOUNT_POINT}"
else
  log_step "Shared filesystem mount verification completed with failures"
  log_info "Review the node output above for the failing mount checks"
fi

exit "${FAILED}"
