#!/bin/sh
# Rotate the hub API's per-region credentials (hub secret api/region-kubeconfigs, one key per region).
#
# For every region R in $REGIONS (space-separated; cluster id in $CLUSTER_<R with - as _, upper>,
# ops SA key /var/run/nebius*/<R>.json):
#   1. read the cluster's public endpoint and CA from the Nebius API and mint an mk8s cluster token
#      for the ops SA (its project role maps to cluster-admin in the cluster),
#   2. ask that cluster for a time-bound ServiceAccount token for api/api-agent (TokenRequest,
#      $TOKEN_TTL_SECONDS, default 48 h),
#   3. write the kubeconfig into the hub secret api/region-kubeconfigs under key R through the
#      in-cluster API (this pod's ServiceAccount; Role on that one Secret).
# The API re-reads a kubeconfig whenever its file changes (services/api/kube.py), so no restart.
# Runs daily (CronJob clusters/hub/apps/overlays/ops/rotate-api-agent-token.yaml): tokens overlap.
# Replaces the static kubernetes.io/service-account-token Secret api-agent-token and
# `onboarding bootstrap --region` for the credential part.
set -eu
: "${REGIONS:?space-separated regions to rotate}"
TTL=${TOKEN_TTL_SECONDS:-172800}
SECRET_NS=${SECRET_NS:-api}; SECRET_NAME=${SECRET_NAME:-region-kubeconfigs}
SA_TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
LOCAL="https://kubernetes.default.svc"
LOCAL_CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
rc=0
for region in $REGIONS; do
  var="CLUSTER_$(echo "$region" | tr '-' '_' | tr '[:lower:]' '[:upper:]')"
  cluster=$(eval "echo \${$var:-}")
  if [ -z "$cluster" ]; then echo "rotate[$region]: $var not set" >&2; rc=1; continue; fi
  # the ops SA key for this region may be in any mounted dir (hub: /var/run/nebius-eu-south1)
  keyfile=$(ls /var/run/nebius*/"$region".json 2>/dev/null | head -1)
  if [ -z "$keyfile" ]; then echo "rotate[$region]: no SA key file for $region" >&2; rc=1; continue; fi
  nebius profile create "sa-$region" --endpoint api.nebius.cloud --service-account-file-path "$keyfile" >/dev/null 2>&1 || true
  info=$(nebius --profile "sa-$region" mk8s v1 cluster get --id "$cluster" --format json)
  server=$(printf %s "$info" | jq -r .status.control_plane.endpoints.public_endpoint)
  ca=$(printf %s "$info" | jq -r .status.control_plane.auth.cluster_ca_certificate)
  if [ -z "$server" ] || [ "$server" = null ] || [ -z "$ca" ] || [ "$ca" = null ]; then echo "rotate[$region]: cluster $cluster: no endpoint/CA" >&2; rc=1; continue; fi
  cafile=$(mktemp); printf '%s\n' "$ca" > "$cafile"
  iam=$(nebius --profile "sa-$region" mk8s v1 cluster get-token --format json | jq -r .status.token)
  tok=$(curl -sSf --cacert "$cafile" -H "Authorization: Bearer $iam" -H 'Content-Type: application/json' \
        -X POST "$server/api/v1/namespaces/$SECRET_NS/serviceaccounts/api-agent/token" \
        -d "{\"apiVersion\":\"authentication.k8s.io/v1\",\"kind\":\"TokenRequest\",\"spec\":{\"expirationSeconds\":$TTL}}" | jq -r .status.token)
  rm -f "$cafile"
  if [ -z "$tok" ] || [ "$tok" = null ]; then echo "rotate[$region]: TokenRequest failed" >&2; rc=1; continue; fi
  kc=$(jq -cn --arg r "$region" --arg s "$server" --arg ca "$(printf %s "$ca" | base64 | tr -d '\n')" --arg t "$tok" '{
    apiVersion: "v1", kind: "Config", "current-context": $r,
    clusters: [{name: $r, cluster: {server: $s, "certificate-authority-data": $ca}}],
    users: [{name: "api-agent", user: {token: $t}}],
    contexts: [{name: $r, context: {cluster: $r, user: "api-agent"}}]}' | base64 | tr -d '\n')
  patch=$(jq -cn --arg r "$region" --arg kc "$kc" '{data: {($r): $kc}}')
  code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$LOCAL_CA" -H "Authorization: Bearer $SA_TOKEN" \
         -H 'Content-Type: application/merge-patch+json' -X PATCH "$LOCAL/api/v1/namespaces/$SECRET_NS/secrets/$SECRET_NAME" -d "$patch")
  if [ "$code" = 404 ]; then
    body=$(jq -cn --arg n "$SECRET_NAME" --arg r "$region" --arg kc "$kc" '{apiVersion:"v1",kind:"Secret",type:"Opaque",metadata:{name:$n},data:{($r):$kc}}')
    code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$LOCAL_CA" -H "Authorization: Bearer $SA_TOKEN" \
           -H 'Content-Type: application/json' -X POST "$LOCAL/api/v1/namespaces/$SECRET_NS/secrets" -d "$body")
  fi
  case "$code" in 2*) echo "rotate[$region]: api-agent token renewed (ttl ${TTL}s) in $SECRET_NS/$SECRET_NAME";; *) echo "rotate[$region]: secret update HTTP $code" >&2; rc=1;; esac
done
exit $rc
