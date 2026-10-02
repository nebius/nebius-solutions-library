#!/usr/bin/env python3
"""Offline renderer for the Evo2 native capture and artifact holders."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

import yaml


HERE = Path(__file__).resolve().parent
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
WORKER_GATE = json.loads((HERE / "worker-gate.json").read_text(encoding="utf-8"))
VALIDATOR_SOURCE = (HERE / "validate_evo2.py").read_text(encoding="utf-8")
VALIDATOR_SHA256 = hashlib.sha256(VALIDATOR_SOURCE.encode("utf-8")).hexdigest()
PREWARM_SOURCE = (HERE / "prewarm_artifact.py").read_text(encoding="utf-8") if (
    HERE / "prewarm_artifact.py"
).exists() else ""
NAMESPACE = "nim-fast-start"
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CaptureRenderError(ValueError):
    pass


def _capture_id(value: str) -> str:
    if len(value) > 24 or DNS_LABEL.fullmatch(value) is None:
        raise CaptureRenderError("capture ID must be a DNS label of at most 24 characters")
    return value


def _hostname_affinity() -> dict[str, Any]:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [PROFILE["hardware"]["retained_capture_node"]],
                            }
                        ]
                    }
                ]
            }
        }
    }


def render_storage() -> list[dict[str, Any]]:
    storage = PROFILE["storage"]
    common_labels = {
        "app.kubernetes.io/name": "evo2",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": storage["artifact_pvc"],
                "namespace": NAMESPACE,
                "labels": {**common_labels, "app.kubernetes.io/component": "native-artifact"},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": storage["artifact_capacity"]}},
                "storageClassName": storage["artifact_storage_class"],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": storage["cache_pvc"],
                "namespace": NAMESPACE,
                "labels": {**common_labels, "app.kubernetes.io/component": "nim-cache"},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": storage["cache_capacity"]}},
                "storageClassName": storage["cache_storage_class"],
            },
        },
    ]


def render_agent(capture_id: str) -> list[dict[str, Any]]:
    name = f"e2-snapshot-{capture_id}"
    return [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": "evo2",
                    "app.kubernetes.io/component": "snapshot-agent",
                    "app.kubernetes.io/part-of": "archvteams-2407-faststart",
                    "archvteams.nebius.ai/capture-id": capture_id,
                },
                "annotations": {
                    "archvteams.nebius.ai/worker-release-ready": "false",
                    "archvteams.nebius.ai/worker-class": "performance-validation",
                },
            },
            "spec": {
                "serviceAccountName": "archvteams-2407-native-snapshot",
                "automountServiceAccountToken": True,
                "enableServiceLinks": False,
                "restartPolicy": "Never",
                "runtimeClassName": "nvidia",
                "hostPID": True,
                "hostIPC": True,
                "hostNetwork": True,
                "affinity": _hostname_affinity(),
                "tolerations": [{"operator": "Exists"}],
                "imagePullSecrets": [{"name": "archvteams-2407-registry-pull"}],
                "initContainers": [
                    {
                        "name": "deploy-seccomp",
                        "image": "docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/bin/sh", "-c"],
                        "args": [
                            "set -eu\n"
                            "mkdir -p /host-seccomp/profiles\n"
                            "cp /seccomp-profiles/block-iouring.json "
                            "/host-seccomp/profiles/block-iouring.json\n"
                        ],
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "16Mi"},
                            "limits": {"cpu": "100m", "memory": "64Mi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "runAsUser": 0,
                            "runAsGroup": 0,
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
                "containers": [
                    {
                        "name": "agent",
                        "image": WORKER_GATE["worker_image"],
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/usr/local/bin/snapshot-agent"],
                        "args": [
                            "--runtime",
                            "containerd",
                            "--runtime-socket",
                            "/run/containerd/containerd.sock",
                        ],
                        "env": [
                            {
                                "name": "NODE_NAME",
                                "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}},
                            },
                            {"name": "SNAPSHOT_LOG_LEVEL", "value": "info"},
                            {
                                "name": "RESTRICTED_NAMESPACE",
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                            },
                        ],
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"cpu": "4", "memory": "4Gi"},
                        },
                        "securityContext": {"privileged": True},
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/etc/snapshot", "readOnly": True},
                            {"name": "checkpoints", "mountPath": "/checkpoints"},
                            {"name": "runtime-run", "mountPath": "/run/containerd"},
                            {
                                "name": "kubelet-pods",
                                "mountPath": "/var/lib/kubelet/pods",
                                "readOnly": True,
                            },
                            {
                                "name": "runtime-storage",
                                "mountPath": "/var/lib/containerd",
                                "readOnly": True,
                            },
                            {"name": "host-proc", "mountPath": "/host/proc"},
                            {"name": "host-cgroup", "mountPath": "/sys/fs/cgroup"},
                            {
                                "name": "pod-resources",
                                "mountPath": "/var/lib/kubelet/pod-resources",
                                "readOnly": True,
                            },
                        ],
                    }
                ],
                "volumes": [
                    {"name": "config", "configMap": {"name": "archvteams-2407-native-snapshot-config"}},
                    {
                        "name": "seccomp-profiles",
                        "configMap": {"name": "archvteams-2407-native-snapshot-seccomp"},
                    },
                    {
                        "name": "host-seccomp",
                        "hostPath": {
                            "path": "/var/lib/kubelet/seccomp",
                            "type": "DirectoryOrCreate",
                        },
                    },
                    {
                        "name": "checkpoints",
                        "persistentVolumeClaim": {"claimName": PROFILE["storage"]["artifact_pvc"]},
                    },
                    {"name": "runtime-run", "hostPath": {"path": "/run/containerd", "type": "Directory"}},
                    {
                        "name": "kubelet-pods",
                        "hostPath": {"path": "/var/lib/kubelet/pods", "type": "Directory"},
                    },
                    {
                        "name": "runtime-storage",
                        "hostPath": {"path": "/var/lib/containerd", "type": "Directory"},
                    },
                    {"name": "host-proc", "hostPath": {"path": "/proc", "type": "Directory"}},
                    {"name": "host-cgroup", "hostPath": {"path": "/sys/fs/cgroup", "type": "Directory"}},
                    {
                        "name": "pod-resources",
                        "hostPath": {
                            "path": "/var/lib/kubelet/pod-resources",
                            "type": "Directory",
                        },
                    },
                ],
            },
        }
    ]


def render_donor(capture_id: str) -> list[dict[str, Any]]:
    name = f"e2-donor-{capture_id}"
    checkpoint_id = PROFILE["artifacts"]["direct"]["checkpoint_id"]
    labels = {
        "app.kubernetes.io/name": "evo2",
        "app.kubernetes.io/component": "checkpoint-donor",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
        "archvteams.nebius.ai/capture-id": capture_id,
        "nvidia.com/snapshot-checkpoint-id": checkpoint_id,
    }
    resources = {
        "requests": PROFILE["pod_profile"]["requests"],
        "limits": PROFILE["pod_profile"]["limits"],
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": labels,
                "annotations": {
                    "archvteams.nebius.ai/validator-sha256": VALIDATOR_SHA256,
                },
            },
            "immutable": True,
            "data": {"validate_evo2.py": VALIDATOR_SOURCE},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 7200,
                "template": {
                    "metadata": {
                        "labels": {
                            **labels,
                            "nvidia.com/snapshot-is-checkpoint-source": "true",
                        },
                        "annotations": {
                            "nvidia.com/snapshot-artifact-version": "1",
                            "nvidia.com/snapshot-target-containers": PROFILE["model"]["container_name"],
                            "nvidia.com/snapshot-storage-type": "pvc",
                            "nvidia.com/snapshot-storage-base-path": "/checkpoints",
                            "linkerd.io/inject": "disabled",
                            "sidecar.istio.io/inject": "false",
                        },
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "runtimeClassName": "nvidia",
                        "affinity": _hostname_affinity(),
                        "imagePullSecrets": [{"name": "nvcrio-cred"}],
                        "securityContext": {
                            "seccompProfile": {
                                "type": "Localhost",
                                "localhostProfile": "profiles/block-iouring.json",
                            }
                        },
                        "containers": [
                            {
                                "name": PROFILE["model"]["container_name"],
                                "image": PROFILE["model"]["image"],
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/bash", "-lc"],
                                "args": [
                                    "set -Eeuo pipefail\n"
                                    "bash /opt/nim/start_server.sh &\n"
                                    "server_pid=$!\n"
                                    "trap 'kill \"$server_pid\" 2>/dev/null || true' TERM INT EXIT\n"
                                    "python3 /validator/validate_evo2.py "
                                    "--base-url http://127.0.0.1:8000 "
                                    "--receipt-dir /tmp/evo2-donor-semantic "
                                    f"--run-id {capture_id}-donor-a --run-id {capture_id}-donor-b "
                                    "--ready-timeout 1800 --timeout 300\n"
                                    "touch /snapshot-control/ready-for-snapshot\n"
                                    "wait \"$server_pid\"\n"
                                ],
                                "env": [
                                    {"name": "DYN_SNAPSHOT_CONTROL_DIR", "value": "/snapshot-control"},
                                    {
                                        "name": "NIM_CACHE_PATH",
                                        "value": PROFILE["model"]["nim_cache_path"],
                                    },
                                    {
                                        "name": "NGC_API_KEY",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "ngc-api-key",
                                                "key": "NGC_API_KEY",
                                            }
                                        },
                                    },
                                ],
                                "ports": [{"name": "http", "containerPort": 8000, "protocol": "TCP"}],
                                "resources": resources,
                                "securityContext": {
                                    "privileged": False,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "runAsUser": 0,
                                    "runAsGroup": 0,
                                },
                                "readinessProbe": {
                                    "exec": {"command": ["/bin/cat", "/snapshot-control/ready-for-snapshot"]},
                                    "periodSeconds": 1,
                                    "timeoutSeconds": 1,
                                    "failureThreshold": 7200,
                                },
                                "volumeMounts": [
                                    {"name": "validator", "mountPath": "/validator", "readOnly": True},
                                    {"name": "dshm", "mountPath": "/dev/shm"},
                                    {
                                        "name": "nim-cache",
                                        "mountPath": PROFILE["model"]["cache_path"],
                                    },
                                    {"name": "workspace", "mountPath": "/opt/nim/workspace"},
                                    {"name": "output", "mountPath": "/output"},
                                    {
                                        "name": "snapshot-control",
                                        "mountPath": "/snapshot-control",
                                        "subPath": PROFILE["model"]["container_name"],
                                    },
                                    {"name": "checkpoints", "mountPath": "/checkpoints", "readOnly": True},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "validator", "configMap": {"name": name, "defaultMode": 292}},
                            {
                                "name": "dshm",
                                "emptyDir": {
                                    "medium": "Memory",
                                    "sizeLimit": PROFILE["pod_profile"]["shared_memory"],
                                },
                            },
                            {
                                "name": "nim-cache",
                                "persistentVolumeClaim": {
                                    "claimName": PROFILE["storage"]["cache_pvc"]
                                },
                            },
                            {"name": "workspace", "emptyDir": {"sizeLimit": "8Gi"}},
                            {"name": "output", "emptyDir": {"sizeLimit": "1Gi"}},
                            {"name": "snapshot-control", "emptyDir": {"sizeLimit": "16Mi"}},
                            {
                                "name": "checkpoints",
                                "persistentVolumeClaim": {
                                    "claimName": PROFILE["storage"]["artifact_pvc"],
                                    "readOnly": True,
                                },
                            },
                        ],
                    },
                },
            },
        },
    ]


def render_content(capture_id: str, source_pod: str, source_uid: str) -> list[dict[str, Any]]:
    if len(source_pod) > 63 or DNS_LABEL.fullmatch(source_pod) is None:
        raise CaptureRenderError("source Pod name is invalid")
    try:
        parsed = uuid.UUID(source_uid)
    except ValueError as exc:
        raise CaptureRenderError("source Pod UID is invalid") from exc
    if str(parsed) != source_uid:
        raise CaptureRenderError("source Pod UID must be canonical lowercase UUID")
    checkpoint_id = PROFILE["artifacts"]["direct"]["checkpoint_id"]
    return [
        {
            "apiVersion": "nvidia.com/v1alpha1",
            "kind": "PodSnapshotContent",
            "metadata": {
                "name": f"e2-{capture_id}",
                "labels": {
                    "nvidia.com/snapshot-node": PROFILE["hardware"]["retained_capture_node"],
                    "archvteams.nebius.ai/capture-id": capture_id,
                },
            },
            "spec": {
                "snapshotRef": {"namespace": NAMESPACE, "name": f"e2-{capture_id}"},
                "source": {
                    "nodeName": PROFILE["hardware"]["retained_capture_node"],
                    "podRef": {
                        "name": source_pod,
                        "uid": source_uid,
                        "containers": [PROFILE["model"]["container_name"]],
                    },
                },
            },
        }
    ]


def render_holder(
    capture_id: str,
    mode: str,
    manifest_sha256: str,
    file_count: int,
    total_bytes: int,
) -> list[dict[str, Any]]:
    if not PREWARM_SOURCE:
        raise CaptureRenderError("prewarm_artifact.py is unavailable")
    if mode not in {"direct", "buffered"}:
        raise CaptureRenderError("holder mode must be direct or buffered")
    if SHA256.fullmatch(manifest_sha256) is None:
        raise CaptureRenderError("manifest SHA-256 is invalid")
    if file_count < 2 or total_bytes <= 0:
        raise CaptureRenderError("artifact inventory must be positive")
    artifact = PROFILE["artifacts"][mode]
    name = f"e2-{mode}-holder-{capture_id}"
    args = [
        "/holder/prewarm_artifact.py",
        "--root",
        f"/checkpoints/{artifact['checkpoint_id']}/versions/{artifact['artifact_version']}",
        "--manifest-sha256",
        manifest_sha256,
        "--file-count",
        str(file_count),
        "--total-bytes",
        str(total_bytes),
        "--mode",
        mode,
        "--receipt",
        "/tmp/prewarm-receipt.json",
        "--ready-marker",
        "/tmp/prewarm-complete",
        "--hold",
    ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "immutable": True,
            "data": {"prewarm_artifact.py": PREWARM_SOURCE},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": "evo2",
                    "app.kubernetes.io/component": "artifact-holder",
                    "archvteams.nebius.ai/checkpoint-id": artifact["checkpoint_id"],
                    "archvteams.nebius.ai/image-io-mode": mode,
                },
                "annotations": {
                    "archvteams.nebius.ai/artifact-manifest-sha256": manifest_sha256,
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "affinity": _hostname_affinity(),
                "containers": [
                    {
                        "name": "holder",
                        "image": "docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["/usr/local/bin/python3"],
                        "args": args,
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "4", "memory": "2Gi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": True,
                        },
                        "readinessProbe": {
                            "exec": {"command": ["/bin/test", "-f", "/tmp/prewarm-complete"]},
                            "periodSeconds": 1,
                            "timeoutSeconds": 1,
                            "failureThreshold": 1800,
                        },
                        "volumeMounts": [
                            {"name": "holder", "mountPath": "/holder", "readOnly": True},
                            {"name": "checkpoints", "mountPath": "/checkpoints", "readOnly": True},
                            {"name": "cache", "mountPath": "/nim-cache", "readOnly": True},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }
                ],
                "volumes": [
                    {"name": "holder", "configMap": {"name": name, "defaultMode": 292}},
                    {
                        "name": "checkpoints",
                        "persistentVolumeClaim": {
                            "claimName": PROFILE["storage"]["artifact_pvc"],
                            "readOnly": True,
                        },
                    },
                    {
                        "name": "cache",
                        "persistentVolumeClaim": {
                            "claimName": PROFILE["storage"]["cache_pvc"],
                            "readOnly": True,
                        },
                    },
                    {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}},
                ],
            },
        },
    ]


def validate_documents(documents: list[dict[str, Any]]) -> None:
    rendered = json.dumps(documents, sort_keys=True)
    if "@@" in rendered or "REPLACE" in rendered or ":latest" in rendered:
        raise CaptureRenderError("render contains a placeholder or mutable image")
    for document in documents:
        metadata = document.get("metadata", {})
        if document.get("kind") != "PodSnapshotContent" and metadata.get("namespace") != NAMESPACE:
            raise CaptureRenderError("namespaced capture object has the wrong namespace")
    for pod in [item for item in documents if item.get("kind") == "Pod"]:
        if "nodeName" in pod.get("spec", {}):
            raise CaptureRenderError("capture Pods must use scheduler affinity")


def dump_documents(documents: Iterable[dict[str, Any]]) -> None:
    yaml.safe_dump_all(
        list(documents), sys.stdout, explicit_start=True, sort_keys=False, default_flow_style=False
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("storage")
    for mode in ("agent", "donor"):
        child = subparsers.add_parser(mode)
        child.add_argument("--capture-id", required=True)
    content = subparsers.add_parser("content")
    content.add_argument("--capture-id", required=True)
    content.add_argument("--source-pod", required=True)
    content.add_argument("--source-uid", required=True)
    holder = subparsers.add_parser("holder")
    holder.add_argument("--capture-id", required=True)
    holder.add_argument("--image-io-mode", choices=("direct", "buffered"), required=True)
    holder.add_argument("--manifest-sha256", required=True)
    holder.add_argument("--file-count", type=int, required=True)
    holder.add_argument("--total-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "storage":
            documents = render_storage()
        else:
            capture_id = _capture_id(args.capture_id)
            if args.mode == "agent":
                documents = render_agent(capture_id)
            elif args.mode == "donor":
                documents = render_donor(capture_id)
            elif args.mode == "content":
                documents = render_content(capture_id, args.source_pod, args.source_uid)
            else:
                documents = render_holder(
                    capture_id,
                    args.image_io_mode,
                    args.manifest_sha256,
                    args.file_count,
                    args.total_bytes,
                )
        validate_documents(documents)
        dump_documents(documents)
        return 0
    except (CaptureRenderError, OSError, KeyError, yaml.YAMLError) as exc:
        print(f"render-evo2-capture: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
