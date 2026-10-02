#!/usr/bin/env python3
"""Fail-closed aggregation for the exact ProteinMPNN response-boundary cohort."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from pathlib import Path
from typing import Any


class AggregateError(ValueError):
    """The cohort is incomplete or violates the frozen measurement contract."""


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


def aggregate(
    root: Path,
    batch_id: str,
    node: str,
    target_image: str,
    worker_image: str,
    probe_image: str,
) -> dict[str, Any]:
    prewarm = _load(root / "artifact-prewarm-receipt.json", "artifact prewarm receipt")
    images = _load(root / "image-residency-receipt.json", "image residency receipt")
    if (
        prewarm.get("schema")
        != "archvteams.nebius.ai/proteinmpnn-artifact-prewarm/v1"
        or prewarm.get("status") != "PASS"
        or prewarm.get("checkpoint_id") != "proteinmpnn-native-f7-v3-buffered"
        or prewarm.get("artifact_version") != "1"
        or prewarm.get("regular_file_count") != 57
        or prewarm.get("regular_bytes_read") != 1_867_046_505
        or prewarm.get("manifest_sha256")
        != "6a298ceefc93b259e5ec7e6c1e74ae3ab43cdd9a757bee1934923dbfcdc06c07"
        or not isinstance(prewarm.get("tree_sha256"), str)
        or len(prewarm["tree_sha256"]) != 64
    ):
        raise AggregateError("artifact prewarm receipt is not the exact full-read PASS")
    prewarm_finished = _stamp(prewarm.get("completed_at"), "prewarm completion")
    expected_images = {
        "target": target_image,
        "restore-worker": worker_image,
        "semantic-probe": probe_image,
    }
    if (
        images.get("schema")
        != "archvteams.nebius.ai/proteinmpnn-image-residency/v1"
        or images.get("status") != "PASS"
        or images.get("node") != node
        or images.get("preloaded_outside_t0") is not True
        or images.get("preloader_absent_before_t0") is not True
        or images.get("images") != expected_images
    ):
        raise AggregateError("image residency receipt is not the exact pre-T0 PASS")

    trials: list[dict[str, Any]] = []
    metrics: dict[str, list[float]] = {
        "demand_to_http_ready": [],
        "demand_to_kubernetes_ready": [],
        "semantic_request_1": [],
        "semantic_request_2": [],
        "demand_to_two_semantic_responses": [],
        "worker_restore": [],
    }
    response_hash_pairs: list[list[str]] = []
    for index in (1, 2, 3):
        run_id = f"{batch_id}-r{index}"
        run_dir = root / "runs" / run_id
        evidence = _load(run_dir / "canary-evidence.json", f"{run_id} evidence")
        summary = _load(run_dir / "semantic-summary.json", f"{run_id} semantic summary")
        cleanup = _load(run_dir / "cleanup-receipt.json", f"{run_id} cleanup receipt")
        image_events = _load(run_dir / "image-events-receipt.json", f"{run_id} image events")
        if (
            evidence.get("schema")
            != "archvteams.nebius.ai/proteinmpnn-production-canary-evidence/v2"
            or evidence.get("status") != "PASS"
            or evidence.get("run_id") != run_id
            or evidence.get("request_count") != 2
            or evidence.get("semantic_pass_count") != 2
            or evidence.get("response_timing_contract")
            != "request-dispatch-to-complete-http-body/v1"
            or evidence.get("t0_source") != "target-submit-at.txt"
        ):
            raise AggregateError(f"{run_id} is not an exact two-response PASS")
        cases = summary.get("cases")
        if (
            summary.get("status") != "PASS"
            or summary.get("request_count") != 2
            or summary.get("passed_case_count") != 2
            or not isinstance(cases, list)
            or len(cases) != 2
            or [case.get("input_id") for case in cases]
            != [f"{run_id}-semantic-a", f"{run_id}-semantic-b"]
            or [case.get("random_seed") for case in cases] != [2370, 2371]
            or any(case.get("status") != "PASS" for case in cases)
        ):
            raise AggregateError(f"{run_id} semantic calls are not two distinct strict PASSes")
        hashes = [case.get("response_sha256") for case in cases]
        if any(not isinstance(value, str) or len(value) != 64 for value in hashes) or len(set(hashes)) != 2:
            raise AggregateError(f"{run_id} response hashes are missing or not distinct")
        t0 = _stamp(evidence.get("t0_at"), f"{run_id} T0")
        response = _stamp(
            evidence.get("evidence", {}).get("second_response_received_at"),
            f"{run_id} second response",
        )
        if prewarm_finished > t0 or response < t0:
            raise AggregateError(f"{run_id} T0/response lies outside the prewarmed interval")
        exact_total = round((response - t0).total_seconds(), 6)
        timings = evidence.get("timings_seconds")
        if not isinstance(timings, dict):
            raise AggregateError(f"{run_id} timings are missing")
        if exact_total != timings.get("demand_to_two_semantic_responses"):
            raise AggregateError(f"{run_id} exact total does not recompute from timestamps")
        if (
            cleanup.get("status") != "PASS"
            or cleanup.get("run_id") != run_id
            or cleanup.get("run_scoped_resource_count") != 0
            or cleanup.get("active_gpu_requests_on_node") != 0
            or image_events.get("status") != "PASS"
            or image_events.get("pulling_event_count") != 0
            or image_events.get("terminal_fault_event_count") != 0
        ):
            raise AggregateError(f"{run_id} cleanup/image-event receipt is not PASS")
        for field in metrics:
            metrics[field].append(_number(timings.get(field), f"{run_id} {field}"))
        response_hash_pairs.append(hashes)
        trials.append(
            {
                "run_id": run_id,
                "t0_at": evidence["t0_at"],
                "http_ready_at": evidence["evidence"]["http_ready_at"],
                "second_response_received_at": evidence["evidence"][
                    "second_response_received_at"
                ],
                "kubernetes_ready_at": evidence["evidence"]["kubernetes_ready_at"],
                "timings_seconds": timings,
                "response_sha256": hashes,
                "target_uid": evidence["target"]["uid"],
            }
        )

    return {
        "schema": "archvteams.nebius.ai/proteinmpnn-response-boundary-n3/v1",
        "status": "PASS",
        "batch_id": batch_id,
        "trial_count": 3,
        "semantic_call_count": 6,
        "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
        "measurement_contract": {
            "t0": "timestamp immediately before target create on a provisioned Ready H100",
            "storage": "exact artifact/cache PVCs attached; all artifact bytes freshly read before cohort T0",
            "readiness": "first successful independent application HTTP readiness response",
            "call_1": "first strict 1UBQ inference dispatch through complete response body",
            "call_2": "immediate distinct strict 1UBQ inference dispatch through complete response body",
            "exact_total": "T0 through call 2 response_received_at",
            "kubernetes_ready": "separate diagnostic only",
        },
        "storage": prewarm,
        "image_residency": images,
        "metrics_seconds": {name: _metric(values) for name, values in metrics.items()},
        "response_sha256_pairs": response_hash_pairs,
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
