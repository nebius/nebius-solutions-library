#!/usr/bin/env python3
"""Strict two-call semantic validator for the pinned GenMol NIM."""

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ENDPOINT = "/generate"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_FIXTURE_SHA256 = "3065261de604f495a2fbae1e7fd92488546ee51f2729e5d40e9be5ee2c22f444"
RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
EXPECTED_CALLS = [
    {
        "name": "qed",
        "payload": {
            "noise": "1",
            "num_molecules": 1,
            "scoring": "QED",
            "smiles": "[*{20-30}]",
            "step_size": 1,
            "temperature": "1",
            "unique": False,
        },
    },
    {
        "name": "logp",
        "payload": {
            "noise": "1",
            "num_molecules": 1,
            "scoring": "LogP",
            "smiles": "[*{12-18}]",
            "step_size": 1,
            "temperature": "0.9",
            "unique": False,
        },
    },
]


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


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be finite")
    return result


def _rdkit_modules() -> tuple[Any, Any, Any]:
    try:
        from rdkit import Chem  # type: ignore
        from rdkit.Chem import Crippen, QED  # type: ignore
    except ImportError as exc:
        raise ValidationError("strict GenMol validation requires RDKit") from exc
    return Chem, Crippen, QED


def _read_fixture(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("request fixture must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read request fixture: {type(exc).__name__}") from exc
    if hashlib.sha256(raw).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValidationError("request fixture bytes do not match the retained QED/LogP contract")
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc
    if value != {"calls": EXPECTED_CALLS}:
        raise ValidationError("request fixture is not the frozen QED then LogP pair")
    return value["calls"]


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
                if response.status == 200 and _health_is_ready(
                    json.loads(raw, parse_constant=_reject_constant)
                ):
                    return _now()
                last_error = f"unexpected health response HTTP {response.status}"
        except ValidationError:
            raise
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise ValidationError(f"readiness timeout: {last_error}")


def _validate_response(value: Any, scoring: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("response root must be an object")
    if value.get("status") != "success":
        raise ValidationError("GenMol status is not success")
    molecules = value.get("molecules")
    if (
        not isinstance(molecules, list)
        or len(molecules) != 1
        or not isinstance(molecules[0], dict)
    ):
        raise ValidationError("GenMol must return exactly one molecule object")
    item = molecules[0]
    smiles = item.get("smiles")
    if not isinstance(smiles, str) or not smiles.strip():
        raise ValidationError("GenMol smiles must be a nonempty string")
    Chem, Crippen, QED = _rdkit_modules()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None or molecule.GetNumAtoms() < 1:
        raise ValidationError("GenMol smiles does not parse as a molecule")
    score = _finite_number(item.get("score"), "GenMol score")
    if scoring == "QED":
        if not 0.0 <= score <= 1.0:
            raise ValidationError("GenMol QED score is outside [0,1]")
        reference = float(QED.qed(molecule))
        tolerance = 0.02
    elif scoring == "LogP":
        reference = float(Crippen.MolLogP(molecule))
        tolerance = 0.05
    else:  # The fixture parser prevents this path.
        raise ValidationError("unsupported GenMol scoring contract")
    if not math.isfinite(reference) or abs(score - reference) > tolerance:
        raise ValidationError(
            f"GenMol {scoring} score disagrees with RDKit by more than {tolerance}"
        )
    return {
        "scoring": scoring,
        "smiles": smiles,
        "atom_count": int(molecule.GetNumAtoms()),
        "score": score,
        "rdkit_score": reference,
        "absolute_error": abs(score - reference),
        "tolerance": tolerance,
    }


def _post(
    base_url: str,
    payload: dict[str, Any],
    request_id: str,
    timeout: float,
) -> tuple[bytes, float, str, str, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + ENDPOINT,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        },
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
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"semantic request returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ValidationError(f"semantic request failed: {type(exc).__name__}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValidationError("semantic response exceeded 16 MiB")
    if content_type != "application/json":
        raise ValidationError("semantic response is not application/json")
    return (
        raw,
        round((response_received_ns - started_ns) / 1_000_000_000, 6),
        request_started_at,
        response_received_at,
        hashlib.sha256(data).hexdigest(),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--request-file",
        type=Path,
        default=Path("/validator/requests-qed-logp.json"),
    )
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--ready-timeout", type=float, default=600.0)
    parser.add_argument("--timeout", type=float, default=600.0)
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
        requests = _read_fixture(args.request_file)
        _rdkit_modules()  # Fail before issuing a semantic request if RDKit is absent.
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, (case_spec, request_id) in enumerate(
            zip(requests, args.run_id, strict=True), 1
        ):
            name = case_spec["name"]
            payload = case_spec["payload"]
            (args.receipt_dir / f"request-{index}-{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            raw, elapsed, request_started_at, response_received_at, request_sha256 = _post(
                args.base_url, payload, request_id, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}-{name}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw, parse_constant=_reject_constant)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValidationError(f"semantic response {index} is not finite JSON") from exc
            invariant = _validate_response(decoded, payload["scoring"])
            case = {
                "index": index,
                "input_id": request_id,
                "name": name,
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": elapsed,
                "request_started_at": request_started_at,
                "response_received_at": response_received_at,
                "request_sha256": request_sha256,
                "response_bytes": len(raw),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "invariant": invariant,
            }
            (args.receipt_dir / f"case-{index}.json").write_text(
                json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            cases.append(case)
        if cases[0]["request_sha256"] == cases[1]["request_sha256"]:
            raise ValidationError("QED and LogP requests were byte-identical")
        if cases[0]["response_sha256"] == cases[1]["response_sha256"]:
            raise ValidationError("QED and LogP responses were byte-identical")
    except (ValidationError, OSError) as exc:
        failure = str(exc)

    validation_finished_at = _now()
    validation_total_elapsed_seconds = round(
        time.monotonic() - monotonic_started, 6
    )
    passed = len(cases)
    summary = {
        "schema_version": 1,
        "validator": "genmol-faststart-semantic-v1",
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
