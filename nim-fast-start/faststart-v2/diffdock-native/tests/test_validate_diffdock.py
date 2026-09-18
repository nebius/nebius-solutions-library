#!/usr/bin/env python3
"""Offline tests for the frozen DiffDock two-call semantic contract."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "validate_diffdock.py"
REQUEST = ROOT / "fixtures" / "1ubq-aspirin-request.json"
RESPONSE = Path(__file__).resolve().parent / "fixtures" / "strict-pass-response.json"

SPEC = importlib.util.spec_from_file_location("validate_diffdock", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = validator._read_fixture(REQUEST)
        self.response = json.loads(RESPONSE.read_bytes())

    def test_retained_strict_pass_response_satisfies_contract(self) -> None:
        invariant = validator._validate_response(self.response, self.fixture)
        self.assertEqual(invariant["ligand"], validator.EXPECTED_LIGAND)
        self.assertEqual(invariant["protein_bytes"], 78_570)
        self.assertEqual(invariant["pose"]["atom_count"], 13)
        self.assertEqual(invariant["pose"]["finite_coordinate_count"], 13)
        self.assertTrue(math.isfinite(invariant["position_confidence"]))

    def test_fixture_is_exact_retained_1ubq_aspirin_request(self) -> None:
        self.assertEqual(self.fixture["ligand"], "CC(=O)Oc1ccccc1C(=O)O")
        self.assertEqual(self.fixture["ligand_file_type"], "txt")
        self.assertEqual(self.fixture["num_poses"], 1)
        self.assertEqual(self.fixture["time_divisions"], 20)
        self.assertEqual(self.fixture["steps"], 18)

    def test_tampered_fixture_is_rejected_before_network_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            value = copy.deepcopy(self.fixture)
            value["ligand"] = "C"
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
        self.assertFalse(validator._health_is_ready(False))

    def test_wrong_returned_ligand_is_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        value["ligand"] = "C"
        with self.assertRaisesRegex(validator.ValidationError, "submitted aspirin"):
            validator._validate_response(value, self.fixture)

    def test_wrong_returned_receptor_is_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        value["protein"] = value["protein"].replace("1UBQ", "2XYZ", 1)
        with self.assertRaisesRegex(validator.ValidationError, "1UBQ receptor"):
            validator._validate_response(value, self.fixture)

    def test_nonfinite_confidence_is_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        value["position_confidence"] = [float("nan")]
        with self.assertRaisesRegex(validator.ValidationError, "must be finite"):
            validator._validate_response(value, self.fixture)

    def test_truncated_pose_is_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        value["ligand_positions"] = ["aspirin\nV2000\nM  END\n"]
        with self.assertRaisesRegex(validator.ValidationError, "nontrivial"):
            validator._validate_response(value, self.fixture)

    def test_degenerate_coordinates_are_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        lines = value["ligand_positions"][0].splitlines()
        atom = lines[4]
        lines[4:17] = [atom] * 13
        value["ligand_positions"] = ["\n".join(lines) + "\n"]
        with self.assertRaisesRegex(validator.ValidationError, "degenerate"):
            validator._validate_response(value, self.fixture)

    def test_missing_trajectory_is_rejected(self) -> None:
        value = copy.deepcopy(self.response)
        value["trajectory"] = []
        with self.assertRaisesRegex(validator.ValidationError, "trajectory"):
            validator._validate_response(value, self.fixture)


class _StrictHandler(BaseHTTPRequestHandler):
    fixture: dict[str, Any]
    response: bytes
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
        if self.path != validator.READY_ENDPOINT:
            self._send(404, b"{}")
            return
        self._send(200, b"true")

    def do_POST(self) -> None:
        if self.path != validator.ENDPOINT:
            self._send(404, b"{}")
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            body = self.rfile.read(length)
            if json.loads(body) != self.fixture:
                raise AssertionError("request body was not the pinned fixture")
            request_id = self.headers.get("X-Request-ID")
            if request_id is None:
                raise AssertionError("X-Request-ID is missing")
            self.request_ids.append(request_id)
        except Exception as exc:  # test server records a precise contract error
            self.errors.append(str(exc))
            self._send(400, json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, self.response)


class EndToEndTests(unittest.TestCase):
    def test_validator_polls_ready_then_executes_exactly_two_real_cases(self) -> None:
        _StrictHandler.fixture = json.loads(REQUEST.read_bytes())
        _StrictHandler.response = RESPONSE.read_bytes()
        _StrictHandler.request_ids = []
        _StrictHandler.errors = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StrictHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                receipts = Path(directory) / "receipts"
                environment = dict(os.environ)
                environment.update(
                    {
                        "HTTP_PROXY": "http://127.0.0.1:1",
                        "HTTPS_PROXY": "http://127.0.0.1:1",
                        "ALL_PROXY": "http://127.0.0.1:1",
                        "NO_PROXY": "",
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
                        "dd-ut-semantic-a",
                        "--run-id",
                        "dd-ut-semantic-b",
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
                self.assertEqual(
                    validator.RESPONSE_TIMING_CONTRACT,
                    summary["response_timing_contract"],
                )
                self.assertEqual(summary["finished_at"], summary["validation_finished_at"])
                for case in summary["cases"]:
                    self.assertLessEqual(case["request_started_at"], case["response_received_at"])
                self.assertEqual(summary["proxy_policy"], "disabled")
                self.assertEqual(len(summary["cases"]), 2)
                self.assertEqual(
                    _StrictHandler.request_ids,
                    ["dd-ut-semantic-a", "dd-ut-semantic-b"],
                )
                self.assertEqual(_StrictHandler.errors, [])
                self.assertTrue((receipts / "response-1.json").is_file())
                self.assertTrue((receipts / "response-2.json").is_file())
                self.assertTrue((receipts / "summary.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
