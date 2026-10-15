"""A suspended, fenced recovery predecessor settles its own attempts, never its successor's GPU time."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

import attempts
import billing
import db
import jobs
import keys
import kube
from config import LABEL as L
from test_database import LEGACY, database  # noqa: F401

NS, ROOT, NEXT = "tenant-demo", "op-recovery", "op-recovery-r-next"


def job(name=ROOT):
    return {
        "metadata": {
            "name": name,
            "namespace": NS,
            "uid": "uid-" + name,
            "labels": {f"{L}/mode": "run", f"{L}/model": "synthetic", f"{L}/region": "eu-north1"},
            "annotations": {
                billing.KEY: keys.key_hash("synthetic"),
                f"{L}/successor": NEXT,
                f"{L}/billing-rates": json.dumps({"eu-north1": {"rtx": 2.0, "l40s": 1.0}}),
            },
        },
        "spec": {"suspend": True, "managedBy": "kueue.x-k8s.io/multikueue"},
        "status": {"active": 1},  # stale active counter; suspension and live worker reads prove it stopped
    }


@pytest.fixture
def stopped(monkeypatch):
    seen, pods = [], {r: [] for r in ("control", "north", "south")}

    def core(region):
        def listed(ns, **kwargs):
            seen.append((region, ns, kwargs.get("label_selector")))
            value = pods[region]
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(items=value)

        return SimpleNamespace(
            api_client=SimpleNamespace(sanitize_for_serialization=lambda x: x), list_namespaced_pod=listed
        )

    monkeypatch.setattr(kube, "regions", lambda: ["north", "south"])
    monkeypatch.setattr(kube, "core", core)
    monkeypatch.setattr(kube, "workload_inactive", lambda *a: True)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "successor_of", lambda name: NEXT)
    return pods, seen


def test_fenced_predecessor_checks_every_worker_even_with_stale_active_status(stopped):
    pods, seen = stopped
    pods["north"] = [{"status": {"phase": "Failed"}}]
    assert billing.unsettled(job(), NS, "control")
    assert {r for r, _, _ in seen} == {"control", "north", "south"}
    assert all(selector == "job-name=" + ROOT for _, _, selector in seen)


@pytest.mark.parametrize("phase", ["Pending", "Running", "Unknown", None])
def test_any_nonterminal_worker_pod_blocks_settlement(stopped, phase):
    stopped[0]["south"] = [{"status": {"phase": phase}}]
    assert not billing.unsettled(job(), NS, "control")


@pytest.mark.parametrize("error", [ApiException(status=403), ApiException(status=503), OSError("unavailable")])
def test_unreadable_worker_is_not_an_absence(stopped, monkeypatch, error):
    monkeypatch.setattr(billing, "retry", lambda fn, *a, **kw: fn(*a, **kw))
    stopped[0]["south"] = error
    assert not billing.unsettled(job(), NS, "control")


def test_absent_namespace_is_not_an_active_worker(stopped):
    stopped[0]["south"] = ApiException(status=404)
    assert billing.unsettled(job(), NS, "control")


@pytest.mark.parametrize(
    "change", ["no-successor", "self-successor", "not-suspended", "no-uid", "unknown-pods", "billed", "no-key"]
)
def test_incomplete_predecessor_proof_never_settles(stopped, change):
    j = job()
    if change == "no-successor":
        j["metadata"]["annotations"].pop(f"{L}/successor")
    elif change == "self-successor":
        j["metadata"]["annotations"][f"{L}/successor"] = ROOT
    elif change == "not-suspended":
        j["spec"]["suspend"] = False
    elif change == "no-uid":
        j["metadata"].pop("uid")
    elif change == "unknown-pods":
        j["_pods_unknown"] = True
    elif change == "billed":
        j["metadata"]["annotations"][billing.BILLED] = "0.1"
    else:
        j["metadata"]["annotations"].pop(billing.KEY)
    assert not billing.unsettled(j, NS, "control")


@pytest.mark.parametrize("fence", [None, "another-successor"])
def test_annotation_alone_is_not_a_durable_fence(stopped, monkeypatch, fence):
    monkeypatch.setattr(attempts, "successor_of", lambda name: fence)
    assert not billing.unsettled(job(), NS, "control")


def test_active_kueue_and_missing_context_are_not_settled(stopped, monkeypatch):
    assert not billing.superseded_over(job(), None, "control")
    assert not billing.superseded_over(job(), NS, None)
    monkeypatch.setattr(kube, "workload_inactive", lambda *a: False)
    assert not billing.unsettled(job(), NS, "control")
    monkeypatch.setattr(kube, "workload_inactive", lambda *a: True)
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    assert not billing.unsettled(job(), NS, "control")


@pytest.mark.parametrize("measured", [True, False])
def test_real_ledger_settles_each_member_once_after_annotation_loss(database, monkeypatch, measured):
    db.migrate()
    h = keys.key_hash("synthetic")
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": h,
                "alias": "recovery-test",
                "budget_gpu_seconds": 10000,
                "max_concurrent": 1,
                "budget_requests": 10,
            }
        ],
        "demo",
        "admin",
    )
    keys.admit(h, ROOT, "demo", 1000, run_member=True)
    keys.prepare_run_member(ROOT, NEXT, ROOT, 1, 1000)
    attempts.mark_successor(ROOT, NEXT)
    with db.pool().connection() as c:
        for name, epoch, pool, start, end, status in [
            (ROOT, 1, "rtx", "2026-10-15T09:00:00Z", "2026-10-15T09:03:08Z" if measured else None, "interrupted"),
            (NEXT, 2, "l40s", "2026-10-15T09:04:00Z", "2026-10-15T09:05:44Z", "succeeded"),
        ]:
            c.execute(
                "insert into attempts (operation,pod,root,tenant,namespace,epoch,status,gpus,pool,started_at,ended_at) "
                "values (%s,%s,%s,'demo',%s,%s,%s,1,%s,%s,%s)",
                (name, name + "-pod", ROOT, NS, epoch, status, pool, start, end),
            )
    original, successor = job(), job(NEXT)
    # Automatic recovery crossed regions (RTX south -> L40S north). A manager Job has no pinned region label;
    # each Workload's durable admitted-cluster selects that member's immutable price snapshot independently.
    for member in (original, successor):
        member["metadata"]["labels"].pop(f"{L}/region")
        member["metadata"]["annotations"][f"{L}/billing-rates"] = json.dumps(
            {"eu-south1": {"rtx": 2.0}, "eu-north1": {"l40s": 1.0}}
        )
    successor["metadata"]["annotations"].pop(f"{L}/successor")
    successor["metadata"]["annotations"][f"{L}/resumed-from"] = ROOT
    successor["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    all_jobs = {ROOT: original, NEXT: successor}
    empty = SimpleNamespace(items=[])
    core = SimpleNamespace(
        api_client=SimpleNamespace(sanitize_for_serialization=lambda x: x), list_namespaced_pod=lambda *a, **kw: empty
    )
    monkeypatch.setattr(kube, "regions", lambda: ["control", "north", "south"])
    monkeypatch.setattr(kube, "core", lambda region: core)
    monkeypatch.setattr(kube, "workload_inactive", lambda *a: True)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {"uid-" + ROOT: "south", "uid-" + NEXT: "north"})
    monkeypatch.setattr(kube, "cluster_region", lambda region: {"south": "eu-south1", "north": "eu-north1"}[region])
    monkeypatch.setattr(jobs, "get", lambda ns, name, region: copy.deepcopy(all_jobs[name]))
    monkeypatch.setattr(billing.catalog, "get", lambda mid: None)
    monkeypatch.setattr(billing.execution, "sweep_copies", lambda *a: None)
    lost = []

    def annotate(region, ns, name, done, job=None):
        if name == ROOT and not lost:
            lost.append(name)
            raise OSError("annotation response lost")
        all_jobs[name]["metadata"]["annotations"].update(done)

    monkeypatch.setattr(billing, "_annotate", annotate)
    assert asyncio.run(billing.settle_job("control", NS, successor))
    assert keys.run_usage(h)["active_operations"] == 1
    assert not asyncio.run(billing.settle_job("control", NS, original))
    assert asyncio.run(billing.settle_job("control", NS, original))
    assert not asyncio.run(billing.settle_job("control", NS, original))
    assert not asyncio.run(billing.settle_job("control", NS, successor))
    with db.pool().connection() as c:
        seconds, requests = c.execute(
            "select spent_gpu_seconds,spent_requests from keys where hash=%s", (h,)
        ).fetchone()
        charges = c.execute("select gpu_seconds from charges order by gpu_seconds").fetchall()
    assert float(seconds) == (292.0 if measured else 104.0)
    assert float(requests) == 1
    assert [float(x[0]) for x in charges] == ([104.0, 188.0] if measured else [104.0])
    assert keys.run_usage(h)["active_operations"] == 0
    assert keys.run_usage(h)["unknown_gpu_seconds"] == (0 if measured else 1000)
    assert original["metadata"]["annotations"][f"{L}/gpu-seconds"] == ("188.0" if measured else "unknown")
