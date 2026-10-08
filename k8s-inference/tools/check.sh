#!/usr/bin/env bash
# The quality gate, the same locally (`make check`, `./stack.sh check`) and in CI (.github/workflows/ci.yml).
# No cloud access: every Terraform stage and module must validate, terraform.tfvars.example must pass the
# schema validations, every chart and manifest must render for every cluster of the example, and the tests
# of the API and the dispatcher must pass.
#
#   tools/check.sh            everything
#   tools/check.sh terraform | render | tests      one part
#
# PYTHON (default python3) runs the tests; the reference fleet's own Terraform (infra/) is checked when present.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="${TFVARS_EXAMPLE:-$ROOT/terraform.tfvars.example}"
# tests run with the repo's virtualenv when it exists (`make venv` creates it from the requirements files)
if [ -z "${PYTHON:-}" ] && [ -x "$(dirname "$0")/../.venv-api/bin/python" ]; then PYTHON="$(cd "$(dirname "$0")/.." && pwd)/.venv-api/bin/python"; fi
PYTHON="${PYTHON:-python3}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export TF_IN_AUTOMATION=1
part="${1:-all}"

step() { echo "== $*"; }
tf() { # <dir> <terraform args...>: an init without backend into a data directory of its own
  local dir="$1"; shift
  TF_DATA_DIR="$ROOT/$dir/.terraform-check" terraform -chdir="$ROOT/$dir" "$@"
}
render() { # a Terraform expression over terraform.tfvars.example, as JSON
  echo "$1" | tf stack/config console -var-file="$EXAMPLE" | "$PYTHON" -c 'import json,sys; print(json.dumps(json.loads(json.loads(sys.stdin.read()))))'
}

check_terraform() {
  step "terraform fmt"
  terraform fmt -check -recursive "$ROOT/stack"
  [ -d "$ROOT/infra" ] && terraform fmt -check -recursive "$ROOT/infra"
  step "terraform validate: every stage and module"
  for d in stack/config stack/cloud stack/platform stack/models stack/modules/cluster stack/modules/tenant-region; do
    tf "$d" init -backend=false -input=false > /dev/null
    tf "$d" validate -no-color > /dev/null && echo "   ok $d"
  done
  if [ -d "$ROOT/infra/cluster" ]; then
    for d in infra/cluster infra/tenant; do tf "$d" init -backend=false -input=false > /dev/null; tf "$d" validate -no-color > /dev/null && echo "   ok $d"; done
  fi
  step "terraform.tfvars.example passes the schema validations"
  tf stack/config plan -input=false -lock=false -var-file="$EXAMPLE" > /dev/null && echo "   ok $(basename "$EXAMPLE")"
}

check_render() {
  tf stack/config init -backend=false -input=false > /dev/null
  render 'jsonencode(local.chart_fleet)' > "$TMP/fleet.json"
  render 'jsonencode(local.catalog)' > "$TMP/catalog.json"
  render 'jsonencode(local.plan)' > "$TMP/plan.json"
  clusters=$("$PYTHON" -c 'import json; p=json.load(open("'"$TMP"'/plan.json")); print(" ".join(p["order"]))')
  "$PYTHON" - "$TMP" <<'EOF'
import json, sys, yaml
tmp = sys.argv[1]
fleet = json.load(open(f"{tmp}/fleet.json")); plan = json.load(open(f"{tmp}/plan.json")); catalog = json.load(open(f"{tmp}/catalog.json"))
for cid, c in plan["clusters"].items():
    worker = c["roles"]["worker"]
    yaml.safe_dump({"cluster": cid, "fleet": fleet, "prepull": {"enabled": worker, "images": []}}, open(f"{tmp}/fleet-{cid}.yaml", "w"))
    cluster = {"region": c["region"], "bucket": "example-bucket", "endpoint": f"storage.{c['region']}.nebius.cloud"} if worker else {"region": c["region"], "manager": True}
    yaml.safe_dump({"name": "example", "cluster": cid, "fleet": fleet, "clusters": {cid: cluster}}, open(f"{tmp}/tenant-{cid}.yaml", "w"))
    for mid, e in catalog.items():
        if "runtime" in e and cid in e.get("deployments", {}):
            yaml.safe_dump(e, open(f"{tmp}/endpoint-{mid}-{cid}.yaml", "w"))
EOF
  step "helm lint"
  first=$(echo "$clusters" | cut -d' ' -f1)
  helm lint "$ROOT/charts/fleet" -f "$TMP/fleet-$first.yaml" > /dev/null && echo "   ok charts/fleet"
  helm lint "$ROOT/charts/tenant" -f "$TMP/tenant-$first.yaml" > /dev/null && echo "   ok charts/tenant"
  step "helm template: fleet and tenant charts for every cluster of the example, endpoint chart for every deployed entry"
  for cid in $clusters; do
    helm template fleet "$ROOT/charts/fleet" -f "$TMP/fleet-$cid.yaml" > /dev/null && echo "   ok charts/fleet $cid"
    helm template tenant "$ROOT/charts/tenant" -f "$TMP/tenant-$cid.yaml" > /dev/null && echo "   ok charts/tenant $cid"
    for f in "$TMP"/endpoint-*-"$cid".yaml; do
      [ -f "$f" ] || continue
      helm template m "$ROOT/charts/endpoint" -f "$f" --set "cluster=$cid" > /dev/null && echo "   ok charts/endpoint $(basename "$f" .yaml)"
    done
  done
  step "kustomize build: every shared manifest base"
  for d in "$ROOT"/clusters/common/manifests/*/; do
    [ -f "$d/kustomization.yaml" ] || continue
    kubectl kustomize "$d" > /dev/null && echo "   ok ${d#"$ROOT"/}"
  done
  if [ -d "$ROOT/clusters/hub" ]; then
    for d in "$ROOT"/clusters/*/apps/overlays/*/ "$ROOT"/clusters/fleet/root/ "$ROOT"/clusters/control/apps/; do
      [ -f "$d/kustomization.yaml" ] && kubectl kustomize "$d" > /dev/null
    done
    echo "   ok reference fleet overlays"
  fi
}

check_tests() {
  step "tests: API and dispatcher"
  (cd "$ROOT" && "$PYTHON" -m pytest -q services/api/tests services/dispatcher)
}

case "$part" in
  all) check_terraform; check_render; check_tests ;;
  terraform) check_terraform ;;
  render) check_render ;;
  tests) check_tests ;;
  *) sed -n 2,10p "$0" >&2; exit 2 ;;
esac
echo "== all checks passed"
