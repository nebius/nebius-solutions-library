"""Endpoint children (docs/JOBS.md "Endpoint children"): a live attempt of a run with `child_endpoints` asks for a
bounded asynchronous endpoint child of its root. Against PostgreSQL (migrations 21/22, the key ledger) with the fake
cluster: the parent projection (no bearer anywhere), the child as a native application-I/O call (the bound request in
its private binding, the API as its refresh broker, the capture at the platform-owned destination), identity and the
immutable request (model, input or input_ref.sha256, timeout, region), replay/readback, the readback authoritative past
the Job's TTL (manifest, attempt record, presigned download), the lifecycle as a durable intent (crash between the row
and the Job, ambiguous creates, definite refusals), the cancel held open until the terminal settlement, the cascade for
cancelled/finished/gone parents, the ledger, the credential at the gateway hook, and the caller script under the child's
exact render: one POST, never a second one after a lost response."""

import hashlib
import json

import pytest
from fastapi import HTTPException

import app as api
import artifacts
import attempts
import catalog
import children
import db
import jobs
import keys
import kube
import models as models_mod
from config import LABEL
from test_api import FLEET, H, client, cluster, pod  # noqa: F401
from test_call_sh import _run
from test_database import LEGACY, database  # noqa: F401

NS = "tenant-demo"
RUN_CLASS = {
    "id": "lerobot-worker",
    "kind": "job",
    "image": "example.org/lerobot:1",
    "command": "python worker.py",
    "gpu": {"count": 0},
    "cpu": "2",
    "memory": "4Gi",
    "child_endpoints": ["cosmos-nano", "cosmos-big"],
    "child_max_units": 3,
}
PLAIN_CLASS = {**RUN_CLASS, "id": "plain-worker"}
del PLAIN_CLASS["child_endpoints"]
del PLAIN_CLASS["child_max_units"]
ENDPOINT = {
    "id": "cosmos-nano",
    "kind": "endpoint",
    "image": "example.org/cosmos:1",
    "port": 8000,
    "gpu": {"count": 1, "classes": ["h100"]},
    "timeout_s": 900,
}
ENDPOINT2 = {**ENDPOINT, "id": "cosmos-big", "image": "example.org/cosmos-big:1"}
BUCKET = "serverless2-demo-eu-north1"


def _entry(spec):
    kube.fleet = lambda: FLEET
    return catalog.normalise(models_mod.to_entry(spec, managed_by="api"))


@pytest.fixture
def rig(database, client, monkeypatch):
    c, fake = client
    db.migrate()
    h = keys.key_hash("sk-good")
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": h,
                "alias": "demo-user",
                "max_concurrent": 4,
                "budget_requests": 10,
                "budget_gpu_seconds": None,  # a finite GPU-second budget refuses endpoint calls (501): see below
                "rate_limit": None,
                "scopes": [
                    "invoke:sync",
                    "invoke:async",
                    "run:submit",
                    "run:control",
                    "read:operations",
                    "read:results",
                ],
            }
        ],
        "demo",
        "admin",
    )
    entries = {e["name"]: e for e in (_entry(RUN_CLASS), _entry(PLAIN_CLASS), _entry(ENDPOINT), _entry(ENDPOINT2))}
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    monkeypatch.setattr(catalog, "get", lambda name: entries.get(name))
    monkeypatch.setattr(
        catalog,
        "require",
        lambda name: entries[name] if name in entries else (_ for _ in ()).throw(HTTPException(404, name)),
    )
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    monkeypatch.setattr(api, "FLEET_MANAGER", False)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"hub": "eu-north1"}.get(cid, cid))
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    # the tenant bucket as a dict: the capture manifest the caller delivers, presigned URLs as plain names
    bucket: dict = {}
    monkeypatch.setattr(
        artifacts, "read_json", lambda ns, key, region="eu-north1", max_bytes=0, prefix=None: bucket.get(key)
    )
    monkeypatch.setattr(
        artifacts,
        "presign_download",
        lambda ns, uri, expires_s=3600, region=None: {
            "name": uri.rsplit("/", 1)[-1],
            "uri": uri,
            "url": f"https://objects.example/get/{uri.split('/', 3)[3]}",
        },
    )
    monkeypatch.setattr(
        artifacts,
        "presign_put",
        lambda ns, uri, expires_s, region="eu-north1": {
            "uri": uri,
            "url": f"https://objects.example/put/{uri.split('/', 3)[3]}",
            "method": "PUT",
            "expires_in_s": expires_s,
        },
    )
    r = c.post("/v1/models/lerobot-worker:invoke", json={"input": {}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    p = pod(op, "Running")
    p["metadata"]["uid"] = "uid-parent-1"
    fake.pods.append(p)
    identity = {"region": "eu-north1", "namespace": NS, "pod": p["metadata"]["name"], "uid": "uid-parent-1"}
    monkeypatch.setattr(children, "verify_parent_token", lambda token: dict(identity))
    fake.bucket = bucket
    fake.entries = entries
    return c, fake, op, identity


T = {"Authorization": "Bearer parent-pod-token"}


def _post(c, op, unit, inp=None, model="cosmos-nano", **extra):
    body = {"unit_id": unit, "model": model, **extra}
    if "input_ref" not in extra:
        body["input"] = inp if inp is not None else {"prompt": "x"}
    return c.post(f"/internal/operations/{op}/children", json=body, headers=T)


def _env(job):
    main = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    return {e["name"]: e.get("value") for e in main["env"]}, main


def _finish(fake, child):
    fake.jobs[(NS, child)]["status"] = {
        "conditions": [{"type": "Complete", "status": "True"}],
        "completionTime": "2026-10-14T10:00:00Z",
    }


def test_parent_render_projects_its_own_identity_and_no_bearer(rig):
    c, fake, op, _ = rig
    job = fake.jobs[(NS, op)]
    env, main = _env(job)
    spec = job["spec"]["template"]["spec"]
    assert env["S2_OPERATION"] == op and env["S2_PARENT_TOKEN_FILE"] == "/var/run/secrets/serverless2/token"
    assert (
        env["S2_CHILD_ENDPOINTS"] == "cosmos-big,cosmos-nano" and env["S2_CHILD_MAX_UNITS"] == "3" and env["S2_API_URL"]
    )
    vol = next(v for v in spec["volumes"] if v["name"] == "s2-parent")
    assert vol["projected"]["sources"][0]["serviceAccountToken"]["path"] == "token"
    assert {"name": "s2-parent", "mountPath": "/var/run/secrets/serverless2", "readOnly": True} in main["volumeMounts"]
    assert job["metadata"]["annotations"][f"{LABEL}/child-endpoints"] == "cosmos-big,cosmos-nano"
    blob = json.dumps(job)
    assert "sk-good" not in blob and "Bearer" not in blob and (NS, f"{op}-auth") not in fake.secrets  # no key, nowhere
    # a class without child_endpoints renders as before
    r = c.post("/v1/models/plain-worker:invoke", json={"input": {}}, headers=H)
    plain = fake.jobs[(NS, r.json()["id"])]
    assert "s2-parent" not in [v["name"] for v in plain["spec"]["template"]["spec"]["volumes"]]
    assert f"{LABEL}/child-endpoints" not in plain["metadata"]["annotations"]


def test_child_is_a_native_application_io_call_with_a_bound_request(rig):
    c, fake, op, identity = rig
    r = _post(c, op, "u1", {"prompt": "x", "n": 1})
    assert r.status_code == 201, r.text
    body = r.json()
    child = body["child"]
    req = children.bound_request("cosmos-nano", {"prompt": "x", "n": 1}, None, None, None)
    assert child == children.child_id(op, "u1") and body["state"] == "active" and body["status"] == "QUEUED"
    assert body["request_sha256"] == children.request_hash(req) and body["request"] == req
    assert body["result_ref"] == f"s3://{BUCKET}/operations/{child}/response.bin" and body["outcome"] == "pending"
    assert body["capture"] is None and body["model_image"] == "example.org/cosmos:1" and body["timeout_s"] == 900
    cjob = fake.jobs[(NS, child)]
    ann = cjob["metadata"]["annotations"]
    assert ann[f"{LABEL}/child-of"] == op and ann[f"{LABEL}/child-root"] == op and ann[f"{LABEL}/child-unit"] == "u1"
    assert ann[f"{LABEL}/key"] == keys.key_hash("sk-good")
    assert ann.get(f"{LABEL}/owner") == fake.jobs[(NS, op)]["metadata"]["annotations"].get(f"{LABEL}/owner")
    # the native application-I/O render: the request from the binding, one durable POST, a retained capture
    env, main = _env(cjob)
    assert (
        env["IO_MODE"] == "application" and env["READY_URL"] and env["PRIVATE_INPUT_DIR"] and env["OPERATION"] == child
    )
    assert "REQUEST_BODY" not in env and ann[f"{LABEL}/pvc"] == f"{child}-work"
    assert json.loads(ann[f"{LABEL}/execution"]) == {"io_mode": "application", "private_input_ref": child}
    assert "private-input" in [v["name"] for v in cjob["spec"]["template"]["spec"]["volumes"]]
    binding = fake.secrets[(NS, f"binding-{child}")]["data"]
    admitted = api._admitted_token(keys.key_hash("sk-good"), "cosmos-nano", child)
    assert json.loads(binding["request.json"]) == {"prompt": "x", "n": 1} and "input_sha256" not in binding
    assert binding["refresh_url"] == f"https://api.example/internal/operations/{child}:refresh"
    assert binding["refresh_token"] == admitted and binding["result_ref"] == body["result_ref"]
    assert (NS, f"{child}-private") in fake.secrets  # the copy the caller mounts (execution.deliver)
    secret = fake.secrets[(NS, f"{child}-auth")]["stringData"]
    assert (
        secret["authorization"] == f"Bearer {api.ADMITTED_BEARER_PREFIX}{admitted}" and secret["admitted"] == admitted
    )
    everything = json.dumps([cjob, fake.secrets[(NS, f"binding-{child}")], fake.secrets[(NS, f"{child}-auth")]])
    assert "sk-good" not in everything and "Bearer sk-" not in everything  # the key never travels
    # the ledger: a run member of the root, kind child, one request on the root's reservation, no GPU bound (an
    # endpoint call's GPU time is not metered per call; the key's policy refuses finite GPU budgets, see below)
    members = {m["operation"]: m for m in keys.run_members(op)}
    assert child in members and members[child]["kind"] == "child" and members[child]["stage"] == "u1"
    assert members[child]["gpu_bound"] == 0.0
    with db.pool().connection() as conn:
        res = conn.execute("select requests, gpu_seconds from key_reservations where operation = %s", (op,)).fetchone()
    assert res[0] == 2 and float(res[1]) == 0.0  # the root's request + one for the child
    # replay (same unit, same request): the existing child, nothing new
    r2 = _post(c, op, "u1", {"n": 1, "prompt": "x"})
    assert r2.status_code == 200 and r2.json()["child"] == child
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-c-")]) == 1
    # the same unit with a different request (input, model, timeout or region): refused
    for kw in ({"inp": {"prompt": "y"}}, {"model": "cosmos-big"}, {"timeout_s": 300}, {"region": "eu-north1"}):
        r3 = _post(c, op, "u1", kw.pop("inp", {"prompt": "x", "n": 1}), **kw)
        assert r3.status_code == 409 and "different request" in r3.text, (kw, r3.text)
    # readback
    g = c.get(f"/internal/operations/{op}/children/u1", headers=T)
    assert g.status_code == 200 and g.json()["child"] == child and g.json()["request_sha256"] == body["request_sha256"]
    assert c.get(f"/internal/operations/{op}/children/none", headers=T).status_code == 404
    # one open child per root
    r4 = _post(c, op, "u2")
    assert r4.status_code == 409 and "still active" in r4.text


def test_input_ref_binds_the_sha_and_the_refresh_broker_serves_renewable_urls(rig):
    c, fake, op, identity = rig
    sha = hashlib.sha256(b'{"video":"..."}').hexdigest()
    assert _post(c, op, "u1", input_ref={"sha256": sha}).status_code == 400  # needs transport.input_url
    assert _post(c, op, "u1", input_ref={"sha256": "nope"}, transport={"input_url": "https://x/y"}).status_code == 400
    assert _post(c, op, "u1", {"blob": "x" * 60000}).status_code == 400  # inline input above the binding's room
    r = _post(
        c,
        op,
        "u1",
        input_ref={"sha256": sha, "size_bytes": 15},
        transport={"input_url": "https://app.example/in/1?sig=a"},
    )
    assert r.status_code == 201, r.text
    child = r.json()["child"]
    assert r.json()["request"]["input_ref"] == {"sha256": sha, "size_bytes": 15} and "input" not in r.json()["request"]
    binding = fake.secrets[(NS, f"binding-{child}")]["data"]
    assert binding["input_sha256"] == sha and "request.json" not in binding
    assert "sig=a" not in json.dumps(fake.jobs[(NS, child)]) and "sig=a" not in json.dumps(
        binding
    )  # URLs: not in the Job, not in the binding
    # a replay with a renewed URL is the same bound request: the transport is renewed, nothing else
    r2 = _post(
        c,
        op,
        "u1",
        input_ref={"sha256": sha, "size_bytes": 15},
        transport={"input_url": "https://app.example/in/1?sig=b"},
    )
    assert r2.status_code == 200 and r2.json()["child"] == child
    # another sha for the same unit: a different request
    other = hashlib.sha256(b"other").hexdigest()
    assert (
        _post(c, op, "u1", input_ref={"sha256": other}, transport={"input_url": "https://app.example/in/2"}).status_code
        == 409
    )
    # the refresh broker: the child's own token AND a live attempt of it; the renewed input URL, fresh PUT URLs at the
    # platform-owned destination; a stale attempt with the valid token gets nothing
    caller = pod(child, "Running")
    fake.pods.append(caller)
    att = caller["metadata"]["name"]
    token = api._admitted_token(keys.key_hash("sk-good"), "cosmos-nano", child)
    hdr = {"Authorization": f"Bearer {token}"}
    rin = c.post(
        f"/internal/operations/{child}:refresh",
        json={"operation_id": child, "attempt_id": att, "phase": "input"},
        headers=hdr,
    )
    assert rin.status_code == 200 and rin.json() == {"input_url": "https://app.example/in/1?sig=b"}, rin.text
    rout = c.post(
        f"/internal/operations/{child}:refresh",
        json={"operation_id": child, "attempt_id": att, "phase": "output"},
        headers=hdr,
    )
    assert rout.status_code == 200, rout.text
    assert rout.json()["response_url"] == f"https://objects.example/put/operations/{child}/response.bin"
    assert rout.json()["call_url"] == f"https://objects.example/put/operations/{child}/call.json"
    stale = {"operation_id": child, "attempt_id": f"{child}-stale", "phase": "output"}
    assert c.post(f"/internal/operations/{child}:refresh", json=stale, headers=hdr).status_code == 403
    assert (
        c.post(f"/internal/operations/{child}:refresh", json={**stale, "attempt_id": ""}, headers=hdr).status_code
        == 403
    )
    # forged, another model's, or a dead child's token: refused
    assert (
        c.post(
            f"/internal/operations/{child}:refresh",
            json={"phase": "output"},
            headers={"Authorization": "Bearer forged"},
        ).status_code
        == 401
    )
    wrong = api._admitted_token(keys.key_hash("sk-good"), "cosmos-big", child)
    assert (
        c.post(
            f"/internal/operations/{child}:refresh",
            json={"phase": "output"},
            headers={"Authorization": f"Bearer {wrong}"},
        ).status_code
        == 401
    )
    assert c.post(f"/internal/operations/{child}:refresh", json={"phase": "output"}).status_code == 401
    children._end(op, "u1", "test")
    assert c.post(f"/internal/operations/{child}:refresh", json={"phase": "output"}, headers=hdr).status_code == 409


def test_readback_is_authoritative_past_the_job_ttl(rig, monkeypatch):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    del fake.jobs[(NS, child)]  # the Job expired (TTL): the row, the records and the destination remain
    rb = children.readback(NS, op, "u1")
    assert rb["status"] == "UNKNOWN" and rb["outcome"] == "pending" and rb["capture"] is None
    assert rb["result_ref"] == f"s3://{BUCKET}/operations/{child}/response.bin"
    # the attempt record outlives the Job: status from it; over without a delivered capture = undelivered (:export)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    attempts.record(
        "eu-north1",
        NS,
        f"{child}-abc",
        {"operation": child, "status": "started", "root": child, "role": "run", "cluster": "hub"},
    )
    attempts.record(
        "eu-north1",
        NS,
        f"{child}-abc",
        {
            "operation": child,
            "status": "failed",
            "root": child,
            "role": "run",
            "cluster": "hub",
            "export": "failed",
            "exit_code": 4,
        },
    )
    rb = children.readback(NS, op, "u1")
    assert rb["status"] == "FAILED" and rb["outcome"] == "unknown" and rb["attempt"]["pod"] == f"{child}-abc"
    assert rb["attempt"]["export_state"] == "failed" and rb["exportable"] is False  # exit 4: no capture to export
    # a later attempt captured (exit 0) and failed only its delivery: undelivered, exportable by the owner's :export
    attempts.record(
        "eu-north1",
        NS,
        f"{child}-def",
        {"operation": child, "status": "started", "root": child, "role": "run", "cluster": "hub"},
    )
    attempts.record(
        "eu-north1",
        NS,
        f"{child}-def",
        {
            "operation": child,
            "status": "failed",
            "root": child,
            "role": "run",
            "cluster": "hub",
            "export": "failed",
            "exit_code": 0,
        },
    )
    rb = children.readback(NS, op, "u1")
    assert rb["outcome"] == "undelivered" and rb["exportable"] is True and rb["attempt"]["pod"] == f"{child}-def"
    # the manifest at the destination is the authority: media type, sha256, size, HTTP status, producing attempt
    fake.bucket[f"operations/{child}/call.json"] = {
        "operation_id": child,
        "attempt_id": f"{child}-def",
        "http_status": 200,
        "content_type": "video/mp4",
        "size_bytes": 4096,
        "sha256": "ab" * 32,
        "timings": {"posted_at": "2026-10-14T10:00:00Z"},
        "capture_complete": True,
    }
    rb = children.readback(NS, op, "u1")
    cap = rb["capture"]
    assert rb["outcome"] == "succeeded" and cap["delivered"] and cap["sha256"] == "ab" * 32
    assert cap["content_type"] == "video/mp4" and cap["size_bytes"] == 4096 and cap["http_status"] == 200
    assert (
        cap["uri"] == rb["result_ref"]
        and cap["download_url"] == f"https://objects.example/get/operations/{child}/response.bin"
    )
    assert cap["attempt_id"] == f"{child}-def" and cap["download_expires_in_s"] == 3600
    g = c.get(f"/internal/operations/{op}/children/u1", headers=T)
    assert g.status_code == 200 and g.json()["capture"]["sha256"] == "ab" * 32  # the parent's readback, token only
    # a captured endpoint error is delivered as is and never a success; an incomplete manifest is no capture
    fake.bucket[f"operations/{child}/call.json"]["http_status"] = 500
    assert children.readback(NS, op, "u1")["outcome"] == "failed"
    fake.bucket[f"operations/{child}/call.json"] = {"capture_complete": False, "outcome": "unknown"}
    assert children.readback(NS, op, "u1")["capture"] is None
    # ended without a delivered capture: failed, with the recorded reason
    children._end(op, "u1", "cancelled: test")
    rb = children.readback(NS, op, "u1")
    assert rb["state"] == "ended" and rb["outcome"] == "undelivered" and rb["reason"] == "cancelled: test"
    # ended with no capture and no attempt that kept one: failed, with the recorded reason
    keys.release(child, 0.0)
    child2 = _post(c, op, "u2", {"q": 2}).json()["child"]
    del fake.jobs[(NS, child2)]
    children._end(op, "u2", "refused: test")
    rb = children.readback(NS, op, "u2")
    assert (
        rb["state"] == "ended"
        and rb["outcome"] == "failed"
        and rb["reason"] == "refused: test"
        and rb["exportable"] is False
    )


def _pending_row(op, unit, child, inp=None):
    """A row as create() writes it, without the completion: an API process that died right after the insert."""
    req = children.bound_request("cosmos-nano", inp or {"prompt": "x"}, None, None, None)
    with db.pool().connection() as conn, conn.transaction():
        conn.execute(
            "insert into endpoint_children (root, unit_id, child, parent, parent_pod, parent_pod_uid, model, request_sha256, state, namespace, region, timeout_s, request, transport, model_image, binding_ref) values (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s)",
            (
                op,
                unit,
                child,
                op,
                f"{op}-run",
                "uid-parent-1",
                "cosmos-nano",
                children.request_hash(req),
                NS,
                "eu-north1",
                900,
                json.dumps(req),
                "{}",
                "example.org/cosmos:1",
                child,
            ),
        )


def test_a_crash_between_the_intent_and_the_job_is_finished_deterministically(rig):
    c, fake, op, identity = rig
    child = children.child_id(op, "u1")
    _pending_row(op, "u1", child)
    assert children.readback(NS, op, "u1")["status"] == "PENDING"
    assert _post(c, op, "u2").status_code == 409  # the pending intent holds the one-open-child slot
    # the sweep finishes it from the row alone: the member, the binding, the Job, then active
    assert children.sweep() == [f"{child}:active"]
    assert (
        (NS, child) in fake.jobs
        and (NS, f"binding-{child}") in fake.secrets
        and keys.member_of(child)["kind"] == "child"
    )
    assert children.readback(NS, op, "u1")["state"] == "active" and children.sweep() == []
    # a replay of the same request on a pending intent finishes it synchronously and reads it back (200)
    child2 = children.child_id(op, "u2")
    children._end(op, "u1", "test")
    keys.release(child, 0.0)
    _pending_row(op, "u2", child2, {"q": 2})
    r = _post(c, op, "u2", {"q": 2})
    assert r.status_code == 200 and r.json()["state"] == "active" and (NS, child2) in fake.jobs
    assert (
        len([k for k in fake.jobs if k[1] == child2]) == 1
        and len([m for m in keys.run_members(op) if m["operation"] == child2]) == 1
    )


def test_an_ambiguous_create_keeps_the_intent_and_never_duplicates(rig, monkeypatch):
    c, fake, op, identity = rig
    real = jobs.create

    def created_but_lost(ns, job, region="eu-north1", secret=None):
        real(ns, job, region, secret)
        raise OSError("connection reset after the create")  # the Job exists, the answer was lost

    monkeypatch.setattr(jobs, "create", created_but_lost)
    r = _post(c, op, "u1")
    assert r.status_code == 503 and "did not confirm" in r.text and "replay" in r.text, r.text
    child = children.child_id(op, "u1")
    assert children.readback(NS, op, "u1")["state"] == "pending" and (NS, child) in fake.jobs
    assert keys.member_of(child)["state"] == "active"
    monkeypatch.setattr(jobs, "create", real)
    assert children.sweep() == [f"{child}:active"]  # found, not created again
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-c-")]) == 1
    assert len([m for m in keys.run_members(op) if m["operation"] == child]) == 1
    with db.pool().connection() as conn:
        assert conn.execute("select requests from key_reservations where operation = %s", (op,)).fetchone()[0] == 2
    # the other ambiguity: the API server answered 5xx, nothing created; the replay creates it (one Job, one member)
    children._end(op, "u1", "test")
    keys.release(child, 0.0)
    monkeypatch.setattr(
        jobs,
        "create",
        lambda ns, job, region="eu-north1", secret=None: (_ for _ in ()).throw(HTTPException(502, "kubernetes: 503")),
    )
    r = _post(c, op, "u2")
    child2 = children.child_id(op, "u2")
    assert (
        r.status_code == 503 and (NS, child2) not in fake.jobs and children.readback(NS, op, "u2")["state"] == "pending"
    )
    monkeypatch.setattr(jobs, "create", real)
    r = _post(c, op, "u2")
    assert r.status_code == 200 and (NS, child2) in fake.jobs and children.readback(NS, op, "u2")["state"] == "active"
    assert len([m for m in keys.run_members(op) if m["operation"] == child2]) == 1


def test_definite_refusals_roll_the_intent_back(rig, monkeypatch):
    c, fake, op, identity = rig
    real = jobs.create
    # the API server refused the create (4xx): no Job exists; the member and its request leave the ledger, no row
    monkeypatch.setattr(
        jobs,
        "create",
        lambda ns, job, region="eu-north1", secret=None: (_ for _ in ()).throw(
            HTTPException(404, "tenant namespace is not onboarded")
        ),
    )
    r = _post(c, op, "u1")
    assert r.status_code == 404 and "not onboarded" in r.text
    child = children.child_id(op, "u1")
    assert children.readback(NS, op, "u1") is None and keys.member_of(child) is None
    with db.pool().connection() as conn:
        assert conn.execute("select requests from key_reservations where operation = %s", (op,)).fetchone()[0] == 1
    monkeypatch.setattr(jobs, "create", real)
    # the ledger's refusal: the key's request budget is exhausted (402): nothing recorded
    with db.pool().connection() as conn:
        conn.execute("update keys set budget_requests = 1 where hash = %s", (keys.key_hash("sk-good"),))
    r = _post(c, op, "u1")
    assert r.status_code == 402 and children.readback(NS, op, "u1") is None and keys.member_of(child) is None
    with db.pool().connection() as conn:
        conn.execute("update keys set budget_requests = 10 where hash = %s", (keys.key_hash("sk-good"),))
    # a pending intent whose child model changed its image: not completed against another image (the sweep ends
    # it with the reason; synchronously the replay is refused)
    _pending_row(op, "u1", child)
    fake.entries["cosmos-nano"]["runtime"]["image"] = "example.org/cosmos:2"
    r = _post(c, op, "u1")
    assert r.status_code == 409 and "changed its image" in r.text and children.readback(NS, op, "u1") is None
    _pending_row(op, "u1", child)
    assert children.sweep() == [f"{child}:ended"]
    rb = children.readback(NS, op, "u1")
    assert rb["state"] == "ended" and "changed its image" in rb["reason"] and keys.member_of(child) is None
    fake.entries["cosmos-nano"]["runtime"]["image"] = "example.org/cosmos:1"
    # a pending intent whose parent is over before anything was created: ended with the cause, nothing created
    children._drop(op, "u2")
    child2 = children.child_id(op, "u2")
    _pending_row(op, "u2", child2)
    fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/cancelled"] = "true"
    assert children.sweep() == [f"{child2}:ended"]
    assert (NS, child2) not in fake.jobs and "before the child was created" in children.readback(NS, op, "u2")["reason"]


def test_a_cancel_is_held_open_until_the_terminal_settlement(rig):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    cjob = fake.jobs[(NS, child)]
    cjob["status"] = {"startTime": "2026-10-14T10:00:00Z", "active": 1}  # started: the caller may be mid-inference
    caller = pod(child, "Running")
    fake.pods.append(caller)
    assert children.cancel_for_root(NS, op) == [child]
    rb = children.readback(NS, op, "u1")
    assert rb["state"] == "active" and rb["cancel_requested_at"] and rb["reason"].startswith("cancelled:")
    assert fake.jobs[(NS, child)]["spec"].get("activeDeadlineSeconds") == 1  # the cancel was issued to the Job
    # no next child while the old caller or worker may still be draining; the sweep issues nothing twice
    r = _post(c, op, "u2")
    assert r.status_code == 409 and "still active" in r.text
    assert children.sweep() == [] and children.cancel_for_root(NS, op) == []
    # the terminal settlement (billing's release, joined with the worker's lease) ends it; the next child may start
    keys.release(child, 0.0)
    assert children.sweep() == [f"{child}:ended"]
    rb = children.readback(NS, op, "u1")
    assert rb["state"] == "ended" and rb["reason"].startswith("cancelled:")
    fake.jobs[(NS, op)]["metadata"]["annotations"].pop(f"{LABEL}/cancelled", None)
    assert _post(c, op, "u2").status_code == 201
    # a never-started child (queued) is deleted by its cancel: nothing ran, nothing attached: settled at once
    child2 = children.child_id(op, "u2")
    assert children.cancel_for_root(NS, op) == [child2]
    assert (NS, child2) not in fake.jobs and children.readback(NS, op, "u2")["state"] == "ended"
    assert keys.member_of(child2)["state"] != "active"


def test_the_sweep_cascades_for_cancelled_finished_or_gone_parents_only(rig):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    assert children.sweep() == []  # active, root alive: nothing
    # a finished parent WITH a live successor (a resume): the successor reads the child back, nothing is cancelled
    parent = fake.jobs[(NS, op)]
    parent["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    succ = {**json.loads(json.dumps(parent)), "status": {}}
    succ["metadata"]["name"] = f"{op}-r1"
    succ["metadata"]["annotations"][f"{LABEL}/resumed-from"] = op
    fake.jobs[(NS, f"{op}-r1")] = succ
    parent["metadata"]["annotations"][f"{LABEL}/successor"] = f"{op}-r1"
    assert children.sweep() == []
    # the successor finishes without one of its own: the cascade
    succ["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    out = children.sweep()
    assert out == [f"{child}:cancelled"]
    rb = children.readback(NS, op, "u1")
    assert "finished without a successor" in rb["reason"]
    assert rb["state"] == "ended" or rb["cancel_requested_at"]  # never-started: ended at once; started: held open
    # the parent gone (no Job, no attempt record): cancelled as well
    children._drop(op, "u2")
    parent["metadata"]["annotations"].pop(f"{LABEL}/successor")
    parent["status"] = {}
    r = _post(c, op, "u2")
    assert r.status_code == 201, r.text
    child2 = r.json()["child"]
    fake.jobs[(NS, child2)]["status"] = {"startTime": "2026-10-14T10:00:00Z"}
    del fake.jobs[(NS, op)]
    assert children.sweep() == [f"{child2}:cancelled"]
    assert "gone" in children.readback(NS, op, "u2")["reason"]


def test_the_frozen_allowlist_and_the_other_fences(rig, monkeypatch):
    c, fake, op, identity = rig
    assert _post(c, op, "u1", model="lerobot-worker").status_code == 403  # not an allowlisted endpoint
    assert _post(c, op, "u1", model="unknown-model").status_code == 403
    assert _post(c, op, "bad unit!").status_code == 400
    assert (
        c.post(
            f"/internal/operations/{op}/children", json={"unit_id": "u1", "model": "cosmos-nano", "input": {}}
        ).status_code
        == 401
    )
    # the allow-list the parent was RENDERED with governs, not the class as it is now
    fake.entries["lerobot-worker"]["job"]["childEndpoints"] = ["cosmos-nano", "cosmos-big", "other-ep"]
    fake.entries["other-ep"] = {**fake.entries["cosmos-nano"], "name": "other-ep"}
    assert _post(c, op, "u1", model="other-ep").status_code == 403
    # a run class without child endpoints
    plain = c.post("/v1/models/plain-worker:invoke", json={"input": {}}, headers=H).json()["id"]
    pp = pod(plain, "Running")
    pp["metadata"]["uid"] = "uid-plain"
    fake.pods.append(pp)
    monkeypatch.setattr(
        children,
        "verify_parent_token",
        lambda token: {"region": "eu-north1", "namespace": NS, "pod": pp["metadata"]["name"], "uid": "uid-plain"},
    )
    assert _post(c, plain, "u1").status_code == 403
    # a stale attempt (another pod UID claims to be the live attempt)
    monkeypatch.setattr(children, "verify_parent_token", lambda token: {**identity, "uid": "uid-old"})
    r = _post(c, op, "u1")
    assert r.status_code == 409 and "not a live attempt" in r.text
    monkeypatch.setattr(children, "verify_parent_token", lambda token: dict(identity))
    # the bound: three units, the fourth refused; a child must settle before the next starts
    for i in (1, 2, 3):
        r = _post(c, op, f"u{i}", {"i": i})
        assert r.status_code == 201, r.text
        ch = r.json()["child"]
        _finish(fake, ch)
        assert children.sweep() == []  # finished, not yet settled: still open
        assert _post(c, op, f"u{i + 1}", {"i": i + 1}).status_code == 409
        keys.release(ch, 0.0)
        assert children.sweep() == [f"{ch}:ended"]
    r = _post(c, op, "u4", {"i": 4})
    assert r.status_code == 409 and "3 children already" in r.text
    # a cancelled parent: nothing attaches
    fake.jobs[(NS, op)]["metadata"]["annotations"][f"{LABEL}/cancelled"] = "true"
    assert _post(c, op, "u9").status_code == 409
    # a closed root (its reservation settled): the ledger refuses, nothing stays recorded
    fake.jobs[(NS, op)]["metadata"]["annotations"].pop(f"{LABEL}/cancelled")
    monkeypatch.setattr(
        children,
        "_live_parent",
        lambda identity, parent: (fake.jobs[(NS, op)], "eu-north1", "op-closed-root", catalog.get("lerobot-worker")),
    )
    with pytest.raises(HTTPException) as e:
        children.create(identity, op, {"unit_id": "z1", "model": "cosmos-nano", "input": {}})
    assert e.value.status_code == 409 and "closed" in e.value.detail
    assert children.readback(NS, "op-closed-root", "z1") is None


def test_credential_is_accepted_at_the_hook_only_while_the_child_lives(rig, monkeypatch):
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    secret = fake.secrets[(NS, f"{child}-auth")]["stringData"]
    caller = pod(child, "Running")
    caller["metadata"]["labels"][f"{LABEL}/mode"] = "async"
    fake.pods.append(caller)
    api._admission_cache.clear()
    hdr = {
        "Authorization": secret["authorization"],
        "x-serverless2-admitted": secret["admitted"],
        "x-serverless2-operation": child,
        "x-serverless2-attempt": caller["metadata"]["name"],
    }
    monkeypatch.setattr(api, "_endpoint_model", lambda eid: catalog.get("cosmos-nano"))
    import leases

    prepared = []
    monkeypatch.setattr(
        leases,
        "prepare",
        lambda operation, attempt, region, model, caller_gone=None: prepared.append((operation, attempt)),
    )
    r = c.get("/internal/authorize/cosmos-nano/v1/health/ready", headers=hdr)
    assert r.status_code == 200 and r.headers["x-serverless2-operation"] == child, r.text
    r = c.post("/internal/authorize/cosmos-nano/generate", headers=hdr)
    assert r.status_code == 200 and r.headers["x-serverless2-attempt"] == caller["metadata"]["name"]
    assert prepared == [(child, caller["metadata"]["name"])]
    # the same credential for another model, or forged, or without the operation: refused
    assert c.post("/internal/authorize/lerobot-worker/generate", headers=hdr).status_code in (401, 403, 404)
    assert (
        c.post(
            "/internal/authorize/cosmos-nano/generate",
            headers={**hdr, "Authorization": f"Bearer {api.ADMITTED_BEARER_PREFIX}forged"},
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/internal/authorize/cosmos-nano/generate",
            headers={k: v for k, v in hdr.items() if k != "x-serverless2-operation"},
        ).status_code
        == 401
    )
    # the root cancelled: the cascade issues the child's cancel; the credential dies with the reservation
    r = c.post(f"/v1/operations/{op}:cancel", headers=H)
    assert r.status_code == 200, r.text
    assert children.readback(NS, op, "u1")["cancel_requested_at"] or children.readback(NS, op, "u1")["state"] == "ended"
    api._admission_cache.clear()
    assert c.post("/internal/authorize/cosmos-nano/generate", headers=hdr).status_code != 200  # no live caller
    keys.release(child, 0.0)  # billing settles the child's member, then the root: no reservation left
    keys.release(op, 0.0)
    assert keys.reservation_key(op) is None and keys.member_of(child)["state"] == "settled"
    api._admission_cache.clear()
    assert c.post("/internal/authorize/cosmos-nano/generate", headers=hdr).status_code == 401


def test_a_key_with_a_finite_gpu_budget_gets_the_endpoint_call_policy(rig):
    """The child is an endpoint call of the root's key: the same 501 as a direct `:invoke` of the endpoint (shared
    endpoint GPU time is not metered per call), decided before any row, member or Job exists."""
    c, fake, op, _ = rig
    with db.pool().connection() as conn:
        conn.execute("update keys set budget_gpu_seconds = 100000 where hash = %s", (keys.key_hash("sk-good"),))
    r = _post(c, op, "u1")
    assert r.status_code == 501 and "GPU-second" in r.text, r.text
    assert children.readback(NS, op, "u1") is None and all(m["kind"] != "child" for m in keys.run_members(op))
    assert (NS, children.child_id(op, "u1")) not in fake.jobs


def _caller_env_and_binding(fake, child):
    """The caller script's environment exactly as the child's render gives it (main's env, the auth Secret) and the
    private binding exactly as the API wrote it."""
    env, _ = _env(fake.jobs[(NS, child)])
    secret = fake.secrets[(NS, f"{child}-auth")]["stringData"]
    e = {k: env[k] for k in ("IO_MODE", "URL", "READY_URL", "OPERATION", "CALL_TIMEOUT") if env.get(k)}
    e.update({"AUTH_HEADER": secret["authorization"], "ADMITTED": secret["admitted"], "POD_NAME": f"{child}-abc"})
    return e, dict(fake.secrets[(NS, f"binding-{child}")]["data"])


def test_the_child_caller_posts_once_and_never_again_after_a_lost_response(rig, tmp_path):
    """services/jobs/bin/call.sh under the child's exact render and binding: readiness, ONE inference POST, the
    response bytes (a binary body) captured with their sha256 and delivered to the URLs the API's refresh broker
    serves; a lost response is an unknown outcome and the retried pod never posts again."""
    c, fake, op, identity = rig
    child = _post(c, op, "u1", {"prompt": "x"}).json()["child"]
    env, binding = _caller_env_and_binding(fake, child)
    assert env["IO_MODE"] == "application" and binding["refresh_url"].endswith(f"/internal/operations/{child}:refresh")
    url, ready = env["URL"], env["READY_URL"]
    body = "MP4-bytes-not-json:\x7f\u00ff"  # not JSON; the stub's script file must stay text (no NUL)
    refresh = json.dumps(
        {"response_url": "https://objects.example/put/response", "call_url": "https://objects.example/put/call"}
    )
    script = [
        f"POST {binding['refresh_url']} 200 {{}}",  # the input phase: inline request, nothing to fetch
        f"GET {ready} 200 ok",
        f"POST {url} 200 {body}",
        f"POST {binding['refresh_url']} 200 {refresh}",
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(
        tmp_path / "ok", binding, script, env={**env, "READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"}
    )
    assert r.returncode == 0, r.stdout + r.stderr
    posts = [x for x in calls if x.startswith(f"POST {url}")]
    assert len(posts) == 1 and posts[0].endswith(" @request.bin")  # the bound request from the binding, posted once
    assert json.loads((work / "request.bin").read_text()) == {"prompt": "x"}
    call = json.loads((work / "call.json").read_text())
    assert call["capture_complete"] and call["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert call["http_status"] == 200 and call["size_bytes"] == len(body.encode())
    result = json.loads((work / "RESULT.json").read_text())
    assert result == {"result_ref": binding["result_ref"], "export": "succeeded", "capture_attempt_id": f"{child}-abc"}
    assert (work / "response.bin").read_bytes() == body.encode()  # bytes, never reinterpreted
    # a lost response: unknown, nothing delivered; the retried pod of the same operation never posts again
    script = [f"POST {binding['refresh_url']} 200 {{}}", f"GET {ready} 200 ok", f"POST {url} 000"]
    r, work, calls = _run(
        tmp_path / "lost", binding, script, env={**env, "READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"}
    )
    assert r.returncode == 4 and [x.split()[0] for x in calls] == ["POST", "GET", "POST"]  # broker, probe, ONE post
    assert json.loads((work / "call.json").read_text())["outcome"] == "unknown"
    assert json.loads((work / "RESULT.json").read_text())["export"] == "failed"
    script = [f"POST {binding['refresh_url']} 200 {{}}", f"GET {ready} 200 ok", f"POST {url} 200 {body}"]
    r2, work2, calls2 = _run(
        tmp_path / "lost",
        binding,
        script,
        env={**env, "READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"},
        work_files={"posted.json": (work / "posted.json").read_text()},
    )
    assert r2.returncode == 4 and "not posting again" in r2.stdout
    assert not [x for x in calls2 if x.startswith(f"POST {url}")] and not [x for x in calls2 if x.startswith("GET")]
    # a transient 5xx/408 is not retried either: the response is a capture and is delivered as is
    script = [
        f"POST {binding['refresh_url']} 200 {{}}",
        f"GET {ready} 200 ok",
        f"POST {url} 503 busy",
        f"POST {binding['refresh_url']} 200 {refresh}",
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r3, work3, calls3 = _run(
        tmp_path / "err", binding, script, env={**env, "READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"}
    )
    assert r3.returncode == 5 and len([x for x in calls3 if x.startswith(f"POST {url}")]) == 1
    assert json.loads((work3 / "call.json").read_text())["http_status"] == 503


def test_the_child_caller_refuses_an_input_that_is_not_the_bound_one(rig, tmp_path):
    """input_ref: the caller fetches the request through the renewable URL the broker serves and verifies its sha256
    against the bound one before the single POST; other bytes mean no POST (the parent's error)."""
    c, fake, op, identity = rig
    payload = b'{"video":"ref","seed":1}'
    sha = hashlib.sha256(payload).hexdigest()
    child = _post(c, op, "u1", input_ref={"sha256": sha}, transport={"input_url": "https://app.example/in/1"}).json()[
        "child"
    ]
    env, binding = _caller_env_and_binding(fake, child)
    assert binding["input_sha256"] == sha and "request.json" not in binding
    url, ready = env["URL"], env["READY_URL"]
    q = {"READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"}
    refresh_in = json.dumps({"input_url": "https://app.example/in/1"})
    refresh_out = json.dumps(
        {"response_url": "https://objects.example/put/response", "call_url": "https://objects.example/put/call"}
    )
    script = [
        f"POST {binding['refresh_url']} 200 {refresh_in}",
        f"GET https://app.example/in/1 200 {payload.decode()}",
        f"GET {ready} 200 ok",
        f"POST {url} 200 out",
        f"POST {binding['refresh_url']} 200 {refresh_out}",
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(tmp_path / "ok", binding, script, env={**env, **q})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        len([x for x in calls if x.startswith(f"POST {url}")]) == 1 and (work / "request.bin").read_bytes() == payload
    )
    script = [
        f"POST {binding['refresh_url']} 200 {refresh_in}",
        'GET https://app.example/in/1 200 {"video":"other"}',
        f"GET {ready} 200 ok",
        f"POST {url} 200 out",
    ]
    r, work, calls = _run(tmp_path / "bad", binding, script, env={**env, **q})
    assert r.returncode == 2 and not [x for x in calls if x.startswith(f"POST {url}")]
    assert "bound input" in r.stdout and json.loads((work / "RESULT.json").read_text())["export"] == "failed"


def test_a_child_export_retry_follows_the_child_lineage_and_the_broker_serves_it(rig, monkeypatch):
    """The owner's `:export` of a child whose capture is complete and undelivered: the transfer-only retry
    `<child>-x1` is admitted as a `child` member after the child's own head inside the parent run's ledger (no second
    request, nothing inferred again: TRANSFER_ONLY), and the refresh broker serves the retry's live attempt with the
    child's token after the original settled; the original's own id is then refused, a foreign id is refused."""
    c, fake, op, identity = rig
    child = _post(c, op, "u1").json()["child"]
    token = api._admitted_token(keys.key_hash("sk-good"), "cosmos-nano", child)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    rec = {"operation": child, "root": child, "role": "run", "cluster": "hub"}
    attempts.record("eu-north1", NS, f"{child}-abc", {**rec, "status": "started"})
    attempts.record("eu-north1", NS, f"{child}-abc", {**rec, "status": "failed", "export": "failed", "exit_code": 0})
    fake.jobs[(NS, child)]["status"] = {
        "startTime": "2026-10-14T10:00:00Z",
        "conditions": [{"type": "Failed", "status": "True"}],
    }
    keys.release(child, 0.0)  # billing settled the child (its lease released); the row ends at the next sweep
    assert children.sweep() == [f"{child}:ended"]
    assert children.readback(NS, op, "u1")["outcome"] == "undelivered"
    r = c.post(f"/v1/operations/{child}:export", headers=H)
    assert r.status_code == 202, r.text
    retry = r.json()["id"]
    assert retry == f"{child}-x1"
    m = keys.member_of(retry)
    assert {k: m[k] for k in ("root", "kind", "stage", "state", "predecessor")} == {
        "root": op,
        "kind": "child",
        "stage": "u1",
        "state": "active",
        "predecessor": child,
    }
    with db.pool().connection() as conn:
        assert conn.execute("select requests from key_reservations where operation = %s", (op,)).fetchone()[0] == 2
    env, _ = _env(fake.jobs[(NS, retry)])
    assert env["TRANSFER_ONLY"] == "1" and env["IO_MODE"] == "application" and "REQUEST_BODY" not in env
    assert fake.jobs[(NS, retry)]["metadata"]["annotations"][f"{LABEL}/export-of"] == child
    # the broker: the retry's live attempt with the child's token; the ended original and a foreign id: refused
    rp = pod(retry, "Running")
    fake.pods.append(rp)
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"operation_id": retry, "attempt_id": rp["metadata"]["name"], "phase": "output"}
    rr = c.post(f"/internal/operations/{child}:refresh", json=body, headers=hdr)
    assert rr.status_code == 200 and rr.json()["response_url"].endswith(f"/operations/{child}/response.bin"), rr.text
    settled = c.post(f"/internal/operations/{child}:refresh", json={**body, "operation_id": child}, headers=hdr)
    assert settled.status_code in (401, 409)  # the original's reservation is settled: nothing is transferred for it
    assert (
        c.post(
            f"/internal/operations/{child}:refresh", json={**body, "operation_id": f"{op}-x1"}, headers=hdr
        ).status_code
        == 400
    )
    assert (
        c.post(
            f"/internal/operations/{child}:refresh", json={**body, "attempt_id": f"{retry}-old"}, headers=hdr
        ).status_code
        == 403
    )
    # a second :export while the retry runs reads it back (one active retry); after it the lineage head is the retry
    assert c.post(f"/v1/operations/{child}:export", headers=H).status_code == 200
    assert keys.child_head(op, "u1") == retry


def test_the_owner_reads_a_unit_back_without_signed_urls_and_a_foreign_owner_gets_404(rig, monkeypatch):
    """GET /v1/operations/{parent}/children/{unit}: the owner's read-only readback (scope read:operations), the same
    authority as the attempt's, keyed by the durable root (a resumed member names it too), without the attempt-scoped
    download URL; another owner's run and an unknown unit are 404; the child's own Operation names root/unit/parent."""
    import copy

    import auth

    c, fake, op, identity = rig
    child = _post(c, op, "u1", {"prompt": "x"}).json()["child"]
    fake.bucket[f"operations/{child}/call.json"] = {
        "capture_complete": True,
        "http_status": 200,
        "sha256": "cd" * 32,
        "content_type": "video/mp4",
        "size_bytes": 10,
        "attempt_id": f"{child}-abc",
    }
    r = c.get(f"/v1/operations/{op}/children/u1", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["child"] == child and body["root"] == op and body["unit_id"] == "u1" and body["state"] == "active"
    assert body["outcome"] == "succeeded" and body["capture"]["sha256"] == "cd" * 32 and body["model_image"]
    assert "download_url" not in body["capture"] and "download_expires_in_s" not in body["capture"]
    assert "download_url" in children.readback(NS, op, "u1")["capture"]  # the attempt's route keeps it
    assert c.get(f"/v1/operations/{op}/children/none", headers=H).status_code == 404
    # a resumed member of the run names the same durable root: the same unit reads back
    succ = copy.deepcopy(fake.jobs[(NS, op)])
    succ["metadata"]["name"] = f"{op}-r1"
    succ["metadata"]["annotations"][f"{LABEL}/resumed-from"] = op
    fake.jobs[(NS, f"{op}-r1")] = succ
    assert c.get(f"/v1/operations/{op}-r1/children/u1", headers=H).json()["child"] == child
    # the child's own public Operation carries its root, unit and parent
    r = c.get(f"/v1/operations/{child}", headers=H)
    assert r.status_code == 200 and r.json()["child"] == {"root": op, "unit": "u1", "parent": op}, r.text
    assert c.get(f"/v1/operations/{op}", headers=H).json().get("child") is None
    # another owner of the tenant: the run does not exist for it (404), the child neither
    from test_api import KEY_INFO

    async def info(key):
        if key == "sk-other":
            return {
                **copy.deepcopy(KEY_INFO),
                "metadata": {"tenant": "demo", "legacy": {"principal_id": "someone-else"}},
            }
        if key == "sk-good":
            return copy.deepcopy(KEY_INFO)
        raise HTTPException(401, "invalid API key")

    monkeypatch.setattr(auth, "key_info", info)
    other = {"Authorization": "Bearer sk-other"}
    assert c.get(f"/v1/operations/{op}/children/u1", headers=other).status_code == 404
    assert c.get(f"/v1/operations/{child}", headers=other).status_code == 404
    # no key, or a key without the scope: refused before any lookup
    assert c.get(f"/v1/operations/{op}/children/u1").status_code == 401
