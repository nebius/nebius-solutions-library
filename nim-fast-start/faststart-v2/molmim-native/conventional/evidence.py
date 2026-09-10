#!/usr/bin/env python3
"""Build strict evidence for one MolMIM conventional-cached comparison trial."""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import render as lane_render
except ImportError:  # pragma: no cover - package import path
    from . import render as lane_render


IMAGE = (
    "nvcr.io/nim/nvidia/molmim@sha256:"
    "7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
)
NODE = "gpu-node-b.example.invalid"
NAMESPACE = "nim-fast-start"
REQUEST_SHA256 = (
    "3a59acaf04e18fc5a7ed37b27ffdeee05f2542f23734d204bf487cc5f172a55e",
    "98ac4fccf35f4a9c3cbb3666b8d98218b24e7f2d5cff9cb174c76b2d242927da",
)


class EvidenceError(ValueError):
    """A recorded comparator object does not prove a strict cached trial."""


def _require_expected(actual: Any, expected: Any, label: str) -> None:
    """Require the rendered subtree while allowing Kubernetes-added fields."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise EvidenceError(f"{label} is not an object")
        for key, expected_value in expected.items():
            if key not in actual:
                raise EvidenceError(f"{label}.{key} is absent")
            _require_expected(actual[key], expected_value, f"{label}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise EvidenceError(f"{label} list does not match the rendered contract")
        for index, (actual_value, expected_value) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _require_expected(actual_value, expected_value, f"{label}[{index}]")
        return
    if actual != expected:
        raise EvidenceError(f"{label} does not match the rendered contract")


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot load {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must contain an object")
    return value


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} is not an RFC3339 UTC timestamp")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} is not an RFC3339 UTC timestamp") from exc
    if result.tzinfo != UTC:
        result = result.astimezone(UTC)
    return result


def _measurement_timestamp(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("target submit timestamp must be a regular non-symlink file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("cannot read target submit timestamp") from exc
    _time(value, "target_submit_at")
    return value


def _normalize_kubernetes_time(
    observed: datetime, measurement_start: datetime, label: str
) -> datetime:
    if observed >= measurement_start:
        return observed
    if (measurement_start - observed).total_seconds() < 1:
        return measurement_start
    raise EvidenceError(f"{label} precedes authoritative target submission")


def _cache_prewarm(
    holder: dict[str, Any], receipt: dict[str, Any], captured_at: str, demand: datetime
) -> dict[str, Any]:
    metadata = holder.get("metadata", {})
    spec = holder.get("spec", {})
    status = holder.get("status", {})
    if not all(isinstance(item, dict) for item in (metadata, spec, status)):
        raise EvidenceError("cache holder Pod is malformed")
    try:
        holder_uid = str(uuid.UUID(str(metadata.get("uid"))))
    except ValueError as exc:
        raise EvidenceError("cache holder Pod UID is not canonical") from exc
    if (
        holder_uid != metadata.get("uid")
        or metadata.get("name") != "molmim-native-f7-cache-holder-t12"
        or metadata.get("namespace") != NAMESPACE
        or spec.get("nodeName") != NODE
        or not any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
            for item in status.get("conditions", [])
        )
        or not any(
            isinstance(item, dict)
            and item.get("name") == "cache"
            and item.get("persistentVolumeClaim", {}).get("claimName")
            == "molmim-native-f7-cache"
            and item.get("persistentVolumeClaim", {}).get("readOnly") is True
            for item in spec.get("volumes", [])
        )
    ):
        raise EvidenceError("cache holder Pod does not prove the attached read-only cache")
    captured = _time(captured_at, "cache prewarm capture")
    if captured > demand:
        raise EvidenceError("cache prewarm receipt was captured after authoritative T0")
    ready_times = [
        _time(item.get("lastTransitionTime"), "cache holder Ready")
        for item in status.get("conditions", [])
        if isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
    ]
    if len(ready_times) != 1 or ready_times[0] > captured:
        raise EvidenceError("cache holder Ready state was not established before capture")
    elapsed = receipt.get("full_read_elapsed_seconds")
    if (
        receipt.get("schema")
        != "archvteams.nebius.ai/molmim-cache-holder-receipt/v1"
        or receipt.get("status") != "PASS"
        or receipt.get("mode") != "cache-full-read"
        or receipt.get("regular_file_count") != 2
        or receipt.get("regular_file_bytes") != 284_497_920
        or receipt.get("unique_bytes") != 284_497_920
        or receipt.get("prewarm_bytes") != 284_497_920
        or receipt.get("tree_sha256")
        != "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c"
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or elapsed <= 0
    ):
        raise EvidenceError("cache holder log is not a complete full-read receipt")
    return {
        "holder_pod": metadata["name"],
        "holder_uid": holder_uid,
        "captured_at": captured_at,
        "mode": receipt["mode"],
        "unique_bytes": receipt["unique_bytes"],
        "tree_sha256": receipt["tree_sha256"],
        "full_read_elapsed_seconds": round(float(elapsed), 6),
    }


def _case(value: Any, index: int, run_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"semantic case {index} is malformed")
    expected_name = ("caffeine", "aspirin")[index - 1]
    expected_run_id = f"{run_id}-semantic-{'a' if index == 1 else 'b'}"
    if (
        value.get("index") != index
        or value.get("input_id") != expected_name
        or value.get("run_id") != expected_run_id
        or value.get("status") != "PASS"
        or value.get("ok") is not True
        or value.get("exit_code") != 0
        or value.get("request_sha256") != REQUEST_SHA256[index - 1]
    ):
        raise EvidenceError(f"semantic case {index} identity/status mismatch")
    elapsed = value.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0:
        raise EvidenceError(f"semantic case {index} elapsed time is invalid")
    _time(value.get("response_received_at"), f"semantic case {index} response_received_at")
    response_bytes = value.get("response_bytes")
    response_hash = value.get("response_sha256")
    if (
        not isinstance(response_bytes, int)
        or isinstance(response_bytes, bool)
        or not 1 <= response_bytes <= 16 * 1024 * 1024
        or not isinstance(response_hash, str)
        or len(response_hash) != 64
        or any(character not in "0123456789abcdef" for character in response_hash)
    ):
        raise EvidenceError(f"semantic case {index} response receipt is invalid")
    invariant = value.get("invariant")
    if not isinstance(invariant, dict):
        raise EvidenceError(f"semantic case {index} invariant is absent")
    if (
        invariant.get("generated_count") != 1
        or not isinstance(invariant.get("smiles"), str)
        or not invariant["smiles"]
        or not isinstance(invariant.get("atom_count"), int)
        or isinstance(invariant.get("atom_count"), bool)
        or invariant["atom_count"] < 1
    ):
        raise EvidenceError(f"semantic case {index} did not prove one parsed molecule")
    score = invariant.get("score")
    reference = invariant.get("rdkit_qed")
    if any(
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        for number in (score, reference)
    ) or not (
        0 <= float(score) <= 1 and 0 <= float(reference) <= 1
    ) or abs(float(score) - float(reference)) > 0.02:
        raise EvidenceError(f"semantic case {index} did not prove its RDKit QED score")
    return value


def build(
    run: dict[str, Any],
    target: dict[str, Any],
    probe_job: dict[str, Any],
    probe_pod: dict[str, Any],
    semantic: dict[str, Any],
    events: dict[str, Any],
    target_submit_at: str | None = None,
    prewarm_holder: dict[str, Any] | None = None,
    prewarm_receipt: dict[str, Any] | None = None,
    prewarm_captured_at: str | None = None,
) -> dict[str, Any]:
    if set(run) != {"schema", "run_id", "demand_at", "node", "image", "mode"}:
        raise EvidenceError("run receipt has the wrong fields")
    run_id = run.get("run_id")
    if run.get("schema") != "archvteams.nebius.ai/molmim-conventional-run/v1" or run.get("mode") != "conventional-cached":
        raise EvidenceError("run receipt schema/mode mismatch")
    if run.get("node") != NODE or run.get("image") != IMAGE or not isinstance(run_id, str):
        raise EvidenceError("run receipt execution identity mismatch")
    planned_demand = _time(run.get("demand_at"), "planned demand_at")
    measurement_start_raw = target_submit_at or run["demand_at"]
    demand = _time(measurement_start_raw, "target_submit_at")
    if demand < planned_demand:
        raise EvidenceError("authoritative target submission precedes planned demand")
    if prewarm_holder is None or prewarm_receipt is None or prewarm_captured_at is None:
        raise EvidenceError("cache prewarm Pod/log capture is required")
    storage_prewarm = _cache_prewarm(
        prewarm_holder, prewarm_receipt, prewarm_captured_at, demand
    )

    try:
        expected_target = lane_render.render_target(run_id, run["demand_at"])[0]
    except lane_render.RenderError as exc:
        raise EvidenceError(str(exc)) from exc
    _require_expected(target, expected_target, "target Pod")

    metadata = target.get("metadata", {})
    spec = target.get("spec", {})
    status = target.get("status", {})
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        raise EvidenceError("target Pod is malformed")
    target_uid = metadata.get("uid")
    try:
        if str(uuid.UUID(str(target_uid))) != target_uid:
            raise ValueError
    except ValueError as exc:
        raise EvidenceError("target Pod UID is not canonical") from exc
    if (
        metadata.get("name") != f"molmim-cached-{run_id}"
        or metadata.get("namespace") != NAMESPACE
        or spec.get("nodeName") != NODE
    ):
        raise EvidenceError("target Pod name/namespace/node mismatch")
    target_created = _normalize_kubernetes_time(
        _time(metadata.get("creationTimestamp"), "target creationTimestamp"),
        demand,
        "target creationTimestamp",
    )
    labels = metadata.get("labels", {})
    if (
        not isinstance(labels, dict)
        or labels.get("app.kubernetes.io/component") != "conventional-cached-target"
        or labels.get("archvteams.nebius.ai/run-id") != run_id
    ):
        raise EvidenceError("target Pod labels do not bind this run")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise EvidenceError("target Pod container set mismatch")
    container = containers[0]
    if (
        container.get("name") != "molmim"
        or container.get("image") != IMAGE
        or container.get("imagePullPolicy") != "IfNotPresent"
        or container.get("command") != ["/opt/nvidia/nvidia_entrypoint.sh"]
        or container.get("args") != ["start_server"]
    ):
        raise EvidenceError("target Pod is not the exact conventional MolMIM process")
    cache_mounts = [
        item
        for item in container.get("volumeMounts", [])
        if isinstance(item, dict) and item.get("name") == "nim-cache"
    ]
    cache_volumes = [
        item
        for item in spec.get("volumes", [])
        if isinstance(item, dict) and item.get("name") == "nim-cache"
    ]
    if (
        len(cache_mounts) != 1
        or cache_mounts[0].get("readOnly", False) is not False
        or len(cache_volumes) != 1
        or cache_volumes[0].get("persistentVolumeClaim", {}).get("readOnly", False)
        is not False
    ):
        raise EvidenceError("target Pod cache is not writable")
    env = container.get("env", [])
    if not any(
        isinstance(item, dict)
        and item.get("name") == "TORCHINDUCTOR_COMPILE_THREADS"
        and item.get("value") == "1"
        for item in env
    ):
        raise EvidenceError("target Pod did not use the single TorchInductor worker setting")
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise EvidenceError("target container status is absent")
    target_status = statuses[0]
    if target_status.get("imageID", "").removeprefix("docker-pullable://") != IMAGE:
        raise EvidenceError("target imageID is not the pinned MolMIM digest")
    try:
        container_started_raw = target_status["state"]["running"]["startedAt"]
    except (KeyError, TypeError) as exc:
        raise EvidenceError("target container did not remain running") from exc
    container_started = _normalize_kubernetes_time(
        _time(container_started_raw, "target container startedAt"),
        demand,
        "target container startedAt",
    )
    ready_conditions = [
        item
        for item in status.get("conditions", [])
        if isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
    ]
    if len(ready_conditions) != 1:
        raise EvidenceError("target Pod lacks one successful Kubernetes Ready condition")
    kubernetes_ready = _normalize_kubernetes_time(
        _time(
            ready_conditions[0].get("lastTransitionTime"),
            "Kubernetes Ready lastTransitionTime",
        ),
        demand,
        "Kubernetes Ready",
    )

    try:
        expected_probe_job = next(
            document
            for document in lane_render.render_probe(run_id, run["demand_at"], target_uid)
            if document.get("kind") == "Job"
        )
    except (lane_render.RenderError, StopIteration) as exc:
        raise EvidenceError(f"cannot reconstruct exact semantic probe Job: {exc}") from exc
    _require_expected(probe_job, expected_probe_job, "semantic probe Job")
    job_metadata = probe_job.get("metadata", {})
    job_uid = job_metadata.get("uid") if isinstance(job_metadata, dict) else None
    try:
        if str(uuid.UUID(str(job_uid))) != job_uid:
            raise ValueError
    except ValueError as exc:
        raise EvidenceError("semantic probe Job UID is not canonical") from exc
    if probe_job.get("status", {}).get("succeeded") != 1:
        raise EvidenceError("semantic probe Job did not succeed exactly once")
    job_annotations = job_metadata.get("annotations", {})
    if (
        not isinstance(job_annotations, dict)
        or job_annotations.get("archvteams.nebius.ai/target-pod-uid") != target_uid
        or job_annotations.get("archvteams.nebius.ai/demand-at") != run["demand_at"]
    ):
        raise EvidenceError("semantic probe Job is not UID/demand bound")
    probe_name = f"molmim-cached-probe-{run_id}"
    probe_metadata = probe_pod.get("metadata", {})
    probe_spec = probe_pod.get("spec", {})
    probe_status = probe_pod.get("status", {})
    if not all(
        isinstance(value, dict) for value in (probe_metadata, probe_spec, probe_status)
    ):
        raise EvidenceError("semantic probe Pod is malformed")
    owners = probe_metadata.get("ownerReferences")
    controllers = (
        [owner for owner in owners if isinstance(owner, dict) and owner.get("controller") is True]
        if isinstance(owners, list)
        else []
    )
    if (
        probe_pod.get("apiVersion") != "v1"
        or probe_pod.get("kind") != "Pod"
        or probe_metadata.get("namespace") != NAMESPACE
        or probe_metadata.get("generateName") != f"{probe_name}-"
        or not isinstance(probe_metadata.get("name"), str)
        or not probe_metadata["name"].startswith(f"{probe_name}-")
        or len(controllers) != 1
        or any(
            controllers[0].get(key) != value
            for key, value in {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": probe_name,
                "uid": job_uid,
            }.items()
        )
    ):
        raise EvidenceError("semantic probe Pod is not controlled by the exact probe Job")
    _require_expected(
        probe_metadata.get("labels"),
        expected_probe_job["spec"]["template"]["metadata"]["labels"],
        "semantic probe Pod labels",
    )
    _require_expected(
        probe_metadata.get("annotations"),
        expected_probe_job["spec"]["template"]["metadata"]["annotations"],
        "semantic probe Pod annotations",
    )
    _require_expected(
        probe_spec,
        expected_probe_job["spec"]["template"]["spec"],
        "semantic probe PodSpec",
    )
    init_statuses = probe_status.get("initContainerStatuses")
    if (
        not isinstance(init_statuses, list)
        or len(init_statuses) != 1
        or init_statuses[0].get("name") != "stage-validator"
        or init_statuses[0].get("imageID", "").removeprefix("docker-pullable://")
        != IMAGE
        or init_statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0
    ):
        raise EvidenceError("semantic probe validator stage is not the pinned successful image")
    probe_statuses = probe_status.get("containerStatuses")
    if not isinstance(probe_statuses, list) or len(probe_statuses) != 1:
        raise EvidenceError("semantic probe Pod status is absent")
    probe_container_status = probe_statuses[0]
    terminated = probe_container_status.get("state", {}).get("terminated", {})
    if (
        probe_container_status.get("name") != "semantic-probe"
        or probe_container_status.get("imageID", "").removeprefix("docker-pullable://")
        != IMAGE
        or not isinstance(terminated, dict)
        or terminated.get("exitCode") != 0
    ):
        raise EvidenceError("semantic probe container did not exit successfully")

    service_name = f"molmim-cached-svc-{run_id}"
    if (
        semantic.get("schema_version") != 1
        or semantic.get("validator") != "molmim-faststart-semantic-v1"
        or semantic.get("base_url") != f"http://{service_name}:8000"
        or semantic.get("inference_path") != "/generate"
        or semantic.get("endpoint") != f"http://{service_name}:8000/generate"
        or semantic.get("proxy_policy") != "disabled"
        or semantic.get("redirect_policy") != "reject"
        or semantic.get("status") != "PASS"
        or semantic.get("ok") is not True
        or semantic.get("passed_case_count") != 2
        or semantic.get("failed_case_count") != 0
        or semantic.get("exit_code") != 0
    ):
        raise EvidenceError("semantic summary does not prove the exact two-call contract")
    total_elapsed = semantic.get("total_elapsed_seconds")
    if (
        isinstance(total_elapsed, bool)
        or not isinstance(total_elapsed, (int, float))
        or not math.isfinite(float(total_elapsed))
        or total_elapsed < 0
    ):
        raise EvidenceError("semantic summary total elapsed time is invalid")
    cases_raw = semantic.get("cases")
    if not isinstance(cases_raw, list) or len(cases_raw) != 2:
        raise EvidenceError("semantic summary must contain exactly two cases")
    cases = [_case(item, index, run_id) for index, item in enumerate(cases_raw, 1)]
    if cases[0]["response_sha256"] == cases[1]["response_sha256"]:
        raise EvidenceError("two distinct calls returned byte-identical responses")
    if cases[0]["invariant"]["smiles"] == cases[1]["invariant"]["smiles"]:
        raise EvidenceError("two distinct seeds returned the same molecule")
    started_at = _time(semantic.get("started_at"), "semantic started_at")
    ready_at = _time(semantic.get("ready_at"), "semantic ready_at")
    finished = _time(semantic.get("finished_at"), "semantic finished_at")
    validation_completed = _time(
        semantic.get("validation_completed_at"), "semantic validation_completed_at"
    )
    response_received = [
        _time(case.get("response_received_at"), f"semantic case {index} response")
        for index, case in enumerate(cases, 1)
    ]
    if not (
        demand
        <= target_created
        <= container_started
        <= ready_at
        <= response_received[0]
        <= response_received[1]
        <= validation_completed
        and finished == validation_completed
        and demand <= started_at <= ready_at
    ):
        raise EvidenceError("demand, container, readiness, and semantic times are not ordered")

    items = events.get("items")
    if not isinstance(items, list):
        raise EvidenceError("target EventList is malformed")
    cached_image_message = f'Container image "{IMAGE}" already present on machine'
    cached_events = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("involvedObject", {}).get("uid") == target_uid
        and item.get("involvedObject", {}).get("fieldPath")
        == "spec.containers{molmim}"
        and item.get("reason") == "Pulled"
        and item.get("message") == cached_image_message
    ]
    if len(cached_events) < 1:
        raise EvidenceError("target Events do not prove that the exact image was already cached")

    return {
        "schema": "archvteams.nebius.ai/molmim-conventional-cached-evidence/v1",
        "status": "PASS",
        "mode": "conventional-cached",
        "run_id": run_id,
        "request_count": 2,
        "semantic_pass_count": 2,
        "measurement": {
            "t0": measurement_start_raw,
            "t0_definition": "timestamp immediately before target Pod create",
            "planned_demand_at": run["demand_at"],
            "storage_state": "attached cache PVC fully read before T0; exact image present on node",
        },
        "execution_identity": {
            "node": NODE,
            "image": IMAGE,
            "image_id": target_status["imageID"],
            "target_pod": metadata["name"],
            "target_uid": target_uid,
            "cache_pvc": "molmim-native-f7-cache",
            "torchinductor_compile_threads": 1,
            "image_already_present_event_count": len(cached_events),
        },
        "storage_prewarm": storage_prewarm,
        "timings_seconds": {
            "demand_to_target_created": round(
                (target_created - demand).total_seconds(), 6
            ),
            "demand_to_container_started": round((container_started - demand).total_seconds(), 6),
            "demand_to_kubernetes_ready": round(
                (kubernetes_ready - demand).total_seconds(), 6
            ),
            "demand_to_http_ready": round((ready_at - demand).total_seconds(), 6),
            "call_1": float(cases[0]["elapsed_seconds"]),
            "call_2": float(cases[1]["elapsed_seconds"]),
            "demand_to_two_semantic_responses": round(
                (response_received[1] - demand).total_seconds(), 6
            ),
            "demand_to_validation_complete": round(
                (validation_completed - demand).total_seconds(), 6
            ),
        },
        "semantic": semantic,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--probe-job", type=Path, required=True)
    parser.add_argument("--probe-pod", type=Path, required=True)
    parser.add_argument("--semantic-summary", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--target-submit-at", type=Path, required=True)
    parser.add_argument("--prewarm-holder-pod", type=Path, required=True)
    parser.add_argument("--prewarm-holder-receipt", type=Path, required=True)
    parser.add_argument("--prewarm-captured-at", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = build(
            _load(args.run, "run receipt"),
            _load(args.target, "target Pod"),
            _load(args.probe_job, "probe Job"),
            _load(args.probe_pod, "probe Pod"),
            _load(args.semantic_summary, "semantic summary"),
            _load(args.events, "target Events"),
            _measurement_timestamp(args.target_submit_at),
            _load(args.prewarm_holder_pod, "cache prewarm holder Pod"),
            _load(args.prewarm_holder_receipt, "cache prewarm holder receipt"),
            _measurement_timestamp(args.prewarm_captured_at),
        )
    except EvidenceError as exc:
        print(f"conventional evidence refused: {exc}", file=sys.stderr)
        return 2
    json.dump(value, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
