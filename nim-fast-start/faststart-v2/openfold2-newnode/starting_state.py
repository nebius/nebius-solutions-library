#!/usr/bin/env python3
"""Classify the exact one-node OpenFold2 benchmark starting state."""

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


SCHEMA = "archvteams.nebius.ai/openfold2-starting-state/v1"
CLUSTER_ID = os.environ.get("OPENFOLD2_CLUSTER_ID", "mk8scluster-example")
NODE_GROUP_ID = os.environ.get("OPENFOLD2_NODE_GROUP_ID", "mk8snodegroup-example")
NODE_NAME = re.compile(r"^computeinstance-[a-z0-9]+$")
EXPECTED_NODE_LABELS = {
    "nebius.com/node-group-id": NODE_GROUP_ID,
    "nebius.com/preemptible": "true",
    "nebius.com/gpu-name": "H100",
    "nebius.com/resource-preset": "1gpu-16vcpu-200gb",
    "nebius.com/nvidia_driver_version": "580.159.04-1ubuntu1",
    "nebius.com/cuda_version": "13.0.3-1",
}
UNREACHABLE = "node.kubernetes.io/unreachable"
SHUTDOWN = "node.cloudprovider.kubernetes.io/shutdown"


class StartingStateError(ValueError):
    """The node group is not in one of the two exact admitted starting states."""


def _load(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise StartingStateError(f"{label} must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StartingStateError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise StartingStateError(f"{label} must be a JSON object")
    return value, raw


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise StartingStateError("collected_at must be an RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise StartingStateError("collected_at must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise StartingStateError("collected_at must include a timezone")
    return value


def _integer(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StartingStateError(f"{label} is not an integer") from exc


def classify(
    group: dict[str, Any],
    group_raw: bytes,
    nodes: dict[str, Any],
    nodes_raw: bytes,
    collected_at: str,
) -> dict[str, Any]:
    try:
        group_id = group["metadata"]["id"]
        parent_id = group["metadata"]["parent_id"]
        resource_version = group["metadata"]["resource_version"]
        fixed = _integer(group["spec"]["fixed_node_count"], "fixed count")
        target = _integer(group["status"].get("target_node_count", 0), "target count")
        count = _integer(group["status"].get("node_count", 0), "node count")
        ready_count = _integer(group["status"].get("ready_node_count", 0), "ready count")
        template = group["spec"]["template"]
    except (KeyError, TypeError) as exc:
        raise StartingStateError("node-group evidence is malformed") from exc
    if group_id != NODE_GROUP_ID or parent_id != CLUSTER_ID:
        raise StartingStateError("node group is not the exact allowed group")
    if not isinstance(resource_version, str) or not resource_version:
        raise StartingStateError("node-group resource version is missing")
    if (fixed, target, count) != (1, 1, 1) or ready_count not in (0, 1):
        raise StartingStateError("cloud counts are not exactly 1/1/1 with ready 0 or 1")
    try:
        exact_template = (
            template["resources"]["platform"] == "gpu-h100-sxm"
            and template["resources"]["preset"] == "1gpu-16vcpu-200gb"
            and template["gpu_settings"]["drivers_preset"] == "cuda13.0"
            and isinstance(template["preemptible"], dict)
        )
    except (KeyError, TypeError):
        exact_template = False
    if not exact_template:
        raise StartingStateError("node-group template is not the exact preemptible H100 template")

    items = nodes.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise StartingStateError("Kubernetes does not contain exactly one group Node")
    node = items[0]
    if node.get("apiVersion") != "v1" or node.get("kind") != "Node":
        raise StartingStateError("captured group member is not a core/v1 Node")
    metadata = node.get("metadata")
    spec = node.get("spec")
    status = node.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        raise StartingStateError("group Node is malformed")
    name = metadata.get("name")
    uid = metadata.get("uid")
    if not isinstance(name, str) or NODE_NAME.fullmatch(name) is None:
        raise StartingStateError("group Node name is not an exact compute instance")
    try:
        parsed_uid = uuid.UUID(str(uid))
    except ValueError as exc:
        raise StartingStateError("group Node UID is not a UUID") from exc
    if str(parsed_uid) != uid:
        raise StartingStateError("group Node UID is not canonical")
    if metadata.get("deletionTimestamp") is not None:
        raise StartingStateError("group Node is already deleting")
    labels = metadata.get("labels")
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in EXPECTED_NODE_LABELS.items()):
        raise StartingStateError("group Node compatibility labels differ")
    if spec.get("providerID") != f"nebius://{name}":
        raise StartingStateError("group Node provider ID differs from its name")

    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        raise StartingStateError("group Node conditions are missing")
    ready = [item for item in conditions if isinstance(item, dict) and item.get("type") == "Ready"]
    if len(ready) != 1:
        raise StartingStateError("group Node does not have exactly one Ready condition")
    ready_status = ready[0].get("status")
    taints = spec.get("taints") or []
    if not isinstance(taints, list) or any(not isinstance(item, dict) for item in taints):
        raise StartingStateError("group Node taints are malformed")
    taint_pairs = {(item.get("key"), item.get("effect")) for item in taints}

    if ready_count == 1 and ready_status == "True" and not taints:
        mode = "healthy"
    elif (
        ready_count == 0
        and ready_status == "Unknown"
        and (UNREACHABLE, "NoSchedule") in taint_pairs
        and (UNREACHABLE, "NoExecute") in taint_pairs
        and (SHUTDOWN, "NoSchedule") in taint_pairs
    ):
        mode = "retiring-unknown"
    else:
        raise StartingStateError("cloud readiness, Node readiness, and taints do not form an admitted state")

    return {
        "schema": SCHEMA,
        "collected_at": _timestamp(collected_at),
        "mode": mode,
        "cluster_id": CLUSTER_ID,
        "node_group": {
            "id": NODE_GROUP_ID,
            "resource_version": resource_version,
            "fixed": fixed,
            "target": target,
            "nodes": count,
            "ready": ready_count,
        },
        "node": {
            "name": name,
            "uid": uid,
            "ready": ready_status,
            "taints": taints,
        },
        "node_group_json_sha256": hashlib.sha256(group_raw).hexdigest(),
        "nodes_json_sha256": hashlib.sha256(nodes_raw).hexdigest(),
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise StartingStateError(f"output already exists: {path}")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(payload)
        handle.flush()
        os.fsync(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-group-json", type=Path, required=True)
    parser.add_argument("--nodes-json", type=Path, required=True)
    parser.add_argument("--collected-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        group, group_raw = _load(args.node_group_json, "node-group JSON")
        nodes, nodes_raw = _load(args.nodes_json, "group Nodes JSON")
        result = classify(group, group_raw, nodes, nodes_raw, args.collected_at)
        _write(args.output, result)
    except StartingStateError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "admitted", "mode": result["mode"], "node": result["node"]["name"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
