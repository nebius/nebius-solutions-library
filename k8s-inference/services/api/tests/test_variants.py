"""Variants and request routing (docs/API.md "Variants"): several deployments of one endpoint in a region behind one
name, in priority order (a warm floor on one pool, overflow on another), per-GPU-class images, a request field that
picks a variant, LiteLLM deployments with priorities, and the objects every variant renders."""

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import kube
import models
from test_api import FLEET, H, client, cluster  # noqa: F401

VARIANT_FLEET = {
    "pools": {
        "hub-h100-res": {
            "region": "hub",
            "pool": "h100-res-1x",
            "gpu_class": "h100",
            "gpus_per_node": 1,
            "capacity": "reserved",
            "usd_per_gpu_hour": 2.0,
        },
        "hub-h100-spot": {
            "region": "hub",
            "pool": "h100-spot-1x",
            "gpu_class": "h100",
            "gpus_per_node": 1,
            "capacity": "spot",
            "usd_per_gpu_hour": 0.8,
        },
        "hub-l40s": {
            "region": "hub",
            "pool": "l40s-spot-1x",
            "gpu_class": "l40s",
            "gpus_per_node": 1,
            "capacity": "spot",
            "usd_per_gpu_hour": 0.7,
        },
    }
}

SPEC = {
    "id": "chat",
    "kind": "endpoint",
    "protocol": "openai",
    "image": "vllm/vllm-openai:v0.31.0",
    "port": 8000,
    "images": {"l40s": "vllm/vllm-openai:v0.31.0-l40s"},
    "gpu": {"count": 1, "classes": ["h100", "l40s"]},
    "deployments": {
        "eu-north1": {
            "variants": [
                {"name": "reserved", "pool": "h100-res-1x", "min": 1, "max": 2},
                {"name": "spot", "pool": "h100-spot-1x", "min": 0, "max": 4},
                {"name": "cheap", "pool": "l40s-spot-1x", "min": 0, "max": 2},
            ]
        }
    },
    "routing": {"field": "tier", "map": {"fast": "reserved", "budget": "cheap"}, "default": "priority"},
}


@pytest.fixture(autouse=True)
def _fleet(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)


def test_entry_carries_variants_with_per_class_images():
    e = models.to_entry(SPEC)
    d = e["deployments"]["hub"]
    assert d["pool"] == "h100-res-1x" and [v["name"] for v in d["variants"]] == ["reserved", "spot", "cheap"]
    assert d["variants"][0]["min"] == 1 and d["variants"][1]["max"] == 4
    assert d["variants"][2]["image"].endswith("vllm/vllm-openai:v0.31.0-l40s") and d["variants"][0]["image"].endswith(
        "vllm/vllm-openai:v0.31.0"
    )
    assert e["routing"]["default"] == "priority"
    vs = models.variants(e, "hub")
    assert [v["object"] for v in vs] == ["chat", "chat-spot", "chat-cheap"]
    assert models.variants({"id": "plain", "deployments": {"hub": {"pool": "h100-spot-1x"}}}, "hub") == [
        {"name": "primary", "pool": "h100-spot-1x", "min": None, "max": None, "image": None, "object": "plain"}
    ]
    # a single deployment of a class with its own image takes it
    single = models.to_entry(
        {
            "id": "one",
            "kind": "endpoint",
            "image": "img:default",
            "images": {"h100": "img:h100"},
            "gpu": {"count": 1, "classes": ["h100"]},
        }
    )
    assert single["deployments"]["hub"]["image"].endswith("img:h100")


def test_validation_of_variants_and_routing():
    def bad(patch, code=400):
        with pytest.raises(HTTPException) as e:
            models.to_entry({**SPEC, **patch})
        assert e.value.status_code == code

    bad({"deployments": {"eu-north1": {"variants": [{"name": "x", "pool": "nope"}]}}})
    bad({"deployments": {"mars": {"variants": [{"pool": "h100-res-1x"}]}}})
    bad(
        {
            "deployments": {
                "eu-north1": {"variants": [{"name": "a", "pool": "h100-res-1x"}, {"name": "a", "pool": "h100-spot-1x"}]}
            }
        }
    )
    bad({"deployments": {"eu-north1": {"variants": [{"name": "a", "pool": "h100-res-1x", "min": 3, "max": 1}]}}})
    bad({"routing": {"field": "tier", "map": {"fast": "unknown"}}})
    bad({"kind": "job", "command": "x", "routing": {"field": "tier"}})


def test_each_variant_renders_its_own_objects_under_the_model_label():
    e = models.to_entry(SPEC)
    docs = models.render(e, "hub")
    isvcs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "InferenceService"}
    assert set(isvcs) == {"chat", "chat-spot", "chat-cheap"}
    assert (
        isvcs["chat"]["spec"]["predictor"]["minReplicas"] == 1
        and isvcs["chat-spot"]["spec"]["predictor"]["maxReplicas"] == 4
    )
    assert isvcs["chat-cheap"]["spec"]["predictor"]["containers"][0]["image"].endswith("-l40s")
    assert isvcs["chat-cheap"]["spec"]["predictor"]["nodeSelector"]["serverless2.nebius/pool"] == "l40s-spot-1x"
    for name, i in isvcs.items():
        assert i["metadata"]["labels"]["serverless2.nebius/model"] == "chat"
    assert (
        isvcs["chat-spot"]["metadata"]["labels"]["serverless2.nebius/variant"] == "spot"
        and "serverless2.nebius/variant" not in isvcs["chat"]["metadata"]["labels"]
    )
    policies = {d["metadata"]["name"]: d for d in docs if d["kind"] == "SecurityPolicy"}
    assert set(policies) == {"chat-authorization", "chat-spot-authorization", "chat-cheap-authorization"}
    assert all(p["spec"]["extAuth"]["http"]["path"] == "/internal/authorize/chat" for p in policies.values())
    assert (
        policies["chat-spot-authorization"]["spec"]["targetSelectors"][0]["matchLabels"]["serving.knative.dev/route"]
        == "chat-spot-predictor"
    )


def test_hostnames_and_litellm_deployments_follow_the_variants(monkeypatch):
    e = models.to_entry(SPEC)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.1.sslip.io")
    assert models.variant_hosts(e, "eu-north1") == [
        "chat-predictor.models.203.0.113.1.sslip.io",
        "chat-spot-predictor.models.203.0.113.1.sslip.io",
        "chat-cheap-predictor.models.203.0.113.1.sslip.io",
    ]
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, ""

    class C:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def patch(self, url, json=None, headers=None):
            calls.append(("patch", url, json))
            return R(404)

        def post(self, url, json=None, headers=None):
            calls.append(("post", url, json))
            return R(200)

    import httpx

    monkeypatch.setattr(httpx, "Client", C)
    monkeypatch.setattr(models, "LITELLM_MASTER_KEY", "m")
    monkeypatch.setattr(models, "ROUTER_KEY", "i")
    monkeypatch.setattr(models, "LITELLM_URL", "http://litellm")
    assert models.litellm_group_upsert(e, "hub", "eu-north1") is None
    posted = [c[2] for c in calls if c[0] == "post"]
    assert [p["model_info"]["id"] for p in posted] == ["chat--hub", "chat--hub--spot", "chat--hub--cheap"]
    assert [p["litellm_params"]["order"] for p in posted] == [1, 2, 3]
    assert posted[2]["litellm_params"]["api_base"] == "https://chat-cheap-predictor.models.203.0.113.1.sslip.io/v1"
    calls.clear()
    assert models.litellm_group_delete("chat", "hub", ["spot", "cheap"]) is None
    assert [c[2]["id"] for c in calls] == ["chat--hub", "chat--hub--spot", "chat--hub--cheap"]

    # an entry LiteLLM no longer has is deleted (its 400 "not found in db"); any other 400 is still a failure
    def answer(code, text):
        r = R(code)
        r.text = text
        return r

    C.post = lambda self, url, json=None, headers=None: answer(
        400, "{'error': 'Model with id=chat--hub not found in db'}"
    )
    assert models.litellm_group_delete("chat", "hub", []) is None
    C.post = lambda self, url, json=None, headers=None: answer(400, "{'error': 'bad request'}")
    assert models.litellm_group_delete("chat", "hub", []).startswith("litellm /model/delete 400")


def test_sync_requests_pick_a_variant_by_field_then_by_readiness(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    e = catalog.normalise(models.to_entry(SPEC))
    monkeypatch.setattr(catalog, "get", lambda name: e if name == "chat" else None)
    monkeypatch.setattr(models, "cluster_id", lambda: "hub")
    ready = {"chat": 0, "chat-spot": 2, "chat-cheap": 0}
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda name, ns, region="eu-north1", nodes=1: {
            "status": "ready" if ready.get(name) else "scaled-to-zero",
            "replicas_ready": ready.get(name, 0),
        },
    )
    hit = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": []}

    class AC:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            hit["url"] = url
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", AC)
    assert (
        c.post("/v1/models/chat:invoke", json={"input": {"messages": [], "tier": "budget"}}, headers=H).status_code
        == 200
    )
    assert hit["url"].startswith("http://chat-cheap-predictor.models.svc.cluster.local")
    c.post("/v1/models/chat:invoke", json={"input": {"messages": []}}, headers=H)
    assert hit["url"].startswith("http://chat-spot-predictor.models.svc.cluster.local")  # the first ready variant
    ready.update({"chat-spot": 0})
    c.post("/v1/models/chat:invoke", json={"input": {"messages": []}}, headers=H)
    assert hit["url"].startswith(e["endpoint_host"])  # nothing ready: the first variant
    # the endpoint listing names the variants and their state; a variant object name resolves to the model
    monkeypatch.setattr(
        kube,
        "isvc",
        lambda n, ns, region="eu-north1": {
            "kind": "InferenceService",
            "metadata": {"name": n},
            "spec": {"predictor": {}},
        },
    )
    monkeypatch.setattr(catalog, "all_models", lambda: {"chat": e})
    listed = c.get("/v1/endpoints", headers=H).json()
    chat = next(x for x in listed if x["model"] == "chat")
    assert [v["name"] for v in chat["variants"]] == ["reserved", "spot", "cheap"] and chat["routing"]["field"] == "tier"
    assert appmod._endpoint_model("chat-cheap")["name"] == "chat"
