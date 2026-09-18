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
WORKER_JOB_UID = "22222222-2222-4222-8222-222222222222"
PROBE_JOB_UID = "44444444-4444-4444-8444-444444444444"
CONTAINER_ID = "containerd://" + "a" * 64
CROSS_LANGUAGE_SPEC_SHA256 = (
    "63bc5db1b138d92751b6860c93b501657553b727cdaf7d88334939b7079b0d1d"
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
            "nodeName": "gpu-node-b.example.invalid",
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
                "name": "molmim",
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
    worker_name = f"molmim-restore-{run_id}"
    probe_name = f"molmim-semantic-{run_id}"
    worker_job = copy.deepcopy(
        next(
            item
            for item in render.render_restore(run, contract, binding)
            if item["kind"] == "Job"
        )
    )
    worker_job["metadata"]["uid"] = WORKER_JOB_UID
    worker_job["status"] = {
        "succeeded": 1,
        "completionTime": "2026-08-17T20:00:06.100000Z",
    }
    worker_template = worker_job["spec"]["template"]
    worker_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{worker_name}-abc12",
            "generateName": f"{worker_name}-",
            "namespace": render.NAMESPACE,
            "labels": copy.deepcopy(worker_template["metadata"]["labels"]),
            "annotations": copy.deepcopy(
                worker_template["metadata"].get("annotations", {})
            ),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": worker_name,
                    "uid": WORKER_JOB_UID,
                    "controller": True,
                }
            ],
        },
        "spec": copy.deepcopy(worker_template["spec"]),
        "status": {
            "containerStatuses": [
                {
                    "name": "restore-worker",
                    "imageID": contract["worker_image"],
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
        "target_name": f"molmim-target-{run_id}",
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
    probe_job["metadata"]["uid"] = PROBE_JOB_UID
    probe_job["status"] = {
        "succeeded": 1,
        "completionTime": "2026-08-17T20:00:10.100000Z",
    }
    probe_template = probe_job["spec"]["template"]
    probe_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"{probe_name}-abc12",
            "generateName": f"{probe_name}-",
            "namespace": render.NAMESPACE,
            "labels": copy.deepcopy(probe_template["metadata"]["labels"]),
            "annotations": copy.deepcopy(
                probe_template["metadata"].get("annotations", {})
            ),
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": probe_name,
                    "uid": PROBE_JOB_UID,
                    "controller": True,
                }
            ],
        },
        "spec": copy.deepcopy(probe_template["spec"]),
        "status": {
            "containerStatuses": [
                {
                    "name": "semantic-probe",
                    "imageID": contract["probe_image"],
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
    service_name = f"molmim-canary-{run_id}"
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
                "app.kubernetes.io/name": "molmim",
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
        "validator": "molmim-faststart-semantic-v1",
        "base_url": f"http://{service_name}:8000",
        "endpoint": f"http://{service_name}:8000/generate",
        "inference_path": "/generate",
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "ok": True,
        "status": "PASS",
        "passed_case_count": 2,
        "failed_case_count": 0,
        "exit_code": 0,
        "started_at": "2026-08-17T20:00:08.100000Z",
        "ready_at": "2026-08-17T20:00:08.500000Z",
        "finished_at": "2026-08-17T20:00:09.900000Z",
        "validation_completed_at": "2026-08-17T20:00:09.900000Z",
        "total_elapsed_seconds": 1.8,
        "cases": [
            {
                "index": 1,
                "input_id": "caffeine",
                "run_id": f"{run_id}-semantic-a",
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": 0.8,
                "response_received_at": "2026-08-17T20:00:09.100000Z",
                "request_sha256": evidence.EXPECTED_SEMANTIC_CASES[0][1],
                "response_bytes": 96,
                "response_sha256": "1" * 64,
                "invariant": {
                    "generated_count": 1,
                    "smiles": "CCO",
                    "atom_count": 3,
                    "score": 0.4068,
                    "rdkit_qed": 0.40680796565539457,
                },
            },
            {
                "index": 2,
                "input_id": "aspirin",
                "run_id": f"{run_id}-semantic-b",
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": 0.9,
                "response_received_at": "2026-08-17T20:00:09.700000Z",
                "request_sha256": evidence.EXPECTED_SEMANTIC_CASES[1][1],
                "response_bytes": 101,
                "response_sha256": "2" * 64,
                "invariant": {
                    "generated_count": 1,
                    "smiles": "CCN",
                    "atom_count": 3,
                    "score": 0.4062,
                    "rdkit_qed": 0.40623709538988323,
                },
            },
        ],
    }
    cache_holder = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "molmim-native-f7-cache-holder-t12",
            "namespace": render.NAMESPACE,
            "uid": "44444444-4444-4444-8444-444444444444",
        },
        "spec": {
            "nodeName": run["target_node"],
            "volumes": [{"name": "cache", "persistentVolumeClaim": {
                "claimName": "molmim-native-f7-cache", "readOnly": True
            }}],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-17T19:59:58Z"}],
            "containerStatuses": [{"name": "holder", "ready": True}],
        },
    }
    cache_receipt = {
        "schema": "archvteams.nebius.ai/molmim-cache-holder-receipt/v1",
        "status": "PASS",
        "mode": "cache-full-read",
        "unique_bytes": 284_497_920,
        "prewarm_bytes": 284_497_920,
        "tree_sha256": "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c",
        "full_read_elapsed_seconds": 0.25,
    }
    artifact_holder = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "molmim-native-f7-holder-t12",
            "namespace": render.NAMESPACE,
            "uid": "55555555-5555-4555-8555-555555555555",
        },
        "spec": {
            "nodeName": run["target_node"],
            "volumes": [
                {"name": "artifacts", "persistentVolumeClaim": {
                    "claimName": "molmim-native-f7-artifacts", "readOnly": True
                }},
                {"name": "nim-cache", "persistentVolumeClaim": {
                    "claimName": "molmim-native-f7-cache", "readOnly": True
                }},
            ],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-17T19:59:58Z"}],
            "containerStatuses": [{"name": "holder", "ready": True}],
        },
    }
    artifact_receipt = {
        "schema": "archvteams.nebius.ai/molmim-native-artifact-receipt/v1",
        "status": "PASS",
        "checkpoint_id": run["checkpoint_id"],
        "artifact_version": run["artifact_version"],
        "source_node": run["target_node"],
        "image_io_mode": run["image_io_mode"],
        "manifest_sha256": run["artifact_manifest_sha256"],
        "regular_file_bytes": 5_214_934_444,
        "unique_bytes": 5_214_934_444,
        "prewarm_bytes": 5_214_934_444,
        "tree_sha256": "6" * 64,
        "full_read_elapsed_seconds": 3.25,
    }
    return {
        "contract": approved_contract(),
        "run": run,
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
        "cache_holder": cache_holder,
        "cache_receipt": cache_receipt,
        "artifact_holder": artifact_holder,
        "artifact_receipt": artifact_receipt,
        "prewarm_captured_at": "2026-08-17T19:59:59Z",
    }


class BindingTests(unittest.TestCase):
    def test_python_matches_go_worker_canonical_pod_spec_fixture(self) -> None:
        fixture_path = TEST_DIR / "fixtures" / "molmim-api-pod-spec.json"
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

    def test_cache_and_security_mutations_are_rejected_before_binding(self) -> None:
        pod = live_target()
        cache = next(
            volume for volume in pod["spec"]["volumes"] if volume["name"] == "nim-cache"
        )
        cache["persistentVolumeClaim"]["readOnly"] = False
        with self.assertRaisesRegex(bind_target.BindingError, "live Pod spec"):
            bind_target.build_binding(
                pod,
                render.validate_run(run_config()),
                approved_contract(),
                "2026-08-17T20:00:03Z",
            )

        pod = live_target()
        pod["spec"]["containers"][0]["securityContext"][
            "allowPrivilegeEscalation"
        ] = True
        with self.assertRaisesRegex(bind_target.BindingError, "live Pod spec"):
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
    def test_performance_worker_evidence_retains_nonrelease_classification(self) -> None:
        inputs = evidence_inputs()
        inputs["contract"]["release_ready"] = False
        inputs["contract"]["release_blocker"] = "performance validation only"
        inputs["contract"]["worker_classification"] = "performance-validation-only"
        receipt = evidence.build_evidence(**inputs)
        self.assertFalse(receipt["evidence"]["release_ready"])
        self.assertEqual(
            receipt["evidence"]["worker_classification"],
            "performance-validation-only",
        )

    def test_worker_cannot_evidence_an_undeclared_image_io_mode(self) -> None:
        inputs = evidence_inputs()
        inputs["contract"]["supported_image_io_modes"] = ["direct"]
        inputs["run"]["image_io_mode"] = "buffered"
        inputs["run"]["checkpoint_id"] = "molmim-native-f7-v2-buffered"
        with self.assertRaisesRegex(evidence.EvidenceError, "not supported"):
            evidence.build_evidence(**inputs)

    def test_probe_may_start_before_worker_and_wait_for_readiness(self) -> None:
        inputs = evidence_inputs()
        terminated = inputs["probe_pod"]["status"]["containerStatuses"][0]["state"][
            "terminated"
        ]
        terminated["startedAt"] = "2026-08-17T20:00:03.500000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["status"], "PASS")

    def test_direct_http_readiness_may_precede_restore_receipt(self) -> None:
        inputs = evidence_inputs()
        inputs["probe_pod"]["status"]["containerStatuses"][0]["state"][
            "terminated"
        ]["startedAt"] = "2026-08-17T20:00:03.500000Z"
        inputs["semantic_summary"]["started_at"] = "2026-08-17T20:00:04Z"
        inputs["semantic_summary"]["ready_at"] = "2026-08-17T20:00:05.500000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 5.5)
        self.assertEqual(receipt["timings_seconds"]["demand_to_restore_receipt"], 5.9)

    def test_target_submit_timestamp_is_authoritative_t0(self) -> None:
        inputs = evidence_inputs()
        receipt = evidence.build_evidence(
            **inputs,
            target_submit_at="2026-08-17T20:00:00.500000Z",
        )
        self.assertEqual(receipt["demand_at"], "2026-08-17T20:00:00.500000Z")
        self.assertEqual(receipt["measurement"]["planned_demand_at"], inputs["run"]["demand_at"])
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 8.0)

    def test_two_semantic_responses_produce_demand_timing(self) -> None:
        receipt = evidence.build_evidence(**evidence_inputs())
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["request_count"], 2)
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_two_semantic_responses"], 9.7
        )
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_validation_complete"], 9.9
        )
        self.assertEqual(receipt["storage_prewarm"]["artifact"]["mode"], "direct")
        self.assertEqual(receipt["timings_seconds"]["demand_to_http_ready"], 8.5)
        self.assertEqual(
            receipt["timings_seconds"]["demand_to_kubernetes_ready"], 7.0
        )
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

    def test_kubernetes_omitted_false_host_boole_are_accepted(self) -> None:
        inputs = evidence_inputs()
        for object_name in ("worker_job", "probe_job"):
            spec = inputs[object_name]["spec"]["template"]["spec"]
            for field in ("hostIPC", "hostNetwork", "hostPID"):
                if spec.get(field) is False:
                    del spec[field]
        for object_name in ("worker_pod", "probe_pod"):
            spec = inputs[object_name]["spec"]
            for field in ("hostIPC", "hostNetwork", "hostPID"):
                if spec.get(field) is False:
                    del spec[field]
        self.assertEqual(evidence.build_evidence(**inputs)["status"], "PASS")

    def test_other_omitted_false_fields_remain_rejected(self) -> None:
        inputs = evidence_inputs()
        del inputs["probe_job"]["spec"]["template"]["spec"]["enableServiceLinks"]
        with self.assertRaisesRegex(evidence.EvidenceError, "enableServiceLinks is absent"):
            evidence.build_evidence(**inputs)

    def test_api_injected_service_account_projection_is_normalized(self) -> None:
        inputs = evidence_inputs()
        pod_spec = inputs["worker_pod"]["spec"]
        name = "kube-api-access-abc12"
        pod_spec["volumes"].append(
            {
                "name": name,
                "projected": {
                    "defaultMode": 420,
                    "sources": [
                        {"serviceAccountToken": {"expirationSeconds": 3607, "path": "token"}},
                        {"configMap": {"items": [{"key": "ca.crt", "path": "ca.crt"}], "name": "kube-root-ca.crt"}},
                        {"downwardAPI": {"items": [{"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"}, "path": "namespace"}]}},
                    ],
                },
            }
        )
        pod_spec["containers"][0]["volumeMounts"].append(
            {
                "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                "name": name,
                "readOnly": True,
            }
        )
        self.assertEqual(evidence.build_evidence(**inputs)["status"], "PASS")

        inputs = evidence_inputs()
        pod_spec = inputs["worker_pod"]["spec"]
        pod_spec["volumes"].append({"name": name, "emptyDir": {}})
        with self.assertRaisesRegex(evidence.EvidenceError, "invalid service-account projection"):
            evidence.build_evidence(**inputs)

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

    def test_ready_timestamp_subsecond_quantization_is_accepted(self) -> None:
        inputs = evidence_inputs()
        for condition in inputs["target"]["status"]["conditions"]:
            if condition["type"] == "Ready":
                condition["lastTransitionTime"] = "2026-08-17T20:00:05Z"
        inputs["worker_receipt"]["completed_at"] = "2026-08-17T20:00:05.900000Z"
        receipt = evidence.build_evidence(**inputs)
        self.assertEqual(receipt["status"], "PASS")

    def test_ready_timestamp_one_second_before_receipt_is_rejected(self) -> None:
        inputs = evidence_inputs()
        for condition in inputs["target"]["status"]["conditions"]:
            if condition["type"] == "Ready":
                condition["lastTransitionTime"] = "2026-08-17T20:00:04Z"
        inputs["worker_receipt"]["completed_at"] = "2026-08-17T20:00:05.900000Z"
        with self.assertRaisesRegex(evidence.EvidenceError, "monotonically ordered"):
            evidence.build_evidence(**inputs)

    def test_more_than_two_semantic_cases_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"].append(
            copy.deepcopy(inputs["semantic_summary"]["cases"][1])
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "exactly two cases"):
            evidence.build_evidence(**inputs)

    def test_unpinned_semantic_request_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"][0]["request_sha256"] = "f" * 64
        with self.assertRaisesRegex(evidence.EvidenceError, "pinned CMA-ES/QED"):
            evidence.build_evidence(**inputs)

    def test_non_distinct_semantic_molecule_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"][1]["invariant"]["smiles"] = "CCO"
        with self.assertRaisesRegex(evidence.EvidenceError, "distinct responses"):
            evidence.build_evidence(**inputs)

    def test_qed_oracle_drift_is_rejected(self) -> None:
        inputs = evidence_inputs()
        inputs["semantic_summary"]["cases"][0]["invariant"]["score"] = 0.1
        with self.assertRaisesRegex(evidence.EvidenceError, "strict MolMIM QED"):
            evidence.build_evidence(**inputs)

    def test_worker_argument_drift_is_rejected(self) -> None:
        inputs = evidence_inputs()
        args = inputs["worker_job"]["spec"]["template"]["spec"]["containers"][0][
            "args"
        ]
        args[args.index("--target-uid") + 1] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(evidence.EvidenceError, "args does not match"):
            evidence.build_evidence(**inputs)

    def test_worker_and_probe_pods_must_belong_to_the_exact_jobs(self) -> None:
        inputs = evidence_inputs()
        inputs["worker_pod"]["metadata"]["ownerReferences"][0]["uid"] = (
            PROBE_JOB_UID
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "not controlled"):
            evidence.build_evidence(**inputs)

        inputs = evidence_inputs()
        inputs["probe_pod"]["metadata"]["ownerReferences"][0]["uid"] = (
            WORKER_JOB_UID
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "not controlled"):
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
            "cache_holder": "cache-holder.json",
            "cache_receipt": "cache-receipt.json",
            "artifact_holder": "artifact-holder.json",
            "artifact_receipt": "artifact-receipt.json",
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
            "cache_holder": "--cache-holder-pod",
            "cache_receipt": "--cache-holder-receipt",
            "artifact_holder": "--artifact-holder-pod",
            "artifact_receipt": "--artifact-holder-receipt",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv: list[str] = []
            for key, filename in filenames.items():
                path = root / filename
                path.write_text(json.dumps(values[key]), encoding="utf-8")
                argv.extend([flags[key], str(path)])
            submit_path = root / "target-submit-at.txt"
            submit_path.write_text(values["run"]["demand_at"] + "\n", encoding="utf-8")
            argv.extend(["--target-submit-at", str(submit_path)])
            prewarm_path = root / "prewarm-captured-at.txt"
            prewarm_path.write_text(
                values["prewarm_captured_at"] + "\n", encoding="utf-8"
            )
            argv.extend(["--prewarm-captured-at", str(prewarm_path)])
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = evidence.main(argv)
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
