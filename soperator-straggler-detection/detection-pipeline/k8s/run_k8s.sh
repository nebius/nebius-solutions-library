#!/bin/bash
# K8s-native run -- sibling to run.sh (Slurm).
# Launches one K8s training Job with aggregator sidecars, creates the
# rendezvous ClusterIP Service, forwards VM port for the alert engine,
# and starts the alert engine locally on the control machine.
#
# "Already running" guard: checks for active Jobs with matching label
# (kubectl equivalent of Slurm's squeue guard).
#
# Source cluster_k8s.env first (written by install_k8s.sh), then run:
#   source cluster_k8s.env
#   k8s/run_k8s.sh [steps] [port] [pin_mode]
#
# Arguments (all optional, with defaults):
#   steps     -- torchrun iterations (default: 300)
#   port      -- torchrun rendezvous port (default: 29500)
#   pin_mode  -- CPU pinning mode: auto|full|dump|off (default: auto)
set -uo pipefail
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS_DIR="$K8S_DIR/manifests"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[run_k8s.sh] $*"; }

CLUSTER_ENV="$PKG_ROOT/cluster_k8s.env"
if [ ! -f "$CLUSTER_ENV" ]; then
    echo "FATAL: $CLUSTER_ENV not found -- run k8s/install_k8s.sh first." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$CLUSTER_ENV"

for v in K8S_NAMESPACE NUM_GPU_NODES GPUS_PER_NODE VM_URL_SVC; do
    val="${!v:-}"
    [ -z "$val" ] && fail "cluster_k8s.env is missing $v -- re-run install_k8s.sh."
done
[ "$FAIL" -eq 1 ] && exit 1

TRAIN_STEPS="${1:-300}"
TRAIN_PORT="${2:-29500}"
PIN_MODE="${3:-auto}"
TRAIN_NUM_NODES="$NUM_GPU_NODES"
TRAIN_GPUS="$GPUS_PER_NODE"
NCCL_LIB_PATH="${NCCL_232_LIB_PATH:-}"
NS="$K8S_NAMESPACE"

VAR_DIR="$PKG_ROOT/var"
mkdir -p "$VAR_DIR"
ALERT_LOG="$VAR_DIR/alert_engine_k8s_supervised.log"
SUMMARY_LOG="$VAR_DIR/alert_summary_k8s.log"

# =========================================================================
# Step 1 -- "already running" guard (kubectl equivalent of squeue check)
# =========================================================================
info "=== Step 1: guard check ==="
_ACTIVE_JOBS="$(kubectl get jobs -n "$NS" -l straggler.nebius.ai/component=training \
    --field-selector='status.active=1' --no-headers 2>/dev/null | wc -l)"
if [ "${_ACTIVE_JOBS:-0}" -gt 0 ]; then
    echo "ABORT: a straggler-detection training job is already active in namespace $NS."
    echo "       Wait for it to finish or delete it with:"
    echo "       kubectl delete jobs -n $NS -l straggler.nebius.ai/component=training"
    exit 1
fi

# =========================================================================
# Step 2 -- generate unique job run name and create rendezvous Service
# =========================================================================
_RUN_ID="$(date +%s)"
JOB_RUN_NAME="straggler-train-${_RUN_ID}"
info "Job run name: $JOB_RUN_NAME"
info "Config: steps=$TRAIN_STEPS, nodes=$TRAIN_NUM_NODES, gpus_per_node=$TRAIN_GPUS, port=$TRAIN_PORT, pin=$PIN_MODE"
[ -n "$NCCL_LIB_PATH" ] && info "NCCL 2.32.3 lib: $NCCL_LIB_PATH (full pinning)" || \
    info "NCCL 2.32.3 lib: not configured (dump-only or no pinning)"

# Create a Headless Service for pod DNS. K8s 1.33 sets each indexed Job pod's
# hostname to <job-name>-<index>, and with subdomain=<headless-svc-name>, each
# pod gets a stable DNS entry:
#   <job-name>-<index>.<headless-svc>.<ns>.svc.cluster.local
# Rank-0 DNS: ${JOB_RUN_NAME}-0.${JOB_RUN_NAME}-headless.${NS}.svc.cluster.local
# This avoids the Cilium hairpin NAT issue that ClusterIP routing-to-self causes.
info "Creating headless Service ${JOB_RUN_NAME}-headless for pod DNS..."
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${JOB_RUN_NAME}-headless
  namespace: ${NS}
  labels:
    straggler.nebius.ai/component: headless
    straggler.nebius.ai/run: ${JOB_RUN_NAME}
spec:
  selector:
    straggler.nebius.ai/run: ${JOB_RUN_NAME}
  clusterIP: None
  ports:
    - port: ${TRAIN_PORT}
      targetPort: ${TRAIN_PORT}
      protocol: TCP
EOF

# =========================================================================
# Step 3 -- apply training Job (healthy run: SLEEP_MS=0, TARGET_RANKS="")
# =========================================================================
info "=== Step 3: launching training Job ==="
export JOB_RUN_NAME TRAIN_STEPS TRAIN_PORT TRAIN_NUM_NODES TRAIN_GPUS \
       NCCL_LIB_PATH PIN_MODE VM_URL_SVC
# SLEEP_MS/TARGET_RANKS: allow caller to override for fault injection.
# STRAGGLER_MODE defaults to 'constant' so SLEEP_MS/TARGET_RANKS work directly
# without needing a trigger file (file_trigger mode requires /outdir/trigger.json
# to exist -- not documented in the README quickstart, so constant is the default).
SLEEP_MS="${SLEEP_MS:-0}"
TARGET_RANKS="${TARGET_RANKS:-}"
# If SLEEP_MS > 0 and MODE not explicitly set, use constant (direct, no trigger file needed).
if [ "${SLEEP_MS:-0}" -gt 0 ] && [ -z "${STRAGGLER_MODE:-}" ]; then
    STRAGGLER_MODE="constant"
fi
export SLEEP_MS TARGET_RANKS STRAGGLER_MODE="${STRAGGLER_MODE:-constant}"
# INSPECTOR: "off" disables Inspector plugin entirely (A/B true-baseline arm).
# Default "on" for all normal runs.
INSPECTOR="${INSPECTOR:-on}"
export INSPECTOR

# envsubst with explicit variable list to prevent substituting internal shell
# variables in the YAML's bash command blocks (e.g. $PKG, $OUTDIR, $DUMPDIR).
envsubst '${JOB_RUN_NAME} ${TRAIN_STEPS} ${TRAIN_PORT} ${TRAIN_NUM_NODES} ${TRAIN_GPUS} ${NCCL_LIB_PATH} ${PIN_MODE} ${VM_URL_SVC} ${SLEEP_MS} ${TARGET_RANKS} ${STRAGGLER_MODE} ${INSPECTOR} ${JOB_RUN_NAME}' \
    < "$MANIFESTS_DIR/04-training-job.yaml.tmpl" | kubectl apply -f -
info "Job $JOB_RUN_NAME applied to namespace $NS."

# =========================================================================
# Step 4 -- port-forward VictoriaMetrics so alert engine can reach it
# =========================================================================
info "=== Step 4: starting kubectl port-forward for VictoriaMetrics ==="
# Kill any stale port-forwards from previous runs
pkill -f "kubectl port-forward.*victoriametrics.*8428" 2>/dev/null || true
sleep 1
kubectl port-forward svc/victoriametrics 8428:8428 -n "$NS" >/dev/null 2>&1 &
_PF_PID=$!
disown $_PF_PID
info "  port-forward pid=$_PF_PID (VM reachable at http://localhost:8428)"
# Wait for port-forward to establish
for i in $(seq 1 15); do
    curl -s --max-time 2 "http://localhost:8428/health" >/dev/null 2>&1 && break
    sleep 2
    [ "$i" -eq 15 ] && fail "VictoriaMetrics port-forward did not become ready within 30s."
done
[ "$FAIL" -eq 1 ] && exit 1
info "  VictoriaMetrics reachable at http://localhost:8428 (port-forwarded)."

# =========================================================================
# Step 5 -- start alert engine locally
# =========================================================================
info "=== Step 5: starting alert engine ==="
_EXISTING="$(pgrep -af 'run_alert_engine_supervised\.sh' 2>/dev/null | head -1)"
if [ -n "$_EXISTING" ]; then
    info "  alert_engine supervisor already running -- leaving it alone."
else
    nohup bash "$PKG_ROOT/observability/run_alert_engine_supervised.sh" \
        "http://localhost:8428" "$ALERT_LOG" >/dev/null 2>&1 &
    disown
    info "  alert_engine supervisor launched (log=$ALERT_LOG)."
fi

# =========================================================================
# Step 6 -- wait for pods to start and confirm aggregators heartbeat in VM
# =========================================================================
info "=== Step 6: startup health confirmation ==="
info "  Waiting up to 120s for training pods to start..."
kubectl wait --for=condition=Ready pods -n "$NS" -l straggler.nebius.ai/run="$JOB_RUN_NAME" \
    --timeout=120s 2>/dev/null || \
    warn "  Some pods not yet Ready within 120s (may still be pulling image; job will continue)"

info "  Waiting up to 60s for aggregator heartbeats in VictoriaMetrics..."
# Get actual node names from the running pods
for _attempt in $(seq 1 20); do
    _NODES="$(kubectl get pods -n "$NS" -l straggler.nebius.ai/run="$JOB_RUN_NAME" \
        -o jsonpath='{.items[*].spec.nodeName}' 2>/dev/null)"
    [ -n "$_NODES" ] && break
    sleep 3
done
_HEARTBEAT_OK=1
for _node in $_NODES; do
    _ok=0
    for _ in $(seq 1 10); do
        _resp="$(curl -s --max-time 5 "http://localhost:8428/api/v1/query" \
            --data-urlencode "query=agg_aggregator_heartbeat{hostname=\"$_node\"}" 2>/dev/null)"
        echo "$_resp" | grep -q '"result":\[{' && { _ok=1; break; }
        sleep 3
    done
    if [ "$_ok" -eq 1 ]; then
        info "  $_node: aggregator HEARTBEAT confirmed in VictoriaMetrics."
    else
        warn "  $_node: no aggregator heartbeat in VM within 30s -- check aggregator sidecar logs:"
        warn "    kubectl logs -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -c aggregator"
        _HEARTBEAT_OK=0
    fi
done
[ "$_HEARTBEAT_OK" -eq 0 ] && warn "  Some aggregators did not confirm -- detection may be degraded."

info "=== run_k8s.sh completed ==="
info "Job:        $JOB_RUN_NAME (namespace: $NS)"
info "Watch pods: kubectl get pods -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -w"
info "Train logs: kubectl logs -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -c training -f"
info "Agg logs:   kubectl logs -n $NS -l straggler.nebius.ai/run=$JOB_RUN_NAME -c aggregator -f"
info "Alerts:     tail -f $SUMMARY_LOG"
info "Full alerts: tail -f $ALERT_LOG"
info ""
info "To inject a fault (100ms sleep on rank 1) for detection validation:"
info "  echo '{\"mode\":\"sleep\",\"sleep_ms\":100,\"target_ranks\":[1]}' > /tmp/trigger.json"
info "  kubectl exec -n $NS -it <pod-name> -c training -- cp /tmp/trigger.json /outdir/trigger.json"
info ""
info "To stop: kubectl delete job $JOB_RUN_NAME svc/${JOB_RUN_NAME}-headless -n $NS"
echo ""
echo "JOB_RUN_NAME=$JOB_RUN_NAME"
