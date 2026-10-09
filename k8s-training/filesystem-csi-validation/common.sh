#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: common.sh
# Purpose:
#   Provide shared configuration and helper functions for the Nebius Shared
#   Filesystem CSI validation workflow so the scripts behave consistently across
#   environments.
#
# Why We Run This:
#   Reusing the same namespace, mount path, naming, and state logic across all
#   validation scripts reduces accidental drift and keeps the workflow easier
#   to maintain.
#
# Reference Docs:
#   https://docs.nebius.com/kubernetes/storage/filesystem-over-csi
#
# Created By: Aaron Fagan
# Created On: 2026-03-17
# Version: 0.1.0
# -----------------------------------------------------------------------------

COMMON_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FILESYSTEM_MOUNT_TEMPLATE="${COMMON_DIR}/../../modules/cloud-init/k8s-cloud-init.tftpl"
STATE_DIR="${STATE_DIR:-${COMMON_DIR}/.state}"

FILESYSTEM_SMOKE_MANIFEST_PATH="${COMMON_DIR}/manifests/01-csi-smoke-test.yaml"
FILESYSTEM_RWX_MANIFEST_PATH="${COMMON_DIR}/manifests/02-csi-rwx-cross-node.yaml"

FILESYSTEM_SMOKE_PVC_NAME="filesystem-csi-smoke-pvc"
FILESYSTEM_SMOKE_POD_NAME="filesystem-csi-smoke-pod"
FILESYSTEM_RWX_PVC_NAME="filesystem-csi-rwx-pvc"
FILESYSTEM_RWX_WRITER_POD_NAME="filesystem-csi-rwx-writer"
FILESYSTEM_RWX_READER_POD_NAME="filesystem-csi-rwx-reader"
FILESYSTEM_VALIDATION_LABEL_SELECTOR="app.kubernetes.io/part-of=filesystem-csi-validation"
FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME="${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME:-csi-mounted-fs-path-sc}"

DEBUG_POD_RECORD_FILE="${DEBUG_POD_RECORD_FILE:-${STATE_DIR}/verify-node-debugger-pods.txt}"

default_mount_point() {
  if [[ -f "${FILESYSTEM_MOUNT_TEMPLATE}" ]]; then
    awk -F'[][]' '
      /virtiofs/ && $2 != "" {
        count = split($2, fields, ",")
        if (count >= 2) {
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", fields[2])
          print fields[2]
          exit
        }
      }
    ' "${FILESYSTEM_MOUNT_TEMPLATE}"
  fi
}

default_namespace() {
  kubectl config view --minify --output 'jsonpath={..namespace}' 2>/dev/null || true
}

ensure_state_dir() {
  mkdir -p "${STATE_DIR}"
}

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    log_fail "Required command '${command_name}' is not available in PATH"
    exit 1
  fi
}

log_step() {
  echo
  echo "==> $*"
}

log_info() {
  echo "  - $*"
}

log_pass() {
  echo "[result] PASS: $*"
}

log_fail() {
  echo "[result] FAIL: $*" >&2
}

MOUNT_POINT="${MOUNT_POINT:-$(default_mount_point)}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/data}"

TEST_NAMESPACE="${TEST_NAMESPACE:-$(default_namespace)}"
TEST_NAMESPACE="${TEST_NAMESPACE:-default}"
