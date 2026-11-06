"""Restricted assets (docs/JOBS.md "Restricted assets"): an operator-registered file or pinned tree, granted to a tenant,
mounted read-only on the run's main container only through the tenant's static claim, with the asset's group as a
supplemental group and no ownership change; aliases and grants come from the fleet document, never from the request;
the shared `weights`/`data` paths cannot reach a registered asset; mount paths overlap nothing."""

import copy

import pytest
from fastapi import HTTPException

import catalog
import jobs
import kube
from test_api import ADMIN, FLEET, H, admin_keys, client, cluster  # noqa: F401

AF3 = "/mnt/weights/scientific-ai/migration-20261013/private/alphafold3-parameters/74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff/af3.bin.zst"
TREE = "/mnt/weights/scientific-ai/migration-20261013/private/bindcraft-pyrosetta-installed-tree/a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
REGISTRY = {
    "af3-params": {
        "kind": "file",
        "host_path": AF3,
        "gid": 65532,
        "sha256": "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
        "size": 1020545840,
    },
    "bindcraft-tree": {
        "kind": "tree",
        "host_path": TREE,
        "gid": 65532,
        "sha256": "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d",
    },
}
FLEET_ASSETS = {
    **FLEET,
    "assets": REGISTRY,
    "tenants": {"demo": {"assets": ["af3-params", "bindcraft-tree"]}, "other": {"assets": []}},
}


def _fleet(monkeypatch):
    """After the client fixture (which pins the plain fleet): the registry and the grants of this fleet."""
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_ASSETS)


def _spec(**extra):
    return {
        "id": "af3-gpu",
        "kind": "job",
        "image": "example.org/af3:1",
        "command": "python fold.py",
        "gpu": {"classes": ["h100"], "count": 1},
        "assets": [{"alias": "af3-params", "mount_path": "/models/af3.bin.zst"}],
        **extra,
    }


def _build(model, tenant="demo", **kw):
    return jobs.build_run(
        "op-af3-1",
        model,
        {"output_prefix": "s3://b/p"},
        tenant,
        None,
        None,
        600,
        None,
        "eu-north1",
        {"output_prefix": "s3://b/p"},
        **kw,
    )


def test_the_asset_is_mounted_on_main_only_read_only_with_the_group(client, admin_keys, monkeypatch):
    c, _ = client
    _fleet(monkeypatch)
    assert c.post("/v1/models", json=_spec(), headers=ADMIN).status_code == 201
    m = catalog.get("af3-gpu")
    assert m["job"]["assets"] == [{"alias": "af3-params", "mountPath": "/models/af3.bin.zst"}]
    job = _build(m)
    spec = job["spec"]["template"]["spec"]
    (vol,) = [v for v in spec["volumes"] if v["name"] == "asset-af3-params"]
    assert vol == {
        "name": "asset-af3-params",
        "persistentVolumeClaim": {"claimName": "asset-af3-params", "readOnly": True},
    }
    main = next(x for x in spec["containers"] if x["name"] == "main")
    assert {"name": "asset-af3-params", "mountPath": "/models/af3.bin.zst", "readOnly": True} in main["volumeMounts"]
    others = [x for x in spec["containers"] + spec["initContainers"] if x["name"] != "main"]
    assert others and not any(mm["name"].startswith("asset-") for x in others for mm in x.get("volumeMounts") or [])
    assert spec["securityContext"]["supplementalGroups"] == [65532]
    assert spec["securityContext"]["fsGroup"] == 10001  # the work volume keeps its group; no fsGroup for the asset
    assert (
        job["metadata"]["annotations"]["serverless2.nebius/assets"]
        == "af3-params@sha256:74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
    )
    assert "_tenant" not in spec
    # a tree, the same way; identity is the inventory digest
    m2 = copy.deepcopy(m)
    m2["job"]["assets"] = [{"alias": "bindcraft-tree", "mountPath": "/opt/bindcraft"}]
    job = _build(m2)
    assert job["metadata"]["annotations"]["serverless2.nebius/assets"].startswith("bindcraft-tree@inventory:a93d68")


def test_registry_grant_and_paths_are_enforced(client, admin_keys, monkeypatch):
    c, _ = client
    _fleet(monkeypatch)
    assert c.post("/v1/models", json=_spec(), headers=ADMIN).status_code == 201
    m = catalog.get("af3-gpu")
    with pytest.raises(HTTPException, match="not granted"):
        _build(m, tenant="other")
    bad = copy.deepcopy(m)
    bad["job"]["assets"] = [{"alias": "nope", "mountPath": "/models/x"}]
    with pytest.raises(HTTPException, match="not a registered"):
        _build(bad)
    for path, why in [
        ("/work/af3", "reserved"),
        ("/etc/af3", "reserved"),
        ("models/x", "bounded absolute"),
        ("/dev/shm/x", "reserved"),
    ]:
        bad["job"]["assets"] = [{"alias": "af3-params", "mountPath": path}]
        with pytest.raises(HTTPException, match=why):
            _build(bad)
    bad["job"]["assets"] = [{"alias": "af3-params", "mountPath": "/weights/x"}]
    bad["job"]["weights"] = {"path": "public/esm"} if isinstance(m["job"].get("weights"), dict) else "public/esm"
    with pytest.raises(HTTPException, match="overlaps the mount"):
        _build(bad)
    bad["job"].pop("weights")
    bad["job"]["assets"] = [{"alias": "af3-params", "mountPath": "/mnt/fs2/af3"}]
    bad["job"]["workMountAliases"] = [{"path": "/mnt/fs2", "scope": "attempt"}]
    with pytest.raises(HTTPException, match="overlaps the mount"):
        _build(bad)
    bad["job"].pop("workMountAliases")
    bad["job"]["assets"] = [
        {"alias": "af3-params", "mountPath": "/models/a"},
        {"alias": "bindcraft-tree", "mountPath": "/models/a/b"},
    ]
    with pytest.raises(HTTPException, match="overlaps"):
        _build(bad)
    bad["job"]["assets"] = [{"alias": "af3-params", "mountPath": "/models/a"}]
    bad["job"]["nodes"] = 2
    with pytest.raises(HTTPException, match="single-node"):
        _build(bad)
    # the spec shape at registration
    for raw, why in [
        ([{"alias": "Bad Alias", "mount_path": "/x"}], "DNS label"),
        ([{"alias": "a", "mount_path": "x"}], "absolute"),
        ("a", "at most 8"),
    ]:
        r = c.post("/v1/models", json=_spec(id="af3-bad", assets=raw), headers=ADMIN)
        assert r.status_code == 400 and why in r.text, r.text


def test_shared_weights_and_data_paths_cannot_reach_a_registered_asset(client, admin_keys, monkeypatch):
    c, _ = client
    _fleet(monkeypatch)
    base = {k: v for k, v in _spec().items() if k != "assets"}
    assert c.post("/v1/models", json={**base, "id": "plain"}, headers=ADMIN).status_code == 201
    m = catalog.get("plain")
    priv = "scientific-ai/migration-20261013/private/alphafold3-parameters/74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
    for path in (
        priv,
        priv + "/af3.bin.zst",
        "scientific-ai/migration-20261013/private",
        "scientific-ai",
        "scientific-ai/migration-20261013/private/bindcraft-pyrosetta-installed-tree",
    ):
        w = copy.deepcopy(m)
        w["job"]["weights"] = path
        with pytest.raises(HTTPException, match="reaches the restricted asset"):
            _build(w)
    ok = copy.deepcopy(m)
    ok["job"]["weights"] = "scientific-ai/public/esm"  # a sibling public tree is fine
    spec = _build(ok)["spec"]["template"]["spec"]
    assert any(v["name"] == "weights" for v in spec["volumes"])
    assert "supplementalGroups" not in spec.get("securityContext", {})  # no asset: no group, nothing changes
    d = copy.deepcopy(m)
    d["job"]["data"] = "x"  # the tenant area: tenants/demo/data/x, nowhere near the registry
    _build(d)


def test_cpu_and_gpu_classes_declare_disjoint_assets(client, admin_keys, monkeypatch):
    """AF3: the CPU prep class names the public database through `weights`, never the parameters; the GPU class names
    the parameters, never the database. Each class is its own declaration; nothing leaks between stages."""
    c, _ = client
    cpu = {
        **_spec(id="af3-cpu", gpu={"classes": ["h100"], "count": 0}),
        "assets": None,
        "weights": {"path": "scientific-ai/public/af3-db"},
    }
    cpu.pop("assets")
    assert c.post("/v1/models", json=cpu, headers=ADMIN).status_code == 201, "cpu"
    assert c.post("/v1/models", json=_spec(), headers=ADMIN).status_code == 201
    _fleet(monkeypatch)  # the registrations above re-pin the plain fleet; the registry again
    cj = _build(catalog.get("af3-cpu"))["spec"]["template"]["spec"]
    gj = _build(catalog.get("af3-gpu"))["spec"]["template"]["spec"]
    assert not any(v["name"].startswith("asset-") for v in cj["volumes"]) and any(
        v["name"] == "weights" for v in cj["volumes"]
    )
    assert any(v["name"] == "asset-af3-params" for v in gj["volumes"]) and not any(
        v["name"] == "weights" for v in gj["volumes"]
    )


def test_export_retry_and_finalizer_never_carry_the_asset(client, admin_keys, monkeypatch):
    """Main-compute-only (root review of the 0.11.68 candidate): a transfer-only export retry and the application
    finalizer are the same class rendered again on the kept volume with a CPU container in main's place; they drop the
    asset volumes, the mounts, the supplemental groups and the identity annotation. The compute Job keeps them."""
    c, fake = client
    _fleet(monkeypatch)
    spec = _spec(
        finalize={"command": "python finish.py", "on_exit_codes": [75], "timeout_s": 900, "cpu": "1", "memory": "2Gi"}
    )
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    m = catalog.get("af3-gpu")
    _fleet(monkeypatch)
    r = c.post("/v1/models/af3-gpu:invoke", json={"input": {}, "region": "eu-north1"}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    compute = fake.jobs[("tenant-demo", op)]
    assert any(v["name"] == "asset-af3-params" for v in compute["spec"]["template"]["spec"]["volumes"])
    assert compute["metadata"]["annotations"]["serverless2.nebius/assets"].startswith("af3-params@sha256:")
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    defaults = {"output_prefix": "s3://b/p"}
    export, _ = jobs.export_again("tenant-demo", op, "eu-north1", m, defaults)
    done = {"pod": f"{op}-abc", "epoch": 1, "exit_code": 75}
    fz, _ = jobs.export_again(
        "tenant-demo", op, "eu-north1", m, defaults, finalize={"spec": m["job"]["finalize"], "attempt": done}
    )
    for j, role in ((export, "export"), (fz, "finalize")):
        s = j["spec"]["template"]["spec"]
        assert j["metadata"]["annotations"]["serverless2.nebius/attempt-role"] == role
        assert not any(v["name"].startswith("asset-") for v in s["volumes"]), role
        for ctr in s["containers"] + s.get("initContainers", []):
            assert not any(mm["name"].startswith("asset-") for mm in ctr.get("volumeMounts") or []), (role, ctr["name"])
        assert "supplementalGroups" not in (s.get("securityContext") or {}), role
        assert "serverless2.nebius/assets" not in j["metadata"]["annotations"], role
        assert s["securityContext"]["fsGroup"] == 10001  # the work volume's group stays
        assert any(v["name"] == "work" for v in s["volumes"])  # the kept volume is still there
    # the compute Job is untouched
    s = fake.jobs[("tenant-demo", op)]["spec"]["template"]["spec"]
    assert any(v["name"] == "asset-af3-params" for v in s["volumes"]) and s["securityContext"][
        "supplementalGroups"
    ] == [65532]


def test_the_asset_bound_is_eight(client, admin_keys, monkeypatch):
    """BindCraft declares five exact operator assets (AF2, vanilla MPNN, soluble MPNN, the PyRosetta tree and its pinned
    inventory file): five are accepted at registration and at render, nine are refused at both, and the registry, grant
    and mount guards are unchanged by the bound."""
    c, _ = client
    reg = {
        f"asset-{i}": {
            "kind": "file",
            "host_path": f"/mnt/weights/private/bc/{i}/f.bin",
            "gid": 65532,
            "sha256": "0" * 64,
            "size": 1,
        }
        for i in range(9)
    }
    fleet = {
        **FLEET,
        "assets": reg,
        "tenants": {"demo": {"assets": [f"asset-{i}" for i in range(8)]}},
    }  # asset-8 registered, not granted
    monkeypatch.setattr(kube, "fleet", lambda: fleet)
    five = [{"alias": f"asset-{i}", "mount_path": f"/models/{i}"} for i in range(5)]
    nine = [{"alias": f"asset-{i}", "mount_path": f"/models/{i}"} for i in range(9)]
    r = c.post("/v1/models", json=_spec(id="bc-nine", assets=nine), headers=ADMIN)
    assert r.status_code == 400 and "at most 8" in r.text, r.text
    assert c.post("/v1/models", json=_spec(id="bc-five", assets=five), headers=ADMIN).status_code == 201
    monkeypatch.setattr(kube, "fleet", lambda: fleet)
    m = catalog.get("bc-five")
    spec = _build(m)["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    assert sorted(v["name"] for v in spec["volumes"] if v["name"].startswith("asset-")) == [
        f"asset-asset-{i}" for i in range(5)
    ]
    assert len([mm for mm in main["volumeMounts"] if mm["name"].startswith("asset-")]) == 5 and spec["securityContext"][
        "supplementalGroups"
    ] == [65532]
    # the render bound and the guards, on the class directly
    bad = copy.deepcopy(m)
    bad["job"]["assets"] = [{"alias": f"asset-{i}", "mountPath": f"/models/{i}"} for i in range(9)]
    with pytest.raises(HTTPException, match="at most 8"):
        _build(bad)
    bad["job"]["assets"] = [{"alias": f"asset-{i}", "mountPath": f"/models/{i}"} for i in range(8)]
    _build(bad)  # eight: accepted
    bad["job"]["assets"] = [{"alias": "asset-8", "mountPath": "/models/8"}]
    with pytest.raises(HTTPException, match="not granted"):
        _build(bad)
    bad["job"]["assets"] = [{"alias": "asset-99", "mountPath": "/models/99"}]
    with pytest.raises(HTTPException, match="not a registered"):
        _build(bad)
    bad["job"]["assets"] = [
        {"alias": "asset-0", "mountPath": "/models/a"},
        {"alias": "asset-1", "mountPath": "/models/a/b"},
    ]
    with pytest.raises(HTTPException, match="overlaps"):
        _build(bad)
