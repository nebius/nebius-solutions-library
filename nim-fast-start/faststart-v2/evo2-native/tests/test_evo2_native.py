from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
sys.path.insert(0, str(MODULE_DIR))

import render  # noqa: E402
import aggregate_results  # noqa: E402
import artifact_variant  # noqa: E402
import bind_target  # noqa: E402
import prewarm_artifact  # noqa: E402
import render_capture  # noqa: E402
import validate_evo2 as validator  # noqa: E402


POD_UID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "containerd://" + "a" * 64
MANIFEST_SHA256 = "b" * 64
WORKER_IMAGE = (
    "registry.example.invalid/faststart/"
    "snapshot-agent@sha256:"
    "d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28"
)
RESTORE_WORKER_SHA256 = "941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651"
TOOL_MANIFEST_SHA256 = "c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e"
SOURCE_TREE_SHA256 = "76838bc28fa641ba3d3165c1deb1f019c4f63ed9fce9571b38194ff65ef7b816"
PROVENANCE_SHA256 = "744997f5b7ed28749c86906cc7f909631e2cf79531750b831ebedb5d02d239a9"
NS_BIND_MOUNT_PATCH_SHA256 = (
    "4847d7d42aae570fc7f91351a8fbf3018f10dc6247d93c2c9696754861731366"
)


def contract() -> dict[str, Any]:
    return json.loads(
        (MODULE_DIR / "restore-interface.performance.json").read_text(encoding="utf-8")
    )


def run_config(mode: str = "direct", run_id: str = "evo-offline-1") -> dict[str, Any]:
    artifact = render.PROFILE["artifacts"][mode]
    return {
        "schema": render.RUN_SCHEMA,
        "demand_at": "2026-08-18T06:00:00.000000Z",
        "run_id": run_id,
        "target_node": render.TARGET_NODE,
        "checkpoint_id": artifact["checkpoint_id"],
        "artifact_version": artifact["artifact_version"],
        "artifact_manifest_sha256": MANIFEST_SHA256,
        "artifact_pvc": render.PROFILE["storage"]["artifact_pvc"],
        "cache_pvc": render.PROFILE["storage"]["cache_pvc"],
        "image_io_mode": mode,
    }


def response_for(probe: validator.Probe) -> dict[str, Any]:
    return {
        "sequence": probe.expected_sequence,
        "logits": None,
        "sampled_probs": None,
        "elapsed_ms": 800,
        "elapsed_ms_per_token": [40] * 20,
    }


def live_target() -> dict[str, Any]:
    run = render.validate_run(run_config())
    approved = render.validate_contract(contract())
    documents = render.render_target(run, approved)
    pod = copy.deepcopy(next(item for item in documents if item["kind"] == "Pod"))
    pod["metadata"]["uid"] = POD_UID
    pod["spec"].update(
        {
            "nodeName": run["target_node"],
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "serviceAccount": "default",
            "serviceAccountName": "default",
        }
    )
    for omitted_false in ("hostIPC", "hostNetwork", "hostPID"):
        pod["spec"].pop(omitted_false, None)
    pod["status"] = {
        "phase": "Running",
        "qosClass": "Burstable",
        "podIP": "10.126.62.248",
        "podIPs": [{"ip": "10.126.62.248"}],
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "containerStatuses": [
            {
                "name": render.CONTAINER_NAME,
                "containerID": CONTAINER_ID,
                "image": render.NIM_IMAGE,
                "imageID": render.NIM_IMAGE,
                "state": {"running": {"startedAt": "2026-08-18T06:00:01Z"}},
            }
        ],
    }
    return pod


def trial_summary(index: int, mode: str = "direct") -> dict[str, Any]:
    return {
        "schema": "archvteams.nebius.ai/evo2-native-trial-summary/v1",
        "run_id": f"run-{index}",
        "status": "PASS",
        "model": "Evo2-40B",
        "image": render.NIM_IMAGE,
        "gpu_topology": "1x NVIDIA H200 SXM, full GPU, non-MIG",
        "image_io_mode": mode,
        "checkpoint_id": render.PROFILE["artifacts"][mode]["checkpoint_id"],
        "artifact_manifest_sha256": MANIFEST_SHA256,
        "pod_uid": f"11111111-1111-4111-8111-{index:012d}",
        "pod_spec_sha256": f"{index}" * 64,
        "semantic_request_count": 2,
        "semantic_response_sha256": ["c" * 64, "d" * 64],
        "worker_receipt": {"status": "succeeded", "duration_ms": 50_000 + index * 100},
        "semantic": {
            "status": "PASS",
            "request_count": 2,
            "passed_case_count": 2,
            "failed_case_count": 0,
            "total_elapsed_seconds": 1.5 + index / 10,
            "cases": [
                {
                    "elapsed_seconds": elapsed,
                    "invariant": {"output_sequence": sequence},
                }
                for sequence, elapsed in zip(
                    render.PROFILE["semantic_profile"]["expected_sequences"],
                    (1.0 + index / 10, 0.2 + index / 10),
                    strict=True,
                )
            ],
        },
        "demand_to_http_ready_seconds": 40.0 + index,
        "demand_to_kubernetes_ready_seconds": 41.0 + index,
        "semantic_request_1_seconds": 1.0 + index / 10,
        "semantic_request_2_seconds": 0.2 + index / 10,
        "demand_to_two_semantic_seconds": 65.0 + index,
        "warm_instance_contract": {
            "node_ready_before_t0": True,
            "target_image_cached_before_t0": True,
            "restore_worker_image_cached_before_t0": True,
            "semantic_probe_image_cached_before_t0": True,
            "storage_attached_before_t0": True,
        },
        "storage_state": {
            "artifact_and_cache_pvcs_attached_before_t0": True,
            "artifact_holder_ready_at": "2026-08-17T23:50:00Z",
            "artifact_regular_file_count": 12,
            "artifact_regular_bytes": 99_959_572_798,
            "artifact_payload_read_before_t0": mode == "buffered",
            "artifact_prewarm_seconds": 31.25 if mode == "buffered" else None,
            "artifact_prewarm_finished_at": (
                "2026-08-17T23:59:00Z" if mode == "buffered" else None
            ),
            "artifact_prewarm_excluded_from_t0": mode == "buffered",
            "page_cache_state": (
                "fully-prewarmed-buffered"
                if mode == "buffered"
                else "not-preloaded-direct-o_direct"
            ),
        },
        "timing_evidence": {
            "demand_at": "2026-08-18T00:00:00Z",
            "t0_at": "2026-08-18T00:00:00Z",
            "t0_source": "target-submit-at.txt",
            "setup_demand_at": "2026-08-17T23:59:59Z",
            "http_ready_at": "2026-08-18T00:00:41Z",
            "kubernetes_ready_at": "2026-08-18T00:00:42Z",
            "semantic_started_at": "2026-08-18T00:00:01Z",
            "semantic_finished_at": "2026-08-18T00:01:06Z",
        },
    }


class Evo2SemanticTests(unittest.TestCase):
    def test_requests_are_exactly_two_distinct_seeded_oracles(self) -> None:
        probes = validator.build_probes(("semantic-a", "semantic-b"))
        self.assertEqual(2, len(probes))
        self.assertEqual([2407001, 2407002], [item.random_seed for item in probes])
        self.assertEqual([20, 20], [item.payload["num_tokens"] for item in probes])
        self.assertEqual([1, 1], [item.payload["top_k"] for item in probes])
        self.assertNotEqual(probes[0].input_sequence, probes[1].input_sequence)
        with self.assertRaises(validator.SetupFailure):
            validator.build_probes(("only-one",))
        with self.assertRaises(validator.SetupFailure):
            validator.build_probes(("same", "same"))

    def test_accepts_retained_response_shape(self) -> None:
        for probe in validator.build_probes(("valid-a", "valid-b")):
            invariant = validator.validate_response(response_for(probe), probe)
            self.assertEqual(probe.expected_sequence, invariant["output_sequence"])
            self.assertEqual(20, invariant["token_count"])
            self.assertEqual(20, invariant["per_token_count"])

    def test_rejects_nonsemantic_or_shape_drift(self) -> None:
        probe = validator.build_probes(("reject-a", "reject-b"))[0]
        mutations = [
            lambda value: value.update(sequence="A" * 20),
            lambda value: value.update(sequence="N" * 20),
            lambda value: value.update(logits=[]),
            lambda value: value.update(sampled_probs=[]),
            lambda value: value.update(elapsed_ms=float("nan")),
            lambda value: value.update(elapsed_ms_per_token=[40] * 19),
            lambda value: value.update(elapsed_ms_per_token=[40] * 19 + [-1]),
        ]
        for mutate in mutations:
            value = response_for(probe)
            mutate(value)
            with self.assertRaises(validator.SemanticFailure):
                validator.validate_response(value, probe)

    def test_readiness_accepts_only_reviewed_shapes(self) -> None:
        self.assertTrue(validator.readiness_is_ready(b"true"))
        self.assertTrue(validator.readiness_is_ready(b'{"status":"ready"}'))
        for body in (b"false", b'{"status":"loading"}', b"ready", b"{}"):
            self.assertFalse(validator.readiness_is_ready(body))


class Evo2RenderAndBindingTests(unittest.TestCase):
    def test_target_probe_and_worker_are_production_shaped(self) -> None:
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        target_documents = render.render_target(run, approved)
        self.assertEqual([], render.lint_documents(target_documents))
        target = next(item for item in target_documents if item["kind"] == "Pod")
        target_container = target["spec"]["containers"][0]
        self.assertEqual(render.NIM_IMAGE, target_container["image"])
        self.assertEqual(render.PROFILE["pod_profile"]["requests"], target_container["resources"]["requests"])
        self.assertEqual(render.PROFILE["pod_profile"]["limits"], target_container["resources"]["limits"])
        self.assertNotIn("nodeName", target["spec"])
        self.assertEqual([{"name": "nvcrio-cred"}], target["spec"]["imagePullSecrets"])
        self.assertEqual(
            render.PROFILE["model"]["cache_path"],
            next(item["mountPath"] for item in target_container["volumeMounts"] if item["name"] == "nim-cache"),
        )
        services = [item for item in target_documents if item["kind"] == "Service"]
        self.assertEqual(2, len(services))
        self.assertTrue(all(item["spec"]["type"] == "ClusterIP" for item in services))

        binding, _ = bind_target.build_binding(
            live_target(), run, approved, "2026-08-18T06:00:02Z"
        )
        probe_documents = render.render_probe(run, approved, binding)
        worker_documents = render.render_restore(run, approved, binding)
        self.assertEqual([], render.lint_documents(probe_documents))
        self.assertEqual([], render.lint_documents(worker_documents))
        probe = next(item for item in probe_documents if item["kind"] == "Job")
        probe_container = probe["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(2, probe_container["args"].count("--run-id"))
        self.assertFalse(render._contains_gpu(probe_container["resources"]))
        self.assertIn(f"http://e2-canary-{run['run_id']}:8000", probe_container["args"])
        worker = next(item for item in worker_documents if item["kind"] == "Job")
        worker_container = worker["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(render.WORKER_GATE["worker_image"], worker_container["image"])
        self.assertFalse(render._contains_gpu(worker_container["resources"]))
        self.assertTrue(worker_container["securityContext"]["privileged"])

    def test_live_binding_preserves_original_podspec_digest(self) -> None:
        pod = live_target()
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        binding, patch = bind_target.build_binding(
            pod, run, approved, "2026-08-18T06:00:02Z"
        )
        expected = bind_target.base_bind.pod_spec_sha256(pod["spec"])
        self.assertEqual(expected, binding["pod_spec_sha256"])
        self.assertEqual(render.NIM_IMAGE, binding["image_id"])
        self.assertEqual(render.TARGET_NODE, binding["node"])
        self.assertEqual(POD_UID, patch[0]["value"])
        self.assertEqual(expected, patch[1]["value"])

    def test_binding_rejects_image_and_node_drift(self) -> None:
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        wrong_image = live_target()
        wrong_image["spec"]["containers"][0]["image"] = "example.test/evo2@sha256:" + "0" * 64
        with self.assertRaisesRegex(bind_target.base_bind.BindingError, "pinned Evo2 image"):
            bind_target.build_binding(wrong_image, run, approved, "2026-08-18T06:00:02Z")
        wrong_node = live_target()
        wrong_node["spec"]["nodeName"] = "different-node"
        with self.assertRaisesRegex(bind_target.base_bind.BindingError, "pinned H200"):
            bind_target.build_binding(wrong_node, run, approved, "2026-08-18T06:00:02Z")

    def test_run_config_binds_mode_to_checkpoint(self) -> None:
        render.validate_run(run_config("direct"))
        render.validate_run(run_config("buffered"))
        mismatch = run_config("direct")
        mismatch["checkpoint_id"] = render.PROFILE["artifacts"]["buffered"]["checkpoint_id"]
        with self.assertRaises(render.RenderError):
            render.validate_run(mismatch)
        mismatch = run_config("direct")
        mismatch["target_node"] = "different-node"
        with self.assertRaises(render.RenderError):
            render.validate_run(mismatch)


class Evo2CaptureAndArtifactTests(unittest.TestCase):
    def test_capture_renderers_pin_native_inputs(self) -> None:
        storage = render_capture.render_storage()
        self.assertEqual(2, len(storage))
        self.assertEqual(
            {render.PROFILE["storage"]["artifact_pvc"], render.PROFILE["storage"]["cache_pvc"]},
            {item["metadata"]["name"] for item in storage},
        )
        donor_documents = render_capture.render_donor("h200-r1")
        render_capture.validate_documents(donor_documents)
        donor = next(item for item in donor_documents if item["kind"] == "Job")
        container = donor["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(render.NIM_IMAGE, container["image"])
        self.assertEqual(2, container["args"][0].count("--run-id"))
        self.assertNotIn("nodeName", donor["spec"]["template"]["spec"])
        agent = render_capture.render_agent("h200-r1")[0]
        self.assertEqual(render.WORKER_GATE["worker_image"], agent["spec"]["containers"][0]["image"])
        content = render_capture.render_content(
            "h200-r1", "e2-donor-h200-r1-abcde", POD_UID
        )[0]
        self.assertEqual(POD_UID, content["spec"]["source"]["podRef"]["uid"])
        self.assertEqual([render.CONTAINER_NAME], content["spec"]["source"]["podRef"]["containers"])

    def _create_direct_artifact(self, root: Path) -> tuple[Path, str, int, int]:
        artifact = root / artifact_variant.SOURCE_ID / "versions" / "1"
        artifact.mkdir(parents=True)
        manifest = (
            f"checkpointId: {artifact_variant.SOURCE_ID}\n"
            "spec:\n"
            "  containers:\n"
            "    - name: evo2\n"
            "      criu:\n"
            "        imageIoMode: direct\n"
        ).encode("ascii")
        (artifact / "manifest.yaml").write_bytes(manifest)
        (artifact / "rootfs-diff.tar").write_bytes(b"rootfs")
        (artifact / "pages-1.img").write_bytes(b"pages" * 100)
        members, total = artifact_variant.inventory(artifact)
        return artifact, hashlib.sha256(manifest).hexdigest(), len(members), total

    def test_buffered_variant_hardlinks_payload_and_changes_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, manifest_sha256, count, total = self._create_direct_artifact(root)
            result = artifact_variant.build_variant(root, manifest_sha256, count, total)
            destination = root / artifact_variant.DESTINATION_ID / "versions" / "1"
            self.assertEqual("PASS", result["status"])
            self.assertEqual("buffered", result["image_io_mode"])
            self.assertIn(b"imageIoMode: buffered", (destination / "manifest.yaml").read_bytes())
            self.assertNotIn(b"imageIoMode: direct", (destination / "manifest.yaml").read_bytes())
            for name in ("rootfs-diff.tar", "pages-1.img"):
                self.assertEqual((source / name).stat().st_ino, (destination / name).stat().st_ino)
            with self.assertRaises(artifact_variant.VariantError):
                artifact_variant.build_variant(root, manifest_sha256, count, total)

    def test_prewarm_distinguishes_direct_metadata_from_buffered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct, direct_sha, direct_count, direct_total = self._create_direct_artifact(root)
            direct_result = prewarm_artifact.verify_and_prewarm(
                direct, direct_sha, direct_count, direct_total, "direct"
            )
            variant = artifact_variant.build_variant(
                root, direct_sha, direct_count, direct_total
            )
            buffered = root / artifact_variant.DESTINATION_ID / "versions" / "1"
            buffered_members, buffered_total = artifact_variant.inventory(buffered)
            buffered_result = prewarm_artifact.verify_and_prewarm(
                buffered,
                variant["manifest_sha256"],
                len(buffered_members),
                buffered_total,
                "buffered",
            )
            self.assertFalse(direct_result["payload_read"])
            self.assertTrue(buffered_result["payload_read"])


class Evo2AggregateAndProvenanceTests(unittest.TestCase):
    def test_n3_aggregate_requires_three_matched_semantic_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for index in range(1, 4):
                path = root / f"trial-{index}.json"
                path.write_text(json.dumps(trial_summary(index)), encoding="utf-8")
                paths.append(path)
            result = aggregate_results.aggregate(paths, "direct")
            self.assertEqual("PASS", result["status"])
            self.assertEqual(3, result["trial_count"])
            self.assertEqual(6, result["semantic_pass_count"])
            self.assertEqual(42.0, result["demand_to_http_ready_seconds"]["median"])
            self.assertEqual(
                43.0, result["demand_to_kubernetes_ready_seconds"]["median"]
            )
            self.assertEqual(1.2, result["semantic_request_1_seconds"]["median"])
            self.assertEqual(0.4, result["semantic_request_2_seconds"]["median"])
            self.assertEqual(67.0, result["demand_to_two_semantic_seconds"]["median"])
            self.assertEqual(50.2, result["worker_restore_seconds"]["median"])
            self.assertEqual(
                "immediately-before-target-create",
                result["measurement_contract"]["t0"],
            )
            self.assertEqual(
                "not-preloaded-direct-o_direct",
                result["storage_state"]["page_cache_state"],
            )

            drifted = json.loads(paths[2].read_text())
            drifted["semantic"]["cases"][1]["invariant"]["output_sequence"] = "A" * 20
            paths[2].write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaises(aggregate_results.AggregateError):
                aggregate_results.aggregate(paths, "direct")

            missing_metric = trial_summary(3)
            del missing_metric["demand_to_http_ready_seconds"]
            paths[2].write_text(json.dumps(missing_metric), encoding="utf-8")
            with self.assertRaisesRegex(aggregate_results.AggregateError, "HTTP ready"):
                aggregate_results.aggregate(paths, "direct")

    def test_worker_gate_matches_checked_in_provenance_and_is_not_release_ready(self) -> None:
        gate = render.WORKER_GATE
        provenance = (MODULE_DIR / gate["provenance_path"]).resolve()
        self.assertTrue(provenance.is_file())
        self.assertEqual(WORKER_IMAGE, gate["worker_image"])
        self.assertEqual(RESTORE_WORKER_SHA256, gate["restore_worker_sha256"])
        self.assertEqual(TOOL_MANIFEST_SHA256, gate["tool_bundle_manifest_sha256"])
        self.assertEqual(SOURCE_TREE_SHA256, gate["source_materialized_tree_sha256"])
        self.assertEqual(PROVENANCE_SHA256, gate["provenance_sha256"])
        self.assertEqual(
            gate["provenance_sha256"], hashlib.sha256(provenance.read_bytes()).hexdigest()
        )
        provenance_payload = json.loads(provenance.read_text(encoding="utf-8"))
        self.assertEqual(
            SOURCE_TREE_SHA256,
            provenance_payload["integrated_source_validation"][
                "materialized_tree_sha256"
            ],
        )
        self.assertEqual(
            NS_BIND_MOUNT_PATCH_SHA256,
            provenance_payload["upgrade_toolchain"][
                "ns_bind_mount_runtime_patch_sha256"
            ],
        )
        performance_image = provenance_payload["integrated_performance_validation_image"]
        self.assertEqual(WORKER_IMAGE, performance_image["reference"])
        self.assertEqual(
            RESTORE_WORKER_SHA256, performance_image["restore_worker_sha256"]
        )
        self.assertEqual(
            TOOL_MANIFEST_SHA256, performance_image["tool_bundle_manifest_sha256"]
        )
        self.assertTrue(gate["performance_validation_ready"])
        self.assertFalse(gate["release_ready"])
        self.assertEqual(
            gate["tool_bundle_manifest_sha256"], contract()["tool_bundle"]["content_sha256"]
        )
        self.assertEqual(gate["provenance_sha256"], contract()["approval"]["evidence_sha256"])

    def test_runner_pins_contract_and_validator_bytes(self) -> None:
        runner = (MODULE_DIR / "run_one_provisioned_trial.sh").read_text(encoding="utf-8")
        contract_sha256 = hashlib.sha256(
            (MODULE_DIR / "restore-interface.performance.json").read_bytes()
        ).hexdigest()
        validator_sha256 = hashlib.sha256(
            (MODULE_DIR / "validate_evo2.py").read_bytes()
        ).hexdigest()
        self.assertIn(f'expected_contract_sha256="{contract_sha256}"', runner)
        self.assertIn(f'expected_validator_sha256="{validator_sha256}"', runner)
        self.assertEqual(validator_sha256, contract()["validator_sha256"])
        self.assertIn('target_submit_at = (directory / "target-submit-at.txt")', runner)
        self.assertIn("target_submit_at=target_submit_at", runner)

    def test_profile_preserves_retained_evidence_and_defers_new_manifests(self) -> None:
        profile = render.PROFILE
        self.assertEqual(99_959_572_798, profile["retained_evidence"]["legacy_checkpoint_bytes"])
        self.assertEqual(67.39, profile["retained_evidence"]["legacy_direct_n3_median_seconds"])
        self.assertEqual(1, profile["hardware"]["gpu_count"])
        self.assertFalse(profile["hardware"]["mig_allowed"])
        self.assertIsNone(profile["artifacts"]["direct"]["manifest_sha256"])
        self.assertIsNone(profile["artifacts"]["buffered"]["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
