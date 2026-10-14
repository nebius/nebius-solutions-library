"""Endpoint registration refuses what would store but never reconcile (docs/API.md): a timeout_s above the fleet's
Knative max-revision-timeout-seconds (the chart sets Knative and the API from one value) and the env names Knative
Serving reserves; run classes keep their env; the ceiling raised by the operator admits the retained 7500 s."""

import pytest

import catalog
import kube
import models
import render_endpoint
from test_api import ADMIN, FLEET, admin_keys, client, cluster  # noqa: F401

ENDPOINT = {
    "id": "medical-seg",
    "kind": "endpoint",
    "image": "example.org/medical:1",
    "port": 8000,
    "gpu": {"count": 1, "classes": ["h100"]},
}


def test_a_timeout_above_the_fleet_ceiling_is_refused_and_admitted_once_the_ceiling_is_raised(
    client, admin_keys, monkeypatch
):
    c, _ = client
    r = c.post("/v1/models", json={**ENDPOINT, "timeout_s": 7500}, headers=ADMIN)
    assert r.status_code == 400, r.text
    assert "7500" in r.text and "3600" in r.text and "max_revision_timeout_s" in r.text and "never reconciles" in r.text
    assert "medical-seg" not in admin_keys.rows  # nothing stored
    assert c.post("/v1/models", json={**ENDPOINT, "timeout_s": 0}, headers=ADMIN).status_code == 400
    assert c.post("/v1/models", json={**ENDPOINT, "timeout_s": 3600}, headers=ADMIN).status_code in (200, 201)
    monkeypatch.setattr(models.config, "KNATIVE_MAX_REVISION_TIMEOUT_S", 7500)  # the operator raised the one value
    r = c.put("/v1/models/medical-seg", json={**ENDPOINT, "timeout_s": 7500}, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    assert (
        c.post("/v1/models", json={**ENDPOINT, "id": "medical-seg-2", "timeout_s": 7501}, headers=ADMIN).status_code
        == 400
    )
    # the stored definition renders the retained 7500 as the revision timeout
    kube.fleet = lambda: FLEET
    e = catalog.normalise(models.to_entry({**ENDPOINT, "timeout_s": 7500}, managed_by="api"))
    assert e["runtime"]["timeout"] == 7500
    cluster_id = e["regions"][0]
    isvc = next(
        d
        for v in models.variants(e, cluster_id)
        for d in render_endpoint.render(e, cluster_id, "models.example", "ib", v)
        if d.get("kind") == "InferenceService"
    )
    assert isvc["spec"]["predictor"]["timeout"] == 7500


@pytest.mark.parametrize(
    "spec,where",
    [
        ({"env": {"PORT": "8000"}}, "env: PORT"),
        ({"env": {"K_SERVICE": "x"}}, "env: K_SERVICE"),
        ({"env_field_refs": {"K_REVISION": "metadata.name"}}, "env_field_refs: K_REVISION"),
        ({"weights": {"path": "w", "env": {"port": "1"}}}, "weights.env: port"),
    ],
)
def test_reserved_knative_env_names_are_refused_for_endpoints(client, admin_keys, spec, where):
    c, _ = client
    r = c.post("/v1/models", json={**ENDPOINT, **spec}, headers=ADMIN)
    assert r.status_code == 400 and where in r.text and "reserved by Knative" in r.text, r.text
    assert "medical-seg" not in admin_keys.rows
    if "PORT" in r.text.upper().split("RESERVED")[0]:
        assert "read PORT at runtime" in r.text  # the actionable hint for the serving port


def test_run_classes_keep_their_env_and_an_ordinary_endpoint_env_passes(client, admin_keys):
    c, _ = client
    job = {
        "id": "worker-with-port",
        "kind": "job",
        "image": "example.org/w:1",
        "command": "python w.py",
        "cpu": "1",
        "memory": "1Gi",
        "env": {"PORT": "9000", "K_SERVICE": "x"},
    }
    assert c.post("/v1/models", json=job, headers=ADMIN).status_code in (200, 201)
    assert c.post(
        "/v1/models", json={**ENDPOINT, "env": {"MODEL_PORT": "8000", "HF_HOME": "/weights"}}, headers=ADMIN
    ).status_code in (200, 201)
