#!/usr/bin/env python3
"""Offline tests for the production-shaped conventional MolMIM comparator."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml


TEST_DIR = Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
LANE_DIR = MODULE_DIR.parent
sys.path.insert(0, str(MODULE_DIR))

import aggregate  # noqa: E402
import compare  # noqa: E402
import evidence  # noqa: E402
import render  # noqa: E402


RUN_ID = "cached-ut-r1"
DEMAND = "2026-08-18T06:00:00.000000Z"
UID = "11111111-1111-4111-8111-111111111111"
JOB_UID = "22222222-2222-4222-8222-222222222222"
SMILES_A = "Cn1c(=O)c2c(ncn2CCN(CCO)C(=O)OC(C)(C)C)n(C)c1=O"
SMILES_B = "CC(=O)Oc1ccccc1C(=O)N[C@@H](C)c1ccc(N(C)C)cc1"


def valid_inputs() -> dict:
    target = next(item for item in render.render_target(RUN_ID, DEMAND) if item["kind"] == "Pod")
    target = copy.deepcopy(target)
    target["metadata"]["uid"] = UID
    target["metadata"]["creationTimestamp"] = "2026-08-18T06:00:00Z"
    target["spec"]["nodeName"] = render.NODE
    target["status"] = {
        "phase": "Running",
        "conditions": [
            {
                "type": "Ready",
                "status": "True",
                "lastTransitionTime": "2026-08-18T06:00:17Z",
            }
        ],
        "containerStatuses": [
            {
                "name": "molmim",
                "imageID": render.IMAGE,
                "state": {"running": {"startedAt": "2026-08-18T06:00:02Z"}},
            }
        ],
    }
    probe_job = next(
        item for item in render.render_probe(RUN_ID, DEMAND, UID) if item["kind"] == "Job"
    )
    probe_job = copy.deepcopy(probe_job)
    probe_job["metadata"]["uid"] = JOB_UID
    probe_job["status"] = {"succeeded": 1}
    probe_template = probe_job["spec"]["template"]
    probe_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"molmim-cached-probe-{RUN_ID}-abc12",
            "generateName": f"molmim-cached-probe-{RUN_ID}-",
            "namespace": render.NAMESPACE,
            "labels": copy.deepcopy(probe_template["metadata"]["labels"]),
            "annotations": copy.deepcopy(probe_template["metadata"]["annotations"]),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": f"molmim-cached-probe-{RUN_ID}",
                    "uid": JOB_UID,
                    "controller": True,
                }
            ],
        },
        "spec": copy.deepcopy(probe_template["spec"]),
        "status": {
            "initContainerStatuses": [
                {
                    "name": "stage-validator",
                    "imageID": render.IMAGE,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ],
            "containerStatuses": [
                {
                    "name": "semantic-probe",
                    "imageID": render.IMAGE,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ]
        },
    }
    cases = [
        {
            "index": 1,
            "input_id": "caffeine",
            "run_id": f"{RUN_ID}-semantic-a",
            "ok": True,
            "status": "PASS",
            "exit_code": 0,
            "elapsed_seconds": 2.9,
            "response_received_at": "2026-08-18T06:00:21.500000Z",
            "request_sha256": evidence.REQUEST_SHA256[0],
            "response_bytes": 512,
            "response_sha256": "a" * 64,
            "invariant": {
                "generated_count": 1,
                "smiles": SMILES_A,
                "atom_count": 24,
                "score": 0.774,
                "rdkit_qed": 0.774,
            },
        },
        {
            "index": 2,
            "input_id": "aspirin",
            "run_id": f"{RUN_ID}-semantic-b",
            "ok": True,
            "status": "PASS",
            "exit_code": 0,
            "elapsed_seconds": 2.0,
            "response_received_at": "2026-08-18T06:00:23.500000Z",
            "request_sha256": evidence.REQUEST_SHA256[1],
            "response_bytes": 512,
            "response_sha256": "b" * 64,
            "invariant": {
                "generated_count": 1,
                "smiles": SMILES_B,
                "atom_count": 25,
                "score": 0.677,
                "rdkit_qed": 0.677,
            },
        },
    ]
    semantic = {
        "schema_version": 1,
        "validator": "molmim-faststart-semantic-v1",
        "base_url": f"http://molmim-cached-svc-{RUN_ID}:8000",
        "endpoint": f"http://molmim-cached-svc-{RUN_ID}:8000/generate",
        "inference_path": "/generate",
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "ok": True,
        "status": "PASS",
        "passed_case_count": 2,
        "failed_case_count": 0,
        "exit_code": 0,
        "started_at": "2026-08-18T06:00:03Z",
        "ready_at": "2026-08-18T06:00:18Z",
        "finished_at": "2026-08-18T06:00:24Z",
        "validation_completed_at": "2026-08-18T06:00:24Z",
        "total_elapsed_seconds": 21.0,
        "cases": cases,
    }
    events = {
        "kind": "EventList",
        "items": [
            {
                "involvedObject": {
                    "uid": UID,
                    "fieldPath": "spec.containers{molmim}",
                },
                "reason": "Pulled",
                "message": f"Container image \"{render.IMAGE}\" already present on machine",
            }
        ],
    }
    run = {
        "schema": "archvteams.nebius.ai/molmim-conventional-run/v1",
        "run_id": RUN_ID,
        "demand_at": DEMAND,
        "node": render.NODE,
        "image": render.IMAGE,
        "mode": "conventional-cached",
    }
    prewarm_holder = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "molmim-native-f7-cache-holder-t12",
            "namespace": render.NAMESPACE,
            "uid": "33333333-3333-4333-8333-333333333333",
        },
        "spec": {
            "nodeName": render.NODE,
            "volumes": [
                {
                    "name": "cache",
                    "persistentVolumeClaim": {
                        "claimName": "molmim-native-f7-cache",
                        "readOnly": True,
                    },
                }
            ],
        },
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "lastTransitionTime": "2026-08-18T05:59:58Z",
                }
            ],
            "containerStatuses": [{"name": "holder", "ready": True}],
        },
    }
    prewarm_receipt = {
        "schema": "archvteams.nebius.ai/molmim-cache-holder-receipt/v1",
        "status": "PASS",
        "mode": "cache-full-read",
        "source_receipt_schema": "archvteams.nebius.ai/molmim-cache-receipt/v1",
        "regular_file_count": 2,
        "regular_file_bytes": 284_497_920,
        "unique_bytes": 284_497_920,
        "prewarm_bytes": 284_497_920,
        "tree_sha256": "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c",
        "full_read_elapsed_seconds": 0.25,
    }
    return {
        "run": run,
        "target": target,
        "probe_job": probe_job,
        "probe_pod": probe_pod,
        "semantic": semantic,
        "events": events,
        "prewarm_holder": prewarm_holder,
        "prewarm_receipt": prewarm_receipt,
        "prewarm_captured_at": "2026-08-18T05:59:59Z",
    }


class RenderTests(unittest.TestCase):
    def test_target_is_scheduler_created_exact_cached_comparator(self) -> None:
        documents = render.render_target(RUN_ID, DEMAND)
        self.assertEqual([item["kind"] for item in documents], ["Pod", "Service", "NetworkPolicy"])
        pod = documents[0]
        self.assertNotIn("nodeName", pod["spec"])
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], render.IMAGE)
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(container["command"], ["/opt/nvidia/nvidia_entrypoint.sh"])
        self.assertEqual(container["args"], ["start_server"])
        self.assertEqual(container["resources"]["requests"]["nvidia.com/gpu"], "1")
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(environment["TORCHINDUCTOR_COMPILE_THREADS"]["value"], "1")
        self.assertEqual(environment["NIM_CACHE_PATH"]["value"], "/home/nvs/.cache/nim")
        self.assertEqual(
            environment["NGC_API_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "ngc-api-key", "key": "NGC_API_KEY"},
        )
        self.assertEqual(
            pod["spec"]["volumes"][1]["persistentVolumeClaim"],
            {"claimName": "molmim-native-f7-cache"},
        )
        self.assertNotIn("readOnly", container["volumeMounts"][1])
        self.assertNotIn("capabilities", container["securityContext"])

    def test_probe_is_separate_cpu_only_exactly_two_call_job(self) -> None:
        documents = render.render_probe(RUN_ID, DEMAND, UID)
        self.assertEqual([item["kind"] for item in documents], ["ConfigMap", "Job", "NetworkPolicy"])
        config, job = documents[:2]
        self.assertTrue(config["immutable"])
        self.assertEqual(
            config["metadata"]["annotations"]["archvteams.nebius.ai/target-pod-uid"], UID
        )
        spec = job["spec"]["template"]["spec"]
        self.assertNotIn("runtimeClassName", spec)
        self.assertEqual(len(spec["containers"]), 1)
        probe = spec["containers"][0]
        self.assertEqual(probe["image"], render.IMAGE)
        self.assertEqual(probe["command"], ["/usr/bin/python3"])
        self.assertEqual(probe["args"].count("--run-id"), 2)
        self.assertTrue(
            all("gpu" not in key.lower() for group in probe["resources"].values() for key in group)
        )
        self.assertEqual(
            config["data"]["validate_molmim.py"].encode(),
            render.VALIDATOR.read_bytes(),
        )

    def test_invalid_run_and_uid_fail_closed(self) -> None:
        with self.assertRaises(render.RenderError):
            render.render_target("Bad_ID", DEMAND)
        with self.assertRaises(render.RenderError):
            render.render_probe(RUN_ID, DEMAND, "not-a-uid")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = render.main(["target", "--run-id", "Bad_ID", "--demand-at", DEMAND])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("refused", stderr.getvalue())

    def test_image_holder_primes_exact_image_without_gpu(self) -> None:
        holder = yaml.safe_load((MODULE_DIR / "image-cache-holder.yaml").read_text())
        container = holder["spec"]["containers"][0]
        self.assertEqual(container["image"], render.IMAGE)
        self.assertEqual(holder["spec"]["nodeName"], render.NODE)
        self.assertEqual(container["command"], ["/usr/bin/python3", "-c"])
        self.assertTrue(
            all("gpu" not in key.lower() for group in container["resources"].values() for key in group)
        )


class EvidenceTests(unittest.TestCase):
    def build(self, values: dict | None = None) -> dict:
        values = valid_inputs() if values is None else values
        return evidence.build(
            values["run"],
            values["target"],
            values["probe_job"],
            values["probe_pod"],
            values["semantic"],
            values["events"],
            prewarm_holder=values["prewarm_holder"],
            prewarm_receipt=values["prewarm_receipt"],
            prewarm_captured_at=values["prewarm_captured_at"],
        )

    def test_valid_cached_trial_produces_demand_to_two_semantics(self) -> None:
        receipt = self.build()
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["request_count"], 2)
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 18.0)
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_kubernetes_ready"], 17.0
        )
        self.assertEqual(receipt["timings_seconds"]["demand_to_two_semantic_responses"], 23.5)
        self.assertEqual(receipt["timings_seconds"]["demand_to_validation_complete"], 24.0)
        self.assertEqual(receipt["storage_prewarm"]["mode"], "cache-full-read")
        self.assertEqual(receipt["execution_identity"]["image_already_present_event_count"], 1)

    def test_target_submit_timestamp_is_authoritative_t0(self) -> None:
        values = valid_inputs()
        receipt = evidence.build(
            values["run"],
            values["target"],
            values["probe_job"],
            values["probe_pod"],
            values["semantic"],
            values["events"],
            target_submit_at="2026-08-18T06:00:00.500000Z",
            prewarm_holder=values["prewarm_holder"],
            prewarm_receipt=values["prewarm_receipt"],
            prewarm_captured_at=values["prewarm_captured_at"],
        )
        self.assertEqual(receipt["measurement"]["t0"], "2026-08-18T06:00:00.500000Z")
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 17.5)

    def test_missing_cached_image_event_is_rejected(self) -> None:
        values = valid_inputs()
        values["events"]["items"] = []
        with self.assertRaisesRegex(evidence.EvidenceError, "already cached"):
            self.build(values)

    def test_cached_image_event_for_wrong_container_is_rejected(self) -> None:
        values = valid_inputs()
        values["events"]["items"][0]["involvedObject"]["fieldPath"] = (
            "spec.initContainers{setup}"
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "already cached"):
            self.build(values)

    def test_cached_image_event_with_nonexact_image_is_rejected(self) -> None:
        values = valid_inputs()
        values["events"]["items"][0]["message"] = (
            f'Container image "{render.IMAGE}-forged" already present on machine'
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "already cached"):
            self.build(values)

    def test_target_cache_or_security_mutation_is_rejected(self) -> None:
        values = valid_inputs()
        values["target"]["spec"]["volumes"][1]["persistentVolumeClaim"][
            "readOnly"
        ] = True
        with self.assertRaisesRegex(evidence.EvidenceError, "target Pod"):
            self.build(values)

        values = valid_inputs()
        values["target"]["spec"]["containers"][0]["securityContext"][
            "allowPrivilegeEscalation"
        ] = True
        with self.assertRaisesRegex(evidence.EvidenceError, "target Pod"):
            self.build(values)

    def test_probe_process_image_and_owner_mutations_are_rejected(self) -> None:
        values = valid_inputs()
        values["probe_job"]["spec"]["template"]["spec"]["containers"][0][
            "command"
        ] = ["/bin/true"]
        with self.assertRaisesRegex(evidence.EvidenceError, "semantic probe Job"):
            self.build(values)

        values = valid_inputs()
        values["probe_pod"]["status"]["containerStatuses"][0]["imageID"] = (
            "docker.io/library/python@sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "did not exit successfully"):
            self.build(values)

        values = valid_inputs()
        values["probe_pod"]["metadata"]["ownerReferences"][0]["uid"] = UID
        with self.assertRaisesRegex(evidence.EvidenceError, "not controlled"):
            self.build(values)

    def test_wrong_qed_or_duplicate_candidate_is_rejected(self) -> None:
        values = valid_inputs()
        values["semantic"]["cases"][0]["invariant"]["rdkit_qed"] = 0.2
        with self.assertRaisesRegex(evidence.EvidenceError, "RDKit QED"):
            self.build(values)
        values = valid_inputs()
        values["semantic"]["cases"][1]["invariant"]["smiles"] = SMILES_A
        with self.assertRaisesRegex(evidence.EvidenceError, "same molecule"):
            self.build(values)

    def test_nonfinite_or_out_of_range_semantic_receipts_are_rejected(self) -> None:
        values = valid_inputs()
        values["semantic"]["cases"][0]["invariant"]["score"] = 1.1
        values["semantic"]["cases"][0]["invariant"]["rdkit_qed"] = 1.1
        with self.assertRaisesRegex(evidence.EvidenceError, "RDKit QED"):
            self.build(values)

        values = valid_inputs()
        values["semantic"]["cases"][0]["response_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(evidence.EvidenceError, "response receipt"):
            self.build(values)


class AggregationTests(unittest.TestCase):
    def test_n3_runner_requires_cleanup_before_trial_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runs").mkdir()
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(MODULE_DIR / "run_cached_n3.sh"),
                    "--run-prefix",
                    "cached-cleanup-ut",
                    "--evidence-root",
                    str(root),
                    "--node",
                    render.NODE,
                    "--kubeconfig",
                    str(root / "unused-kubeconfig"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 64)
        self.assertIn(
            "--cleanup is required between every counted n=3 trial", result.stderr
        )

    def _receipt(self, suffix: int, elapsed: float) -> dict:
        values = valid_inputs()
        value = self.build_from(values)
        value["run_id"] = f"cached-ut-r{suffix}"
        value["timings_seconds"]["demand_to_two_semantic_responses"] = elapsed
        return value

    @staticmethod
    def build_from(values: dict) -> dict:
        return evidence.build(
            values["run"], values["target"], values["probe_job"], values["probe_pod"],
            values["semantic"], values["events"],
            prewarm_holder=values["prewarm_holder"],
            prewarm_receipt=values["prewarm_receipt"],
            prewarm_captured_at=values["prewarm_captured_at"],
        )

    def test_n3_median_and_restore_rejection(self) -> None:
        conventional = aggregate.aggregate(
            [self._receipt(1, 23.0), self._receipt(2, 24.0), self._receipt(3, 25.0)]
        )
        self.assertEqual(conventional["statistics_seconds"]["demand_to_two_semantic_median"], 24.0)
        native = {
            "schema": "archvteams.nebius.ai/molmim-native-n3/v1",
            "status": "PASS",
            "checkpoint_id": "molmim-native-f7-v2-buffered",
            "image_io_mode": "buffered",
            "trial_count": 3,
            "request_count": 6,
            "semantic_pass_count": 6,
            "run_ids": ["native-r1", "native-r2", "native-r3"],
            "demand_to_two_semantic_seconds": [37.0, 38.0, 39.0],
            "statistics_seconds": {"demand_to_two_semantic_median": 38.0},
        }
        verdict = compare.compare(conventional, native)
        self.assertEqual(verdict["status"], "REJECTED")
        self.assertEqual(
            verdict["recommendation"], "REJECT_NATIVE_RESTORE_KEEP_CONVENTIONAL_CACHED"
        )
        native["statistics_seconds"]["demand_to_two_semantic_median"] = 10.0
        native["demand_to_two_semantic_seconds"] = [9.0, 10.0, 11.0]
        self.assertEqual(compare.compare(conventional, native)["status"], "PASS")

    def test_aggregation_and_comparison_reject_nonfinite_timings(self) -> None:
        receipts = [self._receipt(1, 23.0), self._receipt(2, 24.0), self._receipt(3, 25.0)]
        receipts[1]["timings_seconds"]["demand_to_two_semantic_responses"] = float("nan")
        with self.assertRaisesRegex(aggregate.AggregateError, "positive and finite"):
            aggregate.aggregate(receipts)

        conventional = aggregate.aggregate(
            [self._receipt(1, 23.0), self._receipt(2, 24.0), self._receipt(3, 25.0)]
        )
        native = {
            "schema": "archvteams.nebius.ai/molmim-native-n3/v1",
            "status": "PASS",
            "checkpoint_id": "molmim-native-f7-v2-buffered",
            "image_io_mode": "buffered",
            "trial_count": 3,
            "request_count": 6,
            "semantic_pass_count": 6,
            "run_ids": ["native-r1", "native-r2", "native-r3"],
            "demand_to_two_semantic_seconds": [37.0, float("inf"), 39.0],
            "statistics_seconds": {"demand_to_two_semantic_median": float("inf")},
        }
        with self.assertRaisesRegex(compare.ComparisonError, "positive and finite"):
            compare.compare(conventional, native)

    def test_comparison_recomputes_medians_and_requires_independent_runs(self) -> None:
        conventional = aggregate.aggregate(
            [self._receipt(1, 23.0), self._receipt(2, 24.0), self._receipt(3, 25.0)]
        )
        native = {
            "schema": "archvteams.nebius.ai/molmim-native-n3/v1",
            "status": "PASS",
            "checkpoint_id": "molmim-native-f7-v2-buffered",
            "image_io_mode": "buffered",
            "trial_count": 3,
            "request_count": 6,
            "semantic_pass_count": 6,
            "run_ids": ["native-r1", "native-r2", "native-r3"],
            "demand_to_two_semantic_seconds": [9.0, 10.0, 11.0],
            "statistics_seconds": {"demand_to_two_semantic_median": 9.0},
        }
        with self.assertRaisesRegex(compare.ComparisonError, "does not match"):
            compare.compare(conventional, native)

        native["statistics_seconds"]["demand_to_two_semantic_median"] = 10.0
        native["run_ids"][2] = "native-r1"
        with self.assertRaisesRegex(compare.ComparisonError, "three unique"):
            compare.compare(conventional, native)


if __name__ == "__main__":
    unittest.main()
