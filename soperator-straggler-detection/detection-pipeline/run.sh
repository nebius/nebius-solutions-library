#!/bin/bash
# Stage 3 run.sh -- launches the full straggler-detection pipeline end to
# end (node_aggregator_ref.py per real node, the supervised alert_engine.py,
# and a confirm-only check of Grafana access), using ONLY cluster.env's
# real, already-discovered values. Does NOT re-implement node/environment
# discovery -- that is install.sh's job (Stage 2); if a value this script
# needs is missing from cluster.env, this script reports it as a real
# Stage 2 gap and stops, rather than silently re-detecting it here.
#
# Does not launch any training workload itself -- see tools/self_test.sh
# for the one-command "does this actually work on THIS cluster" proof
# (launches one real, already-packaged workload with a real injected
# fault and verifies the pipeline correctly detects+attributes it).
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR_DIR="$PKG_ROOT/var"
mkdir -p "$VAR_DIR" "$VAR_DIR/aggregator_logs"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[run.sh] $*"; }

CLUSTER_ENV="$PKG_ROOT/cluster.env"
if [ ! -f "$CLUSTER_ENV" ]; then
  echo "FATAL: $CLUSTER_ENV not found -- run install.sh first. run.sh only consumes install.sh's own real, live-discovered values; it never re-detects the environment itself." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CLUSTER_ENV"

for v in NODE_LIST NUM_NODES GPUS_PER_NODE VM_URL; do
  val="${!v:-}"
  if [ -z "$val" ]; then
    fail "cluster.env has no real $v value. This is a real Stage 2 (install.sh) gap -- run.sh does not re-detect this itself. Re-run install.sh and check its own FATAL/WARNING output for why $v wasn't written."
  fi
done
[ "$FAIL" -eq 1 ] && { echo "[run.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

IFS=',' read -ra NODES <<< "$NODE_LIST"
DUMP_DIR_BASE="$VAR_DIR/dump"
ALERT_LOG="$VAR_DIR/alert_engine_supervised.log"

# =========================================================================
# Step 1 -- launch the pipeline using ONLY cluster.env's real values
# =========================================================================

info "=== Step 1: launching the pipeline (real nodes: $NODE_LIST) ==="

for node in "${NODES[@]}"; do
  dd="$DUMP_DIR_BASE/$node"
  log="$VAR_DIR/aggregator_logs/$node.log"
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" "pgrep -f 'node_aggregator_ref.py .*$dd'" >/dev/null 2>&1; then
    info "  $node: aggregator already running against $dd -- leaving it alone."
    continue
  fi
  # Real bugs found live (this session), both needed together:
  # 1) `-n`/stdin redirection alone was NOT enough -- ssh still never
  #    returned. `setsid` fully detaches the launched process into its
  #    own session, independent of the ssh session.
  # 2) Even with setsid, `cmd1 && cmd2 &` still hung for cmd2's entire
  #    real runtime -- confirmed live via a minimal `sleep 8` reproduction.
  #    `&` binds to the WHOLE `&&`-list (bash backgrounds "cmd1 && cmd2"
  #    as one implicit-subshell job), not just cmd2, so cmd2's own
  #    redirects don't fully detach the group from the ssh channel until
  #    cmd2 itself finishes. Using `;` instead of `&&` so `&` binds to
  #    only the final simple command fixed it (confirmed: 8.5s -> 0.5s in
  #    the same minimal reproduction).
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$node" \
    "mkdir -p '$dd' || exit 1; setsid nohup bash '$PKG_ROOT/observability/run_aggregator_supervised.sh' '$dd' '$node' '$VM_URL' '$log' >/dev/null 2>&1 </dev/null &" \
    && info "  $node: aggregator supervisor launched (dump_dir=$dd, log=$log)." \
    || fail "  $node: failed to launch aggregator supervisor via ssh."
done

_existing_supervisor="$(pgrep -af 'run_alert_engine_supervised\.sh' 2>/dev/null | head -1)"
if [ -n "$_existing_supervisor" ]; then
  # Real, live-confirmed case: an already-running supervisor (e.g. from a
  # session predating this install's own $ALERT_LOG convention) may be
  # using a different real log path than this install's own default --
  # read ITS real, actual log path from its own real command line rather
  # than assuming this script's own default applies to it.
  _existing_log="$(echo "$_existing_supervisor" | awk '{print $NF}')"
  if [ -n "$_existing_log" ] && [ -f "$_existing_log" ]; then
    ALERT_LOG="$_existing_log"
  fi
  info "  alert_engine.py supervisor already running -- leaving it alone (real log: $ALERT_LOG)."
else
  nohup bash "$PKG_ROOT/observability/run_alert_engine_supervised.sh" "$VM_URL" "$ALERT_LOG" >/dev/null 2>&1 &
  disown
  info "  alert_engine.py supervisor launched (log=$ALERT_LOG)."
fi

# Real before/after baseline, so Step 2's "zero NEW [CHECK-FAILED] at
# startup" is an actual comparison against THIS run's real log, not a
# guess about pre-existing state (computed only now that ALERT_LOG is
# known to be the real, actually-in-use path either way).
PRE_CF=0
if [ -f "$ALERT_LOG" ]; then
  # Real bug found live (this session): `grep -c PATTERN file || echo 0`
  # double-counts when there are zero matches -- grep -c still PRINTS "0"
  # (that's not an error) but exits 1 for "no matches", which also fires
  # the `|| echo 0` fallback, so the substitution captures "0\n0" instead
  # of "0" -- silently breaks the arithmetic below. grep -c's own output
  # is already always a valid number; no `||` fallback needed at all.
  PRE_CF="$(grep -c "CHECK-FAILED" "$ALERT_LOG" 2>/dev/null)"
fi
PRE_CF="${PRE_CF:-0}"

if [ -n "${GRAFANA_URL:-}" ] || [ -n "${GRAFANA_K8S_SVC:-}" ]; then
  info "  Grafana: confirmed reachable per install.sh's own real discovery -- not launched by this script (deployment/lifecycle decision, out of this project's scope; see install.sh's own comment on Step 4.5)."
else
  warn "  Grafana: cluster.env has no GRAFANA_URL/GRAFANA_K8S_SVC -- install.sh found no real, reachable instance at install time (not fatal; the aggregator/alert_engine/VM pipeline works without it). Re-run install.sh once a real instance is up to pick up real access details."
fi

[ "$FAIL" -eq 1 ] && { echo "[run.sh] Aborting before health confirmation -- see FATAL lines above." >&2; exit 1; }

info "Waiting for launched processes to stabilize..."
sleep 3

# =========================================================================
# Step 2 -- real startup health confirmation (never report "started" if
# any of these didn't actually come up correctly)
# =========================================================================

info "=== Step 2: real startup health confirmation ==="
STEP2_FAIL=0

info "Waiting up to 40s for each node's aggregator to push its first real heartbeat to VM..."
for node in "${NODES[@]}"; do
  ok=0
  for _ in $(seq 1 20); do
    resp="$(curl -s --max-time 5 "$VM_URL/api/v1/query" --data-urlencode "query=agg_aggregator_heartbeat{hostname=\"$node\"}" 2>/dev/null)"
    if echo "$resp" | grep -q '"result":\[{'; then
      ok=1
      break
    fi
    sleep 2
  done
  if [ "$ok" -eq 1 ]; then
    info "  $node: aggregator STABILIZED (real agg_aggregator_heartbeat sample confirmed in VictoriaMetrics)."
  else
    fail "  $node: aggregator did NOT stabilize -- no agg_aggregator_heartbeat sample appeared in VictoriaMetrics within 40s. Check $VAR_DIR/aggregator_logs/$node.log on $node."
    STEP2_FAIL=1
  fi
done

# Real bug found live (this session): "log line count grew" is NOT a
# valid liveness signal for alert_engine.py -- its own pipeline_health
# check only PRINTS when something is DOWN ([PIPELINE-DOWN]); once both
# aggregators are genuinely healthy (as just confirmed above), a fully
# healthy cycle produces NO new log output at all, so this check failed
# even though the process was correctly, actively cycling. Real fix:
# confirm actual CPU activity via /proc/<pid>/stat's own utime+stime
# ticks advancing across the wait, which proves real work is happening
# regardless of how quiet a healthy cycle's own log output is.
_pid="$(pgrep -f 'alert_engine\.py .*--duration' 2>/dev/null | head -1)"
_ticks_before=""; _ticks_after=""
if [ -n "$_pid" ] && [ -r "/proc/$_pid/stat" ]; then
  _ticks_before="$(awk '{print $14+$15}' "/proc/$_pid/stat" 2>/dev/null)"
fi
sleep 6
if [ -n "$_pid" ] && [ -r "/proc/$_pid/stat" ]; then
  _ticks_after="$(awk '{print $14+$15}' "/proc/$_pid/stat" 2>/dev/null)"
fi
if [ -n "$_pid" ] && [ -n "$_ticks_before" ] && [ -n "$_ticks_after" ] && [ "$_ticks_after" -gt "$_ticks_before" ]; then
  info "  alert_engine.py: alive and CYCLING (pid $_pid, real CPU ticks advanced $((_ticks_after - _ticks_before)) over 6s)."
else
  fail "  alert_engine.py: NOT confirmed alive/cycling (pid='${_pid:-none}', CPU ticks before=${_ticks_before:-n/a} after=${_ticks_after:-n/a}). Check $ALERT_LOG."
  STEP2_FAIL=1
fi

vm_health="$(curl -s --max-time 5 "$VM_URL/health" 2>/dev/null)"
if echo "$vm_health" | grep -qi '"database":[[:space:]]*"ok"\|^OK$'; then
  info "  VictoriaMetrics: REACHABLE and healthy ($VM_URL)."
else
  fail "  VictoriaMetrics: NOT reachable/healthy at $VM_URL (got: '$vm_health')."
  STEP2_FAIL=1
fi

POST_CF="$(grep -c "CHECK-FAILED" "$ALERT_LOG" 2>/dev/null)"
POST_CF="${POST_CF:-0}"
NEW_CF=$((POST_CF - PRE_CF))
if [ "$NEW_CF" -eq 0 ]; then
  info "  [CHECK-FAILED] count: 0 new since startup (pre=$PRE_CF, post=$POST_CF)."
else
  fail "  [CHECK-FAILED] count: $NEW_CF NEW since startup (pre=$PRE_CF, post=$POST_CF) -- see $ALERT_LOG."
  STEP2_FAIL=1
fi

if [ "$STEP2_FAIL" -eq 1 ]; then
  echo "[run.sh] Step 2 FAILED -- one or more real startup checks above did not pass. The pipeline is NOT confirmed healthy; see the specific FATAL line(s) above for exactly which check failed and why." >&2
  exit 1
fi

# =========================================================================
# Step 3 -- print real, ready-to-run Grafana access commands (never a
# placeholder; only prints what install.sh actually, live-confirmed)
# =========================================================================

info "=== Step 3: Grafana access ==="
if [ -z "${GRAFANA_URL:-}" ] && [ -z "${GRAFANA_K8S_SVC:-}" ]; then
  warn "No real Grafana access to print -- see the WARNING above. Re-run install.sh once a real, reachable instance exists."
else
  echo "[run.sh] Real Grafana access (values below are this cluster's own real, install.sh-discovered values, not placeholders):"
  if [ -n "${GRAFANA_K8S_SVC:-}" ]; then
    _kport="${GRAFANA_K8S_PORT:-3000}"
    echo "  kubectl port-forward -n $GRAFANA_K8S_NAMESPACE svc/$GRAFANA_K8S_SVC ${_kport}:${_kport}"
    echo "    then open http://localhost:${_kport}"
  fi
  if [ -n "${GRAFANA_URL:-}" ]; then
    _gport="$(echo "$GRAFANA_URL" | grep -oP ':\K[0-9]+$')"
    [ -z "$_gport" ] && _gport=3000
    echo "  ssh -L ${_gport}:localhost:${_gport} -N ${GRAFANA_ACCESS_USER}@${GRAFANA_ACCESS_HOST}"
    echo "    then open http://localhost:${_gport}"
    echo "    (${GRAFANA_ACCESS_HOST} is this cluster's own real, private-network address -- there is deliberately no public IP; reach it via whatever VPN/bastion path your organization already uses to reach this cluster's network. See install.sh's own WARNING output if this differs from a real externally-resolvable hostname.)"
  fi
  if [ "${GRAFANA_ANON_ENABLED:-unknown}" = "true" ]; then
    warn "Real per-user auth is NOT enforced on this Grafana instance right now -- anonymous Admin access is enabled (confirmed live by install.sh's own unauthenticated /api/org check). This is a genuine, disclosed gap on the CURRENTLY RUNNING instance specifically -- this script does not manage that instance's config. install.sh already generated a real, enforced-auth config + a real random admin password for a properly-configured instance -- see grafana-standalone/README.md to launch one, then retrieve its real login with:"
    echo "  cat ${GRAFANA_ADMIN_CREDENTIALS_FILE:-\$PKG_ROOT/var/grafana_admin_credentials.txt}"
  elif [ "${GRAFANA_ANON_ENABLED:-unknown}" = "false" ]; then
    info "Real per-user auth IS enforced on this instance (confirmed live by install.sh's own unauthenticated /api/org check -- it did NOT return real org data without credentials). Log in as admin_user=admin with the real, generated password -- retrieve it with:"
    echo "  cat ${GRAFANA_ADMIN_CREDENTIALS_FILE:-\$PKG_ROOT/var/grafana_admin_credentials.txt}"
    echo "  (never printed in plaintext here -- only this file's own path is)"
  else
    warn "Grafana's real auth posture was not checked (GRAFANA_URL was set after install.sh last ran, or install.sh predates this check) -- re-run install.sh to get a real answer instead of assuming."
  fi
fi

info "=== run.sh completed: pipeline is up and confirmed healthy on every real node ==="
info "Self-test: run tools/self_test.sh for a one-command, real, injected-fault proof that detection+attribution actually work on THIS cluster."
