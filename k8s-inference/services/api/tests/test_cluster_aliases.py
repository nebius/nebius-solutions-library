"""Deployments are keyed by cluster id (`north`), the API's clients by Nebius region (`eu-north1`): every path resolves
one to the other through the fleet document (kube.cluster_region) and never guesses between two clusters of one
region (docs/API.md "Regions and clusters")."""

import catalog
import kube
from test_api import FLEET, FakeCluster, H, client, cluster  # noqa: F401

ALIASED = {
    **FLEET,
    "clusters": {
        "north": {"region": "eu-north1"},
        "south": {"region": "eu-south1"},
        "control": {"region": "control", "control": True},
    },
}
TWO_IN_ONE = {
    **FLEET,
    "clusters": {
        "north": {"region": "eu-north1"},
        "north2": {"region": "eu-north1"},
        "control": {"region": "control", "control": True},
    },
}


def _model(deployments):
    m = {**catalog.get("llm-example")}
    m["deployments"] = {c: {} for c in deployments}
    m["regions"] = list(deployments)
    return m


def _aliased(monkeypatch, fleet, deployments):
    monkeypatch.setattr(kube, "fleet", lambda: fleet)
    clusters = fleet["clusters"]
    monkeypatch.setattr(
        kube, "cluster_region", lambda c: (clusters.get(c) or {}).get("region") or c
    )  # the client fixture pins the hub alias only
    m = _model(deployments)
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "llm-example" else None)
    monkeypatch.setattr(catalog, "all_models", lambda: {"llm-example": m})
    monkeypatch.setattr(
        catalog, "require", lambda name: m if name == "llm-example" else (_ for _ in ()).throw(Exception(name))
    )
    return m


def test_async_call_on_an_aliased_cluster_is_created_in_its_region(client, monkeypatch):
    c, fake = client
    _aliased(monkeypatch, ALIASED, ["north"])
    for region in (None, "north", "eu-north1"):
        body = {"mode": "async", "input": {"messages": []}, **({"region": region} if region else {})}
        r = c.post("/v1/models/llm-example:invoke", json=body, headers={**H, "Idempotency-Key": f"k-{region}"})
        assert r.status_code == 202, (region, r.text)
        job = fake.jobs[("tenant-demo", r.json()["id"])]
        assert job["metadata"]["labels"]["serverless2.nebius/region"] == "eu-north1"
        assert r.json()["region"] == "eu-north1"
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {}, "region": "south"}, headers=H)
    assert r.status_code == 400 and "deployed in ['north'], not south" in r.json()["detail"]


def test_endpoint_list_and_detail_resolve_aliases(client, monkeypatch):
    c, fake = client
    _aliased(monkeypatch, ALIASED, ["north"])
    monkeypatch.setattr(
        kube,
        "isvc",
        lambda n, ns, region="eu-north1": {
            "kind": "InferenceService",
            "metadata": {"name": n},
            "spec": {"predictor": {}},
            "status": {},
        }
        if region == "eu-north1"
        else None,
    )
    listed = c.get("/v1/endpoints", headers=H).json()
    assert [(e["model"], e["region"], e["cluster"]) for e in listed] == [("llm-example", "eu-north1", "north")]
    assert c.get("/v1/endpoints?region=north", headers=H).json()[0]["cluster"] == "north"
    assert c.get("/v1/endpoints?region=eu-north1", headers=H).json()[0]["cluster"] == "north"
    assert c.get("/v1/endpoints?region=eu-south1", headers=H).status_code == 400  # not served by this API
    assert c.get("/v1/endpoints?region=nowhere", headers=H).status_code == 400
    eid = catalog.get("llm-example")["k8s_name"]
    for region in ("north", "eu-north1"):
        r = c.get(f"/v1/endpoints/{eid}?region={region}", headers=H)
        assert r.status_code == 200 and r.json()["region"] == "eu-north1", r.text
    assert c.get(f"/v1/endpoints/{eid}?region=south", headers=H).status_code == 400
    m = c.get("/v1/models/llm-example", headers=H).json()
    assert [(x["region"], x["cluster"]) for x in m["regions"]] == [("eu-north1", "north")]


def test_two_clusters_in_one_region_are_never_guessed_between(client, monkeypatch):
    c, fake = client
    _aliased(monkeypatch, TWO_IN_ONE, ["north", "north2"])
    for region in (None, "eu-north1"):
        body = {"mode": "async", "input": {}, **({"region": region} if region else {})}
        r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
        assert r.status_code == 400 and "several deployments" in r.json()["detail"], (region, r.text)
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {}, "region": "north"}, headers=H)
    assert r.status_code == 400 and "one cluster per region" in r.json()["detail"]
    r = c.get(f"/v1/endpoints/{catalog.get('llm-example')['k8s_name']}?region=eu-north1", headers=H)
    assert r.status_code == 400, r.text
    listed = c.get("/v1/endpoints", headers=H).json()
    assert {(e["cluster"], e["status"]) for e in listed} == {("north", "unavailable"), ("north2", "unavailable")}
