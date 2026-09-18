"""Minimal static bearer authentication for Streamable HTTP."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import cast

from starlette.types import Receive, Scope, Send

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class StaticBearerAuthMiddleware:
    """Protect the MCP route with a pre-provisioned bearer token.

    This deliberately does not advertise an OAuth authorization server. OAuth
    discovery and token issuance are future work; v1 accepts a secret supplied
    by the operator and sent by MCP clients on every HTTP request.
    """

    def __init__(self, app: ASGIApp, *, token: str, protected_path: str = "/mcp") -> None:
        if not token:
            raise ValueError("bearer token must not be empty")
        self.app = app
        self.token = token.encode()
        self.protected_path = protected_path.rstrip("/")

    @staticmethod
    def _authorization(scope: Scope) -> bytes | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                return cast(bytes, value)
        return None

    def _valid(self, scope: Scope) -> bool:
        header = self._authorization(scope)
        if header is None or not header.startswith(b"Bearer "):
            return False
        candidate = header[len(b"Bearer ") :]
        return secrets.compare_digest(candidate, self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", "")).rstrip("/")
        protected = path == self.protected_path or path.startswith(f"{self.protected_path}/")
        if scope["type"] == "http" and protected and not self._valid(scope):
            body = json.dumps({"error": "unauthorized", "error_description": "Bearer token required"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b'Bearer realm="nebius-bionemo-mcp"'),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
