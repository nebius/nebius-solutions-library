"""Shared data for runs (docs/JOBS.md "Shared data"): `job.data` mounts a directory of the tenant's area on the region's
shared filesystem (the same RWX claim `checkpoints: shared` uses) read-only at /data; the generic run classes expose it
as the `data` / `data_readonly` parameters; endpoints never land on a pool dedicated to a tenant."""

import pytest
from fastapi import HTTPException

import catalog
import jobs
import kube
import models
from config import SHARED_SCRATCH_PVC
from test_api import FLEET  # noqa: F401


def _run(params):
    m = catalog.get("container-run")
    return jobs.build_run(
        "op-data-1",
        m,
        {"image": "img", "command": "ls /data", **params},
        "demo",
        None,
        None,
        None,
        None,
        "eu-north1",
        {"output_prefix": "s3://b/out"},
    )


def test_container_run_mounts_the_tenant_data_read_only():
    job = _run({"data": "refdb/uniref"})
    pod = job["spec"]["template"]["spec"]
    vol = next(v for v in pod["volumes"] if v["name"] == "data")
    assert vol["persistentVolumeClaim"] == {"claimName": SHARED_SCRATCH_PVC, "readOnly": True}
    main = next(c for c in pod["containers"] if c["name"] == "main")
    mount = next(m for m in main["volumeMounts"] if m["name"] == "data")
    assert mount == {"name": "data", "mountPath": "/data", "subPath": "data/refdb/uniref", "readOnly": True}
    assert not any(m["name"] == "data" for c in pod["initContainers"] for m in c["volumeMounts"])  # fetch never sees it


def test_no_data_parameter_mounts_nothing_and_a_seeding_run_writes():
    pod = _run({})["spec"]["template"]["spec"]
    assert not any(v["name"] == "data" for v in pod["volumes"])
    pod = _run({"data": "refdb", "data_readonly": False})["spec"]["template"]["spec"]
    assert next(v for v in pod["volumes"] if v["name"] == "data")["persistentVolumeClaim"]["readOnly"] is False


def test_parent_references_are_refused():
    with pytest.raises(HTTPException) as e:
        _run({"data": "../other-tenant"})
    assert e.value.status_code == 400


def test_job_definitions_carry_data_and_endpoints_skip_dedicated_pools(monkeypatch):
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {
                "hub-h100-spot-1x": {
                    "region": "hub",
                    "pool": "h100-spot-1x",
                    "gpu_class": "h100",
                    "gpus_per_node": 1,
                    "capacity": "spot",
                },
                "hub-h100-lab-1x": {
                    "region": "hub",
                    "pool": "h100-lab-1x",
                    "gpu_class": "h100",
                    "gpus_per_node": 1,
                    "capacity": "reserved",
                    "tenant": "lab",
                },
            }
        },
    )
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    e = models.to_entry(
        {
            "id": "fold",
            "kind": "job",
            "image": "img",
            "command": "fold",
            "gpu": {"count": 1, "classes": ["h100"]},
            "data": {"path": "refdb/pdb", "mount_path": "/db"},
        }
    )
    assert e["job"]["data"] == "refdb/pdb" and e["job"]["dataMountPath"] == "/db" and e["job"]["dataReadOnly"] is True
    with pytest.raises(HTTPException):
        models.to_entry({"id": "bad", "kind": "job", "image": "img", "command": "x", "data": {"path": "../x"}})
    with pytest.raises(HTTPException):
        models.to_entry({"id": "bad", "kind": "endpoint", "image": "img", "data": {"path": "x"}})
    # the reserved pool is dedicated to tenant `lab`: an endpoint of the fleet never goes there
    assert models.pool_for("hub", ["h100"], 1) == "h100-spot-1x"


def test_weights_mount_read_only_and_images_through_the_cache():
    """`weights` (docs/JOBS.md "Model weights for runs") mounts the region's shared weights read-only; a caller's plain image
    reference is rewritten to the fleet's logical registry host like an endpoint's."""
    from config import WEIGHTS_SHARED_PVC

    job = _run({"weights": "llm-example", "image": "vllm/vllm-openai:v0.31.0"})
    pod = job["spec"]["template"]["spec"]
    vol = next(v for v in pod["volumes"] if v["name"] == "weights")
    assert vol["persistentVolumeClaim"] == {"claimName": WEIGHTS_SHARED_PVC, "readOnly": True}
    main = next(c for c in pod["containers"] if c["name"] == "main")
    assert {"name": "weights", "mountPath": "/weights", "subPath": "llm-example", "readOnly": True} in main[
        "volumeMounts"
    ]
    assert main["image"].startswith("registry.serverless2.local/docker/vllm/vllm-openai:")
    assert not any(v["name"] == "weights" for v in _run({})["spec"]["template"]["spec"]["volumes"])
    with pytest.raises(HTTPException):
        _run({"weights": "../x"})
    e = models.to_entry(
        {
            "id": "wjob",
            "kind": "job",
            "image": "img",
            "command": "x",
            "weights": {"path": "llm-example", "mount_path": "/w"},
        }
    )
    assert e["job"]["weights"] == "llm-example" and e["job"]["weightsMountPath"] == "/w"


def test_run_class_limits_and_ephemeral_storage_are_separate_from_requests(monkeypatch):
    """A public run class (`POST /v1/models`, kind job) carries separate limits and node-local scratch (docs/JOBS.md "Run
    class"): the request is what the scheduler guarantees (96Gi fits a 185Gi node), the limit what the container may
    grow to (256Gi); ephemeral storage request/limit for the kubelet's local scratch; nothing without them (the limit
    equals the request, no CPU quota)."""
    import kube
    from test_api import FLEET

    monkeypatch.setattr(kube, "fleet", lambda: FLEET)  # the fleet the classes are validated against
    spec = {
        "id": "fold",
        "kind": "job",
        "image": "img",
        "command": "fold",
        "gpu": {"count": 8, "classes": ["h100"]},
        "cpu": "32",
        "memory": "96Gi",
        "memory_limit": "256Gi",
        "cpu_limit": "64",
        "ephemeral_storage": "200Gi",
        "ephemeral_storage_limit": "400Gi",
    }
    job_block = models.to_entry(spec)["job"]
    assert (job_block["cpu"], job_block["memory"], job_block["cpuLimit"], job_block["memoryLimit"]) == (
        "32",
        "96Gi",
        "64",
        "256Gi",
    )
    assert (job_block["ephemeralStorage"], job_block["ephemeralStorageLimit"]) == ("200Gi", "400Gi")
    m = catalog.normalise(models.to_entry(spec))
    job = jobs.build_run("op-fold-1", m, {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://b/o"})
    res = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "main")["resources"]
    assert res["requests"] == {"cpu": "32", "memory": "96Gi", "ephemeral-storage": "200Gi", "nvidia.com/gpu": "8"}
    assert res["limits"] == {"cpu": "64", "memory": "256Gi", "ephemeral-storage": "400Gi", "nvidia.com/gpu": "8"}
    plain = models.to_entry(
        {**spec, "memory": "16Gi"}
        | {k: None for k in ("memory_limit", "cpu_limit", "ephemeral_storage", "ephemeral_storage_limit")}
    )
    assert not {"cpuLimit", "memoryLimit", "ephemeralStorage", "ephemeralStorageLimit"} & set(plain["job"])
    m = catalog.normalise(plain)
    res = next(
        c
        for c in jobs.build_run(
            "op-fold-2", m, {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://b/o"}
        )["spec"]["template"]["spec"]["containers"]
        if c["name"] == "main"
    )["resources"]
    assert res["limits"] == {"memory": "16Gi", "nvidia.com/gpu": "8"} and "ephemeral-storage" not in res["requests"]
    one = models.to_entry({**spec} | {"ephemeral_storage_limit": None})
    res = next(
        c
        for c in jobs.build_run(
            "op-fold-3",
            catalog.normalise(one),
            {},
            "demo",
            None,
            None,
            None,
            None,
            "eu-north1",
            {"output_prefix": "s3://b/o"},
        )["spec"]["template"]["spec"]["containers"]
        if c["name"] == "main"
    )["resources"]
    assert res["requests"]["ephemeral-storage"] == res["limits"]["ephemeral-storage"] == "200Gi"
