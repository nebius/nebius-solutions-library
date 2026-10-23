"""Bounded retries and timeouts for Kubernetes and HTTP calls. Admission limits are not here: Kueue
bounds a tenant's concurrent runs (its ClusterQueue), LiteLLM `rpm_limit`/`max_budget` bound sync and
async calls per key, and the Envoy Gateway BackendTrafficPolicy on the API route caps request rates."""

import asyncio
import random
import time

import urllib3
from kubernetes.client.rest import ApiException

RETRY_STATUS = {429, 500, 502, 503, 504}
K8S_TIMEOUT = 15  # seconds per Kubernetes API call


def retry(fn, *args, attempts: int = 3, base: float = 0.2, **kw):
    """Call fn with a per-call timeout; retry transient failures with exponential backoff (0.2, 0.6 s)."""
    kw.setdefault("_request_timeout", K8S_TIMEOUT)
    for i in range(attempts):
        try:
            return fn(*args, **kw)
        except ApiException as e:
            if e.status not in RETRY_STATUS or i == attempts - 1:
                raise
        except (urllib3.exceptions.HTTPError, OSError):
            if i == attempts - 1:
                raise
        time.sleep(base * 3**i + random.random() * 0.1)


async def retry_http(send, attempts: int = 3, base: float = 0.2):
    """send() -> httpx.Response; retried on connection errors and 5xx/429 with backoff."""
    import httpx

    for i in range(attempts):
        try:
            r = await send()
            if r.status_code not in RETRY_STATUS or i == attempts - 1:
                return r
        except (httpx.TransportError, httpx.TimeoutException):
            if i == attempts - 1:
                raise
        await asyncio.sleep(base * 3**i + random.random() * 0.1)


# Shared small helpers (docs/ENGINEERING.md "A helper that exists already"): one copy, imported.


def iso(t) -> str | None:
    """A datetime (or an ISO string) as the API's RFC 3339 `...Z` text; None stays None."""
    if not t:
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(t, "strftime") else str(t)


def parse_ts(s):
    """An RFC 3339 timestamp (`...Z` from raw API objects, `...+00:00` from the client, or a datetime) as an
    aware datetime; None or an unparseable value gives None."""
    from datetime import datetime, timezone

    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def cached(store: dict, key, seconds: float, load):
    """`store[key]` holds `(until, value)`; return the value while it is fresh, else `load()` and remember it for
    `seconds` (`seconds <= 0` never caches, for the tests)."""
    now = time.time()
    hit = store.get(key) if seconds > 0 else None
    if hit and hit[0] > now:
        return hit[1]
    value = load()
    if seconds > 0:
        store[key] = (now + seconds, value)
    return value
