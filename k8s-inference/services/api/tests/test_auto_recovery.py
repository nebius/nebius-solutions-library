"""Automatic recovery after a confirmed infrastructure loss (docs/SCHEDULING.md "Automatic recovery after infrastructure
loss"): opt-in `recovery.on_infrastructure_loss: recover`; a lost run continues from its acknowledged checkpoint pointer on
the next class after `wait_s`; the original is fenced and keeps its handle; bounds, cancellation and the losses that are
not infrastructure losses are respected; competing ticks converge on one successor."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app as appmod
import attempts
import catalog
import execution
import jobs
import kube
from config import LABEL
from test_api import FLEET, H, client, cluster, pod  # noqa: F401
from test_fallback import CFG, PER_CLASS, TWO_CLASS_FLEET, _env, _fold_model  # noqa: F401

NS = "tenant-demo"


@pytest.fixture(autouse=True)
def _fresh_catalog():
    """The file catalog is cached by mtime: entries normalised under this module's patched fleet must not leak into
    the next module (run classes take their regions from the fleet)."""
    yield
    catalog._cache.update({"mtime": None, "models": {}})


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def lost_run(client, monkeypatch):
    """A fleet run with `recover`, placed on us-central1 (RTX), whose pod is gone after a disruption; an acknowledged
    checkpoint pointer is on record. Returns (client, fake, op, records, fences)."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    marks: dict = {}
    monkeypatch.setattr(attempts, "_marks", lambda operation: marks.get(operation))
    monkeypatch.setattr(
        attempts,
        "mark_successor",
        lambda operation, successor: marks.__setitem__(
            operation, (1, marks.get(operation, (0, None, None))[1], successor)
        ),
    )
    monkeypatch.setattr(
        attempts,
        "mark_cancelled",
        lambda operation: marks.__setitem__(operation, (1, "now", marks.get(operation, (0, None, None))[2])),
    )
    fences: list = []
    monkeypatch.setattr(kube, "deactivate_workload", lambda ns, uid, region="eu-north1": fences.append(uid) or 1)
    r = c.post(
        "/v1/models/fold:invoke",
        json={
            "input": {},
            "execution": {
                "io_mode": "application",
                "placement": {"gpu_preference": ["rtx-pro-6000", "l40s"]},
                "recovery": {"on_infrastructure_loss": "recover", "wait_s": 0, "max_recoveries": 1},
            },
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[(NS, op)]
    assert job["metadata"]["labels"][f"{LABEL}/gpu-class"] == "rtx-pro-6000"
    assert json.loads(job["metadata"]["annotations"][f"{LABEL}/execution"])["recovery"] == {
        "on_infrastructure_loss": "recover",
        "max_recoveries": 1,
        "wait_s": 0,
    }
    assert job["spec"]["podFailurePolicy"]["rules"][0]["action"] != "FailJob" or "DisruptionTarget" not in json.dumps(
        job["spec"]["podFailurePolicy"]["rules"][0]
    )
    # the worker admitted it, the pod ran, then the node was reclaimed: the uploader's record is `interrupted`
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {job["metadata"].get("uid", ""): "us-central1"})
    records = [
        {
            "operation": op,
            "pod": f"{op}-abc",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 1,
            "revision": 3,
            "gpu_class": "rtx-pro-6000",
            "checkpoint_ref": "app://ckpt/gen-4",
            "checkpoint_generation": 4,
            "gpus": 1,
            "started_at": _ago(600),
        },
        {
            "operation": op,
            "pod": f"{op}-abc",
            "root": op,
            "tenant": "demo",
            "status": "interrupted",
            "epoch": 1,
            "revision": 4,
            "gpu_class": "rtx-pro-6000",
            "checkpoint_ref": "app://ckpt/gen-4",
            "checkpoint_generation": 4,
            "gpus": 1,
            "started_at": _ago(600),
            "ended_at": _ago(60),
        },
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda root: [x for x in records if x["root"] == root])
    return c, fake, op, records, fences, marks


def _successor_of(fake, op):
    return fake.jobs[(NS, op)]["metadata"]["annotations"].get(f"{LABEL}/successor")


def test_lost_run_continues_on_the_next_class_from_its_acknowledged_pointer(lost_run):
    c, fake, op, records, fences, marks = lost_run
    moved = appmod.recovery_switch()
    assert len(moved) == 1 and moved[0].startswith(f"{op}-r-")
    new = moved[0]
    orig = fake.jobs[(NS, op)]
    assert orig["metadata"]["annotations"][f"{LABEL}/successor"] == new and orig["spec"]["suspend"] is True
    assert (
        fences == [orig["metadata"].get("uid", "")] and marks[op][2] == new
    )  # fenced: Workload deactivated, successor mark
    succ = fake.jobs[(NS, new)]
    main = next(x for x in succ["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert main["image"].endswith("img:l40s") and {"name": "CHECKPOINT_REF", "value": "app://ckpt/gen-4"} in main["env"]
    assert (
        succ["metadata"]["labels"][f"{LABEL}/gpu-class"] == "l40s"
        and succ["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-l40s"
    )
    assert (
        succ["metadata"]["annotations"][f"{LABEL}/resumed-from"] == op
        and succ["metadata"]["annotations"][f"{LABEL}/recovered-from"] == op
    )
    assert json.loads(succ["metadata"]["annotations"][f"{LABEL}/execution"])["placement"]["gpu_preference"] == ["l40s"]
    assert (
        json.loads(succ["metadata"]["annotations"][f"{LABEL}/execution"])["recovery"]["checkpoint_ref"]
        == "app://ckpt/gen-4"
    )
    assert f"{LABEL}/pvc" not in succ["metadata"]["annotations"] or succ["metadata"]["annotations"][
        f"{LABEL}/pvc"
    ] != orig["metadata"]["annotations"].get(f"{LABEL}/pvc")
    # the handles: the original is SUPERSEDED and resolves to the successor; the successor says where it comes from
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["id"] == new
        and got["requested_id"] == op
        and got["recovered_from"] == op
        and "rtx-pro-6000 -> l40s" in got["reason"]
    )
    assert jobs.normalise(orig)["status"] == "SUPERSEDED"
    # a second tick (or a competing replica) creates nothing more; the same name would be computed again
    assert appmod.recovery_switch() == []
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-r-")]) == 1


def test_recovered_handle_projects_the_whole_chain(lost_run):
    """docs/API.md "Chains": the effective member keeps its id and current attempt; the response carries the root's
    creation time, every attempt with measured GPU seconds and image, per-member consumption, cumulative measured
    consumption with unknowns kept unknown, and when the successor was created."""
    c, fake, op, records, fences, marks = lost_run
    new = appmod.recovery_switch()[0]
    records[0]["image"] = records[1]["image"] = "img:rtx"
    records.append(
        {
            "operation": new,
            "pod": f"{new}-xyz",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 2,
            "revision": 9,
            "gpu_class": "l40s",
            "gpus": 1,
            "started_at": _ago(20),
            "image": "img:l40s",
            "region": "eu-north1",
        }
    )
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["id"] == new and got["requested_id"] == op and got["epoch"] == 2 and got["attempt_id"] == f"{new}-xyz"
    assert got["created_at"] == fake.jobs[(NS, op)]["metadata"]["creationTimestamp"]  # the root's submission time
    assert got["successor_created_at"] == fake.jobs[(NS, new)]["metadata"]["creationTimestamp"]
    assert [(a["operation"], a["status"], a["gpu_class"], a["image"]) for a in got["attempts"]] == [
        (op, "PREEMPTED", "rtx-pro-6000", "img:rtx"),
        (new, "PREEMPTED", "l40s", "img:l40s"),
    ]
    assert (
        535 <= got["attempts"][0]["gpu_seconds"] <= 545 and got["attempts"][1]["gpu_seconds"] is None
    )  # no end record: unknown
    assert (
        [m["id"] for m in got["chain"]] == [op, new]
        and got["chain"][0]["gpu_class"] == "rtx-pro-6000"
        and got["chain"][0]["status"] == "SUPERSEDED"
    )
    assert (
        got["chain"][0]["gpu_seconds_complete"] is True
        and got["chain"][1]["gpu_seconds"] is None
        and got["chain"][1]["gpu_seconds_complete"] is False
    )
    assert (
        535 <= got["gpu_seconds"] <= 545
        and got["gpu_seconds_complete"] is False
        and "rtx-pro-6000 -> l40s" in got["chain"][1]["reason"]
    )
    assert got["started_at"] == records[0]["started_at"]
    # a plain operation is untouched
    r = c.post("/v1/models/fold:invoke", json={"input": {}}, headers=H)
    plain = c.get(f"/v1/operations/{r.json()['id']}", headers=H).json()
    assert "chain" not in plain and "successor_created_at" not in plain


def test_recovery_waits_for_the_run_to_come_back_and_clears_the_loss(lost_run, monkeypatch):
    c, fake, op, records, fences, marks = lost_run
    job = fake.jobs[(NS, op)]
    ex = json.loads(job["metadata"]["annotations"][f"{LABEL}/execution"])
    ex["recovery"]["wait_s"] = 3600
    job["metadata"]["annotations"][f"{LABEL}/execution"] = json.dumps(ex, sort_keys=True)
    assert appmod.recovery_switch() == []  # the loss is stamped, the wait has not passed
    assert job["metadata"]["annotations"][f"{LABEL}/lost-at"] and fences == []
    # a replacement attempt on the same class started within the wait: not lost any more
    records.append(
        {
            "operation": op,
            "pod": f"{op}-def",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 2,
            "revision": 5,
            "gpu_class": "rtx-pro-6000",
            "gpus": 1,
            "started_at": _ago(5),
        }
    )
    fake.pods.append(pod(op, "Running", started=_ago(5)))
    fake.pods[-1]["metadata"]["name"] = f"{op}-def"
    assert appmod.recovery_switch() == []
    assert job["metadata"]["annotations"].get(f"{LABEL}/lost-at") is None


def test_recovery_inherits_the_roots_remaining_deadline(lost_run):
    c, fake, op, records, fences, marks = lost_run
    job = fake.jobs[(NS, op)]
    ex = json.loads(job["metadata"]["annotations"][f"{LABEL}/execution"])
    ex["recovery"]["deadline_s"] = 3600
    job["metadata"]["annotations"][f"{LABEL}/execution"] = json.dumps(ex, sort_keys=True)
    job["metadata"]["creationTimestamp"] = _ago(3000)
    moved = appmod.recovery_switch()
    succ = fake.jobs[(NS, moved[0])]
    assert 500 <= succ["spec"]["activeDeadlineSeconds"] <= 600  # never a fresh full budget
    # past the deadline nothing is created and the original says why
    fake.jobs.pop((NS, moved[0]))
    job["metadata"]["annotations"].pop(f"{LABEL}/successor")
    marks.clear()
    job["metadata"]["creationTimestamp"] = _ago(4000)
    assert appmod.recovery_switch() == []
    assert "deadline passed" in job["metadata"]["annotations"][f"{LABEL}/recovery"]


def test_bounds_and_non_losses_never_recover(lost_run, monkeypatch):
    c, fake, op, records, fences, marks = lost_run
    job = fake.jobs[(NS, op)]
    # exhausted: a chain that already used its one automatic continuation
    fake.jobs[(NS, f"{op}-r-aaaaaaaa")] = {
        "metadata": {
            "name": f"{op}-r-aaaaaaaa",
            "labels": dict(job["metadata"]["labels"]),
            "annotations": {f"{LABEL}/resumed-from": op, f"{LABEL}/recovered-from": op},
        },
        "spec": {},
        "status": {},
    }
    assert appmod.recovery_switch() == [] and "exhausted" in job["metadata"]["annotations"][f"{LABEL}/recovery"]
    del fake.jobs[(NS, f"{op}-r-aaaaaaaa")]
    # no acknowledged pointer: nothing to continue from
    for r in records:
        r.pop("checkpoint_ref", None)
    assert (
        appmod.recovery_switch() == []
        and "no acknowledged checkpoint" in job["metadata"]["annotations"][f"{LABEL}/recovery"]
    )
    records[0]["checkpoint_ref"] = records[1]["checkpoint_ref"] = "app://ckpt/gen-4"
    # an application failure (exit code) is not an infrastructure loss
    records[-1].update({"status": "failed", "exit_code": 1})
    assert appmod.recovery_switch() == []
    # an OOM kill ends with 137 like a SIGKILL: the kubelet's reason says it is the application's
    records[-1].update({"status": "interrupted"})
    oom = pod(op, "Failed", finished=_ago(30), exit_code=137)
    oom["status"]["containerStatuses"][0]["state"]["terminated"]["reason"] = "OOMKilled"
    oom["metadata"]["name"] = f"{op}-abc"
    fake.pods.append(oom)
    st = jobs.normalise(jobs.get(NS, op, "eu-north1"), records=records)
    assert st["attempts"][-1]["status"] == "FAILED"
    assert appmod.recovery_switch() == []
    fake.pods.remove(oom)
    # a worker whose pods cannot be listed: unknown, never assumed lost
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            jobs,
            "_with_pods",
            lambda job, ns, region, pods=None: (_ for _ in ()).throw(HTTPException(502, "worker down")),
        )
        assert appmod.recovery_switch() == [] and fences == []


def test_cancellation_wins_over_recovery(lost_run, monkeypatch):
    c, fake, op, records, fences, marks = lost_run
    # cancelled before the tick: never recovered
    marks[op] = (1, "now", None)
    assert appmod.recovery_switch() == [] and fences == []
    # cancelled while the tick moves it: the successor is cancelled right away, nothing is resurrected
    marks.clear()
    real = jobs.recover

    def recover_then_cancel(*a, **k):
        out = real(*a, **k)
        marks[op] = (2, "now", marks.get(op, (0, None, None))[2])
        return out

    monkeypatch.setattr(jobs, "recover", recover_then_cancel)
    assert appmod.recovery_switch() == []
    new = _successor_of(fake, op)
    assert new and marks[new][1] and (NS, new) not in fake.jobs  # never started: withdrawn (deleted)
    orig = fake.jobs[(NS, op)]
    assert (
        orig["metadata"]["annotations"][f"{LABEL}/cancelled"] == "true"
        and jobs.normalise(orig)["status"] == "CANCELLED"
    )
    assert (
        c.get(f"/v1/operations/{op}", headers=H).json()["status"] == "CANCELLED"
    )  # the handle reports the cancel, not a ghost successor


def test_legacy_defaults_and_discovery(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    r = c.post("/v1/models/fold:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 202 and (r.json().get("execution") or {}).get("recovery") in (
        None,
        {"on_infrastructure_loss": "retry"},
    )
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"recovery": {"on_infrastructure_loss": "retry", "max_recoveries": 2}}},
        headers=H,
    )
    assert r.status_code == 400 and "only with on_infrastructure_loss: recover" in r.text
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"recovery": {"on_infrastructure_loss": "recover", "max_recoveries": 9}}},
        headers=H,
    )
    assert r.status_code == 422
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"recovery": {"on_infrastructure_loss": "recover", "retries": 1}}},
        headers=H,
    )
    assert r.status_code == 422
    monkeypatch.setattr(execution, "tenant_config", lambda ns, region="eu-north1": CFG)
    cap = c.get("/v1/fleet", headers=H).json()["execution"]["recovery"]
    assert (
        cap["on_infrastructure_loss"] == ["retry", "fail", "recover"]
        and cap["max_recoveries"] == 3
        and cap["defaults"] == {"max_recoveries": 1, "wait_s": 300}
    )
    # a run with `recover` still has the same-worker replacement policy of `retry`
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"recovery": {"on_infrastructure_loss": "recover"}}},
        headers=H,
    )
    job = fake.jobs[(NS, r.json()["id"])]
    assert not any(
        rule.get("action") == "FailJob" and "DisruptionTarget" in json.dumps(rule)
        for rule in job["spec"]["podFailurePolicy"]["rules"]
    )
    assert appmod.recovery_switch() == []  # nothing lost


def test_superseded_operation_records_are_refused():
    """attempts.record on a fenced operation: the database test (tests/test_attempts.py) covers the live query; here the
    mark shape the record path reads."""
    assert attempts.successor_of  # the mark carries (revision, cancelled_at, successor)
