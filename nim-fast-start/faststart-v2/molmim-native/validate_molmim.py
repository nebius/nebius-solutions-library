#!/usr/bin/env python3
"""Strict exactly-two-call CMA-ES/QED validator for the pinned MolMIM NIM."""

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


ENDPOINT = "/generate"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_FIXTURE_SHA256 = "053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d"
FIXTURE_SCHEMA = "archvteams.nebius.ai/molmim-cmaes-qed-fixture/v1"
RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


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
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _reject_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON constant: {value}")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be finite")
    return result


def _expected_fixture() -> dict[str, Any]:
    return {
        "cases": [
            {
                "name": "caffeine",
                "payload": {
                    "algorithm": "CMA-ES",
                    "iterations": 1,
                    "min_similarity": 0.3,
                    "minimize": False,
                    "num_molecules": 1,
                    "particles": 2,
                    "property_name": "QED",
                    "smi": "CN1C=NC2=C1C(=O)N(C(=O)N2C)C",
                },
            },
            {
                "name": "aspirin",
                "payload": {
                    "algorithm": "CMA-ES",
                    "iterations": 1,
                    "min_similarity": 0.3,
                    "minimize": False,
                    "num_molecules": 1,
                    "particles": 2,
                    "property_name": "QED",
                    "smi": "CC(=O)Oc1ccccc1C(=O)O",
                },
            },
        ],
        "schema": FIXTURE_SCHEMA,
    }


def _read_fixture(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("request fixture must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read request fixture: {type(exc).__name__}") from exc
    if hashlib.sha256(raw).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValidationError("request fixture bytes do not match the retained strict-pass fixture")
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc
    if value != _expected_fixture():
        raise ValidationError("request fixture is not the frozen two-case CMA-ES/QED contract")
    return value["cases"]


def _rdkit_modules() -> tuple[Any, Any]:
    try:
        from rdkit import Chem  # type: ignore
        from rdkit.Chem import QED  # type: ignore
    except ImportError as exc:
        raise ValidationError("strict MolMIM oracle requires RDKit in the CPU probe image") from exc
    return Chem, QED


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
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        time.sleep(0.1)
    raise ValidationError(f"readiness timeout: {last_error}")


def _generated_candidates(value: Any) -> list[tuple[str, float | None]]:
    if not isinstance(value, dict):
        raise ValidationError("semantic response root must be an object")
    candidates: list[tuple[str, float | None]] = []
    generated = value.get("generated")
    if isinstance(generated, list):
        for item in generated:
            if isinstance(item, str):
                candidates.append((item, None))
            elif isinstance(item, dict):
                sample = item.get("sample", item.get("smiles"))
                score = (
                    _finite_number(item["score"], "MolMIM generated score")
                    if "score" in item
                    else None
                )
                if isinstance(sample, str):
                    candidates.append((sample, score))
    molecules = value.get("molecules")
    if isinstance(molecules, str):
        try:
            molecules = json.loads(molecules, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            raise ValidationError("MolMIM molecules string is not JSON") from exc
    if isinstance(molecules, list):
        for item in molecules:
            if isinstance(item, str):
                candidates.append((item, None))
            elif isinstance(item, dict):
                sample = item.get("sample", item.get("smiles"))
                score = (
                    _finite_number(item["score"], "MolMIM molecule score")
                    if "score" in item
                    else None
                )
                if isinstance(sample, str):
                    candidates.append((sample, score))
    return candidates


def _validate_response(value: Any) -> dict[str, Any]:
    candidates = _generated_candidates(value)
    deduplicated: dict[str, float | None] = {}
    for smiles, score in candidates:
        if smiles in deduplicated and deduplicated[smiles] not in (None, score):
            raise ValidationError("duplicate MolMIM molecule has conflicting scores")
        if smiles not in deduplicated or deduplicated[smiles] is None:
            deduplicated[smiles] = score
    if len(deduplicated) != 1:
        raise ValidationError(
            f"MolMIM must return exactly one unique generated molecule, got {len(deduplicated)}"
        )
    smiles, score = next(iter(deduplicated.items()))
    if not isinstance(smiles, str) or not smiles.strip():
        raise ValidationError("MolMIM generated molecule is not a nonempty SMILES string")
    if score is None:
        raise ValidationError("MolMIM QED response omitted its score")
    if not 0.0 <= score <= 1.0:
        raise ValidationError("MolMIM QED score is outside [0,1]")
    Chem, QED = _rdkit_modules()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None or molecule.GetNumAtoms() < 1:
        raise ValidationError("MolMIM generated SMILES does not parse as a molecule")
    reference = float(QED.qed(molecule))
    if not math.isfinite(reference) or abs(score - reference) > 0.02:
        raise ValidationError(
            f"MolMIM QED score {score} disagrees with RDKit {reference} by more than 0.02"
        )
    return {
        "generated_count": 1,
        "smiles": smiles,
        "atom_count": molecule.GetNumAtoms(),
        "score": score,
        "rdkit_qed": reference,
    }


def _post(
    base_url: str, payload: dict[str, Any], run_id: str, timeout: float
) -> tuple[bytes, float, str]:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Request-ID": run_id,
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with HTTP.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            response_received_at = _now()
            elapsed = round(time.monotonic() - started, 6)
            content_type = response.headers.get_content_type()
            if response.status != 200:
                raise ValidationError(f"semantic request returned HTTP {response.status}")
            if content_type != "application/json":
                raise ValidationError(f"semantic response content type is {content_type!r}")
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"semantic request returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ValidationError(f"semantic request failed: {type(exc).__name__}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValidationError("semantic response exceeded 16 MiB")
    return raw, elapsed, response_received_at


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--request-file", type=Path, default=Path("/validator/request-cmaes-qed.json")
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
        fixture_cases = _read_fixture(args.request_file)
        # Import RDKit before the demand probe starts polling. A missing oracle
        # dependency must never be mistaken for a slow target.
        _rdkit_modules()
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, (fixture_case, run_id) in enumerate(zip(fixture_cases, args.run_id), 1):
            payload = fixture_case["payload"]
            raw, elapsed, response_received_at = _post(
                args.base_url, payload, run_id, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw, parse_constant=_reject_constant)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"semantic response {index} is not JSON") from exc
            invariant = _validate_response(decoded)
            case = {
                "index": index,
                "input_id": fixture_case["name"],
                "run_id": run_id,
                "ok": True,
                "status": "PASS",
                "exit_code": 0,
                "elapsed_seconds": elapsed,
                "response_received_at": response_received_at,
                "request_sha256": hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
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
            raise ValidationError("two semantic requests are not distinct")
        if cases[0]["response_sha256"] == cases[1]["response_sha256"]:
            raise ValidationError("two distinct requests returned byte-identical responses")
        if cases[0]["invariant"]["smiles"] == cases[1]["invariant"]["smiles"]:
            raise ValidationError("MolMIM returned the same candidate for two distinct seeds")
    except (ValidationError, OSError) as exc:
        failure = str(exc)

    finished_at = _now()
    passed = len(cases)
    summary = {
        "schema_version": 1,
        "validator": "molmim-faststart-semantic-v1",
        "base_url": args.base_url.rstrip("/"),
        "endpoint": args.base_url.rstrip("/") + ENDPOINT,
        "inference_path": ENDPOINT,
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "ok": failure is None and passed == 2,
        "status": "PASS" if failure is None and passed == 2 else "FAIL",
        "passed_case_count": passed,
        "failed_case_count": 2 - passed,
        "exit_code": 0 if failure is None and passed == 2 else 1,
        "started_at": started_at,
        "ready_at": ready_at,
        "finished_at": finished_at,
        "validation_completed_at": finished_at,
        "total_elapsed_seconds": round(time.monotonic() - monotonic_started, 6),
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
