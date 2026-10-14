"""Per-node fit (docs/SCHEDULING.md "Per-node fit"): the exact AlphaFold 3 MCP case of 2026-10-14 (a 16.1 CPU / 64.25 Gi /
32 Gi ephemeral GPU-free pod admitted by Kueue on an H100 flavor whose aggregate CPU covered it while no single node
does), the impossible shape refused at submission, an explicit pool never enlarged, the class/shared-fs/local-NVMe
terms kept conjunctive, GPU runs narrowed to the class pools whose node holds them, and nothing without fit.yaml."""

import pytest
from fastapi import HTTPException

import catalog
import fit
import jobs
import kube
from config import LABEL
from test_api import FLEET, H, admin_keys, client, cluster  # noqa: F401

# one node of each pool, as charts/fleet renders fit.yaml for the north cluster (system 8vcpu-32gb minus the system
# reserve 4/16, cpu-batch-32 32vcpu-128gb boot 64 GiB, h100-reserved-1x 1gpu-16vcpu-200gb minus the node reserve 2/20)
FIT = {
    "north": {
        "system": {"kind": "system", "gpus": 0, "cpu": 4, "memory_gib": 16, "ephemeral_gib": 104, "shared_fs": False},
        "cpu-batch-32": {
            "kind": "cpu",
            "gpus": 0,
            "cpu": 28,
            "memory_gib": 112,
            "ephemeral_gib": 50,
            "shared_fs": True,
        },
        "h100-reserved-1x": {
            "kind": "gpu",
            "gpus": 1,
            "cpu": 14,
            "memory_gib": 180,
            "ephemeral_gib": 431,
            "shared_fs": True,
        },
    },
    "south": {
        "system": {"kind": "system", "gpus": 0, "cpu": 4, "memory_gib": 16, "ephemeral_gib": 104, "shared_fs": False},
        "h100-spot-8x": {
            "kind": "gpu",
            "gpus": 8,
            "cpu": 126,
            "memory_gib": 1580,
            "ephemeral_gib": 431,
            "shared_fs": True,
        },
    },
}
POOLS = {
    "north-h100-reserved-1x": {
        "region": "north",
        "pool": "h100-reserved-1x",
        "gpu_class": "h100",
        "usd_per_gpu_hour": 2.0,
    },
    "south-h100-spot-8x": {"region": "south", "pool": "h100-spot-8x", "gpu_class": "h100", "usd_per_gpu_hour": 1.0},
}
DOC = {"pools": POOLS, "clusters": {"north": {"region": "eu-north1"}, "south": {"region": "eu-south1"}}, "fit": FIT}

AF3_POD = {
    "initContainers": [{"name": "fetch", "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}}}],
    "containers": [
        {"name": "main", "resources": {"requests": {"cpu": "16", "memory": "64Gi", "ephemeral-storage": "32Gi"}}},
        {"name": "uploader", "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}},
    ],
}


def test_quantities_and_effective_pod_requests():
    assert fit.quantity("100m") == 0.1 and fit.quantity("16") == 16 and fit.quantity("64Gi") == 64 * 2**30
    assert fit.quantity("1G") == 1e9 and fit.quantity("512Mi") == 512 * 2**20 and fit.quantity(None) == 0
    with pytest.raises(ValueError):
        fit.quantity("sixteen")
    req = fit.pod_requests(AF3_POD)
    assert req["cpu"] == pytest.approx(16.1) and req["memory"] == pytest.approx(64.25 * 2**30)
    assert req["ephemeral"] == 32 * 2**30 and req["gpu"] == 0
    # an init container's own request bounds the effective request (never summed with the app containers)
    big_init = {**AF3_POD, "initContainers": [{"name": "fetch", "resources": {"requests": {"cpu": "20"}}}]}
    assert fit.pod_requests(big_init)["cpu"] == 20


def test_the_af3_shape_fits_only_the_cpu_pool(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    req = fit.pod_requests(AF3_POD)
    assert fit.fitting(req) == {"cpu-batch-32": ["north"], "h100-spot-8x": ["south"]}  # 16.1 > 14 on the 1x nodes
    assert fit.fitting(req, clusters=["north"]) == {"cpu-batch-32": ["north"]}
    assert fit.fitting(req, clusters=["north"], pools={"h100-reserved-1x"}) == {}
    assert fit.largest(["north"]).startswith(
        "cpu 28, memory 112Gi, ephemeral-storage 50Gi (pool cpu-batch-32 on north)"
    )
    # the pool Kueue assigned on 0.11.78 (the pod's injected nodeSelector) cannot hold it; the CPU pool can
    pod = {"spec": {**AF3_POD, "nodeSelector": {f"{LABEL}/pool": "h100-reserved-1x"}}}
    assert fit.assigned_pool_holds(pod, "north") is False
    pod["spec"]["nodeSelector"][f"{LABEL}/pool"] = "cpu-batch-32"
    assert fit.assigned_pool_holds(pod, "north") is True
    pod["spec"]["nodeSelector"][f"{LABEL}/pool"] = "unknown-pool"
    assert fit.assigned_pool_holds(pod, "north") is None
    assert fit.assigned_pool_holds({"spec": AF3_POD}, "north") is None  # not admitted yet: no judgement
    assert fit.assigned_pool_holds(pod, None) is None  # cluster unknown: no judgement


def test_the_assigned_pool_is_judged_on_the_admitting_cluster_only(monkeypatch):
    """The same pool label can be a bigger preset elsewhere: only the observed worker's node counts."""
    hetero = {
        "north": {
            "h100-reserved-1x": {
                "kind": "gpu",
                "gpus": 1,
                "cpu": 14,
                "memory_gib": 180,
                "ephemeral_gib": 431,
                "shared_fs": True,
            }
        },
        "south": {
            "h100-reserved-1x": {
                "kind": "gpu",
                "gpus": 1,
                "cpu": 30,
                "memory_gib": 180,
                "ephemeral_gib": 431,
                "shared_fs": True,
            }
        },
    }
    monkeypatch.setattr(kube, "fleet", lambda: {**DOC, "fit": hetero})
    pod = {"spec": {**AF3_POD, "nodeSelector": {f"{LABEL}/pool": "h100-reserved-1x"}}}
    assert fit.assigned_pool_holds(pod, "north") is False  # the actual worker cannot hold it
    assert fit.assigned_pool_holds(pod, "south") is True  # south's nodes of the same label could
    assert fit.assigned_pool_holds(pod, "west") is None  # no such cluster in the table


def test_capabilities_are_intersected_per_pool_before_a_pool_counts(monkeypatch):
    """A node big enough but without the shared filesystem (or local NVMe) the run needs is no candidate: the
    affinity never names a set of pools no single node realizes, and the refusal names the capability."""
    doc = {
        **DOC,
        "pools": {
            **POOLS,
            "north-nvme-1x": {
                "region": "north",
                "pool": "nvme-1x",
                "gpu_class": "h100",
                "usd_per_gpu_hour": 2.0,
                "local_nvme": True,
            },
        },
        "fit": {
            "north": {
                "big-no-fs": {
                    "kind": "cpu",
                    "gpus": 0,
                    "cpu": 60,
                    "memory_gib": 200,
                    "ephemeral_gib": 100,
                    "shared_fs": False,
                },
                "small-fs": {
                    "kind": "cpu",
                    "gpus": 0,
                    "cpu": 8,
                    "memory_gib": 32,
                    "ephemeral_gib": 100,
                    "shared_fs": True,
                },
                "nvme-1x": {
                    "kind": "gpu",
                    "gpus": 1,
                    "cpu": 8,
                    "memory_gib": 180,
                    "ephemeral_gib": 431,
                    "shared_fs": True,
                },
            }
        },
    }
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: cid)
    req = fit.pod_requests(AF3_POD)
    assert fit.fitting(req) == {"big-no-fs": ["north"]}
    assert fit.fitting(req, shared_fs=True) == {}  # the only big node has no shared filesystem
    assert fit.fitting(req, local_nvme=True) == {}  # the NVMe pool's node is too small
    big = fit.largest(shared_fs=True)  # the largest AMONG the capable ones: never the 60-CPU node without the mount
    assert big.startswith("cpu 8, memory 180Gi") and "nvme-1x" in big and "60" not in big
    # through the render: a run that mounts weights needs shared-fs; the big pool without it is no candidate
    fs_run = _model({**CPU_CLASS, "id": "fs", "weights": {"path": "x"}})
    with pytest.raises(HTTPException) as e:
        _build("op-fs", fs_run, {"regions": ["north"]})
    assert e.value.status_code == 400 and "with the shared filesystem" in e.value.detail and "cpu 8" in e.value.detail
    # local-nvme scratch: the NVMe pool is the only candidate and its node is too small
    nvme_run = _model({**CPU_CLASS, "id": "nv", "gpu": {"count": 1, "classes": ["h100"]}, "scratch": "local-nvme"})
    with pytest.raises(HTTPException) as e:
        _build("op-nv", nvme_run, {"regions": ["north"], "classes": ["h100"]})
    assert e.value.status_code == 400 and "with local NVMe" in e.value.detail
    # a run that fits the small shared-fs pool (and the NVMe pool's node, which mounts it too) is constrained to those
    # two: never the big pool it cannot mount
    small = _model(
        {**CPU_CLASS, "id": "sm", "cpu": "4", "memory": "8Gi", "ephemeral_storage": "10Gi", "weights": {"path": "x"}}
    )
    job = _build("op-sm", small, {"regions": ["north"]})
    terms = _affinity(job)
    assert terms[f"{LABEL}/pool"]["values"] == ["nvme-1x", "small-fs"]
    assert terms[f"{LABEL}/shared-fs"]["values"] == ["true"] and "big-no-fs" not in terms[f"{LABEL}/pool"]["values"]


def test_fit_is_computed_on_the_final_rendered_pod(monkeypatch):
    """The execution profile reshapes the pod (application io mode drops the fetch init container): the fit must see
    the pod as rendered, not the shell before the profile."""
    import execution

    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: cid)
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    m = _model(CPU_CLASS)
    # a table so tight that only the final pod's own requests (main + uploader) fit: the fetch init container's
    # request (500m) is bounded away by the effective-request rule, and an application profile removes it entirely
    tight = {
        "north": {
            "cpu-batch-32": {
                "kind": "cpu",
                "gpus": 0,
                "cpu": 16.2,
                "memory_gib": 65,
                "ephemeral_gib": 50,
                "shared_fs": True,
            }
        }
    }
    monkeypatch.setattr(kube, "fleet", lambda: {**DOC, "fit": tight})
    # main 16 + uploader 0.1 = 16.1 fits 16.2; with the fetch init container (0.5 cpu) the bound is still 16.1
    job = _build("op-app", m, {"regions": ["north"]})
    assert _affinity(job)[f"{LABEL}/pool"]["values"] == ["cpu-batch-32"]
    ex = execution.resolve(execution.ExecutionSpec(io_mode="application"), "tenant-demo", m)
    job = jobs.build_run(
        "op-app2",
        m,
        {},
        "demo",
        None,
        None,
        None,
        None,
        None,
        {"output_prefix": ""},
        "k",
        placement={"regions": ["north"]},
        ex=ex,
    )
    spec = job["spec"]["template"]["spec"]
    assert not [c for c in spec.get("initContainers", []) if c["name"] == "fetch"]  # the profile removed it
    assert _affinity(job)[f"{LABEL}/pool"]["values"] == ["cpu-batch-32"]  # fit computed on the final pod
    assert fit.pod_requests(spec)["cpu"] == pytest.approx(16.1)


def _affinity(job):
    aff = job["spec"]["template"]["spec"].get("affinity") or {}
    terms = (aff.get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution", {}).get(
        "nodeSelectorTerms"
    ) or [{}]
    return {t["key"]: t for t in terms[0].get("matchExpressions") or []}


CPU_CLASS = {
    "id": "af3-pipeline",
    "kind": "job",
    "image": "example.org/af3:1",
    "command": "run_data_pipeline",
    "gpu": {"count": 0},
    "cpu": "16",
    "memory": "64Gi",
    "ephemeral_storage": "32Gi",
}


def _model(spec):
    import models

    return catalog.normalise(models.to_entry(spec, managed_by="api"))


def _build(name, m, placement):
    return jobs.build_run(
        name, m, {}, "demo", None, None, None, None, None, {"output_prefix": "s3://b/out"}, "k", placement=placement
    )


def test_a_gpu_free_run_is_constrained_to_the_pools_whose_node_holds_it(client, admin_keys, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"north": "eu-north1", "south": "eu-south1"}.get(cid, cid))
    m = _model(CPU_CLASS)
    assert m["job"]["ephemeralStorage"] == "32Gi" and m["job"]["cpu"] == "16"
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "af3-pipeline" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/af3-pipeline:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    main = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert main["resources"]["requests"]["cpu"] == "16" and main["resources"]["requests"]["ephemeral-storage"] == "32Gi"
    terms = _affinity(job)
    assert terms[f"{LABEL}/pool"]["values"] == ["cpu-batch-32", "h100-spot-8x"]  # never h100-reserved-1x (14 < 16.1)
    assert "nodeSelector" not in job["spec"]["template"]["spec"]  # no explicit pool: Kueue picks among the fitting ones
    # a shape no node anywhere holds: refused at submission, the largest candidate named, nothing created
    huge = _model({**CPU_CLASS, "id": "huge", "cpu": "200"})
    monkeypatch.setattr(catalog, "get", lambda name: huge if name == "huge" else None)
    r = c.post("/v1/models/huge:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 400 and "impossible shape" in r.text and "cpu 126" in r.text, r.text
    assert not [k for k in fake.jobs if k[1].startswith("op-") and "huge" in str(fake.jobs[k]["metadata"]["labels"])]


def test_explicit_pool_class_and_shared_fs_terms_stay_conjunctive(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"north": "eu-north1", "south": "eu-south1"}.get(cid, cid))
    # an explicit pool that cannot hold the pod: refused, never widened to another pool
    pinned = _model({**CPU_CLASS, "id": "pinned"})
    pinned["job"]["pool"] = "h100-reserved-1x"  # a class's own pin (docs/FLEET.md "CPU pools": `pool: <name>`)
    with pytest.raises(HTTPException) as e:
        _build("op-x", pinned, {"regions": ["north"]})
    assert e.value.status_code == 400 and "pool h100-reserved-1x" in e.value.detail
    # an explicit pool that holds it: the nodeSelector stays, no pool affinity term is added
    ok = _model({**CPU_CLASS, "id": "ok"})
    ok["job"]["pool"] = "cpu-batch-32"
    job = _build("op-y", ok, {"regions": ["north"]})
    assert job["spec"]["template"]["spec"]["nodeSelector"] == {f"{LABEL}/pool": "cpu-batch-32"}
    assert f"{LABEL}/pool" not in _affinity(job)
    # a GPU run: the class term is narrowed to the class pools whose node holds the pod (16.1 CPU needs the 8x nodes)
    gpu = _model({**CPU_CLASS, "id": "gpu", "gpu": {"count": 1, "classes": ["h100"]}, "weights": {"path": "x"}})
    job = _build("op-z", gpu, {"regions": ["north", "south"], "classes": ["h100"]})
    terms = _affinity(job)
    assert terms[f"{LABEL}/pool"]["values"] == ["h100-spot-8x"] and terms[f"{LABEL}/shared-fs"]["values"] == ["true"]
    # a lighter GPU run keeps both class pools
    light = _model({**CPU_CLASS, "id": "light", "gpu": {"count": 1, "classes": ["h100"]}, "cpu": "4", "memory": "16Gi"})
    job = _build("op-w", light, {"regions": ["north", "south"], "classes": ["h100"]})
    assert _affinity(job)[f"{LABEL}/pool"]["values"] == ["h100-reserved-1x", "h100-spot-8x"]


def test_without_fit_yaml_nothing_changes(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: {**DOC, "fit": {}})
    monkeypatch.setattr(kube, "cluster_region", lambda cid: cid)
    m = _model(CPU_CLASS)
    job = _build("op-v", m, {"regions": ["north"]})
    assert f"{LABEL}/pool" not in _affinity(job) and "nodeSelector" not in job["spec"]["template"]["spec"]
