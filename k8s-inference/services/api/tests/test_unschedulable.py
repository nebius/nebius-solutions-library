"""Same-root recovery of an admitted-but-unschedulable run (docs/SCHEDULING.md "Per-node fit"): the exact AlphaFold 3
MCP case of 2026-10-14 (admitted by the worker's Kueue on h100-reserved-1x, 16.1 CPU on 14-CPU nodes, Pending for ever)
moves to a successor on the same root rendered with the fit affinity; the original is fenced and keeps its handle;
a pod that merely waits for a node, a started attempt, an unplaced run, a cancelled run, an impossible shape, the
switch bound and a failed create are the boundaries."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import jobs
import kube
import reconciliation
from config import LABEL
from test_api import H, admin_keys, client, cluster, pod  # noqa: F401
from test_fit import AF3_POD, CPU_CLASS, DOC, _affinity, _model

NS = "tenant-demo"


@pytest.fixture(autouse=True)
def _fresh_catalog():
    yield
    catalog._cache.update({"mtime": None, "models": {}})


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending(op: str, pool: str, since_s: int = 1200, requests: dict | None = None, unschedulable: bool = True):
    """The worker's pod as Kueue admitted it: the flavor's pool injected as nodeSelector, never bound to a node."""
    p = pod(op, "Pending", started=_ago(since_s + 5))
    p["spec"].pop("nodeName", None)
    p["spec"]["nodeSelector"] = {f"{LABEL}/pool": pool}
    p["spec"]["initContainers"] = AF3_POD["initContainers"]
    p["spec"]["containers"] = [
        {
            **c,
            "resources": {
                "requests": {**c["resources"]["requests"], **(requests or {} if c["name"] == "main" else {})}
            },
        }
        for c in AF3_POD["containers"]
    ]
    p["status"]["conditions"] = (
        [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": _ago(since_s)}]
        if unschedulable
        else []
    )
    return p


@pytest.fixture
def placed(client, monkeypatch):
    """An AF3 pipeline run placed on north, its pod admitted on h100-reserved-1x and unschedulable there."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"north": "eu-north1", "south": "eu-south1"}.get(cid, cid))
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(reconciliation, "FLEET_MANAGER", True)
    m = _model(CPU_CLASS)
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "af3-pipeline" else None)
    monkeypatch.setattr(
        catalog, "require", lambda name: m if name == "af3-pipeline" else (_ for _ in ()).throw(HTTPException(404))
    )
    fences: list = []
    monkeypatch.setattr(kube, "deactivate_workload", lambda ns, uid, region="eu-north1": fences.append(uid) or 1)
    monkeypatch.setattr(kube, "activate_workload", lambda ns, uid, region="eu-north1": fences.remove(uid) or 1)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/af3-pipeline:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[(NS, op)]
    assert _affinity(job)[f"{LABEL}/pool"]["values"] == ["cpu-batch-32", "h100-spot-8x"]
    # the live defect: the original render of 0.11.78 carried no fit term, and the worker's Kueue put it on the 1x pool
    job["spec"]["template"]["spec"].pop("affinity", None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {job["metadata"]["uid"]: "north"})
    return c, fake, op, job, fences


def test_the_af3_run_moves_to_a_successor_with_the_fit_affinity(placed):
    c, fake, op, job, fences = placed
    fake.pods.append(_pending(op, "h100-reserved-1x"))
    moved = appmod.unschedulable_switch()
    assert moved == [f"{op}-r1"]
    orig = fake.jobs[(NS, op)]
    ann = orig["metadata"]["annotations"]
    assert orig["spec"]["suspend"] is True and ann[f"{LABEL}/successor"] == f"{op}-r1"
    assert ann[f"{LABEL}/recovery"].startswith("unschedulable on north/h100-reserved-1x for 1")
    assert "cpu 16.1" in ann[f"{LABEL}/recovery"] and "cpu-batch-32" in ann[f"{LABEL}/recovery"]
    assert fences == [orig["metadata"]["uid"]]  # the worker's copy and its Pending pod go
    assert jobs.normalise(orig)["status"] == "SUPERSEDED"
    new = fake.jobs[(NS, f"{op}-r1")]
    nann = new["metadata"]["annotations"]
    assert nann[f"{LABEL}/resumed-from"] == op and nann[f"{LABEL}/recovered-from"] == op
    assert nann[f"{LABEL}/key"] == ann[f"{LABEL}/key"] and new["metadata"]["labels"][f"{LABEL}/tenant"] == "demo"
    assert _affinity(new)[f"{LABEL}/pool"]["values"] == ["cpu-batch-32", "h100-spot-8x"]  # never the 1x pool again
    assert (
        new["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]
        == orig["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]
    )
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert main["resources"]["requests"] == {
        "cpu": "16",
        "memory": "64Gi",
        "ephemeral-storage": "32Gi",
    }  # nothing reduced
    # the owner's handle follows the successor; the successor says why it exists
    via = c.get(f"/v1/operations/{op}", headers=H).json()
    assert via["id"] == f"{op}-r1" and via["requested_id"] == op and via["resumed_from"] == op
    assert via["recovered_from"] == op and via["reason"].startswith("unschedulable on north/h100-reserved-1x")
    assert appmod.unschedulable_switch() == []  # a superseded original is never moved again


def test_boundaries_wait_node_started_unplaced_cancelled(placed, monkeypatch):
    c, fake, op, job, fences = placed
    # not yet: the pod has been unschedulable for less than the wait
    fake.pods.append(_pending(op, "h100-reserved-1x", since_s=30))
    assert appmod.unschedulable_switch() == [] and not fences
    fake.pods.clear()
    # a pool that can hold the pod (scaling from zero, full for the moment): temporarily unavailable, left alone
    fake.pods.append(_pending(op, "cpu-batch-32"))
    assert appmod.unschedulable_switch() == [] and not fences
    assert f"{LABEL}/unschedulable" not in fake.jobs[(NS, op)]["metadata"]["annotations"]
    fake.pods.clear()
    # Pending without the scheduler's verdict (no condition yet): nothing
    fake.pods.append(_pending(op, "h100-reserved-1x", unschedulable=False))
    assert appmod.unschedulable_switch() == []
    fake.pods.clear()
    # an attempt that started: never an unschedulable switch (and the builder refuses it outright)
    running = _pending(op, "h100-reserved-1x")
    running["spec"]["nodeName"], running["status"]["phase"] = "node-1", "Running"
    fake.pods.append(running)
    assert appmod.unschedulable_switch() == []
    with pytest.raises(HTTPException) as e:
        jobs.switch_unschedulable(NS, op, "eu-north1", catalog.get("af3-pipeline"), {"output_prefix": "s3://b/o"}, "x")
    assert e.value.status_code == 409 and "started attempt" in e.value.detail
    fake.pods.clear()
    # not placed anywhere: that is the queue, not this
    fake.pods.append(_pending(op, "h100-reserved-1x"))
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    assert appmod.unschedulable_switch() == []
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {job["metadata"]["uid"]: "north"})
    # cancelled: never resurrected
    job["metadata"]["annotations"][f"{LABEL}/cancelled"] = "true"
    assert appmod.unschedulable_switch() == [] and not fences


def test_impossible_shape_is_annotated_once_and_never_moved(placed):
    c, fake, op, job, fences = placed
    fake.pods.append(_pending(op, "h100-reserved-1x", requests={"cpu": "200"}))
    assert appmod.unschedulable_switch() == [] and not fences
    note = fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/unschedulable"]
    assert note.startswith("impossible shape: the pod needs cpu 200.1") and "assigned north/h100-reserved-1x" in note
    assert "cpu 126" in note  # the largest node of the fleet, for the operator
    assert fake.jobs[(NS, op)].get("spec", {}).get("suspend") is not True
    assert appmod.unschedulable_switch() == []  # annotated: not evaluated again


def test_switch_bound_per_root(placed):
    c, fake, op, job, fences = placed
    fake.pods.append(_pending(op, "h100-reserved-1x"))
    for i in (1, 2):  # two earlier unschedulable switches on this root
        prior = {
            **job,
            "metadata": {
                **job["metadata"],
                "name": f"{op}-r{i}",
                "uid": f"uid-{op}-r{i}",
                "annotations": {
                    **job["metadata"]["annotations"],
                    f"{LABEL}/resumed-from": op,
                    f"{LABEL}/recovery": "unschedulable on x",
                    f"{LABEL}/successor": "z",
                },
            },
        }
        fake.jobs[(NS, f"{op}-r{i}")] = prior
    assert appmod.unschedulable_switch() == [] and not fences
    assert "2 unschedulable switches" in fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/unschedulable"]


def test_a_failed_create_restores_the_original(placed, monkeypatch):
    c, fake, op, job, fences = placed
    fake.pods.append(_pending(op, "h100-reserved-1x"))

    def boom(ns, body, region="eu-north1"):
        raise HTTPException(502, "kubernetes: create failed")

    monkeypatch.setattr(jobs, "create", boom)
    assert appmod.unschedulable_switch() == []
    orig = fake.jobs[(NS, op)]
    assert orig["spec"].get("suspend") is not True and not orig["metadata"]["annotations"].get(f"{LABEL}/successor")
    assert fences == []  # deactivated, then activated again
    assert (NS, f"{op}-r1") not in fake.jobs


def test_a_bind_between_snapshot_and_fence_stops_the_switch(placed, monkeypatch):
    """The pre-fence recheck: the pod binds after the first read (while the private input is delivered): nothing is
    fenced, nothing created, the run proceeds."""
    import execution

    c, fake, op, job, fences = placed
    p = _pending(op, "h100-reserved-1x")
    fake.pods.append(p)

    def bind(*a, **k):  # runs between the snapshot and the fence
        p["spec"]["nodeName"], p["status"]["phase"] = "node-9", "Running"

    monkeypatch.setattr(execution, "deliver", bind)
    assert appmod.unschedulable_switch() == []
    orig = fake.jobs[(NS, op)]
    assert orig["spec"].get("suspend") is not True and not orig["metadata"]["annotations"].get(f"{LABEL}/successor")
    assert fences == [] and (NS, f"{op}-r1") not in fake.jobs


def test_a_bind_in_the_fence_window_is_recorded_never_pretended_away(placed, monkeypatch):
    """The post-fence read: the pod bound in the last instants before the fence took effect; the switch records the
    evidence on the original and on the successor's reason (the attempt's records stay accepted)."""
    c, fake, op, job, fences = placed
    p = _pending(op, "h100-reserved-1x")
    fake.pods.append(p)
    real = kube.deactivate_workload

    def fence(ns, uid, region="eu-north1"):
        p["spec"]["nodeName"], p["status"]["phase"] = "node-9", "Running"
        p["status"]["containerStatuses"] = [
            {"name": "main", "state": {"running": {"startedAt": "2026-10-14T08:00:00Z"}}}
        ]
        return real(ns, uid, region)

    monkeypatch.setattr(kube, "deactivate_workload", fence)
    assert appmod.unschedulable_switch() == [f"{op}-r1"]
    orig = fake.jobs[(NS, op)]
    ev = json.loads(orig["metadata"]["annotations"][f"{LABEL}/started-before-fence"])
    assert (
        ev[0]["pod"] == p["metadata"]["name"]
        and ev[0]["node"] == "node-9"
        and ev[0]["started_at"] == "2026-10-14T08:00:00Z"
    )
    assert "had bound to node-9" in orig["metadata"]["annotations"][f"{LABEL}/recovery"]
    new = fake.jobs[(NS, f"{op}-r1")]
    assert (
        "had bound to node-9" in new["metadata"]["annotations"][f"{LABEL}/recovery"]
        and "records are retained" in new["metadata"]["annotations"][f"{LABEL}/recovery"]
    )
