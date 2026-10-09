"""The ranking the API asks for at submission (GET /v1/rank): best candidate first, with its GPU class."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dispatcher as d  # noqa: E402
from test_rank import CLUSTERS  # noqa: E402


def snapshot(free):
    return {"clusters": CLUSTERS, "spot": {}, "free": free, "aliases": {"eu-north1": "hub", "eu-south1": "eu-south1"}, "at": "t"}


def test_rank_response_best_first_with_class_and_region_names():
    # the preferred class (rtx-pro-6000) is free in eu-south1: it wins, named by region, with its class and capacity
    r = d.rank_response(snapshot({("eu-south1", "rtx6000-spot"): 1, ("hub", "h100-spot"): 1}), "prefer-rtx-pro-6000", 1,
                        ["rtx-pro-6000", "h100"], ["eu-north1", "eu-south1"])
    assert r["best"] == {"cluster": "eu-south1", "region": "eu-south1", "pool": "rtx6000-spot", "gpu_class": "rtx-pro-6000",
                         "capacity": "spot", "price": 0.95, "free": True}
    assert [c["gpu_class"] for c in r["ranked"]][:2] == ["rtx-pro-6000", "h100"] and r["profile"]["strategy"] == "preferred"
    # the preferred class is busy, h100 is free: a free class beats a queued preferred one; reserved before spot
    r = d.rank_response(snapshot({("hub", "h100-reserved"): 8, ("hub", "h100-spot"): 1}), "prefer-rtx-pro-6000", 1,
                        ["rtx-pro-6000", "h100"], None)
    assert r["best"]["gpu_class"] == "h100" and r["best"]["pool"] == "h100-reserved" and r["best"]["capacity"] == "reserved"
    # a region pin (region name or cluster id) restricts the candidates
    r = d.rank_response(snapshot({}), "prefer-rtx-pro-6000", 1, ["rtx-pro-6000", "h100"], None, pin="eu-north1")
    assert {c["cluster"] for c in r["ranked"]} == {"hub"} and r["best"]["gpu_class"] == "h100"
    # nothing allowed: no best
    assert d.rank_response(snapshot({}), "default", 1, ["b300"], None)["best"] is None


def test_follower_refreshes_ranking_without_mutating_workloads(monkeypatch):
    monkeypatch.setattr(d, "load_fleet", lambda _: (CLUSTERS, {}))
    monkeypatch.setattr(d, "worker_clients", lambda *_: ({}, {"eu-north1": "hub"}))
    monkeypatch.setattr(d, "free_quota", lambda _: {("hub", "h100-spot"): 1})
    class ReadOnly:
        def list_cluster_custom_object(self, *a):
            raise AssertionError("a follower must not start workload reconciliation")
    d.reconcile(None, ReadOnly(), None, dispatch=False)
    assert d.SNAPSHOT["at"] and d.SNAPSHOT["free"][("hub", "h100-spot")] == 1
