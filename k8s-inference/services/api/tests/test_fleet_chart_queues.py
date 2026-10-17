"""charts/fleet renders, per cluster, the fleet's lender queue and one ClusterQueue per tenant and profile (docs/FLEET.md
"Tenants"): shared pools borrowed under the fair-share weight and the gpu_quota cap, a dedicated pool as its tenant's own
nominal quota that nobody else lists, the manager's queues carrying the MultiKueue admission check."""

import copy
import os
import subprocess

import pytest
import yaml

CHART = os.path.join(os.path.dirname(__file__), "..", "..", "..", "charts", "fleet")
FLEET = {
    "name": "t",
    "capacity_order": ["reserved", "on_demand", "spot"],
    "node_reserve": {"cpu": 2, "memory_gib": 20},
    "images": {
        "host": "images.example.invalid",
        "source": "x",
        "cache": {"size_gib": 10, "node_port": 30500},
        "upstreams": {},
    },
    "control": {"id": "control", "region": "eu-north1"},
    "regions": {
        "eu-north1": {
            "id": "hub",
            "project": "p",
            "pools": {
                "h100-spot-1x": {
                    "platform": "gpu-h100-sxm",
                    "preset": "1gpu-16vcpu-200gb",
                    "gpu_class": "h100",
                    "capacity": {"type": "spot"},
                    "min_nodes": 0,
                    "max_nodes": 4,
                },
                "h100-lab-8x": {
                    "platform": "gpu-h100-sxm",
                    "preset": "8gpu-128vcpu-1600gb",
                    "gpu_class": "h100",
                    "capacity": {"type": "reserved"},
                    "min_nodes": 1,
                    "max_nodes": 1,
                },
            },
        }
    },
    "prices": {"gpu-h100-sxm": {"on_demand": 4.5, "spot": 0.79}},
    "tenants": {
        "demo": {"gpu_quota": 2, "fair_share_weight": 1, "dedicated_pools": []},
        "lab": {"gpu_quota": None, "fair_share_weight": 3, "dedicated_pools": ["h100-lab-8x"]},
    },
}


def _render(cluster: str) -> dict:
    if subprocess.run(["helm", "version"], capture_output=True).returncode:
        pytest.skip("helm is not available here")
    values = os.path.join(os.path.dirname(__file__), f"_fleet_{cluster}.yaml")
    with open(values, "w") as f:
        yaml.safe_dump({"cluster": cluster, "fleet": FLEET}, f)
    try:
        r = subprocess.run(
            ["helm", "template", "fleet", CHART, "-f", values], capture_output=True, text=True, timeout=60
        )
    finally:
        os.unlink(values)
    assert r.returncode == 0, r.stderr
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(r.stdout) if d}


def _flavors(cq: dict) -> dict:
    return {fl["name"]: {r["name"]: r for r in fl["resources"]} for fl in cq["spec"]["resourceGroups"][0]["flavors"]}


def test_worker_queues_lender_tenants_and_dedicated_pool():
    docs = _render("hub")
    lender = _flavors(docs[("ClusterQueue", "default")])
    assert set(lender) == {"system", "h100-spot-1x"}  # the lab's pool is not the fleet's to lend
    # CPU-only admission (docs/FLEET.md "CPU-only runs"): the system flavor first, GPU-free, with the system nodes' CPU
    assert (
        list(lender) == ["system", "h100-spot-1x"]
        and lender["system"]["nvidia.com/gpu"]["nominalQuota"] == 0
        and lender["system"]["cpu"]["nominalQuota"] > 0
    )
    assert docs[("ResourceFlavor", "system")]["spec"] == {
        "nodeLabels": {
            "serverless2.nebius/pool": "system",
            "serverless2.nebius/shared-fs": "false",
        }  # no mount_on_system
    }
    assert lender["h100-spot-1x"]["nvidia.com/gpu"]["nominalQuota"] == 4
    assert docs[("ClusterQueue", "default")]["spec"]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "models"}
    }
    demo = docs[("ClusterQueue", "demo-prefer-h100")]
    assert demo["spec"]["fairSharing"] == {"weight": "1"} and "borrowWithinCohort" not in demo["spec"]["preemption"]
    assert demo["spec"]["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "tenant-demo"}}
    f = _flavors(demo)
    assert list(f) == ["system", "h100-spot-1x"] and f["h100-spot-1x"]["nvidia.com/gpu"] == {
        "name": "nvidia.com/gpu",
        "nominalQuota": 0,
        "borrowingLimit": 2,
    }
    assert (
        f["system"]["nvidia.com/gpu"] == {"name": "nvidia.com/gpu", "nominalQuota": 0, "borrowingLimit": 0}
        and f["system"]["cpu"]["borrowingLimit"] > 0
    )
    lab = _flavors(docs[("ClusterQueue", "lab-default")])
    assert list(lab) == [
        "system",
        "h100-lab-8x",
        "h100-spot-1x",
    ]  # CPU nodes first, then its reservation, then the shared spot pool
    assert lab["h100-lab-8x"]["nvidia.com/gpu"] == {"name": "nvidia.com/gpu", "nominalQuota": 8, "lendingLimit": 0}
    assert lab["h100-spot-1x"]["nvidia.com/gpu"]["borrowingLimit"] == 4  # no gpu_quota: up to the pool
    assert docs[("ClusterQueue", "lab-default")]["spec"]["fairSharing"] == {"weight": "3"}
    assert ("ClusterQueue", "prefer-h100") not in docs  # the old shared profile queues are gone


def test_manager_queues_carry_the_admission_check():
    docs = _render("control")
    q = docs[("ClusterQueue", "demo-prefer-h100")]
    assert q["spec"]["admissionChecksStrategy"] == {"admissionChecks": [{"name": "multikueue-prefer-h100"}]}
    assert list(_flavors(q)) == ["hub-system", "hub-h100-spot-1x"]  # the logical system flavor of every region first
    assert (
        docs[("ResourceFlavor", "hub-system")]["spec"] == {}
        and _flavors(q)["hub-system"]["nvidia.com/gpu"]["borrowingLimit"] == 0
    )
    assert set(_flavors(docs[("ClusterQueue", "lab-prefer-h100")])) == {
        "hub-system",
        "hub-h100-lab-8x",
        "hub-h100-spot-1x",
    }
    assert docs[("ResourceFlavor", "hub-h100-lab-8x")]["metadata"]["labels"]["serverless2.nebius/tenant"] == "lab"


def test_tenant_queues_drop_the_system_flavor_when_the_policy_keeps_tenants_off_the_system_nodes(monkeypatch):
    """docs/FLEET.md "Customer CPU pools": with `tenant_system_flavor: false` no tenant queue lists a system flavor
    (worker `system`, manager `<region>-system`); the lender keeps it for the platform's own GPU-free pods; a region's
    customer CPU pool is the first flavor of every tenant queue; the fleet document carries the policy and the pools."""
    original = FLEET
    fleet = copy.deepcopy(FLEET)
    fleet["tenant_system_flavor"] = False
    fleet["regions"]["eu-north1"]["cpu_pools"] = {
        "cpu-general": {"platform": "cpu-d3", "preset": "8vcpu-32gb", "min_nodes": 1, "max_nodes": 2},
        # by name before cpu-general, by size after it: the flavor order is by size (charts/fleet 0.1.14)
        "cpu-batch-32": {"platform": "cpu-d3", "preset": "32vcpu-128gb", "min_nodes": 0, "max_nodes": 1},
    }
    fleet["regions"]["eu-north1"]["system_pool"] = {"platform": "cpu-d3", "preset": "8vcpu-32gb", "node_count": 2}
    monkeypatch.setitem(globals(), "FLEET", fleet)
    worker = _render("hub")
    assert list(_flavors(worker[("ClusterQueue", "default")])) == [
        "system",
        "cpu-general",
        "cpu-batch-32",
        "h100-spot-1x",
    ]
    assert list(_flavors(worker[("ClusterQueue", "demo-prefer-h100")])) == [
        "cpu-general",
        "cpu-batch-32",
        "h100-spot-1x",
    ]
    assert list(_flavors(worker[("ClusterQueue", "lab-default")])) == [
        "cpu-general",
        "cpu-batch-32",
        "h100-lab-8x",
        "h100-spot-1x",
    ]
    manager = _render("control")
    assert list(_flavors(manager[("ClusterQueue", "demo-prefer-h100")])) == [
        "hub-cpu-general",
        "hub-cpu-batch-32",
        "hub-h100-spot-1x",
    ]
    assert list(_flavors(manager[("ClusterQueue", "default")]))[0] == "hub-system"
    prices = manager[("ConfigMap", "fleet-prices")]["data"]
    assert yaml.safe_load(prices["policy.yaml"]) == {"tenant_system_flavor": False}
    pools = yaml.safe_load(prices["cpu_pools.yaml"])
    assert pools["hub"]["system"]["kind"] == "system" and pools["hub"]["system"]["platform"] == "cpu-d3"
    assert pools["hub"]["cpu-general"] == {
        "kind": "cpu",
        "platform": "cpu-d3",
        "preset": "8vcpu-32gb",
        "min_nodes": 1,
        "max_nodes": 2,
    }
    assert "cluster_id" not in yaml.safe_load(prices["clusters.yaml"])["hub"]  # not applied by the cloud stage yet
    # the default (the key absent, or true): every queue keeps the system flavor first, as before
    monkeypatch.setitem(globals(), "FLEET", original)
    assert list(_flavors(_render("hub")[("ClusterQueue", "demo-prefer-h100")]))[0] == "system"
    assert yaml.safe_load(_render("hub")[("ConfigMap", "fleet-prices")]["data"]["policy.yaml"]) == {
        "tenant_system_flavor": True
    }
