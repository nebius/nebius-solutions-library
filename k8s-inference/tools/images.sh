#!/usr/bin/env bash
# The five platform images (api, dispatcher, jobs, ops, ui) in the fleet's own registry (README.md, step 5).
# Every cluster's cache pulls them from there as registry.serverless2.local/nebius/serverless2/<component>:<tag>.
#
#   tools/images.sh build [<registry>]              build and push all five with docker buildx
#   tools/images.sh copy <from-registry> [<registry>]   copy the five tags from another registry (crane, no build)
#   tools/images.sh list [<registry>]               the tags present in the registry
#
# <registry> is a prefix such as cr.eu-north1.nebius.cloud/<registry id>. Omitted, it is read from the cloud
# stage's output (`./stack.sh output cloud hub`, the registry the fleet created). Tags come from
# terraform.tfvars `images.versions` (defaults in stack/config/variables.tf).
# Login first: `nebius registry` credential helper (docker login through the Nebius CLI) or `crane auth login`.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TFVARS="${TFVARS:-$ROOT/terraform.tfvars}"

usage() { sed -n 2,12p "$0" >&2; exit 2; }

# component=tag pairs, ops first: the dispatcher image copies the Nebius CLI from the ops image.
versions() {
  (cd "$ROOT/stack/config" && terraform init -input=false > /dev/null && echo 'jsonencode(var.fleet.images.versions)' | terraform console -var-file="$TFVARS") \
    | python3 -c 'import json,sys; d=json.loads(json.loads(sys.stdin.read())); order=["ops","api","dispatcher","jobs","ui"]; print(" ".join(f"{k}={d[k]}" for k in order))'
}

registry() { # the argument, else the registry the cloud stage created
  if [ -n "${1:-}" ]; then printf '%s' "$1"; return; fi
  "$ROOT/stack.sh" registry
}

cmd="${1:-}"; shift || true
case "$cmd" in
  build)
    reg="$(registry "${1:-}")"
    for kv in $(versions); do
      c="${kv%%=*}"; t="${kv#*=}"
      case "$c" in
        ui)   ctx="$ROOT/ui";           df="$ROOT/ui/Dockerfile" ;;
        api)  ctx="$ROOT";              df="$ROOT/services/api/Dockerfile" ;;   # needs catalog/models in the build context
        *)    ctx="$ROOT/services/$c";  df="$ROOT/services/$c/Dockerfile" ;;
      esac
      extra=()
      [ "$c" = dispatcher ] && extra=(--build-arg "OPS_IMAGE=$reg/serverless2/ops:$ops_tag")
      [ "$c" = ops ] && ops_tag="$t"
      echo "== $c:$t"
      docker buildx build --push --platform linux/amd64 -t "$reg/serverless2/$c:$t" -f "$df" "${extra[@]}" "$ctx"
    done ;;
  copy)
    from="${1:?from registry}"; to="$(registry "${2:-}")"
    for kv in $(versions); do c="${kv%%=*}"; t="${kv#*=}"; echo "== $c:$t"; crane copy "$from/serverless2/$c:$t" "$to/serverless2/$c:$t"; done ;;
  list)
    reg="$(registry "${1:-}")"
    for kv in $(versions); do c="${kv%%=*}"; printf '%s: ' "$c"; crane ls "$reg/serverless2/$c" 2>/dev/null | tr '\n' ' '; echo; done ;;
  *) usage ;;
esac
