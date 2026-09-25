"""HTTP client for cluster-internal NIM services."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .catalog import CatalogEntry, FleetCatalog
from .schemas import FleetHealthResult, ModelHealth


class FleetError(RuntimeError):
    """A NIM request failed."""


@dataclass(frozen=True)
class NIMResponse:
    payload: dict[str, Any]
    elapsed_seconds: float


class FleetClient:
    """Routes requests using only service URLs exported by Terraform."""

    def __init__(
        self,
        catalog: FleetCatalog,
        *,
        probe_timeout_seconds: float,
        request_timeout_seconds: float,
        max_response_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.catalog = catalog
        self.probe_timeout_seconds = probe_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def model(self, key: str) -> CatalogEntry:
        try:
            model = self.catalog.models[key]
        except KeyError as exc:
            raise FleetError(f"model {key!r} is absent from nim_catalog") from exc
        if not model.enabled:
            raise FleetError(f"model {key!r} is disabled in nim_catalog")
        return model

    @staticmethod
    def _url(model: CatalogEntry, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("NIM paths must be absolute single-slash paths")
        return f"{model.service_url.rstrip('/')}{path}"

    async def probe_one(self, key: str) -> ModelHealth:
        model = self.catalog.models[key]
        if not model.enabled:
            return ModelHealth(
                catalog_key=key,
                display_name=model.display_name,
                image=model.image,
                version=model.version,
                enabled=False,
                healthy=False,
                detail="disabled in nim_catalog",
            )

        started = time.monotonic()
        try:
            response = await self._client.get(
                self._url(model, "/v1/health/ready"),
                timeout=httpx.Timeout(self.probe_timeout_seconds),
            )
            latency = time.monotonic() - started
            healthy = response.status_code == 200
            return ModelHealth(
                catalog_key=key,
                display_name=model.display_name,
                image=model.image,
                version=model.version,
                enabled=True,
                healthy=healthy,
                status_code=response.status_code,
                latency_seconds=round(latency, 4),
                detail=None if healthy else response.text[:240],
            )
        except httpx.HTTPError as exc:
            return ModelHealth(
                catalog_key=key,
                display_name=model.display_name,
                image=model.image,
                version=model.version,
                enabled=True,
                healthy=False,
                latency_seconds=round(time.monotonic() - started, 4),
                detail=f"{type(exc).__name__}: {exc}",
            )

    async def probe_all(self) -> FleetHealthResult:
        results = await asyncio.gather(*(self.probe_one(key) for key in sorted(self.catalog.models)))
        enabled_results = [result for result in results if result.enabled]
        return FleetHealthResult(
            healthy=bool(enabled_results) and all(result.healthy for result in enabled_results),
            checked_at=datetime.now(UTC).isoformat(),
            models=results,
        )

    async def invoke(self, key: str, path: str, payload: dict[str, Any]) -> NIMResponse:
        model = self.model(key)
        started = time.monotonic()
        try:
            async with self._client.stream(
                "POST",
                self._url(model, path),
                json=payload,
                timeout=httpx.Timeout(self.request_timeout_seconds),
            ) as response:
                if not response.is_success:
                    error_body = bytearray()
                    async for chunk in response.aiter_bytes():
                        error_body.extend(chunk[: 1000 - len(error_body)])
                        if len(error_body) >= 1000:
                            break
                    detail = error_body.decode(errors="replace")
                    raise FleetError(f"{key} returned HTTP {response.status_code}: {detail}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise FleetError(f"{key} response exceeded configured limit of {self.max_response_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise FleetError(f"request to {key} failed: {type(exc).__name__}: {exc}") from exc

        try:
            decoded = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FleetError(f"{key} returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise FleetError(f"{key} returned JSON {type(decoded).__name__}; expected an object")
        return NIMResponse(payload=decoded, elapsed_seconds=time.monotonic() - started)
