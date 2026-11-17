#!/bin/bash
# inject_diffuse_slowdown.sh — inject labelled SLOWDOWN-DIFFUSE (injected) episodes
#
# PURPOSE
# -------
# Creates 3 episodes of 5–10 minutes of bulk TCP traffic between the two GPU nodes
# while a healthy training run is in progress.  Used to build labelled SLOWDOWN-DIFFUSE
# training data for the alert-layer classifier design set.
#
# No privileged mode.  Uses busybox image (or python:3.11-slim if busybox not available)
# for iperf3-like TCP traffic between two CPU-only pods.  Does NOT affect or modify
# training pods.
#
# PREREQUISITES
# -------------
#   - A healthy training soak must already be running (Megatron, DDP, or FSDP).
#   - kubectl configured with access to the cluster.
#   - env_sampler.sh should already be running so we capture telemetry during injections.
#
# USAGE
# -----
#   source detection-pipeline/cluster_k8s.env
#   bash k8s/alert_layer/inject_diffuse_slowdown.sh >> /tmp/diffuse_injection.log 2>&1
#
# OUTPUT
# ------
# Writes injection start/stop timestamps to stdout in LABEL format:
#   DIFFUSE_INJECT_START ts=<UTC> episode=<N> duration_target_s=<S>
#   DIFFUSE_INJECT_STOP  ts=<UTC> episode=<N> actual_s=<S>
#
# These timestamps must be recorded in the session's design-set log and labelled:
#   SLOWDOWN-DIFFUSE (injected)

set -uo pipefail

CLUSTER_ENV="${CLUSTER_ENV:-$(dirname "${BASH_SOURCE[0]}")/../../cluster_k8s.env}"
[ -f "$CLUSTER_ENV" ] && source "$CLUSTER_ENV"

NS="${K8S_NAMESPACE:-straggler-detection}"
# CPU nodes: busybox pods land anywhere by default (no GPU taint tolerations)
# We explicitly target the two GPU nodes to maximize cross-node traffic interference.
NODE0="${INJECT_NODE0:-}"
NODE1="${INJECT_NODE1:-}"

# If nodes not set, discover them from cluster_k8s.env GPU_NODES
if [ -z "$NODE0" ] && [ -n "${GPU_NODES:-}" ]; then
    IFS=',' read -ra _NODES <<< "$GPU_NODES"
    NODE0="${_NODES[0]:-}"
    NODE1="${_NODES[1]:-}"
fi

INJECT_NS="diffuse-inject"
EPISODES="${INJECT_EPISODES:-3}"
EPISODE_DURATION_S="${INJECT_DURATION_S:-360}"   # 6 minutes each
INTER_EPISODE_WAIT_S="${INTER_EPISODE_WAIT_S:-120}"

now_utc() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }

echo "[inject_diffuse_slowdown] Starting. episodes=$EPISODES duration=${EPISODE_DURATION_S}s ns=$NS"

# Create a dedicated namespace for the injector pods
kubectl get namespace "$INJECT_NS" > /dev/null 2>&1 || \
    kubectl create namespace "$INJECT_NS" > /dev/null

for ep in $(seq 1 "$EPISODES"); do
    echo ""
    echo "=== Episode $ep / $EPISODES ==="

    # Server pod (node1): listens for TCP connections
    SERVER_POD="diffuse-server-ep${ep}"
    CLIENT_POD="diffuse-client-ep${ep}"

    # Apply server pod
    kubectl apply -n "$INJECT_NS" -f - <<YAML > /dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${SERVER_POD}
  namespace: ${INJECT_NS}
  labels:
    diffuse-inject/episode: "${ep}"
    diffuse-inject/role: server
spec:
  restartPolicy: Never
  nodeName: "${NODE1}"
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: traffic
      image: busybox:1.36
      command:
        - sh
        - -c
        - |
          echo "SERVER ready on port 9999"
          # nc -l -p 9999 loops — accept connections and discard data
          while true; do nc -l -p 9999 > /dev/null 2>&1 || true; done
      resources:
        requests:
          cpu: "100m"
          memory: "64Mi"
        limits:
          cpu: "2"
          memory: "256Mi"
YAML

    # Wait for server pod IP
    _wait=0
    until kubectl get pod "$SERVER_POD" -n "$INJECT_NS" -o jsonpath='{.status.podIP}' 2>/dev/null | grep -q '\.'; do
        sleep 2
        _wait=$((_wait + 2))
        [ $_wait -gt 60 ] && { echo "ERROR: server pod $SERVER_POD never got IP"; break; }
    done
    SERVER_IP="$(kubectl get pod "$SERVER_POD" -n "$INJECT_NS" -o jsonpath='{.status.podIP}' 2>/dev/null)"
    echo "  Server pod IP: $SERVER_IP"

    # Mark start
    START_TS="$(now_utc)"
    START_EPOCH="$(date -u +%s)"
    echo "DIFFUSE_INJECT_START ts=${START_TS} episode=${ep} duration_target_s=${EPISODE_DURATION_S} server_ip=${SERVER_IP}"

    # Apply client pod
    kubectl apply -n "$INJECT_NS" -f - <<YAML > /dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${CLIENT_POD}
  namespace: ${INJECT_NS}
  labels:
    diffuse-inject/episode: "${ep}"
    diffuse-inject/role: client
spec:
  restartPolicy: Never
  nodeName: "${NODE0}"
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: traffic
      image: busybox:1.36
      command:
        - sh
        - -c
        - |
          echo "CLIENT starting bulk transfer to ${SERVER_IP}:9999 for ${EPISODE_DURATION_S}s"
          DEADLINE=\$((\$(date +%s) + ${EPISODE_DURATION_S}))
          while [ \$(date +%s) -lt \$DEADLINE ]; do
            # dd to /dev/zero piped through nc: bulk TCP transfer
            dd if=/dev/zero bs=1M count=256 2>/dev/null | nc -w 5 ${SERVER_IP} 9999 > /dev/null 2>&1 || true
          done
          echo "CLIENT done"
      resources:
        requests:
          cpu: "100m"
          memory: "64Mi"
        limits:
          cpu: "2"
          memory: "256Mi"
YAML

    # Wait for episode to complete (approximate: sleep for duration + buffer)
    sleep "$EPISODE_DURATION_S"
    STOP_TS="$(now_utc)"
    STOP_EPOCH="$(date -u +%s)"
    ACTUAL_S=$(( STOP_EPOCH - START_EPOCH ))
    echo "DIFFUSE_INJECT_STOP ts=${STOP_TS} episode=${ep} actual_s=${ACTUAL_S}"

    # Cleanup pods immediately so they don't linger
    kubectl delete pod "$SERVER_POD" "$CLIENT_POD" -n "$INJECT_NS" --grace-period=5 > /dev/null 2>&1 || true

    if [ "$ep" -lt "$EPISODES" ]; then
        echo "  Waiting ${INTER_EPISODE_WAIT_S}s before next episode..."
        sleep "$INTER_EPISODE_WAIT_S"
    fi
done

echo ""
echo "[inject_diffuse_slowdown] All $EPISODES episodes complete."
echo "Label these time ranges as SLOWDOWN-DIFFUSE (injected) in the design set."
kubectl delete namespace "$INJECT_NS" > /dev/null 2>&1 || true
