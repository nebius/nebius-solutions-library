"""Download presigned artifacts from MCP result JSON into a local run directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _safe_name(value: str) -> str:
    value = _SAFE_NAME.sub("-", value).strip(".-")
    return value or "artifact"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactDownload:
    name: str
    download_url: str
    sha256: str
    size_bytes: int


def _collect(value: Any) -> list[ArtifactDownload]:
    found: list[ArtifactDownload] = []
    if isinstance(value, dict):
        if isinstance(value.get("download_url"), str):
            size_bytes = value.get("size_bytes")
            sha256 = value.get("sha256")
            if not isinstance(size_bytes, int) or size_bytes < 0:
                raise ValueError("artifact size_bytes must be a non-negative integer")
            if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
                raise ValueError("artifact sha256 must be a 64-character hexadecimal digest")
            found.append(
                ArtifactDownload(str(value.get("name") or "artifact"), value["download_url"], sha256, size_bytes)
            )
        for child in value.values():
            found.extend(_collect(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect(child))
    return found


def download(
    result_file: Path,
    run_directory: Path,
    *,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> list[Path]:
    result = json.loads(result_file.read_text(encoding="utf-8"))
    artifacts = _collect(result)
    if not artifacts:
        raise ValueError(f"{result_file} contains no artifact download URLs")
    total_bytes = sum(artifact.size_bytes for artifact in artifacts)
    if total_bytes > max_total_bytes:
        raise ValueError(f"artifact set advertises {total_bytes} bytes; limit is {max_total_bytes}")
    if run_directory.exists() and any(run_directory.iterdir()):
        raise ValueError(f"run directory is not empty: {run_directory}")
    run_directory.mkdir(parents=True, exist_ok=True)
    safe_names = [_safe_name(artifact.name) for artifact in artifacts]
    if len(safe_names) != len(set(safe_names)):
        raise ValueError("artifact names collide after filename sanitization")
    downloaded: list[Path] = []
    with httpx.Client(follow_redirects=False, timeout=300) as client:
        for artifact, safe_name in zip(artifacts, safe_names, strict=True):
            target = run_directory / safe_name
            partial = target.with_name(f".{target.name}.part")
            try:
                parsed = urlsplit(artifact.download_url)
                if parsed.scheme == "file":
                    if parsed.netloc or parsed.query or parsed.fragment:
                        raise ValueError("local-development file URIs cannot contain an authority, query, or fragment")
                    shutil.copyfile(Path(parsed.path), partial)
                elif parsed.scheme == "https":
                    if parsed.username or parsed.password or parsed.fragment:
                        raise ValueError("HTTPS artifact URLs cannot contain user information or fragments")
                    written = 0
                    with client.stream("GET", artifact.download_url) as response:
                        response.raise_for_status()
                        with partial.open("wb") as output:
                            for chunk in response.iter_bytes():
                                written += len(chunk)
                                if written > artifact.size_bytes:
                                    raise ValueError(f"artifact exceeded advertised size: {artifact.name}")
                                output.write(chunk)
                else:
                    raise ValueError("artifact URLs must use https or local-development file URIs")
                if partial.stat().st_size != artifact.size_bytes:
                    raise ValueError(f"size mismatch for {artifact.name}")
                actual = _file_sha256(partial)
                if actual.lower() != artifact.sha256.lower():
                    raise ValueError(f"checksum mismatch for {artifact.name}")
            except Exception:
                partial.unlink(missing_ok=True)
                raise
            partial.replace(target)
            downloaded.append(target)
    return downloaded


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="JSON result returned by an MCP tool")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--max-total-bytes", type=int, default=_DEFAULT_MAX_TOTAL_BYTES)
    args = parser.parse_args(argv)
    for path in download(args.result, args.run_dir, max_total_bytes=args.max_total_bytes):
        print(path)


if __name__ == "__main__":
    main()
