from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import sys

TEST_DIR = Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
sys.path.insert(0, str(MODULE_DIR))

import bind_target  # noqa: E402
import evidence  # noqa: E402
import render  # noqa: E402
try:
    from test_scaffold import approved_contract, run_config  # noqa: E402
except ImportError:  # pragma: no cover - package discovery path
    from .test_scaffold import approved_contract, run_config  # type: ignore[no-redef] # noqa: E402


POD_UID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "containerd://" + "a" * 64
CROSS_LANGUAGE_SPEC_SHA256 = (
    "ad697a3bfb32631ca755961c83c73b59ccd9e49e9c5f9e146fc426888c95e64b"
)


def live_target() -> dict:
    contract = render.validate_contract(approved_contract())
    run = render.validate_run(run_config())
    documents = render.render_target(run, contract)
    pod = copy.deepcopy(next(item for item in documents if item["kind"] == "Pod"))
    pod["metadata"].update(
        {
            "uid": POD_UID,
            "creationTimestamp": "2026-08-17T20:00:01Z",
        }
    )
    # These fields represent API defaulting plus scheduler placement.  They are
    # deliberately part of the canonical hash rather than the source template.
    pod["spec"].update(
        {
            "nodeName": "gpu-node-a.example.invalid",
            "dnsPolicy": "ClusterFirst",
            "schedulerName": "default-scheduler",
            "serviceAccountName": "default",
        }
    )
    # The API server marshals these plain false bools with `omitempty`; a live
    # `GET /pods/...` response therefore omits them even though the submitted
    # manifest states them explicitly.
    for omitted_false in ("hostIPC", "hostNetwork", "hostPID"):
        del pod["spec"][omitted_false]
    pod["status"] = {
        "phase": "Running",
        "qosClass": "Burstable",
        "podIP": "10.50.42.7",
        "podIPs": [{"ip": "10.50.42.7"}],
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "True",
                "lastTransitionTime": "2026-08-17T20:00:02Z",
            },
            {
                "type": "Ready",
                "status": "True",
                "lastTransitionTime": "2026-08-17T20:00:07Z",
            },
        ],
        "containerStatuses": [
            {
                "name": "msa-search",
                "containerID": CONTAINER_ID,
                "imageID": render.NIM_IMAGE,
                "state": {"running": {"startedAt": "2026-08-17T20:00:03Z"}},
            }
        ],
    }
    return pod


def bound_inputs() -> tuple[dict, dict, dict]:
    run = render.validate_run(run_config())
    target = live_target()
    binding, _ = bind_target.build_binding(
        target, run, approved_contract(), "2026-08-17T20:00:03Z"
    )
    target["metadata"]["annotations"][bind_target.POD_SPEC_HASH_KEY] = binding[
        "pod_spec_sha256"
    ]
    return run, target, binding


def evidence_inputs() -> dict:
    run, target, binding = bound_inputs()
    contract = render.validate_contract(approved_contract())
    run_id = run["run_id"]
    worker_name = f"msa-restore-{run_id}"
    probe_name = f"msa-semantic-{run_id}"
    worker_job = copy.deepcopy(
        next(
            item
            for item in render.render_restore(run, contract, binding)
            if item["kind"] == "Job"
        )
    )
    worker_job["status"] = {
        "succeeded": 1,
        "completionTime": "2026-08-17T20:00:06.100000Z",
    }
    worker_pod = {
        "status": {
            "containerStatuses": [
                {
                    "name": "restore-worker",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "startedAt": "2026-08-17T20:00:04Z",
                            "finishedAt": "2026-08-17T20:00:06Z",
                        }
                    },
                }
            ]
        }
    }
    worker_receipt = {
        "schema": evidence.WORKER_RECEIPT_SCHEMA,
        "status": "succeeded",
        "completed_at": "2026-08-17T20:00:05.900000Z",
        "duration_ms": 1500,
        "run_id": run_id,
        "target_namespace": render.NAMESPACE,
        "target_name": f"msa-target-{run_id}",
        "target_uid": POD_UID,
        "target_container_id": CONTAINER_ID,
        "target_image_id": binding["image_id"],
        "target_node": binding["node"],
        "target_pod_ip": binding["pod_ip"],
        "target_pod_spec_sha256": binding["pod_spec_sha256"],
        "checkpoint_id": run["checkpoint_id"],
        "artifact_version": run["artifact_version"],
        "checkpoint_manifest_sha256": run["artifact_manifest_sha256"],
        "tool_bundle_manifest_sha256": approved_contract()["tool_bundle"][
            "content_sha256"
        ],
    }
    probe_job = copy.deepcopy(
        next(
            item
            for item in render.render_probe(run, contract, binding)
            if item["kind"] == "Job"
        )
    )
    probe_job["status"] = {
        "succeeded": 1,
        "completionTime": "2026-08-17T20:00:10.100000Z",
    }
    probe_pod = {
        "status": {
            "containerStatuses": [
                {
                    "name": "semantic-probe",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "startedAt": "2026-08-17T20:00:08Z",
                            "finishedAt": "2026-08-17T20:00:10Z",
                        }
                    },
                }
            ]
        }
    }
    service_name = f"msa-canary-{run_id}"
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": service_name,
            "namespace": render.NAMESPACE,
            "uid": "33333333-3333-4333-8333-333333333333",
        },
        "spec": {
            "type": "ClusterIP",
            "clusterIP": "10.96.42.7",
            "selector": {
                "app.kubernetes.io/name": "msa-search",
                "app.kubernetes.io/component": "restore-target",
                "archvteams.nebius.ai/run-id": run_id,
            },
            "ports": [
                {
                    "name": "http",
                    "port": 8000,
                    "targetPort": "http",
                    "protocol": "TCP",
                }
            ],
        },
    }
    slices = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSliceList",
        "items": [
            {
                "metadata": {
                    "labels": {"kubernetes.io/service-name": service_name},
                    "ownerReferences": [
                        {
                            "kind": "Service",
                            "uid": "33333333-3333-4333-8333-333333333333",
                            "controller": True,
                        }
                    ],
                },
                "endpoints": [
                    {
                        "addresses": [binding["pod_ip"]],
                        "conditions": {"ready": True},
                        "targetRef": {"uid": POD_UID},
                    }
                ],
            }
        ],
    }
    semantic = {
        "schema_version": 1,
        "validator": "msa-search-pdb70-faststart-semantic-v1",
        "base_url": f"http://{service_name}:8000",
        "endpoint": f"http://{service_name}:8000/biology/colabfold/msa-search/predict",
        "inference_path": "/biology/colabfold/msa-search/predict",
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "ok": True,
        "status": "PASS",
        "passed_case_count": 2,
        "failed_case_count": 0,
        "exit_code": 0,
        "request_count": 2,
        "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
        "queries_distinct": True,
        "expected_records_per_response": 128,
        "expected_non_query_homologs_per_response": 127,
        "mmseqs_pipe_expectation": "separately-qualified-in-target",
        "started_at": "2026-08-17T20:00:08.100000Z",
        "ready_at": "2026-08-17T20:00:08.200000Z",
        "finished_at": "2026-08-17T20:00:09.900000Z",
        "validation_finished_at": "2026-08-17T20:00:09.900000Z",
        "total_elapsed_seconds": 1.8,
        "validation_total_elapsed_seconds": 1.8,
        "cases": [
            {
                "index": 1,
                "input_id": f"{run_id}-semantic-a",
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": 0.8,
                "request_started_at": "2026-08-17T20:00:08.300000Z",
                "response_received_at": "2026-08-17T20:00:09.000000Z",
                "query_sha256": "233b4b0b8c4616095bc3249f9375fc345d32af7deaef07117d0383c51d6f19aa",
                "response_sha256": "1" * 64,
                "invariant": {
                    "database": "pdb70_220313",
                    "search_type": "colabfold",
                    "alignment_format": "a3m",
                    "records": 128,
                    "non_query_homologs": 127,
                    "query_length": 76,
                    "query_echo": True,
                    "query_sha256": "233b4b0b8c4616095bc3249f9375fc345d32af7deaef07117d0383c51d6f19aa",
                    "alignment_sha256": "3" * 64,
                },
            },
            {
                "index": 2,
                "input_id": f"{run_id}-semantic-b",
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": 0.9,
                "request_started_at": "2026-08-17T20:00:09.100000Z",
                "response_received_at": "2026-08-17T20:00:09.600000Z",
                "query_sha256": "f0d4533cd51311bf988a54528938464a086a2810380f2a6d1b266f7c62ceacba",
                "response_sha256": "2" * 64,
                "invariant": {
                    "database": "pdb70_220313",
                    "search_type": "colabfold",
                    "alignment_format": "a3m",
                    "records": 128,
                    "non_query_homologs": 127,
                    "query_length": 76,
                    "query_echo": True,
                    "query_sha256": "f0d4533cd51311bf988a54528938464a086a2810380f2a6d1b266f7c62ceacba",
                    "alignment_sha256": "4" * 64,
                },
            },
        ],
    }
    return {
        "contract": approved_contract(),
        "run": run,
        "target_submit_at": "2026-08-17T20:00:00Z",
        "binding": binding,
        "target": target,
        "service": service,
        "endpoint_slices": slices,
        "worker_job": worker_job,
        "worker_pod": worker_pod,
        "worker_receipt": worker_receipt,
        "probe_job": probe_job,
        "probe_pod": probe_pod,
        "semantic_summary": semantic,
    }


class BindingTests(unittest.TestCase):
    def test_python_matches_go_worker_canonical_pod_spec_fixture(self) -> None:
        fixture_path = TEST_DIR / "fixtures" / "msa-search-api-pod-spec.json"
        spec = json.loads(fixture_path.read_text(encoding="utf-8"))
        # This fixed digest is generated by the worker's Go path: unmarshal the
        # API-returned JSON into corev1.PodSpec, json.Marshal it, decode with
        # UseNumber, then sorted compact encoding with HTML escaping disabled.
        self.assertEqual(bind_target.pod_spec_sha256(spec), CROSS_LANGUAGE_SPEC_SHA256)
        self.assertEqual(spec, live_target()["spec"])

    def test_live_defaulted_pod_spec_is_hashed_and_uid_patched(self) -> None:
        run = render.validate_run(run_config())
        pod = live_target()
        binding, patch = bind_target.build_binding(
            pod, run, approved_contract(), "2026-08-17T20:00:03Z"
        )
        expected = bind_target.pod_spec_sha256(pod["spec"])
        self.assertEqual(binding["pod_spec_sha256"], expected)
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/uid", "value": POD_UID})
        self.assertEqual(patch[1]["value"], expected)

    def test_spec_change_after_existing_binding_is_rejected(self) -> None:
        run, pod, _ = bound_inputs()
        pod["spec"]["terminationGracePeriodSeconds"] = 7
        with self.assertRaisesRegex(bind_target.BindingError, "existing target PodSpec"):
            bind_target.build_binding(
                pod, run, approved_contract(), "2026-08-17T20:00:03Z"
            )

    def test_runtime_cgroup_is_derived_from_bound_uid_and_container(self) -> None:
        binding, _ = bind_target.build_binding(
            live_target(),
            render.validate_run(run_config()),
            approved_contract(),
            "2026-08-17T20:00:03Z",
        )
        self.assertIn(POD_UID.replace("-", "_"), binding["cgroup"])
        self.assertIn("a" * 64, binding["cgroup"])

    def test_non_burstable_qos_is_rejected_before_cgroup_derivation(self) -> None:
        pod = live_target()
        pod["status"]["qosClass"] = "Guaranteed"
        with self.assertRaisesRegex(bind_target.BindingError, "QoS"):
            bind_target.build_binding(
                pod,
                render.validate_run(run_config()),
                approved_contract(),
                "2026-08-17T20:00:03Z",
            )

    def test_bind_cli_writes_new_binding_and_uid_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = {
                "contract.json": approved_contract(),
                "run.json": run_config(),
                "pod.json": live_target(),
            }
            for name, value in inputs.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = bind_target.main(
                    [
                        "--contract",
                        str(root / "contract.json"),
                        "--run-config",
                        str(root / "run.json"),
                        "--pod-json",
                        str(root / "pod.json"),
                        "--collected-at",
                        "2026-08-17T20:00:03Z",
                        "--binding-output",
                        str(root / "binding.json"),
                        "--patch-output",
                        str(root / "patch.json"),
                    ]
                )
            self.assertEqual(status, 0, stderr.getvalue())
            binding = json.loads((root / "binding.json").read_text(encoding="utf-8"))
            patch = json.loads((root / "patch.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["pod_spec_sha256"], patch[1]["value"])
            self.assertEqual(json.loads(stdout.getvalue())["status"], "bound")


class EvidenceTests(unittest.TestCase):
    def test_probe_may_start_before_worker_and_wait_for_readiness(self) -> None:
        inputs = evidence_inputs()
        terminated = inputs["probe_pod"]["status"]["containerStatuses"][0]["state"][
            "terminated"
        ]
        terminated["startedAt"] = "2026-08-17T20:00:03.500000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["status"], "PASS")

    def test_two_semantic_responses_produce_demand_timing(self) -> None:
        receipt = evidence.build_evidence(**evidence_inputs())
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["request_count"], 2)
        self.assertEqual(
            receipt["response_timing_contract"],
            "request-dispatch-to-complete-http-body/v1",
        )
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_two_semantic_responses"], 9.6
        )
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 8.2)
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_kubernetes_ready"], 7.0
        )
        self.assertEqual(
            receipt["demand_clock_source"],
            "target-submit-at-immediately-before-create",
        )
        self.assertEqual(receipt["orchestration_started_at"], run_config()["demand_at"])
        self.assertEqual(receipt["timings_seconds"]["semantic_request_1"], 0.8)
        self.assertEqual(receipt["timings_seconds"]["semantic_request_2"], 0.9)
        self.assertEqual(receipt["timings_seconds"]["worker_restore"], 1.5)
        self.assertEqual(
            receipt["artifact"]["target_glibc_version"],
            run_config()["target_glibc_version"],
        )
        self.assertTrue(receipt["evidence"]["release_ready"])
        self.assertEqual(
            receipt["evidence"]["worker_classification"],
            "full-agent-compliance-release",
        )
        self.assertEqual(receipt["artifact"]["image_io_mode"], "direct")
        self.assertEqual(
            receipt["evidence"]["tool_bundle_manifest_sha256"],
            approved_contract()["tool_bundle"]["content_sha256"],
        )
        self.assertEqual(
            receipt["evidence"]["tool_bundle_glibc_compatibility_sha256"],
            approved_contract()["tool_bundle"]["glibc_compatibility_sha256"],
        )

    def test_endpoint_uid_mismatch_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["endpoint_slices"]["items"][0]["endpoints"][0]["targetRef"]["uid"] = (
            "22222222-2222-4222-8222-222222222222"
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "ready bound target"):
            evidence.build_evidence(**inputs)

    def test_generic_kubectl_list_envelope_is_accepted(self) -> None:
        inputs = evidence_inputs()
        inputs["endpoint_slices"]["apiVersion"] = "v1"
        inputs["endpoint_slices"]["kind"] = "List"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["status"], "PASS")

    def test_kubernetes_ready_timestamp_subsecond_quantization_is_accepted(self) -> None:
        inputs = evidence_inputs()
        for condition in inputs["target"]["status"]["conditions"]:
            if condition["type"] == "Ready":
                condition["lastTransitionTime"] = "2026-08-17T20:00:05Z"
        inputs["worker_receipt"]["completed_at"] = "2026-08-17T20:00:05.900000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["status"], "PASS")

    def test_kubernetes_ready_may_precede_restore_receipt(self) -> None:
        inputs = evidence_inputs()
        for condition in inputs["target"]["status"]["conditions"]:
            if condition["type"] == "Ready":
                condition["lastTransitionTime"] = "2026-08-17T20:00:04Z"
        inputs["worker_receipt"]["completed_at"] = "2026-08-17T20:00:05.900000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["timings_seconds"]["demand_to_kubernetes_ready"], 4.0)

    def test_target_creation_subsecond_quantization_is_normalized(self) -> None:
        inputs = evidence_inputs()
        inputs["target_submit_at"] = "2026-08-17T20:00:00.135000Z"
        inputs["target"]["metadata"]["creationTimestamp"] = "2026-08-17T20:00:00Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["timings_seconds"]["demand_to_target_created"], 0.0)

    def test_target_creation_one_second_inversion_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["target_submit_at"] = "2026-08-17T20:00:01Z"
        inputs["target"]["metadata"]["creationTimestamp"] = "2026-08-17T20:00:00Z"
        with self.assertRaisesRegex(evidence.EvidenceError, "at least one second"):
            evidence.build_evidence(**inputs)

    def test_submit_edge_timestamp_not_earlier_run_timestamp_drives_demand(self) -> None:
        inputs = evidence_inputs()
        inputs["run"]["demand_at"] = "2026-08-17T19:59:58Z"
        inputs["target_submit_at"] = "2026-08-17T20:00:00.250000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["demand_at"], inputs["target_submit_at"])
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 7.95)

    def test_submit_edge_before_orchestration_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["target_submit_at"] = "2026-08-17T19:59:59Z"
        with self.assertRaisesRegex(evidence.EvidenceError, "precedes run orchestration"):
            evidence.build_evidence(**inputs)

    def test_http_ready_may_precede_restore_receipt(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["ready_at"] = "2026-08-17T20:00:08.200000Z"
        inputs["worker_receipt"]["completed_at"] = "2026-08-17T20:00:08.500000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 8.2)
        self.assertEqual(receipt["timings_seconds"]["demand_to_restore_receipt"], 8.5)

    def test_more_than_two_semantic_cases_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"].append(
            copy.deepcopy(inputs["semantic_summary"]["cases"][1])
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "exactly two cases"):
            evidence.build_evidence(**inputs)

    def test_legacy_response_boundary_evidence_is_rejected(self) -> None:
        inputs = evidence_inputs()
        del inputs["semantic_summary"]["cases"][1]["response_received_at"]
        with self.assertRaises(evidence.EvidenceError):
            evidence.build_evidence(**inputs)

    def test_non_128_record_semantic_receipt_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"][1]["invariant"]["records"] = 127
        with self.assertRaisesRegex(evidence.EvidenceError, "expected strict PASS"):
            evidence.build_evidence(**inputs)

    def test_identical_distinct_query_responses_are_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"][1]["response_sha256"] = "1" * 64
        with self.assertRaisesRegex(evidence.EvidenceError, "identical response"):
            evidence.build_evidence(**inputs)

    def test_worker_argument_drift_is_rejected(self) -> None:
        inputs = evidence_inputs()
        args = inputs["worker_job"]["spec"]["template"]["spec"]["containers"][0][
            "args"
        ]
        args[args.index("--target-uid") + 1] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(evidence.EvidenceError, "args does not match"):
            evidence.build_evidence(**inputs)

    def test_semantic_summary_from_another_service_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["base_url"] = "http://another-service:8000"
        with self.assertRaisesRegex(evidence.EvidenceError, "exact canary Service"):
            evidence.build_evidence(**inputs)

    def test_evidence_cli_emits_one_pass_receipt(self) -> None:
        values = evidence_inputs()
        filenames = {
            "contract": "contract.json",
            "run": "run.json",
            "binding": "binding.json",
            "target": "target.json",
            "service": "service.json",
            "endpoint_slices": "slices.json",
            "worker_job": "worker-job.json",
            "worker_pod": "worker-pod.json",
            "worker_receipt": "worker-receipt.json",
            "probe_job": "probe-job.json",
            "probe_pod": "probe-pod.json",
            "semantic_summary": "semantic.json",
        }
        flags = {
            "contract": "--contract",
            "run": "--run-config",
            "binding": "--binding",
            "target": "--target-pod",
            "service": "--service",
            "endpoint_slices": "--endpoint-slices",
            "worker_job": "--worker-job",
            "worker_pod": "--worker-pod",
            "worker_receipt": "--worker-receipt",
            "probe_job": "--probe-job",
            "probe_pod": "--probe-pod",
            "semantic_summary": "--semantic-summary",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            submit_path = root / "target-submit-at.txt"
            submit_path.write_text(values["target_submit_at"] + "\n", encoding="utf-8")
            argv: list[str] = ["--target-submit-at", str(submit_path)]
            for key, filename in filenames.items():
                path = root / filename
                path.write_text(json.dumps(values[key]), encoding="utf-8")
                argv.extend([flags[key], str(path)])
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = evidence.main(argv)
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
