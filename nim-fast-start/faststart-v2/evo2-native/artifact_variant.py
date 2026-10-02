#!/usr/bin/env python3
"""Create a hard-linked buffered Evo2 artifact variant without overwriting data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
SOURCE_ID = PROFILE["artifacts"]["direct"]["checkpoint_id"]
DESTINATION_ID = PROFILE["artifacts"]["buffered"]["checkpoint_id"]
VERSION = "1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VariantError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise VariantError(f"refusing existing output: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def inventory(root: Path) -> tuple[list[Path], int]:
    try:
        members = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise VariantError(f"cannot inventory {root}: {exc}") from exc
    total = 0
    for member in members:
        metadata = member.lstat()
        if member.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise VariantError(f"artifact member is not a regular file: {member.name}")
        total += metadata.st_size
    return members, total


def replace_manifest(source: bytes) -> bytes:
    old_id = f"checkpointId: {SOURCE_ID}\n".encode("ascii")
    new_id = f"checkpointId: {DESTINATION_ID}\n".encode("ascii")
    if source.count(old_id) != 1 or source.count(new_id) != 0:
        raise VariantError("source manifest checkpoint identity is not exact")
    updated = source.replace(old_id, new_id, 1)
    matches = list(re.finditer(rb"(?m)^(?P<indent>[ \t]*)imageIoMode: direct[ \t]*$", updated))
    if len(matches) != 1 or re.search(rb"(?m)^[ \t]*imageIoMode: buffered[ \t]*$", source):
        raise VariantError("source manifest must select direct I/O exactly once")
    match = matches[0]
    replacement = match.group("indent") + b"imageIoMode: buffered"
    return updated[: match.start()] + replacement + updated[match.end() :]


def build_variant(
    checkpoints_root: Path,
    source_manifest_sha256: str,
    source_file_count: int,
    source_total_bytes: int,
) -> dict[str, Any]:
    if not checkpoints_root.is_absolute() or not checkpoints_root.is_dir():
        raise VariantError("checkpoints root must be an existing absolute directory")
    source = checkpoints_root / SOURCE_ID / "versions" / VERSION
    destination_root = checkpoints_root / DESTINATION_ID
    destination = destination_root / "versions" / VERSION
    temporary_root = checkpoints_root / f".{DESTINATION_ID}.building"
    temporary_destination = temporary_root / "versions" / VERSION
    if destination_root.exists() or temporary_root.exists():
        raise VariantError("refusing to overwrite destination or staging directory")
    if SHA256.fullmatch(source_manifest_sha256) is None:
        raise VariantError("source manifest digest is invalid")
    members, total = inventory(source)
    if len(members) != source_file_count or total != source_total_bytes:
        raise VariantError(
            f"source inventory mismatch: files={len(members)} bytes={total}"
        )
    manifest_path = source / "manifest.yaml"
    if manifest_path not in members:
        raise VariantError("source artifact has no manifest.yaml")
    source_manifest = manifest_path.read_bytes()
    if digest(source_manifest) != source_manifest_sha256:
        raise VariantError("source manifest digest does not match the reviewed capture")
    destination_manifest = replace_manifest(source_manifest)
    linked = 0
    linked_bytes = 0
    try:
        temporary_destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for member in members:
            if member.name == "manifest.yaml":
                continue
            destination_member = temporary_destination / member.name
            os.link(member, destination_member, follow_symlinks=False)
            if member.stat().st_ino != destination_member.stat().st_ino:
                raise VariantError(f"payload was not hard-linked: {member.name}")
            linked += 1
            linked_bytes += member.stat().st_size
        write_exclusive(temporary_destination / "manifest.yaml", destination_manifest)
        rootfs = temporary_destination / "rootfs-diff.tar"
        if not rootfs.is_file() or rootfs.is_symlink() or rootfs.stat().st_size <= 0:
            raise VariantError("buffered artifact lacks a nonempty rootfs-diff.tar")
        if digest(manifest_path.read_bytes()) != source_manifest_sha256:
            raise VariantError("source manifest changed during construction")
        os.rename(temporary_root, destination_root)
        parent_descriptor = os.open(checkpoints_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    published_members, published_bytes = inventory(destination)
    published_manifest = (destination / "manifest.yaml").read_bytes()
    if b"imageIoMode: buffered" not in published_manifest or b"imageIoMode: direct" in published_manifest:
        raise VariantError("published artifact does not select buffered I/O")
    return {
        "schema": "archvteams.nebius.ai/evo2-buffered-artifact-build/v1",
        "status": "PASS",
        "source_checkpoint_id": SOURCE_ID,
        "checkpoint_id": DESTINATION_ID,
        "artifact_version": VERSION,
        "source_manifest_sha256": source_manifest_sha256,
        "manifest_sha256": digest(published_manifest),
        "image_io_mode": "buffered",
        "regular_file_count": len(published_members),
        "regular_bytes": published_bytes,
        "hardlinked_payload_file_count": linked,
        "hardlinked_payload_bytes": linked_bytes,
        "rootfs_diff_bytes": (destination / "rootfs-diff.tar").stat().st_size,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints-root", type=Path, required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--source-file-count", type=int, required=True)
    parser.add_argument("--source-total-bytes", type=int, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = build_variant(
            args.checkpoints_root,
            args.source_manifest_sha256,
            args.source_file_count,
            args.source_total_bytes,
        )
        payload = (
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("ascii")
        write_exclusive(args.receipt, payload)
        sys.stdout.buffer.write(payload)
        return 0
    except (VariantError, OSError) as exc:
        print(f"evo2-artifact-variant: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
