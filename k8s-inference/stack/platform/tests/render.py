#!/usr/bin/env python3
"""Hold a `helm template charts/platform` render equal to the recorded one (tools/check.sh).

    render.py <golden.json> < <helm template output>          exit 1 on any difference, naming the objects
    render.py --update <golden.json> < <helm template output>  record the render

The record is {id: {component, sha256}}: the id the platform stage keys its kubectl_manifest resources by
(<group or _>/<Kind>/<namespace or _>/<name>), the component (template directory, which decides the wave and hence the
state address) and the hash of the object's canonical JSON. A changed template therefore shows as the list of objects
it changed; the objects themselves are rendered on demand (`helm template`), never tracked.
"""

import hashlib
import json
import re
import sys

import yaml

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
    digest = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    rendered[key] = {"component": m.group(1), "sha256": digest, "object": obj}
if update:
    json.dump(
        {k: {"component": v["component"], "sha256": v["sha256"]} for k, v in rendered.items()},
        open(path, "w"),
        indent=1,
        sort_keys=True,
    )
    open(path, "a").write("\n")
    print(f"   wrote {path}: {len(rendered)} objects")
    sys.exit(0)
golden = json.load(open(path))
bad = 0
for key in sorted(set(golden) | set(rendered)):
    if key not in rendered:
        print(f"!! missing from the render: {key}")
        bad += 1
        continue
    if key not in golden:
        print(f"!! not in the record: {key}")
        bad += 1
        continue
    if golden[key]["component"] != rendered[key]["component"]:
        print(
            f"!! {key}: component {golden[key]['component']} -> {rendered[key]['component']} (the wave, hence the state address, would change)"
        )
        bad += 1
    if golden[key]["sha256"] != rendered[key]["sha256"]:
        bad += 1
        print(f"!! {key} changed; rendered now:")
        print(
            "\n".join(
                "   " + line for line in json.dumps(rendered[key]["object"], indent=1, sort_keys=True).splitlines()[:60]
            )
        )
if bad:
    sys.exit(f"{bad} object(s) differ from {path} (UPDATE_GOLDEN=1 tools/check.sh render records a deliberate change)")
print(f"   ok {len(rendered)} objects equal to {path}")
