#!/usr/bin/env python3
"""Preflight for `./stack.sh preflight`: the plan JSON on stdin (stack/config `local.plan`), the tfvars path as
argv[1]. Checks, with the Nebius CLI (its active profile), that every GPU pool's platform, preset, driver preset
and Kubernetes version are accepted by Managed Kubernetes (get-compatibility-matrix) and that the preset exists
on the platform in the pool's project, and that the state bucket is reachable. Exit 1 on the first set of
findings; a wrong preset otherwise costs a ten-minute failed apply."""
import json, subprocess, sys

plan = json.load(sys.stdin)
tfvars = sys.argv[1]


def nebius(*args):
    r = subprocess.run(["nebius", *args, "--format", "json"], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr.strip()
    return json.loads(r.stdout), None


# the pools, read back through terraform console so the HCL is parsed once, by Terraform
out = subprocess.run(["terraform", "-chdir=stack/config", "console", f"-var-file={tfvars}"],
                     input='jsonencode({ for rn, r in var.fleet.regions : rn => { project = r.project_id, pools = r.pools } })',
                     capture_output=True, text=True)
if out.returncode:
    print(out.stderr); sys.exit(1)
regions = json.loads(json.loads(out.stdout))
k8s = subprocess.run(["terraform", "-chdir=stack/config", "console", f"-var-file={tfvars}"], input="var.fleet.kubernetes_version",
                     capture_output=True, text=True).stdout.strip().strip('"')

findings, checked = [], 0
matrix_cache = {}
for rn, r in regions.items():
    for pn, p in r["pools"].items():
        checked += 1
        plat, preset, drv = p["platform"], p["preset"], p.get("driver_preset") or "cuda13.0"
        d, err = nebius("compute", "platform", "get-by-name", "--name", plat, "--parent-id", r["project"])
        if err:
            findings.append(f"{rn}/{pn}: platform {plat} not found in {r['project']}: {err[:120]}"); continue
        presets = [x["name"] for x in d.get("spec", {}).get("presets", [])]
        if preset not in presets:
            findings.append(f"{rn}/{pn}: preset {preset} not on {plat} in {r['project']}; available: {', '.join(presets)}")
        if plat not in matrix_cache:
            m, err = nebius("mk8s", "node-group", "get-compatibility-matrix", "--cluster-kubernetes-version", k8s, "--platform", plat)
            matrix_cache[plat] = (m, err)
        m, err = matrix_cache[plat]
        if err:
            findings.append(f"{rn}/{pn}: compatibility matrix for {plat}/{k8s}: {err[:120]}"); continue
        ok = any(drv == it.get("drivers_preset") and plat in it.get("compatible_platforms", [])
                 for v in m.get("versions", []) if v.get("kubernetes_version") == k8s for it in v.get("items", []))
        if not ok:
            findings.append(f"{rn}/{pn}: driver preset {drv} is not compatible with {plat} on Kubernetes {k8s}")
        # docs.nebius.com/compute/storage/local-disks (2026-10-08): local SSD only on gpu-b300-sxm / 8gpu-192vcpu-2768gb
        # in uk-south1, eu-west2, us-north1; every other preset rejects local_disks at node-group creation.
        if p.get("interconnect") == "infiniband":
            clusterable = [x["name"] for x in d.get("spec", {}).get("presets", []) if x.get("allow_gpu_clustering")]
            if preset not in clusterable:
                findings.append(f"{rn}/{pn}: interconnect = infiniband but {plat}/{preset} does not allow GPU clustering; presets that do: {', '.join(clusterable) or 'none'}")
        if p.get("local_nvme") and not (plat == "gpu-b300-sxm" and preset == "8gpu-192vcpu-2768gb"):
            findings.append(f"{rn}/{pn}: local_nvme on {plat}/{preset}: only gpu-b300-sxm/8gpu-192vcpu-2768gb ships local NVMe (docs/FLEET.md)")
        # spot pools are preemptible VMs: the platform must be allowed for preemptibles in that project, and the
        # quota that applies is the preemptible one (counted in VMs), not the GPU quota (docs/FLEET.md "Spot capacity")
        if (p.get("capacity") or {}).get("type") == "spot" and d.get("status", {}).get("allowed_for_preemptibles") is not True:
            findings.append(f"{rn}/{pn}: capacity spot, but {plat} is not allowed for preemptibles in {r['project']} (status.allowed_for_preemptibles)")

# the quotas that matter, per project: preemptible VMs for spot pools, GPUs per platform for the others. The
# allowance API exposes usage and a usage state, not the numeric limit, so this is a report, not a check: the
# node-group creation of `apply cloud` is the hard test and fails within minutes when the quota is exhausted.
for project in sorted({r["project"] for r in regions.values()}):
    q, err = nebius("quotas", "quota-allowance", "list", "--parent-id", project, "--page-size", "1000")
    if err:
        print(f"   quotas of {project}: not readable ({err[:80]})"); continue
    wanted = {"compute.instance.preemptible.count"} | {f"compute.instance.gpu.{p['platform'].split('-')[1]}" for r in regions.values() if r["project"] == project for p in r["pools"].values()}
    for item in q.get("items", []):
        name = item["metadata"]["name"]
        if name in wanted:
            st = item.get("status", {})
            print(f"   quota {project} {name}: usage {st.get('usage', '?')} ({st.get('usage_state', '?')})")


# catalog entries whose GPU classes are all absent from the fleet would name a Kueue queue that does not exist
nc = subprocess.run(["terraform", "-chdir=stack/config", "console", f"-var-file={tfvars}"], input="jsonencode(local.catalog_without_fleet_class)",
                    capture_output=True, text=True)
for mid in (json.loads(json.loads(nc.stdout)) if nc.returncode == 0 and nc.stdout.strip() else []):
    findings.append(f"catalog {mid}: none of its gpu.classes is a gpu_class of a pool; add a pool of that class (the built-in classes of catalog/models)")

b, err = nebius("storage", "bucket", "list", "--parent-id", plan["clusters"][plan["hub_id"]]["project_id"], "--page-size", "500")
names = [i["metadata"]["name"] for i in (b or {}).get("items", [])]
if plan["state"]["bucket"] not in names:
    findings.append(f"state bucket {plan['state']['bucket']} not in the hub project: run stack/bootstrap/state-bucket.sh first")

print(f"preflight: {checked} GPU pools in {len(regions)} region(s), kubernetes {k8s}, state bucket {plan['state']['bucket']}")
for f in findings:
    print("!!", f)
sys.exit(1 if findings else 0)
