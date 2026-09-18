#!/usr/bin/env python3
"""Fully read and verify the selected OpenFold3 buffered artifact.

The live cohort executes this source inside the already-Ready, same-node
artifact holder.  The script has no Kubernetes or network dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any


ARTIFACT_ROOT = Path(
    "/artifacts/openfold3-native-f7-v2-buffered/versions/1"
)
CHECKPOINT_ID = "openfold3-native-f7-v2-buffered"
ARTIFACT_VERSION = "1"
DEFAULT_SOURCE_NODE = "gpu-node-a.example.invalid"
EXPECTED_FILE_COUNT = 148
EXPECTED_REGULAR_BYTES = 9_263_246_107
EXPECTED_MANIFEST_SHA256 = (
    "5df221e0736a4c6f369781ea0dbc7c36783c26d3f35dcd874b4ced8f5f9e009f"
)
EXPECTED_TREE_SHA256 = (
    "f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2"
)
REQUIRED_MEMBERS = frozenset(
    {"manifest.yaml", "inventory.img", "pstree.img", "rootfs-diff.tar"}
)


class PrewarmError(ValueError):
    """The artifact is not the reviewed, fully readable buffered artifact."""


def verify_and_prewarm(
    root: Path = ARTIFACT_ROOT,
    *,
    expected_file_count: int = EXPECTED_FILE_COUNT,
    expected_regular_bytes: int = EXPECTED_REGULAR_BYTES,
    expected_manifest_sha256: str = EXPECTED_MANIFEST_SHA256,
    expected_tree_sha256: str = EXPECTED_TREE_SHA256,
    required_members: frozenset[str] = REQUIRED_MEMBERS,
    checkpoint_id: str = CHECKPOINT_ID,
    source_node: str = DEFAULT_SOURCE_NODE,
) -> dict[str, Any]:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise PrewarmError("artifact root must be an absolute non-symlink directory")

    started = time.monotonic()
    members: list[tuple[str, int, Path]] = []
    observed_bytes = 0
    for directory, names, entries in os.walk(root, followlinks=False):
        names.sort()
        entries.sort()
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                raise PrewarmError(f"artifact contains a symlinked directory: {path}")
        for name in entries:
            path = Path(directory) / name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise PrewarmError(f"artifact member is not regular: {path}")
            relative = path.relative_to(root).as_posix()
            members.append((relative, metadata.st_size, path))
            observed_bytes += metadata.st_size
    members.sort(key=lambda item: item[0])

    observed_names = {relative for relative, _, _ in members}
    if not required_members <= observed_names or not any(
        name.startswith("pages-") for name in observed_names
    ):
        raise PrewarmError("artifact is missing required CRIU members")
    if len(members) != expected_file_count or observed_bytes != expected_regular_bytes:
        raise PrewarmError(
            "artifact inventory mismatch: "
            f"files={len(members)} bytes={observed_bytes}"
        )

    tree = hashlib.sha256()
    for relative, expected_size, path in members:
        digest = hashlib.sha256()
        bytes_read = 0
        with path.open("rb", buffering=8 * 1024 * 1024) as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
                bytes_read += len(chunk)
        if bytes_read != expected_size:
            raise PrewarmError(
                f"short artifact read for {relative}: {bytes_read} != {expected_size}"
            )
        tree.update(
            f"{relative}\0{expected_size}\0{digest.hexdigest()}\n".encode("utf-8")
        )

    manifest = (root / "manifest.yaml").read_bytes()
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    if manifest_sha256 != expected_manifest_sha256:
        raise PrewarmError("artifact manifest digest differs from the reviewed receipt")
    manifest_text = manifest.decode("utf-8")
    markers = (
        f"checkpointId: {checkpoint_id}",
        f"sourceNode: {source_node}",
        "podNamespace: nim-fast-start",
        "cudaRestore:",
        "imageIoMode: buffered",
    )
    if any(marker not in manifest_text for marker in markers):
        raise PrewarmError("artifact manifest identity is not the selected buffered route")

    tree_sha256 = tree.hexdigest()
    if tree_sha256 != expected_tree_sha256:
        raise PrewarmError("artifact tree digest differs from the reviewed receipt")
    return {
        "schema": "archvteams.nebius.ai/openfold3-artifact-full-read/v1",
        "status": "PASS",
        "checkpoint_id": checkpoint_id,
        "artifact_version": ARTIFACT_VERSION,
        "source_node": source_node,
        "image_io_mode": "buffered",
        "regular_file_count": len(members),
        "regular_bytes_read": observed_bytes,
        "manifest_sha256": manifest_sha256,
        "tree_sha256": tree_sha256,
        "full_read_elapsed_seconds": round(time.monotonic() - started, 6),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-node", required=True)
    args = parser.parse_args()
    if (
        len(args.source_node) > 253
        or re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", args.source_node)
        is None
    ):
        parser.error("--source-node must be a DNS subdomain")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = verify_and_prewarm(source_node=args.source_node)
    except (OSError, UnicodeDecodeError, PrewarmError) as exc:
        print(f"openfold3-artifact-prewarm: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
