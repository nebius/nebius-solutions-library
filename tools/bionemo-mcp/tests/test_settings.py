from __future__ import annotations

import pytest
from pydantic import ValidationError

from nebius_bionemo_mcp.settings import Settings


def test_s3_backend_requires_https_origin() -> None:
    with pytest.raises(ValidationError, match="HTTPS origin"):
        Settings(
            artifact_backend="s3",
            s3_bucket="results",
            s3_endpoint_url="http://storage.example.test/path",
        )


def test_s3_backend_accepts_https_origin() -> None:
    settings = Settings(
        artifact_backend="s3",
        s3_bucket="results",
        s3_endpoint_url="https://storage.example.test",
    )

    assert settings.s3_endpoint_url == "https://storage.example.test"
