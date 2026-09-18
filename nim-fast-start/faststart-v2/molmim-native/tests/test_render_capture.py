#!/usr/bin/env python3
"""Tests for UID-bound MolMIM capture rendering."""

from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "render_capture.py"
SPEC = importlib.util.spec_from_file_location("render_capture", SOURCE)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


JOB_UID = "22222222-2222-4222-8222-222222222222"


def donor_job() -> dict:
    value = yaml.safe_load((ROOT / "donor-job.yaml").read_text(encoding="utf-8"))
    value["metadata"]["uid"] = JOB_UID
    return value


def donor() -> dict:
    template = donor_job()["spec"]["template"]
    pod_spec = copy.deepcopy(template["spec"])
    pod_spec["nodeName"] = capture.NODE
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "molmim-native-f7-donor-r1-abc12",
            "namespace": capture.NAMESPACE,
            "uid": "11111111-1111-4111-8111-111111111111",
            "labels": copy.deepcopy(template["metadata"]["labels"]),
            "annotations": copy.deepcopy(template["metadata"]["annotations"]),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": capture.DONOR_JOB_NAME,
                    "uid": JOB_UID,
                    "controller": True,
                }
            ],
        },
        "spec": pod_spec,
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "molmim",
                    "imageID": capture.IMAGE,
                    "state": {"running": {"startedAt": "2026-08-18T00:00:00Z"}},
                }
            ],
        },
    }


class CaptureRenderTests(unittest.TestCase):
    def test_ready_exact_donor_renders_uid_bound_content(self) -> None:
        value = capture.render(donor(), donor_job())[0]
        source = value["spec"]["source"]
        self.assertEqual(source["nodeName"], capture.NODE)
        self.assertEqual(source["podRef"]["name"], "molmim-native-f7-donor-r1-abc12")
        self.assertEqual(
            source["podRef"]["uid"],
            "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(source["podRef"]["containers"], ["molmim"])

    def test_wrong_node_is_rejected(self) -> None:
        value = donor()
        value["spec"]["nodeName"] = "different-node.example.invalid"
        with self.assertRaisesRegex(capture.CaptureError, "exact namespace and H100"):
            capture.render(value, donor_job())

    def test_not_ready_is_rejected(self) -> None:
        value = donor()
        value["status"]["conditions"][0]["status"] = "False"
        with self.assertRaisesRegex(capture.CaptureError, "semantic warmups"):
            capture.render(value, donor_job())

    def test_wrong_image_is_rejected(self) -> None:
        value = donor()
        value["spec"]["containers"][0]["image"] = "nvcr.io/nim/nvidia/molmim:latest"
        with self.assertRaisesRegex(capture.CaptureError, "donor PodSpec"):
            capture.render(value, donor_job())

    def test_noncanonical_uid_is_rejected(self) -> None:
        value = donor()
        value["metadata"]["uid"] = "NOT-A-UID"
        with self.assertRaisesRegex(capture.CaptureError, "not a UUID"):
            capture.render(value, donor_job())

    def test_donor_command_or_readiness_mutation_is_rejected(self) -> None:
        value = donor()
        value["spec"]["containers"][0]["command"] = ["/bin/true"]
        with self.assertRaisesRegex(capture.CaptureError, "donor PodSpec"):
            capture.render(value, donor_job())

        value = donor()
        value["spec"]["containers"][0]["readinessProbe"]["exec"]["command"] = [
            "/bin/true"
        ]
        with self.assertRaisesRegex(capture.CaptureError, "donor PodSpec"):
            capture.render(value, donor_job())

    def test_donor_must_be_owned_by_exact_captured_job(self) -> None:
        value = donor()
        value["metadata"]["ownerReferences"][0]["uid"] = (
            "33333333-3333-4333-8333-333333333333"
        )
        with self.assertRaisesRegex(capture.CaptureError, "exact captured donor Job"):
            capture.render(value, donor_job())


if __name__ == "__main__":
    unittest.main()
