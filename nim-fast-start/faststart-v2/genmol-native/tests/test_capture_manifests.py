#!/usr/bin/env python3
"""Cross-file contract tests for the GenMol native capture lane."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "nvcr.io/nim/nvidia/genmol@sha256:"
    "139b909a450fe1fb81198214784a15f67e172e766a93a1569827ba5aa05b4541"
)
VALIDATOR_SHA256 = "f85da2029aaa459d687983e5ebeec6c69dffb19a66f34c084409fe2ccc2efad4"
FIXTURE_SHA256 = "3065261de604f495a2fbae1e7fd92488546ee51f2729e5d40e9be5ee2c22f444"
WORKER_IMAGE = (
    "registry.example.invalid/faststart/"
    "snapshot-agent@sha256:"
    "d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28"
)


def documents(name: str) -> list[dict]:
    with (ROOT / name).open(encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


class CaptureManifestTests(unittest.TestCase):
    def test_input_digests_and_donor_warmup_are_exact(self) -> None:
        validator = (ROOT / "validate_genmol.py").read_bytes()
        fixture = (ROOT / "fixtures" / "requests-qed-logp.json").read_bytes()
        self.assertEqual(hashlib.sha256(validator).hexdigest(), VALIDATOR_SHA256)
        self.assertEqual(hashlib.sha256(fixture).hexdigest(), FIXTURE_SHA256)

        donor = documents("donor-job.yaml")[0]
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/validator-sha256"], VALIDATOR_SHA256)
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/fixture-sha256"], FIXTURE_SHA256)
        container = donor["spec"]["template"]["spec"]["containers"][0]
        script = container["args"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertNotIn("pip uninstall", script)
        self.assertEqual(script.count("--run-id native-f7-donor-"), 2)
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "12", "memory": "128Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "12", "memory": "128Gi", "nvidia.com/gpu": "1"},
            },
        )
        volumes = {item["name"]: item for item in donor["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["dshm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "16Gi"})

    def test_storage_and_artifact_modes_are_isolated(self) -> None:
        storage = documents("storage.yaml")
        self.assertEqual(len(storage), 2)
        claims = {item["metadata"]["name"]: item for item in storage}
        self.assertEqual(
            claims["genmol-native-f7-artifacts"]["spec"]["resources"]["requests"]["storage"],
            "93Gi",
        )
        self.assertEqual(
            claims["genmol-native-f7-artifacts"]["spec"]["storageClassName"],
            "mlspec-archvteams-2407-io-m3",
        )
        self.assertEqual(
            claims["genmol-native-f7-cache"]["spec"]["resources"]["requests"]["storage"],
            "10Gi",
        )
        self.assertEqual(
            claims["genmol-native-f7-cache"]["spec"]["storageClassName"],
            "compute-csi-default-sc",
        )

        direct = documents("artifact-holder.yaml")[0]
        buffered = documents("artifact-holder-buffered.yaml")[0]
        direct_script = direct["spec"]["containers"][0]["args"][0]
        buffered_script = buffered["spec"]["containers"][0]["args"][0]
        self.assertIn('"checkpoint_id": "genmol-native-f7-v1"', direct_script)
        self.assertIn('"image_io_mode": "direct"', direct_script)
        self.assertIn('"checkpoint_id": "genmol-native-f7-v2-buffered"', buffered_script)
        self.assertIn('"image_io_mode": "buffered"', buffered_script)
        self.assertNotEqual(direct["metadata"]["name"], buffered["metadata"]["name"])
        self.assertEqual(direct["metadata"]["name"], "genmol-native-f7-holder-t12-v2")
        for holder in (direct, buffered):
            context = holder["spec"]["containers"][0]["securityContext"]
            self.assertFalse(context["runAsNonRoot"])
            self.assertEqual(context["runAsUser"], 0)
            self.assertEqual(context["runAsGroup"], 0)
            self.assertTrue(context["readOnlyRootFilesystem"])
            self.assertEqual(context["capabilities"], {"drop": ["ALL"]})

    def test_worker_image_has_one_release_contract_source(self) -> None:
        contract = json.loads((ROOT / "dynamo" / "restore-interface.live.json").read_text())
        template = (ROOT / "snapshot-agent.yaml.tmpl").read_text()
        self.assertEqual(template.count("@@WORKER_IMAGE@@"), 1)
        self.assertNotIn(contract["worker_image"], template)
        self.assertEqual(contract["worker_image"], WORKER_IMAGE)
        self.assertEqual(contract["probe_image"], IMAGE)
        self.assertEqual(contract["probe_executable"], "/usr/bin/python3")
        self.assertEqual(
            contract["worker_executable_sha256"],
            "941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651",
        )
        self.assertFalse(contract["release_ready"])
        self.assertEqual(contract["worker_classification"], "performance-validation-only")
        self.assertEqual(
            contract["supported_image_io_modes"],
            ["direct", "writeback", "buffered"],
        )
        self.assertEqual(
            contract["source"]["materialized_tree_sha256"],
            "76838bc28fa641ba3d3165c1deb1f019c4f63ed9fce9571b38194ff65ef7b816",
        )
        self.assertEqual(len(contract["source"]["patch_inputs"]), 8)
        self.assertEqual(
            contract["source"]["patch_inputs"]["ns_bind_mount_glibc35"],
            "4847d7d42aae570fc7f91351a8fbf3018f10dc6247d93c2c9696754861731366",
        )
        self.assertEqual(
            contract["tool_bundle"]["content_sha256"],
            "c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e",
        )
        self.assertEqual(contract["tool_bundle"]["regular_files"], 34)
        self.assertEqual(
            contract["tool_bundle"]["glibc_compatibility_sha256"],
            "f7af5b214cb963c4cf64910dfafe16987f0c5ec886af5d0e5d7aab5b634f6950",
        )
        self.assertNotEqual(contract["release_blocker"], "")


if __name__ == "__main__":
    unittest.main()
