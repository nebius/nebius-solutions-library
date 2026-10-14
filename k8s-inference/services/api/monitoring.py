"""Resource-scoped monitoring. Queries are built here, never supplied by a browser.

The caller resolves/authorizes the resource. Prometheus and Loki of this cluster are called directly; those of
another region through that cluster's API server proxy with the api-agent kubeconfig (services/api/kube.py), so
no monitoring service of any region is exposed outside its cluster. Missing series stay missing (idle and
unavailable are different from a measured zero).
"""

import asyncio
import json
import math
import re
import time
import urllib.parse

import httpx
from fastapi import HTTPException

import kube
from config import LOKI_URL, PROMETHEUS_URL, REGION

RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


def window(period: str, end: float | None = None) -> tuple[float, float, int]:
    if period not in RANGES:
        raise HTTPException(422, "range must be 15m, 1h, 6h, 24h or 7d")
    stop = time.time() if end is None else end
    if not math.isfinite(stop) or stop > time.time() + 60 or stop < 0:
        raise HTTPException(422, "invalid end timestamp")
    return max(0, stop - RANGES[period]), stop, max(15, RANGES[period] // 240)


def target(service_url: str, region: str) -> tuple[str, dict, str | bool]:
    """(base url, headers, verify) for a monitoring service of a region: the in-cluster URL here; in another
    region the API server proxy of that cluster (`/api/v1/namespaces/<ns>/services/<name>:<port>/proxy`), with the
    bearer token and CA of its api-agent kubeconfig. 410 for a region that left the fleet (its cluster, Prometheus
    and Loki with it: the record stays, the metrics and logs are gone; attempt records and outputs remain)."""
    if region == REGION:
        return service_url, {}, True
    if region not in kube.regions():
        raise HTTPException(
            410,
            f"Region {region} is no longer part of this fleet: the metrics and logs of this operation lived "
            f"on its cluster and are gone. Its attempt records and outputs in the bucket are still there (result).",
        )
    u = urllib.parse.urlsplit(service_url)
    name, ns = u.hostname.split(".")[:2]  # <name>.<namespace>.svc[.cluster.local]
    api = kube._client(region)
    headers: dict = {}
    api.update_params_for_auth(headers, [], ["BearerToken"])  # kubernetes 36: (headers, queries, auth_settings)
    conf = api.configuration
    return (
        f"{conf.host.rstrip('/')}/api/v1/namespaces/{ns}/services/{name}:{u.port}/proxy",
        headers,
        conf.ssl_ca_cert or conf.verify_ssl,
    )


def selectors(labels: dict[str, str], regex: set[str] | None = None) -> str:
    return (
        "{" + ",".join(f"{k}{'=~' if k in (regex or set()) else '='}{json.dumps(v)}" for k, v in labels.items()) + "}"
    )


def queries(kind: str, namespace: str, name: str, step: int = 15) -> list[tuple[str, str, str, str]]:
    # RE2-escaped identity, not a user-provided regex. Kubernetes prefixes cover all revisions/attempts.
    pods = selectors(
        {"namespace": namespace, "pod": re.escape(name) + ("-predictor.*" if kind == "endpoint" else "-.*")}, {"pod"}
    )
    containers = pods[:-1] + ',container!="",container!="POD"}'
    w = f"{max(120, 2 * step)}s"  # the rate window follows the step: a coarse window still catches a short-lived pod
    q = [
        ("cpu", "CPU usage", "cores", f"sum(rate(container_cpu_usage_seconds_total{containers}[{w}]))"),
        ("memory", "Memory usage", "bytes", f"sum(container_memory_working_set_bytes{containers})"),
        ("gpu", "GPU utilization", "%", f"avg(DCGM_FI_DEV_GPU_UTIL{pods})"),
        ("gpu_memory", "GPU memory", "bytes", f"sum(DCGM_FI_DEV_FB_USED{pods}) * 1048576"),
    ]
    if kind == "endpoint":
        service = selectors({"namespace": namespace, "inferenceservice": name})
        knative = selectors({"k8s_namespace_name": namespace, "kn_service_name": name + "-predictor"})
        q = [
            (
                "requests",
                "Request rate",
                "req/s",
                f"sum(rate(http_server_request_duration_seconds_count{service}[{w}]))",
            ),
            (
                "latency",
                "Response latency · p95",
                "ms",
                f"histogram_quantile(0.95, sum by(le) (rate(http_server_request_duration_seconds_bucket{service}[{w}]))) * 1000",
            ),
            (
                "errors",
                "Server errors",
                "req/s",
                f'sum(rate(http_server_request_duration_seconds_count{service[:-1]},http_response_status_code=~"5.."}}[{w}]))',
            ),
            ("replicas", "Ready replicas", "replicas", f"sum(kn_revision_pods_count{knative})"),
            ("concurrency", "Concurrent requests", "requests", f"sum(kn_revision_concurrency_stable{knative})"),
        ] + q
    return q


async def _query(client: httpx.AsyncClient, url: str, params: dict, headers: dict | None = None) -> dict:
    try:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            raise ValueError("query failed")
        return data.get("data") or {}
    except (httpx.HTTPError, ValueError, KeyError):
        # No upstream URLs, raw error bodies, or credentials in customer errors.
        raise HTTPException(503, "Monitoring is temporarily unavailable. Try again shortly.")


async def metrics(kind: str, namespace: str, name: str, region: str, period: str, end: float | None = None) -> dict:
    start, stop, step = window(period, end)
    definitions = queries(kind, namespace, name, step)
    base, headers, verify = target(PROMETHEUS_URL, region)
    async with httpx.AsyncClient(timeout=8, verify=verify) as client:
        results = await asyncio.gather(
            *[
                _query(
                    client,
                    base + "/api/v1/query_range",
                    {"query": q, "start": start, "end": stop, "step": step},
                    headers,
                )
                for _, _, _, q in definitions
            ],
            return_exceptions=True,
        )
    panels, unavailable = [], []
    for (mid, title, unit, _), result in zip(definitions, results):
        series = []
        if isinstance(result, BaseException):
            unavailable.append(mid)
        else:
            for item in result.get("result", [])[:20]:
                points = []
                for timestamp, value in item.get("values", []):
                    try:
                        number = float(value)
                        points.append([float(timestamp), number if math.isfinite(number) else None])
                    except (ValueError, TypeError):
                        continue
                series.append({"name": title, "points": points})
        panels.append({"id": mid, "title": title, "unit": unit, "series": series, "unavailable": mid in unavailable})
    if len(unavailable) == len(definitions):
        raise HTTPException(503, "Monitoring is temporarily unavailable. Try again shortly.")
    return {"region": region, "start": start, "end": stop, "step": step, "panels": panels}


async def logs(
    kind: str,
    namespace: str,
    name: str,
    region: str,
    period: str,
    search: str = "",
    limit: int = 300,
    end: float | None = None,
) -> dict:
    start, stop, _ = window(period, end)
    labels = {"namespace": namespace, "inferenceservice" if kind == "endpoint" else "job": name}
    if kind == "jobset":
        # Indexed child Jobs of a JobSet share its name as their prefix.
        labels["job"] = re.escape(name) + "-.*"
    query = selectors(labels, {"job"} if kind == "jobset" else None)
    if search:
        query += " |= " + json.dumps(search)  # LogQL string literal, never executable query syntax
    base, headers, verify = target(LOKI_URL, region)
    async with httpx.AsyncClient(timeout=8, verify=verify) as client:
        data = await _query(
            client,
            base + "/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(int(start * 1e9)),
                "end": str(int(stop * 1e9)),
                "limit": limit,
                "direction": "backward",
            },
            headers,
        )
    lines = []
    for stream in data.get("result", []):
        labels = stream.get("stream") or {}
        for timestamp, line in stream.get("values", []):
            lines.append(
                {
                    "timestamp": str(timestamp),
                    "line": line,
                    "pod": labels.get("pod", ""),
                    "container": labels.get("container", ""),
                }
            )
    lines.sort(key=lambda line: int(line["timestamp"]), reverse=True)
    lines = lines[:limit]
    return {"region": region, "start": start, "end": stop, "lines": lines, "truncated": len(lines) == limit}
