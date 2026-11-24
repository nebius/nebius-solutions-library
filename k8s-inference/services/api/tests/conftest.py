"""Initialize service imports and fixture configuration before collecting any API test module."""

import os
import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
ROOT = API_DIR.parents[1]
sys.path.insert(0, str(API_DIR))
os.environ.update(
    CATALOG_DIRS=f"{ROOT}/catalog/models:{API_DIR}/tests/catalog",
    PUBLIC_API_URL="https://api.example",
    REGION="eu-north1",
    ENDPOINT_DOMAIN="203.0.113.10.sslip.io",
)


@pytest.fixture(autouse=True)
def _marks_for_follows_a_stubbed_ledger(monkeypatch):
    """Every fake-ledger world stubs attempts._marks (and often successor_of/for_operation) but predates the bulk
    attempts.marks_for read; when _marks is a stub, marks_for answers from it instead of the real pool, so no test
    waits on a database it never configured. With the real _marks, the real bulk read stays in force."""
    import attempts

    real_bulk, real_one = attempts.marks_for, attempts._marks

    def bulk(operations):
        if attempts._marks is real_one:
            return real_bulk(operations)
        out = {}
        for op in operations:
            m = attempts._marks(op)
            if m:
                out[op] = tuple(m)
        return out

    monkeypatch.setattr(attempts, "marks_for", bulk)


@pytest.fixture(autouse=True)
def _fresh_listing_caches():
    """The listing caches (api 0.11.124: the tenant-wide Job listing, the fleet's in-flight Jobs, the fleet page's node
    and pod listings, the node index) never leak from one test's fake cluster into the next."""
    import jobs
    import kube

    jobs._ops_list_cache.clear()
    jobs._ops_list_wanted.clear()
    jobs._fleet_ops_cache.update({"until": 0.0, "items": [], "skipped": []})
    kube._live_cache.clear()
    kube._node_cache.clear()
    yield
    jobs._ops_list_cache.clear()
    jobs._ops_list_wanted.clear()
    jobs._fleet_ops_cache.update({"until": 0.0, "items": [], "skipped": []})
    kube._live_cache.clear()
    kube._node_cache.clear()
