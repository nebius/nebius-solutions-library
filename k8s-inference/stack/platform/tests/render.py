#!/usr/bin/env python3
"""Compare a `helm template charts/platform` render with the golden set of a cluster (tools/check.sh).

    render.py <golden.json> < <helm template output>          exit 1 on any difference, with a diff per object
    render.py --update <golden.json> < <helm template output>  rewrite the golden set from the render

The golden set is {id: {component, object}} with the ids the platform stage keys its kubectl_manifest resources by
(<group or _>/<Kind>/<namespace or _>/<name>) and the component (template directory) that decides the wave. It was
first written from the kustomize bases plus the stage's JSON patches on 2026-10-11, so a chart render equal to it
changes nothing on a live cluster; after a deliberate manifest change, --update stores the new render.
"""
import difflib, json, re, sys, yaml

update = sys.argv[1] == "--update"
path = sys.argv[2] if update else sys.argv[1]
rendered = {}
for doc in re.split(r"^---$", sys.stdin.read(), flags=re.M):
    m = re.search(r"^# Source: platform/templates/([^/\n]+)/", doc, flags=re.M)
    obj = yaml.safe_load(doc)
    if not obj:
        continue
    api = obj["apiVersion"]
    key = f"{api.split('/')[0] if '/' in api else '_'}/{obj['kind']}/{obj['metadata'].get('namespace', '_')}/{obj['metadata']['name']}"
    if key in rendered:
        sys.exit(f"rendered twice: {key}")
    rendered[key] = {"component": m.group(1), "object": obj}
if update:
    json.dump(rendered, open(path, "w"), indent=1, sort_keys=True); open(path, "a").write("\n")
    print(f"   wrote {path}: {len(rendered)} objects")
    sys.exit(0)
golden = json.load(open(path))
bad = 0
for key in sorted(set(golden) | set(rendered)):
    if key not in rendered:
        print(f"!! missing from the render: {key}"); bad += 1; continue
    if key not in golden:
        print(f"!! not in the golden set: {key}"); bad += 1; continue
    if golden[key]["component"] != rendered[key]["component"]:
        print(f"!! {key}: component {golden[key]['component']} -> {rendered[key]['component']} (the wave, hence the state address, would change)"); bad += 1
    if golden[key]["object"] != rendered[key]["object"]:
        bad += 1
        a = json.dumps(golden[key]["object"], indent=1, sort_keys=True).splitlines()
        b = json.dumps(rendered[key]["object"], indent=1, sort_keys=True).splitlines()
        print(f"!! {key} differs:"); print("\n".join(list(difflib.unified_diff(a, b, "golden", "render", lineterm="", n=2))[2:40]))
if bad:
    sys.exit(f"{bad} object(s) differ from {path}")
print(f"   ok {len(rendered)} objects equal to {path}")
