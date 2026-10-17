"""The native endpoint renderer (services/api/render_endpoint.py) against its record: one hash per rendered object
and case in tests/golden/endpoint.json (key order is free, no key or value may differ, the object order is the apply
order). Cases: the endpoint fixtures of tests/catalog, the multi-node and variant specs of the other tests, and a
spec that exercises the remaining settings (pull secrets, Secret env, read-only seeded weights, scratch volumes).

Regenerate after a deliberate change of the renderer:  .venv-api/bin/python services/api/tests/test_render_endpoint_golden.py --write
"""

import contextlib
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if __name__ == "__main__":
    import conftest  # noqa: F401  (`--write`: the pytest environment, before config is read)
import catalog  # noqa: E402
import config  # noqa: E402
import kube
import models
import render_endpoint
import test_api  # noqa: E402
import test_multinode_endpoints as multinode  # noqa: E402
import test_variants as variants  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
GOLDEN = os.path.join(HERE, "golden", "endpoint.json")  # {case: [{kind, name, sha256}]}
DOMAIN = "203.0.113.1.sslip.io"
IB_CLAIM_TEMPLATE = "ib"
REGIONS = {"hub": "eu-north1"}  # cluster id -> region (every other id is its own region)


def _fixture(name: str) -> dict:
    """A file endpoint of tests/catalog as the catalog loads it (a spec plus its placement)."""
    with open(os.path.join(HERE, "catalog", f"{name}.yaml")) as f:
        return catalog.endpoint_file(yaml.safe_load(f))


@contextlib.contextmanager
def _fleet(pools: dict):
    """The placement reads the fleet's pools; the tests of the other modules patch the same two functions."""
    before = kube.fleet, kube.cluster_region
    kube.fleet, kube.cluster_region = (lambda: pools), (lambda c: REGIONS.get(c, c))
    try:
        yield
    finally:
        kube.fleet, kube.cluster_region = before


def _definition(spec: dict, pools: dict) -> dict:
    with _fleet(pools):
        return models.definition(spec)


EXTRA_METADATA = {
    "labels": {"example.org/startup-profile": "qualified-science"},
    "annotations": {"example.org/qualification": "evidence/model-immutable.json"},
}

# every setting the renderer reads, in one spec: pull secrets, Secret env, read-only seeded weights (prepared
# nonetheless), a scratch volume in memory, an rps-scaled replica floor, a scale-to-zero retention and window
STORAGE_EXAMPLE = {
    "id": "storage-example",
    "kind": "endpoint",
    "display_name": "Storage example",
    "task": "inference",
    "protocol": "http",
    "port": 9000,
    "gpu": {"count": 2, "classes": ["h100"]},
    "image": "registry.serverless2.local/nvcr/example/storage:1.0",
    "pull_secret": "ngc",
    "command": ["serve"],
    "args": ["--port", "9000"],
    "env": {"LOG_LEVEL": "info"},
    "env_secrets": ["storage-keys"],
    "resources": {"cpu": "16", "memory": "64Gi", "ephemeral_storage": "100Gi"},
    "scaling": {"min": 1, "max": 3, "metric": "requests_per_second", "target": 10, "idle_s": 300, "window_s": 60},
    "timeout_s": 120,
    "shm_gib": 8,
    "empty_dirs": [{"name": "scratch", "mount_path": "/scratch", "size_limit": "20Gi", "memory": True}],
    "health_path": "/ready",
    "weights": {
        "path": "seeded/storage",
        "mount_path": "/models",
        "read_only": True,
        "prepare": True,
        "env": {"MODEL_DIR": "/models", "CACHE": "/scratch"},
    },
}
STORAGE_PLACEMENT = {"hub": {"pool": "h100-spot-2x"}}


def cases() -> dict[str, tuple[dict, str]]:
    """{golden name: (definition, cluster)}."""
    scaling_demo = {
        "id": "scaling-demo",
        "image": "nginx:latest",
        "kind": "endpoint",
        "port": 80,
        "regions": ["eu-north1", "eu-south1"],
        "scaling": {
            "min": 0,
            "max": 4,
            "metric": "requests_per_second",
            "target": 100,
            "cooldown_s": 30,
            "window_s": 20,
            "idle_s": 180,
        },
    }
    metadata_endpoint = {
        "id": "metadata-endpoint",
        "kind": "endpoint",
        "image": "example.org/model:1",
        "gpu": {"classes": ["h100"], "count": 1},
        "pod_metadata": EXTRA_METADATA,
        "automount_service_account_token": False,
    }
    my_llm = {
        "id": "my-llm",
        "kind": "endpoint",
        "image": "vllm/vllm-openai:v0.11.0",
        "args": ["--model", "example-org/small-chat-model"],
        "port": 8000,
        "protocol": "openai",
        "health_path": "/health",
        "served_model": "example-org/small-chat-model",
        "gpu": {"count": 1, "classes": ["h100"]},
        "scaling": {"min": 0, "max": 2},
        "env": {"HF_HOME": "/weights"},
        "weights": {"path": "my-llm"},
        "images": {"default": "ignored-when-image-is-set"},
    }
    ib = {
        **multinode.SPEC,
        "id": "ib",
        "gpu": {"count": 8, "classes": ["h100"]},
        "regions": ["hub"],
        "interconnect": "required",
        "scaling": {"min": 1, "max": 1},
        "automount_service_account_token": False,
        "pod_metadata": EXTRA_METADATA,
        "pull_secret": "ngc",
    }
    return {
        "llm-example-hub": (_fixture("llm-example"), "hub"),
        "llm-example-eu-south1": (_fixture("llm-example"), "eu-south1"),
        "ws-example-eu-south1": (_fixture("ws-example"), "eu-south1"),
        "http-example-hub": (_fixture("http-example"), "hub"),
        "storage-example-hub": (models.definition(STORAGE_EXAMPLE, deployments=STORAGE_PLACEMENT), "hub"),
        "my-llm-hub": (_definition(my_llm, test_api.FLEET), "hub"),
        "scaling-demo-hub": (_definition(scaling_demo, test_api.FLEET), "hub"),
        "metadata-endpoint-hub": (_definition(metadata_endpoint, test_api.FLEET), "hub"),
        "chat-hub": (_definition(variants.SPEC, variants.VARIANT_FLEET), "hub"),
        "kimi-k3-us-central1": (_definition(multinode.SPEC, multinode.POOLS), "us-central1"),
        "ib-hub": (_definition(ib, multinode.POOLS), "hub"),
    }


def remote(cluster: str) -> bool:
    """A cluster other than the API's own (config.REGION, eu-north1 in the tests): the gateway authorizes through the
    `api` Backend there, through the Service on the API's cluster (services/api/models.py `render`)."""
    return REGIONS.get(cluster, cluster) != config.REGION


def native(d: dict, cluster: str) -> list[dict]:
    """models.render as the API runs it (domain and claim template fixed)."""
    return [
        doc
        for v in models.variants(d, cluster)
        for doc in render_endpoint.render(d, cluster, DOMAIN, IB_CLAIM_TEMPLATE, v, remote(cluster))
    ]


def digests(objects: list[dict]) -> list[dict]:
    """One hash per rendered object (kind, name, sha256 of the canonical JSON): what the record holds."""
    import hashlib
    import json

    return [
        {
            "kind": d["kind"],
            "name": d["metadata"]["name"],
            "sha256": hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        for d in objects
    ]


def golden(name: str) -> list[dict]:
    import json

    with open(GOLDEN) as f:
        return json.load(f)[name]


@pytest.mark.parametrize("name", sorted(cases()))
def test_native_rendering_matches_the_record(name):
    """Every object the renderer produces for the case equals the recorded one (a hash per object; a deliberate change
    is recorded with `python test_render_endpoint_golden.py --write`, and the diff of the record names the objects)."""
    d, cluster = cases()[name]
    got, want = digests(native(d, cluster)), golden(name)
    changed = [g["kind"] + "/" + g["name"] for g, w in zip(got, want) if g != w] + [
        g["kind"] + "/" + g["name"] for g in got[len(want) :]
    ]
    assert got == want, f"{name}: changed objects {changed or [w['kind'] + '/' + w['name'] for w in want[len(got):]]}"


def test_every_recorded_case_exists():
    import json

    assert set(json.load(open(GOLDEN))) == set(cases())


@pytest.fixture(autouse=True)
def _regions(monkeypatch):
    monkeypatch.setattr(kube, "cluster_region", lambda c: REGIONS.get(c, c))


def test_a_models_own_env_entry_replaces_the_platform_default():
    """kimi-k3 sets NCCL_IB_DISABLE itself: one entry per name, the model's value (server-side apply refuses duplicates)."""
    d, cluster = cases()["kimi-k3-us-central1"]
    own = {"NCCL_IB_DISABLE": "0", "NCCL_SOCKET_IFNAME": "eth1"}
    patched = {**d, "spec": {**d["spec"], "env": {**(d["spec"].get("env") or {}), **own}}}
    lws = next(x for x in native(patched, cluster) if x["kind"] == "LeaderWorkerSet")
    for tmpl in ("leaderTemplate", "workerTemplate"):
        env = lws["spec"]["leaderWorkerTemplate"][tmpl]["spec"]["containers"][0]["env"]
        names = [e["name"] for e in env]
        assert len(names) == len(set(names))
        values = {e["name"]: e.get("value") for e in env}
        assert values["NCCL_IB_DISABLE"] == "0" and values["NCCL_SOCKET_IFNAME"] == "eth1"


def test_rendering_errors_name_the_object():
    from fastapi import HTTPException

    d, _ = cases()["storage-example-hub"]
    with pytest.raises(HTTPException) as e:
        native({**d, "image": ""}, "hub")
    assert e.value.status_code == 400 and "storage-example" in e.value.detail and "image is required" in e.value.detail
    with pytest.raises(HTTPException):
        native({"id": "run-class", "mode": "run", "job": {}}, "hub")


if __name__ == "__main__" and "--write" in sys.argv:
    import json

    kube.cluster_region = lambda c: REGIONS.get(c, c)
    json.dump(
        {name: digests(native(d, cluster)) for name, (d, cluster) in sorted(cases().items())},
        open(GOLDEN, "w"),
        indent=1,
    )
    open(GOLDEN, "a").write("\n")
    print(f"wrote {GOLDEN}")
