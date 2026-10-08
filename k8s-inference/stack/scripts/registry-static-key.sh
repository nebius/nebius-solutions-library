#!/usr/bin/env bash
# External data source: a static Container Registry key of the hub ops service account for the image caches
# (Zot sync credentials). The Terraform provider has no resource for static keys, so the Nebius CLI issues it
# ONCE per operator and the result is cached at `cache` (stack/.secrets/, gitignored); every later plan reads
# the cache. A second operator gets a key of their own; retire old ones with `nebius iam static-key delete`.
# Query (JSON on stdin): profile, project, sa_id, name, cache. Result: {"key": "<token>", "id": "<key id>"}.
set -euo pipefail
q=$(cat)
field() { printf '%s' "$q" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
profile=$(field profile); project=$(field project); sa=$(field sa_id); name=$(field name); cache=$(field cache)
if [ -s "$cache" ]; then cat "$cache"; exit 0; fi
expires=$(date -u -d "+365 days" +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -v+365d +%Y-%m-%dT00:00:00Z)
out=$(nebius --profile "$profile" iam static-key issue --parent-id "$project" --account-service-account-id "$sa" \
        --service CONTAINER_REGISTRY --name "$name-$(date -u +%Y%m%d%H%M)" --expires-at "$expires" --format json)
mkdir -p "$(dirname "$cache")"; umask 077
printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
token = d.get("token") or d.get("status", {}).get("token") or d.get("secret") or ""
kid = d.get("metadata", {}).get("id") or d.get("id") or ""
if not token:
    sys.exit("static key issued but no token field in the response: " + json.dumps(d)[:300])
print(json.dumps({"key": token, "id": kid}))' > "$cache"
cat "$cache"
