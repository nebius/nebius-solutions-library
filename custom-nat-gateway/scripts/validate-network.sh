#!/usr/bin/env bash
set -euo pipefail

query="$(cat)"
network_id="$(printf '%s' "$query" | jq -r '.network_id // empty')"
parent_id="$(printf '%s' "$query" | jq -r '.parent_id // empty')"

if [[ -z "$network_id" || -z "$parent_id" ]]; then
  printf '%s\n' '{"network_exists":"false","project_matches":"false","error":"network_id and parent_id are required"}'
  exit 0
fi

if ! network_json="$(/Users/realz/.nebius/bin/nebius vpc network get --id "$network_id" --format json 2>&1)"; then
  printf '{"network_exists":"false","project_matches":"false","error":%s}\n' "$(printf '%s' "$network_json" | jq -Rs .)"
  exit 0
fi

network_parent_id="$(printf '%s' "$network_json" | jq -r '.metadata.parent_id // .parent_id // empty')"
network_name="$(printf '%s' "$network_json" | jq -r '.metadata.name // .name // empty')"

project_matches="false"
if [[ "$network_parent_id" == "$parent_id" ]]; then
  project_matches="true"
fi

printf '{"network_exists":"true","project_matches":"%s","network_name":%s,"network_parent_id":%s,"error":""}\n' \
  "$project_matches" \
  "$(printf '%s' "$network_name" | jq -Rs .)" \
  "$(printf '%s' "$network_parent_id" | jq -Rs .)"
