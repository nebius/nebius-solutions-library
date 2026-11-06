"""The admin listing of a tenant's keys (`GET /v1/keys`, api 0.11.104; docs/API.md "Keys") against PostgreSQL: the
same safe limit and usage fields the caller's own view (`GET /v1/keys/me`) carries (scopes, concurrency, budgets and
spend, rate limit, principal id), the run usage, and a retained blocked key shown as `blocked`, never `active`; never
the hash, the raw metadata or a secret. Skipped without TEST_DATABASE_URL."""

import copy

import auth
import db
import keys
from test_api import ADMIN_INFO, H, client, cluster  # noqa: F401
from test_database import LEGACY, database  # noqa: F401


def test_the_listing_carries_the_safe_limits_usage_and_blocked_state(database, client, monkeypatch):
    c, _ = client
    db.migrate()

    async def info(key):
        return copy.deepcopy(ADMIN_INFO)

    monkeypatch.setattr(auth, "key_info", info)  # the tenant's admin
    report = keys.import_records(
        [
            {
                **LEGACY,
                "hash": "d" * 64,
                "alias": "lab-1",
                "principal_id": "p-1",
                "scopes": ["invoke:sync", "run:submit"],
                "max_concurrent": 3,
                "budget_requests": 50,
                "budget_gpu_seconds": 7200,
                "rate_limit": {"requests": 50, "window_s": 86400},
            },
            {**LEGACY, "hash": "e" * 64, "alias": "lab-2", "principal_id": "p-2", "scopes": ["invoke:sync"]},
            {**LEGACY, "hash": "f" * 64, "alias": "lab-3", "principal_id": "p-3", "scopes": ["telepathy:read"]},
        ],
        "demo",
        "admin",
    )
    assert [x["alias"] for x in report["blocked_for_unknown_scope"]] == ["demo-lab-3"]  # retained, blocked
    assert keys.block("demo", "demo-lab-2")  # blocked by the admin
    keys.admit("d" * 64, "op-live-1", "demo", gpu_seconds=600.0, run_member=True)  # work in flight on lab-1
    r = c.get("/v1/keys", headers=H)
    assert r.status_code == 200, r.text
    by = {k["alias"]: k for k in r.json()}
    assert set(by) == {"demo-lab-1", "demo-lab-2", "demo-lab-3"}
    one = by["demo-lab-1"]
    assert one["id"] == "demo-lab-1" and one["status"] == "active" and one["blocked"] is False
    assert one["scopes"] == ["invoke:sync", "run:submit"] and one["max_concurrent"] == 3
    assert one["budget_requests"] == 50 and one["budget_gpu_seconds"] == 7200 and one["principal_id"] == "p-1"
    assert one["spent_requests"] == 0 and one["spent_gpu_seconds"] == 0
    assert one["rate_limit"] == {"requests": 50, "window_s": 86400}
    assert one["usage"]["reserved_gpu_seconds"] == 600 and one["usage"]["active_operations"] == 1
    # a retained blocked key is blocked, not active (an imported record with unknown scopes, or one the admin blocked)
    assert by["demo-lab-2"]["status"] == "blocked" and by["demo-lab-2"]["blocked"] is True
    assert by["demo-lab-3"]["status"] == "blocked" and by["demo-lab-3"]["blocked"] is True
    assert by["demo-lab-3"]["scopes"] == ["telepathy:read"] and by["demo-lab-3"]["principal_id"] == "p-3"  # as imported
    # never the hash, the raw metadata, the legacy blob or a secret
    for k in by.values():
        assert not ({"hash", "token", "metadata", "legacy", "key"} & set(k)), sorted(k)
        assert "d" * 64 not in str(k) and "e" * 64 not in str(k) and "f" * 64 not in str(k)
    # the caller's own view agrees on the shared fields
    me_info = {**ADMIN_INFO, "scopes": ["invoke:sync", "run:submit"], "max_concurrent": 3, "budget_requests": 50}

    async def info2(key):
        return copy.deepcopy(me_info)

    monkeypatch.setattr(auth, "key_info", info2)
    me = c.get("/v1/keys/me", headers=H).json()
    assert me["scopes"] == one["scopes"] and me["max_concurrent"] == 3 and me["budget_requests"] == 50
