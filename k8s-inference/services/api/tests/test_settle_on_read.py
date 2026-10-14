"""Settlement on the owner's read (api 0.11.105; docs/JOBS.md "Billing"): a finished operation its owner reads before
the billing CronJob reached it is settled by that read (billing.settle_job, the pass's own body for one operation):
the same answer carries `usage: measured`, `gpu_seconds`, and the key's slot is free. Concurrent readers and a
concurrent pass settle it exactly once (one charge, one release, one annotation); an unfinished operation, a foreign
reader, and the pass's own semantics for an unmeasured attempt (`unknown`, the reservation never freed as zero) are
unchanged. Against PostgreSQL (the real key ledger) with the fake cluster; skipped without TEST_DATABASE_URL."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor

import auth
import billing
import db
import keys
import kube
from config import LABEL
from test_api import KEY_INFO, H, client, cluster, pod  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

NS = "tenant-demo"
P2 = {"Authorization": "Bearer sk-other"}


def _rig(database, client, monkeypatch):
    c, fake = client
    db.migrate()
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": keys.key_hash(k),
                "alias": alias,
                "principal_id": pid,
                "max_concurrent": 1,
                "budget_requests": 20,
                "rate_limit": None,
                "budget_gpu_seconds": 100000,
                "scopes": ["invoke:sync", "run:submit", "read:operations"],
            }
            for k, alias, pid in (("sk-good", "p1", "p-1"), ("sk-other", "p2", "p-2"))
        ],
        "demo",
        "admin",
    )
    infos = {
        f"sk-{k}": {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": pid}}}
        for k, pid in (("good", "p-1"), ("other", "p-2"))
    }

    async def info(key):
        if key not in infos:
            from fastapi import HTTPException

            raise HTTPException(401, "invalid API key")
        return copy.deepcopy(infos[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    monkeypatch.setattr(billing.artifacts, "attempt_records", lambda ns, op, region, prefix=None: [])
    monkeypatch.setattr(billing.execution, "sweep_copies", lambda ns, op: None)
    monkeypatch.setattr(billing, "cost_of", lambda job, m, records, region: (billing.measured_usage(job, records), 1.0))
    return c, fake


def _run(c, fake, finished=True, measured=True):
    r = c.post(
        "/v1/models/batch-example:invoke",
        json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    p = pod(op, "Succeeded", "2026-10-14T17:16:58Z", "2026-10-14T17:20:36Z" if measured else None, 0)
    fake.pods.append(p)
    if finished:
        fake.jobs[(NS, op)]["status"] = {
            "conditions": [{"type": "Complete", "status": "True"}],
            "completionTime": "2026-10-14T17:20:50Z",
        }
    return op


def _sql(q, *args):
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    with db.pool().connection() as c:
        return c.execute(q, args).fetchall()


def _charges(fake, op):
    uid = fake.jobs[(NS, op)]["metadata"].get("uid")
    return _sql("select count(*) from charges where operation like %s", (f"%/{uid}",))[0][0]


def test_the_owners_first_terminal_read_settles_the_operation_once(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    h = keys.key_hash("sk-good")
    op = _run(c, fake)
    assert keys.run_usage(h)["active_operations"] == 1  # admitted: the slot is held until the settlement
    r = c.get(f"/v1/operations/{op}", headers=H)
    assert r.status_code == 200, r.text
    o = r.json()
    # finished (the fake run has no delivery record: EXPORT_FAILED, settled like any finished run), settled by this read
    assert o["status"] in ("SUCCEEDED", "EXPORT_FAILED") and o["usage"] == "measured" and o["gpu_seconds"] == 218.0, o
    ann = fake.jobs[(NS, op)]["metadata"]["annotations"]
    assert ann[f"{LABEL}/billed"] == "1.0" and ann[f"{LABEL}/gpu-seconds"] == "218.0"
    assert keys.run_usage(h)["active_operations"] == 0  # the slot is free
    assert (
        _charges(fake, op) == 1
        and float(_sql("select spent_gpu_seconds from keys where hash = %s", (h,))[0][0]) == 218.0
    )
    # a second read, and the pass: nothing again
    assert c.get(f"/v1/operations/{op}", headers=H).json()["usage"] == "measured"
    assert asyncio.run(billing.bill_once()) == 0
    assert (
        _charges(fake, op) == 1
        and float(_sql("select spent_gpu_seconds from keys where hash = %s", (h,))[0][0]) == 218.0
    )
    # the freed slot admits the next run of this max_concurrent: 1 key at once
    assert (
        c.post(
            "/v1/models/batch-example:invoke",
            json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}},
            headers=H,
        ).status_code
        == 202
    )


def test_concurrent_readers_and_the_pass_settle_exactly_once(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    h = keys.key_hash("sk-good")
    op = _run(c, fake)

    def read(_):
        return c.get(f"/v1/operations/{op}", headers=H).json()["usage"]

    def pass_(_):
        return asyncio.run(billing.bill_once())

    with ThreadPoolExecutor(max_workers=6) as ex:
        reads = list(ex.map(read, range(4)))
        passes = list(ex.map(pass_, range(2)))
    # a reader that meets the lock held by another settler answers the view before the settlement once (`pending`);
    # at least one read settled, every later read sees it, and the pass found nothing left or settled it itself
    assert "measured" in reads and all(u in ("measured", "pending") for u in reads) and sum(passes) <= 1
    assert c.get(f"/v1/operations/{op}", headers=H).json()["usage"] == "measured"
    assert _charges(fake, op) == 1
    assert float(_sql("select spent_gpu_seconds from keys where hash = %s", (h,))[0][0]) == 218.0
    assert float(_sql("select spent_requests from keys where hash = %s", (h,))[0][0]) == 1.0
    assert keys.run_usage(h)["active_operations"] == 0
    assert fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/billed"] == "1.0"


def test_unfinished_foreign_and_unmeasured_reads_keep_the_pass_semantics(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    h = keys.key_hash("sk-good")
    # unfinished: nothing is settled by a read
    op = _run(c, fake, finished=False)
    fake.pods[-1]["status"]["phase"] = "Running"
    o = c.get(f"/v1/operations/{op}", headers=H).json()
    assert o["usage"] == "pending" and f"{LABEL}/billed" not in fake.jobs[(NS, op)]["metadata"]["annotations"]
    assert keys.run_usage(h)["active_operations"] == 1 and _charges(fake, op) == 0
    # finished now, but a foreign principal's read settles nothing (404 as before)
    fake.jobs[(NS, op)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    fake.pods[-1]["status"]["phase"] = "Succeeded"
    assert c.get(f"/v1/operations/{op}", headers=P2).status_code == 404
    assert _charges(fake, op) == 0 and keys.run_usage(h)["active_operations"] == 1
    # the owner's read settles it
    assert c.get(f"/v1/operations/{op}", headers=H).json()["usage"] == "measured" and _charges(fake, op) == 1
    # an unmeasured attempt (no end on record): `unknown` on read exactly as on the pass; never a zero charge
    op2 = _run(c, fake, measured=False)
    o2 = c.get(f"/v1/operations/{op2}", headers=H).json()
    ann = fake.jobs[(NS, op2)]["metadata"]["annotations"]
    assert o2["usage"] == "unknown" and o2["gpu_seconds"] is None
    assert ann[f"{LABEL}/billed"] == "unknown" and ann[f"{LABEL}/gpu-seconds"] == "unknown"
    assert _charges(fake, op2) == 0
    assert float(_sql("select spent_gpu_seconds from keys where hash = %s", (h,))[0][0]) == 218.0  # op only
    assert asyncio.run(billing.bill_once()) == 0  # the pass finds nothing left to do


def test_a_cancelled_gang_settles_once_kueue_holds_nothing_of_it(client, monkeypatch):
    """A cancelled JobSet never gets Complete/Failed (suspended, its pods deleted, a manager's worker copy removed), so
    the pass and the owner's read never settled it and the key stayed reserved (the first MPI cancel, 2026-10-14). It
    settles once its cancel is on record, it is suspended, no pod of it is left and Kueue holds nothing of it (its
    Workload gone, deactivated or finished): measured from the rank records like a finished run; not before."""
    import asyncio

    import attempts
    import billing
    import jobs
    import kube
    from config import LABEL

    _c, fake = client
    name = "op-gang-cancel-1"
    js = {
        "apiVersion": "jobset.x-k8s.io/v1alpha2",
        "kind": "JobSet",
        "metadata": {
            "name": name,
            "namespace": NS,
            "uid": "uid-gang-cancel-1",
            "labels": {f"{LABEL}/mode": "run", f"{LABEL}/tenant": "demo", f"{LABEL}/model": "distributed-run"},
            "annotations": {f"{LABEL}/key": "k" * 64, f"{LABEL}/cancelled": "true"},
        },
        "spec": {"suspend": True, "managedBy": "kueue.x-k8s.io/multikueue", "replicatedJobs": []},
        "status": {},
    }
    rec = {
        "operation": name,
        "root": name,
        "pod": f"{name}-workers-0-0-abcde",
        "status": "failed",
        "exit_code": 1,
        "started_at": "2026-10-14T18:43:43Z",
        "ended_at": "2026-10-14T18:45:43Z",
        "gpus": 1,
        "gpu_class": "h100",
        "role": "run",
        "gang_rank": 0,
    }
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [rec] if root == name else [])
    monkeypatch.setattr(jobs, "get", lambda ns, n, region: dict(js))
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {"uid-gang-cancel-1": "north"})
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    monkeypatch.setattr(billing, "cost_of", lambda job, m, records, region: (billing.measured_usage(job, records), 0.0))
    patched = []
    monkeypatch.setattr(jobs, "patch_jobset", lambda ns, n, region, body: patched.append((n, body)))
    released = []
    import keys

    monkeypatch.setattr(keys, "release", lambda op, s=None: released.append((op, s)) or True)
    monkeypatch.setattr(db, "enabled", lambda: True)

    async def fake_add(token, usd, operation, gpu_s):
        return 0.0

    monkeypatch.setattr(billing, "add_spend", fake_add)
    # Kueue still holds the Workload (the eviction not through yet): not over, nothing settled
    monkeypatch.setattr(kube, "workload_inactive", lambda ns, uid, region="control": False)
    assert billing.unsettled(js, NS, "control") is False
    assert asyncio.run(billing.settle_job("control", NS, dict(js), {})) is False and not released
    # the Workload deactivated/gone: over; settled from the rank record (120 s x 1 GPU), the key released, billed
    monkeypatch.setattr(kube, "workload_inactive", lambda ns, uid, region="control": True)
    assert billing.unsettled(js, NS, "control") is True
    assert asyncio.run(billing.settle_job("control", NS, dict(js), {})) is True
    assert released == [(name, 120.0)]
    assert patched and patched[-1][1]["metadata"]["annotations"][f"{LABEL}/gpu-seconds"] == "120.0"
    # a cancelled JobSet that is not suspended, or still has a pod, is not over
    monkeypatch.setattr(kube, "workload_inactive", lambda ns, uid, region="control": True)
    assert billing.cancelled_over({**js, "spec": {**js["spec"], "suspend": False}}, NS, "control") is False
    assert billing.cancelled_over({**js, "_pods": [{"metadata": {"name": "p"}}]}, NS, "control") is False
