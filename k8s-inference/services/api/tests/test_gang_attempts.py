"""Gang attempts (docs/JOBS.md "Multi-node runs", "Gang attempts", "Finalization by the application"): the record that
speaks for a JobSet is its coordinator's (rank 0) of the newest incarnation; a gang's outcome counts every rank; a
"delivery pending" exit fails the JobSet without the whole-gang restart; the RDMA memlock contract is stated to the
run instead of granting a capability; ordinary single-pod runs are untouched."""

import attempts
import jobs
import kube
from test_api import client, cluster  # noqa: F401
from test_attempts import _record

FLEET_IB = {
    "pools": {
        "north-h100-8x": {
            "region": "north",
            "pool": "h100-8x",
            "gpu_class": "h100",
            "gpus_per_node": 8,
            "interconnect": "infiniband",
            "ib_devices_per_node": 8,
        }
    },
    "clusters": {},
    "fit": {},
    "images": {},
}


def _gang_rows(op, outcomes, epoch=1):
    return [
        {
            **_record(op, f"{op}-workers-0-{rank}-abcde", st, epoch=epoch),
            "operation": op,
            "root": op,
            "gang_rank": rank,
            "gang_restart": epoch - 1,
        }
        for rank, st in enumerate(outcomes)
    ]


def test_the_coordinator_of_the_newest_incarnation_speaks_for_the_gang():
    op = "op-gang"
    rows = _gang_rows(op, ["succeeded", "succeeded"]) + _gang_rows(op, ["started", "started"], epoch=2)
    assert attempts.authority(rows)["pod"] == f"{op}-workers-0-0-abcde" and attempts.authority(rows)["epoch"] == 2
    # the peers recorded first and the coordinator later: still the coordinator
    rows2 = _gang_rows(op, ["started", "started"], epoch=2)[::-1]
    assert attempts.authority(rows2)["gang_rank"] == 0
    # without a coordinator record yet: the newest row; an ordinary operation: its newest pod
    assert attempts.authority(_gang_rows(op, ["x", "started"], epoch=3)[1:])["gang_rank"] == 1
    solo = [
        {**_record("op-solo", "op-solo-a", "failed", epoch=1), "operation": "op-solo"},
        {**_record("op-solo", "op-solo-b", "started", epoch=2), "operation": "op-solo"},
    ]
    assert attempts.authority(solo)["pod"] == "op-solo-b" and attempts.gang_status(solo) is None
    life = attempts.lifecycle(op, op, rows)
    assert life["attempt_id"] == f"{op}-workers-0-0-abcde" and life["epoch"] == 2


def test_a_gang_outcome_counts_every_rank():
    op = "op-gang"
    assert attempts.gang_status(_gang_rows(op, ["succeeded", "succeeded", "succeeded"])) == "succeeded"
    assert attempts.gang_status(_gang_rows(op, ["succeeded", "failed", "succeeded"])) == "failed"
    assert attempts.gang_status(_gang_rows(op, ["succeeded", "interrupted"])) == "interrupted"
    assert attempts.gang_status(_gang_rows(op, ["succeeded", "started"])) == "started"
    # the newest incarnation decides, not an older one
    rows = _gang_rows(op, ["failed", "failed"]) + _gang_rows(op, ["succeeded", "succeeded"], epoch=2)
    assert attempts.gang_status(rows) == "succeeded"


def _render(monkeypatch, extra=None):
    monkeypatch.setattr(kube, "fleet", lambda: FLEET_IB)
    model = {
        "name": "gromacs-mpi-offline",
        "gpu_classes": ["h100"],
        "parameters": [{"name": "output_prefix", "default": "synthetic/outputs"}],
        "job": {
            "image": "example.invalid/gromacs@sha256:" + "a" * 64,
            "command": ["/usr/bin/true"],
            "nodes": 2,
            "gpu": 8,
            "cpu": 64,
            "memory": "128Gi",
            "pvcSizeGi": 64,
            "interconnect": "required",
            "checkpoints": "shared",
            **(extra or {}),
        },
    }
    return jobs.build_run(
        "op-gang-render",
        model,
        {"output_prefix": "synthetic/outputs"},
        "system",
        None,
        "offline-only",
        3600,
        None,
        ex={"io_mode": "application", "private_input_ref": "synthetic-binding"},
    )


FINALIZE = {"finalize": {"command": "/usr/bin/true", "onExitCodes": [75], "timeoutS": 600, "cpu": "1", "memory": "2Gi"}}


def test_a_delivery_pending_exit_fails_the_jobset_without_a_gang_restart(monkeypatch):
    js = _render(monkeypatch, FINALIZE)
    assert js["kind"] == "JobSet"
    spec = js["spec"]["replicatedJobs"][0]["template"]["spec"]
    assert spec["podFailurePolicy"] == {
        "rules": [{"action": "FailJob", "onExitCodes": {"containerName": "main", "operator": "In", "values": [75]}}]
    }
    assert spec["backoffLimit"] == 0 and spec["template"]["spec"]["restartPolicy"] == "Never"
    fp = js["spec"]["failurePolicy"]
    assert fp["restartStrategy"] == "Recreate" and fp["maxRestarts"] >= 1  # every other loss: the gang restarts
    assert fp["rules"] == [
        {
            "name": "delivery-pending",
            "action": "FailJobSet",
            "onJobFailureReasons": ["PodFailurePolicy"],
            "targetReplicatedJobs": ["workers"],
        }
    ]
    # without a finalizer: no policy, the gang restart as before
    plain = _render(monkeypatch)
    assert "podFailurePolicy" not in plain["spec"]["replicatedJobs"][0]["template"]["spec"]
    assert "rules" not in plain["spec"]["failurePolicy"]


def test_the_rdma_memlock_contract_is_stated_not_granted(monkeypatch):
    js = _render(monkeypatch)
    pod = js["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    main = next(c for c in pod["containers"] if c["name"] == "main")
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["S2_RDMA_MEMLOCK_POLICY"] == "node-unlimited" and env["S2_RDMA_CAPABILITIES"] == "none"
    assert env["S2_RDMA_DEVICES_PER_NODE"] == "8"
    assert main["securityContext"]["capabilities"] == {"drop": ["ALL"]}  # no IPC_LOCK, no privilege
    assert main["resources"]["claims"] == [{"name": "ib"}]
    # TCP on purpose: no fabric, no statement
    tcp = _render(monkeypatch, {"interconnect": "none"})
    tmain = next(
        c
        for c in tcp["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"]
        if c["name"] == "main"
    )
    assert "S2_RDMA_MEMLOCK_POLICY" not in {e["name"] for e in tmain["env"]}


def test_export_of_a_failed_gang_renders_one_cpu_finalizer_on_the_shared_checkpoint_claim(client, monkeypatch):
    """A multi-node application-I/O run whose coordinator ended with the model's delivery-pending exit (a peer's
    teardown exit is not the verdict): `:export` renders the finalizer as ONE CPU Job on the gang's shared checkpoint
    subPath, naming the coordinator of the newest incarnation; the GPU gang is never rendered again. A gang without
    shared checkpoints, or without a finalizer, is refused explicitly."""
    import copy
    import json

    import catalog
    import execution
    from config import LABEL
    from test_api import MULTINODE_FLEET, H

    c, fake = client
    ns = "tenant-demo"
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    base = copy.deepcopy(catalog.get("distributed-run"))
    base["job"]["finalize"] = {
        "command": "python -m deliver --finish",
        "onExitCodes": [75],
        "timeoutS": 900,
        "cpu": "1",
        "memory": "2Gi",
    }
    monkeypatch.setattr(catalog, "get", lambda name, _b=base: _b if name == "distributed-run" else None)
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    fake.pvcs[(ns, jobs.SHARED_SCRATCH_PVC)] = {"metadata": {"name": jobs.SHARED_SCRATCH_PVC}}
    body = {
        "input": {
            "image": "x/y:1",
            "command": "mpirun gmx_mpi mdrun",
            "nodes": 2,
            "gpus_per_node": 8,
            "interconnect": "required",
            "checkpoints": "shared",
        },
        "execution": {"io_mode": "application"},
    }
    r = c.post("/v1/models/distributed-run:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    js = fake.custom[(ns, "jobsets", op)]
    assert js["spec"]["failurePolicy"]["rules"][0]["action"] == "FailJobSet"
    js["status"] = {"conditions": [{"type": "Failed", "status": "True", "message": "delivery pending"}]}
    coord, peer = f"{op}-workers-0-0-aaaaa", f"{op}-workers-0-1-bbbbb"
    rows = [
        {
            **_record(op, coord, "failed", epoch=1, exit_code=75, image_digest="x/y@sha256:0123", region="eu-north1"),
            "operation": op,
            "root": op,
            "gang_rank": 0,
            "gang_restart": 0,
            "io_mode": "application",
            "role": "run",
        },
        {
            **_record(op, peer, "failed", epoch=1, exit_code=143, region="eu-north1"),
            "operation": op,
            "root": op,
            "gang_rank": 1,
            "gang_restart": 0,
            "io_mode": "application",
            "role": "run",
        },
    ]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [x for x in rows if x["root"] == root])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["status"] == "FAILED" and got["attempt_id"] == coord, got
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = r.json()["id"]
    assert x1 == f"{op}-x1" and (ns, x1) in fake.jobs and (ns, "jobsets", x1) not in fake.custom  # a Job, not a gang
    j = fake.jobs[(ns, x1)]
    spec = j["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert main["command"] == ["/bin/sh", "-c", "python -m deliver --finish"] and main["image"] == "x/y@sha256:0123"
    assert "nvidia.com/gpu" not in json.dumps(main["resources"]) and main["resources"]["limits"]["cpu"] == "1"
    assert env["ATTEMPT_ROLE"] == "finalize" and env["FINALIZE_OF"] == coord and env["FINALIZE_EXIT_CODE"] == "75"
    assert env["FINALIZE_RANK"] == "0" and env["FINALIZE_GANG_RESTART"] == "0" and env["FINALIZE_NODES"] == "2"
    assert env["CHECKPOINT_DIR"] == "/work/checkpoint" and "NNODES" not in env and "MASTER_ADDR" not in env
    ck = next(v for v in spec["volumes"] if v["name"] == "checkpoints")
    assert ck["persistentVolumeClaim"]["claimName"] == jobs.SHARED_SCRATCH_PVC
    assert {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{op}"} in main[
        "volumeMounts"
    ]
    assert "nodeSelector" not in spec and "resourceClaims" not in spec and "subdomain" not in spec
    assert (
        j["metadata"]["annotations"][f"{LABEL}/finalize-of"] == coord
        and j["metadata"]["annotations"][f"{LABEL}/export-of"] == op
    )
    # the peer's exit is never the verdict: a gang whose coordinator succeeded is not finalized
    rows[0]["status"], rows[0]["exit_code"] = "succeeded", 0
    del fake.jobs[(ns, x1)]
    assert c.post(f"/v1/operations/{op}:export", headers=H).status_code == 409
    rows[0]["status"], rows[0]["exit_code"] = "failed", 75
    # a gang without shared checkpoints retains nothing; a managed-I/O gang has no finalizer
    js["metadata"]["annotations"][f"{LABEL}/checkpoints"] = "local"
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 409 and "checkpoints: shared" in r.text
    js["metadata"]["annotations"][f"{LABEL}/checkpoints"] = "shared"
    base["job"].pop("finalize")
    rows[0]["io_mode"] = None
    js["metadata"]["annotations"].pop(f"{LABEL}/execution", None)
    r = c.post(f"/v1/operations/{op}:export", headers=H)
    assert r.status_code == 409


def test_the_owner_sees_the_observed_gang_fields_on_the_current_attempt_and_every_attempt(client, monkeypatch):
    """The owner-scoped operation carries `pod_uid`, `gang_rank`, `gang_restart` on its current attempt (the
    coordinator's record) and on every attempts[] entry: from the record, and from the live pod's own UID and
    JobSet labels while it exists; the durable view after the Job is gone too. Another owner: 404, as before."""
    import copy

    import auth
    import catalog
    import execution
    from test_api import KEY_INFO, MULTINODE_FLEET, H

    c, fake = client
    ns = "tenant-demo"
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    base = copy.deepcopy(catalog.get("distributed-run"))
    monkeypatch.setattr(catalog, "get", lambda name, _b=base: _b if name == "distributed-run" else None)
    body = {
        "input": {
            "image": "x/y:1",
            "command": "mpirun",
            "nodes": 2,
            "gpus_per_node": 8,
            "interconnect": "required",
            "checkpoints": "shared",
        },
        "execution": {"io_mode": "application"},
    }
    r = c.post("/v1/models/distributed-run:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    coord, peer = f"{op}-workers-0-0-aaaaa", f"{op}-workers-0-1-bbbbb"
    # live pods of the gang, with the labels the Job and JobSet controllers stamp
    for name, rank, uid in ((coord, "0", "uid-c0"), (peer, "1", "uid-p1")):
        p = {
            "metadata": {
                "name": name,
                "namespace": ns,
                "uid": uid,
                "labels": {
                    "jobset.sigs.k8s.io/jobset-name": op,
                    "batch.kubernetes.io/job-completion-index": rank,
                    "jobset.sigs.k8s.io/restart-attempt": "1",
                    f"{kube.LABEL}/tenant": "demo",
                },
            },
            "spec": {"nodeName": f"node-{rank}"},
            "status": {
                "phase": "Running",
                "startTime": "2026-10-14T12:00:00Z",
                "containerStatuses": [
                    {
                        "name": "main",
                        "state": {"running": {"startedAt": "2026-10-14T12:00:01Z"}},
                        "imageID": "x/y@sha256:1111",
                    }
                ],
            },
        }
        fake.pods.append(p)
    rows = [
        {
            **_record(op, coord, "started", epoch=2, image_digest="x/y@sha256:1111", region="eu-north1"),
            "operation": op,
            "root": op,
            "gang_rank": 0,
            "gang_restart": 1,
            "pod_uid": "uid-c0",
            "io_mode": "application",
            "role": "run",
            "tenant": "demo",
            "namespace": ns,
            "mode": "run",
            "model": "distributed-run",
            "checkpoint_ref": "c" * 64,
            "checkpoint_generation": 3,
        },
        {
            **_record(op, peer, "started", epoch=2, image_digest="x/y@sha256:1111", region="eu-north1"),
            "operation": op,
            "root": op,
            "gang_rank": 1,
            "gang_restart": 1,
            "pod_uid": "uid-p1",
            "io_mode": "application",
            "role": "run",
            "tenant": "demo",
            "namespace": ns,
            "mode": "run",
            "model": "distributed-run",
        },
        {
            **_record(op, f"{op}-workers-0-0-zzzzz", "interrupted", epoch=1, region="eu-north1"),
            "operation": op,
            "root": op,
            "gang_rank": 0,
            "gang_restart": 0,
            "pod_uid": "uid-old",
            "io_mode": "application",
            "role": "run",
            "tenant": "demo",
            "namespace": ns,
            "mode": "run",
            "model": "distributed-run",
        },
    ]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [x for x in rows if x["root"] == root])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    got = c.get(f"/v1/operations/{op}", headers=H)
    assert got.status_code == 200, got.text
    o = got.json()
    assert (o["attempt_id"], o["epoch"], o["pod_uid"], o["gang_rank"], o["gang_restart"]) == (coord, 2, "uid-c0", 0, 1)
    assert o["checkpoint_ref"] == "c" * 64  # the coordinator's publication
    by = {a["attempt_id"]: a for a in o["attempts"]}
    assert (by[coord]["pod_uid"], by[coord]["gang_rank"], by[coord]["gang_restart"]) == ("uid-c0", 0, 1)
    assert (by[peer]["pod_uid"], by[peer]["gang_rank"], by[peer]["gang_restart"], by[peer]["epoch"]) == (
        "uid-p1",
        1,
        1,
        2,
    )
    old = by[f"{op}-workers-0-0-zzzzz"]
    assert (old["pod_uid"], old["gang_rank"], old["gang_restart"], old["epoch"]) == (
        "uid-old",
        0,
        0,
        1,
    )  # the prior incarnation, fenced
    # a live pod without a record yet still states what the API observes on it
    rows.pop(1)
    o = c.get(f"/v1/operations/{op}", headers=H).json()
    live = {a["attempt_id"]: a for a in o["attempts"]}[peer]
    assert (live["pod_uid"], live["gang_rank"], live["gang_restart"]) == ("uid-p1", 1, 1) and live.get("epoch") is None
    # the durable view once the JobSet is gone: the fields survive. The owner of an expired operation is proven by its
    # durable owner record (migration 9), which the fleet database holds in production: supplied here
    import db
    import keys

    owner = auth.Principal(key="sk-good", tenant="demo", info=copy.deepcopy(KEY_INFO)).owner
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(keys, "operation_owner", lambda operation: ("demo", owner, "distributed-run"))
    del fake.custom[(ns, "jobsets", op)]
    fake.pods[:] = [p for p in fake.pods if p["metadata"]["name"] not in (coord, peer)]
    o = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (o["pod_uid"], o["gang_rank"], o["gang_restart"]) == ("uid-c0", 0, 1)
    assert {a["attempt_id"]: (a["pod_uid"], a["gang_rank"]) for a in o["attempts"]}[coord] == ("uid-c0", 0)

    # another owner: not its operation
    async def info(key):
        if key == "sk-other":
            return {
                **copy.deepcopy(KEY_INFO),
                "metadata": {"tenant": "demo", "legacy": {"principal_id": "someone-else"}},
            }
        return copy.deepcopy(KEY_INFO)

    monkeypatch.setattr(auth, "key_info", info)
    assert c.get(f"/v1/operations/{op}", headers={"Authorization": "Bearer sk-other"}).status_code == 404
