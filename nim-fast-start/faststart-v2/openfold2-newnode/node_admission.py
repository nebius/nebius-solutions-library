#!/usr/bin/env python3
"""Build and verify the exact new-node admission receipt used by the benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "archvteams.nebius.ai/openfold2-newnode-admission/v1"
CLUSTER_ID = os.environ.get("OPENFOLD2_CLUSTER_ID", "mk8scluster-example")
PROJECT_ID = os.environ.get("OPENFOLD2_PROJECT_ID", "project-example")
NODE_GROUP_ID = os.environ.get("OPENFOLD2_NODE_GROUP_ID", "mk8snodegroup-example")
NODE_NAME = re.compile(r"^computeinstance-[a-z0-9]+$")
EXPECTED_LABELS = {
    "nebius.com/node-group-id": NODE_GROUP_ID,
    "nebius.com/preemptible": "true",
    "nebius.com/gpu-name": "H100",
    "nebius.com/resource-preset": "1gpu-16vcpu-200gb",
    "nebius.com/nvidia_driver_version": "580.159.04-1ubuntu1",
    "nebius.com/cuda_version": "13.0.3-1",
}
EXPECTED_NODE_INFO = {
    "architecture": "amd64",
    "container_runtime": "containerd://1.7.34",
    "kernel": "6.11.0-1016-nvidia",
    "os_image": "Ubuntu 24.04.4 LTS",
}
REVIEWED_TRANSIENT_STARTUP_TAINTS = frozenset(
    {
        ("node.cilium.io/agent-not-ready", "NoExecute"),
        ("node.kubernetes.io/not-ready", "NoExecute"),
    }
)


class AdmissionError(ValueError):
    """The captured node is not the exact fresh compatible target."""


def _load(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise AdmissionError(f"{label} must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise AdmissionError(f"{label} must be a JSON object")
    return value, raw


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdmissionError(f"{label} must be an RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise AdmissionError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise AdmissionError(f"{label} must include a timezone")
    return value


def _identity(node: dict[str, Any], label: str) -> tuple[str, str]:
    metadata = node.get("metadata")
    if not isinstance(metadata, dict):
        raise AdmissionError(f"{label} metadata is missing")
    name = metadata.get("name")
    uid = metadata.get("uid")
    if not isinstance(name, str) or NODE_NAME.fullmatch(name) is None:
        raise AdmissionError(f"{label} name is not an exact compute instance")
    try:
        parsed_uid = uuid.UUID(str(uid))
    except ValueError as exc:
        raise AdmissionError(f"{label} UID is not a UUID") from exc
    if str(parsed_uid) != uid:
        raise AdmissionError(f"{label} UID is not canonical")
    return name, uid


def classify_startup_taints(node: dict[str, Any]) -> dict[str, Any]:
    """Classify only the two reviewed, transient new-node startup taints.

    This deliberately does not relax ``build``: the final admission receipt is
    still issued only for an entirely untainted node.  Any key/effect outside
    the exact reviewed set is terminal rather than something callers may poll.
    """
    if node.get("apiVersion") != "v1" or node.get("kind") != "Node":
        raise AdmissionError("startup node capture is not a core/v1 Node")
    spec = node.get("spec")
    if not isinstance(spec, dict):
        raise AdmissionError("startup node spec is missing")
    taints = spec.get("taints")
    if taints is None:
        taints = []
    if not isinstance(taints, list):
        raise AdmissionError("startup node taints are not a list")

    observed: list[dict[str, Any]] = []
    pairs: set[tuple[str, str]] = set()
    for taint in taints:
        if not isinstance(taint, dict):
            raise AdmissionError("startup node contains a malformed taint")
        key = taint.get("key")
        effect = taint.get("effect")
        pair = (key, effect)
        if not isinstance(key, str) or not isinstance(effect, str):
            raise AdmissionError("startup node contains a malformed taint")
        if pair not in REVIEWED_TRANSIENT_STARTUP_TAINTS:
            raise AdmissionError(
                f"startup node has unknown or permanent taint: {key}:{effect}"
            )
        if pair in pairs:
            raise AdmissionError(f"startup node repeats transient taint: {key}:{effect}")
        pairs.add(pair)
        normalized = {"key": key, "effect": effect}
        if "timeAdded" in taint:
            normalized["timeAdded"] = _timestamp(
                taint.get("timeAdded"), f"startup taint {key} timeAdded"
            )
        if taint.get("value") not in (None, ""):
            raise AdmissionError(f"startup node transient taint has a value: {key}")
        observed.append(normalized)

    return {
        "status": "clear" if not observed else "wait",
        "taints": sorted(observed, key=lambda item: (item["key"], item["effect"])),
    }


def classify_startup_state(node: dict[str, Any]) -> dict[str, Any]:
    """Classify bounded startup conditions without weakening final admission."""
    taint_state = classify_startup_taints(node)
    status = node.get("status")
    if not isinstance(status, dict):
        raise AdmissionError("startup node status is missing")
    allocatable = status.get("allocatable")
    gpu_present = isinstance(allocatable, dict) and "nvidia.com/gpu" in allocatable
    gpu = allocatable.get("nvidia.com/gpu") if gpu_present else None
    if gpu_present and gpu != "1":
        raise AdmissionError(f"startup node has non-1 allocatable GPU: {gpu!r}")

    wait_reasons: list[str] = []
    if taint_state["status"] == "wait":
        wait_reasons.append("reviewed-transient-taints")
    if not gpu_present:
        wait_reasons.append("gpu-allocatable-absent")
    return {
        "status": "wait" if wait_reasons else "clear",
        "wait_reasons": wait_reasons,
        "taints": taint_state["taints"],
        "gpu_allocatable": gpu,
    }


def build(node: dict[str, Any], node_raw: bytes, previous: dict[str, Any], collected_at: str) -> dict[str, Any]:
    if node.get("apiVersion") != "v1" or node.get("kind") != "Node":
        raise AdmissionError("new node capture is not a core/v1 Node")
    if previous.get("apiVersion") != "v1" or previous.get("kind") != "Node":
        raise AdmissionError("previous node capture is not a core/v1 Node")
    name, uid = _identity(node, "new node")
    previous_name, previous_uid = _identity(previous, "previous node")
    if name == previous_name or uid == previous_uid:
        raise AdmissionError("scaled node did not receive a new name and UID")

    metadata = node["metadata"]
    if metadata.get("deletionTimestamp") is not None:
        raise AdmissionError("new node is already deleting")
    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        raise AdmissionError("new node labels are missing")
    admitted_labels = {key: labels.get(key) for key in EXPECTED_LABELS}
    if admitted_labels != EXPECTED_LABELS:
        raise AdmissionError(f"new node compatibility labels differ: {admitted_labels!r}")

    spec = node.get("spec")
    if not isinstance(spec, dict):
        raise AdmissionError("new node spec is missing")
    if spec.get("unschedulable") is True:
        raise AdmissionError("new node is unschedulable")
    if spec.get("taints") not in (None, []):
        raise AdmissionError("new node has unexpected taints")
    provider_id = spec.get("providerID")
    if provider_id != f"nebius://{name}":
        raise AdmissionError("new node provider ID does not match its exact name")

    status = node.get("status")
    if not isinstance(status, dict):
        raise AdmissionError("new node status is missing")
    ready = [
        item
        for item in status.get("conditions", [])
        if isinstance(item, dict) and item.get("type") == "Ready" and item.get("status") == "True"
    ]
    if len(ready) != 1:
        raise AdmissionError("new node does not have exactly one Ready=True condition")
    ready_at = _timestamp(ready[0].get("lastTransitionTime"), "new node Ready transition")
    allocatable = status.get("allocatable")
    if not isinstance(allocatable, dict) or allocatable.get("nvidia.com/gpu") != "1":
        raise AdmissionError("new node does not expose exactly one allocatable GPU")
    node_info = status.get("nodeInfo")
    if not isinstance(node_info, dict):
        raise AdmissionError("new node runtime information is missing")
    admitted_info = {
        "architecture": node_info.get("architecture"),
        "container_runtime": node_info.get("containerRuntimeVersion"),
        "kernel": node_info.get("kernelVersion"),
        "os_image": node_info.get("osImage"),
    }
    if admitted_info != EXPECTED_NODE_INFO:
        raise AdmissionError(f"new node runtime differs from donor: {admitted_info!r}")

    return {
        "schema": SCHEMA,
        "collected_at": _timestamp(collected_at, "collected_at"),
        "cluster_id": CLUSTER_ID,
        "project_id": PROJECT_ID,
        "node_group_id": NODE_GROUP_ID,
        "previous_node": {"name": previous_name, "uid": previous_uid},
        "node": {
            "name": name,
            "uid": uid,
            "provider_id": provider_id,
            "labels": admitted_labels,
            "ready_transition_at": ready_at,
            "allocatable_gpu": "1",
            "taints": [],
            "unschedulable": False,
            **admitted_info,
        },
        "node_json_sha256": hashlib.sha256(node_raw).hexdigest(),
    }


def validate_admission(path: Path, node_json: Path) -> dict[str, Any]:
    receipt, _ = _load(path, "node admission")
    raw_node, raw = _load(node_json, "new node JSON")
    exact = {
        "schema",
        "collected_at",
        "cluster_id",
        "project_id",
        "node_group_id",
        "previous_node",
        "node",
        "node_json_sha256",
    }
    if set(receipt) != exact:
        raise AdmissionError("node admission fields do not match the v1 schema")
    if receipt.get("schema") != SCHEMA:
        raise AdmissionError("node admission schema is unsupported")
    if receipt.get("cluster_id") != CLUSTER_ID or receipt.get("project_id") != PROJECT_ID:
        raise AdmissionError("node admission is not bound to the allowed cluster project")
    if receipt.get("node_group_id") != NODE_GROUP_ID:
        raise AdmissionError("node admission is not bound to the allowed preemptible group")
    if receipt.get("node_json_sha256") != hashlib.sha256(raw).hexdigest():
        raise AdmissionError("raw new-node JSON digest does not match admission")
    previous = receipt.get("previous_node")
    if not isinstance(previous, dict) or set(previous) != {"name", "uid"}:
        raise AdmissionError("previous node identity is malformed")
    synthetic_previous = {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": previous,
    }
    rebuilt = build(raw_node, raw, synthetic_previous, receipt.get("collected_at"))
    if rebuilt != receipt:
        raise AdmissionError("node admission does not match the raw new-node object")
    return receipt


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise AdmissionError(f"output already exists: {path}")
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(data)
        handle.flush()
        os.fsync(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    builder = subparsers.add_parser("build")
    builder.add_argument("--node-json", type=Path, required=True)
    builder.add_argument("--previous-node-json", type=Path, required=True)
    builder.add_argument("--collected-at", required=True)
    builder.add_argument("--output", type=Path, required=True)
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("--node-json", type=Path, required=True)
    verifier.add_argument("--admission", type=Path, required=True)
    startup = subparsers.add_parser("startup-state")
    startup.add_argument("--node-json", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "build":
            node, raw = _load(args.node_json, "new node JSON")
            previous, _ = _load(args.previous_node_json, "previous node JSON")
            receipt = build(node, raw, previous, args.collected_at)
            _write_exclusive(args.output, receipt)
        elif args.mode == "verify":
            receipt = validate_admission(args.admission, args.node_json)
        else:
            node, _ = _load(args.node_json, "startup node JSON")
            result = classify_startup_state(node)
    except AdmissionError as exc:
        parser.error(str(exc))
    if args.mode == "startup-state":
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    print(json.dumps({"status": "admitted", "node": receipt["node"]["name"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
