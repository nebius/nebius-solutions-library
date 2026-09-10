from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from nebius_bionemo_mcp.schemas import (
    DiffDockRequest,
    GenMolRequest,
    Ligand,
    MolMIMRequest,
    OpenFold3Molecule,
    ProteinMPNNRequest,
    RFDiffusionRequest,
)


def test_nim_wire_aliases_are_preserved() -> None:
    request = ProteinMPNNRequest(input_pdb="ATOM", omit_aas=["C"], bias_aa_jsonl="{}")
    payload = request.model_dump(by_alias=True, exclude_none=True)
    assert payload["omit_AAs"] == ["C"]
    assert payload["bias_AA_jsonl"] == "{}"
    assert "omit_aas" not in payload


def test_discriminated_operation_schema_rejects_cross_operation_fields() -> None:
    adapter = TypeAdapter(MolMIMRequest)
    embedding = adapter.validate_python({"operation": "embedding", "sequences": ["CC"]})
    assert embedding.operation == "embedding"
    with pytest.raises(ValidationError):
        adapter.validate_python({"operation": "embedding", "sequences": ["CC"], "particles": 20})


def test_api_quirks_and_required_structure_are_typed() -> None:
    assert GenMolRequest(smiles="[*{20-30}]", temperature="1").temperature == "1"
    with pytest.raises(ValidationError):
        GenMolRequest(smiles="[*{20-30}]", temperature="eleven")
    with pytest.raises(ValidationError):
        Ligand(smiles="CC", ccd="ATP")
    with pytest.raises(ValidationError):
        RFDiffusionRequest(contigs="100")
    with pytest.raises(ValidationError):
        DiffDockRequest(protein="ATOM", ligand="CC", time_divisions=2)


def test_openfold3_molecule_representation_matches_type() -> None:
    assert OpenFold3Molecule(type="protein", sequence="AC").sequence == "AC"
    assert OpenFold3Molecule(type="ligand", smiles="CC").smiles == "CC"
    with pytest.raises(ValidationError, match="require sequence"):
        OpenFold3Molecule(type="protein")
    with pytest.raises(ValidationError, match="exactly one"):
        OpenFold3Molecule(type="ligand", smiles="CC", ccd_codes="ATP")
