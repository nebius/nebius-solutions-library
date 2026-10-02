#!/usr/bin/env python3
"""Render and verify the run-owned new-node seccomp profile installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

import yaml


NAMESPACE = "nim-fast-start"
RUN_LABEL = "archvteams.nebius.ai/run-id"
CONFIGMAP = "archvteams-2407-native-snapshot-seccomp"
PROFILE_KEY = "block-iouring.json"
PROFILE_SHA256 = "ebbe5e221b6b331bb84efbdfea7adb88e9dddab62a2ea901598bad09fe7f76a0"
PROFILE_TEXT = """{
  "defaultAction": "SCMP_ACT_ALLOW",
  "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
  "syscalls": [
    {
      "names": ["io_uring_setup", "io_uring_enter", "io_uring_register"],
      "action": "SCMP_ACT_ERRNO",
      "comment": "Block io_uring syscalls - CRIU doesn't support io_uring memory mappings"
    }
  ]
}
"""
INSTALLER_IMAGE = (
    "docker.io/library/busybox@"
    "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
NODE_NAME = re.compile(r"^computeinstance-[a-z0-9]+$")
COMMAND = """set -eu
mkdir -p /host-seccomp/profiles
cp /seccomp-profiles/block-iouring.json /host-seccomp/profiles/block-iouring.json
chmod 0644 /host-seccomp/profiles/block-iouring.json
cmp /seccomp-profiles/block-iouring.json /host-seccomp/profiles/block-iouring.json
sha256sum /host-seccomp/profiles/block-iouring.json
touch /tmp/ready
while :; do sleep 3600; done
"""


class InstallerError(ValueError):
    """The installer input or live object violates the exact contract."""


def _run_id(value: str) -> str:
    if len(value) > 30 or RUN_ID.fullmatch(value) is None:
        raise InstallerError("run ID must be a lowercase DNS label no longer than 30 characters")
    return value


def _node(value: str) -> str:
    if NODE_NAME.fullmatch(value) is None:
        raise InstallerError("node must be an exact compute instance name")
    return value


def _load(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InstallerError(f"{label} must be a regular non-symlink file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise InstallerError(f"{label} must be a JSON object")
    return value


def validate_configmap(configmap: dict[str, Any]) -> dict[str, str]:
    if configmap.get("apiVersion") != "v1" or configmap.get("kind") != "ConfigMap":
        raise InstallerError("seccomp source is not a core/v1 ConfigMap")
    metadata = configmap.get("metadata")
    if not isinstance(metadata, dict):
        raise InstallerError("seccomp ConfigMap metadata is missing")
    if metadata.get("name") != CONFIGMAP or metadata.get("namespace") != NAMESPACE:
        raise InstallerError("seccomp ConfigMap identity differs")
    data = configmap.get("data")
    if not isinstance(data, dict) or set(data) != {PROFILE_KEY}:
        raise InstallerError("seccomp ConfigMap must contain only the exact profile key")
    profile = data.get(PROFILE_KEY)
    if not isinstance(profile, str):
        raise InstallerError("seccomp profile must be text")
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    if digest != PROFILE_SHA256:
        raise InstallerError(f"seccomp profile SHA-256 differs: {digest}")
    if profile != PROFILE_TEXT:
        raise InstallerError("seccomp profile bytes differ despite the pinned digest")
    return {"configmap": CONFIGMAP, "profile_sha256": digest}


def render(run_id: str, node: str) -> dict[str, Any]:
    run_id = _run_id(run_id)
    node = _node(node)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"of2-seccomp-{run_id}",
            "namespace": NAMESPACE,
            "labels": {
                RUN_LABEL: run_id,
                "app.kubernetes.io/component": "seccomp-installer",
            },
        },
        "spec": {
            "nodeName": node,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "installer",
                    "image": INSTALLER_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c", COMMAND],
                    "securityContext": {
                        "privileged": False,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "readinessProbe": {
                        "exec": {
                            "command": [
                                "/bin/sh",
                                "-c",
                                "test -f /tmp/ready && cmp /seccomp-profiles/block-iouring.json /host-seccomp/profiles/block-iouring.json",
                            ]
                        },
                        "periodSeconds": 1,
                        "timeoutSeconds": 1,
                        "failureThreshold": 1,
                        "successThreshold": 1,
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "8Mi"},
                        "limits": {"cpu": "100m", "memory": "32Mi"},
                    },
                    "volumeMounts": [
                        {
                            "name": "seccomp-profiles",
                            "mountPath": "/seccomp-profiles",
                            "readOnly": True,
                        },
                        {"name": "host-seccomp", "mountPath": "/host-seccomp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "seccomp-profiles", "configMap": {"name": CONFIGMAP}},
                {
                    "name": "host-seccomp",
                    "hostPath": {
                        "path": "/var/lib/kubelet/seccomp",
                        "type": "DirectoryOrCreate",
                    },
                },
            ],
        },
    }


def validate_live(pod: dict[str, Any], run_id: str, node: str, uid: str) -> dict[str, str]:
    expected = render(run_id, node)
    try:
        parsed_uid = uuid.UUID(uid)
    except ValueError as exc:
        raise InstallerError("installer UID is not a UUID") from exc
    if str(parsed_uid) != uid:
        raise InstallerError("installer UID is not canonical")
    if pod.get("apiVersion") != "v1" or pod.get("kind") != "Pod":
        raise InstallerError("live installer is not a core/v1 Pod")
    metadata = pod.get("metadata")
    if not isinstance(metadata, dict):
        raise InstallerError("live installer metadata is missing")
    expected_metadata = expected["metadata"]
    if (
        metadata.get("name") != expected_metadata["name"]
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("uid") != uid
        or metadata.get("deletionTimestamp") is not None
        or metadata.get("labels", {}).get(RUN_LABEL) != run_id
        or metadata.get("labels", {}).get("app.kubernetes.io/component")
        != "seccomp-installer"
    ):
        raise InstallerError("live installer identity differs")
    spec = pod.get("spec")
    if not isinstance(spec, dict):
        raise InstallerError("live installer spec is missing")
    for key in (
        "nodeName",
        "nodeSelector",
        "restartPolicy",
        "automountServiceAccountToken",
        "enableServiceLinks",
        "securityContext",
    ):
        if spec.get(key) != expected["spec"][key]:
            raise InstallerError(f"live installer {key} differs")
    if spec.get("imagePullSecrets") not in (None, []):
        raise InstallerError("live installer unexpectedly references an image-pull secret")
    if spec.get("initContainers") not in (None, []):
        raise InstallerError("live installer unexpectedly has init containers")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise InstallerError("live installer must have one container")
    container = containers[0]
    expected_container = expected["spec"]["containers"][0]
    for key in (
        "name",
        "image",
        "imagePullPolicy",
        "command",
        "securityContext",
        "readinessProbe",
        "resources",
    ):
        if container.get(key) != expected_container[key]:
            raise InstallerError(f"live installer container {key} differs")
    if container.get("args") not in (None, []):
        raise InstallerError("live installer unexpectedly has container arguments")
    mounts = container.get("volumeMounts")
    if not isinstance(mounts, list):
        raise InstallerError("live installer volume mounts are missing")
    admitted_mounts = [
        {"name": item.get("name"), "mountPath": item.get("mountPath"), "readOnly": item.get("readOnly", False)}
        for item in mounts
        if isinstance(item, dict)
    ]
    expected_mounts = [
        {"name": item["name"], "mountPath": item["mountPath"], "readOnly": item.get("readOnly", False)}
        for item in expected_container["volumeMounts"]
    ]
    if admitted_mounts != expected_mounts:
        raise InstallerError("live installer volume mounts differ")
    volumes = spec.get("volumes")
    if not isinstance(volumes, list) or len(volumes) != 2:
        raise InstallerError("live installer volumes differ")
    admitted_volumes = []
    for volume in volumes:
        if not isinstance(volume, dict):
            raise InstallerError("live installer contains a malformed volume")
        if volume.get("name") == "seccomp-profiles":
            admitted_volumes.append(
                {"name": volume.get("name"), "configMap": {"name": volume.get("configMap", {}).get("name")}}
            )
        elif volume.get("name") == "host-seccomp":
            admitted_volumes.append(
                {
                    "name": volume.get("name"),
                    "hostPath": {
                        "path": volume.get("hostPath", {}).get("path"),
                        "type": volume.get("hostPath", {}).get("type"),
                    },
                }
            )
        else:
            raise InstallerError("live installer contains an unexpected volume")
    if admitted_volumes != expected["spec"]["volumes"]:
        raise InstallerError("live installer volumes differ")
    status = pod.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Running":
        raise InstallerError("live installer is not Running")
    container_statuses = status.get("containerStatuses")
    if (
        not isinstance(container_statuses, list)
        or len(container_statuses) != 1
        or container_statuses[0].get("name") != "installer"
        or container_statuses[0].get("ready") is not True
    ):
        raise InstallerError("live installer is not Ready")
    image_id = container_statuses[0].get("imageID")
    if image_id != INSTALLER_IMAGE:
        raise InstallerError("live installer image ID differs")
    return {
        "name": expected_metadata["name"],
        "uid": uid,
        "node": node,
        "image": INSTALLER_IMAGE,
        "profile_sha256": PROFILE_SHA256,
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise InstallerError(f"output already exists: {path}")
    payload = yaml.safe_dump(value, default_flow_style=False, sort_keys=False).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(payload)
        handle.flush()
        os.fsync(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    config = subparsers.add_parser("validate-configmap")
    config.add_argument("--configmap-json", type=Path, required=True)
    renderer = subparsers.add_parser("render")
    renderer.add_argument("--run-id", required=True)
    renderer.add_argument("--node", required=True)
    renderer.add_argument("--output", type=Path, required=True)
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("--pod-json", type=Path, required=True)
    verifier.add_argument("--run-id", required=True)
    verifier.add_argument("--node", required=True)
    verifier.add_argument("--uid", required=True)
    args = parser.parse_args()
    try:
        if args.mode == "validate-configmap":
            receipt = validate_configmap(_load(args.configmap_json, "seccomp ConfigMap"))
        elif args.mode == "render":
            _write(args.output, render(args.run_id, args.node))
            receipt = {"name": f"of2-seccomp-{args.run_id}", "node": args.node}
        else:
            receipt = validate_live(
                _load(args.pod_json, "live installer Pod"),
                args.run_id,
                args.node,
                args.uid,
            )
    except InstallerError as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
