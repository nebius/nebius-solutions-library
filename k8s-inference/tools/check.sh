#!/usr/bin/env bash
# The quality gate, the same locally (`make check`, `./stack.sh check`) and in CI (.github/workflows/k8s-inference.yml).
# No cloud access: every Terraform stage and module must validate, terraform.tfvars.example must pass the
# schema validations, every chart must render for every cluster of the example (charts/platform equal to its
# golden set), and the tests of the API and the dispatcher must pass.
#
#   tools/check.sh            everything
#   tools/check.sh lint | terraform | render | tests      one part
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

check_lint() {
  step "Python lint and format (Ruff)"
  (cd "$ROOT" && "$PYTHON" -m ruff check services stack && "$PYTHON" -m ruff format --check services stack)
  step "ShellCheck: fleet scripts and container entry points"
  shellcheck "$ROOT"/stack.sh "$ROOT"/stack/{scripts,bootstrap}/*.sh "$ROOT"/tools/*.sh "$ROOT"/services/{jobs,ops}/bin/*.sh
  step "terraform fmt"
  terraform fmt -check -recursive "$ROOT/stack"
}

check_terraform() {
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
    grants = sorted({a for t in (fleet.get("tenants") or {}).values() for a in (t.get("assets") or [])})  # every granted asset renders once
    yaml.safe_dump({"name": "example", "cluster": cid, "fleet": fleet, "clusters": {cid: cluster}, "assets": grants}, open(f"{tmp}/tenant-{cid}.yaml", "w"))
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
    helm template fleet "$ROOT/charts/fleet" -f "$TMP/fleet-$cid.yaml" > "$TMP/fleet-$cid.rendered.yaml"; echo "   ok charts/fleet $cid"
    # CPU-only admission (docs/FLEET.md "CPU-only runs"): the `system` flavor first in every queue with no GPU, the GPU pool
    # flavors after it with their node labels and GPU quota: a GPU-free workload lands on the system nodes, a GPU one on its pool
    "$PYTHON" - "$TMP/fleet-$cid.rendered.yaml" "$cid" <<'PYEOF2'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
cid = sys.argv[2]
flavors = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ResourceFlavor"}
queues = [d for d in docs if d["kind"] == "ClusterQueue"]
if not queues:
    sys.exit(0)
system = [n for n in flavors if n == "system" or n.endswith("-system")]
assert system, f"{cid}: no system flavor rendered"
for n in system:
    spec = flavors[n]["spec"]
    assert not spec.get("tolerations"), f"{cid}: {n} must not tolerate the GPU taint"
    if n == "system":
        assert spec.get("nodeLabels", {}).get("serverless2.nebius/pool") == "system", f"{cid}: {n} must select the system nodes"
        assert spec["nodeLabels"].get("serverless2.nebius/shared-fs") == "false", f"{cid}: {n} must say shared-fs=false (mount_on_system is off in the example)"
# CPU pools (docs/FLEET.md "CPU pools"): GPU-free flavors right after the system ones; the pool's own taint tolerated, never the GPU one
cpu = [n for n, d in flavors.items() if d["metadata"].get("labels", {}).get("serverless2.nebius/capacity") == "cpu"]
for n in cpu:
    spec = flavors[n]["spec"]
    tols = {(t["key"], t.get("value")) for t in spec.get("tolerations") or []}
    assert ("nvidia.com/gpu", "present") not in tols, f"{cid}: CPU pool flavor {n} tolerates the GPU taint"
    if spec.get("nodeLabels"):
        assert ("serverless2.nebius/cpu-pool", "present") in tols, f"{cid}: CPU pool flavor {n} does not tolerate its pool taint"
        assert spec["nodeLabels"].get("serverless2.nebius/pool") == n, f"{cid}: CPU pool flavor {n} selects another pool"
system = system + cpu  # the GPU-free group of every queue, in this order
# filesystem-aware placement (docs/FLEET.md): every worker flavor says whether its nodes mount /mnt/weights
for n, d in flavors.items():
    if d["spec"].get("nodeLabels"):
        assert d["spec"]["nodeLabels"].get("serverless2.nebius/shared-fs") in ("true", "false"), f"{cid}: flavor {n} carries no serverless2.nebius/shared-fs label"
# the example's two CPU pools: `prep` mounts (default), `scratch-only` says shared_filesystem = false explicitly (hasKey, not `default`)
for n, want in (("prep", "true"), ("scratch-only", "false")):
    if n in flavors:
        assert flavors[n]["spec"]["nodeLabels"].get("serverless2.nebius/shared-fs") == want, f"{cid}: CPU pool {n} must carry shared-fs={want}"
for q in queues:
    covered = q["spec"]["resourceGroups"][0]["coveredResources"]
    assert "ephemeral-storage" in covered, f"{cid}: {q['metadata']['name']} does not cover ephemeral-storage (a run requesting node-local scratch would be NoMatchingFlavor)"
    for f in q["spec"]["resourceGroups"][0]["flavors"]:
        res = {r["name"]: r for r in f["resources"]}
        assert "ephemeral-storage" in res, f"{cid}: {q['metadata']['name']}/{f['name']}: no ephemeral-storage entry"
        e = res["ephemeral-storage"]
        budget = e.get("nominalQuota") if e.get("nominalQuota") not in (None, 0) else e.get("borrowingLimit", e.get("nominalQuota"))
        assert str(budget).endswith("Gi") and int(str(budget)[:-2]) >= 0, f"{cid}: {q['metadata']['name']}/{f['name']}: ephemeral-storage budget {budget!r} (a quantity in Gi, zero allowed: a fleet may reserve the whole disk)"
    fl = q["spec"]["resourceGroups"][0]["flavors"]
    first = [f for f in fl if f["name"] in system]
    rest = [f for f in fl if f["name"] not in system]
    assert fl[:len(first)] == first and first, f"{cid}: {q['metadata']['name']}: system flavors must come first"
    names = [f["name"] for f in first]
    assert names == [n for n in names if n not in cpu] + [n for n in names if n in cpu], f"{cid}: {q['metadata']['name']}: CPU pools must follow the system flavors, never precede them"
    for f in first:
        res = {r["name"]: r for r in f["resources"]}
        assert res["nvidia.com/gpu"].get("nominalQuota") == 0 and not res["nvidia.com/gpu"].get("borrowingLimit"), f"{cid}: {q['metadata']['name']}/{f['name']}: GPUs must be 0"
        assert (res["cpu"].get("nominalQuota") or res["cpu"].get("borrowingLimit") or 0) > 0, f"{cid}: {q['metadata']['name']}/{f['name']}: no CPU budget"
    for f in rest:
        res = {r["name"]: r for r in f["resources"]}
        assert (res["nvidia.com/gpu"].get("nominalQuota") or res["nvidia.com/gpu"].get("borrowingLimit") or 0) > 0, f"{cid}: {q['metadata']['name']}/{f['name']}: a GPU pool flavor without GPUs"
        if f["name"] in flavors and flavors[f["name"]]["spec"].get("nodeLabels"):
            assert flavors[f["name"]]["spec"]["nodeLabels"]["serverless2.nebius/pool"] == f["name"], f"{cid}: {f['name']} selects another pool"
print(f"   ok charts/fleet {cid}: system flavor first ({len(system) - len(cpu)}), CPU pools next ({len(cpu)}), {sum(len(q['spec']['resourceGroups'][0]['flavors']) for q in queues)} flavor entries in {len(queues)} queues, ephemeral-storage covered and budgeted on every flavor")
PYEOF2
    helm template tenant "$ROOT/charts/tenant" -f "$TMP/tenant-$cid.yaml" > /dev/null; echo "   ok charts/tenant $cid"
    for f in "$TMP"/endpoint-*-"$cid".yaml; do
      [ -f "$f" ] || continue
      "$PYTHON" "$ROOT/services/api/models.py" render "$cid" < "$f" > /dev/null; echo "   ok render_endpoint $(basename "$f" .yaml)"
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
    if not v["role"]["control"]:
        assert ("Deployment", "api") not in kinds, f"{cid}: the API is the control cluster's only"
        print(f"   ok cache off {cid}: no zot, no containerd DaemonSet, no API (a GPU region)")
        continue
    image = kinds[("Deployment", "api")]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.startswith(d["fleet"]["images"]["source"] + "/serverless2/api:"), f"{cid}: api image {image} is not pulled from the registry"
    env = {e["name"]: e.get("value") for e in kinds[("Deployment", "api")]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env.get("IMAGES_HOST", "") == "", f"{cid}: IMAGES_HOST is set without the cache"
    for img in d["prepull"].get(cid, []):
        assert not img["image"].startswith(d["fleet"]["images"]["host"] + "/"), f"{cid}: pre-pull image {img['image']} names the logical host without the cache"
    print(f"   ok cache off {cid}: no zot, no containerd DaemonSet, api from {image.rsplit('/', 2)[0]}")
EOF2
  # charts/platform for every cluster of the example against stack/platform/tests/golden/<cluster>.json: one hash per
  # object (every field) plus its component (the wave, hence the state address), so a template change shows as the
  # list of objects it changed and an unintended change fails the gate. UPDATE_GOLDEN=1 records a deliberate change.
  step "charts/platform: the render of every cluster equals its record (stack/platform/tests/golden)"
  for cid in $clusters; do
    helm template platform "$ROOT/charts/platform" -f "$TMP/platform-$cid.yaml" > "$TMP/platform-$cid.rendered.yaml"
    if [ "${UPDATE_GOLDEN:-0}" = 1 ]; then
      "$PYTHON" "$ROOT/stack/platform/tests/render.py" --update "$ROOT/stack/platform/tests/golden/$cid.json" < "$TMP/platform-$cid.rendered.yaml"
    else
      "$PYTHON" "$ROOT/stack/platform/tests/render.py" "$ROOT/stack/platform/tests/golden/$cid.json" < "$TMP/platform-$cid.rendered.yaml"
    fi
  done
  # The dispatcher's RBAC against what its source calls: every BatchV1Api method used on the manager Jobs needs its
  # verb in the rendered ClusterRole (a live rollout of 0.2.11 found `list` missing: 403 on every tenant namespace,
  # the sweeper preserved everything), and the rule stays read-only (`get` is kept next to `list`: the single-Job read
  # of earlier versions, harmless and already granted on live clusters).
  step "charts/platform: the dispatcher ClusterRole covers the batch/jobs calls of services/dispatcher/dispatcher.py"
  # Security invariants of the rendered platform (what a reviewer of a public reference checks first): tenant pods
  # never reach the instance metadata service, and the API hostname is served over HTTPS only (port 80 redirects).
  step "charts/platform: tenant egress excludes the metadata service; the API route is HTTPS-only with a redirect"
  echo "$clusters" > "$TMP/clusters.txt"
  "$PYTHON" - "$TMP" "$ROOT" <<'EOF4'
import sys, yaml
tmp = sys.argv[1]
for cid in open(f"{tmp}/clusters.txt").read().split():
    objs = [o for o in yaml.safe_load_all(open(f"{tmp}/platform-{cid}.rendered.yaml")) if o]
    for o in objs:
        if o["kind"] in ("CiliumClusterwideNetworkPolicy", "CiliumNetworkPolicy"):
            for rule in o["spec"].get("egress") or []:
                assert "world" not in (rule.get("toEntities") or []), f"{cid}: {o['metadata']['name']} grants egress to the world entity (the metadata service is part of it)"
                for c in rule.get("toCIDRSet") or []:
                    if c["cidr"] in ("0.0.0.0/0", "::/0"):
                        assert "169.254.0.0/16" in (c.get("except") or []), f"{cid}: {o['metadata']['name']} allows 0.0.0.0/0 without excluding 169.254.0.0/16"
    routes = {o["metadata"]["name"]: o for o in objs if o["kind"] == "HTTPRoute" and o["metadata"]["namespace"] == "api"}
    if "api" in routes:
        assert all(p.get("sectionName") == "https" for p in routes["api"]["spec"]["parentRefs"]), f"{cid}: the api HTTPRoute attaches to a non-HTTPS listener"
        assert "api-redirect" in routes and any(f.get("type") == "RequestRedirect" and f["requestRedirect"].get("scheme") == "https"
                                                for r in routes["api-redirect"]["spec"]["rules"] for f in r.get("filters", [])), f"{cid}: no HTTPS redirect route for the API hostname"
        print(f"   ok security invariants {cid}: metadata service excluded, API HTTPS-only with redirect")
    # the gateways' access logs carry no query string (signed URLs, api_key parameters) and no credential header
    proxies = [o for o in objs if o["kind"] == "EnvoyProxy"]
    assert proxies, f"{cid}: no EnvoyProxy rendered"
    for o in proxies:
        settings = ((o["spec"].get("telemetry") or {}).get("accessLog") or {}).get("settings") or []
        assert settings, f"{cid}: EnvoyProxy {o['metadata']['name']} has no explicit access log format (the default logs the query string)"
        for st in settings:
            text = (st.get("format") or {}).get("text") or ""
            assert "REQ_WITHOUT_QUERY" in text, f"{cid}: EnvoyProxy {o['metadata']['name']} logs the path with its query"
            for bad in ("%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%", "%REQ(:PATH)%", "AUTHORIZATION", "COOKIE", "API_KEY", "SEC-WEBSOCKET-PROTOCOL"):
                assert bad.lower() not in text.lower(), f"{cid}: EnvoyProxy {o['metadata']['name']} access log carries {bad}"
    print(f"   ok access logs {cid}: query-free paths, no credential headers")
EOF4
  "$PYTHON" - "$TMP" "$ROOT" <<'EOF3'
import re, sys, yaml
tmp, root = sys.argv[1], sys.argv[2]
src = open(f"{root}/services/dispatcher/dispatcher.py").read()
verb_of = {"read": "get", "list": "list", "watch": "watch", "patch": "patch", "create": "create", "delete": "delete", "replace": "update"}
called = {verb_of[m] for m in re.findall(r"batch\.(\w+?)_(?:namespaced_|cluster_)?job\b", src)}
assert called, "no batch.<verb>_namespaced_job call found in dispatcher.py (the pattern is stale)"
found = 0
for cid in open(f"{tmp}/clusters.txt").read().split():
    objs = [o for o in yaml.safe_load_all(open(f"{tmp}/platform-{cid}.rendered.yaml")) if o]
    roles = [o for o in objs if o["kind"] == "ClusterRole" and o["metadata"]["name"] == "serverless2-dispatcher"]
    if not roles:
        continue
    found += 1
    rules = [r for r in roles[0]["rules"] if "batch" in r.get("apiGroups", []) and "jobs" in r.get("resources", [])]
    assert len(rules) == 1, f"{cid}: expected one batch/jobs rule in the dispatcher ClusterRole, got {len(rules)}"
    verbs = set(rules[0]["verbs"])
    assert called <= verbs, f"{cid}: dispatcher.py calls batch jobs {sorted(called)}, the ClusterRole grants {sorted(verbs)}"
    assert verbs <= {"get", "list", "watch"}, f"{cid}: the dispatcher ClusterRole grants batch jobs {sorted(verbs)}: it only ever reads manager Jobs"
    print(f"   ok dispatcher RBAC {cid}: batch/jobs {sorted(verbs)} covers the source's calls {sorted(called)}, read-only")
assert found, "no cluster rendered the dispatcher ClusterRole"
EOF3
  "$PYTHON" - "$TMP" <<'EOF5'
import sys, yaml
tmp = sys.argv[1]
# the API reads Knative Revisions in `models` on every cluster that runs endpoints (services/api/kube.py _rollout: the
# ready Revision's stored image is the convergence proof): the control API's own Role and the regional agent's Role
# both carry the read-only rule, and nothing wider on that group
seen = 0
for cid in open(f"{tmp}/clusters.txt").read().split():
    objs = [o for o in yaml.safe_load_all(open(f"{tmp}/platform-{cid}.rendered.yaml")) if o]
    roles = [o for o in objs if o["kind"] == "Role" and o["metadata"].get("namespace") == "models"
             and o["metadata"]["name"] in ("serverless2-api-endpoints", "serverless2-api-agent-models")]
    for role in roles:
        rules = [r for r in role["rules"] if "serving.knative.dev" in r.get("apiGroups", [])]
        assert rules, f"{cid}: Role {role['metadata']['name']} grants no serving.knative.dev read (kube._rollout needs revisions get/list)"
        for r in rules:
            assert set(r.get("resources", [])) <= {"revisions"}, f"{cid}: Role {role['metadata']['name']} reaches {r['resources']} on serving.knative.dev; revisions only"
            assert set(r["verbs"]) <= {"get", "list"}, f"{cid}: Role {role['metadata']['name']} grants {r['verbs']} on revisions: read-only"
        assert any("revisions" in r.get("resources", []) and {"get", "list"} <= set(r["verbs"]) for r in rules), f"{cid}: Role {role['metadata']['name']}: revisions get+list missing"
        # the opt-in ingress policy (services/api/render_endpoint.py _ingress_policy) is applied and deleted by the same Roles:
        # ciliumnetworkpolicies only on cilium.io, nothing wider (no clusterwide policies, no other Cilium objects)
        cil = [r for r in role["rules"] if "cilium.io" in r.get("apiGroups", [])]
        assert cil, f"{cid}: Role {role['metadata']['name']} grants nothing on cilium.io (the ingress policy needs ciliumnetworkpolicies)"
        for r in cil:
            assert set(r.get("resources", [])) <= {"ciliumnetworkpolicies"}, f"{cid}: Role {role['metadata']['name']} reaches {r['resources']} on cilium.io"
            assert set(r["verbs"]) <= {"get", "list", "create", "patch", "delete"}, f"{cid}: Role {role['metadata']['name']} grants {r['verbs']} on ciliumnetworkpolicies"
        # the trust bundle file of a lease-enabled endpoint (render_endpoint._trust_configmap) is a ConfigMap of the endpoint:
        # the same Roles apply and remove it; configmaps only, never secrets, in the core group
        core = [r for r in role["rules"] if "" in r.get("apiGroups", [])]
        assert any("configmaps" in r.get("resources", []) and {"get", "create", "patch", "delete"} <= set(r["verbs"]) for r in core), f"{cid}: Role {role['metadata']['name']}: configmaps get/create/patch/delete missing (the endpoint trust bundle)"
        for r in core:
            assert "secrets" not in r.get("resources", []), f"{cid}: Role {role['metadata']['name']} reaches secrets in models"
            if "configmaps" in r.get("resources", []):
                assert set(r["verbs"]) <= {"get", "list", "create", "patch", "delete"}, f"{cid}: Role {role['metadata']['name']} grants {r['verbs']} on configmaps"
        seen += 1
        print(f"   ok revision read {cid}: Role {role['metadata']['name']} reads serving.knative.dev revisions, read-only; ciliumnetworkpolicies and configmaps scoped")
assert seen, "no cluster rendered an API Role in the models namespace"
EOF5
}

check_tests() {
  step "tests: API and dispatcher"
  (cd "$ROOT" && "$PYTHON" -m pytest -q services/api/tests services/dispatcher)
  step "tests: filesystem attachment and GPU scale-to-zero storage"
  tf stack/modules/cluster init -backend=false -input=false > /dev/null
  tf stack/modules/cluster test -no-color
}

case "$part" in
  all) check_lint; check_terraform; check_render; check_tests ;;
  lint) check_lint ;;
  terraform) check_terraform ;;
  render) check_render ;;
  tests) check_tests ;;
  *) sed -n 2,10p "$0" >&2; exit 2 ;;
esac
echo "== all checks passed"
