#!/bin/sh
# Rotate the Nebius service-account credentials this cluster holds, then retire the old ones.
#
# For every Secret name in $SECRETS (space-separated, namespace $OPS_NS, mounted at
# /var/run/<secret>/<region>.json): generate a new auth key for that service account with the
# current key, patch the Secret key <region>.json through the in-cluster API, and delete every
# other auth key of that service account older than $RETIRE_AFTER_DAYS (default 7; a key file that
# a pod still mounts is at most one rotation old). With $BACKUP_S3_SECRET=<ns>/<name> also rotate
# the S3 access key of the SA behind the first Secret (CNPG barman backups re-read the Secret at the
# next backup) and delete that SA's other access keys older than the same age.
# Weekly CronJob clusters/common/manifests/ops/rotate-nebius-keys.yaml (overlays set SECRETS).
set -eu
: "${SECRETS:?space-separated Secret names}"
OPS_NS=${OPS_NS:-ops}; AGE=${RETIRE_AFTER_DAYS:-7}
SA_TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
LOCAL="https://kubernetes.default.svc"; LOCAL_CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
now=$(date -u +%s); rc=0; first_sa=""; first_profile=""

patch_secret() { # ns name key file
  v=$(base64 < "$4" | tr -d '\n')
  code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$LOCAL_CA" -H "Authorization: Bearer $SA_TOKEN" \
         -H 'Content-Type: application/merge-patch+json' -X PATCH "$LOCAL/api/v1/namespaces/$1/secrets/$2" \
         -d "$(jq -cn --arg k "$3" --arg v "$v" '{data: {($k): $v}}')")
  case "$code" in 2*) return 0;; *) echo "rotate: patch $1/$2[$3] HTTP $code" >&2; return 1;; esac
}
older_than() { # created_at -> 0 if older than $AGE days
  t=$(date -u -d "$1" +%s 2>/dev/null || echo "$now"); [ $((now - t)) -gt $((AGE * 86400)) ]
}

for secret in $SECRETS; do
  for f in /var/run/"$secret"/*.json; do
    [ -f "$f" ] || continue
    region=$(basename "$f" .json); profile="sa-$secret-$region"
    nebius profile create "$profile" --endpoint api.nebius.cloud --service-account-file-path "$f" >/dev/null 2>&1 || true
    said=$(jq -r '.["subject-credentials"].sub // empty' "$f")   # service-account-json: subject-credentials.sub = SA id, .kid = key id
    if [ -z "$said" ]; then echo "rotate: no service account id in $f" >&2; rc=1; continue; fi
    out=$(mktemp)
    parent=$(nebius --profile "$profile" iam service-account get --id "$said" --format json | jq -r .metadata.parent_id)
    if ! nebius --profile "$profile" iam auth-public-key generate --service-account-id "$said" --parent-id "$parent" --output "$out" --output-format service-account-json >/dev/null; then
      echo "rotate: key generation failed for $said ($secret/$region)" >&2; rc=1; rm -f "$out"; continue
    fi
    newid=$(jq -r '.["subject-credentials"].kid // empty' "$out")
    if patch_secret "$OPS_NS" "$secret" "$region.json" "$out"; then
      echo "rotate: $OPS_NS/$secret[$region.json] -> new auth key ${newid:-?} for $said"
    else rc=1; fi
    [ -z "$first_sa" ] && { first_sa=$said; first_profile=$profile; }
    # retire old auth keys (never the one just created; keep anything younger than $AGE days)
    nebius --profile "$profile" iam auth-public-key list-by-account --account-service-account-id "$said" --format json \
      | jq -r '.items[] | "\(.metadata.id) \(.metadata.created_at)"' | while read -r id created; do
        [ "$id" = "$newid" ] && continue
        if older_than "$created"; then
          nebius --profile "$profile" iam auth-public-key delete --id "$id" >/dev/null && echo "rotate: deleted auth key $id ($created)" || echo "rotate: delete $id failed" >&2
        fi
      done
    rm -f "$out"
  done
done

if [ -n "${BACKUP_S3_SECRET:-}" ] && [ -n "$first_sa" ]; then
  ns=${BACKUP_S3_SECRET%/*}; name=${BACKUP_S3_SECRET#*/}; parent=$(nebius --profile "$first_profile" iam service-account get --id "$first_sa" --format json | jq -r .metadata.parent_id)
  k=$(nebius --profile "$first_profile" iam v2 access-key create --parent-id "$parent" --name "backup-s3-$(date -u +%Y%m%d)" \
        --account-service-account-id "$first_sa" --secret-delivery-mode inline --format json)
  ak=$(printf %s "$k" | jq -r .status.aws_access_key_id); sk=$(printf %s "$k" | jq -r .status.secret); kid=$(printf %s "$k" | jq -r .metadata.id)
  if [ -n "$ak" ] && [ "$ak" != null ]; then
    patch=$(jq -cn --arg a "$(printf %s "$ak" | base64 | tr -d '\n')" --arg s "$(printf %s "$sk" | base64 | tr -d '\n')" '{data: {ACCESS_KEY_ID: $a, ACCESS_SECRET_KEY: $s}}')
    code=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$LOCAL_CA" -H "Authorization: Bearer $SA_TOKEN" -H 'Content-Type: application/merge-patch+json' \
           -X PATCH "$LOCAL/api/v1/namespaces/$ns/secrets/$name" -d "$patch")
    case "$code" in 2*) echo "rotate: $ns/$name -> new access key $kid";; *) echo "rotate: patch $ns/$name HTTP $code" >&2; rc=1;; esac
    nebius --profile "$first_profile" iam v2 access-key list --parent-id "$parent" --format json \
      | jq -r --arg sa "$first_sa" '.items[] | select(.spec.account.service_account.id == $sa) | "\(.metadata.id) \(.metadata.created_at)"' | while read -r id created; do
        [ "$id" = "$kid" ] && continue
        if older_than "$created"; then nebius --profile "$first_profile" iam v2 access-key delete --id "$id" >/dev/null && echo "rotate: deleted access key $id ($created)" || echo "rotate: delete access key $id failed" >&2; fi
      done
  else echo "rotate: access-key create failed" >&2; rc=1; fi
fi
exit $rc
