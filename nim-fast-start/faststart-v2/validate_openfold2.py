#!/usr/bin/env python3
"""Submit and strictly validate two deterministic OpenFold2 smoke requests.

The validator is intentionally independent of Kubernetes.  It talks only to the
HTTP(S) origin supplied with ``--base-url`` and creates a private evidence
receipt containing the exact request and response bytes for both probes.

Exit codes:
  0  both responses passed semantic validation
  2  invalid invocation or receipt setup failure
  3  transport failure or redirect
  4  non-200 HTTP response
  5  invalid JSON response
  6  OpenFold2 semantic validation failure
  7  unexpected internal/artifact failure
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)


OPENFOLD2_PATH = "/biology/openfold/openfold2/predict-structure-from-msa-and-template"
READY_PATH = "/v1/health/ready"

# Reversing the same 20-residue composition keeps the two probes comparable
# while making stale/cross-request response reuse detectable.
FIXED_SEQUENCES = (
    "ACDEFGHIKLMNPQRSTVWY",
    "YWVTSRQPNMLKIHGFEDCA",
)

EXPECTED_BACKBONE = frozenset({"N", "CA", "C", "O"})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"
SEMANTIC_SCHEMA_VERSION = 2
NODE_BOOTTIME_SCHEMA = "archvteams.nebius.ai/semantic-node-boottime/v1"

_PDB_THREE_TO_ONE = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "PHE": "F",
    "PRO": "P",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
}


class SetupFailure(ValueError):
    """The invocation cannot safely be executed."""


class SemanticFailure(ValueError):
    """A response does not prove the requested OpenFold2 inference."""


class TransportFailure(RuntimeError):
    """The request failed before a complete HTTP response was read."""

    def __init__(self, message: str, partial_body: bytes = b"") -> None:
        super().__init__(message)
        self.partial_body = partial_body


@dataclass(frozen=True)
class Probe:
    index: int
    run_id: str
    sequence: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str
    elapsed_seconds: float
    request_started_at: str
    response_received_at: str
    request_dispatched_boottime_ns: int
    response_body_received_boottime_ns: int


@dataclass(frozen=True)
class RawHttpResult:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    raw_bytes: int


class RejectRedirects(HTTPRedirectHandler):
    """Turn every HTTP redirect into an HTTPError without following it."""

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


def _timens_offsets() -> list[dict[str, int | str]]:
    try:
        lines = Path("/proc/self/timens_offsets").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupFailure(
            f"could not read the process time-namespace offsets: {type(exc).__name__}"
        ) from exc
    offsets: list[dict[str, int | str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 3 or fields[0] not in {"monotonic", "boottime"}:
            raise SetupFailure("the process time-namespace offsets are malformed")
        try:
            seconds, nanoseconds = int(fields[1]), int(fields[2])
        except ValueError as exc:
            raise SetupFailure("the process time-namespace offsets are malformed") from exc
        offsets.append(
            {"clock": fields[0], "seconds": seconds, "nanoseconds": nanoseconds}
        )
    if [item["clock"] for item in offsets] != ["monotonic", "boottime"]:
        raise SetupFailure("the process time-namespace offsets are incomplete")
    return offsets


def node_boottime_identity() -> dict[str, Any]:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
        resolution_ns = math.ceil(time.clock_getres(time.CLOCK_BOOTTIME) * 1_000_000_000)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SetupFailure(f"could not inspect CLOCK_BOOTTIME: {type(exc).__name__}") from exc
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        boot_id,
    ) is None or not 1 <= resolution_ns <= 1_000_000:
        raise SetupFailure("the node boot identity or CLOCK_BOOTTIME resolution is invalid")
    return {
        "schema": NODE_BOOTTIME_SCHEMA,
        "clock_id": "CLOCK_BOOTTIME",
        "boot_id": boot_id,
        "clock_resolution_ns": resolution_ns,
        "timens_offsets": _timens_offsets(),
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def finite_number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise SemanticFailure(f"{label} is not a finite number")
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
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
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
    if not host or any(character.isspace() for character in host):
        raise SetupFailure("base URL contains an invalid host")
    if ":" in host:
        rendered_host = f"[{host}]"
    else:
        try:
            rendered_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SetupFailure("base URL contains an invalid host") from exc
    origin = f"{scheme}://{rendered_host}"
    if port is not None:
        origin += f":{port}"
    return origin


def build_probes(run_ids: Sequence[str]) -> tuple[Probe, Probe]:
    if len(run_ids) != 2:
        raise SetupFailure("--run-id must be supplied exactly twice")
    normalized = tuple(run_ids)
    for value in normalized:
        if not isinstance(value, str) or RUN_ID.fullmatch(value) is None:
            raise SetupFailure(
                "run IDs must be 1-128 characters using letters, digits, '.', '_', ':', or '-'"
            )
    if normalized[0] == normalized[1]:
        raise SetupFailure("the two caller-supplied run IDs must be unique")
    if len(FIXED_SEQUENCES) != 2 or len(set(FIXED_SEQUENCES)) != 2:
        raise SetupFailure("the built-in probe sequences are not distinct")

    probes: list[Probe] = []
    for index, (run_id, sequence) in enumerate(zip(normalized, FIXED_SEQUENCES, strict=True), 1):
        if len(sequence) != 20:
            raise SetupFailure("the built-in probe sequence length is not 20")
        payload = {
            "input_id": run_id,
            "relax_prediction": False,
            "selected_models": [1],
            "sequence": sequence,
        }
        probes.append(Probe(index=index, run_id=run_id, sequence=sequence, payload=payload))
    return probes[0], probes[1]


def create_receipt_dir(path: Path) -> Path:
    requested = path.expanduser()
    if requested.name in {"", ".", ".."}:
        raise SetupFailure("receipt directory must name a new child directory")
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise SetupFailure(f"receipt parent is unavailable: {exc}") from exc
    if not parent.is_dir():
        raise SetupFailure("receipt parent is not a directory")
    receipt = parent / requested.name
    if os.path.lexists(receipt):
        raise SetupFailure(f"receipt directory already exists: {receipt}")
    try:
        os.mkdir(receipt, 0o700)
        os.chmod(receipt, 0o700, follow_symlinks=False)
    except OSError as exc:
        raise SetupFailure(f"could not create receipt directory: {exc}") from exc
    actual_mode = stat.S_IMODE(receipt.stat().st_mode)
    if actual_mode != 0o700:
        raise SetupFailure(f"receipt directory mode is {actual_mode:04o}, expected 0700")
    return receipt


def write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def write_private_json(path: Path, value: Any) -> None:
    write_private(path, json_bytes(value))


def read_bounded(stream: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = MAX_RESPONSE_BYTES - size
        try:
            chunk = stream.read(min(1024 * 1024, remaining + 1))
        except (OSError, TimeoutError) as exc:
            partial = b"".join(chunks)
            raise TransportFailure(
                f"response read failed: {type(exc).__name__}: {exc}", partial
            ) from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise TransportFailure(
                f"response exceeded {MAX_RESPONSE_BYTES} bytes", b"".join(chunks)
            )


def read_private_file(path: Path, maximum_bytes: int, label: str) -> bytes:
    """Read a caller-owned private regular file without following a symlink."""
    if path.is_symlink() or not path.is_file():
        raise SetupFailure(f"{label} is not a regular non-symlink file")
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetupFailure(f"could not stat {label}: {type(exc).__name__}") from exc
    if details.st_uid != os.geteuid():
        raise SetupFailure(f"{label} is not owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise SetupFailure(f"{label} must not be accessible by group or other users")
    if details.st_size > maximum_bytes:
        raise SetupFailure(f"{label} exceeds the {maximum_bytes}-byte limit")
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise SetupFailure(f"could not read {label}: {type(exc).__name__}") from exc
    if len(data) > maximum_bytes:
        raise SetupFailure(f"{label} exceeds the {maximum_bytes}-byte limit")
    return data


def parse_raw_http_response(raw: bytes) -> RawHttpResult:
    """Parse one complete, non-chunked HTTP/1.1 response fail-closed."""
    if not isinstance(raw, bytes) or not raw:
        raise TransportFailure("raw HTTP response is empty")
    header, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise TransportFailure("raw HTTP response has no CRLF header terminator")
    if len(header) > 64 * 1024:
        raise TransportFailure("raw HTTP response header exceeds 65536 bytes")
    lines = header.split(b"\r\n")
    if not lines or not re.fullmatch(rb"HTTP/1\.1 [0-9]{3}(?: [\x20-\x7e]*)?", lines[0]):
        raise TransportFailure("raw HTTP response has an invalid HTTP/1.1 status line")
    status = int(lines[0][9:12])
    reason = lines[0][13:].decode("ascii") if len(lines[0]) > 12 else ""

    multi_headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise TransportFailure("raw HTTP response contains a malformed header")
        raw_name, raw_value = line.split(b":", 1)
        if re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", raw_name) is None:
            raise TransportFailure("raw HTTP response contains an invalid header name")
        if any(byte < 0x20 and byte != 0x09 for byte in raw_value) or 0x7F in raw_value:
            raise TransportFailure("raw HTTP response contains an invalid header value")
        name = raw_name.decode("ascii").lower()
        value = raw_value.decode("latin-1").strip(" \t")
        multi_headers.setdefault(name, []).append(value)

    if "transfer-encoding" in multi_headers:
        raise TransportFailure("raw HTTP response uses forbidden Transfer-Encoding")
    lengths = multi_headers.get("content-length", [])
    if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
        raise TransportFailure("raw HTTP response needs exactly one decimal Content-Length")
    declared_length = int(lengths[0])
    if declared_length > MAX_RESPONSE_BYTES:
        raise TransportFailure(f"response exceeded {MAX_RESPONSE_BYTES} bytes", body)
    if len(body) != declared_length:
        raise TransportFailure(
            "raw HTTP response body length does not exactly match Content-Length", body
        )
    content_types = multi_headers.get("content-type", [])
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise TransportFailure("raw HTTP response is not exactly one application/json entity")
    if len(multi_headers.get("connection", [])) > 1:
        raise TransportFailure("raw HTTP response contains duplicate Connection headers")

    headers = {name: values[0] for name, values in multi_headers.items()}
    return RawHttpResult(
        status=status,
        reason=reason,
        headers=headers,
        body=body,
        raw_bytes=len(raw),
    )


def direct_opener() -> OpenerDirector:
    # Passing an explicit empty mapping is important: ProxyHandler() without an
    # argument consumes HTTP(S)_PROXY and related process environment variables.
    return build_opener(ProxyHandler({}), RejectRedirects())


def post_json(opener: OpenerDirector, url: str, body: bytes, timeout: float) -> HttpResult:
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Connection": "close",
            "Content-Type": "application/json",
        },
    )
    request_started_at = utc_now()
    request_started_ns = time.monotonic_ns()
    request_started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        try:
            response_body = read_bounded(exc)
            response_received_ns = time.monotonic_ns()
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            headers = {key.lower(): value for key, value in exc.headers.items()}
            return HttpResult(
                int(exc.code),
                headers,
                response_body,
                str(exc.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at,
                response_received_at,
                request_started_boottime_ns,
                response_received_boottime_ns,
            )
        finally:
            exc.close()
    except TransportFailure:
        raise
    except (URLError, OSError, TimeoutError) as exc:
        raise TransportFailure(f"request failed: {type(exc).__name__}: {exc}") from exc

    try:
        with response:
            response_body = read_bounded(response)
            response_received_ns = time.monotonic_ns()
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            return HttpResult(
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                response_body,
                str(response.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at,
                response_received_at,
                request_started_boottime_ns,
                response_received_boottime_ns,
            )
    except TransportFailure:
        raise
    except (OSError, TimeoutError) as exc:
        raise TransportFailure(f"response failed: {type(exc).__name__}: {exc}") from exc


def get_http(opener: OpenerDirector, url: str, timeout: float) -> HttpResult:
    request = Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "Connection": "close"},
    )
    request_started_at = utc_now()
    request_started_ns = time.monotonic_ns()
    request_started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        try:
            response_body = read_bounded(exc)
            response_received_ns = time.monotonic_ns()
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            headers = {key.lower(): value for key, value in exc.headers.items()}
            return HttpResult(
                int(exc.code),
                headers,
                response_body,
                str(exc.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at,
                response_received_at,
                request_started_boottime_ns,
                response_received_boottime_ns,
            )
        finally:
            exc.close()
    except TransportFailure:
        raise
    except (URLError, OSError, TimeoutError) as exc:
        raise TransportFailure(f"readiness request failed: {type(exc).__name__}: {exc}") from exc

    try:
        with response:
            response_body = read_bounded(response)
            response_received_ns = time.monotonic_ns()
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            return HttpResult(
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                response_body,
                str(response.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at,
                response_received_at,
                request_started_boottime_ns,
                response_received_boottime_ns,
            )
    except TransportFailure:
        raise
    except (OSError, TimeoutError) as exc:
        raise TransportFailure(
            f"readiness response failed: {type(exc).__name__}: {exc}"
        ) from exc


def readiness_body_is_ready(body: bytes) -> bool:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if decoded is True:
        return True
    return isinstance(decoded, dict) and decoded.get("status") == "ready"


def wait_until_ready(
    opener: OpenerDirector,
    origin: str,
    timeout: float,
    *,
    interval: float = 0.05,
) -> dict[str, Any]:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise SetupFailure("ready timeout must be a positive finite number")
    if (
        not isinstance(interval, (int, float))
        or isinstance(interval, bool)
        or not math.isfinite(float(interval))
        or interval < 0
    ):
        raise SetupFailure("ready polling interval must be a nonnegative finite number")

    endpoint = origin + READY_PATH
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    deadline = time.monotonic() + float(timeout)
    attempts = 0
    last_result = "no response"

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            elapsed = (time.monotonic_ns() - started_ns) / 1_000_000_000
            raise TransportFailure(
                f"readiness wait timed out after {elapsed:.3f}s and {attempts} attempts; "
                f"last result: {last_result}"
            )
        attempts += 1
        try:
            result = get_http(opener, endpoint, min(1.0, remaining))
        except TransportFailure as exc:
            last_result = str(exc)
        else:
            if result.final_url != endpoint:
                raise TransportFailure(
                    f"readiness client changed the request URL to {result.final_url}"
                )
            if 300 <= result.status < 400:
                raise TransportFailure(f"readiness redirect rejected with HTTP {result.status}")
            if result.status == 200 and readiness_body_is_ready(result.body):
                finished_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
                return {
                    "attempts": attempts,
                    "elapsed_seconds": round(
                        (finished_boottime_ns - started_boottime_ns)
                        / 1_000_000_000,
                        6,
                    ),
                    "endpoint": endpoint,
                    "finished_at": utc_now(),
                    "finished_boottime_ns": finished_boottime_ns,
                    "request_dispatched_boottime_ns": (
                        result.request_dispatched_boottime_ns
                    ),
                    "response_body_received_boottime_ns": (
                        result.response_body_received_boottime_ns
                    ),
                    "started_at": started_at,
                    "started_boottime_ns": started_boottime_ns,
                    "status": "PASS",
                }
            last_result = f"HTTP {result.status} body_sha256={sha256(result.body)}"
        remaining = deadline - time.monotonic()
        if remaining > 0 and interval > 0:
            time.sleep(min(float(interval), remaining))


def validate_pdb(text: str, expected_sequence: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text:
        raise SemanticFailure("OpenFold2 structure is not nonempty PDB text")

    residues: dict[tuple[str, str, str], str] = {}
    backbone: dict[tuple[str, str, str], set[str]] = {}
    atom_records = 0
    coordinate_count = 0
    model_records = 0

    for line_number, line in enumerate(text.splitlines(), 1):
        record = line[:6].strip()
        if record == "MODEL":
            model_records += 1
            continue
        if record not in {"ATOM", "HETATM"}:
            continue
        if len(line) < 54:
            raise SemanticFailure(f"OpenFold2 PDB atom line {line_number} is truncated")
        try:
            coordinates = (
                float(line[30:38]),
                float(line[38:46]),
                float(line[46:54]),
            )
        except ValueError as exc:
            raise SemanticFailure(
                f"OpenFold2 PDB atom line {line_number} has an unparseable coordinate"
            ) from exc
        if not all(math.isfinite(value) for value in coordinates):
            raise SemanticFailure(
                f"OpenFold2 PDB atom line {line_number} has a non-finite coordinate"
            )
        atom_records += 1
        coordinate_count += 3
        if record != "ATOM":
            continue

        sequence_id = line[22:26].strip()
        if not sequence_id:
            raise SemanticFailure(
                f"OpenFold2 PDB atom line {line_number} has no residue identifier"
            )
        key = (line[21:22], sequence_id, line[26:27])
        residue_name = line[17:20].strip()
        if residue_name not in _PDB_THREE_TO_ONE:
            raise SemanticFailure(f"OpenFold2 PDB has unsupported residue {residue_name!r}")
        prior_name = residues.setdefault(key, residue_name)
        if prior_name != residue_name:
            raise SemanticFailure("OpenFold2 PDB changes residue name within one residue")
        backbone.setdefault(key, set()).add(line[12:16].strip())

    if atom_records == 0 or not residues:
        raise SemanticFailure("OpenFold2 PDB contains no atom records")
    if model_records > 1:
        raise SemanticFailure("OpenFold2 PDB contains more than one MODEL")
    if len({key[0] for key in residues}) != 1:
        raise SemanticFailure("OpenFold2 PDB splits the requested monomer across chains")

    observed_sequence = "".join(_PDB_THREE_TO_ONE[name] for name in residues.values())
    if observed_sequence != expected_sequence:
        raise SemanticFailure(
            "OpenFold2 PDB residue sequence does not exactly match the requested sequence"
        )
    if len(residues) != len(expected_sequence):
        raise SemanticFailure("OpenFold2 PDB residue count does not match the request")
    for residue_index, key in enumerate(residues, 1):
        missing = EXPECTED_BACKBONE - backbone.get(key, set())
        if missing:
            rendered = ",".join(sorted(missing))
            raise SemanticFailure(
                f"OpenFold2 PDB residue {residue_index} lacks backbone atom(s): {rendered}"
            )

    return {
        "atom_record_count": atom_records,
        "backbone_residue_count": len(residues),
        "chain_count": 1,
        "finite_coordinate_count": coordinate_count,
        "format": "pdb",
        "model_record_count": model_records,
        "sequence": observed_sequence,
    }


def validate_response(
    response: Mapping[str, Any], expected_input_id: str, expected_sequence: str
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise SemanticFailure("OpenFold2 response JSON is not an object")
    if response.get("input_id") != expected_input_id:
        raise SemanticFailure("OpenFold2 input_id does not match the caller-supplied run ID")
    for key in ("detail", "error"):
        if response.get(key) not in (None, "", []):
            raise SemanticFailure(f"OpenFold2 response contains {key}: {response.get(key)!r}")
    for key in ("of2_nim_handled_error_message", "handled_error"):
        if response.get(key) not in (None, "", "no-handled-error"):
            raise SemanticFailure(f"OpenFold2 reported a handled error: {response.get(key)!r}")

    structures = response.get("structures_in_ranked_order")
    if not isinstance(structures, list) or len(structures) != 1:
        raise SemanticFailure("OpenFold2 must return exactly one selected model structure")
    item = structures[0]
    if not isinstance(item, dict):
        raise SemanticFailure("OpenFold2 structure item is not an object")
    if item.get("format") != "pdb" or not isinstance(item.get("structure"), str):
        raise SemanticFailure("OpenFold2 structure item must contain PDB text")
    if item.get("relaxed") is not False:
        raise SemanticFailure("OpenFold2 response is not the requested no-relax prediction")
    if type(item.get("model_param_set")) is not int or item.get("model_param_set") != 1:
        raise SemanticFailure("OpenFold2 response is not the requested model parameter set 1")

    confidence = finite_number(item.get("confidence"), "OpenFold2 confidence")
    if not 0.0 <= confidence <= 100.0:
        raise SemanticFailure("OpenFold2 confidence is outside [0,100]")

    plddt = item.get("plddt")
    if not isinstance(plddt, list) or len(plddt) != 20:
        raise SemanticFailure("OpenFold2 pLDDT must contain exactly 20 values")
    for index, value in enumerate(plddt):
        score = finite_number(value, f"OpenFold2 pLDDT[{index}]")
        if not 0.0 <= score <= 100.0:
            raise SemanticFailure(f"OpenFold2 pLDDT[{index}] is outside [0,100]")

    pae = item.get("predicted_aligned_error")
    if not isinstance(pae, list) or len(pae) != 20:
        raise SemanticFailure("OpenFold2 PAE must contain exactly 20 rows")
    for row_index, row in enumerate(pae):
        if not isinstance(row, list) or len(row) != 20:
            raise SemanticFailure(f"OpenFold2 PAE row {row_index} must contain exactly 20 values")
        for column_index, value in enumerate(row):
            score = finite_number(value, f"OpenFold2 PAE[{row_index}][{column_index}]")
            if score < 0.0:
                raise SemanticFailure(
                    f"OpenFold2 PAE[{row_index}][{column_index}] is negative"
                )

    ptm_score = finite_number(item.get("ptm_score"), "OpenFold2 pTM score")
    if not 0.0 <= ptm_score <= 1.0:
        raise SemanticFailure("OpenFold2 pTM score is outside [0,1]")

    pdb = validate_pdb(item["structure"], expected_sequence)
    return {
        "confidence": confidence,
        "handled_error_absent": True,
        "pae_shape": [20, 20],
        "pdb": pdb,
        "plddt_count": 20,
        "ptm_score": ptm_score,
    }


def _case_failure(case: dict[str, Any], status: str, exit_code: int, error: str) -> None:
    case.update({"error": error, "exit_code": exit_code, "ok": False, "status": status})


def run_validation(
    *,
    base_url: str,
    receipt_dir: Path,
    run_ids: Sequence[str],
    timeout: float = 300.0,
    ready_timeout: float | None = None,
    opener: OpenerDirector | None = None,
) -> dict[str, Any]:
    origin = validate_base_url(base_url)
    probes = build_probes(run_ids)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise SetupFailure("timeout must be a positive finite number")

    receipt = create_receipt_dir(receipt_dir)
    endpoint = origin + OPENFOLD2_PATH
    client = opener if opener is not None else direct_opener()
    started_ns = time.monotonic_ns()
    started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    summary: dict[str, Any] = {
        "base_url": origin,
        "cases": [],
        "endpoint": endpoint,
        "inference_path": OPENFOLD2_PATH,
        "ok": False,
        "proxy_policy": "disabled",
        "receipt_dir": str(receipt),
        "redirect_policy": "reject",
        "request_count": 2,
        "response_timing_contract": RESPONSE_TIMING_CONTRACT,
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "started_at": utc_now(),
        "started_boottime_ns": started_boottime_ns,
        "status": "RUNNING",
        "timeout_seconds": float(timeout),
        "validator": "openfold2-faststart-semantic-v1",
        "node_clock": node_boottime_identity(),
    }

    if ready_timeout is not None:
        summary["ready_timeout_seconds"] = float(ready_timeout)
        try:
            summary["ready_wait"] = wait_until_ready(
                client, origin, float(ready_timeout)
            )
        except TransportFailure as exc:
            validation_finished_at = utc_now()
            validation_finished_boottime_ns = time.clock_gettime_ns(
                time.CLOCK_BOOTTIME
            )
            validation_total_elapsed_seconds = round(
                (validation_finished_boottime_ns - started_boottime_ns)
                / 1_000_000_000,
                6,
            )
            summary.update(
                {
                    "error": str(exc),
                    "exit_code": 3,
                    "failed_case_count": 0,
                    "finished_at": validation_finished_at,
                    "validation_finished_at": validation_finished_at,
                    "validation_finished_boottime_ns": (
                        validation_finished_boottime_ns
                    ),
                    "ok": False,
                    "passed_case_count": 0,
                    "status": "ERROR_READY",
                    "total_elapsed_seconds": validation_total_elapsed_seconds,
                    "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
                }
            )
            write_private_json(receipt / "summary.json", summary)
            return summary

    for probe in probes:
        request_name = f"request-{probe.index:02d}.json"
        response_name = f"response-{probe.index:02d}.raw"
        request_body = json_bytes(probe.payload)
        write_private(receipt / request_name, request_body)
        case: dict[str, Any] = {
            "index": probe.index,
            "input_id": probe.run_id,
            "ok": False,
            "request_bytes": len(request_body),
            "request_file": request_name,
            "request_sha256": sha256(request_body),
            "response_file": response_name,
            "sequence": probe.sequence,
            "status": "RUNNING",
        }
        try:
            result = post_json(client, endpoint, request_body, float(timeout))
        except TransportFailure as exc:
            response_body = exc.partial_body
            write_private(receipt / response_name, response_body)
            case.update(
                {
                    "response_bytes": len(response_body),
                    "response_sha256": sha256(response_body),
                }
            )
            _case_failure(case, "ERROR_TRANSPORT", 3, str(exc))
        else:
            case.update(
                {
                    "elapsed_seconds": result.elapsed_seconds,
                    "request_started_at": result.request_started_at,
                    "request_dispatched_boottime_ns": (
                        result.request_dispatched_boottime_ns
                    ),
                    "response_received_at": result.response_received_at,
                    "response_body_received_boottime_ns": (
                        result.response_body_received_boottime_ns
                    ),
                }
            )
            write_private(receipt / response_name, result.body)
            case.update(
                {
                    "content_type": result.headers.get("content-type"),
                    "final_url": result.final_url,
                    "http_status": result.status,
                    "response_bytes": len(result.body),
                    "response_sha256": sha256(result.body),
                }
            )
            if result.final_url != endpoint:
                _case_failure(
                    case,
                    "ERROR_REDIRECT",
                    3,
                    f"HTTP client changed the request URL to {result.final_url}",
                )
            elif 300 <= result.status < 400:
                location = result.headers.get("location")
                if location is not None:
                    case["redirect_location"] = location
                _case_failure(
                    case,
                    "ERROR_REDIRECT",
                    3,
                    f"redirect rejected with HTTP {result.status}",
                )
            elif result.status != 200:
                _case_failure(
                    case,
                    "ERROR_HTTP",
                    4,
                    f"expected HTTP 200, got {result.status}",
                )
            else:
                try:
                    decoded = json.loads(result.body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    _case_failure(case, "ERROR_JSON", 5, f"invalid JSON response: {exc}")
                else:
                    if not isinstance(decoded, dict):
                        _case_failure(case, "ERROR_JSON", 5, "response JSON is not an object")
                    else:
                        try:
                            invariant = validate_response(
                                decoded, probe.run_id, probe.sequence
                            )
                        except SemanticFailure as exc:
                            _case_failure(case, "ERROR_SEMANTIC", 6, str(exc))
                        else:
                            case.update(
                                {
                                    "exit_code": 0,
                                    "invariant": invariant,
                                    "ok": True,
                                    "status": "PASS",
                                }
                            )
        summary["cases"].append(case)

    failures = [case for case in summary["cases"] if not case["ok"]]
    exit_code = max((int(case["exit_code"]) for case in failures), default=0)
    validation_finished_at = utc_now()
    validation_finished_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    validation_total_elapsed_seconds = round(
        (validation_finished_boottime_ns - started_boottime_ns) / 1_000_000_000,
        6,
    )
    summary.update(
        {
            "exit_code": exit_code,
            "failed_case_count": len(failures),
            "finished_at": validation_finished_at,
            "validation_finished_at": validation_finished_at,
            "validation_finished_boottime_ns": validation_finished_boottime_ns,
            "ok": not failures,
            "passed_case_count": len(summary["cases"]) - len(failures),
            "status": "PASS" if not failures else "FAIL",
            "total_elapsed_seconds": validation_total_elapsed_seconds,
            "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
        }
    )
    write_private_json(receipt / "summary.json", summary)
    return summary


def run_offline_validation(
    *,
    base_url: str,
    request_paths: Sequence[Path],
    response_paths: Sequence[Path],
    run_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate two captured requests and complete raw HTTP responses.

    Offline mode performs no network I/O and creates no files.  Requiring the
    complete HTTP/1.1 response, rather than a pre-extracted body, preserves the
    online validator's status, redirect, framing, size, and media-type gates.
    """
    origin = validate_base_url(base_url)
    probes = build_probes(run_ids)
    if len(request_paths) != 2 or len(response_paths) != 2:
        raise SetupFailure(
            "offline mode requires exactly two --offline-request and two --offline-response files"
        )

    started_ns = time.monotonic_ns()
    endpoint = origin + OPENFOLD2_PATH
    summary: dict[str, Any] = {
        "base_url": origin,
        "cases": [],
        "endpoint": endpoint,
        "inference_path": OPENFOLD2_PATH,
        "mode": "offline-raw-http",
        "network_io": False,
        "ok": False,
        "redirect_policy": "reject-non-200-without-following",
        "schema_version": 1,
        "started_at": utc_now(),
        "status": "RUNNING",
        "transport_contract": "single-complete-http/1.1-content-length-response",
        "validator": "openfold2-faststart-semantic-v1",
    }
    for probe, request_path, response_path in zip(
        probes, request_paths, response_paths, strict=True
    ):
        case_started_ns = time.monotonic_ns()
        case: dict[str, Any] = {
            "index": probe.index,
            "input_id": probe.run_id,
            "ok": False,
            "request_file": str(request_path),
            "response_file": str(response_path),
            "sequence": probe.sequence,
            "status": "RUNNING",
        }
        try:
            request_body = read_private_file(request_path, 64 * 1024, "offline request")
            case.update(
                {
                    "request_bytes": len(request_body),
                    "request_sha256": sha256(request_body),
                }
            )
            try:
                decoded_request = json.loads(request_body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SemanticFailure(f"captured request is invalid JSON: {exc}") from exc
            if decoded_request != probe.payload:
                raise SemanticFailure(
                    "captured request does not exactly match the fixed probe payload"
                )

            raw = read_private_file(
                response_path,
                MAX_RESPONSE_BYTES + (64 * 1024) + 4,
                "offline raw HTTP response",
            )
            case.update(
                {
                    "raw_response_bytes": len(raw),
                    "raw_response_sha256": sha256(raw),
                }
            )
            try:
                result = parse_raw_http_response(raw)
            except TransportFailure as exc:
                _case_failure(case, "ERROR_TRANSPORT", 3, str(exc))
            else:
                case.update(
                    {
                        "content_type": result.headers["content-type"],
                        "http_reason": result.reason,
                        "http_status": result.status,
                        "response_bytes": len(result.body),
                        "response_sha256": sha256(result.body),
                    }
                )
                if 300 <= result.status < 400:
                    _case_failure(
                        case,
                        "ERROR_REDIRECT",
                        3,
                        f"captured redirect rejected with HTTP {result.status}",
                    )
                elif result.status != 200:
                    _case_failure(
                        case,
                        "ERROR_HTTP",
                        4,
                        f"expected HTTP 200, got {result.status}",
                    )
                else:
                    try:
                        decoded_response = json.loads(result.body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        _case_failure(case, "ERROR_JSON", 5, f"invalid JSON response: {exc}")
                    else:
                        if not isinstance(decoded_response, dict):
                            _case_failure(
                                case, "ERROR_JSON", 5, "response JSON is not an object"
                            )
                        else:
                            try:
                                invariant = validate_response(
                                    decoded_response, probe.run_id, probe.sequence
                                )
                            except SemanticFailure as exc:
                                _case_failure(case, "ERROR_SEMANTIC", 6, str(exc))
                            else:
                                case.update(
                                    {
                                        "exit_code": 0,
                                        "invariant": invariant,
                                        "ok": True,
                                        "status": "PASS",
                                    }
                                )
        except SetupFailure as exc:
            _case_failure(case, "ERROR_SETUP", 2, str(exc))
        except SemanticFailure as exc:
            _case_failure(case, "ERROR_SEMANTIC", 6, str(exc))
        case["validation_elapsed_seconds"] = round(
            (time.monotonic_ns() - case_started_ns) / 1_000_000_000, 6
        )
        summary["cases"].append(case)

    failures = [case for case in summary["cases"] if not case["ok"]]
    exit_code = max((int(case["exit_code"]) for case in failures), default=0)
    validation_finished_at = utc_now()
    validation_total_elapsed_seconds = round(
        (time.monotonic_ns() - started_ns) / 1_000_000_000, 6
    )
    summary.update(
        {
            "exit_code": exit_code,
            "failed_case_count": len(failures),
            "finished_at": validation_finished_at,
            "validation_finished_at": validation_finished_at,
            "ok": not failures,
            "passed_case_count": len(summary["cases"]) - len(failures),
            "status": "PASS" if not failures else "FAIL",
            "total_elapsed_seconds": validation_total_elapsed_seconds,
            "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
        }
    )
    return summary


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--base-url",
        help="OpenFold2 HTTP(S) origin only; no path, query, or credentials",
    )
    argument_parser.add_argument(
        "--receipt-dir",
        type=Path,
        help="new directory to create with mode 0700",
    )
    argument_parser.add_argument(
        "--run-id",
        action="append",
        dest="run_ids",
        required=True,
        metavar="ID",
        help="caller-supplied OpenFold2 input_id; repeat exactly twice with unique values",
    )
    argument_parser.add_argument(
        "--offline-request",
        action="append",
        dest="offline_requests",
        type=Path,
        metavar="FILE",
        help="captured private request JSON; repeat exactly twice for offline mode",
    )
    argument_parser.add_argument(
        "--offline-response",
        action="append",
        dest="offline_responses",
        type=Path,
        metavar="FILE",
        help="captured complete raw HTTP/1.1 response; repeat exactly twice for offline mode",
    )
    argument_parser.add_argument("--timeout", type=float, default=300.0)
    argument_parser.add_argument(
        "--ready-timeout",
        type=float,
        help="wait this many seconds for exact GET /v1/health/ready=true before inference",
    )
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.base_url is None:
            raise SetupFailure("--base-url is required")
        offline_requested = args.offline_requests is not None or args.offline_responses is not None
        if offline_requested:
            if args.receipt_dir is not None:
                raise SetupFailure("--receipt-dir cannot be used in offline mode")
            summary = run_offline_validation(
                base_url=args.base_url,
                request_paths=args.offline_requests or (),
                response_paths=args.offline_responses or (),
                run_ids=args.run_ids,
            )
        else:
            if args.receipt_dir is None:
                raise SetupFailure("--receipt-dir is required in online mode")
            summary = run_validation(
                base_url=args.base_url,
                receipt_dir=args.receipt_dir,
                run_ids=args.run_ids,
                timeout=args.timeout,
                ready_timeout=args.ready_timeout,
            )
    except SetupFailure as exc:
        summary = {
            "error": str(exc),
            "exit_code": 2,
            "ok": False,
            "status": "ERROR_SETUP",
        }
    except Exception as exc:  # pragma: no cover - last-resort CLI reporting.
        summary = {
            "error": f"{type(exc).__name__}: {exc}",
            "exit_code": 7,
            "ok": False,
            "status": "ERROR_INTERNAL",
        }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return int(summary["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
