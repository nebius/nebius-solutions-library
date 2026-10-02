#!/usr/bin/env python3
"""Build the shared warm-instance and runtime-health qualification receipt.

The collector is deliberately offline.  Runners capture the API objects and the
target-container ``nvidia-smi`` output; this program rejects incomplete or
contradictory evidence before a trial can be counted in a fresh cohort.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


SCHEMA = "archvteams.nebius.ai/warm-instance-qualification/v3"
ACCEPTANCE_RESPONSE_PROXY = "client-observed-api-create-response-return/v1"
PRE_DISPATCH_BOUNDARY = "client-target-create-dispatch/v1"
HOST_XID_UNAVAILABLE_REASON = (
    "no task-scoped privileged node-log collector is present; target-container "
    "nvidia-smi cannot authoritatively prove absence of host-driver Xid records"
)
CONTROLLER_CLOCK_BOUNDARY_SCHEMA = (
    "archvteams.nebius.ai/controller-clock-boundary/v1"
)
BOOT_TIME_ANCHOR_SCHEMA = "archvteams.nebius.ai/node-boot-time-anchor/v1"
SEMANTIC_NODE_BOOTTIME_SCHEMA = (
    "archvteams.nebius.ai/semantic-node-boottime/v1"
)
BOOT_TIME_ANCHOR_HOLDER_IMAGE = (
    "docker.io/library/python@sha256:"
    "356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e"
)
SEMANTIC_SCHEMA_VERSION = 2
RESTORE_RECEIPT_SCHEMA = "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1"
MAX_ANCHOR_TO_T0_CONTROLLER_MONOTONIC_SECONDS = 1.25
MAX_CLOCK_RESOLUTION_NS = 1_000_000
MAX_EXPECTED_STARTUP_PROBE_WARNING_WINDOW_SECONDS = 1800.0
EXPECTED_STARTUP_PROBE_WARNING = "Startup probe failed:"
EXPECTED_STARTUP_PROBE = {
    "exec": {
        "command": [
            "/bin/test",
            "-f",
            "/snapshot-control/restore-complete",
        ]
    },
    "failureThreshold": 1800,
    "periodSeconds": 1,
    "successThreshold": 1,
    "timeoutSeconds": 1,
}


class QualificationError(ValueError):
    """Captured qualification evidence is missing, malformed, or unsafe to count."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QualificationError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{label} must be a JSON object")
    return value


def _read_timestamp(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise QualificationError(f"{label} must be a regular non-symlink file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise QualificationError(f"cannot read {label}: {type(exc).__name__}") from exc
    _timestamp(value, label)
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{label} must be an RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise QualificationError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualificationError(f"{label} must include a UTC offset")
    return parsed.astimezone(UTC)


def _sha256(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise QualificationError(f"{label} must be a regular non-symlink file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise QualificationError(f"cannot hash {label}: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _container_health(
    pod: dict[str, Any], *, expected_name: str, expected_phase: str, label: str
) -> dict[str, Any]:
    status = pod.get("status")
    if not isinstance(status, dict):
        raise QualificationError(f"{label} has no status")
    if status.get("phase") != expected_phase:
        raise QualificationError(f"{label} phase is not {expected_phase}")
    if status.get("reason") == "Evicted" or "evicted" in str(
        status.get("message", "")
    ).lower():
        raise QualificationError(f"{label} was evicted")

    statuses: list[dict[str, Any]] = []
    for field in ("initContainerStatuses", "containerStatuses"):
        values = status.get(field, [])
        if not isinstance(values, list):
            raise QualificationError(f"{label} {field} is malformed")
        statuses.extend(value for value in values if isinstance(value, dict))
    matches = [item for item in statuses if item.get("name") == expected_name]
    if len(matches) != 1:
        raise QualificationError(
            f"{label} must have exactly one {expected_name} container status"
        )
    if not statuses:
        raise QualificationError(f"{label} has no container statuses")

    for item in statuses:
        name = item.get("name")
        restart_count = item.get("restartCount")
        if isinstance(restart_count, bool) or restart_count != 0:
            raise QualificationError(f"{label} container {name} restarted")
        for state_field in ("state", "lastState"):
            state = item.get(state_field, {})
            if not isinstance(state, dict):
                raise QualificationError(
                    f"{label} container {name} {state_field} is malformed"
                )
            terminated = state.get("terminated")
            if isinstance(terminated, dict) and (
                terminated.get("reason") == "OOMKilled"
                or terminated.get("exitCode") not in (None, 0)
            ):
                raise QualificationError(
                    f"{label} container {name} has an OOM/nonzero termination"
                )

    expected_state = matches[0].get("state")
    if not isinstance(expected_state, dict):
        raise QualificationError(f"{label} expected container state is malformed")
    if expected_phase == "Running":
        if not isinstance(expected_state.get("running"), dict):
            raise QualificationError(f"{label} target container is not running")
    else:
        terminated = expected_state.get("terminated")
        if (
            not isinstance(terminated, dict)
            or terminated.get("exitCode") != 0
            or terminated.get("reason") == "OOMKilled"
        ):
            raise QualificationError(f"{label} job container did not exit cleanly")
    return {
        "phase": expected_phase,
        "expected_container": expected_name,
        "container_count": len(statuses),
        "restart_count": 0,
        "oom_killed": False,
        "evicted": False,
        "nonzero_termination": False,
    }


def _startup_probe_bounds(
    target: dict[str, Any],
    worker_pod: dict[str, Any],
    *,
    target_container: str,
    worker_container: str,
) -> tuple[datetime, datetime, datetime]:
    all_target_containers = target.get("spec", {}).get("containers", [])
    if not isinstance(all_target_containers, list):
        raise QualificationError("target startup probe contract is not exact")
    target_containers = [
        item
        for item in all_target_containers
        if isinstance(item, dict) and item.get("name") == target_container
    ]
    if (
        len(all_target_containers) != 1
        or len(target_containers) != 1
        or target_containers[0].get("startupProbe") != EXPECTED_STARTUP_PROBE
    ):
        raise QualificationError("target startup probe contract is not exact")
    target_statuses = [
        item
        for item in target.get("status", {}).get("containerStatuses", [])
        if isinstance(item, dict) and item.get("name") == target_container
    ]
    worker_statuses = [
        item
        for item in worker_pod.get("status", {}).get("containerStatuses", [])
        if isinstance(item, dict) and item.get("name") == worker_container
    ]
    ready_conditions = [
        item
        for item in target.get("status", {}).get("conditions", [])
        if isinstance(item, dict) and item.get("type") == "Ready"
    ]
    if (
        len(target_statuses) != 1
        or len(worker_statuses) != 1
        or len(ready_conditions) != 1
        or ready_conditions[0].get("status") != "True"
    ):
        raise QualificationError("startup probe timeline has incomplete Pod status")
    target_started = _timestamp(
        target_statuses[0].get("state", {}).get("running", {}).get("startedAt"),
        "target container start",
    )
    worker_finished = _timestamp(
        worker_statuses[0].get("state", {}).get("terminated", {}).get("finishedAt"),
        "restore worker finish",
    )
    target_ready = _timestamp(
        ready_conditions[0].get("lastTransitionTime"), "target Ready transition"
    )
    startup_window = (worker_finished - target_started).total_seconds()
    if (
        startup_window < 0
        or startup_window > MAX_EXPECTED_STARTUP_PROBE_WARNING_WINDOW_SECONDS
    ):
        raise QualificationError("startup probe timeline is outside its bounded window")
    return target_started, worker_finished, target_ready


def _restore_completion(
    receipt: dict[str, Any],
    *,
    run_id: str,
    namespace: str,
    target_name: str,
    target_uid: str,
    target_node: str,
    target_pod_spec_sha256: str,
    expected_image: str,
) -> datetime:
    duration_ms = receipt.get("duration_ms")
    if (
        receipt.get("schema") != RESTORE_RECEIPT_SCHEMA
        or receipt.get("status") != "succeeded"
        or receipt.get("run_id") != run_id
        or receipt.get("target_namespace") != namespace
        or receipt.get("target_name") != target_name
        or receipt.get("target_uid") != target_uid
        or receipt.get("target_node") != target_node
        or receipt.get("target_pod_spec_sha256") != target_pod_spec_sha256
        or receipt.get("target_image_id") != expected_image
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms < 0
    ):
        raise QualificationError("restore receipt does not identify this target")
    return _timestamp(receipt.get("completed_at"), "restore receipt completion")


def _event_receipt(
    events: dict[str, Any],
    *,
    target_uid: str,
    target_name: str,
    target_namespace: str,
    target_container: str,
    target_node: str,
    target_started: datetime,
    worker_finished: datetime,
    target_ready: datetime,
    restore_completed: datetime,
    expected_image: str,
) -> dict[str, Any]:
    if events.get("kind") not in {"EventList", "List"}:
        raise QualificationError("target Events capture is not an Event list")
    items = events.get("items")
    if not isinstance(items, list) or not items:
        raise QualificationError("target Events capture is empty")

    reason_counts: dict[str, int] = {}
    warning_count = 0
    expected_warning_count = 0
    expected_warning_occurrences = 0
    expected_warning_latest: datetime | None = None
    pulling_count = 0
    cached_pull_count = 0
    for item in items:
        if not isinstance(item, dict):
            raise QualificationError("target Events contains a malformed item")
        involved = item.get("involvedObject")
        if not isinstance(involved, dict) or involved.get("uid") != target_uid:
            raise QualificationError("target Events contains an event for another UID")
        reason = str(item.get("reason", ""))
        event_type = str(item.get("type", ""))
        message = str(item.get("message", ""))
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if event_type == "Warning":
            warning_count += 1
            source = item.get("source")
            count = item.get("count")
            if (
                reason != "Unhealthy"
                or message.rstrip() != EXPECTED_STARTUP_PROBE_WARNING
                or not isinstance(involved, dict)
                or involved.get("kind") != "Pod"
                or involved.get("name") != target_name
                or involved.get("namespace") != target_namespace
                or involved.get("fieldPath")
                != f"spec.containers{{{target_container}}}"
                or item.get("reportingComponent") != "kubelet"
                or item.get("reportingInstance") != target_node
                or not isinstance(source, dict)
                or source.get("component") != "kubelet"
                or source.get("host") != target_node
                or isinstance(count, bool)
                or not isinstance(count, int)
                or not 1 <= count < EXPECTED_STARTUP_PROBE["failureThreshold"]
            ):
                raise QualificationError("target emitted an unexpected Warning event")
            warning_first = _timestamp(
                item.get("firstTimestamp"), "startup probe Warning firstTimestamp"
            )
            warning_last = _timestamp(
                item.get("lastTimestamp"), "startup probe Warning lastTimestamp"
            )
            warning_created = _timestamp(
                item.get("metadata", {}).get("creationTimestamp")
                if isinstance(item.get("metadata"), dict)
                else None,
                "startup probe Warning creationTimestamp",
            )
            if (
                warning_first < target_started
                or warning_first > warning_last
                or warning_created < target_started
                or warning_created > warning_last
                or warning_last > worker_finished
                or warning_last > restore_completed
                or warning_last > target_ready
            ):
                raise QualificationError(
                    "startup probe Warning is outside the pre-restore startup window"
                )
            expected_warning_count += 1
            expected_warning_occurrences += count
            if expected_warning_latest is None or warning_last > expected_warning_latest:
                expected_warning_latest = warning_last
        if reason == "Pulling":
            pulling_count += 1
        if reason == "Pulled":
            if (
                "already present on machine" not in message.lower()
                or expected_image not in message
            ):
                raise QualificationError(
                    "target Pulled event does not prove the exact image was already present"
                )
            cached_pull_count += 1

    if pulling_count:
        raise QualificationError("target emitted a post-T0 Pulling event")
    if expected_warning_occurrences >= EXPECTED_STARTUP_PROBE["failureThreshold"]:
        raise QualificationError("startup probe Warning count reached failureThreshold")
    if cached_pull_count < 1:
        raise QualificationError(
            "target has no Pulled event proving the exact image was already present"
        )
    return {
        "event_count": len(items),
        "reason_counts": dict(sorted(reason_counts.items())),
        "warning_event_count": warning_count,
        "expected_startup_probe_warning_event_count": expected_warning_count,
        "expected_startup_probe_warning_occurrence_count": (
            expected_warning_occurrences
        ),
        "expected_startup_probe_warning_latest_at": (
            expected_warning_latest.isoformat().replace("+00:00", "Z")
            if expected_warning_latest is not None
            else None
        ),
        "unexpected_warning_event_count": 0,
        "maximum_expected_startup_probe_warning_window_seconds": (
            MAX_EXPECTED_STARTUP_PROBE_WARNING_WINDOW_SECONDS
        ),
        "restore_completed_at": restore_completed.isoformat().replace("+00:00", "Z"),
        "pulling_event_count": 0,
        "exact_image_already_present_event_count": cached_pull_count,
    }


def _gpu_receipt(path: Path, stderr_path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QualificationError("nvidia-smi XML must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QualificationError(
            f"cannot read nvidia-smi XML: {type(exc).__name__}"
        ) from exc
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise QualificationError("nvidia-smi XML size is outside the bounded receipt limit")
    if stderr_path.is_symlink() or not stderr_path.is_file():
        raise QualificationError("nvidia-smi stderr must be a regular non-symlink file")
    try:
        raw_stderr = stderr_path.read_bytes()
    except OSError as exc:
        raise QualificationError(
            f"cannot read nvidia-smi stderr: {type(exc).__name__}"
        ) from exc
    if len(raw_stderr) > 64 * 1024 or raw_stderr.strip():
        raise QualificationError("nvidia-smi emitted nonempty or unbounded stderr")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise QualificationError("nvidia-smi output is not valid XML") from exc
    if root.tag != "nvidia_smi_log":
        raise QualificationError("nvidia-smi XML has the wrong root element")
    attached_raw = root.findtext("attached_gpus")
    try:
        attached = int(str(attached_raw))
    except ValueError as exc:
        raise QualificationError("nvidia-smi XML has no valid attached GPU count") from exc
    gpus = root.findall("gpu")
    if attached < 1 or len(gpus) != attached:
        raise QualificationError("nvidia-smi did not expose the declared GPU set")
    identities: list[dict[str, str]] = []
    for gpu in gpus:
        uuid = (gpu.findtext("uuid") or "").strip()
        product_name = (gpu.findtext("product_name") or "").strip()
        if not uuid or not product_name or "H100" not in product_name:
            raise QualificationError("nvidia-smi GPU identity is incomplete")
        identities.append({"uuid": uuid, "product_name": product_name})
    if b"ERR!" in raw:
        raise QualificationError("nvidia-smi reported ERR! in the target container")
    return {
        "status": "PASS",
        "scope": "target-container",
        "command": ["nvidia-smi", "-q", "-x"],
        "attached_gpu_count": attached,
        "gpus": identities,
        "raw_xml_sha256": hashlib.sha256(raw).hexdigest(),
        "stderr_sha256": hashlib.sha256(raw_stderr).hexdigest(),
        "host_xid_check": {
            "status": "unavailable",
            "reason": HOST_XID_UNAVAILABLE_REASON,
        },
    }


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationError(f"{label} must be a positive integer")
    return value


def _controller_boundary(
    value: dict[str, Any], *, phase: str
) -> tuple[datetime, int]:
    if (
        value.get("schema") != CONTROLLER_CLOCK_BOUNDARY_SCHEMA
        or value.get("phase") != phase
        or set(value) != {"schema", "phase", "utc", "monotonic_ns"}
    ):
        raise QualificationError(f"{phase} controller clock boundary is malformed")
    return (
        _timestamp(value.get("utc"), f"{phase} controller UTC"),
        _positive_int(value.get("monotonic_ns"), f"{phase} controller monotonic_ns"),
    )


def _timens_offsets(value: Any, label: str) -> list[dict[str, int | str]]:
    if not isinstance(value, list) or len(value) != 2:
        raise QualificationError(f"{label} time-namespace offsets are malformed")
    rebuilt: list[dict[str, int | str]] = []
    for expected_clock, item in zip(("monotonic", "boottime"), value, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"clock", "seconds", "nanoseconds"}
            or item.get("clock") != expected_clock
            or isinstance(item.get("seconds"), bool)
            or not isinstance(item.get("seconds"), int)
            or isinstance(item.get("nanoseconds"), bool)
            or not isinstance(item.get("nanoseconds"), int)
            or not -999_999_999 <= item["nanoseconds"] <= 999_999_999
        ):
            raise QualificationError(f"{label} time-namespace offsets are malformed")
        rebuilt.append(
            {
                "clock": expected_clock,
                "seconds": item["seconds"],
                "nanoseconds": item["nanoseconds"],
            }
        )
    return rebuilt


def _node_clock_identity(
    value: Any, label: str, *, require_observation: bool
) -> tuple[dict[str, Any], int | None]:
    if not isinstance(value, dict):
        raise QualificationError(f"{label} node clock identity is malformed")
    expected_keys = {
        "schema",
        "clock_id",
        "boot_id",
        "clock_resolution_ns",
        "timens_offsets",
    }
    if require_observation:
        expected_keys.add("boottime_ns")
    if set(value) != expected_keys:
        raise QualificationError(f"{label} node clock identity is malformed")
    resolution_ns = _positive_int(
        value.get("clock_resolution_ns"), f"{label} CLOCK_BOOTTIME resolution"
    )
    boot_id = value.get("boot_id")
    if (
        value.get("schema") != SEMANTIC_NODE_BOOTTIME_SCHEMA
        or value.get("clock_id") != "CLOCK_BOOTTIME"
        or resolution_ns > MAX_CLOCK_RESOLUTION_NS
        or not isinstance(boot_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            boot_id,
        )
        is None
    ):
        raise QualificationError(f"{label} node clock identity is malformed")
    identity = {
        "schema": SEMANTIC_NODE_BOOTTIME_SCHEMA,
        "clock_id": "CLOCK_BOOTTIME",
        "boot_id": boot_id,
        "clock_resolution_ns": resolution_ns,
        "timens_offsets": _timens_offsets(value.get("timens_offsets"), label),
    }
    observation = (
        _positive_int(value.get("boottime_ns"), f"{label} CLOCK_BOOTTIME observation")
        if require_observation
        else None
    )
    return identity, observation


def _holder_receipt(
    pod: dict[str, Any], *, namespace: str, target_node: str
) -> dict[str, str]:
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    status = pod.get("status")
    if not all(isinstance(item, dict) for item in (metadata, spec, status)):
        raise QualificationError("boot-time anchor holder Pod is malformed")
    assert isinstance(metadata, dict)
    assert isinstance(spec, dict)
    assert isinstance(status, dict)
    name = metadata.get("name")
    uid = metadata.get("uid")
    containers = spec.get("containers")
    statuses = status.get("containerStatuses")
    conditions = status.get("conditions")
    if (
        pod.get("apiVersion") != "v1"
        or pod.get("kind") != "Pod"
        or metadata.get("namespace") != namespace
        or not isinstance(name, str)
        or not name
        or not isinstance(uid, str)
        or not uid
        or metadata.get("deletionTimestamp") is not None
        or spec.get("nodeName") != target_node
        or status.get("phase") != "Running"
        or not isinstance(containers, list)
        or len(containers) != 1
        or not isinstance(statuses, list)
        or len(statuses) != 1
        or not isinstance(conditions, list)
        or len(
            [
                item
                for item in conditions
                if isinstance(item, dict)
                and item.get("type") == "Ready"
                and item.get("status") == "True"
            ]
        )
        != 1
    ):
        raise QualificationError("boot-time anchor holder is not an exact Ready Pod")
    container = containers[0]
    container_status = statuses[0]
    if not isinstance(container, dict) or not isinstance(container_status, dict):
        raise QualificationError("boot-time anchor holder container is malformed")
    resources = container.get("resources", {})
    if not isinstance(resources, dict):
        raise QualificationError("boot-time anchor holder resources are malformed")
    for resource_kind in ("requests", "limits"):
        values = resources.get(resource_kind, {})
        if not isinstance(values, dict) or any(
            key in values for key in ("nvidia.com/gpu", "nvidia.com/mig")
        ):
            raise QualificationError("boot-time anchor holder must request zero GPUs")
    if (
        container.get("name") != "holder"
        or container.get("image") != BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or container_status.get("name") != "holder"
        or not isinstance(container_status.get("image"), str)
        or not container_status["image"]
        or container_status.get("imageID") != BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or container_status.get("ready") is not True
        or isinstance(container_status.get("restartCount"), bool)
        or container_status.get("restartCount") != 0
        or not isinstance(container_status.get("state", {}).get("running"), dict)
    ):
        raise QualificationError("boot-time anchor holder image/status is not exact")
    return {
        "namespace": namespace,
        "name": name,
        "uid": uid,
        "node": target_node,
        "container": "holder",
        "image": BOOT_TIME_ANCHOR_HOLDER_IMAGE,
    }


def _boottime_value(value: Any, label: str) -> int:
    return _positive_int(value, f"{label} CLOCK_BOOTTIME ns")


def _reproduced_seconds(start_ns: int, end_ns: int, recorded: Any, label: str) -> float:
    if end_ns < start_ns:
        raise QualificationError(f"{label} CLOCK_BOOTTIME ordering is invalid")
    rebuilt = round((end_ns - start_ns) / 1_000_000_000, 6)
    if (
        isinstance(recorded, bool)
        or not isinstance(recorded, (int, float))
        or not math.isfinite(float(recorded))
        or float(recorded) != rebuilt
    ):
        raise QualificationError(f"{label} elapsed time is not reproduced by CLOCK_BOOTTIME")
    return rebuilt


def _upward_seconds(nanoseconds: int) -> float:
    return math.ceil(nanoseconds / 1_000) / 1_000_000


def _boot_time_alignment(
    admission_boundary: dict[str, Any],
    target_submit_clock: dict[str, Any],
    boot_time_anchor: dict[str, Any],
    anchor_holder: dict[str, Any],
    semantic_summary: dict[str, Any],
    *,
    namespace: str,
    target_node: str,
    target_submit_at: str,
) -> dict[str, Any]:
    admission_utc, admission_mono = _controller_boundary(
        admission_boundary, phase="cohort-admission"
    )
    t0_utc, t0_mono = _controller_boundary(
        target_submit_clock, phase="target-submit"
    )
    if target_submit_clock.get("utc") != target_submit_at:
        raise QualificationError("target-submit boundary does not match primary T0")
    holder = _holder_receipt(
        anchor_holder, namespace=namespace, target_node=target_node
    )
    before = boot_time_anchor.get("controller_before")
    after = boot_time_anchor.get("controller_after")
    if (
        boot_time_anchor.get("schema") != BOOT_TIME_ANCHOR_SCHEMA
        or boot_time_anchor.get("phase") != "pre-t0-anchor"
        or boot_time_anchor.get("sampled_pod_name") != holder["name"]
        or boot_time_anchor.get("sampled_pod_uid") != holder["uid"]
        or boot_time_anchor.get("target_node") != target_node
        or boot_time_anchor.get("sampled_container") != "holder"
        or boot_time_anchor.get("expected_holder_image")
        != BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or not isinstance(before, dict)
        or not isinstance(after, dict)
    ):
        raise QualificationError("boot-time anchor identity is inconsistent")
    before_utc = _timestamp(before.get("utc"), "anchor controller before UTC")
    after_utc = _timestamp(after.get("utc"), "anchor controller after UTC")
    before_mono = _positive_int(
        before.get("monotonic_ns"), "anchor controller before monotonic_ns"
    )
    after_mono = _positive_int(
        after.get("monotonic_ns"), "anchor controller after monotonic_ns"
    )
    if set(before) != {"utc", "monotonic_ns"} or set(after) != {
        "utc",
        "monotonic_ns",
    }:
        raise QualificationError("boot-time anchor controller bracket is malformed")
    maximum_delta_ns = int(
        MAX_ANCHOR_TO_T0_CONTROLLER_MONOTONIC_SECONDS * 1_000_000_000
    )
    if not (
        admission_mono <= before_mono <= after_mono <= t0_mono
        and t0_mono - before_mono <= maximum_delta_ns
    ):
        raise QualificationError(
            "admission/anchor/T0 controller monotonic contract is invalid"
        )

    anchor_identity, anchor_boot = _node_clock_identity(
        boot_time_anchor.get("node_observed"),
        "anchor",
        require_observation=True,
    )
    assert anchor_boot is not None
    if semantic_summary.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
        raise QualificationError("semantic summary does not use the v3 timing contract")
    semantic_identity, unused_observation = _node_clock_identity(
        semantic_summary.get("node_clock"),
        "semantic probe",
        require_observation=False,
    )
    assert unused_observation is None
    if semantic_identity != anchor_identity:
        raise QualificationError(
            "semantic probe does not share the anchor node boot/time namespace"
        )

    cases = semantic_summary.get("cases")
    ready = semantic_summary.get("ready_wait")
    if not isinstance(cases, list) or len(cases) != 2 or not isinstance(ready, dict):
        raise QualificationError("semantic BOOTTIME timeline is incomplete")
    summary_start = _boottime_value(
        semantic_summary.get("started_boottime_ns"), "semantic validation start"
    )
    ready_start = _boottime_value(ready.get("started_boottime_ns"), "readiness start")
    ready_dispatch = _boottime_value(
        ready.get("request_dispatched_boottime_ns"), "readiness dispatch"
    )
    ready_body = _boottime_value(
        ready.get("response_body_received_boottime_ns"), "readiness body completion"
    )
    ready_finish = _boottime_value(
        ready.get("finished_boottime_ns"), "readiness finish"
    )
    call_dispatch: list[int] = []
    call_body: list[int] = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or case.get("index") != index:
            raise QualificationError("semantic BOOTTIME case timeline is malformed")
        dispatched = _boottime_value(
            case.get("request_dispatched_boottime_ns"), f"call{index} dispatch"
        )
        body = _boottime_value(
            case.get("response_body_received_boottime_ns"), f"call{index} body completion"
        )
        _reproduced_seconds(dispatched, body, case.get("elapsed_seconds"), f"call{index}")
        call_dispatch.append(dispatched)
        call_body.append(body)
    summary_finish = _boottime_value(
        semantic_summary.get("validation_finished_boottime_ns"),
        "semantic validation finish",
    )
    ordered = [
        anchor_boot,
        summary_start,
        ready_start,
        ready_dispatch,
        ready_body,
        ready_finish,
        call_dispatch[0],
        call_body[0],
        call_dispatch[1],
        call_body[1],
        summary_finish,
    ]
    if anchor_boot >= summary_start or ordered != sorted(ordered):
        raise QualificationError("semantic CLOCK_BOOTTIME events are not monotonic")
    if ready.get("status") != "PASS":
        raise QualificationError("semantic readiness receipt is not PASS")
    _reproduced_seconds(
        ready_start, ready_finish, ready.get("elapsed_seconds"), "readiness wait"
    )
    _reproduced_seconds(
        summary_start,
        summary_finish,
        semantic_summary.get("validation_total_elapsed_seconds"),
        "semantic validation",
    )
    if semantic_summary.get("total_elapsed_seconds") != semantic_summary.get(
        "validation_total_elapsed_seconds"
    ):
        raise QualificationError("semantic total elapsed fields disagree")

    resolution_ns = anchor_identity["clock_resolution_ns"]
    assert isinstance(resolution_ns, int)
    event_values = {
        "http_ready_complete_body": ready_body,
        "first_semantic_response_complete_body": call_body[0],
        "two_semantic_responses_complete_body": call_body[1],
    }
    conservative: dict[str, dict[str, int | float]] = {}
    for label, event_boot in event_values.items():
        exact_delta_ns = event_boot - anchor_boot
        upper_ns = exact_delta_ns + 2 * resolution_ns
        if exact_delta_ns < 0:
            raise QualificationError("semantic event precedes BOOTTIME anchor")
        conservative[label] = {
            "event_boottime_ns": event_boot,
            "anchor_boottime_ns": anchor_boot,
            "event_minus_anchor_ns": exact_delta_ns,
            "resolution_padding_ns": 2 * resolution_ns,
            "upper_bound_ns": upper_ns,
            "upper_bound_seconds": _upward_seconds(upper_ns),
        }

    return {
        "status": "PASS",
        "method": "pre-t0-ready-holder-clock-boottime-anchor/v1",
        "scope": "controller-monotonic-admission-to-t0-and-node-boottime-events",
        "worker_and_probe_must_share_target_node": True,
        "holder": holder,
        "node_clock_identity": anchor_identity,
        "anchor": {
            "node_boottime_ns": anchor_boot,
            "controller_before": before,
            "controller_after": after,
        },
        "controller_boundaries": {
            "admission": admission_boundary,
            "target_submit_t0": target_submit_clock,
            "admission_to_anchor_before_ns": before_mono - admission_mono,
            "anchor_bracket_ns": after_mono - before_mono,
            "anchor_before_to_t0_ns": t0_mono - before_mono,
            "maximum_anchor_before_to_t0_ns": maximum_delta_ns,
            "controller_after_not_later_than_t0": True,
            "wall_clock_ordered_diagnostic": (
                admission_utc <= before_utc <= after_utc <= t0_utc
            ),
        },
        "semantic_boottime": {
            "validation_started_ns": summary_start,
            "readiness_started_ns": ready_start,
            "readiness_dispatched_ns": ready_dispatch,
            "readiness_body_received_ns": ready_body,
            "readiness_finished_ns": ready_finish,
            "call1_dispatched_ns": call_dispatch[0],
            "call1_body_received_ns": call_body[0],
            "call2_dispatched_ns": call_dispatch[1],
            "call2_body_received_ns": call_body[1],
            "validation_finished_ns": summary_finish,
        },
        "conservative_upper_bounds": conservative,
    }


def _call2_completion(semantic_summary: dict[str, Any]) -> datetime:
    cases = semantic_summary.get("cases")
    if (
        semantic_summary.get("status") != "PASS"
        or semantic_summary.get("passed_case_count") != 2
        or not isinstance(cases, list)
        or len(cases) != 2
        or [item.get("index") for item in cases if isinstance(item, dict)] != [1, 2]
        or not all(
            isinstance(item, dict) and item.get("status") == "PASS" for item in cases
        )
    ):
        raise QualificationError("semantic summary does not prove two successful calls")
    return _timestamp(cases[1].get("response_received_at"), "call2 completion")


def build_receipt(
    *,
    model: str,
    run_id: str,
    namespace: str,
    target_name: str,
    target_container: str,
    expected_image: str,
    target_submit_at: str,
    target_create_response_at: str,
    target_create_response: dict[str, Any],
    target: dict[str, Any],
    target_events: dict[str, Any],
    worker_pod: dict[str, Any],
    worker_receipt: dict[str, Any],
    worker_container: str,
    probe_pod: dict[str, Any],
    probe_container: str,
    semantic_summary: dict[str, Any],
    gpu_health_xml: Path,
    gpu_health_stderr: Path,
    admission_boundary: dict[str, Any],
    target_submit_clock: dict[str, Any],
    boot_time_anchor: dict[str, Any],
    anchor_holder: dict[str, Any],
    source_paths: dict[str, Path] | None = None,
) -> dict[str, Any]:
    if model not in {"openfold2", "boltz2"}:
        raise QualificationError("model must be openfold2 or boltz2")
    if not run_id or not namespace or not target_name or not expected_image:
        raise QualificationError("run identity fields must be nonempty")

    submit = _timestamp(target_submit_at, "target submit")
    response = _timestamp(target_create_response_at, "target create response")
    call2_completed = _call2_completion(semantic_summary)
    api_rtt = (response - submit).total_seconds()
    if not math.isfinite(api_rtt) or api_rtt < 0:
        raise QualificationError("target create response precedes request dispatch")

    response_metadata = target_create_response.get("metadata")
    target_metadata = target.get("metadata")
    if not isinstance(response_metadata, dict) or not isinstance(target_metadata, dict):
        raise QualificationError("target/create-response metadata is missing")
    target_uid = response_metadata.get("uid")
    if (
        target_create_response.get("apiVersion") != "v1"
        or target_create_response.get("kind") != "Pod"
        or response_metadata.get("name") != target_name
        or response_metadata.get("namespace") != namespace
        or not isinstance(target_uid, str)
        or not target_uid
        or target.get("apiVersion") != "v1"
        or target.get("kind") != "Pod"
        or target_metadata.get("name") != target_name
        or target_metadata.get("namespace") != namespace
        or target_metadata.get("uid") != target_uid
        or target_metadata.get("creationTimestamp")
        != response_metadata.get("creationTimestamp")
    ):
        raise QualificationError("API create response does not identify the final target Pod")
    created = _timestamp(response_metadata.get("creationTimestamp"), "server creation")
    if (submit - created).total_seconds() >= 1 or created > response:
        raise QualificationError(
            "coarse server creation timestamp is outside the client request/response bracket"
        )

    annotations = target_metadata.get("annotations", {})
    pod_spec_sha256 = (
        annotations.get("archvteams.nebius.ai/target-pod-spec-sha256")
        if isinstance(annotations, dict)
        else None
    )
    if (
        not isinstance(pod_spec_sha256, str)
        or len(pod_spec_sha256) != 64
        or any(character not in "0123456789abcdef" for character in pod_spec_sha256)
    ):
        raise QualificationError("target PodSpec digest annotation is missing")

    pod_health = {
        "target": _container_health(
            target,
            expected_name=target_container,
            expected_phase="Running",
            label="target Pod",
        ),
        "worker": _container_health(
            worker_pod,
            expected_name=worker_container,
            expected_phase="Succeeded",
            label="worker Pod",
        ),
        "probe": _container_health(
            probe_pod,
            expected_name=probe_container,
            expected_phase="Succeeded",
            label="probe Pod",
        ),
    }
    target_node = target.get("spec", {}).get("nodeName")
    if (
        not isinstance(target_node, str)
        or not target_node
        or worker_pod.get("spec", {}).get("nodeName") != target_node
        or probe_pod.get("spec", {}).get("nodeName") != target_node
    ):
        raise QualificationError(
            "target, worker, and semantic probe are not bound to one node clock"
        )
    target_started, worker_finished, target_ready = _startup_probe_bounds(
        target,
        worker_pod,
        target_container=target_container,
        worker_container=worker_container,
    )
    restore_completed = _restore_completion(
        worker_receipt,
        run_id=run_id,
        namespace=namespace,
        target_name=target_name,
        target_uid=target_uid,
        target_node=target_node,
        target_pod_spec_sha256=pod_spec_sha256,
        expected_image=expected_image,
    )
    event_receipt = _event_receipt(
        target_events,
        target_uid=target_uid,
        target_name=target_name,
        target_namespace=namespace,
        target_container=target_container,
        target_node=target_node,
        target_started=target_started,
        worker_finished=worker_finished,
        target_ready=target_ready,
        restore_completed=restore_completed,
        expected_image=expected_image,
    )
    boot_time_alignment = _boot_time_alignment(
        admission_boundary,
        target_submit_clock,
        boot_time_anchor,
        anchor_holder,
        semantic_summary,
        namespace=namespace,
        target_node=target_node,
        target_submit_at=target_submit_at,
    )
    gpu = _gpu_receipt(gpu_health_xml, gpu_health_stderr)

    source_hashes: dict[str, str] = {}
    for label, path in (source_paths or {}).items():
        source_hashes[label] = _sha256(path, label)
    source_hashes["target_nvidia_smi_xml"] = _sha256(
        gpu_health_xml, "target nvidia-smi XML"
    )
    source_hashes["target_nvidia_smi_stderr"] = _sha256(
        gpu_health_stderr, "target nvidia-smi stderr"
    )

    return {
        "schema": SCHEMA,
        "status": "PASS",
        "model": model,
        "run_id": run_id,
        "target": {
            "namespace": namespace,
            "name": target_name,
            "uid": target_uid,
            "pod_spec_sha256": pod_spec_sha256,
            "image": expected_image,
        },
        "timing_boundaries": {
            "primary": {
                "label": PRE_DISPATCH_BOUNDARY,
                "timestamp": target_submit_at,
                "controller_monotonic_ns": target_submit_clock["monotonic_ns"],
                "conservative_relative_to_api_acceptance": True,
            },
            "acceptance_response_proxy": {
                "label": ACCEPTANCE_RESPONSE_PROXY,
                "timestamp": target_create_response_at,
                "is_exact_server_acceptance": False,
                "client_observed_api_round_trip_seconds": round(api_rtt, 6),
                "server_creation_timestamp": response_metadata["creationTimestamp"],
            },
        },
        "warm_instance": {
            "target_image_already_present_before_t0": True,
            "target_image_pull_or_download_after_t0": False,
            "target_events": event_receipt,
        },
        "pod_health": pod_health,
        "boot_time_alignment": boot_time_alignment,
        "gpu_health": gpu,
        "source_sha256": dict(sorted(source_hashes.items())),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("openfold2", "boltz2"), required=True)
    for name in (
        "run-id",
        "namespace",
        "target-name",
        "target-container",
        "expected-image",
        "worker-container",
        "probe-container",
    ):
        parser.add_argument(f"--{name}", required=True)
    for name in (
        "target-submit-at",
        "target-create-response-at",
        "target-create-response",
        "target-pod",
        "target-events",
        "worker-pod",
        "worker-receipt",
        "probe-pod",
        "semantic-summary",
        "gpu-health-xml",
        "gpu-health-stderr",
        "admission-boundary",
        "target-submit-clock",
        "boot-time-anchor",
        "anchor-holder",
        "capture-agent-absence",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    source_paths = {
        "target_create_response": args.target_create_response,
        "target_pod": args.target_pod,
        "target_events": args.target_events,
        "worker_pod": args.worker_pod,
        "worker_receipt": args.worker_receipt,
        "probe_pod": args.probe_pod,
        "semantic_summary": args.semantic_summary,
        "admission_boundary": args.admission_boundary,
        "target_submit_clock": args.target_submit_clock,
        "boot_time_anchor": args.boot_time_anchor,
        "anchor_holder": args.anchor_holder,
        "capture_agent_absence": args.capture_agent_absence,
    }
    try:
        receipt = build_receipt(
            model=args.model,
            run_id=args.run_id,
            namespace=args.namespace,
            target_name=args.target_name,
            target_container=args.target_container,
            expected_image=args.expected_image,
            target_submit_at=_read_timestamp(args.target_submit_at, "target submit"),
            target_create_response_at=_read_timestamp(
                args.target_create_response_at, "target create response"
            ),
            target_create_response=_load_json(
                args.target_create_response, "target create response"
            ),
            target=_load_json(args.target_pod, "target Pod"),
            target_events=_load_json(args.target_events, "target Events"),
            worker_pod=_load_json(args.worker_pod, "worker Pod"),
            worker_receipt=_load_json(args.worker_receipt, "worker restore receipt"),
            worker_container=args.worker_container,
            probe_pod=_load_json(args.probe_pod, "probe Pod"),
            probe_container=args.probe_container,
            semantic_summary=_load_json(args.semantic_summary, "semantic summary"),
            gpu_health_xml=args.gpu_health_xml,
            gpu_health_stderr=args.gpu_health_stderr,
            admission_boundary=_load_json(
                args.admission_boundary, "cohort admission boundary"
            ),
            target_submit_clock=_load_json(
                args.target_submit_clock, "target-submit clock boundary"
            ),
            boot_time_anchor=_load_json(
                args.boot_time_anchor, "node boot-time anchor"
            ),
            anchor_holder=_load_json(args.anchor_holder, "anchor holder Pod"),
            source_paths=source_paths,
        )
    except QualificationError as exc:
        print(f"qualification-receipt: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
