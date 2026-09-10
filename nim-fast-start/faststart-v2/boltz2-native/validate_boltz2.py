#!/usr/bin/env python3
"""Submit and strictly validate two fixed Boltz2 structure predictions.

The program is deliberately Kubernetes-independent.  It talks directly to one
HTTP origin, disables environment proxies, rejects redirects, and writes the
exact request and response bytes plus a machine-readable summary to a private
receipt directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener


BOLTZ2_PATH = "/biology/mit/boltz2/predict"
READY_PATH = "/v1/health/ready"
FIXED_SEQUENCES = (
    ("A", "ACDEFGHIKLMNPQRSTVWY"),
    ("B", "YWVTSRQPNMLKIHGFEDCA"),
)
EXPECTED_BACKBONE = frozenset({"N", "CA", "C", "O"})
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"
SEMANTIC_SCHEMA_VERSION = 2
NODE_BOOTTIME_SCHEMA = "archvteams.nebius.ai/semantic-node-boottime/v1"

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


class SetupFailure(ValueError):
    pass


class SemanticFailure(ValueError):
    pass


class TransportFailure(RuntimeError):
    def __init__(self, message: str, partial_body: bytes = b"") -> None:
        super().__init__(message)
        self.partial_body = partial_body


@dataclass(frozen=True)
class Probe:
    index: int
    run_id: str
    chain_id: str
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
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                   allow_nan=False) + "\n"
    ).encode("ascii")


def finite_number(value: Any, label: str) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(float(value))):
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
    rendered_host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    return f"{scheme}://{rendered_host}" + (f":{port}" if port is not None else "")


def build_probes(run_ids: Sequence[str]) -> tuple[Probe, Probe]:
    if len(run_ids) != 2:
        raise SetupFailure("--run-id must be supplied exactly twice")
    if run_ids[0] == run_ids[1]:
        raise SetupFailure("the two run IDs must be unique")
    probes: list[Probe] = []
    for index, (run_id, (chain_id, sequence)) in enumerate(
        zip(run_ids, FIXED_SEQUENCES, strict=True), 1
    ):
        if RUN_ID.fullmatch(run_id) is None:
            raise SetupFailure(
                "run IDs must be 1-128 characters using letters, digits, '.', '_', ':', or '-'"
            )
        payload = {
            "diffusion_samples": 1,
            "output_format": "mmcif",
            "polymers": [{
                "id": chain_id,
                "molecule_type": "protein",
                "msa": {"msa_search": {"a3m": {
                    "alignment": f">query\n{sequence}",
                    "format": "a3m",
                    "rank": 0,
                }}},
                "sequence": sequence,
            }],
            "recycling_steps": 1,
            "sampling_steps": 10,
        }
        probes.append(Probe(index, run_id, chain_id, sequence, payload))
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
            raise TransportFailure(
                f"response read failed: {type(exc).__name__}: {exc}", b"".join(chunks)
            ) from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise TransportFailure(
                f"response exceeded {MAX_RESPONSE_BYTES} bytes", b"".join(chunks)
            )


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
    request_started_at = utc_now()
    request_started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        try:
            response_body = read_bounded(exc)
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            return HttpResult(
                int(exc.code), {key.lower(): value for key, value in exc.headers.items()},
                response_body, str(exc.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at, response_received_at,
                request_started_boottime_ns, response_received_boottime_ns,
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
            response_received_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            response_received_at = utc_now()
            return HttpResult(
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                response_body, str(response.geturl()),
                round(
                    (response_received_boottime_ns - request_started_boottime_ns)
                    / 1_000_000_000,
                    6,
                ),
                request_started_at, response_received_at,
                request_started_boottime_ns, response_received_boottime_ns,
            )
    except TransportFailure:
        raise
    except (OSError, TimeoutError) as exc:
        raise TransportFailure(f"response failed: {type(exc).__name__}: {exc}") from exc


def readiness_body_is_ready(body: bytes) -> bool:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return decoded is True or (isinstance(decoded, dict) and decoded.get("status") == "ready")


def wait_until_ready(
    opener: OpenerDirector, origin: str, timeout: float, *, interval: float = 0.05
) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise SetupFailure("ready timeout must be a positive finite number")
    endpoint = origin + READY_PATH
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    deadline = time.monotonic() + timeout
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
            result = request_http(opener, endpoint, min(1.0, remaining))
        except TransportFailure as exc:
            last_result = str(exc)
        else:
            if result.final_url != endpoint or 300 <= result.status < 400:
                raise TransportFailure("readiness redirect rejected")
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
        if interval > 0:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _atom_site_table(mmcif: str) -> tuple[list[str], list[list[str]]]:
    lines = mmcif.splitlines()
    required_headers = {
        "_atom_site.group_PDB", "_atom_site.label_atom_id", "_atom_site.label_comp_id",
        "_atom_site.label_seq_id", "_atom_site.label_asym_id", "_atom_site.Cartn_x",
        "_atom_site.Cartn_y", "_atom_site.Cartn_z", "_atom_site.B_iso_or_equiv",
        "_atom_site.pdbx_PDB_model_num",
    }
    for index, line in enumerate(lines):
        if line.strip() != "loop_":
            continue
        header_index = index + 1
        headers: list[str] = []
        while header_index < len(lines) and lines[header_index].startswith("_atom_site."):
            headers.append(lines[header_index].strip())
            header_index += 1
        if not headers:
            continue
        if not required_headers.issubset(headers):
            missing = ",".join(sorted(required_headers - set(headers)))
            raise SemanticFailure(f"Boltz2 mmCIF atom table lacks required column(s): {missing}")
        rows: list[list[str]] = []
        while header_index < len(lines):
            raw = lines[header_index].strip()
            if not raw or raw == "#" or raw == "loop_" or raw.startswith("_"):
                break
            try:
                fields = shlex.split(raw, posix=True)
            except ValueError as exc:
                raise SemanticFailure("Boltz2 mmCIF has an invalid atom row") from exc
            if len(fields) != len(headers):
                raise SemanticFailure("Boltz2 mmCIF atom row does not match its columns")
            rows.append(fields)
            header_index += 1
        if not rows:
            raise SemanticFailure("Boltz2 mmCIF atom table is empty")
        return headers, rows
    raise SemanticFailure("Boltz2 structure has no _atom_site loop")


def validate_mmcif(text: str, expected_sequence: str, expected_chain: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text or not text.startswith("data_"):
        raise SemanticFailure("Boltz2 structure is not nonempty mmCIF text")
    headers, rows = _atom_site_table(text)
    column = {name: index for index, name in enumerate(headers)}
    residues: dict[int, str] = {}
    backbone: dict[int, set[str]] = {}
    chains: set[str] = set()
    models: set[str] = set()
    coordinate_count = 0
    b_factor_count = 0
    atom_records = 0
    for row_number, row in enumerate(rows, 1):
        if row[column["_atom_site.group_PDB"]] != "ATOM":
            continue
        atom_records += 1
        try:
            sequence_id = int(row[column["_atom_site.label_seq_id"]])
        except ValueError as exc:
            raise SemanticFailure(f"Boltz2 mmCIF atom row {row_number} has invalid sequence ID") from exc
        residue_name = row[column["_atom_site.label_comp_id"]]
        if residue_name not in THREE_TO_ONE:
            raise SemanticFailure(f"Boltz2 mmCIF has unsupported residue {residue_name!r}")
        prior_name = residues.setdefault(sequence_id, residue_name)
        if prior_name != residue_name:
            raise SemanticFailure("Boltz2 mmCIF changes residue name within one residue")
        backbone.setdefault(sequence_id, set()).add(row[column["_atom_site.label_atom_id"]])
        chains.add(row[column["_atom_site.label_asym_id"]])
        models.add(row[column["_atom_site.pdbx_PDB_model_num"]])
        for axis in ("_atom_site.Cartn_x", "_atom_site.Cartn_y", "_atom_site.Cartn_z"):
            try:
                value = float(row[column[axis]])
            except ValueError as exc:
                raise SemanticFailure(
                    f"Boltz2 mmCIF atom row {row_number} has invalid {axis}"
                ) from exc
            finite_number(value, f"Boltz2 mmCIF atom row {row_number} {axis}")
            coordinate_count += 1
        try:
            b_factor = float(row[column["_atom_site.B_iso_or_equiv"]])
        except ValueError as exc:
            raise SemanticFailure(
                f"Boltz2 mmCIF atom row {row_number} has invalid B factor"
            ) from exc
        finite_number(b_factor, f"Boltz2 mmCIF atom row {row_number} B factor")
        b_factor_count += 1
    if atom_records == 0:
        raise SemanticFailure("Boltz2 mmCIF contains no ATOM records")
    if chains != {expected_chain}:
        raise SemanticFailure("Boltz2 mmCIF chain does not match the requested chain")
    if models != {"1"}:
        raise SemanticFailure("Boltz2 mmCIF must contain exactly model 1")
    expected_ids = list(range(1, len(expected_sequence) + 1))
    if sorted(residues) != expected_ids:
        raise SemanticFailure("Boltz2 mmCIF residue identifiers do not exactly cover the request")
    observed_sequence = "".join(THREE_TO_ONE[residues[index]] for index in expected_ids)
    if observed_sequence != expected_sequence:
        raise SemanticFailure("Boltz2 mmCIF residue sequence does not match the request")
    for residue_id in expected_ids:
        missing = EXPECTED_BACKBONE - backbone.get(residue_id, set())
        if missing:
            raise SemanticFailure(
                f"Boltz2 mmCIF residue {residue_id} lacks backbone atom(s): "
                + ",".join(sorted(missing))
            )
    return {
        "atom_record_count": atom_records,
        "backbone_residue_count": len(residues),
        "b_factor_count": b_factor_count,
        "chain": expected_chain,
        "finite_coordinate_count": coordinate_count,
        "format": "mmcif",
        "model_count": len(models),
        "sequence": observed_sequence,
    }


def validate_response(
    response: Mapping[str, Any], expected_sequence: str, expected_chain: str
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise SemanticFailure("Boltz2 response JSON is not an object")
    for key in ("detail", "error"):
        if response.get(key) not in (None, "", []):
            raise SemanticFailure(f"Boltz2 response contains {key}: {response.get(key)!r}")
    structures = response.get("structures")
    if not isinstance(structures, list) or len(structures) != 1:
        raise SemanticFailure("Boltz2 must return exactly one structure")
    item = structures[0]
    if not isinstance(item, dict) or item.get("format") != "mmcif":
        raise SemanticFailure("Boltz2 structure must be an mmCIF object")
    structure = item.get("structure")
    if not isinstance(structure, str):
        raise SemanticFailure("Boltz2 structure has no mmCIF text")
    scores: dict[str, float] = {}
    for key in ("confidence_scores", "ptm_scores"):
        values = response.get(key)
        if not isinstance(values, list) or len(values) != 1:
            raise SemanticFailure(f"Boltz2 {key} must contain exactly one value")
        score = finite_number(values[0], f"Boltz2 {key}[0]")
        if not 0.0 <= score <= 1.0:
            raise SemanticFailure(f"Boltz2 {key}[0] is outside [0,1]")
        scores[key] = score
    return {
        "confidence_score": scores["confidence_scores"],
        "mmcif": validate_mmcif(structure, expected_sequence, expected_chain),
        "ptm_score": scores["ptm_scores"],
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
    if not math.isfinite(timeout) or timeout <= 0:
        raise SetupFailure("timeout must be a positive finite number")
    receipt = create_receipt_dir(receipt_dir)
    endpoint = origin + BOLTZ2_PATH
    client = opener if opener is not None else direct_opener()
    started_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    summary: dict[str, Any] = {
        "base_url": origin,
        "cases": [],
        "endpoint": endpoint,
        "inference_path": BOLTZ2_PATH,
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
        "validator": "boltz2-faststart-semantic-v1",
        "node_clock": node_boottime_identity(),
    }
    if ready_timeout is not None:
        summary["ready_timeout_seconds"] = float(ready_timeout)
        try:
            summary["ready_wait"] = wait_until_ready(client, origin, float(ready_timeout))
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
            summary.update({
                "error": str(exc), "exit_code": 3, "failed_case_count": 0,
                "finished_at": validation_finished_at,
                "validation_finished_at": validation_finished_at,
                "validation_finished_boottime_ns": validation_finished_boottime_ns,
                "ok": False, "passed_case_count": 0,
                "status": "ERROR_READY",
                "total_elapsed_seconds": validation_total_elapsed_seconds,
                "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
            })
            write_private_json(receipt / "summary.json", summary)
            return summary
    response_hashes: list[str] = []
    for probe in probes:
        request_name = f"request-{probe.index:02d}.json"
        response_name = f"response-{probe.index:02d}.raw"
        request_body = json_bytes(probe.payload)
        write_private(receipt / request_name, request_body)
        case: dict[str, Any] = {
            "index": probe.index, "input_id": probe.run_id, "ok": False,
            "request_bytes": len(request_body), "request_file": request_name,
            "request_sha256": sha256(request_body), "response_file": response_name,
            "sequence": probe.sequence, "status": "RUNNING",
        }
        try:
            result = request_http(
                client, endpoint, float(timeout), body=request_body, request_id=probe.run_id
            )
        except TransportFailure as exc:
            write_private(receipt / response_name, exc.partial_body)
            case.update({
                "response_bytes": len(exc.partial_body),
                "response_sha256": sha256(exc.partial_body),
            })
            _case_failure(case, "ERROR_TRANSPORT", 3, str(exc))
        else:
            case.update({
                "elapsed_seconds": result.elapsed_seconds,
                "request_started_at": result.request_started_at,
                "request_dispatched_boottime_ns": result.request_dispatched_boottime_ns,
                "response_received_at": result.response_received_at,
                "response_body_received_boottime_ns": result.response_body_received_boottime_ns,
            })
            write_private(receipt / response_name, result.body)
            body_hash = sha256(result.body)
            case.update({
                "content_type": result.headers.get("content-type"),
                "final_url": result.final_url, "http_status": result.status,
                "response_bytes": len(result.body), "response_sha256": body_hash,
            })
            if result.final_url != endpoint or 300 <= result.status < 400:
                _case_failure(case, "ERROR_REDIRECT", 3, "redirect rejected")
            elif result.status != 200:
                _case_failure(case, "ERROR_HTTP", 4, f"expected HTTP 200, got {result.status}")
            elif result.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                _case_failure(case, "ERROR_HTTP", 4, "response is not application/json")
            else:
                try:
                    decoded = json.loads(result.body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    _case_failure(case, "ERROR_JSON", 5, f"invalid JSON response: {exc}")
                else:
                    try:
                        invariant = validate_response(decoded, probe.sequence, probe.chain_id)
                    except SemanticFailure as exc:
                        _case_failure(case, "ERROR_SEMANTIC", 6, str(exc))
                    else:
                        case.update({
                            "exit_code": 0, "invariant": invariant, "ok": True, "status": "PASS"
                        })
                        response_hashes.append(body_hash)
        summary["cases"].append(case)
    if len(response_hashes) == 2 and response_hashes[0] == response_hashes[1]:
        _case_failure(
            summary["cases"][1], "ERROR_SEMANTIC", 6,
            "the two distinct requests returned byte-identical response bodies",
        )
    failures = [case for case in summary["cases"] if not case["ok"]]
    validation_finished_at = utc_now()
    validation_finished_boottime_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    validation_total_elapsed_seconds = round(
        (validation_finished_boottime_ns - started_boottime_ns) / 1_000_000_000,
        6,
    )
    summary.update({
        "exit_code": max((int(case["exit_code"]) for case in failures), default=0),
        "failed_case_count": len(failures),
        "finished_at": validation_finished_at,
        "validation_finished_at": validation_finished_at,
        "validation_finished_boottime_ns": validation_finished_boottime_ns,
        "ok": not failures, "passed_case_count": 2 - len(failures),
        "responses_distinct": len(response_hashes) == 2 and len(set(response_hashes)) == 2,
        "status": "PASS" if not failures else "FAIL",
        "total_elapsed_seconds": validation_total_elapsed_seconds,
        "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
    })
    write_private_json(receipt / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--receipt-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True, action="append")
    parser.add_argument("--ready-timeout", type=float)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        summary = run_validation(
            base_url=args.base_url, receipt_dir=args.receipt_dir,
            run_ids=args.run_id, timeout=args.timeout, ready_timeout=args.ready_timeout,
        )
    except SetupFailure as exc:
        print(f"setup failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 7
    print(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return int(summary.get("exit_code", 7))


if __name__ == "__main__":
    raise SystemExit(main())
