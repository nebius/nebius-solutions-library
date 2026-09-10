from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.request import ProxyHandler
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "validate_openfold2.py"
SPEC = importlib.util.spec_from_file_location("openfold2_faststart_validator", SOURCE)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import bootstrap guard.
    raise RuntimeError(f"cannot import {SOURCE}")
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


ONE_TO_THREE = {value: key for key, value in VALIDATOR._PDB_THREE_TO_ONE.items()}


def pdb_for(sequence: str) -> str:
    lines = ["MODEL        1"]
    serial = 1
    for residue_index, residue in enumerate(sequence, 1):
        for atom_name, element, offset in (
            ("N", "N", 0.0),
            ("CA", "C", 0.2),
            ("C", "C", 0.4),
            ("O", "O", 0.6),
        ):
            coordinate = residue_index + offset
            lines.append(
                f"ATOM  {serial:5d} {atom_name:^4s} {ONE_TO_THREE[residue]:>3s} "
                f"A{residue_index:4d}    "
                f"{coordinate:8.3f}{coordinate + 1:8.3f}{coordinate + 2:8.3f}"
                f"  1.00 50.00          {element:>2s}"
            )
            serial += 1
    lines.extend(("TER", "ENDMDL", "END"))
    return "\n".join(lines) + "\n"


def response_for(run_id: str, sequence: str) -> dict[str, Any]:
    return {
        "input_id": run_id,
        "metrics": {},
        "of2_nim_handled_error_message": "no-handled-error",
        "structures_in_ranked_order": [
            {
                "confidence": 82.5,
                "format": "pdb",
                "model_param_set": 1,
                "plddt": [70.0 + (index / 100) for index in range(20)],
                "predicted_aligned_error": [
                    [float(abs(row - column)) for column in range(20)]
                    for row in range(20)
                ],
                "ptm_score": 0.67,
                "relaxed": False,
                "structure": pdb_for(sequence),
            }
        ],
    }


@contextmanager
def serving(
    callback: Callable[[BaseHTTPRequestHandler, bytes], None],
    ready_callback: Callable[[BaseHTTPRequestHandler], None] | None = None,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            calls.append(
                {
                    "body": body,
                    "headers": dict(self.headers),
                    "method": "POST",
                    "path": self.path,
                }
            )
            callback(self, body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            calls.append(
                {
                    "body": b"",
                    "headers": dict(self.headers),
                    "method": "GET",
                    "path": self.path,
                }
            )
            if ready_callback is None:
                self.send_error(404)
            else:
                ready_callback(self)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def send_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> bytes:
    body = VALIDATOR.json_bytes(payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return body


def raw_http(payload: Any, status: int = 200, reason: str = "OK") -> bytes:
    body = VALIDATOR.json_bytes(payload)
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"content-length: {len(body)}\r\n"
        "content-type: application/json\r\n"
        "connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


def private_write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


class RequestContractTests(unittest.TestCase):
    def test_accepts_the_two_observed_openfold2_readiness_payloads(self) -> None:
        self.assertTrue(VALIDATOR.readiness_body_is_ready(b"true\n"))
        self.assertTrue(
            VALIDATOR.readiness_body_is_ready(
                b'{"object":"health.response","message":"ready","status":"ready"}'
            )
        )
        self.assertFalse(VALIDATOR.readiness_body_is_ready(b'{"status":"starting"}'))

    def test_builds_two_distinct_fixed_20_residue_requests(self) -> None:
        probes = VALIDATOR.build_probes(("caller-run-a", "caller-run-b"))

        self.assertEqual(2, len(probes))
        self.assertEqual({20}, {len(probe.sequence) for probe in probes})
        self.assertNotEqual(probes[0].sequence, probes[1].sequence)
        self.assertEqual(("caller-run-a", "caller-run-b"), tuple(p.run_id for p in probes))
        for probe in probes:
            self.assertEqual(
                {
                    "input_id": probe.run_id,
                    "relax_prediction": False,
                    "selected_models": [1],
                    "sequence": probe.sequence,
                },
                probe.payload,
            )

    def test_rejects_duplicate_or_missing_run_ids(self) -> None:
        with self.assertRaisesRegex(VALIDATOR.SetupFailure, "exactly twice"):
            VALIDATOR.build_probes(("only-one",))
        with self.assertRaisesRegex(VALIDATOR.SetupFailure, "must be unique"):
            VALIDATOR.build_probes(("same", "same"))

    def test_accepts_only_an_http_origin(self) -> None:
        self.assertEqual("https://example.test:8443", VALIDATOR.validate_base_url("https://example.test:8443/"))
        for invalid in (
            "ftp://example.test",
            "http://user@example.test",
            "http://example.test/a",
            "http://example.test?query=1",
            "http://example.test#fragment",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(VALIDATOR.SetupFailure):
                    VALIDATOR.validate_base_url(invalid)

    def test_default_opener_installs_no_environment_proxy_handler(self) -> None:
        opener = VALIDATOR.direct_opener()
        proxy_handlers = [handler for handler in opener.handlers if isinstance(handler, ProxyHandler)]

        # build_opener sees the explicit ProxyHandler({}), suppresses its
        # environment-derived default, then omits the inert empty handler.
        self.assertEqual([], proxy_handlers)
        self.assertTrue(any(isinstance(handler, VALIDATOR.RejectRedirects) for handler in opener.handlers))


class SemanticValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "semantic-run-01"
        self.sequence = VALIDATOR.FIXED_SEQUENCES[0]

    def test_accepts_a_canonical_response(self) -> None:
        invariant = VALIDATOR.validate_response(
            response_for(self.run_id, self.sequence), self.run_id, self.sequence
        )

        self.assertEqual([20, 20], invariant["pae_shape"])
        self.assertEqual(20, invariant["plddt_count"])
        self.assertEqual(self.sequence, invariant["pdb"]["sequence"])
        self.assertEqual(20, invariant["pdb"]["backbone_residue_count"])
        self.assertEqual(80 * 3, invariant["pdb"]["finite_coordinate_count"])

    def test_rejects_response_shape_and_score_violations(self) -> None:
        def item(payload: dict[str, Any]) -> dict[str, Any]:
            return payload["structures_in_ranked_order"][0]

        mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("wrong input_id", lambda payload: payload.update(input_id="another-run")),
            (
                "handled error",
                lambda payload: payload.update(of2_nim_handled_error_message="model failed"),
            ),
            (
                "two structures",
                lambda payload: payload["structures_in_ranked_order"].append(copy.deepcopy(item(payload))),
            ),
            ("relaxed", lambda payload: item(payload).update(relaxed=True)),
            ("model 2", lambda payload: item(payload).update(model_param_set=2)),
            ("boolean model", lambda payload: item(payload).update(model_param_set=True)),
            ("infinite confidence", lambda payload: item(payload).update(confidence=float("inf"))),
            ("short pLDDT", lambda payload: item(payload).update(plddt=[50.0] * 19)),
            (
                "nonfinite pLDDT",
                lambda payload: item(payload)["plddt"].__setitem__(0, float("nan")),
            ),
            (
                "short PAE",
                lambda payload: item(payload).update(predicted_aligned_error=[[0.0] * 20] * 19),
            ),
            (
                "short PAE row",
                lambda payload: item(payload)["predicted_aligned_error"].__setitem__(0, [0.0] * 19),
            ),
            (
                "negative PAE",
                lambda payload: item(payload)["predicted_aligned_error"][0].__setitem__(0, -0.01),
            ),
            (
                "nonfinite PAE",
                lambda payload: item(payload)["predicted_aligned_error"][0].__setitem__(0, float("inf")),
            ),
            ("pTM above one", lambda payload: item(payload).update(ptm_score=1.01)),
            ("nonfinite pTM", lambda payload: item(payload).update(ptm_score=float("nan"))),
        ]

        for label, mutate in mutations:
            with self.subTest(label=label):
                payload = response_for(self.run_id, self.sequence)
                mutate(payload)
                with self.assertRaises(VALIDATOR.SemanticFailure):
                    VALIDATOR.validate_response(payload, self.run_id, self.sequence)

    def test_rejects_sequence_backbone_and_coordinate_violations(self) -> None:
        with self.assertRaisesRegex(VALIDATOR.SemanticFailure, "sequence"):
            VALIDATOR.validate_response(
                response_for(self.run_id, self.sequence),
                self.run_id,
                VALIDATOR.FIXED_SEQUENCES[1],
            )

        missing_backbone = response_for(self.run_id, self.sequence)
        pdb_lines = missing_backbone["structures_in_ranked_order"][0]["structure"].splitlines()
        pdb_lines = [
            line
            for line in pdb_lines
            if not (
                line.startswith("ATOM")
                and line[22:26].strip() == "1"
                and line[12:16].strip() == "O"
            )
        ]
        missing_backbone["structures_in_ranked_order"][0]["structure"] = "\n".join(pdb_lines) + "\n"
        with self.assertRaisesRegex(VALIDATOR.SemanticFailure, "lacks backbone"):
            VALIDATOR.validate_response(missing_backbone, self.run_id, self.sequence)

        nonfinite = response_for(self.run_id, self.sequence)
        pdb_lines = nonfinite["structures_in_ranked_order"][0]["structure"].splitlines()
        atom_index = next(index for index, line in enumerate(pdb_lines) if line.startswith("ATOM"))
        atom_line = pdb_lines[atom_index]
        pdb_lines[atom_index] = atom_line[:30] + f"{float('nan'):8.3f}" + atom_line[38:]
        nonfinite["structures_in_ranked_order"][0]["structure"] = "\n".join(pdb_lines) + "\n"
        with self.assertRaisesRegex(VALIDATOR.SemanticFailure, "non-finite coordinate"):
            VALIDATOR.validate_response(nonfinite, self.run_id, self.sequence)


class ReceiptAndTransportTests(unittest.TestCase):
    def test_response_latency_stops_before_semantic_validation(self) -> None:
        def callback(handler: BaseHTTPRequestHandler, body: bytes) -> None:
            request = json.loads(body)
            send_json(handler, response_for(request["input_id"], request["sequence"]))

        original_validate = VALIDATOR.validate_response

        def slow_validate(*args: Any, **kwargs: Any) -> dict[str, Any]:
            time.sleep(0.05)
            return original_validate(*args, **kwargs)

        with serving(callback) as (base_url, _calls), tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            VALIDATOR, "validate_response", side_effect=slow_validate
        ):
            summary = VALIDATOR.run_validation(
                base_url=base_url,
                receipt_dir=Path(temporary) / "receipt",
                run_ids=("timing-a", "timing-b"),
                timeout=5,
            )

        self.assertEqual(summary["status"], "PASS")
        self.assertGreater(
            summary["validation_total_elapsed_seconds"]
            - sum(case["elapsed_seconds"] for case in summary["cases"]),
            0.08,
        )

    def test_waits_for_exact_health_before_two_semantic_requests(self) -> None:
        readiness_attempts = 0

        def callback(handler: BaseHTTPRequestHandler, body: bytes) -> None:
            request = json.loads(body)
            send_json(handler, response_for(request["input_id"], request["sequence"]))

        def ready_callback(handler: BaseHTTPRequestHandler) -> None:
            nonlocal readiness_attempts
            readiness_attempts += 1
            body = (
                b'{"object":"health.response","message":"ready","status":"ready"}\n'
                if readiness_attempts >= 3
                else b"not-ready\n"
            )
            handler.send_response(200 if readiness_attempts >= 3 else 503)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        with serving(callback, ready_callback) as (base_url, calls), tempfile.TemporaryDirectory() as temporary:
            summary = VALIDATOR.run_validation(
                base_url=base_url,
                receipt_dir=Path(temporary) / "receipt",
                run_ids=("early-a", "early-b"),
                timeout=5,
                ready_timeout=5,
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(3, summary["ready_wait"]["attempts"])
        self.assertEqual(VALIDATOR.READY_PATH, summary["ready_wait"]["endpoint"].split(base_url)[1])
        self.assertEqual(
            ["GET", "GET", "GET", "POST", "POST"],
            [call["method"] for call in calls],
        )
        self.assertEqual(
            [VALIDATOR.OPENFOLD2_PATH, VALIDATOR.OPENFOLD2_PATH],
            [call["path"] for call in calls if call["method"] == "POST"],
        )

    def test_posts_exact_route_and_retains_private_exact_bytes(self) -> None:
        returned_bodies: list[bytes] = []

        def callback(handler: BaseHTTPRequestHandler, body: bytes) -> None:
            request = json.loads(body)
            returned_bodies.append(send_json(handler, response_for(request["input_id"], request["sequence"])))

        with serving(callback) as (base_url, calls), tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt"
            summary = VALIDATOR.run_validation(
                base_url=base_url,
                receipt_dir=receipt,
                run_ids=("caller-a", "caller-b"),
                timeout=5,
            )

            self.assertTrue(summary["ok"])
            self.assertEqual(0, summary["exit_code"])
            self.assertEqual(
                VALIDATOR.RESPONSE_TIMING_CONTRACT,
                summary["response_timing_contract"],
            )
            self.assertEqual(summary["finished_at"], summary["validation_finished_at"])
            for case in summary["cases"]:
                self.assertLessEqual(case["request_started_at"], case["response_received_at"])
                self.assertGreaterEqual(case["elapsed_seconds"], 0)
            self.assertEqual(2, len(calls))
            self.assertEqual(
                [VALIDATOR.OPENFOLD2_PATH, VALIDATOR.OPENFOLD2_PATH],
                [call["path"] for call in calls],
            )
            requests = [json.loads(call["body"]) for call in calls]
            self.assertEqual(["caller-a", "caller-b"], [request["input_id"] for request in requests])
            self.assertEqual(list(VALIDATOR.FIXED_SEQUENCES), [request["sequence"] for request in requests])

            self.assertEqual(0o700, stat.S_IMODE(receipt.stat().st_mode))
            expected_names = {
                "request-01.json",
                "request-02.json",
                "response-01.raw",
                "response-02.raw",
                "summary.json",
            }
            self.assertEqual(expected_names, {path.name for path in receipt.iterdir()})
            for path in receipt.iterdir():
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode), path.name)
            self.assertEqual(calls[0]["body"], (receipt / "request-01.json").read_bytes())
            self.assertEqual(calls[1]["body"], (receipt / "request-02.json").read_bytes())
            self.assertEqual(returned_bodies[0], (receipt / "response-01.raw").read_bytes())
            self.assertEqual(returned_bodies[1], (receipt / "response-02.raw").read_bytes())
            self.assertEqual(summary, json.loads((receipt / "summary.json").read_bytes()))

    def test_environment_proxy_is_not_used(self) -> None:
        def callback(handler: BaseHTTPRequestHandler, body: bytes) -> None:
            request = json.loads(body)
            send_json(handler, response_for(request["input_id"], request["sequence"]))

        proxy_environment = {
            "ALL_PROXY": "http://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "all_proxy": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
        }
        with serving(callback) as (base_url, calls), tempfile.TemporaryDirectory() as temporary:
            original = {key: os.environ.get(key) for key in proxy_environment}
            try:
                os.environ.update(proxy_environment)
                summary = VALIDATOR.run_validation(
                    base_url=base_url,
                    receipt_dir=Path(temporary) / "receipt",
                    run_ids=("proxy-a", "proxy-b"),
                    timeout=5,
                )
            finally:
                for key, value in original.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        self.assertTrue(summary["ok"])
        self.assertEqual(2, len(calls))

    def test_redirects_are_not_followed_and_raw_bodies_are_retained(self) -> None:
        redirect_body = b"redirect refused\n"

        def callback(handler: BaseHTTPRequestHandler, _body: bytes) -> None:
            if handler.path == VALIDATOR.OPENFOLD2_PATH:
                handler.send_response(307)
                handler.send_header("Location", "/redirect-target")
                handler.send_header("Content-Length", str(len(redirect_body)))
                handler.end_headers()
                handler.wfile.write(redirect_body)
            else:
                send_json(handler, {"unexpected": True})

        with serving(callback) as (base_url, calls), tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt"
            summary = VALIDATOR.run_validation(
                base_url=base_url,
                receipt_dir=receipt,
                run_ids=("redirect-a", "redirect-b"),
                timeout=5,
            )

            self.assertFalse(summary["ok"])
            self.assertEqual(3, summary["exit_code"])
            self.assertEqual(2, len(calls))
            self.assertEqual({VALIDATOR.OPENFOLD2_PATH}, {call["path"] for call in calls})
            self.assertEqual(
                ["ERROR_REDIRECT", "ERROR_REDIRECT"],
                [case["status"] for case in summary["cases"]],
            )
            self.assertEqual(redirect_body, (receipt / "response-01.raw").read_bytes())
            self.assertEqual(redirect_body, (receipt / "response-02.raw").read_bytes())
            self.assertEqual(0o600, stat.S_IMODE((receipt / "summary.json").stat().st_mode))

    def test_transport_failures_attempt_both_probes_and_retain_empty_raw_files(self) -> None:
        class FailingOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request: object, timeout: float) -> None:
                self.calls += 1
                self.timeout = timeout
                raise VALIDATOR.URLError("deliberately offline")

        opener = FailingOpener()
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt"
            summary = VALIDATOR.run_validation(
                base_url="http://127.0.0.1:8000",
                receipt_dir=receipt,
                run_ids=("transport-a", "transport-b"),
                timeout=5,
                opener=opener,
            )

            self.assertEqual(2, opener.calls)
            self.assertEqual(3, summary["exit_code"])
            self.assertEqual(
                ["ERROR_TRANSPORT", "ERROR_TRANSPORT"],
                [case["status"] for case in summary["cases"]],
            )
            for name in ("response-01.raw", "response-02.raw"):
                path = receipt / name
                self.assertEqual(b"", path.read_bytes())
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_receipt_directory_must_be_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "already-there"
            receipt.mkdir()
            with self.assertRaisesRegex(VALIDATOR.SetupFailure, "already exists"):
                VALIDATOR.run_validation(
                    base_url="http://127.0.0.1:8000",
                    receipt_dir=receipt,
                    run_ids=("fresh-a", "fresh-b"),
                    timeout=1,
                )


class OfflineRawHttpTests(unittest.TestCase):
    def make_files(self, root: Path) -> tuple[list[Path], list[Path]]:
        requests: list[Path] = []
        responses: list[Path] = []
        for index, (run_id, sequence) in enumerate(
            zip(("offline-a", "offline-b"), VALIDATOR.FIXED_SEQUENCES, strict=True), 1
        ):
            request = root / f"request-{index}.json"
            response = root / f"response-{index}.http"
            probe = VALIDATOR.build_probes(("offline-a", "offline-b"))[index - 1]
            private_write(request, VALIDATOR.json_bytes(probe.payload).rstrip(b"\n"))
            private_write(response, raw_http(response_for(run_id, sequence)))
            requests.append(request)
            responses.append(response)
        return requests, responses

    def test_validates_two_complete_raw_http_responses_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            requests, responses = self.make_files(Path(temporary))
            summary = VALIDATOR.run_offline_validation(
                base_url="http://10.0.0.42:8000",
                request_paths=requests,
                response_paths=responses,
                run_ids=("offline-a", "offline-b"),
            )

        self.assertTrue(summary["ok"])
        self.assertEqual("offline-raw-http", summary["mode"])
        self.assertFalse(summary["network_io"])
        self.assertEqual([200, 200], [case["http_status"] for case in summary["cases"]])

    def test_rejects_redirects_and_still_checks_both_captures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            requests, responses = self.make_files(Path(temporary))
            for response in responses:
                private_write(response, raw_http({"redirect": True}, 307, "Temporary Redirect"))
            summary = VALIDATOR.run_offline_validation(
                base_url="http://10.0.0.42:8000",
                request_paths=requests,
                response_paths=responses,
                run_ids=("offline-a", "offline-b"),
            )

        self.assertFalse(summary["ok"])
        self.assertEqual(3, summary["exit_code"])
        self.assertEqual(
            ["ERROR_REDIRECT", "ERROR_REDIRECT"],
            [case["status"] for case in summary["cases"]],
        )

    def test_rejects_chunking_bad_lengths_and_nonprivate_evidence(self) -> None:
        good_body = b"{}\n"
        malformed = (
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
            b"content-length: 99\r\n\r\n" + good_body
        )
        with self.assertRaises(VALIDATOR.TransportFailure):
            VALIDATOR.parse_raw_http_response(malformed)
        with self.assertRaises(VALIDATOR.TransportFailure):
            VALIDATOR.parse_raw_http_response(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                b"transfer-encoding: chunked\r\n\r\n0\r\n\r\n"
            )

        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "world-readable"
            evidence.write_bytes(b"{}")
            evidence.chmod(0o644)
            with self.assertRaisesRegex(VALIDATOR.SetupFailure, "group or other"):
                VALIDATOR.read_private_file(evidence, 100, "test evidence")


if __name__ == "__main__":
    unittest.main()
