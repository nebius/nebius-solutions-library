"""Runtime configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed server settings."""

    model_config = SettingsConfigDict(env_prefix="BIONEMO_", extra="ignore")

    catalog_file: Path = Path("/etc/nebius-bionemo/nim-catalog.json")
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    mcp_path: str = "/mcp"
    bearer_token: SecretStr | None = None
    bearer_token_file: Path | None = None
    allow_non_cluster_urls: bool = False
    startup_probe_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    request_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    max_response_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    artifact_backend: Literal["s3", "local"] = "local"
    artifact_directory: Path = Path("./bionemo-runs")
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "us-central1"
    s3_prefix: str = "nebius-bionemo-mcp"
    presign_ttl_seconds: int = Field(default=3600, ge=60, le=604800)

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        if not self.mcp_path.startswith("/") or self.mcp_path == "/":
            raise ValueError("mcp_path must be an absolute non-root path")
        if self.transport == "streamable-http" and self.bearer_token is None and self.bearer_token_file is None:
            raise ValueError("Streamable HTTP requires BIONEMO_BEARER_TOKEN or BIONEMO_BEARER_TOKEN_FILE")
        if self.artifact_backend == "s3" and (not self.s3_bucket or not self.s3_endpoint_url):
            raise ValueError("S3 artifacts require BIONEMO_S3_BUCKET and BIONEMO_S3_ENDPOINT_URL")
        if self.artifact_backend == "s3" and self.s3_endpoint_url:
            endpoint = urlsplit(self.s3_endpoint_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.path not in ("", "/")
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("BIONEMO_S3_ENDPOINT_URL must be an unauthenticated HTTPS origin")
        return self

    def read_bearer_token(self) -> str:
        if self.bearer_token is not None:
            token = self.bearer_token.get_secret_value()
        elif self.bearer_token_file is not None:
            token = self.bearer_token_file.read_text(encoding="utf-8").strip()
        else:
            raise ValueError("bearer token is not configured")
        if len(token) < 32:
            raise ValueError("bearer token must contain at least 32 characters")
        return token
