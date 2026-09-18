#!/usr/bin/env python3
"""Offline tests for OpenFold3 exact-response setup and aggregation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml


LANE = Path(__file__).resolve().parents[1]
DYNAMO = LANE / "dynamo"
TEST_NODE = "gpu-node-a.example.invalid"
TEST_IMAGES = {
    "target": "nvcr.io/nim/openfold/openfold3@sha256:"
    "6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2",
    "restore-worker": "registry.example.invalid/faststart/snapshot-agent@sha256:"
    "d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28",
    "semantic-probe": "docker.io/library/python@sha256:"
    "356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
}


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prewarm = _module("openfold3_prewarm", LANE / "prewarm_buffered_artifact.py")
aggregate = _module("openfold3_response_aggregate", DYNAMO / "aggregate_response_n3.py")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class PrewarmTests(unittest.TestCase):
    def test_full_read_verifies_inventory_manifest_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "artifact"
            root.mkdir()
            payloads = {
                "manifest.yaml": (
                    "checkpointId: test-buffered\n"
                    "sourceNode: test-node\n"
                    "podNamespace: nim-fast-start\n"
                    "cudaRestore:\n"
                    "  imageIoMode: buffered\n"
                ).encode(),
                "inventory.img": b"inventory",
                "pstree.img": b"pstree",
                "rootfs-diff.tar": b"rootfs",
                "pages-1.img": b"pages",
            }
            for name, data in payloads.items():
                (root / name).write_bytes(data)
            tree = hashlib.sha256()
            for name in sorted(payloads):
                data = payloads[name]
                tree.update(
                    f"{name}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}\n".encode()
                )
            receipt = prewarm.verify_and_prewarm(
                root,
                expected_file_count=len(payloads),
                expected_regular_bytes=sum(map(len, payloads.values())),
                expected_manifest_sha256=hashlib.sha256(
                    payloads["manifest.yaml"]
                ).hexdigest(),
                expected_tree_sha256=tree.hexdigest(),
                checkpoint_id="test-buffered",
                source_node="test-node",
            )
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["regular_bytes_read"], sum(map(len, payloads.values())))

    def test_symlinked_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "artifact"
            root.mkdir()
            (root / "manifest.yaml").write_text("x", encoding="utf-8")
            (root / "pages-1.img").symlink_to(root / "manifest.yaml")
            with self.assertRaises(prewarm.PrewarmError):
                prewarm.verify_and_prewarm(
                    root,
                    expected_file_count=2,
                    expected_regular_bytes=2,
                    expected_manifest_sha256="0" * 64,
                    expected_tree_sha256="0" * 64,
                    required_members=frozenset({"manifest.yaml"}),
                )


class AggregateTests(unittest.TestCase):
    def _cohort(self, root: Path, batch_id: str = "of3-rb") -> None:
        _write(
            root / "cohort-preflight" / "receipt.json",
            {
                "schema": "archvteams.nebius.ai/openfold3-cohort-preflight/v1",
                "status": "PASS",
                "node": TEST_NODE,
                "active_gpu_requests_on_node": 0,
                "attached_volume_count": 2,
                "worker_request_mcpu": 500,
                "candidate_headroom_after_target_probe_worker_mcpu": 500,
                "required_candidate_headroom_mcpu": 400,
            },
        )
        _write(
            root / "artifact-prewarm-receipt.json",
            {
                "schema": "archvteams.nebius.ai/openfold3-artifact-prewarm/v1",
                "status": "PASS",
                "checkpoint_id": aggregate.CHECKPOINT_ID,
                "artifact_version": aggregate.ARTIFACT_VERSION,
                "image_io_mode": "buffered",
                "regular_file_count": aggregate.ARTIFACT_FILE_COUNT,
                "regular_bytes_read": aggregate.ARTIFACT_BYTES,
                "manifest_sha256": aggregate.ARTIFACT_MANIFEST_SHA256,
                "tree_sha256": aggregate.ARTIFACT_TREE_SHA256,
                "prewarm_source_sha256": aggregate.PREWARM_SOURCE_SHA256,
                "prewarm_outside_t0": True,
                "source_node": TEST_NODE,
                "holder_uid": "holder-uid",
                "full_read_elapsed_seconds": 40.0,
                "completed_at": "2026-08-18T12:00:05Z",
            },
        )
        _write(
            root / "image-residency-receipt.json",
            {
                "schema": "archvteams.nebius.ai/openfold3-image-residency/v1",
                "status": "PASS",
                "node": TEST_NODE,
                "preloaded_outside_t0": True,
                "preloader_absent_before_t0": True,
                "images": TEST_IMAGES,
                "image_ids": {
                    name: f"sha256:{index}"
                    for index, name in enumerate(TEST_IMAGES)
                },
                "verified_at": "2026-08-18T12:00:04Z",
            },
        )
        for index in (1, 2, 3):
            run_id = f"{batch_id}-r{index}"
            t0 = f"2026-08-18T12:01:0{index}Z"
            response_second = 30 + index
            timings = {
                "demand_to_http_ready": 12.0 + index,
                "demand_to_kubernetes_ready": 13.0 + index,
                "semantic_request_1": 8.0 + index / 10,
                "semantic_request_2": 8.1 + index / 10,
                "demand_to_two_semantic_responses": 30.0,
                "worker_restore": 4.0 + index / 10,
            }
            run_dir = root / "runs" / run_id
            _write(
                run_dir / "canary-evidence.json",
                {
                    "schema": "archvteams.nebius.ai/openfold3-production-canary-evidence/v1",
                    "status": "PASS",
                    "run_id": run_id,
                    "request_count": 2,
                    "semantic_pass_count": 2,
                    "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
                    "t0_source": "target-submit-at.txt",
                    "t0_at": t0,
                    "demand_at": t0,
                    "artifact": {
                        "checkpoint_id": aggregate.CHECKPOINT_ID,
                        "image_io_mode": "buffered",
                    },
                    "target": {"uid": f"target-uid-{index}"},
                    "timings_seconds": timings,
                    "evidence": {
                        "http_ready_at": f"2026-08-18T12:01:{12 + index:02d}Z",
                        "kubernetes_ready_at": f"2026-08-18T12:01:{13 + index:02d}Z",
                        "second_response_received_at": f"2026-08-18T12:01:{response_second:02d}Z",
                        "validation_finished_at": f"2026-08-18T12:01:{response_second + 1:02d}Z",
                    },
                },
            )
            _write(
                run_dir / "semantic-summary.json",
                {
                    "status": "PASS",
                    "request_count": 2,
                    "passed_case_count": 2,
                    "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
                    "cases": [
                        {
                            "input_id": f"{run_id}-semantic-a",
                            "status": "PASS",
                            "response_sha256": f"{index:064x}",
                        },
                        {
                            "input_id": f"{run_id}-semantic-b",
                            "status": "PASS",
                            "response_sha256": f"{index + 10:064x}",
                        },
                    ],
                },
            )
            _write(
                run_dir / "cleanup-receipt.json",
                {
                    "schema": "archvteams.nebius.ai/openfold3-cleanup/v1",
                    "status": "PASS",
                    "run_id": run_id,
                    "run_scoped_resource_count": 0,
                    "active_gpu_requests_on_node": 0,
                },
            )
            _write(
                run_dir / "image-events-receipt.json",
                {
                    "schema": "archvteams.nebius.ai/openfold3-trial-image-events/v1",
                    "status": "PASS",
                    "run_id": run_id,
                    "pulling_event_count": 0,
                    "terminal_fault_event_count": 0,
                },
            )

    def test_exact_response_boundary_cohort_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._cohort(root)
            receipt = aggregate.aggregate(
                root,
                "of3-rb",
                TEST_NODE,
                TEST_IMAGES["target"],
                TEST_IMAGES["restore-worker"],
                TEST_IMAGES["semantic-probe"],
            )
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["trial_count"], 3)
            self.assertEqual(receipt["semantic_call_count"], 6)
            self.assertEqual(
                receipt["metrics_seconds"]["demand_to_two_semantic_responses"]["median"],
                30.0,
            )

    def test_validation_completion_is_not_accepted_as_exact_total(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._cohort(root)
            path = root / "runs" / "of3-rb-r2" / "canary-evidence.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["timings_seconds"]["demand_to_two_semantic_responses"] = 31.0
            _write(path, value)
            with self.assertRaisesRegex(aggregate.AggregateError, "does not recompute"):
                aggregate.aggregate(
                    root,
                    "of3-rb",
                    TEST_NODE,
                    TEST_IMAGES["target"],
                    TEST_IMAGES["restore-worker"],
                    TEST_IMAGES["semantic-probe"],
                )


class RunnerContractTests(unittest.TestCase):
    def test_runner_parameterizes_runtime_identity_and_preserves_storage(self) -> None:
        source = (DYNAMO / "run_response_n3.sh").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"mk8scluster-e00[a-z0-9]+")
        self.assertIn("EXPECTED_API_SERVER", source)
        self.assertIn("EXPECTED_CONTEXT", source)
        self.assertIn("TARGET_NODE", source)
        self.assertIn("OPENFOLD3_ARTIFACT_HOLDER", source)
        self.assertIn("OPENFOLD3_ARTIFACT_PVC", source)
        self.assertIn("OPENFOLD3_CACHE_PVC", source)
        self.assertIn("OPENFOLD3_NGC_PULL_SECRET", source)
        self.assertIn("OPENFOLD3_WORKER_PULL_SECRET", source)
        self.assertIn("jq -er '.worker_image'", source)
        self.assertIn("jq -er '.probe_image'", source)
        self.assertIn("readonly worker_request_mcpu=500", source)
        self.assertIn("--preflight-only", source)
        self.assertNotIn('delete pvc "$artifact_pvc"', source)
        self.assertNotIn('delete pod "$holder_name"', source)

    def test_preloader_uses_exact_three_images_without_a_gpu(self) -> None:
        source = (DYNAMO / "image-preload.yaml.tmpl").read_text(encoding="utf-8")
        document = yaml.safe_load(
            source.replace("@@PRELOAD_NAME@@", "of3-test-images")
            .replace("@@BATCH_ID@@", "of3-test")
            .replace("@@NAMESPACE@@", "nim-fast-start")
            .replace("@@TARGET_NODE@@", TEST_NODE)
            .replace("@@TARGET_IMAGE@@", TEST_IMAGES["target"])
            .replace("@@WORKER_IMAGE@@", TEST_IMAGES["restore-worker"])
            .replace("@@PROBE_IMAGE@@", TEST_IMAGES["semantic-probe"])
            .replace("@@NGC_PULL_SECRET@@", "ngc-pull-secret-example")
            .replace("@@WORKER_PULL_SECRET@@", "worker-pull-secret-example")
        )
        containers = document["spec"]["containers"]
        self.assertEqual(
            {item["name"]: item["image"] for item in containers},
            TEST_IMAGES,
        )
        self.assertTrue(
            all(
                "nvidia.com/gpu" not in item["resources"]["requests"]
                for item in containers
            )
        )


if __name__ == "__main__":
    unittest.main()
