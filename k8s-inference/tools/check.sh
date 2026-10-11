#!/usr/bin/env bash
# The quality gate, the same locally (`make check`, `./stack.sh check`) and in CI (.github/workflows/k8s-inference.yml).
# No cloud access: every Terraform stage and module must validate, terraform.tfvars.example must pass the
# schema validations, every chart must render for every cluster of the example (charts/platform equal to its
# golden set), and the tests of the API and the dispatcher must pass.
#
#   tools/check.sh            everything
#   tools/check.sh terraform | render | tests      one part
#   UPDATE_GOLDEN=1 tools/check.sh render          after a deliberate manifest change: rewrite stack/platform/tests/golden
#
# PYTHON (default python3) runs the tests.
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
render() { # a Terraform expression over terraform.tfvars.example (or TFVARS_EXAMPLE), as JSON
  echo "$1" | tf stack/config console -var-file="${TFVARS_EXAMPLE:-$EXAMPLE}" | "$PYTHON" -c 'import json,sys; print(json.dumps(json.loads(json.loads(sys.stdin.read()))))'
}

check_terraform() {
  step "terraform fmt"
  terraform fmt -check -recursive "$ROOT/stack"
  [ -d "$ROOT/infra" ] && terraform fmt -check -recursive "$ROOT/infra"
  step "terraform validate: every stage and module"
  for d in stack/config stack/cloud stack/platform stack/modules/cluster stack/modules/tenant-region; do
    tf "$d" init -backend=false -input=false > /dev/null
    tf "$d" validate -no-color > /dev/null; echo "   ok $d"
  done
  step "terraform.tfvars.example passes the schema validations"
  tf stack/config plan -input=false -lock=false -var-file="$EXAMPLE" > /dev/null; echo "   ok $(basename "$EXAMPLE")"
}

check_render() {
  tf stack/config init -backend=false -input=false > /dev/null
  render 'jsonencode(local.chart_fleet)' > "$TMP/fleet.json"
  render 'jsonencode(local.catalog)' > "$TMP/catalog.json"
  render 'jsonencode(local.plan)' > "$TMP/plan.json"
  render 'jsonencode(local.platform_values)' > "$TMP/platform-values.json"
  clusters=$("$PYTHON" -c 'import json; p=json.load(open("'"$TMP"'/plan.json")); print(" ".join(p["order"]))')
  "$PYTHON" -c 'import json, sys, yaml
tmp = sys.argv[1]
for cid, v in json.load(open(f"{tmp}/platform-values.json")).items():
    yaml.safe_dump(v, open(f"{tmp}/platform-{cid}.yaml", "w"))' "$TMP"
  "$PYTHON" - "$TMP" <<'EOF'
import json, sys, yaml
tmp = sys.argv[1]
fleet = json.load(open(f"{tmp}/fleet.json")); plan = json.load(open(f"{tmp}/plan.json")); catalog = json.load(open(f"{tmp}/catalog.json"))
for cid, c in plan["clusters"].items():
    worker = c["roles"]["worker"]
    yaml.safe_dump({"cluster": cid, "fleet": fleet, "prepull": {"enabled": worker, "images": []}}, open(f"{tmp}/fleet-{cid}.yaml", "w"))
    cluster = {"region": c["region"]} if worker else {"region": c["region"], "manager": True}
    yaml.safe_dump({"name": "example", "cluster": cid, "fleet": fleet, "clusters": {cid: cluster}}, open(f"{tmp}/tenant-{cid}.yaml", "w"))
    for mid, e in catalog.items():
        if "runtime" in e and cid in e.get("deployments", {}):
            yaml.safe_dump(e, open(f"{tmp}/endpoint-{mid}-{cid}.yaml", "w"))
EOF
  step "helm lint"
  first=$(echo "$clusters" | cut -d' ' -f1)
  helm lint "$ROOT/charts/fleet" -f "$TMP/fleet-$first.yaml" > /dev/null; echo "   ok charts/fleet"
  helm lint "$ROOT/charts/tenant" -f "$TMP/tenant-$first.yaml" > /dev/null; echo "   ok charts/tenant"
  helm lint "$ROOT/charts/platform" -f "$TMP/platform-$first.yaml" > /dev/null; echo "   ok charts/platform"
  step "helm template: fleet and tenant charts for every cluster of the example; every deployed entry through the API's renderer and the reference chart"
  for cid in $clusters; do
    helm template fleet "$ROOT/charts/fleet" -f "$TMP/fleet-$cid.yaml" > /dev/null; echo "   ok charts/fleet $cid"
    helm template tenant "$ROOT/charts/tenant" -f "$TMP/tenant-$cid.yaml" > /dev/null; echo "   ok charts/tenant $cid"
    for f in "$TMP"/endpoint-*-"$cid".yaml; do
      [ -f "$f" ] || continue
      "$PYTHON" "$ROOT/services/api/models.py" render "$cid" < "$f" > /dev/null; echo "   ok render_endpoint $(basename "$f" .yaml)"
      helm template m "$ROOT/charts/endpoint" -f "$f" --set "cluster=$cid" > /dev/null; echo "   ok charts/endpoint $(basename "$f" .yaml)"
    done
  done
  step "example model (stack/platform/llm-example.json): the API's spec -> entry -> rendered objects, for the example's first GPU class"
  "$PYTHON" - "$TMP" "$ROOT" <<'EOF'
import json, subprocess, sys
tmp, root = sys.argv[1], sys.argv[2]
fleet = json.load(open(f"{tmp}/fleet.json")); plan = json.load(open(f"{tmp}/plan.json"))
# the pools the way the API reads them from fleet-prices (charts/fleet pools.yaml): region id, pool, GPU class, capacity
pools = {f"{r['id']}-{pn}": {"region": r["id"], "pool": pn, "gpu_class": p["gpu_class"], "capacity": p["capacity"]["type"]}
         for rn, r in fleet["regions"].items() for pn, p in r["pools"].items()}
classes = sorted({p["gpu_class"] for p in pools.values() if p.get("gpu_class")})
spec = dict(json.load(open(f"{root}/stack/platform/llm-example.json")), gpu={"count": 1, "classes": classes[:1]})
json.dump(pools, open(f"{tmp}/pools.json", "w"))
r = subprocess.run([sys.executable, f"{root}/services/api/models.py", "entry", "--pools", f"{tmp}/pools.json"], input=json.dumps(spec), capture_output=True, text=True)
if r.returncode: print(r.stderr); sys.exit(1)
open(f"{tmp}/llm-example.yaml", "w").write(r.stdout)
import yaml; e = yaml.safe_load(r.stdout)
print(f"   ok entry llm-example: class {classes[:1]}, deployments {sorted(e['deployments'])}")
json.dump(sorted(e["deployments"]), open(f"{tmp}/llm-example-clusters.json", "w"))
EOF
  for cid in $("$PYTHON" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))))' "$TMP/llm-example-clusters.json"); do
    "$PYTHON" "$ROOT/services/api/models.py" render "$cid" < "$TMP/llm-example.yaml" > /dev/null; echo "   ok render_endpoint llm-example $cid"
    helm template llm-example "$ROOT/charts/endpoint" -f "$TMP/llm-example.yaml" --set "cluster=$cid" > /dev/null; echo "   ok charts/endpoint llm-example $cid"
  done
  # The image cache as an add-on (docs/IMAGES.md): the example with `images.cache.enabled = false` renders both charts
  # without the cache's objects and with the platform images pulled from the registry itself.
  step "image cache off: charts/fleet and charts/platform render without the cache"
  sed 's/cache = {/cache = { enabled = false, /' "$EXAMPLE" > "$TMP/nocache.tfvars"
  grep -q 'enabled = false' "$TMP/nocache.tfvars" || { echo "!! the example has no images.cache block to switch off" >&2; exit 1; }
  TFVARS_EXAMPLE="$TMP/nocache.tfvars" render 'jsonencode({ fleet = local.chart_fleet, values = local.platform_values, prepull = local.prepull_by_cluster })' > "$TMP/nocache.json"
  "$PYTHON" - "$TMP" "$ROOT" <<'EOF2'
import json, subprocess, sys, yaml
tmp, root = sys.argv[1], sys.argv[2]
d = json.load(open(f"{tmp}/nocache.json"))
def render(chart, values):
    yaml.safe_dump(values, open(f"{tmp}/nocache-values.yaml", "w"))
    r = subprocess.run(["helm", "template", "x", f"{root}/charts/{chart}", "-f", f"{tmp}/nocache-values.yaml"], capture_output=True, text=True)
    if r.returncode: sys.exit(r.stderr)
    return [o for o in yaml.safe_load_all(r.stdout) if o]
for cid, v in d["values"].items():
    worker = v["role"]["worker"]
    fleet = render("fleet", {"cluster": cid, "fleet": d["fleet"], "prepull": {"enabled": worker, "images": d["prepull"].get(cid, [])}})
    assert not [o for o in fleet if o["metadata"]["name"] in ("zot-config", "zot-pvc-zot-0")], f"{cid}: charts/fleet renders the cache without it"
    platform = render("platform", v)
    kinds = {(o["kind"], o["metadata"]["name"]): o for o in platform}
    assert ("DaemonSet", "containerd-registry-config") not in kinds, f"{cid}: the containerd DaemonSet is rendered without the cache"
    image = kinds[("Deployment", "api")]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.startswith(d["fleet"]["images"]["source"] + "/serverless2/api:"), f"{cid}: api image {image} is not pulled from the registry"
    env = {e["name"]: e.get("value") for e in kinds[("Deployment", "api")]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env.get("IMAGES_HOST", "") == "", f"{cid}: IMAGES_HOST is set without the cache"
    for img in d["prepull"].get(cid, []):
        assert not img["image"].startswith(d["fleet"]["images"]["host"] + "/"), f"{cid}: pre-pull image {img['image']} names the logical host without the cache"
    print(f"   ok cache off {cid}: no zot, no containerd DaemonSet, api from {image.rsplit('/', 2)[0]}")
EOF2
  # charts/platform for every cluster of the example against stack/platform/tests/golden/<cluster>.json: the objects
  # (names, namespaces, labels, every field) and their components (the wave, hence the state address) must be
  # equal, so that an apply changes nothing on a live cluster. The golden set was first written from the kustomize
  # bases plus the stage's JSON patches that the chart replaced (2026-10-11); UPDATE_GOLDEN=1 rewrites it from the
  # chart after a deliberate change, and the diff of the golden files in the review shows exactly what changes.
  step "charts/platform: the render of every cluster equals its golden set (stack/platform/tests/golden)"
  for cid in $clusters; do
    helm template platform "$ROOT/charts/platform" -f "$TMP/platform-$cid.yaml" > "$TMP/platform-$cid.rendered.yaml"
    if [ "${UPDATE_GOLDEN:-0}" = 1 ]; then
      "$PYTHON" "$ROOT/stack/platform/tests/render.py" --update "$ROOT/stack/platform/tests/golden/$cid.json" < "$TMP/platform-$cid.rendered.yaml"
    else
      "$PYTHON" "$ROOT/stack/platform/tests/render.py" "$ROOT/stack/platform/tests/golden/$cid.json" < "$TMP/platform-$cid.rendered.yaml"
    fi
  done
}

check_tests() {
  step "tests: API and dispatcher"
  (cd "$ROOT" && "$PYTHON" -m pytest -q services/api/tests services/dispatcher)
  step "tests: filesystem attachment and GPU scale-to-zero storage"
  tf stack/modules/cluster init -backend=false -input=false > /dev/null
  tf stack/modules/cluster test -no-color
}

case "$part" in
  all) check_terraform; check_render; check_tests ;;
  terraform) check_terraform ;;
  render) check_render ;;
  tests) check_tests ;;
  *) sed -n 2,10p "$0" >&2; exit 2 ;;
esac
echo "== all checks passed"
