"""Bounded retries and timeouts for Kubernetes and HTTP calls. Admission limits are not here: Kueue
bounds a tenant's concurrent runs (its ClusterQueue), LiteLLM `rpm_limit`/`max_budget` bound sync and
async calls per key, and the Envoy Gateway BackendTrafficPolicy on the API route caps request rates."""
import asyncio, random, time
from kubernetes.client.rest import ApiException
import urllib3

RETRY_STATUS = {429, 500, 502, 503, 504}
K8S_TIMEOUT = 15          # seconds per Kubernetes API call


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
        time.sleep(base * 3 ** i + random.random() * 0.1)


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
        await asyncio.sleep(base * 3 ** i + random.random() * 0.1)
