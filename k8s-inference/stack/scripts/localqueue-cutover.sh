#!/usr/bin/env bash
# A LocalQueue's ClusterQueue is immutable (Kueue CRD validation), so moving a tenant's queues from the fleet's
# `<profile>` queues to its own `<tenant>-<profile>` ones (charts/fleet, 2026-10-11) means deleting the ones that point
# elsewhere before the tenant release recreates them. A Workload keeps its queue name and is admitted again when the
# queue reappears; admitted (running) work is not touched. Idempotent: nothing to do when every queue already points
# at the tenant's own ClusterQueue. Env: TENANT; KUBE_SERVER/KUBE_CA/NEBIUS_IAM_TOKEN (stack/scripts/kube.sh).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ns="tenant-${TENANT}"
list=$("$here/kube.sh" -n "$ns" get localqueues -o json 2>/dev/null || echo '{"items":[]}')
printf '%s' "$list" | python3 -c '
import json, sys
tenant = sys.argv[1]
for q in json.load(sys.stdin).get("items", []):
    name, target = q["metadata"]["name"], q["spec"].get("clusterQueue", "")
    if target != f"{tenant}-{name}":
        print(name)
' "$TENANT" | while read -r q; do
  [ -n "$q" ] || continue
  echo "localqueue-cutover: $ns/$q points at another ClusterQueue; deleting it for the tenant release to recreate"
  "$here/kube.sh" -n "$ns" delete localqueue "$q" --wait=true
done
