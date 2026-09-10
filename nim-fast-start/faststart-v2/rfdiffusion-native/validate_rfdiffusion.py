#!/usr/bin/env python3
"""Issue exactly two fixed RFdiffusion calls and validate their biological output.

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
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener


READY_PATH = "/v1/health/ready"
INFERENCE_PATH = "/biology/ipd/rfdiffusion/generate"
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FIXTURE_SHA256 = "d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161"
CONTIGS = "A20-60/0 20-30"
DIFFUSION_STEPS = 15
RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"

FIXED_CASES = (
    {
        "random_seed": 2370,
        "request_sha256": "da696caf8aba3511e63df5a293622e91b4c063f1593c60038bedca16d4865b2d",
    },
    {
        "random_seed": 2371,
        "request_sha256": "8fa20730e48a66c62fc5d095b4d26afac00cf7c4768e59300b95e447bc200c3c",
    },
)


class SetupFailure(ValueError):
    """The invocation cannot prove the required workload contract."""


class SemanticFailure(ValueError):
    """A successful HTTP response is not a valid RFdiffusion result."""


class TransportFailure(RuntimeError):
    """The exact endpoint could not be reached or returned an invalid status."""


@dataclass(frozen=True)
class Probe:
    index: int
    run_id: str
    random_seed: int
    expected_request_sha256: str
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


def canonical_request_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
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


def build_probes(run_ids: Sequence[str], fixture: bytes) -> tuple[Probe, Probe]:
    if len(run_ids) != 2:
        raise SetupFailure("--run-id must be supplied exactly twice")
    if run_ids[0] == run_ids[1]:
        raise SetupFailure("the two run IDs must be distinct")
    if sha256(fixture) != FIXTURE_SHA256:
        raise SetupFailure("1UBQ fixture digest mismatch")
    try:
        input_pdb = fixture.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SetupFailure("1UBQ fixture must be ASCII") from exc
    probes: list[Probe] = []
    for index, (run_id, case) in enumerate(zip(run_ids, FIXED_CASES, strict=True), 1):
        if RUN_ID.fullmatch(run_id) is None:
            raise SetupFailure("run IDs use unsupported characters or length")
        payload = {
            "input_pdb": input_pdb,
            "contigs": CONTIGS,
            "diffusion_steps": DIFFUSION_STEPS,
            "random_seed": case["random_seed"],
        }
        request_sha256 = sha256(canonical_request_bytes(payload))
        if request_sha256 != case["request_sha256"]:
            raise SetupFailure(f"request {index} differs from the retained oracle")
        probes.append(
            Probe(
                index=index,
                run_id=run_id,
                random_seed=int(case["random_seed"]),
                expected_request_sha256=str(case["request_sha256"]),
                payload=payload,
            )
        )
    return probes[0], probes[1]


def validate_pdb(text: str) -> dict[str, Any]:
    residues: OrderedDict[tuple[str, str, str], set[str]] = OrderedDict()
    ca_coordinates: OrderedDict[
        tuple[str, str, str], tuple[float, float, float]
    ] = OrderedDict()
    coordinates: list[tuple[float, float, float]] = []
    atom_count = 0

    for line in text.splitlines():
        if not line.startswith("ATOM"):
            continue
        if len(line) < 54:
            raise SemanticFailure("generated PDB contains a short ATOM record")
        atom_name = line[12:16].strip()
        key = (line[21:22].strip(), line[22:26].strip(), line[26:27].strip())
        if not key[1]:
            raise SemanticFailure("generated PDB ATOM record lacks a residue number")
        try:
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError as exc:
            raise SemanticFailure("generated PDB contains an invalid coordinate") from exc
        if not all(math.isfinite(item) for item in xyz):
            raise SemanticFailure("generated PDB contains a non-finite coordinate")
        residues.setdefault(key, set()).add(atom_name)
        if atom_name == "CA":
            if key in ca_coordinates:
                raise SemanticFailure("generated PDB has duplicate CA atoms")
            ca_coordinates[key] = xyz
        coordinates.append(xyz)
        atom_count += 1

    residue_count = len(residues)
    if not 61 <= residue_count <= 71:
        raise SemanticFailure(f"expected 61-71 modeled residues, got {residue_count}")
    if atom_count < residue_count * 3:
        raise SemanticFailure("generated PDB has too few backbone atoms")
    incomplete = [
        key for key, names in residues.items() if not {"N", "CA", "C"}.issubset(names)
    ]
    if incomplete:
        raise SemanticFailure(
            f"{len(incomplete)} generated residues lack a complete N/CA/C backbone"
        )
    if len(ca_coordinates) != residue_count:
        raise SemanticFailure("generated PDB CA count does not equal residue count")

    extents = [
        max(point[axis] for point in coordinates)
        - min(point[axis] for point in coordinates)
        for axis in range(3)
    ]
    if max(extents) < 5.0:
        raise SemanticFailure("generated coordinates are degenerate")

    adjacent_distances: list[float] = []
    previous_key: tuple[str, str, str] | None = None
    previous_xyz: tuple[float, float, float] | None = None
    for key, xyz in ca_coordinates.items():
        if previous_key is not None and previous_xyz is not None and key[0] == previous_key[0]:
            try:
                sequential = int(key[1]) == int(previous_key[1]) + 1
            except ValueError:
                sequential = False
            if sequential:
                distance = math.dist(previous_xyz, xyz)
                if not 2.5 <= distance <= 5.0:
                    raise SemanticFailure(
                        f"implausible adjacent CA distance {distance:.3f}"
                    )
                adjacent_distances.append(distance)
        previous_key, previous_xyz = key, xyz
    if len(adjacent_distances) < 15:
        raise SemanticFailure("too few sequential CA pairs to prove a backbone")

    return {
        "atom_count": atom_count,
        "residue_count": residue_count,
        "complete_backbone_residue_count": residue_count,
        "ca_count": len(ca_coordinates),
        "adjacent_ca_pair_count": len(adjacent_distances),
        "coordinate_extents": [round(item, 3) for item in extents],
    }


def validate_response(value: Any, probe: Probe) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticFailure("RFdiffusion response must be a JSON object")
    if value.get("error") not in (None, "", []) or value.get("detail") not in (
        None,
        "",
        [],
    ):
        raise SemanticFailure("RFdiffusion response contains an error field")
    output_pdb = value.get("output_pdb")
    if not isinstance(output_pdb, str):
        raise SemanticFailure("RFdiffusion output_pdb is missing")
    elapsed_ms = finite_nonnegative(value.get("elapsed_ms"), "elapsed_ms")
    backbone = validate_pdb(output_pdb)
    return {
        "random_seed": probe.random_seed,
        "contigs": CONTIGS,
        "diffusion_steps": DIFFUSION_STEPS,
        "fixture_sha256": FIXTURE_SHA256,
        "output_pdb_sha256": sha256(output_pdb.encode("utf-8")),
        "elapsed_ms": elapsed_ms,
        "backbone": backbone,
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
    request_body = canonical_request_bytes(probe.payload)
    if sha256(request_body) != probe.expected_request_sha256:
        raise SetupFailure(f"request {probe.index} digest changed after probe construction")
    write_private(receipt / f"request-{probe.index}.json", request_body + b"\n")
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    result = request_http(
        opener, endpoint, timeout, body=request_body, request_id=probe.run_id
    )
    response_received_at = utc_now()
    response_received_ns = time.monotonic_ns()
    elapsed_seconds = (response_received_ns - started_ns) / 1_000_000_000
    write_private(receipt / f"response-{probe.index}.body", result.body)
    write_private(
        receipt / f"response-{probe.index}.metadata.json",
        json_bytes(
            {
                "elapsed_seconds": round(elapsed_seconds, 6),
                "request_started_at": started_at,
                "response_received_at": response_received_at,
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
    validation_finished_at = utc_now()
    return {
        "status": "PASS",
        "ok": True,
        "index": probe.index,
        "run_id": probe.run_id,
        "endpoint": endpoint,
        "started_at": started_at,
        "request_started_at": started_at,
        "response_received_at": response_received_at,
        "validation_finished_at": validation_finished_at,
        "finished_at": validation_finished_at,
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
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--run-id", action="append", default=[])
    parser.add_argument("--ready-timeout", type=float, default=300.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        origin = validate_base_url(args.base_url)
        try:
            fixture = args.fixture.read_bytes()
        except OSError as exc:
            raise SetupFailure(f"could not read 1UBQ fixture: {exc}") from exc
        probes = build_probes(args.run_id, fixture)
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise SetupFailure("inference timeout must be positive and finite")
        receipt = create_receipt_dir(args.receipt_dir)
        started_at = utc_now()
        started_ns = time.monotonic_ns()
        opener = direct_opener()
        ready = wait_until_ready(opener, origin, args.ready_timeout)
        cases = [run_probe(opener, origin, probe, args.timeout, receipt) for probe in probes]
        if cases[0]["response_sha256"] == cases[1]["response_sha256"]:
            raise SemanticFailure("the two seeded calls returned identical responses")
        validation_finished_at = utc_now()
        validation_total_elapsed_seconds = round(
            (time.monotonic_ns() - started_ns) / 1_000_000_000, 6
        )
        summary = {
            "schema": "archvteams.nebius.ai/rfdiffusion-semantic-probe/v1",
            "validator": "rfdiffusion-strict-generate-v1",
            "response_timing_contract": RESPONSE_TIMING_CONTRACT,
            "status": "PASS",
            "ok": True,
            "base_url": origin,
            "ready": ready,
            "started_at": started_at,
            "finished_at": validation_finished_at,
            "validation_finished_at": validation_finished_at,
            "total_elapsed_seconds": validation_total_elapsed_seconds,
            "validation_total_elapsed_seconds": validation_total_elapsed_seconds,
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
        print(f"validate-rfdiffusion: FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
