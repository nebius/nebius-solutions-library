"""Per-GPU-class images for run classes (docs/SCHEDULING.md "Per-GPU images for runs", option A of
the design review of 2026-10-09): the class is chosen at submission from the fleet document in the dispatcher's
order (services/api/placement.py), the Job gets that class's image, an affinity over that class's pools only and
the class's profile queue; a resume keeps the class; a run class with one image is untouched. Fixture:
tests/catalog/batch-example-tuned.yaml; the fleet of test_api.py has an RTX PRO 6000 spot pool (1.6) and an H100 one."""

import copy

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import jobs
import kube
import placement
from test_api import FLEET, FakeCluster, H, L, _main, client, cluster, pod  # noqa: F401 (pytest fixtures)

IMG_RTX = "registry.serverless2.local/nebius/batch-example:1.0-rtx-pro-6000"
IMG_H100 = "registry.serverless2.local/nebius/batch-example:1.0-h100"
IMG_DEFAULT = "registry.serverless2.local/nebius/batch-example:1.0-cuda12.8-sm90-120"


# the test fleet with an RTX PRO 6000 pool in the hub too (in single-cluster mode the API of eu-north1 ranks its own region's pools)
FLEET_RTX_IN_HUB = {
    **FLEET,
    "pools": {
        **FLEET["pools"],
        "hub-rtx6000-spot-1x": {
            "region": "hub",
            "pool": "rtx6000-spot-1x",
            "gpu_class": "rtx-pro-6000",
            "usd_per_gpu_hour": 1.6,
        },
    },
}


def fleet_without(monkeypatch, gpu_class):
    """The test fleet with every pool of one GPU class gone: the ranking then names the next class."""
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {**FLEET, "pools": {k: v for k, v in FLEET["pools"].items() if v["gpu_class"] != gpu_class}},
    )


def test_class_images_schema():
    m = catalog.get("batch-example-tuned")
    assert jobs.class_images(m) == {"default": IMG_DEFAULT, "h100": IMG_H100, "rtx-pro-6000": IMG_RTX}
    assert jobs.image_for_class(m, "h100") == IMG_H100 and jobs.image_for_class(m, "l40s") == IMG_DEFAULT
    assert (
        jobs.class_images(catalog.get("batch-example")) is None
        and jobs.image_for_class(catalog.get("batch-example"), "h100") == IMG_DEFAULT
    )
    bad = copy.deepcopy(m)
    bad["job"]["images"] = {"b300": "x"}  # not one of gpu.classes
    with pytest.raises(HTTPException) as e:
        jobs.class_images(bad)
    assert e.value.status_code == 500 and "b300" in e.value.detail
    bad["job"] = {"images": {"h100": "x"}, "command": "x"}  # neither default nor job.image
    with pytest.raises(HTTPException):
        jobs.class_images(bad)
    # catalog.normalise fills job.image from images.default when only images is given
    m2 = catalog.normalise(
        {
            "id": "t",
            "mode": "run",
            "gpu": {"classes": ["h100"]},
            "job": {"images": {"default": "d", "h100": "h"}, "command": "x"},
        }
    )
    assert m2["job"]["image"] == "d"


def test_rank_pools_follows_the_dispatcher_order():
    """services/dispatcher `rank()` without its free-quota signal: class preference, reserved before on-demand before
    spot, then what the run would cost on the node the pool boots; dedicated pools, regions and a pin filter."""
    pools = {
        "hub-h100-reserved-8x": {
            "region": "hub",
            "pool": "h100-reserved-8x",
            "gpu_class": "h100",
            "capacity": "reserved",
            "usd_per_gpu_hour": 3.0,
            "gpus_per_node": 8,
        },
        "hub-h100-ondemand-1x": {
            "region": "hub",
            "pool": "h100-ondemand-1x",
            "gpu_class": "h100",
            "capacity": "on_demand",
            "usd_per_gpu_hour": 4.5,
        },
        "hub-h100-spot-1x": {
            "region": "hub",
            "pool": "h100-spot-1x",
            "gpu_class": "h100",
            "capacity": "spot",
            "usd_per_gpu_hour": 2.15,
        },
        "hub-h100-spot-8x": {
            "region": "hub",
            "pool": "h100-spot-8x",
            "gpu_class": "h100",
            "capacity": "spot",
            "usd_per_gpu_hour": 0.79,
            "gpus_per_node": 8,
        },
        "hub-h100-lab-8x": {
            "region": "hub",
            "pool": "h100-lab-8x",
            "gpu_class": "h100",
            "capacity": "reserved",
            "usd_per_gpu_hour": 0,
            "gpus_per_node": 8,
            "tenant": "lab",
        },
        "eu-south1-rtx6000-spot-1x": {
            "region": "eu-south1",
            "pool": "rtx6000-spot-1x",
            "gpu_class": "rtx-pro-6000",
            "capacity": "spot",
            "usd_per_gpu_hour": 0.95,
        },
    }
    order = lambda **kw: [c["pool"] for c in placement.rank_pools(pools, ["rtx-pro-6000", "h100"], 1, **kw)]  # noqa: E731
    # the preferred class first; inside a class reserved (marginal cost 0), on-demand, spot; a 1-GPU run pays the whole
    # 8-GPU node a spot-8x pool would boot (8 x 0.79 > 2.15), so the 1x spot pool comes first; the lab's pool is not ours
    assert order() == ["rtx6000-spot-1x", "h100-reserved-8x", "h100-ondemand-1x", "h100-spot-1x", "h100-spot-8x"]
    assert order(tenant="lab")[:2] == ["rtx6000-spot-1x", "h100-lab-8x"]
    assert [c["pool"] for c in placement.rank_pools(pools, ["h100"], 8, tenant="demo")] == [
        "h100-reserved-8x",
        "h100-ondemand-1x",
        "h100-spot-8x",
        "h100-spot-1x",
    ]  # capacity first; a whole-node run: 8 x 0.79 < 8 x 2.15
    assert order(regions=["eu-north1"])[0] == "h100-reserved-8x" and order(pin="hub") == order(
        pin="eu-north1"
    ) == order(regions=["eu-north1"])
    assert order(pin="eu-south1") == ["rtx6000-spot-1x"] and placement.rank_pools(pools, ["b300"], 1) == []
    best = placement.rank_pools(pools, ["h100"], 1)[0]
    assert best == {
        "cluster": "hub",
        "region": "eu-north1",
        "pool": "h100-reserved-8x",
        "gpu_class": "h100",
        "capacity": "reserved",
        "price": 0.0,
        "cost": 0.0,
    }


def test_choose_class_from_the_fleet_document(monkeypatch):
    m = catalog.get("batch-example-tuned")
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    assert placement.choose_class(m, None, 1, None) == (
        "rtx-pro-6000",
        "fleet (eu-south1/rtx6000-spot-1x, spot, $1.6/GPU-h)",
    )
    assert placement.choose_class(m, "eu-north1", 2, None)[0] == "h100"  # the caller's region pins the candidates
    assert (
        placement.choose_class(m, None, 1, ["h100", "rtx-pro-6000"])[0] == "h100"
    )  # the caller's preference narrows and orders
    assert (
        placement.choose_class(m, None, 1, ["b300"])[0] == "rtx-pro-6000"
    )  # a class the model does not allow is ignored
    fleet_without(monkeypatch, "rtx-pro-6000")
    assert placement.choose_class(m, None, 1, None)[0] == "h100"  # the preferred class has no pool
    monkeypatch.setattr(kube, "fleet", lambda: {"pools": {}})  # no fleet data: the preferred class, the queue waits
    assert placement.choose_class(m, None, 1, None) == ("rtx-pro-6000", "preferred (no pool)")


def test_run_with_per_class_images_is_rendered_for_the_chosen_class(client, monkeypatch):
    """In single-cluster mode the API (eu-north1 here) creates the Job in its own cluster, so its region pins the ranking: with an
    RTX pool in the hub the preferred class wins, without one the H100 pool of the region decides."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_RTX_IN_HUB)
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()
    assert op["gpu_class"] == "rtx-pro-6000" and op["image"] == IMG_RTX and op["profile"] == "prefer-rtx-pro-6000"
    job = fake.jobs[("tenant-demo", op["id"])]
    assert _main(job)["image"] == IMG_RTX and job["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000"
    assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-rtx-pro-6000"
    terms = job["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"]
    assert terms[0] == {"key": f"{L}/pool", "operator": "In", "values": ["rtx6000-spot-1x"]}  # that class's pools only
    assert job["spec"]["template"]["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000"  # the pods carry it
    up = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][1]["env"]}
    assert up["GPU_CLASS"] == "rtx-pro-6000"
    # the region has no RTX pool next time: the h100 image, the h100 pools, the h100 profile
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    j = fake.jobs[("tenant-demo", r.json()["id"])]
    assert _main(j)["image"] == IMG_H100 and r.json()["gpu_class"] == "h100" and r.json()["profile"] == "prefer-h100"
    assert j["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ][0]["matchExpressions"][0]["values"] == ["h100-spot-1x"]
    # a run class with one image is untouched: no class label, affinity over every class, image unchanged
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    j = fake.jobs[("tenant-demo", r.json()["id"])]
    assert (
        r.json()["gpu_class"] is None
        and f"{L}/gpu-class" not in j["metadata"]["labels"]
        and _main(j)["image"] == IMG_DEFAULT
    )
    assert sorted(
        j["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"
        ][0]["matchExpressions"][0]["values"]
    ) == ["h100-spot-1x", "rtx6000-spot-1x"]
    # a live pod's attempt and a bucket record both report the class
    p = pod(op["id"], "Running")
    p["metadata"]["labels"][f"{L}/gpu-class"] = "rtx-pro-6000"
    fake.pods.append(p)
    g = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert g["attempts"][0]["gpu_class"] == "rtx-pro-6000" and g["gpu_class"] == "rtx-pro-6000"
    import status

    att = status._attempts(
        [],
        [
            {
                "operation": op["id"],
                "pod": "x",
                "status": "succeeded",
                "gpus": 1,
                "gpu_class": "h100",
                "started_at": "2026-10-09T00:00:00Z",
                "ended_at": "2026-10-09T00:10:00Z",
            }
        ],
        op["id"],
    )
    assert att[0]["gpu_class"] == "h100"


def test_resume_keeps_the_class_even_when_the_ranking_changed(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_RTX_IN_HUB)
    oid = c.post(
        "/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H
    ).json()["id"]
    job = fake.jobs[("tenant-demo", oid)]
    job["status"] = {
        "startTime": "2026-10-09T00:00:30Z",
        "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}],
    }
    fake.pods.append(pod(oid, "Failed", finished="2026-10-09T00:05:00Z", exit_code=1))
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)  # the ranking now names h100: irrelevant for a resume

    def never(*a, **k):  # ... which never asks for a class again
        raise AssertionError("choose_class called on resume")

    monkeypatch.setattr(appmod.placing, "choose_class", never)
    r = c.post(f"/v1/operations/{oid}:resume", headers=H)
    assert r.status_code == 202 and r.json()["gpu_class"] == "rtx-pro-6000" and r.json()["image"] == IMG_RTX
    new = fake.jobs[("tenant-demo", f"{oid}-r1")]
    assert _main(new)["image"] == IMG_RTX and new["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000"


def test_fleet_path_pins_the_chosen_class_for_the_dispatcher(client, monkeypatch):
    """Control-cluster API: the manager Job carries the chosen class as its only allowed class (the pod template
    annotation the dispatcher reads), so the run may move between regions of that class but never to another."""
    c, control = client
    hub = FakeCluster()
    both = {"control": control, "eu-north1": hub}
    monkeypatch.setattr(kube, "regions", lambda: ["control", "eu-north1"])
    monkeypatch.setattr(kube, "batch", lambda region="control": both[region])
    monkeypatch.setattr(kube, "core", lambda region="control": both[region])
    monkeypatch.setattr(kube, "HUB_REGION", "eu-north1")
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    for mod in (appmod, jobs, appmod.artifacts):
        monkeypatch.setattr(mod, "REGION", "control")
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(
        appmod.artifacts, "storage", lambda ns, region="control": {"bucket": "serverless2-demo-eu-north1"}
    )
    fleet_without(monkeypatch, "rtx-pro-6000")  # the ranking names h100
    r = c.post(
        "/v1/models/batch-example-tuned:invoke",
        json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()
    job = control.jobs[("tenant-demo", op["id"])]
    assert (
        job["spec"]["managedBy"] == "kueue.x-k8s.io/multikueue" and op["region"] is None and op["gpu_class"] == "h100"
    )
    assert _main(job)["image"] == IMG_H100 and job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-h100"
    tmpl = job["spec"]["template"]["metadata"]["annotations"]
    assert tmpl[f"{L}/gpu-classes"] == "h100" and tmpl[f"{L}/regions"] == "eu-north1,eu-south1"
    # an explicit region pins the ranking (the H100 region, although the RTX pool is back and cheaper) and stays on the Job
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    r = c.post(
        "/v1/models/batch-example-tuned:invoke",
        json={"region": "eu-north1", "input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    assert (
        r.json()["gpu_class"] == "h100"
        and control.jobs[("tenant-demo", r.json()["id"])]["metadata"]["labels"][f"{L}/region"] == "eu-north1"
    )
