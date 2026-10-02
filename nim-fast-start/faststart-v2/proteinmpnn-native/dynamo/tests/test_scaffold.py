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

import lint_manifest  # noqa: E402
import render  # noqa: E402


def approved_contract() -> dict:
    return {
        "schema": render.CONTRACT_SCHEMA,
        "approved": True,
        "approval": {
            "reviewed_by": "platform-reviewer",
            "reviewed_at": "2026-08-17T20:00:00Z",
            "evidence_sha256": "1" * 64,
        },
        "source": {
            "repository": render.SOURCE_REPOSITORY,
            "commit": render.SOURCE_COMMIT,
            "patch_sha256": "2" * 64,
        },
        "worker_image": "registry.example.invalid/public/dynamo-restore@sha256:" + "3" * 64,
        "worker_executable": "/usr/local/bin/dynamo-restore-one",
        "argument_template": list(render.REQUIRED_ARGUMENT_TEMPLATE),
        "probe_image": "registry.example.invalid/public/python@sha256:" + "6" * 64,
        "probe_executable": "/usr/local/bin/python3",
        "validator_sha256": render.VALIDATOR_SHA256,
        "tool_bundle": {
            "layout": render.TOOL_LAYOUT,
            "content_sha256": "4" * 64,
        },
    }


def run_config() -> dict:
    return {
        "schema": render.RUN_SCHEMA,
        "demand_at": "2026-08-17T20:00:00Z",
        "run_id": "ut-a1b2c3",
        "target_node": "gpu-node-a.example.invalid",
        "checkpoint_id": "proteinmpnn-native-v1",
        "artifact_version": "1",
        "artifact_manifest_sha256": "5" * 64,
        "artifact_pvc": "proteinmpnn-native-artifacts",
        "cache_pvc": "proteinmpnn-nim-cache",
    }


def target_binding() -> dict:
    return {
        "schema": render.BINDING_SCHEMA,
        "collected_at": "2026-08-17T20:01:00Z",
        "run_id": "ut-a1b2c3",
        "namespace": render.NAMESPACE,
        "pod_name": "pmp-target-ut-a1b2c3",
        "pod_uid": "11111111-1111-4111-8111-111111111111",
        "container_name": "proteinmpnn",
        "container_id": "containerd://" + "a" * 64,
        "cgroup": (
            "/kubepods.slice/kubepods-burstable.slice/"
            "kubepods-burstable-pod11111111_1111_4111_8111_111111111111.slice/"
            "cri-containerd-" + "a" * 64 + ".scope"
        ),
        "pod_ip": "10.50.42.7",
        "node": "gpu-node-a.example.invalid",
        "image_id": render.NIM_IMAGE,
        "pod_spec_sha256": "6" * 64,
    }


def find_document(documents: list[dict], kind: str, component: str | None = None) -> dict:
    for document in documents:
        if document.get("kind") != kind:
            continue
        if component is None:
            return document
        labels = document.get("metadata", {}).get("labels", {})
        if labels.get(lint_manifest.COMPONENT_LABEL) == component:
            return document
    raise AssertionError(f"missing {kind}/{component}")


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = render.validate_contract(approved_contract())
        self.run = render.validate_run(run_config())
        self.binding = render.validate_binding(target_binding(), self.run)

    def test_safe_target_worker_and_probe_pass_static_lint(self) -> None:
        target = render.render_target(self.run, self.contract)
        worker = render.render_restore(self.run, self.contract, self.binding)
        probe = render.render_probe(self.run, self.contract, self.binding)
        self.assertEqual(lint_manifest.lint_documents(target), [])
        self.assertEqual(lint_manifest.lint_documents(worker), [])
        self.assertEqual(lint_manifest.lint_documents(probe), [])

    def test_response_preloader_uses_parameterized_public_secret_names(self) -> None:
        template = (MODULE_DIR / "image-preload.yaml.tmpl").read_text(
            encoding="utf-8"
        )
        runner = (MODULE_DIR / "run_response_n3.sh").read_text(encoding="utf-8")
        self.assertIn('name: "@@NGC_PULL_SECRET@@"', template)
        self.assertIn('name: "@@WORKER_PULL_SECRET@@"', template)
        self.assertIn("PMP_NGC_PULL_SECRET:-ngc-pull-secret-example", runner)
        self.assertIn("PMP_WORKER_PULL_SECRET:-worker-pull-secret-example", runner)
        self.assertNotIn("archvteams-2407-registry-pull", template)

    def test_shipped_contract_fails_closed(self) -> None:
        example = json.loads(
            (MODULE_DIR / "restore-interface.example.json").read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(render.RenderError, "not explicitly approved"):
            render.validate_contract(example)

    def test_cli_refuses_shipped_placeholders_without_yaml(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = render.main(
                [
                    "target",
                    "--contract",
                    str(MODULE_DIR / "restore-interface.example.json"),
                    "--run-config",
                    str(MODULE_DIR / "run.example.json"),
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("refused", stderr.getvalue())

    def test_binding_must_match_run_node(self) -> None:
        bad = target_binding()
        bad["node"] = "different-node.example.invalid"
        with self.assertRaisesRegex(render.RenderError, "node does not match"):
            render.validate_binding(bad, self.run)

    def test_binding_requires_exact_image_digest(self) -> None:
        bad = target_binding()
        bad["image_id"] = "nvcr.io/nim/openfold/proteinmpnn@sha256:" + "0" * 64
        with self.assertRaisesRegex(render.RenderError, "exact pinned ProteinMPNN"):
            render.validate_binding(bad, self.run)

    def test_run_rejects_unapproved_node_hostname(self) -> None:
        bad = run_config()
        bad["target_node"] = "unapproved-node.example.invalid"
        with self.assertRaisesRegex(render.RenderError, "exact allowed H100"):
            render.validate_run(bad)

    def test_contract_rejects_retained_non_worker_executables(self) -> None:
        for executable in sorted(render.KNOWN_NON_WORKER_EXECUTABLES):
            with self.subTest(executable=executable):
                bad = approved_contract()
                bad["worker_executable"] = executable
                with self.assertRaisesRegex(render.RenderError, "not the reviewed one-shot"):
                    render.validate_contract(bad)


class UnsafeMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        contract = render.validate_contract(approved_contract())
        run = render.validate_run(run_config())
        binding = render.validate_binding(target_binding(), run)
        self.target = render.render_target(run, contract)
        self.worker = render.render_restore(run, contract, binding)
        self.probe = render.render_probe(run, contract, binding)

    def assert_rejected(self, documents: list[dict], fragment: str) -> None:
        errors = lint_manifest.lint_documents(documents)
        self.assertTrue(
            any(fragment in error for error in errors),
            msg=f"expected {fragment!r} in {errors!r}",
        )

    def test_rejects_mutable_image_tag(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["containers"][0]["image"] = "nvcr.io/nim/openfold/proteinmpnn:1.0"
        self.assert_rejected(documents, "not pinned by @sha256")

    def test_rejects_privileged_target(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["containers"][0]["securityContext"]["privileged"] = True
        self.assert_rejected(documents, "explicitly be nonprivileged")

    def test_rejects_target_node_name(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["nodeName"] = "gpu-node-a.example.invalid"
        self.assert_rejected(documents, "must not set spec.nodeName")

    def test_rejects_privileged_target_init_container(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["initContainers"] = [{
            "name": "unsafe",
            "image": "example.invalid/unsafe@sha256:" + "9" * 64,
            "securityContext": {"privileged": True},
        }]
        self.assert_rejected(documents, "must not contain init containers")

    def test_rejects_image_pull_secret(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["imagePullSecrets"] = [{"name": "registry-credential"}]
        self.assert_rejected(documents, "must not reference image-pull secrets")

    def test_rejects_credential_marker_hidden_in_generic_env(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["containers"][0]["env"].append(
            {"name": "CONFIG", "value": "nvapi-not-a-real-key"}
        )
        self.assert_rejected(documents, "forbidden credential marker")

    def test_rejects_arbitrary_extra_target_pvc_mount(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        pod["spec"]["volumes"].append(
            {"name": "extra", "persistentVolumeClaim": {"claimName": "extra"}}
        )
        pod["spec"]["containers"][0]["volumeMounts"].append(
            {"name": "extra", "mountPath": "/extra"}
        )
        self.assert_rejected(documents, "target volume set is not exact")

    def test_rejects_arbitrary_extra_worker_pvc_mount(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        spec = job["spec"]["template"]["spec"]
        spec["volumes"].append(
            {"name": "extra", "persistentVolumeClaim": {"claimName": "extra"}}
        )
        spec["containers"][0]["volumeMounts"].append(
            {"name": "extra", "mountPath": "/extra"}
        )
        self.assert_rejected(documents, "restore-worker volume set is not exact")

    def test_rejects_non_allowlisted_hostname_affinity(self) -> None:
        documents = copy.deepcopy(self.target)
        pod = find_document(documents, "Pod", "restore-target")
        expressions = pod["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        expressions[0]["values"] = ["unapproved-node.example.invalid"]
        self.assert_rejected(documents, "exact allowed H100 hostname")

    def test_rejects_empty_namespace_restore(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["containers"][0]["args"].extend(
            ["--empty-ns", "net"]
        )
        self.assert_rejected(documents, "forbidden --empty-ns")

    def test_rejects_snapshot_agent_as_restore_worker(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["containers"][0]["command"] = [
            "/usr/local/bin/snapshot-agent"
        ]
        self.assert_rejected(documents, "not a one-shot restore worker")

    def test_rejects_broad_toleration(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["tolerations"] = [{"operator": "Exists"}]
        self.assert_rejected(documents, "broad toleration")

    def test_rejects_restore_worker_request_above_proven_envelope(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["containers"][0]["resources"][
            "requests"
        ]["cpu"] = "1"
        self.assert_rejected(documents, "proven scheduling envelope")

    def test_rejects_restore_worker_limit_drift(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["containers"][0]["resources"][
            "limits"
        ]["cpu"] = "3"
        self.assert_rejected(documents, "proven execution envelope")

    def test_rejects_host_root_mount(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        job["spec"]["template"]["spec"]["volumes"][0]["hostPath"] = {
            "path": "/",
            "type": "Directory",
        }
        self.assert_rejected(documents, "unapproved hostPath")

    def test_rejects_missing_uid_binding(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        args = job["spec"]["template"]["spec"]["containers"][0]["args"]
        index = args.index("--target-uid")
        del args[index : index + 2]
        self.assert_rejected(documents, "restore binding flags mismatch")

    def test_rejects_missing_pod_spec_hash_binding(self) -> None:
        documents = copy.deepcopy(self.worker)
        job = find_document(documents, "Job", "restore-worker")
        args = job["spec"]["template"]["spec"]["containers"][0]["args"]
        index = args.index("--target-pod-spec-sha256")
        del args[index : index + 2]
        self.assert_rejected(documents, "restore binding flags mismatch")

    def test_rejects_probe_third_request(self) -> None:
        documents = copy.deepcopy(self.probe)
        job = find_document(documents, "Job", "semantic-probe")
        job["spec"]["template"]["spec"]["containers"][0]["args"].extend(
            ["--run-id", "ut-a1b2c3-semantic-c"]
        )
        self.assert_rejected(documents, "exactly two fixed strict ProteinMPNN probes")

    def test_rejects_probe_gpu_request(self) -> None:
        documents = copy.deepcopy(self.probe)
        job = find_document(documents, "Job", "semantic-probe")
        job["spec"]["template"]["spec"]["containers"][0]["resources"][
            "requests"
        ]["nvidia.com/gpu"] = "1"
        self.assert_rejected(documents, "must not request a GPU")

    def test_rejects_probe_retry(self) -> None:
        documents = copy.deepcopy(self.probe)
        job = find_document(documents, "Job", "semantic-probe")
        job["spec"]["backoffLimit"] = 1
        self.assert_rejected(documents, "single-attempt")

    def test_rejects_public_service_type(self) -> None:
        documents = copy.deepcopy(self.target)
        service = find_document(documents, "Service", "qualified-service")
        service["spec"]["type"] = "LoadBalancer"
        self.assert_rejected(documents, "must be ClusterIP")

    def test_rejects_qualified_service_without_semantic_selector(self) -> None:
        documents = copy.deepcopy(self.target)
        service = find_document(documents, "Service", "qualified-service")
        del service["spec"]["selector"][lint_manifest.QUALIFIED_LABEL]
        self.assert_rejected(documents, "semantically qualified")

    def test_rejects_added_secret_object(self) -> None:
        documents = copy.deepcopy(self.target)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "unsafe",
                "namespace": render.NAMESPACE,
                "labels": {lint_manifest.RUN_LABEL: run_config()["run_id"]},
            },
            "stringData": {"NGC_API_KEY": "not-a-real-key"},
        }
        documents.append(secret)
        self.assert_rejected(documents, "must never contain a Secret")

    def test_rejects_neutered_network_policy(self) -> None:
        documents = copy.deepcopy(self.target)
        policy = find_document(documents, "NetworkPolicy")
        policy["spec"]["podSelector"] = {}
        policy["spec"]["ingress"] = [{}]
        self.assert_rejected(documents, "NetworkPolic")

    def test_rejects_wrong_rolebinding_subject(self) -> None:
        documents = copy.deepcopy(self.worker)
        binding = find_document(documents, "RoleBinding")
        binding["subjects"] = [{
            "kind": "ServiceAccount",
            "name": "default",
            "namespace": render.NAMESPACE,
        }]
        self.assert_rejected(documents, "subjects are not the exact")


class CliRoundTripTests(unittest.TestCase):
    def test_cli_renders_both_stages_to_valid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            contract_path = root / "contract.json"
            run_path = root / "run.json"
            binding_path = root / "binding.json"
            contract_path.write_text(json.dumps(approved_contract()), encoding="utf-8")
            run_path.write_text(json.dumps(run_config()), encoding="utf-8")
            binding_path.write_text(json.dumps(target_binding()), encoding="utf-8")

            for mode, extra in (
                ("target", []),
                ("restore", ["--binding", str(binding_path)]),
                ("probe", ["--binding", str(binding_path)]),
            ):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = render.main(
                        [
                            mode,
                            "--contract",
                            str(contract_path),
                            "--run-config",
                            str(run_path),
                            *extra,
                        ]
                    )
                self.assertEqual(status, 0, stderr.getvalue())
                documents = list(render.yaml.safe_load_all(stdout.getvalue()))
                self.assertEqual(lint_manifest.lint_documents(documents), [])


if __name__ == "__main__":
    unittest.main()
