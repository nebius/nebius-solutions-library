#!/usr/bin/env python3
"""Offline renderer for the RFdiffusion native capture and artifact holders."""

from __future__ import annotations

import argparse
import copy
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
VALIDATOR_SOURCE = (HERE / "validate_rfdiffusion.py").read_text(encoding="utf-8")
VALIDATOR_SHA256 = hashlib.sha256(VALIDATOR_SOURCE.encode("utf-8")).hexdigest()
FIXTURE_SOURCE = (HERE / PROFILE["semantic_profile"]["fixture"]).read_text(encoding="ascii")
FIXTURE_SHA256 = hashlib.sha256(FIXTURE_SOURCE.encode("ascii")).hexdigest()
PREWARM_SOURCE = (HERE / "prewarm_artifact.py").read_text(encoding="utf-8") if (
    HERE / "prewarm_artifact.py"
).exists() else ""
VARIANT_SOURCE = (HERE / "artifact_variant.py").read_text(encoding="utf-8") if (
    HERE / "artifact_variant.py"
).exists() else ""
PROFILE_SOURCE = (HERE / "profile.json").read_text(encoding="utf-8")
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
        "app.kubernetes.io/name": "rfdiffusion",
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
    name = f"rfd-snapshot-{capture_id}"
    return [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": "rfdiffusion",
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
    name = f"rfd-donor-{capture_id}"
    checkpoint_id = PROFILE["artifacts"]["direct"]["checkpoint_id"]
    labels = {
        "app.kubernetes.io/name": "rfdiffusion",
        "app.kubernetes.io/component": "checkpoint-donor",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
        "archvteams.nebius.ai/capture-id": capture_id,
        "nvidia.com/snapshot-checkpoint-id": checkpoint_id,
    }
    resources = {
        "requests": PROFILE["pod_profile"]["requests"],
        "limits": PROFILE["pod_profile"]["limits"],
    }
    materializer = (
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "root = Path('/validator')\n"
        "for name, value, encoding in ((\n"
        "    'validate_rfdiffusion.py', sys.argv[1], 'utf-8'),\n"
        "    ('1UBQ.pdb', sys.argv[2], 'ascii'),\n"
        "    ('prewarm_artifact.py', sys.argv[3], 'utf-8'),\n"
        "):\n"
        "    path = root / name\n"
        "    path.write_text(value, encoding=encoding)\n"
        "    os.chmod(path, 0o444)\n"
    )
    return [
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
                            "fsGroup": 1000,
                            "fsGroupChangePolicy": "OnRootMismatch",
                            "seccompProfile": {
                                "type": "Localhost",
                                "localhostProfile": "profiles/block-iouring.json",
                            }
                        },
                        "initContainers": [
                            {
                                "name": "materialize-validator",
                                "image": "docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/usr/local/bin/python3"],
                                "args": [
                                    "-c",
                                    materializer,
                                    VALIDATOR_SOURCE,
                                    FIXTURE_SOURCE,
                                    PREWARM_SOURCE,
                                ],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "64Mi"},
                                    "limits": {"cpu": "1", "memory": "256Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                    "runAsUser": 1000,
                                    "runAsGroup": 1000,
                                },
                                "volumeMounts": [
                                    {"name": "validator", "mountPath": "/validator"},
                                ],
                            },
                            {
                                "name": "verify-pinned-cache",
                                "image": "docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/usr/local/bin/python3"],
                                "args": [
                                    "/validator/prewarm_artifact.py",
                                    "--cache-only",
                                    "--cache-root",
                                    "/nim-cache",
                                    "--cache-tree-sha256",
                                    PROFILE["retained_evidence"]["cache_tree_sha256"],
                                    "--cache-file-count",
                                    str(PROFILE["retained_evidence"]["cache_file_count"]),
                                    "--cache-total-bytes",
                                    str(PROFILE["retained_evidence"]["cache_regular_file_bytes"]),
                                    "--required-cache-relative-path",
                                    PROFILE["retained_evidence"]["critical_cache_file"],
                                    "--receipt",
                                    "/tmp/cache-receipt.json",
                                    "--ready-marker",
                                    "/tmp/cache-ready",
                                ],
                                "resources": {
                                    "requests": {"cpu": "4", "memory": "2Gi"},
                                    "limits": {"cpu": "4", "memory": "2Gi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                    "runAsUser": 1000,
                                    "runAsGroup": 1000,
                                },
                                "volumeMounts": [
                                    {"name": "validator", "mountPath": "/validator", "readOnly": True},
                                    {"name": "nim-cache", "mountPath": "/nim-cache", "readOnly": True},
                                    {"name": "cache-verify-tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
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
                                    "python3 /validator/validate_rfdiffusion.py "
                                    "--fixture /validator/1UBQ.pdb "
                                    "--base-url http://127.0.0.1:8000 "
                                    "--receipt-dir /tmp/rfdiffusion-donor-semantic "
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
                                    "runAsUser": 1000,
                                    "runAsGroup": 1000,
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
                                        "readOnly": True,
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
                            {
                                "name": "validator",
                                "emptyDir": {
                                    "sizeLimit": PROFILE["runtime_topology"]["validator_size_limit"]
                                },
                            },
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
                                    "claimName": PROFILE["storage"]["cache_pvc"],
                                    "readOnly": True,
                                },
                            },
                            {"name": "cache-verify-tmp", "emptyDir": {"sizeLimit": "16Mi"}},
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


def render_cache_populator(capture_id: str) -> list[dict[str, Any]]:
    """Render a one-shot conventional run that materializes the pinned NIM cache.

    This setup Job is deliberately not a snapshot source. It completes two strict
    semantic calls, verifies the exact retained cache tree, and exits so the GPU
    is released before the exact-topology donor is created.
    """

    documents = copy.deepcopy(render_donor(capture_id))
    name = f"rfd-cache-{capture_id}"
    job = next(item for item in documents if item["kind"] == "Job")
    for document in documents:
        document["metadata"]["name"] = name
        labels = document["metadata"].get("labels", {})
        labels["app.kubernetes.io/component"] = "cache-populator"
        labels.pop("nvidia.com/snapshot-checkpoint-id", None)
    pod_template = job["spec"]["template"]
    pod_template["metadata"]["labels"]["app.kubernetes.io/component"] = "cache-populator"
    for key in (
        "nvidia.com/snapshot-checkpoint-id",
        "nvidia.com/snapshot-is-checkpoint-source",
    ):
        pod_template["metadata"]["labels"].pop(key, None)
    for key in (
        "nvidia.com/snapshot-artifact-version",
        "nvidia.com/snapshot-target-containers",
        "nvidia.com/snapshot-storage-type",
        "nvidia.com/snapshot-storage-base-path",
    ):
        pod_template["metadata"]["annotations"].pop(key, None)
    spec = pod_template["spec"]
    spec["initContainers"] = [
        item
        for item in spec["initContainers"]
        if item["name"] == "materialize-validator"
    ]
    container = spec["containers"][0]
    container["args"] = [
        "set -Eeuo pipefail\n"
        "bash /opt/nim/start_server.sh &\n"
        "server_pid=$!\n"
        "trap 'kill \"$server_pid\" 2>/dev/null || true' EXIT TERM INT\n"
        "python3 /validator/validate_rfdiffusion.py "
        "--fixture /validator/1UBQ.pdb "
        "--base-url http://127.0.0.1:8000 "
        "--receipt-dir /tmp/rfdiffusion-cache-semantic "
        f"--run-id {capture_id}-cache-a --run-id {capture_id}-cache-b "
        "--ready-timeout 1800 --timeout 300\n"
        "python3 /validator/prewarm_artifact.py --cache-only "
        "--cache-root /home/user/.cache/nim "
        f"--cache-tree-sha256 {PROFILE['retained_evidence']['cache_tree_sha256']} "
        f"--cache-file-count {PROFILE['retained_evidence']['cache_file_count']} "
        f"--cache-total-bytes {PROFILE['retained_evidence']['cache_regular_file_bytes']} "
        f"--required-cache-relative-path {PROFILE['retained_evidence']['critical_cache_file']} "
        "--receipt /tmp/cache-receipt.json --ready-marker /tmp/cache-ready\n"
    ]
    container.pop("readinessProbe", None)
    container["env"] = [
        item for item in container["env"] if item.get("name") != "DYN_SNAPSHOT_CONTROL_DIR"
    ]
    container["volumeMounts"] = [
        item
        for item in container["volumeMounts"]
        if item["name"] not in {"snapshot-control", "checkpoints"}
    ]
    for mount in container["volumeMounts"]:
        if mount["name"] == "nim-cache":
            mount.pop("readOnly", None)
    spec["volumes"] = [
        item
        for item in spec["volumes"]
        if item["name"] not in {"snapshot-control", "checkpoints", "cache-verify-tmp"}
    ]
    cache_volume = next(item for item in spec["volumes"] if item["name"] == "nim-cache")
    cache_volume["persistentVolumeClaim"].pop("readOnly", None)
    return documents


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
                "name": f"rfd-{capture_id}",
                "labels": {
                    "nvidia.com/snapshot-node": PROFILE["hardware"]["retained_capture_node"],
                    "archvteams.nebius.ai/capture-id": capture_id,
                },
            },
            "spec": {
                "snapshotRef": {"namespace": NAMESPACE, "name": f"rfd-{capture_id}"},
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
    name = f"rfd-{mode}-holder-{capture_id}"
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
        "--cache-root",
        "/nim-cache",
        "--cache-tree-sha256",
        PROFILE["retained_evidence"]["cache_tree_sha256"],
        "--cache-file-count",
        str(PROFILE["retained_evidence"]["cache_file_count"]),
        "--cache-total-bytes",
        str(PROFILE["retained_evidence"]["cache_regular_file_bytes"]),
        "--required-cache-relative-path",
        PROFILE["retained_evidence"]["critical_cache_file"],
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
                    "app.kubernetes.io/name": "rfdiffusion",
                    "app.kubernetes.io/component": "artifact-holder",
                    "archvteams.nebius.ai/checkpoint-id": artifact["checkpoint_id"],
                    "archvteams.nebius.ai/image-io-mode": mode,
                },
                "annotations": {
                    "archvteams.nebius.ai/artifact-manifest-sha256": manifest_sha256,
                    "archvteams.nebius.ai/cache-tree-sha256": PROFILE[
                        "retained_evidence"
                    ]["cache_tree_sha256"],
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

def render_variant_builder(
    capture_id: str,
    source_manifest_sha256: str,
    source_file_count: int,
    source_total_bytes: int,
) -> list[dict[str, Any]]:
    """Render the CPU-only, write-once direct-to-buffered artifact builder."""

    if not VARIANT_SOURCE:
        raise CaptureRenderError("artifact_variant.py is unavailable")
    if SHA256.fullmatch(source_manifest_sha256) is None:
        raise CaptureRenderError("source manifest SHA-256 is invalid")
    if source_file_count < 2 or source_total_bytes <= 0:
        raise CaptureRenderError("source artifact inventory must be positive")
    name = f"rfd-buffered-builder-{capture_id}"
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "immutable": True,
            "data": {
                "artifact_variant.py": VARIANT_SOURCE,
                "profile.json": PROFILE_SOURCE,
            },
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": "rfdiffusion",
                    "app.kubernetes.io/component": "artifact-variant-builder",
                    "archvteams.nebius.ai/capture-id": capture_id,
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 600,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/name": "rfdiffusion",
                            "app.kubernetes.io/component": "artifact-variant-builder",
                            "archvteams.nebius.ai/capture-id": capture_id,
                        }
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "affinity": _hostname_affinity(),
                        "securityContext": {
                            "runAsUser": 0,
                            "runAsGroup": 0,
                        },
                        "containers": [
                            {
                                "name": "builder",
                                "image": "docker.io/library/python@sha256:356b0d18f9385f4bdcc673af60e1e64c9d1504952e4ec36ee32044c722a6bc4e",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/usr/local/bin/python3"],
                                "args": [
                                    "/builder/artifact_variant.py",
                                    "--checkpoints-root",
                                    "/checkpoints",
                                    "--source-manifest-sha256",
                                    source_manifest_sha256,
                                    "--source-file-count",
                                    str(source_file_count),
                                    "--source-total-bytes",
                                    str(source_total_bytes),
                                    "--receipt",
                                    "/tmp/buffered-build-receipt.json",
                                ],
                                "resources": {
                                    "requests": {"cpu": "1", "memory": "256Mi"},
                                    "limits": {"cpu": "2", "memory": "1Gi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True,
                                },
                                "volumeMounts": [
                                    {"name": "builder", "mountPath": "/builder", "readOnly": True},
                                    {"name": "checkpoints", "mountPath": "/checkpoints"},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "builder", "configMap": {"name": name, "defaultMode": 292}},
                            {
                                "name": "checkpoints",
                                "persistentVolumeClaim": {
                                    "claimName": PROFILE["storage"]["artifact_pvc"]
                                },
                            },
                            {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}},
                        ],
                    },
                },
            },
        },
    ]


def validate_documents(documents: list[dict[str, Any]]) -> None:
    rendered = json.dumps(documents, sort_keys=True)
    if "@@" in rendered or "REPLACE" in rendered or ":latest" in rendered:
        raise CaptureRenderError("render contains a placeholder or mutable image")
    if FIXTURE_SHA256 != PROFILE["semantic_profile"]["fixture_sha256"]:
        raise CaptureRenderError("1UBQ fixture digest differs from the profile")
    for document in documents:
        metadata = document.get("metadata", {})
        if document.get("kind") != "PodSnapshotContent" and metadata.get("namespace") != NAMESPACE:
            raise CaptureRenderError("namespaced capture object has the wrong namespace")
    for pod in [item for item in documents if item.get("kind") == "Pod"]:
        if "nodeName" in pod.get("spec", {}):
            raise CaptureRenderError("capture Pods must use scheduler affinity")
    for job in [item for item in documents if item.get("kind") == "Job"]:
        if (
            job.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
            != "checkpoint-donor"
        ):
            continue
        spec = job["spec"]["template"]["spec"]
        volumes = {item["name"]: item for item in spec.get("volumes", [])}
        for name, volume in volumes.items():
            sources = set(volume) - {"name"}
            if sources == {"emptyDir"}:
                if not volume["emptyDir"].get("sizeLimit"):
                    raise CaptureRenderError(f"donor emptyDir {name} is unbounded")
            elif sources == {"persistentVolumeClaim"}:
                if not volume["persistentVolumeClaim"].get("claimName"):
                    raise CaptureRenderError(f"donor PVC {name} has no claim")
            else:
                raise CaptureRenderError(
                    f"donor volume {name} is not d5ce-compatible emptyDir/PVC"
                )
        main = spec["containers"][0]
        validator_mounts = [
            item for item in main.get("volumeMounts", []) if item.get("name") == "validator"
        ]
        if validator_mounts != [
            {"name": "validator", "mountPath": "/validator", "readOnly": True}
        ]:
            raise CaptureRenderError("donor validator mount is not exact")
        if volumes.get("validator") != {
            "name": "validator",
            "emptyDir": {"sizeLimit": PROFILE["runtime_topology"]["validator_size_limit"]},
        }:
            raise CaptureRenderError("donor validator volume is not the exact bounded emptyDir")


def dump_documents(documents: Iterable[dict[str, Any]]) -> None:
    yaml.safe_dump_all(
        list(documents), sys.stdout, explicit_start=True, sort_keys=False, default_flow_style=False
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("storage")
    for mode in ("agent", "donor", "cache-populator"):
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
    builder = subparsers.add_parser("variant-builder")
    builder.add_argument("--capture-id", required=True)
    builder.add_argument("--source-manifest-sha256", required=True)
    builder.add_argument("--source-file-count", type=int, required=True)
    builder.add_argument("--source-total-bytes", type=int, required=True)
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
            elif args.mode == "cache-populator":
                documents = render_cache_populator(capture_id)
            elif args.mode == "content":
                documents = render_content(capture_id, args.source_pod, args.source_uid)
            elif args.mode == "variant-builder":
                documents = render_variant_builder(
                    capture_id,
                    args.source_manifest_sha256,
                    args.source_file_count,
                    args.source_total_bytes,
                )
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
        print(f"render-rfdiffusion-capture: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
