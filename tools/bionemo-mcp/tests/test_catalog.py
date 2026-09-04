from __future__ import annotations

import json

import pytest

from nebius_bionemo_mcp.catalog import CatalogError, load_catalog


def _entry(url: str = "http://boltz2-svc.nims.svc.cluster.local:8000") -> dict[str, object]:
    return {
        "display_name": "Boltz2",
        "enabled": True,
        "deployment_name": "boltz2",
        "pod_selector_labels": {"app": "boltz2"},
        "service_name": "boltz2-svc",
        "service_port": 8000,
        "service_url": url,
        "image": "nvcr.io/nim/mit/boltz2",
        "version": "1.0.0",
    }


def test_loads_direct_and_complete_terraform_outputs(tmp_path) -> None:
    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps({"boltz2": _entry()}))
    assert load_catalog(direct).models["boltz2"].service_name == "boltz2-svc"

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"nim_catalog": {"sensitive": False, "type": ["map"], "value": {"boltz2": _entry()}}})
    )
    assert load_catalog(wrapped).models["boltz2"].enabled


@pytest.mark.parametrize(
    "url",
    [
        "https://boltz2-svc.nims.svc.cluster.local:8000",
        "http://127.0.0.1:8000",
        "http://wrong.nims.svc.cluster.local:8000",
        "http://boltz2-svc.nims.svc.cluster.local:9000",
        "http://boltz2-svc.nims.svc.cluster.local:8000/predict",
        "http://user:password@boltz2-svc.nims.svc.cluster.local:8000",
        "http://boltz2-svc.nims.svc.cluster.local:not-a-port",
    ],
)
def test_rejects_catalog_urls_that_are_not_private_service_roots(tmp_path, url: str) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"boltz2": _entry(url)}))
    with pytest.raises(CatalogError):
        load_catalog(path)


def test_local_development_override_accepts_loopback(tmp_path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"boltz2": _entry("http://127.0.0.1:8000")}))
    assert load_catalog(path, allow_non_cluster_urls=True).models["boltz2"].service_url.endswith(":8000")


def test_rejects_catalog_without_the_exported_pod_selector(tmp_path) -> None:
    entry = _entry()
    entry["pod_selector_labels"] = {}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"boltz2": entry}))
    with pytest.raises(CatalogError, match="pod_selector_labels"):
        load_catalog(path)
