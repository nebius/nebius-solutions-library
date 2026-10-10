"""Regression coverage for authorization and interrupted desired-state/job writes."""
import copy
import json
import pytest
from fastapi import HTTPException

from test_api import client, cluster, admin_keys, ADMIN, H, KEY_INFO, L, pod
import app as api
import auth
import billing
import catalog
import jobs
import models


SPEC = {"id": "recovery-test", "kind": "endpoint", "image": "busybox:1.36", "gpu": {"count": 1, "classes": ["h100"]}}


def test_catalogue_and_endpoint_auth_enforce_model_permissions(client, admin_keys, monkeypatch):
    c, _ = client
    spec = {**SPEC, "env": {"PRIVATE_VALUE": "must-not-leak"}}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    assert "must-not-leak" not in c.get("/v1/models", headers=H).text
    async def limited(key):
        return {**KEY_INFO, "models": ["http-example"]}
    monkeypatch.setattr(auth, "key_info", limited)
    assert "recovery-test" not in {m["id"] for m in c.get("/v1/models", headers=H).json()}
    assert c.get("/v1/models/recovery-test", headers=H).status_code == 403
    assert c.get("/v1/endpoints/recovery-test", headers=H).status_code == 403
    # Host headers cannot change the chart-bound identity.
    assert c.get("/internal/authorize/recovery-test/v1/chat", headers={**H, "X-Forwarded-Host": "http-example-predictor.models.example"}).status_code == 403
    assert c.get("/internal/authorize/http-example/v1/echo", headers=H).status_code == 200


def test_endpoint_auth_websocket_and_exhausted_budget(client, monkeypatch):
    c, _ = client
    r = c.get("/internal/authorize/ws-example/ws", headers={"Sec-WebSocket-Protocol": "bearer.sk-good"})
    assert r.status_code == 200 and r.headers["x-serverless2-tenant"] == "demo"
    async def exhausted(key):
        return {**KEY_INFO, "spend": 10}
    monkeypatch.setattr(auth, "key_info", exhausted)
    assert c.get("/internal/authorize/ws-example/ws", headers=H).status_code == 402


def test_failed_deployment_remains_committed_and_retries(client, admin_keys, monkeypatch):
    c, _ = client
    with monkeypatch.context() as m:
        m.setattr(models, "apply", lambda *a: (_ for _ in ()).throw(RuntimeError("region offline")))
        r = c.post("/v1/models", json=SPEC, headers=ADMIN)
        assert r.status_code == 201 and r.json()["pending"]
        assert admin_keys.rows[SPEC["id"]]["version"] == 1
        assert admin_keys.changes[SPEC["id"]]["error"]
    api.reconcile_models()
    assert not admin_keys.changes


def test_copy_failure_is_retried_without_another_history_version(client, admin_keys, monkeypatch):
    c, _ = client
    monkeypatch.setattr(models, "copy_regions", lambda: ["eu-north1"])
    save = models.save
    calls = []
    def fail_once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("ConfigMap write unavailable")
        return save(*args)
    monkeypatch.setattr(models, "save", fail_once)
    assert c.post("/v1/models", json=SPEC, headers=ADMIN).json()["pending"]
    api.reconcile_models()
    assert not admin_keys.changes and len(admin_keys.history(SPEC["id"])) == 1


def test_delete_recreate_and_stale_save(client, admin_keys):
    c, _ = client
    assert c.post("/v1/models", json=SPEC, headers=ADMIN).json()["version"] == 1
    assert c.delete("/v1/models/" + SPEC["id"], headers=ADMIN).status_code == 204
    assert c.post("/v1/models", json=SPEC, headers=ADMIN).json()["version"] == 3
    assert c.put("/v1/models/" + SPEC["id"], json=SPEC, headers={**ADMIN, "If-Match": "1"}).status_code == 409


def test_delete_keeps_authorization_until_routes_disappear(client, admin_keys):
    c, cluster = client
    assert c.post("/v1/models", json=SPEC, headers=ADMIN).status_code == 201
    route = ("models", "httproutes", "old-route")
    cluster.custom[route] = {"metadata": {"name": "old-route", "labels": {"serving.knative.dev/route": SPEC["id"] + "-predictor"}}}
    assert c.delete("/v1/models/" + SPEC["id"], headers=ADMIN).status_code == 204
    policy = ("models", "securitypolicies", SPEC["id"] + "-authorization")
    assert policy in cluster.custom and admin_keys.changes
    assert c.get("/internal/authorize/" + SPEC["id"], headers=H).status_code == 404
    del cluster.custom[route]
    api.reconcile_models()
    assert policy not in cluster.custom and not admin_keys.changes


def test_litellm_model_retry_updates_existing_deployment_without_deleting(monkeypatch):
    import httpx
    calls = []
    class Proxy:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def patch(self, url, **kw):
            calls.append(("patch", url)); return httpx.Response(200, json={})
        def post(self, url, **kw):
            calls.append(("post", url)); raise AssertionError("existing models must update in place")
    monkeypatch.setattr(httpx, "Client", Proxy)
    monkeypatch.setattr(models, "LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setattr(models, "LITELLM_INTERNAL_KEY", "sk-internal")
    monkeypatch.setattr(models, "ENDPOINT_DOMAINS", {"eu-north1": "example.org"})
    entry = {"id": "my-model", "namespace": "models", "protocol": "openai"}
    assert models.litellm_group_upsert(entry, "hub", "eu-north1") is None
    assert len(calls) == 1 and calls[0][1].endswith("/model/my-model--hub/update")


def test_job_replay_repairs_missing_dependencies_and_rejects_changed_input(cluster):
    job = jobs.build_run("retry-test", catalog.get("hello-run"), {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://test/out"})
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "retry-secret"}, "stringData": {"key": "secret"}}
    job["metadata"]["annotations"][f"{L}/pvc"] = "retry-work"
    created, _ = jobs.create("tenant-demo", copy.deepcopy(job), secret=copy.deepcopy(secret))
    cluster.pvcs.clear(); cluster.secrets.clear()
    replay, was_created = jobs.create("tenant-demo", copy.deepcopy(job), secret=copy.deepcopy(secret))
    assert not was_created and replay["metadata"]["uid"] == created["metadata"]["uid"]
    assert ("tenant-demo", "retry-work") in cluster.pvcs and ("tenant-demo", "retry-secret") in cluster.secrets
    changed = copy.deepcopy(job)
    changed["spec"]["template"]["spec"]["containers"][0]["args"] = ["changed"]
    with pytest.raises(HTTPException, match="409"):
        jobs.create("tenant-demo", changed)


def test_native_job_prices_are_snapshotted_and_survive_model_deletion(client, admin_keys, monkeypatch):
    import kube
    c, _ = client
    monkeypatch.setattr(kube, "fleet", lambda: {"pools": {"h100": {"region": "hub", "pool": "h100", "gpu_class": "h100", "usd_per_gpu_hour": 2}}})
    spec = {**SPEC, "kind": "job", "command": "echo ok"}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    job = jobs.build_run("priced-test", catalog.get(SPEC["id"]), {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://test/out"})
    p = pod("priced-test", "Succeeded", finished="2026-10-05T21:01:00Z")
    p["spec"]["nodeSelector"] = {f"{L}/pool": "h100"}
    job["_pods"] = [p]
    assert billing.cost_of(job, None) == (3600, 2)
    p["spec"]["nodeSelector"][f"{L}/pool"] = "unknown"
    job["metadata"]["annotations"][f"{L}/billing-rates"] = json.dumps({"eu-north1": {"h100": 2, "l40s": 1}})
    with pytest.raises(ValueError, match="price"):
        billing.cost_of(job, None)


@pytest.mark.parametrize("kind", ["endpoint", "job"])
def test_model_env_secret_references_render_without_values(client, admin_keys, kind):
    c, _ = client
    spec = {**SPEC, "kind": kind, "env_secrets": ["app-credentials"]}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    entry = admin_keys.rows[SPEC["id"]]["entry"]
    assert entry["runtime" if kind == "endpoint" else "job"]["envFrom"] == [{"secretRef": {"name": "app-credentials"}}]
    assert c.put("/v1/models/" + SPEC["id"], json={**spec, "env_secrets": "invalid"}, headers=ADMIN).status_code == 400
