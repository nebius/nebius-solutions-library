#!/usr/bin/env python3
"""Validate scale-zero, semantic, storage, and restoration evidence for one run."""

from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = "archvteams.nebius.ai/openfold2-newnode-lifecycle-evidence/v1"
NODE_GROUP_ID = os.environ.get("OPENFOLD2_NODE_GROUP_ID", "mk8snodegroup-example")
HOLDER_NODE = os.environ.get("OPENFOLD2_HOLDER_NODE", "gpu-node-b.example.invalid")
HOLDER_NAME = "of2-artifact-holder-t12"
ARTIFACT_PVC = "mlspec-archvteams-2407-ckpt-m3"
CACHE_PVC = "openfold2-nim-cache"
ARTIFACT_PV = os.environ.get("OPENFOLD2_ARTIFACT_PV", "pvc-example-artifact")
CACHE_PV = os.environ.get("OPENFOLD2_CACHE_PV", "pvc-example-cache")


class LifecycleError(ValueError):
    """The retained lifecycle evidence is incomplete or inconsistent."""


def _json(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(f"missing regular evidence file: {name}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"invalid evidence file {name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"evidence file is not an object: {name}")
    return value


def _text(root: Path, name: str) -> str:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(f"missing regular evidence file: {name}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LifecycleError(f"cannot read evidence file {name}") from exc
    if not value:
        raise LifecycleError(f"empty evidence file: {name}")
    return value


def _time(value: str, label: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise LifecycleError(f"invalid timestamp for {label}") from exc
    if parsed.tzinfo is None:
        raise LifecycleError(f"timestamp lacks timezone for {label}")
    return parsed.astimezone(UTC)


def _seconds(start: str, finish: str, label: str) -> float:
    duration = (_time(finish, label) - _time(start, label)).total_seconds()
    if duration < 0:
        raise LifecycleError(f"reversed timestamps for {label}")
    return round(duration, 6)


def _group_count(
    group: dict[str, Any], expected: int, label: str, expected_ready: int | None = None
) -> None:
    try:
        identity = group["metadata"]["id"]
        fixed = int(group["spec"]["fixed_node_count"])
        target = int(group["status"].get("target_node_count", 0))
        count = int(group["status"].get("node_count", 0))
        ready = int(group["status"].get("ready_node_count", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError(f"malformed {label} node-group evidence") from exc
    if expected_ready is None:
        expected_ready = expected
    if identity != NODE_GROUP_ID or (fixed, target, count, ready) != (
        expected,
        expected,
        expected,
        expected_ready,
    ):
        raise LifecycleError(f"{label} node-group count is not exactly {expected}")


def _attachments(value: dict[str, Any], node: str | None, expected_count: int, label: str) -> None:
    items = value.get("items")
    if not isinstance(items, list) or len(items) != expected_count:
        raise LifecycleError(f"{label} has the wrong attachment count")
    if expected_count == 0:
        return
    if any(not isinstance(item, dict) for item in items):
        raise LifecycleError(f"{label} contains a malformed attachment")
    pvs = [item.get("spec", {}).get("source", {}).get("persistentVolumeName") for item in items]
    if any(not isinstance(pv, str) for pv in pvs):
        raise LifecycleError(f"{label} contains a malformed persistent-volume name")
    pvs.sort()
    if pvs != sorted([ARTIFACT_PV, CACHE_PV]):
        raise LifecycleError(f"{label} does not contain the exact two PVC volumes")
    if any(
        item.get("spec", {}).get("nodeName") != node
        or item.get("status", {}).get("attached") is not True
        for item in items
    ):
        raise LifecycleError(f"{label} volumes are not attached to {node}")


def _holder(value: dict[str, Any]) -> str:
    metadata = value.get("metadata")
    spec = value.get("spec")
    status = value.get("status")
    if not all(isinstance(item, dict) for item in (metadata, spec, status)):
        raise LifecycleError("restored holder object is malformed")
    containers = status.get("containerStatuses")
    volumes = spec.get("volumes")
    if (
        metadata.get("name") != HOLDER_NAME
        or metadata.get("deletionTimestamp") is not None
        or metadata.get("ownerReferences") is not None
        or spec.get("nodeName") != HOLDER_NODE
        or status.get("phase") != "Running"
        or not isinstance(containers, list)
        or not containers
        or any(not isinstance(item, dict) or item.get("ready") is not True for item in containers)
        or not isinstance(volumes, list)
    ):
        raise LifecycleError("restored holder is not stable and Ready on its fixed node")
    claims = {
        item.get("persistentVolumeClaim", {}).get("claimName")
        for item in volumes
        if isinstance(item, dict)
        and item.get("persistentVolumeClaim", {}).get("readOnly") is True
    }
    if not {ARTIFACT_PVC, CACHE_PVC}.issubset(claims):
        raise LifecycleError("restored holder does not read-only mount both exact PVCs")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise LifecycleError("restored holder UID is missing")
    return uid


def build(
    root: Path, main_status: int, cleanup_failed: bool, holder_released: bool
) -> dict[str, Any]:
    original = _json(root, "node-group-original.json")
    final = _json(root, "node-group-final.json")
    starting = _json(root, "starting-state.json")
    starting_mode = starting.get("mode")
    if starting_mode not in {"healthy", "retiring-unknown"}:
        raise LifecycleError("starting-state mode is unsupported")
    try:
        original_count = int(original.get("spec", {}).get("fixed_node_count", -1))
    except (TypeError, ValueError) as exc:
        raise LifecycleError("original desired count is malformed") from exc
    if original_count != 1:
        raise LifecycleError("benchmark requires an original desired count of exactly 1")
    _group_count(
        original,
        original_count,
        "original",
        expected_ready=1 if starting_mode == "healthy" else 0,
    )
    _group_count(final, original_count, "final")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": root.name,
        "main_exit_status": main_status,
        "cleanup_passed": not cleanup_failed,
        "holder_released": holder_released,
        "starting_mode": starting_mode,
        "original_desired_count": original_count,
        "restored_desired_count": int(final["spec"]["fixed_node_count"]),
        "raw_evidence_directory": str(root),
    }
    if cleanup_failed:
        result["status"] = "CLEANUP_FAILED"
        return result
    resources_after = _json(root, "resources-after-cleanup.json").get("items")
    if not isinstance(resources_after, list) or resources_after:
        raise LifecycleError("run-labelled resources remain after cleanup")
    if holder_released:
        _attachments(_json(root, "volumeattachments-before.json"), HOLDER_NODE, 2, "initial holder")
        _attachments(
            _json(root, "volumeattachments-after-run-cleanup.json"), None, 0, "post-run detach"
        )
        _attachments(
            _json(root, "volumeattachments-holder-restored.json"), HOLDER_NODE, 2, "restored holder"
        )
        restored_uid = _holder(_json(root, "holder-restored.json"))
        confirmed_uid = _holder(_json(root, "holder-restored-confirmed.json"))
        if restored_uid != confirmed_uid:
            raise LifecycleError("restored holder UID changed during stability confirmation")
        _attachments(
            _json(root, "volumeattachments-holder-restored-confirmed.json"),
            HOLDER_NODE,
            2,
            "confirmed restored holder",
        )
    if main_status != 0:
        result["status"] = "RUN_FAILED_CLEANUP_PASS"
        return result
    if not holder_released:
        raise LifecycleError("successful benchmark did not release and restore the holder")

    zero = _json(root, "node-group-zero.json")
    ready = _json(root, "node-group-new-ready.json")
    _group_count(zero, 0, "zero")
    _group_count(ready, 1, "new-ready")
    admission = _json(root, "node-admission.json")
    previous = admission.get("previous_node", {})
    node = admission.get("node", {})
    new_node = node.get("name")
    if (
        admission.get("node_group_id") != NODE_GROUP_ID
        or not isinstance(new_node, str)
        or new_node == previous.get("name")
        or node.get("uid") == previous.get("uid")
    ):
        raise LifecycleError("new-node admission is not a distinct exact group member")
    starting_node = starting.get("node", {})
    if (
        previous.get("name") != starting_node.get("name")
        or previous.get("uid") != starting_node.get("uid")
    ):
        raise LifecycleError("new-node admission predecessor differs from starting-state evidence")
    if starting_mode == "retiring-unknown":
        retired = _json(root, "retiring-predecessor-removed.json")
        if (
            retired.get("name") != previous.get("name")
            or retired.get("uid") != previous.get("uid")
            or retired.get("absent") is not True
        ):
            raise LifecycleError("retiring predecessor removal evidence is inconsistent")

    _attachments(
        _json(root, "volumeattachments-prepared-detached.json"), None, 0, "prepared detach"
    )
    _attachments(
        _json(root, "volumeattachments-target-attached.json"), new_node, 2, "target attach"
    )
    scale_at = _text(root, "scale-up-demand-at.txt")
    request_returned_at = _text(root, "scale-up-request-returned-at.txt")
    node_admitted_at = _text(root, "new-node-admitted-at.txt")
    criu_ready_at = _text(root, "criu-agent-ready-at.txt")
    placeholder_at = _text(root, "target-placeholder-running-at.txt")
    passed_at = _text(root, "benchmark-passed-at.txt")
    cleanup_finished_at = _text(root, "cleanup-finished-at.txt")
    canary = _json(root, "canary-evidence.json")
    if (
        canary.get("status") != "PASS"
        or canary.get("request_count") != 2
        or canary.get("semantic_pass_count") != 2
        or canary.get("demand_at") != scale_at
    ):
        raise LifecycleError("canary evidence is not the exact scale-request-bound two-call PASS")
    ordered = [
        _time(scale_at, "scale request"),
        _time(request_returned_at, "scale request return"),
        _time(node_admitted_at, "node admission"),
        _time(criu_ready_at, "CRIU agent readiness"),
        _time(placeholder_at, "placeholder running"),
        _time(passed_at, "benchmark pass"),
        _time(cleanup_finished_at, "cleanup finish"),
    ]
    if ordered != sorted(ordered):
        raise LifecycleError(
            "lifecycle timestamps are not scale <= request-return <= admitted <= CRIU-ready <= placeholder <= pass <= cleanup"
        )
    result.update(
        {
            "status": "PASS",
            "new_node": {
                "name": new_node,
                "uid": node.get("uid"),
                "previous_name": previous.get("name"),
                "previous_uid": previous.get("uid"),
            },
            "storage_transitions": [
                "holder-attached",
                "prepared-detached",
                "target-attached",
                "post-run-detached",
                "holder-restored",
            ],
            "semantic": {
                "request_count": 2,
                "pass_count": 2,
                "demand_to_two_semantic_responses": canary["timings_seconds"][
                    "demand_to_two_semantic_responses"
                ],
            },
            "timings_seconds": {
                "scale_request_to_node_admitted": _seconds(
                    scale_at, node_admitted_at, "node admission"
                ),
                "scale_request_to_criu_agent_ready": _seconds(
                    scale_at, criu_ready_at, "CRIU agent readiness"
                ),
                "scale_request_to_placeholder_running_observed": _seconds(
                    scale_at, placeholder_at, "placeholder running"
                ),
                "scale_request_to_benchmark_pass_recorded": _seconds(
                    scale_at, passed_at, "benchmark pass"
                ),
                "scale_request_to_cleanup_finished": _seconds(
                    scale_at, cleanup_finished_at, "cleanup finish"
                ),
            },
        }
    )
    return result


def _write(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise LifecycleError(f"output already exists: {path}")
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(data)
        handle.flush()
        os.fsync(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--main-status", type=int, required=True)
    parser.add_argument("--cleanup-failed", type=int, choices=(0, 1), required=True)
    parser.add_argument("--holder-released", type=int, choices=(0, 1), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(
            args.run_dir.resolve(strict=True),
            args.main_status,
            bool(args.cleanup_failed),
            bool(args.holder_released),
        )
        _write(args.output, result)
    except LifecycleError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": result["status"], "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
