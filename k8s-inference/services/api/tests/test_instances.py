"""Placement and attribution for the operator (api 0.11.122, docs/API.md "Placement", "Requester", "Operation
identity"; docs/FLEET.md "Customer CPU pools"): where an attempt or an endpoint replica ran (pool, instance type,
node group, console link), who asked (the application's `requester` block, the key's alias), the fleet policy that
keeps tenants off the system nodes, and the side-effect-free operation id a later :invoke creates."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import instances
import kube
import models
import render_endpoint
from test_api import ADMIN, H, admin_keys, client, cluster  # noqa: F401 - pytest fixture registration

CLUSTERS = {
    "hub": {"region": "eu-north1", "project": "project-e00rene", "cluster_id": "mk8scluster-e00north"},
    "eu-south1": {"region": "eu-south1", "project": "project-e07south"},  # no cluster id yet: no link
    "control": {"region": "eu-north1", "project": "project-e00rene", "control": True},
}
POOLS = {
    "hub-h100-spot-1x": {
        "region": "hub",
        "pool": "h100-spot-1x",
        "gpu_class": "h100",
        "platform": "gpu-h100-sxm",
        "preset": "1gpu-16vcpu-200gb",
        "node_group_id": "mk8snodegroup-e00h100",
        "project": "project-e00rene",
    }
}
CPU_POOLS = {
    "hub": {
        "system": {"kind": "system", "platform": "cpu-d3", "preset": "8vcpu-32gb", "min_nodes": 2, "max_nodes": 2},
        "cpu-general": {
            "kind": "cpu",
            "platform": "cpu-d3",
            "preset": "8vcpu-32gb",
            "min_nodes": 1,
            "max_nodes": 2,
            "node_group_id": "mk8snodegroup-e00cpu",
        },
        # sorts before cpu-general by name, after it by size: never the pin of a small caller (api 0.11.123)
        "cpu-batch-32": {"kind": "cpu", "platform": "cpu-d3", "preset": "32vcpu-128gb", "min_nodes": 0, "max_nodes": 1},
    },
    "eu-south1": {"system": {"kind": "system", "platform": "cpu-d3", "preset": "8vcpu-32gb"}},
}


def _fleet(monkeypatch, tenant_system_flavor=True, cpu_pools=CPU_POOLS):
    doc = {
        "pools": POOLS,
        "clusters": CLUSTERS,
        "cpu_pools": cpu_pools,
        "policy": {"tenant_system_flavor": tenant_system_flavor},
        "tenants": {},
        "fit": {},
    }
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    monkeypatch.setattr(kube, "cluster_region", lambda c: CLUSTERS.get(c, {}).get("region", c))
    monkeypatch.setattr(kube, "_node_cache", {})
    return doc


# ---- requester ------------------------------------------------------------------------------------------------


def test_requester_block_is_bounded_and_opaque():
    assert instances.validate_requester(None) is None
    ok = {"team": "oncology", "user": "ada", "customer_id": "cust-1"}
    assert instances.validate_requester(ok) == ok
    for bad in ({}, {"Team": "x"}, {"team": ""}, {"team": "x" * 129}, {"team": 1}, {f"k{i}": "v" for i in range(9)}):
        with pytest.raises(HTTPException) as e:
            instances.validate_requester(bad)
        assert e.value.status_code == 400
    job = {"metadata": {}}
    instances.annotate_requester(job, ok)
    ann = job["metadata"]["annotations"]
    assert json.loads(ann[instances.REQUESTER_ANNOTATION]) == ok and instances.requester_of(ann) == ok
    successor = {"metadata": {"annotations": {}}}
    instances.inherit_requester(successor, ann)
    assert instances.requester_of(successor["metadata"]["annotations"]) == ok
    assert instances.requester_of({}) is None and instances.requester_of({instances.REQUESTER_ANNOTATION: "x"}) is None


def test_invoke_stores_the_requester_and_every_read_returns_it(client, monkeypatch):
    c, fake = client
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    who = {"team": "oncology", "user": "ada"}
    bad = c.post("/v1/models/batch-example:invoke", json={**body, "requester": {"Team": "x"}}, headers=H)
    assert bad.status_code in (400, 422)
    r = c.post(
        "/v1/models/batch-example:invoke", json={**body, "requester": who}, headers={**H, "Idempotency-Key": "rq"}
    )
    assert r.status_code == 202, r.text
    op = r.json()
    assert op["requester"] == who
    job = fake.jobs[("tenant-demo", op["id"])]
    assert json.loads(job["metadata"]["annotations"][instances.REQUESTER_ANNOTATION]) == who
    assert c.get(f"/v1/operations/{op['id']}", headers=H).json()["requester"] == who
    listed = {o["id"]: o for o in c.get("/v1/operations", headers=H).json()}
    assert listed[op["id"]]["requester"] == who
    plain = c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "rq2"}).json()
    assert plain["requester"] is None


# ---- placement fields --------------------------------------------------------------------------------------------


def test_fields_from_the_live_node_then_from_the_pool_when_the_node_is_gone(monkeypatch):
    _fleet(monkeypatch)
    live = {
        "eu-north1/computeinstance-e00abc": {
            "instance_type": "gpu-h100-sxm",
            "preset": "1gpu-16vcpu-200gb",
            "node_group": "mk8snodegroup-e00h100",
            "pool": "h100-spot-1x",
        }
    }
    monkeypatch.setattr(kube, "node_placement", lambda region, node: live.get(f"{region}/{node}"))
    f = instances.fields("hub", "computeinstance-e00abc", "h100-spot-1x")
    assert f == {
        "pool": "h100-spot-1x",
        "instance_type": "gpu-h100-sxm/1gpu-16vcpu-200gb",
        "node_group": "mk8snodegroup-e00h100",
        "instance_exact": True,
        "console_url": "https://console.nebius.com/project-e00rene/mk8s/mk8scluster-e00north/node-groups/"
        "mk8snodegroup-e00h100/nodes/computeinstance-e00abc",
    }
    # the node is gone (a reclaimed spot node): the pool's kind of instance, marked inexact, the node group's page
    g = instances.fields("hub", "computeinstance-e00gone", "h100-spot-1x")
    assert g["instance_exact"] is False and g["instance_type"] == "gpu-h100-sxm/1gpu-16vcpu-200gb"
    assert g["console_url"].endswith("/node-groups/mk8snodegroup-e00h100/nodes/computeinstance-e00gone")
    # a CPU pool from cpu_pools.yaml; the system pool has no node group: type without a link
    cpu = instances.fields("hub", "computeinstance-e00cpu", "cpu-general")
    assert cpu["instance_type"] == "cpu-d3/8vcpu-32gb" and cpu["node_group"] == "mk8snodegroup-e00cpu"
    sysp = instances.fields("hub", "computeinstance-e00sys", "system")
    assert sysp["instance_type"] == "cpu-d3/8vcpu-32gb" and sysp["console_url"] is None
    # a cluster without a known mk8s id: the type, no link; an unknown pool: only the pool
    assert instances.fields("eu-south1", "n", "system")["console_url"] is None
    assert instances.fields("hub", "n", "nope") == {"pool": "nope"}
    # the live node's pool stands in when the attempt carries none
    assert instances.fields("hub", "computeinstance-e00abc", None)["pool"] == "h100-spot-1x"


def test_enrich_attempts_adds_placement_only_where_a_node_or_pool_is_known(monkeypatch):
    _fleet(monkeypatch)
    monkeypatch.setattr(kube, "node_placement", lambda region, node: None)
    attempts = [
        {"index": 1, "node": "n1", "pool": "h100-spot-1x", "status": "PREEMPTED"},
        {"index": 2, "status": "QUEUED"},
        {"index": 3, "node": "n2", "pool": "system", "cluster": "eu-south1"},
    ]
    assert instances.placed(attempts, "hub") is attempts
    assert attempts[0]["instance_type"] == "gpu-h100-sxm/1gpu-16vcpu-200gb" and attempts[0]["instance_exact"] is False
    assert "instance_type" not in attempts[1]
    assert attempts[2]["instance_type"] == "cpu-d3/8vcpu-32gb"  # the member's own cluster, not the operation's


# node_placement reads one node listing per region since api 0.11.124: tests/test_pagination.py


def test_endpoint_replicas_carry_the_instance(client, monkeypatch):
    c, fake = client
    _fleet(monkeypatch)
    monkeypatch.setattr(
        kube,
        "node_placement",
        lambda region, node: {
            "instance_type": "cpu-d3",
            "preset": "8vcpu-32gb",
            "node_group": "mk8snodegroup-e00cpu",
            "pool": "cpu-general",
        },
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="llm-example-predictor-00001-deployment-abc"),
        spec=SimpleNamespace(
            node_name="computeinstance-e00cpu1", node_selector={"serverless2.nebius/pool": "cpu-general"}
        ),
        status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(ready=True)], start_time=None),
    )
    monkeypatch.setattr(kube, "endpoint_pods", lambda name, ns, region="eu-north1", nodes=1: [pod])
    isvc = {"metadata": {"labels": {}}, "spec": {"predictor": {"maxReplicas": 2}}}
    monkeypatch.setattr(kube, "isvc", lambda name, ns, region="eu-north1": isvc)
    fake.custom[("models", "inferenceservices", "llm-example")] = isvc
    eps = {e["id"]: e for e in c.get("/v1/endpoints", headers=H).json()}
    rep = eps["llm-example"]["replicas"]
    assert len(rep) == 1 and rep[0]["ready"] is True and rep[0]["node"] == "computeinstance-e00cpu1"
    assert rep[0]["instance_type"] == "cpu-d3/8vcpu-32gb" and rep[0]["instance_exact"] is True
    assert rep[0]["console_url"].endswith("/node-groups/mk8snodegroup-e00cpu/nodes/computeinstance-e00cpu1")
    # a partial pod object (no spec): listed without placement, never an error
    monkeypatch.setattr(kube, "endpoint_pods", lambda name, ns, region="eu-north1", nodes=1: [SimpleNamespace()])
    assert c.get("/v1/endpoints", headers=H).json()


def test_fleet_info_links_pools_and_clusters_and_lists_the_cpu_pools(client, monkeypatch):
    c, _ = client
    _fleet(monkeypatch, tenant_system_flavor=False)
    out = c.get("/v1/fleet", headers=H).json()
    pool = next(p for p in out["pools"] if p["pool"] == "h100-spot-1x")
    assert pool["node_group_id"] == "mk8snodegroup-e00h100" and pool["console_url"].endswith(
        "/mk8s/mk8scluster-e00north/node-groups/mk8snodegroup-e00h100"
    )
    cpu = {(p["cluster"], p["pool"]): p for p in out["cpu_pools"]}
    assert cpu[("hub", "cpu-general")]["kind"] == "cpu" and cpu[("hub", "cpu-general")]["console_url"].endswith(
        "/node-groups/mk8snodegroup-e00cpu"
    )
    assert cpu[("hub", "system")]["kind"] == "system" and cpu[("hub", "system")]["console_url"] is None
    assert out["tenant_system_flavor"] is False


# ---- the fleet policy: tenants off the system nodes -------------------------------------------------------------


def test_tenant_pool_follows_the_policy(monkeypatch):
    _fleet(monkeypatch, tenant_system_flavor=True)
    assert kube.tenant_system_allowed() and kube.tenant_pool("hub", "system") == "system"
    _fleet(monkeypatch, tenant_system_flavor=False)
    assert not kube.tenant_system_allowed()
    assert kube.tenant_pool("hub", "system") == "cpu-general" and kube.cpu_pool_of("hub") == "cpu-general"
    assert kube.cpu_pool_order(CPU_POOLS["hub"]) == ["cpu-general", "cpu-batch-32"]  # size before name
    assert kube.cpu_pool_order(
        {
            "b": {"kind": "cpu", "preset": "8vcpu-32gb", "min_nodes": 0},
            "a": {"kind": "cpu", "preset": "8vcpu-32gb", "min_nodes": 1},
        }
    ) == ["a", "b"]  # the warm one first among equals
    assert kube.cpu_pool_order(
        {
            "z": {"kind": "cpu", "preset": "4vcpu-16gb", "min_nodes": 0},
            "a": {"kind": "cpu", "preset": "8vcpu-32gb", "min_nodes": 1},
        }
    ) == ["z", "a"]
    assert kube.preset_vcpus("16vcpu-64gb") == 16 and kube.preset_vcpus(None) == 0
    assert kube.tenant_pool("hub", "h100-spot-1x") == "h100-spot-1x"  # only `system` is redirected
    assert kube.tenant_pool("eu-south1", "system") == "system"  # no CPU pool there: kept, logged
    assert kube.is_cpu_pool("hub", "cpu-general") and not kube.is_cpu_pool("hub", "system")
    _fleet(monkeypatch, tenant_system_flavor=False, cpu_pools={})  # an old chart without cpu_pools.yaml
    assert kube.tenant_pool("hub", "system") == "system"
    monkeypatch.setattr(kube, "fleet", lambda: {})  # no fleet document at all: the default allows the system nodes
    assert kube.tenant_system_allowed() and kube.tenant_pool("hub", "system") == "system"


def test_cpu_only_definition_and_its_endpoint_take_the_customer_pool(client, admin_keys, monkeypatch):
    c, fake = client
    _fleet(monkeypatch, tenant_system_flavor=False)
    spec = {
        "id": "cpu-echo",
        "kind": "endpoint",
        "image": "x",
        "port": 80,
        "protocol": "http",
        "gpu": {"count": 0, "classes": []},
        "regions": ["eu-north1"],
    }
    entry = models.to_entry(spec)
    assert entry["deployments"]["hub"]["pool"] == "cpu-general"
    r = c.post("/v1/models", json=spec, headers=ADMIN)
    assert r.status_code in (200, 201), r.text
    rendered = json.dumps(fake.custom[("models", "inferenceservices", "cpu-echo")])
    assert '"serverless2.nebius/pool": "cpu-general"' in rendered and '"serverless2.nebius/cpu-pool"' in rendered
    assert '"nvidia.com/gpu"' not in rendered.split('"tolerations"')[1][:200]
    # a definition saved earlier with `system` baked in is re-pinned at render time, so no re-save is needed
    old = {**entry, "deployments": {"hub": {"pool": "system"}}}
    v = render_endpoint.values(old, "hub", "models.example", "ib")
    assert v["pool"] == "cpu-general" and v["tolerations"] == [dict(kube.CPU_POOL_TOLERATION)]
    _fleet(monkeypatch, tenant_system_flavor=True)
    v = render_endpoint.values(old, "hub", "models.example", "ib")
    assert v["pool"] == "system" and v["tolerations"][0]["key"] == "nvidia.com/gpu"


# ---- operation identity ------------------------------------------------------------------------------------------


def test_operation_id_is_the_one_invoke_creates_and_has_no_side_effect(client, monkeypatch):
    c, fake = client
    before = dict(fake.jobs)
    r = c.post("/v1/models/batch-example:operation-id", headers={**H, "Idempotency-Key": "ident-1"})
    assert r.status_code == 200, r.text
    ident = r.json()
    assert ident["model"] == "batch-example" and ident["mode"] == "run" and ident["owner_scope"] == "key"
    assert ident["id"].startswith("op-") and fake.jobs == before  # nothing created
    again = c.post(
        "/v1/models/batch-example:operation-id", json={"mode": "run"}, headers={**H, "Idempotency-Key": "ident-1"}
    )
    assert again.json()["id"] == ident["id"]
    other = c.post("/v1/models/batch-example:operation-id", headers={**H, "Idempotency-Key": "ident-2"}).json()["id"]
    assert other != ident["id"]
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    op = c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "ident-1"}).json()
    assert op["id"] == ident["id"]
    assert c.post("/v1/models/batch-example:operation-id", headers=H).status_code == 400  # no header: random at :invoke
    sync = c.post("/v1/models/batch-example:operation-id", json={"mode": "sync"}, headers={**H, "Idempotency-Key": "x"})
    assert sync.status_code in (400, 422)  # a synchronous call has no durable id (the request schema refuses it)
    assert c.post("/v1/models/nope:operation-id", headers={**H, "Idempotency-Key": "x"}).status_code == 404
    assert c.post("/v1/models/batch-example:operation-id", headers={"Idempotency-Key": "x"}).status_code in (401, 403)


def test_operation_id_follows_the_owner_namespace_of_an_imported_principal(client, monkeypatch):
    import auth

    c, _ = client
    from test_api import KEY_INFO

    plain = c.post("/v1/models/batch-example:operation-id", headers={**H, "Idempotency-Key": "p"}).json()

    async def principal_info(key):
        return {**KEY_INFO, "metadata": {**(KEY_INFO.get("metadata") or {}), "legacy": {"principal_id": "qa"}}}

    monkeypatch.setattr(auth, "key_info", principal_info)
    imported = c.post("/v1/models/batch-example:operation-id", headers={**H, "Idempotency-Key": "p"}).json()
    assert imported["owner_scope"] == "principal" and imported["id"] != plain["id"]
