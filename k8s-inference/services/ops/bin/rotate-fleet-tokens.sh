#!/bin/sh
# Rotate time-bound MultiKueue worker tokens using the regional operations identity.
# REGIONS selects workers; Kueue re-reads kubeconfigs when their Secrets change.
set -eu
: "${REGIONS:?space-separated regions to rotate}"
TTL=${TOKEN_TTL_SECONDS:-172800}
SA_TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
LOCAL="https://kubernetes.default.svc"
LOCAL_CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
rc=0

token_request() { # server cafile iam ns sa -> token
  curl -sSf --cacert "$2" -H "Authorization: Bearer $3" -H 'Content-Type: application/json' \
    -X POST "$1/api/v1/namespaces/$4/serviceaccounts/$5/token" \
    -d "{\"apiVersion\":\"authentication.k8s.io/v1\",\"kind\":\"TokenRequest\",\"spec\":{\"expirationSeconds\":$TTL}}" | jq -r .status.token
}
patch_secret() { # ns name json-merge-patch -> http code
  curl -s -o /dev/null -w '%{http_code}' --cacert "$LOCAL_CA" -H "Authorization: Bearer $SA_TOKEN" \
    -H 'Content-Type: application/merge-patch+json' -X PATCH "$LOCAL/api/v1/namespaces/$1/secrets/$2" -d "$3"
}

for region in $REGIONS; do
  suffix=$(echo "$region" | tr '-' '_' | tr '[:lower:]' '[:upper:]')
  cluster=$(eval "echo \${CLUSTER_$suffix:-}")
  fleet_id=$(eval "echo \${FLEET_ID_$suffix:-$region}")
  if [ -z "$cluster" ]; then echo "rotate[$region]: CLUSTER_$suffix not set" >&2; rc=1; continue; fi
  keyfile=$(ls /var/run/nebius*/"$region".json 2>/dev/null | head -1)
  if [ -z "$keyfile" ]; then echo "rotate[$region]: no SA key file for $region" >&2; rc=1; continue; fi
  nebius profile create "sa-$region" --endpoint api.nebius.cloud --service-account-file-path "$keyfile" >/dev/null 2>&1 || true
  info=$(nebius --profile "sa-$region" mk8s v1 cluster get --id "$cluster" --format json)
  server=$(printf %s "$info" | jq -r .status.control_plane.endpoints.public_endpoint)
  ca=$(printf %s "$info" | jq -r .status.control_plane.auth.cluster_ca_certificate)
  if [ -z "$server" ] || [ "$server" = null ] || [ -z "$ca" ] || [ "$ca" = null ]; then echo "rotate[$region]: cluster $cluster: no endpoint/CA" >&2; rc=1; continue; fi
  cafile=$(mktemp); printf '%s\n' "$ca" > "$cafile"
  iam=$(nebius --profile "sa-$region" mk8s v1 cluster get-token --format json | jq -r .status.token)
  ca64=$(printf %s "$ca" | base64 | tr -d '\n')

  # MultiKueue kubeconfig kueue-system/multikueue-<fleet id>
  tok=$(token_request "$server" "$cafile" "$iam" kueue-system multikueue || true)
  if [ -z "$tok" ] || [ "$tok" = null ]; then echo "rotate[$region]: TokenRequest multikueue failed" >&2; rc=1
  else
    kc=$(jq -cn --arg r "$fleet_id" --arg s "$server" --arg ca "$ca64" --arg t "$tok" '{
      apiVersion: "v1", kind: "Config", "current-context": $r,
      clusters: [{name: $r, cluster: {server: $s, "certificate-authority-data": $ca}}],
      users: [{name: "multikueue", user: {token: $t}}],
      contexts: [{name: $r, context: {cluster: $r, user: "multikueue"}}]}' | base64 | tr -d '\n')
    code=$(patch_secret kueue-system "multikueue-$fleet_id" "$(jq -cn --arg k "$kc" '{data: {kubeconfig: $k}}')")
    case "$code" in 2*) echo "rotate[$region]: kueue-system/multikueue-$fleet_id kubeconfig renewed (ttl ${TTL}s)";; *) echo "rotate[$region]: multikueue-$fleet_id HTTP $code" >&2; rc=1;; esac
  fi
  rm -f "$cafile"
done
exit $rc
