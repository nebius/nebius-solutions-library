#!/usr/bin/env python3
"""Issue exactly two fixed Evo2 calls and validate their biological output.

The validator is deliberately independent of Kubernetes.  The production
probe reaches a run-scoped ClusterIP, disables environment proxies, rejects
redirects, polls readiness, and then performs one POST for each fixed request.
There is no inference retry loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener


READY_PATH = "/v1/health/ready"
INFERENCE_PATH = "/biology/arc/evo2/generate"
MAX_RESPONSE_BYTES = 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DNA20 = re.compile(r"^[ACGT]{20}$")

FIXED_CASES = (
    {
        "input_sequence": "ATCGATCGATCG",
        "expected_sequence": "ATCGATCGATCGATCGATCG",
        "random_seed": 2407001,
    },
    {
        "input_sequence": "GATTACAGATTACA",
        "expected_sequence": "GATTACAGATTACAGATTAC",
        "random_seed": 2407002,
    },
)


class SetupFailure(ValueError):
    """The invocation cannot prove the required workload contract."""


class SemanticFailure(ValueError):
    """A successful HTTP response is not a valid Evo2 result."""


class TransportFailure(RuntimeError):
    """The exact endpoint could not be reached or returned an invalid status."""


@dataclass(frozen=True)
class Probe:
    index: int
    run_id: str
    input_sequence: str
    expected_sequence: str
    random_seed: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def finite_nonnegative(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise SemanticFailure(f"{label} must be a finite nonnegative number")
    return float(value)


def validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SetupFailure("base URL must be a nonempty HTTP(S) origin")
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        raise SetupFailure("base URL contains an unsafe character")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise SetupFailure(f"invalid base URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SetupFailure("base URL must be an HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None:
        raise SetupFailure("base URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SetupFailure("base URL must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SetupFailure("base URL contains an invalid port") from exc
    host = parsed.hostname
    rendered_host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    return f"{parsed.scheme}://{rendered_host}" + (f":{port}" if port is not None else "")


def build_probes(run_ids: Sequence[str]) -> tuple[Probe, Probe]:
    if len(run_ids) != 2:
        raise SetupFailure("--run-id must be supplied exactly twice")
    if run_ids[0] == run_ids[1]:
        raise SetupFailure("the two run IDs must be distinct")
    probes: list[Probe] = []
    for index, (run_id, case) in enumerate(zip(run_ids, FIXED_CASES, strict=True), 1):
        if RUN_ID.fullmatch(run_id) is None:
            raise SetupFailure("run IDs use unsupported characters or length")
        payload = {
            "sequence": case["input_sequence"],
            "num_tokens": 20,
            "temperature": 0.7,
            "top_k": 1,
            "top_p": 0.0,
            "random_seed": case["random_seed"],
            "enable_logits": False,
            "enable_sampled_probs": False,
            "enable_elapsed_ms_per_token": True,
        }
        probes.append(
            Probe(
                index=index,
                run_id=run_id,
                input_sequence=str(case["input_sequence"]),
                expected_sequence=str(case["expected_sequence"]),
                random_seed=int(case["random_seed"]),
                payload=payload,
            )
        )
    return probes[0], probes[1]


def validate_response(value: Any, probe: Probe) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticFailure("Evo2 response must be a JSON object")
    if value.get("sequence") != probe.expected_sequence:
        raise SemanticFailure("Evo2 sequence does not match the pinned deterministic oracle")
    sequence = value["sequence"]
    if not isinstance(sequence, str) or DNA20.fullmatch(sequence) is None:
        raise SemanticFailure("Evo2 sequence must contain exactly 20 A/C/G/T tokens")
    if value.get("logits", object()) is not None:
        raise SemanticFailure("Evo2 returned logits although logits were disabled")
    if value.get("sampled_probs", object()) is not None:
        raise SemanticFailure("Evo2 returned sampled probabilities although they were disabled")
    elapsed_ms = finite_nonnegative(value.get("elapsed_ms"), "elapsed_ms")
    per_token = value.get("elapsed_ms_per_token")
    if not isinstance(per_token, list) or len(per_token) != 20:
        raise SemanticFailure("elapsed_ms_per_token must contain exactly 20 values")
    timings = [
        finite_nonnegative(item, f"elapsed_ms_per_token[{index}]")
        for index, item in enumerate(per_token)
    ]
    return {
        "input_sequence": probe.input_sequence,
        "output_sequence": sequence,
        "output_sha256": sha256(sequence.encode("ascii")),
        "random_seed": probe.random_seed,
        "token_count": 20,
        "elapsed_ms": elapsed_ms,
        "per_token_count": len(timings),
        "per_token_sum_ms": round(sum(timings), 6),
    }


def create_receipt_dir(path: Path) -> Path:
    requested = path.expanduser()
    if requested.name in {"", ".", ".."}:
        raise SetupFailure("receipt directory must name a new child")
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise SetupFailure(f"receipt parent is unavailable: {exc}") from exc
    receipt = parent / requested.name
    if os.path.lexists(receipt):
        raise SetupFailure(f"receipt directory already exists: {receipt}")
    try:
        os.mkdir(receipt, 0o700)
        os.chmod(receipt, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise SetupFailure(f"could not create receipt directory: {exc}") from exc
    if stat.S_IMODE(receipt.stat().st_mode) != 0o700:
        raise SetupFailure("receipt directory mode is not 0700")
    return receipt


def write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def read_bounded(stream: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = stream.read(min(64 * 1024, MAX_RESPONSE_BYTES - size + 1))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise TransportFailure(f"response exceeded {MAX_RESPONSE_BYTES} bytes")


def direct_opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}), RejectRedirects())


def request_http(
    opener: OpenerDirector,
    url: str,
    timeout: float,
    *,
    body: bytes | None = None,
    request_id: str | None = None,
) -> HttpResult:
    headers = {"Accept": "application/json", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    request = Request(url, data=body, method="POST" if body is not None else "GET", headers=headers)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        try:
            return HttpResult(
                status=int(exc.code),
                headers={key.lower(): item for key, item in exc.headers.items()},
                body=read_bounded(exc),
                final_url=str(exc.geturl()),
            )
        finally:
            exc.close()
    except (URLError, OSError, TimeoutError) as exc:
        raise TransportFailure(f"request failed: {type(exc).__name__}: {exc}") from exc
    try:
        with response:
            return HttpResult(
                status=int(response.status),
                headers={key.lower(): item for key, item in response.headers.items()},
                body=read_bounded(response),
                final_url=str(response.geturl()),
            )
    except (OSError, TimeoutError) as exc:
        raise TransportFailure(f"response failed: {type(exc).__name__}: {exc}") from exc


def readiness_is_ready(body: bytes) -> bool:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return value is True or (isinstance(value, dict) and value.get("status") == "ready")


def wait_until_ready(
    opener: OpenerDirector, origin: str, timeout: float, interval: float = 0.05
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise SetupFailure("ready timeout must be positive and finite")
    endpoint = origin + READY_PATH
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    deadline = time.monotonic() + timeout
    attempts = 0
    last_result = "no response"
    while time.monotonic() < deadline:
        attempts += 1
        remaining = max(0.001, deadline - time.monotonic())
        try:
            result = request_http(opener, endpoint, min(1.0, remaining))
        except TransportFailure as exc:
            last_result = str(exc)
        else:
            if result.final_url != endpoint or 300 <= result.status < 400:
                raise TransportFailure("readiness redirect rejected")
            if result.status == 200 and readiness_is_ready(result.body):
                return {
                    "status": "PASS",
                    "endpoint": endpoint,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "attempts": attempts,
                    "elapsed_seconds": round(
                        (time.monotonic_ns() - started_ns) / 1_000_000_000, 6
                    ),
                }
            last_result = f"HTTP {result.status} body_sha256={sha256(result.body)}"
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raise TransportFailure(
        f"readiness timed out after {attempts} attempts; last result: {last_result}"
    )


def run_probe(
    opener: OpenerDirector,
    origin: str,
    probe: Probe,
    timeout: float,
    receipt: Path,
) -> dict[str, Any]:
    endpoint = origin + INFERENCE_PATH
    request_body = json_bytes(probe.payload)
    write_private(receipt / f"request-{probe.index}.json", request_body)
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    result = request_http(
        opener, endpoint, timeout, body=request_body, request_id=probe.run_id
    )
    elapsed_seconds = (time.monotonic_ns() - started_ns) / 1_000_000_000
    write_private(receipt / f"response-{probe.index}.body", result.body)
    write_private(
        receipt / f"response-{probe.index}.metadata.json",
        json_bytes(
            {
                "elapsed_seconds": round(elapsed_seconds, 6),
                "final_url": result.final_url,
                "headers": result.headers,
                "status": result.status,
            }
        ),
    )
    if result.final_url != endpoint or 300 <= result.status < 400:
        raise TransportFailure(f"inference {probe.index} redirect rejected")
    if result.status != 200:
        raise TransportFailure(f"inference {probe.index} returned HTTP {result.status}")
    try:
        decoded = json.loads(result.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticFailure(f"inference {probe.index} returned invalid JSON") from exc
    invariant = validate_response(decoded, probe)
    return {
        "status": "PASS",
        "ok": True,
        "index": probe.index,
        "run_id": probe.run_id,
        "endpoint": endpoint,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(elapsed_seconds, 6),
        "http_status": result.status,
        "request_sha256": sha256(request_body),
        "response_sha256": sha256(result.body),
        "response_bytes": len(result.body),
        "invariant": invariant,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--ready-timeout", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        origin = validate_base_url(args.base_url)
        probes = build_probes(args.run_id)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise SetupFailure("inference timeout must be positive and finite")
        receipt = create_receipt_dir(args.receipt_dir)
        started_at = utc_now()
        started_ns = time.monotonic_ns()
        opener = direct_opener()
        ready = wait_until_ready(opener, origin, args.ready_timeout)
        cases = [run_probe(opener, origin, probe, args.timeout, receipt) for probe in probes]
        if cases[0]["invariant"]["output_sequence"] == cases[1]["invariant"]["output_sequence"]:
            raise SemanticFailure("the two distinct requests returned the same sequence")
        summary = {
            "schema": "archvteams.nebius.ai/evo2-semantic-probe/v1",
            "validator": "evo2-40b-strict-generate-v1",
            "status": "PASS",
            "ok": True,
            "base_url": origin,
            "ready": ready,
            "started_at": started_at,
            "finished_at": utc_now(),
            "total_elapsed_seconds": round(
                (time.monotonic_ns() - started_ns) / 1_000_000_000, 6
            ),
            "passed_case_count": 2,
            "failed_case_count": 0,
            "request_count": 2,
            "cases": cases,
            "proxy_policy": "disabled",
            "redirect_policy": "reject",
        }
        payload = json_bytes(summary)
        write_private(receipt / "summary.json", payload)
        sys.stdout.buffer.write(payload)
        return 0
    except (SetupFailure, SemanticFailure, TransportFailure, OSError) as exc:
        print(f"validate-evo2: FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
