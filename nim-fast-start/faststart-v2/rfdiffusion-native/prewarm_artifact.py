#!/usr/bin/env python3
"""Verify one RFdiffusion native artifact and optionally make all pages resident."""

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
from typing import Any, Sequence


SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PrewarmError(ValueError):
    pass


def write_exclusive(path: Path, data: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise PrewarmError(f"refusing existing output: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def verify_and_prewarm(
    root: Path,
    manifest_sha256: str,
    file_count: int,
    total_bytes: int,
    mode: str,
) -> dict[str, Any]:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise PrewarmError("artifact root must be an existing absolute non-symlink directory")
    if SHA256.fullmatch(manifest_sha256) is None:
        raise PrewarmError("manifest digest is invalid")
    if mode not in {"direct", "buffered"}:
        raise PrewarmError("mode must be direct or buffered")
    started = time.monotonic()
    members = sorted(root.iterdir(), key=lambda item: item.name)
    observed_bytes = 0
    aggregate = hashlib.sha256()
    for member in members:
        metadata = member.lstat()
        if member.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise PrewarmError(f"artifact member is not regular: {member.name}")
        observed_bytes += metadata.st_size
        aggregate.update(member.name.encode("utf-8") + b"\0")
        aggregate.update(metadata.st_size.to_bytes(8, "big"))
        if mode == "buffered":
            observed = 0
            with member.open("rb", buffering=8 * 1024 * 1024) as stream:
                while chunk := stream.read(8 * 1024 * 1024):
                    aggregate.update(chunk)
                    observed += len(chunk)
            if observed != metadata.st_size:
                raise PrewarmError(f"short read for {member.name}")
    if len(members) != file_count or observed_bytes != total_bytes:
        raise PrewarmError(
            f"artifact inventory mismatch: files={len(members)} bytes={observed_bytes}"
        )
    manifest = (root / "manifest.yaml").read_bytes()
    observed_manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    if observed_manifest_sha256 != manifest_sha256:
        raise PrewarmError("manifest digest differs from the reviewed artifact")
    expected_mode = f"imageIoMode: {mode}".encode("ascii")
    if manifest.count(expected_mode) != 1:
        raise PrewarmError("manifest image I/O mode does not match the holder mode")
    return {
        "schema": "archvteams.nebius.ai/rfdiffusion-artifact-holder/v1",
        "status": "PASS",
        "artifact_root": str(root),
        "image_io_mode": mode,
        "manifest_sha256": observed_manifest_sha256,
        "regular_file_count": len(members),
        "regular_bytes": observed_bytes,
        "payload_read": mode == "buffered",
        "aggregate_sha256": aggregate.hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def verify_cache(
    root: Path,
    tree_sha256: str,
    file_count: int,
    total_bytes: int,
    required_relative_path: str,
) -> dict[str, Any]:
    started = time.monotonic()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise PrewarmError("cache root must be an existing absolute non-symlink directory")
    if SHA256.fullmatch(tree_sha256) is None:
        raise PrewarmError("cache tree digest is invalid")
    if (
        not required_relative_path
        or required_relative_path.startswith("/")
        or ".." in Path(required_relative_path).parts
    ):
        raise PrewarmError("required cache path must be a safe relative path")
    members: list[tuple[str, int, Path]] = []
    observed_bytes = 0
    symlink_count = 0
    resolved_root = root.resolve(strict=True)

    def validate_symlink(path: Path) -> None:
        nonlocal symlink_count
        try:
            target = path.resolve(strict=True)
            target.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise PrewarmError(f"cache symlink escapes or is broken: {path}") from exc
        if not target.is_file():
            raise PrewarmError(f"cache symlink target is not a regular file: {path}")
        symlink_count += 1

    for directory, names, entries in os.walk(root, followlinks=False):
        names.sort()
        entries.sort()
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                validate_symlink(path)
        for name in entries:
            path = Path(directory) / name
            metadata = path.lstat()
            if path.is_symlink():
                validate_symlink(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PrewarmError(f"cache member is not regular: {path}")
            relative = path.relative_to(root).as_posix()
            members.append((relative, metadata.st_size, path))
            observed_bytes += metadata.st_size
    members.sort(key=lambda item: item[0])
    if len(members) != file_count or observed_bytes != total_bytes:
        raise PrewarmError(
            f"cache inventory mismatch: files={len(members)} bytes={observed_bytes}"
        )
    required = root / required_relative_path
    if not required.is_file() or required.is_symlink() or required.stat().st_size <= 0:
        raise PrewarmError("required RFdiffusion IGSO cache member is absent")
    tree = hashlib.sha256()
    for relative, size, path in members:
        digest = hashlib.sha256()
        observed = 0
        with path.open("rb", buffering=8 * 1024 * 1024) as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
                observed += len(chunk)
        if observed != size:
            raise PrewarmError(f"short read for cache member {relative}")
        tree.update(f"{relative}\0{size}\0{digest.hexdigest()}\n".encode("utf-8"))
    observed_tree_sha256 = tree.hexdigest()
    if observed_tree_sha256 != tree_sha256:
        raise PrewarmError("cache tree digest differs from the retained reviewed cache")
    return {
        "status": "PASS",
        "tree_sha256": observed_tree_sha256,
        "regular_file_count": len(members),
        "regular_bytes": observed_bytes,
        "safe_internal_symlink_count": symlink_count,
        "required_relative_path": required_relative_path,
        "payload_read": True,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--file-count", type=int)
    parser.add_argument("--total-bytes", type=int)
    parser.add_argument("--mode", choices=("direct", "buffered"))
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--cache-tree-sha256", required=True)
    parser.add_argument("--cache-file-count", type=int, required=True)
    parser.add_argument("--cache-total-bytes", type=int, required=True)
    parser.add_argument("--required-cache-relative-path", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ready-marker", type=Path, required=True)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--hold", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cache = verify_cache(
            args.cache_root,
            args.cache_tree_sha256,
            args.cache_file_count,
            args.cache_total_bytes,
            args.required_cache_relative_path,
        )
        if args.cache_only:
            if any(
                item is not None
                for item in (
                    args.root,
                    args.manifest_sha256,
                    args.file_count,
                    args.total_bytes,
                    args.mode,
                )
            ):
                raise PrewarmError("cache-only verification must not accept artifact inputs")
            result = {
                "schema": "archvteams.nebius.ai/rfdiffusion-cache-verifier/v1",
                "status": "PASS",
                "cache": cache,
            }
        else:
            if any(
                item is None
                for item in (
                    args.root,
                    args.manifest_sha256,
                    args.file_count,
                    args.total_bytes,
                    args.mode,
                )
            ):
                raise PrewarmError("artifact holder inputs are required outside cache-only mode")
            result = verify_and_prewarm(
                args.root,
                args.manifest_sha256,
                args.file_count,
                args.total_bytes,
                args.mode,
            )
            result["cache"] = cache
        payload = (
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("ascii")
        write_exclusive(args.receipt, payload)
        write_exclusive(args.ready_marker, b"PASS\n")
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        if args.hold:
            while True:
                time.sleep(3600)
        return 0
    except (PrewarmError, OSError) as exc:
        print(f"rfdiffusion-artifact-holder: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
