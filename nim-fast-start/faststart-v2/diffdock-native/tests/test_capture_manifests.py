#!/usr/bin/env python3
"""Offline consistency tests for the DiffDock native-capture lane."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = (
    "nvcr.io/nim/mit/diffdock@sha256:"
    "300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480"
)
WORKER_IMAGE = (
    "registry.example.invalid/faststart/"
    "snapshot-agent@sha256:"
    "063286a3a1354d1c5969fa80f445bb5fbd2a96bc0999c7b6897495f0b4c2fd4d"
)
NODE = "gpu-node-a.example.invalid"
VALIDATOR_SHA256 = "245ae98a98db09c34924cd7a499b99da9eb35742667043aaee3e497c33268008"
FIXTURE_SHA256 = "f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977"


def documents(name: str) -> list[dict[str, Any]]:
    values = list(yaml.safe_load_all((ROOT / name).read_text(encoding="utf-8")))
    if not values or any(not isinstance(value, dict) for value in values):
        raise AssertionError(f"{name} contains invalid YAML")
    return values


def only_container(spec: dict[str, Any]) -> dict[str, Any]:
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise AssertionError("expected exactly one container")
    return containers[0]


class CaptureManifestTests(unittest.TestCase):
    def test_response_requalification_preloader_pins_three_zero_gpu_images(self) -> None:
        preloader = documents("dynamo/image-preload.yaml.tmpl")[0]
        runner = (ROOT / "dynamo" / "run_response_n3.sh").read_text(encoding="utf-8")
        contract = json.loads(
            (ROOT / "dynamo" / "restore-interface.live.json").read_bytes()
        )
        self.assertEqual(
            preloader["spec"]["nodeSelector"],
            {"kubernetes.io/hostname": "@@TARGET_NODE@@"},
        )
        containers = {item["name"]: item for item in preloader["spec"]["containers"]}
        self.assertEqual(containers["target"]["image"], "@@TARGET_IMAGE@@")
        self.assertEqual(containers["restore-worker"]["image"], "@@WORKER_IMAGE@@")
        self.assertEqual(containers["semantic-probe"]["image"], "@@PROBE_IMAGE@@")
        self.assertIn(f"readonly target_image='{IMAGE}'", runner)
        self.assertEqual(contract["worker_image"], WORKER_IMAGE)
        self.assertRegex(contract["probe_image"], r"@sha256:[0-9a-f]{64}$")
        for placeholder in (
            "@@TARGET_NODE@@",
            "@@TARGET_IMAGE@@",
            "@@WORKER_IMAGE@@",
            "@@PROBE_IMAGE@@",
            "@@NGC_PULL_SECRET@@",
            "@@WORKER_PULL_SECRET@@",
        ):
            self.assertIn(placeholder, runner)
        self.assertTrue(
            all(
                "nvidia.com/gpu" not in item["resources"]["requests"]
                and "nvidia.com/gpu" not in item["resources"]["limits"]
                for item in containers.values()
            )
        )

    def test_response_requalification_runner_is_cluster_and_uid_fail_closed(self) -> None:
        runner = (ROOT / "dynamo" / "run_response_n3.sh").read_text(encoding="utf-8")
        self.assertIn("EXPECTED_API_SERVER", runner)
        self.assertIn("EXPECTED_CONTEXT", runner)
        self.assertIn("TARGET_NODE", runner)
        self.assertIn("DIFFDOCK_ARTIFACT_HOLDER", runner)
        self.assertIn("DIFFDOCK_ARTIFACT_PVC", runner)
        self.assertIn("DIFFDOCK_CACHE_PVC", runner)
        self.assertNotIn("mk8scluster-", runner)
        self.assertIn('preconditions:{uid:$uid}', runner)
        self.assertIn("worker_cpu_request_mcpu:1000", runner)
        self.assertIn("candidate_headroom>=400", runner)
        self.assertIn("perturbing_artifact_setup_after_prewarm:0", runner)

    def test_selected_response_result_has_exact_absolute_boundary(self) -> None:
        results = json.loads((ROOT / "results.json").read_bytes())
        selected = results["selected_response_boundary_n3"]
        self.assertEqual(selected["status"], "PASS")
        self.assertEqual(selected["semantic_passes"], 6)
        self.assertEqual(
            selected["demand_to_two_semantic_responses_seconds"]["median"],
            14.190621,
        )
        self.assertEqual(len(selected["t0_at"]), 3)
        self.assertEqual(len(selected["second_response_received_at"]), 3)
        self.assertTrue(results["cleanup"]["uid_preconditions_enforced"])

    def test_compatibility_review_records_measured_diffdock_glibc(self) -> None:
        review = json.loads((ROOT / "compatibility-evidence.json").read_bytes())
        self.assertTrue(review["live_mutations_performed"])
        self.assertEqual(review["diffdock_target"]["glibc_version"], "2.35")
        self.assertEqual(review["diffdock_target"]["status"], "measured-live-donor-r2")
        self.assertEqual(
            review["superseded_worker"]["rootfs_extractor_required_glibc"],
            "2.38",
        )
        self.assertEqual(review["portable_worker"]["image"], WORKER_IMAGE)
        self.assertEqual(
            review["portable_worker"]["tool_bundle_manifest_sha256"],
            "fc22c423deca17b4175ab42c23a66310c8e2c4d8c4b63a24c33894300020943b",
        )
        self.assertEqual(
            review["portable_worker"]["restore_worker_sha256"],
            "941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651",
        )
        self.assertEqual(
            review["portable_worker"]["rootfs_extractor"],
            "target-/bin/tar-without-inherited-LD_LIBRARY_PATH",
        )
        self.assertTrue(review["rootfsless_policy"]["candidate_only"])

    def test_local_review_digest_is_bound_into_live_restore_contract(self) -> None:
        review_bytes = (ROOT / "dynamo" / "review-evidence.json").read_bytes()
        review = json.loads(review_bytes)
        contract_path = ROOT / "dynamo" / "restore-interface.live.json"
        contract_bytes = contract_path.read_bytes()
        contract = json.loads(contract_bytes)
        compatibility_bytes = (ROOT / "compatibility-evidence.json").read_bytes()
        compatibility = json.loads(compatibility_bytes)
        self.assertTrue(review["live_mutations_performed"])
        self.assertEqual(
            contract["approval"]["evidence_sha256"],
            hashlib.sha256(review_bytes).hexdigest(),
        )
        self.assertEqual(
            review["compatibility_contract"]["review_sha256"],
            hashlib.sha256(compatibility_bytes).hexdigest(),
        )
        self.assertEqual(contract["worker_image"], WORKER_IMAGE)
        self.assertEqual(
            contract["worker_executable_sha256"],
            compatibility["portable_worker"]["restore_worker_sha256"],
        )
        self.assertEqual(
            contract["tool_bundle"]["content_sha256"],
            compatibility["portable_worker"]["tool_bundle_manifest_sha256"],
        )
        runner = (ROOT / "dynamo" / "run_provisioned_trial.sh").read_text(
            encoding="utf-8"
        )
        contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        self.assertIn(
            f'readonly expected_contract_sha256="{contract_sha256}"',
            runner,
        )

    def test_validator_and_fixture_digests_are_frozen(self) -> None:
        self.assertEqual(
            hashlib.sha256((ROOT / "validate_diffdock.py").read_bytes()).hexdigest(),
            VALIDATOR_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(
                (ROOT / "fixtures" / "1ubq-aspirin-request.json").read_bytes()
            ).hexdigest(),
            FIXTURE_SHA256,
        )

    def test_storage_is_isolated_and_sized_above_retained_artifact(self) -> None:
        claims = documents("storage.yaml")
        self.assertEqual(len(claims), 2)
        by_name = {item["metadata"]["name"]: item for item in claims}
        artifact = by_name["diffdock-native-f7-artifacts"]
        cache = by_name["diffdock-native-f7-cache"]
        self.assertEqual(artifact["spec"]["storageClassName"], "mlspec-archvteams-2407-io-m3")
        self.assertEqual(artifact["spec"]["resources"]["requests"]["storage"], "93Gi")
        self.assertEqual(cache["spec"]["storageClassName"], "compute-csi-default-sc")
        self.assertEqual(cache["spec"]["resources"]["requests"]["storage"], "50Gi")
        self.assertEqual(artifact["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(cache["spec"]["accessModes"], ["ReadWriteOnce"])

    def test_donor_pins_real_image_node_fixture_and_two_call_validator(self) -> None:
        donor = documents("donor-job.yaml")[0]
        self.assertEqual(donor["kind"], "Job")
        self.assertEqual(donor["metadata"]["name"], "diffdock-native-f7-donor-r2")
        self.assertEqual(
            donor["metadata"]["annotations"],
            {
                "archvteams.nebius.ai/validator-sha256": VALIDATOR_SHA256,
                "archvteams.nebius.ai/fixture-sha256": FIXTURE_SHA256,
            },
        )
        spec = donor["spec"]["template"]["spec"]
        self.assertEqual(spec["nodeSelector"], {"kubernetes.io/hostname": NODE})
        self.assertEqual(spec["runtimeClassName"], "nvidia")
        self.assertEqual(spec["imagePullSecrets"], [{"name": "nvcrio-cred"}])
        container = only_container(spec)
        self.assertEqual(container["name"], "diffdock")
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(
            container["resources"],
            {
                "requests": {"cpu": "8", "memory": "64Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "15", "memory": "160Gi", "nvidia.com/gpu": "1"},
            },
        )
        command = container["args"][0]
        self.assertIn(
            "--request-file /output/donor-validation/1ubq-aspirin-request.json",
            command,
        )
        self.assertEqual(command.count("--run-id native-f7-donor-"), 2)
        self.assertIn("not stat.S_ISREG", command)
        self.assertIn("path.is_symlink()", command)
        self.assertIn('"schema": "archvteams.nebius.ai/target-runtime/v1"', command)
        self.assertIn('["getconf", "GNU_LIBC_VERSION"]', command)
        self.assertIn('["ldd", "--version"]', command)
        self.assertIn("touch /snapshot-control/ready-for-snapshot", command)
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            environment["VALIDATOR_SOURCE"]["valueFrom"]["configMapKeyRef"]["name"],
            "diffdock-native-f7-validator-r2",
        )
        self.assertEqual(
            environment["REQUEST_JSON"]["valueFrom"]["configMapKeyRef"]["name"],
            "diffdock-native-f7-validator-r2",
        )
        self.assertEqual(
            environment["NGC_API_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "ngc-api-key", "key": "NGC_API_KEY"},
        )

    def test_capture_agent_reuses_final_generalized_worker(self) -> None:
        agent = documents("snapshot-agent.yaml")[0]
        spec = agent["spec"]
        self.assertEqual(spec["nodeSelector"], {"kubernetes.io/hostname": NODE})
        self.assertEqual(spec["serviceAccountName"], "archvteams-2407-native-snapshot")
        container = only_container(spec)
        self.assertEqual(container["image"], WORKER_IMAGE)
        artifact_volume = next(item for item in spec["volumes"] if item["name"] == "checkpoints")
        self.assertEqual(
            artifact_volume["persistentVolumeClaim"]["claimName"],
            "diffdock-native-f7-artifacts",
        )

    def test_snapshot_content_is_uid_bound_to_exact_node_and_container(self) -> None:
        content = documents("podsnapshotcontent.yaml.tmpl")[0]
        self.assertEqual(content["metadata"]["name"], "diffdock-native-f7-v1-direct-hf93")
        source = content["spec"]["source"]
        self.assertEqual(source["nodeName"], NODE)
        self.assertEqual(source["podRef"]["name"], "@@SOURCE_POD_NAME@@")
        self.assertEqual(source["podRef"]["uid"], "@@SOURCE_POD_UID@@")
        self.assertEqual(source["podRef"]["containers"], ["diffdock"])

    def test_holder_keeps_both_read_only_claims_attached_without_gpu(self) -> None:
        holder = documents("artifact-holder.yaml")[0]
        spec = holder["spec"]
        self.assertEqual(spec["nodeName"], NODE)
        container = only_container(spec)
        resources = container["resources"]
        self.assertNotIn("nvidia.com/gpu", resources["requests"])
        self.assertNotIn("nvidia.com/gpu", resources["limits"])
        self.assertEqual(resources["limits"]["memory"], "16Gi")
        self.assertEqual(
            container["securityContext"],
            {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": False,
                "runAsUser": 0,
                "runAsGroup": 0,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        )
        self.assertIn("manifest_sha256", container["args"][0])
        self.assertIn("regular_file_bytes", container["args"][0])
        self.assertIn("prewarm_bytes", container["args"][0])
        self.assertIn("ThreadPoolExecutor(max_workers=4)", container["args"][0])
        self.assertIn("diffdock-native-f7-v3-buffered", container["args"][0])
        self.assertIn(
            "93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe",
            container["args"][0],
        )
        self.assertEqual(
            container["readinessProbe"]["exec"]["command"],
            ["/usr/bin/test", "-f", "/state/artifact-verified"],
        )
        claims = {
            item["name"]: item["persistentVolumeClaim"]
            for item in spec["volumes"]
            if "persistentVolumeClaim" in item
        }
        self.assertEqual(
            claims,
            {
                "artifacts": {
                    "claimName": "diffdock-native-f7-artifacts",
                    "readOnly": True,
                },
                "nim-cache": {
                    "claimName": "diffdock-native-f7-cache",
                    "readOnly": True,
                },
            },
        )

    def test_buffered_variant_is_write_once_and_preserves_exact_payload(self) -> None:
        config, pod = documents("artifact-buffered-variant.yaml")
        self.assertTrue(config["immutable"])
        self.assertEqual(config["metadata"]["name"], "diffdock-native-f7-v3-buffered-build")
        source = config["data"]["build.py"]
        self.assertIn('SOURCE_ID = "diffdock-native-f7-v1"', source)
        self.assertIn('DESTINATION_ID = "diffdock-native-f7-v3-buffered"', source)
        self.assertIn(
            'SOURCE_MANIFEST_SHA256 = "b1c477efdfc6bcb8e253462524cef24fef6e059f43c97a1fcb94b85dca81e0b8"',
            source,
        )
        self.assertIn('old_mode = b"        imageIoMode: direct\\n"', source)
        self.assertIn('new_mode = b"        imageIoMode: buffered\\n"', source)
        self.assertIn("os.link(source, destination, follow_symlinks=False)", source)
        self.assertIn("refusing to overwrite", source)
        spec = pod["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        container = only_container(spec)
        self.assertNotIn("nvidia.com/gpu", container["resources"]["requests"])
        self.assertEqual(
            next(
                item for item in spec["volumes"] if item["name"] == "checkpoints"
            )["persistentVolumeClaim"]["claimName"],
            "diffdock-native-f7-artifacts",
        )

    def test_source_holder_is_separate_from_winning_buffered_holder(self) -> None:
        source = only_container(documents("artifact-source-holder.yaml")[0]["spec"])
        buffered = only_container(documents("artifact-holder.yaml")[0]["spec"])
        self.assertIn("diffdock-native-f7-v1", source["args"][0])
        self.assertIn(
            "b1c477efdfc6bcb8e253462524cef24fef6e059f43c97a1fcb94b85dca81e0b8",
            source["args"][0],
        )
        self.assertIn("diffdock-native-f7-v3-buffered", buffered["args"][0])


if __name__ == "__main__":
    unittest.main()
