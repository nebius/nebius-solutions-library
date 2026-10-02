#!/usr/bin/env python3
"""Fail-closed aggregation for the exact OpenFold3 response-boundary cohort."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


CHECKPOINT_ID = "openfold3-native-f7-v2-buffered"
ARTIFACT_VERSION = "1"
ARTIFACT_FILE_COUNT = 148
ARTIFACT_BYTES = 9_263_246_107
ARTIFACT_MANIFEST_SHA256 = (
    "5df221e0736a4c6f369781ea0dbc7c36783c26d3f35dcd874b4ced8f5f9e009f"
)
ARTIFACT_TREE_SHA256 = (
    "f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2"
)
PREWARM_SOURCE_SHA256 = (
    "94fdabcae49fcde0fc5a3bddb2b4b75428995b1758015a98e0480ba9a1d36c82"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AggregateError(ValueError):
    """The cohort is incomplete or violates its frozen measurement contract."""


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AggregateError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregateError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise AggregateError(f"{label} must be a JSON object")
    return value


def _stamp(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise AggregateError(f"{label} must be a timestamp")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateError(f"{label} must be an RFC3339 timestamp") from exc
    if result.tzinfo is None:
        raise AggregateError(f"{label} must include a timezone")
    return result.astimezone(dt.UTC)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AggregateError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise AggregateError(f"{label} must be finite and non-negative")
    return result


def _metric(values: list[float]) -> dict[str, Any]:
    return {
        "values": values,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def _validate_setup(
    root: Path,
    node: str,
    expected_images: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    preflight = _load(root / "cohort-preflight" / "receipt.json", "cohort preflight")
    prewarm = _load(root / "artifact-prewarm-receipt.json", "artifact prewarm")
    images = _load(root / "image-residency-receipt.json", "image residency")
    if (
        preflight.get("schema")
        != "archvteams.nebius.ai/openfold3-cohort-preflight/v1"
        or preflight.get("status") != "PASS"
        or preflight.get("node") != node
        or preflight.get("active_gpu_requests_on_node") != 0
        or preflight.get("attached_volume_count") != 2
        or preflight.get("worker_request_mcpu") not in {500, 1000}
        or _number(
            preflight.get("candidate_headroom_after_target_probe_worker_mcpu"),
            "candidate CPU headroom",
        )
        < _number(preflight.get("required_candidate_headroom_mcpu"), "required CPU headroom")
    ):
        raise AggregateError("cohort preflight is not the exact capacity/storage PASS")
    if (
        prewarm.get("schema")
        != "archvteams.nebius.ai/openfold3-artifact-prewarm/v1"
        or prewarm.get("status") != "PASS"
        or prewarm.get("checkpoint_id") != CHECKPOINT_ID
        or prewarm.get("artifact_version") != ARTIFACT_VERSION
        or prewarm.get("image_io_mode") != "buffered"
        or prewarm.get("regular_file_count") != ARTIFACT_FILE_COUNT
        or prewarm.get("regular_bytes_read") != ARTIFACT_BYTES
        or prewarm.get("manifest_sha256") != ARTIFACT_MANIFEST_SHA256
        or prewarm.get("tree_sha256") != ARTIFACT_TREE_SHA256
        or prewarm.get("prewarm_source_sha256") != PREWARM_SOURCE_SHA256
        or prewarm.get("prewarm_outside_t0") is not True
        or prewarm.get("source_node") != node
        or not isinstance(prewarm.get("holder_uid"), str)
        or not prewarm["holder_uid"]
    ):
        raise AggregateError("artifact prewarm is not the exact full-read PASS")
    _number(prewarm.get("full_read_elapsed_seconds"), "full-read elapsed time")
    if (
        images.get("schema")
        != "archvteams.nebius.ai/openfold3-image-residency/v1"
        or images.get("status") != "PASS"
        or images.get("node") != node
        or images.get("preloaded_outside_t0") is not True
        or images.get("preloader_absent_before_t0") is not True
        or images.get("images") != expected_images
        or not isinstance(images.get("image_ids"), dict)
        or set(images["image_ids"]) != set(expected_images)
        or any(
            not isinstance(value, str) or not value
            for value in images["image_ids"].values()
        )
    ):
        raise AggregateError("image residency is not the exact pre-T0 PASS")
    return preflight, prewarm, images


def aggregate(
    root: Path,
    batch_id: str,
    node: str,
    target_image: str,
    worker_image: str,
    probe_image: str,
) -> dict[str, Any]:
    if (
        len(batch_id) > 25
        or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", batch_id) is None
    ):
        raise AggregateError("batch ID is invalid")
    expected_images = {
        "target": target_image,
        "restore-worker": worker_image,
        "semantic-probe": probe_image,
    }
    preflight, prewarm, images = _validate_setup(root, node, expected_images)
    setup_finished = max(
        _stamp(prewarm.get("completed_at"), "prewarm completion"),
        _stamp(images.get("verified_at"), "image verification"),
    )

    metrics: dict[str, list[float]] = {
        "demand_to_http_ready": [],
        "demand_to_kubernetes_ready": [],
        "semantic_request_1": [],
        "semantic_request_2": [],
        "demand_to_two_semantic_responses": [],
        "worker_restore": [],
    }
    trials: list[dict[str, Any]] = []
    target_uids: set[str] = set()
    for index in (1, 2, 3):
        run_id = f"{batch_id}-r{index}"
        run_dir = root / "runs" / run_id
        evidence = _load(run_dir / "canary-evidence.json", f"{run_id} evidence")
        summary = _load(run_dir / "semantic-summary.json", f"{run_id} semantic summary")
        cleanup = _load(run_dir / "cleanup-receipt.json", f"{run_id} cleanup")
        events = _load(run_dir / "image-events-receipt.json", f"{run_id} events")
        if (
            evidence.get("schema")
            != "archvteams.nebius.ai/openfold3-production-canary-evidence/v1"
            or evidence.get("status") != "PASS"
            or evidence.get("run_id") != run_id
            or evidence.get("request_count") != 2
            or evidence.get("semantic_pass_count") != 2
            or evidence.get("response_timing_contract")
            != "request-dispatch-to-complete-http-body/v1"
            or evidence.get("t0_source") != "target-submit-at.txt"
            or evidence.get("t0_at") != evidence.get("demand_at")
            or evidence.get("artifact", {}).get("checkpoint_id") != CHECKPOINT_ID
            or evidence.get("artifact", {}).get("image_io_mode") != "buffered"
        ):
            raise AggregateError(f"{run_id} is not an exact two-response PASS")
        cases = summary.get("cases")
        expected_ids = [f"{run_id}-semantic-a", f"{run_id}-semantic-b"]
        if (
            summary.get("status") != "PASS"
            or summary.get("request_count") != 2
            or summary.get("passed_case_count") != 2
            or summary.get("response_timing_contract")
            != "request-dispatch-to-complete-http-body/v1"
            or not isinstance(cases, list)
            or len(cases) != 2
            or [case.get("input_id") for case in cases] != expected_ids
            or any(case.get("status") != "PASS" for case in cases)
        ):
            raise AggregateError(f"{run_id} semantic calls are not two distinct PASSes")
        response_hashes = [case.get("response_sha256") for case in cases]
        if any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None
            for value in response_hashes
        ) or len(set(response_hashes)) != 2:
            raise AggregateError(f"{run_id} responses are missing or not distinct")

        t0 = _stamp(evidence.get("t0_at"), f"{run_id} T0")
        second_response = _stamp(
            evidence.get("evidence", {}).get("second_response_received_at"),
            f"{run_id} second response",
        )
        validation_finished = _stamp(
            evidence.get("evidence", {}).get("validation_finished_at"),
            f"{run_id} validation finish",
        )
        if setup_finished > t0 or not t0 <= second_response <= validation_finished:
            raise AggregateError(f"{run_id} boundaries lie outside setup/T0/validation")
        exact_total = round((second_response - t0).total_seconds(), 6)
        timings = evidence.get("timings_seconds")
        if not isinstance(timings, dict) or exact_total != timings.get(
            "demand_to_two_semantic_responses"
        ):
            raise AggregateError(f"{run_id} exact total does not recompute")
        if (
            cleanup.get("schema")
            != "archvteams.nebius.ai/openfold3-cleanup/v1"
            or cleanup.get("status") != "PASS"
            or cleanup.get("run_id") != run_id
            or cleanup.get("run_scoped_resource_count") != 0
            or cleanup.get("active_gpu_requests_on_node") != 0
            or events.get("schema")
            != "archvteams.nebius.ai/openfold3-trial-image-events/v1"
            or events.get("status") != "PASS"
            or events.get("run_id") != run_id
            or events.get("pulling_event_count") != 0
            or events.get("terminal_fault_event_count") != 0
        ):
            raise AggregateError(f"{run_id} cleanup/image-event receipt is not PASS")
        for field in metrics:
            metrics[field].append(_number(timings.get(field), f"{run_id} {field}"))
        target_uid = evidence.get("target", {}).get("uid")
        if not isinstance(target_uid, str) or not target_uid or target_uid in target_uids:
            raise AggregateError(f"{run_id} target UID is missing or reused")
        target_uids.add(target_uid)
        trials.append(
            {
                "run_id": run_id,
                "t0_at": evidence["t0_at"],
                "http_ready_at": evidence["evidence"]["http_ready_at"],
                "kubernetes_ready_at": evidence["evidence"]["kubernetes_ready_at"],
                "second_response_received_at": evidence["evidence"][
                    "second_response_received_at"
                ],
                "validation_finished_at": evidence["evidence"][
                    "validation_finished_at"
                ],
                "timings_seconds": timings,
                "response_sha256": response_hashes,
                "target_uid": target_uid,
            }
        )

    return {
        "schema": "archvteams.nebius.ai/openfold3-response-boundary-n3/v1",
        "status": "PASS",
        "batch_id": batch_id,
        "trial_count": 3,
        "semantic_call_count": 6,
        "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
        "measurement_contract": {
            "t0": "timestamp immediately before target create on a provisioned Ready H100",
            "storage": "exact artifact/cache PVCs attached; all buffered artifact bytes freshly read before cohort T0",
            "readiness": "first successful independent application HTTP readiness response",
            "call_1": "first strict OpenFold3 inference dispatch through complete response body",
            "call_2": "immediate distinct strict OpenFold3 inference dispatch through complete response body",
            "exact_total": "T0 through call 2 response_received_at",
            "kubernetes_ready": "separate diagnostic only",
        },
        "cohort_preflight": preflight,
        "storage": prewarm,
        "image_residency": images,
        "metrics_seconds": {name: _metric(values) for name, values in metrics.items()},
        "trials": trials,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--probe-image", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = aggregate(
            args.evidence_root,
            args.batch_id,
            args.node,
            args.target_image,
            args.worker_image,
            args.probe_image,
        )
    except AggregateError as exc:
        raise SystemExit(f"aggregate: refused: {exc}") from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
