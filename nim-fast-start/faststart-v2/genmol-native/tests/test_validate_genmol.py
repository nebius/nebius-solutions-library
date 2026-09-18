#!/usr/bin/env python3
"""Offline tests for the frozen GenMol QED/LogP semantic contract."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import math
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "validate_genmol.py"
REQUEST = ROOT / "fixtures" / "requests-qed-logp.json"

SPEC = importlib.util.spec_from_file_location("validate_genmol", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


class _FakeMolecule:
    def __init__(self, smiles: str) -> None:
        self.smiles = smiles

    def GetNumAtoms(self) -> int:
        return {"CCO": 3, "CCCC": 4}.get(self.smiles, 1)


class _FakeChem:
    @staticmethod
    def MolFromSmiles(smiles: str) -> _FakeMolecule | None:
        return None if smiles == "not-smiles" else _FakeMolecule(smiles)


class _FakeQED:
    @staticmethod
    def qed(molecule: _FakeMolecule) -> float:
        return 0.4205 if molecule.smiles == "CCO" else 0.1


class _FakeCrippen:
    @staticmethod
    def MolLogP(molecule: _FakeMolecule) -> float:
        return 1.462 if molecule.smiles == "CCCC" else -0.1


FAKE_RDKIT = (_FakeChem, _FakeCrippen, _FakeQED)


def response_for(scoring: str) -> dict[str, Any]:
    if scoring == "QED":
        return {
            "status": "success",
            "molecules": [{"smiles": "CCO", "score": 0.42}],
        }
    return {
        "status": "success",
        "molecules": [{"smiles": "CCCC", "score": 1.46}],
    }


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests = validator._read_fixture(REQUEST)

    def test_fixture_is_exact_qed_then_logp_pair(self) -> None:
        self.assertEqual([item["name"] for item in self.requests], ["qed", "logp"])
        self.assertEqual(
            [item["payload"]["scoring"] for item in self.requests],
            ["QED", "LogP"],
        )
        self.assertNotEqual(self.requests[0]["payload"], self.requests[1]["payload"])

    def test_strict_qed_and_logp_responses_satisfy_rdkit_contract(self) -> None:
        with mock.patch.object(validator, "_rdkit_modules", return_value=FAKE_RDKIT):
            qed = validator._validate_response(response_for("QED"), "QED")
            logp = validator._validate_response(response_for("LogP"), "LogP")
        self.assertEqual(qed["atom_count"], 3)
        self.assertEqual(logp["atom_count"], 4)
        self.assertLessEqual(qed["absolute_error"], 0.02)
        self.assertLessEqual(logp["absolute_error"], 0.05)
        self.assertTrue(math.isfinite(qed["rdkit_score"]))
        self.assertTrue(math.isfinite(logp["rdkit_score"]))

    def test_tampered_fixture_is_rejected_before_network_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            value = {"calls": copy.deepcopy(self.requests)}
            value["calls"][0]["payload"]["num_molecules"] = 2
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

    def test_nonfinite_score_is_rejected(self) -> None:
        response = response_for("QED")
        response["molecules"][0]["score"] = float("nan")
        with mock.patch.object(validator, "_rdkit_modules", return_value=FAKE_RDKIT):
            with self.assertRaisesRegex(validator.ValidationError, "must be finite"):
                validator._validate_response(response, "QED")

    def test_invalid_smiles_is_rejected(self) -> None:
        response = response_for("LogP")
        response["molecules"][0]["smiles"] = "not-smiles"
        with mock.patch.object(validator, "_rdkit_modules", return_value=FAKE_RDKIT):
            with self.assertRaisesRegex(validator.ValidationError, "does not parse"):
                validator._validate_response(response, "LogP")

    def test_descriptor_mismatch_is_rejected(self) -> None:
        response = response_for("QED")
        response["molecules"][0]["score"] = 0.8
        with mock.patch.object(validator, "_rdkit_modules", return_value=FAKE_RDKIT):
            with self.assertRaisesRegex(validator.ValidationError, "disagrees with RDKit"):
                validator._validate_response(response, "QED")


class _StrictHandler(BaseHTTPRequestHandler):
    expected_calls: list[dict[str, Any]] = []
    request_ids: list[str] = []
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
            expected_index = len(self.request_ids)
            if expected_index >= 2 or body != self.expected_calls[expected_index]["payload"]:
                raise AssertionError("request body was not the next frozen semantic case")
            self.request_ids.append(request_id)
            scoring = body["scoring"]
        except Exception as exc:
            self.errors.append(str(exc))
            self._send(400, json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, json.dumps(response_for(scoring)).encode())


class EndToEndTests(unittest.TestCase):
    def test_exactly_two_distinct_real_http_calls(self) -> None:
        _StrictHandler.expected_calls = json.loads(REQUEST.read_bytes())["calls"]
        _StrictHandler.request_ids = []
        _StrictHandler.errors = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StrictHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                receipts = Path(directory) / "receipts"
                arguments = [
                    str(VALIDATOR),
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--request-file",
                    str(REQUEST),
                    "--receipt-dir",
                    str(receipts),
                    "--run-id",
                    "genmol-ut-semantic-a",
                    "--run-id",
                    "genmol-ut-semantic-b",
                    "--ready-timeout",
                    "3",
                    "--timeout",
                    "3",
                ]
                stdout = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", arguments),
                    mock.patch.object(validator, "_rdkit_modules", return_value=FAKE_RDKIT),
                    redirect_stdout(stdout),
                ):
                    status = validator.main()
                self.assertEqual(status, 0, stdout.getvalue())
                summary = json.loads(stdout.getvalue())
                self.assertEqual(summary["status"], "PASS")
                self.assertEqual(summary["passed_case_count"], 2)
                self.assertEqual(
                    validator.RESPONSE_TIMING_CONTRACT,
                    summary["response_timing_contract"],
                )
                self.assertEqual(summary["finished_at"], summary["validation_finished_at"])
                for case in summary["cases"]:
                    self.assertLessEqual(case["request_started_at"], case["response_received_at"])
                self.assertEqual(
                    [case["name"] for case in summary["cases"]], ["qed", "logp"]
                )
                self.assertEqual(
                    _StrictHandler.request_ids,
                    ["genmol-ut-semantic-a", "genmol-ut-semantic-b"],
                )
                self.assertEqual(_StrictHandler.errors, [])
                self.assertTrue((receipts / "response-1-qed.json").is_file())
                self.assertTrue((receipts / "response-2-logp.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
