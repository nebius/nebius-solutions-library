#!/usr/bin/env python3
"""Build benchmark timings from semantic HTTP and Kubernetes evidence."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


class TimingEvidenceError(ValueError):
    """Raised when timing evidence is missing, malformed, or contradictory."""


RESPONSE_TIMING_CONTRACT = "request-dispatch-to-complete-http-body/v1"
ACCEPTANCE_RESPONSE_PROXY = "client-observed-api-create-response-return/v1"


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TimingEvidenceError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimingEvidenceError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimingEvidenceError(f"{label} timestamp must include a UTC offset")
    return parsed


def _elapsed(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise TimingEvidenceError(f"{label} must be a finite nonnegative number")
    return round(float(value), 6)


def _kubernetes_ready(target: dict[str, Any]) -> tuple[datetime, str]:
    conditions = target.get("status", {}).get("conditions")
    if not isinstance(conditions, list):
        raise TimingEvidenceError("target has no Kubernetes conditions")
    matches = [
        condition
        for condition in conditions
        if isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
    ]
    if len(matches) != 1:
        raise TimingEvidenceError(
            "target must have exactly one successful Kubernetes Ready condition"
        )
    raw = matches[0].get("lastTransitionTime")
    return _timestamp(raw, "Kubernetes Ready"), str(raw)


def _http_ready(semantic: dict[str, Any]) -> tuple[datetime, datetime, str]:
    ready = semantic.get("ready_wait")
    if ready is None:
        ready = semantic.get("ready")
    if isinstance(ready, dict):
        if ready.get("status") != "PASS":
            raise TimingEvidenceError(
                "semantic probe has no successful HTTP readiness receipt"
            )
        base_url = semantic.get("base_url")
        if (
            not isinstance(base_url, str)
            or ready.get("endpoint") != base_url.rstrip("/") + "/v1/health/ready"
        ):
            raise TimingEvidenceError(
                "HTTP readiness receipt is not bound to the semantic probe origin"
            )
        started = _timestamp(ready.get("started_at"), "HTTP readiness probe start")
        raw_finished = ready.get("finished_at")
        return started, _timestamp(raw_finished, "successful HTTP readiness"), str(
            raw_finished
        )

    raw_ready = semantic.get("ready_at")
    http_ready = _timestamp(raw_ready, "successful HTTP readiness")
    return (
        _timestamp(semantic.get("started_at"), "semantic probe start"),
        http_ready,
        str(raw_ready),
    )


def build_timing_evidence(
    run: dict[str, Any],
    semantic: dict[str, Any],
    target: dict[str, Any],
    *,
    target_submit_at: str | None = None,
    target_create_response_at: str | None = None,
) -> dict[str, Any]:
    """Return the standardized warm-instance benchmark timing fields.

    Production-shaped callers pass the timestamp persisted immediately before
    target creation as ``target_submit_at``.  Fresh-cohort callers additionally
    pass the client timestamp recorded immediately after the API create response
    as ``target_create_response_at``.  That second edge is an explicitly labeled
    proxy and is never represented as exact server-side acceptance.

    Worker timing is deliberately absent: worker receipt and semantic probing are
    independent concurrent timelines and must not be ordered against one another.
    """

    if (
        semantic.get("status") != "PASS"
        or semantic.get("ok") is not True
        or semantic.get("request_count") != 2
        or semantic.get("passed_case_count") != 2
        or semantic.get("failed_case_count") != 0
    ):
        raise TimingEvidenceError("semantic summary is not a strict two-call PASS")
    cases = semantic.get("cases")
    if not isinstance(cases, list) or len(cases) != 2:
        raise TimingEvidenceError("semantic summary must contain exactly two cases")
    if semantic.get("response_timing_contract") != RESPONSE_TIMING_CONTRACT:
        raise TimingEvidenceError("semantic summary has no reviewed response timing contract")
    validation_total = _elapsed(
        semantic.get("validation_total_elapsed_seconds"),
        "semantic validation total",
    )
    if semantic.get("total_elapsed_seconds") != validation_total:
        raise TimingEvidenceError(
            "total_elapsed_seconds is not the validation-total compatibility alias"
        )
    calls: list[float] = []
    call_boundaries: list[tuple[datetime, datetime, str, str]] = []
    for index, case in enumerate(cases, 1):
        if (
            not isinstance(case, dict)
            or case.get("status") != "PASS"
            or case.get("ok") is not True
        ):
            raise TimingEvidenceError(f"semantic request {index} did not pass")
        calls.append(_elapsed(case.get("elapsed_seconds"), f"semantic request {index}"))
        request_started_raw = case.get("request_started_at")
        response_received_raw = case.get("response_received_at")
        request_started = _timestamp(
            request_started_raw, f"semantic request {index} start"
        )
        response_received = _timestamp(
            response_received_raw, f"semantic response {index} receipt"
        )
        if response_received < request_started:
            raise TimingEvidenceError(
                f"semantic response {index} precedes its request"
            )
        call_boundaries.append(
            (
                request_started,
                response_received,
                str(request_started_raw),
                str(response_received_raw),
            )
        )

    setup_demand_raw = run.get("demand_at")
    demand_raw = target_submit_at if target_submit_at is not None else setup_demand_raw
    semantic_started_raw = semantic.get("started_at")
    validation_finished_raw = semantic.get("validation_finished_at")
    setup_demand = _timestamp(setup_demand_raw, "setup demand")
    demand = _timestamp(demand_raw, "target submit/T0")
    if demand < setup_demand:
        raise TimingEvidenceError("target submit/T0 precedes setup demand")
    semantic_started = _timestamp(semantic_started_raw, "semantic probe start")
    validation_finished = _timestamp(
        validation_finished_raw, "semantic validation completion"
    )
    if semantic.get("finished_at") != validation_finished_raw:
        raise TimingEvidenceError(
            "finished_at is not the validation_finished_at compatibility alias"
        )
    http_probe_started, http_ready, http_ready_raw = _http_ready(semantic)
    kubernetes_ready, kubernetes_ready_raw = _kubernetes_ready(target)

    acceptance_response = None
    if target_create_response_at is not None:
        acceptance_response = _timestamp(
            target_create_response_at, "client-observed target create response"
        )
        if not demand <= acceptance_response <= semantic_started:
            raise TimingEvidenceError(
                "target create response is outside the submit/probe-start interval"
            )

    if not (
        demand
        <= semantic_started
        <= http_probe_started
        <= http_ready
        <= call_boundaries[0][0]
        <= call_boundaries[0][1]
        <= call_boundaries[1][0]
        <= call_boundaries[1][1]
        <= validation_finished
    ):
        raise TimingEvidenceError("semantic probe timestamps are not monotonically ordered")
    kubernetes_ready_seconds = (kubernetes_ready - demand).total_seconds()
    if kubernetes_ready_seconds < 0:
        if abs(kubernetes_ready_seconds) >= 1:
            raise TimingEvidenceError(
                "Kubernetes Ready precedes demand by at least one second"
            )
        # Kubernetes condition timestamps may be serialized to whole seconds.
        # Preserve the raw timestamp below, but normalize only this bounded
        # same-second precision inversion for the diagnostic duration.
        kubernetes_ready_seconds = 0.0

    result: dict[str, Any] = {
        "response_timing_contract": RESPONSE_TIMING_CONTRACT,
        "demand_to_http_ready_seconds": round(
            (http_ready - demand).total_seconds(), 6
        ),
        "demand_to_kubernetes_ready_seconds": round(kubernetes_ready_seconds, 6),
        "demand_to_two_semantic_seconds": round(
            (call_boundaries[1][1] - demand).total_seconds(), 6
        ),
        "demand_to_first_semantic_seconds": round(
            (call_boundaries[0][1] - demand).total_seconds(), 6
        ),
        "semantic_request_1_seconds": calls[0],
        "semantic_request_2_seconds": calls[1],
        "timing_evidence": {
            "demand_at": demand_raw,
            "t0_at": demand_raw,
            "t0_source": (
                "target-submit-at.txt" if target_submit_at is not None else "run.json:demand_at"
            ),
            "setup_demand_at": setup_demand_raw,
            "http_ready_at": http_ready_raw,
            "kubernetes_ready_at": kubernetes_ready_raw,
            "semantic_started_at": semantic_started_raw,
            "semantic_response_1_received_at": call_boundaries[0][3],
            "semantic_response_2_received_at": call_boundaries[1][3],
            "validation_finished_at": validation_finished_raw,
        },
    }
    if acceptance_response is not None:
        proxy_ready_seconds = (kubernetes_ready - acceptance_response).total_seconds()
        if proxy_ready_seconds < 0:
            if abs(proxy_ready_seconds) >= 1:
                raise TimingEvidenceError(
                    "Kubernetes Ready precedes the create-response proxy by at least one second"
                )
            proxy_ready_seconds = 0.0
        result.update(
            {
                "target_create_api_round_trip_seconds": round(
                    (acceptance_response - demand).total_seconds(), 6
                ),
                "acceptance_response_proxy_to_http_ready_seconds": round(
                    (http_ready - acceptance_response).total_seconds(), 6
                ),
                "acceptance_response_proxy_to_kubernetes_ready_seconds": round(
                    proxy_ready_seconds, 6
                ),
                "acceptance_response_proxy_to_two_semantic_seconds": round(
                    (call_boundaries[1][1] - acceptance_response).total_seconds(), 6
                ),
                "acceptance_response_proxy_to_first_semantic_seconds": round(
                    (call_boundaries[0][1] - acceptance_response).total_seconds(), 6
                ),
            }
        )
        result["timing_evidence"].update(
            {
                "target_create_response_at": target_create_response_at,
                "target_create_response_proxy_label": ACCEPTANCE_RESPONSE_PROXY,
                "target_create_response_is_exact_server_acceptance": False,
            }
        )
    return result
