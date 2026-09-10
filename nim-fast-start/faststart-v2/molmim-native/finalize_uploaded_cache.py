#!/usr/bin/env python3
"""Verify the bounded uploaded MolMIM cache and write its immutable receipt."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


ROOT = Path("/cache")
RECEIPT_PATH = ROOT / ".molmim-cache-receipt.json"
EXPECTED = {
    "jit-molmim-h100-v1.tar": (
        2_908_160,
        "ded771b8aac405cf68127be27197f0221f421c3656ca5c7d324d990ad77ff97a",
    ),
    "models/molmim_v1.3/molmim_70m_24_3.nemo": (
        281_589_760,
        "10522c9db6018c355313f9f01a0edea2b021ddc0a5a22ae4540cbf5bdafbd1f5",
    ),
}
EXPECTED_BYTES = 284_497_920


def main() -> int:
    if RECEIPT_PATH.exists() or RECEIPT_PATH.is_symlink():
        raise SystemExit("cache receipt already exists; refusing overwrite")
    observed: dict[str, tuple[int, str]] = {}
    for directory, names, entries in os.walk(ROOT, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                raise SystemExit("uploaded cache contains a symlinked directory")
        for name in entries:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise SystemExit("uploaded cache contains a non-regular file")
            relative = path.relative_to(ROOT).as_posix()
            digest = hashlib.sha256()
            with path.open("rb", buffering=0) as stream:
                while block := stream.read(8 * 1024 * 1024):
                    digest.update(block)
            observed[relative] = (info.st_size, digest.hexdigest())
    if observed != EXPECTED:
        raise SystemExit("uploaded cache identity does not match the retained source")
    total = sum(size for size, _ in observed.values())
    if total != EXPECTED_BYTES:
        raise SystemExit("uploaded cache byte count is not exact")
    tree = hashlib.sha256()
    for relative, (size, digest) in sorted(observed.items()):
        tree.update(f"{relative}\0{size}\0{digest}\n".encode("utf-8"))
        os.chmod(ROOT / relative, 0o444)
    receipt = {
        "schema": "archvteams.nebius.ai/molmim-cache-receipt/v1",
        "source_host_path": "/snapshots/nim-caches/molmim",
        "transfer": "hash-verified-private-stream-v1",
        "regular_file_count": len(observed),
        "regular_file_bytes": total,
        "checkpoint_file_bytes": 281_589_760,
        "tree_sha256": tree.hexdigest(),
        "prewarm_bytes": total,
        "status": "PASS",
    }
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(
        RECEIPT_PATH,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    with os.fdopen(descriptor, "wb", buffering=0) as output:
        output.write(raw)
        os.fsync(output.fileno())
    print(raw.decode().rstrip(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
