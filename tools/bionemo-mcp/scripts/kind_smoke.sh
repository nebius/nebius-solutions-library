#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=${KIND_CLUSTER_NAME:-archvteams-2370-smoke}
CONTEXT="kind-${CLUSTER}"
SOURCE_IMAGE=${BIONEMO_MCP_IMAGE:-nebius-bionemo-mcp:archvteams-2370-local}
KIND_IMAGE=nebius-bionemo-mcp:archvteams-2370-local
TOKEN=kind-smoke-token-0000000000000000
PORT_FORWARD_PID=""

cleanup() {
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
    wait "$PORT_FORWARD_PID" >/dev/null 2>&1 || true
  fi
  kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if kind get clusters | rg --quiet --fixed-strings --line-regexp "$CLUSTER"; then
  echo "Refusing to modify existing Kind cluster: $CLUSTER" >&2
  exit 1
fi

kind create cluster --name "$CLUSTER" --wait 120s
if [[ "$SOURCE_IMAGE" != "$KIND_IMAGE" ]]; then
  docker image tag "$SOURCE_IMAGE" "$KIND_IMAGE"
fi
kind load docker-image --name "$CLUSTER" "$KIND_IMAGE"
kubectl --context "$CONTEXT" create namespace bionemo
kubectl --context "$CONTEXT" create namespace nims
kubectl --context "$CONTEXT" apply --filename "$ROOT/deploy/kind/mock-nim.yaml"
kubectl --context "$CONTEXT" --namespace nims rollout status deployment/boltz2 --timeout 120s

kubectl --context "$CONTEXT" --namespace bionemo create secret generic bionemo-mcp-auth \
  --from-literal="token=$TOKEN"
kubectl --context "$CONTEXT" --namespace bionemo create secret generic bionemo-mcp-s3 \
  --from-literal=access-key-id=kind-access \
  --from-literal=secret-access-key=kind-secret

helm upgrade --install smoke "$ROOT/deploy/helm/nebius-bionemo-mcp" \
  --kube-context "$CONTEXT" \
  --namespace bionemo \
  --values "$ROOT/deploy/helm/nebius-bionemo-mcp/values.test.yaml" \
  --set ingress.enabled=false \
  --set replicaCount=2 \
  --set image.repository=nebius-bionemo-mcp \
  --set image.tag=archvteams-2370-local \
  --set image.pullPolicy=Never \
  --wait --timeout 5m

kubectl --context "$CONTEXT" --namespace bionemo port-forward \
  service/smoke-nebius-bionemo-mcp 18080:8000 >/tmp/archvteams-2370-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!

for _ in $(seq 1 30); do
  if curl --silent --fail --max-time 2 http://127.0.0.1:18080/healthz >/dev/null; then
    break
  fi
  sleep 1
done
curl --silent --fail --max-time 2 http://127.0.0.1:18080/healthz >/dev/null

unauthenticated_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 http://127.0.0.1:18080/mcp)
invalid_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 --header 'Authorization: Bearer invalid' http://127.0.0.1:18080/mcp)
if [[ "$unauthenticated_status" != 401 || "$invalid_status" != 401 ]]; then
  echo "Expected bearer failures to return 401, got $unauthenticated_status and $invalid_status" >&2
  exit 1
fi

uv run python "$ROOT/scripts/smoke_http.py" \
  --url http://127.0.0.1:18080/mcp \
  --token "$TOKEN" \
  --expected-tool list_models \
  --expected-tool fleet_health \
  --expected-tool boltz2_predict \
  --repetitions 8

helm test smoke --kube-context "$CONTEXT" --namespace bionemo --logs --timeout 2m
kubectl --context "$CONTEXT" --namespace bionemo get deployment,pod,service,networkpolicy
echo "Kind smoke passed: two stateless replicas, bearer rejection, authenticated MCP, dynamic Boltz2 registration, and Helm test"
