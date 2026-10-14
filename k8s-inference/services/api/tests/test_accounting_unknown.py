"""Accounting annotations the platform writes as `unknown` (billing.py: a run without a trustworthy measurement, a
native call released without metering) must leave the operation readable: on api 0.11.78 `status.normalise` and the
chain view parsed `<LABEL>/gpu-seconds` and `<LABEL>/billed` with float() and a Cosmos native call's owner got 500 on
status and result. Unknown stays unknown (null, `usage: unknown`), never zero; malformed values are unknown too."""

import math

import pytest

import billing
import status
from test_api import L, admin_keys, client, cluster  # noqa: F401


def _job(**ann):
    return {
        "metadata": {
            "name": "op-489ec51ef5693265",
            "namespace": "tenant-demo",
            "labels": {f"{L}/mode": "sync", f"{L}/model": "cosmos-example", f"{L}/tenant": "demo"},
            "annotations": {f"{L}/key": billing.key_hash("sk-good"), **ann},
            "uid": "uid-op-489ec51ef5693265",
            "creationTimestamp": "2026-10-14T06:59:00Z",
        },
        "spec": {"template": {"spec": {"containers": [{"name": "main"}]}}},
        "status": {"conditions": [{"type": "Complete", "status": "True"}], "startTime": "2026-10-14T07:00:00Z"},
    }


@pytest.mark.parametrize(
    "value", ["unknown", "", "   ", "nan", "NaN", "inf", "-inf", "-1", "abc", "1e400", "1,5", None, [], {}]
)
def test_measured_treats_everything_but_a_finite_non_negative_number_as_unknown(value):
    assert status.measured(value) is None


def test_measured_reads_numbers_as_the_platform_writes_them():
    assert status.measured("42.0") == 42.0 and status.measured("0") == 0.0 and status.measured(" 7 ") == 7.0
    assert status.measured(3600.0) == 3600.0 and status.measured("1.6") == 1.6
    assert not math.isnan(status.measured("12.5"))


def test_native_settlement_without_measurement_keeps_the_operation_readable():
    # exactly what billing.py writes for a native call released with gpu_seconds null (docs/API.md leases)
    job = _job(**{f"{L}/billed": "unknown", f"{L}/gpu-seconds": "unknown", f"{L}/native": "released"})
    op = status.normalise(job, price_per_call=0.25)
    assert op["status"] == "SUCCEEDED"
    assert op["gpu_seconds"] is None and op["cost"] is None and op["usage"] == "unknown"  # unknown, never 0 / free
    # a run settled with a measurement
    op = status.normalise(_job(**{f"{L}/billed": "1.6", f"{L}/gpu-seconds": "3600.0"}))
    assert op["gpu_seconds"] == 3600.0 and op["cost"] == 1.6 and op["usage"] == "measured"
    # not settled yet: pending, and the per-call price applies to a succeeded sync call as before
    op = status.normalise(_job(), price_per_call=0.25)
    assert op["gpu_seconds"] is None and op["cost"] == 0.25 and op["usage"] == "pending"
    # malformed (an edited annotation): unknown, readable, never zero
    op = status.normalise(_job(**{f"{L}/billed": "n/a", f"{L}/gpu-seconds": "-3"}))
    assert op["gpu_seconds"] is None and op["cost"] is None and op["usage"] == "unknown"


def test_owner_status_and_result_answer_200_with_unknown_usage(client, admin_keys, monkeypatch):
    """The regression as the owner sees it: GET status and /result of a settled-unknown operation never answer 500."""
    import attempts

    c, cluster = client
    monkeypatch.setattr(attempts, "enabled", lambda: False)  # no ledger in the fake world: the Job is the record
    monkeypatch.setattr(attempts, "for_operation", lambda op: [])
    job = _job(**{f"{L}/billed": "unknown", f"{L}/gpu-seconds": "unknown", f"{L}/native": "released"})
    cluster.jobs[("tenant-demo", job["metadata"]["name"])] = job
    r = c.get(f"/v1/operations/{job['metadata']['name']}", headers={"Authorization": "Bearer sk-good"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage"] == "unknown" and body["gpu_seconds"] is None and body["cost"] is None
    r = c.get(f"/v1/operations/{job['metadata']['name']}/result", headers={"Authorization": "Bearer sk-good"})
    assert r.status_code != 500, r.text
