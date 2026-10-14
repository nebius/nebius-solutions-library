"""Durable endpoint calls on the tenant's shared scratch (docs/JOBS.md "Scratch storage", `call_scratch: shared`): the
caller mounts only its API-derived leaf, a trusted init container provisions it, no per-call claim exists, the same
leaf serves the transfer-only export retry (no PVC lookup, no POST), the uploader is told to empty the leaf and never
to delete the claim, and the caller falls back to the per-call claim where the region or namespace cannot carry it."""

import pytest

import app as api
import attempts
import catalog
import jobs
import kube
from config import LABEL, SHARED_SCRATCH_PVC
from test_api import H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_async_private import _env, _main, _tenant, _uploader  # noqa: F401
from test_attempts import _record

NS = "tenant-demo"
FIT = {
    "hub": {
        "system": {"kind": "system", "cpu": 4, "memory_gib": 16, "ephemeral_gib": 100, "shared_fs": False},
        "cpu-batch-8": {"kind": "cpu", "cpu": 6, "memory_gib": 24, "ephemeral_gib": 100, "shared_fs": True},
        "h100-spot-1x": {
            "kind": "gpu",
            "gpus": 1,
            "cpu": 14,
            "memory_gib": 180,
            "ephemeral_gib": 400,
            "shared_fs": True,
        },
    }
}
GPU_ONLY_FIT = {"hub": {k: v for k, v in FIT["hub"].items() if k != "cpu-batch-8"}}
BOUND = {"metadata": {"name": SHARED_SCRATCH_PVC, "namespace": NS}, "status": {"phase": "Bound"}}


def place(pod: dict, cluster_fit: dict, saturated: set[str] = frozenset()) -> str | None:
    """What Kueue and the scheduler do with the rendered caller on a cluster, from the facts the fleet chart renders:
    Kueue walks the flavors in order (system, CPU pools, GPU pools), takes the first whose nodeLabels (`pool`,
    `shared-fs`) satisfy every required affinity expression and whose quota holds the requests (one node here), and
    injects that flavor's tolerations (the CPU pool's taint, the GPU pools' nvidia taint); the scheduler then binds on
    a node of that pool. Returns the pool label, or None (admitted nowhere)."""
    import fit as fitmod

    req = fitmod.pod_requests(pod)
    exprs: list = []
    nodesel = ((pod.get("affinity") or {}).get("nodeAffinity") or {}).get(
        "requiredDuringSchedulingIgnoredDuringExecution", {}
    )
    for t in nodesel.get("nodeSelectorTerms") or []:
        exprs = t.get("matchExpressions") or []  # one term in every render here
    order = sorted(cluster_fit.items(), key=lambda kv: {"system": 0, "cpu": 1, "gpu": 2}[kv[1]["kind"]])
    for pool, node in order:
        if pool in saturated:  # the flavor's quota is taken: Kueue moves on to the next flavor in order
            continue
        labels = {f"{LABEL}/pool": pool, f"{LABEL}/shared-fs": "true" if node.get("shared_fs") else "false"}
        if not all(labels.get(e["key"]) in e["values"] for e in exprs if e["operator"] == "In"):
            continue
        if not fitmod.holds(node, req):
            continue
        return pool
    return None


@pytest.fixture
def shared(client, admin_keys, fake_keys, monkeypatch):
    c, cl = client
    _tenant(monkeypatch)
    jobs._shared_scratch_cache.clear()
    fleet = dict(kube.fleet())
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": FIT})
    monkeypatch.setattr(kube, "cluster_id_or_none", lambda region: "hub" if region == "eu-north1" else None)
    m = dict(catalog.get("llm-example"))
    m["runtime"] = {**(m.get("runtime") or {}), "callScratch": "shared"}
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "llm-example" else None)
    cl.pvcs[(NS, SHARED_SCRATCH_PVC)] = dict(BOUND)
    data = {
        "request.json": "{}",
        "response_url": "https://objects.example/put/r",
        "call_url": "https://objects.example/put/c",
        "result_ref": "app://r",
    }
    assert c.put("/v1/bindings/call-1", json={"data": data}, headers=H).status_code == 200
    return c, cl


_n = [0]


def _call(c):
    """One private binding per call (a binding is bound to exactly one operation)."""
    _n[0] += 1
    ref = f"call-{_n[0]}"
    data = {
        "request.json": "{}",
        "response_url": "https://objects.example/put/r",
        "call_url": "https://objects.example/put/c",
        "result_ref": "app://r",
    }
    assert c.put(f"/v1/bindings/{ref}", json={"data": data}, headers=H).status_code == 200
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": ref}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_caller_mounts_only_its_leaf_with_a_provisioning_init_and_no_claim_of_its_own(shared):
    c, cl = shared
    oid = _call(c)
    job = cl.jobs[(NS, oid)]
    spec = job["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["work"]["persistentVolumeClaim"]["claimName"] == SHARED_SCRATCH_PVC
    assert (NS, f"{oid}-work") not in cl.pvcs  # no per-call claim, nothing to attach
    leaf = f"calls/{oid}"
    for cname in ("main", "uploader"):
        cont = next(x for x in spec["containers"] if x["name"] == cname)
        (m,) = [m for m in cont["volumeMounts"] if m["name"] == "work"]
        assert m == {"name": "work", "mountPath": "/work", "subPath": leaf}  # the leaf only, never the tenant root
    init = spec["initContainers"][0]
    assert init["name"] == "scratch-provision" and init["command"] == [
        "/usr/local/bin/scratch.sh",
        "provision",
        "/work",
    ]
    assert init["volumeMounts"] == [{"name": "work", "mountPath": "/work", "subPath": leaf}]
    sc = init["securityContext"]
    assert sc["runAsUser"] == 0 and sc["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"]}
    assert sc["allowPrivilegeEscalation"] is False and sc["seccompProfile"] == {"type": "RuntimeDefault"}
    up = _env(_uploader(job))
    assert "PVC_NAME" not in up and up["SCRATCH"]["value"] == "shared" and up["SCRATCH_LEAF"]["value"] == leaf
    ann = job["metadata"]["annotations"]
    assert ann[f"{LABEL}/scratch"] == "shared" and ann[f"{LABEL}/scratch-leaf"] == leaf and f"{LABEL}/pvc" not in ann
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]
    assert {"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]} in terms["matchExpressions"]
    assert {"key": f"{LABEL}/pool", "operator": "In", "values": ["cpu-batch-8"]} in terms[
        "matchExpressions"
    ]  # the eligible pools
    # the main container is the caller as before (one POST rule lives in call.sh on the same /work)
    assert _main(job)["command"] == ["/usr/local/bin/call.sh"] and _env(_main(job))["IO_MODE"]["value"] == "application"
    # actual placement, not the rendered affinity: Kueue's flavor order lands this caller on the GPU-free shared-fs pool
    assert place(spec, FIT["hub"]) == "cpu-batch-8"
    # CPU quota saturated while the shared-fs GPU flavor has room: the constrained render is admitted nowhere (QUEUED
    # for its CPU pool), never on the GPU flavor; the shared-fs label alone would have taken the GPU flavor
    assert place(spec, FIT["hub"], saturated={"cpu-batch-8"}) is None
    label_only = {
        **spec,
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]}]}
                    ]
                }
            }
        },
    }
    assert place(label_only, FIT["hub"], saturated={"cpu-batch-8"}) == "h100-spot-1x"


def test_export_retry_reuses_the_same_leaf_without_a_pvc_and_without_a_post(shared, monkeypatch):
    c, cl = shared
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    oid = _call(c)
    job = cl.jobs[(NS, oid)]
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-14T09:00:00Z"}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [{**_record(oid, f"{oid}-a", export="failed"), "operation": oid, "io_mode": "application"}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202, r.text
    new = cl.jobs[(NS, f"{oid}-x1")]
    spec = new["spec"]["template"]["spec"]
    env = _env(_main(new))
    assert env["TRANSFER_ONLY"]["value"] == "1" and "REQUEST_BODY" not in env
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["work"]["persistentVolumeClaim"]["claimName"] == SHARED_SCRATCH_PVC
    assert [m for m in _main(new)["volumeMounts"] if m["name"] == "work"][0][
        "subPath"
    ] == f"calls/{oid}"  # the ORIGINAL's leaf
    assert new["metadata"]["annotations"][f"{LABEL}/scratch-leaf"] == f"calls/{oid}"
    assert (NS, f"{oid}-work") not in cl.pvcs and (NS, f"{oid}-x1-work") not in cl.pvcs
    rterms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]
    assert {"key": f"{LABEL}/pool", "operator": "In", "values": ["cpu-batch-8"]} in rterms["matchExpressions"]
    assert place(spec, FIT["hub"], saturated={"cpu-batch-8"}) is None  # the retry waits for its CPU pool too
    # the CPU pool gone from the table: the retry cannot be placed on the leaf now; refused with the reason, never a
    # GPU flavor and never a per-call claim (the capture is on the leaf)
    oid3 = _call(c)
    job3 = cl.jobs[(NS, oid3)]
    job3["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-14T09:00:00Z"}
    rows.append({**_record(oid3, f"{oid3}-a", export="failed"), "operation": oid3, "io_mode": "application"})
    monkeypatch.setattr(attempts, "for_operation", lambda root: [r for r in rows if r["operation"] == root])
    fleet = dict(kube.fleet())
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": GPU_ONLY_FIT})
    jobs._shared_scratch_cache.clear()
    r = c.post(f"/v1/operations/{oid3}:export", headers=H)
    assert r.status_code == 409 and "cannot be placed now" in r.text and "only GPU pools" in r.text, r.text
    assert (NS, f"{oid3}-x1") not in cl.jobs
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": FIT})
    # the shared claim gone from the namespace: a fresh operation's export has nothing to mount, said plainly
    oid2 = _call(c)
    job2 = cl.jobs[(NS, oid2)]
    job2["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-14T09:00:00Z"}
    rows.append({**_record(oid2, f"{oid2}-a", export="failed"), "operation": oid2, "io_mode": "application"})
    monkeypatch.setattr(attempts, "for_operation", lambda root: [r for r in rows if r["operation"] == root])
    del cl.pvcs[(NS, SHARED_SCRATCH_PVC)]
    jobs._shared_scratch_cache.clear()
    r = c.post(f"/v1/operations/{oid2}:export", headers=H)
    assert r.status_code == 409 and "shared scratch claim is gone" in r.text, r.text


def test_fallback_to_the_per_call_claim_where_the_leaf_cannot_be_carried(shared, monkeypatch):
    c, cl = shared
    fleet = dict(kube.fleet())

    def fallback_of(oid):
        job = cl.jobs[(NS, oid)]
        vols = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
        assert vols["work"]["persistentVolumeClaim"]["claimName"] == f"{oid}-work" and (NS, f"{oid}-work") in cl.pvcs
        ann = job["metadata"]["annotations"]
        assert ann[f"{LABEL}/pvc"] == f"{oid}-work" and ann[f"{LABEL}/scratch"] == "network"
        assert not job["spec"]["template"]["spec"].get("initContainers")
        assert "PVC_NAME" in _env(_uploader(job)) and "SCRATCH" not in _env(_uploader(job))
        return ann[f"{LABEL}/scratch-fallback"], job["spec"]["template"]["spec"]

    # 1. only GPU pools mount the shared filesystem: the caller would be admitted on a GPU flavor (Kueue) and occupy a
    #    GPU node for 100m: fallback; the placement model shows where the shared render would have gone
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": GPU_ONLY_FIT})
    jobs._shared_scratch_cache.clear()
    why, spec = fallback_of(_call(c))
    assert why == "only GPU pools of hub mount the shared filesystem (a caller never occupies a GPU node)"
    probe = {
        **spec,
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]}]}
                    ]
                }
            }
        },
    }
    assert place(probe, GPU_ONLY_FIT["hub"]) == "h100-spot-1x"  # the shared render would have landed on a GPU node
    assert place(spec, GPU_ONLY_FIT["hub"]) == "system"  # the fallback render lands on the system pool as before
    # 2. the GPU-free shared-fs pool is too small for the caller: fallback, naming the requests
    tiny = {"hub": {**FIT["hub"], "cpu-batch-8": {**FIT["hub"]["cpu-batch-8"], "cpu": 0.1, "memory_gib": 0.1}}}
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": tiny})
    jobs._shared_scratch_cache.clear()
    why, _ = fallback_of(_call(c))
    assert why.startswith("no GPU-free pool of hub that mounts the shared filesystem holds the caller (cpu")
    # 3. no pool at all mounts it
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": {"hub": {"system": {**FIT["hub"]["system"]}}}})
    jobs._shared_scratch_cache.clear()
    why, _ = fallback_of(_call(c))
    assert why == "no GPU-free pool of hub mounts the shared filesystem"
    # 4. the claim exists but is not Bound (a static volume not yet bound, a wrong storage class)
    monkeypatch.setattr(kube, "fleet", lambda: {**fleet, "fit": FIT})
    cl.pvcs[(NS, SHARED_SCRATCH_PVC)] = {"metadata": {"name": SHARED_SCRATCH_PVC}, "status": {"phase": "Pending"}}
    jobs._shared_scratch_cache.clear()
    why, _ = fallback_of(_call(c))
    assert why == f"claim {SHARED_SCRATCH_PVC} in {NS} on eu-north1 is not Bound (Pending)"
    # 5. the claim is missing
    del cl.pvcs[(NS, SHARED_SCRATCH_PVC)]
    jobs._shared_scratch_cache.clear()
    why, _ = fallback_of(_call(c))
    assert "claim scratch-shared not found" in why
    # 6. the cluster is unknown to the fit table
    monkeypatch.setattr(kube, "cluster_id_or_none", lambda region: None)
    cl.pvcs[(NS, SHARED_SCRATCH_PVC)] = dict(BOUND)
    jobs._shared_scratch_cache.clear()
    why, _ = fallback_of(_call(c))
    assert "no per-node fit table" in why
    # a managed-I/O call (no execution block) never had a retained volume: unchanged (emptyDir)
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"x": 1}}, headers=H)
    assert r.status_code == 202, r.text
    plain = cl.jobs[(NS, r.json()["id"])]
    assert "emptyDir" in {v["name"]: v for v in plain["spec"]["template"]["spec"]["volumes"]}["work"]


def test_shared_fs_requirement_joins_an_existing_required_affinity_by_conjunction(shared, monkeypatch):
    import execution

    c, cl = shared
    real = execution.apply_async

    def with_profile_affinity(pod, ex, name):
        real(pod, ex, name)
        pod["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "example.org/zone", "operator": "In", "values": ["a"]}]}
                    ]
                }
            }
        }

    monkeypatch.setattr(execution, "apply_async", with_profile_affinity)
    oid = _call(c)
    spec = cl.jobs[(NS, oid)]["spec"]["template"]["spec"]
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert len(terms) == 1 and terms[0]["matchExpressions"] == [
        {"key": "example.org/zone", "operator": "In", "values": ["a"]},
        {"key": f"{LABEL}/shared-fs", "operator": "In", "values": ["true"]},
        {"key": f"{LABEL}/pool", "operator": "In", "values": ["cpu-batch-8"]},
    ]  # all required, in one term (AND): nothing of the profile's requirement dropped or widened
    assert jobs.shared_scratch_available(
        NS, "eu-north1", {"containers": [{"name": "m", "resources": {"requests": {"nvidia.com/gpu": "1"}}}]}
    ) == (False, "the caller requests a GPU (never for a shared-scratch leaf)", [])


def test_leaf_names_are_api_derived_and_the_default_model_is_unchanged(shared):
    c, cl = shared
    assert jobs.scratch_leaf("op-abc123") == "calls/op-abc123"
    for bad in ("../x", "op/../../y", "", "A B", "x" * 70):
        with pytest.raises(Exception):
            jobs.scratch_leaf(bad)
    probe = {"containers": [{"name": "m", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}]}
    ok, why, pools = jobs.shared_scratch_available(NS, "eu-west1", probe)  # a cluster the fit table does not know
    assert ok is False and "no per-node fit table" in why and pools == []
