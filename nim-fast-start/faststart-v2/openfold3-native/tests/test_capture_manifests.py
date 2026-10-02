#!/usr/bin/env python3
"""Cross-file contract tests for the OpenFold3 native capture lane."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "nvcr.io/nim/openfold/openfold3@sha256:"
    "6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2"
)
VALIDATOR_SHA256 = "679b3e027b18e78b4646569e8c6395fb5f62c4647704bb5089aa2385a20d11f5"
FIXTURE_SHA256 = "09b30bf2132e3764f99d4f417b47713cd6350bd332fe3100cceb1be11589f8ae"
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
        validator = (ROOT / "validate_openfold3.py").read_bytes()
        fixture = (ROOT / "fixtures" / "request-20aa.json").read_bytes()
        self.assertEqual(hashlib.sha256(validator).hexdigest(), VALIDATOR_SHA256)
        self.assertEqual(hashlib.sha256(fixture).hexdigest(), FIXTURE_SHA256)

        donor = documents("donor-job.yaml")[0]
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/validator-sha256"], VALIDATOR_SHA256)
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/fixture-sha256"], FIXTURE_SHA256)
        container = donor["spec"]["template"]["spec"]["containers"][0]
        script = container["args"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertIn("python3 -m pip uninstall -y uvloop", script)
        self.assertEqual(script.count("--run-id native-f7-donor-"), 2)
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "10", "memory": "120Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "11", "memory": "180Gi", "nvidia.com/gpu": "1"},
            },
        )
        volumes = {item["name"]: item for item in donor["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["dshm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "64Gi"})

    def test_storage_and_artifact_modes_are_isolated(self) -> None:
        storage = documents("storage.yaml")
        self.assertEqual(len(storage), 2)
        claims = {item["metadata"]["name"]: item for item in storage}
        self.assertEqual(
            claims["openfold3-native-f7-artifacts"]["spec"]["resources"]["requests"]["storage"],
            "93Gi",
        )
        self.assertEqual(
            claims["openfold3-native-f7-artifacts"]["spec"]["storageClassName"],
            "mlspec-archvteams-2407-io-m3",
        )
        self.assertEqual(
            claims["openfold3-native-f7-cache"]["spec"]["resources"]["requests"]["storage"],
            "50Gi",
        )
        self.assertEqual(
            claims["openfold3-native-f7-cache"]["spec"]["storageClassName"],
            "compute-csi-default-sc",
        )

        direct = documents("artifact-holder.yaml")[0]
        buffered = documents("artifact-holder-buffered.yaml")[0]
        direct_script = direct["spec"]["containers"][0]["args"][0]
        buffered_script = buffered["spec"]["containers"][0]["args"][0]
        self.assertIn('"checkpoint_id": "openfold3-native-f7-v1"', direct_script)
        self.assertIn('"image_io_mode": "direct"', direct_script)
        self.assertIn('"checkpoint_id": "openfold3-native-f7-v2-buffered"', buffered_script)
        self.assertIn('"image_io_mode": "buffered"', buffered_script)
        self.assertNotEqual(direct["metadata"]["name"], buffered["metadata"]["name"])
        for holder in (direct, buffered):
            security = holder["spec"]["containers"][0]["securityContext"]
            self.assertEqual(security["runAsUser"], 0)
            self.assertEqual(security["runAsGroup"], 0)
            self.assertFalse(security["runAsNonRoot"])
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_worker_image_has_one_release_contract_source(self) -> None:
        contract = json.loads((ROOT / "dynamo" / "restore-interface.live.json").read_text())
        template = (ROOT / "snapshot-agent.yaml.tmpl").read_text()
        self.assertEqual(template.count("@@WORKER_IMAGE@@"), 1)
        self.assertNotIn(contract["worker_image"], template)
        self.assertEqual(contract["worker_image"], WORKER_IMAGE)
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
