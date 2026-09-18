#!/usr/bin/env python3
"""Offline tests for the frozen OpenFold3 two-call semantic contract."""

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
VALIDATOR = ROOT / "validate_openfold3.py"
REQUEST = ROOT / "fixtures" / "request-20aa.json"

SPEC = importlib.util.spec_from_file_location("validate_openfold3", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def cif_document() -> str:
    header = (
        "data_structure\n#\nloop_\n_atom_site.Cartn_x\n"
        "_atom_site.Cartn_y\n_atom_site.Cartn_z\n"
    )
    atoms = "".join(
        f"ATOM {index} C C . ALA A 1 1 ? {index:.3f} 2.000 3.000 1.00 10.0\n"
        for index in range(1, 168)
    )
    return header + ("# retained-openfold3-contract\n" * 200) + atoms


def response_for(request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "outputs": [
            {
                "input_id": request_id,
                "runtime_metrics": {},
                "structures_with_scores": [
                    {
                        "format": "cif",
                        "name": f"{request_id}_sample_1",
                        "source": "seed_42",
                        "structure": cif_document(),
                        "confidence_score": 0.05,
                        "complex_plddt_score": 56.9,
                        "complex_pde_score": 1.69,
                        "ptm_score": 0.046,
                        "iptm_score": 0.0,
                    }
                ],
            }
        ],
    }


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = validator._read_fixture(REQUEST)
        self.response = response_for("of3-ut-semantic-a")

    def test_fixture_is_exact_retained_query_only_msa_request(self) -> None:
        molecule = self.fixture["inputs"][0]["molecules"][0]
        self.assertEqual(molecule["sequence"], "ACDEFGHIKLMNPQRSTVWY")
        self.assertEqual(molecule["diffusion_samples"], 1)
        self.assertEqual(molecule["msa"]["main"]["a3m"]["format"], "a3m")

    def test_distinct_case_rewrites_both_identifiers(self) -> None:
        case = validator._request_for_case(self.fixture, "of3-ut-semantic-a")
        self.assertEqual(case["request_id"], "of3-ut-semantic-a")
        self.assertEqual(case["inputs"][0]["input_id"], "of3-ut-semantic-a")
        self.assertEqual(
            self.fixture["request_id"], validator.EXPECTED_TEMPLATE_ID
        )

    def test_strict_pass_response_satisfies_contract(self) -> None:
        invariant = validator._validate_response(
            self.response, "of3-ut-semantic-a"
        )
        self.assertEqual(invariant["structure"]["atom_rows"], 167)
        self.assertGreater(invariant["structure"]["characters"], 10_000)
        self.assertTrue(
            all(math.isfinite(value) for value in invariant["scores"].values())
        )

    def test_tampered_fixture_is_rejected_before_network_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            value = copy.deepcopy(self.fixture)
            value["inputs"][0]["molecules"][0]["sequence"] = "A"
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

    def test_wrong_call_identity_is_rejected(self) -> None:
        self.response["request_id"] = "wrong"
        with self.assertRaisesRegex(validator.ValidationError, "request_id"):
            validator._validate_response(self.response, "of3-ut-semantic-a")

    def test_nonfinite_score_is_rejected(self) -> None:
        self.response["outputs"][0]["structures_with_scores"][0][
            "confidence_score"
        ] = float("nan")
        with self.assertRaisesRegex(validator.ValidationError, "must be finite"):
            validator._validate_response(self.response, "of3-ut-semantic-a")

    def test_short_or_coordinate_free_cif_is_rejected(self) -> None:
        result = self.response["outputs"][0]["structures_with_scores"][0]
        result["structure"] = "data_structure\n"
        with self.assertRaisesRegex(validator.ValidationError, "nontrivial CIF"):
            validator._validate_response(self.response, "of3-ut-semantic-a")


class _StrictHandler(BaseHTTPRequestHandler):
    fixture: dict[str, Any]
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
            expected = validator._request_for_case(self.fixture, request_id)
            if body != expected:
                raise AssertionError("request body was not the distinct strict case")
            self.request_ids.append(request_id)
        except Exception as exc:
            self.errors.append(str(exc))
            self._send(400, json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, json.dumps(response_for(request_id)).encode())


class EndToEndTests(unittest.TestCase):
    def test_exactly_two_distinct_real_calls(self) -> None:
        _StrictHandler.fixture = json.loads(REQUEST.read_bytes())
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
                        "of3-ut-semantic-a",
                        "--run-id",
                        "of3-ut-semantic-b",
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
                self.assertEqual(
                    completed.returncode, 0, completed.stderr + completed.stdout
                )
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
                self.assertEqual(
                    _StrictHandler.request_ids,
                    ["of3-ut-semantic-a", "of3-ut-semantic-b"],
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
