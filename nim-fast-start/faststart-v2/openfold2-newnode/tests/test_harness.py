from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HARNESS = Path(__file__).resolve().parents[1]
PIPELINE = HARNESS.parent / "dynamo"
if not PIPELINE.is_dir():
    PIPELINE = HARNESS / "frozen" / "faststart-v2" / "dynamo"
sys.path.insert(0, str(HARNESS))

import manifest_overlay
import node_admission
import lifecycle_evidence
import seccomp_installer
import starting_state


def compatible_node(name: str, uid: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": name,
            "uid": uid,
            "labels": dict(node_admission.EXPECTED_LABELS),
        },
        "spec": {
            "providerID": f"nebius://{name}",
            "taints": None,
        },
        "status": {
            "allocatable": {"nvidia.com/gpu": "1"},
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "lastTransitionTime": "2026-08-18T03:00:00Z",
                }
            ],
            "nodeInfo": {
                "architecture": "amd64",
                "containerRuntimeVersion": "containerd://1.7.34",
                "kernelVersion": "6.11.0-1016-nvidia",
                "osImage": "Ubuntu 24.04.4 LTS",
            },
        },
    }


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = compatible_node(
            "computeinstance-old1", "11111111-1111-4111-8111-111111111111"
        )
        self.new = compatible_node(
            "computeinstance-new1", "22222222-2222-4222-8222-222222222222"
        )

    def test_exact_new_node_is_admitted_and_revalidated(self) -> None:
        raw = json.dumps(self.new, sort_keys=True).encode()
        receipt = node_admission.build(
            self.new, raw, self.old, "2026-08-18T03:00:01.123456789Z"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node_path = root / "node.json"
            admission_path = root / "admission.json"
            node_path.write_bytes(raw)
            admission_path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(
                node_admission.validate_admission(admission_path, node_path), receipt
            )

    def test_reused_identity_is_rejected(self) -> None:
        raw = json.dumps(self.old, sort_keys=True).encode()
        with self.assertRaisesRegex(node_admission.AdmissionError, "new name and UID"):
            node_admission.build(self.old, raw, self.old, "2026-08-18T03:00:01Z")

    def test_tainted_or_incompatible_node_is_rejected(self) -> None:
        self.new["spec"]["taints"] = [{"key": "blocked", "effect": "NoSchedule"}]
        raw = json.dumps(self.new, sort_keys=True).encode()
        with self.assertRaisesRegex(node_admission.AdmissionError, "unexpected taints"):
            node_admission.build(self.new, raw, self.old, "2026-08-18T03:00:01Z")

    def test_reviewed_startup_taints_wait_until_both_clear(self) -> None:
        self.new["spec"]["taints"] = [
            {
                "key": "node.kubernetes.io/not-ready",
                "effect": "NoExecute",
                "timeAdded": "2026-08-18T03:00:02Z",
            },
            {"key": "node.cilium.io/agent-not-ready", "effect": "NoExecute"},
        ]
        state = node_admission.classify_startup_taints(self.new)
        self.assertEqual(state["status"], "wait")
        self.new["spec"]["taints"] = None
        self.assertEqual(
            node_admission.classify_startup_taints(self.new),
            {"status": "clear", "taints": []},
        )

    def test_reviewed_startup_taint_subset_remains_waitable(self) -> None:
        self.new["spec"]["taints"] = [
            {"key": "node.cilium.io/agent-not-ready", "effect": "NoExecute"}
        ]
        self.assertEqual(
            node_admission.classify_startup_taints(self.new)["status"], "wait"
        )

    def test_unknown_or_permanent_startup_taint_is_terminal(self) -> None:
        for taint in (
            {"key": "node.kubernetes.io/not-ready", "effect": "NoSchedule"},
            {"key": "example.invalid/permanent", "effect": "NoExecute"},
        ):
            with self.subTest(taint=taint):
                self.new["spec"]["taints"] = [taint]
                with self.assertRaisesRegex(
                    node_admission.AdmissionError, "unknown or permanent taint"
                ):
                    node_admission.classify_startup_taints(self.new)

    def test_absent_gpu_allocatable_waits_but_present_non_one_is_terminal(self) -> None:
        del self.new["status"]["allocatable"]["nvidia.com/gpu"]
        state = node_admission.classify_startup_state(self.new)
        self.assertEqual(state["status"], "wait")
        self.assertEqual(state["wait_reasons"], ["gpu-allocatable-absent"])
        self.assertIsNone(state["gpu_allocatable"])

        self.new["status"]["allocatable"]["nvidia.com/gpu"] = "0"
        with self.assertRaisesRegex(
            node_admission.AdmissionError, "non-1 allocatable GPU"
        ):
            node_admission.classify_startup_state(self.new)

    def test_startup_state_clears_only_with_no_taints_and_exact_gpu(self) -> None:
        self.assertEqual(
            node_admission.classify_startup_state(self.new),
            {
                "status": "clear",
                "wait_reasons": [],
                "taints": [],
                "gpu_allocatable": "1",
            },
        )
        self.new["spec"]["taints"] = [
            {"key": "node.cilium.io/agent-not-ready", "effect": "NoExecute"}
        ]
        del self.new["status"]["allocatable"]["nvidia.com/gpu"]
        state = node_admission.classify_startup_state(self.new)
        self.assertEqual(
            state["wait_reasons"],
            ["reviewed-transient-taints", "gpu-allocatable-absent"],
        )


class StartingStateTests(unittest.TestCase):
    @staticmethod
    def group(ready: int) -> dict:
        return {
            "metadata": {
                "id": starting_state.NODE_GROUP_ID,
                "parent_id": starting_state.CLUSTER_ID,
                "resource_version": "4",
            },
            "spec": {
                "fixed_node_count": "1",
                "template": {
                    "resources": {
                        "platform": "gpu-h100-sxm",
                        "preset": "1gpu-16vcpu-200gb",
                    },
                    "gpu_settings": {"drivers_preset": "cuda13.0"},
                    "preemptible": {},
                },
            },
            "status": {
                "target_node_count": "1",
                "node_count": "1",
                "ready_node_count": str(ready),
            },
        }

    @staticmethod
    def nodes(node: dict) -> dict:
        return {"apiVersion": "v1", "kind": "List", "items": [node]}

    def test_exact_retiring_unknown_member_is_admitted(self) -> None:
        node = compatible_node(
            "computeinstance-retiring01", "44444444-4444-4444-8444-444444444444"
        )
        ready = next(item for item in node["status"]["conditions"] if item["type"] == "Ready")
        ready["status"] = "Unknown"
        node["spec"]["taints"] = [
            {"key": starting_state.UNREACHABLE, "effect": "NoSchedule"},
            {"key": "node.cilium.io/agent-not-ready", "effect": "NoSchedule"},
            {"key": starting_state.UNREACHABLE, "effect": "NoExecute"},
            {"key": starting_state.SHUTDOWN, "effect": "NoSchedule"},
        ]
        group = self.group(0)
        nodes = self.nodes(node)
        result = starting_state.classify(
            group,
            json.dumps(group).encode(),
            nodes,
            json.dumps(nodes).encode(),
            "2026-08-18T04:00:00Z",
        )
        self.assertEqual(result["mode"], "retiring-unknown")
        self.assertEqual(result["node"]["name"], node["metadata"]["name"])

    def test_exact_ready_untainted_member_remains_healthy(self) -> None:
        node = compatible_node(
            "computeinstance-healthy01", "55555555-5555-4555-8555-555555555555"
        )
        group = self.group(1)
        nodes = self.nodes(node)
        result = starting_state.classify(
            group,
            json.dumps(group).encode(),
            nodes,
            json.dumps(nodes).encode(),
            "2026-08-18T04:00:00Z",
        )
        self.assertEqual(result["mode"], "healthy")

    def test_retiring_member_without_shutdown_taint_is_rejected(self) -> None:
        node = compatible_node(
            "computeinstance-retiring01", "44444444-4444-4444-8444-444444444444"
        )
        ready = next(item for item in node["status"]["conditions"] if item["type"] == "Ready")
        ready["status"] = "Unknown"
        node["spec"]["taints"] = [
            {"key": starting_state.UNREACHABLE, "effect": "NoSchedule"},
            {"key": starting_state.UNREACHABLE, "effect": "NoExecute"},
        ]
        group = self.group(0)
        nodes = self.nodes(node)
        with self.assertRaisesRegex(starting_state.StartingStateError, "admitted state"):
            starting_state.classify(
                group,
                json.dumps(group).encode(),
                nodes,
                json.dumps(nodes).encode(),
                "2026-08-18T04:00:00Z",
            )


class NodeServiceGateTests(unittest.TestCase):
    def run_gate(self, pods: dict, node: str, uid: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pods.json"
            path.write_text(json.dumps(pods), encoding="utf-8")
            return subprocess.run(
                [
                    "jq",
                    "-e",
                    "--arg",
                    "node",
                    node,
                    "--arg",
                    "node_uid",
                    uid,
                    "-f",
                    str(HARNESS / "node_service_gate.jq"),
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_daemonset_and_exact_node_owned_device_plugin_are_accepted(self) -> None:
        node = "computeinstance-fixture01"
        uid = "77777777-7777-4777-8777-777777777777"
        pods = {
            "items": [
                {
                    "metadata": {
                        "namespace": "kube-system",
                        "name": "cilium-a",
                        "ownerReferences": [
                            {"kind": "DaemonSet", "name": "cilium", "controller": True}
                        ],
                    }
                },
                {
                    "metadata": {
                        "namespace": "kube-system",
                        "name": f"nvidia-device-plugin-{node}",
                        "deletionTimestamp": None,
                        "ownerReferences": [
                            {
                                "apiVersion": "v1",
                                "kind": "Node",
                                "name": node,
                                "uid": uid,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"nodeName": node},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"name": "plugin", "ready": True}],
                    },
                },
            ]
        }
        self.assertEqual(self.run_gate(pods, node, uid).returncode, 0)

    def test_workload_or_mismatched_node_owner_is_rejected(self) -> None:
        node = "computeinstance-fixture01"
        uid = "77777777-7777-4777-8777-777777777777"
        workload = {
            "items": [
                {
                    "metadata": {
                        "namespace": "nim-fast-start",
                        "name": "user-workload",
                        "ownerReferences": [
                            {"kind": "Job", "name": "job", "controller": True}
                        ],
                    }
                }
            ]
        }
        self.assertNotEqual(self.run_gate(workload, node, uid).returncode, 0)
        plugin = {
            "items": [
                {
                    "metadata": {
                        "namespace": "kube-system",
                        "name": f"nvidia-device-plugin-{node}",
                        "deletionTimestamp": None,
                        "ownerReferences": [
                            {
                                "apiVersion": "v1",
                                "kind": "Node",
                                "name": node,
                                "uid": "88888888-8888-4888-8888-888888888888",
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"nodeName": node},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"name": "plugin", "ready": True}],
                    },
                }
            ]
        }
        self.assertNotEqual(self.run_gate(plugin, node, uid).returncode, 0)


class SeccompInstallerTests(unittest.TestCase):
    def test_exact_configmap_manifest_and_live_pod_are_verified(self) -> None:
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": seccomp_installer.CONFIGMAP,
                "namespace": seccomp_installer.NAMESPACE,
            },
            "data": {seccomp_installer.PROFILE_KEY: seccomp_installer.PROFILE_TEXT},
        }
        receipt = seccomp_installer.validate_configmap(configmap)
        self.assertEqual(receipt["profile_sha256"], seccomp_installer.PROFILE_SHA256)

        run_id = "offline-seccomp-a1"
        node = "computeinstance-fixture01"
        uid = "99999999-9999-4999-8999-999999999999"
        pod = seccomp_installer.render(run_id, node)
        self.assertEqual(
            pod["spec"]["containers"][0]["image"], seccomp_installer.INSTALLER_IMAGE
        )
        self.assertNotIn("imagePullSecrets", pod["spec"])
        pod["metadata"]["uid"] = uid
        pod["metadata"]["deletionTimestamp"] = None
        pod["status"] = {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "installer",
                    "ready": True,
                    "imageID": seccomp_installer.INSTALLER_IMAGE,
                }
            ],
        }
        self.assertEqual(
            seccomp_installer.validate_live(pod, run_id, node, uid)["uid"], uid
        )

    def test_configmap_drift_and_live_uid_drift_are_rejected(self) -> None:
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": seccomp_installer.CONFIGMAP,
                "namespace": seccomp_installer.NAMESPACE,
            },
            "data": {seccomp_installer.PROFILE_KEY: "{}\n"},
        }
        with self.assertRaisesRegex(seccomp_installer.InstallerError, "SHA-256"):
            seccomp_installer.validate_configmap(configmap)

        run_id = "offline-seccomp-a1"
        node = "computeinstance-fixture01"
        uid = "99999999-9999-4999-8999-999999999999"
        pod = seccomp_installer.render(run_id, node)
        pod["metadata"]["uid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        pod["status"] = {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "installer",
                    "ready": True,
                    "imageID": seccomp_installer.INSTALLER_IMAGE,
                }
            ],
        }
        with self.assertRaisesRegex(seccomp_installer.InstallerError, "identity"):
            seccomp_installer.validate_live(pod, run_id, node, uid)


class PullReferenceTests(unittest.TestCase):
    def test_target_uses_run_scoped_service_account_with_only_regional_reference(self) -> None:
        documents = [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "of2-target-test-a1",
                    "namespace": "nim-fast-start",
                    "labels": {manifest_overlay.RUN_LABEL: "test-a1"},
                },
                "spec": {},
            }
        ]
        overlaid, account = manifest_overlay.target_overlay(documents, "test-a1")
        self.assertEqual(
            overlaid[0]["spec"]["serviceAccountName"], "of2-target-pull-test-a1"
        )
        self.assertNotIn("imagePullSecrets", overlaid[0]["spec"])
        self.assertEqual(
            account[0]["imagePullSecrets"], [{"name": "archvteams-2407-registry-pull"}]
        )

    def test_worker_uses_only_existing_nebius_registry_reference(self) -> None:
        documents = [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": "of2-restore-test-a1",
                    "namespace": "nim-fast-start",
                    "labels": {manifest_overlay.RUN_LABEL: "test-a1"},
                },
            }
        ]
        overlaid = manifest_overlay.restore_overlay(documents, "test-a1")
        self.assertEqual(
            overlaid[0]["imagePullSecrets"],
            [{"name": "archvteams-2407-registry-pull"}],
        )


class LifecycleEvidenceTests(unittest.TestCase):
    @staticmethod
    def write(root: Path, name: str, value: object) -> None:
        path = root / name
        if isinstance(value, str):
            path.write_text(value + "\n", encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def group(count: int) -> dict:
        return {
            "metadata": {"id": lifecycle_evidence.NODE_GROUP_ID},
            "spec": {"fixed_node_count": str(count)},
            "status": {
                "target_node_count": str(count),
                "node_count": str(count),
                "ready_node_count": str(count),
            },
        }

    @staticmethod
    def attachments(node: str | None) -> dict:
        if node is None:
            return {"items": []}
        return {
            "items": [
                {
                    "spec": {
                        "source": {"persistentVolumeName": pv},
                        "nodeName": node,
                    },
                    "status": {"attached": True},
                }
                for pv in (lifecycle_evidence.ARTIFACT_PV, lifecycle_evidence.CACHE_PV)
            ]
        }

    @staticmethod
    def holder(uid: str = "holder-restored-uid") -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": lifecycle_evidence.HOLDER_NAME,
                "uid": uid,
                "ownerReferences": None,
            },
            "spec": {
                "nodeName": lifecycle_evidence.HOLDER_NODE,
                "volumes": [
                    {
                        "persistentVolumeClaim": {
                            "claimName": claim,
                            "readOnly": True,
                        }
                    }
                    for claim in (
                        lifecycle_evidence.ARTIFACT_PVC,
                        lifecycle_evidence.CACHE_PVC,
                    )
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "holder", "ready": True}],
            },
        }

    def test_success_requires_zero_new_node_two_calls_and_full_storage_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "offline-a1"
            root.mkdir()
            self.write(root, "node-group-original.json", self.group(1))
            self.write(
                root,
                "starting-state.json",
                {"mode": "healthy", "node": {"name": "old", "uid": "old-uid"}},
            )
            self.write(root, "node-group-zero.json", self.group(0))
            self.write(root, "node-group-new-ready.json", self.group(1))
            self.write(root, "node-group-final.json", self.group(1))
            self.write(
                root,
                "node-admission.json",
                {
                    "node_group_id": lifecycle_evidence.NODE_GROUP_ID,
                    "previous_node": {"name": "old", "uid": "old-uid"},
                    "node": {"name": "new", "uid": "new-uid"},
                },
            )
            self.write(
                root,
                "volumeattachments-before.json",
                self.attachments(lifecycle_evidence.HOLDER_NODE),
            )
            self.write(
                root,
                "volumeattachments-prepared-detached.json",
                self.attachments(None),
            )
            self.write(
                root, "volumeattachments-target-attached.json", self.attachments("new")
            )
            self.write(
                root,
                "volumeattachments-after-run-cleanup.json",
                self.attachments(None),
            )
            self.write(
                root,
                "volumeattachments-holder-restored.json",
                self.attachments(lifecycle_evidence.HOLDER_NODE),
            )
            self.write(
                root,
                "volumeattachments-holder-restored-confirmed.json",
                self.attachments(lifecycle_evidence.HOLDER_NODE),
            )
            self.write(root, "holder-restored.json", self.holder())
            self.write(root, "holder-restored-confirmed.json", self.holder())
            self.write(root, "resources-after-cleanup.json", {"items": []})
            self.write(root, "scale-up-demand-at.txt", "2026-08-18T03:00:00Z")
            self.write(root, "scale-up-request-returned-at.txt", "2026-08-18T03:00:01Z")
            self.write(root, "new-node-admitted-at.txt", "2026-08-18T03:01:00Z")
            self.write(root, "criu-agent-ready-at.txt", "2026-08-18T03:01:30Z")
            self.write(
                root, "target-placeholder-running-at.txt", "2026-08-18T03:02:00Z"
            )
            self.write(root, "benchmark-passed-at.txt", "2026-08-18T03:03:00Z")
            self.write(root, "cleanup-finished-at.txt", "2026-08-18T03:04:00Z")
            self.write(
                root,
                "canary-evidence.json",
                {
                    "status": "PASS",
                    "request_count": 2,
                    "semantic_pass_count": 2,
                    "demand_at": "2026-08-18T03:00:00Z",
                    "timings_seconds": {"demand_to_two_semantic_responses": 179.0},
                },
            )
            receipt = lifecycle_evidence.build(root, 0, False, True)
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["timings_seconds"]["scale_request_to_cleanup_finished"], 240.0)
            self.assertEqual(receipt["storage_transitions"][-1], "holder-restored")

            failed_receipt = lifecycle_evidence.build(root, 1, False, True)
            self.assertEqual(failed_receipt["status"], "RUN_FAILED_CLEANUP_PASS")
            (root / "holder-restored-confirmed.json").unlink()
            with self.assertRaisesRegex(lifecycle_evidence.LifecycleError, "missing regular evidence"):
                lifecycle_evidence.build(root, 1, False, True)
            self.write(root, "holder-restored-confirmed.json", self.holder())
            self.write(root, "new-node-admitted-at.txt", "2026-08-18T03:02:30Z")
            with self.assertRaisesRegex(lifecycle_evidence.LifecycleError, "lifecycle timestamps"):
                lifecycle_evidence.build(root, 0, False, True)

            retiring_group = self.group(1)
            retiring_group["status"]["ready_node_count"] = "0"
            self.write(root, "node-group-original.json", retiring_group)
            self.write(
                root,
                "starting-state.json",
                {
                    "mode": "retiring-unknown",
                    "node": {"name": "old", "uid": "old-uid"},
                },
            )
            self.write(
                root,
                "retiring-predecessor-removed.json",
                {"name": "old", "uid": "old-uid", "absent": True},
            )
            self.write(root, "new-node-admitted-at.txt", "2026-08-18T03:01:00Z")
            retiring_receipt = lifecycle_evidence.build(root, 0, False, True)
            self.assertEqual(retiring_receipt["starting_mode"], "retiring-unknown")
            self.assertEqual(retiring_receipt["status"], "PASS")

    def test_failed_before_holder_release_and_wrong_original_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "node-group-original.json", self.group(1))
            self.write(root, "node-group-final.json", self.group(1))
            self.write(
                root,
                "starting-state.json",
                {"mode": "healthy", "node": {"name": "old", "uid": "old-uid"}},
            )
            self.write(root, "resources-after-cleanup.json", {"items": []})
            receipt = lifecycle_evidence.build(root, 1, False, False)
            self.assertEqual(receipt["status"], "RUN_FAILED_CLEANUP_PASS")

            self.write(root, "node-group-original.json", self.group(2))
            self.write(root, "node-group-final.json", self.group(2))
            with self.assertRaisesRegex(lifecycle_evidence.LifecycleError, "exactly 1"):
                lifecycle_evidence.build(root, 1, True, False)


class FrozenPipelineIntegrationTests(unittest.TestCase):
    def assert_current_status_receipt(self) -> None:
        status = json.loads(
            (HARNESS / "CURRENT_STATUS.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            status["schema"],
            "archvteams.nebius.ai/openfold2-newnode-current-status/v1",
        )
        self.assertEqual(status["audit_mode"], "offline-read-only")
        self.assertEqual(status["current_contract"]["sample_count"], 0)
        self.assertEqual(status["current_contract"]["poolable_run_ids"], [])

        historical = {item["run_id"]: item for item in status["historical_runs"]}
        self.assertEqual(
            set(historical),
            {"of2-newnode-r4-0418", "of2-newnode-r5-regional"},
        )
        expected = {
            "of2-newnode-r4-0418": (604.270994, 607.247235),
            "of2-newnode-r5-regional": (572.607133, 575.458978),
        }
        for run_id, (http_ready, validation_complete) in expected.items():
            with self.subTest(run_id=run_id):
                run = historical[run_id]
                self.assertEqual(run["classification"], "HISTORICAL_NONPOOLABLE")
                self.assertEqual(run["lifecycle_status"], "PASS")
                self.assertTrue(
                    run["raw_evidence_root"].endswith(f"/{run_id}/")
                )
                self.assertEqual(
                    run["independent_scale_to_http_ready_seconds"], http_ready
                )
                self.assertEqual(
                    run["legacy_scale_to_validation_complete_seconds"],
                    validation_complete,
                )
                self.assertIsNone(run["call_1_dispatch_to_body_seconds"])
                self.assertIsNone(run["call_2_dispatch_to_body_seconds"])
                self.assertIsNone(run["exact_scale_to_call_2_body_seconds"])
                self.assertEqual(len(run["source_receipts_sha256"]), 5)
                self.assertTrue(
                    all(
                        len(digest) == 64
                        for digest in run["source_receipts_sha256"].values()
                    )
                )

        r4 = (HARNESS / "R4_RESULT.md").read_text(encoding="utf-8")
        r5 = (HARNESS / "R5_REGIONAL_RESULT.md").read_text(encoding="utf-8")
        self.assertIn(
            "Independent first successful HTTP readiness response | **604.270994**",
            r4,
        )
        self.assertIn(
            "Legacy validation complete after two strict semantic calls | **607.247235**",
            r4,
        )
        self.assertIn(
            "Independent first successful HTTP readiness response | **572.607133**",
            r5,
        )
        self.assertIn(
            "Legacy validation complete after two strict semantic calls | **575.458978**",
            r5,
        )
        self.assertNotIn("| Two strict semantic responses |", r4)
        self.assertNotIn("| Two strict semantic responses |", r5)

    def test_archived_v1_pipeline_fails_closed_on_stale_pins(self) -> None:
        self.assert_current_status_receipt()
        status = json.loads(
            (HARNESS / "CURRENT_STATUS.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            status["current_contract"]["future_execution_path"],
            "newnode-v2-only",
        )
        self.assertTrue(
            status["v1_blockers"]["shared_pipeline_pin_drift_diagnostics_only"]
        )
        drift = status["v1_blockers"][
            "shared_pipeline_pin_drift_diagnostics_only"
        ]
        self.assertEqual(len(drift), 8)
        self.assertEqual(
            {item.split(" ", 1)[0] for item in drift},
            {
                "dynamo/render.py",
                "dynamo/lint_manifest.py",
                "dynamo/evidence.py",
                "dynamo/manifests/target.yaml.tmpl",
                "dynamo/manifests/restore-worker.yaml.tmpl",
                "dynamo/manifests/semantic-probe.yaml.tmpl",
                "dynamo/restore-interface.live.json",
                "validate_openfold2.py",
            },
        )
        for item in drift:
            label, digests = item.split(" ", 1)
            _archived, current = digests.split(" -> ", 1)
            self.assertEqual(
                hashlib.sha256((HARNESS.parent / label).read_bytes()).hexdigest(),
                current,
            )
        matching = status["v1_blockers"][
            "shared_pipeline_matching_pins_diagnostics_only"
        ]
        self.assertEqual(
            {item.split(" ", 1)[0] for item in matching},
            {"dynamo/bind_target.py"},
        )
        for item in matching:
            label, expected = item.split(" ", 1)
            self.assertEqual(
                hashlib.sha256((HARNESS.parent / label).read_bytes()).hexdigest(),
                expected,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            admission_path = root / "admission.json"
            node_path = root / "node.json"
            admission_path.write_text("{}", encoding="utf-8")
            node_path.write_text("{}", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(HARNESS / "runtime_pipeline.py"),
                    "--pipeline-root",
                    str(PIPELINE),
                    "--admission",
                    str(admission_path),
                    "--node-json",
                    str(node_path),
                    "render",
                ],
                check=False,
                cwd=HARNESS,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "frozen validator does not have the approved SHA-256",
            result.stderr,
        )

if __name__ == "__main__":
    unittest.main()
