"""Bounds on what a caller can make the API hold: a sync timeout of at most a week, inputs of at most INPUT_MAX_BYTES
on a Job (never truncated), a liveness probe without the database, the endpoint domain of a cluster id."""

import json

import app as appmod
import kube
import models
from test_api import H, client, cluster  # noqa: F401 - fixtures


def test_timeout_bounds_and_input_cap(client):
    c, _ = client
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}, "timeout_s": 0})
    assert r.status_code == 422
    r = c.post(
        "/v1/models/hello-run:invoke",
        headers=H,
        json={"mode": "run", "input": {}, "timeout_s": appmod.INVOKE_TIMEOUT_MAX_S + 1},
    )
    assert r.status_code == 422
    big = {"text": "x" * (appmod.INPUT_MAX_BYTES + 1)}
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": big})
    assert r.status_code == 413 and "input_prefix" in r.json()["detail"]
    assert len(json.dumps(big)) > appmod.INPUT_MAX_BYTES


def test_livez_answers_without_the_database(client, monkeypatch):
    c, _ = client
    import catalog

    monkeypatch.setattr(catalog, "all_models", lambda: (_ for _ in ()).throw(RuntimeError("database down")))
    assert c.get("/livez").json() == {"ok": True}  # the readiness probe (/healthz) would raise here; liveness does not


def test_endpoint_domain_accepts_a_cluster_id(monkeypatch):
    monkeypatch.setattr(models, "ENDPOINT_DOMAINS", {"eu-north1": "203.0.113.1.sslip.io"})
    monkeypatch.setattr(kube, "cluster_region", lambda cid: {"north": "eu-north1"}.get(cid, cid))
    assert models.endpoint_domain("eu-north1") == "203.0.113.1.sslip.io"
    assert models.endpoint_domain("north") == "203.0.113.1.sslip.io"  # the fleet's own id for that worker
    assert models.endpoint_domain("south") == ""  # no domain for that region: the caller gets its 503


def test_fourteen_day_runs_and_recovery_deadlines_are_accepted(client, monkeypatch):
    """The long-run contract: a run's timeout_s and recovery.deadline_s accept 14 days (1209600 s), one second more is
    refused; the GPU bound of a budgeted key follows the explicit timeout."""
    c, cl = client
    body = {
        "mode": "run",
        "input": {},
        "timeout_s": 14 * 86400,
        "execution": {"recovery": {"on_infrastructure_loss": "recover", "deadline_s": 14 * 86400}},
    }
    r = c.post("/v1/models/hello-run:invoke", headers=H, json=body)
    assert r.status_code == 202, r.text
    assert cl.jobs[("tenant-demo", r.json()["id"])]["spec"]["activeDeadlineSeconds"] == 14 * 86400
    assert appmod.INVOKE_TIMEOUT_MAX_S == 14 * 86400
    body["timeout_s"] = 14 * 86400 + 1
    assert c.post("/v1/models/hello-run:invoke", headers=H, json=body).status_code == 422
    body["timeout_s"] = 3600
    body["execution"]["recovery"]["deadline_s"] = 14 * 86400 + 1
    assert c.post("/v1/models/hello-run:invoke", headers=H, json=body).status_code == 422
