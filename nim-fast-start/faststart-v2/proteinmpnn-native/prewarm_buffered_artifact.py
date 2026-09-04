#!/usr/bin/env python3
import hashlib
import json
import pathlib
import stat
import time


ROOT = pathlib.Path("/checkpoints/proteinmpnn-native-f7-v3-buffered/versions/1")
EXPECTED_FILE_COUNT = 57
EXPECTED_BYTES = 1_867_046_505
EXPECTED_MANIFEST_SHA256 = "6a298ceefc93b259e5ec7e6c1e74ae3ab43cdd9a757bee1934923dbfcdc06c07"

started = time.monotonic()
members = sorted(ROOT.iterdir(), key=lambda member: member.name)
aggregate = hashlib.sha256()
total = 0
for member in members:
    metadata = member.lstat()
    if member.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"non-regular artifact member: {member.name}")
    aggregate.update(member.name.encode("utf-8") + b"\0")
    size = 0
    with member.open("rb", buffering=8 * 1024 * 1024) as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            aggregate.update(chunk)
            size += len(chunk)
    if size != metadata.st_size:
        raise SystemExit(f"short read for {member.name}: {size} != {metadata.st_size}")
    aggregate.update(size.to_bytes(8, "big"))
    total += size

if len(members) != EXPECTED_FILE_COUNT or total != EXPECTED_BYTES:
    raise SystemExit(
        f"artifact inventory mismatch: files={len(members)} bytes={total}"
    )
manifest_sha256 = hashlib.sha256((ROOT / "manifest.yaml").read_bytes()).hexdigest()
if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
    raise SystemExit(f"manifest digest mismatch: {manifest_sha256}")

print(
    json.dumps(
        {
            "status": "PASS",
            "checkpoint_id": "proteinmpnn-native-f7-v3-buffered",
            "version": "1",
            "regular_file_count": len(members),
            "regular_bytes_read": total,
            "manifest_sha256": manifest_sha256,
            "aggregate_content_sha256": aggregate.hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 6),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
