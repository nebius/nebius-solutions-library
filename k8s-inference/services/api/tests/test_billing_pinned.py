"""Billing of a run pinned to its admitting cluster (a resume on the kept volume, jobs._pin_to_worker): the Job's region
label is the cluster id (`north`) while the rate snapshot it carries is keyed by the Nebius region (`eu-north1`). On
API54 both resumed members of a graph stayed accounting-active ("GPU attempt has no verified pool price; accounting
deferred") while their original siblings, labelled by region, settled. The lookup resolves the label through the
fleet document; nothing is guessed: no matching key still defers as unknown, never zero."""

import asyncio
import json

import pytest

import billing
import catalog
import kube
from test_api import L, admin_keys, client, cluster, fake_keys, pod  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_catalog():
    """The file catalog caches fleet-derived regions: a fleet with cluster ids must not leak into other modules."""
    catalog._cache.update({"mtime": None, "models": {}})
    yield
    catalog._cache.update({"mtime": None, "models": {}})


FLEET_WITH_IDS = {
    "clusters": {"north": {"region": "eu-north1"}, "south": {"region": "eu-south1"}},
    "pools": {
        "north-h100-spot-1x": {"region": "north", "pool": "h100-spot-1x", "gpu_class": "h100", "usd_per_gpu_hour": 2.0},
    },
}


def _resumed_job(name="op-3d77-s-gpu-1-r1", label="north", rates=None):
    """The real shape of a resumed stage member: pinned by cluster id, the snapshot keyed by region at submission."""
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "namespace": "tenant-demo",
            "labels": {f"{L}/region": label, f"{L}/model": "batch-example", f"{L}/mode": "run", f"{L}/tenant": "demo"},
            "annotations": {
                f"{L}/key": "deadbeef",
                f"{L}/resumed-from": "op-3d77-s-gpu-1",
                f"{L}/stage-root": "op-3d77",
                f"{L}/billing-rates": json.dumps(rates if rates is not None else {"eu-north1": {"h100-spot-1x": 2.0}}),
            },
        },
        "spec": {
            "template": {"spec": {"containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}
        },
        "status": {"conditions": [{"type": "Complete", "status": "True"}]},
    }


def _attempt(name):
    p = pod(name, "Succeeded", "2026-10-13T23:00:00Z", "2026-10-13T23:30:00Z", 0)
    p["spec"]["nodeSelector"] = {f"{L}/pool": "h100-spot-1x"}
    return p


def test_pinned_resumed_member_is_priced_through_the_cluster_id(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_WITH_IDS)
    job = _resumed_job()
    job["_pods"] = [_attempt(job["metadata"]["name"])]
    assert billing.cost_of(job, None) == (1800.0, 1.0)  # 0.5 h x 1 GPU x $2
    # a run placed by region keeps working as before
    by_region = _resumed_job(label="eu-north1")
    by_region["_pods"] = [_attempt(by_region["metadata"]["name"])]
    assert billing.cost_of(by_region, None) == (1800.0, 1.0)
    # an older snapshot keyed by the cluster id itself is still read
    old = _resumed_job(rates={"north": {"h100-spot-1x": 2.0}})
    old["_pods"] = [_attempt(old["metadata"]["name"])]
    assert billing.cost_of(old, None) == (1800.0, 1.0)
    # nothing matching: deferred as unknown, never a zero price
    unknown = _resumed_job(label="west", rates={"eu-north1": {"h100-spot-1x": 2.0}})
    unknown["_pods"] = [_attempt(unknown["metadata"]["name"])]
    with pytest.raises(ValueError, match="price"):
        billing.cost_of(unknown, None)
    assert billing._rates_for({"eu-north1": {"x": 1}}, "north", None) == {"x": 1}
    assert billing._rates_for({"eu-north1": {"x": 1}}, None, "eu-north1") == {"x": 1}
    assert billing._rates_for({"eu-north1": {"x": 1}}, "south", "eu-south1") == {}


def test_billing_pass_settles_the_pinned_resumed_member_once(client, admin_keys, fake_keys, monkeypatch):
    """bill_once on the real annotation shape: the resumed member is priced, its member released with the measured
    seconds, the billed annotation written; a second pass changes nothing (no recompute, no second release)."""
    _c, cluster = client  # the fake fleet (and its fake database: catalog lookups never reach PostgreSQL)
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_WITH_IDS)
    job = _resumed_job()
    cluster.jobs[("tenant-demo", job["metadata"]["name"])] = job
    cluster.pods.append(_attempt(job["metadata"]["name"]))
    monkeypatch.setattr(billing.artifacts, "attempt_records", lambda ns, op, region, prefix=None: [])
    monkeypatch.setattr(billing.attempts, "enabled", lambda: False)
    spent = []

    async def fake_add(token, usd, operation, gpu_s):
        spent.append((token, usd, gpu_s))
        return 1.0

    monkeypatch.setattr(billing, "add_spend", fake_add)
    monkeypatch.setattr(billing.execution, "sweep_copies", lambda ns, op: None)
    assert asyncio.run(billing.bill_once()) == 1
    ann = cluster.jobs[("tenant-demo", job["metadata"]["name"])]["metadata"]["annotations"]
    assert ann[f"{L}/billed"] == "1.0" and ann[f"{L}/gpu-seconds"] == "1800.0"
    assert spent == [("deadbeef", 1.0, 1800.0)]
    assert [x for x in fake_keys.calls if x[0] == "release"] == [("release", job["metadata"]["name"], 1800.0)]
    assert asyncio.run(billing.bill_once()) == 0  # billed: never again
    assert len([x for x in fake_keys.calls if x[0] == "release"]) == 1
