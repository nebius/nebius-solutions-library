#!/bin/bash
# =============================================================================
# GPU Soak Test Runner (PyTorchJob / torch.distributed)
# Equivalent to cloudmeter auto-loader for MK8s clusters.
# Runs sustained GPU compute + HBM fill + NCCL all_reduce (IB/NVLink) stress
# for a configurable duration, then emits a structured pass/fail report.
#
# Usage:
#   ./run-soak-test.sh [duration_seconds] [gpu_node_count]
#
# Examples:
#   ./run-soak-test.sh              # 1 hour, 2 nodes (defaults)
#   ./run-soak-test.sh 7200 2       # 2 hours, 2 nodes
#   ./run-soak-test.sh 3600 4       # 1 hour, 4 nodes
#
# Environment overrides:
#   SOAK_IMAGE          Container image (default is a Hopper/H100 CUDA 12.3 image;
#                       set a CUDA 12.6+ arm64 image for Blackwell B200/GB200/GB300)
#   HBM_FILL_FRACTION   Fraction of HBM to fill per GPU (default 0.75)
#   MAX_TEMP            Overtemp threshold in °C (default 83; raise for Blackwell)
#   AUTO_CLEANUP=y      Delete the namespace at the end without prompting
#
# What it tests (equivalent to auto-loader):
#   - Sustained GPU utilization across all GPU nodes
#   - Configurable HBM fill (dynamically sized to GPU type)
#   - InfiniBand/NVLink stress via repeated NCCL all_reduce (verified + busbw)
#   - GPU temperature monitoring (fail if > 83°C)
#   - XID error detection (best-effort; flagged UNVERIFIED if unavailable)
#   - Node health monitoring
#   - Structured pass/fail output
# =============================================================================
set -euo pipefail

DURATION="${1:-3600}"
# Exported so monitor.sh can bound its poll loop to the soak duration (+ buffer).
export SOAK_DURATION_SECONDS="$DURATION"
NODE_COUNT="${2:-2}"
NAMESPACE="gpu-soak"
# GPUs per node (nproc_per_node + GPU resource request). Auto-detected from node
# allocatable below once the GPU node type is known — hardcoding 8 breaks any
# non-8-GPU node (e.g. GB200/GB300). Set SLOTS=<n> to override detection.
SLOTS="${SLOTS:-}"
HBM_FILL_FRACTION="${HBM_FILL_FRACTION:-0.75}"
# Overtemp threshold °C. Exported so monitor.sh and the report agree on one value
# (raise for Blackwell, e.g. MAX_TEMP=90). Default matches monitor.sh's default.
export MAX_TEMP="${MAX_TEMP:-83}"
# Container image for the workload. The default is a Hopper-era PyTorch (CUDA
# 12.3) that works on H100/H200. It will NOT run on Blackwell (B200/GB200/GB300)
# — those need a newer CUDA 12.6+ image, and Grace-based GB200/GB300 also need an
# arm64/sbsa image. Override per platform, e.g.:
#   SOAK_IMAGE=nvcr.io/nvidia/pytorch:<blackwell-arm64-tag> ./run-soak-test.sh ...
SOAK_IMAGE="${SOAK_IMAGE:-nvcr.io/nvidia/pytorch:24.01-py3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPORT_FILE="soak-report-$(date +%Y%m%d_%H%M%S).txt"
START_TIME=$(date -u)
START_EPOCH=$(date +%s)
# Anchor the monitor log to this script's dir (not the caller's CWD) so the report
# generator below reliably finds it no matter where the script was invoked from.
# Also export the run start so monitor.sh can scope its dmesg XID check to this run.
export SOAK_LOG_DIR="$SCRIPT_DIR"
export SOAK_START_EPOCH="$START_EPOCH"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Delete the test namespace. Guarded so it runs at most once.
CLEANED=0
cleanup_ns() {
  [ "$CLEANED" = "1" ] && return
  CLEANED=1
  echo ""
  echo "Cleaning up namespace $NAMESPACE ..."
  kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --wait=false 2>/dev/null || true
}
# On Ctrl-C / termination, tear down so an aborted run never leaks GPU pods.
trap 'cleanup_ns; exit 130' INT TERM
# On an unexpected error-exit (set -e) BEFORE we reach the normal cleanup
# decision, also tear down — otherwise a mid-setup failure leaks the namespace.
# REACHED_END is set to 1 once the run completes and the explicit cleanup
# decision (prompt / AUTO_CLEANUP / leave-in-place) takes over, so this trap
# never overrides that deliberate choice on a normal finish.
REACHED_END=0
trap '[ "$REACHED_END" = "1" ] || cleanup_ns' EXIT

echo "=== GPU Soak Test (PyTorchJob / torch.distributed) ==="
echo "Duration:     ${DURATION}s ($(( DURATION / 60 )) minutes)"
echo "GPU nodes:    $NODE_COUNT"
echo "HBM fill:     $(awk "BEGIN{printf \"%.0f\", ${HBM_FILL_FRACTION}*100}")%"
echo "Max temp:     ${MAX_TEMP}°C"
echo "Image:        $SOAK_IMAGE"
echo "Namespace:    $NAMESPACE"
echo "Started:      $START_TIME"
echo ""

# Verify the Training Operator (PyTorchJob CRD) is installed
if ! kubectl get crd pytorchjobs.kubeflow.org &>/dev/null; then
  echo -e "${RED}ERROR: Kubeflow Training Operator (PyTorchJob) not installed${NC}"
  echo "Install it with:"
  echo "  kubectl apply -k 'github.com/kubeflow/training-operator/manifests/overlays/standalone?ref=v1.7.0'"
  exit 1
fi

# =============================================================================
# DYNAMIC GPU NODE DETECTION
# =============================================================================
echo "Detecting GPU node type in cluster..."

GPU_INSTANCE_TYPE=$(kubectl get nodes \
  -o jsonpath='{.items[*].metadata.labels.node\.kubernetes\.io/instance-type}' \
  2>/dev/null | tr ' ' '\n' | grep -v "cpu" | sort | uniq -c | sort -rn | head -1 | awk '{print $2}' || echo "")

if [ -z "$GPU_INSTANCE_TYPE" ]; then
  echo -e "${RED}ERROR: No GPU nodes found in cluster${NC}"
  echo "Ensure GPU nodes are Ready and have the node.kubernetes.io/instance-type label"
  exit 1
fi

echo "Detected GPU type: $GPU_INSTANCE_TYPE"

case "$GPU_INSTANCE_TYPE" in
  gpu-h100-sxm)  GPU_NAME="H100 SXM (80GB HBM3)" ;;
  gpu-h200-sxm)  GPU_NAME="H200 SXM (141GB HBM3e)" ;;
  gpu-b200-sxm)  GPU_NAME="B200 SXM (192GB HBM3e)" ;;
  gpu-b300-sxm)  GPU_NAME="B300 SXM" ;;
  *)             GPU_NAME="$GPU_INSTANCE_TYPE" ;;
esac

echo "GPU name: $GPU_NAME"

# Verify enough Ready GPU nodes are available.
# NOTE: `grep -c Ready` is wrong — "NotReady" contains the substring "Ready",
# so it would count degraded nodes as available. Match the STATUS column exactly.
AVAILABLE_GPU_NODES=$(kubectl get nodes -l "node.kubernetes.io/instance-type=${GPU_INSTANCE_TYPE}" \
  --no-headers 2>/dev/null | awk '$2=="Ready"' | wc -l | tr -d ' ')

if [ "$AVAILABLE_GPU_NODES" -lt "$NODE_COUNT" ]; then
  echo -e "${RED}ERROR: Requested $NODE_COUNT GPU nodes but only $AVAILABLE_GPU_NODES available${NC}"
  exit 1
fi

echo "GPU nodes available: $AVAILABLE_GPU_NODES ✓"

# Detect GPUs per node from a Ready node's allocatable, so the job is sized to the
# actual hardware (GB200/GB300 are not 8-GPU). Falls back to 8 only if unreadable.
FIRST_GPU_NODE=$(kubectl get nodes -l "node.kubernetes.io/instance-type=${GPU_INSTANCE_TYPE}" \
  --no-headers 2>/dev/null | awk '$2=="Ready"{print $1; exit}')
DETECTED_SLOTS=$(kubectl get node "$FIRST_GPU_NODE" \
  -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
SLOTS="${SLOTS:-$DETECTED_SLOTS}"
if ! [ "${SLOTS:-0}" -gt 0 ] 2>/dev/null; then
  echo -e "${YELLOW}WARNING: could not detect GPUs/node from allocatable — defaulting to 8${NC}"
  SLOTS=8
fi
echo "GPUs/node:           $SLOTS"
echo ""

# Single-tenant by design: a soak saturates every GPU on the cluster, so only one
# run at a time makes sense. Claim the namespace ATOMICALLY with `kubectl create`
# — the API server rejects a duplicate, so two runs starting at the same instant
# can't both win the claim. Only reclaim an existing namespace when it's stale (no
# running pods); otherwise another run owns it and we refuse.
if ! kubectl create namespace "$NAMESPACE" 2>/dev/null; then
  RUNNING_PODS=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | awk '$3=="Running"' | wc -l | tr -d ' ')
  if [ "${RUNNING_PODS:-0}" -gt 0 ]; then
    echo -e "${RED}ERROR: namespace $NAMESPACE already has $RUNNING_PODS running pod(s) — another soak run may be in progress.${NC}"
    echo "A soak saturates all cluster GPUs, so only one run at a time is supported."
    echo "If you're sure it's stale, delete it manually: kubectl delete namespace $NAMESPACE"
    exit 1
  fi
  echo "Reclaiming stale $NAMESPACE namespace from a previous run..."
  kubectl delete namespace "$NAMESPACE" --ignore-not-found=true
  kubectl wait --for=delete namespace/"$NAMESPACE" --timeout=120s 2>/dev/null || true
  kubectl create namespace "$NAMESPACE"
fi

# Mount the workload script as a ConfigMap
kubectl create configmap soak-script -n "$NAMESPACE" \
  --from-file=soak.py="$SCRIPT_DIR/scripts/soak.py"

# Pre-pull the (large) container image onto the target GPU nodes before submitting
# the job. The Training Operator injects a worker init container that waits only
# ~200s for the master's DNS; on a cold cluster the first-time image pull can take
# far longer, so the worker gives up (Init:Error) before the master is Ready.
# Priming each node's image cache first makes the real pods start promptly.
echo "Pre-pulling image on GPU nodes (first run on a cold cluster can take several minutes)..."
kubectl apply -f - <<PREPULL >/dev/null
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: soak-prepull
  namespace: $NAMESPACE
spec:
  selector:
    matchLabels:
      app: soak-prepull
  template:
    metadata:
      labels:
        app: soak-prepull
    spec:
      nodeSelector:
        node.kubernetes.io/instance-type: $GPU_INSTANCE_TYPE
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      containers:
      - name: prepull
        image: $SOAK_IMAGE
        command: ["sh", "-c", "echo image-cached; sleep 3600"]
        resources:
          requests:
            cpu: "10m"
            memory: "16Mi"
PREPULL

if kubectl rollout status ds/soak-prepull -n "$NAMESPACE" --timeout=900s 2>/dev/null; then
  echo "Image cached on all target GPU nodes ✓"
else
  echo -e "${YELLOW}WARNING: pre-pull did not finish within 15m — proceeding anyway.${NC}"
  echo "If workers hit Init:Error, the image is likely still pulling; re-run once it's cached."
fi
# The image stays in each node's containerd cache after the DaemonSet is removed.
kubectl delete ds/soak-prepull -n "$NAMESPACE" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true

# Patch the PyTorchJob template. Worker replicas = total nodes - 1 (master is
# rank 0 and also runs GPUs), so total pods == NODE_COUNT.
WORKER_REPLICAS=$(( NODE_COUNT - 1 ))
MANIFEST=$(sed \
  -e "s/__GPU_INSTANCE_TYPE__/${GPU_INSTANCE_TYPE}/g" \
  -e "s/__WORKER_REPLICAS__/${WORKER_REPLICAS}/g" \
  -e "s/__DURATION__/${DURATION}/g" \
  -e "s/__FILL_FRACTION__/${HBM_FILL_FRACTION}/g" \
  -e "s/__SLOTS__/${SLOTS}/g" \
  -e "s|__SOAK_IMAGE__|${SOAK_IMAGE}|g" \
  "$SCRIPT_DIR/templates/pytorchjob.yaml")

echo "$MANIFEST" | kubectl apply -f -

echo ""
echo "PyTorchJob submitted. Waiting for master pod to start..."

kubectl wait --namespace "$NAMESPACE" \
  --for=condition=Ready \
  --timeout=300s \
  -l training.kubeflow.org/replica-type=master \
  pod 2>/dev/null || true

echo "Pods started. Beginning monitoring..."
echo ""
echo "To watch master logs in another terminal:"
echo "  kubectl logs -f -n "$NAMESPACE" -l training.kubeflow.org/replica-type=master"
echo ""

# Run monitor. set +e so a FAILED soak still produces a report and cleans up
# (bash monitor.sh returning non-zero must NOT abort this script under set -e).
set +e
bash "$SCRIPT_DIR/scripts/monitor.sh"
MONITOR_EXIT=$?
set -e

# Collect final master logs (rank 0 prints the summary + BUSBW lines)
# --tail=-1 is required with -l: `kubectl logs` defaults to only the last 10 lines
# per pod when a label selector is used (unlike naming a pod, which returns all),
# which would silently drop the summary/BUSBW lines we parse below.
MASTER_LOGS=$(kubectl logs --request-timeout=30s --tail=-1 -n "$NAMESPACE" -l training.kubeflow.org/replica-type=master 2>/dev/null | tail -40)

END_TIME=$(date -u)
END_EPOCH=$(date +%s)
ACTUAL_DURATION=$(( END_EPOCH - START_EPOCH ))

# =============================================================================
# GENERATE REPORT
# =============================================================================
generate_report() {
  local LOG_FILE
  LOG_FILE=$(ls -t "$SCRIPT_DIR"/soak-monitor-*.log 2>/dev/null | head -1)

  # NOTE: `grep -c` prints "0" AND exits 1 when there are no matches, so a
  # trailing `|| echo 0` would DOUBLE it to "0\n0" and break the "= 0" tests
  # below. Use `|| true` (keeps grep's own "0") and default empties to 0.
  local TOTAL_POLLS OVERTEMP LOW_UTIL XID XID_UNVERIFIED
  TOTAL_POLLS=$(grep -c "Poll #" "$LOG_FILE" 2>/dev/null || true); TOTAL_POLLS=${TOTAL_POLLS:-0}
  OVERTEMP=$(grep -c "OVERTEMP" "$LOG_FILE" 2>/dev/null || true); OVERTEMP=${OVERTEMP:-0}
  LOW_UTIL=$(grep -c "LOW UTIL" "$LOG_FILE" 2>/dev/null || true); LOW_UTIL=${LOW_UTIL:-0}
  XID=$(grep "XID errors detected:" "$LOG_FILE" 2>/dev/null | sed -n 's/.*detected: \([0-9][0-9]*\).*/\1/p' | tail -1 || true); XID=${XID:-0}
  XID_UNVERIFIED=$(grep -c "XID_UNVERIFIED" "$LOG_FILE" 2>/dev/null || true); XID_UNVERIFIED=${XID_UNVERIFIED:-0}

  # Portable extraction (sed, not grep -oP which is GNU-only / breaks on macOS).
  local PEAK_TEMP MAX_UTIL MAX_POWER AVG_UTIL AVG_TEMP
  PEAK_TEMP=$(sed -n 's/.*temp=\([0-9][0-9]*\).*/\1/p' "$LOG_FILE" 2>/dev/null | sort -n | tail -1); PEAK_TEMP=${PEAK_TEMP:-N/A}
  MAX_UTIL=$(sed -n 's/.*util=\([0-9][0-9]*\).*/\1/p' "$LOG_FILE" 2>/dev/null | sort -n | tail -1); MAX_UTIL=${MAX_UTIL:-N/A}
  MAX_POWER=$(sed -n 's/.*power=\([0-9][0-9.]*\).*/\1/p' "$LOG_FILE" 2>/dev/null | sort -n | tail -1); MAX_POWER=${MAX_POWER:-N/A}
  AVG_UTIL=$(sed -n 's/.*util=\([0-9][0-9]*\).*/\1/p' "$LOG_FILE" 2>/dev/null | awk '{s+=$1;c++} END {if(c)printf "%.0f", s/c; else print "N/A"}')
  AVG_TEMP=$(sed -n 's/.*temp=\([0-9][0-9]*\).*/\1/p' "$LOG_FILE" 2>/dev/null | awk '{s+=$1;c++} END {if(c)printf "%.0f", s/c; else print "N/A"}')

  # NCCL evidence straight from the master (rank 0) logs
  local PEAK_BUSBW AVG_BUSBW ITERS FAILED_ITERS
  PEAK_BUSBW=$(echo "$MASTER_LOGS" | sed -n 's/.*BUSBW_GBPS: \([0-9][0-9.]*\).*/\1/p' | tail -1); PEAK_BUSBW=${PEAK_BUSBW:-N/A}
  AVG_BUSBW=$(echo "$MASTER_LOGS" | sed -n 's/.*BUSBW_AVG_GBPS: \([0-9][0-9.]*\).*/\1/p' | tail -1); AVG_BUSBW=${AVG_BUSBW:-N/A}
  ITERS=$(echo "$MASTER_LOGS" | sed -n 's/.*Total iterations: \([0-9][0-9]*\).*/\1/p' | tail -1); ITERS=${ITERS:-N/A}
  FAILED_ITERS=$(echo "$MASTER_LOGS" | sed -n 's/.*Failed iterations (all ranks): \([0-9][0-9]*\).*/\1/p' | tail -1); FAILED_ITERS=${FAILED_ITERS:-N/A}

  # A run with no valid NCCL bandwidth means the collective produced nothing —
  # treat that as a failure even if the master pod happened to exit 0, so an
  # empty result never reads as a green PASS.
  local NCCL_OK=1
  if [ "$PEAK_BUSBW" = "N/A" ] || [ -z "$PEAK_BUSBW" ]; then NCCL_OK=0; fi

  local RESULT="PASSED"
  if [ "$MONITOR_EXIT" != "0" ] || [ "$NCCL_OK" = "0" ]; then RESULT="FAILED"; fi

  cat > "$SCRIPT_DIR/$REPORT_FILE" << REPORT
================================================================================
  GPU SOAK TEST REPORT
================================================================================

  Cluster:        Nebius MK8s
  GPU Type:       $GPU_NAME
  GPU Instance:   $GPU_INSTANCE_TYPE
  GPU Nodes:      $NODE_COUNT
  Total GPUs:     $(( NODE_COUNT * SLOTS ))
  Orchestration:  PyTorchJob + torchrun (torch.distributed, NCCL)
  Duration:       ${DURATION}s ($(( DURATION / 60 )) minutes)
  Actual runtime: ${ACTUAL_DURATION}s ($(( ACTUAL_DURATION / 60 )) minutes)

  Started:        $START_TIME
  Finished:       $END_TIME

================================================================================
  GPU PERFORMANCE
================================================================================

  Peak temperature:     ${PEAK_TEMP}°C    (threshold: ${MAX_TEMP}°C)
  Average temperature:  ${AVG_TEMP}°C
  Peak utilization:     ${MAX_UTIL}%
  Average utilization:  ${AVG_UTIL}%
  Peak power draw:      ${MAX_POWER}W    (expected: 650-700W under load)

================================================================================
  MEMORY STRESS
================================================================================

  HBM fill:       $(awk "BEGIN{printf \"%.0f\", ${HBM_FILL_FRACTION}*100}")% of total GPU memory per GPU (leaves NCCL headroom)
  Method:         Resident FP16 hog tensor + continuous matmul

================================================================================
  NCCL ALL_REDUCE (InfiniBand / NVLink stress)
================================================================================

  Total iterations:         ${ITERS}
  Failed iterations:        ${FAILED_ITERS}
  Peak bus bandwidth:       ${PEAK_BUSBW} GB/s
  Average bus bandwidth:    ${AVG_BUSBW} GB/s
  (A blank/N/A bandwidth means NCCL produced no valid result — treat as a red
   flag even if other checks pass.)

================================================================================
  HEALTH CHECKS
================================================================================

  Total monitor polls:       $TOTAL_POLLS
  Overtemperature events:    $OVERTEMP    (threshold: >${MAX_TEMP}°C)
  Low utilization events:    $LOW_UTIL    (threshold: <80%)
  XID errors detected:       $XID
  XID check unverified:      $([ "$XID_UNVERIFIED" -gt 0 ] 2>/dev/null && echo "YES — see note below" || echo "no")

================================================================================
  PASS / FAIL SUMMARY
================================================================================

$(if [ "$OVERTEMP" = "0" ]; then echo "  [PASS] No overtemperature events"; else echo "  [FAIL] $OVERTEMP overtemperature event(s) detected"; fi)
$(if [ "${XID_UNVERIFIED:-0}" -gt 0 ] 2>/dev/null; then echo "  [WARN] XID check could not run — GPUs NOT certified XID-clean"; elif [ "$XID" = "0" ]; then echo "  [PASS] No XID errors"; else echo "  [FAIL] $XID XID error(s) detected"; fi)
$(if [ "$FAILED_ITERS" = "0" ]; then echo "  [PASS] All NCCL all_reduce iterations completed correctly"; elif [ "$FAILED_ITERS" = "N/A" ]; then echo "  [WARN] all_reduce iteration status unknown — no master summary in logs"; else echo "  [FAIL] $FAILED_ITERS all_reduce iteration(s) returned incorrect data"; fi)
$(if [ "$NCCL_OK" = "1" ]; then echo "  [PASS] NCCL produced valid bandwidth (peak ${PEAK_BUSBW} GB/s)"; else echo "  [FAIL] No valid NCCL bandwidth recorded — collective produced no result"; fi)
$(if [ "$LOW_UTIL" -lt "10" ] 2>/dev/null; then echo "  [PASS] GPU utilization stayed healthy"; else echo "  [WARN] $LOW_UTIL low utilization events detected"; fi)

  OVERALL RESULT: $RESULT

================================================================================
  Raw log file: $LOG_FILE
================================================================================
REPORT

  echo ""
  echo -e "${BLUE}============================================================${NC}"
  echo -e "${BLUE}  SOAK TEST REPORT${NC}"
  echo -e "${BLUE}============================================================${NC}"
  cat "$SCRIPT_DIR/$REPORT_FILE"
  echo ""
  echo "Report saved to: $SCRIPT_DIR/$REPORT_FILE"
}

generate_report

# Run completed and the report is written — from here the explicit cleanup
# decision below owns teardown, so the error-exit trap must no longer fire.
REACHED_END=1

# Cleanup — auto (CI), interactive prompt (tty), or leave-and-instruct (piped).
echo ""
if [ "${AUTO_CLEANUP:-}" = "y" ]; then
  cleanup_ns
  echo "Cleaned up."
elif [ -t 0 ]; then
  read -p "Delete namespace $NAMESPACE and all test resources? (y/n): " CLEANUP || CLEANUP=n
  if [ "$CLEANUP" = "y" ]; then
    cleanup_ns
    echo "Cleaned up."
  fi
else
  echo "Non-interactive shell — leaving namespace $NAMESPACE in place."
  echo "Delete with: kubectl delete namespace $NAMESPACE   (or re-run with AUTO_CLEANUP=y)"
fi

exit $MONITOR_EXIT
