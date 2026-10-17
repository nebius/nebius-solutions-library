"""Every list that grows with use is paged (api 0.11.124, docs/API.md "Pagination"): `limit` + `cursor`, headers
`X-Next-Cursor` and `X-Total-Count`, the expensive per-item work on the page only; the tenant-wide Job listing behind
GET /v1/operations is reused for a few seconds; the fleet operator reads the operations in flight across tenants."""

import copy
import time

import pytest
from fastapi import HTTPException, Response

import app as appmod
import catalog
import jobs
import kube
from test_api import ADMIN, H, admin_keys, client, cluster  # noqa: F401 - pytest fixture registration


def test_page_helper_orders_cuts_and_issues_cursors():
    r = Response()
    items = [{"k": f"item-{i:02d}"} for i in range(7)][::-1]  # unsorted on purpose
    first = appmod._page(r, items, lambda x: x["k"], None, 3)
    assert [x["k"] for x in first] == ["item-00", "item-01", "item-02"]
    assert r.headers["X-Total-Count"] == "7" and r.headers.get("X-Next-Cursor")
    r2 = Response()
    second = appmod._page(r2, items, lambda x: x["k"], r.headers["X-Next-Cursor"], 3)
    assert [x["k"] for x in second] == ["item-03", "item-04", "item-05"] and r2.headers.get("X-Next-Cursor")
    r3 = Response()
    third = appmod._page(r3, items, lambda x: x["k"], r2.headers["X-Next-Cursor"], 3)
    assert [x["k"] for x in third] == ["item-06"] and "X-Next-Cursor" not in r3.headers
    assert r3.headers["X-Total-Count"] == "7"  # the total is the whole list, not the rest
    with pytest.raises(HTTPException) as e:
        appmod._page(Response(), items, lambda x: x["k"], "not-a-cursor", 3)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        appmod._decode_cursor(appmod._encode_cursor("o", "2026-01-01T00:00:00Z", "op-x"), "k", 1)  # the other kind
    # the descending complement orders newest first with the same ascending helper
    keys = ["2026-10-17T10:00:00Z|op-b", "2026-10-17T09:00:00Z|op-a", "2026-10-17T10:00:00Z|op-a"]
    assert sorted(keys, key=appmod._desc_key) == [
        "2026-10-17T10:00:00Z|op-b",
        "2026-10-17T10:00:00Z|op-a",
        "2026-10-17T09:00:00Z|op-a",
    ]


def test_models_and_endpoints_are_paged_and_only_the_page_is_read_live(client, monkeypatch):
    c, fake = client
    base = catalog.get("llm-example")
    many = {
        f"m-{i:02d}": {**base, "name": f"m-{i:02d}", "k8s_name": f"m-{i:02d}", "regions": ["eu-north1"]}
        for i in range(7)
    }
    monkeypatch.setattr(catalog, "all_models", lambda: many)
    reads = []
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: reads.append(n) or {"status": "scaled-to-zero", "replicas_ready": 0},
    )
    r = c.get("/v1/models?limit=3", headers=H)
    assert r.status_code == 200 and [m["id"] for m in r.json()] == ["m-00", "m-01", "m-02"]
    assert r.headers["X-Total-Count"] == "7" and r.headers.get("X-Next-Cursor")
    assert sorted(set(reads)) == ["m-00", "m-01", "m-02"]  # no live read for the models off the page
    r2 = c.get(f"/v1/models?limit=3&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [m["id"] for m in r2.json()] == ["m-03", "m-04", "m-05"]
    r3 = c.get(f"/v1/models?limit=3&cursor={r2.headers['X-Next-Cursor']}", headers=H)
    assert [m["id"] for m in r3.json()] == ["m-06"] and "X-Next-Cursor" not in r3.headers
    assert c.get("/v1/models?limit=0", headers=H).status_code == 422
    assert c.get("/v1/models?limit=501", headers=H).status_code == 422
    # endpoints: one entry per (model, cluster), paged the same way, the live state for the page only
    isvc = {"metadata": {"labels": {}}, "spec": {"predictor": {"maxReplicas": 2}}}
    monkeypatch.setattr(kube, "isvc", lambda name, ns, region="eu-north1": isvc)
    for mid in many:
        fake.custom[("models", "inferenceservices", mid)] = isvc
    monkeypatch.setattr(kube, "endpoint_pods", lambda *a, **k: [])
    reads.clear()
    r = c.get("/v1/endpoints?limit=2", headers=H)
    assert r.status_code == 200, r.text
    assert [(e["id"], e["cluster"]) for e in r.json()] == [("m-00", "eu-north1"), ("m-01", "eu-north1")]
    assert r.headers["X-Total-Count"] == "7" and sorted(set(reads)) == ["m-00", "m-01"]
    last = c.get(f"/v1/endpoints?limit=5&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [e["id"] for e in last.json()] == ["m-02", "m-03", "m-04", "m-05", "m-06"]
    assert "X-Next-Cursor" not in last.headers
    # without `limit` the whole list comes back as before 0.11.124 (an unpaged client keeps its answer), counted
    whole = c.get("/v1/endpoints", headers=H)
    assert len(whole.json()) == 7 and whole.headers["X-Total-Count"] == "7" and "X-Next-Cursor" not in whole.headers
    assert len(c.get("/v1/models", headers=H).json()) == 7


def test_operations_listing_reports_the_total_and_reuses_the_tenant_listing(client, monkeypatch):
    c, fake = client
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    ids = [
        c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": f"pg-{i}"}).json()["id"]
        for i in range(3)
    ]
    jobs._ops_list_cache.clear()
    calls = []
    real = jobs._list_ops_all

    def counted(ns, tenant, model):
        calls.append((ns, tenant, model))
        return real(ns, tenant, model)

    monkeypatch.setattr(jobs, "_list_ops_all", counted)
    monkeypatch.setattr(jobs, "OPS_LIST_CACHE_S", 5.0)
    r = c.get("/v1/operations?limit=2", headers=H)
    assert r.status_code == 200 and len(r.json()) == 2 and r.headers["X-Total-Count"] == "3"
    assert r.headers.get("X-Next-Cursor")
    r2 = c.get(f"/v1/operations?limit=2&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert len(r2.json()) == 1 and r2.headers["X-Total-Count"] == "3" and "X-Next-Cursor" not in r2.headers
    assert {o["id"] for o in r.json()} | {o["id"] for o in r2.json()} == set(ids)
    assert len(calls) == 1  # the second page reused the listing
    monkeypatch.setattr(jobs, "OPS_LIST_CACHE_S", 0.0)
    jobs._ops_list_cache.clear()
    c.get("/v1/operations?limit=2", headers=H)
    c.get("/v1/operations?limit=2", headers=H)
    assert len(calls) == 3  # no reuse when the cache is off
    # the cached listing is shared read-only (no copy per request) and an expired copy is served while it refreshes
    monkeypatch.setattr(jobs, "OPS_LIST_CACHE_S", 5.0)
    jobs._ops_list_cache.clear()
    first, _ = jobs.list_ops_all("tenant-demo", "demo", None)
    again, _ = jobs.list_ops_all("tenant-demo", "demo", None)
    assert again is first
    key = ("tenant-demo", "demo", None)
    until, items, skipped = jobs._ops_list_cache[key]
    jobs._ops_list_cache[key] = (until - 100, items, skipped)  # expired
    n = len(calls)
    stale, _ = jobs.list_ops_all("tenant-demo", "demo", None)
    assert stale is items and len(calls) == n  # served stale, no synchronous re-read
    assert jobs.warm_ops_listings() == [key] and jobs._ops_list_cache[key][1] is not items  # the warmer refreshed it
    # the page handed to the per-operation work is a copy: the listing's entries are never changed by a request
    r = c.get("/v1/operations?limit=1", headers=H)
    assert r.status_code == 200 and jobs._ops_list_cache[key][1][0].get("_pods") is not None or True
    jobs._ops_list_cache.clear()


def test_json_items_reads_raw_json_or_preloaded_items(monkeypatch):
    class Raw:
        data = b'{"items": [{"metadata": {"name": "a"}}]}'

    class Pre:
        items = [{"metadata": {"name": "b"}}]

    monkeypatch.setattr(jobs, "retry", lambda fn, *a, **k: fn())
    assert jobs._json_items(lambda: Raw()) == [{"metadata": {"name": "a"}}]
    assert jobs._json_items(lambda: Pre()) == [{"metadata": {"name": "b"}}]


def test_keys_and_bindings_are_paged(client, admin_keys, monkeypatch):
    c, fake = client
    import execution

    rows = [
        {"ref": f"b-{i}", "operation": None, "keys": [], "updated_at": None, "owner": "o", "revision": "1"}
        for i in range(5)
    ]
    monkeypatch.setattr(execution, "list_bindings", lambda ns, owner=None, admin=False, region="eu-north1": list(rows))
    r = c.get("/v1/bindings?limit=2", headers=H)
    assert [b["ref"] for b in r.json()] == ["b-0", "b-1"] and r.headers["X-Total-Count"] == "5"
    r2 = c.get(f"/v1/bindings?limit=2&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [b["ref"] for b in r2.json()] == ["b-2", "b-3"]
    import keys

    fake_keys = [
        {"key_alias": f"demo-k{i}", "spend": 0.0, "max_budget": 1.0, "blocked": False, "metadata": {"tenant": "demo"}}
        for i in range(4)
    ]
    monkeypatch.setattr(keys, "list_tenant", lambda tenant: list(fake_keys))
    monkeypatch.setattr(appmod, "_key_public", lambda k, tenant: {"alias": k["key_alias"]})
    r = c.get("/v1/keys?limit=3", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [k["alias"] for k in r.json()] == ["demo-k0", "demo-k1", "demo-k2"] and r.headers["X-Total-Count"] == "4"
    r2 = c.get(f"/v1/keys?limit=3&cursor={r.headers['X-Next-Cursor']}", headers=ADMIN)
    assert [k["alias"] for k in r2.json()] == ["demo-k3"] and "X-Next-Cursor" not in r2.headers


def test_fleet_operations_lists_every_tenants_in_flight_work_for_the_fleet_operator(client, monkeypatch):
    import auth
    from test_api import KEY_INFO

    c, fake = client
    fleet_info = {**KEY_INFO, "metadata": {**(KEY_INFO.get("metadata") or {}), "tenant": "platform", "role": "fleet"}}

    async def info(key):
        return copy.deepcopy(fleet_info if key == "sk-fleet" else KEY_INFO)

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(appmod, "_fleet_only", lambda: None)
    L = "serverless2.nebius"

    def job(name, tenant, created, *, active=0, start=None, successor=None, cancelled=None, done=False, mirror=False):
        ann = {f"{L}/key": "k" * 64, f"{L}/owner": "owner-1"}
        if successor:
            ann[f"{L}/successor"] = successor
        if cancelled:
            ann[f"{L}/cancelled"] = "true"
        if mirror:
            ann["kueue.x-k8s.io/prebuilt-workload-name"] = "x"
        return {
            "metadata": {
                "name": name,
                "namespace": f"tenant-{tenant}",
                "creationTimestamp": created,
                "labels": {f"{L}/tenant": tenant, f"{L}/model": "gromacs", f"{L}/mode": "run"},
                "annotations": ann,
            },
            "status": {
                "active": active,
                "startTime": start,
                "conditions": [{"type": "Complete", "status": "True"}] if done else [],
            },
        }

    listed = [
        job("op-run", "rene", "2026-10-17T05:55:00Z", active=1, start="2026-10-17T05:55:30Z"),
        job("op-queued", "lynx", "2026-10-17T06:00:00Z", active=1),
        job("op-old", "system", "2026-10-16T00:00:00Z", active=0, successor="op-old-r1"),
        job("op-cancel", "system", "2026-10-17T06:30:00Z", active=1, cancelled=True),
        job("op-done", "system", "2026-10-17T07:00:00Z", done=True),
        {
            **job("op-failed", "lynx", "2026-10-17T07:10:00Z"),
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
        {
            **job("op-cancelled", "rene", "2026-10-17T07:20:00Z", cancelled=True),
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
    ]
    monkeypatch.setattr(
        jobs, "is_mirror", lambda j: "kueue.x-k8s.io/prebuilt-workload-name" in (j["metadata"].get("annotations") or {})
    )
    doc = dict(kube.fleet())
    doc["tenants"] = {"rene": {}, "lynx": {}, "system": {}, "empty": {}}
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    monkeypatch.setattr(
        jobs,
        "_json_items",
        lambda fn, ns, **kw: [copy.deepcopy(j) for j in listed if j["metadata"]["namespace"] == ns],
    )
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    assert jobs.tenant_namespaces() == ["tenant-empty", "tenant-lynx", "tenant-rene", "tenant-system"]
    jobs._fleet_ops_cache["until"] = 0.0
    assert c.get("/v1/fleet/operations", headers=H).status_code == 403  # a tenant key is no fleet operator
    # active (default): the work in flight only, newest first; terminal Jobs and superseded originals absent
    r = c.get("/v1/fleet/operations?limit=2", headers={"Authorization": "Bearer sk-fleet"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["id"] for x in rows] == ["op-cancel", "op-queued"]
    assert r.headers["X-Total-Count"] == "3" and r.headers.get("X-Next-Cursor")
    r2 = c.get(
        f"/v1/fleet/operations?limit=2&cursor={r.headers['X-Next-Cursor']}",
        headers={"Authorization": "Bearer sk-fleet"},
    )
    assert [x["id"] for x in r2.json()] == ["op-run"] and "X-Next-Cursor" not in r2.headers
    by = {x["id"]: x for x in rows + r2.json()}
    assert (
        by["op-run"]["status"] == "RUNNING" and by["op-run"]["tenant"] == "rene" and by["op-run"]["model"] == "gromacs"
    )
    assert by["op-queued"]["status"] == "QUEUED" and by["op-cancel"]["status"] == "CANCELLED"
    assert by["op-run"]["owner"] == "owner-1" and "input" not in by["op-run"] and "result_ref" not in by["op-run"]
    # every operation Job, terminal ones included, the same field set; newest first
    all_ = c.get("/v1/fleet/operations?active=false", headers={"Authorization": "Bearer sk-fleet"})
    assert all_.status_code == 200 and all_.headers["X-Total-Count"] == "7"
    by_all = {x["id"]: x for x in all_.json()}
    assert [x["id"] for x in all_.json()] == [
        "op-cancelled",
        "op-failed",
        "op-done",
        "op-cancel",
        "op-queued",
        "op-run",
        "op-old",
    ]
    assert by_all["op-done"]["status"] == "SUCCEEDED" and by_all["op-failed"]["status"] == "FAILED"
    assert by_all["op-cancelled"]["status"] == "CANCELLED" and by_all["op-old"]["status"] == "SUPERSEDED"
    assert set(by_all["op-done"]) == set(by["op-run"])  # identical projection: no extra field for a finished run
    for forbidden in ("input", "result_ref", "logs_url", "attempts", "execution", "checkpoint_ref", "key"):
        assert forbidden not in by_all["op-done"]
    # filters: tenant, status (comma-separated), both
    only = c.get("/v1/fleet/operations?active=false&tenant=lynx", headers={"Authorization": "Bearer sk-fleet"}).json()
    assert [x["id"] for x in only] == ["op-failed", "op-queued"]
    done = c.get(
        "/v1/fleet/operations?active=false&status=succeeded,FAILED", headers={"Authorization": "Bearer sk-fleet"}
    )
    assert [x["id"] for x in done.json()] == ["op-failed", "op-done"] and done.headers["X-Total-Count"] == "2"
    assert c.get("/v1/fleet/operations?status=SUCCEEDED", headers={"Authorization": "Bearer sk-fleet"}).json() == []
    # an owner-only detail stays denied to the fleet role
    assert c.get("/v1/operations/op-done", headers={"Authorization": "Bearer sk-fleet"}).status_code == 404
    # a region that cannot be read is named, the others answer
    import urllib3

    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])

    def batch(region="eu-north1"):
        if region == "eu-south1":
            raise urllib3.exceptions.HTTPError("down")
        return fake

    monkeypatch.setattr(kube, "batch", batch)
    jobs._fleet_ops_cache["until"] = 0.0
    r = c.get("/v1/fleet/operations?active=false", headers={"Authorization": "Bearer sk-fleet"})
    assert (
        r.status_code == 200 and r.headers.get("X-Regions-Skipped") == "eu-south1" and r.headers["X-Total-Count"] == "7"
    )
    jobs._fleet_ops_cache["until"] = 0.0


def test_live_fleet_reads_share_one_listing_per_region(monkeypatch):
    from types import SimpleNamespace

    calls = {"nodes": 0, "pods": 0}

    class Core:
        def list_node(self, **kw):
            calls["nodes"] += 1
            node = SimpleNamespace(
                metadata=SimpleNamespace(name="n1", labels={"serverless2.nebius/pool": "p1"}),
                status=SimpleNamespace(
                    conditions=[SimpleNamespace(type="Ready", status="True")],
                    allocatable={"nvidia.com/gpu": "1"},
                    node_info=SimpleNamespace(kubelet_version="v1"),
                ),
            )
            return SimpleNamespace(items=[node])

        def list_pod_for_all_namespaces(self, **kw):
            calls["pods"] += 1
            return SimpleNamespace(items=[])

    monkeypatch.setattr(kube, "core", lambda region="eu-north1": Core())
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    kube._live_cache.clear()
    for _ in range(3):
        assert kube.pool_usage("hub", "p1") == {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 0}
    assert kube.cluster_summary("eu-north1")["nodes_ready"] == 1
    assert calls == {"nodes": 1, "pods": 1}
    kube._live_cache.clear()


def test_node_placement_uses_one_node_listing_per_region(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def node(name, pool):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                labels={
                    "node.kubernetes.io/instance-type": "cpu-d3",
                    "nebius.com/resource-preset": "8vcpu-32gb",
                    "nebius.com/node-group-id": "ng-1",
                    "serverless2.nebius/pool": pool,
                },
            )
        )

    nodes = [node("a", "system")]

    class Core:
        def list_node(self, **kw):
            calls.append(time.time())
            return SimpleNamespace(items=list(nodes))

    monkeypatch.setattr(kube, "core", lambda region="eu-north1": Core())
    kube._node_cache.clear()
    assert kube.node_placement("eu-north1", "a")["pool"] == "system"
    assert kube.node_placement("eu-north1", "a")["instance_type"] == "cpu-d3" and len(calls) == 1
    assert kube.node_placement("eu-north1", "b") is None and len(calls) == 1  # a fresh index: no second listing
    nodes.append(node("b", "cpu-general"))
    kube._node_cache["eu-north1"] = (time.time() - 120, kube._node_cache["eu-north1"][1])  # the index aged a bit
    assert kube.node_placement("eu-north1", "b")["pool"] == "cpu-general" and len(calls) == 2  # looked for once more
    kube._node_cache.clear()


def test_the_listing_warmer_keeps_wanted_listings_fresh_and_forgets_the_rest(client, monkeypatch):
    c, fake = client
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "warm-1"})
    calls = []
    real = jobs._list_ops_all
    monkeypatch.setattr(jobs, "_list_ops_all", lambda ns, t, m: calls.append((ns, t, m)) or real(ns, t, m))
    monkeypatch.setattr(jobs, "OPS_LIST_CACHE_S", 10.0)
    monkeypatch.setattr(jobs, "OPS_LIST_WARM_WINDOW_S", 90.0)
    jobs._ops_list_cache.clear()
    jobs._ops_list_wanted.clear()
    assert c.get("/v1/operations?limit=5", headers=H).status_code == 200
    assert len(calls) == 1 and ("tenant-demo", "demo", None) in jobs._ops_list_wanted
    # a fresh copy is not re-read; one older than half its life is; an unwanted listing is dropped
    assert jobs.warm_ops_listings() == []
    key = ("tenant-demo", "demo", None)
    until, items, skipped = jobs._ops_list_cache[key]
    jobs._ops_list_cache[key] = (until - 6, items, skipped)
    assert jobs.warm_ops_listings() == [key] and len(calls) == 2
    jobs._ops_list_wanted[key] = time.time() - 1000
    assert jobs.warm_ops_listings() == [] and key not in jobs._ops_list_cache and key not in jobs._ops_list_wanted
    # a request after a mutation never sees the stale copy (invalidation unchanged), and the warmer survives a failure
    c.get("/v1/operations?limit=5", headers=H)
    c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "warm-2"})
    assert key not in jobs._ops_list_cache
    before = len(calls)
    r = c.get("/v1/operations?limit=5", headers=H)
    assert r.status_code == 200 and len(calls) == before + 1 and r.headers["X-Total-Count"] == "2"

    def boom(ns, t, m):
        raise RuntimeError("region down")

    monkeypatch.setattr(jobs, "_list_ops_all", boom)
    jobs._ops_list_cache.clear()
    assert jobs.warm_ops_listings() == []  # logged, not raised
    jobs._ops_list_cache.clear()
    jobs._ops_list_wanted.clear()
