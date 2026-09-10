from __future__ import annotations

import hashlib

import pytest

from nebius_bionemo_mcp.artifacts import ArtifactManager, LocalArtifactStore, extract_artifacts


def test_extracts_scientific_files_from_individual_and_pipeline_results() -> None:
    individual = extract_artifacts(
        "boltz2", {"structures": [{"structure": "data_TEST", "format": "mmcif"}], "confidence_scores": [0.9]}
    )
    assert [item.name for item in individual] == ["response.json", "structure-1.cif"]

    pipeline = extract_artifacts(
        "msa_structure_pipeline",
        {
            "msa_search": {"alignments": {"uniref": {"a3m": {"alignment": ">q\nAC", "format": "a3m"}}}},
            "openfold3": {"outputs": [{"structures_with_scores": [{"structure": "ATOM", "format": "pdb"}]}]},
        },
    )
    assert {item.name for item in pipeline} == {
        "response.json",
        "msa-alignment-uniref.a3m",
        "structure-output-1-structure-1.pdb",
    }

    openfold2_pdb = "REMARK OpenFold2 metadata\nATOM      1  CA"
    openfold2 = extract_artifacts(
        "openfold2",
        {"structures_in_ranked_order": [{"format": "pdb", "structure": openfold2_pdb}]},
    )
    assert [item.name for item in openfold2] == ["response.json", "structure-1.pdb"]
    assert openfold2[1].data.decode() == openfold2_pdb


@pytest.mark.asyncio
async def test_local_store_returns_verifiable_reference_and_compacts_response(tmp_path) -> None:
    manager = ArtifactManager(LocalArtifactStore(tmp_path))
    result = await manager.persist(
        model="rfdiffusion",
        operation="generate",
        request={"contigs": "100-100", "diffusion_steps": 50},
        response={"output_pdb": "ATOM" * 1000, "elapsed_ms": 1},
        elapsed_seconds=1.25,
        run_id="test-run",
    )
    assert result.response_summary["output_pdb"].startswith("<string of")
    names = {artifact.name for artifact in result.artifacts}
    assert names == {"request.json", "response.json", "backbone.pdb"}
    request = next(artifact for artifact in result.artifacts if artifact.name == "request.json")
    assert (tmp_path / request.object_key).read_text() == '{\n  "contigs": "100-100",\n  "diffusion_steps": 50\n}\n'
    for artifact in result.artifacts:
        content = (tmp_path / artifact.object_key).read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact.sha256
