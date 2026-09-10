from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from nebius_bionemo_mcp.artifacts import ArtifactManager, LocalArtifactStore
from nebius_bionemo_mcp.auth import StaticBearerAuthMiddleware
from nebius_bionemo_mcp.catalog import FleetCatalog
from nebius_bionemo_mcp.fleet import FleetClient
from nebius_bionemo_mcp.server import Runtime, build_server
from nebius_bionemo_mcp.settings import Settings

TOKEN = "transport-test-token-000000000000"


@pytest.mark.asyncio
async def test_authenticated_stateless_streamable_http_protocol(
    tmp_path, catalog_factory: Callable[..., FleetCatalog]
) -> None:
    fleet_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="ready")))
    fleet = FleetClient(
        catalog_factory("boltz2"),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024,
        client=fleet_http,
    )
    runtime = Runtime(
        Settings(catalog_file=tmp_path / "unused", artifact_directory=tmp_path),
        fleet,
        ArtifactManager(LocalArtifactStore(tmp_path)),
    )
    server = await build_server(runtime)
    inner = server.streamable_http_app(streamable_http_path="/mcp", host="testserver", stateless_http=True)
    app = StaticBearerAuthMiddleware(inner, token=TOKEN)

    async with inner.router.lifespan_context(inner):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as http_client:
            transport = streamable_http_client("http://testserver/mcp", http_client=http_client)
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} >= {"list_models", "fleet_health", "boltz2_predict"}
                result = await client.call_tool("list_models", {})
                assert not result.is_error
                assert result.structured_content["models"][0]["tool_name"] == "boltz2_predict"
            readiness = await http_client.get("/healthz")
            assert readiness.status_code == 200
            assert readiness.json()["registered_model_tools"] == 1
            liveness = await http_client.get("/livez")
            assert liveness.status_code == 200
            assert liveness.json() == {"status": "ok"}
    await fleet_http.aclose()


class _NIMHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/health/ready":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")
        else:
            self.send_error(404)

    def log_message(self, _format: str, *args: object) -> None:
        return


@contextmanager
def _nim_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NIMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_stdio_subprocess_uses_same_dynamic_server(tmp_path: Path) -> None:
    with _nim_server() as nim:
        port = nim.server_address[1]
        catalog = {
            "boltz2": {
                "display_name": "Boltz2",
                "enabled": True,
                "deployment_name": "boltz2",
                "pod_selector_labels": {"app": "boltz2"},
                "service_name": "boltz2-svc",
                "service_port": port,
                "service_url": f"http://127.0.0.1:{port}",
                "image": "nvcr.io/test/boltz2",
                "version": "1",
            }
        }
        catalog_file = tmp_path / "nim-catalog.json"
        catalog_file.write_text(json.dumps(catalog), encoding="utf-8")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nebius_bionemo_mcp", "--transport", "stdio"],
            env={
                "BIONEMO_CATALOG_FILE": str(catalog_file),
                "BIONEMO_ALLOW_NON_CLUSTER_URLS": "true",
                "BIONEMO_ARTIFACT_DIRECTORY": str(tmp_path / "runs"),
            },
        )
        async with Client(stdio_client(parameters)) as client:
            tools = await client.list_tools()
            assert "boltz2_predict" in {tool.name for tool in tools.tools}
            result = await client.call_tool("list_models", {})
            assert not result.is_error
