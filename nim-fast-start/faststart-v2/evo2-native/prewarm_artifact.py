#!/usr/bin/env python3
"""Verify one Evo2 native artifact and optionally make all pages resident."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
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
    started_at = datetime.now(timezone.utc)
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
    finished_at = datetime.now(timezone.utc)
    return {
        "schema": "archvteams.nebius.ai/evo2-artifact-holder/v1",
        "status": "PASS",
        "artifact_root": str(root),
        "image_io_mode": mode,
        "manifest_sha256": observed_manifest_sha256,
        "regular_file_count": len(members),
        "regular_bytes": observed_bytes,
        "payload_read": mode == "buffered",
        "aggregate_sha256": aggregate.hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "started_at": started_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "finished_at": finished_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--file-count", type=int, required=True)
    parser.add_argument("--total-bytes", type=int, required=True)
    parser.add_argument("--mode", choices=("direct", "buffered"), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ready-marker", type=Path, required=True)
    parser.add_argument("--hold", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = verify_and_prewarm(
            args.root, args.manifest_sha256, args.file_count, args.total_bytes, args.mode
        )
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
        print(f"evo2-artifact-holder: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
