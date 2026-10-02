from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from nebius_bionemo_mcp.catalog import FleetCatalog
from nebius_bionemo_mcp.fleet import FleetClient, FleetError


@pytest.mark.asyncio
async def test_probe_and_invoke_use_catalog_service_url(catalog_factory: Callable[..., FleetCatalog]) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, text="ready")
        return httpx.Response(200, json={"status": "success", "molecules": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fleet = FleetClient(
        catalog_factory("genmol"),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024,
        client=http,
    )
    health = await fleet.probe_all()
    assert health.healthy
    response = await fleet.invoke("genmol", "/generate", {"smiles": "[*{1-2}]"})
    assert response.payload["status"] == "success"
    assert seen == [
        ("GET", "http://genmol-svc.nims.svc.cluster.local:8000/v1/health/ready"),
        ("POST", "http://genmol-svc.nims.svc.cluster.local:8000/generate"),
    ]
    await http.aclose()


@pytest.mark.asyncio
async def test_invoke_bounds_response_and_surfaces_nim_error(catalog_factory: Callable[..., FleetCatalog]) -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"{" + b'"x":"' + b"a" * 100 + b'"}'),
            httpx.Response(422, json={"detail": "bad payload"}),
        ]
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses)))
    fleet = FleetClient(
        catalog_factory("boltz2"),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=32,
        client=http,
    )
    with pytest.raises(FleetError, match="exceeded configured limit"):
        await fleet.invoke("boltz2", "/predict", {})
    with pytest.raises(FleetError, match="HTTP 422"):
        await fleet.invoke("boltz2", "/predict", {})
    await http.aclose()


@pytest.mark.asyncio
async def test_disabled_model_is_not_probed_or_invoked(catalog_factory: Callable[..., FleetCatalog]) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("network request was made")))
    fleet = FleetClient(
        catalog_factory("boltz2", enabled=False),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024,
        client=http,
    )
    health = await fleet.probe_all()
    assert not health.models[0].healthy
    with pytest.raises(FleetError, match="disabled"):
        await fleet.invoke("boltz2", "/predict", {})
    await http.aclose()


@pytest.mark.asyncio
async def test_non_utf8_response_is_reported_as_invalid_json(catalog_factory: Callable[..., FleetCatalog]) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"\xff")))
    fleet = FleetClient(
        catalog_factory("boltz2"),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024,
        client=http,
    )
    with pytest.raises(FleetError, match="non-JSON"):
        await fleet.invoke("boltz2", "/predict", {})
    await http.aclose()
