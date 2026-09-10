#!/usr/bin/env python3
"""Aggregate exactly three passing, matched Evo2 native trials."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))


class AggregateError(ValueError):
    pass


def finite_positive(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise AggregateError(f"{label} must be a finite positive number")
    return float(value)


def statistics_block(values: list[float]) -> dict[str, Any]:
    return {
        "values": values,
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def load_summary(path: Path, mode: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregateError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AggregateError(f"{path} is not a JSON object")
    exact = {
        "schema": "archvteams.nebius.ai/evo2-native-trial-summary/v1",
        "status": "PASS",
        "model": "Evo2-40B",
        "image": PROFILE["model"]["image"],
        "image_io_mode": mode,
        "checkpoint_id": PROFILE["artifacts"][mode]["checkpoint_id"],
        "semantic_request_count": 2,
    }
    for key, expected in exact.items():
        if value.get(key) != expected:
            raise AggregateError(f"{path}: {key} does not match the n=3 contract")
    semantic = value.get("semantic")
    if (
        not isinstance(semantic, dict)
        or semantic.get("status") != "PASS"
        or semantic.get("request_count") != 2
        or semantic.get("passed_case_count") != 2
        or semantic.get("failed_case_count") != 0
        or not isinstance(semantic.get("cases"), list)
        or len(semantic["cases"]) != 2
    ):
        raise AggregateError(f"{path}: semantic receipt is not exactly two passing calls")
    expected_sequences = PROFILE["semantic_profile"]["expected_sequences"]
    observed_sequences = [
        case.get("invariant", {}).get("output_sequence") for case in semantic["cases"]
    ]
    if observed_sequences != expected_sequences:
        raise AggregateError(f"{path}: semantic sequences differ from the pinned oracle")
    finite_positive(value.get("demand_to_two_semantic_seconds"), f"{path}: end-to-end")
    finite_positive(value.get("demand_to_http_ready_seconds"), f"{path}: HTTP ready")
    finite_positive(
        value.get("demand_to_kubernetes_ready_seconds"), f"{path}: Kubernetes Ready"
    )
    request_1 = finite_positive(
        value.get("semantic_request_1_seconds"), f"{path}: request 1"
    )
    request_2 = finite_positive(
        value.get("semantic_request_2_seconds"), f"{path}: request 2"
    )
    case_elapsed = [
        finite_positive(case.get("elapsed_seconds"), f"{path}: semantic case elapsed")
        for case in semantic["cases"]
    ]
    if [request_1, request_2] != case_elapsed:
        raise AggregateError(f"{path}: request timings differ from semantic evidence")
    timestamps = value.get("timing_evidence")
    if not isinstance(timestamps, dict) or any(
        not isinstance(timestamps.get(key), str) or not timestamps[key]
        for key in (
            "demand_at",
            "t0_at",
            "t0_source",
            "setup_demand_at",
            "http_ready_at",
            "kubernetes_ready_at",
            "semantic_started_at",
            "semantic_finished_at",
        )
    ):
        raise AggregateError(f"{path}: timing evidence timestamps are incomplete")
    if timestamps["t0_source"] != "target-submit-at.txt":
        raise AggregateError(f"{path}: T0 is not the immediate target-submit edge")
    warm = value.get("warm_instance_contract")
    if warm != {
        "node_ready_before_t0": True,
        "target_image_cached_before_t0": True,
        "restore_worker_image_cached_before_t0": True,
        "semantic_probe_image_cached_before_t0": True,
        "storage_attached_before_t0": True,
    }:
        raise AggregateError(f"{path}: warm-instance preconditions are incomplete")
    storage = value.get("storage_state")
    expected_payload_read = mode == "buffered"
    expected_page_cache = (
        "fully-prewarmed-buffered"
        if expected_payload_read
        else "not-preloaded-direct-o_direct"
    )
    if (
        not isinstance(storage, dict)
        or storage.get("artifact_and_cache_pvcs_attached_before_t0") is not True
        or storage.get("artifact_payload_read_before_t0") is not expected_payload_read
        or storage.get("artifact_prewarm_excluded_from_t0") is not expected_payload_read
        or storage.get("page_cache_state") != expected_page_cache
        or not isinstance(storage.get("artifact_regular_file_count"), int)
        or storage["artifact_regular_file_count"] < 2
        or not isinstance(storage.get("artifact_regular_bytes"), int)
        or storage["artifact_regular_bytes"] <= 0
    ):
        raise AggregateError(f"{path}: storage state does not match {mode} mode")
    if expected_payload_read:
        finite_positive(storage.get("artifact_prewarm_seconds"), f"{path}: prewarm")
        if not isinstance(storage.get("artifact_prewarm_finished_at"), str):
            raise AggregateError(f"{path}: buffered prewarm timestamp is missing")
    elif (
        storage.get("artifact_prewarm_seconds") is not None
        or storage.get("artifact_prewarm_finished_at") is not None
    ):
        raise AggregateError(f"{path}: direct mode incorrectly reports payload prewarm")
    worker = value.get("worker_receipt")
    if not isinstance(worker, dict) or worker.get("status") != "succeeded":
        raise AggregateError(f"{path}: worker receipt did not succeed")
    finite_positive(worker.get("duration_ms"), f"{path}: worker duration")
    return value


def aggregate(paths: Sequence[Path], mode: str) -> dict[str, Any]:
    if len(paths) != 3:
        raise AggregateError("exactly three trial summaries are required")
    summaries = [load_summary(path, mode) for path in paths]
    run_ids = [item.get("run_id") for item in summaries]
    pod_uids = [item.get("pod_uid") for item in summaries]
    if len(set(run_ids)) != 3 or any(not isinstance(item, str) or not item for item in run_ids):
        raise AggregateError("the three run IDs must be unique and nonempty")
    if len(set(pod_uids)) != 3 or any(not isinstance(item, str) or not item for item in pod_uids):
        raise AggregateError("the three target Pod UIDs must be unique and nonempty")
    manifests = {item.get("artifact_manifest_sha256") for item in summaries}
    if len(manifests) != 1:
        raise AggregateError("the three runs did not use one immutable artifact manifest")
    e2e = [float(item["demand_to_two_semantic_seconds"]) for item in summaries]
    http_ready = [float(item["demand_to_http_ready_seconds"]) for item in summaries]
    kubernetes_ready = [
        float(item["demand_to_kubernetes_ready_seconds"]) for item in summaries
    ]
    request_1 = [float(item["semantic_request_1_seconds"]) for item in summaries]
    request_2 = [float(item["semantic_request_2_seconds"]) for item in summaries]
    restore = [float(item["worker_receipt"]["duration_ms"]) / 1000 for item in summaries]
    semantic = [float(item["semantic"]["total_elapsed_seconds"]) for item in summaries]
    storage_states = [item["storage_state"] for item in summaries]
    if any(item != storage_states[0] for item in storage_states[1:]):
        raise AggregateError("the three runs did not use one identical prepared storage state")
    return {
        "schema": "archvteams.nebius.ai/evo2-native-n3/v1",
        "status": "PASS",
        "model": "Evo2-40B",
        "image": PROFILE["model"]["image"],
        "gpu_topology": "1x NVIDIA H200 SXM, full GPU, non-MIG",
        "image_io_mode": mode,
        "checkpoint_id": PROFILE["artifacts"][mode]["checkpoint_id"],
        "artifact_manifest_sha256": next(iter(manifests)),
        "trial_count": 3,
        "semantic_pass_count": 6,
        "measurement_contract": {
            "t0": "immediately-before-target-create",
            "primary_readiness": "first-successful-application-http-readiness",
            "call_1": "first-strict-semantic-inference-including-deferred-work",
            "call_2": "immediate-second-distinct-strict-semantic-inference",
            "kubernetes_ready_is_diagnostic": True,
            "image_pull_excluded": True,
            "storage_setup_excluded": True,
            "prewarm_excluded": mode == "buffered",
        },
        "storage_state": storage_states[0],
        "run_ids": run_ids,
        "pod_uids": pod_uids,
        "demand_to_http_ready_seconds": statistics_block(http_ready),
        "demand_to_kubernetes_ready_seconds": statistics_block(kubernetes_ready),
        "semantic_request_1_seconds": statistics_block(request_1),
        "semantic_request_2_seconds": statistics_block(request_2),
        "demand_to_two_semantic_seconds": statistics_block(e2e),
        "worker_restore_seconds": statistics_block(restore),
        "semantic_probe_seconds": statistics_block(semantic),
        "source_summaries": [str(path) for path in paths],
    }


def write_exclusive(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise AggregateError(f"refusing existing aggregate output: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-io-mode", choices=("direct", "buffered"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("summaries", type=Path, nargs=3)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = aggregate(args.summaries, args.image_io_mode)
        payload = (
            json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("ascii")
        write_exclusive(args.output, payload)
        sys.stdout.buffer.write(payload)
        return 0
    except (AggregateError, OSError) as exc:
        print(f"aggregate-evo2: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
