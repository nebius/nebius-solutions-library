#!/usr/bin/env bash
# One-time, idempotent: the fleet's Terraform state bucket and the identity that reads and writes it
# (README.md step 1). Everything else of the fleet is Terraform with its state in this bucket.
#
#   stack/bootstrap/state-bucket.sh            # reads terraform.tfvars (name, hub project, profile)
#
# In the hub project: bucket <name>-tfstate (versioning ENABLED), service account and group <name>-tfstate
# with storage.object-editor on that bucket only (bucket policy), and one S3 access key of the SA written to
# $STATE_CREDS (default ~/.config/<name>/tfstate.env, mode 0600; the AWS variables the s3 backend reads).
# A second operator runs the script with their own CLI profile: bucket/SA/group are found, only a new key is
# issued for them (STATE_NEW_KEY=1 forces a new key).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TFVARS="${TFVARS:-$ROOT/terraform.tfvars}"
PLAN=$(cd "$ROOT/stack/config" && terraform init -input=false > /dev/null && echo 'jsonencode(local.plan)' | terraform console -var-file="$TFVARS" | python3 -c 'import json,sys; print(json.dumps(json.loads(json.loads(sys.stdin.read()))))')
pq() { printf '%s' "$PLAN" | python3 -c "import json,sys; p=json.load(sys.stdin); print($1)"; }
NAME=$(pq 'p["name"]'); PROFILE=$(pq 'p["profile"]'); BUCKET=$(pq 'p["state"]["bucket"]'); PROJECT=$(pq 'p["clusters"][p["hub_id"]]["project_id"]')
CREDS="${STATE_CREDS:-$HOME/.config/$NAME/tfstate.env}"
n() { nebius --profile "$PROFILE" "$@"; }
find_id() { # <service list args...> <name>
  local name="${*: -1}"; n "${@:1:$#-1}" --parent-id "$PROJECT" --page-size 500 --format json \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(next((i["metadata"]["id"] for i in d.get("items",[]) if i["metadata"].get("name")==sys.argv[1]),""))' "$name"
}
sa=$(find_id iam service-account list "$BUCKET")
[ -n "$sa" ] || sa=$(n iam service-account create --parent-id "$PROJECT" --name "$BUCKET" --description "Terraform state access" --format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["id"])')
group=$(find_id iam group list "$BUCKET")
[ -n "$group" ] || group=$(n iam group create --parent-id "$PROJECT" --name "$BUCKET" --format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["id"])')
if ! n iam group-membership list-members --parent-id "$group" --page-size 500 --format json | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(sys.argv[1] in json.dumps(i) for i in d.get("items",[])) else 1)' "$sa"; then
  n iam group-membership create --parent-id "$group" --member-id "$sa" > /dev/null
fi
policy="[{\"paths\":[\"*\"],\"roles\":[\"storage.object-editor\"],\"group_id\":\"$group\"}]"
bucket=$(find_id storage bucket list "$BUCKET")
if [ -z "$bucket" ]; then
  bucket=$(n storage bucket create --parent-id "$PROJECT" --name "$BUCKET" --versioning-policy ENABLED --bucket-policy-rules "$policy" --format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["id"])')
else
  n storage bucket update --id "$bucket" --versioning-policy ENABLED --bucket-policy-rules "$policy" > /dev/null
fi
echo "state bucket $BUCKET ($bucket) in $PROJECT; SA $BUCKET ($sa), group $group: storage.object-editor on the bucket"
if [ -r "$CREDS" ] && [ "${STATE_NEW_KEY:-}" != 1 ]; then echo "credentials file $CREDS exists (STATE_NEW_KEY=1 issues another key)"; exit 0; fi
key=$(n iam v2 access-key create --parent-id "$PROJECT" --name "tfstate-$(whoami)-$(date -u +%Y%m%d)" --account-service-account-id "$sa" --secret-delivery-mode INLINE --format json)
mkdir -p "$(dirname "$CREDS")"; umask 077
printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' \
  "$(printf %s "$key" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"]["aws_access_key_id"])')" \
  "$(printf %s "$key" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"]["secret"])')" > "$CREDS"
echo "wrote $CREDS (access key $(printf %s "$key" | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["id"])')); stack.sh sources it"
