#!/usr/bin/env bash
# Delete every image from the fleet's registry so Terraform can delete the registry (Nebius refuses while any
# artifact remains: "Please remove all artifacts from registry!"). Runs at destroy time from the cloud stage.
# Uses the Registry service's image API through the Nebius CLI: deleting tags with crane/docker is not enough,
# the registry keeps the manifest objects (measured 2026-10-08: 10 artifacts left after every tag was gone).
#
#   stack/scripts/empty-registry.sh <registry id>      (the Nebius CLI's active profile)
set -euo pipefail
reg="${1:?registry id, e.g. registry-e00xxxxxxxxxxxxxxxxx}"
command -v nebius > /dev/null || { echo "!! empty-registry: the Nebius CLI is needed to empty $reg before Terraform deletes it" >&2; exit 1; }
n=0
for i in 1 2 3; do   # a few passes: index manifests reference their platform manifests
  ids=$(nebius registry image list --parent-id "$reg" --page-size 1000 --format json 2> /dev/null \
        | python3 -c 'import json,sys; print(" ".join(i["id"] for i in json.load(sys.stdin).get("items", [])))' 2> /dev/null || true)
  [ -n "$ids" ] || break
  for id in $ids; do nebius registry image delete --id "$id" > /dev/null 2>&1 && n=$((n + 1)) || true; done
done
echo "empty-registry: $n image object(s) deleted from $reg"
