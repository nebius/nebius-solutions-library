#!/usr/bin/env bash
# The image cache's static Container Registry key of the hub ops service account (Secret registry/zot-sync-credentials,
# stack/platform/secrets.tf): an explicit operator command, run ONCE per fleet, never by Terraform (a plan issues no
# credential). The Terraform provider has no resource for static keys, so the Nebius CLI (active profile) issues it.
#
#   stack/scripts/registry-static-key.sh <fleet name> [--rotate] [--import <old cache file>] [--project <id> --sa <id>]
#
# Prints the `export REGISTRY_STATIC_KEY=...` line (the variable terraform.tfvars secrets.registry_key_env names) and
# keeps the key with its id under ~/.config/serverless2/<fleet name>/registry-key.json (0600; never in a checkout, so a
# second worktree does not mint a second key). Refuses to issue again while that file exists: `--rotate` issues a new
# key and prints the old id for `nebius iam static-key delete`. `--import` adopts the file the former data source
# cached under stack/.secrets/ (same shape: {"key": ..., "id": ...}). Project and SA come from the cloud stage's state
# (`./stack.sh tf cloud -- output -json`): hub project id and the hub's ops_service_account_id.
set -euo pipefail
usage() { echo "usage: $0 <fleet name> [--rotate] [--import <file>] [--project <hub project id> --sa <ops SA id>]" >&2; exit 2; }
[ $# -ge 1 ] || usage
name=$1; shift
rotate=0; import=""; project=""; sa=""
while [ $# -gt 0 ]; do
  case $1 in
    --rotate) rotate=1 ;;
    --import) import=${2:?}; shift ;;
    --project) project=${2:?}; shift ;;
    --sa) sa=${2:?}; shift ;;
    *) usage ;;
  esac
  shift
done
dir="${SERVERLESS2_CONFIG_DIR:-$HOME/.config/serverless2}/$name"
file="$dir/registry-key.json"
mkdir -p "$dir"; chmod 700 "$dir"; umask 077
emit() { python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("export REGISTRY_STATIC_KEY=" + repr(d["key"]) + "   # key id " + d.get("id", "?") + " (" + sys.argv[1] + ")")' "$file"; }

if [ -n "$import" ]; then
  [ -s "$import" ] || { echo "!! $import is empty or missing" >&2; exit 1; }
  [ -s "$file" ] && [ "$rotate" = 0 ] && { echo "!! $file exists; --rotate to replace it" >&2; exit 1; }
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); assert d.get("key"), "no key field"' "$import"
  cp "$import" "$file.tmp"
  if [ "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("id", ""))' "$file.tmp")" = "" ]; then
    # the former data source recorded "" as every key's id: resolve it from the token (static-key find)
    token=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["key"])' "$file.tmp")
    kid=$(nebius iam static-key find --token "$token" --format json 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin).get("metadata", {}).get("id", ""))' || true)
    python3 -c 'import json, sys; f = sys.argv[1]; d = json.load(open(f)); d["id"] = sys.argv[2]; json.dump(d, open(f, "w"))' "$file.tmp" "$kid"
  fi
  mv "$file.tmp" "$file"; chmod 600 "$file"
  echo "imported $import -> $file" >&2; emit; exit 0
fi

exec 9>"$dir/.registry-key.lock"; flock -w 60 9 || { echo "!! another issue is running for $name" >&2; exit 1; }
if [ -s "$file" ] && [ "$rotate" = 0 ]; then
  echo "key already issued for $name ($file); --rotate for a new one" >&2; emit; exit 0
fi
old_id=""; [ -s "$file" ] && old_id=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("id", ""))' "$file")

if [ -z "$project" ] || [ -z "$sa" ]; then
  here=$(cd "$(dirname "$0")/../.." && pwd)
  cloud=$("$here/stack.sh" tf cloud -- output -json 2>/dev/null) || { echo "!! cannot read the cloud stage's outputs (run from the fleet checkout with its profile and state credentials, or pass --project and --sa)" >&2; exit 1; }
  read -r project sa < <(printf '%s' "$cloud" | python3 -c '
import json, sys
o = json.load(sys.stdin)
cloud = o["cloud"]["value"] if "cloud" in o else o
hub = cloud["hub_id"] if "hub_id" in cloud else next(cid for cid, c in cloud["clusters"].items() if c.get("roles", {}).get("hub") or c.get("hub"))
c = cloud["clusters"][hub]
print(c["project_id"], c["ops_service_account_id"])')
fi
expires=$(date -u -d "+365 days" +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -v+365d +%Y-%m-%dT00:00:00Z)
suffix=$(python3 -c 'import secrets; print(secrets.token_hex(3))')
out=$(nebius iam static-key issue --parent-id "$project" --account-service-account-id "$sa" \
        --service CONTAINER_REGISTRY --name "$name-image-cache-$suffix" --expires-at "$expires" --format json)
# IssueStaticKeyResponse (nebius/iam/v1/static_key_service.proto) is {token, operation}: the key's id is the
# operation's resource_id (the former parser read metadata.id / id and recorded "" for every key). When the response
# carries no id, `static-key find --token` resolves the key from its own token (the exact ownership proof).
printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
token = d.get("token") or d.get("status", {}).get("token") or d.get("secret") or ""
op = d.get("operation") or {}
kid = op.get("resource_id") or op.get("resourceId") or d.get("metadata", {}).get("id") or d.get("id") or ""
if not token:
    sys.exit("static key issued but no token field in the response: " + json.dumps(d)[:300])
print(json.dumps({"key": token, "id": kid}))' > "$file.tmp"
if [ "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["id"])' "$file.tmp")" = "" ]; then
  token=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["key"])' "$file.tmp")
  kid=$(nebius iam static-key find --token "$token" --format json 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin).get("metadata", {}).get("id", ""))' || true)
  python3 -c 'import json, sys; f = sys.argv[1]; d = json.load(open(f)); d["id"] = sys.argv[2]; json.dump(d, open(f, "w"))' "$file.tmp" "$kid"
  [ -n "$kid" ] || echo "!! key issued (name $name-image-cache-$suffix) but its id could not be resolved; find it with: nebius iam static-key find --token <token>" >&2
fi
mv "$file.tmp" "$file"; chmod 600 "$file"
echo "issued key for $name -> $file" >&2
[ -n "$old_id" ] && echo "previous key id $old_id: apply platform on every cluster with the new value, then: nebius iam static-key delete --id $old_id" >&2
emit
