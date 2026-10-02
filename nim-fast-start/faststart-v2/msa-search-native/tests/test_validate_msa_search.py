#!/usr/bin/env python3
"""Offline tests for the frozen MSA Search two-query PDB70 contract."""

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


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "validate_msa_search.py"
REQUEST = ROOT / "fixtures" / "request-pdb70.json"

SPEC = importlib.util.spec_from_file_location("validate_msa_search", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def a3m_for(query: str, record_count: int = 128) -> str:
    records = [f">A|-|A\n{query}"]
    for index in range(1, record_count):
        records.append(f">hit-{index:03d}\n{query}")
    return "\n".join(records) + "\n"


def response_for(query: str, record_count: int = 128) -> dict[str, Any]:
    return {
        "metrics": {"search_type": "colabfold"},
        "alignments": {
            validator.DATABASE: {
                "a3m": {"alignment": a3m_for(query, record_count), "format": "a3m"}
            }
        },
    }


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = validator._read_fixture(REQUEST)
        self.response = response_for(validator.QUERY_1)

    def test_fixture_is_exact_retained_pdb70_request(self) -> None:
        self.assertEqual(self.fixture["sequence"], validator.QUERY_1)
        self.assertEqual(self.fixture["databases"], ["pdb70_220313"])
        self.assertEqual(self.fixture["max_msa_sequences"], 500)
        self.assertEqual(self.fixture["output_alignment_formats"], ["a3m"])

    def test_distinct_case_changes_only_query(self) -> None:
        case = validator._request_for_case(self.fixture, validator.QUERY_2)
        expected = dict(self.fixture)
        expected["sequence"] = validator.QUERY_2
        self.assertEqual(case, expected)
        self.assertEqual(self.fixture["sequence"], validator.QUERY_1)

    def test_strict_128_record_response_satisfies_contract(self) -> None:
        invariant = validator._validate_response(self.response, validator.QUERY_1)
        self.assertEqual(invariant["database"], "pdb70_220313")
        self.assertEqual(invariant["search_type"], "colabfold")
        self.assertEqual(invariant["records"], 128)
        self.assertEqual(invariant["non_query_homologs"], 127)
        self.assertEqual(invariant["query_length"], 76)
        self.assertTrue(invariant["query_echo"])

    def test_127_or_129_records_are_rejected(self) -> None:
        for count in (127, 129):
            with self.subTest(count=count), self.assertRaisesRegex(
                validator.ValidationError, "exactly 128"
            ):
                validator._validate_response(
                    response_for(validator.QUERY_1, count), validator.QUERY_1
                )

    def test_stale_primary_query_response_is_rejected_for_mutant(self) -> None:
        with self.assertRaisesRegex(validator.ValidationError, "does not echo"):
            validator._validate_response(self.response, validator.QUERY_2)

    def test_wrong_database_or_search_path_is_rejected(self) -> None:
        wrong = copy.deepcopy(self.response)
        wrong["metrics"]["search_type"] = "other"
        with self.assertRaisesRegex(validator.ValidationError, "colabfold"):
            validator._validate_response(wrong, validator.QUERY_1)
        wrong = copy.deepcopy(self.response)
        wrong["alignments"] = {"uniref90": wrong["alignments"][validator.DATABASE]}
        with self.assertRaisesRegex(validator.ValidationError, "PDB70"):
            validator._validate_response(wrong, validator.QUERY_1)

    def test_tampered_fixture_is_rejected_before_network_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            value = copy.deepcopy(self.fixture)
            value["sequence"] = validator.QUERY_2
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(validator.ValidationError, "digest"):
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


class _StrictHandler(BaseHTTPRequestHandler):
    queries: list[str] = []
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
            query = body["sequence"]
            if query not in (validator.QUERY_1, validator.QUERY_2):
                raise AssertionError("request did not contain a frozen distinct query")
            expected = json.loads(REQUEST.read_bytes())
            expected["sequence"] = query
            if body != expected:
                raise AssertionError("request body changed beyond the query")
            self.queries.append(query)
        except Exception as exc:
            self.errors.append(str(exc))
            self._send(400, json.dumps({"error": str(exc)}).encode())
            return
        self._send(200, json.dumps(response_for(query)).encode())


class EndToEndTests(unittest.TestCase):
    def test_exactly_two_distinct_real_calls(self) -> None:
        _StrictHandler.queries = []
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
                        "msa-ut-semantic-a",
                        "--run-id",
                        "msa-ut-semantic-b",
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
                self.assertTrue(summary["queries_distinct"])
                self.assertEqual(summary["expected_records_per_response"], 128)
                self.assertEqual(summary["expected_non_query_homologs_per_response"], 127)
                self.assertEqual(_StrictHandler.queries, [validator.QUERY_1, validator.QUERY_2])
                self.assertEqual(_StrictHandler.errors, [])
                self.assertNotEqual(
                    summary["cases"][0]["response_sha256"],
                    summary["cases"][1]["response_sha256"],
                )
                self.assertTrue((receipts / "response-1.json").is_file())
                self.assertTrue((receipts / "response-2.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
