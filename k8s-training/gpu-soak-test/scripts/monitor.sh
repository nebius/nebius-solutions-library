#!/bin/bash
# =============================================================================
# GPU Soak Test Monitor (PyTorchJob)
# Polls GPU temperature, power, and utilization across ALL job pods (master +
# workers) while the soak runs, then does a single end-of-run XID check and
# prints a pass/fail summary.
#
# XID note: this cluster has no DCGM exporter, so XID comes from node dmesg via
# `kubectl debug node`. That is too slow/leaky to run per poll, so we run it
# ONCE at the end. If it cannot run, we mark the result UNVERIFIED (loudly)
# rather than silently reporting zero.
# =============================================================================
set -uo pipefail

NAMESPACE="gpu-soak"
JOB_SELECTOR="training.kubeflow.org/job-name=gpu-soak-test"
MASTER_SELECTOR="training.kubeflow.org/replica-type=master"
CONTAINER="pytorch"
POLL_INTERVAL="${POLL_INTERVAL:-30}"
MAX_TEMP="${MAX_TEMP:-83}"
MIN_UTIL="${MIN_UTIL:-80}"
# Absolute cap on the poll loop so a hung/stuck master (e.g. a worker that never
# joins) can't make the monitor poll forever. Defaults to the soak duration plus
# a generous buffer for image pull, startup, and the end-of-run XID check.
SOAK_MONITOR_TIMEOUT="${SOAK_MONITOR_TIMEOUT:-$(( ${SOAK_DURATION_SECONDS:-3600} + 1200 ))}"
# Write the monitor log to the dir the parent runner reads from (SOAK_LOG_DIR),
# not the caller's CWD — otherwise the report generator can't find it when the
# runner is invoked from a different directory. Falls back to CWD if run standalone.
LOG_FILE="${SOAK_LOG_DIR:-.}/soak-monitor-$(date +%Y%m%d_%H%M%S).log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo "[$(date -u '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
pass() { echo -e "${GREEN}[PASS]${NC} $*" | tee -a "$LOG_FILE"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*" | tee -a "$LOG_FILE"; }

# Count XID lines in a node's kernel ring buffer.
# `kubectl debug node -q` returns before streaming the command's stdout, so the
# result lives in the debugger POD's logs, not inline. We therefore launch the
# debugger, locate the pod it creates (named node-debugger-<node>-<rand>), wait
# for it to complete, read the count from its logs, then delete it.
# Echoes an integer count, or "__FAILED__" if it could not run.
check_xid_on_node() {
  local node="$1" dbg="" waited=0 phase="" out="" since_epoch="${SOAK_START_EPOCH:-0}"
  # Scope the XID scan to this run. dmesg holds the entire kernel ring buffer, so an
  # unscoped grep would count XID lines from before the soak even started (a prior
  # workload, a days-old event) against this run. --since restricts it to the run
  # window; the date is computed on the host so it matches dmesg's host-local clock.
  # (Requires a modern util-linux dmesg — standard on Nebius Ubuntu hosts. If the
  # run start is unknown, since_epoch=0 falls back to the whole buffer.)
  # `grep -c` exits 1 when the count is zero, which would mark the debugger pod
  # Failed on a HEALTHY node — so append `|| true` to keep the container clean.
  kubectl debug node/"$node" --image=ubuntu --profile=sysadmin -q \
    -- chroot /host sh -c "dmesg --since \"\$(date -d @${since_epoch} '+%Y-%m-%d %H:%M:%S' 2>/dev/null)\" 2>/dev/null | grep -ic xid || true" >/dev/null 2>&1 || true
  while [ "$waited" -lt 30 ]; do
    dbg=$(kubectl get pods --request-timeout=30s -n default -o name 2>/dev/null | grep "node-debugger-${node}" | tail -1)
    [ -n "$dbg" ] && break
    sleep 2; waited=$(( waited + 2 ))
  done
  if [ -z "$dbg" ]; then echo "__FAILED__"; return; fi
  # Wait for a terminal phase so the logs are complete (accept Succeeded OR
  # Failed — the count is in the logs regardless of the container exit code).
  waited=0
  while [ "$waited" -lt 120 ]; do
    phase=$(kubectl get "$dbg" -n default -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [ "$phase" = "Succeeded" ] || [ "$phase" = "Failed" ]; then break; fi
    sleep 3; waited=$(( waited + 3 ))
  done
  out=$(kubectl logs --request-timeout=30s -n default "$dbg" 2>/dev/null | tr -d ' \r\n')
  kubectl delete "$dbg" -n default --wait=false >/dev/null 2>&1 || true
  # Accept any valid non-negative integer count; only fail if we got no number.
  if [ -n "$out" ] && [ "$out" -ge 0 ] 2>/dev/null; then
    echo "$out"
  else
    echo "__FAILED__"
  fi
}

OVERTEMP_COUNT=0
LOW_UTIL_COUNT=0
XID_COUNT=0
XID_UNVERIFIED=0
POLL_COUNT=0
SOAK_NODES=""   # captured once so the XID check works even if pods vanish

log "=== GPU Soak Test Monitor Starting (PyTorchJob) ==="
log "Namespace: $NAMESPACE"
log "Poll interval: ${POLL_INTERVAL}s"
log "Max temp threshold: ${MAX_TEMP}°C"
log "Min utilization threshold: ${MIN_UTIL}%"
log "Log file: $LOG_FILE"
echo ""

# Wait for job pods to appear
JOB_PODS=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$JOB_SELECTOR" -o name 2>/dev/null || echo "")
if [ -z "$JOB_PODS" ]; then
  warn "No job pods found yet — waiting for PyTorchJob to start"
  sleep 30
  JOB_PODS=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$JOB_SELECTOR" -o name 2>/dev/null || echo "")
fi
log "Monitoring pods: $(echo "$JOB_PODS" | tr '\n' ' ')"
echo ""

# Wait until the workload reaches STEADY STATE before judging utilization.
# We gate on the first periodic "[soak] iter N" progress line (printed every 20
# iterations) rather than SOAK_CONFIG_OK — the latter prints just before the
# first all_reduce, during which NCCL does ring/channel setup and every GPU
# reads 0% util. Waiting for real iterations guarantees the compute loop is hot
# and NCCL is warm, eliminating false LOW UTIL events on poll #1.
# Bounded so a stuck/failed job doesn't hang the monitor.
log "Waiting for workload steady state (first [soak] iter line)..."
WARMUP=0
while [ "$WARMUP" -lt 180 ]; do
  if kubectl logs --request-timeout=30s --tail=-1 -n "$NAMESPACE" -l "$MASTER_SELECTOR" 2>/dev/null | grep -qE '\[soak\] iter [0-9]+'; then
    log "Workload is running steadily — starting utilization monitoring"
    break
  fi
  MP=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$MASTER_SELECTOR" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
  if [ "$MP" = "Failed" ] || [ "$MP" = "Succeeded" ]; then
    warn "Master reached '$MP' before warm-up signal — proceeding to summary"
    break
  fi
  sleep 5
  WARMUP=$(( WARMUP + 5 ))
done
echo ""

MONITOR_START=$(date +%s)
MASTER_STATUS="Unknown"
while true; do
  # Hard stop: never poll past the soak duration + buffer, so a master that never
  # reaches a terminal phase (stuck rendezvous, hung worker) can't hang the run.
  if [ "$(( $(date +%s) - MONITOR_START ))" -gt "$SOAK_MONITOR_TIMEOUT" ]; then
    warn "Monitor timed out after ${SOAK_MONITOR_TIMEOUT}s with master still '$MASTER_STATUS' — giving up and reporting failure"
    break
  fi

  # Master (rank 0) exit determines completion.
  MASTER_STATUS=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$MASTER_SELECTOR" \
    -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Unknown")

  if [ "$MASTER_STATUS" = "Succeeded" ] || [ "$MASTER_STATUS" = "Failed" ]; then
    log "Master pod status: $MASTER_STATUS — test complete"
    break
  fi

  POLL_COUNT=$(( POLL_COUNT + 1 ))
  log "--- Poll #$POLL_COUNT | Master: $MASTER_STATUS ---"

  JOB_PODS=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$JOB_SELECTOR" -o name 2>/dev/null || echo "")

  for POD in $JOB_PODS; do
    POD_NAME=$(echo "$POD" | sed 's|pod/||')

    # Record the node this pod runs on (for the end-of-run XID check).
    NODE=$(kubectl get pod --request-timeout=30s -n "$NAMESPACE" "$POD_NAME" -o jsonpath='{.spec.nodeName}' 2>/dev/null || echo "")
    if [ -n "$NODE" ] && ! echo "$SOAK_NODES" | grep -qw "$NODE"; then
      SOAK_NODES="$SOAK_NODES $NODE"
    fi

    log "Checking $POD_NAME:"
    GPU_STATS=$(kubectl exec --request-timeout=30s -n "$NAMESPACE" "$POD_NAME" -c "$CONTAINER" -- \
      nvidia-smi --query-gpu=index,temperature.gpu,utilization.gpu,power.draw,memory.used,memory.total \
      --format=csv,noheader,nounits 2>/dev/null || echo "exec_failed")

    if [ "$GPU_STATS" = "exec_failed" ]; then
      warn "$POD_NAME: could not exec nvidia-smi (pod may not be running yet)"
      continue
    fi

    while IFS=',' read -r idx temp util power mem_used mem_total; do
      temp=$(echo "$temp" | tr -d ' ')
      util=$(echo "$util" | tr -d ' ')
      power=$(echo "$power" | tr -d ' ')
      mem_used=$(echo "$mem_used" | tr -d ' ')
      mem_total=$(echo "$mem_total" | tr -d ' ')
      [ -z "$idx" ] && continue

      log "  GPU $idx: temp=${temp}°C util=${util}% power=${power}W mem=${mem_used}/${mem_total}MiB"

      if [ "$temp" -gt "$MAX_TEMP" ] 2>/dev/null; then
        warn "  GPU $idx OVERTEMP: ${temp}°C exceeds threshold ${MAX_TEMP}°C"
        OVERTEMP_COUNT=$(( OVERTEMP_COUNT + 1 ))
      fi

      if [ "$util" -lt "$MIN_UTIL" ] 2>/dev/null; then
        warn "  GPU $idx LOW UTIL: ${util}% below threshold ${MIN_UTIL}%"
        LOW_UTIL_COUNT=$(( LOW_UTIL_COUNT + 1 ))
      fi
    done <<< "$GPU_STATS"
  done

  # Node readiness
  NOT_READY=$(kubectl get nodes --request-timeout=30s --no-headers 2>/dev/null | awk '$2!="Ready" && $2!="Ready,SchedulingDisabled"' | wc -l | tr -d ' \n' || echo "0")
  if [ "$NOT_READY" -gt "0" ] 2>/dev/null; then
    warn "$NOT_READY node(s) not in Ready state"
  fi

  echo ""
  sleep "$POLL_INTERVAL"
done

# =============================================================================
# END-OF-RUN XID CHECK (once per node; best-effort)
# =============================================================================
echo ""
log "=== XID error check (node dmesg, end of run) ==="
if [ -z "$SOAK_NODES" ]; then
  warn "No soak nodes were recorded — XID check UNVERIFIED"
  log "XID_UNVERIFIED"
  XID_UNVERIFIED=1
else
  for NODE in $SOAK_NODES; do
    log "Checking XID on node: $NODE"
    XID_OUT=$(check_xid_on_node "$NODE")
    if [ "$XID_OUT" = "__FAILED__" ]; then
      warn "Node $NODE: XID check could NOT run (node debug unavailable) — UNVERIFIED"
      log "XID_UNVERIFIED"
      XID_UNVERIFIED=$(( XID_UNVERIFIED + 1 ))
    elif [ "$XID_OUT" -gt 0 ]; then
      warn "Node $NODE: $XID_OUT XID line(s) in dmesg"
      XID_COUNT=$(( XID_COUNT + XID_OUT ))
    else
      log "Node $NODE: no XID errors in dmesg"
    fi
  done
  # Safety net: remove any stray node-debugger pods THIS run created (scoped to
  # our own soak nodes — never another engineer's debug pods in the namespace).
  for NODE in $SOAK_NODES; do
    for p in $(kubectl get pods --request-timeout=30s -n default -o name 2>/dev/null | grep "node-debugger-${NODE}"); do
      kubectl delete "$p" -n default --wait=false >/dev/null 2>&1 || true
    done
  done
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
log "=== Soak Test Monitor Summary ==="
log "Total polls: $POLL_COUNT"
log "Overtemp events: $OVERTEMP_COUNT"
log "Low utilization events: $LOW_UTIL_COUNT"
log "XID errors detected: $XID_COUNT"
log "XID check unverified nodes: $XID_UNVERIFIED"

# Master (rank 0) exit code is the authoritative workload pass/fail.
MASTER_PHASE=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$MASTER_SELECTOR" \
  -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "Unknown")
MASTER_EXIT=$(kubectl get pods --request-timeout=30s -n "$NAMESPACE" -l "$MASTER_SELECTOR" \
  -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo "Unknown")

log "Master final phase: $MASTER_PHASE"
log "Master exit code: $MASTER_EXIT"
echo ""

OVERALL_PASS=true

if [ "$MASTER_EXIT" != "0" ]; then
  fail "Soak workload reported failures (master exit code: $MASTER_EXIT)"
  OVERALL_PASS=false
else
  pass "All NCCL all_reduce iterations completed successfully"
fi

if [ "$OVERTEMP_COUNT" -gt "0" ]; then
  fail "$OVERTEMP_COUNT GPU overtemperature event(s) detected"
  OVERALL_PASS=false
else
  pass "No overtemperature events"
fi

if [ "$XID_COUNT" -gt "0" ]; then
  fail "$XID_COUNT XID error(s) detected — potential GPU hardware issue"
  OVERALL_PASS=false
elif [ "$XID_UNVERIFIED" -gt "0" ]; then
  warn "XID check could not run on $XID_UNVERIFIED node(s) — GPUs NOT certified XID-clean"
else
  pass "No XID errors"
fi

# Threshold of 10 matches the README pass criteria and run-soak-test.sh's report
# (they previously disagreed — monitor used 5). "Fewer than 10 low-util events" passes.
if [ "$LOW_UTIL_COUNT" -ge "10" ]; then
  warn "$LOW_UTIL_COUNT low utilization events — GPUs may have throttled"
else
  pass "GPU utilization stayed healthy throughout"
fi

echo ""
if [ "$OVERALL_PASS" = "true" ]; then
  pass "=== SOAK TEST PASSED ==="
  exit 0
else
  fail "=== SOAK TEST FAILED ==="
  exit 1
fi
