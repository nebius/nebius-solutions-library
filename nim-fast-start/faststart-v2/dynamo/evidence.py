#!/usr/bin/env python3
"""Build a production canary timing receipt from captured Kubernetes objects.

The collector is offline and fail-closed.  It proves that the Service endpoint,
one-shot restore receipt, and exactly two strict semantic responses all refer to
the same UID- and PodSpec-bound OpenFold2 target before reporting latency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import render
except ImportError:  # pragma: no cover - package import path
    from . import render


RECEIPT_SCHEMA = "archvteams.nebius.ai/openfold2-production-canary-evidence/v2"
WORKER_RECEIPT_SCHEMA = "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1"
POD_SPEC_HASH_KEY = "archvteams.nebius.ai/target-pod-spec-sha256"
QUALIFICATION_SCHEMA = "archvteams.nebius.ai/warm-instance-qualification/v3"
ACCEPTANCE_RESPONSE_PROXY = "client-observed-api-create-response-return/v1"


class EvidenceError(ValueError):
    """Captured evidence is incomplete, inconsistent, or unsuccessful."""


def _load(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular non-symlink file")
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read {label} JSON: {type(exc).__name__}") from exc


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


def _coarse_kubernetes_seconds(
    demand: datetime, transition: datetime, label: str
) -> float:
    result = (transition - demand).total_seconds()
    if result < 0:
        if abs(result) >= 1:
            raise EvidenceError(f"{label} has reversed timestamps")
        return 0.0
    return round(result, 6)


def _kubernetes_seconds(demand: datetime, transition: datetime) -> float:
    return _coarse_kubernetes_seconds(demand, transition, "Kubernetes readiness")


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
    if job.get("metadata", {}).get("name") != expected_name:
        raise EvidenceError(f"{label} name does not match the run")
    status = _object(job.get("status"), f"{label} status")
    if status.get("succeeded") != 1 or status.get("failed", 0) not in (0, None):
        raise EvidenceError(f"{label} did not have exactly one successful completion")
    return _timestamp(status.get("completionTime"), f"{label} completion")


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
) -> tuple[datetime, datetime, datetime, list[float]]:
    if (
        summary.get("schema_version") != 2
        or summary.get("validator") != "openfold2-faststart-semantic-v1"
        or summary.get("ok") is not True
        or summary.get("status") != "PASS"
        or summary.get("passed_case_count") != 2
        or summary.get("failed_case_count") != 0
        or summary.get("exit_code") != 0
        or summary.get("request_count") != 2
        or summary.get("response_timing_contract")
        != "request-dispatch-to-complete-http-body/v1"
    ):
        raise EvidenceError("semantic summary is not a two-call PASS receipt")
    cases = summary.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise EvidenceError("semantic summary must contain exactly two cases")
    total = summary.get("validation_total_elapsed_seconds")
    if (
        not isinstance(total, (int, float))
        or isinstance(total, bool)
        or not math.isfinite(float(total))
        or total < 0
    ):
        raise EvidenceError("semantic summary has invalid total elapsed time")
    if summary.get("total_elapsed_seconds") != total:
        raise EvidenceError("total_elapsed_seconds is not the validation-total alias")
    expected_ids = [f"{run_id}-semantic-a", f"{run_id}-semantic-b"]
    elapsed: list[float] = []
    boundaries: list[tuple[datetime, datetime]] = []
    for index, (case, expected_id) in enumerate(zip(cases, expected_ids, strict=True), 1):
        if (
            not isinstance(case, dict)
            or case.get("index") != index
            or case.get("input_id") != expected_id
            or case.get("ok") is not True
            or case.get("status") != "PASS"
            or case.get("exit_code") != 0
            or not isinstance(case.get("invariant"), dict)
        ):
            raise EvidenceError(f"semantic case {index} is not the expected strict PASS")
        value = case.get("elapsed_seconds")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise EvidenceError(f"semantic case {index} has invalid elapsed time")
        elapsed.append(round(float(value), 6))
        request_started = _timestamp(
            case.get("request_started_at"), f"semantic request {index} start"
        )
        response_received = _timestamp(
            case.get("response_received_at"), f"semantic response {index} receipt"
        )
        if response_received < request_started:
            raise EvidenceError(f"semantic response {index} precedes its request")
        boundaries.append((request_started, response_received))
    semantic_started = _timestamp(summary.get("started_at"), "semantic probe start")
    validation_finished_raw = summary.get("validation_finished_at")
    validation_finished = _timestamp(
        validation_finished_raw, "semantic validation completion"
    )
    if summary.get("finished_at") != validation_finished_raw:
        raise EvidenceError(
            "finished_at is not the validation_finished_at compatibility alias"
        )
    ready_wait = summary.get("ready_wait")
    if not isinstance(ready_wait, dict) or ready_wait.get("status") != "PASS":
        raise EvidenceError("semantic summary has no successful HTTP readiness receipt")
    expected_ready_endpoint = str(summary.get("base_url", "")).rstrip("/") + "/v1/health/ready"
    if ready_wait.get("endpoint") != expected_ready_endpoint:
        raise EvidenceError("semantic HTTP readiness receipt is not bound to the canary Service")
    http_ready_started = _timestamp(
        ready_wait.get("started_at"), "HTTP readiness probe start"
    )
    http_ready = _timestamp(
        ready_wait.get("finished_at"), "successful HTTP readiness"
    )
    if not (
        semantic_started
        <= http_ready_started
        <= http_ready
        <= boundaries[0][0]
        <= boundaries[0][1]
        <= boundaries[1][0]
        <= boundaries[1][1]
        <= validation_finished
    ):
        raise EvidenceError("semantic probe timestamps are not monotonically ordered")
    return http_ready, boundaries[1][1], validation_finished, elapsed


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
    target_submit_at: str,
    target_create_response_at: str | None = None,
    qualification_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        contract = render.validate_contract(contract)
        run = render.validate_run(run)
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
        item for item in statuses if isinstance(item, dict) and item.get("name") == "openfold2"
    ]
    if (
        len(target_container) != 1
        or target_container[0].get("containerID") != binding["container_id"]
        or target_container[0].get("imageID", "").removeprefix("docker-pullable://")
        != render.NIM_IMAGE
    ):
        raise EvidenceError("target container identity changed after binding")

    demand = _timestamp(target_submit_at, "target submit/T0")
    acceptance_response = None
    if target_create_response_at is not None:
        acceptance_response = _timestamp(
            target_create_response_at, "client-observed target create response"
        )
        if acceptance_response < demand:
            raise EvidenceError("target create response precedes request dispatch")
    target_created = _timestamp(target_metadata.get("creationTimestamp"), "target creation")
    scheduled = _condition_time(target, "PodScheduled")
    ready = _condition_time(target, "Ready")
    placeholder_started, _ = _container_times(target, "openfold2", "target Pod")

    service_name = f"of2-canary-{run_id}"
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
            "app.kubernetes.io/name": "openfold2",
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

    worker_name = f"of2-restore-{run_id}"
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

    probe_name = f"of2-semantic-{run_id}"
    probe_completed = _job_succeeded(probe_job, probe_name, "semantic probe Job")
    probe_annotations = _object(probe_job.get("metadata", {}).get("annotations"), "probe annotations")
    if (
        probe_annotations.get("archvteams.nebius.ai/target-pod-uid") != binding["pod_uid"]
        or probe_annotations.get(POD_SPEC_HASH_KEY) != binding["pod_spec_sha256"]
    ):
        raise EvidenceError("semantic probe Job is not UID- and PodSpec-bound")
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
    probe_started, probe_finished = _container_times(probe_pod, "semantic-probe", "probe Pod")
    if probe_finished is None:
        raise EvidenceError("semantic probe container has not completed")
    http_ready, second_response_received, validation_finished, case_elapsed = _validate_semantics(
        semantic_summary, run_id
    )
    expected_origin = f"http://{service_name}:8000"
    expected_path = "/biology/openfold/openfold2/predict-structure-from-msa-and-template"
    if (
        semantic_summary.get("base_url") != expected_origin
        or semantic_summary.get("endpoint") != expected_origin + expected_path
        or semantic_summary.get("inference_path") != expected_path
        or semantic_summary.get("proxy_policy") != "disabled"
        or semantic_summary.get("redirect_policy") != "reject"
    ):
        raise EvidenceError("semantic summary is not bound to the exact canary Service endpoint")

    # API creation and Pod condition timestamps are whole-second Kubernetes
    # fields.  Validate their submit edge with the same bounded precision rule
    # used for Kubernetes Ready, then order the remaining coarse fields.
    _coarse_kubernetes_seconds(demand, target_created, "target creation")
    _coarse_kubernetes_seconds(demand, scheduled, "target scheduling")
    target_order = [target_created, scheduled, placeholder_started]
    restore_order = [
        placeholder_started,
        worker_started,
        receipt_completed,
    ]
    probe_order = [
        placeholder_started,
        probe_started,
        http_ready,
        second_response_received,
        validation_finished,
    ]
    if any(
        later < earlier
        for ordered in (target_order, restore_order, probe_order)
        for earlier, later in zip(ordered, ordered[1:])
    ):
        raise EvidenceError("canary phase timestamps are not monotonically ordered")
    # Kubelet container termination timestamps are serialized at whole-second
    # precision, while the validator's in-process completion timestamp retains
    # microseconds.  A completion observed in the same second can therefore
    # appear up to (but not including) one second earlier.  Bound that single
    # coarse edge without weakening the response timestamps used for metrics.
    if probe_finished < validation_finished and (
        validation_finished - probe_finished
    ).total_seconds() >= 1:
        raise EvidenceError("probe finish precedes semantic validation by at least one second")
    if worker_completed < worker_finished or probe_completed < probe_finished:
        raise EvidenceError("Job completion precedes its successful container finish")

    boot_upper_bounds: dict[str, float] | None = None
    if qualification_receipt is not None:
        expected_qualification_target = {
            "namespace": render.NAMESPACE,
            "name": target_name,
            "uid": binding["pod_uid"],
            "pod_spec_sha256": binding["pod_spec_sha256"],
            "image": render.NIM_IMAGE,
        }
        if (
            qualification_receipt.get("schema") != QUALIFICATION_SCHEMA
            or qualification_receipt.get("status") != "PASS"
            or qualification_receipt.get("model") != "openfold2"
            or qualification_receipt.get("run_id") != run_id
            or qualification_receipt.get("target") != expected_qualification_target
            or qualification_receipt.get("warm_instance", {}).get(
                "target_image_already_present_before_t0"
            )
            is not True
            or qualification_receipt.get("warm_instance", {}).get(
                "target_image_pull_or_download_after_t0"
            )
            is not False
            or qualification_receipt.get("gpu_health", {}).get("status") != "PASS"
            or qualification_receipt.get("gpu_health", {})
            .get("host_xid_check", {})
            .get("status")
            != "unavailable"
        ):
            raise EvidenceError("warm-instance qualification receipt does not match the trial")
        proxy = qualification_receipt.get("timing_boundaries", {}).get(
            "acceptance_response_proxy", {}
        )
        if (
            target_create_response_at is None
            or proxy.get("label") != ACCEPTANCE_RESPONSE_PROXY
            or proxy.get("timestamp") != target_create_response_at
            or proxy.get("is_exact_server_acceptance") is not False
        ):
            raise EvidenceError("acceptance-response proxy receipt is missing or mislabeled")
        conservative = qualification_receipt.get("boot_time_alignment", {}).get(
            "conservative_upper_bounds"
        )
        source_keys = {
            "demand_to_http_ready_boottime_upper": (
                "http_ready_complete_body"
            ),
            "demand_to_first_semantic_response_boottime_upper": (
                "first_semantic_response_complete_body"
            ),
            "demand_to_two_semantic_responses_boottime_upper": (
                "two_semantic_responses_complete_body"
            ),
        }
        if not isinstance(conservative, dict):
            raise EvidenceError("BOOTTIME conservative upper bounds are missing")
        boot_upper_bounds = {}
        for output_key, source_key in source_keys.items():
            item = conservative.get(source_key)
            value = item.get("upper_bound_seconds") if isinstance(item, dict) else None
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise EvidenceError("BOOTTIME conservative upper bounds are malformed")
            boot_upper_bounds[output_key] = float(value)

    timings_seconds = {
        "demand_to_target_created": _coarse_kubernetes_seconds(
            demand, target_created, "target creation"
        ),
        "demand_to_scheduled": _coarse_kubernetes_seconds(
            demand, scheduled, "target scheduling"
        ),
        "demand_to_placeholder_running": _seconds(
            demand, placeholder_started, "placeholder start"
        ),
        "demand_to_worker_started": _seconds(demand, worker_started, "worker start"),
        "demand_to_restore_receipt": _seconds(
            demand, receipt_completed, "restore receipt"
        ),
        "demand_to_http_ready": _seconds(demand, http_ready, "HTTP readiness"),
        "demand_to_kubernetes_ready": _kubernetes_seconds(demand, ready),
        "demand_to_two_semantic_responses": _seconds(
            demand, second_response_received, "two semantic responses"
        ),
        "demand_to_first_semantic_response": _seconds(
            demand,
            _timestamp(
                semantic_summary["cases"][0]["response_received_at"],
                "first semantic response",
            ),
            "first semantic response",
        ),
        "worker_restore": round(duration_ms / 1000, 6),
        "semantic_probe_validation_total": round(
            float(semantic_summary.get("validation_total_elapsed_seconds")), 6
        ),
        "semantic_request_1": case_elapsed[0],
        "semantic_request_2": case_elapsed[1],
    }
    if acceptance_response is not None:
        if acceptance_response > probe_started:
            raise EvidenceError("target create response follows semantic probe start")
        proxy_ready_seconds = (ready - acceptance_response).total_seconds()
        if proxy_ready_seconds < 0:
            if abs(proxy_ready_seconds) >= 1:
                raise EvidenceError(
                    "Kubernetes Ready precedes create-response proxy by at least one second"
                )
            proxy_ready_seconds = 0.0
        timings_seconds.update(
            {
                "target_create_api_round_trip": _seconds(
                    demand, acceptance_response, "target create API round trip"
                ),
                "acceptance_response_proxy_to_http_ready": _seconds(
                    acceptance_response, http_ready, "proxy to HTTP readiness"
                ),
                "acceptance_response_proxy_to_kubernetes_ready": round(
                    proxy_ready_seconds, 6
                ),
                "acceptance_response_proxy_to_two_semantic_responses": _seconds(
                    acceptance_response,
                    second_response_received,
                    "proxy to two semantic responses",
                ),
                "acceptance_response_proxy_to_first_semantic_response": _seconds(
                    acceptance_response,
                    _timestamp(
                        semantic_summary["cases"][0]["response_received_at"],
                        "first semantic response",
                    ),
                    "proxy to first semantic response",
                ),
            }
        )

    result = {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS",
        "run_id": run_id,
        "request_count": 2,
        "response_timing_contract": semantic_summary["response_timing_contract"],
        "semantic_pass_count": 2,
        "checkpoint_id": run["checkpoint_id"],
        "artifact_manifest_sha256": run["artifact_manifest_sha256"],
        "demand_at": target_submit_at,
        "t0_at": target_submit_at,
        "t0_source": "target-submit-at.txt",
        "setup_demand_at": run["demand_at"],
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
        "timings_seconds": timings_seconds,
        "evidence": {
            "worker_completed_at": worker_receipt["completed_at"],
            "worker_job_completed_at": worker_completed.isoformat(),
            "probe_job_completed_at": probe_completed.isoformat(),
            "http_ready_at": http_ready.isoformat(),
            "kubernetes_ready_at": ready.isoformat(),
            "second_response_received_at": semantic_summary["cases"][1][
                "response_received_at"
            ],
            "validation_finished_at": semantic_summary["validation_finished_at"],
            "validator": semantic_summary["validator"],
        },
        "qualification": qualification_receipt,
    }
    if boot_upper_bounds is not None:
        result["timings_upper_bound_seconds"] = boot_upper_bounds
    return result


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
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--target-submit-at", type=Path, required=True)
    parser.add_argument("--target-create-response-at", type=Path, required=True)
    parser.add_argument("--qualification-receipt", type=Path, required=True)
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
            target_submit_at=args.target_submit_at.read_text(encoding="utf-8").strip(),
            target_create_response_at=args.target_create_response_at.read_text(
                encoding="utf-8"
            ).strip(),
            qualification_receipt=_object(
                _load(args.qualification_receipt, "qualification receipt"),
                "qualification receipt",
            ),
        )
    except (EvidenceError, render.RenderError) as exc:
        print(f"evidence: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
