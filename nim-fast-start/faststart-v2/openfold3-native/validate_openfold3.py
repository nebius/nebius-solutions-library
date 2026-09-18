#!/usr/bin/env python3
"""Strict two-call semantic validator for the pinned OpenFold3 NIM."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ENDPOINT = "/biology/openfold/openfold3/predict"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_FIXTURE_SHA256 = "09b30bf2132e3764f99d4f417b47713cd6350bd332fe3100cceb1be11589f8ae"
EXPECTED_TEMPLATE_ID = "openfold3-h100-faststart"
EXPECTED_SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"
EXPECTED_ALIGNMENT = ">query\nACDEFGHIKLMNPQRSTVWY"
SCORE_KEYS = (
    "confidence_score",
    "complex_plddt_score",
    "complex_pde_score",
    "ptm_score",
    "iptm_score",
)
RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ValidationError(ValueError):
    """The endpoint did not satisfy the frozen semantic contract."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


# Reach only the run-scoped ClusterIP origin supplied on the command line.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read request fixture: {type(exc).__name__}") from exc
    if hashlib.sha256(raw).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValidationError("request fixture bytes do not match the retained strict-pass fixture")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc

    expected = {
        "request_id": EXPECTED_TEMPLATE_ID,
        "inputs": [
            {
                "input_id": EXPECTED_TEMPLATE_ID,
                "output_format": "cif",
                "molecules": [
                    {
                        "type": "protein",
                        "id": "A",
                        "sequence": EXPECTED_SEQUENCE,
                        "diffusion_samples": 1,
                        "msa": {
                            "main": {
                                "a3m": {
                                    "alignment": EXPECTED_ALIGNMENT,
                                    "format": "a3m",
                                }
                            }
                        },
                    }
                ],
            }
        ],
    }
    if value != expected:
        raise ValidationError("request fixture is not the frozen 20-aa query-only-MSA case")
    return value


def _request_for_case(template: dict[str, Any], request_id: str) -> dict[str, Any]:
    value = copy.deepcopy(template)
    value["request_id"] = request_id
    value["inputs"][0]["input_id"] = request_id
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
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValidationError("readiness response exceeded 1 MiB")
                if response.status == 200 and _health_is_ready(json.loads(raw)):
                    return _now()
                last_error = f"unexpected health response HTTP {response.status}"
        except ValidationError:
            raise
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise ValidationError(f"readiness timeout: {last_error}")


def _validate_structure(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) <= 10_000:
        raise ValidationError("structure must be a nontrivial CIF document")
    if not value.startswith("data_"):
        raise ValidationError("structure does not begin with a CIF data block")
    for field in ("_atom_site.Cartn_x", "_atom_site.Cartn_y", "_atom_site.Cartn_z"):
        if field not in value:
            raise ValidationError(f"structure is missing {field}")
    atom_rows = sum(1 for line in value.splitlines() if line.startswith("ATOM "))
    if atom_rows < 100:
        raise ValidationError("structure has fewer than 100 atom-site rows")
    return {
        "format": "cif",
        "characters": len(value),
        "atom_rows": atom_rows,
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _validate_response(value: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"request_id", "outputs"}:
        raise ValidationError("response has the wrong top-level shape")
    if value["request_id"] != request_id:
        raise ValidationError("response request_id does not match this distinct call")
    outputs = value["outputs"]
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise ValidationError("response must contain exactly one output")
    output = outputs[0]
    if not isinstance(output, dict) or set(output) != {
        "input_id",
        "runtime_metrics",
        "structures_with_scores",
    }:
        raise ValidationError("response output has the wrong shape")
    if output["input_id"] != request_id:
        raise ValidationError("response input_id does not match this distinct call")
    if not isinstance(output["runtime_metrics"], dict):
        raise ValidationError("runtime_metrics must be an object")
    structures = output["structures_with_scores"]
    if not isinstance(structures, list) or len(structures) != 1:
        raise ValidationError("response must contain exactly one scored structure")
    result = structures[0]
    expected_keys = {
        "format",
        "name",
        "source",
        "structure",
        *SCORE_KEYS,
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        raise ValidationError("scored structure has the wrong shape")
    if result["format"] != "cif":
        raise ValidationError("scored structure format is not cif")
    if not isinstance(result["name"], str) or not result["name"].startswith(request_id):
        raise ValidationError("scored structure name is not bound to this call")
    if not isinstance(result["source"], str) or not result["source"]:
        raise ValidationError("scored structure source is empty")
    scores = {key: _finite_number(result[key], key) for key in SCORE_KEYS}
    return {
        "input_id": request_id,
        "structure": _validate_structure(result["structure"]),
        "scores": scores,
    }


def _post(
    base_url: str,
    payload: dict[str, Any],
    request_id: str,
    timeout: float,
) -> tuple[bytes, float, str, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + ENDPOINT,
        data=data,
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    request_started_at = _now()
    started_ns = time.monotonic_ns()
    try:
        with HTTP.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            response_received_ns = time.monotonic_ns()
            response_received_at = _now()
            if response.status != 200:
                raise ValidationError(f"semantic request returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"semantic request returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ValidationError(f"semantic request failed: {type(exc).__name__}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
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
    parser.add_argument(
        "--request-file", type=Path, default=Path("/validator/request-20aa.json")
    )
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--ready-timeout", type=float, default=600.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.base_url)
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        parser.error("--base-url must be a plain HTTP origin")
    if (
        len(args.run_id) != 2
        or len(set(args.run_id)) != 2
        or any(len(item) > 63 or not RUN_ID.fullmatch(item) for item in args.run_id)
    ):
        parser.error("exactly two distinct DNS-label --run-id values are required")
    if (
        isinstance(args.ready_timeout, bool)
        or isinstance(args.timeout, bool)
        or not math.isfinite(args.ready_timeout)
        or not math.isfinite(args.timeout)
        or args.ready_timeout <= 0
        or args.timeout <= 0
    ):
        parser.error("timeouts must be positive finite numbers")
    return args


def main() -> int:
    args = _arguments()
    started_at = _now()
    monotonic_started = time.monotonic()
    cases: list[dict[str, Any]] = []
    failure: str | None = None
    ready_at: str | None = None
    try:
        template = _read_fixture(args.request_file)
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, run_id in enumerate(args.run_id, 1):
            payload = _request_for_case(template, run_id)
            raw, elapsed, request_started_at, response_received_at = _post(
                args.base_url, payload, run_id, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"semantic response {index} is not JSON") from exc
            invariant = _validate_response(decoded, run_id)
            case = {
                "index": index,
                "input_id": run_id,
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
        "validator": "openfold3-faststart-semantic-v1",
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
