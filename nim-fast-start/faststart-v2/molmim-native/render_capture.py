#!/usr/bin/env python3
"""Render the UID-bound MolMIM PodSnapshotContent from captured Pod JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "podsnapshotcontent.yaml.tmpl"
DONOR_JOB = ROOT / "donor-job.yaml"
NAMESPACE = "nim-fast-start"
NODE = "gpu-node-b.example.invalid"
DONOR_JOB_NAME = "molmim-native-f7-donor-r1"
DONOR_PREFIX = "molmim-native-f7-donor-r1-"
IMAGE = (
    "nvcr.io/nim/nvidia/molmim@sha256:"
    "7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
)
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class CaptureError(ValueError):
    """The captured Pod is not the exact ready MolMIM donor."""


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CaptureError(f"{label} JSON must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"cannot read {label} JSON: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise CaptureError(f"{label} JSON must be an object")
    return value


def _expected_donor_job() -> dict[str, Any]:
    try:
        value = yaml.safe_load(DONOR_JOB.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CaptureError(f"cannot read pinned donor Job: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise CaptureError("pinned donor Job is malformed")
    return value


def _require_expected(actual: Any, expected: Any, label: str) -> None:
    """Require the rendered subtree while allowing Kubernetes-added fields."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise CaptureError(f"{label} is not an object")
        for key, expected_value in expected.items():
            if key not in actual:
                raise CaptureError(f"{label}.{key} is absent")
            _require_expected(actual[key], expected_value, f"{label}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise CaptureError(f"{label} list does not match the pinned donor manifest")
        for index, (actual_value, expected_value) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _require_expected(actual_value, expected_value, f"{label}[{index}]")
        return
    if actual != expected:
        raise CaptureError(f"{label} does not match the pinned donor manifest")


def _uid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CaptureError(f"{label} UID is absent")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise CaptureError(f"{label} UID is not a UUID") from exc
    if str(parsed) != value:
        raise CaptureError(f"{label} UID is not canonical lowercase UUID text")
    return value


def donor_identity(pod: dict[str, Any], donor_job: dict[str, Any]) -> tuple[str, str]:
    expected_job = _expected_donor_job()
    _require_expected(donor_job, expected_job, "donor Job")
    job_metadata = donor_job.get("metadata")
    if not isinstance(job_metadata, dict):
        raise CaptureError("donor Job metadata is malformed")
    job_uid = _uid(job_metadata.get("uid"), "donor Job")

    if pod.get("apiVersion") != "v1" or pod.get("kind") != "Pod":
        raise CaptureError("capture source is not a core/v1 Pod")
    metadata = pod.get("metadata")
    spec = pod.get("spec")
    status = pod.get("status")
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        raise CaptureError("donor Pod metadata, spec, or status is malformed")
    name = metadata.get("name")
    if (
        not isinstance(name, str)
        or not name.startswith(DONOR_PREFIX)
        or len(name) > 63
        or not DNS_LABEL.fullmatch(name)
    ):
        raise CaptureError("donor Pod name is not owned by the pinned donor Job")
    raw_uid = _uid(metadata.get("uid"), "donor Pod")
    if metadata.get("namespace") != NAMESPACE or spec.get("nodeName") != NODE:
        raise CaptureError("donor Pod is not bound to the exact namespace and H100 node")
    owners = metadata.get("ownerReferences")
    matching_owners = (
        [owner for owner in owners if isinstance(owner, dict)]
        if isinstance(owners, list)
        else []
    )
    expected_owner = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "name": DONOR_JOB_NAME,
        "uid": job_uid,
        "controller": True,
    }
    if len(matching_owners) != 1 or any(
        matching_owners[0].get(key) != value
        for key, value in expected_owner.items()
    ):
        raise CaptureError("donor Pod is not controlled by the exact captured donor Job")
    template = expected_job["spec"]["template"]
    _require_expected(
        metadata.get("labels"), template["metadata"]["labels"], "donor Pod labels"
    )
    _require_expected(
        metadata.get("annotations"),
        template["metadata"]["annotations"],
        "donor Pod annotations",
    )
    _require_expected(spec, template["spec"], "donor PodSpec")
    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise CaptureError("donor Pod labels or annotations are malformed")
    if (
        labels.get("app.kubernetes.io/name") != "molmim"
        or labels.get("app.kubernetes.io/component") != "checkpoint-donor"
        or labels.get("nvidia.com/snapshot-is-checkpoint-source") != "true"
        or labels.get("nvidia.com/snapshot-checkpoint-id") != "molmim-native-f7-v1"
        or annotations.get("nvidia.com/snapshot-artifact-version") != "1"
        or annotations.get("nvidia.com/snapshot-target-containers") != "molmim"
        or annotations.get("nvidia.com/snapshot-storage-type") != "pvc"
        or annotations.get("nvidia.com/snapshot-storage-base-path") != "/checkpoints"
    ):
        raise CaptureError("donor Pod metadata does not match the native-f7 capture contract")
    containers = spec.get("containers")
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != "molmim"
        or containers[0].get("image") != IMAGE
    ):
        raise CaptureError("donor Pod does not contain the exact pinned MolMIM container")
    if status.get("phase") != "Running" or not any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
        for item in status.get("conditions", [])
    ):
        raise CaptureError("donor Pod has not completed both semantic warmups")
    statuses = status.get("containerStatuses")
    if (
        not isinstance(statuses, list)
        or len(statuses) != 1
        or statuses[0].get("name") != "molmim"
        or not isinstance(statuses[0].get("state", {}).get("running"), dict)
        or statuses[0].get("imageID", "").removeprefix("docker-pullable://") != IMAGE
    ):
        raise CaptureError("donor container is not the running pinned image")
    return name, raw_uid


def render(pod: dict[str, Any], donor_job: dict[str, Any]) -> list[dict[str, Any]]:
    name, uid = donor_identity(pod, donor_job)
    try:
        source = TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaptureError(f"cannot read PodSnapshotContent template: {exc}") from exc
    source = source.replace("@@SOURCE_POD_NAME@@", name).replace("@@SOURCE_POD_UID@@", uid)
    if "@@" in source:
        raise CaptureError("PodSnapshotContent template has an unresolved placeholder")
    try:
        values = list(yaml.safe_load_all(source))
    except yaml.YAMLError as exc:
        raise CaptureError(f"rendered PodSnapshotContent is invalid YAML: {exc}") from exc
    if len(values) != 1 or not isinstance(values[0], dict):
        raise CaptureError("rendered PodSnapshotContent must contain exactly one object")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--donor-pod-json", required=True, type=Path)
    parser.add_argument("--donor-job-json", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        values = render(
            _read_object(args.donor_pod_json, "donor Pod"),
            _read_object(args.donor_job_json, "donor Job"),
        )
    except CaptureError as exc:
        print(f"render-capture refused input: {exc}", file=sys.stderr)
        return 2
    yaml.safe_dump_all(values, sys.stdout, explicit_start=True, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
