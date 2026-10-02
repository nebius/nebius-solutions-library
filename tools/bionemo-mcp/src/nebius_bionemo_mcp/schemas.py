"""Typed MCP request and response schemas for the supported BioNeMo NIMs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _a3m_formats() -> list[Literal["a3m", "fasta"]]:
    return ["a3m"]


class Polymer(StrictModel):
    molecule_type: Literal["protein", "dna", "rna"]
    sequence: str = Field(min_length=1, max_length=4096)
    id: str | None = None
    cyclic: bool = False
    msa: dict[str, Any] | None = None
    modifications: list[dict[str, Any]] = Field(default_factory=list)
    structural_templates: list[dict[str, Any]] = Field(default_factory=list)


class Ligand(StrictModel):
    id: str | None = None
    smiles: str | None = None
    ccd: str | None = None
    predict_affinity: bool = False

    @model_validator(mode="after")
    def one_representation(self) -> Ligand:
        if (self.smiles is None) == (self.ccd is None):
            raise ValueError("provide exactly one of smiles or ccd")
        return self


class Boltz2Request(StrictModel):
    polymers: list[Polymer] = Field(min_length=1, max_length=12)
    ligands: list[Ligand] = Field(default_factory=list, max_length=20)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    recycling_steps: int = Field(default=3, ge=1, le=10)
    sampling_steps: int = Field(default=50, ge=10, le=1000)
    diffusion_samples: int = Field(default=1, ge=1, le=25)
    step_scale: float = Field(default=1.638, ge=0.5, le=5.0)
    sampling_steps_affinity: int = Field(default=200, ge=10, le=1000)
    diffusion_samples_affinity: int = Field(default=5, ge=1, le=10)
    output_format: Literal["mmcif"] = "mmcif"
    without_potentials: bool = False
    concatenate_msas: bool = False
    affinity_mw_correction: bool = False
    write_full_pae: bool = False


class OpenFold2Request(StrictModel):
    sequence: str = Field(min_length=1, max_length=2048)
    input_id: str | None = Field(default=None, max_length=128)
    alignments: dict[str, Any] | None = None
    templates: dict[str, Any] | None = None
    selected_models: list[int] | None = None
    relax_prediction: bool | None = None
    use_templates: bool | None = None
    explicit_templates: list[dict[str, Any]] | None = None


class OpenFold3Molecule(StrictModel):
    type: Literal["protein", "rna", "dna", "ligand"]
    id: str | list[str] | None = None
    sequence: str | None = Field(default=None, min_length=2, max_length=4096)
    smiles: str | None = Field(default=None, min_length=1)
    ccd_codes: str | None = Field(default=None, min_length=1, max_length=5)
    msa: dict[str, Any] | None = None
    paired_msa: dict[str, Any] | None = None
    diffusion_samples: int = Field(default=1, ge=1, le=5)
    structural_templates: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def representation_matches_type(self) -> OpenFold3Molecule:
        if self.type == "ligand":
            if self.sequence is not None or (self.smiles is None) == (self.ccd_codes is None):
                raise ValueError("ligands require exactly one of smiles or ccd_codes and no sequence")
        elif self.sequence is None or self.smiles is not None or self.ccd_codes is not None:
            raise ValueError("protein, DNA, and RNA molecules require sequence and cannot contain ligand fields")
        return self


class OpenFold3Input(StrictModel):
    input_id: str | None = Field(default=None, max_length=128)
    output_format: Literal["cif", "pdb"] = "cif"
    molecules: list[OpenFold3Molecule] = Field(min_length=1, max_length=32)


class OpenFold3Request(StrictModel):
    request_id: str | None = Field(default=None, max_length=128)
    inputs: list[OpenFold3Input] = Field(min_length=1, max_length=1)


class DiffDockRequest(StrictModel):
    protein: str = Field(min_length=1)
    ligand: str = Field(min_length=1)
    ligand_file_type: Literal["mol2", "sdf", "txt"] = "txt"
    num_poses: int = Field(default=10, ge=1, le=100)
    time_divisions: int = Field(default=20, ge=3, le=20)
    steps: int = Field(default=18, ge=1, le=18)
    save_trajectory: bool = False
    skip_gen_conformer: bool = False
    is_staged: bool = False


class GenMolRequest(StrictModel):
    smiles: str = Field(min_length=1, description="SAFE notation, including optional [*{min-max}] masks")
    num_molecules: int = Field(default=30, ge=1, le=1000)
    temperature: str = Field(default="1", pattern=r"^(?:10(?:\.0+)?|(?:[0-9](?:\.\d+)?|0?\.0*[1-9]\d*))$")
    noise: str = Field(default="1", pattern=r"^(?:[01](?:\.\d+)?|2(?:\.0+)?)$")
    step_size: int = Field(default=1, ge=1, le=10)
    scoring: Literal["QED", "LogP"] = "QED"
    unique: bool = True


class MolMIMGenerateRequest(StrictModel):
    operation: Literal["generate"] = "generate"
    smi: str = Field(min_length=1)
    algorithm: Literal["CMA-ES", "none"] = "CMA-ES"
    num_molecules: int = Field(default=10, ge=1, le=100)
    iterations: int = Field(default=10, ge=1, le=1000)
    property_name: Literal["QED", "plogP"] = "QED"
    particles: int = Field(default=20, ge=2, le=1000)
    minimize: bool = False
    min_similarity: float = Field(default=0.7, ge=0, le=1)
    scaled_radius: float = Field(default=1.0, ge=0, le=2)


class MolMIMEmbeddingRequest(StrictModel):
    operation: Literal["embedding", "hidden"]
    sequences: list[str] = Field(min_length=1)


class MolMIMDecodeRequest(StrictModel):
    operation: Literal["decode"]
    hiddens: list[Any]
    mask: list[Any]


class MolMIMSamplingRequest(StrictModel):
    operation: Literal["sampling"]
    sequences: list[str] = Field(min_length=1)
    beam_size: int = Field(default=1, ge=1, le=10)
    num_molecules: int = Field(default=1, ge=1, le=10)
    scaled_radius: float = Field(default=0.7, ge=0, le=2)


MolMIMRequest = Annotated[
    MolMIMGenerateRequest | MolMIMEmbeddingRequest | MolMIMDecodeRequest | MolMIMSamplingRequest,
    Field(discriminator="operation"),
]


class MSAStandardRequest(StrictModel):
    operation: Literal["standard"] = "standard"
    sequence: str = Field(min_length=1, max_length=4096)
    databases: list[str] = Field(default_factory=lambda: ["all"], min_length=1, max_length=5)
    search_type: Literal["colabfold", "alphafold2"] = "colabfold"
    e_value: float = Field(default=0.0001, ge=0, le=1)
    iterations: int = Field(default=1, ge=1, le=6)
    max_msa_sequences: int = Field(default=500, ge=1, le=500)
    output_alignment_formats: list[Literal["a3m", "fasta"]] = Field(default_factory=_a3m_formats)


class MSAPairedRequest(StrictModel):
    operation: Literal["paired"]
    sequences: list[str] | dict[str, str]
    databases: list[str] = Field(default_factory=lambda: ["all"], min_length=1, max_length=5)
    e_value: float = Field(default=0.0001, ge=0, le=1)
    max_msa_sequences: int = Field(default=500, ge=1, le=500)
    pairing_strategy: Literal["greedy", "complete"] = "greedy"


class MSATemplateRequest(StrictModel):
    operation: Literal["templates"]
    sequence: str = Field(min_length=1, max_length=4096)
    structural_template_databases: list[str] = Field(default_factory=lambda: ["pdb70_220313"])
    msa_databases: list[str] = Field(default_factory=lambda: ["all"])
    e_value: float = Field(default=0.0001, ge=0, le=1)
    max_structures: int = Field(default=20, ge=1, le=100)
    max_msa_sequences: int = Field(default=500, ge=1, le=500)


MSASearchRequest = Annotated[
    MSAStandardRequest | MSAPairedRequest | MSATemplateRequest,
    Field(discriminator="operation"),
]


class RFDiffusionRequest(StrictModel):
    contigs: str = Field(min_length=1)
    input_pdb: str | None = None
    input_pdb_asset: str | None = None
    hotspot_res: list[str] | None = None
    diffusion_steps: int = Field(default=50, ge=1, le=50)
    random_seed: int | None = None

    @model_validator(mode="after")
    def require_structure(self) -> RFDiffusionRequest:
        if self.input_pdb is None and self.input_pdb_asset is None:
            raise ValueError("provide input_pdb or input_pdb_asset")
        return self


class ProteinMPNNRequest(StrictModel):
    input_pdb: str | None = None
    input_pdb_asset: str | None = None
    input_pdb_chains: list[str] | None = None
    ca_only: bool = False
    use_soluble_model: bool = False
    random_seed: int | None = None
    num_seq_per_target: int = Field(default=1, ge=1, le=100)
    sampling_temp: list[float] | None = None
    pssm_jsonl: str | None = None
    pssm_multi: float = Field(default=0, ge=0, le=1)
    pssm_threshold: float = 0
    pssm_bias_flag: bool = False
    pssm_log_odds_flag: bool = False
    fixed_positions_jsonl: str | None = None
    omit_aas: list[str] | None = Field(default=None, alias="omit_AAs")
    omit_aa_jsonl: str | None = Field(default=None, alias="omit_AA_jsonl")
    bias_aa_jsonl: str | None = Field(default=None, alias="bias_AA_jsonl")
    bias_by_res_jsonl: str | None = None
    tied_positions_jsonl: str | None = None

    @model_validator(mode="after")
    def require_structure(self) -> ProteinMPNNRequest:
        if self.input_pdb is None and self.input_pdb_asset is None:
            raise ValueError("provide input_pdb or input_pdb_asset")
        return self


class Evo2GenerateRequest(StrictModel):
    operation: Literal["generate"] = "generate"
    sequence: str = Field(min_length=1)
    num_tokens: int = Field(default=100, ge=1)
    temperature: float = Field(default=0.7, gt=0, le=1.3)
    top_k: int = Field(default=3, ge=0, le=6)
    top_p: float = Field(default=0, ge=0, le=1)
    random_seed: int | None = None
    enable_logits: bool = False
    enable_sampled_probs: bool = False
    enable_elapsed_ms_per_token: bool = False


class Evo2ForwardRequest(StrictModel):
    operation: Literal["forward"]
    sequence: str = Field(min_length=1)
    output_layers: list[str] = Field(min_length=1)


Evo2Request = Annotated[Evo2GenerateRequest | Evo2ForwardRequest, Field(discriminator="operation")]


class MSAStructurePipelineRequest(StrictModel):
    sequence: str = Field(min_length=1, max_length=4096)
    input_id: str = Field(default="msa-structure", max_length=128)
    databases: list[str] = Field(default_factory=lambda: ["all"], min_length=1, max_length=5)
    max_msa_sequences: int = Field(default=500, ge=1, le=500)
    output_format: Literal["cif", "pdb"] = "cif"


class DrugDiscoveryPipelineRequest(StrictModel):
    target_sequence: str = Field(min_length=1, max_length=4096)
    target_pdb: str = Field(min_length=1)
    safe_input: str = Field(default="[*{20-30}]", min_length=1)
    num_molecules: int = Field(default=10, ge=1, le=100)
    molecules_to_dock: int = Field(default=3, ge=1, le=20)
    poses_per_molecule: int = Field(default=3, ge=1, le=20)
    affinity_candidates: int = Field(default=1, ge=1, le=5)


class ArtifactReference(StrictModel):
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    object_key: str
    download_url: str
    expires_at: str | None = None


class InvocationResult(StrictModel):
    run_id: str
    model: str
    operation: str
    elapsed_seconds: float
    response_summary: dict[str, Any]
    artifacts: list[ArtifactReference]


class ModelHealth(StrictModel):
    catalog_key: str
    display_name: str
    image: str
    version: str
    enabled: bool
    healthy: bool
    tool_name: str | None = None
    status_code: int | None = None
    latency_seconds: float | None = None
    detail: str | None = None


class FleetHealthResult(StrictModel):
    healthy: bool
    checked_at: str
    models: list[ModelHealth]
