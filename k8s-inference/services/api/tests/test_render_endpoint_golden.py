"""The native endpoint renderer (services/api/render_endpoint.py) against the objects `helm template charts/endpoint`
produced: one golden file per case under tests/golden/endpoint/<name>.yaml, written from the chart. The renderer
must reproduce every file exactly (parsed YAML: key order is free, no key or value may differ, the document
order is the chart's), and the chart must still produce the files (skipped without helm), so neither can drift
while both exist. Cases: the endpoint fixtures of tests/catalog, the multi-node and variant specs of the other
tests, and entries that exercise the remaining settings (claims, caches, probes, Kueue placement, pod metadata).

Regenerate after a deliberate change of the chart:  .venv-api/bin/python services/api/tests/test_render_endpoint_golden.py --write
"""
import contextlib
import glob
import os
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_api                                   # noqa: E402  (sets the test environment: REGION, CATALOG_DIRS, ...)
import test_multinode_endpoints as multinode      # noqa: E402
import test_variants as variants                  # noqa: E402
import kube, models, render_endpoint              # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CHART = os.path.join(ROOT, "charts", "endpoint")
GOLDEN = os.path.join(HERE, "golden", "endpoint")
DOMAIN = "203.0.113.1.sslip.io"
IB_CLAIM_TEMPLATE = "ib"
REGIONS = {"hub": "eu-north1"}                    # cluster id -> region (every other id is its own region)


def _fixture(name: str) -> dict:
    with open(os.path.join(HERE, "catalog", f"{name}.yaml")) as f:
        return yaml.safe_load(f)


@contextlib.contextmanager
def _fleet(pools: dict):
    """to_entry reads the fleet's pools; the tests of the other modules patch the same two functions."""
    before = kube.fleet, kube.cluster_region
    kube.fleet, kube.cluster_region = (lambda: pools), (lambda c: REGIONS.get(c, c))
    try:
        yield
    finally:
        kube.fleet, kube.cluster_region = before


def _entry(spec: dict, pools: dict) -> dict:
    with _fleet(pools):
        return models.to_entry(spec)


EXTRA_METADATA = {"labels": {"example.org/startup-profile": "qualified-science"},
                  "annotations": {"example.org/qualification": "evidence/model-immutable.json"}}

# every setting the chart reads, in one entry: claims kept across scale-to-zero, a node-local model cache, both
# probes, a per-pool image, extra selectors and tolerations, pull secrets, Secret env, read-only seeded weights
STORAGE_EXAMPLE = {
    "id": "storage-example", "displayName": "Storage example", "task": "inference", "mode": "sync", "protocol": "http", "port": 9000,
    "gpu": {"count": 2, "classes": ["h100"]}, "labels": {"example.org/team": "science"}, "annotations": {"example.org/owner": "ops"},
    "runtime": {
        "image": "registry.serverless2.local/nvcr/example/storage:1.0", "imagePullSecrets": ["ngc"], "command": ["serve"], "args": ["--port", "9000"],
        "workingDir": "/srv", "port": 9000, "protocol": "http",
        "env": [{"name": "LOG_LEVEL", "value": "info"}], "envFrom": [{"secretRef": {"name": "storage-keys"}}],
        "resources": {"gpu": 2, "cpu": "16", "memory": "64Gi", "ephemeralStorage": "100Gi"},
        "nodeSelector": {"example.org/zone": "a"}, "tolerations": [{"key": "example.org/dedicated", "operator": "Exists", "effect": "NoSchedule"}],
        "scaling": {"minReplicas": 1, "maxReplicas": 3, "metric": "rps", "target": 10, "containerConcurrency": 0, "scaleToZeroRetention": "5m"},
        "timeout": 120, "priorityClassName": "serverless2-interactive", "queue": {"priorityClass": "science"},
        "images": {"h100-spot-2x": "registry.serverless2.local/nvcr/example/storage:1.0-h100"},
        "shm": {"enabled": True, "size": "8Gi"},
        "volumes": [{"name": "scratch", "mountPath": "/scratch", "sizeLimit": "20Gi", "medium": "Memory"}],
        "persistentVolumes": [{"name": "index", "mountPath": "/index", "size": "200Gi", "storageClass": "shared-rwx"},
                              {"name": "state", "mountPath": "/state", "size": "10Gi"}],
        "readinessProbe": {"path": "/ready", "port": 9001, "periodSeconds": 5, "failureThreshold": 3},
        "livenessProbe": {"path": "/live", "initialDelaySeconds": 60},
        "securityContext": {"runAsUser": 1000}, "containerSecurityContext": {"readOnlyRootFilesystem": True},
        "localModelCache": {"enabled": True, "storageUri": "s3://weights/storage-example", "nodeGroups": ["h100"], "size": "120Gi"},
        "weights": {"sharedFilesystem": {"enabled": True, "path": "seeded/storage", "mountPath": "/models", "readOnly": True, "claimName": "weights-ro"},
                    "env": {"MODEL_DIR": "/models", "CACHE": "/scratch"}},
        "annotations": {"autoscaling.knative.dev/window": "60s"},
    },
    "deployments": {"hub": {"pool": "h100-spot-2x", "price_per_gpu_hour": 2.5, "paused": False, "parameters": {}}},
}

# Kueue places the pods (queue.name): no pool selector, the per-pool image is not used, the extra selector stays
KUEUE_EXAMPLE = {
    "id": "kueue-example", "task": "inference", "mode": "sync",
    "runtime": {"image": "registry.serverless2.local/docker/library/nginx:1.27", "port": 80, "protocol": "http",
                "resources": {"gpu": 1, "cpu": "2", "memory": "4Gi"}, "queue": {"name": "prefer-h100"}, "nodeSelector": {"example.org/zone": "b"},
                "images": {"h100-spot-1x": "registry.serverless2.local/docker/library/nginx:1.27-h100"}, "shm": {"enabled": False},
                "weights": {"sharedFilesystem": {"enabled": True, "prepare": False}}},
    "deployments": {"hub": {"pool": "h100-spot-1x"}},
}


def cases() -> dict[str, tuple[dict, str]]:
    """{golden name: (entry, cluster)}."""
    scaling_demo = {"id": "scaling-demo", "image": "nginx:latest", "kind": "endpoint", "port": 80, "regions": ["eu-north1", "eu-south1"],
                    "scaling": {"min": 0, "max": 4, "metric": "requests_per_second", "target": 100, "cooldown_s": 30, "window_s": 20, "idle_s": 180}}
    metadata_endpoint = {"id": "metadata-endpoint", "kind": "endpoint", "image": "example.org/model:1", "gpu": {"classes": ["h100"], "count": 1},
                         "pod_metadata": EXTRA_METADATA, "automount_service_account_token": False}
    my_llm = {"id": "my-llm", "kind": "endpoint", "image": "vllm/vllm-openai:v0.11.0", "args": ["--model", "example-org/small-chat-model"],
              "port": 8000, "protocol": "openai", "health_path": "/health", "served_model": "example-org/small-chat-model",
              "gpu": {"count": 1, "classes": ["h100"]}, "scaling": {"min": 0, "max": 2}, "env": {"HF_HOME": "/weights"},
              "weights": {"path": "my-llm"}, "images": {"default": "ignored-when-image-is-set"}}
    ib = {**multinode.SPEC, "id": "ib", "gpu": {"count": 8, "classes": ["h100"]}, "regions": ["hub"], "interconnect": "required",
          "scaling": {"min": 1, "max": 1}, "automount_service_account_token": False, "pod_metadata": EXTRA_METADATA, "pull_secret": "ngc"}
    return {
        "llm-example-hub": (_fixture("llm-example"), "hub"),
        "llm-example-eu-south1": (_fixture("llm-example"), "eu-south1"),
        "ws-example-eu-south1": (_fixture("ws-example"), "eu-south1"),
        "http-example-hub": (_fixture("http-example"), "hub"),
        "storage-example-hub": (STORAGE_EXAMPLE, "hub"),
        "kueue-example-hub": (KUEUE_EXAMPLE, "hub"),
        "my-llm-hub": (_entry(my_llm, test_api.FLEET), "hub"),
        "scaling-demo-hub": (_entry(scaling_demo, test_api.FLEET), "hub"),
        "metadata-endpoint-hub": (_entry(metadata_endpoint, test_api.FLEET), "hub"),
        "chat-hub": (_entry(variants.SPEC, variants.VARIANT_FLEET), "hub"),
        "kimi-k3-us-central1": (_entry(multinode.SPEC, multinode.POOLS), "us-central1"),
        "ib-hub": (_entry(ib, multinode.POOLS), "hub"),
    }


def native(entry: dict, cluster: str) -> list[dict]:
    """models.render as the API runs it (domain and claim template fixed)."""
    return [d for v in models.variants(entry, cluster) for d in render_endpoint.render(entry, cluster, DOMAIN, IB_CLAIM_TEMPLATE, v)]


def helm(entry: dict, cluster: str) -> list[dict]:
    """`helm template charts/endpoint` the way models.render called it before 0.11.14: once per variant."""
    out: list[dict] = []
    for v in models.variants(entry, cluster):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(render_endpoint.variant_entry(entry, cluster, v), f, sort_keys=False)
        try:
            cmd = ["helm", "template", v["object"], CHART, "-f", f.name, "--set", f"cluster={cluster}", "--set", f"domain={DOMAIN}",
                   "--set", f"ibClaimTemplate={IB_CLAIM_TEMPLATE}"]
            if v["object"] != entry["id"]:
                cmd += ["--set", f"variantName={v['object']}", "--set", f"variant={v['name']}"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(f.name)
        assert r.returncode == 0, r.stderr
        out += [d for d in yaml.safe_load_all(r.stdout) if d]
    return out


def golden(name: str) -> list[dict]:
    with open(os.path.join(GOLDEN, f"{name}.yaml")) as f:
        return [d for d in yaml.safe_load_all(f) if d]


@pytest.fixture(autouse=True)
def _regions(monkeypatch):
    monkeypatch.setattr(kube, "cluster_region", lambda c: REGIONS.get(c, c))


@pytest.mark.parametrize("name", sorted(cases()))
def test_native_rendering_matches_the_golden_objects(name):
    entry, cluster = cases()[name]
    assert native(entry, cluster) == golden(name)


def test_every_golden_file_has_a_case():
    assert {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(GOLDEN, "*.yaml"))} == set(cases())


def test_models_render_uses_the_native_renderer(monkeypatch):
    monkeypatch.setattr(models, "endpoint_domain", lambda region: DOMAIN)
    monkeypatch.setattr(models, "INFINIBAND_CLAIM_TEMPLATE", IB_CLAIM_TEMPLATE)
    entry, cluster = cases()["ib-hub"]
    assert models.render(entry, cluster) == golden("ib-hub")


@pytest.mark.parametrize("name", sorted(cases()))
def test_the_chart_still_produces_the_golden_objects(name):
    if not shutil.which("helm") or not os.path.isdir(CHART):
        pytest.skip("helm or charts/endpoint is not available here")
    entry, cluster = cases()[name]
    assert helm(entry, cluster) == golden(name)


def test_rendering_errors_name_the_object():
    from fastapi import HTTPException
    entry = {**KUEUE_EXAMPLE, "runtime": {**KUEUE_EXAMPLE["runtime"], "image": ""}}
    with pytest.raises(HTTPException) as e:
        native(entry, "hub")
    assert e.value.status_code == 400 and "kueue-example" in e.value.detail and "image is required" in e.value.detail
    with pytest.raises(HTTPException):
        native({"id": "run-class", "mode": "run", "job": {}}, "hub")


if __name__ == "__main__" and "--write" in sys.argv:
    os.makedirs(GOLDEN, exist_ok=True)
    kube.cluster_region = lambda c: REGIONS.get(c, c)
    for name, (entry, cluster) in cases().items():
        with open(os.path.join(GOLDEN, f"{name}.yaml"), "w") as f:
            yaml.safe_dump_all(helm(entry, cluster), f, sort_keys=False)
        print(f"wrote {name}.yaml")
