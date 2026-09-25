#!/usr/bin/env python3
"""Fail-closed aggregation for fresh OpenFold2 or Boltz2 warm-node cohorts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

FASTSTART_ROOT = Path(__file__).resolve().parent.parent
if str(FASTSTART_ROOT) not in sys.path:
    sys.path.insert(0, str(FASTSTART_ROOT))

import qualification_receipt as qualification_builder
import instrumentation_contract as instrumentation_builder
from dynamo import evidence as openfold2_evidence


LEDGER_SCHEMA = "archvteams.nebius.ai/fresh-cohort-ledger-event/v1"
AGGREGATE_SCHEMA = "archvteams.nebius.ai/fresh-warm-node-cohort/v2"
QUALIFICATION_SCHEMA = "archvteams.nebius.ai/warm-instance-qualification/v3"
RESPONSE_CONTRACT = "request-dispatch-to-complete-http-body/v1"
PROXY_LABEL = "client-observed-api-create-response-return/v1"
MINIMUM_ATTEMPTS = 20
SHA256_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
OF2_TARGET_DIGEST = (
    "fc64916731fee39e124225829dca78e80ec24fe1891be47057d0d69209b93ab4"
)
OF2_TARGET_IMAGE = os.environ.get(
    "OF2_TARGET_IMAGE",
    f"registry.example.invalid/faststart/openfold2@sha256:{OF2_TARGET_DIGEST}",
)
if (
    OF2_TARGET_IMAGE.count("@sha256:") != 1
    or not OF2_TARGET_IMAGE.endswith(f"@sha256:{OF2_TARGET_DIGEST}")
    or any(character.isspace() for character in OF2_TARGET_IMAGE)
):
    raise RuntimeError("OF2_TARGET_IMAGE must retain the reviewed immutable digest")

APPROVED_CONTRACTS: dict[str, dict[str, str]] = {
    "openfold2": {
        "run_schema": "archvteams.nebius.ai/openfold2-faststart-run/v1",
        "checkpoint_id": "openfold2-native-f7-v1",
        "artifact_manifest_sha256": (
            "78368af3e6f143d7dc681632c4150b29f6354717103638b56e776244d9631b04"
        ),
        "artifact_pvc": os.environ.get(
            "OF2_ARTIFACT_PVC", "openfold2-artifacts-example"
        ),
        "cache_pvc": os.environ.get("OF2_CACHE_PVC", "openfold2-cache-example"),
        "restore_contract_sha256": (
            os.environ.get(
                "OF2_EXPECTED_CONTRACT_SHA256",
                "e9f6da8b0923a776ee305c3b5b351dcca40f1db8d914b76c64fc14aa9c03130b",
            )
        ),
        "target_image": OF2_TARGET_IMAGE,
    },
    "boltz2": {
        "run_schema": "archvteams.nebius.ai/boltz2-faststart-run/v1",
        "checkpoint_id": "boltz2-native-f7-v1",
        "artifact_manifest_sha256": (
            "6539b9f50a71c9f5fb6a3fbacd44f5d5ea41003539b6563682a38600d1492456"
        ),
        "artifact_pvc": os.environ.get(
            "B2_ARTIFACT_PVC", "boltz2-artifacts-example"
        ),
        "cache_pvc": os.environ.get("B2_CACHE_PVC", "boltz2-cache-example"),
        "restore_contract_sha256": (
            os.environ.get(
                "B2_EXPECTED_CONTRACT_SHA256",
                "ca266cc317802971d6f767bf8c28008338bd88dc7470d0fc45ed7084f6845e9c",
            )
        ),
        "target_image": (
            "nvcr.io/nim/mit/boltz2@sha256:"
            "0788c95c8b5b6c1a73a62c656b298ecc353a8187dc22b794f496ae40672c4c98"
        ),
    },
}


class AggregateError(ValueError):
    """The cohort cannot be safely or reproducibly aggregated."""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AggregateError(f"{label} must be a regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregateError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise AggregateError(f"{label} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AggregateError(f"hash source must be a regular non-symlink file: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AggregateError(f"cannot hash {path}: {type(exc).__name__}") from exc
    return digest.hexdigest()


def _read_text(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise AggregateError(f"{label} must be a regular non-symlink file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise AggregateError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not value:
        raise AggregateError(f"{label} is empty")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AggregateError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AggregateError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AggregateError(f"{label} timestamp has no UTC offset")
    return parsed


def _number(value: Any, label: str, *, allow_zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
        or (not allow_zero and float(value) == 0)
    ):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise AggregateError(f"{label} must be a finite {qualifier} number")
    return round(float(value), 6)


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise AggregateError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _canonical_sha256(value: Any, label: str) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AggregateError(f"{label} is not canonicalizable JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise AggregateError("ledger must be a regular non-symlink file")
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise AggregateError(f"ledger line {line_number} is empty")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AggregateError(
                        f"ledger line {line_number} is invalid JSON"
                    ) from exc
                if not isinstance(event, dict) or event.get("schema") != LEDGER_SCHEMA:
                    raise AggregateError(f"ledger line {line_number} has the wrong schema")
                events.append(event)
    except (OSError, UnicodeDecodeError) as exc:
        raise AggregateError(f"cannot read ledger: {type(exc).__name__}") from exc
    if not events:
        raise AggregateError("ledger is empty")
    return events


def _validate_semantics(path: Path, run_id: str, model: str) -> dict[str, Any]:
    semantic = _load_json(path, f"{run_id} semantic summary")
    cases = semantic.get("cases")
    if model == "openfold2":
        service_name = f"of2-canary-{run_id}"
        validator = "openfold2-faststart-semantic-v1"
        inference_path = "/biology/openfold/openfold2/predict-structure-from-msa-and-template"
    else:
        service_name = f"b2-canary-{run_id}"
        validator = "boltz2-faststart-semantic-v1"
        inference_path = "/biology/mit/boltz2/predict"
    base_url = f"http://{service_name}:8000"
    if (
        semantic.get("schema_version") != qualification_builder.SEMANTIC_SCHEMA_VERSION
        or semantic.get("validator") != validator
        or semantic.get("status") != "PASS"
        or semantic.get("ok") is not True
        or semantic.get("request_count") != 2
        or semantic.get("passed_case_count") != 2
        or semantic.get("failed_case_count") != 0
        or semantic.get("response_timing_contract") != RESPONSE_CONTRACT
        or semantic.get("base_url") != base_url
        or semantic.get("endpoint") != base_url + inference_path
        or semantic.get("inference_path") != inference_path
        or semantic.get("proxy_policy") != "disabled"
        or semantic.get("redirect_policy") != "reject"
        or not isinstance(cases, list)
        or len(cases) != 2
    ):
        raise AggregateError(f"{run_id} does not have exactly two semantic PASS calls")
    input_ids: list[str] = []
    request_hashes: list[str] = []
    response_hashes: list[str] = []
    fixed_sequences = ("ACDEFGHIKLMNPQRSTVWY", "YWVTSRQPNMLKIHGFEDCA")
    for index, case in enumerate(cases, 1):
        if (
            not isinstance(case, dict)
            or case.get("index") != index
            or case.get("status") != "PASS"
            or case.get("ok") is not True
            or not isinstance(case.get("invariant"), dict)
            or case.get("sequence") != fixed_sequences[index - 1]
        ):
            raise AggregateError(f"{run_id} semantic case {index} is not a strict PASS")
        _number(case.get("elapsed_seconds"), f"{run_id} semantic case {index}")
        _timestamp(case.get("request_started_at"), f"{run_id} request {index}")
        _timestamp(case.get("response_received_at"), f"{run_id} response {index}")
        expected_input_id = f"{run_id}-semantic-{'a' if index == 1 else 'b'}"
        if case.get("input_id") != expected_input_id or case.get("exit_code") != 0:
            raise AggregateError(f"{run_id} semantic case {index} identity is wrong")
        input_ids.append(str(case.get("input_id", "")))
        request_hashes.append(
            _digest(case.get("request_sha256"), f"{run_id} request {index}")
        )
        response_hashes.append(
            _digest(case.get("response_sha256"), f"{run_id} response {index}")
        )
    if len(set(input_ids)) != 2 or any(not value for value in input_ids):
        raise AggregateError(f"{run_id} semantic inputs are not distinct")
    for hashes, label in ((request_hashes, "requests"), (response_hashes, "responses")):
        if len(hashes) != 2 or len(set(hashes)) != 2:
            raise AggregateError(f"{run_id} semantic {label} are not distinct")
    return semantic


def _validate_qualification(
    value: dict[str, Any], *, model: str, run_id: str, pod_uid: str, pod_spec: str
) -> None:
    target = value.get("target")
    warm = value.get("warm_instance")
    pod_health = value.get("pod_health")
    gpu = value.get("gpu_health")
    boot = value.get("boot_time_alignment")
    boundaries = value.get("timing_boundaries")
    source_hashes = value.get("source_sha256")
    expected_health_phases = {
        "target": "Running",
        "worker": "Succeeded",
        "probe": "Succeeded",
    }
    if (
        value.get("schema") != QUALIFICATION_SCHEMA
        or value.get("status") != "PASS"
        or value.get("model") != model
        or value.get("run_id") != run_id
        or not isinstance(target, dict)
        or target.get("uid") != pod_uid
        or target.get("pod_spec_sha256") != pod_spec
        or not isinstance(target.get("image"), str)
        or IMAGE_DIGEST_RE.search(target["image"]) is None
        or not isinstance(warm, dict)
        or warm.get("target_image_already_present_before_t0") is not True
        or warm.get("target_image_pull_or_download_after_t0") is not False
        or not isinstance(pod_health, dict)
        or set(pod_health) != {"target", "worker", "probe"}
        or any(
            not isinstance(pod_health[name], dict)
            or pod_health[name].get("phase") != expected_health_phases[name]
            or pod_health[name].get("restart_count") != 0
            or pod_health[name].get("oom_killed") is not False
            or pod_health[name].get("evicted") is not False
            or pod_health[name].get("nonzero_termination") is not False
            for name in ("target", "worker", "probe")
        )
        or not isinstance(gpu, dict)
        or gpu.get("status") != "PASS"
        or gpu.get("scope") != "target-container"
        or not isinstance(gpu.get("attached_gpu_count"), int)
        or gpu["attached_gpu_count"] < 1
        or not isinstance(gpu.get("gpus"), list)
        or len(gpu["gpus"]) != gpu["attached_gpu_count"]
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("uuid"), str)
            or not item["uuid"]
            or not isinstance(item.get("product_name"), str)
            or not item["product_name"]
            or "H100" not in item["product_name"]
            for item in gpu["gpus"]
        )
        or gpu.get("host_xid_check", {}).get("status") != "unavailable"
        or not isinstance(gpu.get("host_xid_check", {}).get("reason"), str)
        or not gpu["host_xid_check"]["reason"]
        or not isinstance(boot, dict)
        or boot.get("status") != "PASS"
        or boot.get("method")
        != "pre-t0-ready-holder-clock-boottime-anchor/v1"
        or boot.get("worker_and_probe_must_share_target_node") is not True
        or not isinstance(boot.get("holder"), dict)
        or not isinstance(boot.get("node_clock_identity"), dict)
        or not isinstance(boot.get("anchor"), dict)
        or not isinstance(boot.get("controller_boundaries"), dict)
        or not isinstance(boot.get("semantic_boottime"), dict)
        or not isinstance(boot.get("conservative_upper_bounds"), dict)
        or set(boot["conservative_upper_bounds"])
        != {
            "http_ready_complete_body",
            "first_semantic_response_complete_body",
            "two_semantic_responses_complete_body",
        }
        or not isinstance(boundaries, dict)
        or boundaries.get("primary", {}).get("label")
        != "client-target-create-dispatch/v1"
        or boundaries.get("primary", {}).get(
            "conservative_relative_to_api_acceptance"
        )
        is not True
        or isinstance(boundaries.get("primary", {}).get("controller_monotonic_ns"), bool)
        or not isinstance(
            boundaries.get("primary", {}).get("controller_monotonic_ns"), int
        )
        or boundaries["primary"]["controller_monotonic_ns"] <= 0
        or boundaries.get("acceptance_response_proxy", {}).get("label") != PROXY_LABEL
        or boundaries.get("acceptance_response_proxy", {}).get(
            "is_exact_server_acceptance"
        )
        is not False
        or not isinstance(source_hashes, dict)
        or set(source_hashes)
        != {
            "capture_agent_absence",
            "admission_boundary",
            "anchor_holder",
            "boot_time_anchor",
            "probe_pod",
            "semantic_summary",
            "target_submit_clock",
            "target_create_response",
            "target_events",
            "target_nvidia_smi_xml",
            "target_nvidia_smi_stderr",
            "target_pod",
            "worker_pod",
            "worker_receipt",
        }
    ):
        raise AggregateError(f"{run_id} warm-instance qualification is incomplete")
    for label, digest in source_hashes.items():
        _digest(digest, f"{run_id} qualification source {label}")
    _digest(gpu.get("raw_xml_sha256"), f"{run_id} nvidia-smi XML")
    _digest(gpu.get("stderr_sha256"), f"{run_id} nvidia-smi stderr")
    if (
        gpu["raw_xml_sha256"] != source_hashes["target_nvidia_smi_xml"]
        or gpu["stderr_sha256"] != source_hashes["target_nvidia_smi_stderr"]
    ):
        raise AggregateError(f"{run_id} GPU receipt is not derived from its raw sources")
    events = warm.get("target_events")
    if (
        not isinstance(events, dict)
        or isinstance(events.get("warning_event_count"), bool)
        or not isinstance(events.get("warning_event_count"), int)
        or events["warning_event_count"] < 0
        or events.get("unexpected_warning_event_count") != 0
        or events.get("expected_startup_probe_warning_event_count")
        != events["warning_event_count"]
        or isinstance(
            events.get("expected_startup_probe_warning_occurrence_count"), bool
        )
        or not isinstance(
            events.get("expected_startup_probe_warning_occurrence_count"), int
        )
        or events["expected_startup_probe_warning_occurrence_count"]
        < events["expected_startup_probe_warning_event_count"]
        or events.get("maximum_expected_startup_probe_warning_window_seconds")
        != qualification_builder.MAX_EXPECTED_STARTUP_PROBE_WARNING_WINDOW_SECONDS
        or events.get("pulling_event_count") != 0
        or not isinstance(events.get("exact_image_already_present_event_count"), int)
        or events["exact_image_already_present_event_count"] < 1
    ):
        raise AggregateError(f"{run_id} target Events do not prove cached-image startup")


def _validate_holder(
    path: Path,
    *,
    run_id: str,
    node: str,
    claims: Sequence[str],
    primary_t0: datetime,
) -> dict[str, str]:
    receipt = _load_json(path, f"{run_id} warm storage holder")
    pod = receipt.get("pod")
    mount_receipts = receipt.get("mount_verifications")
    if (
        receipt.get("schema") != "archvteams.nebius.ai/warm-storage-holder-check/v1"
        or not isinstance(pod, dict)
        or _timestamp(receipt.get("checked_at"), f"{run_id} holder check") > primary_t0
        or pod.get("spec", {}).get("nodeName") != node
        or not isinstance(pod.get("metadata", {}).get("name"), str)
        or not isinstance(pod.get("metadata", {}).get("uid"), str)
        or not pod["metadata"]["name"]
        or not pod["metadata"]["uid"]
        or pod.get("metadata", {}).get("deletionTimestamp") is not None
        or not isinstance(mount_receipts, list)
        or len(mount_receipts) != len(claims)
        or not claims
        or len(set(claims)) != len(claims)
    ):
        raise AggregateError(f"{run_id} holder identity/timeline is invalid")
    conditions = pod.get("status", {}).get("conditions")
    statuses = pod.get("status", {}).get("containerStatuses")
    volumes = pod.get("spec", {}).get("volumes")
    containers = pod.get("spec", {}).get("containers")
    if (
        not isinstance(conditions, list)
        or not any(
            isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
            for item in conditions
        )
        or not isinstance(statuses, list)
        or not statuses
        or any(
            not isinstance(item, dict)
            or item.get("ready") is not True
            or item.get("restartCount") != 0
            for item in statuses
        )
        or not isinstance(volumes, list)
        or not isinstance(containers, list)
    ):
        raise AggregateError(f"{run_id} holder does not prove Ready attached storage")

    expected_receipts: list[dict[str, Any]] = []
    for claim in claims:
        matching_volumes = [
            item
            for item in volumes
            if isinstance(item, dict)
            and isinstance(item.get("persistentVolumeClaim"), dict)
            and item["persistentVolumeClaim"].get("claimName") == claim
        ]
        mounted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if len(matching_volumes) == 1:
            volume_name = matching_volumes[0].get("name")
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for mount in container.get("volumeMounts", []):
                    if isinstance(mount, dict) and mount.get("name") == volume_name:
                        mounted.append((container, mount))
        matching_receipts = [
            item
            for item in mount_receipts
            if isinstance(item, dict) and item.get("claim") == claim
        ]
        if (
            len(matching_volumes) != 1
            or len(mounted) != 1
            or len(matching_receipts) != 1
            or not isinstance(mounted[0][0].get("name"), str)
            or not mounted[0][0]["name"]
            or not isinstance(mounted[0][1].get("mountPath"), str)
            or not mounted[0][1]["mountPath"].startswith("/")
            or not any(
                isinstance(item, dict)
                and item.get("name") == mounted[0][0]["name"]
                and item.get("ready") is True
                and item.get("restartCount") == 0
                for item in statuses
            )
            or _timestamp(
                matching_receipts[0].get("checked_at"),
                f"{run_id} {claim} holder mount check",
            )
            > primary_t0
        ):
            raise AggregateError(f"{run_id} holder does not mount reviewed claim {claim}")
        expected_receipts.append(
            {
                "checked_at": matching_receipts[0]["checked_at"],
                "claim": claim,
                "container": mounted[0][0]["name"],
                "volume_name": matching_volumes[0]["name"],
                "mount_path": mounted[0][1]["mountPath"],
                "command": ["/bin/test", "-d", mounted[0][1]["mountPath"]],
                "status": "PASS",
                "exit_code": 0,
            }
        )
    if sorted(mount_receipts, key=lambda item: str(item.get("claim"))) != sorted(
        expected_receipts, key=lambda item: item["claim"]
    ):
        raise AggregateError(f"{run_id} holder mount receipts differ from the PodSpec")
    return {
        "name": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "claims": ",".join(sorted(claims)),
    }


def _validate_capture_agent_absence(
    path: Path, *, run_id: str, primary_t0: datetime
) -> None:
    receipt = _load_json(path, f"{run_id} capture-agent absence receipt")
    daemonsets = receipt.get("daemonset_list")
    items = daemonsets.get("items") if isinstance(daemonsets, dict) else None
    forbidden = "archvteams-2407-native-snapshot-agent"
    if (
        receipt.get("schema") != "archvteams.nebius.ai/capture-agent-absence/v1"
        or receipt.get("status") != "PASS"
        or receipt.get("namespace") != "nim-fast-start"
        or receipt.get("forbidden_name") != forbidden
        or _timestamp(receipt.get("checked_at"), f"{run_id} capture-agent check")
        > primary_t0
        or not isinstance(daemonsets, dict)
        or daemonsets.get("kind") not in {"DaemonSetList", "List"}
        or not isinstance(items, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("metadata", {}).get("name"), str)
            for item in items
        )
        or any(item["metadata"]["name"] == forbidden for item in items)
    ):
        raise AggregateError(f"{run_id} does not prove capture-agent absence")


def _strict_ns(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AggregateError(f"{label} must be a positive integer")
    return value


def _validate_boot_time_alignment(
    trial_dir: Path,
    *,
    run_id: str,
    qualification: dict[str, Any],
    admitted_at: datetime,
    target_node: str,
    holder_name: str,
    holder_uid: str,
) -> dict[str, float]:
    admission = _load_json(
        trial_dir / "admission-boundary.json", f"{run_id} admission boundary"
    )
    submit = _load_json(
        trial_dir / "target-submit-clock.json", f"{run_id} target-submit boundary"
    )
    anchor = _load_json(
        trial_dir / "boot-time-anchor.json", f"{run_id} BOOTTIME anchor"
    )
    anchor_holder = _load_json(
        trial_dir / "anchor-holder.json", f"{run_id} anchor holder"
    )
    semantic = _load_json(
        trial_dir / "semantic-summary.json", f"{run_id} semantic summary"
    )
    if (
        admission.get("schema")
        != qualification_builder.CONTROLLER_CLOCK_BOUNDARY_SCHEMA
        or admission.get("phase") != "cohort-admission"
        or submit.get("schema")
        != qualification_builder.CONTROLLER_CLOCK_BOUNDARY_SCHEMA
        or submit.get("phase") != "target-submit"
        or _timestamp(admission.get("utc"), f"{run_id} admission UTC")
        != admitted_at
        or submit.get("utc")
        != _read_text(trial_dir / "target-submit-at.txt", f"{run_id} target T0")
        or qualification.get("timing_boundaries", {}).get("primary", {}).get(
            "controller_monotonic_ns"
        )
        != submit.get("monotonic_ns")
    ):
        raise AggregateError(f"{run_id} controller BOOTTIME boundaries are inconsistent")
    admission_mono = _strict_ns(admission.get("monotonic_ns"), f"{run_id} admission")
    t0_mono = _strict_ns(submit.get("monotonic_ns"), f"{run_id} T0")
    before = anchor.get("controller_before")
    after = anchor.get("controller_after")
    node = anchor.get("node_observed")
    if (
        anchor.get("schema") != qualification_builder.BOOT_TIME_ANCHOR_SCHEMA
        or anchor.get("phase") != "pre-t0-anchor"
        or anchor.get("sampled_pod_name") != holder_name
        or anchor.get("sampled_pod_uid") != holder_uid
        or anchor.get("sampled_container") != "holder"
        or anchor.get("target_node") != target_node
        or anchor.get("expected_holder_image")
        != qualification_builder.BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(node, dict)
    ):
        raise AggregateError(f"{run_id} BOOTTIME anchor identity is inconsistent")
    before_mono = _strict_ns(before.get("monotonic_ns"), f"{run_id} anchor before")
    after_mono = _strict_ns(after.get("monotonic_ns"), f"{run_id} anchor after")
    _timestamp(before.get("utc"), f"{run_id} anchor before UTC")
    _timestamp(after.get("utc"), f"{run_id} anchor after UTC")
    maximum_ns = int(
        qualification_builder.MAX_ANCHOR_TO_T0_CONTROLLER_MONOTONIC_SECONDS
        * 1_000_000_000
    )
    if not (
        admission_mono <= before_mono <= after_mono <= t0_mono
        and t0_mono - before_mono <= maximum_ns
    ):
        raise AggregateError(f"{run_id} controller admission/anchor/T0 order is invalid")

    holder_meta = anchor_holder.get("metadata", {})
    holder_spec = anchor_holder.get("spec", {})
    holder_status = anchor_holder.get("status", {})
    holder_containers = holder_spec.get("containers", [])
    holder_statuses = holder_status.get("containerStatuses", [])
    if (
        holder_meta.get("name") != holder_name
        or holder_meta.get("uid") != holder_uid
        or holder_meta.get("deletionTimestamp") is not None
        or holder_spec.get("nodeName") != target_node
        or holder_status.get("phase") != "Running"
        or not isinstance(holder_containers, list)
        or len(holder_containers) != 1
        or holder_containers[0].get("name") != "holder"
        or holder_containers[0].get("image")
        != qualification_builder.BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or not isinstance(holder_statuses, list)
        or len(holder_statuses) != 1
        or holder_statuses[0].get("name") != "holder"
        or holder_statuses[0].get("imageID")
        != qualification_builder.BOOT_TIME_ANCHOR_HOLDER_IMAGE
        or holder_statuses[0].get("ready") is not True
        or isinstance(holder_statuses[0].get("restartCount"), bool)
        or holder_statuses[0].get("restartCount") != 0
    ):
        raise AggregateError(f"{run_id} BOOTTIME anchor holder is not exact")

    node_identity_keys = {
        "schema",
        "clock_id",
        "boot_id",
        "clock_resolution_ns",
        "timens_offsets",
    }
    semantic_identity = semantic.get("node_clock")
    if (
        set(node) != node_identity_keys | {"boottime_ns"}
        or not isinstance(semantic_identity, dict)
        or set(semantic_identity) != node_identity_keys
        or {key: node[key] for key in node_identity_keys} != semantic_identity
        or node.get("schema") != qualification_builder.SEMANTIC_NODE_BOOTTIME_SCHEMA
        or node.get("clock_id") != "CLOCK_BOOTTIME"
        or semantic.get("schema_version")
        != qualification_builder.SEMANTIC_SCHEMA_VERSION
    ):
        raise AggregateError(f"{run_id} semantic probe boot identity differs from anchor")
    anchor_boot = _strict_ns(node.get("boottime_ns"), f"{run_id} anchor BOOTTIME")
    resolution = _strict_ns(
        node.get("clock_resolution_ns"), f"{run_id} CLOCK_BOOTTIME resolution"
    )
    if resolution > qualification_builder.MAX_CLOCK_RESOLUTION_NS:
        raise AggregateError(f"{run_id} CLOCK_BOOTTIME resolution is unbounded")
    offsets = node.get("timens_offsets")
    if (
        not isinstance(offsets, list)
        or len(offsets) != 2
        or [item.get("clock") for item in offsets if isinstance(item, dict)]
        != ["monotonic", "boottime"]
        or any(
            set(item) != {"clock", "seconds", "nanoseconds"}
            or isinstance(item.get("seconds"), bool)
            or not isinstance(item.get("seconds"), int)
            or isinstance(item.get("nanoseconds"), bool)
            or not isinstance(item.get("nanoseconds"), int)
            for item in offsets
            if isinstance(item, dict)
        )
    ):
        raise AggregateError(f"{run_id} time namespace offsets are malformed")

    ready = semantic.get("ready_wait")
    cases = semantic.get("cases")
    if not isinstance(ready, dict) or not isinstance(cases, list) or len(cases) != 2:
        raise AggregateError(f"{run_id} semantic BOOTTIME events are missing")
    points = {
        "validation_start": _strict_ns(
            semantic.get("started_boottime_ns"), f"{run_id} validation start"
        ),
        "ready_start": _strict_ns(
            ready.get("started_boottime_ns"), f"{run_id} ready start"
        ),
        "ready_dispatch": _strict_ns(
            ready.get("request_dispatched_boottime_ns"), f"{run_id} ready dispatch"
        ),
        "ready_body": _strict_ns(
            ready.get("response_body_received_boottime_ns"), f"{run_id} ready body"
        ),
        "ready_finish": _strict_ns(
            ready.get("finished_boottime_ns"), f"{run_id} ready finish"
        ),
        "call1_dispatch": _strict_ns(
            cases[0].get("request_dispatched_boottime_ns"), f"{run_id} call1 dispatch"
        ),
        "call1_body": _strict_ns(
            cases[0].get("response_body_received_boottime_ns"), f"{run_id} call1 body"
        ),
        "call2_dispatch": _strict_ns(
            cases[1].get("request_dispatched_boottime_ns"), f"{run_id} call2 dispatch"
        ),
        "call2_body": _strict_ns(
            cases[1].get("response_body_received_boottime_ns"), f"{run_id} call2 body"
        ),
        "validation_finish": _strict_ns(
            semantic.get("validation_finished_boottime_ns"),
            f"{run_id} validation finish",
        ),
    }
    ordered = [anchor_boot, *points.values()]
    if anchor_boot >= points["validation_start"] or ordered != sorted(ordered):
        raise AggregateError(f"{run_id} semantic BOOTTIME event order is invalid")

    def reproduce(start: int, finish: int, recorded: Any, label: str) -> None:
        expected = round((finish - start) / 1_000_000_000, 6)
        if (
            isinstance(recorded, bool)
            or not isinstance(recorded, (int, float))
            or not math.isfinite(float(recorded))
            or float(recorded) != expected
        ):
            raise AggregateError(f"{run_id} {label} is not reproduced by BOOTTIME")

    reproduce(
        points["ready_start"],
        points["ready_finish"],
        ready.get("elapsed_seconds"),
        "readiness elapsed",
    )
    reproduce(
        points["call1_dispatch"],
        points["call1_body"],
        cases[0].get("elapsed_seconds"),
        "call1 elapsed",
    )
    reproduce(
        points["call2_dispatch"],
        points["call2_body"],
        cases[1].get("elapsed_seconds"),
        "call2 elapsed",
    )

    source_points = {
        "demand_to_http_ready_boottime_upper_seconds": (
            "http_ready_complete_body",
            points["ready_body"],
        ),
        "demand_to_first_semantic_boottime_upper_seconds": (
            "first_semantic_response_complete_body",
            points["call1_body"],
        ),
        "demand_to_two_semantic_boottime_upper_seconds": (
            "two_semantic_responses_complete_body",
            points["call2_body"],
        ),
    }
    receipt_upper = qualification.get("boot_time_alignment", {}).get(
        "conservative_upper_bounds"
    )
    if not isinstance(receipt_upper, dict):
        raise AggregateError(f"{run_id} BOOTTIME upper-bound receipt is missing")
    result: dict[str, float] = {}
    for metric_name, (receipt_name, event_boot) in source_points.items():
        upper_ns = event_boot - anchor_boot + 2 * resolution
        expected_receipt = {
            "event_boottime_ns": event_boot,
            "anchor_boottime_ns": anchor_boot,
            "event_minus_anchor_ns": event_boot - anchor_boot,
            "resolution_padding_ns": 2 * resolution,
            "upper_bound_ns": upper_ns,
            "upper_bound_seconds": math.ceil(upper_ns / 1_000) / 1_000_000,
        }
        if receipt_upper.get(receipt_name) != expected_receipt:
            raise AggregateError(f"{run_id} BOOTTIME upper bound differs from raw clocks")
        result[metric_name] = expected_receipt["upper_bound_seconds"]
    return result


def _validate_boltz_binding(
    trial_dir: Path,
    *,
    run_id: str,
    run: dict[str, Any],
    binding: dict[str, Any],
    target: dict[str, Any],
) -> None:
    target_name = f"b2-target-{run_id}"
    target_metadata = target.get("metadata", {})
    target_status = target.get("status", {})
    if (
        binding.get("schema") != "archvteams.nebius.ai/boltz2-target-binding/v1"
        or binding.get("namespace") != "nim-fast-start"
        or binding.get("pod_name") != target_name
        or binding.get("container_name") != "boltz2"
        or binding.get("image_id") != APPROVED_CONTRACTS["boltz2"]["target_image"]
        or binding.get("node") != run.get("target_node")
        or target.get("apiVersion") != "v1"
        or target.get("kind") != "Pod"
        or target_metadata.get("name") != target_name
        or target_metadata.get("namespace") != "nim-fast-start"
        or target_metadata.get("uid") != binding.get("pod_uid")
        or target_metadata.get("annotations", {}).get(
            "archvteams.nebius.ai/target-pod-spec-sha256"
        )
        != binding.get("pod_spec_sha256")
        or target.get("spec", {}).get("nodeName") != binding.get("node")
        or _canonical_sha256(target.get("spec"), f"{run_id} final target PodSpec")
        != binding.get("pod_spec_sha256")
        or target_status.get("phase") != "Running"
        or target_status.get("podIP") != binding.get("pod_ip")
    ):
        raise AggregateError(f"{run_id} Boltz2 target binding is inconsistent")
    containers = target.get("spec", {}).get("containers")
    target_statuses = target_status.get("containerStatuses")
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != "boltz2"
        or containers[0].get("image") != binding.get("image_id")
        or not isinstance(target_statuses, list)
        or len(
            [
                item
                for item in target_statuses
                if isinstance(item, dict) and item.get("name") == "boltz2"
            ]
        )
        != 1
    ):
        raise AggregateError(f"{run_id} Boltz2 target container is not bound")
    target_container_status = next(
        item
        for item in target_statuses
        if isinstance(item, dict) and item.get("name") == "boltz2"
    )
    if (
        target_container_status.get("containerID") != binding.get("container_id")
        or str(target_container_status.get("imageID", "")).removeprefix(
            "docker-pullable://"
        )
        != binding.get("image_id")
    ):
        raise AggregateError(f"{run_id} Boltz2 live container identity changed")

    service = _load_json(trial_dir / "canary-service.json", f"{run_id} Service")
    service_uid = service.get("metadata", {}).get("uid")
    service_name = f"b2-canary-{run_id}"
    if (
        service.get("apiVersion") != "v1"
        or service.get("kind") != "Service"
        or service.get("metadata", {}).get("name") != service_name
        or service.get("metadata", {}).get("namespace") != "nim-fast-start"
        or not isinstance(service_uid, str)
        or not service_uid
        or service.get("spec", {}).get("type") != "ClusterIP"
        or service.get("spec", {}).get("clusterIP") in (None, "", "None")
        or service.get("spec", {}).get("selector")
        != {
            "app.kubernetes.io/name": "boltz2",
            "app.kubernetes.io/component": "restore-target",
            "archvteams.nebius.ai/run-id": run_id,
        }
    ):
        raise AggregateError(f"{run_id} Boltz2 canary Service is not run-scoped")
    slices = _load_json(
        trial_dir / "canary-endpointslices.json", f"{run_id} EndpointSlices"
    )
    items = (
        slices.get("items")
        if slices.get("kind") in {"List", "EndpointSliceList"}
        else [slices]
    )
    if not isinstance(items, list):
        raise AggregateError(f"{run_id} EndpointSlice capture is malformed")
    endpoints: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("metadata", {}).get("labels", {}).get(
            "kubernetes.io/service-name"
        ) != service_name:
            continue
        owners = item.get("metadata", {}).get("ownerReferences")
        if not isinstance(owners, list) or not any(
            isinstance(owner, dict)
            and owner.get("kind") == "Service"
            and owner.get("uid") == service_uid
            and owner.get("controller") is True
            for owner in owners
        ):
            raise AggregateError(f"{run_id} EndpointSlice owner is not the canary Service")
        values = item.get("endpoints")
        if isinstance(values, list):
            endpoints.extend(value for value in values if isinstance(value, dict))
    if (
        len(endpoints) != 1
        or endpoints[0].get("targetRef", {}).get("uid") != binding.get("pod_uid")
        or endpoints[0].get("conditions", {}).get("ready") is not True
        or binding.get("pod_ip") not in endpoints[0].get("addresses", [])
    ):
        raise AggregateError(f"{run_id} semantic endpoint is not the bound target UID")

    for role, name, container_name in (
        ("worker", f"b2-restore-{run_id}", "restore-worker"),
        ("probe", f"b2-semantic-{run_id}", "semantic-probe"),
    ):
        job = _load_json(trial_dir / f"{role}-job.json", f"{run_id} {role} Job")
        rendered_job = _load_json(
            trial_dir / ("worker-bundle" if role == "worker" else "probe-bundle") / "primary.json",
            f"{run_id} rendered {role} Job",
        )
        pod = _load_json(trial_dir / f"{role}-pod.json", f"{run_id} {role} Pod")
        annotations = job.get("metadata", {}).get("annotations", {})
        job_uid = job.get("metadata", {}).get("uid")
        job_containers = job.get("spec", {}).get("template", {}).get("spec", {}).get(
            "containers"
        )
        rendered_containers = (
            rendered_job.get("spec", {}).get("template", {}).get("spec", {}).get(
                "containers"
            )
        )
        if (
            job.get("apiVersion") != "batch/v1"
            or job.get("kind") != "Job"
            or job.get("metadata", {}).get("name") != name
            or job.get("metadata", {}).get("namespace") != "nim-fast-start"
            or not isinstance(job_uid, str)
            or not job_uid
            or job.get("status", {}).get("succeeded") != 1
            or job.get("status", {}).get("failed", 0) not in (0, None)
            or annotations.get("archvteams.nebius.ai/target-pod-uid")
            != binding.get("pod_uid")
            or annotations.get("archvteams.nebius.ai/target-pod-spec-sha256")
            != binding.get("pod_spec_sha256")
            or rendered_job.get("apiVersion") != "batch/v1"
            or rendered_job.get("kind") != "Job"
            or rendered_job.get("metadata", {}).get("name") != name
            or not isinstance(job_containers, list)
            or len(job_containers) != 1
            or not isinstance(rendered_containers, list)
            or len(rendered_containers) != 1
            or job_containers[0].get("name") != container_name
            or any(
                job_containers[0].get(field) != rendered_containers[0].get(field)
                for field in ("image", "command", "args")
            )
        ):
            raise AggregateError(f"{run_id} {role} Job is not target-bound")
        owners = pod.get("metadata", {}).get("ownerReferences")
        pod_containers = pod.get("spec", {}).get("containers")
        if (
            pod.get("apiVersion") != "v1"
            or pod.get("kind") != "Pod"
            or pod.get("metadata", {}).get("namespace") != "nim-fast-start"
            or not isinstance(owners, list)
            or not any(
                isinstance(owner, dict)
                and owner.get("kind") == "Job"
                and owner.get("uid") == job_uid
                and owner.get("controller") is True
                for owner in owners
            )
            or pod.get("spec", {}).get("nodeName") != binding.get("node")
            or not isinstance(pod_containers, list)
            or len(pod_containers) != 1
            or pod_containers[0].get("name") != container_name
            or any(
                pod_containers[0].get(field) != job_containers[0].get(field)
                for field in ("image", "command", "args")
            )
        ):
            raise AggregateError(f"{run_id} {role} Pod is not owned and node-bound")

    worker = _load_json(
        trial_dir / "worker-receipt.json", f"{run_id} worker receipt"
    )
    expected_worker = {
        "schema": "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1",
        "status": "succeeded",
        "run_id": run_id,
        "target_namespace": "nim-fast-start",
        "target_name": target_name,
        "target_uid": binding.get("pod_uid"),
        "target_container_id": binding.get("container_id"),
        "target_image_id": binding.get("image_id"),
        "target_node": binding.get("node"),
        "target_pod_ip": binding.get("pod_ip"),
        "target_pod_spec_sha256": binding.get("pod_spec_sha256"),
        "checkpoint_id": run.get("checkpoint_id"),
        "artifact_version": run.get("artifact_version"),
        "checkpoint_manifest_sha256": run.get("artifact_manifest_sha256"),
        "tool_bundle_manifest_sha256": _load_json(
            trial_dir / "restore-interface.json", f"{run_id} restore interface"
        ).get("tool_bundle", {}).get("content_sha256"),
    }
    if (
        any(worker.get(key) != value for key, value in expected_worker.items())
        or isinstance(worker.get("duration_ms"), bool)
        or not isinstance(worker.get("duration_ms"), int)
        or worker["duration_ms"] < 0
    ):
        raise AggregateError(f"{run_id} Boltz2 worker receipt is not target-bound")


def _successful_attempt(
    *,
    model: str,
    run_id: str,
    trial_dir: Path,
    summary_path: Path,
    admitted_at: datetime,
) -> dict[str, Any]:
    expected_summary = (
        trial_dir / "canary-evidence.json"
        if model == "openfold2"
        else trial_dir / "trial-summary.json"
    )
    if summary_path != expected_summary:
        raise AggregateError(f"{run_id} ledger summary path is not run-scoped")
    summary = _load_json(summary_path, f"{run_id} summary")
    run = _load_json(trial_dir / "run.json", f"{run_id} run config")
    binding = _load_json(trial_dir / "binding.json", f"{run_id} binding")
    qualification = _load_json(
        trial_dir / "qualification-receipt.json", f"{run_id} qualification receipt"
    )
    target_final = _load_json(trial_dir / "target-final.json", f"{run_id} target Pod")
    approved = APPROVED_CONTRACTS[model]
    expected_run = {
        "schema": approved["run_schema"],
        "run_id": run_id,
        "checkpoint_id": approved["checkpoint_id"],
        "artifact_version": "1",
        "artifact_manifest_sha256": approved["artifact_manifest_sha256"],
        "artifact_pvc": approved["artifact_pvc"],
        "cache_pvc": approved["cache_pvc"],
    }
    if (
        summary.get("status") != "PASS"
        or summary.get("run_id") != run_id
        or any(run.get(key) != value for key, value in expected_run.items())
        or summary.get("response_timing_contract") != RESPONSE_CONTRACT
        or not isinstance(binding.get("pod_uid"), str)
        or not binding["pod_uid"]
        or not isinstance(binding.get("pod_spec_sha256"), str)
        or qualification.get("target", {}).get("image") != approved["target_image"]
    ):
        raise AggregateError(f"{run_id} summary/run/binding identity is inconsistent")
    _digest(binding["pod_spec_sha256"], f"{run_id} PodSpec")
    _validate_qualification(
        qualification,
        model=model,
        run_id=run_id,
        pod_uid=binding["pod_uid"],
        pod_spec=binding["pod_spec_sha256"],
    )
    demand_at = _timestamp(run.get("demand_at"), f"{run_id} setup demand")
    primary_t0 = _timestamp(
        qualification["timing_boundaries"]["primary"].get("timestamp"),
        f"{run_id} primary T0",
    )
    if demand_at > admitted_at or admitted_at > primary_t0:
        raise AggregateError(f"{run_id} was not admitted before its fresh T0")
    if not isinstance(run.get("target_node"), str):
        raise AggregateError(f"{run_id} target node is missing")
    _validate_capture_agent_absence(
        trial_dir / "capture-agent-absence.json",
        run_id=run_id,
        primary_t0=primary_t0,
    )
    artifact_holder = _validate_holder(
        trial_dir / "artifact-holder.json",
        run_id=run_id,
        node=run["target_node"],
        claims=(
            str(run.get("artifact_pvc", "")),
            str(run.get("cache_pvc", "")),
        )
        if model == "openfold2"
        else (str(run.get("artifact_pvc", "")),),
        primary_t0=primary_t0,
    )
    cache_holder = None
    if model == "boltz2":
        cache_holder = _validate_holder(
            trial_dir / "cache-holder.json",
            run_id=run_id,
            node=run["target_node"],
            claims=(str(run.get("cache_pvc", "")),),
            primary_t0=primary_t0,
        )
    target_volumes = target_final.get("spec", {}).get("volumes")
    target_claims = {
        item.get("persistentVolumeClaim", {}).get("claimName")
        for item in target_volumes
        if isinstance(item, dict)
        and isinstance(item.get("persistentVolumeClaim"), dict)
    } if isinstance(target_volumes, list) else set()
    if not {run.get("artifact_pvc"), run.get("cache_pvc")} <= target_claims:
        raise AggregateError(f"{run_id} target does not mount the fixed storage claims")
    if model == "boltz2":
        _validate_boltz_binding(
            trial_dir,
            run_id=run_id,
            run=run,
            binding=binding,
            target=target_final,
        )
    qualification_sources = {
        "capture_agent_absence": trial_dir / "capture-agent-absence.json",
        "admission_boundary": trial_dir / "admission-boundary.json",
        "target_submit_clock": trial_dir / "target-submit-clock.json",
        "boot_time_anchor": trial_dir / "boot-time-anchor.json",
        "anchor_holder": trial_dir / "anchor-holder.json",
        "target_create_response": trial_dir / "target-create-response.json",
        "target_pod": trial_dir / "target-final.json",
        "target_events": trial_dir / "target-events.json",
        "worker_pod": trial_dir / "worker-pod.json",
        "worker_receipt": trial_dir / "worker-receipt.json",
        "probe_pod": trial_dir / "probe-pod.json",
        "semantic_summary": trial_dir / "semantic-summary.json",
        "target_nvidia_smi_xml": trial_dir / "target-nvidia-smi.xml",
        "target_nvidia_smi_stderr": trial_dir / "target-nvidia-smi.stderr",
    }
    for label, path in qualification_sources.items():
        if qualification["source_sha256"].get(label) != _sha256(path):
            raise AggregateError(f"{run_id} qualification source {label} was modified")
    worker_pod = _load_json(trial_dir / "worker-pod.json", f"{run_id} worker Pod")
    probe_pod = _load_json(trial_dir / "probe-pod.json", f"{run_id} probe Pod")
    if (
        worker_pod.get("spec", {}).get("nodeName") != run["target_node"]
        or probe_pod.get("spec", {}).get("nodeName") != run["target_node"]
    ):
        raise AggregateError(f"{run_id} worker/probe clocks differ from the target node")
    target_name = f"{'of2' if model == 'openfold2' else 'b2'}-target-{run_id}"
    target_container = "openfold2" if model == "openfold2" else "boltz2"
    try:
        rebuilt_qualification = qualification_builder.build_receipt(
            model=model,
            run_id=run_id,
            namespace="nim-fast-start",
            target_name=target_name,
            target_container=target_container,
            expected_image=approved["target_image"],
            target_submit_at=_read_text(
                trial_dir / "target-submit-at.txt", f"{run_id} target T0"
            ),
            target_create_response_at=_read_text(
                trial_dir / "target-create-response-at.txt",
                f"{run_id} create-response timestamp",
            ),
            target_create_response=_load_json(
                trial_dir / "target-create-response.json",
                f"{run_id} target create response",
            ),
            target=target_final,
            target_events=_load_json(
                trial_dir / "target-events.json", f"{run_id} target Events"
            ),
            worker_pod=worker_pod,
            worker_receipt=_load_json(
                trial_dir / "worker-receipt.json", f"{run_id} worker receipt"
            ),
            worker_container="restore-worker",
            probe_pod=probe_pod,
            probe_container="semantic-probe",
            semantic_summary=_load_json(
                trial_dir / "semantic-summary.json", f"{run_id} semantic summary"
            ),
            gpu_health_xml=trial_dir / "target-nvidia-smi.xml",
            gpu_health_stderr=trial_dir / "target-nvidia-smi.stderr",
            admission_boundary=_load_json(
                trial_dir / "admission-boundary.json",
                f"{run_id} admission boundary",
            ),
            target_submit_clock=_load_json(
                trial_dir / "target-submit-clock.json",
                f"{run_id} target-submit boundary",
            ),
            boot_time_anchor=_load_json(
                trial_dir / "boot-time-anchor.json", f"{run_id} BOOTTIME anchor"
            ),
            anchor_holder=_load_json(
                trial_dir / "anchor-holder.json", f"{run_id} anchor holder"
            ),
            source_paths=qualification_sources,
        )
    except qualification_builder.QualificationError as exc:
        raise AggregateError(f"{run_id} raw qualification sources fail: {exc}") from exc
    if rebuilt_qualification != qualification:
        raise AggregateError(f"{run_id} qualification was not rebuilt from raw sources")
    boot_upper_metrics = _validate_boot_time_alignment(
        trial_dir,
        run_id=run_id,
        qualification=qualification,
        admitted_at=admitted_at,
        target_node=run["target_node"],
        holder_name=artifact_holder["name"],
        holder_uid=artifact_holder["uid"],
    )
    embedded = summary.get("qualification")
    if embedded != qualification:
        raise AggregateError(f"{run_id} summary does not embed the exact qualification")
    semantic = _validate_semantics(
        trial_dir / "semantic-summary.json", run_id, model
    )
    if model == "boltz2" and (
        summary.get("semantic") != semantic
        or summary.get("worker_receipt")
        != _load_json(trial_dir / "worker-receipt.json", f"{run_id} worker receipt")
    ):
        raise AggregateError(f"{run_id} Boltz2 summary embeds drifted source evidence")

    if model == "openfold2":
        try:
            rebuilt_openfold2 = openfold2_evidence.build_evidence(
                contract=_load_json(
                    trial_dir / "restore-interface.json", f"{run_id} restore contract"
                ),
                run=run,
                binding=binding,
                target=target_final,
                service=_load_json(
                    trial_dir / "canary-service.json", f"{run_id} canary Service"
                ),
                endpoint_slices=_load_json(
                    trial_dir / "canary-endpointslices.json",
                    f"{run_id} canary EndpointSlices",
                ),
                worker_job=_load_json(
                    trial_dir / "worker-job.json", f"{run_id} worker Job"
                ),
                worker_pod=worker_pod,
                worker_receipt=_load_json(
                    trial_dir / "worker-receipt.json", f"{run_id} worker receipt"
                ),
                probe_job=_load_json(
                    trial_dir / "probe-job.json", f"{run_id} probe Job"
                ),
                probe_pod=probe_pod,
                semantic_summary=semantic,
                target_submit_at=_read_text(
                    trial_dir / "target-submit-at.txt", f"{run_id} target T0"
                ),
                target_create_response_at=_read_text(
                    trial_dir / "target-create-response-at.txt",
                    f"{run_id} target create-response timestamp",
                ),
                qualification_receipt=qualification,
            )
        except (
            openfold2_evidence.EvidenceError,
            openfold2_evidence.render.RenderError,
        ) as exc:
            raise AggregateError(
                f"{run_id} raw OpenFold2 binding evidence fails: {exc}"
            ) from exc
        if rebuilt_openfold2 != summary:
            raise AggregateError(
                f"{run_id} OpenFold2 summary was not rebuilt from raw sources"
            )

    if model == "openfold2":
        timings = summary.get("timings_seconds")
        upper_timings = summary.get("timings_upper_bound_seconds")
        if not isinstance(timings, dict) or not isinstance(upper_timings, dict):
            raise AggregateError(f"{run_id} OpenFold2 timings are missing")
        fields = {
            "demand_to_http_ready_seconds": timings.get("demand_to_http_ready"),
            "demand_to_kubernetes_ready_seconds": timings.get(
                "demand_to_kubernetes_ready"
            ),
            "semantic_request_1_seconds": timings.get("semantic_request_1"),
            "semantic_request_2_seconds": timings.get("semantic_request_2"),
            "demand_to_two_semantic_seconds": timings.get(
                "demand_to_two_semantic_responses"
            ),
            "demand_to_first_semantic_seconds": timings.get(
                "demand_to_first_semantic_response"
            ),
            "target_create_api_round_trip_seconds": timings.get(
                "target_create_api_round_trip"
            ),
            "acceptance_response_proxy_to_http_ready_seconds": timings.get(
                "acceptance_response_proxy_to_http_ready"
            ),
            "acceptance_response_proxy_to_kubernetes_ready_seconds": timings.get(
                "acceptance_response_proxy_to_kubernetes_ready"
            ),
            "acceptance_response_proxy_to_two_semantic_seconds": timings.get(
                "acceptance_response_proxy_to_two_semantic_responses"
            ),
            "acceptance_response_proxy_to_first_semantic_seconds": timings.get(
                "acceptance_response_proxy_to_first_semantic_response"
            ),
        }
    else:
        fields = {
            name: summary.get(name)
            for name in (
                "demand_to_http_ready_seconds",
                "demand_to_kubernetes_ready_seconds",
                "semantic_request_1_seconds",
                "semantic_request_2_seconds",
                "demand_to_two_semantic_seconds",
                "demand_to_first_semantic_seconds",
                "target_create_api_round_trip_seconds",
                "acceptance_response_proxy_to_http_ready_seconds",
                "acceptance_response_proxy_to_kubernetes_ready_seconds",
                "acceptance_response_proxy_to_two_semantic_seconds",
                "acceptance_response_proxy_to_first_semantic_seconds",
            )
        }
    normalized = {
        name: _number(
            value,
            f"{run_id} {name}",
            allow_zero=("kubernetes_ready" in name or "api_round_trip" in name),
        )
        for name, value in fields.items()
    }
    if qualification["timing_boundaries"]["acceptance_response_proxy"][
        "client_observed_api_round_trip_seconds"
    ] != normalized["target_create_api_round_trip_seconds"]:
        raise AggregateError(f"{run_id} API RTT differs from qualification receipt")
    for primary_name, proxy_name in (
        (
            "demand_to_http_ready_seconds",
            "acceptance_response_proxy_to_http_ready_seconds",
        ),
        (
            "demand_to_two_semantic_seconds",
            "acceptance_response_proxy_to_two_semantic_seconds",
        ),
        (
            "demand_to_first_semantic_seconds",
            "acceptance_response_proxy_to_first_semantic_seconds",
        ),
    ):
        expected_proxy = round(
            normalized[primary_name]
            - normalized["target_create_api_round_trip_seconds"],
            6,
        )
        if not math.isclose(
            normalized[proxy_name], expected_proxy, rel_tol=0.0, abs_tol=0.000001
        ):
            raise AggregateError(f"{run_id} proxy timing does not match its API RTT")

    ready_wait = semantic.get("ready_wait")
    cases = semantic["cases"]
    conditions = target_final.get("status", {}).get("conditions")
    ready_conditions = (
        [
            item
            for item in conditions
            if isinstance(item, dict)
            and item.get("type") == "Ready"
            and item.get("status") == "True"
        ]
        if isinstance(conditions, list)
        else []
    )
    if not isinstance(ready_wait, dict) or len(ready_conditions) != 1:
        raise AggregateError(f"{run_id} raw readiness boundaries are missing")
    t0 = primary_t0
    proxy = _timestamp(
        qualification["timing_boundaries"]["acceptance_response_proxy"].get(
            "timestamp"
        ),
        f"{run_id} API response proxy",
    )
    semantic_started = _timestamp(semantic.get("started_at"), f"{run_id} probe start")
    ready_started = _timestamp(
        ready_wait.get("started_at"), f"{run_id} HTTP ready probe start"
    )
    http_ready = _timestamp(ready_wait.get("finished_at"), f"{run_id} HTTP ready")
    request_1 = _timestamp(cases[0].get("request_started_at"), f"{run_id} call 1 start")
    response_1 = _timestamp(
        cases[0].get("response_received_at"), f"{run_id} call 1 response"
    )
    request_2 = _timestamp(cases[1].get("request_started_at"), f"{run_id} call 2 start")
    response_2 = _timestamp(
        cases[1].get("response_received_at"), f"{run_id} call 2 response"
    )
    if not (
        t0
        <= proxy
        <= semantic_started
        <= ready_started
        <= http_ready
        <= request_1
        <= response_1
        <= request_2
        <= response_2
    ):
        raise AggregateError(f"{run_id} raw HTTP timing boundaries are not monotonic")
    kubernetes_ready = _timestamp(
        ready_conditions[0].get("lastTransitionTime"), f"{run_id} Kubernetes Ready"
    )

    def raw_seconds(start: datetime, end: datetime, label: str, *, coarse: bool = False) -> float:
        seconds = (end - start).total_seconds()
        if seconds < 0:
            if not coarse or abs(seconds) >= 1:
                raise AggregateError(f"{run_id} {label} has a negative duration")
            seconds = 0.0
        return round(seconds, 6)

    expected_metrics = {
        "demand_to_http_ready_seconds": raw_seconds(t0, http_ready, "HTTP ready"),
        "demand_to_kubernetes_ready_seconds": raw_seconds(
            t0, kubernetes_ready, "Kubernetes Ready", coarse=True
        ),
        "semantic_request_1_seconds": _number(
            cases[0].get("elapsed_seconds"), f"{run_id} call 1 elapsed"
        ),
        "semantic_request_2_seconds": _number(
            cases[1].get("elapsed_seconds"), f"{run_id} call 2 elapsed"
        ),
        "demand_to_two_semantic_seconds": raw_seconds(
            t0, response_2, "second semantic response"
        ),
        "demand_to_first_semantic_seconds": raw_seconds(
            t0, response_1, "first semantic response"
        ),
        "target_create_api_round_trip_seconds": raw_seconds(
            t0, proxy, "target create API RTT", coarse=True
        ),
        "acceptance_response_proxy_to_http_ready_seconds": raw_seconds(
            proxy, http_ready, "proxy to HTTP ready"
        ),
        "acceptance_response_proxy_to_kubernetes_ready_seconds": raw_seconds(
            proxy, kubernetes_ready, "proxy to Kubernetes Ready", coarse=True
        ),
        "acceptance_response_proxy_to_two_semantic_seconds": raw_seconds(
            proxy, response_2, "proxy to second semantic response"
        ),
        "acceptance_response_proxy_to_first_semantic_seconds": raw_seconds(
            proxy, response_1, "proxy to first semantic response"
        ),
    }
    if normalized != expected_metrics:
        raise AggregateError(f"{run_id} summary metrics differ from raw timestamps")
    if model == "openfold2":
        embedded_upper = {
            "demand_to_http_ready_boottime_upper_seconds": upper_timings.get(
                "demand_to_http_ready_boottime_upper"
            ),
            "demand_to_first_semantic_boottime_upper_seconds": upper_timings.get(
                "demand_to_first_semantic_response_boottime_upper"
            ),
            "demand_to_two_semantic_boottime_upper_seconds": upper_timings.get(
                "demand_to_two_semantic_responses_boottime_upper"
            ),
        }
    else:
        embedded_upper = {
            name: summary.get(name) for name in boot_upper_metrics
        }
    if embedded_upper != boot_upper_metrics:
        raise AggregateError(f"{run_id} summary BOOTTIME upper bounds differ from raw clocks")
    normalized.update(boot_upper_metrics)

    contract_path = trial_dir / "restore-interface.sha256"
    if contract_path.is_symlink() or not contract_path.is_file():
        raise AggregateError(f"{run_id} contract digest receipt is missing")
    contract_parts = contract_path.read_text(encoding="utf-8").strip().split()
    if len(contract_parts) != 2:
        raise AggregateError(f"{run_id} contract digest receipt is malformed")
    _digest(contract_parts[0], f"{run_id} restore contract")
    if contract_parts[0] != _sha256(trial_dir / "restore-interface.json"):
        raise AggregateError(f"{run_id} restore contract digest differs from the file")
    if contract_parts[0] != approved["restore_contract_sha256"]:
        raise AggregateError(f"{run_id} restore contract is not the approved production contract")
    _digest(run.get("artifact_manifest_sha256"), f"{run_id} artifact manifest")
    return {
        "run_id": run_id,
        "pod_uid": binding["pod_uid"],
        "pod_spec_sha256": binding["pod_spec_sha256"],
        "checkpoint_id": run.get("checkpoint_id"),
        "artifact_manifest_sha256": run.get("artifact_manifest_sha256"),
        "artifact_pvc": run.get("artifact_pvc"),
        "cache_pvc": run.get("cache_pvc"),
        "target_node": run.get("target_node"),
        "target_image": qualification["target"]["image"],
        "artifact_holder_name": artifact_holder["name"],
        "artifact_holder_uid": artifact_holder["uid"],
        "cache_holder_name": cache_holder["name"] if cache_holder else None,
        "cache_holder_uid": cache_holder["uid"] if cache_holder else None,
        "contract_sha256": contract_parts[0],
        "metrics": normalized,
    }


def _validate_cleanup(
    *,
    model: str,
    run_id: str,
    trial_dir: Path,
    admission_time: datetime,
    completion: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    cleanup_path = Path(str(completion.get("cleanup_receipt_path", "")))
    if cleanup_path != trial_dir / "cleanup-receipt.json":
        raise AggregateError(f"{run_id} cleanup path is not run-scoped")
    cleanup = _load_json(cleanup_path, f"{run_id} cleanup receipt")
    resources = cleanup.get("resources")
    if (
        cleanup.get("schema") != "archvteams.nebius.ai/run-cleanup-receipt/v1"
        or cleanup.get("run_id") != run_id
        or cleanup.get("requested") is not True
        or cleanup.get("status") not in {"PASS", "FAIL"}
        or completion.get("cleanup_status") != cleanup.get("status")
        or not isinstance(resources, list)
        or not resources
    ):
        raise AggregateError(f"{run_id} cleanup receipt is malformed")
    calculated_cleanup_failure = False
    successful_resources: list[dict[str, Any]] = []
    object_resources: list[dict[str, Any]] = []
    group_errors: dict[str, dict[str, Any]] = {}
    for resource_index, resource in enumerate(resources, 1):
        if not isinstance(resource, dict):
            raise AggregateError(f"{run_id} cleanup resource {resource_index} is malformed")
        group_role = resource.get("group_role")
        resource_kind = resource.get("resource_kind")
        create_attempted = resource.get("create_attempted")
        delete_attempted = resource.get("delete_attempted")
        preconditioned = resource.get("uid_precondition_enforced")
        lookup_exit_code = resource.get("lookup_exit_code")
        delete_exit_code = resource.get("delete_exit_code")
        wait_exit_code = resource.get("wait_exit_code")
        if (
            group_role
            not in {"target", "target-support", "restore-worker", "semantic-probe"}
            or not isinstance(resource_kind, str)
            or not isinstance(create_attempted, bool)
            or not isinstance(delete_attempted, bool)
            or not isinstance(preconditioned, bool)
            or any(
                isinstance(code, bool) or not isinstance(code, int) or code < 0
                for code in (lookup_exit_code, delete_exit_code, wait_exit_code)
            )
        ):
            raise AggregateError(f"{run_id} cleanup resource {resource_index} is malformed")
        status = resource.get("status")
        if resource_kind == "group":
            if (
                status not in {"not-created", "uid-receipt-missing", "uid-receipt-incomplete"}
                or delete_attempted
                or preconditioned
                or (status == "not-created" and create_attempted)
                or (status != "not-created" and not create_attempted)
            ):
                raise AggregateError(f"{run_id} cleanup group error is inconsistent")
            if group_role in group_errors:
                raise AggregateError(f"{run_id} cleanup group error is duplicated")
            group_errors[group_role] = resource
            calculated_cleanup_failure |= status != "not-created"
            continue
        expected_uid = resource.get("expected_uid")
        observed_uid = resource.get("observed_uid_before_delete")
        if not isinstance(expected_uid, str) or not expected_uid:
            raise AggregateError(f"{run_id} cleanup UID receipt is missing")
        object_resources.append(resource)
        if status == "uid-precondition-deleted":
            if (
                not delete_attempted
                or not preconditioned
                or resource.get("delete_transport")
                != "kubectl-authenticated-local-proxy"
                or expected_uid != observed_uid
                or any(code != 0 for code in (lookup_exit_code, delete_exit_code, wait_exit_code))
            ):
                raise AggregateError(f"{run_id} UID deletion receipt is inconsistent")
            successful_resources.append(resource)
        elif status == "already-absent":
            if delete_attempted or preconditioned or lookup_exit_code != 0 or observed_uid:
                raise AggregateError(f"{run_id} absent cleanup receipt is inconsistent")
            successful_resources.append(resource)
        else:
            if status not in {
                "lookup-failed",
                "lookup-identity-missing",
                "uid-mismatch-preserved",
                "uid-proxy-unavailable",
                "uid-delete-options-failed",
                "uid-delete-failed",
                "uid-delete-wait-failed",
                "unsupported-resource-kind",
            }:
                raise AggregateError(f"{run_id} cleanup status is unknown")
            calculated_cleanup_failure = True
    expected_cleanup_status = "FAIL" if calculated_cleanup_failure else "PASS"
    if cleanup["status"] != expected_cleanup_status:
        raise AggregateError(f"{run_id} cleanup status is inconsistent")

    expected_success_shape = sorted(
        [
            ("semantic-probe", "job"),
            ("semantic-probe", "configmap"),
            ("restore-worker", "job"),
            ("restore-worker", "rolebinding"),
            ("restore-worker", "role"),
            ("restore-worker", "serviceaccount"),
            ("target", "pod"),
            ("target-support", "networkpolicy"),
            ("target-support", "networkpolicy"),
            ("target-support", "service"),
            ("target-support", "service"),
        ]
    )
    actual_success_shape = sorted(
        (str(item["group_role"]), str(item["resource_kind"]))
        for item in successful_resources
    )
    prefix = "of2" if model == "openfold2" else "b2"
    expected_resource_identities = sorted(
        [
            ("semantic-probe", "job", f"{prefix}-semantic-{run_id}"),
            ("semantic-probe", "configmap", f"{prefix}-semantic-{run_id}"),
            ("restore-worker", "job", f"{prefix}-restore-{run_id}"),
            ("restore-worker", "rolebinding", f"{prefix}-restore-{run_id}"),
            ("restore-worker", "role", f"{prefix}-restore-{run_id}"),
            ("restore-worker", "serviceaccount", f"{prefix}-restore-{run_id}"),
            ("target", "pod", f"{prefix}-target-{run_id}"),
            ("target-support", "networkpolicy", f"{prefix}-probe-{run_id}"),
            ("target-support", "networkpolicy", f"{prefix}-target-{run_id}"),
            ("target-support", "service", f"{prefix}-canary-{run_id}"),
            ("target-support", "service", f"{prefix}-qualified-{run_id}"),
        ]
    )
    actual_resource_identities = sorted(
        (
            str(item["group_role"]),
            str(item["resource_kind"]),
            str(item.get("resource_name", "")),
        )
        for item in successful_resources
    )
    successful_uids = [item["expected_uid"] for item in successful_resources]
    all_expected_uids = [item["expected_uid"] for item in object_resources]
    captured_resources: list[tuple[str, str, str, str]] = []
    for group_role, filename in (
        ("target", "target-create-response.json"),
        ("target-support", "target-support-create-response.json"),
        ("restore-worker", "worker-create-response.json"),
        ("semantic-probe", "probe-create-response.json"),
    ):
        marker = group_errors.get(group_role)
        group_objects = [
            item for item in object_resources if item.get("group_role") == group_role
        ]
        if marker is not None and marker["status"] in {
            "not-created",
            "uid-receipt-missing",
        }:
            if group_objects:
                raise AggregateError(
                    f"{run_id} {group_role} cleanup contradicts its group receipt"
                )
            continue
        if marker is None and not group_objects:
            raise AggregateError(f"{run_id} {group_role} cleanup evidence is absent")
        capture = _load_json(trial_dir / filename, f"{run_id} {group_role} create receipt")
        values = capture.get("items") if capture.get("kind") == "List" else [capture]
        if not isinstance(values, list):
            raise AggregateError(f"{run_id} {group_role} create receipt is malformed")
        for value in values:
            if not isinstance(value, dict):
                raise AggregateError(f"{run_id} {group_role} create object is malformed")
            api_kind = (value.get("apiVersion"), value.get("kind"))
            kind_map = {
                ("v1", "Pod"): "pod",
                ("v1", "Service"): "service",
                ("v1", "ConfigMap"): "configmap",
                ("v1", "ServiceAccount"): "serviceaccount",
                ("batch/v1", "Job"): "job",
                ("networking.k8s.io/v1", "NetworkPolicy"): "networkpolicy",
                ("rbac.authorization.k8s.io/v1", "Role"): "role",
                ("rbac.authorization.k8s.io/v1", "RoleBinding"): "rolebinding",
            }
            name = value.get("metadata", {}).get("name")
            uid = value.get("metadata", {}).get("uid")
            if (
                api_kind not in kind_map
                or value.get("metadata", {}).get("namespace") != "nim-fast-start"
                or not isinstance(name, str)
                or not name
                or not isinstance(uid, str)
                or not uid
            ):
                raise AggregateError(f"{run_id} {group_role} create identity is incomplete")
            captured_resources.append((group_role, kind_map[api_kind], name, uid))
    cleanup_resource_receipts = sorted(
        (
            str(item["group_role"]),
            str(item["resource_kind"]),
            str(item["resource_name"]),
            str(item["expected_uid"]),
        )
        for item in object_resources
    )
    if (
        len(set(all_expected_uids)) != len(all_expected_uids)
        or cleanup_resource_receipts != sorted(captured_resources)
    ):
        raise AggregateError(f"{run_id} cleanup identities differ from create receipts")

    completed_at = _timestamp(completion.get("completed_at"), f"{run_id} completion")
    cleanup_started = _timestamp(cleanup.get("started_at"), f"{run_id} cleanup start")
    cleanup_completed = _timestamp(
        cleanup.get("completed_at"), f"{run_id} cleanup completion"
    )
    if not admission_time <= cleanup_started <= cleanup_completed <= completed_at:
        raise AggregateError(f"{run_id} cleanup timeline is invalid")

    exit_code = completion.get("runner_exit_code")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not 0 <= exit_code <= 255
    ):
        raise AggregateError(f"{run_id} runner exit code is invalid")
    attempt_result = _load_json(
        trial_dir / "attempt-result.json", f"{run_id} attempt result"
    )
    original_exit = attempt_result.get("original_runner_exit_code")
    if (
        attempt_result.get("schema")
        != "archvteams.nebius.ai/runner-attempt-result/v1"
        or attempt_result.get("run_id") != run_id
        or attempt_result.get("model") != model
        or attempt_result.get("admitted") is not True
        or attempt_result.get("completed_at") != completion.get("completed_at")
        or attempt_result.get("cleanup_status") != cleanup["status"]
        or attempt_result.get("final_exit_code") != exit_code
        or isinstance(original_exit, bool)
        or not isinstance(original_exit, int)
        or not 0 <= original_exit <= 255
        or cleanup.get("original_runner_exit_code") != original_exit
    ):
        raise AggregateError(f"{run_id} attempt result is inconsistent")
    if exit_code == 0 and (
        original_exit != 0
        or cleanup["status"] != "PASS"
        or actual_success_shape != expected_success_shape
        or actual_resource_identities != expected_resource_identities
        or len(set(successful_uids)) != len(successful_uids)
        or any(not item["create_attempted"] for item in successful_resources)
    ):
        raise AggregateError(f"{run_id} successful result lacks full cleanup")
    return cleanup, exit_code


def _metric_block(
    name: str, attempts: list[dict[str, Any]], successful: list[dict[str, Any]]
) -> dict[str, Any]:
    values_by_run = [
        {"run_id": item["run_id"], "seconds": item["metrics"][name]}
        for item in successful
    ]
    ordered = sorted(item["seconds"] for item in values_by_run)
    attempt_count = len(attempts)

    def percentile(percent: float) -> dict[str, Any]:
        rank = math.ceil(percent * attempt_count)
        if rank <= len(ordered):
            return {"rank": rank, "seconds": ordered[rank - 1], "status": "MEASURED"}
        return {"rank": rank, "seconds": None, "status": "FAILED_ATTEMPT_AT_RANK"}

    return {
        "estimator": "nearest-rank-with-failed-attempts-sorted-after-successes/v1",
        "attempt_count": attempt_count,
        "successful_sample_count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "maximum": {
            "seconds": max(ordered) if len(ordered) == attempt_count else None,
            "status": "MEASURED" if len(ordered) == attempt_count else "FAILED_ATTEMPT_PRESENT",
        },
        "successful_maximum_seconds": max(ordered) if ordered else None,
        "values_by_run": values_by_run,
    }


def aggregate(ledger_path: Path, model: str) -> dict[str, Any]:
    events = _read_ledger(ledger_path)
    allowed_event_types = {
        "cohort_started",
        "pre_admission_rejection",
        "admitted",
        "completed",
        "controller_abort",
        "cohort_finished",
    }
    if (
        events[0].get("event") != "cohort_started"
        or events[-1].get("event") != "cohort_finished"
        or any(event.get("event") not in allowed_event_types for event in events)
    ):
        raise AggregateError("ledger start, finish, or event type is invalid")
    starts = [event for event in events if event.get("event") == "cohort_started"]
    finishes = [event for event in events if event.get("event") == "cohort_finished"]
    rejected = [
        event for event in events if event.get("event") == "pre_admission_rejection"
    ]
    admitted = [event for event in events if event.get("event") == "admitted"]
    completed = [event for event in events if event.get("event") == "completed"]
    if len(starts) != 1 or len(finishes) != 1:
        raise AggregateError("ledger must contain exactly one cohort start and finish")
    start, finish = starts[0], finishes[0]
    cohort_id = start.get("cohort_id")
    requested = start.get("requested_attempt_count")
    run_prefix = start.get("run_prefix")
    evidence_root = Path(str(start.get("evidence_root", "")))
    runner_sha256 = start.get("runner_sha256")
    instrumentation_contract_sha256 = start.get(
        "instrumentation_contract_sha256"
    )
    maximum_scheduled = start.get("maximum_scheduled_attempts")
    cohort_started = _timestamp(start.get("started_at"), "cohort start")
    cohort_finished = _timestamp(finish.get("finished_at"), "cohort finish")
    if (
        start.get("model") != model
        or finish.get("model") != model
        or finish.get("cohort_id") != cohort_id
        or not isinstance(cohort_id, str)
        or not cohort_id
        or not isinstance(run_prefix, str)
        or not run_prefix
        or not evidence_root.is_absolute()
        or evidence_root.is_symlink()
        or not evidence_root.is_dir()
        or (evidence_root / "runs").is_symlink()
        or not (evidence_root / "runs").is_dir()
        or ledger_path != evidence_root / "cohorts" / str(cohort_id) / "attempts.ndjson"
        or SHA256_RE.fullmatch(str(runner_sha256)) is None
        or SHA256_RE.fullmatch(str(instrumentation_contract_sha256)) is None
        or not isinstance(requested, int)
        or isinstance(requested, bool)
        or requested < MINIMUM_ATTEMPTS
        or not isinstance(maximum_scheduled, int)
        or isinstance(maximum_scheduled, bool)
        or maximum_scheduled < requested
        or finish.get("requested_attempt_count") != requested
        or finish.get("controller_abort") is not False
        or any(event.get("event") == "controller_abort" for event in events)
        or cohort_finished < cohort_started
    ):
        raise AggregateError("cohort header/footer does not meet the fresh n>=20 contract")
    try:
        expected_instrumentation = instrumentation_builder.build_contract(model)
    except instrumentation_builder.InstrumentationContractError as exc:
        raise AggregateError(f"cannot rebuild instrumentation contract: {exc}") from exc
    instrumentation_path = (
        evidence_root / "cohorts" / str(cohort_id) / "instrumentation-contract.json"
    )
    captured_instrumentation = _load_json(
        instrumentation_path, "cohort instrumentation contract"
    )
    if (
        captured_instrumentation != expected_instrumentation
        or instrumentation_contract_sha256
        != expected_instrumentation["instrumentation_contract_sha256"]
    ):
        raise AggregateError("cohort instrumentation contract drifted or is not current")
    if len(admitted) != requested or len(admitted) < MINIMUM_ATTEMPTS:
        raise AggregateError("cohort admission count differs from the fresh request")
    scheduled = finish.get("scheduled_attempt_count")
    if (
        finish.get("admitted_attempt_count") != len(admitted)
        or isinstance(scheduled, bool)
        or not isinstance(scheduled, int)
        or not len(admitted) <= scheduled <= maximum_scheduled
        or scheduled != len(admitted) + len(rejected)
    ):
        raise AggregateError("cohort footer scheduled/admission count differs from the ledger")

    # Every scheduled runner invocation must have exactly one visible outcome.
    # Enforce the controller's serial state machine so omitted pre-admission
    # failures cannot disappear from the acquisition denominator.
    expected_schedule_ordinal = 1
    active_run: str | None = None
    for event in events[1:-1]:
        event_type = event.get("event")
        expected_run_id = f"{run_prefix}-{expected_schedule_ordinal:03d}"
        if event_type == "admitted":
            if active_run is not None or event.get("run_id") != expected_run_id:
                raise AggregateError("scheduled outcomes are not one serial run sequence")
            active_run = expected_run_id
            expected_schedule_ordinal += 1
        elif event_type == "completed":
            if active_run is None or event.get("run_id") != active_run:
                raise AggregateError("completion does not close the active scheduled run")
            active_run = None
        elif event_type == "pre_admission_rejection":
            exit_code = event.get("observed_runner_exit_code")
            observed_at = _timestamp(
                event.get("observed_at"), "pre-admission rejection"
            )
            if (
                active_run is not None
                or event.get("run_id") != expected_run_id
                or event.get("model") != model
                or event.get("cohort_id") != cohort_id
                or isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
                or not 1 <= exit_code <= 255
                or not cohort_started <= observed_at <= cohort_finished
            ):
                raise AggregateError("pre-admission rejection is malformed or out of order")
            rejected_dir = evidence_root / "runs" / expected_run_id
            if rejected_dir.exists() or rejected_dir.is_symlink():
                if rejected_dir.is_symlink() or not rejected_dir.is_dir():
                    raise AggregateError("pre-admission evidence path is unsafe")
                rejected_result = _load_json(
                    rejected_dir / "attempt-result.json",
                    f"{expected_run_id} rejected attempt result",
                )
                rejected_cleanup = _load_json(
                    rejected_dir / "cleanup-receipt.json",
                    f"{expected_run_id} rejected cleanup receipt",
                )
                if (
                    rejected_result.get("schema")
                    != "archvteams.nebius.ai/runner-attempt-result/v1"
                    or rejected_result.get("run_id") != expected_run_id
                    or rejected_result.get("model") != model
                    or rejected_result.get("admitted") is not False
                    or rejected_result.get("cleanup_status") != "PASS"
                    or rejected_result.get("final_exit_code") != exit_code
                    or rejected_cleanup.get("schema")
                    != "archvteams.nebius.ai/run-cleanup-receipt/v1"
                    or rejected_cleanup.get("run_id") != expected_run_id
                    or rejected_cleanup.get("requested") is not True
                    or rejected_cleanup.get("status") != "PASS"
                ):
                    raise AggregateError(
                        "pre-admission rejection does not prove safe cleanup"
                    )
            expected_schedule_ordinal += 1
        else:
            raise AggregateError("unexpected event inside completed cohort")
    if active_run is not None or expected_schedule_ordinal - 1 != scheduled:
        raise AggregateError("not every scheduled runner has one accounted outcome")

    run_ids = [event.get("run_id") for event in admitted]
    indices = [event.get("attempt_index") for event in admitted]
    run_pattern = re.compile(rf"{re.escape(run_prefix)}-[0-9]{{3}}")
    if (
        any(
            event.get("model") != model
            or event.get("cohort_id") != cohort_id
            or event.get("runner_sha256") != runner_sha256
            or event.get("instrumentation_contract_sha256")
            != instrumentation_contract_sha256
            or run_pattern.fullmatch(str(event.get("run_id", ""))) is None
            or not cohort_started
            <= _timestamp(event.get("admitted_at"), "attempt admission")
            <= cohort_finished
            for event in admitted
        )
        or len(set(run_ids)) != len(run_ids)
        or any(not isinstance(run_id, str) or not run_id for run_id in run_ids)
        or indices != list(range(1, len(admitted) + 1))
    ):
        raise AggregateError("admission records are not one ordered fresh run sequence")
    completion_by_run: dict[str, dict[str, Any]] = {}
    event_positions = {id(event): index for index, event in enumerate(events)}
    for event in completed:
        run_id = event.get("run_id")
        if (
            event.get("model") != model
            or event.get("cohort_id") != cohort_id
            or run_id not in run_ids
            or run_id in completion_by_run
        ):
            raise AggregateError("completion records are duplicated or outside the cohort")
        completion_by_run[str(run_id)] = event
    if set(completion_by_run) != set(run_ids):
        raise AggregateError("every admitted attempt must have one completion record")

    for index, admission in enumerate(admitted):
        completion = completion_by_run[str(admission["run_id"])]
        admission_position = event_positions[id(admission)]
        completion_position = event_positions[id(completion)]
        if completion_position <= admission_position:
            raise AggregateError("completion must follow its own admission")
        if index + 1 < len(admitted):
            next_admission = admitted[index + 1]
            if (
                completion_position >= event_positions[id(next_admission)]
                or _timestamp(
                    completion.get("completed_at"), "serial completion"
                )
                > _timestamp(next_admission.get("admitted_at"), "serial admission")
            ):
                raise AggregateError("cohort attempts are not serial and non-overlapping")

    attempts: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    for admission in admitted:
        run_id = str(admission["run_id"])
        completion = completion_by_run[run_id]
        trial_dir = Path(str(admission.get("trial_dir", "")))
        admission_time = _timestamp(admission.get("admitted_at"), f"{run_id} admission")
        completion_time = _timestamp(
            completion.get("completed_at"), f"{run_id} completion"
        )
        if (
            trial_dir != evidence_root / "runs" / run_id
            or not trial_dir.is_absolute()
            or trial_dir.is_symlink()
            or not trial_dir.is_dir()
            or completion.get("trial_dir") != str(trial_dir)
            or completion.get("attempt_index") != admission.get("attempt_index")
            or event_positions[id(completion)] <= event_positions[id(admission)]
            or not admission_time <= completion_time <= cohort_finished
        ):
            raise AggregateError(f"{run_id} trial directory/timeline is invalid")
        run_instrumentation = _load_json(
            trial_dir / "instrumentation-contract.json",
            f"{run_id} instrumentation contract",
        )
        if run_instrumentation != captured_instrumentation:
            raise AggregateError(
                f"{run_id} does not share the homogeneous instrumentation contract"
            )
        expected_summary = trial_dir / (
            "canary-evidence.json" if model == "openfold2" else "trial-summary.json"
        )
        if Path(str(completion.get("summary_path", ""))) != expected_summary:
            raise AggregateError(f"{run_id} summary path is not run-scoped")
        cleanup, exit_code = _validate_cleanup(
            model=model,
            run_id=run_id,
            trial_dir=trial_dir,
            admission_time=admission_time,
            completion=completion,
        )
        attempt = {
            "attempt_index": admission["attempt_index"],
            "run_id": run_id,
            "runner_exit_code": exit_code,
            "cleanup_status": cleanup["status"],
            "status": "PASS" if exit_code == 0 and cleanup["status"] == "PASS" else "FAIL",
        }
        attempts.append(attempt)
        if attempt["status"] == "PASS":
            summary_path = Path(str(completion.get("summary_path", "")))
            success = _successful_attempt(
                model=model,
                run_id=run_id,
                trial_dir=trial_dir,
                summary_path=summary_path,
                admitted_at=admission_time,
            )
            successful.append(success)

    pod_uids = [item["pod_uid"] for item in successful]
    if len(set(pod_uids)) != len(pod_uids):
        raise AggregateError("successful target Pod UIDs are not unique")
    immutable_fields = (
        "pod_spec_sha256",
        "checkpoint_id",
        "artifact_manifest_sha256",
        "artifact_pvc",
        "cache_pvc",
        "target_node",
        "target_image",
        "contract_sha256",
        "artifact_holder_name",
        "artifact_holder_uid",
    )
    if model == "boltz2":
        immutable_fields += ("cache_holder_name", "cache_holder_uid")
    immutable: dict[str, Any] = {}
    for field in immutable_fields:
        values = {item.get(field) for item in successful}
        if successful and (len(values) != 1 or None in values or "" in values):
            raise AggregateError(f"successful attempts do not share one {field}")
        immutable[field] = next(iter(values)) if values else None

    metric_names = (
        "demand_to_http_ready_seconds",
        "demand_to_kubernetes_ready_seconds",
        "semantic_request_1_seconds",
        "semantic_request_2_seconds",
        "demand_to_first_semantic_seconds",
        "demand_to_two_semantic_seconds",
        "target_create_api_round_trip_seconds",
        "acceptance_response_proxy_to_http_ready_seconds",
        "acceptance_response_proxy_to_kubernetes_ready_seconds",
        "acceptance_response_proxy_to_first_semantic_seconds",
        "acceptance_response_proxy_to_two_semantic_seconds",
        "demand_to_http_ready_boottime_upper_seconds",
        "demand_to_first_semantic_boottime_upper_seconds",
        "demand_to_two_semantic_boottime_upper_seconds",
    )
    metrics = {
        name: _metric_block(name, attempts, successful) for name in metric_names
    }
    failure_count = sum(item["status"] == "FAIL" for item in attempts)
    primary_p95 = metrics["demand_to_two_semantic_seconds"]["p95"]["seconds"]
    primary_upper_p95 = metrics[
        "demand_to_two_semantic_boottime_upper_seconds"
    ]["p95"]["seconds"]
    status = (
        "PASS"
        if failure_count == 0
        and primary_upper_p95 is not None
        and primary_upper_p95 < 30
        else "FAIL"
    )
    return {
        "schema": AGGREGATE_SCHEMA,
        "status": status,
        "model": model,
        "cohort_id": cohort_id,
        "fresh_cohort": True,
        "old_n3_mixed": False,
        "scheduled_runner_count": scheduled,
        "pre_admission_rejection_count": len(rejected),
        "attempt_count": len(attempts),
        "successful_attempt_count": len(successful),
        "failure_count": failure_count,
        "failure_rate": round(failure_count / len(attempts), 6),
        "primary_target": {
            "metric": "client pre-dispatch to second complete semantic response body",
            "observed_p95_seconds": primary_p95,
            "boottime_conservative_upper_bound_p95_seconds": primary_upper_p95,
            "p95_under_30_seconds": (
                primary_upper_p95 is not None and primary_upper_p95 < 30
            ),
            "requires_zero_runner_or_cleanup_failures": True,
            "pass_uses_boottime_conservative_upper_bound": True,
        },
        "timing_contract": {
            "primary_t0": "client-target-create-dispatch/v1",
            "primary_is_conservative_relative_to_api_acceptance": True,
            "acceptance_response_proxy": PROXY_LABEL,
            "acceptance_response_proxy_is_exact_server_acceptance": False,
            "response_boundary": RESPONSE_CONTRACT,
            "boot_time_anchor": "pre-t0-ready-holder-clock-boottime-anchor/v1",
            "maximum_controller_anchor_before_to_t0_seconds": (
                qualification_builder.MAX_ANCHOR_TO_T0_CONTROLLER_MONOTONIC_SECONDS
            ),
            "upper_bound_formula": (
                "(event CLOCK_BOOTTIME - anchor CLOCK_BOOTTIME) + "
                "2 * CLOCK_BOOTTIME resolution, rounded upward to microseconds"
            ),
            "worker_and_probe_required_on_target_node": True,
            "percentile_estimator": (
                "nearest-rank with failed attempts sorted after successful samples"
            ),
        },
        "instrumentation_contract": captured_instrumentation,
        "instrumentation_contract_receipt": str(instrumentation_path),
        "instrumentation_contract_receipt_sha256": _sha256(instrumentation_path),
        "immutable_contract": immutable,
        "attempts": attempts,
        "successful_run_ids": [item["run_id"] for item in successful],
        "successful_pod_uids": pod_uids,
        "metrics": metrics,
        "source_ledger": str(ledger_path),
        "source_ledger_sha256": _sha256(ledger_path),
    }


def write_exclusive(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise AggregateError(f"refusing existing aggregate output: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("openfold2", "boltz2"), required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = aggregate(args.ledger, args.model)
        payload = (
            json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("ascii")
        write_exclusive(args.output, payload)
        sys.stdout.buffer.write(payload)
        return 0 if result["status"] == "PASS" else 1
    except (AggregateError, OSError) as exc:
        print(f"aggregate-fresh-cohort: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
