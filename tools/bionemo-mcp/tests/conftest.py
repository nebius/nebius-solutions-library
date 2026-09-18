from __future__ import annotations

from collections.abc import Callable

import pytest

from nebius_bionemo_mcp.catalog import CatalogEntry, FleetCatalog


@pytest.fixture
def catalog_factory() -> Callable[..., FleetCatalog]:
    def build(*keys: str, enabled: bool = True, base_url: str | None = None) -> FleetCatalog:
        models = {}
        for key in keys:
            service_name = f"{key.replace('_', '-')}-svc"
            service_url = base_url or f"http://{service_name}.nims.svc.cluster.local:8000"
            models[key] = CatalogEntry(
                display_name=key,
                enabled=enabled,
                deployment_name=key.replace("_", "-"),
                pod_selector_labels={"app": key.replace("_", "-")},
                service_name=service_name,
                service_port=8000,
                service_url=service_url,
                image="nvcr.io/test/image",
                version="1.0.0",
            )
        return FleetCatalog(models=models)

    return build
