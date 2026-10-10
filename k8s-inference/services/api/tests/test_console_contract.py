"""The standalone console needs account roles and region-specific endpoint state."""
from types import SimpleNamespace
from test_api import client, cluster, FakeCluster, ADMIN_INFO, H
import auth, catalog, kube

read_status = kube.endpoint_status


def test_session_role_and_fleet_metadata(client, monkeypatch):
    c, _ = client
    assert c.get("/v1/keys/me", headers=H).json()["role"] == "user"
    async def admin_info(key):
        return dict(ADMIN_INFO)
    monkeypatch.setattr(auth, "key_info", admin_info)
    assert c.get("/v1/keys/me", headers=H).json()["role"] == "admin"
    body = c.get("/healthz").json()
    assert body["regions"] == ["eu-north1"]
    assert body["gpu_classes"] == ["h100", "l40s", "rtx-pro-6000"]


def test_endpoint_reads_and_scaling_target_the_selected_region(client, monkeypatch):
    c, north = client
    south = FakeCluster()
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": {"eu-north1": north, "eu-south1": south}[region])
    m = catalog.get("llm-example")
    monkeypatch.setattr(catalog, "all_models", lambda: {"llm-example": {**m, "regions": ["eu-north1", "eu-south1"]}})
    isvc = {"metadata": {"labels": {"serverless2.nebius/created-by": "api"}}, "spec": {"predictor": {"maxReplicas": 2}}}
    for fake in (north, south):
        fake.custom[(m["namespace"], "inferenceservices", m["k8s_name"])] = isvc
    monkeypatch.setattr(kube, "isvc", lambda name, ns, region="eu-north1": kube.api(region).custom.get((ns, "inferenceservices", name)))
    monkeypatch.setattr(kube, "endpoint_status", lambda name, ns, region="eu-north1": {"status": "ready", "replicas_ready": 1 if region == "eu-north1" else 2})
    rows = c.get("/v1/endpoints", headers=H).json()
    assert {(r["region"], r["replicas_ready"]) for r in rows} == {("eu-north1", 1), ("eu-south1", 2)}
    assert all("placement" not in r for r in rows)  # capacity type must not be invented
    assert c.get("/v1/endpoints?region=eu-south1", headers=H).json() == [rows[1]]
    assert c.get("/v1/endpoints/llm-example?region=eu-south1", headers=H).json()["replicas_ready"] == 2
    assert c.get("/v1/endpoints?region=unknown", headers=H).status_code == 400
    assert c.get("/v1/endpoints/llm-example?region=unknown", headers=H).status_code == 400
    assert c.patch("/v1/endpoints/llm-example?region=eu-south1", json={"min_replicas": 1}, headers=H).status_code == 403
    async def admin_info(key):
        return dict(ADMIN_INFO)
    monkeypatch.setattr(auth, "key_info", admin_info)
    # Fleet definitions are read-only for admins as well. API-managed policies are
    # persisted and reconciled across regions, covered in test_api.py.
    assert c.patch("/v1/endpoints/llm-example?region=eu-south1", json={"min_replicas": 1}, headers=H).status_code == 403


def test_replica_cache_is_separate_for_each_region(cluster, monkeypatch):
    class API:
        def get_namespaced_custom_object(self, *args, **kw):
            return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    class Core:
        def __init__(self, count): self.count = count
        def list_namespaced_pod(self, *args, **kw):
            pod = SimpleNamespace(status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(ready=True)]))
            return SimpleNamespace(items=[pod] * self.count)
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": API())
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": Core(1 if region == "eu-north1" else 2))
    monkeypatch.setattr(kube, "_isvc_cache", {})
    assert read_status("same-name", "models", "eu-north1")["replicas_ready"] == 1
    assert read_status("same-name", "models", "eu-south1")["replicas_ready"] == 2
    assert read_status("same-name", "models", "eu-north1")["replicas_ready"] == 1
