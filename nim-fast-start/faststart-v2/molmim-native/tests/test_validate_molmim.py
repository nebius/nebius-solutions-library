#!/usr/bin/env python3
"""Offline tests for the frozen MolMIM two-call CMA-ES/QED contract."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "validate_molmim.py"
REQUEST = ROOT / "fixtures" / "request-cmaes-qed.json"

SPEC = importlib.util.spec_from_file_location("validate_molmim", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)

SMILES_A = "Cn1c(=O)c2c(ncn2CCN(CCO)C(=O)OC(C)(C)C)n(C)c1=O"
SMILES_B = "CC(=O)Oc1ccccc1C(=O)N[C@@H](C)c1ccc(N(C)C)cc1"
QED_VALUES = {SMILES_A: 0.774042882923163, SMILES_B: 0.6771441574010184}


class _Molecule:
    def __init__(self, smiles: str) -> None:
        self.smiles = smiles

    def GetNumAtoms(self) -> int:
        return 24


class _Chem:
    @staticmethod
    def MolFromSmiles(smiles: str) -> _Molecule | None:
        return None if smiles == "INVALID" else _Molecule(smiles)


class _QED:
    @staticmethod
    def qed(molecule: _Molecule) -> float:
        return QED_VALUES.get(molecule.smiles, 0.5)


def response_for(smiles: str) -> dict[str, Any]:
    return {"generated": [{"smiles": smiles, "score": QED_VALUES[smiles]}]}


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = validator._read_fixture(REQUEST)

    def test_fixture_is_exact_retained_two_case_contract(self) -> None:
        self.assertEqual([case["name"] for case in self.cases], ["caffeine", "aspirin"])
        first, second = (case["payload"] for case in self.cases)
        self.assertEqual(first["algorithm"], second["algorithm"])
        self.assertEqual(first["algorithm"], "CMA-ES")
        self.assertEqual(first["property_name"], second["property_name"])
        self.assertEqual(first["property_name"], "QED")
        self.assertNotEqual(first["smi"], second["smi"])
        self.assertEqual(first["num_molecules"], second["num_molecules"])
        self.assertEqual(first["num_molecules"], 1)

    def test_strict_pass_recomputes_qed_and_parses_smiles(self) -> None:
        with mock.patch.object(validator, "_rdkit_modules", return_value=(_Chem, _QED)):
            invariant = validator._validate_response(response_for(SMILES_A))
        self.assertEqual(invariant["smiles"], SMILES_A)
        self.assertEqual(invariant["generated_count"], 1)
        self.assertEqual(invariant["atom_count"], 24)
        self.assertAlmostEqual(invariant["score"], invariant["rdkit_qed"])

    def test_tampered_fixture_is_rejected_before_network_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            value = {"schema": validator.FIXTURE_SCHEMA, "cases": copy.deepcopy(self.cases)}
            value["cases"][0]["payload"]["iterations"] = 2
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(validator.ValidationError, "fixture bytes"):
                validator._read_fixture(path)

    def test_symlinked_fixture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.symlink_to(REQUEST)
            with self.assertRaisesRegex(validator.ValidationError, "non-symlink"):
                validator._read_fixture(path)

    def test_both_observed_readiness_shapes_are_accepted(self) -> None:
        self.assertTrue(validator._health_is_ready(True))
        self.assertTrue(validator._health_is_ready({"status": "ready"}))
        self.assertFalse(validator._health_is_ready({"status": "live"}))

    def test_wrong_qed_is_rejected(self) -> None:
        value = response_for(SMILES_A)
        value["generated"][0]["score"] = 0.1
        with mock.patch.object(validator, "_rdkit_modules", return_value=(_Chem, _QED)):
            with self.assertRaisesRegex(validator.ValidationError, "disagrees with RDKit"):
                validator._validate_response(value)

    def test_invalid_smiles_is_rejected(self) -> None:
        value = {"generated": [{"smiles": "INVALID", "score": 0.5}]}
        with mock.patch.object(validator, "_rdkit_modules", return_value=(_Chem, _QED)):
            with self.assertRaisesRegex(validator.ValidationError, "does not parse"):
                validator._validate_response(value)

    def test_missing_or_multiple_candidates_are_rejected(self) -> None:
        with mock.patch.object(validator, "_rdkit_modules", return_value=(_Chem, _QED)):
            with self.assertRaisesRegex(validator.ValidationError, "exactly one"):
                validator._validate_response({"generated": []})
            with self.assertRaisesRegex(validator.ValidationError, "exactly one"):
                validator._validate_response(
                    {"generated": [response_for(SMILES_A)["generated"][0], response_for(SMILES_B)["generated"][0]]}
                )

    def test_molecules_json_string_shape_is_supported(self) -> None:
        value = {"molecules": json.dumps([{"sample": SMILES_B, "score": QED_VALUES[SMILES_B]}])}
        with mock.patch.object(validator, "_rdkit_modules", return_value=(_Chem, _QED)):
            invariant = validator._validate_response(value)
        self.assertEqual(invariant["smiles"], SMILES_B)


class _StrictHandler(BaseHTTPRequestHandler):
    cases: list[dict[str, Any]] = []
    request_ids: list[str] = []
    requests: list[dict[str, Any]] = []
    errors: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, b"true") if self.path == validator.READY_ENDPOINT else self._send(404, b"{}")

    def do_POST(self) -> None:
        if self.path != validator.ENDPOINT:
            self._send(404, b"{}")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request_id = self.headers.get("X-Request-ID")
            if request_id is None:
                raise AssertionError("X-Request-ID is missing")
            index = len(self.requests)
            if index >= 2 or body != self.cases[index]["payload"]:
                raise AssertionError("request body was not the next frozen strict case")
            self.request_ids.append(request_id)
            self.requests.append(body)
        except Exception as exc:
            self.errors.append(str(exc))
            self._send(400, json.dumps({"error": str(exc)}).encode())
            return
        smiles = (SMILES_A, SMILES_B)[index]
        self._send(200, json.dumps(response_for(smiles), separators=(",", ":")).encode())


def _write_fake_rdkit(root: Path) -> None:
    package = root / "rdkit" / "Chem"
    package.mkdir(parents=True)
    (root / "rdkit" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from . import QED\n"
        "class Molecule:\n"
        "    def __init__(self, smiles): self.smiles = smiles\n"
        "    def GetNumAtoms(self): return 24\n"
        "def MolFromSmiles(smiles): return None if smiles == 'INVALID' else Molecule(smiles)\n",
        encoding="utf-8",
    )
    (package / "QED.py").write_text(
        "VALUES = " + repr(QED_VALUES) + "\n"
        "def qed(molecule): return VALUES.get(molecule.smiles, 0.5)\n",
        encoding="utf-8",
    )


class EndToEndTests(unittest.TestCase):
    def test_exactly_two_distinct_real_calls(self) -> None:
        _StrictHandler.cases = json.loads(REQUEST.read_bytes())["cases"]
        _StrictHandler.request_ids = []
        _StrictHandler.requests = []
        _StrictHandler.errors = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StrictHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_fake_rdkit(root)
                receipts = root / "receipts"
                environment = dict(os.environ)
                environment.update(
                    {
                        "HTTP_PROXY": "http://127.0.0.1:1",
                        "HTTPS_PROXY": "http://127.0.0.1:1",
                        "ALL_PROXY": "http://127.0.0.1:1",
                        "NO_PROXY": "",
                        "PYTHONPATH": str(root),
                    }
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(VALIDATOR),
                        "--base-url",
                        f"http://127.0.0.1:{server.server_port}",
                        "--request-file",
                        str(REQUEST),
                        "--receipt-dir",
                        str(receipts),
                        "--run-id",
                        "molmim-ut-semantic-a",
                        "--run-id",
                        "molmim-ut-semantic-b",
                        "--ready-timeout",
                        "3",
                        "--timeout",
                        "3",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                summary = json.loads(completed.stdout)
                self.assertEqual(summary["status"], "PASS")
                self.assertEqual(summary["passed_case_count"], 2)
                self.assertEqual(summary["inference_path"], "/generate")
                self.assertEqual(
                    _StrictHandler.request_ids,
                    ["molmim-ut-semantic-a", "molmim-ut-semantic-b"],
                )
                self.assertNotEqual(
                    summary["cases"][0]["request_sha256"],
                    summary["cases"][1]["request_sha256"],
                )
                self.assertNotEqual(
                    summary["cases"][0]["invariant"]["smiles"],
                    summary["cases"][1]["invariant"]["smiles"],
                )
                self.assertLessEqual(
                    summary["cases"][0]["response_received_at"],
                    summary["cases"][1]["response_received_at"],
                )
                self.assertLessEqual(
                    summary["cases"][1]["response_received_at"],
                    summary["validation_completed_at"],
                )
                self.assertEqual(
                    summary["finished_at"], summary["validation_completed_at"]
                )
                self.assertEqual(_StrictHandler.errors, [])
                self.assertTrue((receipts / "response-1.json").is_file())
                self.assertTrue((receipts / "response-2.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
