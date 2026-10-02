#!/usr/bin/env python3
"""Tests for UID-bound MSA Search capture rendering."""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "render_capture.py"
SPEC = importlib.util.spec_from_file_location("render_capture", SOURCE)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


def donor() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "msa-search-native-f7-donor-r1-abc12",
            "namespace": capture.NAMESPACE,
            "uid": "11111111-1111-4111-8111-111111111111",
            "labels": {
                "app.kubernetes.io/name": "msa-search",
                "app.kubernetes.io/component": "checkpoint-donor",
                "nvidia.com/snapshot-is-checkpoint-source": "true",
                "nvidia.com/snapshot-checkpoint-id": "msa-search-native-f7-v1",
            },
            "annotations": {
                "nvidia.com/snapshot-artifact-version": "1",
                "nvidia.com/snapshot-target-containers": "msa-search",
                "nvidia.com/snapshot-storage-type": "pvc",
                "nvidia.com/snapshot-storage-base-path": "/checkpoints",
            },
        },
        "spec": {
            "nodeName": capture.NODE,
            "containers": [{"name": "msa-search", "image": capture.IMAGE}],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "msa-search",
                    "imageID": capture.IMAGE,
                    "state": {"running": {"startedAt": "2026-08-18T00:00:00Z"}},
                }
            ],
        },
    }


class CaptureRenderTests(unittest.TestCase):
    def test_ready_exact_donor_renders_uid_bound_content(self) -> None:
        value = capture.render(donor())[0]
        source = value["spec"]["source"]
        self.assertEqual(source["nodeName"], capture.NODE)
        self.assertEqual(source["podRef"]["name"], "msa-search-native-f7-donor-r1-abc12")
        self.assertEqual(
            source["podRef"]["uid"],
            "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(source["podRef"]["containers"], ["msa-search"])

    def test_wrong_node_is_rejected(self) -> None:
        value = donor()
        value["spec"]["nodeName"] = "different-node.example.invalid"
        with self.assertRaisesRegex(capture.CaptureError, "exact namespace and H100"):
            capture.render(value)

    def test_not_ready_is_rejected(self) -> None:
        value = donor()
        value["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(capture.CaptureError, "semantic warmups"):
            capture.render(value)

    def test_wrong_image_is_rejected(self) -> None:
        value = donor()
        value["spec"]["containers"][0]["image"] = "nvcr.io/nim/colabfold/msa-search:latest"
        with self.assertRaisesRegex(capture.CaptureError, "exact pinned"):
            capture.render(value)

    def test_noncanonical_uid_is_rejected(self) -> None:
        value = donor()
        value["metadata"]["uid"] = "NOT-A-UID"
        with self.assertRaisesRegex(capture.CaptureError, "not a UUID"):
            capture.render(value)


if __name__ == "__main__":
    unittest.main()
