#!/bin/sh
# Restart (or delete) STOPPED VMs of the given Managed Kubernetes node groups (CronJob `recover-stopped-nodes`).
# NOTE: dash `echo` interprets backslash escapes, which corrupts JSON: always printf "%s\n".
# A spot VM preempted with on_preemption=STOP stays in the node group as a NotReady node and keeps
# counting against max_node_count; nothing restarts it. `instance start` brings the same node back
# (same name, cached images) in about a minute when capacity is available; if the start is refused
# (no capacity) the instance is deleted so the node-group controller replaces it.
# env: NEBIUS_PARENT_ID, DELETE_ON_START_FAILURE (default true), and either NODE_GROUP_IDS
# (comma-separated) or NEBIUS_CLUSTER_ID: with a cluster id and no explicit list, every preemptible
# node group of that cluster is covered automatically (new pools need no manifest change).
set -u
: "${NEBIUS_PARENT_ID:?}"
DELETE_ON_START_FAILURE=${DELETE_ON_START_FAILURE:-true}
if [ -z "${NODE_GROUP_IDS:-}" ]; then
  : "${NEBIUS_CLUSTER_ID:?NODE_GROUP_IDS or NEBIUS_CLUSTER_ID is required}"
  NODE_GROUP_IDS=$(nebius mk8s node-group list --parent-id "$NEBIUS_CLUSTER_ID" --page-size 999 --format json \
    | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | tr -d '\000-\011\013\014\016-\037' \
    | jq -r '[.items[]? | select(.spec.template.preemptible != null) | .metadata.id] | join(",")')
  [ -z "$NODE_GROUP_IDS" ] && { echo "$(date -u +%FT%TZ) no preemptible node groups in $NEBIUS_CLUSTER_ID"; exit 0; }
fi
# the CLI occasionally interleaves a progress/ANSI sequence into piped stdout: strip control chars, retry once
fetch() { nebius compute instance list --parent-id "$NEBIUS_PARENT_ID" --page-size 999 --format json | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | tr -d '\000-\011\013\014\016-\037'; }
list=$(fetch); printf "%s\n" "$list" | jq -e . >/dev/null 2>&1 || { sleep 5; list=$(fetch); }
printf "%s\n" "$list" | jq -e . >/dev/null 2>&1 || { echo "list failed or not JSON"; exit 1; }
stopped=$(printf "%s\n" "$list" | jq -r --arg ngs "$NODE_GROUP_IDS" '
  ($ngs | split(",")) as $g
  | .items[]? | select(.status.state == "STOPPED" and ((.metadata.labels["mk8s-node-group-id"] // "") as $n | $g | index($n)))
  | "\(.metadata.id) \(.metadata.name) \(.metadata.labels["mk8s-node-group-id"])"')
total=$(printf "%s\n" "$list" | jq -r --arg ngs "$NODE_GROUP_IDS" '($ngs|split(",")) as $g | [.items[]? | select((.metadata.labels["mk8s-node-group-id"] // "") as $n | $g | index($n))] | map(.status.state) | group_by(.) | map("\(.[0])=\(length)") | join(" ")')
echo "$(date -u +%FT%TZ) node groups $NODE_GROUP_IDS: ${total:-no instances}"
[ -z "$stopped" ] && exit 0
printf "%s\n" "$stopped" | while read -r id name ng; do
  t0=$(date +%s)
  echo "$(date -u +%FT%TZ) $id ($name, $ng) is STOPPED: starting"
  if out=$(nebius compute instance start --id "$id" 2>&1); then
    echo "$(date -u +%FT%TZ) $id started in $(( $(date +%s) - t0 )) s"
  else
    echo "$(date -u +%FT%TZ) $id start failed: $(echo "$out" | head -3 | tr '\n' ' ')"
    if [ "$DELETE_ON_START_FAILURE" = "true" ]; then
      echo "$(date -u +%FT%TZ) $id deleting so the node group replaces it"
      nebius compute instance delete --id "$id" 2>&1 | tail -1
    fi
  fi
done
