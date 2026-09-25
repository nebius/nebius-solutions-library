#!/usr/bin/env python3
"""Offline tests for the write-once buffered artifact renderer."""

from __future__ import annotations

import importlib.util
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
        "schema": "archvteams.nebius.ai/openfold3-native-artifact-receipt/v1",
        "checkpoint_id": module.SOURCE_ID,
        "artifact_version": "1",
        "source_node": module.NODE,
        "regular_file_count": 154,
        "regular_file_bytes": 9_346_630_368,
        "prewarm_bytes": 9_346_630_368,
        "tree_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "image_io_mode": "direct",
    }


class BufferedVariantTests(unittest.TestCase):
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
        compile(script, "<buffered-builder>", "exec")
        self.assertIn('old_mode = b"        imageIoMode: direct', script)
        self.assertIn('new_mode = b"        imageIoMode: buffered', script)
        self.assertIn("os.link(source, destination", script)
        self.assertIn("refusing to overwrite", script)
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


if __name__ == "__main__":
    unittest.main()
