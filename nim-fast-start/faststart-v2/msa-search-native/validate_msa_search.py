#!/usr/bin/env python3
"""Strict two-query semantic validator for the pinned MSA Search PDB70 NIM."""

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


ENDPOINT = "/biology/colabfold/msa-search/predict"
READY_ENDPOINT = "/v1/health/ready"
EXPECTED_FIXTURE_SHA256 = "874b0e5e3be9776ea289fb46444032e04b63875d9d4110f1560e5435de72686a"
DATABASE = "pdb70_220313"
QUERY_1 = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
QUERY_2 = "AQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
EXPECTED_RECORDS = 128
EXPECTED_NON_QUERY_HOMOLOGS = 127
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


def _read_fixture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValidationError("request fixture must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read request fixture: {type(exc).__name__}") from exc
    if hashlib.sha256(raw).hexdigest() != EXPECTED_FIXTURE_SHA256:
        raise ValidationError("request fixture digest is not the retained PDB70 request")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"request fixture is invalid: {type(exc).__name__}") from exc
    expected = {
        "sequence": QUERY_1,
        "databases": [DATABASE],
        "max_msa_sequences": 500,
        "output_alignment_formats": ["a3m"],
    }
    if value != expected:
        raise ValidationError("request fixture is not the frozen 76-residue PDB70 case")
    return value


def _request_for_case(template: dict[str, Any], query: str) -> dict[str, Any]:
    value = copy.deepcopy(template)
    value["sequence"] = query
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


def _parse_a3m(alignment: str) -> list[tuple[str, str]]:
    if not isinstance(alignment, str) or not alignment.lstrip().startswith(">"):
        raise ValidationError("PDB70 A3M alignment is empty or malformed")
    records: list[tuple[str, str]] = []
    header: str | None = None
    sequence: list[str] = []
    for raw_line in alignment.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(sequence)))
            header = line[1:]
            sequence = []
        else:
            if header is None:
                raise ValidationError("A3M sequence precedes its header")
            sequence.append(line)
    if header is not None:
        records.append((header, "".join(sequence)))
    return records


def _matched_sequence(value: str) -> str:
    return "".join(character for character in value if character.isupper())


def _validate_response(value: Any, query: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"metrics", "alignments"}:
        raise ValidationError("response must contain exactly metrics and alignments")
    metrics = value["metrics"]
    if not isinstance(metrics, dict) or metrics != {"search_type": "colabfold"}:
        raise ValidationError("metrics.search_type is not the exact colabfold receipt")
    alignments = value["alignments"]
    if not isinstance(alignments, dict) or set(alignments) != {DATABASE}:
        raise ValidationError("response must contain exactly the requested PDB70 database")
    formats = alignments[DATABASE]
    if not isinstance(formats, dict) or set(formats) != {"a3m"}:
        raise ValidationError("PDB70 response must contain exactly one A3M format")
    a3m = formats["a3m"]
    if not isinstance(a3m, dict) or set(a3m) != {"alignment", "format"}:
        raise ValidationError("A3M response has the wrong shape")
    if a3m["format"] != "a3m":
        raise ValidationError("alignment format is not a3m")
    records = _parse_a3m(a3m["alignment"])
    if len(records) != EXPECTED_RECORDS:
        raise ValidationError(f"PDB70 response must contain exactly {EXPECTED_RECORDS} A3M records")
    if _matched_sequence(records[0][1]) != query:
        raise ValidationError("first A3M record does not echo the requested 76-residue query")
    homologs = [_matched_sequence(sequence) for _, sequence in records[1:]]
    if len(homologs) != EXPECTED_NON_QUERY_HOMOLOGS or not all(homologs):
        raise ValidationError("PDB70 response must contain exactly 127 non-empty homologs")
    if len(query) != 76:
        raise ValidationError("frozen query length changed")
    return {
        "database": DATABASE,
        "search_type": "colabfold",
        "alignment_format": "a3m",
        "records": len(records),
        "non_query_homologs": len(homologs),
        "query_length": len(query),
        "query_echo": True,
        "query_sha256": hashlib.sha256(query.encode("ascii")).hexdigest(),
        "alignment_sha256": hashlib.sha256(a3m["alignment"].encode("utf-8")).hexdigest(),
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
        "--request-file", type=Path, default=Path("/validator/request-pdb70.json")
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
        template = _read_fixture(args.request_file)
        args.receipt_dir.mkdir(parents=True, exist_ok=False)
        ready_at = _wait_ready(args.base_url, args.ready_timeout)
        for index, (run_id, query) in enumerate(
            zip(args.run_id, (QUERY_1, QUERY_2), strict=True), 1
        ):
            payload = _request_for_case(template, query)
            raw, elapsed, request_started_at, response_received_at = _post(
                args.base_url, payload, args.timeout
            )
            response_path = args.receipt_dir / f"response-{index}.json"
            response_path.write_bytes(raw)
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"semantic response {index} is not JSON") from exc
            invariant = _validate_response(decoded, query)
            case = {
                "index": index,
                "input_id": run_id,
                "query_sha256": hashlib.sha256(query.encode("ascii")).hexdigest(),
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
        if cases[0]["query_sha256"] == cases[1]["query_sha256"]:
            raise ValidationError("semantic calls did not use distinct queries")
        if cases[0]["response_sha256"] == cases[1]["response_sha256"]:
            raise ValidationError("distinct queries returned an identical response")
    except (ValidationError, OSError) as exc:
        failure = str(exc)

    validation_finished_at = _now()
    validation_total_elapsed_seconds = round(
        time.monotonic() - monotonic_started, 6
    )
    passed = len(cases) if failure is None else 0
    summary = {
        "schema_version": 1,
        "validator": "msa-search-pdb70-faststart-semantic-v1",
        "base_url": args.base_url.rstrip("/"),
        "endpoint": args.base_url.rstrip("/") + ENDPOINT,
        "inference_path": ENDPOINT,
        "proxy_policy": "disabled",
        "redirect_policy": "reject",
        "request_count": 2,
        "response_timing_contract": RESPONSE_TIMING_CONTRACT,
        "ok": failure is None and len(cases) == 2,
        "status": "PASS" if failure is None and len(cases) == 2 else "FAIL",
        "passed_case_count": passed,
        "failed_case_count": 2 - passed,
        "exit_code": 0 if failure is None and len(cases) == 2 else 1,
        "queries_distinct": failure is None and len(cases) == 2,
        "expected_records_per_response": EXPECTED_RECORDS,
        "expected_non_query_homologs_per_response": EXPECTED_NON_QUERY_HOMOLOGS,
        "mmseqs_pipe_expectation": "separately-qualified-in-target",
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
