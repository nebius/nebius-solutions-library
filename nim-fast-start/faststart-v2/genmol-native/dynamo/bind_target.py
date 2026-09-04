#!/usr/bin/env python3
"""Bind one scheduled GenMol target to the one-shot restore worker.

This helper is offline: it consumes previously captured Kubernetes Pod JSON,
computes the worker's canonical live PodSpec hash and the allowed cluster's
systemd/containerd cgroup path, and writes a binding receipt plus an RFC 6902
metadata patch. It never calls Kubernetes itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import render
except ImportError:  # pragma: no cover - package import path
    from . import render


POD_SPEC_HASH_KEY = "archvteams.nebius.ai/target-pod-spec-sha256"


class BindingError(ValueError):
    """Captured inputs do not describe the exact scheduled target."""


def _load_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise BindingError(f"{label} must be a regular non-symlink file")
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BindingError(f"cannot read {label} JSON: {type(exc).__name__}") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BindingError(f"{label} must be a JSON object")
    return value


def canonical_pod_spec(spec: Any) -> bytes:
    """Match the worker's sorted, compact, unstructured PodSpec encoding."""
    if not isinstance(spec, dict):
        raise BindingError("live Pod spec must be a JSON object")
    try:
        return json.dumps(
            spec,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BindingError("live Pod spec is not canonicalizable JSON") from exc


def pod_spec_sha256(spec: Any) -> str:
    return hashlib.sha256(canonical_pod_spec(spec)).hexdigest()


def _burstable_systemd_cgroup(pod_uid: str, container_id: str) -> str:
    """Derive the allowed cluster's cgroup-v2/systemd/containerd path.

    The worker resolves and compares the live runtime cgroup before restore, so
    a topology change fails before CRIU is invoked.
    """
    uid_component = pod_uid.replace("-", "_")
    container_hex = container_id.removeprefix("containerd://")
    return (
        "/kubepods.slice/kubepods-burstable.slice/"
        f"kubepods-burstable-pod{uid_component}.slice/"
        f"cri-containerd-{container_hex}.scope"
    )


def _condition(pod: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    conditions = pod.get("status", {}).get("conditions", [])
    if not isinstance(conditions, list):
        return None
    matches = [
        condition
        for condition in conditions
        if isinstance(condition, dict) and condition.get("type") == condition_type
    ]
    return matches[0] if len(matches) == 1 else None


def build_binding(
    pod_value: Any,
    run: dict[str, Any],
    contract: dict[str, Any],
    collected_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pod = _mapping(pod_value, "live Pod")
    try:
        render._timestamp(collected_at, "binding collected_at")
    except render.RenderError as exc:
        raise BindingError(str(exc)) from exc
    if pod.get("apiVersion") != "v1" or pod.get("kind") != "Pod":
        raise BindingError("live object is not a v1 Pod")
    metadata = _mapping(pod.get("metadata"), "live Pod metadata")
    spec = _mapping(pod.get("spec"), "live Pod spec")
    status = _mapping(pod.get("status"), "live Pod status")
    expected_name = render._target_name(run["run_id"])
    pod_uid = metadata.get("uid")
    try:
        parsed_uid = uuid.UUID(str(pod_uid))
    except ValueError as exc:
        raise BindingError("live Pod UID must be a canonical UUID") from exc
    if str(parsed_uid) != pod_uid:
        raise BindingError("live Pod UID must be a canonical lowercase UUID")
    expected_metadata = {"namespace": render.NAMESPACE, "name": expected_name}
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            raise BindingError(f"live Pod metadata.{key} does not match the binding")
    labels = _mapping(metadata.get("labels"), "live Pod labels")
    required_labels = {
        "app.kubernetes.io/name": "genmol",
        "app.kubernetes.io/component": "restore-target",
        "archvteams.nebius.ai/run-id": run["run_id"],
        "nvidia.com/snapshot-is-restore-target": "true",
        "nvidia.com/snapshot-checkpoint-id": run["checkpoint_id"],
    }
    if any(labels.get(key) != value for key, value in required_labels.items()):
        raise BindingError("live Pod labels do not match the exact target contract")
    annotations = _mapping(metadata.get("annotations"), "live Pod annotations")
    required_annotations = {
        "nvidia.com/snapshot-target-containers": "genmol",
        "nvidia.com/snapshot-artifact-version": run["artifact_version"],
        "nvidia.com/snapshot-storage-type": "pvc",
        "nvidia.com/snapshot-storage-base-path": "/checkpoints",
        "archvteams.nebius.ai/artifact-manifest-sha256": run[
            "artifact_manifest_sha256"
        ],
        "archvteams.nebius.ai/tool-bundle-sha256": contract["tool_bundle"][
            "content_sha256"
        ],
    }
    if any(annotations.get(key) != value for key, value in required_annotations.items()):
        raise BindingError("live Pod annotations do not match the exact artifact contract")
    if spec.get("nodeName") != run["target_node"]:
        raise BindingError("live Pod was not scheduler-bound to the requested target node")
    if status.get("phase") != "Running":
        raise BindingError("live Pod is not Running")
    if status.get("qosClass") != "Burstable":
        raise BindingError("live Pod QoS does not match the bound systemd cgroup layout")
    scheduled = _condition(pod, "PodScheduled")
    if not scheduled or scheduled.get("status") != "True":
        raise BindingError("live Pod is not confirmed scheduled")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise BindingError("live Pod must contain exactly one target container")
    container = _mapping(containers[0], "live target container")
    if (
        container.get("name") != "genmol"
        or container.get("image") != render.NIM_IMAGE
        or container.get("command") != ["/bin/sleep"]
        or container.get("args") != ["2147483647"]
    ):
        raise BindingError("live target container identity or inert command changed")
    resources = _mapping(container.get("resources"), "live target resources")
    for group in ("requests", "limits"):
        values = _mapping(resources.get(group), f"live target resources.{group}")
        if values.get("nvidia.com/gpu") != "1":
            raise BindingError("live target must own exactly one GPU")
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list):
        raise BindingError("live Pod has no container statuses")
    matches = [item for item in statuses if isinstance(item, dict) and item.get("name") == "genmol"]
    if len(matches) != 1:
        raise BindingError("live Pod does not have one GenMol container status")
    container_status = matches[0]
    if not isinstance(container_status.get("state", {}).get("running"), dict):
        raise BindingError("live GenMol placeholder container is not running")
    container_id = container_status.get("containerID")
    if not isinstance(container_id, str) or render.CONTAINER_ID.fullmatch(container_id) is None:
        raise BindingError("live container status has no full containerd ID")
    image_id = container_status.get("imageID")
    if not isinstance(image_id, str) or image_id.removeprefix("docker-pullable://") != render.NIM_IMAGE:
        raise BindingError("live container image ID is not the pinned GenMol digest")
    pod_ip = status.get("podIP")
    if not isinstance(pod_ip, str):
        raise BindingError("live Pod has no Pod IP")
    pod_ips = status.get("podIPs")
    if pod_ips != [{"ip": pod_ip}]:
        raise BindingError("live Pod must expose exactly one bound Pod IP")

    digest = pod_spec_sha256(spec)
    existing_digest = annotations.get(POD_SPEC_HASH_KEY)
    if existing_digest is not None and existing_digest != digest:
        raise BindingError("existing target PodSpec annotation does not match the live PodSpec")
    binding = {
        "schema": render.BINDING_SCHEMA,
        "collected_at": collected_at,
        "run_id": run["run_id"],
        "namespace": render.NAMESPACE,
        "pod_name": expected_name,
        "pod_uid": pod_uid,
        "container_name": "genmol",
        "container_id": container_id,
        "cgroup": _burstable_systemd_cgroup(pod_uid, container_id),
        "pod_ip": pod_ip,
        "node": spec["nodeName"],
        "image_id": image_id.removeprefix("docker-pullable://"),
        "pod_spec_sha256": digest,
    }
    try:
        render.validate_binding(binding, run)
    except render.RenderError as exc:
        raise BindingError(str(exc)) from exc
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": pod_uid},
        {
            "op": "add",
            "path": "/metadata/annotations/archvteams.nebius.ai~1target-pod-spec-sha256",
            "value": digest,
        },
    ]
    return binding, patch


def _write_exclusive(path: Path, value: Any) -> None:
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or path.is_symlink() or os.path.lexists(path):
        raise BindingError(f"output path must be a new regular file: {path}")
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(payload)
        handle.flush()
        os.fsync(descriptor)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--pod-json", type=Path, required=True)
    parser.add_argument("--collected-at", required=True)
    parser.add_argument("--binding-output", type=Path, required=True)
    parser.add_argument("--patch-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run = render.validate_run(
            _mapping(_load_json(args.run_config, "run config"), "run config")
        )
        contract = render.validate_contract(
            _mapping(_load_json(args.contract, "contract"), "contract")
        )
        binding, patch = build_binding(
            _load_json(args.pod_json, "live Pod"),
            run,
            contract,
            args.collected_at,
        )
        _write_exclusive(args.binding_output, binding)
        _write_exclusive(args.patch_output, patch)
    except (BindingError, render.RenderError) as exc:
        print(f"bind-target: refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "binding": str(args.binding_output),
                "patch": str(args.patch_output),
                "pod_spec_sha256": binding["pod_spec_sha256"],
                "status": "bound",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
