"""Command-line entry point for both supported transports."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .auth import StaticBearerAuthMiddleware
from .server import build_server, create_runtime
from .settings import Settings

LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a Nebius BioNeMo NIM fleet through MCP")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"))
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    return parser


async def _run(settings: Settings) -> None:
    runtime = create_runtime(settings)
    try:
        server = await build_server(runtime)
        if settings.transport == "stdio":
            await server.run_stdio_async()
            return

        app = server.streamable_http_app(
            streamable_http_path=settings.mcp_path,
            max_request_body_size=32 * 1024 * 1024,
            host=settings.host,
            stateless_http=True,
        )
        authenticated = StaticBearerAuthMiddleware(
            app,
            token=settings.read_bearer_token(),
            protected_path=settings.mcp_path,
        )
        config = uvicorn.Config(
            authenticated,
            host=settings.host,
            port=settings.port,
            log_level="info",
            server_header=False,
        )
        await uvicorn.Server(config).serve()
    finally:
        await runtime.close()


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    overrides = {
        key: value
        for key, value in {
            "transport": args.transport,
            "catalog_file": args.catalog_file,
            "host": args.host,
            "port": args.port,
        }.items()
        if value is not None
    }
    settings = Settings(**overrides)
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        LOGGER.info("server stopped")


if __name__ == "__main__":
    main()
