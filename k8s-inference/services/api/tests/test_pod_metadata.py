"""External opt-in metadata survives API persistence and generated pod templates."""
import copy

import pytest

from test_api import client, cluster, admin_keys, ADMIN, H, MULTINODE_FLEET
import catalog
import jobs
import kube


EXTRA = {"labels": {"example.org/startup-profile": "qualified-science"},
         "annotations": {"example.org/qualification": "evidence/model-immutable.json"}}


def test_endpoint_metadata_is_admin_owned_and_can_be_removed(client, admin_keys):
    c, fake = client
    spec = {"id": "metadata-endpoint", "kind": "endpoint", "image": "example.org/model:1",
            "gpu": {"classes": ["h100"], "count": 1}, "pod_metadata": EXTRA,
            "automount_service_account_token": False}
    assert c.post("/v1/models", json=spec, headers=H).status_code == 403
    response = c.post("/v1/models", json=spec, headers=ADMIN)
    assert response.status_code == 201, response.text
    key = ("models", "inferenceservices", spec["id"])
    predictor = fake.custom[key]["spec"]["predictor"]
    assert predictor["labels"]["example.org/startup-profile"] == "qualified-science"
    assert predictor["labels"]["kueue.x-k8s.io/priority-class"] == "customer-batch"
    assert predictor["annotations"] == EXTRA["annotations"]
    assert predictor["automountServiceAccountToken"] is False
    assert "example.org/startup-profile" not in fake.custom[key]["metadata"]["labels"]
    assert admin_keys.rows[spec["id"]]["entry"]["podMetadata"] == EXTRA
    assert c.get("/v1/models/metadata-endpoint", headers=ADMIN).json()["spec"]["pod_metadata"] == EXTRA
    del spec["pod_metadata"]
    del spec["automount_service_account_token"]
    response = c.put("/v1/models/metadata-endpoint", json=spec, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert "example.org/startup-profile" not in fake.custom[key]["spec"]["predictor"].get("labels", {})
    assert "annotations" not in fake.custom[key]["spec"]["predictor"]
    assert "automountServiceAccountToken" not in fake.custom[key]["spec"]["predictor"]


def test_run_metadata_survives_local_and_multikueue_templates(client, admin_keys):
    c, fake = client
    spec = {"id": "metadata-job", "kind": "job", "image": "example.org/model:1", "command": "python model.py",
            "gpu": {"classes": ["h100"], "count": 1}, "pod_metadata": EXTRA}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    response = c.post("/v1/models/metadata-job:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert response.status_code == 202, response.text
    job = fake.jobs[("tenant-demo", response.json()["id"])]
    for current in (job, jobs.build_run("op-metadata-fleet", catalog.get(spec["id"]), {"output_prefix": "s3://workspace/qa/metadata"}, "demo", None, None,
                    600, None, region=None, placement={"manager": True, "classes": ["h100"], "regions": ["hub"]})):
        metadata = current["spec"]["template"]["metadata"]
        assert metadata["labels"]["example.org/startup-profile"] == "qualified-science"
        assert metadata["annotations"]["example.org/qualification"] == EXTRA["annotations"]["example.org/qualification"]
        assert "serverless2.nebius/tenant" in metadata["labels"]
        assert "kueue.x-k8s.io/queue-name" in current["metadata"]["labels"]
        assert "example.org/startup-profile" not in current["metadata"]["labels"]
    rejected = c.post("/v1/models/metadata-job:invoke", json={"mode": "run", "input": {"pod_metadata": {}}}, headers=H)
    assert rejected.status_code == 400


def test_jobset_pod_metadata_preserves_distributed_admission(cluster, monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    model = copy.deepcopy(catalog.get("distributed-run"))
    model["podMetadata"] = EXTRA
    job = jobs.build_run("op-metadata-distributed", model,
                         {"nodes": 2, "gpus_per_node": 8, "command": "true", "image": "example.org/model:1", "interconnect": "none",
                          "output_prefix": "s3://workspace/qa/metadata"}, "demo", None, None,
                         600, None, region=None, placement={"manager": True, "classes": ["h100"], "regions": ["hub"]})
    metadata = job["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["metadata"]
    assert metadata["labels"]["example.org/startup-profile"] == "qualified-science"
    assert metadata["annotations"]["example.org/qualification"] == EXTRA["annotations"]["example.org/qualification"]
    assert metadata["annotations"]["serverless2.nebius/gpu-classes"] == "h100"


@pytest.mark.parametrize("metadata", [
    {"labels": {"serverless2.nebius/tenant": "other"}},
    {"annotations": {"kueue.x-k8s.io/queue-name": "other"}},
    {"annotations": {"autoscaling.knative.dev/min-scale": "5"}},
    {"labels": {"example.org/profile": "bad value"}},
    {"labels": {"example.org/profile": 1}},
    {"annotations": {"example.org/evidence": "{{ customer_input }}"}},
    {"labels": {"/profile": "test"}},
    {"annotations": {"example.org/evidence": "x" * 2049}},
    {"automountServiceAccountToken": False},
])
def test_invalid_or_platform_metadata_never_reaches_the_database(client, admin_keys, metadata):
    c, _ = client
    spec = {"id": "metadata-rejected", "kind": "endpoint", "image": "example.org/model:1", "pod_metadata": metadata}
    response = c.post("/v1/models", json=spec, headers=ADMIN)
    assert response.status_code == 400, response.text
    assert spec["id"] not in admin_keys.rows


@pytest.mark.parametrize("kind,value", [("endpoint", "false"), ("endpoint", 0), ("job", False)])
def test_token_setting_is_explicit_and_never_disables_run_helpers(client, admin_keys, kind, value):
    c, _ = client
    spec = {"id": "token-rejected", "kind": kind, "image": "example.org/model:1",
            "automount_service_account_token": value}
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 400
    assert spec["id"] not in admin_keys.rows
