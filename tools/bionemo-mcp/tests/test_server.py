from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from mcp import Client

from nebius_bionemo_mcp.artifacts import ArtifactManager, LocalArtifactStore
from nebius_bionemo_mcp.catalog import FleetCatalog
from nebius_bionemo_mcp.fleet import FleetClient
from nebius_bionemo_mcp.server import Runtime, build_server
from nebius_bionemo_mcp.settings import Settings
from nebius_bionemo_mcp.tools import MODEL_TOOL_NAMES, PIPELINE_REQUIREMENTS


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/health/ready":
        if request.url.host.startswith("diffdock"):
            return httpx.Response(503, text="warming")
        return httpx.Response(200, text="ready")
    if request.url.path.endswith("boltz2/predict"):
        return httpx.Response(
            200,
            json={"structures": [{"structure": "data_TEST", "format": "mmcif"}], "confidence_scores": [0.9]},
        )
    return httpx.Response(200, json={})


@pytest.mark.asyncio
async def test_dynamic_registration_and_in_memory_call(tmp_path, catalog_factory: Callable[..., FleetCatalog]) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    fleet = FleetClient(
        catalog_factory("boltz2", "diffdock", "genmol", "openfold3", "msa_search"),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024 * 1024,
        client=http,
    )
    settings = Settings(catalog_file=tmp_path / "unused", artifact_directory=tmp_path)
    runtime = Runtime(settings, fleet, ArtifactManager(LocalArtifactStore(tmp_path)))
    server = await build_server(runtime)

    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "boltz2_predict" in names
        assert "diffdock_dock" not in names
        assert "msa_structure_prediction_pipeline" in names
        assert "drug_discovery_pipeline" not in names
        boltz = next(tool for tool in tools.tools if tool.name == "boltz2_predict")
        assert boltz.input_schema["required"] == ["request"]
        result = await client.call_tool(
            "boltz2_predict",
            {"request": {"polymers": [{"molecule_type": "protein", "sequence": "MTEYK", "id": "A"}]}},
        )
        assert not result.is_error
        assert result.structured_content["model"] == "boltz2"
        assert {item["name"] for item in result.structured_content["artifacts"]} >= {
            "request.json",
            "response.json",
        }
        listed = await client.call_tool("list_models", {})
        listed_models = listed.structured_content["models"]
        boltz2 = next(model for model in listed_models if model["catalog_key"] == "boltz2")
        assert boltz2["image"] == "nvcr.io/test/image"
        assert boltz2["version"] == "1.0.0"
        diffdock = next(model for model in listed_models if model["catalog_key"] == "diffdock")
        assert not diffdock["healthy"] and diffdock["tool_name"] is None
    await http.aclose()


def _pipeline_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(200, text="ready")
    host = request.url.host
    if host.startswith("msa-search"):
        return httpx.Response(
            200,
            json={"alignments": {"uniref": {"a3m": {"alignment": ">query\nACDE", "format": "a3m"}}}},
        )
    if host.startswith("openfold3"):
        return httpx.Response(
            200,
            json={"outputs": [{"structures_with_scores": [{"structure": "data_TEST", "format": "cif"}]}]},
        )
    if host.startswith("genmol"):
        return httpx.Response(200, json={"status": "success", "molecules": [{"smiles": "CC", "score": 0.8}]})
    if host.startswith("diffdock"):
        return httpx.Response(200, json={"position_confidence": [0.7], "ligand_positions": ["POSE"]})
    if host.startswith("boltz2"):
        return httpx.Response(
            200,
            json={
                "structures": [{"structure": "data_COMPLEX", "format": "mmcif"}],
                "affinities": {"L1": {"affinity_pic50": [6.2]}},
            },
        )
    return httpx.Response(200, json={})


@pytest.mark.asyncio
async def test_complete_tool_surface_and_both_pipelines(tmp_path, catalog_factory: Callable[..., FleetCatalog]) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_pipeline_handler))
    fleet = FleetClient(
        catalog_factory(*MODEL_TOOL_NAMES),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024 * 1024,
        client=http,
    )
    runtime = Runtime(
        Settings(catalog_file=tmp_path / "unused", artifact_directory=tmp_path),
        fleet,
        ArtifactManager(LocalArtifactStore(tmp_path)),
    )
    server = await build_server(runtime)

    async with Client(server) as client:
        tools = await client.list_tools()
        expected = {"list_models", "fleet_health", *MODEL_TOOL_NAMES.values(), *PIPELINE_REQUIREMENTS}
        assert {tool.name for tool in tools.tools} == expected

        msa = await client.call_tool(
            "msa_structure_prediction_pipeline",
            {"request": {"sequence": "ACDE", "input_id": "pipeline-test"}},
        )
        assert not msa.is_error
        assert msa.structured_content["model"] == "msa_structure_pipeline"
        assert {item["name"] for item in msa.structured_content["artifacts"]} >= {
            "msa-alignment-uniref.a3m",
            "structure-output-1-structure-1.cif",
        }

        discovery = await client.call_tool(
            "drug_discovery_pipeline",
            {
                "request": {
                    "target_sequence": "ACDE",
                    "target_pdb": "ATOM      1  CA  ALA A   1       0.000   0.000   0.000",
                    "num_molecules": 1,
                    "molecules_to_dock": 1,
                    "poses_per_molecule": 1,
                    "affinity_candidates": 1,
                }
            },
        )
        assert not discovery.is_error
        assert discovery.structured_content["model"] == "drug_discovery_pipeline"
        assert {item["name"] for item in discovery.structured_content["artifacts"]} >= {
            "candidate-1-pose-1.sdf",
            "affinity-1-structure-1.cif",
        }
    await http.aclose()


@pytest.mark.parametrize(
    ("catalog_key", "tool_name", "payload", "expected_path"),
    [
        (
            "boltz2",
            "boltz2_predict",
            {"polymers": [{"molecule_type": "protein", "sequence": "AC"}]},
            "/biology/mit/boltz2/predict",
        ),
        (
            "openfold2",
            "openfold2_predict",
            {"sequence": "AC"},
            "/biology/openfold/openfold2/predict-structure-from-msa-and-template",
        ),
        (
            "openfold3",
            "openfold3_predict",
            {"inputs": [{"molecules": [{"type": "protein", "sequence": "AC"}]}]},
            "/biology/openfold/openfold3/predict",
        ),
        ("diffdock", "diffdock_dock", {"protein": "ATOM", "ligand": "CC"}, "/molecular-docking/diffdock/generate"),
        ("genmol", "genmol_generate", {"smiles": "[*{20-30}]"}, "/generate"),
        ("molmim", "molmim_run", {"operation": "generate", "smi": "CC"}, "/generate"),
        (
            "msa_search",
            "msa_search",
            {"operation": "standard", "sequence": "AC"},
            "/biology/colabfold/msa-search/predict",
        ),
        (
            "rfdiffusion",
            "rfdiffusion_generate",
            {"contigs": "100", "input_pdb": "ATOM"},
            "/biology/ipd/rfdiffusion/generate",
        ),
        (
            "proteinmpnn",
            "proteinmpnn_design",
            {"input_pdb": "ATOM"},
            "/biology/ipd/proteinmpnn/predict",
        ),
        ("evo2_40b", "evo2_run", {"operation": "generate", "sequence": "AC"}, "/biology/arc/evo2/generate"),
    ],
)
@pytest.mark.asyncio
async def test_every_model_tool_routes_through_its_catalog_service(
    tmp_path,
    catalog_factory: Callable[..., FleetCatalog],
    catalog_key: str,
    tool_name: str,
    payload: dict[str, object],
    expected_path: str,
) -> None:
    seen_posts: list[httpx.Request] = []

    def handler(nim_request: httpx.Request) -> httpx.Response:
        if nim_request.method == "GET":
            return httpx.Response(200, text="ready")
        seen_posts.append(nim_request)
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fleet = FleetClient(
        catalog_factory(catalog_key),
        probe_timeout_seconds=1,
        request_timeout_seconds=1,
        max_response_bytes=1024 * 1024,
        client=http,
    )
    runtime = Runtime(
        Settings(catalog_file=tmp_path / "unused", artifact_directory=tmp_path),
        fleet,
        ArtifactManager(LocalArtifactStore(tmp_path)),
    )
    server = await build_server(runtime)

    async with Client(server) as client:
        result = await client.call_tool(tool_name, {"request": payload})
        assert not result.is_error

    assert len(seen_posts) == 1
    assert seen_posts[0].url.host == f"{catalog_key.replace('_', '-')}-svc.nims.svc.cluster.local"
    assert seen_posts[0].url.path == expected_path
    if "operation" in payload:
        assert "operation" not in json.loads(seen_posts[0].content)
        request_artifact = next(
            artifact for artifact in result.structured_content["artifacts"] if artifact["name"] == "request.json"
        )
        saved_request = json.loads((tmp_path / request_artifact["object_key"]).read_text())
        assert saved_request["operation"] == payload["operation"]
    await http.aclose()
