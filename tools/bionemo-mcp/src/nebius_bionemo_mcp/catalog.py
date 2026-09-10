"""Load and validate the ARCHVTEAMS-2369 Terraform catalog contract."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class CatalogError(ValueError):
    """Raised when the Terraform catalog cannot be used safely."""


class CatalogEntry(BaseModel):
    """The subset of the exported NIM catalog needed by the MCP server."""

    model_config = ConfigDict(extra="allow")

    display_name: str
    enabled: bool
    deployment_name: str
    pod_selector_labels: dict[str, str] = Field(min_length=1)
    service_name: str
    service_port: int = Field(ge=1, le=65535)
    service_url: str
    image: str
    version: str
    lb_group: str | None = None
    proxy_port: int | None = Field(default=None, ge=1, le=65535)
    scaling_enabled: bool = False

    @field_validator("pod_selector_labels")
    @classmethod
    def require_app_selector(cls, labels: dict[str, str]) -> dict[str, str]:
        if not labels.get("app"):
            raise ValueError("pod_selector_labels must contain a non-empty app label")
        return labels


class FleetCatalog(BaseModel):
    """Validated model catalog keyed by the Terraform model key."""

    model_config = ConfigDict(frozen=True)

    models: dict[str, CatalogEntry]

    def enabled(self) -> dict[str, CatalogEntry]:
        return {key: model for key, model in self.models.items() if model.enabled}


def _unwrap_terraform_output(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CatalogError("nim_catalog must be a JSON object")

    wrapped = raw.get("nim_catalog")
    if isinstance(wrapped, dict) and "value" in wrapped:
        raw = wrapped["value"]

    if not isinstance(raw, dict):
        raise CatalogError("Terraform nim_catalog output value must be a JSON object")
    return raw


def _validate_service_url(model_key: str, entry: CatalogEntry, allow_non_cluster_urls: bool) -> None:
    parsed = urlsplit(entry.service_url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise CatalogError(f"{model_key}.service_url must be an unauthenticated http URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise CatalogError(f"{model_key}.service_url must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CatalogError(f"{model_key}.service_url contains an invalid port") from exc
    if port != entry.service_port:
        raise CatalogError(f"{model_key}.service_url port does not match service_port")

    if allow_non_cluster_urls:
        return

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise CatalogError(f"{model_key}.service_url must use cluster DNS, not an IP address")

    expected_prefix = f"{entry.service_name}."
    if not hostname.startswith(expected_prefix) or not hostname.endswith(".svc.cluster.local"):
        raise CatalogError(f"{model_key}.service_url must address {entry.service_name} through *.svc.cluster.local")


def load_catalog(path: Path, *, allow_non_cluster_urls: bool = False) -> FleetCatalog:
    """Load `terraform output -json nim_catalog` or the complete output object."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogError(f"cannot read NIM catalog {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"NIM catalog {path} is not valid JSON: {exc}") from exc

    values = _unwrap_terraform_output(raw)
    models: dict[str, CatalogEntry] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise CatalogError("NIM catalog keys must be non-empty strings")
        try:
            entry = CatalogEntry.model_validate(value)
        except ValidationError as exc:
            raise CatalogError(f"invalid NIM catalog entry {key}: {exc}") from exc
        _validate_service_url(key, entry, allow_non_cluster_urls)
        models[key] = entry

    if not models:
        raise CatalogError("NIM catalog is empty")
    return FleetCatalog(models=models)
