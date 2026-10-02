#!/usr/bin/env python3
"""Aggregate exactly three passing, matched RFdiffusion native trials."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))


class AggregateError(ValueError):
    pass


def sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def finite_positive(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise AggregateError(f"{label} must be a finite positive number")
    return float(value)


def finite_nonnegative(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise AggregateError(f"{label} must be a finite nonnegative number")
    return float(value)


def timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AggregateError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AggregateError(f"{label} timestamp lacks a UTC offset")
    return parsed


def assert_seconds(reported: Any, start: datetime, finish: datetime, label: str) -> None:
    observed = finite_positive(reported, label)
    recomputed = round((finish - start).total_seconds(), 6)
    if recomputed <= 0 or abs(observed - recomputed) > 0.002:
        raise AggregateError(
            f"{label} differs from absolute timestamps: reported={observed} recomputed={recomputed}"
        )


def assert_nonnegative_seconds(
    reported: Any, start: datetime, finish: datetime, label: str
) -> None:
    observed = finite_nonnegative(reported, label)
    recomputed = round((finish - start).total_seconds(), 6)
    if recomputed < 0 or abs(observed - recomputed) > 0.002:
        raise AggregateError(
            f"{label} differs from absolute timestamps: reported={observed} recomputed={recomputed}"
        )


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
        "schema": "archvteams.nebius.ai/rfdiffusion-native-trial-summary/v1",
        "status": "PASS",
        "model": "RFdiffusion",
        "image": PROFILE["model"]["image"],
        "gpu_topology": "1x NVIDIA H100, full GPU, non-MIG",
        "image_io_mode": mode,
        "checkpoint_id": PROFILE["artifacts"][mode]["checkpoint_id"],
        "artifact_manifest_sha256": PROFILE["artifacts"][mode]["manifest_sha256"],
        "semantic_request_count": 2,
    }
    for key, expected in exact.items():
        if value.get(key) != expected:
            raise AggregateError(f"{path}: {key} does not match the n=3 contract")
    if value.get("t0_basis") != "target-submit-at immediately before kubectl create":
        raise AggregateError(f"{path}: T0 is not the target-submit edge")
    storage = value.get("storage_state")
    expected_state = (
        "buffered_fully_prewarmed"
        if mode == "buffered"
        else "direct_o_direct_no_artifact_payload_prewarm"
    )
    if (
        not isinstance(storage, dict)
        or storage.get("schema")
        != "archvteams.nebius.ai/rfdiffusion-storage-state/v1"
        or storage.get("state") != expected_state
        or storage.get("image_io_mode") != mode
        or storage.get("storage_attached_before_t0") is not True
        or storage.get("image_present_before_t0") is not True
        or storage.get("prewarm_outside_t0") is not True
        or storage.get("cache_payload_read") is not True
        or storage.get("artifact_payload_read") != (mode == "buffered")
        or not isinstance(storage.get("image_holder"), dict)
        or storage["image_holder"].get("schema")
        != "archvteams.nebius.ai/rfdiffusion-image-cache-holder/v1"
        or storage["image_holder"].get("status") != "PASS"
        or storage["image_holder"].get("image") != PROFILE["model"]["image"]
        or storage["image_holder"].get("image_digest")
        != PROFILE["model"]["image"].split("@", 1)[1]
        or str(storage["image_holder"].get("live_image_id", "")).removeprefix(
            "docker-pullable://"
        )
        != PROFILE["model"]["image"]
    ):
        raise AggregateError(f"{path}: pre-T0 storage state is invalid")
    for key in (
        "artifact_regular_bytes",
        "cache_regular_bytes",
        "cache_prewarm_seconds",
        "total_pre_t0_full_read_seconds",
    ):
        finite_positive(storage.get(key), f"{path}: storage {key}")
    artifact_prewarm = storage.get("artifact_prewarm_seconds")
    if (
        isinstance(artifact_prewarm, bool)
        or not isinstance(artifact_prewarm, (int, float))
        or not math.isfinite(float(artifact_prewarm))
        or float(artifact_prewarm) < 0
    ):
        raise AggregateError(f"{path}: artifact prewarm duration is invalid")
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
    seeds = [case.get("invariant", {}).get("random_seed") for case in semantic["cases"]]
    if seeds != PROFILE["semantic_profile"]["request_seeds"]:
        raise AggregateError(f"{path}: semantic seeds differ from the pinned oracle")
    request_digests = [case.get("request_sha256") for case in semantic["cases"]]
    if request_digests != PROFILE["semantic_profile"]["request_body_sha256"]:
        raise AggregateError(f"{path}: semantic request bodies differ from the pinned oracle")
    response_digests = [case.get("response_sha256") for case in semantic["cases"]]
    if not all(sha256_digest(item) for item in response_digests) or len(set(response_digests)) != 2:
        raise AggregateError(f"{path}: seeded semantic responses are not distinct")
    case_run_ids = [case.get("run_id") for case in semantic["cases"]]
    if len(set(case_run_ids)) != 2 or any(
        not isinstance(item, str) or not item for item in case_run_ids
    ):
        raise AggregateError(f"{path}: semantic case run IDs are not distinct")
    for case in semantic["cases"]:
        if case.get("status") != "PASS" or case.get("ok") is not True or case.get("http_status") != 200:
            raise AggregateError(f"{path}: semantic case did not strictly pass")
        invariant = case.get("invariant", {})
        if invariant.get("fixture_sha256") != PROFILE["semantic_profile"]["fixture_sha256"]:
            raise AggregateError(f"{path}: semantic fixture identity is invalid")
        backbone = invariant.get("backbone", {})
        residues = backbone.get("residue_count")
        if (
            isinstance(residues, bool)
            or not isinstance(residues, int)
            or not PROFILE["semantic_profile"]["residue_count_min"]
            <= residues
            <= PROFILE["semantic_profile"]["residue_count_max"]
            or backbone.get("complete_backbone_residue_count") != residues
            or backbone.get("ca_count") != residues
            or not isinstance(backbone.get("adjacent_ca_pair_count"), int)
            or backbone["adjacent_ca_pair_count"] < 15
        ):
            raise AggregateError(f"{path}: semantic backbone invariant is invalid")
    target_submit_raw = value.get("target_submit_at")
    target_submit = timestamp(target_submit_raw, f"{path}: target submit")
    prepared = timestamp(value.get("prepared_at"), f"{path}: preparation")
    if prepared > target_submit:
        raise AggregateError(f"{path}: preparation timestamp follows T0")
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
    finite_positive(
        value.get("demand_to_semantic_validation_seconds"),
        f"{path}: T0 through semantic validation",
    )
    finite_nonnegative(
        value.get("semantic_validation_overhang_seconds"),
        f"{path}: semantic validation overhang",
    )
    case_elapsed = [
        finite_positive(case.get("elapsed_seconds"), f"{path}: semantic case elapsed")
        for case in semantic["cases"]
    ]
    if [request_1, request_2] != case_elapsed:
        raise AggregateError(f"{path}: request timings differ from semantic evidence")
    response_contract = "request-dispatch-to-complete-http-body/v1"
    if (
        value.get("response_timing_contract") != response_contract
        or semantic.get("response_timing_contract") != response_contract
    ):
        raise AggregateError(f"{path}: response timing contract is invalid")
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
            "semantic_response_1_received_at",
            "semantic_response_2_received_at",
            "validation_finished_at",
        )
    ):
        raise AggregateError(f"{path}: timing evidence timestamps are incomplete")
    if (
        timestamps["demand_at"] != target_submit_raw
        or timestamps["t0_at"] != target_submit_raw
        or timestamps["t0_source"] != "target-submit-at.txt"
        or timestamps["setup_demand_at"] != value.get("prepared_at")
    ):
        raise AggregateError(f"{path}: timing evidence is not rooted at target-submit-at")
    http_ready = timestamp(timestamps["http_ready_at"], f"{path}: HTTP ready")
    kubernetes_ready = timestamp(
        timestamps["kubernetes_ready_at"], f"{path}: Kubernetes Ready"
    )
    semantic_started_raw = semantic.get("started_at")
    if timestamps["semantic_started_at"] != semantic_started_raw:
        raise AggregateError(f"{path}: semantic-start provenance is inconsistent")
    semantic_started = timestamp(semantic_started_raw, f"{path}: semantic start")
    ready = semantic.get("ready_wait", semantic.get("ready"))
    if (
        not isinstance(ready, dict)
        or ready.get("status") != "PASS"
        or ready.get("endpoint")
        != str(semantic.get("base_url", "")).rstrip("/") + "/v1/health/ready"
        or ready.get("finished_at") != timestamps["http_ready_at"]
    ):
        raise AggregateError(f"{path}: semantic HTTP readiness provenance is invalid")
    ready_started = timestamp(ready.get("started_at"), f"{path}: readiness start")
    call_2_received_raw = value.get("call_2_response_received_at")
    if (
        call_2_received_raw != semantic["cases"][1].get("response_received_at")
        or call_2_received_raw != timestamps["semantic_response_2_received_at"]
    ):
        raise AggregateError(f"{path}: call-2 body-received provenance is inconsistent")
    call_2_received = timestamp(call_2_received_raw, f"{path}: call 2 response received")
    assert_seconds(
        value.get("demand_to_http_ready_seconds"),
        target_submit,
        http_ready,
        f"{path}: HTTP ready",
    )
    kubernetes_reported = finite_nonnegative(
        value.get("demand_to_kubernetes_ready_seconds"),
        f"{path}: Kubernetes Ready",
    )
    kubernetes_seconds = round((kubernetes_ready - target_submit).total_seconds(), 6)
    if kubernetes_seconds < 0:
        if abs(kubernetes_seconds) >= 1:
            raise AggregateError(f"{path}: Kubernetes Ready precedes T0 by at least one second")
        kubernetes_seconds = 0.0
    if abs(kubernetes_reported - kubernetes_seconds) > 0.002:
        raise AggregateError(f"{path}: Kubernetes Ready metric differs from absolute timestamps")
    assert_seconds(
        value.get("demand_to_two_semantic_seconds"),
        target_submit,
        call_2_received,
        f"{path}: T0 through call 2 body",
    )
    previous_received: datetime | None = None
    for index, case in enumerate(semantic["cases"], 1):
        request_started_raw = case.get("request_started_at")
        if request_started_raw != case.get("started_at"):
            raise AggregateError(
                f"{path}: call {index} request-start compatibility alias is inconsistent"
            )
        started = timestamp(request_started_raw, f"{path}: call {index} start")
        expected_received_raw = timestamps[f"semantic_response_{index}_received_at"]
        if case.get("response_received_at") != expected_received_raw:
            raise AggregateError(
                f"{path}: call {index} body-received provenance is inconsistent"
            )
        received = timestamp(
            case.get("response_received_at"), f"{path}: call {index} response received"
        )
        if case.get("validation_finished_at") != case.get("finished_at"):
            raise AggregateError(
                f"{path}: call {index} validation-finish compatibility alias is inconsistent"
            )
        validated = timestamp(
            case.get("validation_finished_at"), f"{path}: call {index} validation finish"
        )
        if not (
            target_submit
            <= semantic_started
            <= ready_started
            <= http_ready
            <= started
            <= received
            <= validated
        ):
            raise AggregateError(f"{path}: call {index} absolute timestamps are out of order")
        if previous_received is not None and started < previous_received:
            raise AggregateError(f"{path}: call 2 started before call 1 body completed")
        assert_seconds(
            value.get(f"semantic_request_{index}_seconds"),
            started,
            received,
            f"{path}: call {index} body latency",
        )
        previous_received = received
    validation_finished = timestamp(
        value.get("semantic_validation_finished_at"), f"{path}: semantic validation finish"
    )
    if (
        value.get("semantic_validation_finished_at")
        != semantic.get("validation_finished_at")
        or value.get("semantic_validation_finished_at") != semantic.get("finished_at")
        or value.get("semantic_validation_finished_at")
        != timestamps["validation_finished_at"]
        or validation_finished < timestamp(
            semantic["cases"][1].get("validation_finished_at"),
            f"{path}: call 2 validation finish",
        )
    ):
        raise AggregateError(f"{path}: semantic validation-finish provenance is inconsistent")
    assert_seconds(
        semantic.get("validation_total_elapsed_seconds"),
        semantic_started,
        validation_finished,
        f"{path}: semantic validation duration",
    )
    if semantic.get("total_elapsed_seconds") != semantic.get(
        "validation_total_elapsed_seconds"
    ):
        raise AggregateError(f"{path}: semantic duration compatibility alias is inconsistent")
    assert_seconds(
        value.get("demand_to_semantic_validation_seconds"),
        target_submit,
        validation_finished,
        f"{path}: T0 through semantic validation",
    )
    assert_nonnegative_seconds(
        value.get("semantic_validation_overhang_seconds"),
        call_2_received,
        validation_finished,
        f"{path}: semantic validation overhang",
    )
    worker = value.get("worker_receipt")
    if not isinstance(worker, dict) or worker.get("status") != "succeeded":
        raise AggregateError(f"{path}: worker receipt did not succeed")
    finite_positive(worker.get("duration_ms"), f"{path}: worker duration")
    if value.get("semantic_response_sha256") != response_digests:
        raise AggregateError(f"{path}: summary response digests differ from semantic evidence")
    try:
        if str(uuid.UUID(str(value.get("pod_uid")))) != value.get("pod_uid"):
            raise ValueError
    except ValueError as exc:
        raise AggregateError(f"{path}: target Pod UID is invalid") from exc
    if not sha256_digest(value.get("pod_spec_sha256")):
        raise AggregateError(f"{path}: target PodSpec digest is invalid")
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
    if (
        len(manifests) != 1
        or not sha256_digest(next(iter(manifests)))
    ):
        raise AggregateError("the three runs did not use one immutable artifact manifest")
    storage_states = [item["storage_state"] for item in summaries]
    if any(item != storage_states[0] for item in storage_states[1:]):
        raise AggregateError("the three runs did not use one immutable pre-T0 storage state")
    e2e = [float(item["demand_to_two_semantic_seconds"]) for item in summaries]
    http_ready = [float(item["demand_to_http_ready_seconds"]) for item in summaries]
    kubernetes_ready = [
        float(item["demand_to_kubernetes_ready_seconds"]) for item in summaries
    ]
    request_1 = [float(item["semantic_request_1_seconds"]) for item in summaries]
    request_2 = [float(item["semantic_request_2_seconds"]) for item in summaries]
    restore = [float(item["worker_receipt"]["duration_ms"]) / 1000 for item in summaries]
    semantic = [float(item["semantic"]["total_elapsed_seconds"]) for item in summaries]
    validation = [
        float(item["demand_to_semantic_validation_seconds"]) for item in summaries
    ]
    validation_overhang = [
        float(item["semantic_validation_overhang_seconds"]) for item in summaries
    ]
    return {
        "schema": "archvteams.nebius.ai/rfdiffusion-native-n3/v1",
        "status": "PASS",
        "model": "RFdiffusion",
        "image": PROFILE["model"]["image"],
        "gpu_topology": "1x NVIDIA H100, full GPU, non-MIG",
        "image_io_mode": mode,
        "checkpoint_id": PROFILE["artifacts"][mode]["checkpoint_id"],
        "artifact_manifest_sha256": next(iter(manifests)),
        "t0_basis": "target-submit-at immediately before kubectl create",
        "ready_definition": "first successful semantic application HTTP readiness response",
        "call_definition": "two immediate distinct strict RF backbone inferences after readiness",
        "storage_state": storage_states[0],
        "trial_count": 3,
        "semantic_pass_count": 6,
        "run_ids": run_ids,
        "pod_uids": pod_uids,
        "demand_to_http_ready_seconds": statistics_block(http_ready),
        "demand_to_kubernetes_ready_seconds": statistics_block(kubernetes_ready),
        "semantic_request_1_seconds": statistics_block(request_1),
        "semantic_request_2_seconds": statistics_block(request_2),
        "demand_to_two_semantic_seconds": statistics_block(e2e),
        "demand_to_semantic_validation_seconds": statistics_block(validation),
        "semantic_validation_overhang_seconds": statistics_block(validation_overhang),
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
        print(f"aggregate-rfdiffusion: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
