#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# File: 05-run-checkpoint-restore-test.sh
# Purpose:
#   Validate that model checkpoints can be written to and restored from the
#   Nebius Shared Filesystem correctly across nodes. Tests both same-node
#   and cross-node restore on ALL GPU nodes in the cluster.
#
# Why We Run This:
#   Writing checkpoints is only half the story. Untested restore is one of
#   the most common silent POC blockers — a routine node failure turns into
#   lost training time when restore hasn't been validated. This script
#   proves the full save-and-restart cycle works on Nebius storage + fabric.
#
# What This Script Tests:
#   1. Write a checkpoint (large tensor) to shared filesystem via PVC
#   2. Restore checkpoint on the SAME node — confirms basic restore works
#   3. Restore checkpoint on EVERY OTHER GPU node — confirms cross-node restore
#      at scale, not just one representative node
#   4. Verify data integrity via MD5 checksum on every restore
#   5. Cleanup
#
# Expected behavior:
#   - Write completes without error
#   - Restored tensor matches original checksum on same node
#   - Restored tensor matches original checksum on every other GPU node
#   - No data corruption detected on any node
#
# Usage:
#   ./05-run-checkpoint-restore-test.sh
#
# Optional Environment Variables:
#   TEST_NAMESPACE     Namespace for test pods. Defaults to 'default'
#   CHECKPOINT_SIZE_GB Size of checkpoint in GB. Defaults to 4
#   GPU_NODE_LABEL     Node label to identify GPU nodes.
#                      Defaults to auto-detect from cluster.
#
# Created By: Adam Sabry (Nebius MSA)
# Version: 2.1.0
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

CHECKPOINT_SIZE_GB="${CHECKPOINT_SIZE_GB:-4}"
CHECKPOINT_PATH="/data/checkpoint"
# Unique per-run suffix so two runs sharing the namespace can't collide on fixed
# resource names or cross-delete each other's pods/PVC during cleanup.
RUN_ID="$$-$(date +%s)"
WRITER_POD="checkpoint-writer-${RUN_ID}"
SAME_NODE_READER_POD="checkpoint-reader-same-${RUN_ID}"
PVC_NAME="checkpoint-restore-pvc-${RUN_ID}"
# part-of matches the sibling scripts, so the shared 04-cleanup also catches these;
# the run label lets this script tear down exactly its own resources.
RUN_LABEL="checkpoint-restore/run=${RUN_ID}"
FAILED=0
NODES_TESTED=0
NODES_PASSED=0

PYTORCH_IMAGE="nvcr.io/nvidia/pytorch:24.01-py3"

# -----------------------------------------------------------------------------
# cleanup — always runs on exit (success, failure, or Ctrl-C) via the trap
# below. Without this, any mid-run failure (set -e) left the PVC and pods
# orphaned in the cluster, which then blocked the next run.
# -----------------------------------------------------------------------------
cleanup() {
  [[ -n "${CLEANED_UP:-}" ]] && return
  CLEANED_UP=1
  log_step "Cleaning up checkpoint test resources"
  # Scoped to THIS run via its unique label — deletes the writer, all readers, and
  # the PVC in one shot without touching any other run's resources. Portable (no
  # xargs -r, which is GNU-only and not available on macOS).
  kubectl delete pod,pvc -n "${TEST_NAMESPACE}" -l "${RUN_LABEL}" \
    --ignore-not-found=true --wait=false --request-timeout=30s 2>/dev/null || true
  log_pass "Checkpoint test resources cleaned up"
}
trap cleanup EXIT

# -----------------------------------------------------------------------------
# wait_for_pod — block until the pod reaches a terminal phase, then return
# 0 for Succeeded and 1 for Failed. Unlike `kubectl wait --for=...Succeeded`,
# this detects Failed immediately instead of blocking the full timeout.
# -----------------------------------------------------------------------------
wait_for_pod() {
  local pod="$1" timeout="${2:-600}" elapsed=0 phase=""
  while [ "$elapsed" -lt "$timeout" ]; do
    phase=$(kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "$pod" \
      -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    case "$phase" in
      Succeeded) return 0 ;;
      Failed)    return 1 ;;
    esac
    sleep 5
    elapsed=$(( elapsed + 5 ))
  done
  log_fail "Timed out after ${timeout}s waiting for pod ${pod} (last phase: ${phase:-unknown})"
  return 1
}

log_step "Starting Checkpoint Write and Restore Validation"
log_info "Namespace: ${TEST_NAMESPACE}"
log_info "Checkpoint size: ${CHECKPOINT_SIZE_GB}GB"
log_info "Checkpoint path: ${CHECKPOINT_PATH}"
log_info "Storage class: ${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}"
log_info "PyTorch image: ${PYTORCH_IMAGE}"

log_step "Checking required local dependencies"
require_command kubectl
log_pass "Required local commands are available"

# =============================================================================
# DETECT GPU NODES DYNAMICALLY
# Only test GPU nodes — CPU nodes don't run training jobs so cross-node
# restore from a CPU node is not a relevant production scenario.
# =============================================================================
log_step "Detecting GPU nodes in cluster"

# Auto-detect GPU instance type from node labels
GPU_INSTANCE_TYPE=$(kubectl get nodes --request-timeout=30s \
  -o jsonpath='{.items[*].metadata.labels.node\.kubernetes\.io/instance-type}' \
  2>/dev/null | tr ' ' '\n' | grep -v "cpu" | sort | uniq -c | sort -rn | head -1 | awk '{print $2}' || echo "")

if [ -z "$GPU_INSTANCE_TYPE" ]; then
  log_fail "No GPU nodes found in cluster. Ensure GPU nodes are Ready with node.kubernetes.io/instance-type label."
  exit 1
fi

log_info "Detected GPU instance type: ${GPU_INSTANCE_TYPE}"

# Get all GPU node names that can actually take work: Ready AND schedulable.
# kubectl -l returns nodes regardless of status, so we filter here — otherwise a
# NotReady node (pod sits Pending until timeout) or a Cordoned node
# (spec.unschedulable=true) would be selected and waste the full wait.
GPU_NODES=()
while IFS= read -r node; do
  [[ -n "$node" ]] && GPU_NODES+=("$node")
done < <(kubectl get nodes -l "node.kubernetes.io/instance-type=${GPU_INSTANCE_TYPE}" \
  --no-headers --request-timeout=30s \
  -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,SCHED:.spec.unschedulable' \
  2>/dev/null | awk '$2=="True" && $3!="true" {print $1}')

if [ "${#GPU_NODES[@]}" -eq 0 ]; then
  log_fail "No Ready GPU nodes found for instance type: ${GPU_INSTANCE_TYPE}"
  exit 1
fi

log_pass "Found ${#GPU_NODES[@]} GPU node(s): ${GPU_NODES[*]}"

# =============================================================================
# STEP 1 — Create PVC
# =============================================================================
log_step "Creating PVC for checkpoint storage"
kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC_NAME}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    checkpoint-restore/run: "${RUN_ID}"
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ${FILESYSTEM_DEFAULT_STORAGE_CLASS_NAME}
  resources:
    requests:
      storage: $((CHECKPOINT_SIZE_GB * 3))Gi
EOF

log_pass "PVC '${PVC_NAME}' created — will bind when writer pod starts"

# =============================================================================
# STEP 2 — Write checkpoint from first GPU node
# =============================================================================
WRITER_NODE="${GPU_NODES[0]}"
log_step "Writing checkpoint from writer pod on node: ${WRITER_NODE}"

kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${WRITER_POD}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    checkpoint-restore/run: "${RUN_ID}"
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${WRITER_NODE}
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: writer
    image: ${PYTORCH_IMAGE}
    command: ["/bin/sh", "-c"]
    args:
    - |
      python3 -c "
      import torch
      import hashlib
      import os
      import time

      size_gb = ${CHECKPOINT_SIZE_GB}
      checkpoint_path = '${CHECKPOINT_PATH}'
      os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)

      print(f'Creating {size_gb}GB checkpoint tensor...', flush=True)
      num_elements = int(size_gb * 1024**3 / 4)
      tensor = torch.randn(num_elements, dtype=torch.float32)

      checksum = hashlib.md5(tensor.numpy().tobytes()).hexdigest()
      print(f'Tensor checksum (pre-write): {checksum}', flush=True)

      print(f'Writing to {checkpoint_path}...', flush=True)
      start = time.time()
      torch.save({'model_state': tensor, 'checksum': checksum, 'step': 1000}, checkpoint_path)
      elapsed = time.time() - start

      file_size = os.path.getsize(checkpoint_path)
      throughput = file_size / elapsed / 1024**3
      print(f'Write complete: {file_size / 1024**3:.2f}GB in {elapsed:.1f}s ({throughput:.2f} GB/s)', flush=True)
      print(f'CHECKSUM:{checksum}', flush=True)
      print('WRITE_COMPLETE', flush=True)
      "
    volumeMounts:
    - name: checkpoint-storage
      mountPath: /data
    resources:
      requests:
        memory: "$((CHECKPOINT_SIZE_GB * 2))Gi"
        cpu: "2"
  volumes:
  - name: checkpoint-storage
    persistentVolumeClaim:
      claimName: ${PVC_NAME}
EOF

log_info "Waiting for writer pod to complete..."
wait_for_pod "${WRITER_POD}" 600 || true

WRITER_LOGS=$(kubectl logs --request-timeout=30s -n "${TEST_NAMESPACE}" "${WRITER_POD}" 2>/dev/null)
echo "$WRITER_LOGS"

if echo "$WRITER_LOGS" | grep -q "WRITE_COMPLETE"; then
  log_pass "Checkpoint written successfully on node: ${WRITER_NODE}"
  WRITE_CHECKSUM=$(echo "$WRITER_LOGS" | grep "CHECKSUM:" | awk -F'CHECKSUM:' '{print $2}' | tr -d ' ')
  log_info "Original checksum: ${WRITE_CHECKSUM}"
else
  log_fail "Checkpoint write failed"
  FAILED=1
  exit 1
fi

# =============================================================================
# STEP 3 — Same-node restore
# =============================================================================
log_step "Restoring checkpoint on SAME node (${WRITER_NODE})"

kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${SAME_NODE_READER_POD}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    checkpoint-restore/run: "${RUN_ID}"
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${WRITER_NODE}
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: reader
    image: ${PYTORCH_IMAGE}
    command: ["/bin/sh", "-c"]
    args:
    - |
      python3 -c "
      import torch, hashlib, os, time
      checkpoint_path = '${CHECKPOINT_PATH}'
      expected_checksum = '${WRITE_CHECKSUM}'

      if not os.path.exists(checkpoint_path):
          print('ERROR: Checkpoint file not found', flush=True)
          exit(1)

      file_size = os.path.getsize(checkpoint_path)
      start = time.time()
      checkpoint = torch.load(checkpoint_path, map_location='cpu')
      elapsed = time.time() - start
      tensor = checkpoint['model_state']
      throughput = file_size / elapsed / 1024**3
      print(f'Restore complete: {file_size / 1024**3:.2f}GB in {elapsed:.1f}s ({throughput:.2f} GB/s)', flush=True)

      restored_checksum = hashlib.md5(tensor.numpy().tobytes()).hexdigest()
      print(f'Restored checksum:  {restored_checksum}', flush=True)
      print(f'Expected checksum:  {expected_checksum}', flush=True)

      if restored_checksum == expected_checksum:
          print('CHECKSUM_MATCH', flush=True)
      else:
          print('CHECKSUM_MISMATCH — DATA CORRUPTION DETECTED', flush=True)
          exit(1)
      "
    volumeMounts:
    - name: checkpoint-storage
      mountPath: /data
    resources:
      requests:
        memory: "$((CHECKPOINT_SIZE_GB * 2))Gi"
        cpu: "2"
  volumes:
  - name: checkpoint-storage
    persistentVolumeClaim:
      claimName: ${PVC_NAME}
EOF

wait_for_pod "${SAME_NODE_READER_POD}" 600 || true

SAME_NODE_LOGS=$(kubectl logs --request-timeout=30s -n "${TEST_NAMESPACE}" "${SAME_NODE_READER_POD}" 2>/dev/null)
echo "$SAME_NODE_LOGS"

if echo "$SAME_NODE_LOGS" | grep -q "CHECKSUM_MATCH"; then
  log_pass "Same-node restore: checksum verified — no data corruption"
  NODES_TESTED=$(( NODES_TESTED + 1 ))
  NODES_PASSED=$(( NODES_PASSED + 1 ))
else
  log_fail "Same-node restore: checksum mismatch or restore failed"
  FAILED=1
  NODES_TESTED=$(( NODES_TESTED + 1 ))
  echo "--- Pod status ---"
  kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${SAME_NODE_READER_POD}" 2>/dev/null
  echo "--- Pod events ---"
  kubectl describe pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${SAME_NODE_READER_POD}" 2>/dev/null | grep -A 20 "Events:"
fi

# =============================================================================
# STEP 4 — Cross-node restore on ALL other GPU nodes
# =============================================================================
OTHER_GPU_NODES=()
for NODE in "${GPU_NODES[@]}"; do
  if [ "$NODE" != "$WRITER_NODE" ]; then
    OTHER_GPU_NODES+=("$NODE")
  fi
done

if [ "${#OTHER_GPU_NODES[@]}" -eq 0 ]; then
  log_info "Only one GPU node in cluster — skipping cross-node restore test"
  log_info "Cross-node test requires 2+ GPU nodes"
else
  log_step "Cross-node restore — testing ${#OTHER_GPU_NODES[@]} remaining GPU node(s)"

  NODE_INDEX=0
  for CROSS_NODE in "${OTHER_GPU_NODES[@]}"; do
    NODE_INDEX=$(( NODE_INDEX + 1 ))
    READER_POD="checkpoint-reader-cross-${NODE_INDEX}-${RUN_ID}"

    log_info "Testing node ${NODE_INDEX}/${#OTHER_GPU_NODES[@]}: ${CROSS_NODE}"

    kubectl apply -n "${TEST_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${READER_POD}
  namespace: ${TEST_NAMESPACE}
  labels:
    app.kubernetes.io/part-of: filesystem-csi-validation
    checkpoint-restore/run: "${RUN_ID}"
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${CROSS_NODE}
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  containers:
  - name: reader
    image: ${PYTORCH_IMAGE}
    command: ["/bin/sh", "-c"]
    args:
    - |
      python3 -c "
      import torch, hashlib, os, time
      checkpoint_path = '${CHECKPOINT_PATH}'
      expected_checksum = '${WRITE_CHECKSUM}'

      if not os.path.exists(checkpoint_path):
          print('ERROR: Checkpoint file not found on this node', flush=True)
          exit(1)

      file_size = os.path.getsize(checkpoint_path)
      start = time.time()
      checkpoint = torch.load(checkpoint_path, map_location='cpu')
      elapsed = time.time() - start
      tensor = checkpoint['model_state']
      throughput = file_size / elapsed / 1024**3
      print(f'Cross-node restore: {file_size / 1024**3:.2f}GB in {elapsed:.1f}s ({throughput:.2f} GB/s)', flush=True)

      restored_checksum = hashlib.md5(tensor.numpy().tobytes()).hexdigest()
      print(f'Restored checksum:  {restored_checksum}', flush=True)
      print(f'Expected checksum:  {expected_checksum}', flush=True)

      if restored_checksum == expected_checksum:
          print('CROSS_NODE_CHECKSUM_MATCH', flush=True)
      else:
          print('CROSS_NODE_CHECKSUM_MISMATCH — DATA CORRUPTION', flush=True)
          exit(1)
      "
    volumeMounts:
    - name: checkpoint-storage
      mountPath: /data
    resources:
      requests:
        memory: "$((CHECKPOINT_SIZE_GB * 2))Gi"
        cpu: "2"
  volumes:
  - name: checkpoint-storage
    persistentVolumeClaim:
      claimName: ${PVC_NAME}
EOF

    wait_for_pod "${READER_POD}" 600 || true

    CROSS_LOGS=$(kubectl logs --request-timeout=30s -n "${TEST_NAMESPACE}" "${READER_POD}" 2>/dev/null)
    echo "$CROSS_LOGS"
    NODES_TESTED=$(( NODES_TESTED + 1 ))

    if echo "$CROSS_LOGS" | grep -q "CROSS_NODE_CHECKSUM_MATCH"; then
      log_pass "Node ${NODE_INDEX} (${CROSS_NODE}): checksum verified"
      NODES_PASSED=$(( NODES_PASSED + 1 ))
    else
      log_fail "Node ${NODE_INDEX} (${CROSS_NODE}): checksum mismatch or restore failed"
      FAILED=1
      echo "--- Pod status ---"
      kubectl get pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${READER_POD}" 2>/dev/null
      echo "--- Pod events ---"
      kubectl describe pod --request-timeout=30s -n "${TEST_NAMESPACE}" "${READER_POD}" 2>/dev/null | grep -A 20 "Events:"
      echo "--- Full pod logs ---"
      kubectl logs --request-timeout=30s -n "${TEST_NAMESPACE}" "${READER_POD}" 2>/dev/null || echo "No logs available"
    fi
  done
fi

# =============================================================================
# STEP 5 — Cleanup is handled by the cleanup() trap on EXIT, so resources are
# removed even if the script fails partway through.
# =============================================================================

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
log_step "Checkpoint Write and Restore Summary"
log_info "GPU nodes tested:  ${NODES_TESTED}"
log_info "GPU nodes passed:  ${NODES_PASSED}"
log_info "GPU nodes failed:  $(( NODES_TESTED - NODES_PASSED ))"
log_info "GPU instance type: ${GPU_INSTANCE_TYPE}"
log_info "Checkpoint size:   ${CHECKPOINT_SIZE_GB}GB"
log_info "Writer node:       ${WRITER_NODE}"
echo ""

if [ "${FAILED}" -eq 0 ]; then
  log_step "Checkpoint write and restore validation completed successfully"
  log_pass "Checkpoint written and restored with correct data integrity on all ${NODES_TESTED} GPU node(s)"
  log_pass "Shared filesystem is consistent across all GPU nodes in the cluster"
else
  log_step "Checkpoint write and restore validation completed with failures"
  log_fail "${NODES_PASSED}/${NODES_TESTED} nodes passed — review output above for details"
fi

exit "${FAILED}"