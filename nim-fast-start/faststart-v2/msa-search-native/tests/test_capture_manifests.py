#!/usr/bin/env python3
"""Cross-file contract tests for the MSA Search native capture lane."""

from __future__ import annotations

import hashlib
import json
import statistics
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "nvcr.io/nim/colabfold/msa-search@sha256:"
    "944f3cf845761be8e42b33147ae08b68c61eca7cad67bf5251e1708d03c0165c"
)
VALIDATOR_SHA256 = "20e8951ceaaa1b81e8129d86b787c6bb009cf2e207d55829cf13f4fa9489188b"
FIXTURE_SHA256 = "874b0e5e3be9776ea289fb46444032e04b63875d9d4110f1560e5435de72686a"
PIPE_VALIDATOR_SHA256 = "29f45a3c0d7197b5ad0757174666b1f6a8e11f2e3dd7cc54d63fc71fb030ad23"
WORKER_IMAGE = (
    "registry.example.invalid/faststart/"
    "snapshot-agent@sha256:"
    "d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28"
)


def documents(name: str) -> list[dict]:
    with (ROOT / name).open(encoding="utf-8") as source:
        return list(yaml.safe_load_all(source))


class CaptureManifestTests(unittest.TestCase):
    def test_selected_conventional_lane_measures_submit_edge_and_strict_n3(self) -> None:
        holder = documents("conventional-cache-holder.yaml")[0]
        self.assertEqual(holder["metadata"]["name"], "@@CACHE_HOLDER@@")
        self.assertEqual(holder["spec"]["nodeName"], "@@TARGET_NODE@@")
        holder_container = holder["spec"]["containers"][0]
        holder_script = holder_container["args"][0]
        self.assertIn("seen = set()", holder_script)
        self.assertIn('"prewarm_outside_t0": True', holder_script)
        self.assertIn('"prewarm_elapsed_seconds"', holder_script)
        self.assertIn('"prewarm_completed_at"', holder_script)
        self.assertTrue(holder["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"])

        preloader = documents("conventional-image-preload.yaml.tmpl")[0]
        preload_container = preloader["spec"]["containers"][0]
        self.assertEqual(preloader["spec"]["nodeName"], "@@TARGET_NODE@@")
        self.assertEqual(preload_container["image"], "@@TARGET_IMAGE@@")
        self.assertNotIn("nvidia.com/gpu", preload_container["resources"]["requests"])

        job = documents("conventional-job.yaml.tmpl")[0]
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(job["spec"]["template"]["spec"]["nodeName"], "@@TARGET_NODE@@")
        self.assertEqual(container["image"], "@@TARGET_IMAGE@@")
        script = container["args"][0]
        self.assertEqual(script.count('--run-id "@@RUN_ID@@-semantic-'), 2)
        self.assertIn("--ready-timeout 120", script)
        self.assertIn("--timeout 30", script)
        self.assertIn("verify_mmseqs_pipe.py", script)
        self.assertEqual(
            container["resources"]["requests"],
            {"cpu": "10", "memory": "128Gi", "nvidia.com/gpu": "1"},
        )

        runner_lines = (ROOT / "run_conventional_n3.sh").read_text(
            encoding="utf-8"
        ).splitlines()
        submit_index = next(
            index
            for index, line in enumerate(runner_lines)
            if 'target-submit-at.txt"' in line and line.lstrip().startswith("date ")
        )
        self.assertEqual(
            runner_lines[submit_index + 1].strip(),
            '"${kubectl[@]}" create -f "$run_dir/job.yaml"',
        )
        runner = "\n".join(runner_lines)
        self.assertIn("get pods --all-namespaces", runner)
        self.assertIn('"demand_to_kubernetes_ready_seconds"', runner)
        self.assertIn('"unique_prewarm_bytes": holder["prewarm_bytes"]', runner)
        self.assertIn('preloaded_outside_t0:true', runner)
        self.assertIn('"cleanup-receipt.json"', runner)
        self.assertIn('.reason=="Pulling"', runner)
        self.assertIn('--context "$allowed_context"', runner)
        self.assertIn('MSA_CACHE_PVC:-msa-search-cache-example', runner)
        self.assertIn('MSA_CACHE_HOLDER:-msa-search-cache-holder-example', runner)
        self.assertIn('@@TARGET_IMAGE@@', runner)
        self.assertIn('for run in 1 2 3', runner)

    def test_results_record_matches_counted_submit_edge_receipt(self) -> None:
        results = json.loads((ROOT / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["status"], "PASS")
        self.assertEqual(results["selected_route"], "conventional-cache-attached-prewarmed")
        selected = results["conventional_cached_n3"]
        self.assertEqual(selected["trial_count"], 3)
        self.assertEqual(selected["semantic_call_count"], 6)
        self.assertEqual(selected["mmseqs_pipe_pass_count"], 3)
        expected = {
            "demand_to_http_ready_seconds": [4.830585, 4.962104, 4.8724],
            "semantic_request_1_seconds": [0.040644, 0.041808, 0.039441],
            "semantic_request_2_seconds": [0.028986, 0.030188, 0.02992],
            "demand_to_call2_response_seconds": [4.901161, 5.035089, 4.942788],
            "demand_to_validation_complete_seconds": [4.901863, 5.035818, 4.943544],
            "demand_to_kubernetes_ready_seconds": [4.98236, 4.545373, 4.687717],
        }
        for name, values in expected.items():
            recorded = selected[name]
            self.assertEqual(recorded["values"], values)
            self.assertEqual(recorded["min"], min(values))
            self.assertEqual(recorded["median"], statistics.median(values))
            self.assertEqual(recorded["max"], max(values))
        self.assertTrue(selected["storage"]["prewarm_outside_t0"])
        self.assertEqual(selected["storage"]["unique_prewarm_bytes"], 112682799)
        self.assertEqual(selected["storage"]["prewarm_elapsed_seconds"], 0.104987)
        self.assertEqual(selected["target_image_residency"]["target_event_pulling_count"], 0)
        self.assertEqual(
            results["response_boundary_requalification"]["status"],
            "PASS",
        )
        legacy = results["legacy_conventional_cached_n3"]
        self.assertEqual(legacy["batch_id"], "msa-conv-f7c")
        self.assertEqual(
            legacy["demand_to_validation_complete_seconds"]["median"], 5.144951
        )
        self.assertEqual(results["native_checkpoint"]["counted_trials"], 0)

    def test_input_digests_and_donor_warmup_are_exact(self) -> None:
        validator = (ROOT / "validate_msa_search.py").read_bytes()
        fixture = (ROOT / "fixtures" / "request-pdb70.json").read_bytes()
        pipe_validator = (ROOT / "verify_mmseqs_pipe.py").read_bytes()
        self.assertEqual(hashlib.sha256(validator).hexdigest(), VALIDATOR_SHA256)
        self.assertEqual(hashlib.sha256(fixture).hexdigest(), FIXTURE_SHA256)
        self.assertEqual(
            hashlib.sha256(pipe_validator).hexdigest(), PIPE_VALIDATOR_SHA256
        )

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
                "requests": {"cpu": "10", "memory": "128Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "15", "memory": "180Gi", "nvidia.com/gpu": "1"},
            },
        )
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(
            env["NIM_MODEL_PROFILE"],
            "ad5086cc67393792e71fa57444f13eaff8425658e8fb5feea07070ca3b2d34bb",
        )
        self.assertEqual(env["NIM_GLOBAL_MAX_MSA_DEPTH"], "500")
        volumes = {item["name"]: item for item in donor["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["dshm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "16Gi"})
        self.assertNotIn("workspace", volumes)
        mounts = [
            item for item in container["volumeMounts"] if item["name"] == "nim-cache"
        ]
        self.assertEqual(
            {item["mountPath"] for item in mounts},
            {"/opt/nim/.cache", "/opt/nim/workspace"},
        )
        runner = (ROOT / "dynamo" / "run_provisioned_trial.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(PIPE_VALIDATOR_SHA256, runner)
        self.assertIn("mmseqs-pipe-receipt.json", runner)

    def test_storage_and_artifact_modes_are_isolated(self) -> None:
        storage = documents("storage.yaml")
        self.assertEqual(len(storage), 2)
        claims = {item["metadata"]["name"]: item for item in storage}
        self.assertEqual(
            claims["msa-search-native-f7-artifacts"]["spec"]["resources"]["requests"]["storage"],
            "93Gi",
        )
        self.assertEqual(
            claims["msa-search-native-f7-artifacts"]["spec"]["storageClassName"],
            "mlspec-archvteams-2407-io-m3",
        )
        self.assertEqual(
            claims["msa-search-native-f7-cache"]["spec"]["resources"]["requests"]["storage"],
            "50Gi",
        )
        self.assertEqual(
            claims["msa-search-native-f7-cache"]["spec"]["storageClassName"],
            "compute-csi-default-sc",
        )

        direct = documents("artifact-holder.yaml")[0]
        buffered = documents("artifact-holder-buffered.yaml")[0]
        direct_script = direct["spec"]["containers"][0]["args"][0]
        buffered_script = buffered["spec"]["containers"][0]["args"][0]
        self.assertIn('"checkpoint_id": "msa-search-native-f7-v1"', direct_script)
        self.assertIn('"image_io_mode": "direct"', direct_script)
        self.assertIn('"checkpoint_id": "msa-search-native-f7-v2-buffered"', buffered_script)
        self.assertIn('"image_io_mode": "buffered"', buffered_script)
        self.assertNotEqual(direct["metadata"]["name"], buffered["metadata"]["name"])

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
