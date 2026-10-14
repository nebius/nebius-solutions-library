"""The generated API contract against actual handler output.

The shared Pydantic types own requests and responses. These tests exercise fixture Jobs, durable attempt
records, endpoints, fleet status, keys and bindings; undeclared output fields still fail the contract gate.
"""

import ast
import copy
import inspect
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

import app as appmod
import attempts
import auth
import catalog
import db
import endpoints as ep
import kube
import models as models_mod
import schemas
import status
from test_api import ADMIN_INFO, FLEET, H, L, admin_keys, client, cluster, fake_keys, pod  # noqa: F401

SPEC = appmod.app.openapi()


def resolved(node):
    if isinstance(node, list):
        return [resolved(v) for v in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return resolved(SPEC["components"]["schemas"][node["$ref"].rsplit("/", 1)[1]])
    node = {k: resolved(v) for k, v in node.items()}
    choices = node.get("anyOf", [])
    nonnull = [v for v in choices if v.get("type") != "null"]
    if len(nonnull) == 1:
        node = {**node, **nonnull[0]}
        if "enum" in node and len(nonnull) < len(choices):
            node["enum"] = [*node["enum"], None]
    return node


SCHEMAS = resolved(SPEC["components"]["schemas"])
SCHEMAS["ExportState"] = SCHEMAS["ExportStateValue"]
REAL_ENDPOINT_STATUS = kube.endpoint_status  # the client fixture replaces it; _live_statuses runs the real one
REAL_DB_HISTORY = db.history  # the admin_keys fixture replaces it with the fake's


def props(name: str) -> set:
    return set(SCHEMAS[name]["properties"])


def enum(name: str) -> set:
    return set(SCHEMAS[name]["enum"]) - {None}


def covered(obj: dict, component: str, where: str = "") -> None:
    getattr(schemas, component).model_validate(obj)
    extra = set(obj) - props(component)
    assert (
        not extra
    ), f"{where or component}: the code returns {sorted(extra)}, generated contract {component} does not declare them"


# ---- requests ----------------------------------------------------------------------------------------------------


def _spec_keys_read_by_models_py() -> set:
    """Every spec key services/api/models.py reads in validate, _validate_variants and to_entry (`s.get("x")`,
    `s["x"]`, `"x" in s`), plus the reserved keys it carries through as given."""
    tree = ast.parse(inspect.getsource(models_mod))
    found = set(models_mod.RESERVED)
    for fn in (
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("validate", "_validate_variants", "to_entry")
    ):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "setdefault", "pop")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("s", "spec")
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("s", "spec")
                and isinstance(node.slice, ast.Constant)
            ):
                found.add(node.slice.value)
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Constant)
                and any(isinstance(c, ast.Name) and c.id in ("s", "spec") for c in node.comparators)
            ):
                found.add(node.left.value)
    return {k for k in found if isinstance(k, str)}


def test_model_spec_component_names_every_key_models_py_reads():
    read = _spec_keys_read_by_models_py()
    assert read, "the extraction found nothing: models.py changed shape, adjust the walker"
    missing = read - props("ModelSpec")
    assert (
        not missing
    ), f"models.py reads {sorted(missing)} from a model spec; generated contract ModelSpec does not declare them"
    stale = props("ModelSpec") - read
    assert not stale, f"generated contract ModelSpec declares {sorted(stale)}, which models.py never reads"
    assert set(SCHEMAS["ScalingSpec"]["properties"]) == set(models_mod.ScalingSpec.model_json_schema()["properties"])


# ---- the operation --------------------------------------------------------------------------------------------------


def _job(name="op-1", **annotations):
    """A run's Job as the API renders it, with every annotation status.normalise reads."""
    ann = {
        f"{L}/input": '{"nsteps": "5"}',
        f"{L}/pvc": f"{name}-work",
        f"{L}/name": "label",
        f"{L}/image": "registry/img:1",
        f"{L}/execution": '{"io_mode": "managed", "placement": {"gpu_preference": ["h100"]}}',
        f"{L}/nodes": "1",
        f"{L}/interconnect": "none",
        f"{L}/gpu-seconds": "12.5",
        f"{L}/billed": "0.01",
        f"{L}/resumed-from": "op-0",
        f"{L}/fallback": "rtx-pro-6000 -> l40s after 60 s without capacity",
        f"{L}/recovered-from": "op-0",
        **{f"{L}/{k.replace('_', '-')}": v for k, v in annotations.items()},
    }
    return {
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": "tenant-demo",
            "creationTimestamp": "2026-10-05T18:49:23Z",
            "labels": {
                f"{L}/model": "batch-example",
                f"{L}/mode": "run",
                f"{L}/tenant": "demo",
                f"{L}/region": "eu-north1",
                f"{L}/profile": "prefer-h100",
                f"{L}/priority": "high",
                f"{L}/gpu-class": "h100",
            },
            "annotations": ann,
        },
        "spec": {"activeDeadlineSeconds": 3600},
        "status": {},
        "_pods": [],
        "_region": "eu-north1",
    }


RECORD = {
    "operation": "op-1",
    "pod": "op-1-suc",
    "status": "succeeded",
    "export": "succeeded",
    "gpus": 1,
    "epoch": 2,
    "revision": 4,
    "result_ref": "s3://b/out",
    "checkpoint_ref": "app://ckpt/7",
    "checkpoint_generation": 7,
    "image_digest": "sha256:abc",
    "role": "run",
    "started_at": "2026-10-05T18:57:29Z",
    "ended_at": "2026-10-05T19:10:37Z",
    "cluster": "hub",
    "region": "eu-north1",
    "tenant": "demo",
    "root": "op-1",
}


def _operations():
    """status.normalise over the states the code distinguishes: every key and status it can produce."""
    out = []
    done = _job()
    done["status"] = {
        "startTime": "2026-10-05T18:49:23Z",
        "completionTime": "2026-10-05T19:10:47Z",
        "conditions": [{"type": "Complete", "status": "True"}],
    }
    done["_pods"] = [
        pod("op-1", "Failed", "2026-10-05T18:49:23Z", "2026-10-05T18:57:29Z", 137, disrupted=True),
        pod("op-1", "Succeeded", "2026-10-05T18:57:29Z", "2026-10-05T19:10:37Z", 0),
    ]
    out.append(status.normalise(done, 2.0, [RECORD]))  # SUCCEEDED, delivery on record
    out.append(status.normalise(done, 2.0, [{**RECORD, "export": "failed"}]))  # EXPORT_FAILED
    out.append(status.normalise(done, 2.0, []))  # FINALIZING / EXPORT_FAILED by age
    failed = _job()
    failed["status"] = {
        "startTime": "2026-10-05T18:49:23Z",
        "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded", "message": "backoff"}],
    }
    failed["_pods"] = [pod("op-1", "Failed", "2026-10-05T18:49:23Z", "2026-10-05T18:57:29Z", 1)]
    out.append(status.normalise(failed))  # FAILED
    cancelled = copy.deepcopy(failed)
    cancelled["metadata"]["annotations"][f"{L}/cancelled"] = "true"
    cancelled["_pods"] = [pod("op-1", "Running")]
    out.append(status.normalise(cancelled))  # CANCELLED with a cancelled attempt
    queued = _job()
    queued["_pods"] = [pod("op-1", "Pending")]
    out.append(status.normalise(queued))  # QUEUED: phase + reason
    manager = _job(successor="op-1-r1")
    manager["spec"]["managedBy"] = "kueue.x-k8s.io/multikueue"
    manager["_reason"] = "waiting for admission"
    out.append(status.normalise(manager))  # SUPERSEDED
    running = _job()
    running["_pods"] = [pod("op-1", "Running")]
    out.append(
        status.normalise(
            running,
            None,
            [
                {**RECORD, "pod": "op-1-gone", "status": "interrupted"},
                {**RECORD, "pod": "op-1-lost", "status": "started"},
            ],
        )
    )
    jobset = _job(checkpoints="shared")
    jobset["kind"] = "JobSet"
    jobset["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    jobset["_pods"] = [pod("op-1", "Failed", "2026-10-05T18:49:23Z", "2026-10-05T18:57:29Z", 1)]
    out.append(status.normalise(jobset))  # a multi-node run
    return out


def test_normalised_operations_are_declared():
    statuses = set()
    for op in _operations():
        covered(op, "Operation", f"Operation {op['status']}")
        statuses.add(op["status"])
        assert op["status"] in enum("OperationStatus"), op["status"]
        if op["phase"] is not None:
            assert op["phase"] in set(SCHEMAS["Operation"]["properties"]["phase"]["enum"]) - {None}, op["phase"]
        for a in op["attempts"]:
            covered(a, "Attempt", f"Attempt of {op['status']}")
            assert a["status"] in enum("AttemptStatus"), a["status"]
            assert a["attempt_role"] in set(SCHEMAS["Attempt"]["properties"]["attempt_role"]["enum"])
            if a.get("export_state") is not None:
                assert a["export_state"] in enum("ExportState")
    assert {"SUCCEEDED", "EXPORT_FAILED", "FAILED", "CANCELLED", "QUEUED", "RUNNING", "SUPERSEDED"} <= statuses
    # the required keys are always present
    for op in _operations():
        assert set(SCHEMAS["Operation"]["required"]) <= set(op)


def test_every_status_literal_in_the_code_is_in_the_enum():
    """The statuses status.py, attempts.py and app.py assign (`status = "X"`, `"status": "X"`, mapping values) are all
    declared: a new state in the code must reach the schema."""
    found = set()
    for mod in (status, attempts, appmod):
        src = inspect.getsource(mod)
        found |= set(re.findall(r'(?:status\s*(?:,\s*\w+\s*)?=\s*|"status":\s*|: )"([A-Z][A-Z_]+)"', src))
        found |= set(re.findall(r'status\s*(?:,\s*\w+\s*)?=\s*"([A-Z][A-Z_]+)",', src))
    found |= set(status.PHASE.values()) | set(status.RECORD_STATUS.values())
    assert found - {"FINALIZING"} <= enum("OperationStatus") | enum("AttemptStatus"), found
    assert {"PREEMPTED", "FINALIZING", "EXPORT_FAILED", "SUPERSEDED", "CANCELLED"} <= enum("OperationStatus")
    assert "CANCELLED" in enum("AttemptStatus")


def test_lifecycle_chain_and_finalizer_views_are_declared(client, admin_keys, fake_keys, monkeypatch):
    """GET /v1/operations/{id} with the attempt records of a chain: the lifecycle fields (attempts.lifecycle), the
    chain view (app._chain), the finalizer view (app._finalizers) and the delivery closure (`exported_by`)."""
    c, fake = client
    r = c.post(
        "/v1/models/batch-example:invoke",
        json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in", "nsteps": 7}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    covered(r.json(), "Operation", "invoke response")
    rows = [
        {
            **RECORD,
            "operation": op,
            "pod": f"{op}-a",
            "root": op,
            "export_state": "succeeded",
            "image": "registry/img:1",
        },
        {
            **RECORD,
            "operation": f"{op}-r1",
            "pod": f"{op}-r1-a",
            "root": op,
            "status": "failed",
            "export_state": "failed",
            "epoch": 2,
            "revision": 5,
        },
    ]
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == op else [])
    fake.jobs[("tenant-demo", op)]["status"] = {
        "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]
    }
    # an active application finalizer of the root
    fz = copy.deepcopy(fake.jobs[("tenant-demo", op)])
    fz["metadata"]["name"] = f"{op}-x1"
    fz["metadata"]["annotations"].update({f"{L}/export-of": op, f"{L}/attempt-role": "finalize"})
    fz["status"] = {}
    fake.jobs[("tenant-demo", f"{op}-x1")] = fz
    got = c.get(f"/v1/operations/{op}", headers=H)
    assert got.status_code == 200, got.text
    view = got.json()
    assert (
        view["status"] == "FINALIZING"
        and view["finalizing_by"] == f"{op}-x1"
        and view["chain"]
        and view["root_operation_id"] == op
    )
    covered(view, "Operation", "Operation with chain and finalizer")
    for member in view["chain"]:
        covered(member, "ChainMember")
        assert member["status"] in enum("OperationStatus")
    for a in view["attempts"]:
        covered(a, "Attempt", "Attempt in the chain view")
    # the finalizer delivered: the root closes with exported_by
    del fake.jobs[("tenant-demo", f"{op}-x1")]
    rows.append(
        {
            **RECORD,
            "operation": f"{op}-x1",
            "pod": f"{op}-x1-a",
            "root": op,
            "export_state": "succeeded",
            "role": "finalize",
            "epoch": 3,
            "revision": 6,
        }
    )
    view = c.get(f"/v1/operations/{op}", headers=H).json()
    assert view["status"] == "SUCCEEDED" and view["exported_by"] == f"{op}-x1"
    covered(view, "Operation", "Operation closed by a finalizer")
    assert c.get(f"/v1/operations/{op}?since=99", headers=H).status_code == 304
    monkeypatch.setattr(
        appmod.artifacts,
        "list_outputs",
        lambda ns, id, region, prefix: [
            {"name": "out.txt", "uri": "s3://b/out.txt", "url": "https://x", "size_bytes": 1}
        ],
    )
    res = c.get(f"/v1/operations/{op}/result", headers=H)
    assert res.status_code == 200, res.text
    covered(res.json(), "OperationResult")
    for art in res.json()["artifacts"]:
        covered(art, "Artifact")
    # the durable record of an operation whose Job is gone
    syn = attempts.synthesize(op, "demo")
    covered(syn, "Operation", "Operation synthesized from its records")
    for a in syn["attempts"]:
        covered(a, "Attempt", "Attempt synthesized from its record")
    assert syn["status"] in enum("OperationStatus")
    monkeypatch.setattr(attempts, "successor_of", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    del fake.jobs[("tenant-demo", op)]
    gone = c.get(f"/v1/operations/{op}", headers=H)
    assert gone.status_code == 200 and gone.json()["reason"]
    covered(gone.json(), "Operation", "Operation served from its durable record")
    gone_result = c.get(f"/v1/operations/{op}/result", headers=H)
    assert gone_result.status_code == 200, gone_result.text
    covered(gone_result.json(), "OperationResult", "OperationResult from the durable record")


def test_cancel_and_resume_responses_are_declared(client, monkeypatch):
    c, fake = client
    op = c.post(
        "/v1/models/batch-example:invoke",
        json={"input": {"input_prefix": "s3://serverless2-demo-eu-north1/in", "nsteps": 7}},
        headers=H,
    ).json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    resumed = c.post(f"/v1/operations/{op}:resume", json={}, headers=H)
    assert resumed.status_code == 202, resumed.text
    covered(resumed.json(), "Operation", "resume response")
    cancelled = c.post(f"/v1/operations/{resumed.json()['id']}:cancel", headers=H)
    assert cancelled.status_code == 200, cancelled.text
    covered(cancelled.json(), "Operation", "cancel response")
    assert cancelled.json()["status"] in enum("OperationStatus")


# ---- endpoints and models --------------------------------------------------------------------------------------------


def _pod(ready: bool, worker_index: str | None = None):
    now = datetime(2026, 10, 10, 15, 17, 23, tzinfo=timezone.utc)
    labels = {"leaderworkerset.sigs.k8s.io/worker-index": worker_index} if worker_index is not None else {}
    return SimpleNamespace(
        metadata=SimpleNamespace(creation_timestamp=now, labels=labels),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=ready)],
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False", last_transition_time=now)],
        ),
    )


def _live_statuses(monkeypatch) -> list[dict]:
    """kube.endpoint_status run against fakes for the states it reports: a ready InferenceService (start-up timing),
    one Knative gave up on (error + reason), a scaled-to-zero one, and a ready LeaderWorkerSet (nodes, pods, last request)."""
    cases = {}

    class Custom:
        def __init__(self, isvc, lws):
            self.isvc, self.lws = isvc, lws

        def get_namespaced_custom_object(self, group, version, ns, plural, name, **kw):
            return self.lws if plural == "leaderworkersets" else self.isvc

    class Core:
        def __init__(self, pods):
            self.pods = pods

        def list_namespaced_pod(self, ns, label_selector=None, **kw):
            return SimpleNamespace(items=self.pods)

    def run(name, isvc=None, lws=None, pods=(), nodes=1):
        monkeypatch.setattr(kube, "api", lambda region=None: Custom(isvc, lws))
        monkeypatch.setattr(kube, "core", lambda region=None: Core(list(pods)))
        kube._isvc_cache.clear()
        cases[name] = REAL_ENDPOINT_STATUS(name, "models", "eu-north1", nodes=nodes)

    run("ready", isvc={"status": {"conditions": [{"type": "Ready", "status": "True"}]}}, pods=[_pod(True)])
    run(
        "failed",
        isvc={
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "RevisionFailed",
                        "message": "ProgressDeadlineExceeded",
                    }
                ]
            }
        },
    )
    run("zero", isvc={"status": {"conditions": [{"type": "Ready", "status": "True"}]}})
    run(
        "lws",
        lws={
            "spec": {"replicas": 1},
            "metadata": {"annotations": {kube.LAST_REQUEST_ANNOTATION: "2026-10-10T16:00:00Z"}},
        },
        pods=[_pod(True, "0"), _pod(True, "1")],
        nodes=2,
    )
    assert (
        cases["ready"]["status"] == "ready"
        and cases["failed"]["status"] == "error"
        and cases["zero"]["status"] == "scaled-to-zero"
        and cases["lws"]["status"] == "ready"
    )
    assert "reason" in cases["failed"] and "startup_s" in cases["ready"] and "last_request_at" in cases["lws"]
    return list(cases.values())


def test_endpoint_status_fields_are_declared_on_model_regions(monkeypatch):
    region_props = set(SCHEMAS["Model"]["properties"]["regions"]["items"]["properties"])
    for st in _live_statuses(monkeypatch):
        extra = set(st) - region_props
        assert not extra, f"kube.endpoint_status returns {sorted(extra)}: Model.regions items do not declare them"
        assert st["status"] in set(SCHEMAS["Model"]["properties"]["regions"]["items"]["properties"]["status"]["enum"])
        assert st["status"] in set(SCHEMAS["Endpoint"]["properties"]["status"]["enum"])


def test_endpoint_shapes_are_declared(client, monkeypatch):
    c, fake = client
    live = _live_statuses(monkeypatch)
    m = catalog.get("llm-example")
    isvc = {
        "kind": "InferenceService",
        "metadata": {
            "name": "llm-example",
            "namespace": "models",
            "creationTimestamp": "2026-10-05T20:00:00Z",
            "annotations": {"autoscaling.knative.dev/scale-to-zero-pod-retention-period": "2m"},
        },
        "spec": {
            "predictor": {
                "minReplicas": 0,
                "maxReplicas": 1,
                "scaleTarget": 4,
                "containers": [{"image": "x", "resources": {"requests": {"cpu": "4", "memory": "16Gi"}}}],
            }
        },
    }
    for st in live:
        monkeypatch.setattr(kube, "endpoint_status", lambda n, ns, region="eu-north1", nodes=1, st=st: st)
        one = ep.to_public(m, isvc, "eu-north1")
        covered(one, "Endpoint", f"Endpoint ({st['status']})")
        assert one["status"] in set(SCHEMAS["Endpoint"]["properties"]["status"]["enum"])
        covered(one["scaling"], "EndpointScaling")
        assert set(SCHEMAS["Endpoint"]["required"]) <= set(one)
    lws = {
        "kind": "LeaderWorkerSet",
        "metadata": {
            "name": "big",
            "creationTimestamp": "2026-10-10T19:45:58Z",
            "annotations": {
                "serverless2.nebius/nodes": "2",
                "serverless2.nebius/interconnect": "none",
                "serverless2.nebius/idle-s": "1800",
            },
        },
        "spec": {
            "replicas": 1,
            "leaderWorkerTemplate": {
                "size": 2,
                "leaderTemplate": {"spec": {"containers": [{"image": "x", "resources": {"requests": {"cpu": "4"}}}]}},
            },
        },
    }
    monkeypatch.setattr(kube, "endpoint_status", lambda n, ns, region="eu-north1", nodes=1, st=live[-1]: st)
    multi = ep.to_public({**m, "nodes": 2}, lws, "eu-north1")
    covered(multi, "Endpoint", "Endpoint (multi-node)")
    covered(multi["scaling"], "EndpointScaling")
    assert multi["scaling"]["metric"] in set(SCHEMAS["EndpointScaling"]["properties"]["metric"]["enum"])
    # the list's unavailable form and the variants block
    monkeypatch.setattr(
        kube, "isvc", lambda n, ns, region="eu-north1": (_ for _ in ()).throw(ApiException(status=503, reason="down"))
    )
    listed = c.get("/v1/endpoints", headers=H)
    assert listed.status_code == 200 and listed.json()
    for e in listed.json():
        covered(e, "Endpoint", "Endpoint (region unavailable)")
        assert e["status"] in set(SCHEMAS["Endpoint"]["properties"]["status"]["enum"])
    monkeypatch.setattr(kube, "endpoint_status", lambda n, ns, region="eu-north1", nodes=1: live[0])
    vm = {
        **m,
        "deployments": {
            "hub": {
                "pool": "h100-spot-1x",
                "variants": [
                    {"name": "primary", "pool": "h100-spot-1x", "min": 0, "max": 1, "image": "x"},
                    {"name": "cheap", "pool": "l40s-ondemand-1x", "min": 0, "max": 1, "image": "y"},
                ],
            }
        },
    }
    for v in appmod._variants_public(vm, "eu-north1"):
        covered(v, "EndpointVariant")


def test_model_shapes_are_declared(client, admin_keys, monkeypatch):
    c, fake = client
    live = _live_statuses(monkeypatch)
    monkeypatch.setattr(kube, "endpoint_status", lambda n, ns, region="eu-north1", nodes=1: live[-1])
    r = c.post(
        "/v1/models",
        json={
            "id": "owned",
            "kind": "endpoint",
            "image": "x",
            "port": 80,
            "protocol": "http",
            "example": {"q": 1},
            "gpu": {"count": 0, "classes": []},
            "regions": ["eu-north1"],
        },
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert r.status_code == 201, r.text
    covered(r.json()["model"], "Model", "Model in the create response")
    for who in (H, {"Authorization": "Bearer sk-admin"}):
        listed = c.get("/v1/models", headers=who)
        assert listed.status_code == 200 and listed.json()
        for m in listed.json():
            covered(m, "Model")
            assert set(SCHEMAS["Model"]["required"]) <= set(m)
            assert m["managed_by"] in set(SCHEMAS["Model"]["properties"]["managed_by"]["enum"])
            for st in m["regions"]:
                extra = set(st) - set(SCHEMAS["Model"]["properties"]["regions"]["items"]["properties"])
                assert not extra, f"Model.regions: {sorted(extra)} undeclared"
    # the history rows as the real db.history builds them (the test fake adds an `id` of its own)
    built = set(re.findall(r'"(\w+)":', inspect.getsource(REAL_DB_HISTORY)))
    declared = set(SCHEMAS["ModelHistory"]["properties"])
    assert built and built == declared, (built, declared)


# ---- fleet, keys, bindings --------------------------------------------------------------------------------------------


def test_fleet_info_is_declared(client, monkeypatch):
    """kube.pool_usage, tenant_usage and cluster_summary run against the fake cluster (their unreachable branches
    return every key); the tenant block comes from the fleet document."""
    c, _ = client
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            **FLEET,
            "tenants": {"demo": {"gpu_quota": 4, "fair_share_weight": 2, "dedicated_pools": ["h100-lab-1x"]}},
        },
    )
    for query in ("", "?live=true"):
        r = c.get(f"/v1/fleet{query}", headers=H)
        assert r.status_code == 200, r.text
        d = r.json()
        covered(d, "FleetInfo", f"FleetInfo{query}")
        covered(d["execution"], "ExecutionCapabilities")
        assert set(d["execution"]["finalize"]) <= set(
            SCHEMAS["ExecutionCapabilities"]["properties"]["finalize"]["properties"]
        )
        assert set(d["execution"]["recovery"]) <= set(
            SCHEMAS["ExecutionCapabilities"]["properties"]["recovery"]["properties"]
        )
        covered(d["tenant"], "FleetTenant")
        for pool in d["pools"]:
            covered(pool, "FleetPool")
        for cl in d.get("clusters", []):
            covered(cl, "FleetCluster")
    assert d["clusters"] and "usage" in d["tenant"] and "nodes_ready" in d["pools"][0]
    assert set(next(iter(d["tenant"]["usage"].values()))) <= set(
        SCHEMAS["FleetTenant"]["properties"]["usage"]["additionalProperties"]["properties"]
    )


def test_key_shapes_are_declared(client, fake_keys, monkeypatch):
    c, _ = client
    me = c.get("/v1/keys/me", headers=H)
    assert me.status_code == 200
    covered(me.json(), "ApiKey", "ApiKey (GET /v1/keys/me)")
    assert me.json()["status"] in set(SCHEMAS["ApiKey"]["properties"]["status"]["enum"])

    async def fake_info(key):
        return dict(ADMIN_INFO)

    monkeypatch.setattr(auth, "key_info", fake_info)
    auth.forget("sk-good")
    created = c.post("/v1/keys", json={"alias": "ci", "budget": 5, "expires_days": 7}, headers=H)
    assert created.status_code == 201, created.text
    covered(created.json(), "ApiKey", "ApiKey (created)")
    for k in c.get("/v1/keys", headers=H).json():
        covered(k, "ApiKey", "ApiKey (listed)")
        assert k["role"] in set(SCHEMAS["ApiKey"]["properties"]["role"]["enum"])
    patched = c.patch("/v1/keys/demo-ci", json={"budget": 50}, headers=H)
    assert patched.status_code == 200
    covered(patched.json(), "ApiKey", "ApiKey (patched)")


def test_binding_shapes_are_declared(client):
    c, _ = client
    put = c.put("/v1/bindings/b1", json={"data": {"token": "x"}}, headers=H)
    assert put.status_code == 200, put.text
    covered(put.json(), "Binding", "Binding (PUT)")
    listed = c.get("/v1/bindings", headers=H)
    assert listed.status_code == 200 and listed.json()
    for b in listed.json():
        covered(b, "Binding", "Binding (listed)")
    assert c.delete("/v1/bindings/b1", headers=H).status_code == 204
