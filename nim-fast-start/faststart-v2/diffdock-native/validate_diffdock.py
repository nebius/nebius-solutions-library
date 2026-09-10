#!/usr/bin/env python3
"""Strict two-call semantic validator for the pinned DiffDock NIM."""

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


ENDPOINT = "/molecular-docking/diffdock/generate"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_FIXTURE_SHA256 = "f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977"
EXPECTED_PROTEIN_SHA256 = "d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161"
EXPECTED_PROTEIN_BYTES = 78_570
EXPECTED_LIGAND = "CC(=O)Oc1ccccc1C(=O)O"
EXPECTED_KEYS = {
    "details",
    "ligand",
    "ligand_positions",
    "position_confidence",
    "protein",
    "status",
    "trajectory",
}
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


# The probe must reach only the run-scoped ClusterIP origin supplied on the
# command line; ignore ambient HTTP(S)_PROXY variables and reject redirects.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


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
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read request fixture: {type(exc).__name__}") from exc
    if hashlib.sha256(raw).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValidationError("request fixture bytes do not match the retained strict-pass fixture")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or set(value) != {
        "ligand",
        "ligand_file_type",
        "protein",
        "num_poses",
        "time_divisions",
        "steps",
    }:
        raise ValidationError("request fixture has the wrong shape")
    protein = value["protein"]
    if (
        not isinstance(protein, str)
        or len(protein.encode("utf-8")) != EXPECTED_PROTEIN_BYTES
        or hashlib.sha256(protein.encode("utf-8")).hexdigest() != EXPECTED_PROTEIN_SHA256
        or "1UBQ" not in protein[:256]
    ):
        raise ValidationError("request fixture is not the pinned RCSB 1UBQ structure")
    if value != {
        "ligand": EXPECTED_LIGAND,
        "ligand_file_type": "txt",
        "protein": protein,
        "num_poses": 1,
        "time_divisions": 20,
        "steps": 18,
    }:
        raise ValidationError("request fixture does not request the pinned aspirin docking case")
    return value


def _health_is_ready(value: Any) -> bool:
    # DiffDock returns the JSON boolean true; the object form is retained for
    # the second successful BioNeMo readiness shape observed in this project.
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


def _validate_pose(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) <= 1_000:
        raise ValidationError("ligand pose must be nontrivial V2000 molfile text")
    lines = value.splitlines()
    if len(lines) < 8 or "V2000" not in lines[3] or "M  END" not in lines:
        raise ValidationError("ligand pose is not a complete V2000 molfile")
    try:
        atom_count = int(lines[3][0:3])
        bond_count = int(lines[3][3:6])
    except (ValueError, IndexError) as exc:
        raise ValidationError("ligand pose has an invalid V2000 counts line") from exc
    if atom_count != 13 or bond_count != 13:
        raise ValidationError("ligand pose is not the 13-heavy-atom aspirin graph")
    if len(lines) < 4 + atom_count + bond_count + 1:
        raise ValidationError("ligand pose is truncated")
    coordinates: list[tuple[float, float, float]] = []
    for index, line in enumerate(lines[4 : 4 + atom_count], 1):
        fields = line.split()
        if len(fields) < 4:
            raise ValidationError(f"ligand atom row {index} is malformed")
        try:
            coordinate = tuple(
                _finite_number(float(item), f"ligand atom {index} coordinate")
                for item in fields[:3]
            )
        except ValueError as exc:
            raise ValidationError(f"ligand atom row {index} has a nonnumeric coordinate") from exc
        if len(coordinate) != 3:  # pragma: no cover - fixed slice
            raise ValidationError(f"ligand atom row {index} is malformed")
        coordinates.append(coordinate)
    if len(set(coordinates)) < 10:
        raise ValidationError("ligand pose coordinates are degenerate")
    return {
        "format": "V2000",
        "atom_count": atom_count,
        "bond_count": bond_count,
        "finite_coordinate_count": len(coordinates),
        "pose_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _validate_response(value: Any, fixture: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != EXPECTED_KEYS:
        raise ValidationError("response has the wrong top-level shape")
    if value["status"] != "success":
        raise ValidationError("response status is not success")
    if not isinstance(value["details"], str) or not value["details"].startswith("success"):
        raise ValidationError("response details do not report success")
    if value["ligand"] != fixture["ligand"]:
        raise ValidationError("response ligand does not match submitted aspirin")
    protein = value["protein"]
    if (
        not isinstance(protein, str)
        or protein != fixture["protein"]
        or hashlib.sha256(protein.encode("utf-8")).hexdigest() != EXPECTED_PROTEIN_SHA256
    ):
        raise ValidationError("response protein does not match the full submitted 1UBQ receptor")

    poses = value["ligand_positions"]
    if not isinstance(poses, list) or len(poses) != 1:
        raise ValidationError("response must contain exactly one ligand pose")
    pose = _validate_pose(poses[0])

    confidences = value["position_confidence"]
    if not isinstance(confidences, list) or len(confidences) != 1:
        raise ValidationError("response must contain exactly one pose confidence")
    confidence = _finite_number(confidences[0], "position_confidence[0]")

    trajectory = value["trajectory"]
    if (
        not isinstance(trajectory, list)
        or len(trajectory) != 1
        or not isinstance(trajectory[0], str)
    ):
        raise ValidationError("response must contain exactly one trajectory string")
    return {
        "response_keys": sorted(value),
        "ligand": EXPECTED_LIGAND,
        "protein_sha256": EXPECTED_PROTEIN_SHA256,
        "protein_bytes": len(protein.encode("utf-8")),
        "pose": pose,
        "position_confidence": confidence,
        "trajectory_count": 1,
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
        "--request-file",
        type=Path,
        default=Path("/validator/1ubq-aspirin-request.json"),
    )
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--ready-timeout", type=float, default=300.0)
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
        fixture = _read_fixture(args.request_file)
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, run_id in enumerate(args.run_id, 1):
            raw, elapsed, request_started_at, response_received_at = _post(
                args.base_url, fixture, run_id, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"semantic response {index} is not JSON") from exc
            invariant = _validate_response(decoded, fixture)
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
        "validator": "diffdock-faststart-semantic-v1",
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
