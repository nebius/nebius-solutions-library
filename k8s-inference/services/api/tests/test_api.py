"""Unit tests: no cluster, no LiteLLM (both are monkeypatched)."""
import copy, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ["CATALOG_DIRS"] = os.path.join(ROOT, "catalog", "models") + ":" + os.path.join(os.path.dirname(__file__), "catalog")   # platform classes + generic endpoint/run-class fixtures
os.environ["PUBLIC_API_URL"] = "https://api.example"
os.environ["ENDPOINT_DOMAIN"] = "203.0.113.10.sslip.io"   # async endpoint calls go through the gateway (docs/JOBS.md)
import pytest, yaml
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException
import app as appmod, auth, catalog, jobs, kube

KEY_INFO = {"key_name": "sk-...abcd", "key_alias": "tenant-demo", "spend": 0.1, "max_budget": 10.0, "models": [],
            "metadata": {"tenant": "demo", "allowed_passthrough_routes": ["/models/llm/chat"]}}
L = "serverless2.nebius"
FLEET = {"pools": {"hub-h100-spot-1x": {"region": "hub", "pool": "h100-spot-1x", "gpu_class": "h100"},
                   "hub-l40s-ondemand-1x": {"region": "hub", "pool": "l40s-ondemand-1x", "gpu_class": "l40s"},
                   "eu-south1-rtx6000-spot-1x": {"region": "eu-south1", "pool": "rtx6000-spot-1x", "gpu_class": "rtx-pro-6000"}},
         "registries": {"hub": "cr.eu-north1.nebius.cloud/e00exampleexampleex", "eu-south1": "cr.eu-south1.nebius.cloud/e07exampleexampleex"}}


class Items:
    def __init__(self, items): self.items = items


class FakeCluster:
    """Jobs, pods, PVCs and Secrets of one cluster, behind the BatchV1Api / CoreV1Api methods jobs.py and
    billing.py call (dicts in, dicts out; sanitize_for_serialization is the identity)."""
    def __init__(self):
        self.jobs, self.pods, self.pvcs, self.secrets, self.namespaces = {}, [], {}, {}, ["tenant-demo"]
        self.custom = {}
        self.api_client = self
        self.deleted = []

    def sanitize_for_serialization(self, o): return o

    def read_namespace(self, name, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(metadata=SimpleNamespace(annotations={}))

    def __getattribute__(self, name):   # swallow the _request_timeout kwarg the retry helper adds (and server-side apply kwargs)
        attr = object.__getattribute__(self, name)
        if callable(attr) and "_namespace" in name:
            return lambda *a, **kw: attr(*a, **{k: v for k, v in kw.items() if k not in ("_request_timeout", "field_manager", "force", "_content_type")})
        return attr

    # configmaps (runtime model catalog, services/api/models.py)
    def list_namespaced_config_map(self, ns, label_selector=""):
        cms = getattr(self, "configmaps", {})
        return Items([v for (n, name), v in cms.items() if n == ns])

    def patch_namespaced_config_map(self, name, ns, body):
        self.__dict__.setdefault("configmaps", {})[(ns, name)] = copy.deepcopy(body)
        return body

    def delete_namespaced_config_map(self, name, ns):
        cms = self.__dict__.setdefault("configmaps", {})
        if (ns, name) not in cms:
            raise ApiException(status=404, reason="NotFound")
        del cms[(ns, name)]

    # batch
    def create_namespaced_job(self, ns, body):
        name = body["metadata"]["name"]
        if (ns, name) in self.jobs:
            raise ApiException(status=409, reason="AlreadyExists")
        if ns not in self.namespaces:
            raise ApiException(status=404, reason="NotFound")
        body = copy.deepcopy(body)
        body["metadata"].update({"namespace": ns, "uid": f"uid-{name}", "creationTimestamp": "2026-10-05T20:00:00Z"})
        body["status"] = {}
        self.jobs[(ns, name)] = body
        return body

    def read_namespaced_job(self, name, ns):
        if (ns, name) not in self.jobs:
            raise ApiException(status=404, reason="NotFound")
        return self.jobs[(ns, name)]

    def list_namespaced_job(self, ns, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(",") if kv)
        return Items([j for (n, _), j in self.jobs.items() if n == ns and all(j["metadata"]["labels"].get(k) == v for k, v in want.items())])

    def patch_namespaced_job(self, name, ns, body):
        j = self.jobs[(ns, name)]
        j["spec"].update(body.get("spec", {}))
        j["metadata"].setdefault("annotations", {}).update((body.get("metadata") or {}).get("annotations") or {})
        return j

    def delete_namespaced_job(self, name, ns, propagation_policy=None):
        self.deleted.append(name)
        del self.jobs[(ns, name)]

    # core
    def list_namespaced_pod(self, ns, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(",") if kv)
        return Items([p for p in self.pods if p["metadata"]["namespace"] == ns and all(p["metadata"]["labels"].get(k) == v for k, v in want.items())])

    def create_namespaced_persistent_volume_claim(self, ns, body):
        if (ns, body["metadata"]["name"]) in self.pvcs:
            raise ApiException(status=409, reason="AlreadyExists")
        self.pvcs[(ns, body["metadata"]["name"])] = body

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if (ns, name) not in self.pvcs:
            raise ApiException(status=404, reason="NotFound")
        return self.pvcs[(ns, name)]

    def patch_namespaced_persistent_volume_claim(self, name, ns, body):
        self.pvcs[(ns, name)]["metadata"].update(body["metadata"])

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        if (ns, name) not in self.pvcs:
            raise ApiException(status=404, reason="NotFound")
        del self.pvcs[(ns, name)]

    def create_namespaced_secret(self, ns, body):
        self.secrets[(ns, body["metadata"]["name"])] = body

    # custom objects (JobSets of multi-node runs; Kueue Workloads are listed empty)
    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        name = body["metadata"]["name"]
        if (ns, plural, name) in self.custom:
            raise ApiException(status=409, reason="AlreadyExists")
        body = copy.deepcopy(body)
        body["metadata"].update({"namespace": ns, "uid": f"uid-{name}", "creationTimestamp": "2026-10-08T12:00:00Z"})
        body["status"] = {}
        self.custom[(ns, plural, name)] = body
        return body

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        if (ns, plural, name) not in self.custom:
            raise ApiException(status=404, reason="NotFound")
        return self.custom[(ns, plural, name)]

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=""):
        want = dict(kv.split("=") for kv in label_selector.split(",") if kv)
        return {"items": [o for (n, p, _), o in self.custom.items() if n == ns and p == plural and all(o["metadata"].get("labels", {}).get(k) == v for k, v in want.items())]}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        if (ns, plural, name) not in self.custom and body.get("kind"):   # server-side apply creates
            self.custom[(ns, plural, name)] = copy.deepcopy(body)
            return body
        o = self.custom[(ns, plural, name)]
        o["spec"].update(body.get("spec", {}))
        o["metadata"].setdefault("annotations", {}).update((body.get("metadata") or {}).get("annotations") or {})
        return o

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self.deleted.append(name)
        del self.custom[(ns, plural, name)]

    def list_namespace(self, label_selector=""):
        class NS:
            def __init__(self, n): self.metadata = type("M", (), {"name": n})()
        return Items([NS(n) for n in self.namespaces])


def pod(job: str, phase: str, started="2026-10-05T20:01:00Z", finished=None, exit_code=None, disrupted=False, gpus=1, ns="tenant-demo"):
    state = ({"terminated": {"startedAt": started, "finishedAt": finished, "exitCode": exit_code, "reason": "Error" if exit_code else "Completed"}} if finished
             else {"running": {"startedAt": started}} if phase == "Running" else {"waiting": {"reason": "PodInitializing"}})
    return {"metadata": {"name": f"{job}-{phase.lower()[:3]}", "namespace": ns, "labels": {"job-name": job, f"{L}/tenant": "demo", f"{L}/mode": "run"}, "creationTimestamp": started},
            "spec": {"nodeName": "node-1", "containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": str(gpus)} if gpus else {}}}]},
            "status": {"phase": phase, "startTime": started, "containerStatuses": [{"name": "main", "state": state}],
                       "conditions": [{"type": "DisruptionTarget", "status": "True", "message": "node is being drained"}] if disrupted else []}}


@pytest.fixture
def cluster(monkeypatch):
    fake = FakeCluster()
    monkeypatch.setattr(kube, "batch", lambda region="eu-north1": fake)
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": fake)
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": fake)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    return fake


@pytest.fixture
def client(monkeypatch, cluster):
    monkeypatch.setattr(appmod.artifacts, "storage", lambda ns, region="eu-north1": {"bucket": f"serverless2-demo-{region}"})
    monkeypatch.setattr(kube, "endpoint_status", lambda n, ns, region="eu-north1": {"status": "scaled-to-zero", "replicas_ready": 0})
    monkeypatch.setattr(kube, "isvc", lambda n, ns, region="eu-north1": None)

    async def fake_info(key):
        if key != "sk-good":
            from fastapi import HTTPException
            raise HTTPException(401, "invalid API key")
        return dict(KEY_INFO)
    monkeypatch.setattr(auth, "key_info", fake_info)
    return TestClient(appmod.app), cluster


H = {"Authorization": "Bearer sk-good"}


def test_catalog_one_schema():
    m = catalog.normalise({"id": "foo", "mode": "run"})
    assert m["job"] == {} and m["protocol"] == "kubernetes-job" and m["modes"] == ["run"] and m["regions"] == ["eu-north1"]
    s = catalog.normalise({"id": "bar.v2", "endpoints": {"chat": "/v1/chat", "ready": "/health"}, "price": {"unit": "call", "usd": 0.01},
                           "gpu": {"count": 1, "classes": ["h100"]}, "deployments": {"hub": {}, "eu-south1": {"paused": True}}})
    assert s["k8s_name"] == "bar-v2" and s["endpoint_host"] == "http://bar-v2-predictor.models.svc.cluster.local"
    assert s["modes"] == ["sync", "async"] and s["endpoint_path"] == "/v1/chat" and s["price_per_call"] == 0.01 and s["gpu"] == "1x h100"
    assert s["regions"] == ["eu-north1"]          # paused deployments are not regions
    # the committed catalog: endpoints and run classes in one directory
    ids = set(catalog.all_models())
    assert {"batch-example", "hello-run", "container-run", "ws-example", "llm-example", "http-example"} <= ids
    g = catalog.get("batch-example")
    assert g["mode"] == "run" and g["regions"] == ["eu-north1", "eu-south1"] and g["job"]["image"].startswith("registry.serverless2.local/nebius/") and g["job"]["gpu"] == 1


def test_catalog_entries_are_renderable_or_run_classes():
    """Every entry either has a runtime (rendered by the models ApplicationSet through charts/endpoint) or is a run class with a job block."""
    for d in os.environ["CATALOG_DIRS"].split(":"):
      for f in os.listdir(d):
        doc = yaml.safe_load(open(os.path.join(d, f)))
        assert doc.get("id"), f
        assert doc.get("runtime") or (doc.get("mode") == "run" and doc.get("job", {}).get("image") and doc["job"].get("command")), f


def test_auth(client):
    c, _ = client
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"Authorization": "Bearer sk-bad"}).status_code == 401
    r = c.get("/v1/keys/me", headers=H)
    assert r.status_code == 200 and r.json()["tenant"] == "demo" and r.json()["status"] == "active"
    assert {"llm-example", "batch-example", "hello-run"} <= {m["id"] for m in c.get("/v1/models", headers=H).json()}
    assert c.get("/v1/models/nope", headers=H).status_code == 404
    assert c.get("/v1/models/llm-example", headers=H).json()["regions"][0]["status"] == "scaled-to-zero"


def _main(job):
    return next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "main")


def test_run_builds_job_with_volume_and_is_idempotent(client, monkeypatch):
    c, fake = client
    body = {"name": "t", "input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    r1 = c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "k1"})
    r2 = c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "k1"})
    assert (r1.status_code, r2.status_code) == (202, 200) and r1.json()["id"] == r2.json()["id"]
    op = r1.json()
    assert op["status"] == "QUEUED" and op["mode"] == "run" and op["region"] == "eu-north1" and op["resumable"] is False and op["attempts"] == []
    job = fake.jobs[("tenant-demo", op["id"])]
    spec, pod_spec = job["spec"], job["spec"]["template"]["spec"]
    assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-rtx-pro-6000" and job["metadata"]["labels"][f"{L}/profile"] == "prefer-rtx-pro-6000" and job["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "customer-batch"
    assert job["metadata"]["labels"][f"{L}/tenant"] == "demo" and job["metadata"]["labels"][f"{L}/region"] == "eu-north1"
    assert spec["backoffLimit"] == 3 and spec["ttlSecondsAfterFinished"] == 90 * 86400 and spec["parallelism"] == 1
    rules = spec["podFailurePolicy"]["rules"]
    assert rules[0]["onPodConditions"][0]["type"] == "DisruptionTarget" and rules[1]["onExitCodes"]["values"] == [137, 143]
    assert pod_spec["serviceAccountName"] == "job-runner" and pod_spec["restartPolicy"] == "Never" and pod_spec["securityContext"]["fsGroup"] == 10001
    # no pool pin: the profile queue places it; the class affinity keeps it off GPUs the image does not run on (no L40S)
    assert "nodeSelector" not in pod_spec and pod_spec["priorityClassName"] == "serverless2-batch"
    assert pod_spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0] == \
        {"key": f"{L}/pool", "operator": "In", "values": ["h100-spot-1x", "rtx6000-spot-1x"]}
    main = _main(job)
    assert main["image"].startswith("registry.serverless2.local/nebius/batch-example:") and main["resources"]["limits"]["nvidia.com/gpu"] == "1" and "cpu" not in main["resources"]["limits"]
    assert "steps[[:space:]]*=).*/\\1 1000/" in main["command"][2] and "EXAMPLE_THREADS=\"16\"" in main["command"][2] and "{{" not in main["command"][2]
    names = [x["name"] for x in pod_spec["containers"]]
    assert names == ["main", "uploader"] and pod_spec["initContainers"][0]["name"] == "fetch"
    env = {e["name"]: e.get("value") for e in pod_spec["initContainers"][0]["env"]}
    assert env["INPUT_PREFIX"] == "s3://serverless2-demo-eu-north1/inputs/x"
    up = {e["name"]: e.get("value") for e in pod_spec["containers"][1]["env"]}
    assert up["OUTPUT_PREFIX"] == f"s3://serverless2-demo-eu-north1/operations/{op['id']}" and up["PVC_NAME"] == f"{op['id']}-work"
    pvc = fake.pvcs[("tenant-demo", f"{op['id']}-work")]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "50Gi" and pvc["metadata"]["ownerReferences"][0]["uid"] == f"uid-{op['id']}"
    assert pod_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{op['id']}-work"
    # the job carries the key hash, never the key
    import billing
    assert job["metadata"]["annotations"][f"{L}/key"] == billing.key_hash("sk-good") and "sk-good" not in json.dumps(job)
    # validation
    assert c.post("/v1/models/batch-example:invoke", json={"input": {}, "region": "us-central1"}, headers=H).status_code == 400
    assert c.post("/v1/models/batch-example:invoke", json={"input": {"bogus": 1}}, headers=H).status_code == 400
    assert c.post("/v1/models/batch-example:invoke", json={"input": {}}, headers=H).status_code == 400          # input_prefix is required
    assert c.post("/v1/models/batch-example:invoke", json={"mode": "sync"}, headers=H).status_code == 400
    assert c.get(f"/v1/operations/{op['id']}/result", headers=H).status_code == 409
    assert c.get("/v1/operations/op-missing", headers=H).status_code == 404
    assert c.post("/v1/operations/op-missing:resume", headers=H).status_code == 404
    assert len(c.get("/v1/operations", headers=H).json()) == 1
    # hello-run: a string command runs under /bin/sh -c, numbers render as text
    r = c.post("/v1/models/hello-run:invoke", json={"input": {"seconds": 3}}, headers=H)
    hello = _main(fake.jobs[("tenant-demo", r.json()["id"])])
    assert hello["command"][:2] == ["/bin/sh", "-c"] and "sleep 3" in hello["command"][2] and "envFrom" not in hello
    # a run in another region: the Job lands in that cluster (same fleet-wide image references)
    south = FakeCluster()
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "batch", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    rs = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://x/in"}, "region": "eu-south1"}, headers=H)
    assert rs.status_code == 202 and rs.json()["region"] == "eu-south1" and ("tenant-demo", rs.json()["id"]) in south.jobs
    sj = south.jobs[("tenant-demo", rs.json()["id"])]
    assert _main(sj)["image"] == "registry.serverless2.local/nebius/batch-example:1.0-cuda12.8-sm90-120"
    assert sj["spec"]["template"]["spec"]["containers"][1]["image"] == "registry.serverless2.local/nebius/serverless2/jobs:0.1.7"
    assert sj["spec"]["template"]["spec"]["initContainers"][0]["image"].startswith("registry.serverless2.local/nebius/serverless2/jobs:")
    g = c.get(f"/v1/operations/{rs.json()['id']}", headers=H)
    assert g.status_code == 200 and g.json()["region"] == "eu-south1"
    assert c.get(f"/v1/operations/{rs.json()['id']}/result", headers=H).json()["region"] == "eu-south1"
    assert len(c.get("/v1/operations", headers=H).json()) == 3


def test_cancel_queued_deletes_and_running_stops(client):
    c, fake = client
    r = c.post("/v1/models/hello-run:invoke", json={"input": {}}, headers=H)
    oid = r.json()["id"]
    assert c.post(f"/v1/operations/{oid}:cancel", headers=H).json()["status"] == "CANCELLED"
    assert fake.deleted == [oid] and ("tenant-demo", oid) not in fake.jobs      # never started: gone
    r = c.post("/v1/models/hello-run:invoke", json={"input": {}}, headers=H)
    oid = r.json()["id"]
    fake.jobs[("tenant-demo", oid)]["status"] = {"startTime": "2026-10-05T20:00:30Z"}
    fake.pods.append(pod(oid, "Running"))
    assert c.get(f"/v1/operations/{oid}", headers=H).json()["status"] == "RUNNING"
    r = c.post(f"/v1/operations/{oid}:cancel", headers=H)
    job = fake.jobs[("tenant-demo", oid)]
    assert r.json()["status"] == "CANCELLED" and job["spec"]["activeDeadlineSeconds"] == 1 and job["metadata"]["annotations"][f"{L}/cancelled"] == "true"
    # once the controller has failed it (DeadlineExceeded) it stays CANCELLED, not FAILED, and is resumable
    job["status"]["conditions"] = [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]
    fake.pods[-1] = pod(oid, "Failed", finished="2026-10-05T20:05:00Z", exit_code=143)
    op = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert op["status"] == "CANCELLED" and op["resumable"] is True and op["timeout_s"] is None


def test_async_builds_endpoint_call_job(client, monkeypatch):
    c, fake = client
    monkeypatch.setitem(catalog.get("llm-example"), "litellm_route", "/models/llm/chat")
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202 and r.json()["mode"] == "async"
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    main = _main(job)
    env = {e["name"]: e for e in main["env"]}
    assert main["command"] == ["/usr/local/bin/call.sh"] and env["URL"]["value"].endswith("/models/llm/chat") and json.loads(env["REQUEST_BODY"]["value"]) == {"messages": [], "model": "llm"}   # served model injected (catalog --model_name)
    assert env["AUTH_HEADER"]["valueFrom"]["secretKeyRef"]["name"] == f"{r.json()['id']}-auth" and "sk-good" not in json.dumps(job)
    secret = fake.secrets[("tenant-demo", f"{r.json()['id']}-auth")]
    assert secret["stringData"]["authorization"] == "Bearer sk-good" and secret["metadata"]["ownerReferences"][0]["kind"] == "Job"
    assert "activeDeadlineSeconds" not in job["spec"] and "persistentVolumeClaim" not in json.dumps(job["spec"]["template"]["spec"]["volumes"])
    assert job["spec"]["podFailurePolicy"]["rules"][0] == {"action": "FailJob", "onExitCodes": {"containerName": "main", "operator": "In", "values": [2]}}
    assert r.json()["resumable"] is False and not fake.pvcs
    # the result is the response the uploader put in the bucket
    job["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
    monkeypatch.setattr(appmod.artifacts, "read_json", lambda ns, key, region, prefix=None: {"choices": [], "_key": key})
    res = c.get(f"/v1/operations/{r.json()['id']}/result", headers=H).json()
    assert res["status"] == "SUCCEEDED" and res["result"]["_key"] == f"operations/{r.json()['id']}/out/response.json" and res["artifacts"] == []


def test_async_direct_model_goes_through_the_gateway_with_the_key(client):
    """F2 (docs/SECURITY-PREREVIEW.md): a model without a LiteLLM route is called at its gateway hostname with
    the caller's key, never at the predictor Service; the connection is routed to the gateway's in-cluster
    Service (pods cannot hairpin to the public IP) while the TLS name stays the public hostname."""
    c, fake = client
    assert "litellm_route" not in catalog.get("llm-example")
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {e["name"]: e for e in _main(job)["env"]}
    assert env["URL"]["value"] == "https://llm-example-predictor.models.203.0.113.10.sslip.io/openai/v1/chat/completions"
    assert env["CONNECT_TO"]["value"] == "llm-example-predictor.models.203.0.113.10.sslip.io:443:knative-external.envoy-gateway-system.svc:443"
    assert "svc.cluster.local" not in json.dumps(job)
    assert fake.secrets[("tenant-demo", f"{r.json()['id']}-auth")]["stringData"]["authorization"] == "Bearer sk-good"


def test_rendered_pods_are_hardened(client):
    """F1 hardening: every container drops all capabilities, forbids privilege escalation and uses the runtime
    seccomp profile; runner containers are non-root uid 10001; the model container keeps the image's user."""
    c, fake = client
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in"}}, headers=H)
    assert r.status_code == 202
    spec = fake.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["spec"]
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"} and spec["securityContext"]["fsGroup"] == 10001
    for ctn in spec["initContainers"] + spec["containers"]:
        sc = ctn["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"] == {"drop": ["ALL"]} and sc["seccompProfile"] == {"type": "RuntimeDefault"}
    by_name = {ctn["name"]: ctn["securityContext"] for ctn in spec["initContainers"] + spec["containers"]}
    assert by_name["fetch"]["runAsUser"] == 10001 and by_name["uploader"]["runAsNonRoot"] is True
    assert "runAsNonRoot" not in by_name["main"]          # the model image keeps its own user
    ra = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    aspec = fake.jobs[("tenant-demo", ra.json()["id"])]["spec"]["template"]["spec"]
    assert all(ctn["securityContext"]["capabilities"] == {"drop": ["ALL"]} for ctn in aspec["containers"])


def test_render_and_build_run(cluster):
    m = catalog.get("batch-example")
    assert m["regions"] == ["eu-north1", "eu-south1"] and catalog.region_params(m, "eu-south1") == {"threads": "16"} == catalog.fleet_params(m)
    job = jobs.build_run("op-x", m, {"nsteps": "50000", "input_prefix": "s3://b/in"}, "demo", None, None, 3600, "low", "eu-south1", catalog.region_params(m, "eu-south1") | {"output_prefix": "s3://b/operations/op-x"})
    assert job["metadata"]["labels"][f"{L}/region"] == "eu-south1" and job["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "bulk-backfill"
    assert job["spec"]["activeDeadlineSeconds"] == 3600 and "--batch-size 200" in _main(job)["command"][2]
    op = jobs.normalise(job)
    assert op["region"] == "eu-south1" and op["logs_url"] is None and op["priority"] == "low" and op["timeout_s"] == 3600
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:          # a {{token}} without a value is the caller's error
        jobs.build_run("op-y", catalog.get("container-run"), {"command": "x"}, "demo", None, None, None, None, "eu-south1", {"output_prefix": "x"})
    assert e.value.status_code == 400 and "image" in e.value.detail
    with pytest.raises(HTTPException) as e:
        jobs.build_run("op-y", catalog.normalise({"id": "nojob", "mode": "run"}), {}, "demo", None, None, None, None)
    assert e.value.status_code == 500
    cr = jobs.build_run("op-c", catalog.get("container-run"), {"image": "img:1", "command": "python train.py", "gpus": 2}, "demo", None, None, None, "high", "eu-north1", {"output_prefix": "x"})
    main = _main(cr)
    assert main["image"] == "img:1" and main["command"] == ["/bin/sh", "-c", "python train.py"] and main["resources"]["requests"]["nvidia.com/gpu"] == "2"
    assert {e["name"]: e["value"] for e in main["env"]}["CHECKPOINT_DIR"] == "/work/checkpoint" and cr["spec"]["template"]["spec"]["priorityClassName"] == "serverless2-batch-priority"
    assert cr["metadata"]["annotations"][f"{L}/pvc-size-gi"] == "100" and cr["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ngc"}]
    monkeypatch_urls = {"eu-north1": "https://grafana.example"}
    jobs.GRAFANA_URLS.update(monkeypatch_urls)
    try:
        assert jobs.logs_url("tenant-demo", "op-c", "eu-north1").startswith("https://grafana.example/explore?orgId=1&left=")
    finally:
        jobs.GRAFANA_URLS.clear()
    assert jobs.op_name("demo", "batch-example", "k") == jobs.op_name("demo", "batch-example", "k") != jobs.op_name("demo", "batch-example", "k2")


def test_normalise_status_and_attempts():
    job = {"metadata": {"name": "op-1", "namespace": "tenant-demo", "labels": {f"{L}/model": "batch-example", f"{L}/mode": "run", f"{L}/tenant": "demo", f"{L}/region": "eu-north1"},
                        "annotations": {f"{L}/input": '{"nsteps": "5"}', f"{L}/pvc": "op-1-work"}, "creationTimestamp": "2026-10-05T18:49:23Z"},
           "spec": {}, "status": {"startTime": "2026-10-05T18:49:23Z", "completionTime": "2026-10-05T19:10:47Z", "conditions": [{"type": "Complete", "status": "True"}]},
           "_pods": [pod("op-1", "Failed", "2026-10-05T18:49:23Z", "2026-10-05T18:57:29Z", 137, disrupted=True),
                     pod("op-1", "Succeeded", "2026-10-05T18:57:29Z", "2026-10-05T19:10:37Z", 0)]}
    op = jobs.normalise(job, 2.0)
    assert op["status"] == "SUCCEEDED" and op["duration_s"] == 1284.0 and op["cost"] == 2.0 and op["input"] == {"nsteps": "5"} and op["resumable"] is False
    assert [a["status"] for a in op["attempts"]] == ["PREEMPTED", "SUCCEEDED"] and op["attempts"][0]["reason"] == "node is being drained" and op["attempts"][0]["exit_code"] == 137
    assert jobs.gpu_seconds(job) == 486.0 + 788.0
    job["status"] = {"startTime": "2026-10-05T18:49:23Z", "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "Job has reached the specified backoff limit"}]}
    job["_pods"] = [pod("op-1", "Failed", "2026-10-05T18:49:23Z", "2026-10-05T18:57:29Z", 1)]
    op = jobs.normalise(job)
    assert op["status"] == "FAILED" and op["error"] == "Error" and op["resumable"] is True and op["ended_at"] == "2026-10-05T18:57:29Z"
    job["metadata"]["annotations"][f"{L}/cancelled"] = "true"
    assert jobs.normalise(job)["status"] == "CANCELLED"
    del job["metadata"]["annotations"][f"{L}/cancelled"]
    job["status"] = {}; job["_pods"] = [pod("op-1", "Pending")]
    assert jobs.normalise(job)["status"] == "QUEUED"
    job["_pods"] = [pod("op-1", "Running")]
    assert jobs.normalise(job)["status"] == "RUNNING" and jobs.normalise(job)["attempts"][0]["node"] == "node-1"
    job["status"] = {"conditions": [{"type": "FailureTarget", "status": "True"}]}
    assert jobs.normalise(job)["status"] == "FAILED"
    # attempt records from the bucket stand in for pods that preemption deleted (same pod name: the live pod wins)
    records = [{"operation": "op-1", "pod": "op-1-gone", "node": "node-0", "status": "interrupted", "exit_code": None, "gpus": 1,
                "started_at": "2026-10-05T18:00:00Z", "ended_at": "2026-10-05T18:10:00Z"},
               {"operation": "op-1", "pod": "op-1-run", "status": "succeeded", "gpus": 1, "started_at": "2026-10-05T20:01:00Z", "ended_at": "2026-10-05T20:02:00Z"},
               {"operation": "op-1-r1", "pod": "op-1-r1-x", "status": "succeeded", "gpus": 1, "started_at": "2026-10-05T21:00:00Z", "ended_at": "2026-10-05T22:00:00Z"}]
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
    op = jobs.normalise(job, None, records)
    assert [(a["index"], a["status"], a["node"]) for a in op["attempts"]] == [(1, "PREEMPTED", "node-0"), (2, "RUNNING", "node-1")]
    assert op["attempts"][0]["reason"] == "pod deleted (preempted)" and "_gpus" not in op["attempts"][0]
    assert jobs.gpu_seconds(job, records) == 600.0                      # the other operation's record is not ours


def test_resume_reuses_the_volume_in_the_same_region(client):
    c, fake = client
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in", "nsteps": 7}}, headers=H)
    oid = r.json()["id"]
    assert c.post(f"/v1/operations/{oid}:resume", headers=H).status_code == 409        # still queued
    job = fake.jobs[("tenant-demo", oid)]
    job["status"] = {"startTime": "2026-10-05T20:00:30Z", "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
    job["metadata"]["labels"]["batch.kubernetes.io/controller-uid"] = "abc"
    job["spec"]["selector"] = {"matchLabels": {"batch.kubernetes.io/controller-uid": "abc"}}
    job["spec"]["template"]["metadata"]["labels"]["batch.kubernetes.io/job-name"] = oid
    job["spec"]["suspend"] = False
    fake.pods.append(pod(oid, "Failed", finished="2026-10-05T20:05:00Z", exit_code=1))
    r = c.post(f"/v1/operations/{oid}:resume", headers=H)
    assert r.status_code == 202 and r.json()["id"] == f"{oid}-r1" and r.json()["resumed_from"] == oid and r.json()["status"] == "QUEUED"
    new = fake.jobs[("tenant-demo", f"{oid}-r1")]
    assert new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{oid}-work"
    assert "selector" not in new["spec"] and "suspend" not in new["spec"] and "batch.kubernetes.io/controller-uid" not in new["metadata"]["labels"]
    assert "batch.kubernetes.io/job-name" not in new["spec"]["template"]["metadata"]["labels"] and f"{L}/key" in new["metadata"]["annotations"]
    assert _main(new)["command"] == _main(job)["command"] and "resourceVersion" not in new["metadata"]
    up = {e["name"]: e.get("value") for e in new["spec"]["template"]["spec"]["containers"][1]["env"]}
    assert up["OPERATION"] == f"{oid}-r1" and up["OUTPUT_PREFIX"] == f"s3://serverless2-demo-eu-north1/operations/{oid}-r1"
    owners = [o["uid"] for o in fake.pvcs[("tenant-demo", f"{oid}-work")]["metadata"]["ownerReferences"]]
    assert owners == [f"uid-{oid}", f"uid-{oid}-r1"]                                # the volume now outlives either Job
    assert c.get(f"/v1/operations/{oid}", headers=H).json()["resumable"] is True
    # the resumed run fails again: the next resume chains from the root name
    new["status"] = {"startTime": "x", "conditions": [{"type": "Failed", "status": "True"}]}
    r = c.post(f"/v1/operations/{oid}-r1:resume", headers=H)
    assert r.status_code == 202 and r.json()["id"] == f"{oid}-r2" and r.json()["resumed_from"] == oid
    # a successful run released its volume: nothing to resume from
    del fake.pvcs[("tenant-demo", f"{oid}-work")]
    r = c.post(f"/v1/operations/{oid}:resume", headers=H)
    assert r.status_code == 409 and "volume" in r.json()["detail"]
    # only run operations resume
    r = c.post("/v1/models/llm-example:invoke", json={"mode": "async", "input": {}}, headers=H)
    fake.jobs[("tenant-demo", r.json()["id"])]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    assert c.post(f"/v1/operations/{r.json()['id']}:resume", headers=H).status_code == 400


def test_sync_proxies_to_the_predictor(client, monkeypatch):
    c, _ = client
    calls = []

    class R:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "hi"}}]}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None):
            calls.append((url, headers)); return R()
    monkeypatch.setattr(appmod.httpx, "AsyncClient", FakeClient)
    r = c.post("/v1/models/llm-example:invoke", json={"input": {"messages": []}}, headers=H)
    assert r.status_code == 200 and r.json()["result"]["choices"]
    assert calls[0][0] == "http://llm-example-predictor.models.svc.cluster.local/openai/v1/chat/completions" and calls[0][1] == {}
    # with a LiteLLM pass-through route the caller's key goes along and the per-call price is reported
    monkeypatch.setitem(catalog.get("llm-example"), "litellm_route", "/models/llm/chat")
    monkeypatch.setitem(catalog.get("llm-example"), "price_per_call", 0.05)
    r = c.post("/v1/models/llm-example:invoke", json={"input": {"messages": []}}, headers=H)
    assert r.json()["operation"]["cost"] == 0.05 and calls[-1][0].endswith("/models/llm/chat") and calls[-1][1]["Authorization"] == "Bearer sk-good"


def test_sync_for_another_region_is_forwarded_to_that_regions_api(client, monkeypatch):
    """The control cluster's API hosts no endpoint: with REGION_API_URLS a sync/async invoke for another region
    is forwarded as is (key, idempotency key, body) and the regional answer is returned; without it: 400."""
    c, _ = client
    calls = []

    class R:
        status_code = 402
        def json(self): return {"detail": "budget exceeded"}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None):
            calls.append((url, json, headers)); return R()
    monkeypatch.setattr(appmod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(appmod, "REGION", "control")   # as on the control cluster (HUB_REGION=eu-north1 maps deployments.hub there)
    monkeypatch.setitem(catalog.get("llm-example"), "regions", ["eu-north1"])
    body = {"input": {"messages": []}, "region": "eu-north1"}
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 400 and "eu-north1" in r.json()["detail"] and not calls
    monkeypatch.setattr(appmod, "REGION_API_URLS", {"eu-north1": "https://api.hub.example"})
    r = c.post("/v1/models/llm-example:invoke", json=body, headers={**H, "Idempotency-Key": "k1"})
    assert r.status_code == 402 and r.json() == {"detail": "budget exceeded"}
    url, sent, headers = calls[0]
    assert url == "https://api.hub.example/v1/models/llm-example:invoke"
    assert sent["input"] == {"messages": []} and sent["region"] == "eu-north1"
    assert headers == {"Authorization": "Bearer sk-good", "Idempotency-Key": "k1"}


ADMIN_INFO = {**KEY_INFO, "metadata": {**KEY_INFO["metadata"], "role": "admin"}}


def test_endpoints_read_and_scale_only(client, monkeypatch):
    import endpoints as ep
    c, fake = client
    isvc = {"metadata": {"name": "llm-example", "namespace": "models", "creationTimestamp": "2026-10-05T20:00:00Z",
                         "annotations": {"autoscaling.knative.dev/scale-to-zero-pod-retention-period": "2m"}},
            "spec": {"predictor": {"minReplicas": 0, "maxReplicas": 1, "scaleTarget": 4}}}
    pub = ep.to_public(catalog.get("llm-example"), isvc)
    assert pub["id"] == "llm-example" and pub["scale_to_zero_after_s"] == 120 and pub["managed_by"] == "git"
    assert pub["url"] == "https://api.example/v1/models/llm-example:invoke"
    assert ep.scaling_patch({"min_replicas": 1, "scale_to_zero_after_s": 300}) == {
        "spec": {"predictor": {"minReplicas": 1}}, "metadata": {"annotations": {"autoscaling.knative.dev/scale-to-zero-pod-retention-period": "300s"}}}
    # no create/delete routes any more: endpoints are git-only
    assert c.post("/v1/endpoints", json={"model": "llm-example"}, headers=H).status_code == 405
    assert c.delete("/v1/endpoints/llm-example", headers=H).status_code == 405
    assert c.patch("/v1/endpoints/llm-example", json={"min_replicas": 1}, headers=H).status_code == 403   # admin only
    assert c.get("/v1/endpoints", headers=H).status_code == 200


def test_admin_routes_need_admin_role(client, monkeypatch):
    c, _ = client
    assert c.post("/v1/keys", json={"alias": "x"}, headers=H).status_code == 403

    async def fake_info(key):
        return dict(ADMIN_INFO)
    monkeypatch.setattr(auth, "key_info", fake_info)
    calls = []

    async def fake_litellm(method, path, **kw):
        calls.append((method, path, kw))
        if path == "/key/generate":
            return {"key": "sk-newkey1234", "created_at": "now"}
        return {"keys": [{"key_alias": "demo-ci", "key_name": "sk-...1234", "spend": 0, "max_budget": 5, "metadata": {"tenant": "demo"}},
                         {"key_alias": "other", "metadata": {"tenant": "other"}}]}
    monkeypatch.setattr(appmod, "_litellm", fake_litellm)
    r = c.post("/v1/keys", json={"alias": "ci", "budget": 5, "expires_days": 7}, headers=H)
    assert r.status_code == 201 and r.json()["key"] == "sk-newkey1234" and r.json()["alias"] == "demo-ci"
    assert calls[-1][2]["json"]["metadata"] == {"tenant": "demo", "allowed_passthrough_routes": ["/models/llm/chat"]} and calls[-1][2]["json"]["duration"] == "7d"
    assert [k["alias"] for k in c.get("/v1/keys", headers=H).json()] == ["demo-ci"]
    assert c.delete("/v1/keys/demo-ci", headers=H).status_code == 204 and calls[-1][1] == "/key/delete"
    assert c.delete("/v1/keys/other", headers=H).status_code == 404


def test_billing_once(cluster, monkeypatch):
    """The CronJob entry point bills finished, unbilled runs exactly once (annotation guard), no claim protocol."""
    import asyncio, billing
    done = {"metadata": {"name": "op-b", "namespace": "tenant-demo", "labels": {f"{L}/region": "eu-south1", f"{L}/model": "batch-example", f"{L}/mode": "run", f"{L}/tenant": "demo"},
                         "annotations": {f"{L}/key": "deadbeef", f"{L}/pvc": "op-b-work"}},
            "spec": {}, "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    cluster.jobs[("tenant-demo", "op-b")] = done
    cluster.pvcs[("tenant-demo", "op-b-work")] = {"metadata": {"name": "op-b-work"}}      # the uploader was preempted before releasing it
    cluster.pods += [pod("op-b", "Failed", "2026-10-05T18:00:00Z", "2026-10-05T18:30:00Z", 137),        # preempted attempt: billed too
                     pod("op-b", "Succeeded", "2026-10-05T18:30:00Z", "2026-10-05T19:00:00Z", 0)]
    monkeypatch.setattr(billing.artifacts, "attempt_records", lambda ns, op, region, prefix=None: [
        {"operation": "op-b", "pod": "op-b-suc", "status": "succeeded", "gpus": 1, "started_at": "2026-10-05T18:30:00Z", "ended_at": "2026-10-05T19:00:00Z"}])   # duplicate of a live pod: not double counted
    running = {"metadata": {"name": "op-r", "namespace": "tenant-demo", "labels": {f"{L}/region": "eu-south1", f"{L}/model": "batch-example", f"{L}/mode": "run"}, "annotations": {f"{L}/key": "deadbeef"}},
               "spec": {}, "status": {"startTime": "x"}}
    cluster.jobs[("tenant-demo", "op-r")] = running
    done["_pods"] = cluster.pods
    assert billing.cost_of(done, catalog.get("batch-example")) == (3600.0, 1.6)
    spent = []

    async def fake_add(token, usd):
        spent.append((token, usd)); return 2.1
    monkeypatch.setattr(billing, "add_spend", fake_add)
    assert asyncio.run(billing.bill_once()) == 1 and spent == [("deadbeef", 1.6)]
    assert done["metadata"]["annotations"][f"{L}/billed"] == "1.6" and done["metadata"]["annotations"][f"{L}/gpu-seconds"] == "3600.0"
    assert ("tenant-demo", "op-b-work") not in cluster.pvcs                              # released by the pass
    assert asyncio.run(billing.bill_once()) == 0            # idempotent; the running one waits
    op = jobs.normalise(done)
    assert op["cost"] == 1.6 and op["gpu_seconds"] == 3600.0


def test_openapi_paths_match_served_routes():
    spec = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.yaml")))
    documented = {(p, m) for p, ops in spec["paths"].items() for m in ops if m in ("get", "post", "patch", "delete")}
    served = {(p, m) for p, ops in appmod.app.openapi()["paths"].items() for m in ops if m in ("get", "post", "patch", "delete")}
    assert documented == served, documented ^ served


def test_retry_helper(monkeypatch):
    import resilience
    monkeypatch.setattr(resilience.time, "sleep", lambda s: None)
    calls = []

    def flaky(**kw):
        calls.append(kw["_request_timeout"])
        if len(calls) < 3:
            raise ApiException(status=503, reason="busy")
        return "ok"
    assert resilience.retry(flaky) == "ok" and calls == [15, 15, 15]

    def forbidden(**kw):
        raise ApiException(status=403, reason="no")
    with pytest.raises(ApiException):
        resilience.retry(forbidden)


def test_region_client_reloads_when_kubeconfig_changes(monkeypatch, tmp_path):
    """The api-agent kubeconfigs are rotated daily (ops/rotate-api-agent-token): a changed file must yield a new client."""
    kc = tmp_path / "eu-south1"
    kc.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(kube, "REGION_KUBECONFIGS", {"eu-south1": str(kc)})
    monkeypatch.setattr(kube, "_clients", {})
    monkeypatch.setattr(kube, "_kubeconfig_mtime", {})
    built = []
    monkeypatch.setattr(kube.kconfig, "new_client_from_config", lambda path: built.append(path) or object())
    first = kube._client("eu-south1")
    assert kube._client("eu-south1") is first and len(built) == 1          # unchanged file: cached
    os.utime(kc, (1, 1))                                                  # the Secret projection replaced the file
    second = kube._client("eu-south1")
    assert second is not first and len(built) == 2
    assert kube._client("eu-south1") is second and len(built) == 2
    with pytest.raises(Exception):
        kube._client("us-central1")                                       # not mounted: 400


def test_fleet_manager_path(client, monkeypatch):
    """Control-cluster API (FLEET_MANAGER): a run is a managedBy multikueue Job on the manager with the profile
    queue, no region pin, no PVC (the dispatcher creates it on the worker); status, pods and resume follow the
    Workload's cluster; the worker's mirror is never an operation of its own; billing bills the mirror."""
    import billing
    c, control = client
    hub = FakeCluster()
    both = {"control": control, "eu-north1": hub}
    monkeypatch.setattr(kube, "regions", lambda: ["control", "eu-north1"])
    monkeypatch.setattr(kube, "batch", lambda region="control": both[region])
    monkeypatch.setattr(kube, "core", lambda region="control": both[region])
    monkeypatch.setattr(kube, "HUB_REGION", "eu-north1")
    placed = {}
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: dict(placed))
    for mod in (appmod, jobs, appmod.artifacts):
        monkeypatch.setattr(mod, "REGION", "control")
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(appmod.artifacts, "storage", lambda ns, region="control": {"bucket": f"serverless2-demo-{'eu-north1' if region == 'control' else region}"})
    # inputs must sit in the fleet (hub-region) bucket
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://serverless2-demo-eu-south1/in"}}, headers=H)
    assert r.status_code == 400 and "fleet bucket" in r.json()["detail"]
    # a required parameter the fleet defaults cannot supply is the caller's error
    r = c.post("/v1/models/container-run:invoke", json={"input": {"command": "x"}}, headers=H)
    assert r.status_code == 400 and "image" in r.json()["detail"]
    # a GPU run class through the fleet: one fleet-wide image reference (the logical registry host, docs/IMAGES.md), so the
    # run stays unpinned and MultiKueue + the dispatcher place it at queue time (re-nominating while it waits)
    rg = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}}, headers=H)
    assert rg.status_code == 202 and rg.json()["profile"] == "prefer-rtx-pro-6000" and rg.json()["region"] is None
    gj = control.jobs[("tenant-demo", rg.json()["id"])]
    assert gj["spec"]["managedBy"] == "kueue.x-k8s.io/multikueue" and f"{L}/region" not in gj["metadata"]["labels"]
    assert _main(gj)["image"] == "registry.serverless2.local/nebius/batch-example:1.0-cuda12.8-sm90-120" and 'EXAMPLE_THREADS="16"' in _main(gj)["command"][2]
    assert gj["spec"]["template"]["spec"]["initContainers"][0]["image"].startswith("registry.serverless2.local/nebius/serverless2/jobs:")
    r = c.post("/v1/models/container-run:invoke", json={"name": "t", "input": {"image": "nvcr.io/nvidia/cuda:12.8.0-base-ubuntu22.04", "command": "nvidia-smi"}}, headers=H)
    assert r.status_code == 202
    op = r.json()
    assert op["status"] == "QUEUED" and op["region"] is None and op["profile"] == "prefer-h100" and op["logs_url"] is None
    job = control.jobs[("tenant-demo", op["id"])]
    assert job["spec"]["managedBy"] == "kueue.x-k8s.io/multikueue" and f"{L}/region" not in job["metadata"]["labels"]
    assert job["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-h100"
    assert job["spec"]["template"]["spec"]["initContainers"][0]["image"] == "registry.serverless2.local/nebius/serverless2/jobs:0.1.7"
    tmpl = job["spec"]["template"]["metadata"]["annotations"]
    assert tmpl[f"{L}/gpu-classes"] == "h100,rtx-pro-6000,l40s" and tmpl[f"{L}/regions"] == "eu-north1,eu-south1" and tmpl[f"{L}/pvc-size-gi"] == "100"
    assert not control.pvcs and ("tenant-demo", op["id"]) not in hub.jobs                  # no volume on the manager
    up = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][1]["env"]}
    assert up["OUTPUT_PREFIX"] == f"s3://serverless2-demo-eu-north1/operations/{op['id']}"
    # an explicit region pins (label) and applies that region's defaults, still through the manager
    rp = c.post("/v1/models/batch-example:invoke", json={"region": "eu-south1", "input": {"input_prefix": "s3://serverless2-demo-eu-north1/in"}}, headers=H)
    assert rp.status_code == 202 and rp.json()["region"] == "eu-south1"
    pj = control.jobs[("tenant-demo", rp.json()["id"])]
    assert pj["metadata"]["labels"][f"{L}/region"] == "eu-south1" and pj["spec"]["managedBy"] and _main(pj)["image"].startswith("registry.serverless2.local/nebius/")
    # MultiKueue mirrors the Job to the hub (origin label) and syncs its status back; the API joins the worker's pods
    mirror = copy.deepcopy(job)
    mirror["metadata"]["labels"]["kueue.x-k8s.io/multikueue-origin"] = "control"
    del mirror["spec"]["managedBy"]
    hub.jobs[("tenant-demo", op["id"])] = mirror
    placed[job["metadata"]["uid"]] = "hub"
    mirror["status"] = {"startTime": "2026-10-07T09:00:00Z"}          # the manager's mirror of it lags behind
    hub.pods.append(pod(op["id"], "Running", started="2026-10-07T09:00:10Z"))
    g = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert g["status"] == "RUNNING" and g["region"] == "eu-north1" and g["attempts"][0]["node"] == "node-1"
    ops = c.get("/v1/operations", headers=H).json()
    assert [o["id"] for o in ops].count(op["id"]) == 1 and len(ops) == 3                   # the mirror is not listed twice
    # cancel while running (the manager's status not yet synced): deadline on the worker's copy, annotation on the
    # manager's, the manager Job stays (it is the operation)
    assert c.post(f"/v1/operations/{op['id']}:cancel", headers=H).json()["status"] == "CANCELLED"
    assert mirror["spec"]["activeDeadlineSeconds"] == 1 and "activeDeadlineSeconds" not in job["spec"]
    assert job["metadata"]["annotations"][f"{L}/cancelled"] == "true" and ("tenant-demo", op["id"]) in control.jobs and not control.deleted
    # cancel of a run the dispatcher has not placed yet: deleted on the manager
    rq = c.post("/v1/models/hello-run:invoke", json={"input": {}}, headers=H)
    assert c.post(f"/v1/operations/{rq.json()['id']}:cancel", headers=H).json()["status"] == "CANCELLED" and control.deleted == [rq.json()["id"]]
    job["status"] = mirror["status"] = {"startTime": "2026-10-07T09:00:00Z", "conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
    hub.pods[-1] = pod(op["id"], "Failed", started="2026-10-07T09:00:10Z", finished="2026-10-07T09:05:00Z", exit_code=143)
    hub.pvcs[("tenant-demo", f"{op['id']}-work")] = {"metadata": {"name": f"{op['id']}-work", "ownerReferences": [{"uid": "uid-worker"}]}}
    g = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert g["status"] == "CANCELLED" and g["resumable"] is True
    # resume: a new manager Job pinned to the worker's region, on the same volume (the dispatcher adopts it there)
    rr = c.post(f"/v1/operations/{op['id']}:resume", headers=H)
    assert rr.status_code == 202 and rr.json()["id"] == f"{op['id']}-r1" and rr.json()["region"] == "eu-north1"
    new = control.jobs[("tenant-demo", f"{op['id']}-r1")]
    assert new["spec"]["managedBy"] == "kueue.x-k8s.io/multikueue" and new["metadata"]["labels"][f"{L}/region"] == "eu-north1"
    assert new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{op['id']}-work" and not control.pvcs
    assert hub.pvcs[("tenant-demo", f"{op['id']}-work")]["metadata"]["ownerReferences"] == [{"uid": "uid-worker"}]   # untouched from here
    # billing: MultiKueue removes the worker's copy (and its pods) when the manager's Job finishes, so the manager's
    # Job is billed from the uploader's attempt records with the price of the region its Workload was admitted in;
    # a mirror that still exists is skipped (never billed twice)
    import asyncio
    hub.namespaces, control.namespaces = ["tenant-demo"], ["tenant-demo"]
    mirror["status"] = job["status"] = {"startTime": "2026-10-07T09:00:00Z", "conditions": [{"type": "Complete", "status": "True"}]}
    hub.pods.clear()
    monkeypatch.setattr(billing.artifacts, "attempt_records", lambda ns, op, region, prefix=None: [
        {"operation": op, "pod": f"{op}-x", "status": "succeeded", "gpus": 1, "started_at": "2026-10-07T09:00:00Z", "ended_at": "2026-10-07T10:00:00Z"}] if op == job["metadata"]["name"] else [])
    spent = []

    async def fake_add(token, usd):
        spent.append(usd); return 1.0
    monkeypatch.setattr(billing, "add_spend", fake_add)
    assert asyncio.run(billing.bill_once()) == 1 and spent == [2.15]                        # hub price (H100 spot cap), 1 GPU-hour from the record
    assert job["metadata"]["annotations"][f"{L}/billed"] == "2.15" and f"{L}/billed" not in mirror["metadata"].get("annotations", {})
    assert c.get(f"/v1/operations/{op['id']}", headers=H).json()["cost"] == 2.15
    assert asyncio.run(billing.bill_once()) == 0


def test_workload_clusters_reports_the_admitted_cluster_only(monkeypatch):
    """The region of a fleet-placed run is where it was ADMITTED; a nomination is not a placement (the
    dispatcher re-nominates while a run waits)."""
    items = [{"metadata": {"labels": {kube.KUEUE_JOB_UID_LABEL: "u1"}}, "status": {"nominatedClusterNames": ["eu-south1"]}},
             {"metadata": {"labels": {kube.KUEUE_JOB_UID_LABEL: "u2"}}, "status": {"nominatedClusterNames": ["eu-south1", "hub"], "clusterName": "hub"}},
             # finished: Kueue cleared clusterName, the dispatcher's record on the Workload remains
             {"metadata": {"labels": {kube.KUEUE_JOB_UID_LABEL: "u3"}, "annotations": {"serverless2.nebius/admitted-cluster": "eu-south1"}},
              "status": {"nominatedClusterNames": ["eu-south1"], "conditions": [{"type": "Finished", "status": "True"}]}}]

    class Api:
        def list_namespaced_custom_object(self, *a, **k):
            return {"items": items}
    monkeypatch.setattr(kube, "api", lambda *a, **k: Api())
    assert kube.workload_clusters("tenant-demo") == {"u2": "hub", "u3": "eu-south1"}


def test_run_pods_are_hardened_and_scratch_local_nvme_uses_host_nvme(client, monkeypatch):
    c, fake = client
    r = c.post("/v1/models/container-run:invoke", json={"input": {"image": "registry.serverless2.local/docker/library/busybox:1.36", "command": "echo hi", "gpus": 1}}, headers=H)
    assert r.status_code == 202, r.text
    pod = fake.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["spec"]
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"} and "runAsNonRoot" not in pod["securityContext"]
    for ctr in pod["initContainers"] + pod["containers"]:
        assert ctr["securityContext"]["allowPrivilegeEscalation"] is False and ctr["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{r.json()['id']}-work"
    # local-nvme: an emptyDir on the node, pinned to NVMe nodes, no PVC, no PVC release by the uploader
    r2 = c.post("/v1/models/container-run:invoke", json={"input": {"image": "registry.serverless2.local/docker/library/busybox:1.36", "command": "echo hi", "gpus": 1, "scratch": "local-nvme"}}, headers=H)
    assert r2.status_code == 202, r2.text
    job2 = fake.jobs[("tenant-demo", r2.json()["id"])]
    pod2 = job2["spec"]["template"]["spec"]
    assert pod2["volumes"][0]["emptyDir"]["sizeLimit"].endswith("Gi") and "persistentVolumeClaim" not in pod2["volumes"][0]
    terms = pod2["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
    assert {"key": f"{L}/local-nvme", "operator": "In", "values": ["true"]} in terms
    assert job2["metadata"]["annotations"][f"{L}/scratch"] == "local-nvme" and f"{L}/pvc" not in job2["metadata"]["annotations"]
    assert ("tenant-demo", f"{r2.json()['id']}-work") not in fake.pvcs
    up = {e["name"]: e.get("value") for e in pod2["containers"][1]["env"]}
    assert "PVC_NAME" not in up
    assert c.post("/v1/models/container-run:invoke", json={"input": {"image": "x", "command": "true", "scratch": "ram"}}, headers=H).status_code == 400


def test_tenant_image_allow_list_and_output_prefix_guard(client, monkeypatch):
    c, fake = client
    from fastapi import HTTPException
    import jobs as jobs_mod

    def check(ns, image, region="eu-north1"):
        if not image.startswith("registry.serverless2.local/nebius/"):
            raise HTTPException(400, "image not allowed")
    monkeypatch.setattr(jobs_mod, "check_image_allowed", check)
    bad = c.post("/v1/models/container-run:invoke", json={"input": {"image": "docker.io/evil/x:1", "command": "true"}}, headers=H)
    assert bad.status_code == 400 and "not allowed" in bad.text
    ok = c.post("/v1/models/container-run:invoke", json={"input": {"image": "registry.serverless2.local/nebius/serverless2/jobs:0.1.3", "command": "true"}}, headers=H)
    assert ok.status_code == 202
    outside = c.post("/v1/models/hello-run:invoke", json={"input": {"output_prefix": "s3://someone-elses-bucket/x"}}, headers=H)
    assert outside.status_code == 400 and "output_prefix" in outside.text
    inside = c.post("/v1/models/hello-run:invoke", json={"input": {"output_prefix": "s3://serverless2-demo-eu-north1/custom/x"}}, headers=H)
    assert inside.status_code == 202


MULTINODE_FLEET = {"pools": {
    "hub-h100-spot-1x": {"region": "hub", "pool": "h100-spot-1x", "gpu_class": "h100", "gpus_per_node": 1, "interconnect": "none"},
    "hub-h100-spot-8x": {"region": "hub", "pool": "h100-spot-8x", "gpu_class": "h100", "gpus_per_node": 8, "interconnect": "none"},
    "hub-h200-ib-8x": {"region": "hub", "pool": "h200-ib-8x", "gpu_class": "h200", "gpus_per_node": 8, "interconnect": "infiniband"}}}


def _jobset_pod(js):
    return js["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]


def test_multinode_run_builds_a_jobset(client, monkeypatch):
    """`nodes: N` renders a JobSet: one indexed Job of N whole-node pods, restart-all on any pod loss, torchrun/NCCL
    env, InfiniBand pools + fabric NICs when required, node-local scratch, checkpoints on the shared claim."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    body = {"name": "pretrain", "input": {"image": "registry.serverless2.local/nvcr/nvidia/pytorch:25.09-py3", "command": "torchrun --nnodes=$NNODES train.py",
                                          "nodes": 2, "gpus_per_node": 8, "interconnect": "required", "checkpoints": "shared", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/d"}}
    r = c.post("/v1/models/distributed-run:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()
    assert op["status"] == "QUEUED" and op["mode"] == "run" and op["nodes"] == 2 and op["resumable"] is False
    js = fake.custom[("tenant-demo", "jobsets", op["id"])]
    assert js["kind"] == "JobSet" and js["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-h100"
    spec = js["spec"]
    assert spec["network"]["enableDNSHostnames"] is True and spec["network"]["subdomain"] == op["id"]
    assert spec["failurePolicy"] == {"maxRestarts": 3, "restartStrategy": "Recreate"} and spec["successPolicy"]["operator"] == "All"
    rj = spec["replicatedJobs"][0]["template"]["spec"]
    assert (rj["parallelism"], rj["completions"], rj["completionMode"], rj["backoffLimit"]) == (2, 2, "Indexed", 0)
    pod = _jobset_pod(js)
    main = next(x for x in pod["containers"] if x["name"] == "main")
    env = {e["name"]: e for e in main["env"]}
    assert env["NNODES"]["value"] == "2" and env["GPUS_PER_NODE"]["value"] == "8" and env["WORLD_SIZE"]["value"] == "16"
    assert env["MASTER_ADDR"]["value"] == f"{op['id']}-workers-0-0.{op['id']}" and env["MASTER_PORT"]["value"] == "29500"
    assert env["NODE_RANK"]["valueFrom"]["fieldRef"]["fieldPath"].endswith("job-completion-index']") and env["NCCL_IB_HCA"]["value"] == "mlx5"
    assert env["CHECKPOINT_DIR"]["value"] == "/work/checkpoint" and env["OPERATION"]["value"] == op["id"]
    assert main["resources"]["limits"]["nvidia.com/gpu"] == "8" and main["resources"]["claims"] == [{"name": "ib"}]
    assert pod["resourceClaims"] == [{"name": "ib", "resourceClaimTemplateName": "ib-8"}]
    assert main["securityContext"]["capabilities"] == {"drop": ["ALL"]} and main["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}   # no IPC_LOCK: memlock is unlimited on the runtime
    terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
    assert {"key": f"{L}/pool", "operator": "In", "values": ["h200-ib-8x"]} in terms          # whole 8-GPU nodes on InfiniBand only
    assert {"key": f"{L}/interconnect", "operator": "In", "values": ["infiniband"]} in terms
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["work"]["emptyDir"]["sizeLimit"] == "500Gi" and vols["checkpoints"]["persistentVolumeClaim"]["claimName"] == "scratch-shared"
    assert {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{op['id']}"} in main["volumeMounts"]
    up = {e["name"]: e.get("value") for e in next(x for x in pod["containers"] if x["name"] == "uploader")["env"]}
    assert up["UPLOAD_SCOPE"] == "rank0" and "PVC_NAME" not in up and up["UPLOAD_EXCLUDES"] == "--exclude checkpoint/*"
    assert fake.pvcs == {}                                   # no per-run RWO volume: /work is per node
    fetch = pod["initContainers"][0]
    assert fetch["name"] == "fetch" and {e["name"]: e.get("value") for e in fetch["env"]}["INPUT_PREFIX"] == "s3://serverless2-demo-eu-north1/inputs/d"
    # read back as an operation, then cancel while queued: the JobSet is deleted
    got = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert got["status"] == "QUEUED" and got["nodes"] == 2 and got["attempts"] == []
    assert c.post(f"/v1/operations/{op['id']}:cancel", headers=H).json()["status"] == "CANCELLED" and op["id"] in fake.deleted
    # the default is `required` (docs/JOBS.md "Limits of multi-node runs"): InfiniBand pool + claim without asking
    r = c.post("/v1/models/distributed-run:invoke", json={"input": {"image": "x/y:1", "command": "echo", "nodes": 4}}, headers=H)
    assert r.status_code == 202, r.text
    pod = _jobset_pod(fake.custom[("tenant-demo", "jobsets", r.json()["id"])])
    assert pod["resourceClaims"] == [{"name": "ib", "resourceClaimTemplateName": "ib-8"}]
    assert {"key": f"{L}/pool", "operator": "In", "values": ["h200-ib-8x"]} in pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
    # NCCL over TCP only on explicit opt-in: whole 8-GPU nodes of the class, no claim, soft preference for IB pools
    r = c.post("/v1/models/distributed-run:invoke", json={"input": {"image": "x/y:1", "command": "echo", "nodes": 4, "interconnect": "preferred"}}, headers=H)
    pod = _jobset_pod(fake.custom[("tenant-demo", "jobsets", r.json()["id"])])
    main = next(x for x in pod["containers"] if x["name"] == "main")
    terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
    assert terms == [{"key": f"{L}/pool", "operator": "In", "values": ["h100-spot-8x", "h200-ib-8x"]}] and "resourceClaims" not in pod
    assert main["securityContext"]["capabilities"] == {"drop": ["ALL"]} and "NCCL_IB_HCA" not in {e["name"] for e in main["env"]}
    assert pod["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]["preference"]["matchExpressions"][0]["key"] == f"{L}/interconnect"
    assert "checkpoints" not in {v["name"] for v in pod["volumes"]}
    # required InfiniBand with no such pool in the fleet is refused up front
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    r = c.post("/v1/models/distributed-run:invoke", json={"input": {"image": "x/y:1", "command": "echo", "nodes": 2}}, headers=H)
    assert r.status_code == 400 and "InfiniBand" in r.json()["detail"] and "interconnect: none" in r.json()["detail"]
    r = c.post("/v1/models/distributed-run:invoke", json={"input": {"image": "x/y:1", "command": "echo", "nodes": 2, "interconnect": "none"}}, headers=H)
    assert r.status_code == 400 and "no pool with 8-GPU nodes" in r.json()["detail"]   # FLEET has no whole-node pool at all


def test_multinode_status_cancel_and_resume(client, monkeypatch):
    """A running JobSet: attempts are its pods (one per node); cancel suspends it; a cancelled run with shared
    checkpoints resumes as <id>-r1 on the same checkpoint directory; billing sums pod-seconds x GPUs."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    r = c.post("/v1/models/distributed-run:invoke", json={"input": {"image": "x/y:1", "command": "echo", "nodes": 2, "checkpoints": "shared"}}, headers=H)
    op = r.json()
    for i in range(2):
        p = pod(op["id"], "Running", gpus=8)
        p["metadata"]["name"] = f"{op['id']}-workers-0-{i}"
        p["metadata"]["labels"] = {"jobset.sigs.k8s.io/jobset-name": op["id"], f"{L}/tenant": "demo", f"{L}/mode": "run"}
        fake.pods.append(p)
    got = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert got["status"] == "RUNNING" and len(got["attempts"]) == 2 and got["started_at"] == "2026-10-05T20:01:00Z"
    assert c.get("/v1/operations", headers=H).json()[0]["id"] == op["id"]
    cancelled = c.post(f"/v1/operations/{op['id']}:cancel", headers=H).json()
    assert cancelled["status"] == "CANCELLED" and fake.custom[("tenant-demo", "jobsets", op["id"])]["spec"]["suspend"] is True
    # the pods ended (SIGTERM): the run is CANCELLED and resumable (shared checkpoints)
    for p in fake.pods:
        p["status"]["containerStatuses"][0]["state"] = {"terminated": {"startedAt": "2026-10-05T20:01:00Z", "finishedAt": "2026-10-05T20:31:00Z", "exitCode": 143}}
    got = c.get(f"/v1/operations/{op['id']}", headers=H).json()
    assert got["status"] == "CANCELLED" and got["resumable"] is True and got["ended_at"] == "2026-10-05T20:31:00Z"
    r = c.post(f"/v1/operations/{op['id']}:resume", headers=H)
    assert r.status_code == 202, r.text
    assert r.json()["id"] == f"{op['id']}-r1" and r.json()["resumed_from"] == op["id"]
    new = fake.custom[("tenant-demo", "jobsets", f"{op['id']}-r1")]
    main = next(x for x in _jobset_pod(new)["containers"] if x["name"] == "main")
    assert {"name": "checkpoints", "mountPath": "/work/checkpoint", "subPath": f"checkpoints/{op['id']}"} in main["volumeMounts"]
    import billing
    job, _ = jobs.find("tenant-demo", op["id"])
    gpu_s, usd = billing.cost_of(job, catalog.get("distributed-run"), [], "eu-north1")
    assert gpu_s == 2 * 1800 * 8 and usd == round(gpu_s / 3600 * 2.15, 6)


# ---- runtime models: a container becomes an endpoint or a job class through the API ---------------------
ADMIN = {"Authorization": "Bearer sk-admin"}


class FakeDB:
    """services/api/db.py in memory: the models table and its history."""
    def __init__(self):
        self.rows, self.hist = {}, []

    def enabled(self): return True

    def list_models(self): return dict(sorted(self.rows.items()))

    def get_model(self, mid): return self.rows.get(mid)

    def upsert_model(self, entry, spec, by, managed_by="api"):
        version = self.rows[entry["id"]]["version"] + 1 if entry["id"] in self.rows else 1
        self.rows[entry["id"]] = {"id": entry["id"], "kind": spec.get("kind", "endpoint"), "spec": copy.deepcopy(spec), "entry": copy.deepcopy(entry),
                                  "version": version, "managed_by": managed_by, "updated_by": by}
        self.hist.append({"version": version, "action": "create" if version == 1 else "update", "by": by, "at": "now", "spec": copy.deepcopy(spec), "id": entry["id"]})
        return self.rows[entry["id"]]

    def delete_model(self, mid, by):
        if mid not in self.rows:
            return False
        self.hist.append({"version": self.rows[mid]["version"] + 1, "action": "delete", "by": by, "at": "now", "spec": self.rows[mid]["spec"], "id": mid})
        del self.rows[mid]
        return True

    def history(self, mid, limit=50): return [h for h in reversed(self.hist) if h["id"] == mid][:limit]


@pytest.fixture
def admin_keys(monkeypatch):
    """sk-admin is an admin key of tenant demo, every other key a plain user key; this API is the fleet's
    (control) API: it has the model database."""
    async def fake_info(key):
        return dict(ADMIN_INFO) if key == "sk-admin" else dict(KEY_INFO)
    monkeypatch.setattr(auth, "key_info", fake_info)
    import db, models as m
    monkeypatch.setattr(m, "CHART_DIR", os.path.join(ROOT, "charts", "endpoint"))
    fdb = FakeDB()
    for name in ("enabled", "list_models", "get_model", "upsert_model", "delete_model", "history"):
        monkeypatch.setattr(db, name, getattr(fdb, name))
    monkeypatch.setattr(m, "ENDPOINT_DOMAINS", {"eu-north1": "203.0.113.10.sslip.io", "eu-south1": "203.0.113.20.sslip.io"})
    auth.forget("sk-admin"); auth.forget("sk-good")
    catalog.invalidate()
    return fdb


def test_normalise_image_maps_public_registries_to_the_cache_host():
    import models as m
    assert m.normalise_image("vllm/vllm-openai:v0.11.0") == "registry.serverless2.local/docker/vllm/vllm-openai:v0.11.0"
    assert m.normalise_image("busybox:1.36") == "registry.serverless2.local/docker/library/busybox:1.36"
    assert m.normalise_image("nvcr.io/example/http-example:1.0.0") == "registry.serverless2.local/nvcr/example/http-example:1.0.0"
    assert m.normalise_image("ghcr.io/org/app:1") == "registry.serverless2.local/ghcr/org/app:1"
    assert m.normalise_image("registry.serverless2.local/nebius/x:1") == "registry.serverless2.local/nebius/x:1"
    assert m.normalise_image("cr.example.com/team/private:1") == "cr.example.com/team/private:1"   # unknown registry: direct pull
    assert m.normalise_image("{{image}}") == "{{image}}"


def test_create_endpoint_from_a_container(client, admin_keys, monkeypatch):
    c, fake = client
    fdb = admin_keys
    # the fleet's API also reaches eu-south1: it gets a read-only copy of every definition
    south = FakeCluster()
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    groups = []
    import httpx
    class FakeLLM:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None, headers=None):
            groups.append((url.rsplit("/", 1)[1], json)); return httpx.Response(200, json={})
    import models as m
    monkeypatch.setattr(m, "LITELLM_MASTER_KEY", "sk-master")
    monkeypatch.setattr(m, "LITELLM_INTERNAL_KEY", "sk-platform-internal")
    monkeypatch.setattr(httpx, "Client", FakeLLM)
    spec = {"id": "my-llm", "kind": "endpoint", "image": "vllm/vllm-openai:v0.11.0", "args": ["--model", "example-org/small-chat-model"],
            "port": 8000, "protocol": "openai", "health_path": "/health", "served_model": "example-org/small-chat-model",
            "gpu": {"count": 1, "classes": ["h100"]}, "scaling": {"min": 0, "max": 2}, "env": {"HF_HOME": "/weights"},
            "weights": {"path": "my-llm"}, "images": {"default": "ignored-when-image-is-set"}}
    assert c.post("/v1/models", json=spec, headers=H).status_code == 403          # user key
    r = c.post("/v1/models", json=spec, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["regions"] == ["hub"] and body["model"]["managed_by"] == "api" and body["model"]["protocol"] == "openai"
    isvc = fake.custom[("models", "inferenceservices", "my-llm")]
    cont = isvc["spec"]["predictor"]["containers"][0]
    assert cont["image"] == "registry.serverless2.local/docker/vllm/vllm-openai:v0.11.0"
    assert cont["args"] == ["--model", "example-org/small-chat-model"] and isvc["spec"]["predictor"]["maxReplicas"] == 2
    assert isvc["spec"]["predictor"]["nodeSelector"]["serverless2.nebius/pool"] == "h100-spot-1x"
    row = fdb.rows["my-llm"]
    assert row["version"] == 1 and row["managed_by"] == "api" and row["updated_by"] == "tenant-demo" and row["entry"]["runtime"]["port"] == 8000
    assert "configmaps" not in fake.__dict__                                   # the control API's source is the database ...
    cm = south.configmaps[("api", "catalog-my-llm")]                           # ... every other region holds a copy
    assert cm["metadata"]["labels"]["serverless2.nebius/catalog"] == "runtime" and "runtime:" in cm["data"]["entry.yaml"]
    # the OpenAI endpoint is a LiteLLM model group whose deployment is the gateway hostname of its region
    assert groups[-1][0] == "new" and groups[-1][1]["litellm_params"]["api_base"] == "https://my-llm-predictor.models.203.0.113.10.sslip.io/v1"
    assert groups[-1][1]["model_info"]["id"] == "my-llm--hub" and body["version"] == 1
    # the group calls the gateway hostname as the platform-internal key (the edge requires a LiteLLM key)
    assert groups[-1][1]["litellm_params"]["api_key"] == "sk-platform-internal" and "warnings" not in body
    # the hostname is on the region's model certificate (ACME), issued through the gateway
    cert = fake.custom[("envoy-gateway-system", "certificates", "models")]["spec"]
    assert cert["dnsNames"] == ["my-llm-predictor.models.203.0.113.10.sslip.io"] and cert["issuerRef"]["name"] == "letsencrypt"
    assert ("envoy-gateway-system", "certificates", "models") not in south.custom      # nothing deployed there
    # it is a model like any other now, with the served model name injected on calls
    got = c.get("/v1/models/my-llm", headers=H).json()
    assert got["modes"] == ["sync", "async"] and got["spec"]["image"] == "vllm/vllm-openai:v0.11.0"
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 409          # exists; PUT replaces
    spec["scaling"] = {"min": 1, "max": 3}
    r = c.put("/v1/models/my-llm", json=spec, headers=ADMIN)
    assert r.status_code == 200 and fake.custom[("models", "inferenceservices", "my-llm")]["spec"]["predictor"]["minReplicas"] == 1
    assert r.json()["version"] == 2 and fdb.rows["my-llm"]["version"] == 2
    hist = c.get("/v1/models/my-llm/history", headers=ADMIN).json()
    assert [h["action"] for h in hist] == ["update", "create"] and hist[0]["spec"]["scaling"] == {"min": 1, "max": 3}
    assert c.get("/v1/models/my-llm/history", headers=H).status_code == 403
    assert c.delete("/v1/models/my-llm", headers=ADMIN).status_code == 204
    assert ("models", "inferenceservices", "my-llm") not in fake.custom and ("api", "catalog-my-llm") not in south.configmaps
    assert "my-llm" not in fdb.rows and groups[-1] == ("delete", {"id": "my-llm--hub"})
    cert = fake.custom[("envoy-gateway-system", "certificates", "models")]["spec"]          # back to the placeholder
    assert cert["dnsNames"] == ["models.example.invalid"] and cert["issuerRef"]["name"] == "selfsigned"
    assert c.get("/v1/models/my-llm", headers=H).status_code == 404
    assert c.get("/v1/models/my-llm/history", headers=ADMIN).json()[0]["action"] == "delete"


def test_model_group_needs_the_internal_key(client, admin_keys, monkeypatch, caplog):
    """Without LITELLM_INTERNAL_KEY the endpoint is deployed but no LiteLLM group is registered: a warning in the
    response and the log, never a call with a dummy key the edge would reject."""
    c, fake = client
    calls = []
    import httpx
    class FakeLLM:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None, headers=None):
            calls.append(url); return httpx.Response(200, json={})
    import models as m
    monkeypatch.setattr(m, "LITELLM_MASTER_KEY", "sk-master")
    monkeypatch.setattr(m, "LITELLM_INTERNAL_KEY", "")
    monkeypatch.setattr(httpx, "Client", FakeLLM)
    spec = {"id": "my-llm", "kind": "endpoint", "image": "vllm/vllm-openai:v0.11.0", "port": 8000, "protocol": "openai",
            "gpu": {"count": 1, "classes": ["h100"]}}
    with caplog.at_level("WARNING", logger="api"):
        r = c.post("/v1/models", json=spec, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert ("models", "inferenceservices", "my-llm") in fake.custom                 # the endpoint itself is there
    assert calls == [] and any("LITELLM_INTERNAL_KEY" in w for w in r.json()["warnings"])
    assert any("LITELLM_INTERNAL_KEY" in rec.getMessage() for rec in caplog.records)
    assert c.delete("/v1/models/my-llm", headers=ADMIN).status_code == 204


def test_regional_api_serves_copies_and_refuses_writes(client, admin_keys, monkeypatch):
    """A regional API has no database: it lists the ConfigMap copies the control API wrote and answers writes with 409."""
    c, fake = client
    import db, models as m
    monkeypatch.setattr(db, "enabled", lambda: False)
    catalog.invalidate()
    entry = m.to_entry({"id": "copied", "kind": "job", "image": "busybox", "command": "echo hi"})
    m.save(entry)                                        # what the control API does into this cluster
    catalog.invalidate()
    got = c.get("/v1/models/copied", headers=H).json()
    assert got["managed_by"] == "api" and got["default_mode"] == "run"
    r = c.post("/v1/models", json={"id": "x", "image": "busybox"}, headers=ADMIN)
    assert r.status_code == 409 and "api.example" in r.json()["detail"]
    assert c.delete("/v1/models/copied", headers=ADMIN).status_code == 409


def test_create_job_class_and_run_it(client, admin_keys):
    c, fake = client
    spec = {"id": "my-trainer", "kind": "job", "image": "ghcr.io/org/trainer:1.2", "command": "python train.py --out $OUTPUT_DIR",
            "gpu": {"count": 1, "classes": ["h100"]}, "cpu": "8", "memory": "32Gi", "disk_gi": 100,
            "parameters": [{"name": "epochs", "type": "number", "default": 1}]}
    r = c.post("/v1/models", json=spec, headers=ADMIN)
    assert r.status_code == 201, r.text
    got = c.get("/v1/models/my-trainer", headers=H).json()
    assert got["default_mode"] == "run" and got["parameters"][0]["name"] == "epochs" and got["managed_by"] == "api"
    r = c.post("/v1/models/my-trainer:invoke", json={"mode": "run", "input": {"epochs": 2}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    main = _main(job)
    assert main["image"] == "registry.serverless2.local/ghcr/org/trainer:1.2"
    assert main["resources"]["limits"]["nvidia.com/gpu"] == "1" or main["resources"]["limits"]["nvidia.com/gpu"] == 1


def test_bundled_and_terraform_models_are_read_only(client, admin_keys):
    c, fake = client
    assert c.delete("/v1/models/hello-run", headers=ADMIN).status_code == 409
    r = c.put("/v1/models/hello-run", json={"id": "hello-run", "kind": "job", "image": "busybox"}, headers=ADMIN)
    assert r.status_code == 409


def test_model_spec_validation(client, admin_keys):
    c, fake = client
    assert c.post("/v1/models", json={"id": "Bad Id", "image": "x"}, headers=ADMIN).status_code == 400
    assert c.post("/v1/models", json={"id": "no-image"}, headers=ADMIN).status_code == 400
    assert c.post("/v1/models", json={"id": "no-class", "image": "x", "gpu": {"classes": ["b300"]}}, headers=ADMIN).status_code == 400
    # no GPU class: a CPU endpoint on the region's system pool (the endpoint chart needs a pool)
    r = c.post("/v1/models", json={"id": "cpu-echo", "kind": "endpoint", "image": "x", "port": 80, "protocol": "http",
                                   "gpu": {"count": 0, "classes": []}, "regions": ["eu-north1"]}, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    assert r.json()["model"]["gpu"] == "none"
    assert '"serverless2.nebius/pool": "system"' in json.dumps(fake.custom[("models", "inferenceservices", "cpu-echo")])
    assert c.post("/v1/models", json={"id": "bad-proto", "image": "x", "protocol": "ftp"}, headers=ADMIN).status_code == 400
