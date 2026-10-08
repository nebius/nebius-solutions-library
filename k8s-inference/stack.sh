#!/usr/bin/env bash
# The fleet in three Terraform stages, driven by ONE file: terraform.tfvars (README.md).
#
#   ./stack.sh preflight                      validate the tfvars and every pool against the Nebius compatibility matrix
#   ./stack.sh plan|apply|destroy [all]       all stages in dependency order (destroy: reverse)
#   ./stack.sh plan|apply|destroy cloud       stage 1: clusters, pools, identities, registry, buckets (one state)
#   ./stack.sh plan|apply|destroy platform <cluster-id>   stage 2: the platform on one cluster
#   ./stack.sh plan|apply|destroy models <cluster-id>     stage 3: tenants and models on one cluster
#   ./stack.sh output cloud [name]            outputs of a stage (`output platform <id>`, `output models <id>`)
#   ./stack.sh tf <stage> [<cluster-id>] -- <terraform args>   any terraform command against a stage's state
#   ./stack.sh clusters                       the cluster ids and roles derived from terraform.tfvars
#   ./stack.sh registry                       the registry the cloud stage created (tools/images.sh pushes there)
#   ./stack.sh check                          render everything and run the tests, no cloud access (tools/check.sh)
#
# Environment: TFVARS (default ./terraform.tfvars); AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for the state bucket
# (else sourced from STATE_CREDS, default ~/.config/<fleet name>/tfstate.env written by stack/bootstrap/state-bucket.sh);
# AUTO_APPROVE=1 to skip the apply/destroy prompts; secret inputs named in terraform.tfvars `secrets` (NGC_API_KEY, ...).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TFVARS="${TFVARS:-$ROOT/terraform.tfvars}"
[ -f "$TFVARS" ] || { echo "no $TFVARS (copy terraform.tfvars.example)" >&2; exit 2; }
cmd="${1:-}"; shift || true
export TF_IN_AUTOMATION=1
[ "$cmd" = check ] && exec "$ROOT/tools/check.sh" "$@"

# The plan stack.sh follows (cluster ids, roles, order, state bucket) is derived from the tfvars by the
# config root (stack/config/locals.tf `plan`), so this script never parses HCL itself.
plan_json() {
  (cd "$ROOT/stack/config" && terraform init -input=false > /dev/null && echo 'jsonencode(local.plan)' | terraform console -var-file="$TFVARS") \
    | python3 -c 'import json,sys; print(json.dumps(json.loads(json.loads(sys.stdin.read()))))'
}
PLAN="$(plan_json)"
pq() { printf '%s' "$PLAN" | python3 -c "import json,sys; p=json.load(sys.stdin); print($1)"; }
NAME=$(pq 'p["name"]'); BUCKET=$(pq 'p["state"]["bucket"]'); REGION=$(pq 'p["state"]["region"]'); ENDPOINT=$(pq 'p["state"]["endpoint"]')
ORDER=$(pq '" ".join(p["order"])')
HUB=$(pq 'p["hub_id"]')

creds() {
  if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    local f="${STATE_CREDS:-$HOME/.config/$NAME/tfstate.env}"
    [ -r "$f" ] || { echo "!! no AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for the state bucket and no $f (run stack/bootstrap/state-bucket.sh)" >&2; exit 4; }
    set -a; . "$f"; set +a
  fi
}

tf_init() { # <stage> [<cluster-id>]
  local stage="$1" id="${2:-}" key dir="$ROOT/stack/$1"
  case "$stage" in
    cloud) key="cloud/fleet.tfstate" ;;
    platform|models) [ -n "$id" ] || { echo "usage: $stage <cluster-id>" >&2; exit 2; }; key="$stage/$id.tfstate" ;;
    *) echo "unknown stage $stage (cloud|platform|models)" >&2; exit 2 ;;
  esac
  creds
  # One working directory per stage AND cluster: `init` writes the backend key into TF_DATA_DIR, and a second
  # cluster's init in the same directory would re-key an apply that is still starting (two stage runs of the
  # same stage may overlap, e.g. platform hub and platform eu-west2).
  export TF_DATA_DIR="$dir/.terraform${id:+-$id}"
  terraform -chdir="$dir" init -input=false -reconfigure \
    -backend-config="$ROOT/stack/backend.hcl" \
    -backend-config="bucket=$BUCKET" -backend-config="key=$key" -backend-config="region=$REGION" \
    -backend-config="endpoints={s3=\"$ENDPOINT\"}" > /dev/null
}

run() { # <tf command> <stage> [<cluster-id>] [extra args...]
  local tfcmd="$1" stage="$2" id="" ; shift 2
  case "$stage" in platform|models) id="${1:?cluster id}"; shift ;; esac
  tf_init "$stage" "$id"
  local dir="$ROOT/stack/$stage" args=(-input=false -var-file="$TFVARS")
  [ -z "$id" ] || args+=(-var "target=$id")
  case "$tfcmd" in
    apply|destroy) [ "${AUTO_APPROVE:-0}" = 1 ] && args+=(-auto-approve) ;;
  esac
  echo "== $tfcmd $stage ${id:+$id }(state $BUCKET/$( [ -z "$id" ] && echo cloud/fleet || echo "$stage/$id" ).tfstate)"
  terraform -chdir="$dir" "$tfcmd" "${args[@]}" "$@"
}

case "$cmd" in
  clusters) pq 'json.dumps({"order": p["order"], "clusters": p["clusters"], "dedicated": p["dedicated"], "hub": p["hub_id"]}, indent=1)' ;;
  registry) tf_init cloud; terraform -chdir="$ROOT/stack/cloud" output -json hub | python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"])' ;;
  preflight) python3 "$ROOT/stack/scripts/preflight.py" "$TFVARS" <<< "$PLAN" ;;
  plan|apply|destroy)
    target="${1:-all}"; shift || true
    case "$target" in
      all)
        if [ "$cmd" = destroy ]; then
          # models: every cluster reads the hub's tenant storage from the hub's models state, so the hub goes last
          for id in $(echo "$ORDER" | tr ' ' '\n' | tac | grep -vx "$HUB"; echo "$HUB"); do run destroy models "$id" "$@"; done
          for id in $(echo "$ORDER" | tr ' ' '\n' | tac); do run destroy platform "$id" "$@"; done
          run destroy cloud "$@"
        else
          run "$cmd" cloud "$@"
          for id in $ORDER; do run "$cmd" platform "$id" "$@"; done
          for id in $ORDER; do run "$cmd" models "$id" "$@"; done
        fi ;;
      cloud) run "$cmd" cloud "$@" ;;
      platform|models) run "$cmd" "$target" "$@" ;;
      *) echo "target: all | cloud | platform <id> | models <id>" >&2; exit 2 ;;
    esac ;;
  output)
    stage="${1:?stage}"; shift; id=""
    case "$stage" in platform|models) id="${1:?cluster id}"; shift ;; esac
    tf_init "$stage" "$id"; terraform -chdir="$ROOT/stack/$stage" output "$@" ;;
  tf)
    stage="${1:?stage}"; shift; id=""
    case "$stage" in platform|models) id="${1:?cluster id}"; shift ;; esac
    [ "${1:-}" = "--" ] && shift
    tf_init "$stage" "$id"; terraform -chdir="$ROOT/stack/$stage" "$@" ;;
  *) sed -n 2,18p "$0" >&2; exit 2 ;;
esac
