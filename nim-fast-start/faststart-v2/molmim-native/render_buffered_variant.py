#!/usr/bin/env python3
"""Render a write-once direct-to-buffered MolMIM artifact variant Job."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import yaml


SOURCE_ID = "molmim-native-f7-v1"
DESTINATION_ID = "molmim-native-f7-v2-buffered"
NODE = "gpu-node-b.example.invalid"
PVC = "molmim-native-f7-artifacts"
PYTHON_IMAGE = (
    "docker.io/library/python@sha256:"
    "356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VariantError(ValueError):
    """The source receipt cannot authorize a buffered variant build."""


def read_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VariantError(f"cannot read artifact receipt: {type(exc).__name__}") from exc
    expected_keys = {
        "schema",
        "status",
        "checkpoint_id",
        "artifact_version",
        "source_node",
        "regular_file_count",
        "regular_file_bytes",
        "unique_bytes",
        "prewarm_bytes",
        "full_read_elapsed_seconds",
        "tree_sha256",
        "manifest_sha256",
        "image_io_mode",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise VariantError("artifact receipt has the wrong shape")
    if (
        value["schema"] != "archvteams.nebius.ai/molmim-native-artifact-receipt/v1"
        or value["status"] != "PASS"
        or value["checkpoint_id"] != SOURCE_ID
        or value["artifact_version"] != "1"
        or value["source_node"] != NODE
        or value["image_io_mode"] != "direct"
    ):
        raise VariantError("artifact receipt does not identify the exact direct source")
    count = value["regular_file_count"]
    size = value["regular_file_bytes"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 20
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1_000_000_000
        or value["prewarm_bytes"] != size
        or value["unique_bytes"] != size
        or isinstance(value["full_read_elapsed_seconds"], bool)
        or not isinstance(value["full_read_elapsed_seconds"], (int, float))
        or not math.isfinite(float(value["full_read_elapsed_seconds"]))
        or value["full_read_elapsed_seconds"] <= 0
    ):
        raise VariantError("artifact receipt does not prove a complete prewarm")
    for field in ("tree_sha256", "manifest_sha256"):
        if not isinstance(value[field], str) or not SHA256.fullmatch(value[field]):
            raise VariantError(f"artifact receipt {field} is invalid")
    return value


def build_script(
    receipt: dict[str, Any], *, checkpoints_root: str = "/checkpoints"
) -> str:
    return f'''import hashlib
import json
import os
import pathlib
import shutil
import stat

SOURCE_ID = {SOURCE_ID!r}
DESTINATION_ID = {DESTINATION_ID!r}
EXPECTED_MANIFEST_SHA256 = {receipt["manifest_sha256"]!r}
EXPECTED_TREE_SHA256 = {receipt["tree_sha256"]!r}
EXPECTED_FILE_COUNT = {receipt["regular_file_count"]}
EXPECTED_BYTES = {receipt["regular_file_bytes"]}
CHECKPOINTS = pathlib.Path({checkpoints_root!r})
SOURCE = CHECKPOINTS / SOURCE_ID / "versions" / "1"
DESTINATION_ROOT = CHECKPOINTS / DESTINATION_ID
DESTINATION = DESTINATION_ROOT / "versions" / "1"
TEMP_ROOT = CHECKPOINTS / f".{{DESTINATION_ID}}.building"
TEMP_DESTINATION = TEMP_ROOT / "versions" / "1"

def digest(data):
    return hashlib.sha256(data).hexdigest()

def source_tree_receipt():
    members = sorted(SOURCE.iterdir(), key=lambda item: item.name)
    if len(members) != EXPECTED_FILE_COUNT:
        raise SystemExit("source artifact file count changed")
    total = 0
    tree = hashlib.sha256()
    for member in members:
        mode = member.lstat().st_mode
        if member.is_symlink() or not stat.S_ISREG(mode):
            raise SystemExit(f"source artifact member is not regular: {{member.name}}")
        size = member.stat().st_size
        total += size
        member_digest = hashlib.sha256()
        with member.open("rb", buffering=0) as source_file:
            while block := source_file.read(8 * 1024 * 1024):
                member_digest.update(block)
        tree.update(
            f"{{member.name}}\\0{{size}}\\0{{member_digest.hexdigest()}}\\n".encode("utf-8")
        )
    return members, total, tree.hexdigest()

if DESTINATION_ROOT.exists() or TEMP_ROOT.exists():
    raise SystemExit("refusing to overwrite destination or staging directory")
members, total, tree_sha256 = source_tree_receipt()
if total != EXPECTED_BYTES:
    raise SystemExit("source artifact byte count changed")
if tree_sha256 != EXPECTED_TREE_SHA256:
    raise SystemExit("source artifact tree digest changed")
source_manifest = (SOURCE / "manifest.yaml").read_bytes()
if digest(source_manifest) != EXPECTED_MANIFEST_SHA256:
    raise SystemExit("source manifest digest changed")
old_id = f"checkpointId: {{SOURCE_ID}}\\n".encode()
new_id = f"checkpointId: {{DESTINATION_ID}}\\n".encode()
old_mode = b"        imageIoMode: direct\\n"
new_mode = b"        imageIoMode: buffered\\n"
if source_manifest.count(old_id) != 1 or source_manifest.count(old_mode) != 1:
    raise SystemExit("source manifest identity or direct I/O marker is not exact")

try:
    TEMP_DESTINATION.mkdir(mode=0o700, parents=True, exist_ok=False)
    linked = 0
    linked_bytes = 0
    for source in members:
        if source.name == "manifest.yaml":
            continue
        destination = TEMP_DESTINATION / source.name
        os.link(source, destination, follow_symlinks=False)
        if source.stat().st_ino != destination.stat().st_ino:
            raise RuntimeError(f"artifact member was not hard-linked: {{source.name}}")
        linked += 1
        linked_bytes += source.stat().st_size
    destination_manifest = source_manifest.replace(old_id, new_id, 1).replace(
        old_mode, new_mode, 1
    )
    descriptor = os.open(
        TEMP_DESTINATION / "manifest.yaml",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(destination_manifest)
        output.flush()
        os.fsync(output.fileno())
    _, current_total, current_tree_sha256 = source_tree_receipt()
    if current_total != EXPECTED_BYTES or current_tree_sha256 != EXPECTED_TREE_SHA256:
        raise RuntimeError("source artifact tree changed during variant construction")
    os.rename(TEMP_ROOT, DESTINATION_ROOT)
    parent = os.open(CHECKPOINTS, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
except BaseException:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    raise

published = (DESTINATION / "manifest.yaml").read_bytes()
files = list(DESTINATION.iterdir())
regular_bytes = sum(path.stat().st_size for path in files)
if published.count(new_mode) != 1 or old_mode in published:
    raise SystemExit("published manifest does not select buffered image I/O")
print(json.dumps({{
    "schema": "archvteams.nebius.ai/molmim-buffered-build/v1",
    "status": "PASS",
    "checkpoint_id": DESTINATION_ID,
    "artifact_version": "1",
    "image_io_mode": "buffered",
    "manifest_sha256": digest(published),
    "regular_file_count": len(files),
    "regular_file_bytes": regular_bytes,
    "hardlinked_payload_file_count": linked,
    "hardlinked_payload_bytes": linked_bytes,
}}, sort_keys=True, separators=(",", ":")))
'''


def render(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "app.kubernetes.io/name": "molmim",
        "app.kubernetes.io/component": "buffered-artifact-builder",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "molmim-native-f7-v2-buffered-build",
            "namespace": "nim-fast-start",
            "labels": labels,
            "annotations": {
                "archvteams.nebius.ai/source-manifest-sha256": receipt[
                    "manifest_sha256"
                ],
                "archvteams.nebius.ai/source-tree-sha256": receipt["tree_sha256"],
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "restartPolicy": "Never",
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": "kubernetes.io/hostname",
                                                "operator": "In",
                                                "values": [NODE],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "builder",
                            "image": PYTHON_IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/usr/local/bin/python3"],
                            "args": ["-c", build_script(receipt)],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "volumeMounts": [
                                {"name": "checkpoints", "mountPath": "/checkpoints"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "checkpoints",
                            "persistentVolumeClaim": {"claimName": PVC},
                        }
                    ],
                },
            },
        },
    }
    return [job]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        documents = render(read_receipt(args.artifact_receipt))
    except VariantError as exc:
        print(f"render_buffered_variant: refused: {exc}", file=sys.stderr)
        return 2
    yaml.safe_dump_all(documents, sys.stdout, explicit_start=True, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
