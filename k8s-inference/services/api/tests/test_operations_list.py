"""GET /v1/operations (docs/API.md): authorization before any cut (the tenant's live Jobs are no longer truncated to
500 before the owner filter), expired operations listed from their durable attempt records, a stable order with
bounded cursor paging (array response unchanged), the key's grant fences, and partial region visibility."""

import copy
import json

from fastapi import HTTPException

import attempts
import auth
import db
import keys
import kube
from test_api import KEY_INFO, FakeCluster, client, cluster  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

NS = "tenant-demo"
P1A, P1B, P2 = {"Authorization": "Bearer sk-p1a"}, {"Authorization": "Bearer sk-p1b"}, {"Authorization": "Bearer sk-p2"}


def _principal(owner, **extra):
    return {**KEY_INFO, "max_budget": None, "metadata": {"tenant": "demo", "legacy": {"principal_id": owner}}, **extra}


def _rig(database, client, monkeypatch, values=None):
    c, fake = client
    db.migrate()
    values = values or {"sk-p1a": _principal("p1"), "sk-p1b": _principal("p1"), "sk-p2": _principal("p2")}

    async def info(key):
        if key not in values:
            raise HTTPException(401, "invalid API key")
        return copy.deepcopy(values[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": keys.key_hash(k),
                "alias": k,
                "max_concurrent": None,
                "budget_requests": None,
                "budget_gpu_seconds": None,
                "rate_limit": None,
                "scopes": ["invoke:async", "run:submit", "run:control", "read:operations", "read:results"],
            }
            for k in values
        ],
        "demo",
        "admin",
    )
    return c, fake


def _invoke(c, headers, n=1):
    out = []
    for _ in range(n):
        r = c.post("/v1/models/hello-run:invoke", headers=headers, json={"input": {}})
        assert r.status_code == 202, r.text
        out.append(r.json()["id"])
    return out


def _clone(fake, src, name, created):
    j = json.loads(json.dumps(fake.jobs[(NS, src)]))
    j["metadata"]["name"] = name
    j["metadata"]["creationTimestamp"] = created
    j["metadata"]["uid"] = f"uid-{name}"
    fake.jobs[(NS, name)] = j


def _ids(r):
    assert r.status_code == 200, r.text
    return [o["id"] for o in r.json()]


def _all_pages(c, headers, limit, **q):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": limit, **q, **({"cursor": cursor} if cursor else {})}
        r = c.get("/v1/operations", headers=headers, params=params)
        ids = _ids(r)
        seen += ids
        pages += 1
        cursor = r.headers.get("x-next-cursor")
        if not cursor:
            return seen, pages
        assert len(ids) == limit  # a full page whenever another follows


def _expire(c, fake, op, started, ended):
    """The Job expired (TTL) after it ran: only its attempt records remain (what the uploader posted)."""
    rec = {"operation": op, "root": op, "role": "run", "cluster": "hub", "mode": "run", "model": "hello-run"}
    attempts.record("eu-north1", NS, f"{op}-abc", {**rec, "status": "started", "started_at": started})
    attempts.record(
        "eu-north1",
        NS,
        f"{op}-abc",
        {**rec, "status": "succeeded", "started_at": started, "ended_at": ended, "export": "succeeded", "exit_code": 0},
    )
    del fake.jobs[(NS, op)]


def test_own_operations_are_listed_behind_more_than_500_foreign_ones(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    foreign = _invoke(c, P2)[0]
    for i in range(600):  # 600 of p2's, all newer than anything p1 will create
        _clone(fake, foreign, f"op-hello-run-f{i:04d}", f"2026-10-14T13:{i // 60:02d}:{i % 60:02d}Z")
    mine = _invoke(c, P1A, 3)
    assert _ids(c.get("/v1/operations", headers=P1A)) == sorted(
        mine
    )  # equal (fake) timestamps: id ascending  # all three, none of p2's
    assert _ids(c.get("/v1/operations", headers=P1B)) == sorted(
        mine
    )  # equal (fake) timestamps: id ascending  # the same principal, another key
    r = c.get("/v1/operations", headers=P2, params={"limit": 500})
    assert len(_ids(r)) == 500 and r.headers.get("x-next-cursor")
    seen, pages = _all_pages(c, P2, 500)
    assert len(seen) == 601 and len(set(seen)) == 601 and pages == 2 and not (set(seen) & set(mine))
    assert seen[:600] == sorted(seen[:600], reverse=True)  # newest first by created_at, then id


def test_expired_operations_are_listed_from_their_durable_records(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    live = _invoke(c, P1A)[0]
    gone = _invoke(c, P1A)[0]
    _expire(c, fake, gone, "2026-10-14T09:00:00Z", "2026-10-14T09:30:00Z")
    r = c.get("/v1/operations", headers=P1A)
    by_id = {o["id"]: o for o in r.json()}
    assert set(by_id) == {live, gone}, r.text
    assert (
        by_id[gone]["status"] == "SUCCEEDED"
        and "expired" in by_id[gone]["reason"]
        and by_id[gone]["model"] == "hello-run"
    )
    assert (
        by_id[gone]["attempts"][0]["attempt_id"] == f"{gone}-abc"
        and by_id[gone]["created_at"] == "2026-10-14T09:00:00Z"
    )
    assert by_id[live]["status"] in ("QUEUED", "RUNNING")
    assert gone not in _ids(c.get("/v1/operations", headers=P2))  # another principal: not its record
    # newest first by created_at: the expired record's first start (today) precedes the fake's fixed live timestamp
    assert _ids(c.get("/v1/operations", headers=P1A, params={"model": "hello-run"})) == [gone, live]
    assert _ids(c.get("/v1/operations", headers=P1A, params={"model": "other-model"})) == []
    # the same operation through GET /v1/operations/{id} is the same durable view
    assert c.get(f"/v1/operations/{gone}", headers=P1A).json()["status"] == "SUCCEEDED"


def test_paging_is_stable_across_live_and_expired_operations(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    ops = _invoke(c, P1A, 4)
    _expire(c, fake, ops[1], "2026-10-13T08:00:00Z", "2026-10-13T08:10:00Z")
    _expire(c, fake, ops[3], "2026-10-13T10:00:00Z", "2026-10-13T10:10:00Z")
    fake.jobs[(NS, ops[0])]["metadata"]["creationTimestamp"] = "2026-10-13T09:00:00Z"
    fake.jobs[(NS, ops[2])]["metadata"]["creationTimestamp"] = "2026-10-13T11:00:00Z"
    seen, pages = _all_pages(c, P1A, 2)
    assert seen == [ops[2], ops[3], ops[0], ops[1]] and pages == 2  # newest first, live and expired interleaved
    seen1, _ = _all_pages(c, P1A, 1)
    assert seen1 == seen
    assert _ids(c.get("/v1/operations", headers=P1A, params={"limit": 500})) == seen
    assert c.get("/v1/operations", headers=P1A, params={"cursor": "not-a-cursor"}).status_code == 400
    assert c.get("/v1/operations", headers=P1A, params={"limit": 501}).status_code == 422


def test_grant_and_revocation_fences_apply_to_live_and_expired_operations(database, client, monkeypatch):
    values = {"sk-p1a": _principal("p1"), "sk-p1b": _principal("p1", models=["other-model"]), "sk-p2": _principal("p2")}
    c, fake = _rig(database, client, monkeypatch, values)
    live, gone = _invoke(c, P1A, 2)
    _expire(c, fake, gone, "2026-10-14T09:00:00Z", "2026-10-14T09:30:00Z")
    assert set(_ids(c.get("/v1/operations", headers=P1A))) == {live, gone}
    assert _ids(c.get("/v1/operations", headers=P1B)) == []  # the same principal, a key not granted the model
    assert _ids(c.get("/v1/operations", headers=P2)) == []  # another principal
    values.pop("sk-p1a")  # revoked: refused before any listing
    assert c.get("/v1/operations", headers=P1A).status_code == 401
    # a key of the same principal without the scope: refused before any listing
    values["sk-p1c"] = _principal("p1", scopes=["invoke:async"])
    assert c.get("/v1/operations", headers={"Authorization": "Bearer sk-p1c"}).status_code == 403


def test_an_unreachable_region_is_reported_and_never_hides_the_others(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    mine = _invoke(c, P1A, 2)
    south = FakeCluster()

    def down(*a, **k):
        raise OSError("eu-south1: connection refused")

    south.list_namespaced_job = down
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "batch", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": {"eu-north1": fake, "eu-south1": south}[region])
    r = c.get("/v1/operations", headers=P1A)
    assert r.status_code == 200, r.text
    assert _ids(r) == sorted(mine)  # equal (fake) timestamps: id ascending; both of p1's, from the readable region
    assert r.headers.get("x-regions-skipped") == "eu-south1"  # the unreachable region is named, never fatal
    assert "x-records-skipped" not in r.headers and "x-next-cursor" not in r.headers
    # the region back: nothing skipped
    south.list_namespaced_job = FakeCluster.list_namespaced_job.__get__(south, FakeCluster)
    r = c.get("/v1/operations", headers=P1A)
    assert _ids(r) == sorted(mine) and "x-regions-skipped" not in r.headers
