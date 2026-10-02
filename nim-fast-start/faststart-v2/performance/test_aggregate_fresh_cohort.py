from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path


HERE = Path(__file__).resolve().parent
FASTSTART = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FASTSTART))

import aggregate_fresh_cohort as aggregate  # noqa: E402
import instrumentation_contract as instrumentation_builder  # noqa: E402
import qualification_receipt as qualification_builder  # noqa: E402
import timing_evidence  # noqa: E402
from dynamo import evidence as openfold2_evidence  # noqa: E402
from dynamo import render as openfold2_render  # noqa: E402


NODE = "gpu-node-a.example.invalid"
NAMESPACE = "nim-fast-start"
ARTIFACT_CLAIM = "openfold2-artifacts-example"
GPU_XML = b"""<?xml version="1.0"?>
<nvidia_smi_log><attached_gpus>1</attached_gpus><gpu>
<product_name>NVIDIA H100 80GB HBM3</product_name>
<uuid>GPU-11111111-2222-3333-4444-555555555555</uuid>
</gpu></nvidia_smi_log>
"""


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def canonical_sha(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def holder_receipt(
    name: str, uid: str, claims: tuple[str, ...], checked: datetime
) -> dict:
    volumes = []
    mounts = []
    verifications = []
    for index, claim in enumerate(claims, 1):
        volume = f"storage-{index}"
        mount = f"/storage/{index}"
        volumes.append(
            {"name": volume, "persistentVolumeClaim": {"claimName": claim}}
        )
        mounts.append({"name": volume, "mountPath": mount, "readOnly": True})
        mount_checked = checked + timedelta(milliseconds=index * 10)
        verifications.append(
            {
                "checked_at": iso(mount_checked),
                "claim": claim,
                "container": "holder",
                "volume_name": volume,
                "mount_path": mount,
                "command": ["/bin/test", "-d", mount],
                "status": "PASS",
                "exit_code": 0,
            }
        )
    return {
        "schema": "archvteams.nebius.ai/warm-storage-holder-check/v1",
        "checked_at": iso(checked),
        "pod": {
            "metadata": {"name": name, "uid": uid},
            "spec": {
                "nodeName": NODE,
                "volumes": volumes,
                "containers": [
                    {"name": "holder", "volumeMounts": mounts}
                ],
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {"name": "holder", "ready": True, "restartCount": 0}
                ],
            },
        },
        "mount_verifications": verifications,
    }


def object_receipt(api: str, kind: str, name: str, uid: str) -> dict:
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE, "uid": uid},
    }


class CohortFixture:
    def __init__(
        self,
        root: Path,
        model: str = "openfold2",
        count: int = 20,
        coarse_kube_edge: bool = False,
    ):
        self.root = root
        self.model = model
        self.count = count
        self.coarse_kube_edge = coarse_kube_edge
        self.evidence = root / "evidence"
        self.runs = self.evidence / "runs"
        self.cohort = self.evidence / "cohorts" / "fresh-cohort"
        self.runs.mkdir(parents=True)
        self.cohort.mkdir(parents=True)
        self.ledger = self.cohort / "attempts.ndjson"
        self.instrumentation = instrumentation_builder.build_contract(model)
        write_json(self.cohort / "instrumentation-contract.json", self.instrumentation)
        self.base = datetime(2026, 8, 18, tzinfo=UTC)
        self.events: list[dict] = [
            {
                "schema": aggregate.LEDGER_SCHEMA,
                "event": "cohort_started",
                "cohort_id": "fresh-cohort",
                "model": model,
                "run_prefix": "fresh",
                "evidence_root": str(self.evidence),
                "started_at": iso(self.base),
                "runner_sha256": "e" * 64,
                "instrumentation_contract_sha256": self.instrumentation[
                    "instrumentation_contract_sha256"
                ],
                "requested_attempt_count": count,
                "maximum_scheduled_attempts": count + 10,
            }
        ]
        self.trials: list[Path] = []
        self.raw_two: list[float] = []
        for index in range(1, count + 1):
            self.add_attempt(index)
        finish = self.base + timedelta(minutes=2 * (count + 1))
        self.events.append(
            {
                "schema": aggregate.LEDGER_SCHEMA,
                "event": "cohort_finished",
                "cohort_id": "fresh-cohort",
                "model": model,
                "finished_at": iso(finish),
                "requested_attempt_count": count,
                "admitted_attempt_count": count,
                "scheduled_attempt_count": count,
                "controller_abort": False,
            }
        )
        self.flush()

    def uid(self, index: int, resource: int) -> str:
        return f"{index:08x}-0000-4000-8000-{resource:012d}"

    def add_attempt(self, index: int) -> None:
        approved = aggregate.APPROVED_CONTRACTS[self.model]
        prefix = "of2" if self.model == "openfold2" else "b2"
        container_name = "openfold2" if self.model == "openfold2" else "boltz2"
        run_id = f"fresh-{index:03d}"
        trial = self.runs / run_id
        trial.mkdir()
        self.trials.append(trial)
        write_json(trial / "instrumentation-contract.json", self.instrumentation)

        admitted = self.base + timedelta(minutes=2 * index)
        demand = admitted - timedelta(seconds=2)
        t0 = admitted + timedelta(milliseconds=100)
        api_response = t0 + timedelta(milliseconds=200)
        semantic_started = t0 + timedelta(milliseconds=500)
        ready_started = t0 + timedelta(milliseconds=600)
        http_ready = t0 + timedelta(seconds=8 + index / 10)
        response_1 = http_ready + timedelta(seconds=1.6)
        request_1 = response_1 - timedelta(seconds=1.5)
        request_2 = response_1 + timedelta(milliseconds=100)
        response_2 = request_2 + timedelta(milliseconds=250)
        validation_finished = response_2 + timedelta(milliseconds=50)
        kube_ready = (
            t0.replace(microsecond=0)
            if self.coarse_kube_edge
            else t0 + timedelta(seconds=7 + index / 10)
        )
        cleanup_started = response_2 + timedelta(seconds=1)
        cleanup_finished = cleanup_started + timedelta(milliseconds=500)
        completed = cleanup_finished + timedelta(milliseconds=100)
        target_uid = self.uid(index, 1)
        container_id = "containerd://" + f"{index:064x}"
        pod_ip = f"10.20.{index // 250}.{index % 250 + 1}"

        target_spec = {
            "nodeName": NODE,
            "containers": [
                {
                    "name": container_name,
                    "image": approved["target_image"],
                    "command": ["/bin/sleep"],
                    "args": ["2147483647"],
                    "startupProbe": copy.deepcopy(
                        qualification_builder.EXPECTED_STARTUP_PROBE
                    ),
                }
            ],
            "volumes": [
                {
                    "name": "artifacts",
                    "persistentVolumeClaim": {"claimName": approved["artifact_pvc"]},
                },
                {
                    "name": "cache",
                    "persistentVolumeClaim": {"claimName": approved["cache_pvc"]},
                },
            ],
        }
        pod_spec_sha = canonical_sha(target_spec)
        creation = t0.replace(microsecond=0)
        target = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{prefix}-target-{run_id}",
                "namespace": NAMESPACE,
                "uid": target_uid,
                "creationTimestamp": iso(creation),
                "annotations": {
                    "archvteams.nebius.ai/target-pod-spec-sha256": pod_spec_sha
                },
            },
            "spec": target_spec,
            "status": {
                "phase": "Running",
                "podIP": pod_ip,
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "True",
                        "lastTransitionTime": iso(creation),
                    },
                    {
                        "type": "Ready",
                        "status": "True",
                        "lastTransitionTime": iso(kube_ready),
                    }
                ],
                "containerStatuses": [
                    {
                        "name": container_name,
                        "containerID": container_id,
                        "imageID": "docker-pullable://" + approved["target_image"],
                        "restartCount": 0,
                        "state": {
                            "running": {
                                "startedAt": iso(t0 + timedelta(milliseconds=400))
                            }
                        },
                        "lastState": {},
                    }
                ],
            },
        }
        create_response = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": target["metadata"]["name"],
                "namespace": NAMESPACE,
                "uid": target_uid,
                "creationTimestamp": iso(creation),
            },
        }
        events = {
            "apiVersion": "v1",
            "kind": "EventList",
            "items": [
                {
                    "type": "Normal",
                    "reason": "Scheduled",
                    "message": "assigned",
                    "involvedObject": {"uid": target_uid},
                },
                {
                    "type": "Normal",
                    "reason": "Pulled",
                    "message": (
                        f'Container image "{approved["target_image"]}" '
                        "already present on machine"
                    ),
                    "involvedObject": {"uid": target_uid},
                },
            ],
        }

        contract_source = (
            FASTSTART / "dynamo" / "restore-interface.live.json"
            if self.model == "openfold2"
            else FASTSTART / "boltz2-native" / "restore-interface.live.json"
        )
        contract_bytes = contract_source.read_bytes()
        (trial / "restore-interface.json").write_bytes(contract_bytes)
        (trial / "restore-interface.sha256").write_text(
            hashlib.sha256(contract_bytes).hexdigest() + "  restore-interface.json\n",
            encoding="utf-8",
        )
        contract = json.loads(contract_bytes)
        run = {
            "schema": approved["run_schema"],
            "run_id": run_id,
            "demand_at": iso(demand),
            "target_node": NODE,
            "checkpoint_id": approved["checkpoint_id"],
            "artifact_version": "1",
            "artifact_manifest_sha256": approved["artifact_manifest_sha256"],
            "artifact_pvc": approved["artifact_pvc"],
            "cache_pvc": approved["cache_pvc"],
        }
        binding = {
            "pod_uid": target_uid,
            "pod_spec_sha256": pod_spec_sha,
        }
        if self.model == "openfold2":
            binding.update(
                {
                    "schema": "archvteams.nebius.ai/openfold2-target-binding/v1",
                    "collected_at": iso(t0 + timedelta(seconds=1)),
                    "run_id": run_id,
                    "namespace": NAMESPACE,
                    "pod_name": target["metadata"]["name"],
                    "container_name": "openfold2",
                    "container_id": container_id,
                    "cgroup": f"/kubepods/burstable/pod{target_uid}",
                    "pod_ip": pod_ip,
                    "node": NODE,
                    "image_id": approved["target_image"],
                }
            )
        else:
            binding.update(
                {
                    "schema": "archvteams.nebius.ai/boltz2-target-binding/v1",
                    "namespace": NAMESPACE,
                    "pod_name": target["metadata"]["name"],
                    "container_name": "boltz2",
                    "container_id": container_id,
                    "image_id": approved["target_image"],
                    "node": NODE,
                    "pod_ip": pod_ip,
                }
            )
        write_json(trial / "run.json", run)
        write_json(trial / "binding.json", binding)
        write_json(trial / "target-final.json", target)
        write_json(trial / "target-create-response.json", create_response)
        (trial / "target-submit-at.txt").write_text(iso(t0) + "\n", encoding="utf-8")
        (trial / "target-create-response-at.txt").write_text(
            iso(api_response) + "\n", encoding="utf-8"
        )
        write_json(trial / "target-events.json", events)
        (trial / "target-nvidia-smi.xml").write_bytes(GPU_XML)
        (trial / "target-nvidia-smi.stderr").write_bytes(b"")

        worker_name = f"{prefix}-restore-{run_id}"
        probe_name = f"{prefix}-semantic-{run_id}"
        worker_uid = self.uid(index, 2)
        probe_uid = self.uid(index, 3)
        rendered_worker_job = None
        rendered_probe_job = None
        if self.model == "openfold2":
            rendered_worker_job = next(
                item
                for item in openfold2_render.render_restore(run, contract, binding)
                if item.get("kind") == "Job"
            )
            rendered_probe_job = next(
                item
                for item in openfold2_render.render_probe(run, contract, binding)
                if item.get("kind") == "Job"
            )
            worker_container = copy.deepcopy(
                rendered_worker_job["spec"]["template"]["spec"]["containers"][0]
            )
            probe_container = copy.deepcopy(
                rendered_probe_job["spec"]["template"]["spec"]["containers"][0]
            )
        else:
            worker_container = {
                "name": "restore-worker",
                "image": contract["worker_image"],
                "command": [contract["worker_executable"]],
                "args": ["restore", "--run-id", run_id],
            }
            probe_container = {
                "name": "semantic-probe",
                "image": contract["probe_image"],
                "command": [contract["probe_executable"]],
                "args": ["validate", "--run-id", run_id],
            }

        def job_and_pod(
            role: str,
            name: str,
            uid: str,
            container: dict,
            rendered_job: dict | None,
            finished_at: datetime,
        ) -> tuple[dict, dict]:
            if rendered_job is None:
                job = {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": name,
                        "namespace": NAMESPACE,
                        "annotations": {
                            "archvteams.nebius.ai/target-pod-uid": target_uid,
                            "archvteams.nebius.ai/target-pod-spec-sha256": pod_spec_sha,
                        },
                    },
                    "spec": {"template": {"spec": {"containers": [container]}}},
                }
            else:
                job = copy.deepcopy(rendered_job)
            job["metadata"]["uid"] = uid
            job["status"] = {
                "succeeded": 1,
                "completionTime": iso(finished_at + timedelta(milliseconds=100)),
            }
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": name + "-pod",
                    "namespace": NAMESPACE,
                    "uid": self.uid(index, 20 if role == "worker" else 21),
                    "ownerReferences": [
                        {"kind": "Job", "uid": uid, "controller": True}
                    ],
                },
                "spec": {"nodeName": NODE, "containers": [container]},
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {
                            "name": container["name"],
                            "restartCount": 0,
                            "state": {
                                "terminated": {
                                    "exitCode": 0,
                                    "reason": "Completed",
                                    "startedAt": iso(semantic_started),
                                    "finishedAt": iso(finished_at),
                                }
                            },
                            "lastState": {},
                        }
                    ],
                },
            }
            return job, pod

        worker_job, worker_pod = job_and_pod(
            "worker",
            worker_name,
            worker_uid,
            worker_container,
            rendered_worker_job,
            http_ready - timedelta(seconds=1),
        )
        probe_job, probe_pod = job_and_pod(
            "probe",
            probe_name,
            probe_uid,
            probe_container,
            rendered_probe_job,
            validation_finished,
        )
        write_json(trial / "worker-job.json", worker_job)
        write_json(trial / "probe-job.json", probe_job)
        write_json(trial / "worker-pod.json", worker_pod)
        write_json(trial / "probe-pod.json", probe_pod)
        rendered_worker = rendered_worker_job or {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": worker_name, "namespace": NAMESPACE},
            "spec": {"template": {"spec": {"containers": [worker_container]}}},
        }
        rendered_probe = rendered_probe_job or {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": probe_name, "namespace": NAMESPACE},
            "spec": {"template": {"spec": {"containers": [probe_container]}}},
        }
        write_json(trial / "worker-bundle" / "primary.json", rendered_worker)
        write_json(trial / "probe-bundle" / "primary.json", rendered_probe)

        worker_receipt = {
            "schema": "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1",
            "status": "succeeded",
            "run_id": run_id,
            "target_namespace": NAMESPACE,
            "target_name": target["metadata"]["name"],
            "target_uid": target_uid,
            "target_container_id": container_id,
            "target_image_id": approved["target_image"],
            "target_node": NODE,
            "target_pod_ip": pod_ip,
            "target_pod_spec_sha256": pod_spec_sha,
            "checkpoint_id": approved["checkpoint_id"],
            "artifact_version": "1",
            "checkpoint_manifest_sha256": approved["artifact_manifest_sha256"],
            "tool_bundle_manifest_sha256": contract["tool_bundle"]["content_sha256"],
            "duration_ms": 4000,
            "completed_at": iso(http_ready - timedelta(seconds=1)),
        }
        write_json(trial / "worker-receipt.json", worker_receipt)

        base_url = f"http://{prefix}-canary-{run_id}:8000"
        inference_path = (
            "/biology/openfold/openfold2/predict-structure-from-msa-and-template"
            if self.model == "openfold2"
            else "/biology/mit/boltz2/predict"
        )
        node_t0_boottime_ns = (
            20_000_000_000_000
            + round((t0 - self.base).total_seconds() * 1_000_000_000)
        )
        def event_boottime(value: datetime) -> int:
            return node_t0_boottime_ns + round(
                (value - t0).total_seconds() * 1_000_000_000
            )

        node_clock = {
            "schema": qualification_builder.SEMANTIC_NODE_BOOTTIME_SCHEMA,
            "clock_id": "CLOCK_BOOTTIME",
            "boot_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "clock_resolution_ns": 1,
            "timens_offsets": [
                {"clock": "monotonic", "seconds": 0, "nanoseconds": 0},
                {"clock": "boottime", "seconds": 0, "nanoseconds": 0},
            ],
        }
        semantic = {
            "schema_version": qualification_builder.SEMANTIC_SCHEMA_VERSION,
            "validator": (
                "openfold2-faststart-semantic-v1"
                if self.model == "openfold2"
                else "boltz2-faststart-semantic-v1"
            ),
            "status": "PASS",
            "ok": True,
            "exit_code": 0,
            "request_count": 2,
            "passed_case_count": 2,
            "failed_case_count": 0,
            "response_timing_contract": aggregate.RESPONSE_CONTRACT,
            "base_url": base_url,
            "endpoint": base_url + inference_path,
            "inference_path": inference_path,
            "proxy_policy": "disabled",
            "redirect_policy": "reject",
            "started_at": iso(semantic_started),
            "started_boottime_ns": event_boottime(semantic_started),
            "node_clock": node_clock,
            "ready_wait": {
                "status": "PASS",
                "endpoint": base_url + "/v1/health/ready",
                "started_at": iso(ready_started),
                "started_boottime_ns": event_boottime(ready_started),
                "request_dispatched_boottime_ns": event_boottime(ready_started),
                "response_body_received_boottime_ns": event_boottime(http_ready),
                "finished_at": iso(http_ready),
                "finished_boottime_ns": event_boottime(http_ready),
                "elapsed_seconds": round(
                    (http_ready - ready_started).total_seconds(), 6
                ),
            },
            "cases": [
                {
                    "index": 1,
                    "input_id": f"{run_id}-semantic-a",
                    "status": "PASS",
                    "ok": True,
                    "exit_code": 0,
                    "sequence": "ACDEFGHIKLMNPQRSTVWY",
                    "elapsed_seconds": 1.5,
                    "request_started_at": iso(request_1),
                    "request_dispatched_boottime_ns": event_boottime(request_1),
                    "response_received_at": iso(response_1),
                    "response_body_received_boottime_ns": event_boottime(response_1),
                    "request_sha256": "1" * 64,
                    "response_sha256": "3" * 64,
                    "invariant": {"structure": "valid"},
                },
                {
                    "index": 2,
                    "input_id": f"{run_id}-semantic-b",
                    "status": "PASS",
                    "ok": True,
                    "exit_code": 0,
                    "sequence": "YWVTSRQPNMLKIHGFEDCA",
                    "elapsed_seconds": 0.25,
                    "request_started_at": iso(request_2),
                    "request_dispatched_boottime_ns": event_boottime(request_2),
                    "response_received_at": iso(response_2),
                    "response_body_received_boottime_ns": event_boottime(response_2),
                    "request_sha256": "2" * 64,
                    "response_sha256": "4" * 64,
                    "invariant": {"structure": "valid"},
                },
            ],
            "validation_total_elapsed_seconds": round(
                (validation_finished - semantic_started).total_seconds(), 6
            ),
            "total_elapsed_seconds": round(
                (validation_finished - semantic_started).total_seconds(), 6
            ),
            "validation_finished_at": iso(validation_finished),
            "validation_finished_boottime_ns": event_boottime(
                validation_finished
            ),
            "finished_at": iso(validation_finished),
        }
        write_json(trial / "semantic-summary.json", semantic)

        service_uid = self.uid(index, 4)
        service_name = f"{prefix}-canary-{run_id}"
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": NAMESPACE,
                "uid": service_uid,
            },
            "spec": {
                "type": "ClusterIP",
                "clusterIP": "10.96.0.10",
                "selector": {
                    "app.kubernetes.io/name": container_name,
                    "app.kubernetes.io/component": "restore-target",
                    "archvteams.nebius.ai/run-id": run_id,
                },
                "ports": [
                    {
                        "name": "http",
                        "port": 8000,
                        "targetPort": "http",
                        "protocol": "TCP",
                    }
                ],
            },
        }
        endpoint_slices = {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSliceList",
            "items": [
                {
                    "metadata": {
                        "labels": {"kubernetes.io/service-name": service_name},
                        "ownerReferences": [
                            {"kind": "Service", "uid": service_uid, "controller": True}
                        ],
                    },
                    "endpoints": [
                        {
                            "targetRef": {"uid": target_uid},
                            "conditions": {"ready": True},
                            "addresses": [pod_ip],
                        }
                    ],
                }
            ],
        }
        write_json(trial / "canary-service.json", service)
        write_json(trial / "canary-endpointslices.json", endpoint_slices)

        capture_absence = {
            "schema": "archvteams.nebius.ai/capture-agent-absence/v1",
            "checked_at": iso(t0 - timedelta(seconds=2)),
            "namespace": NAMESPACE,
            "forbidden_name": "archvteams-2407-native-snapshot-agent",
            "daemonset_list": {
                "apiVersion": "apps/v1",
                "kind": "DaemonSetList",
                "items": [],
            },
            "status": "PASS",
        }
        write_json(trial / "capture-agent-absence.json", capture_absence)
        artifact_holder_name = "artifact-holder"
        artifact_holder_uid = "90000000-0000-4000-8000-000000000001"
        artifact_claims = (
            (approved["artifact_pvc"], approved["cache_pvc"])
            if self.model == "openfold2"
            else (approved["artifact_pvc"],)
        )
        write_json(
            trial / "artifact-holder.json",
            holder_receipt(
                artifact_holder_name,
                artifact_holder_uid,
                artifact_claims,
                t0 - timedelta(seconds=3),
            ),
        )
        if self.model == "boltz2":
            write_json(
                trial / "cache-holder.json",
                holder_receipt(
                    "cache-holder",
                    "90000000-0000-4000-8000-000000000002",
                    (approved["cache_pvc"],),
                    t0 - timedelta(seconds=3),
                ),
            )

        controller_base_ns = 50_000_000_000_000 + index * 10_000_000_000
        admission_boundary = {
            "schema": qualification_builder.CONTROLLER_CLOCK_BOUNDARY_SCHEMA,
            "phase": "cohort-admission",
            "utc": iso(admitted),
            "monotonic_ns": controller_base_ns,
        }
        target_submit_clock = {
            "schema": qualification_builder.CONTROLLER_CLOCK_BOUNDARY_SCHEMA,
            "phase": "target-submit",
            "utc": iso(t0),
            "monotonic_ns": controller_base_ns + 100_000_000,
        }
        holder_image = qualification_builder.BOOT_TIME_ANCHOR_HOLDER_IMAGE
        anchor_holder = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": artifact_holder_name,
                "namespace": NAMESPACE,
                "uid": artifact_holder_uid,
            },
            "spec": {
                "nodeName": NODE,
                "containers": [
                    {"name": "holder", "image": holder_image, "resources": {}}
                ],
            },
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "name": "holder",
                        "image": "sha256:" + "1" * 64,
                        "imageID": holder_image,
                        "ready": True,
                        "restartCount": 0,
                        "state": {"running": {"startedAt": iso(demand)}},
                    }
                ],
            },
        }
        anchor_node = {**node_clock, "boottime_ns": node_t0_boottime_ns - 80_000_000}
        boot_time_anchor = {
            "schema": qualification_builder.BOOT_TIME_ANCHOR_SCHEMA,
            "phase": "pre-t0-anchor",
            "sampled_pod_name": artifact_holder_name,
            "sampled_pod_uid": artifact_holder_uid,
            "target_node": NODE,
            "sampled_container": "holder",
            "expected_holder_image": holder_image,
            "controller_before": {
                "utc": iso(admitted + timedelta(milliseconds=20)),
                "monotonic_ns": controller_base_ns + 20_000_000,
            },
            "node_observed": anchor_node,
            "controller_after": {
                "utc": iso(admitted + timedelta(milliseconds=40)),
                "monotonic_ns": controller_base_ns + 40_000_000,
            },
        }
        write_json(trial / "admission-boundary.json", admission_boundary)
        write_json(trial / "target-submit-clock.json", target_submit_clock)
        write_json(trial / "boot-time-anchor.json", boot_time_anchor)
        write_json(trial / "anchor-holder.json", anchor_holder)

        source_paths = {
            "capture_agent_absence": trial / "capture-agent-absence.json",
            "admission_boundary": trial / "admission-boundary.json",
            "target_submit_clock": trial / "target-submit-clock.json",
            "boot_time_anchor": trial / "boot-time-anchor.json",
            "anchor_holder": trial / "anchor-holder.json",
            "target_create_response": trial / "target-create-response.json",
            "target_pod": trial / "target-final.json",
            "target_events": trial / "target-events.json",
            "worker_pod": trial / "worker-pod.json",
            "worker_receipt": trial / "worker-receipt.json",
            "probe_pod": trial / "probe-pod.json",
            "semantic_summary": trial / "semantic-summary.json",
        }
        q = qualification_builder.build_receipt(
            model=self.model,
            run_id=run_id,
            namespace=NAMESPACE,
            target_name=target["metadata"]["name"],
            target_container=container_name,
            expected_image=approved["target_image"],
            target_submit_at=iso(t0),
            target_create_response_at=iso(api_response),
            target_create_response=create_response,
            target=target,
            target_events=events,
            worker_pod=worker_pod,
            worker_receipt=worker_receipt,
            worker_container="restore-worker",
            probe_pod=probe_pod,
            probe_container="semantic-probe",
            semantic_summary=semantic,
            gpu_health_xml=trial / "target-nvidia-smi.xml",
            gpu_health_stderr=trial / "target-nvidia-smi.stderr",
            admission_boundary=admission_boundary,
            target_submit_clock=target_submit_clock,
            boot_time_anchor=boot_time_anchor,
            anchor_holder=anchor_holder,
            source_paths=source_paths,
        )
        write_json(trial / "qualification-receipt.json", q)
        timings = timing_evidence.build_timing_evidence(
            run,
            semantic,
            target,
            target_submit_at=iso(t0),
            target_create_response_at=iso(api_response),
        )
        self.raw_two.append(timings["demand_to_two_semantic_seconds"])
        if self.model == "openfold2":
            summary = openfold2_evidence.build_evidence(
                contract=contract,
                run=run,
                binding=binding,
                target=target,
                service=service,
                endpoint_slices=endpoint_slices,
                worker_job=worker_job,
                worker_pod=worker_pod,
                worker_receipt=worker_receipt,
                probe_job=probe_job,
                probe_pod=probe_pod,
                semantic_summary=semantic,
                target_submit_at=iso(t0),
                target_create_response_at=iso(api_response),
                qualification_receipt=q,
            )
            summary_name = "canary-evidence.json"
        else:
            conservative = q["boot_time_alignment"]["conservative_upper_bounds"]
            summary = {
                "schema": "archvteams.nebius.ai/boltz2-native-trial-summary/v1",
                "status": "PASS",
                "run_id": run_id,
                "worker_receipt": worker_receipt,
                "semantic": semantic,
                "qualification": q,
                "demand_to_http_ready_boottime_upper_seconds": conservative[
                    "http_ready_complete_body"
                ]["upper_bound_seconds"],
                "demand_to_first_semantic_boottime_upper_seconds": conservative[
                    "first_semantic_response_complete_body"
                ]["upper_bound_seconds"],
                "demand_to_two_semantic_boottime_upper_seconds": conservative[
                    "two_semantic_responses_complete_body"
                ]["upper_bound_seconds"],
                **timings,
            }
            summary_name = "trial-summary.json"
        write_json(trial / summary_name, summary)

        target_support = [
            object_receipt("v1", "Service", f"{prefix}-canary-{run_id}", self.uid(index, 4)),
            object_receipt("v1", "Service", f"{prefix}-qualified-{run_id}", self.uid(index, 5)),
            object_receipt(
                "networking.k8s.io/v1", "NetworkPolicy", f"{prefix}-target-{run_id}", self.uid(index, 6)
            ),
            object_receipt(
                "networking.k8s.io/v1", "NetworkPolicy", f"{prefix}-probe-{run_id}", self.uid(index, 7)
            ),
        ]
        worker_created = [
            object_receipt("v1", "ServiceAccount", worker_name, self.uid(index, 8)),
            object_receipt("rbac.authorization.k8s.io/v1", "Role", worker_name, self.uid(index, 9)),
            object_receipt(
                "rbac.authorization.k8s.io/v1", "RoleBinding", worker_name, self.uid(index, 10)
            ),
            object_receipt("batch/v1", "Job", worker_name, worker_uid),
        ]
        probe_created = [
            object_receipt("v1", "ConfigMap", probe_name, self.uid(index, 11)),
            object_receipt("batch/v1", "Job", probe_name, probe_uid),
        ]
        write_json(
            trial / "target-support-create-response.json",
            {"apiVersion": "v1", "kind": "List", "items": target_support},
        )
        write_json(
            trial / "worker-create-response.json",
            {"apiVersion": "v1", "kind": "List", "items": worker_created},
        )
        write_json(
            trial / "probe-create-response.json",
            {"apiVersion": "v1", "kind": "List", "items": probe_created},
        )
        captured = (
            [("target", create_response)]
            + [("target-support", item) for item in target_support]
            + [("restore-worker", item) for item in worker_created]
            + [("semantic-probe", item) for item in probe_created]
        )
        kind_map = {
            "Pod": "pod",
            "Service": "service",
            "NetworkPolicy": "networkpolicy",
            "Job": "job",
            "ServiceAccount": "serviceaccount",
            "Role": "role",
            "RoleBinding": "rolebinding",
            "ConfigMap": "configmap",
        }
        cleanup_resources = [
            {
                "group_role": role,
                "resource_kind": kind_map[item["kind"]],
                "resource_name": item["metadata"]["name"],
                "status": "uid-precondition-deleted",
                "expected_uid": item["metadata"]["uid"],
                "observed_uid_before_delete": item["metadata"]["uid"],
                "create_attempted": True,
                "delete_attempted": True,
                "uid_precondition_enforced": True,
                "delete_transport": "kubectl-authenticated-local-proxy",
                "lookup_exit_code": 0,
                "delete_exit_code": 0,
                "wait_exit_code": 0,
            }
            for role, item in captured
        ]
        cleanup = {
            "schema": "archvteams.nebius.ai/run-cleanup-receipt/v1",
            "run_id": run_id,
            "requested": True,
            "status": "PASS",
            "original_runner_exit_code": 0,
            "started_at": iso(cleanup_started),
            "completed_at": iso(cleanup_finished),
            "resources": cleanup_resources,
        }
        write_json(trial / "cleanup-receipt.json", cleanup)
        write_json(
            trial / "attempt-result.json",
            {
                "schema": "archvteams.nebius.ai/runner-attempt-result/v1",
                "run_id": run_id,
                "model": self.model,
                "admitted": True,
                "completed_at": iso(completed),
                "original_runner_exit_code": 0,
                "cleanup_status": "PASS",
                "final_exit_code": 0,
            },
        )
        self.events.extend(
            [
                {
                    "schema": aggregate.LEDGER_SCHEMA,
                    "event": "admitted",
                    "cohort_id": "fresh-cohort",
                    "model": self.model,
                    "attempt_index": index,
                    "run_id": run_id,
                    "admitted_at": iso(admitted),
                    "trial_dir": str(trial),
                    "runner_sha256": "e" * 64,
                    "instrumentation_contract_sha256": self.instrumentation[
                        "instrumentation_contract_sha256"
                    ],
                },
                {
                    "schema": aggregate.LEDGER_SCHEMA,
                    "event": "completed",
                    "cohort_id": "fresh-cohort",
                    "model": self.model,
                    "attempt_index": index,
                    "run_id": run_id,
                    "completed_at": iso(completed),
                    "trial_dir": str(trial),
                    "summary_path": str(trial / summary_name),
                    "cleanup_receipt_path": str(trial / "cleanup-receipt.json"),
                    "cleanup_status": "PASS",
                    "runner_exit_code": 0,
                },
            ]
        )

    def flush(self) -> None:
        self.ledger.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in self.events),
            encoding="utf-8",
        )


class AggregateFreshCohortTests(unittest.TestCase):
    def make_fixture(
        self,
        model: str = "openfold2",
        count: int = 20,
        coarse_kube_edge: bool = False,
    ) -> CohortFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return CohortFixture(
            Path(temporary.name),
            model=model,
            count=count,
            coarse_kube_edge=coarse_kube_edge,
        )

    def test_nearest_rank_n20_and_boottime_bounded_contract(self) -> None:
        fixture = self.make_fixture()
        result = aggregate.aggregate(fixture.ledger, "openfold2")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["attempt_count"], 20)
        metric = result["metrics"]["demand_to_two_semantic_seconds"]
        ordered = sorted(fixture.raw_two)
        self.assertEqual(metric["p50"]["seconds"], ordered[9])
        self.assertEqual(metric["p95"]["seconds"], ordered[18])
        self.assertEqual(metric["maximum"]["seconds"], ordered[-1])
        upper = result["metrics"][
            "demand_to_two_semantic_boottime_upper_seconds"
        ]
        self.assertGreater(upper["p95"]["seconds"], metric["p95"]["seconds"])
        self.assertIn("demand_to_first_semantic_seconds", result["metrics"])
        self.assertIn("demand_to_http_ready_boottime_upper_seconds", result["metrics"])
        self.assertTrue(
            result["primary_target"]["pass_uses_boottime_conservative_upper_bound"]
        )
        self.assertFalse(result["old_n3_mixed"])

    def test_kubernetes_ready_quantization_edge_is_bounded_not_rejected(self) -> None:
        fixture = self.make_fixture(coarse_kube_edge=True)
        result = aggregate.aggregate(fixture.ledger, "openfold2")
        self.assertEqual(
            result["metrics"]["demand_to_kubernetes_ready_seconds"]["p95"][
                "seconds"
            ],
            0.0,
        )
        self.assertEqual(
            result["metrics"][
                "acceptance_response_proxy_to_kubernetes_ready_seconds"
            ]["p95"]["seconds"],
            0.0,
        )
        self.assertGreater(
            result["metrics"][
                "demand_to_http_ready_boottime_upper_seconds"
            ]["p95"]["seconds"],
            0.0,
        )

    def test_boltz2_binding_and_same_aggregate_contract(self) -> None:
        fixture = self.make_fixture("boltz2")
        result = aggregate.aggregate(fixture.ledger, "boltz2")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["immutable_contract"]["checkpoint_id"], "boltz2-native-f7-v1"
        )
        slices_path = fixture.trials[-1] / "canary-endpointslices.json"
        slices = json.loads(slices_path.read_text())
        slices["items"][0]["endpoints"][0]["targetRef"]["uid"] = "foreign"
        write_json(slices_path, slices)
        with self.assertRaisesRegex(aggregate.AggregateError, "bound target UID"):
            aggregate.aggregate(fixture.ledger, "boltz2")

    def test_counted_failure_is_retained_and_fails_qualification(self) -> None:
        fixture = self.make_fixture()
        trial = fixture.trials[-1]
        completed = next(
            event
            for event in fixture.events
            if event.get("event") == "completed" and event.get("attempt_index") == 20
        )
        completed["runner_exit_code"] = 1
        attempt = json.loads((trial / "attempt-result.json").read_text())
        attempt["original_runner_exit_code"] = 1
        attempt["final_exit_code"] = 1
        write_json(trial / "attempt-result.json", attempt)
        cleanup = json.loads((trial / "cleanup-receipt.json").read_text())
        cleanup["original_runner_exit_code"] = 1
        cleanup["resources"] = [
            item
            for item in cleanup["resources"]
            if item.get("group_role") != "semantic-probe"
        ]
        cleanup["resources"].append(
            {
                "group_role": "semantic-probe",
                "resource_kind": "group",
                "resource_name": "semantic-probe",
                "status": "not-created",
                "expected_uid": "",
                "observed_uid_before_delete": "",
                "create_attempted": False,
                "delete_attempted": False,
                "uid_precondition_enforced": False,
                "lookup_exit_code": 0,
                "delete_exit_code": 0,
                "wait_exit_code": 0,
            }
        )
        write_json(trial / "cleanup-receipt.json", cleanup)
        (trial / "probe-create-response.json").unlink()
        fixture.flush()
        result = aggregate.aggregate(fixture.ledger, "openfold2")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["failure_rate"], 0.05)
        self.assertIsNone(
            result["metrics"]["demand_to_two_semantic_seconds"]["maximum"]["seconds"]
        )

    def test_rejects_source_cleanup_and_holder_tampering(self) -> None:
        mutations = ("source", "cleanup-uid", "holder-mount", "of2-endpoint")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture = self.make_fixture()
                trial = fixture.trials[-1]
                if mutation == "source":
                    events = json.loads((trial / "target-events.json").read_text())
                    events["items"][0]["message"] = "tampered"
                    write_json(trial / "target-events.json", events)
                elif mutation == "cleanup-uid":
                    cleanup = json.loads((trial / "cleanup-receipt.json").read_text())
                    cleanup["resources"][1]["expected_uid"] = cleanup["resources"][0][
                        "expected_uid"
                    ]
                    cleanup["resources"][1]["observed_uid_before_delete"] = cleanup[
                        "resources"
                    ][0]["expected_uid"]
                    write_json(trial / "cleanup-receipt.json", cleanup)
                elif mutation == "holder-mount":
                    holder = json.loads((trial / "artifact-holder.json").read_text())
                    holder["mount_verifications"].pop()
                    write_json(trial / "artifact-holder.json", holder)
                else:
                    slices = json.loads(
                        (trial / "canary-endpointslices.json").read_text()
                    )
                    slices["items"][0]["endpoints"][0]["targetRef"][
                        "uid"
                    ] = "foreign"
                    write_json(trial / "canary-endpointslices.json", slices)
                with self.assertRaises(aggregate.AggregateError):
                    aggregate.aggregate(fixture.ledger, "openfold2")

    def test_rejects_nonserial_or_external_trials_and_n_below_twenty(self) -> None:
        fixture = self.make_fixture()
        fixture.events[2], fixture.events[3] = fixture.events[3], fixture.events[2]
        fixture.flush()
        with self.assertRaisesRegex(aggregate.AggregateError, "serial"):
            aggregate.aggregate(fixture.ledger, "openfold2")

        short = self.make_fixture(count=19)
        with self.assertRaisesRegex(aggregate.AggregateError, "n>=20"):
            aggregate.aggregate(short.ledger, "openfold2")

        missing_outcomes = self.make_fixture()
        missing_outcomes.events[-1]["scheduled_attempt_count"] = 30
        missing_outcomes.flush()
        with self.assertRaisesRegex(aggregate.AggregateError, "scheduled"):
            aggregate.aggregate(missing_outcomes.ledger, "openfold2")

    def test_rejects_mixed_v2_and_instrumentation_contract_drift(self) -> None:
        mixed = self.make_fixture()
        receipt_path = mixed.trials[-1] / "qualification-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["schema"] = "archvteams.nebius.ai/warm-instance-qualification/v2"
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(aggregate.AggregateError, "qualification"):
            aggregate.aggregate(mixed.ledger, "openfold2")

        admission_drift = self.make_fixture()
        admission = next(
            event
            for event in reversed(admission_drift.events)
            if event.get("event") == "admitted"
        )
        admission["instrumentation_contract_sha256"] = "0" * 64
        admission_drift.flush()
        with self.assertRaisesRegex(aggregate.AggregateError, "admission"):
            aggregate.aggregate(admission_drift.ledger, "openfold2")

        receipt_drift = self.make_fixture()
        contract_path = receipt_drift.cohort / "instrumentation-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["sources"][0]["sha256"] = "0" * 64
        write_json(contract_path, contract)
        with self.assertRaisesRegex(aggregate.AggregateError, "instrumentation"):
            aggregate.aggregate(receipt_drift.ledger, "openfold2")


if __name__ == "__main__":
    unittest.main()
