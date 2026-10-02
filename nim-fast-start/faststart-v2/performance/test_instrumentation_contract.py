from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import instrumentation_contract as contract


class InstrumentationContractTests(unittest.TestCase):
    def test_both_models_pin_dynamic_dynamo_dependencies(self) -> None:
        common = {
            "dynamo/__init__.py",
            "dynamo/bind_target.py",
            "dynamo/evidence.py",
            "dynamo/lint_manifest.py",
            "dynamo/manifests/restore-worker.yaml.tmpl",
            "dynamo/manifests/semantic-probe.yaml.tmpl",
            "dynamo/manifests/target.yaml.tmpl",
            "dynamo/render.py",
            "performance/aggregate_fresh_cohort.py",
            "performance/clock_sample.sh",
            "performance/instrumentation_contract.py",
            "performance/qualification_receipt.py",
            "performance/run_fresh_cohort.sh",
            "performance/split_manifest.py",
            "performance/uid_cleanup.sh",
        }
        expected = {
            "openfold2": common
            | {
                "dynamo/restore-interface.live.json",
                "dynamo/run_provisioned_trial.sh",
                "validate_openfold2.py",
            },
            "boltz2": common
            | {
                "boltz2-native/bind_target.py",
                "boltz2-native/render.py",
                "boltz2-native/restore-interface.live.json",
                "boltz2-native/run_one_native_trial.sh",
                "boltz2-native/validate_boltz2.py",
                "timing_evidence.py",
            },
        }
        expected_counts = {"openfold2": 18, "boltz2": 21}
        for model in ("openfold2", "boltz2"):
            with self.subTest(model=model):
                receipt = contract.build_contract(model)
                paths = [item["path"] for item in receipt["sources"]]
                self.assertEqual(paths, sorted(set(paths)))
                self.assertEqual(set(paths), expected[model])
                self.assertEqual(receipt["source_count"], expected_counts[model])
                self.assertEqual(len(paths), expected_counts[model])

    def test_missing_pinned_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = sorted(
                (*contract.COMMON_PATHS, *contract.MODEL_PATHS["openfold2"])
            )
            for relative in paths[1:]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"source\n")
            with self.assertRaises(contract.InstrumentationContractError):
                contract.build_contract("openfold2", root)

    def test_unreviewed_extra_source_changes_path_set_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_paths = set(
                (*contract.COMMON_PATHS, *contract.MODEL_PATHS["openfold2"])
            )
            extra = "performance/unreviewed.py"
            for relative in sorted(original_paths | {extra}):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{relative}\n".encode())
            baseline = contract.build_contract("openfold2", root)
            with mock.patch.dict(
                contract.MODEL_PATHS,
                {"openfold2": (*contract.MODEL_PATHS["openfold2"], extra)},
            ):
                changed = contract.build_contract("openfold2", root)
            self.assertEqual(
                {item["path"] for item in baseline["sources"]}, original_paths
            )
            self.assertEqual(
                {item["path"] for item in changed["sources"]},
                original_paths | {extra},
            )
            self.assertNotEqual(
                baseline["instrumentation_contract_sha256"],
                changed["instrumentation_contract_sha256"],
            )

    def test_any_pinned_source_mutation_changes_contract_digest(self) -> None:
        for model in ("openfold2", "boltz2"):
            with self.subTest(model=model):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    paths = sorted((*contract.COMMON_PATHS, *contract.MODEL_PATHS[model]))
                    for index, relative in enumerate(paths):
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(f"source-{index}\n".encode())
                    baseline = contract.build_contract(model, root)
                    changed = root / "dynamo/render.py"
                    changed.write_bytes(changed.read_bytes() + b"drift\n")
                    drifted = contract.build_contract(model, root)
                    self.assertNotEqual(
                        baseline["instrumentation_contract_sha256"],
                        drifted["instrumentation_contract_sha256"],
                    )


if __name__ == "__main__":
    unittest.main()
