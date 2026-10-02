"""Durable artifact handling for NIM responses."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import boto3

from .schemas import ArtifactReference, InvocationResult

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_STRUCTURE_PREFIXES = ("ATOM", "HETATM", "MODEL", "HEADER", "REMARK", "TITLE", "CRYST1", "data_")


def _safe_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("-", name).strip(".-")
    return cleaned or "artifact"


@dataclass(frozen=True)
class ArtifactPayload:
    name: str
    data: bytes
    media_type: str


class ArtifactStore(Protocol):
    async def put(self, run_id: str, payload: ArtifactPayload) -> ArtifactReference: ...


class LocalArtifactStore:
    """Local development artifact store."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    async def put(self, run_id: str, payload: ArtifactPayload) -> ArtifactReference:
        target = self.directory / _safe_name(run_id) / _safe_name(payload.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, payload.data)
        digest = hashlib.sha256(payload.data).hexdigest()
        return ArtifactReference(
            name=payload.name,
            media_type=payload.media_type,
            size_bytes=len(payload.data),
            sha256=digest,
            object_key=str(target.relative_to(self.directory)),
            download_url=target.as_uri(),
        )


class S3ArtifactStore:
    """Nebius Object Storage artifact store through its S3-compatible API."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str,
        prefix: str,
        presign_ttl_seconds: int,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.presign_ttl_seconds = presign_ttl_seconds
        self._client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    async def put(self, run_id: str, payload: ArtifactPayload) -> ArtifactReference:
        key = "/".join(part for part in (self.prefix, _safe_name(run_id), _safe_name(payload.name)) if part)

        def upload() -> tuple[str, str]:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload.data,
                ContentType=payload.media_type,
                Metadata={"sha256": hashlib.sha256(payload.data).hexdigest()},
            )
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.presign_ttl_seconds,
            )
            return key, url

        object_key, url = await asyncio.to_thread(upload)
        return ArtifactReference(
            name=payload.name,
            media_type=payload.media_type,
            size_bytes=len(payload.data),
            sha256=hashlib.sha256(payload.data).hexdigest(),
            object_key=object_key,
            download_url=url,
            expires_at=(datetime.now(UTC) + timedelta(seconds=self.presign_ttl_seconds)).isoformat(),
        )


def _json_payload(name: str, value: Any) -> ArtifactPayload:
    return ArtifactPayload(
        name=name,
        data=(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(),
        media_type="application/json",
    )


def _structure_strings(value: Any) -> list[str]:
    structures: list[str] = []
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(_STRUCTURE_PREFIXES):
            structures.append(value)
    elif isinstance(value, list):
        for item in value:
            structures.extend(_structure_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            structures.extend(_structure_strings(item))
    return structures


def extract_artifacts(model: str, response: dict[str, Any]) -> list[ArtifactPayload]:
    """Extract known scientific files while retaining the complete response JSON."""

    artifacts = [_json_payload("response.json", response)]

    if model == "boltz2":
        for index, item in enumerate(response.get("structures", []), start=1):
            if isinstance(item, dict) and isinstance(item.get("structure"), str):
                artifacts.append(
                    ArtifactPayload(f"structure-{index}.cif", item["structure"].encode(), "chemical/x-cif")
                )
    elif model == "openfold2":
        for index, structure_text in enumerate(_structure_strings(response), start=1):
            is_cif = structure_text.lstrip().startswith("data_")
            extension = "cif" if is_cif else "pdb"
            media_type = "chemical/x-cif" if is_cif else "chemical/x-pdb"
            artifacts.append(ArtifactPayload(f"structure-{index}.{extension}", structure_text.encode(), media_type))
    elif model == "openfold3":
        for output_index, output in enumerate(response.get("outputs", []), start=1):
            if not isinstance(output, dict):
                continue
            for structure_index, item in enumerate(output.get("structures_with_scores", []), start=1):
                if not isinstance(item, dict) or not isinstance(item.get("structure"), str):
                    continue
                extension = "pdb" if item.get("format") == "pdb" else "cif"
                media_type = "chemical/x-pdb" if extension == "pdb" else "chemical/x-cif"
                artifacts.append(
                    ArtifactPayload(
                        f"output-{output_index}-structure-{structure_index}.{extension}",
                        item["structure"].encode(),
                        media_type,
                    )
                )
    elif model == "diffdock":
        for index, pose in enumerate(response.get("ligand_positions", []), start=1):
            if isinstance(pose, str):
                artifacts.append(ArtifactPayload(f"pose-{index}.sdf", pose.encode(), "chemical/x-mdl-sdfile"))
    elif model == "rfdiffusion" and isinstance(response.get("output_pdb"), str):
        artifacts.append(ArtifactPayload("backbone.pdb", response["output_pdb"].encode(), "chemical/x-pdb"))
    elif model == "proteinmpnn" and isinstance(response.get("mfasta"), str):
        artifacts.append(ArtifactPayload("designed-sequences.fasta", response["mfasta"].encode(), "text/x-fasta"))
    elif model == "msa_search":
        for database, formats in response.get("alignments", {}).items():
            if not isinstance(formats, dict):
                continue
            for format_name, item in formats.items():
                if isinstance(item, dict) and isinstance(item.get("alignment"), str):
                    artifacts.append(
                        ArtifactPayload(
                            f"alignment-{_safe_name(str(database))}.{_safe_name(str(format_name))}",
                            item["alignment"].encode(),
                            "text/plain",
                        )
                    )
    elif model == "evo2" and isinstance(response.get("data"), str):
        try:
            decoded = base64.b64decode(response["data"], validate=True)
        except ValueError:
            pass
        else:
            artifacts.append(ArtifactPayload("layer-outputs.npz", decoded, "application/octet-stream"))
    elif model == "msa_structure_pipeline":
        msa = response.get("msa_search")
        if isinstance(msa, dict):
            for item in extract_artifacts("msa_search", msa)[1:]:
                artifacts.append(ArtifactPayload(f"msa-{item.name}", item.data, item.media_type))
        structure = response.get("openfold3")
        if isinstance(structure, dict):
            for item in extract_artifacts("openfold3", structure)[1:]:
                artifacts.append(ArtifactPayload(f"structure-{item.name}", item.data, item.media_type))
    elif model == "drug_discovery_pipeline":
        docking = response.get("docking")
        if isinstance(docking, list):
            for candidate_index, candidate in enumerate(docking, start=1):
                if not isinstance(candidate, dict) or not isinstance(candidate.get("response"), dict):
                    continue
                for item in extract_artifacts("diffdock", candidate["response"])[1:]:
                    artifacts.append(
                        ArtifactPayload(f"candidate-{candidate_index}-{item.name}", item.data, item.media_type)
                    )
        affinity = response.get("affinity")
        if isinstance(affinity, list):
            for candidate_index, candidate in enumerate(affinity, start=1):
                if not isinstance(candidate, dict) or not isinstance(candidate.get("response"), dict):
                    continue
                for item in extract_artifacts("boltz2", candidate["response"])[1:]:
                    artifacts.append(
                        ArtifactPayload(f"affinity-{candidate_index}-{item.name}", item.data, item.media_type)
                    )

    return artifacts


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<nested value saved in response.json>"
    if isinstance(value, str) and len(value) > 512:
        return f"<string of {len(value)} characters saved as an artifact>"
    if isinstance(value, list):
        if len(value) > 20:
            return {"item_count": len(value), "preview": [_compact(item, depth=depth + 1) for item in value[:3]]}
        return [_compact(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _compact(item, depth=depth + 1) for key, item in value.items()}
    return value


class ArtifactManager:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    async def persist(
        self,
        *,
        model: str,
        operation: str,
        request: dict[str, Any],
        response: dict[str, Any],
        elapsed_seconds: float,
        run_id: str | None = None,
    ) -> InvocationResult:
        run_id = run_id or uuid4().hex
        payloads = [_json_payload("request.json", request), *extract_artifacts(model, response)]
        references = [await self.store.put(run_id, item) for item in payloads]
        return InvocationResult(
            run_id=run_id,
            model=model,
            operation=operation,
            elapsed_seconds=round(elapsed_seconds, 4),
            response_summary=_compact(response),
            artifacts=references,
        )
