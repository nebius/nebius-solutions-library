#!/usr/bin/env python3
"""Render hf93-pinned rootfs inspection or immutable candidate-build objects."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "rootfs_variant.py"
TOOL_SHA256 = "d37dc95cfb5469a47f13bcc26ec3a7ac2bdd716acef3f7586496423ac91d640c"
NODE = "gpu-node-a.example.invalid"
NAMESPACE = "nim-fast-start"
PVC = "diffdock-native-f7-artifacts"
PYTHON_IMAGE = (
    "docker.io/library/python@sha256:"
    "356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e"
)
SOURCE_ID = "diffdock-native-f7-v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RenderError(ValueError):
    """Input evidence is not exact enough to render a mutating builder."""


def _load(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read {label}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must be a JSON object")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if raw.strip() != canonical:
        raise RenderError(f"{label} must be canonical single-object JSON")
    return value, canonical


def _tool_source() -> str:
    raw = TOOL.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOOL_SHA256:
        raise RenderError("rootfs variant tool digest changed")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RenderError("rootfs variant tool is not UTF-8") from exc


def _pod_spec(mode: str, args: list[str]) -> dict[str, Any]:
    read_only = mode == "inspect"
    config_name = (
        "diffdock-native-f7-rootfs-inspect"
        if read_only
        else "diffdock-native-f7-rootfsless-build"
    )
    return {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [NODE],
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "securityContext": {
            "runAsUser": 65534 if read_only else 0,
            "runAsGroup": 65534 if read_only else 0,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": f"rootfs-{mode}",
                "image": PYTHON_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/usr/local/bin/python3"],
                "args": ["/tool/rootfs_variant.py", "--checkpoints", "/checkpoints", *args],
                "env": [
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                ],
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "volumeMounts": [
                    {"name": "tool", "mountPath": "/tool", "readOnly": True},
                    {
                        "name": "checkpoints",
                        "mountPath": "/checkpoints",
                        "readOnly": read_only,
                    },
                ],
            }
        ],
        "volumes": [
            {
                "name": "tool",
                "configMap": {
                    "name": config_name,
                    "defaultMode": 0o444,
                },
            },
            {
                "name": "checkpoints",
                "persistentVolumeClaim": {"claimName": PVC, "readOnly": read_only},
            },
        ],
    }


def render_inspect() -> list[dict[str, Any]]:
    mode = "inspect"
    name = "diffdock-native-f7-rootfs-inspect"
    annotations = {"archvteams.nebius.ai/tool-sha256": TOOL_SHA256}
    labels = {
        "app.kubernetes.io/name": "diffdock",
        "app.kubernetes.io/component": "rootfs-inspector",
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": labels,
                "annotations": annotations,
            },
            "immutable": True,
            "data": {"rootfs_variant.py": _tool_source()},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "template": {"metadata": {"labels": labels}, "spec": _pod_spec(mode, [mode])},
            },
        },
    ]


def render_build(artifact_path: Path, review_path: Path) -> list[dict[str, Any]]:
    artifact, _ = _load(artifact_path, "artifact receipt")
    review, review_bytes = _load(review_path, "rootfs review")
    if (
        artifact.get("schema")
        != "archvteams.nebius.ai/diffdock-native-artifact-receipt/v1"
        or artifact.get("checkpoint_id") != SOURCE_ID
        or artifact.get("artifact_version") != "1"
    ):
        raise RenderError("artifact receipt is not the exact source checkpoint")
    manifest_sha256 = artifact.get("manifest_sha256")
    if not isinstance(manifest_sha256, str) or not SHA256.fullmatch(manifest_sha256):
        raise RenderError("artifact receipt has no canonical manifest digest")
    if (
        review.get("schema") != "archvteams.nebius.ai/diffdock-rootfs-review/v1"
        or review.get("source_checkpoint_id") != SOURCE_ID
        or review.get("artifact_version") != "1"
        or review.get("source_manifest_sha256") != manifest_sha256
        or review.get("eligible_for_rootfsless_candidate") is not True
        or review.get("unclassified_members") != []
    ):
        raise RenderError("rootfs review does not approve an exact source-only candidate")
    review_sha256 = hashlib.sha256(review_bytes).hexdigest()
    mode = "build"
    name = "diffdock-native-f7-rootfsless-build"
    annotations = {
        "archvteams.nebius.ai/tool-sha256": TOOL_SHA256,
        "archvteams.nebius.ai/source-manifest-sha256": manifest_sha256,
        "archvteams.nebius.ai/rootfs-review-sha256": review_sha256,
    }
    labels = {
        "app.kubernetes.io/name": "diffdock",
        "app.kubernetes.io/component": "rootfsless-builder",
        "archvteams.nebius.ai/checkpoint-id": "diffdock-native-f7-v2-rootfsless",
    }
    args = [
        mode,
        "--expected-source-manifest-sha256",
        manifest_sha256,
        "--expected-review-sha256",
        review_sha256,
    ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": labels,
                "annotations": annotations,
            },
            "immutable": True,
            "data": {"rootfs_variant.py": _tool_source()},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "template": {"metadata": {"labels": labels}, "spec": _pod_spec(mode, args)},
            },
        },
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("inspect")
    builder = subparsers.add_parser("build")
    builder.add_argument("--artifact-receipt", type=Path, required=True)
    builder.add_argument("--rootfs-review", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        documents = (
            render_inspect()
            if args.mode == "inspect"
            else render_build(args.artifact_receipt, args.rootfs_review)
        )
    except (OSError, RenderError) as exc:
        print(f"render-rootfs-variant: refused: {exc}", file=sys.stderr)
        return 2
    yaml.safe_dump_all(
        documents,
        sys.stdout,
        explicit_start=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
