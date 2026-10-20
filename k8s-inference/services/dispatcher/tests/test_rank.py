import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dispatcher as d  # noqa: E402

# fleet-prices pools.yaml as charts/fleet renders it on the control cluster (keys "<region id>-<pool>")
POOLS = {
    "hub-h100-reserved": {
        "region": "hub",
        "pool": "h100-reserved",
        "gpu_class": "h100",
        "capacity": "reserved",
        "usd_per_gpu_hour": 0,
    },
    "hub-h100-spot": {
        "region": "hub",
        "pool": "h100-spot",
        "gpu_class": "h100",
        "capacity": "spot",
        "usd_per_gpu_hour": 2.15,
    },
    "hub-l40s-spot": {
        "region": "hub",
        "pool": "l40s-spot",
        "gpu_class": "l40s",
        "capacity": "spot",
        "usd_per_gpu_hour": 0.74,
    },
    "eu-south1-rtx6000-spot": {
        "region": "eu-south1",
        "pool": "rtx6000-spot",
        "gpu_class": "rtx-pro-6000",
        "capacity": "spot",
        "usd_per_gpu_hour": 0.95,
    },
}
CLUSTERS = d.clusters_from_pools(POOLS)
PROF = {"classes": ["l40s", "h100", "rtx-pro-6000"], "strategy": "preferred"}


def test_clusters_from_pools_groups_by_region():
    assert set(CLUSTERS) == {"hub", "eu-south1"}
    assert CLUSTERS["hub"]["pools"]["h100-spot"]["class"] == "h100"
    assert CLUSTERS["hub"]["pools"]["h100-spot"]["price"] == 2.15


def test_profile_from_queue_name():
    p = d.profile_from_name("prefer-h100", CLUSTERS, {})
    assert p == {"classes": ["h100", "l40s", "rtx-pro-6000"], "strategy": "preferred"}  # others by cheapest
    assert d.profile_from_name("default", CLUSTERS, {}) == {
        "classes": ["h100", "l40s", "rtx-pro-6000"],
        "strategy": "cheapest",
    }
    assert d.profile_from_name("prefer-unknown", CLUSTERS, {})["strategy"] == "cheapest"
    assert d.profile_from_name(None, CLUSTERS, {})["strategy"] == "cheapest"


def test_preferred_class_first_when_free():
    free = {("hub", "l40s-spot"): 1, ("hub", "h100-reserved"): 4, ("eu-south1", "rtx6000-spot"): 2}
    r = d.rank(PROF, CLUSTERS, free, 1, {})
    assert r[0][:2] == ("hub", "l40s-spot")


def test_fallback_class_when_preferred_busy_reserved_before_spot():
    free = {("hub", "h100-reserved"): 4, ("hub", "h100-spot"): 4, ("eu-south1", "rtx6000-spot"): 2}
    r = d.rank(PROF, CLUSTERS, free, 1, {})
    assert r[0][:2] == ("hub", "h100-reserved")
    assert r[1][:2] == ("hub", "h100-spot")


def test_cheapest_strategy_uses_price_only_and_live_spot_prices():
    free = {("hub", "h100-spot"): 4, ("eu-south1", "rtx6000-spot"): 2, ("hub", "l40s-spot"): 1}
    prof = d.profile_from_name("default", CLUSTERS, {"hub/l40s-spot": 1.50})
    r = d.rank(prof, CLUSTERS, free, 1, {"hub/l40s-spot": 1.50})
    assert r[0][:2] == ("eu-south1", "rtx6000-spot")  # 0.95 < live l40s 1.50 < h100 2.15


def test_nothing_free_nominates_best_allowed_to_queue_there():
    r = d.rank(PROF, CLUSTERS, {}, 1, {})
    assert r[0][3] is False and r[0][:2] == ("hub", "l40s-spot")


def test_region_pin_restricts_candidates():
    free = {("hub", "l40s-spot"): 1, ("eu-south1", "rtx6000-spot"): 0}
    r = d.rank(PROF, CLUSTERS, free, 1, {}, pin="eu-south1")
    assert {c[0] for c in r} == {"eu-south1"}


def test_multi_gpu_request_needs_enough_free_quota():
    free = {("hub", "h100-spot"): 2, ("hub", "h100-reserved"): 8}
    r = d.rank({"classes": ["h100"], "strategy": "preferred"}, CLUSTERS, free, 4, {})
    assert r[0][:2] == ("hub", "h100-reserved") and r[1][3] is False


def test_renomination_appends_next_cluster_after_timeout():
    ranked = [("hub", "l40s-spot", 0.74, False), ("eu-south1", "rtx6000-spot", 0.95, False)]
    assert d.nominations(ranked, [], 0) == ["hub"]
    assert d.nominations(ranked, ["hub"], d.RENOMINATE_AFTER_S - 1) == ["hub"]
    assert d.nominations(ranked, ["hub"], d.RENOMINATE_AFTER_S + 1) == ["hub", "eu-south1"]


def test_gpu_request_sums_pod_sets():
    wl = {
        "spec": {
            "podSets": [
                {
                    "count": 2,
                    "template": {"spec": {"containers": [{"resources": {"requests": {"nvidia.com/gpu": "2"}}}]}},
                }
            ]
        }
    }
    assert d.gpu_request(wl) == 4


def test_pending_multikueue_filters():
    base = {
        "status": {
            "conditions": [{"type": "QuotaReserved", "status": "True"}],
            "admissionChecks": [{"name": "multikueue", "state": "Pending"}],
        }
    }
    assert d.pending_multikueue(base)
    assert not d.pending_multikueue({"status": {**base["status"], "clusterName": "eu-south1"}})
    assert not d.pending_multikueue(
        {"status": {"conditions": [], "admissionChecks": base["status"]["admissionChecks"]}}
    )


REGISTRIES = {
    "hub": "cr.eu-north1.nebius.cloud/e00exampleexampleex",
    "eu-south1": "cr.eu-south1.nebius.cloud/e07exampleexampleex",
}
TEMPLATE = {
    "metadata": {
        "annotations": {
            "serverless2.nebius/gpu-classes": "h100,rtx-pro-6000",
            "serverless2.nebius/regions": "eu-north1,eu-south1",
            "serverless2.nebius/pvc-size-gi": "50",
        }
    },
    "spec": {
        "initContainers": [{"name": "fetch", "image": "registry.serverless2.local/nebius/serverless2/jobs:0.1.3"}],
        "containers": [
            {"name": "main", "image": "registry.serverless2.local/nebius/batch-example:1.0"},
            {"name": "uploader", "image": "registry.serverless2.local/nebius/serverless2/jobs:0.1.3"},
        ],
        "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": "op-1-work"}}],
    },
}


def test_allowed_classes_and_regions_restrict_candidates():
    free = {("hub", "l40s-spot"): 1, ("hub", "h100-spot"): 1, ("eu-south1", "rtx6000-spot"): 2}
    r = d.rank(PROF, CLUSTERS, free, 1, {}, allowed_classes=["h100", "rtx-pro-6000"])
    assert r[0][:2] == ("hub", "h100-spot") and all(
        c[1] != "l40s-spot" for c in r
    )  # l40s not allowed although preferred by the profile
    r = d.rank(PROF, CLUSTERS, free, 1, {}, allowed_classes=["h100", "rtx-pro-6000"], allowed_clusters=["eu-south1"])
    assert [c[0] for c in r] == ["eu-south1"]
    wl = {"spec": {"podSets": [{"template": TEMPLATE}]}}
    assert d.allowed_of(wl) == (["h100", "rtx-pro-6000"], ["eu-north1", "eu-south1"])
    assert d.allowed_of({"spec": {"podSets": [{"template": {"spec": {}}}]}}) == (None, None)


def test_work_volume_only_where_admitted_and_swept_elsewhere():
    """A nomination creates nothing (the run may move while it waits); the admitted cluster gets the volume;
    a volume whose Jobs were admitted in another cluster is swept at once, an orphan after the grace period."""
    wl = {
        "metadata": {"namespace": "t", "ownerReferences": [{"kind": "Job", "name": "op-1"}]},
        "spec": {"podSets": [{"template": TEMPLATE}]},
        "status": {"nominatedClusterNames": ["eu-south1", "hub"]},
    }
    assert d.admitted_volume(wl) is None
    # the remote copy (same name) is Admitted in one nominated worker: that is the admission (the manager's own
    # clusterName never turns under the external dispatcher)
    remote = {
        "eu-south1": {"status": {"conditions": [{"type": "QuotaReserved", "status": "True"}]}},
        "hub": {"status": {"conditions": [{"type": "Admitted", "status": "True"}]}},
    }
    assert d.admitted_cluster(wl, remote) == "hub" and d.admitted_cluster(wl, {}) is None
    assert d.admitted_volume(wl, d.admitted_cluster(wl, remote)) == ("hub", "t", "op-1-work", "50", "op-1")
    wl["status"]["clusterName"] = "hub"
    assert d.admitted_volume(wl) == ("hub", "t", "op-1-work", "50", "op-1")
    wl["status"]["conditions"] = [{"type": "Finished", "status": "True"}]
    assert d.admitted_volume(wl) is None
    assert d.admitted_cluster(wl) == "hub"
    del wl["status"]["clusterName"]  # Kueue clears it once the worker's copy is gone
    assert d.admitted_cluster(wl) is None
    wl["metadata"]["annotations"] = {d.ADMITTED_ANN: "hub"}  # ... the dispatcher's record remains
    assert d.admitted_cluster(wl) == "hub"
    admitted = {("t", "op-1"): "hub"}
    assert d.volume_action("eu-south1", {"op-1"}, set(), admitted, "t", age_s=0) == "elsewhere"
    assert d.volume_action("hub", {"op-1"}, set(), admitted, "t", age_s=0) is None
    assert d.volume_action("eu-south1", {"op-1"}, set(), {}, "t", age_s=0) is None  # not admitted yet: keep
    assert (
        d.volume_action("hub", {"op-1", "op-1-r1"}, set(), {("t", "op-1"): "eu-south1"}, "t", age_s=0) is None
    )  # a resume still pending here
    assert d.volume_action("hub", set(), set(), {}, "t", age_s=d.ORPHAN_AFTER_S + 1) == "orphan"
    assert d.volume_action("hub", set(), set(), {}, "t", age_s=1) is None
    assert d.pvc_of(TEMPLATE) == "op-1-work" and d.pvc_of({"spec": {}}) is None


# ---- closed recovery chains: scratch retention (docs/SCHEDULING.md "Automatic recovery", docs/JOBS.md "Work volume") ----
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402


def mjob(name: str, claim: str | None, successor: str | None = None, finished: bool = False):
    """A manager Job as the kubernetes client returns it: a pod template that names its work claim, the `successor`
    annotation when the platform superseded it (suspended, Workload deactivated: never Finished)."""
    ann = {f"{d.LABEL}/successor": successor} if successor else {}
    vols = (
        [NS(name="work", persistent_volume_claim=NS(claim_name=claim))]
        if claim
        else [NS(name="shm", persistent_volume_claim=None)]
    )
    return NS(
        metadata=NS(name=name, annotations=ann),
        spec=NS(template=NS(spec=NS(volumes=vols)), suspend=bool(successor)),
        status=NS(succeeded=1 if finished else 0),
    )


class FakeBatch:
    def __init__(self, jobs: dict | None):
        self.jobs, self.lists = jobs, 0

    def list_namespaced_job(self, ns):
        self.lists += 1
        if self.jobs is None:
            raise RuntimeError("manager unreachable")
        return NS(items=list(self.jobs.values()))


class FakeCore:
    def __init__(self, pvcs: dict, age_s: float = 0):
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        self.items = [
            NS(metadata=NS(namespace="t", name=n, annotations={d.PVC_JOBS_ANN: a}, creation_timestamp=ts))
            for n, a in pvcs.items()
        ]
        self.deleted = []

    def list_persistent_volume_claim_for_all_namespaces(self, label_selector=None):
        return NS(items=self.items)

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        self.deleted.append(name)


def test_superseded_root_releases_scratch_once_the_successor_has_its_own_volume():
    """The observed defect: a recovery original is suspended with a `successor` annotation, its Workload deactivated,
    never Finished. Its old scratch is released (the successor restores a portable checkpoint onto a fresh volume and
    never reads it), the readable handle stays, and the successor's own volume is kept while its Job exists."""
    idx = {
        "op-1": mjob("op-1", "op-1-work", successor="op-1-r-ab12cd34"),
        "op-1-r-ab12cd34": mjob("op-1-r-ab12cd34", "op-1-r-ab12cd34-work", finished=True),
    }
    users, sup = d.claim_references(["op-1"], idx, "op-1-work")
    assert (users, sup) == (set(), {"op-1"})
    assert (
        d.volume_action("eu-south1", users, sup, {("t", "op-1"): "eu-south1"}, "t", age_s=1) == "superseded"
    )  # no grace period: the fence is the proof
    users, sup = d.claim_references(["op-1-r-ab12cd34"], idx, "op-1-r-ab12cd34-work")
    assert (users, sup) == ({"op-1-r-ab12cd34"}, set())
    assert (
        d.volume_action("hub", users, sup, {("t", "op-1-r-ab12cd34"): "hub"}, "t", age_s=10**6) is None
    )  # a completed successor keeps its volume until its handle goes (uploader/TTL)
    # the volume pass never recreates the superseded original's claim; an unknown manager keeps the old behaviour
    assert d.is_superseded(idx["op-1"]) and not d.is_superseded(idx["op-1-r-ab12cd34"]) and not d.is_superseded(None)
    cache = {}
    assert (
        d.manager_jobs(FakeBatch(idx), "t", cache) == idx and d.manager_jobs(FakeBatch(None), "t", cache) == idx
    )  # one list per reconcile
    assert d.manager_jobs(FakeBatch(None), "u", cache) is None


def test_multi_link_recovery_chain_releases_every_closed_link():
    """root -> r-a -> r-b, each link recovered onto a fresh volume: the two superseded links' volumes go, the running
    link's stays; the handles are not deleted."""
    idx = {
        "op-1": mjob("op-1", "op-1-work", successor="op-1-r-aaaaaaaa"),
        "op-1-r-aaaaaaaa": mjob("op-1-r-aaaaaaaa", "op-1-r-aaaaaaaa-work", successor="op-1-r-bbbbbbbb"),
        "op-1-r-bbbbbbbb": mjob("op-1-r-bbbbbbbb", "op-1-r-bbbbbbbb-work"),
    }
    admitted = {("t", "op-1"): "eu-south1", ("t", "op-1-r-aaaaaaaa"): "hub", ("t", "op-1-r-bbbbbbbb"): "hub"}
    core = FakeCore(
        {"op-1-work": "op-1", "op-1-r-aaaaaaaa-work": "op-1-r-aaaaaaaa", "op-1-r-bbbbbbbb-work": "op-1-r-bbbbbbbb"}
    )
    batch = FakeBatch(idx)
    d.sweep_volumes({"hub": {"core": core}}, batch, admitted)
    assert sorted(core.deleted) == ["op-1-r-aaaaaaaa-work", "op-1-work"]
    assert batch.lists == 1 and set(idx) == {"op-1", "op-1-r-aaaaaaaa", "op-1-r-bbbbbbbb"}  # handles untouched


def test_same_volume_resume_or_finalizer_not_yet_recorded_keeps_the_claim():
    """A `-r<n>` resume or `-x<n>` finalizer the API created on the original's volume is a reference from the moment
    the Job exists, before the admitted-volume pass records it on the claim: the sweeper reads the manager's Jobs,
    not the record alone (also when the root itself was superseded: nothing that still names the claim loses it)."""
    idx = {"op-1": mjob("op-1", "op-1-work", successor="op-1-r-ab12cd34"), "op-1-x1": mjob("op-1-x1", "op-1-work")}
    users, sup = d.claim_references(["op-1"], idx, "op-1-work")
    assert (users, sup) == ({"op-1-x1"}, {"op-1"})
    assert (
        d.volume_action("eu-south1", users, sup, {("t", "op-1"): "eu-south1"}, "t", age_s=1) is None
    )  # not admitted yet: keep
    assert (
        d.volume_action(
            "eu-south1", users, sup, {("t", "op-1"): "eu-south1", ("t", "op-1-x1"): "eu-south1"}, "t", age_s=1
        )
        is None
    )
    idx["op-1-r1"] = mjob("op-1-r1", "op-1-work")
    core = FakeCore({"op-1-work": "op-1"})
    d.sweep_volumes({"eu-south1": {"core": core}}, FakeBatch(idx), {("t", "op-1"): "eu-south1"})
    assert core.deleted == []
    del idx["op-1-x1"], idx["op-1-r1"]  # both gone (TTL): the superseded root alone
    d.sweep_volumes({"eu-south1": {"core": core}}, FakeBatch(idx), {("t", "op-1"): "eu-south1"})
    assert core.deleted == ["op-1-work"]


def test_failed_delivery_and_failed_runs_keep_their_volume():
    """A FAILED run with a 'delivery pending' exit code, an EXPORT_FAILED run, a cancelled run: finished, not
    superseded, their volume is what `:export`/`:resume` work on; kept whatever its age, until the handle goes."""
    idx = {"op-1": mjob("op-1", "op-1-work", finished=True)}
    users, sup = d.claim_references(["op-1"], idx, "op-1-work")
    assert (users, sup) == ({"op-1"}, set())
    assert d.volume_action("eu-south1", users, sup, {("t", "op-1"): "eu-south1"}, "t", age_s=10**7) is None
    # a chain whose first link failed on this volume and whose same-volume resume was then recovered elsewhere: the
    # FAILED root still names the claim and is resumable on it; the volume stays until that handle goes
    idx["op-1-r1"] = mjob("op-1-r1", "op-1-work", successor="op-1-r-ab12cd34")
    users, sup = d.claim_references(["op-1", "op-1-r1"], idx, "op-1-work")
    assert (users, sup) == ({"op-1"}, {"op-1-r1"})
    assert (
        d.volume_action(
            "eu-south1", users, sup, {("t", "op-1"): "eu-south1", ("t", "op-1-r1"): "eu-south1"}, "t", age_s=10**7
        )
        is None
    )
    del idx["op-1"]
    users, sup = d.claim_references(["op-1", "op-1-r1"], idx, "op-1-work")
    assert d.volume_action("eu-south1", users, sup, {("t", "op-1-r1"): "eu-south1"}, "t", age_s=1) == "superseded"


def test_sweeper_preserves_on_unknown_manager_and_ignores_foreign_claims():
    """The manager's Jobs cannot be listed: nothing is deleted. A Job that names another claim is no reference."""
    core = FakeCore({"op-1-work": "op-1"}, age_s=d.ORPHAN_AFTER_S + 1)
    d.sweep_volumes({"hub": {"core": core}}, FakeBatch(None), {})
    assert core.deleted == []
    idx = {"op-2": mjob("op-2", "op-2-work"), "op-3": mjob("op-3", None)}
    assert d.claim_references(["op-1"], idx, "op-1-work") == (set(), set())
    d.sweep_volumes({"hub": {"core": core}}, FakeBatch(idx), {})
    assert core.deleted == ["op-1-work"]  # orphan after the grace period, as before


def test_only_the_work_volume_is_the_dispatchers():
    """A multi-node run with `checkpoints: shared` mounts the tenant's `scratch-shared` claim next to an emptyDir
    `work`: the dispatcher must neither adopt nor sweep it (it deleted the tenant's claim as an "orphan" on a test fleet,
    2026-10-09). Only the pod's `work` volume names the dispatcher's claim."""
    shared = {
        "spec": {
            "volumes": [
                {"name": "work", "emptyDir": {"sizeLimit": "500Gi"}},
                {"name": "shm", "emptyDir": {"medium": "Memory"}},
                {"name": "checkpoints", "persistentVolumeClaim": {"claimName": "scratch-shared"}},
            ]
        }
    }
    assert d.pvc_of(shared) is None
    assert d.pvc_of(TEMPLATE) == "op-1-work"
    wl = {
        "metadata": {"namespace": "t", "ownerReferences": [{"kind": "JobSet", "name": "op-2"}]},
        "spec": {"podSets": [{"template": shared}]},
        "status": {"clusterName": "hub"},
    }
    assert d.admitted_volume(wl) is None


def test_nominate_uses_server_side_apply_as_kueue_admission():
    from unittest.mock import MagicMock

    custom = MagicMock()
    d.nominate(custom, "tenant-eval", "job-x", ["eu-south1"])
    args, kwargs = custom.patch_namespaced_custom_object_status.call_args
    assert args[:5] == (d.GROUP, d.VERSION, "tenant-eval", "workloads", "job-x")
    body = args[5]
    assert body["apiVersion"] == f"{d.GROUP}/{d.VERSION}" and body["kind"] == "Workload"
    assert body["metadata"] == {"name": "job-x", "namespace": "tenant-eval"}
    assert body["status"] == {"nominatedClusterNames": ["eu-south1"]}
    assert kwargs == {
        "field_manager": "kueue-admission",
        "force": True,
        "_content_type": "application/apply-patch+yaml",
    }


def test_nominate_restates_kueue_admission_owned_status():
    from unittest.mock import MagicMock

    custom = MagicMock()
    status = {
        "admission": {
            "clusterQueue": "prefer-h100",
            "podSetAssignments": [{"name": "main", "flavors": {"nvidia.com/gpu": "hub-h100-spot-1x"}}],
        },
        "admissionChecks": [
            {
                "name": "multikueue-prefer-h100",
                "state": "Pending",
                "message": "Reset to Pending after eviction. Previously: Retry",
                "retryCount": 1,
            }
        ],
        "conditions": [{"type": "QuotaReserved", "status": "True"}],
        "nominatedClusterNames": [],
    }
    d.nominate(custom, "tenant-eval", "job-x", ["hub"], status)
    body = custom.patch_namespaced_custom_object_status.call_args[0][5]
    assert body["status"]["nominatedClusterNames"] == ["hub"]
    assert body["status"]["admission"] == status["admission"]  # quota reservation kept
    assert body["status"]["admissionChecks"] == status["admissionChecks"]  # check states kept (state/message required)
    assert "conditions" not in body["status"]  # nothing else is claimed


def test_owner_job_accepts_jobsets_of_multinode_runs():
    assert d.owner_job({"metadata": {"ownerReferences": [{"kind": "JobSet", "name": "op-mn"}]}}) == "op-mn"
    assert d.owner_job({"metadata": {"ownerReferences": [{"kind": "Deployment", "name": "x"}]}}) is None


def test_spot_pool_is_a_candidate_at_once_after_on_demand_of_its_class():
    # capacity-first: inside one class reserved, then on-demand, then spot; a spot pool needs no waiting period
    pools = dict(POOLS)
    pools["hub-h100-ondemand"] = {
        "region": "hub",
        "pool": "h100-ondemand",
        "gpu_class": "h100",
        "capacity": "on_demand",
        "usd_per_gpu_hour": 4.5,
    }
    clusters = d.clusters_from_pools(pools)
    free = {("hub", "h100-reserved"): 4, ("hub", "h100-ondemand"): 4, ("hub", "h100-spot"): 4}
    r = d.rank({"classes": ["h100"], "strategy": "preferred"}, clusters, free, 1, {"hub/h100-spot": 0.79})
    assert [x[1] for x in r] == ["h100-reserved", "h100-ondemand", "h100-spot"]
    assert r[2][2] == 0.79  # the live quote, not the 2.15 list price
    # the spot pool wins as soon as the others are full
    r = d.rank(
        {"classes": ["h100"], "strategy": "preferred"}, clusters, {("hub", "h100-spot"): 4}, 1, {"hub/h100-spot": 0.79}
    )
    assert r[0][1] == "h100-spot" and r[0][3] is True


def test_price_feed_quotes_spot_as_follow_price_not_priority(monkeypatch):
    import price_feed

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            stdout = '{"hourly_cost": {"general": {"total": {"cost": "7.92"}}}}'

        return R()

    monkeypatch.setattr(price_feed.nebius_cli.subprocess, "run", fake_run)  # the CLI runs through nebius_cli.run
    monkeypatch.setattr(price_feed.nebius_cli, "_profiles", {"eu-north1": "sa-eu-north1"})
    assert price_feed.estimate("project-x", "gpu-b300-sxm", "8gpu-192vcpu-2768gb", True) == 7.92
    cmd = seen["cmd"]
    assert cmd[:3] == ["nebius", "--profile", "sa-eu-north1"]  # as the mounted region identity
    assert "--resource-spec-compute-instance-spec-follows-spot-price" in cmd
    assert cmd[cmd.index("--resource-spec-compute-instance-spec-preemptible-on-preemption") + 1] == "STOP"
    assert not any("priority" in a for a in cmd)  # deprecated since 2026-05-11, the CLI rejects it
    price_feed.estimate("project-x", "gpu-b300-sxm", "8gpu-192vcpu-2768gb", False)
    assert not any("preemptible" in a or "spot" in a for a in seen["cmd"])


def test_a_small_run_does_not_boot_a_whole_node():
    """A 1-GPU run prefers the 1-GPU pool (2.15 capped) over the 8-GPU pool following the spot price (0.79): with
    nothing up, the 8-GPU pool would boot a whole node (8 x 0.79 = 6.32/h). Once such a node is up with free
    GPUs, its per-GPU price counts and it wins."""
    pools = {
        "hub-h100-spot-1x": {
            "region": "hub",
            "pool": "h100-spot-1x",
            "gpu_class": "h100",
            "capacity": "spot",
            "usd_per_gpu_hour": 2.15,
            "gpus_per_node": 1,
        },
        "hub-h100-spot-8x": {
            "region": "hub",
            "pool": "h100-spot-8x",
            "gpu_class": "h100",
            "capacity": "spot",
            "usd_per_gpu_hour": 0.79,
            "gpus_per_node": 8,
        },
    }
    cl = d.clusters_from_pools(pools)
    prof = {"classes": ["h100"], "strategy": "preferred"}
    assert d.rank(prof, cl, {}, 1, {})[0][1] == "h100-spot-1x"
    assert d.rank({**prof, "strategy": "cheapest"}, cl, {}, 1, {})[0][1] == "h100-spot-1x"
    assert d.rank(prof, cl, {("hub", "h100-spot-8x"): 7}, 1, {})[0][1] == "h100-spot-8x"  # a node is up with room
    assert d.rank(prof, cl, {}, 8, {})[0][1] == "h100-spot-8x"  # a whole-node run


def test_tenant_profiles_and_dedicated_pools():
    """charts/fleet names a tenant's queues <tenant>-prefer-<class>; a pool dedicated to one tenant is a candidate for
    that tenant only (fleet tenants.<name>.dedicated_pools)."""
    clusters = {
        "hub": {
            "pools": {
                "h100-spot-1x": {"class": "h100", "capacity": "spot", "price": 0.8, "tenant": ""},
                "h100-lab-8x": {"class": "h100", "capacity": "reserved", "price": 0.0, "tenant": "lab"},
            }
        }
    }
    free = {("hub", "h100-spot-1x"): 2, ("hub", "h100-lab-8x"): 8}
    prof = d.profile_from_name("lab-prefer-h100", clusters, {}, tenant="lab")
    assert prof == d.profile_from_name("prefer-h100", clusters, {}) and prof["strategy"] == "preferred"
    assert (
        d.profile_from_name("lab-prefer-h100", clusters, {})["strategy"] == "cheapest"
    )  # no tenant known: not a profile
    lab = d.rank(prof, clusters, free, 1, {}, tenant="lab")
    assert lab[0][1] == "h100-lab-8x"  # its reservation first
    other = d.rank(prof, clusters, free, 1, {}, tenant="demo")
    assert [c[1] for c in other] == ["h100-spot-1x"]  # never the lab's pool
    assert [c[1] for c in d.rank(prof, clusters, free, 1, {})] == ["h100-spot-1x"]  # no tenant known: shared pools only


def test_free_quota_counts_every_queue_of_the_cohort():
    """The fleet's `default` queue holds the nominal quota; the tenants' queues borrow it, so their usage is what empties it."""

    class Custom:
        def list_cluster_custom_object(self, g, v, plural):
            return {
                "items": [
                    {
                        "metadata": {"name": "default"},
                        "spec": {
                            "resourceGroups": [
                                {
                                    "flavors": [
                                        {
                                            "name": "h100-spot-1x",
                                            "resources": [{"name": "nvidia.com/gpu", "nominalQuota": 8}],
                                        }
                                    ]
                                }
                            ]
                        },
                        "status": {"flavorsUsage": []},
                    },
                    {
                        "metadata": {"name": "demo-prefer-h100"},
                        "spec": {
                            "resourceGroups": [
                                {
                                    "flavors": [
                                        {
                                            "name": "h100-spot-1x",
                                            "resources": [
                                                {"name": "nvidia.com/gpu", "nominalQuota": 0, "borrowingLimit": 8}
                                            ],
                                        }
                                    ]
                                }
                            ]
                        },
                        "status": {
                            "flavorsUsage": [
                                {"name": "h100-spot-1x", "resources": [{"name": "nvidia.com/gpu", "total": "3"}]}
                            ]
                        },
                    },
                    {
                        "metadata": {"name": "lab-default"},
                        "spec": {
                            "resourceGroups": [
                                {
                                    "flavors": [
                                        {
                                            "name": "h100-spot-1x",
                                            "resources": [
                                                {"name": "nvidia.com/gpu", "nominalQuota": 0, "borrowingLimit": 8}
                                            ],
                                        }
                                    ]
                                }
                            ]
                        },
                        "status": {
                            "flavorsUsage": [
                                {"name": "h100-spot-1x", "resources": [{"name": "nvidia.com/gpu", "total": "2"}]}
                            ]
                        },
                    },
                ]
            }

    assert d.free_quota({"hub": {"custom": Custom()}}) == {("hub", "h100-spot-1x"): 3}
