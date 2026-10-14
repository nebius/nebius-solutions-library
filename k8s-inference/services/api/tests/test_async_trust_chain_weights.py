"""Four bounded fixes after the API48 native async and DAG reviews: the fleet CA bundle next to an endpoint call's
caller (fleet-CA-signed regional gateways, verification kept), the readiness path kept through catalog normalisation
(READY_URL), a chain member's attempts from its own live pods (a cancelled parent no longer shows its running resumed
child as PREEMPTED), and an explicit read-only weights payload (no prepare step, readOnly mount)."""

import copy

import app as api
import attempts
import auth
import catalog
import jobs
import models
import render_endpoint
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys, pod  # noqa: F401
from test_async_private import _bind, _tenant
from test_attempts import _record

NS = "tenant-demo"
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_caller_gets_the_fleet_trust_bundle_when_the_fleet_has_one(client, monkeypatch):
    c, cl = client
    _tenant(monkeypatch)
    _bind(c)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    monkeypatch.setattr(jobs, "trust_bundle", lambda: PEM)
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-1"}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    spec = cl.jobs[(NS, oid)]["spec"]["template"]["spec"]
    main = next(x for x in spec["containers"] if x["name"] == "main")
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert {env[k] for k in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE")} == {
        "/etc/ssl/fleet/ca-bundle.pem"
    }
    assert {"name": "trust", "mountPath": "/etc/ssl/fleet", "readOnly": True} in main["volumeMounts"]
    vol = next(v for v in spec["volumes"] if v["name"] == "trust")
    assert vol["secret"] == {"secretName": f"{oid}-auth", "items": [{"key": "ca-bundle.pem", "path": "ca-bundle.pem"}]}
    assert cl.secrets[(NS, f"{oid}-auth")]["stringData"]["ca-bundle.pem"] == PEM
    uploader = next(x for x in spec["containers"] if x["name"] == "uploader")
    assert not any(m["name"] == "trust" for m in uploader.get("volumeMounts") or [])  # the caller only
    # a fleet with public certificates everywhere: nothing projected, the runner's roots stand
    monkeypatch.setattr(jobs, "trust_bundle", lambda: None)
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    spec = cl.jobs[(NS, r.json()["id"])]["spec"]["template"]["spec"]
    assert not any(v["name"] == "trust" for v in spec["volumes"])
    assert "CURL_CA_BUNDLE" not in {e["name"] for e in spec["containers"][0]["env"]}


def test_trust_bundle_is_the_api_s_own_merged_file(monkeypatch, tmp_path):
    import glob

    f = tmp_path / "trust.pem"
    f.write_text("roots\n" + PEM)
    monkeypatch.setenv("SSL_CERT_FILE", str(f))
    monkeypatch.setattr(glob, "glob", lambda p: ["/etc/ssl/msp/fleet-ca.pem"] if p.startswith("/etc/ssl/msp") else [])
    assert jobs.trust_bundle() == "roots\n" + PEM
    monkeypatch.setattr(glob, "glob", lambda p: [])  # no extra CA mounted: nothing to project
    assert jobs.trust_bundle() is None
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setattr(glob, "glob", lambda p: ["/etc/ssl/msp/fleet-ca.pem"])
    assert jobs.trust_bundle() is None


SPEC = {
    "id": "my-llm",
    "kind": "endpoint",
    "image": "vllm/vllm-openai:v0.11.0",
    "port": 8000,
    "protocol": "openai",
    "health_path": "/v1/health/ready",
    "served_model": "x",
    "gpu": {"count": 1, "classes": ["h100"]},
    "weights": {"path": "my-llm"},
}


def test_readiness_path_survives_normalisation_and_reaches_the_caller(client, monkeypatch):
    entry = models.to_entry(SPEC)
    assert entry["endpoints"]["ready"] == "/v1/health/ready"
    m = catalog.normalise(entry)
    assert m["health_path"] == "/v1/health/ready" and "ready" not in m["endpoints"]  # kept, not a callable endpoint
    assert catalog.normalise({"id": "x", "mode": "sync"}).get("health_path") is None
    # model -> catalog -> build_async: the caller probes the readiness path, not the root
    c, cl = client
    _tenant(monkeypatch)
    _bind(c)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    base = catalog.get("llm-example")
    raw = {k: v for k, v in base.items() if k != "health_path"}
    raw["endpoints"] = {**base["endpoints"], "ready": "/v1/health/ready"}
    probed = catalog.normalise(raw)
    monkeypatch.setattr(catalog, "get", lambda mid: probed if mid == "llm-example" else base)
    monkeypatch.setattr(catalog, "require", lambda mid: probed if mid == "llm-example" else base)
    monkeypatch.setattr(api, "_model", lambda mid: probed if mid == "llm-example" else base)
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-1"}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    main = cl.jobs[(NS, r.json()["id"])]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in main["env"]}
    assert env["READY_URL"].endswith("/v1/health/ready"), env["READY_URL"]


def test_cancelled_parent_shows_its_running_resumed_child_as_running(client, admin_keys, fake_keys, monkeypatch):
    """The chain on a parent's handle derives every member's attempts from that member's own live pods and records:
    the parent's terminated pod is the parent's, the child's running pod is the child's (status 65/134 only saw the
    parent's pods and marked the child's started record PREEMPTED)."""
    c, cl = client

    async def info(key):
        return copy.deepcopy({**KEY_INFO, "max_budget": None})

    monkeypatch.setattr(auth, "key_info", info)
    rows: list = []
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [r for r in rows if r["operation"].startswith(root)])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "successor_of", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    monkeypatch.setattr(attempts, "latest_checkpoint", lambda root: None)
    root = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}}).json()["id"]
    cl.jobs[(NS, root)]["status"] = {"startTime": "2026-10-13T20:00:00Z"}
    assert c.post(f"/v1/operations/{root}:cancel", headers=H).status_code == 200
    cl.jobs[(NS, root)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    parent_pod = pod(root, "Failed", finished="2026-10-13T20:05:00Z", exit_code=143)
    cl.pods.append(parent_pod)
    rows.append(
        {
            **_record(root, parent_pod["metadata"]["name"], st="interrupted"),
            "tenant": "demo",
            "root": root,
            "exit_code": 143,
        }
    )
    r = c.post(f"/v1/operations/{root}:resume", headers=H, json={})
    assert r.status_code == 202, r.text
    child = r.json()["id"]
    child_pod = pod(child, "Running", started="2026-10-13T20:10:00Z")
    cl.pods.append(child_pod)
    cl.jobs[(NS, child)]["status"] = {"startTime": "2026-10-13T20:10:00Z", "active": 1}
    rows.append(
        {
            **_record(child, child_pod["metadata"]["name"], st="started"),
            "tenant": "demo",
            "root": root,
            "ended_at": None,
        }
    )
    direct = c.get(f"/v1/operations/{child}", headers=H).json()
    assert direct["status"] == "RUNNING"
    parent = c.get(f"/v1/operations/{root}", headers=H).json()
    # the parent's handle is its lineage head: the running child (api 0.11.65); the parent's own end is in the chain
    assert parent["id"] == child and parent["status"] == "RUNNING" and parent["root_operation_id"] == root
    by_op = {(a["operation"], a["status"]) for a in parent["attempts"]}
    assert (child, "RUNNING") in by_op and (root, "CANCELLED") in by_op or (root, "PREEMPTED") in by_op
    assert not any(a["operation"] == child and a["status"] == "PREEMPTED" for a in parent["attempts"])
    assert {m["id"]: m["status"] for m in parent["chain"]}[child] == "RUNNING"


def test_read_only_weights_mount_read_only_without_a_prepare_step(client):  # the fixture pins the fleet (gpu classes)
    fs = models.to_entry({**SPEC, "weights": {"path": "p", "read_only": True}})["runtime"]["weights"][
        "sharedFilesystem"
    ]
    assert fs["readOnly"] is True and fs["prepare"] is False
    fs = models.to_entry({**SPEC, "weights": {"path": "p"}})["runtime"]["weights"]["sharedFilesystem"]
    assert fs["readOnly"] is False and fs["prepare"] is True  # unchanged default: writable, directory prepared
    fs = models.to_entry({**SPEC, "weights": {"path": "p", "read_only": True, "prepare": True}})["runtime"]["weights"][
        "sharedFilesystem"
    ]
    assert fs["readOnly"] is True and fs["prepare"] is True  # explicit
    v = {
        "name": "w",
        "weights": {"sharedFilesystem": {"enabled": True, "prepare": False, "readOnly": True, "path": "p"}},
    }
    assert render_endpoint._weights_prepare(v) == []
    mount = next(m for m in render_endpoint._volume_mounts(v) if m["name"] == "weights")
    assert mount["readOnly"] is True and mount["subPath"] == "p"
    v["weights"]["sharedFilesystem"].update({"prepare": True, "readOnly": False, "prepareImage": "busybox"})
    assert render_endpoint._weights_prepare(v)[0]["name"] == "weights-prepare"
    assert next(m for m in render_endpoint._volume_mounts(v) if m["name"] == "weights")["readOnly"] is False


def test_async_only_key_reaches_its_own_endpoint_call_through_the_gateway(client, admin_keys, fake_keys, monkeypatch):
    """The hook (the rendered SecurityPolicy forwards x-serverless2-admitted, -operation and -attempt) honours an
    admitted endpoint call of an asynchronous-only key for the readiness GET and the one inference POST, on an exact
    bounded proof: the token is the HMAC for the key, the model and the operation; the operation holds a live
    reservation on that key; its Job is that key's and that model's and not over; the attempt is one of its live pods.
    Without it, or for another operation, model or key, or a terminal operation, or a pod that is not the Job's, the
    key needs invoke:sync as before (API51 ADMET: readiness GET 403, zero inference POST). The key-only and key+model
    tokens of earlier releases pass only with ADMITTED_LEGACY."""
    c, cl = client
    _tenant(monkeypatch)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    monkeypatch.setattr(api.cfg, "ADMITTED_LEGACY", False)
    monkeypatch.setattr(api.kube, "stamp_last_request", lambda *a, **k: None)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)  # no cancel mark (the fake fleet database)
    api._admission_cache.clear()

    async def info(key):
        base = {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": "p1"}}}
        return (
            {**base, "scopes": ["invoke:async", "read:operations", "artifacts:write"]}
            if key == "sk-async"
            else dict(base)
        )

    monkeypatch.setattr(auth, "key_info", info)
    import keys

    A = {"Authorization": "Bearer sk-async"}
    data = {"request.json": "{}", "response_url": "https://objects.example/put/r", "result_ref": "app://r"}
    assert c.put("/v1/bindings/call-1", json={"data": data}, headers=A).status_code == 200  # the caller's own binding
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-1"}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=A)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    h = keys.key_hash("sk-async")
    token = cl.secrets[(NS, f"{oid}-auth")]["stringData"]["admitted"]
    assert token == api._admitted_token(h, "llm-example", oid)
    assert fake_keys.reservation_key(oid) == h  # admitted: the live reservation the hook checks
    assert (
        c.post("/v1/models/llm-example:invoke", json={"input": {"messages": []}}, headers=A).status_code == 403
    )  # sync
    caller = pod(oid, "Running")
    caller["metadata"]["labels"]["serverless2.nebius/mode"] = "async"
    cl.pods.append(caller)
    attempt = caller["metadata"]["name"]
    ok = {**A, "x-serverless2-admitted": token, "x-serverless2-operation": oid, "x-serverless2-attempt": attempt}
    before = len([x for x in fake_keys.calls if x[0] == "admit"])
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 200
    assert c.post("/internal/authorize/llm-example/openai/v1/chat/completions", headers=ok).status_code == 200
    assert len([x for x in fake_keys.calls if x[0] == "admit"]) == before  # nothing counted again

    # every weaker or wrong proof is a direct call: invoke:sync required
    def denied(**changes):
        hdr = {**ok, **changes}
        hdr = {k: v for k, v in hdr.items() if v is not None}
        api._admission_cache.clear()
        return c.get("/internal/authorize/llm-example/v1/health/ready", headers=hdr).status_code == 403

    assert denied(**{"x-serverless2-admitted": None, "x-serverless2-operation": None, "x-serverless2-attempt": None})
    assert denied(**{"x-serverless2-admitted": "forged"})
    assert denied(
        **{
            "x-serverless2-operation": "op-other",
            "x-serverless2-admitted": api._admitted_token(h, "llm-example", "op-other"),
        }
    )
    assert denied(**{"x-serverless2-attempt": "not-a-pod-of-it"})
    assert denied(**{"x-serverless2-attempt": None})
    assert denied(**{"x-serverless2-admitted": api._admitted_token(h, "http-example", oid)})  # another model's
    assert denied(
        **{"x-serverless2-admitted": api._admitted_token(keys.key_hash("sk-good"), "llm-example", oid)}
    )  # another key's
    assert denied(**{"x-serverless2-admitted": api._admitted_token(h, "llm-example")})  # key+model (0.11.53 form)
    assert denied(**{"x-serverless2-admitted": api._admitted_token(h)})  # key-only (up to 0.11.52)
    # live state fails closed after a previously successful hook call, with the identity still cached: a cancel mark,
    # then a released reservation (terminal, billed) each close the gate at once
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 200  # cached identity
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: (1, "2026-10-13T22:50:00Z", None))
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 403
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 200
    fake_keys.release(oid, 0.0)
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 403
    # and a terminal Job with its identity no longer cached
    fake_keys.admit(h, oid, "demo")
    api._admission_cache.clear()
    cl.jobs[(NS, oid)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=ok).status_code == 403
    cl.jobs[(NS, oid)]["status"] = {}
    fake_keys.release(oid, 0.0)
    # the transitional switch: the earlier forms pass while the API51-rendered callers drain
    monkeypatch.setattr(api.cfg, "ADMITTED_LEGACY", True)
    legacy = {**A, "x-serverless2-admitted": api._admitted_token(h)}
    assert c.get("/internal/authorize/llm-example/v1/health/ready", headers=legacy).status_code == 200
    assert (
        c.get(
            "/internal/authorize/llm-example/v1/health/ready",
            headers={**A, "x-serverless2-admitted": api._admitted_token(h, "llm-example")},
        ).status_code
        == 200
    )
