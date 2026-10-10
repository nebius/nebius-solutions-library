"""Multi-node endpoints (docs/API.md "Multi-node endpoints"): a definition with `nodes` > 1 renders a LeaderWorkerSet of
whole-node pods behind a Service and an HTTPRoute that carry a KServe predictor's name, so the SecurityPolicy, the
certificate and the LiteLLM entry are the same as for a one-pod endpoint; scaling.min 1 | 0 starts and stops it."""
import os
import subprocess

import pytest
import yaml
from fastapi import HTTPException

import kube
import models

POOLS = {"pools": {
    "us-central1-b200-spot-1x": {"region": "us-central1", "pool": "b200-spot-1x", "gpu_class": "b200", "gpus_per_node": 1, "capacity": "spot"},
    "us-central1-b200-ondemand-8x": {"region": "us-central1", "pool": "b200-ondemand-8x", "gpu_class": "b200", "gpus_per_node": 8, "capacity": "on_demand"},
    "hub-h100-spot-8x": {"region": "hub", "pool": "h100-spot-8x", "gpu_class": "h100", "gpus_per_node": 8, "capacity": "spot"},
    "hub-h100-spot-8x-ib": {"region": "hub", "pool": "h100-spot-8x-ib", "gpu_class": "h100", "gpus_per_node": 8, "capacity": "spot",
                            "interconnect": "infiniband", "ib_devices_per_node": 8},
}}

SPEC = {"id": "kimi-k3", "kind": "endpoint", "image": "vllm/vllm-openai:v0.31.0", "protocol": "openai", "port": 8000,
        "command": "vllm serve /weights/kimi --tensor-parallel-size 8 --pipeline-parallel-size 2",
        "worker_command": "ray start --address=$LWS_LEADER_ADDRESS:6379 --block", "health_path": "/health",
        "gpu": {"count": 8, "classes": ["b200"]}, "nodes": 2, "resources": {"cpu": "120", "memory": "1500Gi"},
        "scaling": {"min": 0, "max": 1, "idle_s": 1800}, "shm_gib": 32, "weights": {"path": "kimi-k3", "env": {"HF_HOME": "/weights"}},
        "regions": ["us-central1"]}


@pytest.fixture(autouse=True)
def fleet(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: POOLS)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "195.242.0.1.sslip.io")


def test_pool_for_takes_whole_nodes_and_honours_the_interconnect():
    assert models.pool_for("us-central1", ["b200"], 1) == "b200-spot-1x"                       # one pod per replica: the smallest preset
    assert models.pool_for("us-central1", ["b200"], 8, nodes=2) == "b200-ondemand-8x"           # whole nodes: exactly 8 GPUs per node
    assert models.pool_for("us-central1", ["b200"], 1, nodes=2) == "b200-spot-1x"                # two whole 1-GPU nodes is a whole-node pool too
    assert models.pool_for("us-central1", ["b200"], 4, nodes=2) is None                          # no preset with 4 GPUs per node
    assert models.pool_for("hub", ["h100"], 8, nodes=2) == "h100-spot-8x"                        # none: plain pools first (scale from zero)
    assert models.pool_for("hub", ["h100"], 8, nodes=2, interconnect="preferred") == "h100-spot-8x-ib"
    assert models.pool_for("hub", ["h100"], 8, nodes=2, interconnect="required") == "h100-spot-8x-ib"
    assert models.pool_for("us-central1", ["b200"], 8, nodes=2, interconnect="required") is None


def test_entry_of_a_two_node_endpoint():
    e = models.to_entry(SPEC)
    assert e["nodes"] == 2 and e["runtime"]["nodes"] == 2 and e["runtime"]["interconnect"] == "none"
    assert e["runtime"]["workerCommand"] == ["/bin/sh", "-c", SPEC["worker_command"]]
    assert e["runtime"]["idleSeconds"] == 1800 and e["runtime"]["scaling"]["minReplicas"] == 0
    assert e["deployments"] == {"us-central1": {"pool": "b200-ondemand-8x"}}
    ib = models.to_entry({**SPEC, "id": "ib", "gpu": {"count": 8, "classes": ["h100"]}, "regions": ["hub"], "interconnect": "required"})
    assert ib["deployments"] == {"hub": {"pool": "h100-spot-8x-ib", "ibDevices": 8}}


def test_validation_of_multi_node_fields():
    with pytest.raises(HTTPException) as e:
        models.to_entry({**SPEC, "gpu": {"count": 4, "classes": ["b200"]}})
    assert "no pool" in e.value.detail
    with pytest.raises(HTTPException):
        models.to_entry({**SPEC, "interconnect": "fabric"})
    with pytest.raises(HTTPException):
        models.to_entry({**SPEC, "scaling": {"min": 1, "max": 1, "buffer": 1}})
    one = models.to_entry({**SPEC, "nodes": 1, "gpu": {"count": 1, "classes": ["b200"]}})
    assert "nodes" not in one and "nodes" not in one["runtime"]                                 # a plain endpoint is unchanged


def test_chart_renders_a_leaderworkerset_behind_a_predictor_route():
    chart = os.path.join(os.path.dirname(__file__), "..", "..", "..", "charts", "endpoint")
    if not os.path.isdir(chart) or subprocess.run(["helm", "version"], capture_output=True).returncode:
        pytest.skip("helm or the chart is not available here")
    e = models.to_entry(SPEC)
    values = os.path.join(os.path.dirname(__file__), "_lws_entry.yaml")
    with open(values, "w") as f:
        yaml.safe_dump(e, f, sort_keys=False)
    try:
        r = subprocess.run(["helm", "template", "kimi-k3", chart, "-f", values, "--set", "cluster=us-central1", "--set", "domain=195.242.0.1.sslip.io"],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(values)
    assert r.returncode == 0, r.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(r.stdout) if d}
    assert set(docs) == {("LeaderWorkerSet", "kimi-k3"), ("Service", "kimi-k3-predictor"), ("HTTPRoute", "kimi-k3-predictor"), ("SecurityPolicy", "kimi-k3-authorization")}
    lws = docs[("LeaderWorkerSet", "kimi-k3")]
    assert lws["spec"]["replicas"] == 0 and lws["spec"]["leaderWorkerTemplate"]["size"] == 2
    leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]
    worker = lws["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
    assert leader["nodeSelector"] == {"serverless2.nebius/pool": "b200-ondemand-8x"}
    assert leader["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "8"
    assert leader["containers"][0]["command"][2].startswith("vllm serve") and worker["containers"][0]["command"][2].startswith("ray start")
    env = {v["name"]: v.get("value") for v in leader["containers"][0]["env"]}
    assert env["NNODES"] == "2" and env["GPUS_PER_NODE"] == "8" and env["WORLD_SIZE"] == "16" and env["MASTER_ADDR"] == "kimi-k3-0.kimi-k3" and env["HF_HOME"] == "/weights"
    assert "readinessProbe" in leader["containers"][0] and "readinessProbe" not in worker["containers"][0]
    assert "resourceClaims" not in leader                                                        # Ethernet: no DRA claim
    route = docs[("HTTPRoute", "kimi-k3-predictor")]
    assert route["spec"]["hostnames"] == ["kimi-k3-predictor.models.195.242.0.1.sslip.io"]
    assert route["metadata"]["labels"]["serving.knative.dev/route"] == "kimi-k3-predictor"     # the SecurityPolicy's selector
    assert docs[("Service", "kimi-k3-predictor")]["spec"]["selector"]["leaderworkerset.sigs.k8s.io/worker-index"] == "0"


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
            class R: items = []
            return R()

    monkeypatch.setattr(kube, "api", lambda region=None: Custom())
    monkeypatch.setattr(kube, "core", lambda region=None: Core())
    kube._isvc_cache.clear()
    assert kube.lws("kimi-k3", "models", "us-central1")["spec"]["replicas"] == 0
    assert kube.endpoint_status("kimi-k3", "models", "us-central1", nodes=2)["status"] == "scaled-to-zero"
    kube._last_stamp.clear()
    kube.stamp_last_request("kimi-k3", "models", "us-central1")
    assert calls and all(c == ("leaderworkerset.x-k8s.io", "v1", "models", "leaderworkersets", "kimi-k3") for c in calls)
