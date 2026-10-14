"""Native execution leases at the API (leases.py, docs/API.md "Native execution leases"): the hook prepares one
execution per operation on the verified inference POST and forwards the verified markers (empty on a direct call); a
retry attempt's POST is refused while another attempt's execution lives; the worker routes verify the pod (faked
here) and are idempotent; a cancel is a request; the billing pass joins the caller's end with the worker's; the
database rules are in test_database.py."""

import asyncio
import copy

import pytest
from fastapi import HTTPException

import app as api
import attempts
import auth
import billing
import db
import leases
from test_api import KEY_INFO, H, L, admin_keys, client, cluster, fake_keys, pod  # noqa: F401
from test_async_private import _tenant

NS = "tenant-demo"


class FakeLeases:
    """leases.py in memory (the API's view); the database rules live in test_database.py."""

    def __init__(self):
        self.rows, self.calls = {}, []

    def get(self, op):
        return copy.deepcopy(self.rows.get(op))

    def prepare(self, op, attempt, region, model, caller_gone=None):
        self.calls.append(("prepare", op, attempt))
        cur = self.rows.get(op)
        if cur and cur["attempt"] != attempt and cur["state"] in ("prepared", "attached"):
            raise HTTPException(
                409, f"operation {op}: attempt {cur['attempt']} holds its native execution; {attempt} waits"
            )
        if cur is None or cur["attempt"] != attempt:
            self.rows[op] = {
                "operation": op,
                "attempt": attempt,
                "state": "prepared",
                "pod_uid": None,
                "worker_request_id": None,
                "region": region,
                "model": model,
                "prepared_at": None,
                "attached_at": None,
                "released_at": None,
                "outcome": None,
                "gpu_seconds": None,
                "lane_seconds": None,
                "cancel_requested_at": None,
                "cancel_reason": None,
                "settled_at": None,
            }
        return self.get(op)

    def attach(self, op, attempt, worker, worker_request_id):
        e = self.rows[op]
        if (
            e["state"] == "attached"
            and e["attempt"] == attempt
            and e["pod_uid"] == worker["uid"]
            and e["worker_request_id"] == worker_request_id
        ):
            return self.get(op)
        if e["state"] != "prepared" or e["attempt"] != attempt:
            raise HTTPException(409, f"operation {op}: native execution is {e['state']} for attempt {e['attempt']}")
        e.update(
            {"state": "attached", "pod_uid": worker["uid"], "worker_request_id": worker_request_id, "attached_at": "t1"}
        )
        return self.get(op)

    def history(self, op):
        return [copy.deepcopy(self.rows[op])] if op in self.rows else []

    def release(self, op, attempt, worker, worker_request_id, outcome, gpu_seconds):
        e = self.rows[op]
        same = e["attempt"] == attempt and e["pod_uid"] == worker["uid"] and e["worker_request_id"] == worker_request_id
        if e["state"] == "released" and same:
            return self.get(op)
        if e["state"] != "attached" or not same:
            raise HTTPException(
                409,
                f"operation {op}: native execution is {e['state']}; only its attached worker, with its request id, releases it",
            )
        e.update(
            {
                "state": "released",
                "outcome": outcome,
                "gpu_seconds": gpu_seconds,
                "lane_seconds": 30.0,
                "released_at": "t2",
            }
        )
        return self.get(op)

    def request_cancel(self, op, reason):
        e = self.rows.get(op)
        if e and e["state"] in ("prepared", "attached"):
            e["cancel_requested_at"], e["cancel_reason"] = "now", reason

    def mark_settled(self, op):
        if op in self.rows:
            self.rows[op]["settled_at"] = "now"


def _settle(e, required):
    if e is None or e["state"] == "prepared":
        return (True, None, "unattached") if required else (True, 0.0, "no native execution")
    if e["state"] == "attached":
        return False, None, "native execution attached: the worker has not settled"
    if e["state"] == "released":
        return True, e["gpu_seconds"], f"released ({e['outcome']})"
    return True, None, "worker lost"


@pytest.fixture
def live(client, admin_keys, fake_keys, monkeypatch):
    c, cl = client
    _tenant(monkeypatch)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    monkeypatch.setattr(api.kube, "stamp_last_request", lambda *a, **k: None)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    fl = FakeLeases()
    for name in ("get", "history", "prepare", "attach", "release", "request_cancel", "mark_settled"):
        monkeypatch.setattr(leases, name, getattr(fl, name))
    monkeypatch.setattr(leases, "settle_decision", lambda op, required: _settle(fl.rows.get(op), required))
    api._admission_cache.clear()

    async def info(key):
        base = {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": "p1"}}}
        return (
            {**base, "scopes": ["invoke:async", "read:operations", "run:control", "artifacts:write"]}
            if key == "sk-async"
            else dict(base)
        )

    monkeypatch.setattr(auth, "key_info", info)
    import keys

    A = {"Authorization": "Bearer sk-async"}
    data = {"request.json": "{}", "response_url": "https://objects.example/put/r", "result_ref": "app://r"}
    assert c.put("/v1/bindings/call-1", json={"data": data}, headers=A).status_code == 200
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-1"}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=A)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    token = cl.secrets[(NS, f"{oid}-auth")]["stringData"]["admitted"]
    assert token == api._admitted_token(keys.key_hash("sk-async"), "llm-example", oid)
    caller = pod(oid, "Running")
    caller["metadata"]["labels"]["serverless2.nebius/mode"] = "async"
    cl.pods.append(caller)
    attempt = caller["metadata"]["name"]
    hdr = {**A, "x-serverless2-admitted": token, "x-serverless2-operation": oid, "x-serverless2-attempt": attempt}
    return c, cl, fl, oid, attempt, hdr, A


def test_hook_prepares_once_forwards_verified_markers_and_fences_a_retry_attempt(live):
    c, cl, fl, oid, attempt, hdr, A = live
    # the readiness GET prepares nothing; the inference POST prepares the execution for this attempt and forwards the
    # verified markers towards the worker
    r = c.get("/internal/authorize/llm-example/v1/health/ready", headers=hdr)
    assert r.status_code == 200 and fl.calls == [] and r.headers["x-serverless2-operation"] == oid
    r = c.post("/internal/authorize/llm-example/openai/v1/chat/completions", headers=hdr)
    assert r.status_code == 200 and fl.calls == [("prepare", oid, attempt)]
    assert r.headers["x-serverless2-operation"] == oid and r.headers["x-serverless2-attempt"] == attempt
    assert fl.rows[oid]["state"] == "prepared"
    # the same attempt's POST again: its row stands (one prepare call more, same row)
    assert c.post("/internal/authorize/llm-example/openai/v1/chat/completions", headers=hdr).status_code == 200
    assert fl.rows[oid]["attempt"] == attempt
    # a retry pod of the same operation: its POST is refused while the first attempt's execution lives
    retry_pod = pod(oid, "Pending")
    retry_pod["metadata"]["name"] = f"{oid}-retry"
    retry_pod["status"]["phase"] = "Running"
    cl.pods.append(retry_pod)
    api._admission_cache.clear()
    r = c.post(
        "/internal/authorize/llm-example/openai/v1/chat/completions",
        headers={**hdr, "x-serverless2-attempt": f"{oid}-retry"},
    )
    assert r.status_code == 409 and "waits" in r.json()["detail"]
    # a direct call (no admission) forwards empty markers: whatever a client sent is overridden at the policy
    r = c.post(
        "/internal/authorize/llm-example/openai/v1/chat/completions",
        headers={**H, "x-serverless2-operation": "op-forged", "x-serverless2-attempt": "x"},
    )
    assert (
        r.status_code == 200 and r.headers["x-serverless2-operation"] == "" and r.headers["x-serverless2-attempt"] == ""
    )


def test_worker_attach_release_watch_and_cancel_request(live, monkeypatch):
    c, cl, fl, oid, attempt, hdr, A = live
    c.post("/internal/authorize/llm-example/openai/v1/chat/completions", headers=hdr)

    def verify(token, e, endpoint_model):
        if token == "bad":
            raise HTTPException(401, "worker token not accepted")
        return {"pod": "p-" + token, "namespace": "models", "uid": "uid-" + token, "region": e["region"]}

    monkeypatch.setattr(leases, "verify_worker", verify)
    W = {"Authorization": "Bearer w1"}
    assert (
        c.post(
            f"/internal/executions/{oid}:attach",
            json={"attempt": attempt, "worker_request_id": "r1"},
            headers={"Authorization": "Bearer bad"},
        ).status_code
        == 401
    )
    assert c.post("/internal/executions/op-unknown:attach", json={"attempt": attempt}, headers=W).status_code == 404
    r = c.post(
        f"/internal/executions/{oid}:attach", json={"attempt": "stale-attempt", "worker_request_id": "r1"}, headers=W
    )
    assert r.status_code == 409
    r = c.post(f"/internal/executions/{oid}:attach", json={"attempt": attempt, "worker_request_id": "r1"}, headers=W)
    assert r.status_code == 200 and r.json()["state"] == "attached" and r.json()["worker_request_id"] == "r1"
    assert r.json()["owner"] == {
        "tenant": "demo",
        "owner": fake_owner(c, oid),
        "model": "llm-example",
    }  # trusted owner context
    assert (
        c.post(
            f"/internal/executions/{oid}:attach", json={"attempt": attempt, "worker_request_id": "r1"}, headers=W
        ).status_code
        == 200
    )  # dropped response
    assert (
        c.post(
            f"/internal/executions/{oid}:attach",
            json={"attempt": attempt, "worker_request_id": "r1"},
            headers={"Authorization": "Bearer w2"},
        ).status_code
        == 409
    )  # another pod
    assert c.get(f"/internal/executions/{oid}", headers={"Authorization": "Bearer w2"}).status_code == 403
    assert c.get(f"/internal/executions/{oid}", headers=W).json()["cancel_requested"] is False
    # the handle shows the worker's side; a cancel is a request, the execution stays attached
    op = c.get(f"/v1/operations/{oid}", headers=A).json()
    assert op["native"]["state"] == "attached" and op["native"]["cancel_requested"] is False
    cl.jobs[(NS, oid)]["status"] = {"startTime": "2026-10-14T00:00:00Z"}
    assert c.post(f"/v1/operations/{oid}:cancel", headers=A).status_code == 200
    watched = c.get(f"/internal/executions/{oid}", headers=W).json()
    assert watched["cancel_requested"] is True and watched["cancel_reason"] == "cancelled by the owner"
    assert watched["owner"]["tenant"] == "demo" and watched["owner"]["model"] == "llm-example"
    # release: only the attached pod, after its settlement; unknown stays unknown; idempotent
    assert (
        c.post(
            f"/internal/executions/{oid}:release",
            json={"attempt": attempt, "worker_request_id": "r1", "outcome": "cancelled", "gpu_seconds": None},
            headers={"Authorization": "Bearer w2"},
        ).status_code
        == 409
    )
    assert (
        c.post(
            f"/internal/executions/{oid}:release",
            json={"attempt": attempt, "worker_request_id": "r9", "outcome": "cancelled"},
            headers=W,
        ).status_code
        == 409
    )  # the same pod with another request id: a stale release, refused
    assert (
        c.post(
            f"/internal/executions/{oid}:release", json={"attempt": "other", "outcome": "cancelled"}, headers=W
        ).status_code
        == 409
    )
    r = c.post(
        f"/internal/executions/{oid}:release",
        json={"attempt": attempt, "worker_request_id": "r1", "outcome": "cancelled", "gpu_seconds": None},
        headers=W,
    )
    assert (
        r.status_code == 200
        and r.json()["state"] == "released"
        and r.json()["gpu_seconds"] is None
        and r.json()["lane_seconds"] == 30.0
    )
    assert (
        c.post(
            f"/internal/executions/{oid}:release",
            json={"attempt": attempt, "worker_request_id": "r1", "outcome": "cancelled"},
            headers=W,
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/internal/executions/{oid}:release",
            json={"attempt": attempt, "worker_request_id": "r1", "outcome": "nope"},
            headers=W,
        ).status_code
        == 422
    )


def test_billing_joins_the_caller_end_with_the_worker_end(live, monkeypatch):
    """The caller Job is over: while the worker is attached the pass defers (nothing freed, whatever the caller did);
    once released the member is released with the worker's measurement (or unknown); required mode: a caller that
    ended unattached settles unknown; optional mode: as before."""
    c, cl, fl, oid, attempt, hdr, A = live
    import catalog

    c.post("/internal/authorize/llm-example/openai/v1/chat/completions", headers=hdr)
    fl.attach(oid, attempt, {"pod": "p", "namespace": "models", "uid": "uid-w"}, "r1")
    cl.jobs[(NS, oid)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}  # the caller died
    monkeypatch.setattr(billing.execution, "sweep_copies", lambda ns, op: None)
    monkeypatch.setattr(billing.artifacts, "attempt_records", lambda ns, op, region, prefix=None: [])
    monkeypatch.setattr(db, "enabled", lambda: True)
    assert asyncio.run(billing.bill_once()) == 0  # deferred: attached
    assert not [x for x in fake_keys_calls(fl, c) if x[0] == "release"]
    assert "serverless2.nebius/billed" not in cl.jobs[(NS, oid)]["metadata"]["annotations"]
    fl.release(oid, attempt, {"uid": "uid-w"}, "r1", "done", 42.0)
    assert asyncio.run(billing.bill_once()) == 1
    ann = cl.jobs[(NS, oid)]["metadata"]["annotations"]
    assert (
        ann["serverless2.nebius/billed"] == "0"
        and ann["serverless2.nebius/gpu-seconds"] == "42.0"
        and ann["serverless2.nebius/native"] == "released (done)"
    )
    assert fl.rows[oid]["settled_at"] == "now"
    assert asyncio.run(billing.bill_once()) == 0  # once
    # required mode, no attach: unknown
    model = catalog.get("llm-example")
    monkeypatch.setattr(
        catalog, "get", lambda mid: {**model, "native_lease": "required"} if mid == "llm-example" else None
    )
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=A)
    oid2 = r.json()["id"]
    cl.jobs[(NS, oid2)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    assert asyncio.run(billing.bill_once()) == 1
    ann = cl.jobs[(NS, oid2)]["metadata"]["annotations"]
    assert ann["serverless2.nebius/billed"] == "unknown" and ann["serverless2.nebius/native"] == "unattached"


def fake_owner(c, oid):
    import keys

    return keys.operation_owner(oid)[1]


def fake_keys_calls(fl, c):
    import keys

    return getattr(keys, "calls", [])
