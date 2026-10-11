"""Portable retries and placement-time image selection (docs/SCHEDULING.md "Fallback to the next class", docs/JOBS.md
"Execution options"): a queued run moves to the next class of its preference after the wait bound, a resume with a
checkpoint reference needs no volume and may change class (and image), a resume re-renders the pinned model revision,
and a queued operation says why it waits."""
import json
import pytest

from test_api import FLEET, H, client, cluster  # noqa: F401
import app as appmod
import catalog
import db
import execution
import jobs
import kube
from config import LABEL

CFG = {"identities": {}, "execution_profiles": {}}
PER_CLASS = {"id": "fold", "kind": "job", "image": "img:default", "images": {"rtx-pro-6000": "img:rtx", "l40s": "img:l40s"},
             "command": "fold", "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]}}
TWO_CLASS_FLEET = {"pools": {
    "us-rtx": {"region": "us-central1", "pool": "rtx6000-spot-1x", "gpu_class": "rtx-pro-6000", "usd_per_gpu_hour": 1.09},
    "hub-l40s": {"region": "hub", "pool": "l40s-spot-1x", "gpu_class": "l40s", "usd_per_gpu_hour": 0.85}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(execution, "tenant_config", lambda ns, region="eu-north1": CFG)
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "us-central1"])
    monkeypatch.setattr(kube, "workload_reasons", lambda ns: {})
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(jobs.placing if hasattr(jobs, "placing") else appmod.placing, "choose_class", lambda m, region, gpus, available=None, tenant=None: (available[0] if available else "rtx-pro-6000", "test"))


def _fold_model():
    import models
    e = models.to_entry(PER_CLASS, managed_by="api")
    return catalog.normalise({**e, "version": 3})


def test_fallback_moves_a_queued_run_to_the_next_class(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)     # after the client fixture's own fleet
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "execution": {"placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "fallback_wait_s": 60}}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[("tenant-demo", op)]
    assert job["metadata"]["labels"][f"{LABEL}/gpu-class"] == "rtx-pro-6000" and job["metadata"]["annotations"][f"{LABEL}/fallback-wait-s"] == "60"
    assert job["metadata"]["annotations"][f"{LABEL}/model-version"] == "3"
    assert next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")["image"].endswith("img:rtx")
    # not yet: the wait bound has not passed
    from datetime import datetime, timezone
    job["metadata"]["creationTimestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert appmod.fallback_switch() == []
    job["metadata"]["creationTimestamp"] = "2026-10-05T20:00:00Z"
    moved = appmod.fallback_switch()
    assert moved == [f"{op}-r1"] and ("tenant-demo", op) not in fake.jobs
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    assert new["metadata"]["labels"][f"{LABEL}/gpu-class"] == "l40s" and new["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-l40s"
    assert next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")["image"].endswith("img:l40s")
    assert new["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/gpu-classes"] == "l40s"
    assert json.loads(new["metadata"]["annotations"][f"{LABEL}/execution"])["placement"]["gpu_preference"] == ["l40s"]
    assert "rtx-pro-6000 -> l40s" in new["metadata"]["annotations"][f"{LABEL}/fallback"]
    got = c.get(f"/v1/operations/{op}-r1", headers=H).json()
    assert got["resumed_from"] == op and "rtx-pro-6000 -> l40s" in got["reason"]
    # the last class of the preference keeps waiting
    new["metadata"]["creationTimestamp"] = "2026-10-05T20:00:00Z"
    assert appmod.fallback_switch() == []


def test_resume_with_a_checkpoint_reference_needs_no_volume_and_may_change_class(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)     # after the client fixture's own fleet
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "region": "us-central1", "execution": {"recovery": {"on_infrastructure_loss": "fail"}}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[("tenant-demo", op)]
    job["status"] = {"conditions": [{"type": "Failed", "status": "True", "message": "node lost"}]}
    fake.pvcs.pop(("tenant-demo", f"{op}-work"), None)                    # the volume is gone with the region
    assert c.post(f"/v1/operations/{op}:resume", headers=H).status_code == 409
    assert c.post(f"/v1/operations/{op}:resume", json={"gpu_class": "l40s"}, headers=H).status_code == 400
    r = c.post(f"/v1/operations/{op}:resume", json={"checkpoint_ref": "s3://cust/ckpt/step-900", "gpu_class": "l40s"}, headers=H)
    assert r.status_code == 202, r.text
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert main["image"].endswith("img:l40s") and {"name": "CHECKPOINT_REF", "value": "s3://cust/ckpt/step-900"} in main["env"]
    assert new["metadata"]["labels"][f"{LABEL}/gpu-class"] == "l40s" and new["metadata"]["annotations"][f"{LABEL}/pvc"] == f"{op}-r1-work"
    assert new["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/gpu-classes"] == "l40s"
    assert ("tenant-demo", f"{op}-r1-work") not in fake.pvcs             # fleet-placed: the dispatcher creates the fresh volume on the worker it picks


def test_resume_renders_the_pinned_revision(monkeypatch):
    import models
    old_spec = {**PER_CLASS, "command": "fold --v3"}
    current = catalog.normalise({**models.to_entry({**PER_CLASS, "command": "fold --v4"}, managed_by="api"), "version": 4})
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "history", lambda mid, limit=50: [{"version": 4, "spec": {**PER_CLASS, "command": "fold --v4"}}, {"version": 3, "spec": old_spec}])
    pinned = catalog.pinned(current, {f"{LABEL}/model-version": "3"})
    assert pinned["job"]["command"] == "fold --v3" and pinned["version"] == 3
    assert catalog.pinned(current, {f"{LABEL}/model-version": "4"}) is current
    assert catalog.pinned(current, {}) is current


def test_queued_operation_reports_the_workload_reason(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/container-run:invoke", json={"input": {"image": "x", "command": "y"}}, headers=H)
    op = r.json()["id"]
    monkeypatch.setattr(kube, "workload_reasons", lambda ns: {f"uid-{op}": "Quota reserved failed: insufficient quota for nvidia.com/gpu in flavor rtx6000-spot-1x"})
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["status"] == "QUEUED" and "insufficient quota" in got["reason"]
