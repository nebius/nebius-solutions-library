#!/usr/bin/env python3
"""Build a production canary timing receipt from captured Kubernetes objects.

The collector is offline and fail-closed.  It proves that the Service endpoint,
one-shot restore receipt, and exactly two strict semantic responses all refer to
the same UID- and PodSpec-bound MolMIM target before reporting latency.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import render
except ImportError:  # pragma: no cover - package import path
    from . import render


RECEIPT_SCHEMA = "archvteams.nebius.ai/molmim-production-canary-evidence/v1"
WORKER_RECEIPT_SCHEMA = "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1"
POD_SPEC_HASH_KEY = "archvteams.nebius.ai/target-pod-spec-sha256"
NODE = "gpu-node-b.example.invalid"
EXPECTED_SEMANTIC_CASES = (
    (
        "caffeine",
        "3a59acaf04e18fc5a7ed37b27ffdeee05f2542f23734d204bf487cc5f172a55e",
    ),
    (
        "aspirin",
        "98ac4fccf35f4a9c3cbb3666b8d98218b24e7f2d5cff9cb174c76b2d242927da",
    ),
)
KUBERNETES_OMITTED_FALSE_POD_FIELDS = frozenset({"hostIPC", "hostNetwork", "hostPID"})


class EvidenceError(ValueError):
    """Captured evidence is incomplete, inconsistent, or unsuccessful."""


def _require_expected(actual: Any, expected: Any, label: str) -> None:
    """Require rendered fields while allowing controller/API-added fields."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise EvidenceError(f"{label} is not an object")
        for key, expected_value in expected.items():
            if key not in actual:
                # Kubernetes' JSON serialization omits these PodSpec booleans
                # when they are false. Their API default is also false, so an
                # omitted live value is equivalent to the explicit reviewed
                # manifest value; do not weaken any other required field.
                if (
                    expected_value is False
                    and key in KUBERNETES_OMITTED_FALSE_POD_FIELDS
                ):
                    continue
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


def _without_default_service_account_projection(
    pod_spec: dict[str, Any], expected_spec: dict[str, Any], label: str
) -> dict[str, Any]:
    """Remove only the API-injected bound service-account projection.

    Job Pod templates do not contain the generated ``kube-api-access-*``
    volume, while their live Pods do when token automount is enabled. Validate
    its exact shape before normalizing it out for the rendered-contract match.
    """
    expected_volume_names = {
        item.get("name")
        for item in expected_spec.get("volumes", [])
        if isinstance(item, dict)
    }
    actual_volumes = pod_spec.get("volumes", [])
    if not isinstance(actual_volumes, list):
        return pod_spec
    extras = [
        item
        for item in actual_volumes
        if isinstance(item, dict) and item.get("name") not in expected_volume_names
    ]
    if not extras:
        return pod_spec
    if len(extras) != 1:
        raise EvidenceError(f"{label} has unexpected injected volumes")
    volume = extras[0]
    name = volume.get("name")
    projected = volume.get("projected")
    if (
        not isinstance(name, str)
        or re.fullmatch(r"kube-api-access-[a-z0-9]{5}", name) is None
        or not isinstance(projected, dict)
        or projected.get("defaultMode") != 420
    ):
        raise EvidenceError(f"{label} has an invalid service-account projection")
    sources = projected.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        raise EvidenceError(f"{label} has an invalid service-account projection")
    token = sources[0].get("serviceAccountToken") if isinstance(sources[0], dict) else None
    root_ca = sources[1].get("configMap") if isinstance(sources[1], dict) else None
    namespace = sources[2].get("downwardAPI") if isinstance(sources[2], dict) else None
    expiration = token.get("expirationSeconds") if isinstance(token, dict) else None
    if (
        not isinstance(expiration, int)
        or isinstance(expiration, bool)
        or not 600 <= expiration <= 7200
        or token.get("path") != "token"
        or root_ca
        != {
            "items": [{"key": "ca.crt", "path": "ca.crt"}],
            "name": "kube-root-ca.crt",
        }
        or namespace
        != {
            "items": [
                {
                    "fieldRef": {
                        "apiVersion": "v1",
                        "fieldPath": "metadata.namespace",
                    },
                    "path": "namespace",
                }
            ]
        }
    ):
        raise EvidenceError(f"{label} has an invalid service-account projection")

    normalized = copy.deepcopy(pod_spec)
    normalized["volumes"] = [
        item for item in normalized["volumes"] if item.get("name") != name
    ]
    expected_container_names = {
        item.get("name")
        for kind in ("containers", "initContainers")
        for item in expected_spec.get(kind, [])
        if isinstance(item, dict)
    }
    found_mounts = 0
    for kind in ("containers", "initContainers"):
        for container in normalized.get(kind, []):
            if not isinstance(container, dict) or container.get("name") not in expected_container_names:
                continue
            mounts = container.get("volumeMounts", [])
            if not isinstance(mounts, list):
                raise EvidenceError(f"{label} has malformed volume mounts")
            injected = [mount for mount in mounts if isinstance(mount, dict) and mount.get("name") == name]
            for mount in injected:
                if mount != {
                    "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount",
                    "name": name,
                    "readOnly": True,
                }:
                    raise EvidenceError(f"{label} has an invalid service-account mount")
            found_mounts += len(injected)
            container["volumeMounts"] = [mount for mount in mounts if mount.get("name") != name]
    if found_mounts != len(expected_container_names):
        raise EvidenceError(f"{label} does not mount the service-account projection exactly once")
    return normalized


def _load(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular non-symlink file")
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read {label} JSON: {type(exc).__name__}") from exc


def _measurement_timestamp(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("target submit timestamp must be a regular non-symlink file")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("cannot read target submit timestamp") from exc
    _timestamp(value, "target_submit_at")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} must be an RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvidenceError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _seconds(start: datetime, finish: datetime, label: str) -> float:
    result = (finish - start).total_seconds()
    if result < 0:
        raise EvidenceError(f"{label} has reversed timestamps")
    return round(result, 6)


def _normalize_kubernetes_time(
    observed: datetime, measurement_start: datetime, label: str
) -> datetime:
    if observed >= measurement_start:
        return observed
    if (measurement_start - observed).total_seconds() < 1:
        return measurement_start
    raise EvidenceError(f"{label} precedes authoritative target submission")


def _holder_identity(
    holder: dict[str, Any], expected_name: str, captured: datetime, label: str
) -> str:
    metadata = _object(holder.get("metadata"), f"{label} metadata")
    spec = _object(holder.get("spec"), f"{label} spec")
    status = _object(holder.get("status"), f"{label} status")
    uid = metadata.get("uid")
    try:
        if str(uuid.UUID(str(uid))) != uid:
            raise ValueError
    except ValueError as exc:
        raise EvidenceError(f"{label} UID is not canonical") from exc
    ready = [
        _timestamp(item.get("lastTransitionTime"), f"{label} Ready")
        for item in status.get("conditions", [])
        if isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
    ]
    if (
        holder.get("apiVersion") != "v1"
        or holder.get("kind") != "Pod"
        or metadata.get("name") != expected_name
        or metadata.get("namespace") != render.NAMESPACE
        or spec.get("nodeName") != NODE
        or len(ready) != 1
        or ready[0] > captured
        or not all(
            isinstance(item, dict) and item.get("ready") is True
            for item in status.get("containerStatuses", [])
        )
    ):
        raise EvidenceError(f"{label} was not Ready on the exact node before capture")
    return uid


def _positive_receipt_seconds(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise EvidenceError(f"{label} must be positive and finite")
    return round(float(value), 6)


def _storage_prewarm(
    *,
    run: dict[str, Any],
    cache_holder: dict[str, Any],
    cache_receipt: dict[str, Any],
    artifact_holder: dict[str, Any],
    artifact_receipt: dict[str, Any],
    captured_at: str,
    demand: datetime,
) -> dict[str, Any]:
    captured = _timestamp(captured_at, "storage prewarm capture")
    if captured > demand:
        raise EvidenceError("storage prewarm receipts were captured after authoritative T0")
    cache_uid = _holder_identity(
        cache_holder,
        "molmim-native-f7-cache-holder-t12",
        captured,
        "cache holder Pod",
    )
    artifact_name = (
        "molmim-native-f7-holder-t12"
        if run["image_io_mode"] == "direct"
        else "molmim-native-f7-buffered-holder-t12"
    )
    artifact_uid = _holder_identity(
        artifact_holder, artifact_name, captured, "artifact holder Pod"
    )
    cache_volumes = cache_holder.get("spec", {}).get("volumes", [])
    if not any(
        isinstance(item, dict)
        and item.get("name") == "cache"
        and item.get("persistentVolumeClaim", {}).get("claimName")
        == "molmim-native-f7-cache"
        and item.get("persistentVolumeClaim", {}).get("readOnly") is True
        for item in cache_volumes
    ):
        raise EvidenceError("cache holder does not mount the exact cache read-only")
    artifact_volumes = artifact_holder.get("spec", {}).get("volumes", [])
    required_claims = {
        "artifacts": "molmim-native-f7-artifacts",
        "nim-cache": "molmim-native-f7-cache",
    }
    for volume_name, claim_name in required_claims.items():
        if not any(
            isinstance(item, dict)
            and item.get("name") == volume_name
            and item.get("persistentVolumeClaim", {}).get("claimName") == claim_name
            and item.get("persistentVolumeClaim", {}).get("readOnly") is True
            for item in artifact_volumes
        ):
            raise EvidenceError("artifact holder does not mount both exact PVCs read-only")
    if (
        cache_receipt.get("schema")
        != "archvteams.nebius.ai/molmim-cache-holder-receipt/v1"
        or cache_receipt.get("status") != "PASS"
        or cache_receipt.get("mode") != "cache-full-read"
        or cache_receipt.get("unique_bytes") != 284_497_920
        or cache_receipt.get("prewarm_bytes") != 284_497_920
        or cache_receipt.get("tree_sha256")
        != "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c"
    ):
        raise EvidenceError("cache holder log is not the exact full-read receipt")
    artifact_tree = artifact_receipt.get("tree_sha256")
    artifact_bytes = artifact_receipt.get("regular_file_bytes")
    if (
        artifact_receipt.get("schema")
        != "archvteams.nebius.ai/molmim-native-artifact-receipt/v1"
        or artifact_receipt.get("status") != "PASS"
        or artifact_receipt.get("checkpoint_id") != run["checkpoint_id"]
        or artifact_receipt.get("artifact_version") != run["artifact_version"]
        or artifact_receipt.get("source_node") != NODE
        or artifact_receipt.get("image_io_mode") != run["image_io_mode"]
        or artifact_receipt.get("manifest_sha256")
        != run["artifact_manifest_sha256"]
        or not isinstance(artifact_bytes, int)
        or isinstance(artifact_bytes, bool)
        or artifact_bytes < 1_000_000_000
        or artifact_receipt.get("unique_bytes") != artifact_bytes
        or artifact_receipt.get("prewarm_bytes") != artifact_bytes
        or not isinstance(artifact_tree, str)
        or len(artifact_tree) != 64
        or any(character not in "0123456789abcdef" for character in artifact_tree)
    ):
        raise EvidenceError("artifact holder log is not the exact full-read receipt")
    return {
        "captured_at": captured_at,
        "cache": {
            "holder_pod": "molmim-native-f7-cache-holder-t12",
            "holder_uid": cache_uid,
            "mode": cache_receipt["mode"],
            "unique_bytes": cache_receipt["unique_bytes"],
            "tree_sha256": cache_receipt["tree_sha256"],
            "full_read_elapsed_seconds": _positive_receipt_seconds(
                cache_receipt.get("full_read_elapsed_seconds"), "cache full-read elapsed"
            ),
        },
        "artifact": {
            "holder_pod": artifact_name,
            "holder_uid": artifact_uid,
            "mode": artifact_receipt["image_io_mode"],
            "unique_bytes": artifact_receipt["unique_bytes"],
            "tree_sha256": artifact_tree,
            "manifest_sha256": artifact_receipt["manifest_sha256"],
            "full_read_elapsed_seconds": _positive_receipt_seconds(
                artifact_receipt.get("full_read_elapsed_seconds"),
                "artifact full-read elapsed",
            ),
        },
    }


def _condition_time(pod: dict[str, Any], kind: str, required_status: str = "True") -> datetime:
    conditions = pod.get("status", {}).get("conditions", [])
    matches = [
        item
        for item in conditions
        if isinstance(item, dict)
        and item.get("type") == kind
        and item.get("status") == required_status
    ]
    if len(matches) != 1:
        raise EvidenceError(f"target Pod does not have one successful {kind} condition")
    return _timestamp(matches[0].get("lastTransitionTime"), f"target {kind} time")


def _container_times(pod: dict[str, Any], name: str, label: str) -> tuple[datetime, datetime | None]:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    matches = [item for item in statuses if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise EvidenceError(f"{label} has no unique {name} container status")
    state = _object(matches[0].get("state"), f"{label} container state")
    if isinstance(state.get("running"), dict):
        return _timestamp(state["running"].get("startedAt"), f"{label} start"), None
    terminated = state.get("terminated")
    if not isinstance(terminated, dict):
        raise EvidenceError(f"{label} container is neither running nor terminated")
    if terminated.get("exitCode") != 0:
        raise EvidenceError(f"{label} container did not exit successfully")
    return (
        _timestamp(terminated.get("startedAt"), f"{label} start"),
        _timestamp(terminated.get("finishedAt"), f"{label} finish"),
    )


def _job_succeeded(job: dict[str, Any], expected_name: str, label: str) -> datetime:
    if job.get("apiVersion") != "batch/v1" or job.get("kind") != "Job":
        raise EvidenceError(f"{label} is not a batch/v1 Job")
    if (
        job.get("metadata", {}).get("name") != expected_name
        or job.get("metadata", {}).get("namespace") != render.NAMESPACE
    ):
        raise EvidenceError(f"{label} name does not match the run")
    status = _object(job.get("status"), f"{label} status")
    if status.get("succeeded") != 1 or status.get("failed", 0) not in (0, None):
        raise EvidenceError(f"{label} did not have exactly one successful completion")
    return _timestamp(status.get("completionTime"), f"{label} completion")


def _validate_job_pod(
    pod: dict[str, Any],
    job: dict[str, Any],
    expected_job: dict[str, Any],
    container_name: str,
    image: str,
    label: str,
) -> None:
    metadata = _object(job.get("metadata"), f"{label} Job metadata")
    job_name = metadata.get("name")
    job_uid = metadata.get("uid")
    try:
        if str(uuid.UUID(str(job_uid))) != job_uid:
            raise ValueError
    except ValueError as exc:
        raise EvidenceError(f"{label} Job UID is not canonical") from exc
    pod_metadata = _object(pod.get("metadata"), f"{label} Pod metadata")
    pod_spec = _object(pod.get("spec"), f"{label} Pod spec")
    owners = pod_metadata.get("ownerReferences")
    controllers = (
        [owner for owner in owners if isinstance(owner, dict) and owner.get("controller") is True]
        if isinstance(owners, list)
        else []
    )
    if (
        pod.get("apiVersion") != "v1"
        or pod.get("kind") != "Pod"
        or pod_metadata.get("namespace") != render.NAMESPACE
        or pod_metadata.get("generateName") != f"{job_name}-"
        or not isinstance(pod_metadata.get("name"), str)
        or not pod_metadata["name"].startswith(f"{job_name}-")
        or len(controllers) != 1
        or any(
            controllers[0].get(key) != value
            for key, value in {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job_name,
                "uid": job_uid,
            }.items()
        )
    ):
        raise EvidenceError(f"{label} Pod is not controlled by the exact Job")
    template = expected_job["spec"]["template"]
    _require_expected(
        pod_metadata.get("labels"), template["metadata"]["labels"], f"{label} Pod labels"
    )
    if "annotations" in template["metadata"]:
        _require_expected(
            pod_metadata.get("annotations"),
            template["metadata"]["annotations"],
            f"{label} Pod annotations",
        )
    normalized_pod_spec = _without_default_service_account_projection(
        pod_spec, template["spec"], f"{label} Pod spec"
    )
    _require_expected(normalized_pod_spec, template["spec"], f"{label} Pod spec")
    statuses = pod.get("status", {}).get("containerStatuses")
    matches = (
        [item for item in statuses if isinstance(item, dict) and item.get("name") == container_name]
        if isinstance(statuses, list)
        else []
    )
    if (
        len(matches) != 1
        or matches[0].get("imageID", "").removeprefix("docker-pullable://") != image
    ):
        raise EvidenceError(f"{label} Pod container image is not the pinned digest")


def _only_job_container(job: dict[str, Any], name: str, label: str) -> dict[str, Any]:
    try:
        containers = job["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError) as exc:
        raise EvidenceError(f"{label} has no pod-template containers") from exc
    if not isinstance(containers, list):
        raise EvidenceError(f"{label} pod-template containers are malformed")
    matches = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == name
    ]
    if len(containers) != 1 or len(matches) != 1:
        raise EvidenceError(f"{label} does not contain exactly one {name} container")
    return matches[0]


def _validate_semantics(
    summary: dict[str, Any], run_id: str
) -> tuple[datetime, datetime, datetime, datetime, list[float]]:
    if (
        summary.get("schema_version") != 1
        or summary.get("validator") != "molmim-faststart-semantic-v1"
        or summary.get("ok") is not True
        or summary.get("status") != "PASS"
        or summary.get("passed_case_count") != 2
        or summary.get("failed_case_count") != 0
        or summary.get("exit_code") != 0
    ):
        raise EvidenceError("semantic summary is not a two-call PASS receipt")
    cases = summary.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise EvidenceError("semantic summary must contain exactly two cases")
    total = summary.get("total_elapsed_seconds")
    if (
        not isinstance(total, (int, float))
        or isinstance(total, bool)
        or not math.isfinite(float(total))
        or total <= 0
    ):
        raise EvidenceError("semantic summary has invalid total elapsed time")
    expected_run_ids = [f"{run_id}-semantic-a", f"{run_id}-semantic-b"]
    elapsed: list[float] = []
    response_hashes: list[str] = []
    generated_smiles: list[str] = []
    response_received: list[datetime] = []
    for index, (case, expected_case, expected_run_id) in enumerate(
        zip(cases, EXPECTED_SEMANTIC_CASES, expected_run_ids, strict=True), 1
    ):
        expected_input_id, expected_request_hash = expected_case
        if (
            not isinstance(case, dict)
            or case.get("index") != index
            or case.get("input_id") != expected_input_id
            or case.get("run_id") != expected_run_id
            or case.get("ok") is not True
            or case.get("status") != "PASS"
            or case.get("exit_code") != 0
            or not isinstance(case.get("invariant"), dict)
        ):
            raise EvidenceError(f"semantic case {index} is not the expected strict PASS")
        if case.get("request_sha256") != expected_request_hash:
            raise EvidenceError(f"semantic case {index} is not the pinned CMA-ES/QED request")
        response_bytes = case.get("response_bytes")
        response_hash = case.get("response_sha256")
        if (
            not isinstance(response_bytes, int)
            or isinstance(response_bytes, bool)
            or response_bytes < 1
            or response_bytes > 16 * 1024 * 1024
            or not isinstance(response_hash, str)
            or len(response_hash) != 64
            or any(character not in "0123456789abcdef" for character in response_hash)
        ):
            raise EvidenceError(f"semantic case {index} has an invalid response receipt")
        invariant = case["invariant"]
        smiles = invariant.get("smiles")
        atom_count = invariant.get("atom_count")
        score = invariant.get("score")
        rdkit_qed = invariant.get("rdkit_qed")
        if (
            invariant.get("generated_count") != 1
            or not isinstance(smiles, str)
            or not smiles.strip()
            or not isinstance(atom_count, int)
            or isinstance(atom_count, bool)
            or atom_count < 1
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
            or not isinstance(rdkit_qed, (int, float))
            or isinstance(rdkit_qed, bool)
            or not math.isfinite(float(rdkit_qed))
            or not 0 <= float(rdkit_qed) <= 1
            or abs(float(score) - float(rdkit_qed)) > 0.02
        ):
            raise EvidenceError(f"semantic case {index} lacks the strict MolMIM QED invariant")
        value = case.get("elapsed_seconds")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise EvidenceError(f"semantic case {index} has invalid elapsed time")
        elapsed.append(round(float(value), 6))
        response_received.append(
            _timestamp(
                case.get("response_received_at"),
                f"semantic case {index} response_received_at",
            )
        )
        response_hashes.append(response_hash)
        generated_smiles.append(smiles)
    if len(set(response_hashes)) != 2 or len(set(generated_smiles)) != 2:
        raise EvidenceError("two distinct MolMIM requests must yield distinct responses")
    started = _timestamp(summary.get("started_at"), "semantic probe start")
    ready = _timestamp(summary.get("ready_at"), "direct HTTP readiness")
    finished = _timestamp(summary.get("finished_at"), "second semantic completion")
    validation_completed = _timestamp(
        summary.get("validation_completed_at"), "semantic validation completion"
    )
    if not (
        started
        <= ready
        <= response_received[0]
        <= response_received[1]
        <= validation_completed
        and finished == validation_completed
    ):
        raise EvidenceError("semantic start, direct readiness, and completion are not ordered")
    return started, ready, response_received[1], validation_completed, elapsed


def build_evidence(
    *,
    contract: dict[str, Any],
    run: dict[str, Any],
    binding: dict[str, Any],
    target: dict[str, Any],
    service: dict[str, Any],
    endpoint_slices: dict[str, Any],
    worker_job: dict[str, Any],
    worker_pod: dict[str, Any],
    worker_receipt: dict[str, Any],
    probe_job: dict[str, Any],
    probe_pod: dict[str, Any],
    semantic_summary: dict[str, Any],
    target_submit_at: str | None = None,
    cache_holder: dict[str, Any] | None = None,
    cache_receipt: dict[str, Any] | None = None,
    artifact_holder: dict[str, Any] | None = None,
    artifact_receipt: dict[str, Any] | None = None,
    prewarm_captured_at: str | None = None,
) -> dict[str, Any]:
    try:
        contract = render.validate_contract(contract)
        run = render.validate_run(run)
        render.validate_compatibility(run, contract)
        binding = render.validate_binding(binding, run)
    except render.RenderError as exc:
        raise EvidenceError(str(exc)) from exc
    run_id = run["run_id"]
    target_name = render._target_name(run_id)
    target_metadata = _object(target.get("metadata"), "target metadata")
    if (
        target.get("apiVersion") != "v1"
        or target.get("kind") != "Pod"
        or target_metadata.get("name") != target_name
        or target_metadata.get("namespace") != render.NAMESPACE
        or target_metadata.get("uid") != binding["pod_uid"]
    ):
        raise EvidenceError("target Pod identity does not match the binding")
    annotations = _object(target_metadata.get("annotations"), "target annotations")
    if annotations.get(POD_SPEC_HASH_KEY) != binding["pod_spec_sha256"]:
        raise EvidenceError("target PodSpec hash annotation does not match the binding")
    try:
        canonical_spec = json.dumps(
            target.get("spec"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvidenceError("final target PodSpec is not canonicalizable") from exc
    if hashlib.sha256(canonical_spec).hexdigest() != binding["pod_spec_sha256"]:
        raise EvidenceError("final target PodSpec changed after binding")
    target_status = _object(target.get("status"), "target status")
    if target_status.get("phase") != "Running" or target_status.get("podIP") != binding["pod_ip"]:
        raise EvidenceError("target Pod is not the bound Running endpoint")
    statuses = target_status.get("containerStatuses", [])
    target_container = [
        item for item in statuses if isinstance(item, dict) and item.get("name") == "molmim"
    ]
    if (
        len(target_container) != 1
        or target_container[0].get("containerID") != binding["container_id"]
        or target_container[0].get("imageID", "").removeprefix("docker-pullable://")
        != render.NIM_IMAGE
    ):
        raise EvidenceError("target container identity changed after binding")

    planned_demand = _timestamp(run["demand_at"], "planned demand_at")
    measurement_start_raw = target_submit_at or run["demand_at"]
    demand = _timestamp(measurement_start_raw, "target_submit_at")
    if demand < planned_demand:
        raise EvidenceError("authoritative target submission precedes planned demand")
    if any(
        value is None
        for value in (
            cache_holder,
            cache_receipt,
            artifact_holder,
            artifact_receipt,
            prewarm_captured_at,
        )
    ):
        raise EvidenceError("pre-T0 cache/artifact holder Pod and log receipts are required")
    storage_prewarm = _storage_prewarm(
        run=run,
        cache_holder=cache_holder,
        cache_receipt=cache_receipt,
        artifact_holder=artifact_holder,
        artifact_receipt=artifact_receipt,
        captured_at=prewarm_captured_at,
        demand=demand,
    )
    target_created = _normalize_kubernetes_time(
        _timestamp(target_metadata.get("creationTimestamp"), "target creation"),
        demand,
        "target creation",
    )
    scheduled = _normalize_kubernetes_time(
        _condition_time(target, "PodScheduled"), demand, "target scheduling"
    )
    kubernetes_ready = _normalize_kubernetes_time(
        _condition_time(target, "Ready"), demand, "Kubernetes readiness"
    )
    placeholder_started, _ = _container_times(target, "molmim", "target Pod")
    placeholder_started = _normalize_kubernetes_time(
        placeholder_started, demand, "placeholder start"
    )

    service_name = f"molmim-canary-{run_id}"
    service_metadata = _object(service.get("metadata"), "Service metadata")
    service_spec = _object(service.get("spec"), "Service spec")
    if (
        service.get("apiVersion") != "v1"
        or service.get("kind") != "Service"
        or service_metadata.get("name") != service_name
        or service_metadata.get("namespace") != render.NAMESPACE
        or not isinstance(service_metadata.get("uid"), str)
        or service_spec.get("type") != "ClusterIP"
        or service_spec.get("clusterIP") in (None, "", "None")
        or service_spec.get("selector")
        != {
            "app.kubernetes.io/name": "molmim",
            "app.kubernetes.io/component": "restore-target",
            "archvteams.nebius.ai/run-id": run_id,
        }
        or service_spec.get("ports")
        != [{"name": "http", "port": 8000, "targetPort": "http", "protocol": "TCP"}]
    ):
        raise EvidenceError("canary Service is not the exact run-scoped ClusterIP")
    # `kubectl get endpointslices -o json` may serialize the collection as the
    # typed `EndpointSliceList` or as the generic core `List`, depending on the
    # client/API discovery path.  The item-level API/ownership checks below are
    # authoritative, so accept both collection envelopes.
    items = (
        endpoint_slices.get("items")
        if endpoint_slices.get("kind") in {"EndpointSliceList", "List"}
        else [endpoint_slices]
    )
    if not isinstance(items, list):
        raise EvidenceError("EndpointSlice capture is malformed")
    endpoints: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("metadata", {}).get("labels", {}).get(
            "kubernetes.io/service-name"
        ) != service_name:
            continue
        owners = item.get("metadata", {}).get("ownerReferences", [])
        if not any(
            isinstance(owner, dict)
            and owner.get("kind") == "Service"
            and owner.get("uid") == service_metadata["uid"]
            and owner.get("controller") is True
            for owner in owners
        ):
            raise EvidenceError("EndpointSlice is not controlled by the exact canary Service")
        values = item.get("endpoints", [])
        if isinstance(values, list):
            endpoints.extend(value for value in values if isinstance(value, dict))
    if len(endpoints) != 1:
        raise EvidenceError("canary Service must resolve to exactly one EndpointSlice endpoint")
    endpoint = endpoints[0]
    if (
        endpoint.get("targetRef", {}).get("uid") != binding["pod_uid"]
        or endpoint.get("conditions", {}).get("ready") is not True
        or binding["pod_ip"] not in endpoint.get("addresses", [])
    ):
        raise EvidenceError("canary Service endpoint is not the ready bound target Pod")

    worker_name = f"molmim-restore-{run_id}"
    worker_completed = _job_succeeded(worker_job, worker_name, "restore worker Job")
    worker_annotations = _object(worker_job.get("metadata", {}).get("annotations"), "worker annotations")
    if (
        worker_annotations.get("archvteams.nebius.ai/target-pod-uid") != binding["pod_uid"]
        or worker_annotations.get(POD_SPEC_HASH_KEY) != binding["pod_spec_sha256"]
    ):
        raise EvidenceError("restore worker Job is not UID- and PodSpec-bound")
    expected_worker_docs = render.render_restore(run, contract, binding)
    expected_worker_job = next(
        item for item in expected_worker_docs if item.get("kind") == "Job"
    )
    expected_worker_container = expected_worker_job["spec"]["template"]["spec"][
        "containers"
    ][0]
    actual_worker_container = _only_job_container(
        worker_job, "restore-worker", "restore worker Job"
    )
    for field in ("image", "command", "args"):
        if actual_worker_container.get(field) != expected_worker_container.get(field):
            raise EvidenceError(f"restore worker Job {field} does not match the bound interface")
    _require_expected(worker_job, expected_worker_job, "restore worker Job")
    _validate_job_pod(
        worker_pod,
        worker_job,
        expected_worker_job,
        "restore-worker",
        contract["worker_image"],
        "restore worker",
    )
    worker_started, worker_finished = _container_times(worker_pod, "restore-worker", "worker Pod")
    if worker_finished is None:
        raise EvidenceError("restore worker container has not completed")
    expected_receipt = {
        "schema": WORKER_RECEIPT_SCHEMA,
        "status": "succeeded",
        "run_id": run_id,
        "target_namespace": render.NAMESPACE,
        "target_name": target_name,
        "target_uid": binding["pod_uid"],
        "target_container_id": binding["container_id"],
        "target_image_id": binding["image_id"],
        "target_node": binding["node"],
        "target_pod_ip": binding["pod_ip"],
        "target_pod_spec_sha256": binding["pod_spec_sha256"],
        "checkpoint_id": run["checkpoint_id"],
        "artifact_version": run["artifact_version"],
        "checkpoint_manifest_sha256": run["artifact_manifest_sha256"],
        "tool_bundle_manifest_sha256": contract["tool_bundle"]["content_sha256"],
    }
    if any(worker_receipt.get(key) != value for key, value in expected_receipt.items()):
        raise EvidenceError("one-shot worker success receipt does not match the exact binding")
    duration_ms = worker_receipt.get("duration_ms")
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
        raise EvidenceError("one-shot worker receipt has invalid duration_ms")
    receipt_completed = _timestamp(worker_receipt.get("completed_at"), "worker receipt completion")

    probe_name = f"molmim-semantic-{run_id}"
    probe_completed = _job_succeeded(probe_job, probe_name, "semantic probe Job")
    probe_annotations = _object(probe_job.get("metadata", {}).get("annotations"), "probe annotations")
    if (
        probe_annotations.get("archvteams.nebius.ai/target-pod-uid") != binding["pod_uid"]
        or probe_annotations.get(POD_SPEC_HASH_KEY) != binding["pod_spec_sha256"]
        or probe_annotations.get("archvteams.nebius.ai/image-io-mode")
        != run["image_io_mode"]
    ):
        raise EvidenceError(
            "semantic probe Job is not UID-, PodSpec-, and image-I/O-mode-bound"
        )
    expected_probe_docs = render.render_probe(run, contract, binding)
    expected_probe_job = next(
        item for item in expected_probe_docs if item.get("kind") == "Job"
    )
    expected_probe_container = expected_probe_job["spec"]["template"]["spec"][
        "containers"
    ][0]
    actual_probe_container = _only_job_container(
        probe_job, "semantic-probe", "semantic probe Job"
    )
    for field in ("image", "command", "args"):
        if actual_probe_container.get(field) != expected_probe_container.get(field):
            raise EvidenceError(f"semantic probe Job {field} does not match the two-call contract")
    _require_expected(probe_job, expected_probe_job, "semantic probe Job")
    _validate_job_pod(
        probe_pod,
        probe_job,
        expected_probe_job,
        "semantic-probe",
        contract["probe_image"],
        "semantic probe",
    )
    probe_started, probe_finished = _container_times(probe_pod, "semantic-probe", "probe Pod")
    if probe_finished is None:
        raise EvidenceError("semantic probe container has not completed")
    semantic_started, semantic_ready, semantic_response_2, validation_completed, case_elapsed = (
        _validate_semantics(semantic_summary, run_id)
    )
    expected_origin = f"http://{service_name}:8000"
    expected_path = "/generate"
    if (
        semantic_summary.get("base_url") != expected_origin
        or semantic_summary.get("endpoint") != expected_origin + expected_path
        or semantic_summary.get("inference_path") != expected_path
        or semantic_summary.get("proxy_policy") != "disabled"
        or semantic_summary.get("redirect_policy") != "reject"
    ):
        raise EvidenceError("semantic summary is not bound to the exact canary Service endpoint")

    # Kubernetes condition timestamps are serialized to whole seconds, while
    # the worker receipt retains nanoseconds.  If readiness flips later in the
    # same wall-clock second as restore completion, its truncated timestamp can
    # appear slightly earlier.  Tolerate only that sub-second quantization; a
    # gap of one second or more remains an ordering failure.
    kubernetes_ready_for_order = kubernetes_ready
    if kubernetes_ready < receipt_completed:
        if (receipt_completed - kubernetes_ready).total_seconds() >= 1:
            raise EvidenceError("canary phase timestamps are not monotonically ordered")
        kubernetes_ready_for_order = receipt_completed

    target_order = [
        demand,
        target_created,
        scheduled,
        placeholder_started,
    ]
    restore_order = [
        placeholder_started,
        worker_started,
        receipt_completed,
    ]
    kubernetes_ready_order = [receipt_completed, kubernetes_ready_for_order]
    probe_order = [
        placeholder_started,
        probe_started,
        semantic_started,
        semantic_ready,
        semantic_response_2,
        validation_completed,
    ]
    if any(
        later < earlier
        for ordered in (
            target_order,
            restore_order,
            kubernetes_ready_order,
            probe_order,
        )
        for earlier, later in zip(ordered, ordered[1:])
    ):
        raise EvidenceError("canary phase timestamps are not monotonically ordered")
    if worker_completed < worker_finished or probe_completed < probe_finished:
        raise EvidenceError("Job completion precedes its successful container finish")

    return {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS",
        "run_id": run_id,
        "request_count": 2,
        "semantic_pass_count": 2,
        "demand_at": measurement_start_raw,
        "measurement": {
            "t0": measurement_start_raw,
            "t0_definition": "timestamp immediately before target Pod create",
            "planned_demand_at": run["demand_at"],
            "storage_state": "attached artifact/cache PVCs fully read before T0; exact image present on node",
        },
        "artifact": {
            "checkpoint_id": run["checkpoint_id"],
            "version": run["artifact_version"],
            "manifest_sha256": run["artifact_manifest_sha256"],
            "target_glibc_version": run["target_glibc_version"],
            "image_io_mode": run["image_io_mode"],
        },
        "storage_prewarm": storage_prewarm,
        "target": {
            "namespace": render.NAMESPACE,
            "name": target_name,
            "uid": binding["pod_uid"],
            "node": binding["node"],
            "image": render.NIM_IMAGE,
            "pod_spec_sha256": binding["pod_spec_sha256"],
            "service": service_name,
            "cluster_ip": service_spec["clusterIP"],
        },
        "timings_seconds": {
            "demand_to_target_created": _seconds(demand, target_created, "target creation"),
            "demand_to_scheduled": _seconds(demand, scheduled, "scheduling"),
            "demand_to_placeholder_running": _seconds(
                demand, placeholder_started, "placeholder start"
            ),
            "demand_to_worker_started": _seconds(demand, worker_started, "worker start"),
            "demand_to_restore_receipt": _seconds(
                demand, receipt_completed, "restore receipt"
            ),
            "demand_to_kubernetes_ready": _seconds(
                demand, kubernetes_ready, "Kubernetes readiness"
            ),
            "demand_to_http_ready": _seconds(
                demand, semantic_ready, "direct HTTP readiness"
            ),
            "demand_to_two_semantic_responses": _seconds(
                demand, semantic_response_2, "two semantic responses"
            ),
            "demand_to_validation_complete": _seconds(
                demand, validation_completed, "semantic validation completion"
            ),
            "worker_restore": round(duration_ms / 1000, 6),
            "semantic_probe_total": round(
                float(semantic_summary.get("total_elapsed_seconds")), 6
            ),
            "semantic_request_1": case_elapsed[0],
            "semantic_request_2": case_elapsed[1],
        },
        "evidence": {
            "worker_image": contract["worker_image"],
            "worker_classification": contract["worker_classification"],
            "worker_executable_sha256": contract["worker_executable_sha256"],
            "release_ready": contract["release_ready"],
            "supported_image_io_modes": contract["supported_image_io_modes"],
            "source_materialized_tree_sha256": contract["source"][
                "materialized_tree_sha256"
            ],
            "tool_bundle_manifest_sha256": contract["tool_bundle"]["content_sha256"],
            "tool_bundle_regular_files": contract["tool_bundle"]["regular_files"],
            "tool_bundle_glibc_compatibility_sha256": contract["tool_bundle"][
                "glibc_compatibility_sha256"
            ],
            "worker_completed_at": worker_receipt["completed_at"],
            "worker_job_completed_at": worker_completed.isoformat(),
            "probe_job_completed_at": probe_completed.isoformat(),
            "semantic_ready_at": semantic_summary["ready_at"],
            "semantic_finished_at": semantic_summary["finished_at"],
            "semantic_response_2_received_at": semantic_summary["cases"][1][
                "response_received_at"
            ],
            "semantic_validation_completed_at": semantic_summary[
                "validation_completed_at"
            ],
            "validator": semantic_summary["validator"],
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "run-config",
        "contract",
        "binding",
        "target-pod",
        "service",
        "endpoint-slices",
        "worker-job",
        "worker-pod",
        "worker-receipt",
        "probe-job",
        "probe-pod",
        "semantic-summary",
        "target-submit-at",
        "cache-holder-pod",
        "cache-holder-receipt",
        "artifact-holder-pod",
        "artifact-holder-receipt",
        "prewarm-captured-at",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        receipt = build_evidence(
            contract=_object(_load(args.contract, "contract"), "contract"),
            run=_object(_load(args.run_config, "run config"), "run config"),
            binding=_object(_load(args.binding, "binding"), "binding"),
            target=_object(_load(args.target_pod, "target Pod"), "target Pod"),
            service=_object(_load(args.service, "Service"), "Service"),
            endpoint_slices=_object(
                _load(args.endpoint_slices, "EndpointSlices"), "EndpointSlices"
            ),
            worker_job=_object(_load(args.worker_job, "worker Job"), "worker Job"),
            worker_pod=_object(_load(args.worker_pod, "worker Pod"), "worker Pod"),
            worker_receipt=_object(
                _load(args.worker_receipt, "worker receipt"), "worker receipt"
            ),
            probe_job=_object(_load(args.probe_job, "probe Job"), "probe Job"),
            probe_pod=_object(_load(args.probe_pod, "probe Pod"), "probe Pod"),
            semantic_summary=_object(
                _load(args.semantic_summary, "semantic summary"), "semantic summary"
            ),
            target_submit_at=_measurement_timestamp(args.target_submit_at),
            cache_holder=_object(
                _load(args.cache_holder_pod, "cache holder Pod"), "cache holder Pod"
            ),
            cache_receipt=_object(
                _load(args.cache_holder_receipt, "cache holder receipt"),
                "cache holder receipt",
            ),
            artifact_holder=_object(
                _load(args.artifact_holder_pod, "artifact holder Pod"),
                "artifact holder Pod",
            ),
            artifact_receipt=_object(
                _load(args.artifact_holder_receipt, "artifact holder receipt"),
                "artifact holder receipt",
            ),
            prewarm_captured_at=_measurement_timestamp(args.prewarm_captured_at),
        )
    except (EvidenceError, render.RenderError) as exc:
        print(f"evidence: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
