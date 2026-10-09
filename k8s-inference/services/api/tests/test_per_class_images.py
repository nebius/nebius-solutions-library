"""Per-GPU-class images for run classes (docs/SCHEDULING.md "Per-GPU images for runs", option A of
docs/DESIGN-REVIEW-2026-10-09.md): the class is chosen at submission from the dispatcher's ranking, the Job gets
that class's image, an affinity over that class's pools only and the class's profile queue; a resume keeps the
class; a run class with one image is untouched. Fixture: tests/catalog/batch-example-tuned.yaml."""
import copy
import json

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import jobs
import kube
import placement
from test_api import FLEET, H, L, FakeCluster, _main, client, cluster, pod  # noqa: F401 (pytest fixtures)

IMG_RTX = "registry.serverless2.local/nebius/batch-example:1.0-rtx-pro-6000"
IMG_H100 = "registry.serverless2.local/nebius/batch-example:1.0-h100"
IMG_DEFAULT = "registry.serverless2.local/nebius/batch-example:1.0-cuda12.8-sm90-120"


def rank_answer(best_class, region="eu-south1", pool="rtx6000-spot-1x", free=True):
    return {"best": {"cluster": region, "region": region, "pool": pool, "gpu_class": best_class, "capacity": "spot", "price": 0.95, "free": free},
            "ranked": [], "profile": {}}


def fake_dispatcher(monkeypatch, best_class, calls):
    class R:
        def __init__(self, body): self._b = body
        def raise_for_status(self): return None
        def json(self): return self._b

    def get(url, params=None, timeout=None):
        calls.append(params)
        return R(rank_answer(best_class))
    monkeypatch.setattr(placement.httpx, "get", get)


def test_class_images_schema():
    m = catalog.get("batch-example-tuned")
    assert jobs.class_images(m) == {"default": IMG_DEFAULT, "h100": IMG_H100, "rtx-pro-6000": IMG_RTX}
    assert jobs.image_for_class(m, "h100") == IMG_H100 and jobs.image_for_class(m, "l40s") == IMG_DEFAULT
    assert jobs.class_images(catalog.get("batch-example")) is None and jobs.image_for_class(catalog.get("batch-example"), "h100") == IMG_DEFAULT
    bad = copy.deepcopy(m)
    bad["job"]["images"] = {"b300": "x"}                      # not one of gpu.classes
    with pytest.raises(HTTPException) as e:
        jobs.class_images(bad)
    assert e.value.status_code == 500 and "b300" in e.value.detail
    bad["job"] = {"images": {"h100": "x"}, "command": "x"}      # neither default nor job.image
    with pytest.raises(HTTPException):
        jobs.class_images(bad)
    # catalog.normalise fills job.image from images.default when only images is given
    m2 = catalog.normalise({"id": "t", "mode": "run", "gpu": {"classes": ["h100"]}, "job": {"images": {"default": "d", "h100": "h"}, "command": "x"}})
    assert m2["job"]["image"] == "d"


def test_choose_class_from_the_ranking_and_without_it(monkeypatch):
    m = catalog.get("batch-example-tuned")
    calls = []
    fake_dispatcher(monkeypatch, "h100", calls)
    assert placement.choose_class(m, None, 1, ["h100", "rtx-pro-6000"])[0] == "h100"
    assert calls[0]["profile"] == "prefer-rtx-pro-6000" and calls[0]["classes"] == "rtx-pro-6000,h100" and "pin" not in calls[0]
    assert placement.choose_class(m, "eu-south1", 2, None)[0] == "h100" and calls[1]["pin"] == "eu-south1" and calls[1]["gpus"] == 2
    # a class the ranking names that the model does not allow is ignored -> preferred class
    fake_dispatcher(monkeypatch, "b300", calls)
    assert placement.choose_class(m, None, 1, None) == ("rtx-pro-6000", "preferred (no ranking)")
    # the dispatcher is down: the preferred class that has a pool
    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(placement.httpx, "get", boom)
    assert placement.choose_class(m, None, 1, ["h100"]) == ("h100", "preferred (no ranking)")
    assert placement.choose_class(m, None, 1, None)[0] == "rtx-pro-6000"


def test_run_with_per_class_images_is_rendered_for_the_chosen_class(client, monkeypatch):
    c, fake = client
    calls = []
    fake_dispatcher(monkeypatch, "rtx-pro-6000", calls)
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()
    assert op["gpu_class"] == "rtx-pro-6000" and op["image"] == IMG_RTX and op["profile"] == "prefer-rtx-pro-6000"
    job = fake.jobs[("tenant-demo", op["id"])]
    assert _main(job)["image"] == IMG_RTX and job["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000"
    assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-rtx-pro-6000"
    terms = job["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
    assert terms[0] == {"key": f"{L}/pool", "operator": "In", "values": ["rtx6000-spot-1x"]}     # that class's pools only
    assert job["spec"]["template"]["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000"           # the pods carry it
    up = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][1]["env"]}
    assert up["GPU_CLASS"] == "rtx-pro-6000"
    # the ranking says h100 next time: the h100 image, the h100 pools, the h100 profile
    fake_dispatcher(monkeypatch, "h100", calls)
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    j = fake.jobs[("tenant-demo", r.json()["id"])]
    assert _main(j)["image"] == IMG_H100 and r.json()["gpu_class"] == "h100" and r.json()["profile"] == "prefer-h100"
    assert j["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["h100-spot-1x"]
    # a run class with one image is untouched: no class label, affinity over every class, image unchanged
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    j = fake.jobs[("tenant-demo", r.json()["id"])]
    assert r.json()["gpu_class"] is None and f"{L}/gpu-class" not in j["metadata"]["labels"] and _main(j)["image"] == IMG_DEFAULT
    assert sorted(j["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]) == ["h100-spot-1x", "rtx6000-spot-1x"]
    # a live pod's attempt and a bucket record both report the class
    jp = fake.jobs[("tenant-demo", op["id"])]
    p = pod(op["id"], "Running")
    p["metadata"]["labels"][f"{L}/gpu-class"] = "rtx-pro-6000"
    fake.pods.append(p)
    g = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert g["attempts"][0]["gpu_class"] == "rtx-pro-6000" and g["gpu_class"] == "rtx-pro-6000"
    import status
    att = status._attempts([], [{"operation": op["id"], "pod": "x", "status": "succeeded", "gpus": 1, "gpu_class": "h100",
                                 "started_at": "2026-10-09T00:00:00Z", "ended_at": "2026-10-09T00:10:00Z"}], op["id"])
    assert att[0]["gpu_class"] == "h100"


def test_resume_keeps_the_class_even_when_the_ranking_changed(client, monkeypatch):
    c, fake = client
    calls = []
    fake_dispatcher(monkeypatch, "rtx-pro-6000", calls)
    oid = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H).json()["id"]
    job = fake.jobs[("tenant-demo", oid)]
    job["status"] = {"startTime": "2026-10-09T00:00:30Z", "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
    fake.pods.append(pod(oid, "Failed", finished="2026-10-09T00:05:00Z", exit_code=1))
    fake_dispatcher(monkeypatch, "h100", calls)                 # the ranking now prefers h100: irrelevant for a resume
    n = len(calls)
    r = c.post(f"/v1/operations/{oid}:resume", headers=H)
    assert r.status_code == 202 and r.json()["gpu_class"] == "rtx-pro-6000" and r.json()["image"] == IMG_RTX
    new = fake.jobs[("tenant-demo", f"{oid}-r1")]
    assert _main(new)["image"] == IMG_RTX and new["metadata"]["labels"][f"{L}/gpu-class"] == "rtx-pro-6000" and len(calls) == n


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
    monkeypatch.setattr(appmod.artifacts, "storage", lambda ns, region="control": {"bucket": "serverless2-demo-eu-north1"})
    calls = []
    fake_dispatcher(monkeypatch, "h100", calls)
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()
    job = control.jobs[("tenant-demo", op["id"])]
    assert job["spec"]["managedBy"] == "kueue.x-k8s.io/multikueue" and op["region"] is None and op["gpu_class"] == "h100"
    assert _main(job)["image"] == IMG_H100 and job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-h100"
    tmpl = job["spec"]["template"]["metadata"]["annotations"]
    assert tmpl[f"{L}/gpu-classes"] == "h100" and tmpl[f"{L}/regions"] == "eu-north1,eu-south1"
    assert calls[-1]["regions"] == "eu-north1,eu-south1" and "pin" not in calls[-1]
    # an explicit region is passed to the ranking as the pin and stays on the Job
    r = c.post("/v1/models/batch-example-tuned:invoke", json={"region": "eu-south1", "input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}}, headers=H)
    assert r.status_code == 202 and calls[-1]["pin"] == "eu-south1" and control.jobs[("tenant-demo", r.json()["id"])]["metadata"]["labels"][f"{L}/region"] == "eu-south1"
