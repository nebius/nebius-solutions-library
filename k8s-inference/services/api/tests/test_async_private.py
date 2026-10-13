"""Endpoint calls with private input and application-owned output (docs/JOBS.md "Endpoint calls with private input
and application-owned output"): the execution block on mode async, the binding as the only carrier of the request and
the destination, the retained capture, the delivery rule for the status, the transfer-only export retry, the
capability marker, and bounded keys admitted on the path."""

import json

import app as api
import attempts
import auth
import execution as ex
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_attempts import _record


def _main(job):
    return next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "main")


def _uploader(job):
    return next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "uploader")


def _env(c):
    return {e["name"]: e for e in c.get("env", [])}


def _tenant(monkeypatch):
    monkeypatch.setattr(
        ex,
        "tenant_config",
        lambda *a, **k: {
            "identities": {"broker": {"audience": "https://app.example/broker", "env": {"BROKER": "x"}}},
            "execution_profiles": {"confidential": {"shared_filesystem": False}},
        },
    )


def _bind(c, ref="call-1"):
    data = {
        "request.json": json.dumps({"messages": [], "model": "llm"}),
        "response_url": "https://objects.example/put/response?sig=1",
        "call_url": "https://objects.example/put/call?sig=1",
        "result_ref": "app://results/run-7",
        "refresh_url": "https://app.example/broker/refresh",
        "refresh_token": "op-scoped-token",
    }
    assert c.put(f"/v1/bindings/{ref}", json={"data": data}, headers=H).status_code == 200


def test_private_call_job_carries_no_request_and_delivers_itself(client, monkeypatch):
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    body = {
        "mode": "async",
        "input": {},
        "timeout_s": 7200,
        "execution": {
            "io_mode": "application",
            "private_input_ref": "call-1",
            "workload_identity_ref": "broker",
            "profile_ref": "confidential",
        },
    }
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    job = cluster.jobs[("tenant-demo", oid)]
    env = _env(_main(job))
    assert "REQUEST_BODY" not in env and env["IO_MODE"]["value"] == "application"
    assert env["PRIVATE_INPUT_DIR"]["value"] == ex.PRIVATE_DIR and env["IDENTITY_TOKEN_FILE"]["value"].endswith(
        "/token"
    )
    assert env["CALL_TIMEOUT"]["value"] == "7200" and env["OPERATION"]["value"] == oid and env["BROKER"]["value"] == "x"
    assert env["ADMITTED"]["valueFrom"]["secretKeyRef"] == {"name": f"{oid}-auth", "key": "admitted", "optional": True}
    spec = job["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in spec["volumes"]}
    assert (
        vols["work"]["persistentVolumeClaim"]["claimName"] == f"{oid}-work"
        and ("tenant-demo", f"{oid}-work") in cluster.pvcs
    )
    assert vols["private-input"]["secret"]["secretName"] == f"{oid}-private" and "workload-identity" in vols
    assert spec["serviceAccountName"] == "identity-broker"
    assert cluster.secrets[("tenant-demo", f"{oid}-private")]["data"]["result_ref"] == "app://results/run-7"
    ann = job["metadata"]["annotations"]
    assert (
        json.loads(ann["serverless2.nebius/input"]) == {}
        and json.loads(ann["serverless2.nebius/execution"])["io_mode"] == "application"
    )
    assert (
        ann["serverless2.nebius/pvc"] == f"{oid}-work"
        and job["metadata"]["labels"]["serverless2.nebius/execution-profile"] == "confidential"
    )
    up = _env(_uploader(job))
    assert up["UPLOAD_MODE"]["value"] == "record" and "OUTPUT_PREFIX" not in up and "envFrom" not in _uploader(job)
    secret = cluster.secrets[("tenant-demo", f"{oid}-auth")]
    assert secret["stringData"]["authorization"] == "Bearer sk-good"
    assert secret["stringData"]["admitted"] == api._admitted_token(api.keys.key_hash("sk-good"))
    assert "sk-good" not in json.dumps(job) and "objects.example" not in json.dumps(job)
    # the binding is bound to this operation; another call cannot take it
    assert (
        c.post("/v1/models/llm-example:invoke", json=body, headers={**H, "Idempotency-Key": "other"}).status_code == 409
    )


def test_private_call_rejects_inline_input_placement_and_missing_binding(client, monkeypatch):
    c, _ = client
    _tenant(monkeypatch)
    _bind(c, "call-2")
    base = {"mode": "async", "execution": {"io_mode": "application", "private_input_ref": "call-2"}}
    r = c.post("/v1/models/llm-example:invoke", json={**base, "input": {"messages": []}}, headers=H)
    assert r.status_code == 400 and "binding" in r.json()["detail"]
    r = c.post(
        "/v1/models/llm-example:invoke",
        json={**base, "input": {}, "execution": {**base["execution"], "placement": {"gpu_preference": ["h100"]}}},
        headers=H,
    )
    assert r.status_code == 400 and "placement" in r.json()["detail"]
    r = c.post(
        "/v1/models/llm-example:invoke",
        json={"mode": "async", "input": {}, "execution": {"io_mode": "application"}},
        headers=H,
    )
    assert r.status_code == 400 and "private_input_ref" in r.json()["detail"]
    # a managed call keeps its shape: the body in the Job, the platform bucket as the destination, no volume
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202  # the shape is covered by test_async_builds_endpoint_call_job


def test_private_call_status_follows_the_delivery_record(client, monkeypatch):
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c, "call-3")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-3"}}
    oid = c.post("/v1/models/llm-example:invoke", json=body, headers=H).json()["id"]
    job = cluster.jobs[("tenant-demo", oid)]
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-13T08:00:00Z"}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    rows: list = []
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    monkeypatch.setattr(attempts, "for_operations", lambda roots: {oid: rows})
    monkeypatch.setattr(attempts, "marks_for", lambda names: {})
    assert (
        c.get(f"/v1/operations/{oid}", headers=H).json()["status"] == "EXPORT_FAILED"
    )  # exit 0, no record, grace over
    rows.append({**_record(oid, f"{oid}-a", export="failed"), "operation": oid, "io_mode": "application"})
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert got["status"] == "EXPORT_FAILED"
    rows[0] = {**_record(oid, f"{oid}-a", export="succeeded", result_ref="app://results/run-7"), "operation": oid}
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert got["status"] == "SUCCEEDED" and got["result_ref"] == "app://results/run-7"


def test_private_call_export_is_transfer_only_on_the_retained_capture(client, admin_keys, fake_keys, monkeypatch):
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c, "call-4")
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-4"}}
    oid = c.post("/v1/models/llm-example:invoke", json=body, headers=H).json()["id"]
    job = cluster.jobs[("tenant-demo", oid)]
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-13T08:00:00Z"}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [{**_record(oid, f"{oid}-a", export="failed"), "operation": oid, "io_mode": "application"}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    r = c.post(f"/v1/operations/{oid}:export", headers=H)
    assert r.status_code == 202, r.text
    x1 = r.json()["id"]
    assert x1 == f"{oid}-x1"
    retry = cluster.jobs[("tenant-demo", x1)]
    env = _env(_main(retry))
    assert (
        env["TRANSFER_ONLY"]["value"] == "1" and env["IO_MODE"]["value"] == "application" and "REQUEST_BODY" not in env
    )
    vols = {v["name"]: v for v in retry["spec"]["template"]["spec"]["volumes"]}
    assert (
        vols["work"]["persistentVolumeClaim"]["claimName"] == f"{oid}-work"
        and vols["private-input"]["secret"]["secretName"] == f"{x1}-private"
    )
    ann = retry["metadata"]["annotations"]
    assert ann["serverless2.nebius/export-of"] == oid and ann["serverless2.nebius/resumed-from"] == oid
    assert (
        cluster.secrets[("tenant-demo", f"{x1}-auth")]["stringData"]
        == cluster.secrets[("tenant-demo", f"{oid}-auth")]["stringData"]
    )
    assert [x[1:4] for x in fake_keys.calls if x[0] == "prepare"] == [
        (oid, x1, oid)
    ]  # a transfer-only member after the head
    assert [x for x in fake_keys.calls if x[0] == "prepare"][0][6] is True
    # a succeeded call has nothing to export
    rows[0] = {**_record(oid, f"{oid}-a", export="succeeded", result_ref="app://r"), "operation": oid}
    assert c.post(f"/v1/operations/{oid}:export", headers=H).status_code == 409


def test_bounded_key_is_admitted_on_async_calls_and_the_fleet_declares_the_contract(
    client, admin_keys, fake_keys, monkeypatch
):
    c, _ = client

    async def info(key):
        return {**KEY_INFO, "max_concurrent": 2, "budget_requests": 10, "rate_limit": {"requests": 5, "window_s": 60}}

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    assert [x for x in fake_keys.calls if x[0] == "admit"][-1][1] == r.json()["id"]
    fleet = c.get("/v1/fleet", headers=H).json()
    marker = fleet["execution"]["async_private_io"]
    assert marker["version"] == 1 and marker["refresh"] is True and marker["retained_export"] is True
    assert "refresh_url" in marker["binding_keys"] and "result_ref" in marker["binding_keys"]


def test_application_result_is_the_reference_and_an_export_retry_is_followed(
    client, admin_keys, fake_keys, monkeypatch
):
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c, "call-5")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-5"}}
    oid = c.post("/v1/models/llm-example:invoke", json=body, headers=H).json()["id"]
    job = cluster.jobs[("tenant-demo", oid)]
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "completionTime": "2026-10-13T08:00:00Z"}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [{**_record(oid, f"{oid}-a", export="failed"), "operation": oid, "io_mode": "application"}]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    read = []
    monkeypatch.setattr(api.artifacts, "read_json", lambda *a, **k: read.append(a) or {"never": True})
    res = c.get(f"/v1/operations/{oid}/result", headers=H).json()
    assert res["status"] == "EXPORT_FAILED" and res["result"] is None and read == []  # never the platform bucket
    x1 = c.post(f"/v1/operations/{oid}:export", headers=H).json()["id"]
    rows.append({**_record(x1, f"{x1}-b", st="started", export=None), "operation": x1, "root": oid})
    rows[-1].pop("ended_at")
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert (
        got["status"] == "FINALIZING"
        and got["finalizing_by"] == x1
        and got["attempt_id"] == f"{x1}-b"
        and got["attempt_role"] == "export"
    )
    rows[-1].update(
        {
            "status": "succeeded",
            "export_state": "succeeded",
            "result_ref": "app://results/run-7",
            "ended_at": "2026-10-13T08:10:00Z",
        }
    )
    cluster.jobs[("tenant-demo", x1)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    res = c.get(f"/v1/operations/{oid}/result", headers=H).json()
    assert res["status"] == "SUCCEEDED" and res["result_ref"] == "app://results/run-7" and res["exported_by"] == x1


def test_a_gone_private_call_keeps_its_provenance_on_the_durable_path(client, admin_keys, fake_keys, monkeypatch):
    """After the Job is gone the operation is served from its rows: an application call's result is its reference (no
    platform bucket read), another principal still gets 404, and the key's model allow-list still applies."""
    c, _ = client
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "successor_of", lambda operation: None)
    oid = "op-llm-example-gone"
    rows = [
        {
            **_record(oid, f"{oid}-a", export="succeeded", result_ref="app://results/9"),
            "operation": oid,
            "root": oid,
            "tenant": "demo",
            "mode": "async",
            "model": "llm-example",
            "io_mode": "application",
        }
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == oid else [])
    synth = {
        "id": oid,
        "mode": "async",
        "model": "llm-example",
        "status": "SUCCEEDED",
        "execution": {"io_mode": "application"},
        "result_ref": "app://results/9",
        "export_state": "succeeded",
        "attempts": [],
        "region": "eu-north1",
    }
    monkeypatch.setattr(attempts, "synthesize", lambda operation, tenant: dict(synth) if operation == oid else None)
    read = []
    monkeypatch.setattr(api.artifacts, "read_json", lambda *a, **k: read.append(a) or {"never": True})
    fake_keys.owners[oid] = ("demo", auth.Principal("sk-good", "demo", KEY_INFO).owner, "llm-example")
    res = c.get(f"/v1/operations/{oid}/result", headers=H)
    assert res.status_code == 200, res.text
    assert res.json()["result_ref"] == "app://results/9" and res.json()["result"] is None and read == []
    other = {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": "someone-else"}}}
    restricted = {**KEY_INFO, "models": ["hello-run"]}

    async def info(key):
        return dict({"sk-other": other, "sk-restricted": restricted, "sk-good": KEY_INFO}[key])

    monkeypatch.setattr(auth, "key_info", info)
    assert c.get(f"/v1/operations/{oid}/result", headers={"Authorization": "Bearer sk-other"}).status_code == 404
    fake_keys.owners.pop(oid, None)  # no durable owner record: the model allow-list alone still fences the key
    assert c.get(f"/v1/operations/{oid}/result", headers={"Authorization": "Bearer sk-restricted"}).status_code == 404


def test_a_delivered_error_capture_stays_failed_on_the_root_live_and_after_gc(
    client, admin_keys, fake_keys, monkeypatch
):
    """An export retry that delivers a captured endpoint error closes the root as FAILED with the delivery next to it
    (export_state succeeded, exported_by, result_ref), never as SUCCEEDED; the same after the Job is gone."""
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c, "call-6")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-6"}}
    oid = c.post("/v1/models/llm-example:invoke", json=body, headers=H).json()["id"]
    cluster.jobs[("tenant-demo", oid)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    rows = [
        {
            **_record(oid, f"{oid}-a", st="failed", export="failed", exit_code=2),
            "operation": oid,
            "io_mode": "application",
        }
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    x1 = c.post(f"/v1/operations/{oid}:export", headers=H).json()["id"]
    rows.append(
        {
            **_record(x1, f"{x1}-b", st="failed", export="succeeded", result_ref="app://results/err", exit_code=2),
            "operation": x1,
            "root": oid,
        }
    )
    cluster.jobs[("tenant-demo", x1)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert got["status"] == "FAILED" and got["export_state"] == "succeeded" and got["exported_by"] == x1
    assert got["result_ref"] == "app://results/err" and "did not succeed" in got["error"]
    res = c.get(f"/v1/operations/{oid}/result", headers=H).json()
    assert res["status"] == "FAILED" and res["export_state"] == "succeeded" and res["result_ref"] == "app://results/err"
    # after the Job is gone: served from the rows, same answer
    del cluster.jobs[("tenant-demo", oid)]
    monkeypatch.setattr(attempts, "successor_of", lambda operation: None)
    synth = {
        "id": oid,
        "mode": "async",
        "model": "llm-example",
        "status": "FAILED",
        "execution": {"io_mode": "application"},
        "result_ref": None,
        "export_state": "failed",
        "attempts": [],
        "region": "eu-north1",
    }
    monkeypatch.setattr(attempts, "synthesize", lambda operation, tenant: dict(synth) if operation == oid else None)
    for r in rows:
        r.setdefault("tenant", "demo")
        r.setdefault("root", oid)
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert (
        got["status"] == "FAILED"
        and got["export_state"] == "succeeded"
        and got["exported_by"] == x1
        and got["result_ref"] == "app://results/err"
    )
    # a delivered 2xx capture through a retry: SUCCEEDED
    rows[-1].update({"status": "succeeded", "exit_code": 0})
    got = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert got["status"] == "SUCCEEDED" and got["exported_by"] == x1


def test_private_call_pod_owns_its_volume_and_binding_and_a_dead_main_is_failed(client, monkeypatch):
    """The caller runs as uid 10001: the pod's fsGroup makes the claim's root and the 0440 binding files its (the
    permission-denied blocker of 2026-10-13); and a main that exited with a FailJob code is FAILED at once, not RUNNING
    while the uploader still posts."""
    import status
    from test_api import pod as make_pod

    c, cluster = client
    _tenant(monkeypatch)
    _bind(c, "call-7")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-7"}}
    oid = c.post("/v1/models/llm-example:invoke", json=body, headers=H).json()["id"]
    job = cluster.jobs[("tenant-demo", oid)]
    spec = job["spec"]["template"]["spec"]
    assert (
        spec["securityContext"]["fsGroup"] == 10001
        and spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    )
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["private-input"]["secret"]["defaultMode"] == 0o440
    # main exited 2 (the caller's error), the pod still Running (uploader): the operation is FAILED now
    p = make_pod(oid, "Running", finished="2026-10-13T20:00:10Z", exit_code=2, gpus=0)
    assert (
        status._fails_job(job, 2)
        and status._fails_job(job, 4)
        and status._fails_job(job, 5)
        and not status._fails_job(job, 1)
    )
    op = status.normalise({**job, "_pods": [p]}, None, [])
    assert op["status"] == "FAILED" and op["error"]  # the error is the attempt's container reason
    managed = cluster.jobs[
        (
            "tenant-demo",
            c.post(
                "/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H
            ).json()["id"],
        )
    ]
    assert (
        status.normalise(
            {
                **managed,
                "_pods": [
                    make_pod(
                        managed["metadata"]["name"], "Running", finished="2026-10-13T20:00:10Z", exit_code=1, gpus=0
                    )
                ],
            },
            None,
            [],
        )["status"]
        == "RUNNING"
    )


def test_private_call_pod_shares_its_process_namespace(client, monkeypatch):
    """The caller pod shares one PID namespace between main and the uploader, as a run's pod does: the uploader
    records `started` only when it sees the caller's process and `finished` when it is gone (upload.sh main_alive);
    without the shared namespace no attempt is ever opened and an application broker that grants nothing before the
    authoritative start record keeps the caller waiting for good (the 21:54 UTC deadlock on API45/jobs19)."""
    c, cluster = client
    _tenant(monkeypatch)
    _bind(c)
    monkeypatch.setattr(api.cfg, "ROUTER_KEY", "synthetic-router")
    body = {"mode": "async", "input": {}, "execution": {"io_mode": "application", "private_input_ref": "call-1"}}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    spec = cluster.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["spec"]
    assert spec["shareProcessNamespace"] is True
    assert [x["name"] for x in spec["containers"]] == ["main", "uploader"]
    # the managed path (no execution block) renders the same pod shape
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    assert cluster.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["spec"]["shareProcessNamespace"] is True
