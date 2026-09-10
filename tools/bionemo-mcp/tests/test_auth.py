from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from nebius_bionemo_mcp.auth import StaticBearerAuthMiddleware

TOKEN = "a" * 32


async def ok(_) -> PlainTextResponse:
    return PlainTextResponse("ok")


@pytest.mark.asyncio
async def test_bearer_auth_protects_only_mcp_route() -> None:
    app = StaticBearerAuthMiddleware(
        Starlette(routes=[Route("/mcp", ok), Route("/mcp/session/123", ok), Route("/healthz", ok)]),
        token=TOKEN,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/mcp")
        wrong = await client.get("/mcp", headers={"Authorization": "Bearer wrong"})
        valid = await client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
        subpath = await client.get("/mcp/session/123")
        health = await client.get("/healthz")
    assert missing.status_code == wrong.status_code == 401
    assert subpath.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert valid.status_code == health.status_code == 200
