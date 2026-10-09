import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dispatcher as d  # noqa: E402

# fleet-prices pools.yaml as charts/fleet renders it on the control cluster (keys "<region id>-<pool>")
POOLS = {
    "hub-h100-reserved": {"region": "hub", "pool": "h100-reserved", "gpu_class": "h100", "capacity": "reserved", "usd_per_gpu_hour": 0},
    "hub-h100-spot": {"region": "hub", "pool": "h100-spot", "gpu_class": "h100", "capacity": "spot", "usd_per_gpu_hour": 2.15},
    "hub-l40s-spot": {"region": "hub", "pool": "l40s-spot", "gpu_class": "l40s", "capacity": "spot", "usd_per_gpu_hour": 0.74},
    "eu-south1-rtx6000-spot": {"region": "eu-south1", "pool": "rtx6000-spot", "gpu_class": "rtx-pro-6000", "capacity": "spot", "usd_per_gpu_hour": 0.95},
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
    assert d.profile_from_name("default", CLUSTERS, {}) == {"classes": ["h100", "l40s", "rtx-pro-6000"], "strategy": "cheapest"}
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
    wl = {"spec": {"podSets": [{"count": 2, "template": {"spec": {"containers": [
        {"resources": {"requests": {"nvidia.com/gpu": "2"}}}]}}}]}}
    assert d.gpu_request(wl) == 4


def test_pending_multikueue_filters():
    base = {"status": {"conditions": [{"type": "QuotaReserved", "status": "True"}],
                       "admissionChecks": [{"name": "multikueue", "state": "Pending"}]}}
    assert d.pending_multikueue(base)
    assert not d.pending_multikueue({"status": {**base["status"], "clusterName": "eu-south1"}})
    assert not d.pending_multikueue({"status": {"conditions": [], "admissionChecks": base["status"]["admissionChecks"]}})


REGISTRIES = {"hub": "cr.eu-north1.nebius.cloud/e00exampleexampleex", "eu-south1": "cr.eu-south1.nebius.cloud/e07exampleexampleex"}
TEMPLATE = {"metadata": {"annotations": {"serverless2.nebius/gpu-classes": "h100,rtx-pro-6000", "serverless2.nebius/regions": "eu-north1,eu-south1",
                                         "serverless2.nebius/pvc-size-gi": "50"}},
            "spec": {"initContainers": [{"name": "fetch", "image": "registry.serverless2.local/nebius/serverless2/jobs:0.1.3"}],
                     "containers": [{"name": "main", "image": "registry.serverless2.local/nebius/batch-example:1.0"},
                                    {"name": "uploader", "image": "registry.serverless2.local/nebius/serverless2/jobs:0.1.3"}],
                     "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": "op-1-work"}}]}}


def test_allowed_classes_and_regions_restrict_candidates():
    free = {("hub", "l40s-spot"): 1, ("hub", "h100-spot"): 1, ("eu-south1", "rtx6000-spot"): 2}
    r = d.rank(PROF, CLUSTERS, free, 1, {}, allowed_classes=["h100", "rtx-pro-6000"])
    assert r[0][:2] == ("hub", "h100-spot") and all(c[1] != "l40s-spot" for c in r)       # l40s not allowed although preferred by the profile
    r = d.rank(PROF, CLUSTERS, free, 1, {}, allowed_classes=["h100", "rtx-pro-6000"], allowed_clusters=["eu-south1"])
    assert [c[0] for c in r] == ["eu-south1"]
    wl = {"spec": {"podSets": [{"template": TEMPLATE}]}}
    assert d.allowed_of(wl) == (["h100", "rtx-pro-6000"], ["eu-north1", "eu-south1"])
    assert d.allowed_of({"spec": {"podSets": [{"template": {"spec": {}}}]}}) == (None, None)


def test_work_volume_only_where_admitted_and_swept_elsewhere():
    """A nomination creates nothing (the run may move while it waits); the admitted cluster gets the volume;
    a volume whose Jobs were admitted in another cluster is swept at once, an orphan after the grace period."""
    wl = {"metadata": {"namespace": "t", "ownerReferences": [{"kind": "Job", "name": "op-1"}]},
          "spec": {"podSets": [{"template": TEMPLATE}]}, "status": {"nominatedClusterNames": ["eu-south1", "hub"]}}
    assert d.admitted_volume(wl) is None
    # the remote copy (same name) is Admitted in one nominated worker: that is the admission (the manager's own
    # clusterName never turns under the external dispatcher)
    remote = {"eu-south1": {"status": {"conditions": [{"type": "QuotaReserved", "status": "True"}]}},
              "hub": {"status": {"conditions": [{"type": "Admitted", "status": "True"}]}}}
    assert d.admitted_cluster(wl, remote) == "hub" and d.admitted_cluster(wl, {}) is None
    assert d.admitted_volume(wl, d.admitted_cluster(wl, remote)) == ("hub", "t", "op-1-work", "50", "op-1")
    wl["status"]["clusterName"] = "hub"
    assert d.admitted_volume(wl) == ("hub", "t", "op-1-work", "50", "op-1")
    wl["status"]["conditions"] = [{"type": "Finished", "status": "True"}]
    assert d.admitted_volume(wl) is None
    assert d.admitted_cluster(wl) == "hub"
    del wl["status"]["clusterName"]                                                      # Kueue clears it once the worker's copy is gone
    assert d.admitted_cluster(wl) is None
    wl["metadata"]["annotations"] = {d.ADMITTED_ANN: "hub"}                             # ... the dispatcher's record remains
    assert d.admitted_cluster(wl) == "hub"
    admitted = {("t", "op-1"): "hub"}
    assert d.volume_action("eu-south1", ["op-1"], admitted, "t", jobs_exist=True, age_s=0) == "elsewhere"
    assert d.volume_action("hub", ["op-1"], admitted, "t", jobs_exist=True, age_s=0) is None
    assert d.volume_action("eu-south1", ["op-1"], {}, "t", jobs_exist=True, age_s=0) is None            # not admitted yet: keep
    assert d.volume_action("hub", ["op-1", "op-1-r1"], {("t", "op-1"): "eu-south1"}, "t", jobs_exist=True, age_s=0) is None   # a resume still pending here
    assert d.volume_action("hub", ["op-1"], {}, "t", jobs_exist=False, age_s=d.ORPHAN_AFTER_S + 1) == "orphan"
    assert d.volume_action("hub", ["op-1"], {}, "t", jobs_exist=False, age_s=1) is None
    assert d.pvc_of(TEMPLATE) == "op-1-work" and d.pvc_of({"spec": {}}) is None


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
    assert kwargs == {"field_manager": "kueue-admission", "force": True, "_content_type": "application/apply-patch+yaml"}


def test_nominate_restates_kueue_admission_owned_status():
    from unittest.mock import MagicMock
    custom = MagicMock()
    status = {"admission": {"clusterQueue": "prefer-h100", "podSetAssignments": [{"name": "main", "flavors": {"nvidia.com/gpu": "hub-h100-spot-1x"}}]},
              "admissionChecks": [{"name": "multikueue-prefer-h100", "state": "Pending", "message": "Reset to Pending after eviction. Previously: Retry", "retryCount": 1}],
              "conditions": [{"type": "QuotaReserved", "status": "True"}], "nominatedClusterNames": []}
    d.nominate(custom, "tenant-eval", "job-x", ["hub"], status)
    body = custom.patch_namespaced_custom_object_status.call_args[0][5]
    assert body["status"]["nominatedClusterNames"] == ["hub"]
    assert body["status"]["admission"] == status["admission"]              # quota reservation kept
    assert body["status"]["admissionChecks"] == status["admissionChecks"]  # check states kept (state/message required)
    assert "conditions" not in body["status"]                              # nothing else is claimed


def test_owner_job_accepts_jobsets_of_multinode_runs():
    assert d.owner_job({"metadata": {"ownerReferences": [{"kind": "JobSet", "name": "op-mn"}]}}) == "op-mn"
    assert d.owner_job({"metadata": {"ownerReferences": [{"kind": "Deployment", "name": "x"}]}}) is None


def test_spot_pool_is_a_candidate_at_once_after_on_demand_of_its_class():
    # capacity-first: inside one class reserved, then on-demand, then spot; a spot pool needs no waiting period
    pools = dict(POOLS)
    pools["hub-h100-ondemand"] = {"region": "hub", "pool": "h100-ondemand", "gpu_class": "h100", "capacity": "on_demand", "usd_per_gpu_hour": 4.5}
    clusters = d.clusters_from_pools(pools)
    free = {("hub", "h100-reserved"): 4, ("hub", "h100-ondemand"): 4, ("hub", "h100-spot"): 4}
    r = d.rank({"classes": ["h100"], "strategy": "preferred"}, clusters, free, 1, {"hub/h100-spot": 0.79})
    assert [x[1] for x in r] == ["h100-reserved", "h100-ondemand", "h100-spot"]
    assert r[2][2] == 0.79   # the live quote, not the 2.15 list price
    # the spot pool wins as soon as the others are full
    r = d.rank({"classes": ["h100"], "strategy": "preferred"}, clusters, {("hub", "h100-spot"): 4}, 1, {"hub/h100-spot": 0.79})
    assert r[0][1] == "h100-spot" and r[0][3] is True


def test_price_feed_quotes_spot_as_follow_price_not_priority(monkeypatch):
    import price_feed

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            stdout = '{"hourly_cost": {"general": {"total": {"cost": "7.92"}}}}'
        return R()

    monkeypatch.setattr(price_feed.subprocess, "run", fake_run)
    assert price_feed.estimate("project-x", "gpu-b300-sxm", "8gpu-192vcpu-2768gb", True) == 7.92
    cmd = seen["cmd"]
    assert "--resource-spec-compute-instance-spec-follows-spot-price" in cmd
    assert cmd[cmd.index("--resource-spec-compute-instance-spec-preemptible-on-preemption") + 1] == "STOP"
    assert not any("priority" in a for a in cmd)   # deprecated since 2026-05-11, the CLI rejects it
    price_feed.estimate("project-x", "gpu-b300-sxm", "8gpu-192vcpu-2768gb", False)
    assert not any("preemptible" in a or "spot" in a for a in seen["cmd"])
