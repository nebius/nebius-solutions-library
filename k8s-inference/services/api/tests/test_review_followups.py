"""Regressions of the pre-merge review of 2026-10-13: per-principal isolation of artifact downloads, the write scope
of binding revocation, variants dropped within a retained region, and record-aware operation lists and replays."""

import copy

import app as api
import artifacts
import attempts
import auth
import kube
import models
from test_api import ADMIN, FLEET, KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_attempts import _record
from test_variants import SPEC as VARIANT_SPEC
from test_variants import VARIANT_FLEET


def _auth(monkeypatch, values):
    async def info(key):
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)


def _principal(owner, **extra):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **extra}


def test_artifact_download_is_authorized_like_the_operation_result(client, admin_keys, fake_keys, monkeypatch):
    """[P1] GET /v1/artifacts/{uri} needs read:results; an operation's object is the owner's (or an admin's), within
    the key's model allow-list, like /v1/operations/{id}/result; the tenant bucket alone is not the boundary."""
    c, cluster = client
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    signed = []
    monkeypatch.setattr(
        artifacts,
        "presign_download",
        lambda ns, uri, expires_s=3600, region=None: signed.append(uri)
        or {"name": uri.rsplit("/", 1)[-1], "uri": uri, "url": "https://signed"},
    )
    _auth(
        monkeypatch,
        {
            "sk-good": _principal("p1"),
            "sk-other": _principal("p2"),
            "sk-catalog": _principal("p2", scopes=["read:catalog"], models=["llm-example"]),
            "sk-results": _principal("p2", scopes=["read:operations", "read:results"], models=["llm-example"]),
            "sk-admin": {**KEY_INFO, "metadata": {"tenant": "demo", "role": "admin"}},
        },
    )
    op = c.post(
        "/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {"message": "synthetic"}}
    ).json()["id"]
    uri = f"s3://serverless2-demo-eu-north1/operations/{op}/out/result.json"
    path = f"/v1/artifacts/{uri.replace('/', '%2F')}"
    # the reviewer's two probes: a catalog-only key (403) and another principal with the results scope (404)
    assert c.get(path, headers={"Authorization": "Bearer sk-catalog"}).status_code == 403
    assert c.get(path, headers={"Authorization": "Bearer sk-results"}).status_code == 404
    assert c.get(path, headers={"Authorization": "Bearer sk-other"}).status_code == 404
    assert signed == []
    # the owner and the tenant's admin read it; the key's own uploads need the scope only
    assert c.get(path, headers=H).status_code == 200 and signed == [uri]
    assert c.get(path, headers=ADMIN).status_code == 200
    up = "s3://serverless2-demo-eu-north1/uploads/abc123/input.json"
    assert (
        c.get(f"/v1/artifacts/{up.replace('/', '%2F')}", headers={"Authorization": "Bearer sk-results"}).status_code
        == 200
    )
    assert (
        c.get(f"/v1/artifacts/{up.replace('/', '%2F')}", headers={"Authorization": "Bearer sk-catalog"}).status_code
        == 403
    )
    assert (
        artifacts.operation_of(uri) == op
        and artifacts.operation_of(up) is None
        and artifacts.operation_of("s3://b/operations/") is None
    )


def test_binding_revocation_needs_the_write_scope(client, fake_keys, monkeypatch):
    """[P1] DELETE /v1/bindings/{ref} requires artifacts:write, like PUT (a revocation empties a running attempt's copies)."""
    c, _ = client
    assert c.put("/v1/bindings/cust-r", json={"data": {"input.json": "synthetic"}}, headers=H).status_code == 200
    _auth(monkeypatch, {"sk-good": {**KEY_INFO, "scopes": ["read:catalog"]}})
    r = c.delete("/v1/bindings/cust-r", headers=H)
    assert r.status_code == 403 and "artifacts:write" in r.json()["detail"]
    _auth(monkeypatch, {"sk-good": dict(KEY_INFO)})
    assert c.delete("/v1/bindings/cust-r", headers=H).status_code == 204


def test_a_variant_dropped_within_a_retained_region_is_removed(client, admin_keys, monkeypatch):
    """[P2] PUT without a variant: its InferenceService, authorization and LiteLLM deployment go, the others stay."""
    c, cluster = client
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda cid: "eu-north1" if cid == "hub" else cid)
    deleted = []
    monkeypatch.setattr(models, "litellm_deployments_delete", lambda ids: deleted.extend(ids))
    monkeypatch.setattr(models, "litellm_group_upsert", lambda *a, **k: None)
    r = c.post("/v1/models", json=VARIANT_SPEC, headers=ADMIN)
    assert r.status_code == 201, r.text
    api.reconcile_models()
    isvc = lambda name: ("models", "inferenceservices", name) in cluster.custom  # noqa: E731
    assert isvc("chat") and isvc("chat-spot") and isvc("chat-cheap")
    spec = copy.deepcopy(VARIANT_SPEC)
    spec["deployments"]["eu-north1"]["variants"] = [
        v for v in spec["deployments"]["eu-north1"]["variants"] if v["name"] != "spot"
    ]
    r = c.put("/v1/models/chat", json=spec, headers={**ADMIN, "If-Match": str(r.json()["version"])})
    assert r.status_code == 200, r.text
    api.reconcile_models()
    assert isvc("chat") and isvc("chat-cheap") and not isvc("chat-spot")
    assert ("models", "securitypolicies", "chat-spot-authorization") not in cluster.custom
    assert ("models", "securitypolicies", "chat-authorization") in cluster.custom
    assert deleted == ["chat--hub--spot"] and not admin_keys.changes
    # authorization of the dropped variant is retained while its predictor route is still being torn down
    spec["deployments"]["eu-north1"]["variants"] = spec["deployments"]["eu-north1"]["variants"][:1]
    spec["routing"] = {"field": "tier", "map": {"fast": "reserved"}, "default": "priority"}
    cluster.custom[("models", "httproutes", "cheap-route")] = {
        "metadata": {"name": "cheap-route", "labels": {"serving.knative.dev/route": "chat-cheap-predictor"}}
    }
    assert (
        c.put("/v1/models/chat", json=spec, headers={**ADMIN, "If-Match": str(r.json()["version"])}).status_code == 200
    )
    api.reconcile_models()
    assert (
        not isvc("chat-cheap")
        and ("models", "securitypolicies", "chat-cheap-authorization") in cluster.custom
        and admin_keys.changes
    )
    del cluster.custom[("models", "httproutes", "cheap-route")]
    api.reconcile_models()
    assert ("models", "securitypolicies", "chat-cheap-authorization") not in cluster.custom and not admin_keys.changes
    assert isvc("chat") and ("models", "securitypolicies", "chat-authorization") in cluster.custom


def test_removed_variants_diff_follows_object_names_and_litellm_ids():
    kube_region = kube.cluster_region
    kube.fleet, kube.cluster_region = (lambda: VARIANT_FLEET), (lambda cid: "eu-north1" if cid == "hub" else cid)
    try:
        before = models.to_entry(VARIANT_SPEC)
        spec = copy.deepcopy(VARIANT_SPEC)
        spec["deployments"]["eu-north1"]["variants"] = spec["deployments"]["eu-north1"]["variants"][:1]
        spec["routing"] = {"field": "tier", "map": {"fast": "reserved"}, "default": "priority"}
        after = models.to_entry(spec)
        assert models.removed_variants(before, after, "hub") == (
            {"chat-spot", "chat-cheap"},
            ["chat--hub--spot", "chat--hub--cheap"],
        )
        assert models.removed_variants(before, before, "hub") == (set(), [])
        # the first variant keeps the model's own object name and LiteLLM id, whatever it is called
        spec["deployments"]["eu-north1"]["variants"] = [{"name": "spot", "pool": "h100-spot-1x", "min": 0, "max": 4}]
        spec["routing"] = {"field": "tier", "map": {"fast": "spot"}, "default": "priority"}
        assert models.removed_variants(before, models.to_entry(spec), "hub") == (
            {"chat-spot", "chat-cheap"},
            ["chat--hub--spot", "chat--hub--cheap"],
        )
    finally:
        kube.cluster_region = kube_region


def test_operation_list_and_replays_report_from_the_durable_records(client, monkeypatch):
    """[P2] A completed Job whose pods the worker removed after delivery: the list and the idempotent replay report the
    records' SUCCEEDED like GET /v1/operations/{id}, not FINALIZING/EXPORT_FAILED."""
    c, fake = client
    body = {"mode": "run", "input": {"message": "synthetic"}}
    op = c.post("/v1/models/hello-run:invoke", json=body, headers={**H, "Idempotency-Key": "review-replay"}).json()[
        "id"
    ]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    rows = [
        {
            **_record(op, f"{op}-a", export="succeeded", epoch=1, revision=3, region="eu-north1"),
            "operation": op,
            "root": op,
        }
    ]
    queried = []
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == op else [])
    monkeypatch.setattr(attempts, "for_operations", lambda roots: queried.append(set(roots)) or {op: rows})
    monkeypatch.setattr(attempts, "marks_for", lambda names: queried.append(set(names)) or {op: (5, None, None)})
    one = c.get(f"/v1/operations/{op}", headers=H).json()
    assert one["status"] == "SUCCEEDED" and one["export_state"] == "succeeded"
    listed = {o["id"]: o for o in c.get("/v1/operations", headers=H).json()}
    assert (
        listed[op]["status"] == "SUCCEEDED"
        and listed[op]["attempts"][0]["export_state"] == "succeeded"
        and queried == [{op}, {op}]
    )
    # the lifecycle fields of the list are the single lookup's (records and marks fetched once for the whole list)
    assert {
        k: listed[op][k]
        for k in ("export_state", "root_operation_id", "attempt_id", "epoch", "result_ref", "attempt_role")
    } == {k: one[k] for k in ("export_state", "root_operation_id", "attempt_id", "epoch", "result_ref", "attempt_role")}
    assert listed[op]["revision"] == 5
    replay = c.post("/v1/models/hello-run:invoke", json=body, headers={**H, "Idempotency-Key": "review-replay"})
    assert (
        replay.status_code == 200
        and replay.json()["id"] == op
        and replay.json()["status"] == "SUCCEEDED"
        and replay.json()["export_state"] == "succeeded"
    )
    # without records on file (no database) the list projects from the Job as before
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    assert {o["id"] for o in c.get("/v1/operations", headers=H).json()} == {op}
