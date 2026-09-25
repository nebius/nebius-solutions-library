from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
MODULE_DIR = TEST_DIR.parent
sys.path.insert(0, str(MODULE_DIR))

import render  # noqa: E402
import bind_target  # noqa: E402
import validate_boltz2 as validator  # noqa: E402


POD_UID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "containerd://" + "a" * 64
EXPECTED_SPEC_SHA256 = "e2b7152f67ffca1cb6a03b9417c3efc50a6e811ede09284b15fdce7b573eb6a1"


def contract() -> dict[str, Any]:
    return json.loads((MODULE_DIR / "restore-interface.live.json").read_text(encoding="utf-8"))


def run_config() -> dict[str, Any]:
    return {
        "schema": render.RUN_SCHEMA,
        "demand_at": "2026-08-18T03:36:44.660787988Z",
        "run_id": "boltz-offline-1",
        "target_node": "gpu-node-b.example.invalid",
        "checkpoint_id": "boltz2-native-f7-v1",
        "artifact_version": "1",
        "artifact_manifest_sha256": (
            "6539b9f50a71c9f5fb6a3fbacd44f5d5ea41003539b6563682a38600d1492456"
        ),
        "artifact_pvc": "mlspec-archvteams-2407-ckpt-m3",
        "cache_pvc": "boltz2-nim-cache-native-f7-r3",
    }


def mmcif_for(sequence: str, chain: str) -> str:
    one_to_three = {value: key for key, value in validator.THREE_TO_ONE.items()}
    headers = [
        "_atom_site.group_PDB",
        "_atom_site.label_atom_id",
        "_atom_site.label_comp_id",
        "_atom_site.label_seq_id",
        "_atom_site.label_asym_id",
        "_atom_site.Cartn_x",
        "_atom_site.Cartn_y",
        "_atom_site.Cartn_z",
        "_atom_site.B_iso_or_equiv",
        "_atom_site.pdbx_PDB_model_num",
    ]
    lines = ["data_boltz2_test", "loop_", *headers]
    for residue_id, residue in enumerate(sequence, 1):
        for atom_index, atom in enumerate(("N", "CA", "C", "O"), 1):
            coordinate = residue_id + atom_index / 10
            lines.append(
                f"ATOM {atom} {one_to_three[residue]} {residue_id} {chain} "
                f"{coordinate:.3f} {coordinate + 1:.3f} {coordinate + 2:.3f} 50.0 1"
            )
    lines.append("#")
    return "\n".join(lines) + "\n"


def response_for(sequence: str, chain: str) -> dict[str, Any]:
    return {
        "structures": [{"format": "mmcif", "structure": mmcif_for(sequence, chain)}],
        "confidence_scores": [0.72],
        "ptm_scores": [0.31],
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
                "name": "boltz2",
                "containerID": CONTAINER_ID,
                "imageID": render.NIM_IMAGE,
                "state": {"running": {"startedAt": "2026-08-18T03:36:46Z"}},
            }
        ],
    }
    return pod


class BoltzRequestAndSemanticTests(unittest.TestCase):
    def test_response_latency_stops_before_semantic_validation(self) -> None:
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, body: bytes, url: str) -> None:
                self.stream = io.BytesIO(body)
                self.url = url

            def read(self, size: int = -1) -> bytes:
                return self.stream.read(size)

            def geturl(self) -> str:
                return self.url

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        class Opener:
            def open(self, request: Any, timeout: float) -> Response:
                self.timeout = timeout
                payload = json.loads(request.data)
                polymer = payload["polymers"][0]
                body = json.dumps(
                    response_for(polymer["sequence"], polymer["id"]),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return Response(body, request.full_url)

        original_validate = validator.validate_response

        def slow_validate(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.05)
            return original_validate(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            validator, "validate_response", side_effect=slow_validate
        ):
            summary = validator.run_validation(
                base_url="http://127.0.0.1:8000",
                receipt_dir=Path(temporary) / "receipt",
                run_ids=("timing-a", "timing-b"),
                timeout=5,
                opener=Opener(),
            )

        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(
            summary["response_timing_contract"], validator.RESPONSE_TIMING_CONTRACT
        )
        self.assertGreater(
            summary["validation_total_elapsed_seconds"]
            - sum(case["elapsed_seconds"] for case in summary["cases"]),
            0.08,
        )
        for case in summary["cases"]:
            self.assertLessEqual(case["request_started_at"], case["response_received_at"])

    def test_actual_lf_msa_rejects_literal_backslash_n_regression(self) -> None:
        probes = validator.build_probes(("actual-lf-a", "actual-lf-b"))
        for probe in probes:
            alignment = probe.payload["polymers"][0]["msa"]["msa_search"]["a3m"][
                "alignment"
            ]
            self.assertEqual(f">query\n{probe.sequence}", alignment)
            self.assertEqual([">query", probe.sequence], alignment.splitlines())
            self.assertEqual(1, alignment.count("\n"))
            self.assertNotIn("\\n", alignment)

            literal_backslash_n = f">query\\n{probe.sequence}"
            self.assertNotEqual(alignment, literal_backslash_n)
            self.assertNotEqual([">query", probe.sequence], literal_backslash_n.splitlines())

    def test_request_nesting_matches_archived_passing_boltz_shape(self) -> None:
        probe = validator.build_probes(("shape-a", "shape-b"))[0]
        polymer = probe.payload["polymers"][0]
        self.assertEqual("A", polymer["id"])
        self.assertEqual("protein", polymer["molecule_type"])
        self.assertEqual(
            {"alignment": f">query\n{probe.sequence}", "format": "a3m", "rank": 0},
            polymer["msa"]["msa_search"]["a3m"],
        )
        self.assertNotIn("custom", polymer["msa"])

    def test_accepts_canonical_mmcif_response(self) -> None:
        chain, sequence = validator.FIXED_SEQUENCES[0]
        invariant = validator.validate_response(response_for(sequence, chain), sequence, chain)
        self.assertEqual(sequence, invariant["mmcif"]["sequence"])
        self.assertEqual(20, invariant["mmcif"]["backbone_residue_count"])
        self.assertEqual(80, invariant["mmcif"]["atom_record_count"])
        self.assertEqual(240, invariant["mmcif"]["finite_coordinate_count"])

    def test_rejects_malformed_semantic_outputs(self) -> None:
        chain, sequence = validator.FIXED_SEQUENCES[0]

        def only_structure(payload: dict[str, Any]) -> dict[str, Any]:
            return payload["structures"][0]

        mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("handled detail", lambda value: value.update(detail="inference failed")),
            ("no structure", lambda value: value.update(structures=[])),
            ("two structures", lambda value: value["structures"].append(copy.deepcopy(only_structure(value)))),
            ("wrong format", lambda value: only_structure(value).update(format="pdb")),
            ("missing structure", lambda value: only_structure(value).pop("structure")),
            ("confidence above one", lambda value: value.update(confidence_scores=[1.01])),
            ("nonfinite pTM", lambda value: value.update(ptm_scores=[float("nan")])),
            ("boolean confidence", lambda value: value.update(confidence_scores=[True])),
            (
                "wrong chain",
                lambda value: only_structure(value).update(structure=mmcif_for(sequence, "Z")),
            ),
            (
                "missing backbone",
                lambda value: only_structure(value).update(
                    structure=only_structure(value)["structure"].replace(
                        "ATOM O ALA 1 A 1.400 2.400 3.400 50.0 1\n", "", 1
                    )
                ),
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                payload = response_for(sequence, chain)
                mutate(payload)
                with self.assertRaises(validator.SemanticFailure):
                    validator.validate_response(payload, sequence, chain)


class BoltzRenderAndBindingTests(unittest.TestCase):
    def test_trial_runner_emits_aligned_timing_metrics(self) -> None:
        runner = (MODULE_DIR / "run_one_native_trial.sh").read_text(encoding="utf-8")
        self.assertIn('${BASH_SOURCE[0]}', runner)
        self.assertNotIn("forbidden-live-context", runner)
        self.assertIn("build_timing_evidence", runner)
        self.assertIn('directory / "target-submit-at.txt"', runner)
        self.assertIn("target_submit_at=target_submit_at", runner)
        for field in (
            "demand_to_http_ready_seconds",
            "demand_to_kubernetes_ready_seconds",
            "semantic_request_1_seconds",
            "semantic_request_2_seconds",
            "demand_to_two_semantic_seconds",
        ):
            self.assertIn(field, runner)

    def test_target_restore_and_probe_render_smoke(self) -> None:
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        target_documents = render.render_target(run, approved)
        self.assertEqual([], render.lint_documents(target_documents))
        target = next(item for item in target_documents if item["kind"] == "Pod")
        self.assertEqual("b2-target-boltz-offline-1", target["metadata"]["name"])
        self.assertEqual(render.NIM_IMAGE, target["spec"]["containers"][0]["image"])
        self.assertIn("@sha256:", target["spec"]["containers"][0]["image"])
        self.assertEqual("boltz2-nim-cache-native-f7-r3", next(
            item["persistentVolumeClaim"]["claimName"]
            for item in target["spec"]["volumes"]
            if item["name"] == "nim-cache"
        ))

        pod = live_target()
        binding, patch = bind_target.build_binding(
            pod, run, approved, "2026-08-18T03:36:47.660632637Z"
        )
        restore_documents = render.render_restore(run, approved, binding)
        probe_documents = render.render_probe(run, approved, binding)
        self.assertEqual([], render.lint_documents(restore_documents))
        self.assertEqual([], render.lint_documents(probe_documents))

        worker = next(item for item in restore_documents if item["kind"] == "Job")
        args = worker["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual(binding["pod_spec_sha256"], args[args.index("--target-pod-spec-sha256") + 1])
        self.assertEqual(render.NIM_IMAGE, args[args.index("--expected-image-id") + 1])

        probe = next(item for item in probe_documents if item["kind"] == "Job")
        expressions = probe["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertEqual(
            [{"key": "kubernetes.io/hostname", "operator": "In", "values": [run["target_node"]]}],
            expressions,
        )
        config = next(item for item in probe_documents if item["kind"] == "ConfigMap")
        source = config["data"]["validate_boltz2.py"].encode("utf-8")
        self.assertEqual(render.VALIDATOR_SHA256, hashlib.sha256(source).hexdigest())

    def test_exact_digest_and_live_podspec_binding(self) -> None:
        pod = live_target()
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        binding, patch = bind_target.build_binding(
            pod, run, approved, "2026-08-18T03:36:47.660632637Z"
        )
        expected = bind_target.base_bind.pod_spec_sha256(pod["spec"])
        self.assertEqual(EXPECTED_SPEC_SHA256, expected)
        self.assertEqual(expected, binding["pod_spec_sha256"])
        self.assertEqual(render.NIM_IMAGE, binding["image_id"])
        self.assertEqual(expected, patch[1]["value"])
        self.assertEqual(POD_UID, patch[0]["value"])

    def test_rejects_image_or_podspec_drift(self) -> None:
        run = render.validate_run(run_config())
        approved = render.validate_contract(contract())
        wrong_image = live_target()
        wrong_image["spec"]["containers"][0]["image"] = (
            "nvcr.io/nim/mit/boltz2@sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(bind_target.base_bind.BindingError, "pinned Boltz2 image"):
            bind_target.build_binding(
                wrong_image, run, approved, "2026-08-18T03:36:47.660632637Z"
            )

        drifted = live_target()
        binding, _ = bind_target.build_binding(
            drifted, run, approved, "2026-08-18T03:36:47.660632637Z"
        )
        drifted["metadata"]["annotations"][bind_target.base_bind.POD_SPEC_HASH_KEY] = binding[
            "pod_spec_sha256"
        ]
        drifted["spec"]["terminationGracePeriodSeconds"] = 7
        with self.assertRaisesRegex(bind_target.base_bind.BindingError, "existing target PodSpec"):
            bind_target.build_binding(
                drifted, run, approved, "2026-08-18T03:36:47.660632637Z"
            )


if __name__ == "__main__":
    unittest.main()
