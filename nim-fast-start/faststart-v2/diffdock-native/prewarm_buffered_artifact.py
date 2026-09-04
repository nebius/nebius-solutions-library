#!/usr/bin/env python3
"""Re-read and hash every byte of the selected DiffDock buffered artifact."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path("/artifacts/diffdock-native-f7-v3-buffered/versions/1")
CHECKPOINT_ID = "diffdock-native-f7-v3-buffered"
EXPECTED_FILES = 122
EXPECTED_BYTES = 7_516_058_314
EXPECTED_MANIFEST = "93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe"
EXPECTED_TREE = "2d9e339392d6b4c5207ddbd4ef8f26465e324b2e165bd4cd9b43530f006e1b1d"


def _digest_file(item: tuple[str, int, Path]) -> tuple[str, int, str]:
    relative, size, path = item
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return relative, size, digest.hexdigest()


def main() -> int:
    started = time.monotonic()
    if ROOT.is_symlink() or not ROOT.is_dir():
        raise SystemExit("selected artifact root is absent or symlinked")
    files: list[tuple[str, int, Path]] = []
    for directory, names, entries in os.walk(ROOT, followlinks=False):
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise SystemExit("selected artifact contains a symlinked directory")
        for name in entries:
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit("selected artifact contains a non-regular file")
            files.append((path.relative_to(ROOT).as_posix(), info.st_size, path))
    if len(files) != EXPECTED_FILES or sum(item[1] for item in files) != EXPECTED_BYTES:
        raise SystemExit("selected artifact inventory does not match the frozen contract")
    with ThreadPoolExecutor(max_workers=4) as pool:
        hashed = sorted(pool.map(_digest_file, files))
    tree = hashlib.sha256()
    for relative, size, digest in hashed:
        tree.update(f"{relative}\0{size}\0{digest}\n".encode())
    manifest = hashlib.sha256((ROOT / "manifest.yaml").read_bytes()).hexdigest()
    if manifest != EXPECTED_MANIFEST or tree.hexdigest() != EXPECTED_TREE:
        raise SystemExit("selected artifact digest does not match the frozen contract")
    receipt = {
        "schema": "archvteams.nebius.ai/diffdock-full-read/v1",
        "status": "PASS",
        "checkpoint_id": CHECKPOINT_ID,
        "version": "1",
        "image_io_mode": "buffered",
        "regular_file_count": len(hashed),
        "regular_bytes_read": sum(item[1] for item in hashed),
        "manifest_sha256": manifest,
        "aggregate_content_sha256": tree.hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
