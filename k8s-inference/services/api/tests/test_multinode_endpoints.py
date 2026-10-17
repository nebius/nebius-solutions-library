"""Multi-node endpoints (docs/API.md "Multi-node endpoints"): a definition with `nodes` > 1 renders a LeaderWorkerSet of
whole-node pods behind a Service and an HTTPRoute that carry a KServe predictor's name, so the SecurityPolicy, the
certificate and the LiteLLM entry are the same as for a one-pod endpoint; scaling.min 1 | 0 starts and stops it."""

import pytest
from fastapi import HTTPException

import kube
import models
import render_endpoint

POOLS = {
    "pools": {
        "us-central1-b200-spot-1x": {
            "region": "us-central1",
            "pool": "b200-spot-1x",
            "gpu_class": "b200",
            "gpus_per_node": 1,
            "capacity": "spot",
        },
        "us-central1-b200-ondemand-8x": {
            "region": "us-central1",
            "pool": "b200-ondemand-8x",
            "gpu_class": "b200",
            "gpus_per_node": 8,
            "capacity": "on_demand",
        },
        "hub-h100-spot-8x": {
            "region": "hub",
            "pool": "h100-spot-8x",
            "gpu_class": "h100",
            "gpus_per_node": 8,
            "capacity": "spot",
        },
        "hub-h100-spot-8x-ib": {
            "region": "hub",
            "pool": "h100-spot-8x-ib",
            "gpu_class": "h100",
            "gpus_per_node": 8,
            "capacity": "spot",
            "interconnect": "infiniband",
            "ib_devices_per_node": 8,
        },
    }
}

SPEC = {
    "id": "kimi-k3",
    "kind": "endpoint",
    "image": "vllm/vllm-openai:v0.31.0",
    "protocol": "openai",
    "port": 8000,
    "command": "vllm serve /weights/kimi --tensor-parallel-size 8 --pipeline-parallel-size 2",
    "worker_command": "ray start --address=$LWS_LEADER_ADDRESS:6379 --block",
    "health_path": "/health",
    "gpu": {"count": 8, "classes": ["b200"]},
    "nodes": 2,
    "resources": {"cpu": "120", "memory": "1500Gi"},
    "scaling": {"min": 0, "max": 1, "idle_s": 1800},
    "shm_gib": 32,
    "weights": {"path": "kimi-k3", "env": {"HF_HOME": "/weights"}},
    "regions": ["us-central1"],
}


@pytest.fixture(autouse=True)
def fleet(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: POOLS)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.1.sslip.io")


def test_pool_for_takes_whole_nodes_and_honours_the_interconnect():
    assert models.pool_for("us-central1", ["b200"], 1) == "b200-spot-1x"  # one pod per replica: the smallest preset
    assert (
        models.pool_for("us-central1", ["b200"], 8, nodes=2) == "b200-ondemand-8x"
    )  # whole nodes: exactly 8 GPUs per node
    assert (
        models.pool_for("us-central1", ["b200"], 1, nodes=2) == "b200-spot-1x"
    )  # two whole 1-GPU nodes is a whole-node pool too
    assert models.pool_for("us-central1", ["b200"], 4, nodes=2) is None  # no preset with 4 GPUs per node
    assert models.pool_for("hub", ["h100"], 8, nodes=2) == "h100-spot-8x"  # none: plain pools first (scale from zero)
    assert models.pool_for("hub", ["h100"], 8, nodes=2, interconnect="preferred") == "h100-spot-8x-ib"
    assert models.pool_for("hub", ["h100"], 8, nodes=2, interconnect="required") == "h100-spot-8x-ib"
    assert models.pool_for("us-central1", ["b200"], 8, nodes=2, interconnect="required") is None


def test_definition_of_a_two_node_endpoint():
    d = models.definition(SPEC)
    assert d["nodes"] == 2 and d["spec"].get("interconnect", "none") == "none"
    assert render_endpoint.command(d["worker_command"]) == ["/bin/sh", "-c", SPEC["worker_command"]]
    assert d["scaling"]["idle_s"] == 1800 and d["scaling"]["min"] == 0
    assert d["deployments"] == {"us-central1": {"pool": "b200-ondemand-8x"}}
    ib = models.definition(
        {**SPEC, "id": "ib", "gpu": {"count": 8, "classes": ["h100"]}, "regions": ["hub"], "interconnect": "required"}
    )
    assert ib["deployments"] == {"hub": {"pool": "h100-spot-8x-ib", "ib_devices": 8}}


def test_validation_of_multi_node_fields():
    with pytest.raises(HTTPException) as e:
        models.definition({**SPEC, "gpu": {"count": 4, "classes": ["b200"]}})
    assert "no pool" in e.value.detail
    with pytest.raises(HTTPException):
        models.definition({**SPEC, "interconnect": "fabric"})
    with pytest.raises(HTTPException):
        models.definition(
            {**SPEC, "scaling": {"min": 1, "max": 1, "buffer": 1}}
        )  # not a scaling field any more (stored ones are ignored on read)
    one = models.definition({**SPEC, "nodes": 1, "gpu": {"count": 1, "classes": ["b200"]}})
    assert one["nodes"] == 1 and "worker_command" in one["spec"]  # a plain endpoint keeps its spec, renders one pod


def test_leaderworkerset_reads_use_the_namespace_before_the_plural(monkeypatch):
    calls = []

    class Custom:
        def get_namespaced_custom_object(self, group, version, namespace, plural, name, **kw):
            calls.append((group, version, namespace, plural, name))
            return {"kind": "LeaderWorkerSet", "spec": {"replicas": 0}, "metadata": {"annotations": {}}}

        def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body, **kw):
            calls.append((group, version, namespace, plural, name))
            return {}

    class Core:
        def list_namespaced_pod(self, ns, label_selector=None, **kw):
            class R:
                items = []

            return R()

    monkeypatch.setattr(kube, "api", lambda region=None: Custom())
    monkeypatch.setattr(kube, "core", lambda region=None: Core())
    kube._isvc_cache.clear()
    assert kube.lws("kimi-k3", "models", "us-central1")["spec"]["replicas"] == 0
    assert kube.endpoint_status("kimi-k3", "models", "us-central1", nodes=2)["status"] == "scaled-to-zero"
    kube._last_stamp.clear()
    kube.stamp_last_request("kimi-k3", "models", "us-central1")
    assert calls and all(
        c == ("leaderworkerset.x-k8s.io", "v1", "models", "leaderworkersets", "kimi-k3") for c in calls
    )


def test_example_request_travels_with_the_definition():
    import catalog
    import endpoints as ep

    spec = {
        **SPEC,
        "nodes": 1,
        "gpu": {"count": 1, "classes": ["b200"]},
        "example": {"messages": [{"role": "user", "content": "hi"}]},
    }
    m = models.definition(spec)
    assert m["example"] == spec["example"]
    assert catalog.to_public(m)["example"] == spec["example"]
    pub = ep.to_public(m, {"kind": "InferenceService", "metadata": {}, "spec": {"predictor": {}}}, "us-central1")
    assert pub["example"] == spec["example"]
    with pytest.raises(HTTPException):
        models.definition({**spec, "example": "not an object"})


def test_lws_public_shape_and_stopped_invoke(monkeypatch):
    """GET /v1/endpoints of a multi-node endpoint reads the LeaderWorkerSet; :invoke on a stopped one answers 503 with
    the way to start it (docs/API.md "Multi-node endpoints")."""
    import endpoints as ep

    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda name, ns, region=None, nodes=1: {
            "status": "scaled-to-zero",
            "replicas_ready": 0,
            "pods_ready": 0,
            "nodes": nodes,
        },
    )
    m = {
        "k8s_name": "kimi-k3",
        "name": "kimi-k3",
        "display_name": "Kimi",
        "namespace": "models",
        "gpu": "8x b300",
        "protocol": "openai",
        "managed_by": "api",
        "nodes": 2,
        "spec": {"scaling": {"min": 0, "idle_s": 1800}, "timeout_s": 600},
        "example": {"messages": []},
    }
    obj = {
        "kind": "LeaderWorkerSet",
        "metadata": {
            "annotations": {"serverless2.nebius/nodes": "2", "serverless2.nebius/interconnect": "none"},
            "creationTimestamp": "t",
        },
        "spec": {
            "replicas": 0,
            "leaderWorkerTemplate": {
                "size": 2,
                "leaderTemplate": {
                    "spec": {
                        "containers": [{"image": "vllm", "resources": {"requests": {"cpu": "160", "memory": "2000Gi"}}}]
                    }
                },
            },
        },
    }
    pub = ep.to_public(m, obj, "eu-west2")
    assert (
        pub["nodes"] == 2
        and pub["status"] == "scaled-to-zero"
        and pub["min_replicas"] == 0
        and pub["scaling"]["idle_s"] == 1800
    )
    assert (
        pub["example"] == {"messages": []}
        and pub["image"] == "vllm"
        and pub["cpu"] == "160"
        and pub["managed_by"] == "api"
    )
    one = ep.to_public(
        {**m, "nodes": 1},
        {"kind": "InferenceService", "metadata": {}, "spec": {"predictor": {"minReplicas": 1}}},
        "eu-west2",
    )
    assert one["nodes"] == 1 and one["min_replicas"] == 1 and one["example"] == {"messages": []}


def test_idle_shutdown_stops_an_idle_group_and_skips_a_busy_one(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import app as appmod
    import catalog
    import db

    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec = {**SPEC, "scaling": {"min": 1, "max": 1, "idle_s": 1800}}
    idle = {
        "name": "kimi-k3",
        "k8s_name": "kimi-k3",
        "namespace": "models",
        "managed_by": "api",
        "nodes": 2,
        "spec": spec,
        "deployments": {"us-central1": {}},
    }
    busy = {**idle, "name": "busy", "k8s_name": "busy"}
    starting = {**idle, "name": "starting", "k8s_name": "starting"}
    status = {
        "kimi-k3": {"status": "ready", "replicas_ready": 1, "ready_at": old, "last_request_at": old},
        "busy": {"status": "ready", "replicas_ready": 1, "ready_at": old, "last_request_at": fresh},
        "starting": {"status": "deploying", "replicas_ready": 0},
    }
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(catalog, "all_models", lambda: {m["name"]: m for m in (idle, busy, starting)})
    monkeypatch.setattr(catalog, "invalidate", lambda: None)
    monkeypatch.setattr(kube, "endpoint_status", lambda name, ns, region=None, nodes=1: status[name])
    monkeypatch.setattr(kube, "cluster_region", lambda c: c)
    monkeypatch.setattr(db, "get_model", lambda mid: {"version": 7})
    writes = []
    monkeypatch.setattr(
        db,
        "upsert_model",
        lambda entry, spec, by, expected=None, create_only=False, **kw: writes.append(
            (entry["id"], spec["scaling"]["min"], by, expected)
        ),
    )
    assert appmod.idle_shutdown() == ["kimi-k3"]
    assert writes == [("kimi-k3", 0, "idle-shutdown", 7)]
