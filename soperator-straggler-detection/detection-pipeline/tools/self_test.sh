#!/bin/bash
# Stage 3 self-test -- the "does this actually work on THIS cluster" proof
# an operator can run immediately after install.sh + run.sh, without
# needing this project's own history to construct a real test themselves.
#
# Launches one real, already-packaged workload (nanoGPT straggler shape)
# with one real, injected software fault (STRAGGLER_SLEEP_MS/
# STRAGGLER_TARGET_RANKS -- this project's own validated, portable fault-
# injection mechanism; see docs/migration_readiness.md section 5 on why
# this replaced nvidia-smi -lgc), targeting a real rank on the SECOND real
# discovered node (proving cross-node attribution, not just node 0), and
# confirms the ALREADY-RUNNING pipeline (aggregator + alert_engine --
# started by run.sh; this script does not launch or re-detect them)
# correctly detects it AND attributes it to the exact real injected rank
# -- not just "an alert fired somewhere."
#
# Verification standard: this project's own mandatory rule
# (migration_readiness.md section 4, step 14) -- record the real injected
# target rank BEFORE checking alerts, then explicitly cross-reference the
# alert's own flagged identity against real evidence (the dump file's own
# header.rank for that identity) rather than just declaring "an alert
# fired" sufficient.
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAR_DIR="$PKG_ROOT/var"

fail() { echo "FATAL: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[self_test] $*"; }

CLUSTER_ENV="$PKG_ROOT/cluster.env"
[ -f "$CLUSTER_ENV" ] || fail "$CLUSTER_ENV not found -- run install.sh first."
# shellcheck disable=SC1090
source "$CLUSTER_ENV"
for v in NODE_LIST NUM_NODES GPUS_PER_NODE VM_URL; do
  val="${!v:-}"
  [ -z "$val" ] && fail "cluster.env has no real $v value -- re-run install.sh."
done
IFS=',' read -ra NODES <<< "$NODE_LIST"
DUMP_DIR_BASE="$VAR_DIR/dump"

# =========================================================================
# Step 0 -- confirm the pipeline is actually up before testing it (this
# script tests detection, it does not launch/re-detect the pipeline
# itself -- see run.sh for that)
# =========================================================================

info "Confirming the pipeline (run.sh) is already up..."
for node in "${NODES[@]}"; do
  resp="$(curl -s --max-time 5 "$VM_URL/api/v1/query" --data-urlencode "query=agg_aggregator_heartbeat{hostname=\"$node\"}" 2>/dev/null)"
  echo "$resp" | grep -q '"result":\[{' || fail "$node has no real, recent agg_aggregator_heartbeat sample in VictoriaMetrics -- the pipeline is not confirmed up. Run run.sh first."
done
_existing_supervisor="$(pgrep -af 'run_alert_engine_supervised\.sh' 2>/dev/null | head -1)"
[ -z "$_existing_supervisor" ] && fail "alert_engine.py supervisor is not running -- run run.sh first."
ALERT_LOG="$(echo "$_existing_supervisor" | awk '{print $NF}')"
[ -f "$ALERT_LOG" ] || fail "alert_engine.py supervisor is running but its real log ($ALERT_LOG) doesn't exist."
info "Pipeline confirmed up (real log: $ALERT_LOG)."

PRE_CF="$(grep -c "CHECK-FAILED" "$ALERT_LOG" 2>/dev/null)"; PRE_CF="${PRE_CF:-0}"
if [ -n "$(squeue -u "$USER" -h 2>/dev/null)" ]; then
  fail "an existing Slurm job for $USER is already queued/running -- self_test.sh needs a clean queue (run_straggler_nanogpt.sh's own internal check would also catch this, but checked here first for a clearer message)."
fi

# =========================================================================
# Step 1 -- pick a REAL target: rank GPUS_PER_NODE (the first real rank on
# the SECOND real discovered node) proves cross-node attribution, not just
# same-node-as-rank0. Falls back to this single node's own last real rank
# if there's genuinely only one real node (cross-node isn't meaningful
# there anyway).
# =========================================================================

if [ "$NUM_NODES" -ge 2 ]; then
  TARGET_RANK="$GPUS_PER_NODE"
  TARGET_HOST="${NODES[1]}"
else
  TARGET_RANK=$((GPUS_PER_NODE - 1))
  TARGET_HOST="${NODES[0]}"
fi
STRAGGLER_SLEEP_MS=200  # real, validated value (docs/straggler_dectection_history.md: 198.6ms observed delta against a 200ms injection)
info "Real injected target: rank=$TARGET_RANK (real, expected host=$TARGET_HOST), STRAGGLER_SLEEP_MS=$STRAGGLER_SLEEP_MS -- recorded BEFORE launching, per this project's own mandatory verification standard."

# Real, live-confirmed free port (never assumed).
PORT=29750
while ss -tln 2>/dev/null | grep -q ":$PORT "; do
  PORT=$((PORT + 1))
done
info "Using real, confirmed-free PORT=$PORT."

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="$VAR_DIR/self_test/$TS"
mkdir -p "$OUTDIR"
STEPS=100000  # generously large; this script stops the job itself once detection is confirmed, it does not wait for STEPS to complete

_launch_marker_ts=$(date +%s)
info "Launching workloads/nanogpt/run_straggler_nanogpt.sh (steps=$STEPS outdir=$OUTDIR port=$PORT sleep_ms=$STRAGGLER_SLEEP_MS target_ranks=$TARGET_RANK dumpdir_base=$DUMP_DIR_BASE)..."
bash "$PKG_ROOT/workloads/nanogpt/run_straggler_nanogpt.sh" "$STEPS" "$OUTDIR" "$PORT" "$DUMP_DIR_BASE" "$STRAGGLER_SLEEP_MS" "$TARGET_RANK"
DRIVER_PID_FILE="$OUTDIR/srun_driver.pid"

cleanup() {
  if [ -f "$DRIVER_PID_FILE" ]; then
    _dp="$(cat "$DRIVER_PID_FILE" 2>/dev/null)"
    [ -n "$_dp" ] && kill "$_dp" 2>/dev/null
  fi
  scancel -u "$USER" 2>/dev/null
  info "Cleanup: srun driver + any remaining Slurm job for $USER cancelled."
}
trap cleanup EXIT

# =========================================================================
# Step 2 -- find the REAL PID that is TARGET_RANK, from the real dump
# files the just-launched job itself produces (ground truth, established
# BEFORE looking at any alert).
# =========================================================================

info "Waiting for real dump files to appear on $TARGET_HOST for rank $TARGET_RANK (up to 90s)..."
TARGET_PID=""
for _ in $(seq 1 45); do
  for f in "$DUMP_DIR_BASE/$TARGET_HOST"/*.log; do
    [ -e "$f" ] || continue
    [ "$(stat -c %Y "$f" 2>/dev/null || echo 0)" -lt "$_launch_marker_ts" ] && continue
    r="$(head -1 "$f" 2>/dev/null | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print(d['header']['rank'])
except Exception:
    pass" 2>/dev/null)"
    if [ "$r" = "$TARGET_RANK" ]; then
      TARGET_PID="$(basename "$f" | grep -oP '(?<=-pid)\d+(?=\.log)')"
      break 2
    fi
  done
  sleep 2
done
[ -z "$TARGET_PID" ] && fail "could not find a real dump file for rank $TARGET_RANK on $TARGET_HOST within 90s -- the job may have failed to start. Check $OUTDIR/full_output.log and $OUTDIR/train_$TARGET_HOST.log."
info "Ground truth established: rank=$TARGET_RANK is real PID $TARGET_PID on $TARGET_HOST (read directly from its own dump file, before any alert)."

# =========================================================================
# Step 3 -- poll for the pipeline's own real alert, bounded wait. Real,
# honest diagnosis (not just "failed") if none appears in time.
# =========================================================================

MAX_WAIT_S=600
info "Polling $ALERT_LOG for a real [ALERT] matching rank=$TARGET_PID node=$TARGET_HOST (up to ${MAX_WAIT_S}s -- this project's own real calibration+grace-period timing, not a guess)..."
FOUND_LINE=""
_deadline=$(( $(date +%s) + MAX_WAIT_S ))
while [ "$(date +%s)" -lt "$_deadline" ]; do
  FOUND_LINE="$(grep "\[ALERT\] rank=$TARGET_PID .*node=$TARGET_HOST " "$ALERT_LOG" 2>/dev/null | tail -1)"
  [ -n "$FOUND_LINE" ] && break
  sleep 5
done

if [ -z "$FOUND_LINE" ]; then
  echo "[self_test] FAIL: no real [ALERT] for rank=$TARGET_PID node=$TARGET_HOST appeared within ${MAX_WAIT_S}s." >&2
  info "Diagnosing honestly rather than just reporting failure -- checking whether this test simply didn't run long enough yet, per this project's own preflight methodology:"
  _cadence_line="$(grep "measured cadence" "$VAR_DIR/aggregator_logs/$TARGET_HOST.log" 2>/dev/null | tail -1)"
  if [ -n "$_cadence_line" ]; then
    _rate="$(echo "$_cadence_line" | grep -oP 'rate=\K[0-9.]+')"
    info "  real measured cadence: $_cadence_line"
    [ -n "$_rate" ] && python3 "$PKG_ROOT/tools/preflight_duration_check.py" "$MAX_WAIT_S" "$_rate" "self_test" || true
  else
    warn "  no 'measured cadence' line found yet in $VAR_DIR/aggregator_logs/$TARGET_HOST.log -- the aggregator may not have discovered this job's real communicator yet within the wait window."
  fi
  exit 1
fi

info "Real alert found: $FOUND_LINE"

# =========================================================================
# Step 4 -- explicit rank-match verification (this project's own mandatory
# standard -- never just "an alert fired")
# =========================================================================

ALERTED_PID="$(echo "$FOUND_LINE" | grep -oP '(?<=rank=)\S+')"
ALERTED_HOST="$(echo "$FOUND_LINE" | grep -oP '(?<=node=)\S+')"
if [ "$ALERTED_PID" = "$TARGET_PID" ] && [ "$ALERTED_HOST" = "$TARGET_HOST" ]; then
  info "RANK-MATCH CONFIRMED: injected rank=$TARGET_RANK (real pid=$TARGET_PID, host=$TARGET_HOST) == alerted rank=$ALERTED_PID host=$ALERTED_HOST. EXACT MATCH."
  RESULT="PASS"
else
  echo "[self_test] RANK-MATCH FAILED: injected rank=$TARGET_RANK (real pid=$TARGET_PID, host=$TARGET_HOST) != alerted rank=$ALERTED_PID host=$ALERTED_HOST." >&2
  RESULT="FAIL"
fi

POST_CF="$(grep -c "CHECK-FAILED" "$ALERT_LOG" 2>/dev/null)"; POST_CF="${POST_CF:-0}"
NEW_CF=$((POST_CF - PRE_CF))
info "[CHECK-FAILED] count: $NEW_CF new since test start (pre=$PRE_CF, post=$POST_CF)."

echo "[self_test] === RESULT: $RESULT ==="
[ "$RESULT" = "PASS" ]
