#!/usr/bin/env python3
"""Cross-file contract tests for the MolMIM native capture lane."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "nvcr.io/nim/nvidia/molmim@sha256:"
    "7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
)
VALIDATOR_SHA256 = "0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7"
FIXTURE_SHA256 = "053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d"
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
        validator = (ROOT / "validate_molmim.py").read_bytes()
        fixture = (ROOT / "fixtures" / "request-cmaes-qed.json").read_bytes()
        self.assertEqual(hashlib.sha256(validator).hexdigest(), VALIDATOR_SHA256)
        self.assertEqual(hashlib.sha256(fixture).hexdigest(), FIXTURE_SHA256)

        donor = documents("donor-job.yaml")[0]
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/validator-sha256"], VALIDATOR_SHA256)
        self.assertEqual(donor["metadata"]["annotations"]["archvteams.nebius.ai/fixture-sha256"], FIXTURE_SHA256)
        container = donor["spec"]["template"]["spec"]["containers"][0]
        script = container["args"][0]
        self.assertEqual(container["image"], IMAGE)
        environment = {
            item["name"]: item
            for item in donor["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(
            environment["NGC_API_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "ngc-api-key", "key": "NGC_API_KEY"},
        )
        self.assertIn("/opt/nvidia/nvidia_entrypoint.sh start_server", script)
        self.assertNotIn("uvloop", script)
        self.assertEqual(script.count("--run-id native-f7-donor-"), 2)
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(environment["TORCHINDUCTOR_COMPILE_THREADS"]["value"], "1")
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "4", "memory": "32Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "5", "memory": "40Gi", "nvidia.com/gpu": "1"},
            },
        )
        volumes = {item["name"]: item for item in donor["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["dshm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "16Gi"})
        mounts = {item["name"]: item for item in container["volumeMounts"]}
        self.assertNotIn("readOnly", mounts["nim-cache"])
        self.assertNotIn("readOnly", volumes["nim-cache"]["persistentVolumeClaim"])
        self.assertNotIn("capabilities", container["securityContext"])

    def test_storage_and_artifact_modes_are_isolated(self) -> None:
        storage = documents("storage.yaml")
        self.assertEqual(len(storage), 2)
        claims = {item["metadata"]["name"]: item for item in storage}
        self.assertEqual(
            claims["molmim-native-f7-artifacts"]["spec"]["resources"]["requests"]["storage"],
            "93Gi",
        )
        self.assertEqual(
            claims["molmim-native-f7-artifacts"]["spec"]["storageClassName"],
            "mlspec-archvteams-2407-io-m3",
        )
        self.assertEqual(
            claims["molmim-native-f7-cache"]["spec"]["resources"]["requests"]["storage"],
            "2Gi",
        )
        self.assertEqual(
            claims["molmim-native-f7-cache"]["spec"]["storageClassName"],
            "compute-csi-default-sc",
        )

        direct = documents("artifact-holder.yaml")[0]
        buffered = documents("artifact-holder-buffered.yaml")[0]
        direct_script = direct["spec"]["containers"][0]["args"][0]
        buffered_script = buffered["spec"]["containers"][0]["args"][0]
        self.assertIn('"checkpoint_id": "molmim-native-f7-v1"', direct_script)
        self.assertIn('"image_io_mode": "direct"', direct_script)
        self.assertIn('"checkpoint_id": "molmim-native-f7-v2-buffered"', buffered_script)
        self.assertIn('"image_io_mode": "buffered"', buffered_script)
        self.assertNotEqual(direct["metadata"]["name"], buffered["metadata"]["name"])

    def test_retained_cache_is_write_once_and_fully_prewarmed(self) -> None:
        seed = documents("cache-seed-job.yaml")[0]
        seed_spec = seed["spec"]["template"]["spec"]
        self.assertEqual(
            seed_spec["nodeSelector"]["kubernetes.io/hostname"],
            "gpu-node-a.example.invalid",
        )
        self.assertNotIn("nodeName", seed_spec)
        volumes = {item["name"]: item for item in seed_spec["volumes"]}
        self.assertEqual(
            volumes["retained-cache"]["hostPath"],
            {"path": "/snapshots/nim-caches/molmim", "type": "Directory"},
        )
        self.assertEqual(
            volumes["cache"]["persistentVolumeClaim"]["claimName"],
            "molmim-native-f7-cache",
        )
        seed_script = seed_spec["containers"][0]["args"][0]
        self.assertIn("281589760", seed_script)
        self.assertIn("source_bytes != 284497920", seed_script)
        self.assertIn("O_EXCL", seed_script)
        self.assertIn("tree_sha256", seed_script)

        holder = documents("cache-holder.yaml")[0]
        self.assertEqual(holder["metadata"]["name"], "molmim-native-f7-cache-holder-t12")
        holder_script = holder["spec"]["containers"][0]["args"][0]
        self.assertIn('receipt.get("regular_file_bytes") != 284497920', holder_script)
        self.assertIn("prewarm_bytes", holder_script)
        self.assertIn("tree.hexdigest()", holder_script)
        resources = holder["spec"]["containers"][0]["resources"]
        self.assertTrue(
            all("gpu" not in key.lower() for group in resources.values() for key in group)
        )

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
        self.assertEqual(contract["probe_image"], IMAGE)
        self.assertEqual(contract["probe_executable"], "/usr/bin/python3")
        self.assertEqual(
            contract["supported_image_io_modes"],
            ["direct", "writeback", "buffered"],
        )
        self.assertEqual(
            contract["source"]["materialized_tree_sha256"],
            "76838bc28fa641ba3d3165c1deb1f019c4f63ed9fce9571b38194ff65ef7b816",
        )
        self.assertEqual(
            contract["approval"]["evidence_sha256"],
            contract["source"]["materialized_tree_sha256"],
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
