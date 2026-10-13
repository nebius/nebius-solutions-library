"""InfiniBand pools grow on demand (services/dispatcher/infiniband.py, docs/SCHEDULING.md): the floor of an InfiniBand
pool follows the largest multi-node run or endpoint that waits for or holds its nodes, through the Nebius CLI as the
region's identity, and returns to the fleet's `min_nodes` once the pool has been idle for RESTORE_AFTER_S."""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dispatcher as d  # noqa: E402
import infiniband as ib  # noqa: E402
import nebius_cli  # noqa: E402

# fleet-prices pools.yaml on the control cluster with the keys the platform stage adds (min_nodes, node_group_id)
POOLS = {
    "hub-h100-spot-1x": {
        "region": "hub",
        "pool": "h100-spot-1x",
        "gpu_class": "h100",
        "capacity": "spot",
        "usd_per_gpu_hour": 2.15,
        "gpus_per_node": 1,
        "min_nodes": 0,
        "max_nodes": 8,
        "interconnect": "none",
        "node_group_id": "ng-1x",
    },
    "hub-h100-ib-spot": {
        "region": "hub",
        "pool": "h100-ib-spot",
        "gpu_class": "h100",
        "capacity": "spot",
        "usd_per_gpu_hour": 2.15,
        "gpus_per_node": 8,
        "min_nodes": 0,
        "max_nodes": 4,
        "interconnect": "infiniband",
        "node_group_id": "ng-ib-spot",
    },
    "hub-h100-ib-reserved": {
        "region": "hub",
        "pool": "h100-ib-reserved",
        "gpu_class": "h100",
        "capacity": "reserved",
        "usd_per_gpu_hour": 0,
        "gpus_per_node": 8,
        "min_nodes": 1,
        "max_nodes": 2,
        "interconnect": "infiniband",
        "node_group_id": "ng-ib-res",
    },
    "hub-h100-ib-new": {
        "region": "hub",
        "pool": "h100-ib-new",
        "gpu_class": "h100",
        "capacity": "spot",
        "usd_per_gpu_hour": 2.15,
        "gpus_per_node": 8,
        "min_nodes": 0,
        "max_nodes": 4,
        "interconnect": "infiniband",
    },  # not applied by the cloud stage yet
}
CLUSTERS = d.clusters_from_pools(POOLS)
IB = ib.ib_pools(CLUSTERS["hub"])


def multinode_wl(name, nodes, pools, ib_required=True, assigned=None, finished=False, active=True):
    """A worker Workload of a multi-node run as services/api/jobs.py `_multinode` renders the JobSet's pod template."""
    terms = [{"key": "serverless2.nebius/pool", "operator": "In", "values": pools}]
    if ib_required:
        terms.append({"key": "serverless2.nebius/interconnect", "operator": "In", "values": ["infiniband"]})
    wl = {
        "metadata": {"name": name, "namespace": "tenant-demo", "ownerReferences": [{"kind": "JobSet", "name": name}]},
        "spec": {
            "active": active,
            "podSets": [
                {
                    "name": "workers",
                    "count": nodes,
                    "template": {
                        "spec": {
                            "affinity": {
                                "nodeAffinity": {
                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                        "nodeSelectorTerms": [{"matchExpressions": terms}]
                                    }
                                }
                            },
                            "containers": [{"name": "main", "resources": {"requests": {"nvidia.com/gpu": "8"}}}],
                        }
                    },
                }
            ],
        },
        "status": {"conditions": []},
    }
    if assigned:
        wl["status"]["admission"] = {
            "podSetAssignments": [{"name": "workers", "flavors": {"nvidia.com/gpu": assigned}}]
        }
        wl["status"]["conditions"].append({"type": "Admitted", "status": "True"})
    if finished:
        wl["status"]["conditions"].append({"type": "Finished", "status": "True"})
    return wl


def lws(name, size, pool, replicas=1):
    return {
        "metadata": {
            "name": name,
            "namespace": "models",
            "annotations": {"serverless2.nebius/nodes": str(size), "serverless2.nebius/interconnect": "required"},
        },
        "spec": {
            "replicas": replicas,
            "leaderWorkerTemplate": {
                "size": size,
                "leaderTemplate": {"spec": {"nodeSelector": {"serverless2.nebius/pool": pool}}},
            },
        },
    }


def test_ib_pools_are_the_infiniband_pools_with_a_node_group():
    assert set(IB) == {"h100-ib-spot", "h100-ib-reserved"}  # Ethernet pool and the pool without a node group left out


def test_workload_demand_pending_run_takes_the_pool_kueue_takes_first():
    wl = multinode_wl("op-1", 2, ["h100-ib-spot", "h100-ib-reserved"])
    assert ib.workload_demand(wl, IB) == {"h100-ib-reserved": 2}  # reserved before spot (capacity_order)
    wl = multinode_wl("op-2", 3, ["h100-ib-spot"])
    assert ib.workload_demand(wl, IB) == {"h100-ib-spot": 3}


def test_workload_demand_admitted_run_counts_on_its_assigned_flavor():
    wl = multinode_wl("op-1", 2, ["h100-ib-spot", "h100-ib-reserved"], assigned="h100-ib-spot")
    assert ib.workload_demand(wl, IB) == {"h100-ib-spot": 2}


def test_workload_demand_ignores_finished_deactivated_ethernet_and_single_node_runs():
    assert ib.workload_demand(multinode_wl("op-1", 2, ["h100-ib-spot"], finished=True), IB) == {}
    assert ib.workload_demand(multinode_wl("op-2", 2, ["h100-ib-spot"], active=False), IB) == {}
    assert (
        ib.workload_demand(multinode_wl("op-3", 2, ["h100-spot-1x", "h100-ib-spot"], ib_required=False), IB) == {}
    )  # interconnect: none / preferred, not placed yet
    assert ib.workload_demand(
        multinode_wl("op-4", 2, ["h100-spot-1x", "h100-ib-spot"], ib_required=False, assigned="h100-ib-spot"), IB
    ) == {"h100-ib-spot": 2}  # ... but counted once Kueue put it on the fabric
    single = {
        "metadata": {"name": "op-5", "namespace": "t"},
        "spec": {
            "podSets": [
                {
                    "name": "main",
                    "count": 1,
                    "template": {"spec": {"nodeSelector": {"serverless2.nebius/pool": "h100-spot-1x"}}},
                }
            ]
        },
        "status": {},
    }
    assert ib.workload_demand(single, IB) == {}


def test_lws_demand_counts_started_endpoints_only():
    assert ib.lws_demand(lws("kimi", 2, "h100-ib-spot"), IB) == {"h100-ib-spot": 2}
    assert ib.lws_demand(lws("kimi", 2, "h100-ib-spot", replicas=0), IB) == {}
    assert ib.lws_demand(lws("eth", 2, "h100-spot-1x"), IB) == {}


def test_demand_is_the_largest_single_need_per_pool_and_drops_what_cannot_fit():
    wls = [
        multinode_wl("a", 2, ["h100-ib-spot"]),
        multinode_wl("b", 3, ["h100-ib-spot"]),
        multinode_wl("c", 2, ["h100-ib-reserved"]),
        multinode_wl("huge", 5, ["h100-ib-spot"]),
    ]  # 5 > max_nodes 4: never fits
    assert ib.demand(wls, [lws("kimi", 4, "h100-ib-spot")], IB, "hub") == {"h100-ib-spot": 4, "h100-ib-reserved": 2}


def test_plan_raises_at_once_lowers_after_the_restore_delay_and_never_below_the_fleet_floor():
    state = {}
    assert ib.plan(IB, {"h100-ib-spot": 2}, state, 1000.0) == [("h100-ib-spot", 2, "demand 2 nodes")]
    state["h100-ib-spot"] = {"set": 2}  # the caller records a successful call
    assert ib.plan(IB, {"h100-ib-spot": 2}, state, 1015.0) == []  # idempotent
    assert ib.plan(IB, {"h100-ib-spot": 3}, state, 1030.0) == [("h100-ib-spot", 3, "demand 3 nodes")]
    state["h100-ib-spot"] = {"set": 3}
    assert ib.plan(IB, {}, state, 1045.0) == []  # demand gone: the cooldown starts
    assert state["h100-ib-spot"]["low_since"] == 1045.0
    assert ib.plan(IB, {}, state, 1045.0 + ib.RESTORE_AFTER_S - 1) == []
    assert ib.plan(IB, {"h100-ib-spot": 1}, state, 1045.0 + ib.RESTORE_AFTER_S) == [
        ("h100-ib-spot", 1, "demand down to 1 nodes")
    ]
    assert ib.plan(IB, {}, state, 1045.0 + ib.RESTORE_AFTER_S) == [
        ("h100-ib-spot", 0, "idle, back to the fleet's min_nodes")
    ]
    state["h100-ib-spot"] = {"set": 3, "low_since": 1045.0}
    assert (
        ib.plan(IB, {"h100-ib-spot": 3}, state, 1100.0) == [] and "low_since" not in state["h100-ib-spot"]
    )  # demand is back: cooldown cleared
    # the reserved pool's own min_nodes (1) is the floor: one node of demand changes nothing
    assert ib.plan(IB, {"h100-ib-reserved": 1}, {}, 0.0) == []
    assert ib.plan(IB, {"h100-ib-reserved": 2}, {}, 0.0) == [("h100-ib-reserved", 2, "demand 2 nodes")]


class FakeCore:
    """The state ConfigMap in kueue-system."""

    def __init__(self):
        self.cms = {}

    def read_namespaced_config_map(self, name, ns):
        if name not in self.cms:
            raise ib.client.exceptions.ApiException(status=404)
        return type("CM", (), {"data": self.cms[name]})()

    def patch_namespaced_config_map(self, name, ns, body):
        if name not in self.cms:
            raise ib.client.exceptions.ApiException(status=404)
        self.cms[name] = body["data"]

    def create_namespaced_config_map(self, ns, body):
        self.cms[body["metadata"]["name"]] = body["data"]


class FakeCustom:
    def __init__(self, workloads, lwss=None, lws_crd=True):
        self.workloads, self.lwss, self.lws_crd = workloads, lwss or [], lws_crd

    def list_cluster_custom_object(self, group, version, plural):
        if plural == "workloads":
            return {"items": self.workloads}
        if not self.lws_crd:
            raise ib.client.exceptions.ApiException(status=404)
        return {"items": self.lwss}


def _cli(monkeypatch, calls, fail=False):
    """The Nebius CLI: every call recorded; `fail` = the node-group update exits 1."""

    def run(cmd, **kw):
        calls.append(cmd)
        if fail and "update" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="permission denied")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(nebius_cli, "_profiles", {"eu-north1": "sa-eu-north1"})


def test_reconcile_raises_through_the_cli_as_the_region_identity_and_restores_when_idle(monkeypatch):
    calls, core = [], FakeCore()
    _cli(monkeypatch, calls)
    custom = FakeCustom([multinode_wl("op-1", 2, ["h100-ib-spot"])])
    workers, aliases = {"hub": {"custom": custom}}, {"eu-north1": "hub"}
    now = [1000.0]
    monkeypatch.setattr(ib.time, "time", lambda: now[0])
    ib.reconcile(core, workers, CLUSTERS, aliases)
    assert calls == [
        [
            "nebius",
            "--profile",
            "sa-eu-north1",
            "mk8s",
            "node-group",
            "update",
            "--id",
            "ng-ib-spot",
            "--patch",
            "--autoscaling-min-node-count",
            "2",
            "--format",
            "json",
        ]
    ]
    state = json.loads(core.cms[ib.STATE_CM]["state.json"])
    assert state["hub"]["h100-ib-spot"]["set"] == 2
    ib.reconcile(core, workers, CLUSTERS, aliases)  # nothing changed: no call
    assert len(calls) == 1
    custom.workloads = []  # the run finished; its Workload is gone
    now[0] += 30
    ib.reconcile(core, workers, CLUSTERS, aliases)
    assert (
        len(calls) == 1
        and json.loads(core.cms[ib.STATE_CM]["state.json"])["hub"]["h100-ib-spot"]["low_since"] == now[0]
    )
    now[0] += ib.RESTORE_AFTER_S
    ib.reconcile(core, workers, CLUSTERS, aliases)
    assert calls[-1][-3:] == ["0", "--format", "json"] and "ng-ib-spot" in calls[-1]
    assert json.loads(core.cms[ib.STATE_CM]["state.json"]) == {}  # idle: nothing remembered, terraform plan is clean


def test_reconcile_restarted_dispatcher_restores_from_the_state_configmap(monkeypatch):
    """A dispatcher that comes back finds the floor it set recorded and lowers it when nothing needs it any more."""
    calls, core = [], FakeCore()
    core.cms[ib.STATE_CM] = {"state.json": json.dumps({"hub": {"h100-ib-spot": {"set": 4, "low_since": 0.0}}})}
    _cli(monkeypatch, calls)
    monkeypatch.setattr(ib.time, "time", lambda: float(ib.RESTORE_AFTER_S + 1))
    ib.reconcile(core, {"hub": {"custom": FakeCustom([], lws_crd=False)}}, CLUSTERS, {"eu-north1": "hub"})
    assert [c for c in calls if "update" in c][0][-3:] == ["0", "--format", "json"]


def test_reconcile_endpoint_holds_the_floor_and_a_failed_call_is_retried(monkeypatch):
    calls, core = [], FakeCore()
    _cli(monkeypatch, calls, fail=True)
    workers = {"hub": {"custom": FakeCustom([], [lws("kimi", 2, "h100-ib-reserved")])}}
    ib.reconcile(core, workers, CLUSTERS, {"eu-north1": "hub"})
    assert len([c for c in calls if "update" in c]) == 1 and ib.STATE_CM not in core.cms  # failed: nothing recorded
    _cli(monkeypatch, calls)
    ib.reconcile(core, workers, CLUSTERS, {"eu-north1": "hub"})
    assert (
        len([c for c in calls if "update" in c]) == 2
        and json.loads(core.cms[ib.STATE_CM]["state.json"])["hub"]["h100-ib-reserved"]["set"] == 2
    )


def test_reconcile_without_credentials_for_the_region_changes_nothing(monkeypatch):
    calls, core = [], FakeCore()
    _cli(monkeypatch, calls)
    monkeypatch.setattr(nebius_cli, "_profiles", {})
    ib.reconcile(
        core,
        {"hub": {"custom": FakeCustom([multinode_wl("op-1", 2, ["h100-ib-spot"])])}},
        CLUSTERS,
        {"eu-north1": "hub"},
    )
    assert calls == [] and ib.STATE_CM not in core.cms


def test_login_creates_one_profile_per_mounted_key(monkeypatch, tmp_path):
    calls = []
    for r in ("eu-north1", "eu-west2"):
        (tmp_path / f"{r}.json").write_text("{}")
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
    )
    assert nebius_cli.login(str(tmp_path)) == {"eu-north1": "sa-eu-north1", "eu-west2": "sa-eu-west2"}
    assert calls[0][:4] == ["nebius", "profile", "create", "sa-eu-north1"] and calls[0][-1].endswith("eu-north1.json")
    assert (
        nebius_cli.profile("eu-west2") == "sa-eu-west2"
        and nebius_cli.profile() == "sa-eu-north1"
        and nebius_cli.profile("us-central1") is None
    )
