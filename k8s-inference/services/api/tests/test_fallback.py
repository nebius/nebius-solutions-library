"""Portable retries and placement-time image selection (docs/SCHEDULING.md "Fallback to the next class", docs/JOBS.md
"Execution options"): a queued run moves to the next class of its preference after the wait bound, a resume with a
checkpoint reference needs no volume and may change class (and image), a resume re-renders the pinned model revision,
and a queued operation says why it waits."""
import json
import pytest

from test_api import FLEET, H, client, cluster  # noqa: F401
import app as appmod
import attempts
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
    assert moved == [f"{op}-r1"]
    # the original handle stays readable, names its successor and never admits again
    orig = fake.jobs[("tenant-demo", op)]
    assert orig["metadata"]["annotations"][f"{LABEL}/successor"] == f"{op}-r1" and orig["spec"]["suspend"] is True
    via = c.get(f"/v1/operations/{op}", headers=H).json()
    assert via["id"] == f"{op}-r1" and via["requested_id"] == op and via["resumed_from"] == op
    assert jobs.normalise(orig)["status"] == "SUPERSEDED" and jobs.normalise(orig)["successor"] == f"{op}-r1"
    assert appmod.fallback_switch() == []        # a superseded original is never moved again
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


@pytest.mark.parametrize("binding_available", [True, False])
def test_fallback_delivers_private_input_before_creating_successor(client, monkeypatch, binding_available):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    monkeypatch.setattr(catalog, "get", lambda name: _fold_model() if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    assert c.put("/v1/bindings/private-fallback", json={"data": {"input.json": "private"}}, headers=H).status_code == 200
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "execution": {
        "io_mode": "application", "private_input_ref": "private-fallback",
        "placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "fallback_wait_s": 60}}}, headers=H)
    assert r.status_code == 202, r.text
    root = r.json()["id"]
    successor = f"{root}-r1"
    create = fake.create_namespaced_job

    def require_private_input(ns, body):
        assert fake.secrets[(ns, f"{successor}-private")]["data"] == {"input.json": "private"}
        return create(ns, body)

    monkeypatch.setattr(fake, "create_namespaced_job", require_private_input)
    if not binding_available:
        assert c.delete("/v1/bindings/private-fallback", headers=H).status_code == 204
    moved = appmod.fallback_switch()
    assert moved == ([successor] if binding_available else [])
    original = fake.jobs[("tenant-demo", root)]
    if binding_available:
        assert fake.secrets[("tenant-demo", f"{successor}-private")]["metadata"]["labels"][f"{LABEL}/operation"] == root
        assert original["metadata"]["annotations"][f"{LABEL}/successor"] == successor
    else:
        assert ("tenant-demo", successor) not in fake.jobs
        assert f"{LABEL}/successor" not in original["metadata"]["annotations"]


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


def test_resume_replays_one_continuation_per_idempotency_key(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/container-run:invoke", json={"input": {"image": "x", "command": "y"}, "region": "eu-north1"}, headers=H)
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {f"uid-{op}": "hub"})        # it ran on the hub ...
    fake.pvcs[("tenant-demo", f"{op}-work")] = {"metadata": {"name": f"{op}-work"}}          # ... and kept its volume
    h = {**H, "Idempotency-Key": "intent-7"}
    a = c.post(f"/v1/operations/{op}:resume", headers=h)
    b = c.post(f"/v1/operations/{op}:resume", headers=h)
    assert a.status_code == 202, a.text
    assert b.status_code == 200 and a.json()["id"] == b.json()["id"] and a.json()["id"].startswith(f"{op}-r-"), b.text
    conflict = c.post(f"/v1/operations/{op}:resume", json={"checkpoint_ref": "s3://other"}, headers=h)
    assert conflict.status_code == 409                       # the same intent with different input
    other = c.post(f"/v1/operations/{op}:resume", headers={**H, "Idempotency-Key": "intent-8"})
    assert other.status_code == 202 and other.json()["id"] != a.json()["id"]
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-r")]) == 2


def test_resume_uses_the_acknowledged_checkpoint_pointer(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "region": "us-central1", "execution": {"io_mode": "application", "recovery": {"on_infrastructure_loss": "fail"}}}, headers=H)
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True", "message": "node lost"}]}
    fake.pvcs.pop(("tenant-demo", f"{op}-work"), None)                    # abrupt loss: the volume is gone with the node
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [
        {"operation": op, "pod": f"{op}-a", "root": op, "tenant": "demo", "status": "started", "epoch": 1, "revision": 5, "checkpoint_ref": "app://ckpt/gen-3", "checkpoint_generation": 3, "gpus": 1},
        {"operation": op, "pod": f"{op}-a", "root": op, "tenant": "demo", "status": "started", "epoch": 1, "revision": 4, "checkpoint_ref": "app://ckpt/gen-2", "checkpoint_generation": 2, "gpus": 1}])
    assert attempts.latest_checkpoint(op)["checkpoint_ref"] == "app://ckpt/gen-3"
    r = c.post(f"/v1/operations/{op}:resume", json={"gpu_class": "l40s"}, headers=H)       # no checkpoint_ref: the acknowledged pointer
    assert r.status_code == 202, r.text
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert {"name": "CHECKPOINT_REF", "value": "app://ckpt/gen-3"} in main["env"] and main["image"].endswith("img:l40s")
    assert r.json()["execution"]["recovery"]["checkpoint_ref"] == "app://ckpt/gen-3"


def test_expired_operation_is_served_from_its_rows_and_superseded_handles_follow(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    rows = [{"operation": "op-gone", "pod": "op-gone-a", "root": "op-gone", "tenant": "demo", "status": "succeeded", "epoch": 1, "revision": 9,
             "export_state": "succeeded", "started_at": "2026-10-11T10:00:00Z", "ended_at": "2026-10-11T11:00:00Z", "gpus": 1, "region": "eu-north1"}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == "op-gone" else [])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    got = c.get("/v1/operations/op-gone", headers=H)
    assert got.status_code == 200 and got.json()["status"] == "SUCCEEDED" and got.json()["attempt_id"] == "op-gone-a" and "durable record" in got.json()["reason"]
    assert c.get("/v1/operations/op-unknown", headers=H).status_code == 404
    # a superseded handle whose Job expired resolves through the database link
    monkeypatch.setattr(attempts, "_marks", lambda operation: (3, None, "op-gone") if operation == "op-old" else None)
    assert c.get("/v1/operations/op-old", headers=H).json()["id"] == "op-gone"
    assert c.post("/v1/operations/op-gone:cancel", headers=H).status_code == 409
