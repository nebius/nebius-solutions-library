#!/usr/bin/env python3
"""Render the UID-bound OpenFold3 PodSnapshotContent from captured Pod JSON."""

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
NAMESPACE = "nim-fast-start"
NODE = "gpu-node-a.example.invalid"
DONOR_PREFIX = "openfold3-native-f7-donor-r3-"
IMAGE = (
    "nvcr.io/nim/openfold/openfold3@sha256:"
    "6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2"
)
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class CaptureError(ValueError):
    """The captured Pod is not the exact ready OpenFold3 donor."""


def _read_pod(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CaptureError("donor Pod JSON must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"cannot read donor Pod JSON: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise CaptureError("donor Pod JSON must be an object")
    return value


def donor_identity(pod: dict[str, Any]) -> tuple[str, str]:
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
    raw_uid = metadata.get("uid")
    if not isinstance(raw_uid, str):
        raise CaptureError("donor Pod UID is absent")
    try:
        parsed_uid = uuid.UUID(raw_uid)
    except ValueError as exc:
        raise CaptureError("donor Pod UID is not a UUID") from exc
    if str(parsed_uid) != raw_uid:
        raise CaptureError("donor Pod UID is not canonical lowercase UUID text")
    if metadata.get("namespace") != NAMESPACE or spec.get("nodeName") != NODE:
        raise CaptureError("donor Pod is not bound to the exact namespace and H100 node")
    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise CaptureError("donor Pod labels or annotations are malformed")
    if (
        labels.get("app.kubernetes.io/name") != "openfold3"
        or labels.get("app.kubernetes.io/component") != "checkpoint-donor"
        or labels.get("nvidia.com/snapshot-is-checkpoint-source") != "true"
        or labels.get("nvidia.com/snapshot-checkpoint-id") != "openfold3-native-f7-v1"
        or annotations.get("nvidia.com/snapshot-artifact-version") != "1"
        or annotations.get("nvidia.com/snapshot-target-containers") != "openfold3"
        or annotations.get("nvidia.com/snapshot-storage-type") != "pvc"
        or annotations.get("nvidia.com/snapshot-storage-base-path") != "/checkpoints"
    ):
        raise CaptureError("donor Pod metadata does not match the native-f7 capture contract")
    containers = spec.get("containers")
    if (
        not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != "openfold3"
        or containers[0].get("image") != IMAGE
    ):
        raise CaptureError("donor Pod does not contain the exact pinned OpenFold3 container")
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
        or statuses[0].get("name") != "openfold3"
        or not isinstance(statuses[0].get("state", {}).get("running"), dict)
        or statuses[0].get("imageID", "").removeprefix("docker-pullable://") != IMAGE
    ):
        raise CaptureError("donor container is not the running pinned image")
    return name, raw_uid


def render(pod: dict[str, Any]) -> list[dict[str, Any]]:
    name, uid = donor_identity(pod)
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
    args = parser.parse_args(argv)
    try:
        values = render(_read_pod(args.donor_pod_json))
    except CaptureError as exc:
        print(f"render-capture refused input: {exc}", file=sys.stderr)
        return 2
    yaml.safe_dump_all(values, sys.stdout, explicit_start=True, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
