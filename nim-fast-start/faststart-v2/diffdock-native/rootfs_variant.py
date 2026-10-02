#!/usr/bin/env python3
"""Inspect and, only for an exact eligible delta, build a rootfsless artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


SOURCE_ID = "diffdock-native-f7-v1"
DESTINATION_ID = "diffdock-native-f7-v2-rootfsless"
VERSION = "1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
NVCR_CONF = re.compile(r"etc/ld\.so\.conf\.d/(?:00|zz)-nvcr-[0-9]+\.conf$")
CUDA_COMPAT_MARKER = re.compile(
    r"usr/local/cuda-[0-9.]+/compat/\.[0-9.]+\.[a-z0-9-]+\.checked$"
)
NVIDIA_LIBRARY = re.compile(
    r"usr/lib/x86_64-linux-gnu/(?:"
    r"libcuda|libcudadebugger|libnvcuvid|libnvidia-[a-z0-9-]+"
    r")\.so(?:\.[0-9]+(?:\.[0-9]+)*)?$"
)
NVIDIA_VDPAU = re.compile(
    r"usr/lib/x86_64-linux-gnu/vdpau/libvdpau_nvidia\.so"
    r"(?:\.[0-9]+(?:\.[0-9]+)*)?$"
)


class VariantError(ValueError):
    """The artifact is not eligible for an immutable rootfsless variant."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _artifact_root(checkpoints: Path, checkpoint_id: str) -> Path:
    return checkpoints / checkpoint_id / "versions" / VERSION


def _normalize_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return "."
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or "." in value.parts:
        raise VariantError(f"rootfs delta contains a non-canonical member: {name!r}")
    normalized = value.as_posix()
    if normalized.startswith("/") or normalized == "..":  # defensive
        raise VariantError(f"rootfs delta member escapes the root: {name!r}")
    return normalized


def _classify_file(name: str) -> str | None:
    if name == "etc/ld.so.cache" or name == "var/cache/ldconfig/aux-cache":
        return "runtime-ldconfig-state"
    if NVCR_CONF.fullmatch(name):
        return "runtime-ldconfig-state"
    if name.startswith("run/nvidia-"):
        return "nvidia-container-runtime"
    if name == "usr/bin/nvidia-smi" or name.startswith("usr/bin/nvidia-"):
        return "nvidia-container-runtime"
    if CUDA_COMPAT_MARKER.fullmatch(name):
        return "nvidia-container-runtime"
    if name.startswith("usr/lib/firmware/nvidia/"):
        return "nvidia-container-runtime"
    if NVIDIA_LIBRARY.fullmatch(name) or NVIDIA_VDPAU.fullmatch(name):
        return "nvidia-container-runtime"
    return None


def inspect(checkpoints: Path) -> dict[str, Any]:
    source = _artifact_root(checkpoints, SOURCE_ID)
    if source.is_symlink() or not source.is_dir():
        raise VariantError("source artifact root is absent or symlinked")
    manifest = source / "manifest.yaml"
    rootfs = source / "rootfs-diff.tar"
    for path, label in ((manifest, "manifest"), (rootfs, "rootfs delta")):
        if path.is_symlink() or not path.is_file():
            raise VariantError(f"source {label} is not a regular non-symlink file")
    manifest_bytes = manifest.read_bytes()
    marker = f"checkpointId: {SOURCE_ID}\n".encode("utf-8")
    if not manifest_bytes.startswith(marker) or manifest_bytes.count(marker) != 1:
        raise VariantError("source manifest checkpoint identity is not exact")

    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with tarfile.open(rootfs, mode="r:*") as archive:
            for member in archive:
                name = _normalize_member(member.name)
                if name in seen:
                    raise VariantError(f"rootfs delta contains duplicate member {name!r}")
                seen.add(name)
                if member.isdir():
                    category = "directory-metadata-only"
                else:
                    category = _classify_file(name)
                linkname = member.linkname or ""
                if member.issym() or member.islnk():
                    if not linkname or PurePosixPath(linkname).is_absolute() or ".." in PurePosixPath(linkname).parts:
                        category = None
                members.append(
                    {
                        "category": category or "unclassified",
                        "linkname": linkname,
                        "name": name,
                        "size": member.size,
                        "type": member.type.decode("latin-1"),
                    }
                )
    except (OSError, tarfile.TarError) as exc:
        raise VariantError(f"cannot inspect rootfs delta: {type(exc).__name__}") from exc

    unclassified = sorted(
        item["name"] for item in members if item["category"] == "unclassified"
    )
    return {
        "schema": "archvteams.nebius.ai/diffdock-rootfs-review/v1",
        "source_checkpoint_id": SOURCE_ID,
        "artifact_version": VERSION,
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "rootfs_diff_sha256": _sha256_file(rootfs),
        "rootfs_diff_bytes": rootfs.stat().st_size,
        "member_count": len(members),
        "members": members,
        "unclassified_members": unclassified,
        "eligible_for_rootfsless_candidate": not unclassified,
        "decision_scope": (
            "candidate-only; requires a strict two-call restore canary before measured use"
        ),
    }


def _validate_expected_digest(value: str, label: str) -> str:
    if not SHA256.fullmatch(value):
        raise VariantError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def build(
    checkpoints: Path,
    expected_source_manifest_sha256: str,
    expected_review_sha256: str,
) -> dict[str, Any]:
    _validate_expected_digest(expected_source_manifest_sha256, "source manifest digest")
    _validate_expected_digest(expected_review_sha256, "rootfs review digest")
    review = inspect(checkpoints)
    review_bytes = _canonical(review)
    if hashlib.sha256(review_bytes).hexdigest() != expected_review_sha256:
        raise VariantError("rootfs review digest changed since the reviewed inspection")
    if review["source_manifest_sha256"] != expected_source_manifest_sha256:
        raise VariantError("source manifest digest changed since artifact verification")
    if review["eligible_for_rootfsless_candidate"] is not True:
        raise VariantError("rootfs delta contains unclassified members")

    source = _artifact_root(checkpoints, SOURCE_ID)
    destination_root = checkpoints / DESTINATION_ID
    destination = _artifact_root(checkpoints, DESTINATION_ID)
    temporary_root = checkpoints / f".{DESTINATION_ID}.building"
    temporary = _artifact_root(checkpoints, f".{DESTINATION_ID}.building")
    if destination_root.exists() or destination_root.is_symlink():
        raise VariantError("refusing to overwrite the immutable destination artifact")
    if temporary_root.exists() or temporary_root.is_symlink():
        raise VariantError("refusing to reuse an existing staging artifact")

    source_manifest = (source / "manifest.yaml").read_bytes()
    old_line = f"checkpointId: {SOURCE_ID}\n".encode("utf-8")
    new_line = f"checkpointId: {DESTINATION_ID}\n".encode("utf-8")
    linked_count = 0
    linked_bytes = 0
    try:
        temporary.mkdir(mode=0o700, parents=True, exist_ok=False)
        for member in sorted(source.iterdir(), key=lambda item: item.name):
            mode = member.lstat().st_mode
            if member.is_symlink() or not stat.S_ISREG(mode):
                raise VariantError(f"unexpected non-regular artifact member: {member.name}")
            if member.name in {"manifest.yaml", "rootfs-diff.tar"}:
                continue
            destination_member = temporary / member.name
            os.link(member, destination_member, follow_symlinks=False)
            if member.stat().st_ino != destination_member.stat().st_ino:
                raise VariantError(f"artifact member was not hard-linked: {member.name}")
            linked_count += 1
            linked_bytes += member.stat().st_size

        destination_manifest = source_manifest.replace(old_line, new_line, 1)
        manifest_path = temporary / "manifest.yaml"
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(destination_manifest)
            output.flush()
            os.fsync(output.fileno())
        review_path = temporary / "rootfsless-review.json"
        descriptor = os.open(
            review_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(review_bytes + b"\n")
            output.flush()
            os.fsync(output.fileno())
        if (temporary / "rootfs-diff.tar").exists():
            raise VariantError("rootfs delta unexpectedly exists in the candidate")
        os.rename(temporary_root, destination_root)
        parent_descriptor = os.open(checkpoints, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise

    manifest_sha256 = _sha256_file(destination / "manifest.yaml")
    return {
        "schema": "archvteams.nebius.ai/diffdock-rootfsless-build/v1",
        "status": "PASS",
        "source_checkpoint_id": SOURCE_ID,
        "checkpoint_id": DESTINATION_ID,
        "artifact_version": VERSION,
        "source_manifest_sha256": expected_source_manifest_sha256,
        "source_rootfs_review_sha256": expected_review_sha256,
        "manifest_sha256": manifest_sha256,
        "hardlinked_payload_file_count": linked_count,
        "hardlinked_payload_bytes": linked_bytes,
        "rootfs_diff_present": False,
        "validation_required": "strict-two-call-ClusterIP-canary",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, default=Path("/checkpoints"))
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("inspect")
    builder = subparsers.add_parser("build")
    builder.add_argument("--expected-source-manifest-sha256", required=True)
    builder.add_argument("--expected-review-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "inspect":
            receipt = inspect(args.checkpoints)
        else:
            receipt = build(
                args.checkpoints,
                args.expected_source_manifest_sha256,
                args.expected_review_sha256,
            )
    except (OSError, VariantError) as exc:
        print(f"rootfs-variant: refused: {exc}", file=sys.stderr)
        return 2
    print(_canonical(receipt).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
