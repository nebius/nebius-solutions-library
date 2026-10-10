"""Monitoring authorization, bounded queries, regional forwarding, and real response normalization."""
import asyncio, time
import httpx, pytest
from fastapi import HTTPException
from kubernetes.client.rest import ApiException
from test_api import client, cluster, H, KEY_INFO
import app as appmod, auth, catalog, jobs, kube, monitoring


def transport(monkeypatch, responder):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(responder), **kwargs))


def test_endpoint_metrics_are_authenticated_and_model_scoped(client, monkeypatch):
    c, _ = client
    calls = []
    transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(200, json={"status":"success","data":{"result":[]}}))
    assert c.get("/v1/endpoints/llm-example/metrics").status_code == 401
    assert c.get("/v1/endpoints/missing/metrics", headers=H).status_code == 404
    async def restricted(_): return {**KEY_INFO, "models":["another-model"]}
    monkeypatch.setattr(auth,"key_info",restricted)
    assert c.get("/v1/endpoints/llm-example/metrics", headers=H).status_code == 403
    assert not calls


def test_metrics_missing_is_not_zero_and_nan_is_serializable(client, monkeypatch):
    c, _ = client
    queries=[]
    def respond(req):
        q=req.url.params["query"];queries.append(q)
        values=[[time.time()-15,"NaN"],[time.time(),"0.5"]] if "cpu_usage" in q else []
        return httpx.Response(200,json={"status":"success","data":{"result":[{"values":values}] if values else []}})
    transport(monkeypatch,respond)
    r=c.get("/v1/endpoints/llm-example/metrics?range=15m",headers=H)
    assert r.status_code==200
    panels={p["id"]:p for p in r.json()["panels"]}
    assert panels["requests"]["series"]==[]
    assert panels["cpu"]["series"][0]["points"][0][1] is None
    assert panels["cpu"]["series"][0]["points"][1][1]==.5
    assert all('namespace="models"' in q or 'k8s_namespace_name="models"' in q for q in queries)


def test_partial_metric_failure_preserves_other_panels(client,monkeypatch):
    c,_=client
    transport(monkeypatch,lambda req:httpx.Response(500) if "GPU_UTIL" in req.url.params["query"] else httpx.Response(200,json={"status":"success","data":{"result":[]}}))
    r=c.get("/v1/endpoints/llm-example/metrics",headers=H)
    assert r.status_code==200
    assert {p["id"] for p in r.json()["panels"] if p["unavailable"]}=={"gpu"}


def test_upstream_failure_is_clear_and_does_not_expose_body(client,monkeypatch):
    c,_=client
    transport(monkeypatch,lambda req:httpx.Response(503,text="private credentials and internal URL"))
    r=c.get("/v1/endpoints/llm-example/logs",headers=H)
    assert r.status_code==503 and "private credentials" not in r.text


@pytest.mark.parametrize("query",["range=30d","end=nan","end=inf","end=99999999999","limit=1001","limit=0","search="+"a"*201])
def test_log_query_bounds(client,query):
    c,_=client
    assert c.get("/v1/endpoints/llm-example/logs?"+query,headers=H).status_code==422


def test_log_search_is_literal_and_streams_are_sorted(client,monkeypatch):
    c,_=client;requests=[]
    def respond(req):
        requests.append(req)
        return httpx.Response(200,json={"status":"success","data":{"result":[{"stream":{"pod":"worker","container":"main"},"values":[["1000000000","first"],["2000000000","second"]]}]}})
    transport(monkeypatch,respond)
    r=c.get("/v1/endpoints/llm-example/logs",params={"search":'"} | json'},headers=H)
    assert r.status_code==200
    q=requests[0].url.params["query"]
    assert q.startswith('{namespace="models",inferenceservice="llm-example"}')
    assert q.endswith(' |= "\\\"} | json"')
    assert [l["line"] for l in r.json()["lines"]]==["second","first"]


def test_operation_ownership_checked_before_monitoring(client,monkeypatch):
    c,_=client;calls=[]
    monkeypatch.setattr(jobs,"find",lambda ns,id:({"metadata":{"labels":{"serverless2.nebius/tenant":"other"}}},"eu-north1"))
    transport(monkeypatch,lambda req:calls.append(req) or httpx.Response(500))
    assert c.get("/v1/operations/op-other/logs",headers=H).status_code==404
    assert not calls


def test_worker_monitoring_forwards_bearer_and_resource_only(client,monkeypatch):
    c,_=client;requests=[]
    monkeypatch.setattr(jobs,"find",lambda ns,id:({"metadata":{"labels":{"serverless2.nebius/tenant":"demo"}}},"eu-south1"))
    monkeypatch.setattr(appmod,"REGION_API_URLS",{"eu-south1":"https://worker-api.example"})
    transport(monkeypatch,lambda req:requests.append(req) or httpx.Response(200,json={"region":"eu-south1","lines":[],"truncated":False}))
    assert c.get("/v1/operations/op-owned/logs?range=6h",headers=H).status_code==200
    assert requests[0].url.host=="worker-api.example"
    assert requests[0].url.path=="/v1/operations/op-owned/logs"
    assert requests[0].headers["Authorization"]=="Bearer sk-good"
    assert "region" not in requests[0].url.params


def test_fleet_endpoint_list_keeps_unreachable_region(client,monkeypatch):
    c,_=client
    m={**catalog.get("llm-example"),"regions":["eu-north1","eu-south1"]}
    monkeypatch.setattr(catalog,"all_models",lambda:{"llm-example":m})
    monkeypatch.setattr(appmod,"FLEET_MANAGER",True)
    monkeypatch.setattr(kube,"regions",lambda:["eu-north1","eu-south1"])
    def isvc(name,ns,region="eu-north1"):
        if region=="eu-south1":raise HTTPException(503,"unavailable")
        return {"metadata":{},"spec":{"predictor":{}}}
    monkeypatch.setattr(kube,"isvc",isvc)
    rows=c.get("/v1/endpoints",headers=H).json()
    assert len(rows)==2
    assert rows[0]["region"]=="eu-north1"
    assert rows[1]["status"]=="unavailable" and rows[1]["replicas_ready"] is None


def test_scaling_persists_and_rejects_invalid_range(client,monkeypatch):
    c,_=client;writes=[]
    async def admin_info(_):return {**KEY_INFO,"metadata":{**KEY_INFO["metadata"],"role":"admin"}}
    monkeypatch.setattr(auth,"key_info",admin_info)
    m={**catalog.get("llm-example"),"managed_by":"api","spec":{"id":"llm-example","scaling":{"min":0,"max":2}}}
    monkeypatch.setattr(appmod,"_endpoint_model",lambda id:m)
    monkeypatch.setattr(appmod,"_write_model",lambda spec,p,replace:writes.append(spec))
    monkeypatch.setattr(appmod,"get_endpoint",lambda *args:{"id":"llm-example"})
    assert c.patch("/v1/endpoints/llm-example?region=invalid",json={"min_replicas":1},headers=H).status_code==400
    assert not writes
    assert c.patch("/v1/endpoints/llm-example",json={"min_replicas":3},headers=H).status_code==422
    assert not writes
    assert c.patch("/v1/endpoints/llm-example",json={"min_replicas":1,"scale_to_zero_after_s":180},headers=H).status_code==200
    assert writes[0]["scaling"]=={"min":1,"max":2,"idle_s":180}
    m["managed_by"] = "git"
    assert c.patch("/v1/endpoints/llm-example",json={"min_replicas":1},headers=H).status_code==403
    assert len(writes)==1


def test_container_arguments_and_environment_are_typed(client):
    c,fake=client
    r=c.post("/v1/models/container-run:invoke",json={"mode":"run","input":{"image":"example/image","command":"python train.py","gpus":0,"args":["value with spaces"],"env":{"TEST_VALUE":"a=b=c"}}},headers=H)
    assert r.status_code==202,r.text
    container=fake.jobs[("tenant-demo",r.json()["id"])]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"]==["value with spaces"]
    assert container["command"][-2:] == ['python train.py "$@"', "--"]
    assert {v["name"]:v["value"] for v in container["env"]}["TEST_VALUE"]=="a=b=c"
    r=c.post("/v1/models/container-run:invoke",json={"mode":"run","input":{"image":"example/image","command":"true","env":{"BAD-NAME":"v"}}},headers=H)
    assert r.status_code==400


def test_role_is_public_and_defaults_to_user(client):
    c,_=client
    assert c.get("/v1/keys/me",headers=H).json()["role"]=="user"


@pytest.mark.parametrize("params", [
    {"args": 0}, {"args": None}, {"args": ["valid", 1]},
    {"env": 0}, {"env": None}, {"env": {"VALID": 1}},
])
def test_container_rejects_invalid_argument_and_environment_shapes(client, params):
    c, _ = client
    response = c.post("/v1/models/container-run:invoke", headers=H,
                      json={"mode": "run", "input": {"image": "example/image", "command": "true", **params}})
    assert response.status_code == 400


def test_unreachable_endpoint_state_does_not_invent_zero_replicas(monkeypatch):
    class Unreachable:
        def get_namespaced_custom_object(self, *args, **kwargs):
            raise ApiException(status=403, reason="forbidden")
    monkeypatch.setattr(kube, "api", lambda region: Unreachable())
    monkeypatch.setattr(kube, "_isvc_cache", {})
    assert kube.endpoint_status("example", "example-ns") == {"status": "unavailable", "replicas_ready": None}


def test_monitoring_of_a_retired_region_says_so(monkeypatch):
    """An operation that ran in a region that left the fleet: 410 with the explanation, not a connection error."""
    import asyncio
    import app as appmod
    import kube
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-north2"])
    monkeypatch.setattr(appmod, "REGION_API_URLS", {"eu-north2": "https://api.example.invalid"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(appmod._remote_monitoring(None, "eu-west1", "/v1/operations/x/metrics", {}))
    assert e.value.status_code == 410 and "no longer part of this fleet" in e.value.detail
    with pytest.raises(HTTPException) as e:
        asyncio.run(appmod._remote_monitoring(None, "eu-north1", "/v1/operations/x/metrics", {}))
    assert e.value.status_code == 503
