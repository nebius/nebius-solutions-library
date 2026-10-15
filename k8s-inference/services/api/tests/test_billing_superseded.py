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


FENCE = "2026-10-15T19:32:54Z"


@pytest.fixture
def never_ran(stopped, monkeypatch):
    """Q2 (op-cc97d4399365128b, 2026-10-15): the original moved to L40S while QUEUED; nothing of it ever ran. Every
    proof source answers authoritatively: the durable fence, the successor Job annotated as a fallback (its creation
    is the fence), zero database rows, exactly one owner-matched Workload with Admitted=False since before the fence
    and no cluster, Kueue inactive, no pod in any worker (`stopped`)."""
    world = {
        "rows": [],
        "successor": {
            "metadata": {"name": NEXT, "creationTimestamp": FENCE, "annotations": {f"{L}/fallback": "rtx -> l40s"}}
        },
        "workload": {"admitted": False, "cluster": None, "active": None, "not_admitted_since": "2026-10-15T19:31:48Z"},
    }
    monkeypatch.setattr(attempts, "for_operation", lambda root: list(world["rows"]))
    monkeypatch.setattr(jobs, "get", lambda ns, name, region: world["successor"])
    monkeypatch.setattr(kube, "workload_admission", lambda ns, uid, region: world["workload"])
    return world


def _queued():
    j = job()
    j["status"] = {}  # never resumed: no startTime, no active counter, no condition
    j["spec"]["managedBy"] = "kueue.x-k8s.io/multikueue"
    # a GPU run (the live Q2 shape): a GPU template with no attempt measures None, never 0 (billing.measured_usage)
    j["spec"]["template"] = {
        "spec": {"containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}
    }
    return j


def test_a_never_admitted_fallback_original_is_a_known_zero(never_ran):
    assert billing.never_started(_queued(), NS, "control")
    assert billing.known_zero(_queued(), [], NS, "control")


@pytest.mark.parametrize(
    "records",
    [
        [{"operation": ROOT, "pod": ROOT + "-a", "status": "started", "gpus": 1, "started_at": "2026-10-15T19:30:00Z"}],
        [{"operation": ROOT, "pod": ROOT + "-a", "status": "succeeded", "gpus": 1}],
    ],
)
def test_a_bucket_only_attempt_is_never_a_zero(never_ran, records):
    """settle_job merges the database rows with the bucket's records: an S3-only start without an end (an incomplete
    GPU attempt) or any recorded attempt keeps the unknown; known_zero needs zero records from every source."""
    assert not billing.known_zero(_queued(), records, NS, "control")


@pytest.mark.parametrize("counter", ["succeeded", "failed"])
def test_a_job_with_a_terminal_counter_never_qualifies(never_ran, counter):
    j = _queued()
    j["status"] = {counter: 1}
    assert not billing.never_started(j, NS, "control")


def test_a_billed_unknown_original_is_revisited_only_with_the_proof(never_ran, monkeypatch):
    j = _queued()
    j["metadata"]["annotations"][billing.BILLED] = "unknown"
    monkeypatch.setattr(db, "enabled", lambda: True)
    assert billing.unsettled(j, NS, "control")  # the one revisit of a billed Job
    j["metadata"]["annotations"][billing.KNOWN_ZERO] = "done"
    assert not billing.unsettled(j, NS, "control")  # corrected already: never twice
    j["metadata"]["annotations"].pop(billing.KNOWN_ZERO)
    j["metadata"]["annotations"][billing.BILLED] = "12.5"
    assert not billing.unsettled(j, NS, "control")  # a measured bill is never revisited
    j["metadata"]["annotations"][billing.BILLED] = "unknown"
    never_ran["rows"] = [{"operation": ROOT, "pod": ROOT + "-a", "status": "started"}]
    assert not billing.unsettled(j, NS, "control")  # an unknown with a record stays unknown


@pytest.mark.parametrize(
    "change",
    [
        "db-record",
        "started",
        "active",
        "complete",
        "workload-absent",
        "workload-admitted",
        "workload-cluster",
        "no-admitted-false",
        "admitted-false-after-fence",
        "successor-not-fallback",
        "successor-unreadable",
        "workload-unreadable",
        "db-unreadable",
        "pods-present",
        "pods-unreadable",
        "kueue-active",
        "fence-mismatch",
    ],
)
def test_anything_short_of_the_full_proof_keeps_unknown(never_ran, stopped, monkeypatch, change):
    j = _queued()
    w = never_ran
    if change == "db-record":  # any attempt record (a recovery predecessor, a lost worker): it ran
        w["rows"] = [{"operation": ROOT, "pod": ROOT + "-a", "status": "interrupted"}]
    elif change == "started":  # Kueue resumed it once
        j["status"] = {"startTime": "2026-10-15T19:30:00Z"}
    elif change == "active":
        j["status"] = {"active": 1}
    elif change == "complete":
        j["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    elif change == "workload-absent":  # an absence proves nothing
        w["workload"] = None
    elif change == "workload-admitted":  # it was admitted once (an evicted, cleared, unrecorded start)
        w["workload"] = {**w["workload"], "admitted": True}
    elif change == "workload-cluster":
        w["workload"] = {**w["workload"], "cluster": "south"}
    elif change == "no-admitted-false":  # no explicit Admitted=False condition on record
        w["workload"] = {**w["workload"], "not_admitted_since": None}
    elif change == "admitted-false-after-fence":  # Admitted=False only after the move: admission history unclear
        w["workload"] = {**w["workload"], "not_admitted_since": "2026-10-15T19:40:00Z"}
    elif change == "successor-not-fallback":  # superseded by recovery, not by a class fallback
        w["successor"]["metadata"]["annotations"] = {f"{L}/recovery": "node lost"}
    elif change == "successor-unreadable":
        monkeypatch.setattr(jobs, "get", lambda *a: (_ for _ in ()).throw(OSError("unreadable")))
    elif change == "workload-unreadable":
        monkeypatch.setattr(kube, "workload_admission", lambda *a: (_ for _ in ()).throw(ApiException(status=503)))
    elif change == "db-unreadable":
        monkeypatch.setattr(attempts, "for_operation", lambda root: (_ for _ in ()).throw(OSError("pool down")))
    elif change == "pods-present":  # a terminal, unrecorded pod in a worker: stopped is not never-ran
        stopped[0]["north"] = [{"status": {"phase": "Succeeded"}}]
    elif change == "pods-unreadable":  # one worker unreadable: no proof
        stopped[0]["south"] = ApiException(status=503)
    elif change == "kueue-active":
        monkeypatch.setattr(kube, "workload_inactive", lambda *a: False)
    else:  # the durable fence names another successor
        monkeypatch.setattr(attempts, "successor_of", lambda name: "someone-else")
    assert not billing.never_started(j, NS, "control")


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
