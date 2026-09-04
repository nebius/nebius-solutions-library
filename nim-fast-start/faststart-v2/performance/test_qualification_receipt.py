from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qualification_receipt as qualification


UID = "11111111-1111-4111-8111-111111111111"
IMAGE = "registry.example/openfold2@sha256:" + "a" * 64
POD_SPEC = "b" * 64
BOOT_ID = "22222222-2222-4222-8222-222222222222"
ANCHOR_BOOT_NS = 5_000_000_000_000


def target() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "of2-target-fresh-001",
            "namespace": "nim-fast-start",
            "uid": UID,
            "creationTimestamp": "2026-08-18T00:00:00Z",
            "annotations": {
                "archvteams.nebius.ai/target-pod-spec-sha256": POD_SPEC
            },
        },
        "spec": {
            "nodeName": "qualified-h100",
            "containers": [
                {
                    "name": "openfold2",
                    "startupProbe": qualification.EXPECTED_STARTUP_PROBE,
                }
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "lastTransitionTime": "2026-08-18T00:00:03Z",
                }
            ],
            "containerStatuses": [
                {
                    "name": "openfold2",
                    "restartCount": 0,
                    "state": {
                        "running": {"startedAt": "2026-08-18T00:00:01Z"}
                    },
                    "lastState": {},
                }
            ],
        },
    }


def create_response() -> dict:
    value = target()
    value["status"] = {}
    return value


def job_pod(container: str) -> dict:
    return {
        "spec": {"nodeName": "qualified-h100"},
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "name": container,
                    "restartCount": 0,
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "reason": "Completed",
                            "startedAt": "2026-08-18T00:00:01Z",
                            "finishedAt": "2026-08-18T00:00:02Z",
                        }
                    },
                    "lastState": {},
                }
            ],
        }
    }


def worker_receipt() -> dict:
    return {
        "schema": qualification.RESTORE_RECEIPT_SCHEMA,
        "status": "succeeded",
        "completed_at": "2026-08-18T00:00:02.500000Z",
        "duration_ms": 1500,
        "run_id": "fresh-001",
        "target_namespace": "nim-fast-start",
        "target_name": "of2-target-fresh-001",
        "target_uid": UID,
        "target_node": "qualified-h100",
        "target_pod_spec_sha256": POD_SPEC,
        "target_image_id": IMAGE,
    }


def events() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "EventList",
        "items": [
            {
                "type": "Normal",
                "reason": "Scheduled",
                "message": "Successfully assigned",
                "involvedObject": {"uid": UID},
            },
            {
                "type": "Normal",
                "reason": "Pulled",
                "message": f'Container image "{IMAGE}" already present on machine',
                "involvedObject": {"uid": UID},
            },
        ],
    }


def startup_warning(*, last_timestamp: str = "2026-08-18T00:00:02Z") -> dict:
    return {
        "type": "Warning",
        "reason": "Unhealthy",
        "message": "Startup probe failed: ",
        "count": 13,
        "firstTimestamp": "2026-08-18T00:00:01Z",
        "lastTimestamp": last_timestamp,
        "metadata": {"creationTimestamp": "2026-08-18T00:00:01Z"},
        "reportingComponent": "kubelet",
        "reportingInstance": "qualified-h100",
        "source": {"component": "kubelet", "host": "qualified-h100"},
        "involvedObject": {
            "kind": "Pod",
            "name": "of2-target-fresh-001",
            "namespace": "nim-fast-start",
            "uid": UID,
            "fieldPath": "spec.containers{openfold2}",
        },
    }


GPU_XML = b"""<?xml version="1.0" ?>
<nvidia_smi_log>
  <attached_gpus>1</attached_gpus>
  <gpu id="00000000:01:00.0">
    <product_name>NVIDIA H100 80GB HBM3</product_name>
    <uuid>GPU-11111111-2222-3333-4444-555555555555</uuid>
  </gpu>
</nvidia_smi_log>
"""


def controller_boundary(phase: str) -> dict:
    if phase == "cohort-admission":
        utc, monotonic_ns = "2026-08-18T00:00:00.000000Z", 100_000_000_000
    else:
        utc, monotonic_ns = "2026-08-18T00:00:00.100000Z", 101_000_000_000
    return {
        "schema": qualification.CONTROLLER_CLOCK_BOUNDARY_SCHEMA,
        "phase": phase,
        "utc": utc,
        "monotonic_ns": monotonic_ns,
    }


def node_clock(*, observed: bool) -> dict:
    result = {
        "schema": qualification.SEMANTIC_NODE_BOOTTIME_SCHEMA,
        "clock_id": "CLOCK_BOOTTIME",
        "boot_id": BOOT_ID,
        "clock_resolution_ns": 1,
        "timens_offsets": [
            {"clock": "monotonic", "seconds": 0, "nanoseconds": 0},
            {"clock": "boottime", "seconds": 0, "nanoseconds": 0},
        ],
    }
    if observed:
        result["boottime_ns"] = ANCHOR_BOOT_NS
    return result


def anchor_holder() -> dict:
    image = qualification.BOOT_TIME_ANCHOR_HOLDER_IMAGE
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "artifact-holder",
            "namespace": "nim-fast-start",
            "uid": "99999999-9999-4999-8999-999999999999",
        },
        "spec": {
            "nodeName": "qualified-h100",
            "containers": [
                {"name": "holder", "image": image, "resources": {}}
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "holder",
                    "image": "sha256:" + "1" * 64,
                    "imageID": image,
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-08-17T23:59:00Z"}},
                }
            ],
        },
    }


def boot_time_anchor() -> dict:
    return {
        "schema": qualification.BOOT_TIME_ANCHOR_SCHEMA,
        "phase": "pre-t0-anchor",
        "sampled_pod_name": "artifact-holder",
        "sampled_pod_uid": "99999999-9999-4999-8999-999999999999",
        "target_node": "qualified-h100",
        "sampled_container": "holder",
        "expected_holder_image": qualification.BOOT_TIME_ANCHOR_HOLDER_IMAGE,
        "controller_before": {
            "utc": "2026-08-18T00:00:00.020000Z",
            "monotonic_ns": 100_100_000_000,
        },
        "node_observed": node_clock(observed=True),
        "controller_after": {
            "utc": "2026-08-18T00:00:00.080000Z",
            "monotonic_ns": 100_200_000_000,
        },
    }


def semantic_summary() -> dict:
    ready_start = ANCHOR_BOOT_NS + 1_000_000_000
    ready_dispatch = ready_start + 100_000_000
    ready_body = ready_start + 5_000_000_000
    ready_finish = ready_body + 1_000
    call1_dispatch = ready_finish + 1_000
    call1_body = call1_dispatch + 2_000_000_000
    call2_dispatch = call1_body + 1_000
    call2_body = call2_dispatch + 1_000_000_000
    finish = call2_body + 1_000
    return {
        "schema_version": qualification.SEMANTIC_SCHEMA_VERSION,
        "status": "PASS",
        "ok": True,
        "passed_case_count": 2,
        "failed_case_count": 0,
        "started_at": "2026-08-18T00:00:01Z",
        "started_boottime_ns": ready_start,
        "node_clock": node_clock(observed=False),
        "ready_wait": {
            "status": "PASS",
            "started_at": "2026-08-18T00:00:01Z",
            "started_boottime_ns": ready_start,
            "request_dispatched_boottime_ns": ready_dispatch,
            "response_body_received_boottime_ns": ready_body,
            "finished_at": "2026-08-18T00:00:06Z",
            "finished_boottime_ns": ready_finish,
            "elapsed_seconds": round((ready_finish - ready_start) / 1e9, 6),
        },
        "cases": [
            {
                "index": 1,
                "status": "PASS",
                "ok": True,
                "request_dispatched_boottime_ns": call1_dispatch,
                "response_body_received_boottime_ns": call1_body,
                "elapsed_seconds": 2.0,
                "response_received_at": "2026-08-18T00:00:09Z",
            },
            {
                "index": 2,
                "status": "PASS",
                "ok": True,
                "request_dispatched_boottime_ns": call2_dispatch,
                "response_body_received_boottime_ns": call2_body,
                "elapsed_seconds": 1.0,
                "response_received_at": "2026-08-18T00:00:10Z",
            },
        ],
        "validation_finished_at": "2026-08-18T00:00:10.100000Z",
        "validation_finished_boottime_ns": finish,
        "total_elapsed_seconds": round((finish - ready_start) / 1e9, 6),
        "validation_total_elapsed_seconds": round((finish - ready_start) / 1e9, 6),
    }


class QualificationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.gpu_xml = Path(self.temporary.name) / "nvidia-smi.xml"
        self.gpu_xml.write_bytes(GPU_XML)
        self.gpu_stderr = Path(self.temporary.name) / "nvidia-smi.stderr"
        self.gpu_stderr.write_bytes(b"")

    def build(self, **changes: object) -> dict:
        values = {
            "model": "openfold2",
            "run_id": "fresh-001",
            "namespace": "nim-fast-start",
            "target_name": "of2-target-fresh-001",
            "target_container": "openfold2",
            "expected_image": IMAGE,
            "target_submit_at": "2026-08-18T00:00:00.100000Z",
            "target_create_response_at": "2026-08-18T00:00:00.400000Z",
            "target_create_response": create_response(),
            "target": target(),
            "target_events": events(),
            "worker_pod": job_pod("restore-worker"),
            "worker_receipt": worker_receipt(),
            "worker_container": "restore-worker",
            "probe_pod": job_pod("semantic-probe"),
            "probe_container": "semantic-probe",
            "semantic_summary": semantic_summary(),
            "gpu_health_xml": self.gpu_xml,
            "gpu_health_stderr": self.gpu_stderr,
            "admission_boundary": controller_boundary("cohort-admission"),
            "target_submit_clock": controller_boundary("target-submit"),
            "boot_time_anchor": boot_time_anchor(),
            "anchor_holder": anchor_holder(),
        }
        values.update(changes)
        return qualification.build_receipt(**values)  # type: ignore[arg-type]

    def test_builds_cached_image_zero_restart_bounded_gpu_receipt(self) -> None:
        receipt = self.build()
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(
            receipt["timing_boundaries"]["acceptance_response_proxy"][
                "client_observed_api_round_trip_seconds"
            ],
            0.3,
        )
        self.assertFalse(
            receipt["timing_boundaries"]["acceptance_response_proxy"][
                "is_exact_server_acceptance"
            ]
        )
        self.assertEqual(receipt["pod_health"]["target"]["restart_count"], 0)
        self.assertEqual(receipt["gpu_health"]["attached_gpu_count"], 1)
        self.assertEqual(
            receipt["gpu_health"]["host_xid_check"]["status"], "unavailable"
        )
        self.assertEqual(receipt["boot_time_alignment"]["status"], "PASS")
        self.assertEqual(
            receipt["boot_time_alignment"]["conservative_upper_bounds"]
            ["two_semantic_responses_complete_body"]["upper_bound_seconds"],
            9.000004,
        )

    def test_rejects_image_pull_or_non_cached_pulled_event(self) -> None:
        for reason, message in (
            ("Pulling", f'Pulling image "{IMAGE}"'),
            ("Pulled", f'Successfully pulled image "{IMAGE}"'),
        ):
            with self.subTest(reason=reason):
                changed = events()
                changed["items"][1].update({"reason": reason, "message": message})
                with self.assertRaises(qualification.QualificationError):
                    self.build(target_events=changed)

    def test_rejects_warning_event(self) -> None:
        changed = events()
        changed["items"].append(
            {
                "type": "Warning",
                "reason": "FailedMount",
                "message": "mount failed",
                "involvedObject": {"uid": UID},
            }
        )
        with self.assertRaisesRegex(qualification.QualificationError, "Warning"):
            self.build(target_events=changed)

    def test_accepts_exact_startup_probe_warning_before_restore(self) -> None:
        changed = events()
        changed["items"].append(startup_warning())
        receipt = self.build(target_events=changed)
        event_receipt = receipt["warm_instance"]["target_events"]
        self.assertEqual(event_receipt["warning_event_count"], 1)
        self.assertEqual(
            event_receipt["expected_startup_probe_warning_event_count"], 1
        )
        self.assertEqual(
            event_receipt["expected_startup_probe_warning_occurrence_count"], 13
        )
        self.assertEqual(event_receipt["unexpected_warning_event_count"], 0)

    def test_rejects_startup_probe_warning_after_restore(self) -> None:
        changed = events()
        changed["items"].append(
            startup_warning(last_timestamp="2026-08-18T00:00:04Z")
        )
        with self.assertRaisesRegex(
            qualification.QualificationError, "pre-restore startup window"
        ):
            self.build(target_events=changed)

    def test_rejects_drifted_startup_warning_or_probe_contract(self) -> None:
        mutations = (
            ("message", "Readiness probe failed:"),
            ("count", True),
            ("count", 1800),
            ("reportingInstance", "other-node"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                changed = events()
                warning = startup_warning()
                warning[field] = value
                changed["items"].append(warning)
                with self.assertRaises(qualification.QualificationError):
                    self.build(target_events=changed)

        changed_target = target()
        changed_target["spec"]["containers"][0]["startupProbe"] = copy.deepcopy(
            qualification.EXPECTED_STARTUP_PROBE
        )
        changed_target["spec"]["containers"][0]["startupProbe"][
            "failureThreshold"
        ] = 1799
        with self.assertRaisesRegex(
            qualification.QualificationError, "startup probe contract"
        ):
            self.build(target=changed_target)

    def test_startup_warning_does_not_mask_post_t0_pull(self) -> None:
        changed = events()
        changed["items"].append(startup_warning())
        changed["items"].append(
            {
                "type": "Normal",
                "reason": "Pulling",
                "message": f'Pulling image "{IMAGE}"',
                "involvedObject": {"uid": UID},
            }
        )
        with self.assertRaisesRegex(qualification.QualificationError, "Pulling"):
            self.build(target_events=changed)

    def test_rejects_restart_oom_eviction_and_nonzero_termination(self) -> None:
        mutations = []
        restarted = target()
        restarted["status"]["containerStatuses"][0]["restartCount"] = 1
        mutations.append(restarted)
        oom = target()
        oom["status"]["containerStatuses"][0]["lastState"] = {
            "terminated": {"exitCode": 137, "reason": "OOMKilled"}
        }
        mutations.append(oom)
        evicted = target()
        evicted["status"].update({"reason": "Evicted", "message": "node pressure"})
        mutations.append(evicted)
        for changed in mutations:
            with self.subTest(status=changed["status"]):
                with self.assertRaises(qualification.QualificationError):
                    self.build(target=changed)

        worker = job_pod("restore-worker")
        worker["status"]["containerStatuses"][0]["state"]["terminated"][
            "exitCode"
        ] = 2
        with self.assertRaises(qualification.QualificationError):
            self.build(worker_pod=worker)

    def test_rejects_invalid_nvidia_smi_or_create_response_bracket(self) -> None:
        self.gpu_xml.write_text("not xml", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "valid XML"):
            self.build()
        self.gpu_xml.write_bytes(GPU_XML)
        self.gpu_stderr.write_text("driver warning", encoding="utf-8")
        with self.assertRaisesRegex(qualification.QualificationError, "stderr"):
            self.build()
        self.gpu_stderr.write_bytes(b"")
        with self.assertRaisesRegex(qualification.QualificationError, "precedes"):
            self.build(target_create_response_at="2026-08-17T23:59:59Z")

    def test_rejects_cross_node_or_wrong_boot_identity(self) -> None:
        probe = job_pod("semantic-probe")
        probe["spec"]["nodeName"] = "other-node"
        with self.assertRaisesRegex(qualification.QualificationError, "one node clock"):
            self.build(probe_pod=probe)

        changed = boot_time_anchor()
        changed["target_node"] = "other-node"
        with self.assertRaisesRegex(qualification.QualificationError, "identity"):
            self.build(boot_time_anchor=changed)

        summary = semantic_summary()
        summary["node_clock"]["boot_id"] = "33333333-3333-4333-8333-333333333333"
        with self.assertRaisesRegex(qualification.QualificationError, "boot/time"):
            self.build(semantic_summary=summary)

    def test_rejects_stale_anchor_and_controller_ordering(self) -> None:
        stale_t0 = controller_boundary("target-submit")
        stale_t0["monotonic_ns"] = 101_400_000_001
        with self.assertRaisesRegex(qualification.QualificationError, "monotonic"):
            self.build(target_submit_clock=stale_t0)

        late_anchor = boot_time_anchor()
        late_anchor["controller_after"]["monotonic_ns"] = 101_000_000_001
        with self.assertRaisesRegex(qualification.QualificationError, "monotonic"):
            self.build(boot_time_anchor=late_anchor)

        before_admission = boot_time_anchor()
        before_admission["controller_before"]["monotonic_ns"] = 99_999_999_999
        with self.assertRaisesRegex(qualification.QualificationError, "monotonic"):
            self.build(boot_time_anchor=before_admission)

    def test_wall_clock_steps_do_not_invalidate_monotonic_contract(self) -> None:
        admission = controller_boundary("cohort-admission")
        admission["utc"] = "2026-08-18T00:10:00Z"
        anchor = boot_time_anchor()
        anchor["controller_before"]["utc"] = "2026-08-17T23:50:00Z"
        anchor["controller_after"]["utc"] = "2026-08-18T01:00:00Z"
        receipt = self.build(
            admission_boundary=admission,
            boot_time_anchor=anchor,
        )
        self.assertFalse(
            receipt["boot_time_alignment"]["controller_boundaries"]
            ["wall_clock_ordered_diagnostic"]
        )

    def test_rejects_malformed_bool_identity_and_elapsed_drift(self) -> None:
        anchor = boot_time_anchor()
        anchor["node_observed"]["clock_resolution_ns"] = True
        with self.assertRaisesRegex(qualification.QualificationError, "positive integer"):
            self.build(boot_time_anchor=anchor)

        holder = anchor_holder()
        holder["status"]["containerStatuses"][0]["restartCount"] = True
        with self.assertRaisesRegex(qualification.QualificationError, "image/status"):
            self.build(anchor_holder=holder)

        summary = semantic_summary()
        summary["cases"][0]["elapsed_seconds"] = 2.000001
        with self.assertRaisesRegex(qualification.QualificationError, "not reproduced"):
            self.build(semantic_summary=summary)

    def test_live_holder_shape_requires_exact_spec_image_and_image_id(self) -> None:
        live_shape = anchor_holder()
        self.assertTrue(
            live_shape["status"]["containerStatuses"][0]["image"].startswith(
                "sha256:"
            )
        )
        self.build(anchor_holder=live_shape)

        wrong_spec = anchor_holder()
        wrong_spec["spec"]["containers"][0]["image"] = "python:latest"
        with self.assertRaisesRegex(qualification.QualificationError, "image/status"):
            self.build(anchor_holder=wrong_spec)

        wrong_image_id = anchor_holder()
        wrong_image_id["status"]["containerStatuses"][0]["imageID"] = (
            "docker.io/library/python@sha256:" + "0" * 64
        )
        with self.assertRaisesRegex(qualification.QualificationError, "image/status"):
            self.build(anchor_holder=wrong_image_id)

    def test_rejects_timens_resolution_and_event_order_drift(self) -> None:
        summary = semantic_summary()
        summary["node_clock"]["timens_offsets"][1]["seconds"] = 1
        with self.assertRaisesRegex(qualification.QualificationError, "boot/time"):
            self.build(semantic_summary=summary)

        summary = semantic_summary()
        summary["node_clock"]["clock_resolution_ns"] = 2
        with self.assertRaisesRegex(qualification.QualificationError, "boot/time"):
            self.build(semantic_summary=summary)

        summary = semantic_summary()
        summary["cases"][1]["request_dispatched_boottime_ns"] = (
            summary["cases"][0]["response_body_received_boottime_ns"] - 1
        )
        with self.assertRaises(qualification.QualificationError):
            self.build(semantic_summary=summary)

    def test_conservative_upper_bound_is_upward_rounded(self) -> None:
        anchor = boot_time_anchor()
        anchor["node_observed"]["clock_resolution_ns"] = 3
        summary = semantic_summary()
        summary["node_clock"]["clock_resolution_ns"] = 3
        receipt = self.build(
            boot_time_anchor=anchor,
            semantic_summary=summary,
        )
        upper = receipt["boot_time_alignment"]["conservative_upper_bounds"]
        self.assertEqual(
            upper["first_semantic_response_complete_body"]["upper_bound_ns"],
            8_000_002_006,
        )
        self.assertEqual(
            upper["first_semantic_response_complete_body"]["upper_bound_seconds"],
            8.000003,
        )


if __name__ == "__main__":
    unittest.main()
