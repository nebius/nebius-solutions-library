#!/usr/bin/env python3
"""Offline tests for the write-once buffered artifact renderer."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "render_buffered_variant.py"
SPEC = importlib.util.spec_from_file_location("render_buffered_variant", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def receipt() -> dict:
    return {
        "schema": "archvteams.nebius.ai/molmim-native-artifact-receipt/v1",
        "status": "PASS",
        "checkpoint_id": module.SOURCE_ID,
        "artifact_version": "1",
        "source_node": module.NODE,
        "regular_file_count": 154,
        "regular_file_bytes": 9_346_630_368,
        "unique_bytes": 9_346_630_368,
        "prewarm_bytes": 9_346_630_368,
        "full_read_elapsed_seconds": 4.25,
        "tree_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "image_io_mode": "direct",
    }


class BufferedVariantTests(unittest.TestCase):
    def _small_source(self, checkpoints: Path) -> tuple[dict, Path]:
        source = checkpoints / module.SOURCE_ID / "versions" / "1"
        source.mkdir(parents=True)
        manifest = (
            f"checkpointId: {module.SOURCE_ID}\n"
            "sourceNode: gpu-node-b.example.invalid\n"
            "        imageIoMode: direct\n"
        ).encode()
        (source / "manifest.yaml").write_bytes(manifest)
        for index in range(19):
            (source / f"payload-{index:02d}.img").write_bytes(
                f"payload-{index:02d}".encode()
            )

        members = sorted(source.iterdir(), key=lambda item: item.name)
        tree = hashlib.sha256()
        total = 0
        for member in members:
            data = member.read_bytes()
            total += len(data)
            tree.update(
                f"{member.name}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}\n".encode()
            )
        value = receipt()
        value.update(
            {
                "regular_file_count": len(members),
                "regular_file_bytes": total,
                "unique_bytes": total,
                "prewarm_bytes": total,
                "tree_sha256": tree.hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            }
        )
        return value, source

    def test_exact_receipt_renders_write_once_hardlink_builder(self) -> None:
        document = module.render(receipt())[0]
        self.assertEqual(document["kind"], "Job")
        pod = document["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(
            pod["affinity"]["nodeAffinity"]
            ["requiredDuringSchedulingIgnoredDuringExecution"]
            ["nodeSelectorTerms"][0]["matchExpressions"][0]["values"],
            [module.NODE],
        )
        container = pod["containers"][0]
        script = container["args"][1]
        self.assertIn('old_mode = b"        imageIoMode: direct', script)
        self.assertIn('new_mode = b"        imageIoMode: buffered', script)
        self.assertIn("os.link(source, destination", script)
        self.assertIn("refusing to overwrite", script)
        self.assertIn("EXPECTED_TREE_SHA256", script)
        self.assertEqual(
            document["metadata"]["annotations"][
                "archvteams.nebius.ai/source-tree-sha256"
            ],
            receipt()["tree_sha256"],
        )
        self.assertEqual(
            pod["volumes"][0]["persistentVolumeClaim"]["claimName"], module.PVC
        )

    def test_receipt_without_complete_prewarm_is_rejected(self) -> None:
        value = receipt()
        value["prewarm_bytes"] -= 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(module.VariantError, "complete prewarm"):
                module.read_receipt(path)

    def test_buffered_source_cannot_authorize_second_variant(self) -> None:
        value = receipt()
        value["image_io_mode"] = "buffered"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(module.VariantError, "exact direct source"):
                module.read_receipt(path)

    def test_same_size_source_tampering_is_rejected_by_tree_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            value, source = self._small_source(checkpoints)
            victim = source / "payload-00.img"
            victim.write_bytes(b"X" * victim.stat().st_size)
            script = module.build_script(value, checkpoints_root=str(checkpoints))
            with self.assertRaisesRegex(SystemExit, "tree digest changed"):
                exec(compile(script, "buffered-builder", "exec"), {})
            self.assertFalse((checkpoints / module.DESTINATION_ID).exists())

    def test_exact_small_source_executes_generated_builder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            value, source = self._small_source(checkpoints)
            script = module.build_script(value, checkpoints_root=str(checkpoints))
            exec(compile(script, "buffered-builder", "exec"), {})

            destination = (
                checkpoints / module.DESTINATION_ID / "versions" / "1"
            )
            self.assertTrue(destination.is_dir())
            published = (destination / "manifest.yaml").read_text()
            self.assertIn(f"checkpointId: {module.DESTINATION_ID}\n", published)
            self.assertIn("        imageIoMode: buffered\n", published)
            self.assertEqual(
                (source / "payload-00.img").stat().st_ino,
                (destination / "payload-00.img").stat().st_ino,
            )


if __name__ == "__main__":
    unittest.main()
