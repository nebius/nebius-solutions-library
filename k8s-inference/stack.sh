#!/usr/bin/env bash
# The fleet in two Terraform stages, driven by ONE file: terraform.tfvars (README.md).
#
#   ./stack.sh preflight                      validate the tfvars and every pool against the Nebius compatibility matrix
#   ./stack.sh plan|apply|destroy [all]       both stages in dependency order: cloud, then platform on every cluster (destroy: reverse)
#   ./stack.sh plan|apply|destroy cloud       stage 1: clusters, pools, identities, registry, buckets, the database (one state)
#   ./stack.sh plan|apply|destroy platform <cluster-id>   stage 2: the platform, tenants and keys on one cluster (one state per cluster)
#   ./stack.sh output cloud [name]            outputs of a stage (`output platform control` prints the URLs and `-json tenant_keys` the keys)
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
# Authentication, the same way as the library's other solutions: an IAM access token of the Nebius CLI's active
# profile (`nebius profile activate <name>` to switch), read by the Nebius provider from NEBIUS_IAM_TOKEN and
# passed to the Kubernetes/Helm providers as TF_VAR_iam_token (an ephemeral variable: never in a plan file or the
# state; `tf platform <id> -- apply <saved plan>` takes the token this script exports at apply time, however old the
# plan is). The CLI hands out its cached token, whose remaining lifetime is unknown here, so the token is fetched
# again before every stage run (a token that expires inside one stage fails that stage with "Unauthorized"; re-run
# it). Set NEBIUS_IAM_TOKEN yourself (CI: a service-account token) to skip the CLI calls.
TOKEN_FROM_CLI=0
[ -n "${NEBIUS_IAM_TOKEN:-}" ] || TOKEN_FROM_CLI=1
token() { [ "$TOKEN_FROM_CLI" = 1 ] && NEBIUS_IAM_TOKEN="$(nebius iam get-access-token)"; export NEBIUS_IAM_TOKEN TF_VAR_iam_token="$NEBIUS_IAM_TOKEN"; }
token

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

# Secret inputs: terraform.tfvars `secrets` names the environment variables; each becomes TF_VAR_<input> of the
# platform stage. A named variable that is unset stops a platform plan/apply/destroy before anything runs (an apply
# without the NGC key would otherwise delete the NGC Secrets and the cache's credentials; set the name in the tfvars
# to "" to run a fleet without that secret on purpose). Only the platform stage consumes them: the cloud stage,
# `output`, `registry`, `clusters` and any read-only `tf` call run without (stack/scripts/registry-static-key.sh
# discovers the hub project and ops SA through `output cloud` BEFORE the registry key exists).
secret_inputs() {
  local pair input name missing=0
  for pair in $(pq '" ".join(f"{k}={v}" for k, v in p["secrets"].items() if v)'); do
    input="${pair%%=*}"; name="${pair#*=}"
    if [ -z "${!name:-}" ]; then echo "!! $name is not set (terraform.tfvars secrets: $input); export it or set the name to \"\" in the tfvars" >&2; missing=1
    else export "TF_VAR_$input=${!name}"; fi
  done
  [ "$missing" = 0 ] || exit 4
}

creds() {
  if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    local f="${STATE_CREDS:-$HOME/.config/$NAME/tfstate.env}"
    [ -r "$f" ] || { echo "!! no AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY for the state bucket and no $f (run stack/bootstrap/state-bucket.sh)" >&2; exit 4; }
    # The operator's state-bucket credentials are loaded at runtime.
    set -a
    # shellcheck source=/dev/null
    . "$f"
    set +a
  fi
}

tf_init() { # <stage> [<cluster-id>]
  local stage="$1" id="${2:-}" key dir="$ROOT/stack/$1"
  case "$stage" in
    cloud) key="cloud/fleet.tfstate" ;;
    platform) [ -n "$id" ] || { echo "usage: $stage <cluster-id>" >&2; exit 2; }; key="$stage/$id.tfstate" ;;
    *) echo "unknown stage $stage (cloud|platform)" >&2; exit 2 ;;
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
  case "$stage" in platform) id="${1:?cluster id}"; shift ;; esac
  token
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
    case "$target" in all|platform) secret_inputs ;; esac
    case "$target" in
      all)
        if [ "$cmd" = destroy ]; then
          for id in $(echo "$ORDER" | tr ' ' '\n' | tac); do run destroy platform "$id" "$@"; done
          run destroy cloud "$@"
        else
          run "$cmd" cloud "$@"
          for id in $ORDER; do run "$cmd" platform "$id" "$@"; done
        fi ;;
      cloud) run "$cmd" cloud "$@" ;;
      platform) run "$cmd" "$target" "$@" ;;
      *) echo "target: all | cloud | platform <id>" >&2; exit 2 ;;
    esac ;;
  output)
    stage="${1:?stage}"; shift; id=""
    case "$stage" in platform) id="${1:?cluster id}"; shift ;; esac
    tf_init "$stage" "$id"; terraform -chdir="$ROOT/stack/$stage" output "$@" ;;
  tf)
    stage="${1:?stage}"; shift; id=""
    case "$stage" in platform) id="${1:?cluster id}"; shift ;; esac
    [ "${1:-}" = "--" ] && shift
    case "$stage/${1:-}" in platform/plan|platform/apply|platform/destroy) secret_inputs ;; esac
    tf_init "$stage" "$id"; terraform -chdir="$ROOT/stack/$stage" "$@" ;;
  *) sed -n 2,18p "$0" >&2; exit 2 ;;
esac
