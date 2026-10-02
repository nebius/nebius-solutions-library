#!/usr/bin/env python3
"""Fail-closed helpers for the fast-start resource-usage ledger.

The module deliberately uses only the Python standard library.  JSON decimal
values are strings, and the loader rejects JSON binary floats before any
accounting is attempted.
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Rounded,
    localcontext,
)
from pathlib import Path
from typing import Any, Iterable


RECEIPT_VERSION = "faststart-usage-receipt/v1"
LEDGER_VERSION = "faststart-usage-ledger/v1"
PRICE_VERSION = "faststart-price-snapshot/v1"

PHASES = {
    "node_provision",
    "pre_t0_setup",
    "gpu_critical_path",
    "cleanup",
    "idle_retained",
    "failed_attempt",
}
RESOURCE_UNITS = {
    "node": "node-second",
    "gpu": "gpu-second",
    "cpu": "vcpu-second",
    "memory": "memory-gibibyte-second",
    "storage": "storage-gibibyte-second",
}

_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_TIMESTAMP_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?P<fraction>\.[0-9]+)?Z$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Receipt values are small in practice, but a hostile or accidental very long
# decimal must fail instead of being rounded.  Every accounting operation goes
# through the exact helpers below, with rounding/inexact traps enabled.
_EXACT_CONTEXT = Context(prec=256)
_EXACT_CONTEXT.traps[Inexact] = True
_EXACT_CONTEXT.traps[Rounded] = True


class LedgerError(ValueError):
    """Raised when an input is incomplete, ambiguous, or inconsistent."""


def _reject_float(value: str) -> None:
    raise LedgerError(
        f"binary JSON float {value!r} is forbidden; use a decimal string"
    )


def load_json(path: str | Path) -> tuple[Any, str]:
    """Load JSON while rejecting binary floats and return its SHA-256."""

    raw = Path(path).read_bytes()
    try:
        value = json.loads(
            raw,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerError(f"invalid JSON in {path}: {exc}") from exc
    return value, hashlib.sha256(raw).hexdigest()


def dump_json(value: Any, path: str | Path) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    Path(path).write_text(rendered, encoding="utf-8")


def require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{where} must be an object")
    return value


def require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise LedgerError(f"{where} must be an array")
    return value


def normalize_string_set(value: Any, where: str) -> list[str]:
    items = require_list(value, where)
    normalized = [
        require_string(item, f"{where}[{index}]") for index, item in enumerate(items)
    ]
    if len(set(normalized)) != len(normalized):
        raise LedgerError(f"{where} must contain unique strings")
    return sorted(normalized)


def require_keys(
    value: dict[str, Any], required: Iterable[str], allowed: Iterable[str], where: str
) -> None:
    required_set = set(required)
    allowed_set = set(allowed)
    missing = sorted(required_set - value.keys())
    extra = sorted(value.keys() - allowed_set)
    if missing:
        raise LedgerError(f"{where} is missing required field(s): {', '.join(missing)}")
    if extra:
        raise LedgerError(f"{where} has unsupported field(s): {', '.join(extra)}")


def require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{where} must be a non-empty string")
    return value


def require_nullable_string(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return require_string(value, where)


def require_count(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerError(f"{where} must be a non-negative integer")
    return value


def require_decimal(value: Any, where: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise LedgerError(
            f"{where} must be a non-negative plain decimal string (no float/exponent)"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:  # defensive; the regular expression is stricter
        raise LedgerError(f"{where} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        qualifier = "positive" if positive else "non-negative"
        raise LedgerError(f"{where} must be a finite {qualifier} decimal")
    return parsed


def decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise LedgerError("cannot serialize a non-finite decimal")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def exact_add(left: Decimal, right: Decimal, where: str) -> Decimal:
    try:
        with localcontext(_EXACT_CONTEXT):
            return left + right
    except DecimalException as exc:
        raise LedgerError(f"{where} exceeds exact Decimal precision") from exc


def exact_subtract(left: Decimal, right: Decimal, where: str) -> Decimal:
    try:
        with localcontext(_EXACT_CONTEXT):
            return left - right
    except DecimalException as exc:
        raise LedgerError(f"{where} exceeds exact Decimal precision") from exc


def exact_multiply(left: Decimal, right: Decimal, where: str) -> Decimal:
    try:
        with localcontext(_EXACT_CONTEXT):
            return left * right
    except DecimalException as exc:
        raise LedgerError(f"{where} exceeds exact Decimal precision") from exc


def timestamp_decimal(value: Any, where: str) -> Decimal:
    """Parse canonical UTC RFC3339 without converting through a float."""

    if not isinstance(value, str):
        raise LedgerError(f"{where} must be a UTC timestamp string")
    match = _TIMESTAMP_RE.fullmatch(value)
    if not match:
        raise LedgerError(
            f"{where} must use canonical UTC RFC3339 form YYYY-MM-DDTHH:MM:SS[.fraction]Z"
        )
    try:
        base = datetime.strptime(
            f"{match.group('date')}T{match.group('hour')}:"
            f"{match.group('minute')}:{match.group('second')}",
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError as exc:
        raise LedgerError(f"{where} is not a valid UTC timestamp: {exc}") from exc
    seconds = Decimal(calendar.timegm(base.timetuple()))
    fraction = match.group("fraction")
    if fraction:
        seconds = exact_add(seconds, Decimal(f"0{fraction}"), where)
    return seconds


def duration(start: str, end: str, where: str) -> Decimal:
    start_value = timestamp_decimal(start, f"{where}.start_at")
    end_value = timestamp_decimal(end, f"{where}.end_at")
    if end_value < start_value:
        raise LedgerError(f"{where} has a negative/nonmonotonic interval")
    return exact_subtract(end_value, start_value, where)


def _derived_interval(name: str, start: str, end: str, where: str) -> dict[str, str]:
    return {
        "name": name,
        "start_at": start,
        "end_at": end,
        "duration_seconds": decimal_string(duration(start, end, where)),
    }


def _attempt_sort_key(attempt: dict[str, Any]) -> tuple[Decimal, str]:
    return (
        timestamp_decimal(attempt["t0_at"], "attempt.t0_at"),
        attempt["attempt_id"],
    )


_ATTEMPT_REQUIRED = {
    "attempt_id",
    "outcome",
    "t0_at",
    "http_ready_at",
    "call1_dispatched_at",
    "call1_response_received_at",
    "call2_dispatched_at",
    "call2_response_received_at",
    "validation_completed_at",
    "failure_at",
    "failure",
}


def normalize_attempt(raw: Any, where: str) -> dict[str, Any]:
    attempt = require_object(raw, where)
    require_keys(attempt, _ATTEMPT_REQUIRED, _ATTEMPT_REQUIRED, where)
    attempt_id = require_string(attempt["attempt_id"], f"{where}.attempt_id")
    outcome = attempt["outcome"]
    if outcome not in {"SUCCEEDED", "FAILED"}:
        raise LedgerError(f"{where}.outcome must be SUCCEEDED or FAILED")
    t0 = require_string(attempt["t0_at"], f"{where}.t0_at")
    timestamp_decimal(t0, f"{where}.t0_at")

    normalized: dict[str, Any] = {
        "attempt_id": attempt_id,
        "outcome": outcome,
        "t0_at": t0,
    }
    success_fields = [
        "http_ready_at",
        "call1_dispatched_at",
        "call1_response_received_at",
        "call2_dispatched_at",
        "call2_response_received_at",
        "validation_completed_at",
    ]
    if outcome == "SUCCEEDED":
        values: list[str] = []
        for field in success_fields:
            field_value = require_string(attempt[field], f"{where}.{field}")
            timestamp_decimal(field_value, f"{where}.{field}")
            values.append(field_value)
            normalized[field] = field_value
        if attempt["failure_at"] is not None or attempt["failure"] is not None:
            raise LedgerError(f"{where} succeeded but contains failure details")
        ordered = [t0, *values]
        numeric = [timestamp_decimal(value, where) for value in ordered]
        if numeric != sorted(numeric):
            raise LedgerError(f"{where} contains nonmonotonic success milestones")
        normalized["failure_at"] = None
        normalized["failure"] = None
        normalized["derived_intervals"] = [
            _derived_interval("demand_to_http_ready", t0, values[0], where),
            _derived_interval("first_inference_call", values[1], values[2], where),
            _derived_interval("second_inference_call", values[3], values[4], where),
            _derived_interval("demand_to_second_response", t0, values[4], where),
            _derived_interval("validation_tail", values[4], values[5], where),
        ]
    else:
        for field in success_fields:
            if attempt[field] is not None:
                raise LedgerError(f"{where}.{field} must be null for a failed attempt")
            normalized[field] = None
        failure_at = require_string(attempt["failure_at"], f"{where}.failure_at")
        if timestamp_decimal(failure_at, f"{where}.failure_at") < timestamp_decimal(
            t0, f"{where}.t0_at"
        ):
            raise LedgerError(f"{where} has a failure before T0")
        failure = require_object(attempt["failure"], f"{where}.failure")
        require_keys(failure, {"stage", "code"}, {"stage", "code"}, f"{where}.failure")
        normalized["failure_at"] = failure_at
        normalized["failure"] = {
            "stage": require_string(failure["stage"], f"{where}.failure.stage"),
            "code": require_string(failure["code"], f"{where}.failure.code"),
        }
        normalized["derived_intervals"] = [
            _derived_interval("failed_attempt", t0, failure_at, where)
        ]
    return normalized


_RUN_REQUIRED = {
    "run_id",
    "model_id",
    "measurement_class",
    "observed_from",
    "observed_until",
    "attempt_count",
    "successful_attempt_count",
    "failed_attempt_count",
}


def normalize_run(raw: Any, where: str) -> dict[str, Any]:
    run = require_object(raw, where)
    require_keys(run, _RUN_REQUIRED, _RUN_REQUIRED, where)
    normalized = {
        "run_id": require_string(run["run_id"], f"{where}.run_id"),
        "model_id": require_string(run["model_id"], f"{where}.model_id"),
        "measurement_class": require_string(
            run["measurement_class"], f"{where}.measurement_class"
        ),
        "observed_from": require_string(
            run["observed_from"], f"{where}.observed_from"
        ),
        "observed_until": require_nullable_string(
            run["observed_until"], f"{where}.observed_until"
        ),
        "attempt_count": require_count(run["attempt_count"], f"{where}.attempt_count"),
        "successful_attempt_count": require_count(
            run["successful_attempt_count"], f"{where}.successful_attempt_count"
        ),
        "failed_attempt_count": require_count(
            run["failed_attempt_count"], f"{where}.failed_attempt_count"
        ),
    }
    start = timestamp_decimal(normalized["observed_from"], f"{where}.observed_from")
    if normalized["observed_until"] is not None:
        end = timestamp_decimal(normalized["observed_until"], f"{where}.observed_until")
        if end < start:
            raise LedgerError(f"{where} has a negative observation window")
    return normalized


_INTERVAL_REQUIRED = {"interval_id", "attempt_id", "phase", "start_at", "end_at"}
_RESOURCE_REQUIRED = {
    "resource_id",
    "resource_type",
    "sku",
    "usage_unit",
    "quantity",
    "shared",
    "applies_to_attempt_ids",
    "allocated_at",
    "released_at",
    "intervals",
}


def normalize_interval(raw: Any, where: str) -> dict[str, Any]:
    interval = require_object(raw, where)
    require_keys(interval, _INTERVAL_REQUIRED, _INTERVAL_REQUIRED, where)
    interval_id = require_string(interval["interval_id"], f"{where}.interval_id")
    attempt_id = require_nullable_string(interval["attempt_id"], f"{where}.attempt_id")
    phase = interval["phase"]
    if phase not in PHASES:
        raise LedgerError(f"{where}.phase is unsupported: {phase!r}")
    start = require_string(interval["start_at"], f"{where}.start_at")
    timestamp_decimal(start, f"{where}.start_at")
    end = require_nullable_string(interval["end_at"], f"{where}.end_at")
    if end is None:
        if phase != "idle_retained":
            raise LedgerError(f"{where} may be open only for idle_retained")
    else:
        duration(start, end, where)
    if phase == "idle_retained" and attempt_id is not None:
        raise LedgerError(f"{where}.idle_retained must not carry attempt_id")
    if phase != "idle_retained" and attempt_id is None:
        raise LedgerError(f"{where}.{phase} requires attempt_id")
    return {
        "interval_id": interval_id,
        "attempt_id": attempt_id,
        "phase": phase,
        "start_at": start,
        "end_at": end,
    }


def normalize_resource(raw: Any, where: str) -> dict[str, Any]:
    resource = require_object(raw, where)
    require_keys(resource, _RESOURCE_REQUIRED, _RESOURCE_REQUIRED, where)
    resource_type = resource["resource_type"]
    if resource_type not in RESOURCE_UNITS:
        raise LedgerError(f"{where}.resource_type is unsupported: {resource_type!r}")
    expected_unit = RESOURCE_UNITS[resource_type]
    usage_unit = require_string(resource["usage_unit"], f"{where}.usage_unit")
    if usage_unit != expected_unit:
        raise LedgerError(
            f"{where}.usage_unit {usage_unit!r} does not match {resource_type!r} "
            f"({expected_unit!r})"
        )
    if not isinstance(resource["shared"], bool):
        raise LedgerError(f"{where}.shared must be a boolean")
    intervals = [
        normalize_interval(value, f"{where}.intervals[{index}]")
        for index, value in enumerate(require_list(resource["intervals"], f"{where}.intervals"))
    ]
    if not intervals:
        raise LedgerError(f"{where}.intervals must not be empty")
    intervals.sort(key=_interval_sort_key)
    if any(item["phase"] == "node_provision" for item in intervals) and resource_type != "node":
        raise LedgerError(f"{where}.node_provision requires resource_type 'node'")
    _validate_interval_order(intervals, where)
    allocated_at = require_string(resource["allocated_at"], f"{where}.allocated_at")
    allocated_value = timestamp_decimal(allocated_at, f"{where}.allocated_at")
    released_at = require_nullable_string(resource["released_at"], f"{where}.released_at")
    if released_at is not None:
        released_value = timestamp_decimal(released_at, f"{where}.released_at")
        if released_value < allocated_value:
            raise LedgerError(f"{where} has release before allocation")
    if intervals[0]["start_at"] != allocated_at:
        raise LedgerError(f"{where} intervals must begin exactly at allocated_at")
    if intervals[-1]["end_at"] != released_at:
        raise LedgerError(f"{where} intervals must end exactly at released_at")
    return {
        "resource_id": require_string(resource["resource_id"], f"{where}.resource_id"),
        "resource_type": resource_type,
        "sku": require_string(resource["sku"], f"{where}.sku"),
        "usage_unit": usage_unit,
        "quantity": decimal_string(
            require_decimal(resource["quantity"], f"{where}.quantity", positive=True)
        ),
        "shared": resource["shared"],
        "applies_to_attempt_ids": normalize_string_set(
            resource["applies_to_attempt_ids"],
            f"{where}.applies_to_attempt_ids",
        ),
        "allocated_at": allocated_at,
        "released_at": released_at,
        "intervals": intervals,
    }


def _interval_sort_key(
    interval: dict[str, Any],
) -> tuple[Decimal, int, Decimal, str, str, str]:
    start = timestamp_decimal(interval["start_at"], "interval.start_at")
    if interval["end_at"] is None:
        end_is_open = 1
        end = Decimal(0)
    else:
        end_is_open = 0
        end = timestamp_decimal(interval["end_at"], "interval.end_at")
    return (
        start,
        end_is_open,
        end,
        interval["interval_id"],
        interval["attempt_id"] or "",
        interval["phase"],
    )


def _validate_interval_order(intervals: list[dict[str, Any]], where: str) -> None:
    previous_start: Decimal | None = None
    previous_end: Decimal | None = None
    previous_open = False
    for index, interval in enumerate(intervals):
        start = timestamp_decimal(interval["start_at"], f"{where}.intervals[{index}].start_at")
        end = (
            None
            if interval["end_at"] is None
            else timestamp_decimal(interval["end_at"], f"{where}.intervals[{index}].end_at")
        )
        if previous_start is not None and start < previous_start:
            raise LedgerError(f"{where}.intervals are nonmonotonic")
        if previous_open:
            raise LedgerError(f"{where} has an interval after an open interval")
        if previous_end is not None and start < previous_end:
            raise LedgerError(f"{where}.intervals overlap and would double-count usage")
        if previous_end is not None and start > previous_end:
            raise LedgerError(
                f"{where}.intervals contain an unaccounted gap; record it as idle_retained"
            )
        previous_start = start
        previous_end = end
        previous_open = end is None


def normalize_receipt(raw: Any, sha256: str, where: str) -> dict[str, Any]:
    receipt = require_object(raw, where)
    required = {"schema_version", "receipt_id", "run", "attempts", "resources"}
    require_keys(receipt, required, required, where)
    if receipt["schema_version"] != RECEIPT_VERSION:
        raise LedgerError(
            f"{where}.schema_version must be {RECEIPT_VERSION!r}"
        )
    receipt_id = require_string(receipt["receipt_id"], f"{where}.receipt_id")
    run = normalize_run(receipt["run"], f"{where}.run")
    attempts = [
        normalize_attempt(value, f"{where}.attempts[{index}]")
        for index, value in enumerate(require_list(receipt["attempts"], f"{where}.attempts"))
    ]
    if len({attempt["attempt_id"] for attempt in attempts}) != len(attempts):
        raise LedgerError(f"{where} contains duplicate attempt_id values")
    attempts.sort(key=_attempt_sort_key)
    successful = sum(item["outcome"] == "SUCCEEDED" for item in attempts)
    failed = sum(item["outcome"] == "FAILED" for item in attempts)
    if run["attempt_count"] != len(attempts):
        raise LedgerError(
            f"{where} omits attempts: declared {run['attempt_count']}, present {len(attempts)}"
        )
    if run["successful_attempt_count"] != successful or run["failed_attempt_count"] != failed:
        raise LedgerError(
            f"{where} success/failure declarations do not match the explicit attempts"
        )
    resources = [
        normalize_resource(value, f"{where}.resources[{index}]")
        for index, value in enumerate(require_list(receipt["resources"], f"{where}.resources"))
    ]
    if not resources:
        raise LedgerError(f"{where}.resources must not be empty")
    _validate_receipt_observation(run, attempts, resources, where)
    return {
        "schema_version": RECEIPT_VERSION,
        "receipt_id": receipt_id,
        "sha256": sha256,
        "run": run,
        "attempts": attempts,
        "resources": resources,
    }


def _assert_within_observation(
    timestamp: str, run: dict[str, Any], where: str
) -> None:
    value = timestamp_decimal(timestamp, where)
    start = timestamp_decimal(run["observed_from"], "run.observed_from")
    if value < start:
        raise LedgerError(f"{where} precedes run.observed_from")
    if run["observed_until"] is not None:
        end = timestamp_decimal(run["observed_until"], "run.observed_until")
        if value > end:
            raise LedgerError(f"{where} follows run.observed_until")


def _validate_receipt_observation(
    run: dict[str, Any],
    attempts: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    where: str,
) -> None:
    for attempt_index, attempt in enumerate(attempts):
        for field in (
            "t0_at",
            "http_ready_at",
            "call1_dispatched_at",
            "call1_response_received_at",
            "call2_dispatched_at",
            "call2_response_received_at",
            "validation_completed_at",
            "failure_at",
        ):
            if attempt[field] is not None:
                _assert_within_observation(
                    attempt[field],
                    run,
                    f"{where}.attempts[{attempt_index}].{field}",
                )
    for resource_index, resource in enumerate(resources):
        resource_where = f"{where}.resources[{resource_index}]"
        _assert_within_observation(
            resource["allocated_at"], run, f"{resource_where}.allocated_at"
        )
        if resource["released_at"] is not None:
            _assert_within_observation(
                resource["released_at"], run, f"{resource_where}.released_at"
            )
        for interval_index, interval in enumerate(resource["intervals"]):
            interval_where = f"{resource_where}.intervals[{interval_index}]"
            _assert_within_observation(
                interval["start_at"], run, f"{interval_where}.start_at"
            )
            if interval["end_at"] is not None:
                _assert_within_observation(
                    interval["end_at"], run, f"{interval_where}.end_at"
                )


def _interval_identity(interval: dict[str, Any]) -> tuple[Any, ...]:
    return (
        interval["interval_id"],
        interval["attempt_id"],
        interval["phase"],
        interval["start_at"],
        interval["end_at"],
    )


def _resource_identity(resource: dict[str, Any]) -> tuple[Any, ...]:
    return (
        resource["resource_type"],
        resource["sku"],
        resource["usage_unit"],
        resource["quantity"],
        resource["shared"],
        tuple(resource["applies_to_attempt_ids"]),
        resource["allocated_at"],
        resource["released_at"],
    )


def _attempt_terminal(attempt: dict[str, Any]) -> str:
    if attempt["outcome"] == "SUCCEEDED":
        return attempt["call2_response_received_at"]
    return attempt["failure_at"]


def _validate_resource_phase_topology(
    resource: dict[str, Any],
    attempts_by_id: dict[str, dict[str, Any]],
    where: str,
) -> None:
    applies = resource["applies_to_attempt_ids"]
    unknown_applies = sorted(set(applies) - attempts_by_id.keys())
    if unknown_applies:
        raise LedgerError(
            f"{where}.applies_to_attempt_ids references unknown attempt(s): "
            f"{', '.join(unknown_applies)}"
        )

    allocated = timestamp_decimal(resource["allocated_at"], f"{where}.allocated_at")
    released = (
        None
        if resource["released_at"] is None
        else timestamp_decimal(resource["released_at"], f"{where}.released_at")
    )
    for attempt_id in applies:
        attempt = attempts_by_id[attempt_id]
        start = timestamp_decimal(attempt["t0_at"], f"attempt {attempt_id}.t0_at")
        terminal_text = _attempt_terminal(attempt)
        terminal = timestamp_decimal(
            terminal_text, f"attempt {attempt_id}.terminal_at"
        )
        fully_covers = allocated <= start and (released is None or released >= terminal)
        if not fully_covers:
            raise LedgerError(
                f"{where} does not span the complete active interval for applicable "
                f"attempt {attempt_id!r}"
            )

    active_counts: dict[str, int] = defaultdict(int)
    for index, interval in enumerate(resource["intervals"]):
        phase = interval["phase"]
        attempt_id = interval["attempt_id"]
        interval_where = f"{where}.intervals[{index}]"
        if phase == "idle_retained":
            continue
        if attempt_id not in attempts_by_id:
            raise LedgerError(f"{interval_where} references an unknown attempt")
        attempt = attempts_by_id[attempt_id]
        terminal = _attempt_terminal(attempt)
        if phase in {"gpu_critical_path", "failed_attempt"}:
            if attempt_id not in applies:
                raise LedgerError(
                    f"{interval_where} is active for a resource not applicable to "
                    f"attempt {attempt_id!r}"
                )
            expected_phase = (
                "gpu_critical_path"
                if attempt["outcome"] == "SUCCEEDED"
                else "failed_attempt"
            )
            if (
                phase != expected_phase
                or interval["start_at"] != attempt["t0_at"]
                or interval["end_at"] != terminal
            ):
                raise LedgerError(
                    f"{interval_where} does not match the exact phase and absolute "
                    f"boundaries for attempt {attempt_id!r}"
                )
            active_counts[attempt_id] += 1
        elif phase == "cleanup":
            if attempt_id not in applies:
                raise LedgerError(
                    f"{interval_where}.cleanup requires an applicable attempt"
                )
            if timestamp_decimal(
                interval["start_at"], f"{interval_where}.start_at"
            ) < timestamp_decimal(terminal, f"attempt {attempt_id}.terminal_at"):
                raise LedgerError(
                    f"{interval_where}.cleanup starts before attempt {attempt_id!r} "
                    "reaches its terminal timestamp"
                )
        elif phase == "pre_t0_setup":
            if timestamp_decimal(
                interval["end_at"], f"{interval_where}.end_at"
            ) > timestamp_decimal(attempt["t0_at"], f"attempt {attempt_id}.t0_at"):
                raise LedgerError(
                    f"{interval_where}.pre_t0_setup ends after target attempt "
                    f"{attempt_id!r} T0"
                )
        elif phase == "node_provision":
            if attempt_id not in applies:
                raise LedgerError(
                    f"{interval_where}.node_provision requires an applicable attempt"
                )
            if index != 0 or interval["start_at"] != resource["allocated_at"]:
                raise LedgerError(
                    f"{interval_where}.node_provision must be the initial resource interval"
                )
            if timestamp_decimal(
                interval["end_at"], f"{interval_where}.end_at"
            ) > timestamp_decimal(attempt["t0_at"], f"attempt {attempt_id}.t0_at"):
                raise LedgerError(
                    f"{interval_where}.node_provision ends after target attempt "
                    f"{attempt_id!r} T0"
                )

    for attempt_id in applies:
        if active_counts[attempt_id] != 1:
            raise LedgerError(
                f"{where} must contain exactly one active interval for applicable "
                f"attempt {attempt_id!r}"
            )


def build_usage_ledger(receipts_with_hashes: list[tuple[Any, str]]) -> dict[str, Any]:
    if not receipts_with_hashes:
        raise LedgerError("at least one explicit receipt is required")
    receipts = [
        normalize_receipt(raw, sha256, f"receipt[{index}]")
        for index, (raw, sha256) in enumerate(receipts_with_hashes)
    ]
    receipt_ids = [receipt["receipt_id"] for receipt in receipts]
    if len(set(receipt_ids)) != len(receipt_ids):
        raise LedgerError("receipt_id values must be unique")

    run_ids = {receipt["run"]["run_id"] for receipt in receipts}
    models = {receipt["run"]["model_id"] for receipt in receipts}
    classes = {receipt["run"]["measurement_class"] for receipt in receipts}
    if len(run_ids) != 1 or len(models) != 1 or len(classes) != 1:
        raise LedgerError("all receipts must describe the same run, model, and measurement class")

    all_attempts: list[dict[str, Any]] = []
    for receipt in receipts:
        all_attempts.extend(copy.deepcopy(receipt["attempts"]))
    if not all_attempts:
        raise LedgerError("usage ledger requires at least one explicit attempt")
    attempt_ids = [attempt["attempt_id"] for attempt in all_attempts]
    if len(set(attempt_ids)) != len(attempt_ids):
        raise LedgerError("attempt_id values must be globally unique across receipts")
    all_attempts.sort(key=_attempt_sort_key)

    observed_from = min(
        (receipt["run"]["observed_from"] for receipt in receipts),
        key=lambda value: (
            timestamp_decimal(value, "run.observed_from"),
            value,
        ),
    )
    observed_until_values = [receipt["run"]["observed_until"] for receipt in receipts]
    observed_until = (
        None
        if any(value is None for value in observed_until_values)
        else max(
            observed_until_values,
            key=lambda value: (
                timestamp_decimal(value, "run.observed_until"),
                value,
            ),
        )
    )
    run = {
        "run_id": next(iter(run_ids)),
        "model_id": next(iter(models)),
        "measurement_class": next(iter(classes)),
        "observed_from": observed_from,
        "observed_until": observed_until,
        "attempt_count": len(all_attempts),
        "successful_attempt_count": sum(
            attempt["outcome"] == "SUCCEEDED" for attempt in all_attempts
        ),
        "failed_attempt_count": sum(
            attempt["outcome"] == "FAILED" for attempt in all_attempts
        ),
    }

    for index, attempt in enumerate(all_attempts):
        for field in (
            "t0_at",
            "http_ready_at",
            "call1_dispatched_at",
            "call1_response_received_at",
            "call2_dispatched_at",
            "call2_response_received_at",
            "validation_completed_at",
            "failure_at",
        ):
            if attempt[field] is not None:
                _assert_within_observation(attempt[field], run, f"attempts[{index}].{field}")

    resources_by_id: dict[str, dict[str, Any]] = {}
    seen_resource_receipts: dict[str, set[str]] = defaultdict(set)
    for receipt in receipts:
        for resource in receipt["resources"]:
            resource_id = resource["resource_id"]
            existing = resources_by_id.get(resource_id)
            if existing is None:
                existing = {
                    key: copy.deepcopy(value)
                    for key, value in resource.items()
                    if key != "intervals"
                }
                existing["source_receipt_ids"] = []
                existing["intervals"] = []
                resources_by_id[resource_id] = existing
            else:
                if not existing["shared"] or not resource["shared"]:
                    raise LedgerError(
                        f"non-shared resource_id {resource_id!r} appears more than once"
                    )
                if _resource_identity(existing) != _resource_identity(resource):
                    raise LedgerError(
                        f"shared resource_id {resource_id!r} has mismatched identity/SKU/unit"
                    )
            if receipt["receipt_id"] not in seen_resource_receipts[resource_id]:
                existing["source_receipt_ids"].append(receipt["receipt_id"])
                seen_resource_receipts[resource_id].add(receipt["receipt_id"])
            for interval in resource["intervals"]:
                duplicate = next(
                    (
                        item
                        for item in existing["intervals"]
                        if _interval_identity(item) == _interval_identity(interval)
                    ),
                    None,
                )
                if duplicate is not None:
                    if receipt["receipt_id"] not in duplicate["source_receipt_ids"]:
                        duplicate["source_receipt_ids"].append(receipt["receipt_id"])
                    continue
                if any(
                    item["interval_id"] == interval["interval_id"]
                    for item in existing["intervals"]
                ):
                    raise LedgerError(
                        f"resource {resource_id!r} reuses interval_id "
                        f"{interval['interval_id']!r} for different observations"
                    )
                normalized_interval = copy.deepcopy(interval)
                normalized_interval["source_receipt_ids"] = [receipt["receipt_id"]]
                existing["intervals"].append(normalized_interval)

    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in all_attempts}
    output_resources: list[dict[str, Any]] = []
    for resource_id in sorted(resources_by_id):
        resource = resources_by_id[resource_id]
        resource["source_receipt_ids"].sort()
        resource["intervals"].sort(key=_interval_sort_key)
        _validate_interval_order(resource["intervals"], f"resource {resource_id!r}")
        quantity = require_decimal(resource["quantity"], f"resource {resource_id}.quantity")
        for index, interval in enumerate(resource["intervals"]):
            interval["source_receipt_ids"].sort()
            _assert_within_observation(
                interval["start_at"], run, f"resource {resource_id}.intervals[{index}].start_at"
            )
            if interval["end_at"] is not None:
                _assert_within_observation(
                    interval["end_at"], run, f"resource {resource_id}.intervals[{index}].end_at"
                )
            attempt_id = interval["attempt_id"]
            if attempt_id is not None and attempt_id not in attempts_by_id:
                raise LedgerError(
                    f"resource {resource_id!r} interval references unknown attempt {attempt_id!r}"
                )
            if interval["end_at"] is None:
                interval["duration_seconds"] = None
                interval["usage_quantity"] = None
            else:
                seconds = duration(
                    interval["start_at"],
                    interval["end_at"],
                    f"resource {resource_id}.intervals[{index}]",
                )
                interval["duration_seconds"] = decimal_string(seconds)
                interval["usage_quantity"] = decimal_string(
                    exact_multiply(seconds, quantity, "resource usage")
                )
            interval["cost"] = empty_cost()
        _validate_resource_phase_topology(
            resource,
            attempts_by_id,
            f"resource {resource_id!r}",
        )
        output_resources.append(resource)

    accounted_attempts = {
        attempt_id
        for resource in output_resources
        for attempt_id in resource["applies_to_attempt_ids"]
    }
    for attempt in all_attempts:
        if attempt["attempt_id"] not in accounted_attempts:
            raise LedgerError(
                f"attempt {attempt['attempt_id']!r} has no applicable resource meter"
            )

    ledger = {
        "schema_version": LEDGER_VERSION,
        "ledger_id": f"usage-ledger/{run['run_id']}",
        "source_receipts": sorted(
            (
                {"receipt_id": receipt["receipt_id"], "sha256": receipt["sha256"]}
                for receipt in receipts
            ),
            key=lambda item: item["receipt_id"],
        ),
        "run": run,
        "attempts": all_attempts,
        "resources": output_resources,
        "usage_summary": build_usage_summary(output_resources),
        "pricing": {
            "status": "INCOMPLETE",
            "snapshot_id": None,
            "snapshot_sha256": None,
            "snapshot_captured_at": None,
            "currency": None,
            "total_cost": None,
            "reason_codes": ["PRICE_SNAPSHOT_NOT_JOINED"],
        },
    }
    validate_ledger(ledger)
    return ledger


def empty_cost() -> dict[str, Any]:
    return {
        "status": "INCOMPLETE",
        "price_id": None,
        "price_status": None,
        "price_sku": None,
        "price_usage_unit": None,
        "effective_from": None,
        "effective_to": None,
        "currency": None,
        "unit_price": None,
        "amount": None,
    }


def build_usage_summary(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for resource in resources:
        for interval in resource["intervals"]:
            key = (
                interval["phase"],
                resource["resource_type"],
                resource["sku"],
                resource["usage_unit"],
            )
            group = groups.setdefault(
                key,
                {
                    "phase": interval["phase"],
                    "resource_type": resource["resource_type"],
                    "sku": resource["sku"],
                    "usage_unit": resource["usage_unit"],
                    "resource_ids": set(),
                    "closed_interval_count": 0,
                    "open_interval_count": 0,
                    "closed_duration_seconds": Decimal(0),
                    "closed_usage_quantity": Decimal(0),
                },
            )
            group["resource_ids"].add(resource["resource_id"])
            if interval["end_at"] is None:
                group["open_interval_count"] += 1
            else:
                group["closed_interval_count"] += 1
                group["closed_duration_seconds"] = exact_add(
                    group["closed_duration_seconds"],
                    require_decimal(interval["duration_seconds"], "duration_seconds"),
                    "usage summary duration",
                )
                group["closed_usage_quantity"] = exact_add(
                    group["closed_usage_quantity"],
                    require_decimal(interval["usage_quantity"], "usage_quantity"),
                    "usage summary quantity",
                )
    output = []
    for key in sorted(groups):
        group = groups[key]
        output.append(
            {
                "phase": group["phase"],
                "resource_type": group["resource_type"],
                "sku": group["sku"],
                "usage_unit": group["usage_unit"],
                "resource_ids": sorted(group["resource_ids"]),
                "closed_interval_count": group["closed_interval_count"],
                "open_interval_count": group["open_interval_count"],
                "closed_duration_seconds": decimal_string(
                    group["closed_duration_seconds"]
                ),
                "closed_usage_quantity": decimal_string(
                    group["closed_usage_quantity"]
                ),
            }
        )
    return output


def _validate_derived_attempt(attempt: dict[str, Any], where: str) -> None:
    raw = {key: attempt[key] for key in _ATTEMPT_REQUIRED}
    expected = normalize_attempt(raw, where)
    if attempt.get("derived_intervals") != expected["derived_intervals"]:
        raise LedgerError(f"{where}.derived_intervals do not match absolute timestamps")


def _validate_cost_shape(cost: Any, where: str) -> dict[str, Any]:
    value = require_object(cost, where)
    keys = {
        "status",
        "price_id",
        "price_status",
        "price_sku",
        "price_usage_unit",
        "effective_from",
        "effective_to",
        "currency",
        "unit_price",
        "amount",
    }
    require_keys(value, keys, keys, where)
    if value["status"] not in {"COMPLETE", "INCOMPLETE"}:
        raise LedgerError(f"{where}.status must be COMPLETE or INCOMPLETE")
    return value


def validate_ledger(raw: Any) -> None:
    """Recompute and validate every derived usage and cost field."""

    ledger = require_object(raw, "ledger")
    top_keys = {
        "schema_version",
        "ledger_id",
        "source_receipts",
        "run",
        "attempts",
        "resources",
        "usage_summary",
        "pricing",
    }
    require_keys(ledger, top_keys, top_keys, "ledger")
    if ledger["schema_version"] != LEDGER_VERSION:
        raise LedgerError(f"ledger.schema_version must be {LEDGER_VERSION!r}")
    require_string(ledger["ledger_id"], "ledger.ledger_id")

    sources = require_list(ledger["source_receipts"], "ledger.source_receipts")
    source_ids: list[str] = []
    for index, source_raw in enumerate(sources):
        where = f"ledger.source_receipts[{index}]"
        source = require_object(source_raw, where)
        require_keys(source, {"receipt_id", "sha256"}, {"receipt_id", "sha256"}, where)
        source_ids.append(require_string(source["receipt_id"], f"{where}.receipt_id"))
        sha256 = require_string(source["sha256"], f"{where}.sha256")
        if not _SHA256_RE.fullmatch(sha256):
            raise LedgerError(f"{where}.sha256 must be lowercase SHA-256")
    if len(set(source_ids)) != len(source_ids) or source_ids != sorted(source_ids):
        raise LedgerError("ledger.source_receipts must be unique and sorted")

    run = normalize_run(ledger["run"], "ledger.run")
    if ledger["ledger_id"] != f"usage-ledger/{run['run_id']}":
        raise LedgerError("ledger.ledger_id does not match run_id")
    attempts = require_list(ledger["attempts"], "ledger.attempts")
    if not attempts:
        raise LedgerError("ledger.attempts must contain at least one explicit attempt")
    attempt_ids: list[str] = []
    attempt_order: list[tuple[Decimal, str]] = []
    for index, attempt_raw in enumerate(attempts):
        where = f"ledger.attempts[{index}]"
        attempt = require_object(attempt_raw, where)
        require_keys(
            attempt,
            _ATTEMPT_REQUIRED | {"derived_intervals"},
            _ATTEMPT_REQUIRED | {"derived_intervals"},
            where,
        )
        _validate_derived_attempt(attempt, where)
        attempt_id = require_string(attempt["attempt_id"], f"{where}.attempt_id")
        attempt_ids.append(attempt_id)
        attempt_order.append(_attempt_sort_key(attempt))
        for field in (
            "t0_at",
            "http_ready_at",
            "call1_dispatched_at",
            "call1_response_received_at",
            "call2_dispatched_at",
            "call2_response_received_at",
            "validation_completed_at",
            "failure_at",
        ):
            if attempt[field] is not None:
                _assert_within_observation(attempt[field], run, f"{where}.{field}")
    if len(set(attempt_ids)) != len(attempt_ids):
        raise LedgerError("ledger.attempts contain duplicate attempt_id values")
    if attempt_order != sorted(attempt_order):
        raise LedgerError("ledger.attempts are not in canonical T0/attempt_id order")
    successful = sum(attempt["outcome"] == "SUCCEEDED" for attempt in attempts)
    failed = sum(attempt["outcome"] == "FAILED" for attempt in attempts)
    if (
        run["attempt_count"] != len(attempts)
        or run["successful_attempt_count"] != successful
        or run["failed_attempt_count"] != failed
    ):
        raise LedgerError("ledger.run attempt counts omit or misclassify attempts")
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}

    resources = require_list(ledger["resources"], "ledger.resources")
    resource_ids: list[str] = []
    accounted_attempts: set[str] = set()
    interval_costs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for resource_index, resource_raw in enumerate(resources):
        where = f"ledger.resources[{resource_index}]"
        resource = require_object(resource_raw, where)
        required = _RESOURCE_REQUIRED | {"source_receipt_ids"}
        require_keys(resource, required, required, where)
        resource_id = require_string(resource["resource_id"], f"{where}.resource_id")
        resource_ids.append(resource_id)
        resource_type = resource["resource_type"]
        if resource_type not in RESOURCE_UNITS:
            raise LedgerError(f"{where}.resource_type is unsupported")
        if resource["usage_unit"] != RESOURCE_UNITS[resource_type]:
            raise LedgerError(f"{where}.usage_unit mismatches resource_type")
        require_string(resource["sku"], f"{where}.sku")
        quantity = require_decimal(resource["quantity"], f"{where}.quantity", positive=True)
        if not isinstance(resource["shared"], bool):
            raise LedgerError(f"{where}.shared must be boolean")
        applies = normalize_string_set(
            resource["applies_to_attempt_ids"],
            f"{where}.applies_to_attempt_ids",
        )
        if resource["applies_to_attempt_ids"] != applies:
            raise LedgerError(f"{where}.applies_to_attempt_ids must be sorted")
        accounted_attempts.update(applies)
        allocated_at = require_string(resource["allocated_at"], f"{where}.allocated_at")
        _assert_within_observation(allocated_at, run, f"{where}.allocated_at")
        released_at = require_nullable_string(resource["released_at"], f"{where}.released_at")
        if released_at is not None:
            _assert_within_observation(released_at, run, f"{where}.released_at")
            if timestamp_decimal(released_at, f"{where}.released_at") < timestamp_decimal(
                allocated_at, f"{where}.allocated_at"
            ):
                raise LedgerError(f"{where} has release before allocation")
        resource_source_ids = require_list(
            resource["source_receipt_ids"], f"{where}.source_receipt_ids"
        )
        if (
            not resource_source_ids
            or any(item not in source_ids for item in resource_source_ids)
            or resource_source_ids != sorted(set(resource_source_ids))
        ):
            raise LedgerError(f"{where}.source_receipt_ids are invalid")
        intervals = require_list(resource["intervals"], f"{where}.intervals")
        if not intervals:
            raise LedgerError(f"{where}.intervals must not be empty")
        normalized_for_order: list[dict[str, Any]] = []
        interval_ids: set[str] = set()
        for interval_index, interval_raw in enumerate(intervals):
            interval_where = f"{where}.intervals[{interval_index}]"
            interval = require_object(interval_raw, interval_where)
            required_interval = _INTERVAL_REQUIRED | {
                "source_receipt_ids",
                "duration_seconds",
                "usage_quantity",
                "cost",
            }
            require_keys(interval, required_interval, required_interval, interval_where)
            normalized = normalize_interval(
                {key: interval[key] for key in _INTERVAL_REQUIRED}, interval_where
            )
            normalized_for_order.append(normalized)
            interval_id = normalized["interval_id"]
            if interval_id in interval_ids:
                raise LedgerError(f"{where} contains duplicate interval_id values")
            interval_ids.add(interval_id)
            ids = require_list(
                interval["source_receipt_ids"],
                f"{interval_where}.source_receipt_ids",
            )
            if not ids or any(item not in source_ids for item in ids) or ids != sorted(set(ids)):
                raise LedgerError(f"{interval_where}.source_receipt_ids are invalid")
            _assert_within_observation(interval["start_at"], run, f"{interval_where}.start_at")
            if interval["end_at"] is None:
                if (
                    interval["duration_seconds"] is not None
                    or interval["usage_quantity"] is not None
                ):
                    raise LedgerError(f"{interval_where} open interval must retain null usage")
            else:
                _assert_within_observation(interval["end_at"], run, f"{interval_where}.end_at")
                expected_duration = duration(
                    interval["start_at"], interval["end_at"], interval_where
                )
                actual_duration = require_decimal(
                    interval["duration_seconds"], f"{interval_where}.duration_seconds"
                )
                actual_usage = require_decimal(
                    interval["usage_quantity"], f"{interval_where}.usage_quantity"
                )
                if actual_duration != expected_duration:
                    raise LedgerError(f"{interval_where}.duration_seconds was not recomputed")
                if actual_usage != exact_multiply(
                    expected_duration, quantity, f"{interval_where}.usage_quantity"
                ):
                    raise LedgerError(f"{interval_where}.usage_quantity was not recomputed")
            attempt_id = interval["attempt_id"]
            if attempt_id is not None and attempt_id not in attempts_by_id:
                raise LedgerError(f"{interval_where} references an unknown attempt")
            cost = _validate_cost_shape(interval["cost"], f"{interval_where}.cost")
            interval_costs.append((resource, interval, cost))
        if normalized_for_order != sorted(normalized_for_order, key=_interval_sort_key):
            raise LedgerError(f"{where}.intervals are not in canonical order")
        _validate_interval_order(normalized_for_order, where)
        if (
            any(item["phase"] == "node_provision" for item in intervals)
            and resource_type != "node"
        ):
            raise LedgerError(f"{where}.node_provision requires resource_type 'node'")
        if intervals[0]["start_at"] != allocated_at:
            raise LedgerError(f"{where} intervals do not begin at allocated_at")
        if intervals[-1]["end_at"] != released_at:
            raise LedgerError(f"{where} intervals do not end at released_at")
        interval_source_ids = sorted(
            {
                receipt_id
                for interval in intervals
                for receipt_id in interval["source_receipt_ids"]
            }
        )
        if resource_source_ids != interval_source_ids:
            raise LedgerError(
                f"{where}.source_receipt_ids do not match interval provenance"
            )
        _validate_resource_phase_topology(resource, attempts_by_id, where)
    if resource_ids != sorted(resource_ids) or len(set(resource_ids)) != len(resource_ids):
        raise LedgerError(
            "ledger.resources must have unique sorted IDs; shared IDs must be deduplicated"
        )
    for attempt in attempts:
        if attempt["attempt_id"] not in accounted_attempts:
            raise LedgerError(
                f"attempt {attempt['attempt_id']!r} has no applicable resource meter"
            )

    expected_summary = build_usage_summary(resources)
    if ledger["usage_summary"] != expected_summary:
        raise LedgerError("ledger.usage_summary does not match recomputed resource intervals")
    _validate_pricing(ledger["pricing"], interval_costs)


def _validate_pricing(
    pricing_raw: Any,
    interval_costs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> None:
    pricing = require_object(pricing_raw, "ledger.pricing")
    keys = {
        "status",
        "snapshot_id",
        "snapshot_sha256",
        "snapshot_captured_at",
        "currency",
        "total_cost",
        "reason_codes",
    }
    require_keys(pricing, keys, keys, "ledger.pricing")
    if pricing["status"] not in {"COMPLETE", "INCOMPLETE"}:
        raise LedgerError("ledger.pricing.status must be COMPLETE or INCOMPLETE")
    reasons = require_list(pricing["reason_codes"], "ledger.pricing.reason_codes")
    if any(not isinstance(reason, str) or not reason for reason in reasons):
        raise LedgerError("ledger.pricing.reason_codes must contain strings")
    if reasons != sorted(set(reasons)):
        raise LedgerError("ledger.pricing.reason_codes must be unique and sorted")

    if pricing["snapshot_id"] is None:
        if (
            pricing
            != {
                "status": "INCOMPLETE",
                "snapshot_id": None,
                "snapshot_sha256": None,
                "snapshot_captured_at": None,
                "currency": None,
                "total_cost": None,
                "reason_codes": ["PRICE_SNAPSHOT_NOT_JOINED"],
            }
        ):
            raise LedgerError("unjoined pricing summary must be explicitly INCOMPLETE/null")
        for _, _, cost in interval_costs:
            if cost != empty_cost():
                raise LedgerError("unjoined interval cost fields must be INCOMPLETE/null")
        return

    require_string(pricing["snapshot_id"], "ledger.pricing.snapshot_id")
    sha = require_string(pricing["snapshot_sha256"], "ledger.pricing.snapshot_sha256")
    if not _SHA256_RE.fullmatch(sha):
        raise LedgerError("ledger.pricing.snapshot_sha256 must be lowercase SHA-256")
    require_string(pricing["snapshot_captured_at"], "ledger.pricing.snapshot_captured_at")
    timestamp_decimal(pricing["snapshot_captured_at"], "ledger.pricing.snapshot_captured_at")

    calculated_total = Decimal(0)
    currencies: set[str] = set()
    expected_reasons: set[str] = set()
    for resource, interval, cost in interval_costs:
        require_string(cost["price_id"], "interval.cost.price_id")
        if cost["price_status"] not in {"AVAILABLE", "UNAVAILABLE"}:
            raise LedgerError("joined interval cost has invalid price_status")
        if cost["price_sku"] != resource["sku"]:
            raise LedgerError("joined price SKU mismatches resource SKU")
        if cost["price_usage_unit"] != resource["usage_unit"]:
            raise LedgerError("joined price unit mismatches resource usage unit")
        effective_from = require_string(cost["effective_from"], "interval.cost.effective_from")
        effective_start = timestamp_decimal(effective_from, "interval.cost.effective_from")
        interval_start = timestamp_decimal(interval["start_at"], "interval.start_at")
        if effective_start > interval_start:
            raise LedgerError("price effective date starts after the usage interval")
        effective_to = require_nullable_string(cost["effective_to"], "interval.cost.effective_to")
        effective_end = (
            None
            if effective_to is None
            else timestamp_decimal(effective_to, "interval.cost.effective_to")
        )
        if effective_end is not None and effective_end <= effective_start:
            raise LedgerError("price effective interval is negative/nonmonotonic")
        if interval["end_at"] is None:
            if effective_end is not None:
                raise LedgerError("open usage requires an open-ended effective price record")
            expected_reasons.add("OPEN_USAGE_INTERVAL")
        elif effective_end is not None and timestamp_decimal(
            interval["end_at"], "interval.end_at"
        ) > effective_end:
            raise LedgerError("price effective dates do not cover the usage interval")

        if cost["price_status"] == "UNAVAILABLE":
            if any(cost[field] is not None for field in ("currency", "unit_price", "amount")):
                raise LedgerError("unavailable price must retain null currency/value fields")
            if cost["status"] != "INCOMPLETE":
                raise LedgerError("unavailable price must produce INCOMPLETE cost")
            expected_reasons.add("PRICE_UNAVAILABLE")
            continue

        currency = require_string(cost["currency"], "interval.cost.currency")
        unit_price = require_decimal(cost["unit_price"], "interval.cost.unit_price")
        currencies.add(currency)
        if interval["usage_quantity"] is None:
            if cost["amount"] is not None or cost["status"] != "INCOMPLETE":
                raise LedgerError("open usage cannot have a complete cost amount")
        else:
            amount = require_decimal(cost["amount"], "interval.cost.amount")
            expected_amount = exact_multiply(
                unit_price,
                require_decimal(interval["usage_quantity"], "interval.usage_quantity"),
                "interval cost amount",
            )
            if amount != expected_amount:
                raise LedgerError("interval cost amount does not match Decimal multiplication")
            if cost["status"] != "COMPLETE":
                raise LedgerError("closed usage with an available price must be COMPLETE")
            calculated_total = exact_add(calculated_total, amount, "pricing total")

    if len(currencies) > 1:
        raise LedgerError("joined price snapshot mixes currencies")
    if expected_reasons:
        if pricing["status"] != "INCOMPLETE":
            raise LedgerError("incomplete interval costs require INCOMPLETE pricing")
        if pricing["currency"] is not None or pricing["total_cost"] is not None:
            raise LedgerError("incomplete total currency/cost must remain null")
        if reasons != sorted(expected_reasons):
            raise LedgerError("pricing reason_codes do not match incomplete intervals")
    else:
        if pricing["status"] != "COMPLETE" or reasons:
            raise LedgerError("fully priced closed usage must be COMPLETE")
        if len(currencies) != 1:
            raise LedgerError("complete pricing must contain exactly one currency")
        currency = next(iter(currencies))
        if pricing["currency"] != currency:
            raise LedgerError("pricing currency does not match interval currencies")
        if require_decimal(pricing["total_cost"], "ledger.pricing.total_cost") != calculated_total:
            raise LedgerError("pricing total_cost does not match Decimal interval sum")


_PRICE_REQUIRED = {
    "price_id",
    "sku",
    "usage_unit",
    "effective_from",
    "effective_to",
    "status",
    "currency",
    "unit_price",
    "source",
}


def normalize_price_snapshot(raw: Any, sha256: str) -> dict[str, Any]:
    snapshot = require_object(raw, "price_snapshot")
    keys = {"schema_version", "snapshot_id", "captured_at", "currency", "prices"}
    require_keys(snapshot, keys, keys, "price_snapshot")
    if snapshot["schema_version"] != PRICE_VERSION:
        raise LedgerError(f"price_snapshot.schema_version must be {PRICE_VERSION!r}")
    captured_at = require_string(snapshot["captured_at"], "price_snapshot.captured_at")
    timestamp_decimal(captured_at, "price_snapshot.captured_at")
    snapshot_currency = require_nullable_string(snapshot["currency"], "price_snapshot.currency")
    prices: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, price_raw in enumerate(require_list(snapshot["prices"], "price_snapshot.prices")):
        where = f"price_snapshot.prices[{index}]"
        price = require_object(price_raw, where)
        require_keys(price, _PRICE_REQUIRED, _PRICE_REQUIRED, where)
        price_id = require_string(price["price_id"], f"{where}.price_id")
        if price_id in ids:
            raise LedgerError("price_snapshot contains duplicate price_id values")
        ids.add(price_id)
        effective_from = require_string(price["effective_from"], f"{where}.effective_from")
        effective_start = timestamp_decimal(effective_from, f"{where}.effective_from")
        effective_to = require_nullable_string(price["effective_to"], f"{where}.effective_to")
        if effective_to is not None:
            effective_end = timestamp_decimal(effective_to, f"{where}.effective_to")
            if effective_end <= effective_start:
                raise LedgerError(f"{where} has negative/nonmonotonic effective dates")
        status = price["status"]
        if status not in {"AVAILABLE", "UNAVAILABLE"}:
            raise LedgerError(f"{where}.status must be AVAILABLE or UNAVAILABLE")
        currency = require_nullable_string(price["currency"], f"{where}.currency")
        unit_price = price["unit_price"]
        source = require_nullable_string(price["source"], f"{where}.source")
        if status == "AVAILABLE":
            if currency is None or source is None:
                raise LedgerError(f"{where} available price requires currency and source")
            unit_price = decimal_string(
                require_decimal(unit_price, f"{where}.unit_price")
            )
            if snapshot_currency is None or currency != snapshot_currency:
                raise LedgerError(f"{where} currency mismatches snapshot currency")
        else:
            if currency is not None or unit_price is not None:
                raise LedgerError(
                    f"{where} unavailable price must use null currency and unit_price"
                )
        usage_unit = require_string(price["usage_unit"], f"{where}.usage_unit")
        if usage_unit not in set(RESOURCE_UNITS.values()):
            raise LedgerError(f"{where}.usage_unit is unsupported: {usage_unit!r}")
        prices.append(
            {
                "price_id": price_id,
                "sku": require_string(price["sku"], f"{where}.sku"),
                "usage_unit": usage_unit,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "status": status,
                "currency": currency,
                "unit_price": unit_price,
                "source": source,
            }
        )
    if not prices:
        raise LedgerError("price_snapshot.prices must not be empty")
    if snapshot_currency is None and any(price["status"] == "AVAILABLE" for price in prices):
        raise LedgerError("price_snapshot.currency is missing for available prices")
    if not _SHA256_RE.fullmatch(sha256):
        raise LedgerError("price snapshot SHA-256 is invalid")

    by_meter: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for price in prices:
        by_meter[(price["sku"], price["usage_unit"])].append(price)
    for meter, records in by_meter.items():
        records.sort(key=lambda item: timestamp_decimal(item["effective_from"], "effective_from"))
        previous_end: Decimal | None = None
        previous_open = False
        for record in records:
            start = timestamp_decimal(record["effective_from"], "effective_from")
            if previous_open or (previous_end is not None and start < previous_end):
                raise LedgerError(
                    f"price records for SKU/unit {meter!r} overlap and are ambiguous"
                )
            previous_end = (
                None
                if record["effective_to"] is None
                else timestamp_decimal(record["effective_to"], "effective_to")
            )
            previous_open = record["effective_to"] is None
    return {
        "schema_version": PRICE_VERSION,
        "snapshot_id": require_string(snapshot["snapshot_id"], "price_snapshot.snapshot_id"),
        "captured_at": captured_at,
        "currency": snapshot_currency,
        "prices": prices,
        "sha256": sha256,
    }


def _select_price(
    prices: list[dict[str, Any]], resource: dict[str, Any], interval: dict[str, Any]
) -> dict[str, Any]:
    same_sku = [price for price in prices if price["sku"] == resource["sku"]]
    if not same_sku:
        raise LedgerError(f"missing price SKU {resource['sku']!r}")
    same_meter = [
        price for price in same_sku if price["usage_unit"] == resource["usage_unit"]
    ]
    if not same_meter:
        raise LedgerError(
            f"price unit mismatch for SKU {resource['sku']!r}: "
            f"required {resource['usage_unit']!r}"
        )
    start = timestamp_decimal(interval["start_at"], "interval.start_at")
    end = (
        None
        if interval["end_at"] is None
        else timestamp_decimal(interval["end_at"], "interval.end_at")
    )
    matches = []
    for price in same_meter:
        price_start = timestamp_decimal(price["effective_from"], "price.effective_from")
        price_end = (
            None
            if price["effective_to"] is None
            else timestamp_decimal(price["effective_to"], "price.effective_to")
        )
        covered = price_start <= start
        if end is None:
            covered = covered and price_end is None
        elif price_end is not None:
            covered = covered and end <= price_end
        if covered:
            matches.append(price)
    if len(matches) != 1:
        raise LedgerError(
            f"price effective dates do not uniquely cover resource {resource['resource_id']!r} "
            f"interval {interval['interval_id']!r}"
        )
    return matches[0]


def join_price_snapshot(
    ledger_raw: Any, snapshot_raw: Any, snapshot_sha256: str
) -> dict[str, Any]:
    validate_ledger(ledger_raw)
    if ledger_raw["pricing"]["snapshot_id"] is not None:
        raise LedgerError("ledger already has a joined price snapshot")
    snapshot = normalize_price_snapshot(snapshot_raw, snapshot_sha256)
    ledger = copy.deepcopy(ledger_raw)
    reasons: set[str] = set()
    total = Decimal(0)
    currencies: set[str] = set()
    for resource in ledger["resources"]:
        for interval in resource["intervals"]:
            price = _select_price(snapshot["prices"], resource, interval)
            cost = {
                "status": "INCOMPLETE",
                "price_id": price["price_id"],
                "price_status": price["status"],
                "price_sku": price["sku"],
                "price_usage_unit": price["usage_unit"],
                "effective_from": price["effective_from"],
                "effective_to": price["effective_to"],
                "currency": price["currency"],
                "unit_price": price["unit_price"],
                "amount": None,
            }
            if interval["end_at"] is None:
                reasons.add("OPEN_USAGE_INTERVAL")
            if price["status"] == "UNAVAILABLE":
                reasons.add("PRICE_UNAVAILABLE")
            elif interval["usage_quantity"] is not None:
                amount = exact_multiply(
                    require_decimal(price["unit_price"], "price.unit_price"),
                    require_decimal(interval["usage_quantity"], "interval.usage_quantity"),
                    "price join amount",
                )
                cost["status"] = "COMPLETE"
                cost["amount"] = decimal_string(amount)
                total = exact_add(total, amount, "price join total")
                currencies.add(price["currency"])
            else:
                currencies.add(price["currency"])
            interval["cost"] = cost
    if len(currencies) > 1:
        raise LedgerError("price snapshot would mix currencies in one ledger")
    ledger["pricing"] = {
        "status": "INCOMPLETE" if reasons else "COMPLETE",
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": snapshot["sha256"],
        "snapshot_captured_at": snapshot["captured_at"],
        "currency": None if reasons else next(iter(currencies)),
        "total_cost": None if reasons else decimal_string(total),
        "reason_codes": sorted(reasons),
    }
    validate_ledger(ledger)
    return ledger
