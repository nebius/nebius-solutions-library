#!/usr/bin/env python3
"""Strict two-call semantic validator for the ProteinMPNN NIM."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENDPOINT = "/biology/ipd/proteinmpnn/predict"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_PDB_SHA256 = "d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161"
EXPECTED_INPUT_SEQUENCE = (
    "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
)
CANONICAL_AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")
RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"


class ValidationError(ValueError):
    """The endpoint did not satisfy the frozen semantic contract."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


HTTP = urllib.request.build_opener(_RejectRedirects)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be finite")
    return result


def _read_fixture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("request fixture must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or set(value) != {
        "input_pdb",
        "num_seq_per_target",
        "random_seed",
    }:
        raise ValidationError("request fixture has the wrong shape")
    pdb = value["input_pdb"]
    if not isinstance(pdb, str) or hashlib.sha256(pdb.encode()).hexdigest() != EXPECTED_PDB_SHA256:
        raise ValidationError("request fixture is not the pinned RCSB 1UBQ structure")
    if value["num_seq_per_target"] != 1:
        raise ValidationError("request fixture must ask for exactly one designed sequence")
    return value


def _health_is_ready(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, dict) and value.get("status") == "ready"


def _wait_ready(base_url: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last_error = "not contacted"
    url = base_url.rstrip("/") + READY_ENDPOINT
    while time.monotonic() < deadline:
        try:
            with HTTP.open(url, timeout=min(2.0, timeout)) as response:
                raw = response.read(1024 * 1024)
                if response.status == 200 and _health_is_ready(json.loads(raw)):
                    return _now()
                last_error = f"unexpected health response HTTP {response.status}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise ValidationError(f"readiness timeout: {last_error}")


def _parse_mfasta(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, str) or not value.endswith("\n"):
        raise ValidationError("mfasta must be a newline-terminated string")
    records: list[tuple[str, str]] = []
    header: str | None = None
    sequence: list[str] = []
    for line in value.splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(sequence)))
            header = line[1:]
            sequence = []
        elif header is None:
            raise ValidationError("mfasta contains sequence before its first header")
        else:
            sequence.append(line.strip())
    if header is not None:
        records.append((header, "".join(sequence)))
    if len(records) != 2:
        raise ValidationError("mfasta must contain exactly input and one designed record")
    return records


def _validate_response(value: Any, seed: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"mfasta", "scores", "probs"}:
        raise ValidationError("response must contain exactly mfasta, scores, and probs")

    records = _parse_mfasta(value["mfasta"])
    input_header, input_sequence = records[0]
    design_header, design_sequence = records[1]
    if input_sequence != EXPECTED_INPUT_SEQUENCE:
        raise ValidationError("mfasta input record is not the 76-residue 1UBQ sequence")
    if f"seed={seed}" not in input_header or "designed_chains=['A']" not in input_header:
        raise ValidationError("mfasta input header does not bind the requested seed and chain")
    if "sample=1" not in design_header or "seq_recovery=" not in design_header:
        raise ValidationError("mfasta designed header is incomplete")
    if len(design_sequence) != len(EXPECTED_INPUT_SEQUENCE):
        raise ValidationError("designed sequence length does not match 1UBQ")
    if not set(design_sequence) <= CANONICAL_AMINO_ACIDS:
        raise ValidationError("designed sequence contains a non-canonical amino acid")

    scores = value["scores"]
    if not isinstance(scores, list) or len(scores) != 1:
        raise ValidationError("scores must contain exactly one value")
    score = _finite_number(scores[0], "scores[0]")
    if score < 0:
        raise ValidationError("scores[0] must be non-negative")

    probs = value["probs"]
    if not isinstance(probs, list) or len(probs) != 1 or not isinstance(probs[0], list):
        raise ValidationError("probs must contain exactly one residue matrix")
    if len(probs[0]) != len(EXPECTED_INPUT_SEQUENCE):
        raise ValidationError("probability matrix must have one row per 1UBQ residue")
    for row_index, row in enumerate(probs[0]):
        if not isinstance(row, list) or len(row) != 21:
            raise ValidationError(f"probability row {row_index} must have 21 values")
        numeric = [_finite_number(item, f"probs[0][{row_index}]") for item in row]
        if any(item < 0 or item > 1 for item in numeric):
            raise ValidationError(f"probability row {row_index} has an out-of-range value")
        if not math.isclose(sum(numeric), 1.0, rel_tol=0.0, abs_tol=1e-4):
            raise ValidationError(f"probability row {row_index} does not sum to one")

    return {
        "response_keys": sorted(value),
        "input_sequence_length": len(input_sequence),
        "designed_sequence_length": len(design_sequence),
        "designed_sequence_sha256": hashlib.sha256(design_sequence.encode()).hexdigest(),
        "score": score,
        "probability_shape": [1, len(probs[0]), 21],
        "requested_seed_in_header": True,
    }


def _post(
    base_url: str, payload: dict[str, Any], timeout: float
) -> tuple[bytes, float, str, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    request_started_at = _now()
    started_ns = time.monotonic_ns()
    try:
        with HTTP.open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            response_received_ns = time.monotonic_ns()
            response_received_at = _now()
            if response.status != 200:
                raise ValidationError(f"semantic request returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"semantic request returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ValidationError(f"semantic request failed: {type(exc).__name__}") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise ValidationError("semantic response exceeded 8 MiB")
    return (
        raw,
        round((response_received_ns - started_ns) / 1_000_000_000, 6),
        request_started_at,
        response_received_at,
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--request-file", type=Path, default=Path("/validator/1ubq-request.json"))
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--ready-timeout", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.base_url)
    if parsed.scheme != "http" or not parsed.netloc or parsed.query or parsed.fragment:
        parser.error("--base-url must be a plain HTTP origin")
    if len(args.run_id) != 2 or any(
        len(item) > 63 or not RUN_ID.fullmatch(item) for item in args.run_id
    ):
        parser.error("exactly two DNS-label --run-id values are required")
    if args.ready_timeout <= 0 or args.timeout <= 0:
        parser.error("timeouts must be positive")
    return args


def main() -> int:
    args = _arguments()
    started_at = _now()
    monotonic_started = time.monotonic()
    cases: list[dict[str, Any]] = []
    failure: str | None = None
    ready_at: str | None = None
    try:
        fixture = _read_fixture(args.request_file)
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, (run_id, seed) in enumerate(zip(args.run_id, (2370, 2371), strict=True), 1):
            payload = dict(fixture)
            payload["random_seed"] = seed
            raw, elapsed, request_started_at, response_received_at = _post(
                args.base_url, payload, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"semantic response {index} is not JSON") from exc
            invariant = _validate_response(decoded, seed)
            case = {
                "index": index,
                "input_id": run_id,
                "random_seed": seed,
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": elapsed,
                "request_started_at": request_started_at,
                "response_received_at": response_received_at,
                "response_bytes": len(raw),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "invariant": invariant,
            }
            (args.receipt_dir / f"case-{index}.json").write_text(
                json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            cases.append(case)
    except (ValidationError, OSError) as exc:
        failure = str(exc)

    validation_finished_at = _now()
    validation_total_elapsed_seconds = round(
        time.monotonic() - monotonic_started, 6
    )
    passed = len(cases)
    summary = {
        "schema_version": 1,
        "validator": "proteinmpnn-faststart-semantic-v1",
        "base_url": args.base_url.rstrip("/"),
        "endpoint": args.base_url.rstrip("/") + ENDPOINT,
        "inference_path": ENDPOINT,
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "request_count": 2,
        "response_timing_contract": RESPONSE_TIMING_CONTRACT,
        "ok": failure is None and passed == 2,
        "status": "PASS" if failure is None and passed == 2 else "FAIL",
        "passed_case_count": passed,
        "failed_case_count": 2 - passed,
        "exit_code": 0 if failure is None and passed == 2 else 1,
        "started_at": started_at,
        "ready_at": ready_at,
        "finished_at": validation_finished_at,
        "validation_finished_at": validation_finished_at,
        "total_elapsed_seconds": validation_total_elapsed_seconds,
        "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
        "cases": cases,
    }
    if failure is not None:
        summary["error"] = failure
    try:
        if args.receipt_dir.exists():
            (args.receipt_dir / "summary.json").write_text(
                json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")), flush=True)
    return int(summary["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
