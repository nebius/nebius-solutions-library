#!/usr/bin/env python3
"""Render production-shaped MolMIM conventional-cached target and early probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "nim-fast-start"
NODE = "gpu-node-b.example.invalid"
IMAGE = (
    "nvcr.io/nim/nvidia/molmim@sha256:"
    "7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
)
VALIDATOR = ROOT / "validate_molmim.py"
FIXTURE = ROOT / "fixtures" / "request-cmaes-qed.json"
VALIDATOR_SHA256 = "0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7"
FIXTURE_SHA256 = "053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d"
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class RenderError(ValueError):
    """A conventional-cached run input is not the frozen contract."""


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RenderError("demand timestamp must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RenderError("demand timestamp must be RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise RenderError("demand timestamp must be RFC3339 UTC")
    return value


def _run_id(value: str) -> str:
    if not isinstance(value, str) or len(value) > 28 or DNS_LABEL.fullmatch(value) is None:
        raise RenderError("run ID must be a DNS label of at most 28 characters")
    return value


def _uid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RenderError("target UID must be canonical UUID text") from exc
    if str(parsed) != value:
        raise RenderError("target UID must be canonical UUID text")
    return value


def _read_pinned(path: Path, expected: str, label: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RenderError(f"cannot read {label}: {type(exc).__name__}") from exc
    if path.is_symlink() or not path.is_file() or hashlib.sha256(raw).hexdigest() != expected:
        raise RenderError(f"{label} does not match its pinned digest")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RenderError(f"{label} is not UTF-8") from exc


def names(run_id: str) -> dict[str, str]:
    run_id = _run_id(run_id)
    return {
        "target": f"molmim-cached-{run_id}",
        "service": f"molmim-cached-svc-{run_id}",
        "probe": f"molmim-cached-probe-{run_id}",
        "target_policy": f"molmim-cached-target-{run_id}",
        "probe_policy": f"molmim-cached-probe-{run_id}",
    }


def render_target(run_id: str, demand_at: str) -> list[dict[str, Any]]:
    run_id = _run_id(run_id)
    demand_at = _timestamp(demand_at)
    value = names(run_id)
    labels = {
        "app.kubernetes.io/name": "molmim",
        "app.kubernetes.io/component": "conventional-cached-target",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
        "archvteams.nebius.ai/run-id": run_id,
    }
    affinity = {
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
    }
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": value["target"],
            "namespace": NAMESPACE,
            "labels": labels,
            "annotations": {
                "archvteams.nebius.ai/demand-at": demand_at,
                "archvteams.nebius.ai/startup-mode": "conventional-cached",
                "linkerd.io/inject": "disabled",
                "sidecar.istio.io/inject": "false",
            },
        },
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "runtimeClassName": "nvidia",
            "affinity": affinity,
            "terminationGracePeriodSeconds": 5,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "molmim",
                    "image": IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/opt/nvidia/nvidia_entrypoint.sh"],
                    "args": ["start_server"],
                    "env": [
                        {"name": "NIM_CACHE_PATH", "value": "/home/nvs/.cache/nim"},
                        {"name": "TORCHINDUCTOR_COMPILE_THREADS", "value": "1"},
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
                    "resources": {
                        "requests": {"cpu": "4", "memory": "32Gi", "nvidia.com/gpu": "1"},
                        "limits": {"cpu": "5", "memory": "40Gi", "nvidia.com/gpu": "1"},
                    },
                    "securityContext": {
                        "privileged": False,
                        "allowPrivilegeEscalation": False,
                        "runAsUser": 0,
                        "runAsGroup": 0,
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/v1/health/ready", "port": "http"},
                        "periodSeconds": 1,
                        "timeoutSeconds": 1,
                        "failureThreshold": 900,
                    },
                    "volumeMounts": [
                        {"name": "dshm", "mountPath": "/dev/shm"},
                        {
                            "name": "nim-cache",
                            "mountPath": "/home/nvs/.cache/nim",
                        },
                    ],
                }
            ],
            "volumes": [
                {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}},
                {
                    "name": "nim-cache",
                    "persistentVolumeClaim": {
                        "claimName": "molmim-native-f7-cache",
                    },
                },
            ],
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": value["service"],
            "namespace": NAMESPACE,
            "labels": labels,
        },
        "spec": {
            "type": "ClusterIP",
            "publishNotReadyAddresses": True,
            "selector": {
                "app.kubernetes.io/name": "molmim",
                "app.kubernetes.io/component": "conventional-cached-target",
                "archvteams.nebius.ai/run-id": run_id,
            },
            "ports": [{"name": "http", "port": 8000, "targetPort": "http", "protocol": "TCP"}],
        },
    }
    target_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": value["target_policy"], "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/component": "conventional-cached-target",
                    "archvteams.nebius.ai/run-id": run_id,
                }
            },
                "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/component": "conventional-cached-probe",
                                    "archvteams.nebius.ai/run-id": run_id,
                                }
                            }
                        }
                    ],
                    "ports": [{"port": 8000, "protocol": "TCP"}],
                }
            ],
        },
    }
    return [pod, service, target_policy]


def render_probe(run_id: str, demand_at: str, target_uid: str) -> list[dict[str, Any]]:
    run_id = _run_id(run_id)
    demand_at = _timestamp(demand_at)
    target_uid = _uid(target_uid)
    value = names(run_id)
    validator_source = _read_pinned(VALIDATOR, VALIDATOR_SHA256, "validator")
    fixture_source = _read_pinned(FIXTURE, FIXTURE_SHA256, "fixture")
    labels = {
        "app.kubernetes.io/name": "molmim",
        "app.kubernetes.io/component": "conventional-cached-probe",
        "app.kubernetes.io/part-of": "archvteams-2407-faststart",
        "archvteams.nebius.ai/run-id": run_id,
    }
    annotations = {
        "archvteams.nebius.ai/demand-at": demand_at,
        "archvteams.nebius.ai/target-pod": value["target"],
        "archvteams.nebius.ai/target-pod-uid": target_uid,
        "archvteams.nebius.ai/validator-sha256": VALIDATOR_SHA256,
        "archvteams.nebius.ai/fixture-sha256": FIXTURE_SHA256,
        "archvteams.nebius.ai/startup-mode": "conventional-cached",
    }
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": value["probe"],
            "namespace": NAMESPACE,
            "labels": labels,
            "annotations": annotations,
        },
        "immutable": True,
        "data": {
            "validate_molmim.py": validator_source,
            "request-cmaes-qed.json": fixture_source,
        },
    }
    stage_source = f'''import hashlib
import os
from pathlib import Path

expected = {{
    "validate_molmim.py": "{VALIDATOR_SHA256}",
    "request-cmaes-qed.json": "{FIXTURE_SHA256}",
}}
os.umask(0o077)
for name, digest in expected.items():
    data = (Path("/source") / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit("validator input digest mismatch")
    descriptor = os.open(
        Path("/validator") / name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
'''
    affinity = {
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
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": value["probe"],
            "namespace": NAMESPACE,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 660,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": {
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 1,
                    "affinity": affinity,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65534,
                        "runAsGroup": 65534,
                        "fsGroup": 65534,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [
                        {
                            "name": "stage-validator",
                            "image": IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/usr/bin/python3"],
                            "args": ["-c", stage_source],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "128Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "volumeMounts": [
                                {"name": "source", "mountPath": "/source", "readOnly": True},
                                {"name": "validator", "mountPath": "/validator"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "semantic-probe",
                            "image": IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/usr/bin/python3"],
                            "args": [
                                "/validator/validate_molmim.py",
                                "--base-url",
                                f"http://{value['service']}:8000",
                                "--request-file",
                                "/validator/request-cmaes-qed.json",
                                "--receipt-dir",
                                "/evidence/semantic",
                                "--run-id",
                                f"{run_id}-semantic-a",
                                "--run-id",
                                f"{run_id}-semantic-b",
                                "--ready-timeout",
                                "300",
                                "--timeout",
                                "300",
                            ],
                            "env": [
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "PYTHONUNBUFFERED", "value": "1"},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "2Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "volumeMounts": [
                                {"name": "validator", "mountPath": "/validator", "readOnly": True},
                                {"name": "evidence", "mountPath": "/evidence"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "source",
                            "configMap": {"name": value["probe"], "defaultMode": 0o444},
                        },
                        {"name": "validator", "emptyDir": {"sizeLimit": "1Mi"}},
                        {"name": "evidence", "emptyDir": {"sizeLimit": "40Mi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}},
                    ],
                },
            },
        },
    }
    probe_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": value["probe_policy"], "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/component": "conventional-cached-probe",
                    "archvteams.nebius.ai/run-id": run_id,
                }
            },
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/component": "conventional-cached-target",
                                    "archvteams.nebius.ai/run-id": run_id,
                                }
                            }
                        }
                    ],
                    "ports": [{"port": 8000, "protocol": "TCP"}],
                },
                {"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
            ],
        },
    }
    return [config, job, probe_policy]


def _dump(documents: list[dict[str, Any]]) -> None:
    yaml.safe_dump_all(documents, sys.stdout, explicit_start=True, sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    target = subparsers.add_parser("target")
    target.add_argument("--run-id", required=True)
    target.add_argument("--demand-at", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--run-id", required=True)
    probe.add_argument("--demand-at", required=True)
    probe.add_argument("--target-uid", required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "target":
            documents = render_target(args.run_id, args.demand_at)
        else:
            documents = render_probe(args.run_id, args.demand_at, args.target_uid)
        _dump(documents)
    except RenderError as exc:
        print(f"conventional render refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
