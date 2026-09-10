"""Typed model and pipeline tool handlers."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from mcp.server.mcpserver import Context
from pydantic import BaseModel

from .artifacts import ArtifactManager
from .fleet import FleetClient, FleetError
from .schemas import (
    Boltz2Request,
    DiffDockRequest,
    DrugDiscoveryPipelineRequest,
    Evo2Request,
    FleetHealthResult,
    GenMolRequest,
    InvocationResult,
    MolMIMRequest,
    MSASearchRequest,
    MSAStructurePipelineRequest,
    OpenFold2Request,
    OpenFold3Request,
    ProteinMPNNRequest,
    RFDiffusionRequest,
)

MODEL_TOOL_NAMES = {
    "boltz2": "boltz2_predict",
    "openfold2": "openfold2_predict",
    "openfold3": "openfold3_predict",
    "diffdock": "diffdock_dock",
    "genmol": "genmol_generate",
    "molmim": "molmim_run",
    "msa_search": "msa_search",
    "rfdiffusion": "rfdiffusion_generate",
    "proteinmpnn": "proteinmpnn_design",
    "evo2_40b": "evo2_run",
}

PIPELINE_REQUIREMENTS = {
    "drug_discovery_pipeline": frozenset({"genmol", "diffdock", "boltz2"}),
    "msa_structure_prediction_pipeline": frozenset({"msa_search", "openfold3"}),
}


def _payload(request: BaseModel, *, exclude_operation: bool = False) -> dict[str, Any]:
    exclude = {"operation"} if exclude_operation else None
    return request.model_dump(mode="json", exclude_none=True, exclude=exclude, by_alias=True)


class ToolHandlers:
    def __init__(
        self,
        fleet: FleetClient,
        artifacts: ArtifactManager,
        startup_health: FleetHealthResult,
        registered_tools: dict[str, str],
    ) -> None:
        self.fleet = fleet
        self.artifacts = artifacts
        self.startup_health = startup_health
        self.registered_tools = registered_tools

    def _annotate_health(self, health: FleetHealthResult) -> FleetHealthResult:
        return health.model_copy(
            update={
                "models": [
                    model.model_copy(update={"tool_name": self.registered_tools.get(model.catalog_key)})
                    for model in health.models
                ]
            }
        )

    async def list_models(self) -> FleetHealthResult:
        """List the catalog models and the tools registered from the startup health snapshot."""

        return self._annotate_health(self.startup_health)

    async def fleet_health(self) -> FleetHealthResult:
        """Probe every enabled NIM and return current readiness without changing the tool list."""

        return self._annotate_health(await self.fleet.probe_all())

    async def _invoke(
        self,
        *,
        catalog_key: str,
        artifact_model: str,
        operation: str,
        path: str,
        payload: dict[str, Any],
        request: dict[str, Any] | None = None,
    ) -> InvocationResult:
        response = await self.fleet.invoke(catalog_key, path, payload)
        return await self.artifacts.persist(
            model=artifact_model,
            operation=operation,
            request=request if request is not None else payload,
            response=response.payload,
            elapsed_seconds=response.elapsed_seconds,
        )

    async def boltz2_predict(self, request: Boltz2Request) -> InvocationResult:
        """Predict biomolecular structures and optional ligand affinity with Boltz2."""

        return await self._invoke(
            catalog_key="boltz2",
            artifact_model="boltz2",
            operation="predict",
            path="/biology/mit/boltz2/predict",
            payload=_payload(request),
        )

    async def openfold2_predict(self, request: OpenFold2Request) -> InvocationResult:
        """Predict a monomer structure from sequence and optional MSA/templates with OpenFold2."""

        return await self._invoke(
            catalog_key="openfold2",
            artifact_model="openfold2",
            operation="predict-structure-from-msa-and-template",
            path="/biology/openfold/openfold2/predict-structure-from-msa-and-template",
            payload=_payload(request),
        )

    async def openfold3_predict(self, request: OpenFold3Request) -> InvocationResult:
        """Predict protein, nucleic-acid, ligand, or complex structures with OpenFold3."""

        return await self._invoke(
            catalog_key="openfold3",
            artifact_model="openfold3",
            operation="predict",
            path="/biology/openfold/openfold3/predict",
            payload=_payload(request),
        )

    async def diffdock_dock(self, request: DiffDockRequest) -> InvocationResult:
        """Dock a ligand into a protein structure with DiffDock."""

        return await self._invoke(
            catalog_key="diffdock",
            artifact_model="diffdock",
            operation="dock",
            path="/molecular-docking/diffdock/generate",
            payload=_payload(request),
        )

    async def genmol_generate(self, request: GenMolRequest) -> InvocationResult:
        """Generate or optimize molecules from SAFE notation with GenMol."""

        return await self._invoke(
            catalog_key="genmol",
            artifact_model="genmol",
            operation="generate",
            path="/generate",
            payload=_payload(request),
        )

    async def molmim_run(self, request: MolMIMRequest) -> InvocationResult:
        """Generate molecules or use a local MolMIM latent-space operation."""

        return await self._invoke(
            catalog_key="molmim",
            artifact_model="molmim",
            operation=request.operation,
            path=f"/{request.operation}",
            payload=_payload(request, exclude_operation=True),
            request=_payload(request),
        )

    async def msa_search(self, request: MSASearchRequest) -> InvocationResult:
        """Run standard, paired, or structure-template MSA Search."""

        paths = {
            "standard": "/biology/colabfold/msa-search/predict",
            "paired": "/biology/colabfold/msa-search/paired/predict",
            "templates": "/biology/colabfold/msa-search/structure-templates/predict",
        }
        return await self._invoke(
            catalog_key="msa_search",
            artifact_model="msa_search",
            operation=request.operation,
            path=paths[request.operation],
            payload=_payload(request, exclude_operation=True),
            request=_payload(request),
        )

    async def rfdiffusion_generate(self, request: RFDiffusionRequest) -> InvocationResult:
        """Generate a protein backbone from an RFdiffusion contig specification."""

        return await self._invoke(
            catalog_key="rfdiffusion",
            artifact_model="rfdiffusion",
            operation="generate",
            path="/biology/ipd/rfdiffusion/generate",
            payload=_payload(request),
        )

    async def proteinmpnn_design(self, request: ProteinMPNNRequest) -> InvocationResult:
        """Design protein sequences for a supplied backbone with ProteinMPNN."""

        return await self._invoke(
            catalog_key="proteinmpnn",
            artifact_model="proteinmpnn",
            operation="design",
            path="/biology/ipd/proteinmpnn/predict",
            payload=_payload(request),
        )

    async def evo2_run(self, request: Evo2Request) -> InvocationResult:
        """Generate DNA or capture Evo2 forward-pass layer outputs."""

        return await self._invoke(
            catalog_key="evo2_40b",
            artifact_model="evo2",
            operation=request.operation,
            path=f"/biology/arc/evo2/{request.operation}",
            payload=_payload(request, exclude_operation=True),
            request=_payload(request),
        )

    @staticmethod
    def _first_alignment(response: dict[str, Any]) -> tuple[str, str]:
        alignments = response.get("alignments")
        if not isinstance(alignments, dict):
            raise FleetError("MSA Search response did not contain alignments")
        for database, formats in alignments.items():
            if not isinstance(formats, dict):
                continue
            ordered = sorted(formats.items(), key=lambda item: item[0] != "a3m")
            for _, item in ordered:
                if isinstance(item, dict) and isinstance(item.get("alignment"), str):
                    return str(database), item["alignment"]
        raise FleetError("MSA Search response contained no usable alignment")

    async def msa_structure_prediction_pipeline(
        self, request: MSAStructurePipelineRequest, ctx: Context
    ) -> InvocationResult:
        """Run MSA Search and feed its A3M alignment directly into OpenFold3."""

        run_id = uuid4().hex
        started = time.monotonic()
        await ctx.report_progress(0, 2, "Searching sequence databases")
        msa = await self.fleet.invoke(
            "msa_search",
            "/biology/colabfold/msa-search/predict",
            {
                "sequence": request.sequence,
                "databases": request.databases,
                "search_type": "colabfold",
                "e_value": 0.0001,
                "iterations": 1,
                "max_msa_sequences": request.max_msa_sequences,
                "output_alignment_formats": ["a3m"],
            },
        )
        database, alignment = self._first_alignment(msa.payload)

        await ctx.report_progress(1, 2, "Predicting MSA-informed structure")
        structure = await self.fleet.invoke(
            "openfold3",
            "/biology/openfold/openfold3/predict",
            {
                "request_id": run_id,
                "inputs": [
                    {
                        "input_id": request.input_id,
                        "output_format": request.output_format,
                        "molecules": [
                            {
                                "type": "protein",
                                "id": "A",
                                "sequence": request.sequence,
                                "msa": {database: {"a3m": {"alignment": alignment, "format": "a3m", "rank": 0}}},
                            }
                        ],
                    }
                ],
            },
        )
        await ctx.report_progress(2, 2, "Pipeline complete")
        response = {"msa_search": msa.payload, "openfold3": structure.payload}
        return await self.artifacts.persist(
            model="msa_structure_pipeline",
            operation="msa-structure-prediction",
            request=_payload(request),
            response=response,
            elapsed_seconds=time.monotonic() - started,
            run_id=run_id,
        )

    @staticmethod
    def _atom_records(pdb: str) -> str:
        records = "\n".join(line for line in pdb.splitlines() if line.startswith("ATOM"))
        if not records:
            raise ValueError("target_pdb must contain at least one ATOM record")
        return records

    async def drug_discovery_pipeline(self, request: DrugDiscoveryPipelineRequest, ctx: Context) -> InvocationResult:
        """Generate molecules, dock candidates, and rank top hits with Boltz2 affinity."""

        run_id = uuid4().hex
        started = time.monotonic()
        protein = self._atom_records(request.target_pdb)
        await ctx.report_progress(0, 3, "Generating candidate molecules")
        generated = await self.fleet.invoke(
            "genmol",
            "/generate",
            {
                "smiles": request.safe_input,
                "num_molecules": request.num_molecules,
                "temperature": "1",
                "noise": "1",
                "step_size": 1,
                "scoring": "QED",
                "unique": True,
            },
        )
        molecules = generated.payload.get("molecules")
        if not isinstance(molecules, list) or not molecules:
            raise FleetError("GenMol returned no candidate molecules")
        candidates = sorted(
            (item for item in molecules if isinstance(item, dict) and isinstance(item.get("smiles"), str)),
            key=lambda item: float(item.get("score", 0)),
            reverse=True,
        )[: request.molecules_to_dock]
        if not candidates:
            raise FleetError("GenMol response contained no usable SMILES candidates")

        await ctx.report_progress(1, 3, "Docking generated candidates")
        docked: list[dict[str, Any]] = []
        for candidate in candidates:
            docking = await self.fleet.invoke(
                "diffdock",
                "/molecular-docking/diffdock/generate",
                {
                    "protein": protein,
                    "ligand": candidate["smiles"],
                    "ligand_file_type": "txt",
                    "num_poses": request.poses_per_molecule,
                    "time_divisions": 20,
                    "steps": 18,
                    "save_trajectory": False,
                    "skip_gen_conformer": False,
                    "is_staged": False,
                },
            )
            confidences = docking.payload.get("position_confidence", [])
            best_confidence = max((float(value) for value in confidences), default=float("-inf"))
            docked.append({"candidate": candidate, "best_confidence": best_confidence, "response": docking.payload})
        docked.sort(key=lambda item: item["best_confidence"], reverse=True)

        await ctx.report_progress(2, 3, "Scoring top candidates with Boltz2")
        affinity_results: list[dict[str, Any]] = []
        for item in docked[: request.affinity_candidates]:
            ligand_smiles = item["candidate"]["smiles"]
            affinity = await self.fleet.invoke(
                "boltz2",
                "/biology/mit/boltz2/predict",
                {
                    "polymers": [{"molecule_type": "protein", "sequence": request.target_sequence, "id": "A"}],
                    "ligands": [{"id": "L1", "smiles": ligand_smiles, "predict_affinity": True}],
                    "diffusion_samples": 1,
                    "diffusion_samples_affinity": 1,
                },
            )
            affinity_results.append(
                {
                    "smiles": ligand_smiles,
                    "docking_confidence": item["best_confidence"],
                    "response": affinity.payload,
                }
            )
        await ctx.report_progress(3, 3, "Pipeline complete")
        response = {
            "genmol": generated.payload,
            "docking": docked,
            "affinity": affinity_results,
        }
        return await self.artifacts.persist(
            model="drug_discovery_pipeline",
            operation="drug-discovery",
            request=_payload(request),
            response=response,
            elapsed_seconds=time.monotonic() - started,
            run_id=run_id,
        )
