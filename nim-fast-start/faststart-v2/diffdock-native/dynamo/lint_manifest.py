#!/usr/bin/env python3
"""Static safety checks for rendered DiffDock fast-start manifests."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment failure
    raise SystemExit("lint-manifest: PyYAML is required") from exc


NAMESPACE = "nim-fast-start"
NIM_IMAGE = (
    "nvcr.io/nim/mit/diffdock@sha256:"
    "300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480"
)
RUN_LABEL = "archvteams.nebius.ai/run-id"
COMPONENT_LABEL = "app.kubernetes.io/component"
QUALIFIED_LABEL = "archvteams.nebius.ai/semantic-qualified"
POD_SPEC_HASH_KEY = "archvteams.nebius.ai/target-pod-spec-sha256"
VALIDATOR_SHA256 = "245ae98a98db09c34924cd7a499b99da9eb35742667043aaee3e497c33268008"
FIXTURE_SHA256 = "f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977"
PROBE_EXECUTABLE = "/usr/local/bin/python3"
STAGER_SHA256 = "21c6d235cefb6f215ad3f97f7d763d5bb86b0ba2175efe30db9fba51e5c82c2b"
PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CONTAINER_ID = re.compile(r"^containerd://[0-9a-f]{64}$")
ALLOWED_HOST_PATHS = {
    "/run/containerd/containerd.sock",
    "/proc",
    "/sys/fs/cgroup",
    "/var/lib/kubelet/pod-resources/kubelet.sock",
}
ALLOWED_H100_NODES = {
    "gpu-node-a.example.invalid",
}
FORBIDDEN_ENV_VALUE_MARKERS = ("nvapi-", "NGC_API_KEY=", "NVIDIA_API_KEY=")
KNOWN_NON_WORKER_EXECUTABLES = {
    "/usr/local/bin/snapshot-agent",
    "/usr/local/bin/nsrestore",
    "/snapshot-binaries/nsrestore",
}
REQUIRED_BINDING_FLAGS = {
    "--target-namespace",
    "--target-name",
    "--target-uid",
    "--target-container",
    "--target-container-id",
    "--target-cgroup",
    "--target-pod-ip",
    "--target-node",
    "--target-pod-spec-sha256",
    "--expected-image-id",
    "--run-id",
    "--checkpoint-id",
    "--artifact-version",
    "--artifact-manifest-sha256",
    "--tool-bundle-sha256",
    "--container-runtime-socket",
    "--host-proc",
    "--host-cgroup",
    "--pod-resources-socket",
}


def _metadata(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _labels(document: dict[str, Any]) -> dict[str, Any]:
    labels = _metadata(document).get("labels")
    return labels if isinstance(labels, dict) else {}


def _pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    kind = document.get("kind")
    if kind == "Pod":
        spec = document.get("spec")
        return spec if isinstance(spec, dict) else None
    if kind == "Job":
        try:
            spec = document["spec"]["template"]["spec"]
        except (KeyError, TypeError):
            return None
        return spec if isinstance(spec, dict) else None
    return None


def _containers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("initContainers", "containers", "ephemeralContainers"):
        items = spec.get(key, [])
        if isinstance(items, list):
            result.extend(item for item in items if isinstance(item, dict))
    return result


def _hostname_values(spec: dict[str, Any]) -> list[str]:
    try:
        terms = spec["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
    except (KeyError, TypeError):
        return []
    values: list[str] = []
    if not isinstance(terms, list):
        return values
    for term in terms:
        if not isinstance(term, dict):
            continue
        expressions = term.get("matchExpressions", [])
        if not isinstance(expressions, list):
            continue
        for expression in expressions:
            if (
                isinstance(expression, dict)
                and expression.get("key") == "kubernetes.io/hostname"
                and expression.get("operator") == "In"
                and isinstance(expression.get("values"), list)
            ):
                values.extend(str(item) for item in expression["values"])
    return values


def _find_volume(spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    for volume in spec.get("volumes", []):
        if isinstance(volume, dict) and volume.get("name") == name:
            return volume
    return None


def _find_mount(container: dict[str, Any], name: str, mount_path: str | None = None) -> dict[str, Any] | None:
    for mount in container.get("volumeMounts", []):
        if not isinstance(mount, dict) or mount.get("name") != name:
            continue
        if mount_path is None or mount.get("mountPath") == mount_path:
            return mount
    return None


def _check_no_secret_env(
    container: dict[str, Any], location: str, errors: list[str]
) -> None:
    if container.get("envFrom"):
        errors.append(f"{location} must not use envFrom")
    for env in container.get("env", []):
        if not isinstance(env, dict):
            errors.append(f"{location} has malformed env entry")
            continue
        name = str(env.get("name", ""))
        if any(marker in name.upper() for marker in ("SECRET", "PASSWORD", "API_KEY", "TOKEN")):
            errors.append(f"{location} exposes secret-like environment variable {name}")
        value_from = env.get("valueFrom")
        if value_from is not None:
            errors.append(f"{location} uses forbidden valueFrom environment data")
        value = env.get("value")
        if isinstance(value, str) and any(marker in value for marker in FORBIDDEN_ENV_VALUE_MARKERS):
            errors.append(f"{location} contains a forbidden credential marker in environment data")


def _volume_names(spec: dict[str, Any], location: str, errors: list[str]) -> list[str]:
    names: list[str] = []
    volumes = spec.get("volumes")
    if not isinstance(volumes, list):
        errors.append(f"{location} volumes must be a list")
        return names
    for volume in volumes:
        if not isinstance(volume, dict) or not isinstance(volume.get("name"), str):
            errors.append(f"{location} has a malformed named volume")
            continue
        names.append(volume["name"])
    if len(names) != len(set(names)):
        errors.append(f"{location} contains duplicate volume names")
    return names


def _mount_signatures(container: dict[str, Any], location: str, errors: list[str]) -> list[tuple[Any, ...]]:
    signatures: list[tuple[Any, ...]] = []
    mounts = container.get("volumeMounts")
    if not isinstance(mounts, list):
        errors.append(f"{location} volumeMounts must be a list")
        return signatures
    for mount in mounts:
        if not isinstance(mount, dict):
            errors.append(f"{location} has a malformed volume mount")
            continue
        signatures.append(
            (mount.get("name"), mount.get("mountPath"), mount.get("subPath"), mount.get("readOnly", False))
        )
    return signatures


def _check_images_and_common_pod_safety(
    document: dict[str, Any], spec: dict[str, Any], errors: list[str]
) -> None:
    location = f"{document.get('kind')}/{_metadata(document).get('name', '?')}"
    if "nodeName" in spec:
        errors.append(f"{location} must not set spec.nodeName")
    if spec.get("imagePullSecrets") not in (None, []):
        errors.append(f"{location} must not reference image-pull secrets")
    tolerations = spec.get("tolerations", [])
    if not isinstance(tolerations, list):
        errors.append(f"{location} tolerations must be a list")
    else:
        for toleration in tolerations:
            if not isinstance(toleration, dict):
                errors.append(f"{location} has malformed toleration")
            elif not toleration.get("key") or toleration.get("operator") == "Exists":
                errors.append(f"{location} has a broad toleration")

    for volume in spec.get("volumes", []):
        if not isinstance(volume, dict):
            errors.append(f"{location} has a malformed volume")
            continue
        if "secret" in volume:
            errors.append(f"{location} has a forbidden Secret volume")
        projected = volume.get("projected")
        if isinstance(projected, dict):
            for source in projected.get("sources", []):
                if isinstance(source, dict) and (
                    "secret" in source or "serviceAccountToken" in source
                ):
                    errors.append(f"{location} has a forbidden projected secret or token")
        csi = volume.get("csi")
        if isinstance(csi, dict) and csi.get("driver") == "secrets-store.csi.k8s.io":
            errors.append(f"{location} has a forbidden secret-store CSI volume")
        host_path = volume.get("hostPath")
        if isinstance(host_path, dict):
            path = host_path.get("path")
            if path not in ALLOWED_HOST_PATHS:
                errors.append(f"{location} has unapproved hostPath {path!r}")

    for index, container in enumerate(_containers(spec)):
        container_location = f"{location} container[{index}]"
        image = container.get("image")
        if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
            errors.append(f"{container_location} image is not pinned by @sha256")
        _check_no_secret_env(container, container_location, errors)
        flattened = [container.get("command"), container.get("args")]
        if "--empty-ns" in " ".join(str(item) for item in flattened):
            errors.append(f"{container_location} uses forbidden --empty-ns")


def _check_read_only_claim(
    spec: dict[str, Any], container: dict[str, Any], name: str, errors: list[str], location: str
) -> None:
    volume = _find_volume(spec, name)
    if not volume or not isinstance(volume.get("persistentVolumeClaim"), dict):
        errors.append(f"{location} is missing {name} PVC")
        return
    if volume["persistentVolumeClaim"].get("readOnly") is not True:
        errors.append(f"{location} {name} PVC must be read-only")
    mount = _find_mount(container, name)
    if not mount or mount.get("readOnly") is not True:
        errors.append(f"{location} {name} mount must be read-only")


def _check_target(document: dict[str, Any], errors: list[str]) -> None:
    spec = _pod_spec(document)
    location = f"Pod/{_metadata(document).get('name', '?')}"
    if spec is None:
        errors.append(f"{location} has no pod spec")
        return
    if spec.get("automountServiceAccountToken") is not False:
        errors.append(f"{location} must be tokenless")
    if spec.get("enableServiceLinks") is not False:
        errors.append(f"{location} must disable service links")
    if spec.get("initContainers") not in (None, []):
        errors.append(f"{location} must not contain init containers")
    if spec.get("ephemeralContainers") not in (None, []):
        errors.append(f"{location} must not contain ephemeral containers")
    if spec.get("runtimeClassName") != "nvidia":
        errors.append(f"{location} must use runtimeClassName nvidia")
    for field in ("hostPID", "hostIPC", "hostNetwork"):
        if spec.get(field) is not False:
            errors.append(f"{location} must set {field}: false")
    hostnames = _hostname_values(spec)
    if len(hostnames) != 1 or hostnames[0] not in ALLOWED_H100_NODES:
        errors.append(f"{location} must select one exact allowed H100 hostname")
    if spec.get("tolerations"):
        errors.append(f"{location} must not carry tolerations")

    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        errors.append(f"{location} must contain exactly one target container")
        return
    container = containers[0]
    if not isinstance(container, dict):
        errors.append(f"{location} target container is malformed")
        return
    if container.get("name") != "diffdock" or container.get("image") != NIM_IMAGE:
        errors.append(f"{location} must use the exact pinned DiffDock image")
    security = container.get("securityContext", {})
    if not isinstance(security, dict):
        errors.append(f"{location} target securityContext is malformed")
        security = {}
    if security.get("privileged") is not False:
        errors.append(f"{location} target must explicitly be nonprivileged")
    if security.get("allowPrivilegeEscalation") is not False:
        errors.append(f"{location} target must deny privilege escalation")
    capabilities = security.get("capabilities", {})
    if not isinstance(capabilities, dict) or capabilities.get("drop") != ["ALL"]:
        errors.append(f"{location} target must drop all Linux capabilities")
    if container.get("command") != ["/bin/sleep"] or container.get("args") != ["2147483647"]:
        errors.append(f"{location} target command must be the inert placeholder")
    resources = container.get("resources", {})
    requests = resources.get("requests", {}) if isinstance(resources, dict) else {}
    limits = resources.get("limits", {}) if isinstance(resources, dict) else {}
    if requests != {"cpu": "8", "memory": "64Gi", "nvidia.com/gpu": "1"}:
        errors.append(f"{location} target requests do not match the capture envelope")
    if limits != {"cpu": "15", "memory": "160Gi", "nvidia.com/gpu": "1"}:
        errors.append(f"{location} target limits do not match the capture envelope")
    expected_env = [
        {"name": "DYN_SNAPSHOT_RESTORE_STANDBY", "value": "1"},
        {"name": "DYN_SNAPSHOT_CONTROL_DIR", "value": "/snapshot-control"},
        {"name": "NIM_CACHE_PATH", "value": "/opt/nim/.cache"},
    ]
    if container.get("env") != expected_env:
        errors.append(f"{location} target environment is not the exact approved set")

    dshm = _find_volume(spec, "dshm")
    empty = dshm.get("emptyDir", {}) if dshm else {}
    if empty.get("medium") != "Memory" or empty.get("sizeLimit") != "16Gi":
        errors.append(f"{location} must provide a 16Gi memory-backed /dev/shm")
    if not _find_mount(container, "dshm", "/dev/shm"):
        errors.append(f"{location} does not mount dshm at /dev/shm")
    for name in ("checkpoints", "nim-cache"):
        _check_read_only_claim(spec, container, name, errors, location)
    for volume in spec.get("volumes", []):
        if isinstance(volume, dict) and "hostPath" in volume:
            errors.append(f"{location} target must not use hostPath volumes")
    expected_volume_names = {
        "dshm", "nim-cache", "workspace", "output", "snapshot-control", "checkpoints"
    }
    if set(_volume_names(spec, location, errors)) != expected_volume_names:
        errors.append(f"{location} target volume set is not exact")
    expected_mounts = {
        ("dshm", "/dev/shm", None, False),
        ("nim-cache", "/opt/nim/.cache", None, True),
        ("workspace", "/opt/nim/workspace", None, False),
        ("output", "/output", None, False),
        ("snapshot-control", "/snapshot-control", "diffdock", False),
        ("checkpoints", "/checkpoints", None, True),
    }
    if set(_mount_signatures(container, location, errors)) != expected_mounts:
        errors.append(f"{location} target volume-mount set is not exact")

    labels = _labels(document)
    if labels.get(QUALIFIED_LABEL) != "false":
        errors.append(f"{location} must start semantically unqualified")
    if labels.get("nvidia.com/snapshot-is-restore-target") != "true":
        errors.append(f"{location} is missing the native Dynamo restore label")
    annotations = _metadata(document).get("annotations", {})
    if not isinstance(annotations, dict) or annotations.get(
        "nvidia.com/snapshot-target-containers"
    ) != "diffdock":
        errors.append(f"{location} is missing the exact target-container annotation")


def _arguments_by_flag(arguments: Any) -> dict[str, str]:
    if not isinstance(arguments, list) or not arguments or arguments[0] != "restore":
        return {}
    result: dict[str, str] = {}
    index = 1
    while index < len(arguments):
        flag = arguments[index]
        if not isinstance(flag, str) or not flag.startswith("--") or index + 1 >= len(arguments):
            return {}
        value = arguments[index + 1]
        if not isinstance(value, str) or flag in result:
            return {}
        result[flag] = value
        index += 2
    return result


def _check_worker(document: dict[str, Any], errors: list[str]) -> None:
    spec = _pod_spec(document)
    location = f"Job/{_metadata(document).get('name', '?')}"
    if spec is None:
        errors.append(f"{location} has no pod template spec")
        return
    if spec.get("hostPID") is not True:
        errors.append(f"{location} requires hostPID")
    if spec.get("hostNetwork") is not False or spec.get("hostIPC") is not False:
        errors.append(f"{location} must not use host network or host IPC")
    if spec.get("initContainers") not in (None, []):
        errors.append(f"{location} must not contain init containers")
    if spec.get("ephemeralContainers") not in (None, []):
        errors.append(f"{location} must not contain ephemeral containers")
    if spec.get("runtimeClassName") != "nvidia":
        errors.append(f"{location} must use runtimeClassName nvidia")
    if spec.get("automountServiceAccountToken") is not True:
        errors.append(f"{location} must explicitly use its run-scoped service account token")
    if not spec.get("serviceAccountName"):
        errors.append(f"{location} is missing serviceAccountName")
    if spec.get("tolerations"):
        errors.append(f"{location} must not carry tolerations")
    hostnames = _hostname_values(spec)
    if len(hostnames) != 1 or hostnames[0] not in ALLOWED_H100_NODES:
        errors.append(f"{location} must select one exact allowed H100 hostname")

    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        errors.append(f"{location} must contain exactly one restore worker")
        return
    container = containers[0]
    if not isinstance(container, dict):
        errors.append(f"{location} restore worker is malformed")
        return
    security = container.get("securityContext", {})
    if not isinstance(security, dict):
        errors.append(f"{location} restore-worker securityContext is malformed")
        security = {}
    if security.get("privileged") is not True:
        errors.append(f"{location} restore worker must explicitly be privileged")
    if security.get("readOnlyRootFilesystem") is not True:
        errors.append(f"{location} restore worker root filesystem must be read-only")
    if container.get("env") not in (None, []):
        errors.append(f"{location} restore worker must not have environment variables")
    command = container.get("command")
    if (
        not isinstance(command, list)
        or len(command) != 1
        or not isinstance(command[0], str)
        or not command[0].startswith("/")
        or command[0] in {"/bin/sh", "/bin/bash", "/usr/bin/env"}
    ):
        errors.append(f"{location} must invoke one absolute non-shell executable")
    elif command[0] in KNOWN_NON_WORKER_EXECUTABLES:
        errors.append(
            f"{location} invokes a known Dynamo daemon or unbound low-level helper, "
            "not a one-shot restore worker"
        )
    arguments = _arguments_by_flag(container.get("args"))
    if set(arguments) != REQUIRED_BINDING_FLAGS:
        missing = sorted(REQUIRED_BINDING_FLAGS - set(arguments))
        extra = sorted(set(arguments) - REQUIRED_BINDING_FLAGS)
        errors.append(f"{location} restore binding flags mismatch; missing={missing}, extra={extra}")
    if not UUID.fullmatch(arguments.get("--target-uid", "")):
        errors.append(f"{location} has no canonical target UID binding")
    if not CONTAINER_ID.fullmatch(arguments.get("--target-container-id", "")):
        errors.append(f"{location} has no full container ID binding")
    if not arguments.get("--target-cgroup", "").startswith("/kubepods"):
        errors.append(f"{location} has no exact kubepods cgroup binding")
    if arguments.get("--target-node") not in hostnames:
        errors.append(f"{location} target-node binding does not match hostname affinity")
    if arguments.get("--run-id") != _labels(document).get(RUN_LABEL):
        errors.append(f"{location} run-id binding does not match Job label")

    annotations = _metadata(document).get("annotations", {})
    if not isinstance(annotations, dict):
        annotations = {}
    annotation_pairs = {
        "archvteams.nebius.ai/target-pod-uid": "--target-uid",
        "archvteams.nebius.ai/target-container-id": "--target-container-id",
        "archvteams.nebius.ai/target-cgroup": "--target-cgroup",
        "archvteams.nebius.ai/target-pod-ip": "--target-pod-ip",
        "archvteams.nebius.ai/target-node": "--target-node",
        POD_SPEC_HASH_KEY: "--target-pod-spec-sha256",
    }
    for annotation, flag in annotation_pairs.items():
        if not annotations.get(annotation) or annotations.get(annotation) != arguments.get(flag):
            errors.append(f"{location} {annotation} does not match {flag}")
    if not re.fullmatch(r"[0-9a-f]{64}", arguments.get("--target-pod-spec-sha256", "")):
        errors.append(f"{location} has no canonical target PodSpec hash binding")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        annotations.get("archvteams.nebius.ai/worker-executable-sha256", ""),
    ):
        errors.append(f"{location} has no canonical worker executable digest")

    resources = container.get("resources", {})
    if resources != {
        "requests": {"cpu": "1", "memory": "1Gi"},
        "limits": {"cpu": "4", "memory": "4Gi"},
    }:
        errors.append(f"{location} restore-worker resources are not the exact 1CPU contract")
    for group in ("requests", "limits"):
        if isinstance(resources, dict) and "nvidia.com/gpu" in resources.get(group, {}):
            errors.append(f"{location} worker must not reserve the target GPU")
    for name in ("checkpoints",):
        _check_read_only_claim(spec, container, name, errors, location)
    host_paths = {
        volume.get("hostPath", {}).get("path")
        for volume in spec.get("volumes", [])
        if isinstance(volume, dict) and isinstance(volume.get("hostPath"), dict)
    }
    if host_paths != ALLOWED_HOST_PATHS:
        errors.append(f"{location} hostPath set is not the narrow approved set")
    expected_volume_names = {
        "runtime-socket", "host-proc", "host-cgroup", "pod-resources",
        "checkpoints", "scratch",
    }
    if set(_volume_names(spec, location, errors)) != expected_volume_names:
        errors.append(f"{location} restore-worker volume set is not exact")
    expected_mounts = {
        ("runtime-socket", "/run/containerd/containerd.sock", None, False),
        ("host-proc", "/host/proc", None, False),
        ("host-cgroup", "/sys/fs/cgroup", None, False),
        ("pod-resources", "/var/lib/kubelet/pod-resources/kubelet.sock", None, True),
        ("checkpoints", "/checkpoints", None, True),
        ("scratch", "/tmp", None, False),
    }
    if set(_mount_signatures(container, location, errors)) != expected_mounts:
        errors.append(f"{location} restore-worker volume-mount set is not exact")

    job_spec = document.get("spec", {})
    if (
        not isinstance(job_spec, dict)
        or job_spec.get("backoffLimit") != 0
        or not isinstance(job_spec.get("activeDeadlineSeconds"), int)
        or job_spec["activeDeadlineSeconds"] > 900
    ):
        errors.append(f"{location} must be a bounded, no-retry Job")


def _check_services(documents: list[dict[str, Any]], run_id: str, errors: list[str]) -> None:
    services = [document for document in documents if document.get("kind") == "Service"]
    if len(services) != 2:
        errors.append("target manifest must contain exactly two Services")
        return
    components = {_labels(service).get(COMPONENT_LABEL): service for service in services}
    canary = components.get("canary-service")
    qualified = components.get("qualified-service")
    if canary is None or qualified is None:
        errors.append("target manifest must contain canary-service and qualified-service")
        return
    for service in services:
        name = _metadata(service).get("name", "?")
        spec = service.get("spec", {})
        if not isinstance(spec, dict) or spec.get("type") != "ClusterIP":
            errors.append(f"Service/{name} must be ClusterIP")
            continue
        for forbidden in ("externalIPs", "externalName", "loadBalancerClass"):
            if spec.get(forbidden):
                errors.append(f"Service/{name} must not set {forbidden}")
        if any(isinstance(port, dict) and port.get("nodePort") for port in spec.get("ports", [])):
            errors.append(f"Service/{name} must not expose nodePort")
        selector = spec.get("selector", {})
        if not isinstance(selector, dict) or selector.get(RUN_LABEL) != run_id:
            errors.append(f"Service/{name} selector is not bound to the run ID")
    if canary.get("spec", {}).get("publishNotReadyAddresses") is not True:
        errors.append("canary Service must publish not-ready addresses")
    qualified_spec = qualified.get("spec", {})
    if qualified_spec.get("publishNotReadyAddresses") is not False:
        errors.append("qualified Service must obey readiness")
    if qualified_spec.get("selector", {}).get(QUALIFIED_LABEL) != "true":
        errors.append("qualified Service must select only semantically qualified targets")


def _check_network_policies(
    documents: list[dict[str, Any]], run_id: str, errors: list[str]
) -> None:
    policies = [document for document in documents if document.get("kind") == "NetworkPolicy"]
    if len(policies) != 2:
        errors.append("target manifest must contain exactly two NetworkPolicies")
        return
    by_component: dict[str, dict[str, Any]] = {}
    for policy in policies:
        try:
            component = policy["spec"]["podSelector"]["matchLabels"][COMPONENT_LABEL]
        except (KeyError, TypeError):
            component = ""
        if not isinstance(component, str) or component in by_component:
            errors.append("target NetworkPolicies must have unique exact component selectors")
            continue
        by_component[component] = policy

    target = by_component.get("restore-target")
    probe = by_component.get("semantic-probe")
    if target is None or probe is None:
        errors.append("target NetworkPolicies must select restore-target and semantic-probe")
        return
    expected_target = {
        "podSelector": {"matchLabels": {
            COMPONENT_LABEL: "restore-target", RUN_LABEL: run_id,
        }},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [{
            "from": [{"podSelector": {"matchLabels": {
                COMPONENT_LABEL: "semantic-probe", RUN_LABEL: run_id,
            }}}],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        }],
        "egress": [],
    }
    expected_probe = {
        "podSelector": {"matchLabels": {
            COMPONENT_LABEL: "semantic-probe", RUN_LABEL: run_id,
        }},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "to": [{"podSelector": {"matchLabels": {
                    COMPONENT_LABEL: "restore-target", RUN_LABEL: run_id,
                }}}],
                "ports": [{"port": 8000, "protocol": "TCP"}],
            },
            {"ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ]},
        ],
    }
    if target.get("spec") != expected_target:
        errors.append("restore-target NetworkPolicy is not the exact deny-by-default probe-only policy")
    if probe.get("spec") != expected_probe:
        errors.append("semantic-probe NetworkPolicy is not the exact target-and-DNS-only policy")


def _check_rbac(documents: list[dict[str, Any]], target_name: str, errors: list[str]) -> None:
    roles = [document for document in documents if document.get("kind") == "Role"]
    bindings = [document for document in documents if document.get("kind") == "RoleBinding"]
    accounts = [document for document in documents if document.get("kind") == "ServiceAccount"]
    if len(roles) != 1 or len(bindings) != 1 or len(accounts) != 1:
        errors.append("restore manifest must contain one ServiceAccount, Role, and RoleBinding")
        return
    rules = roles[0].get("rules")
    expected_rule = {
        "apiGroups": [""],
        "resources": ["pods"],
        "resourceNames": [target_name],
        "verbs": ["get", "patch", "update"],
    }
    if rules != [expected_rule]:
        errors.append("restore Role is not limited to get/patch/update on the exact target Pod")
    if accounts[0].get("automountServiceAccountToken") is not True:
        errors.append("restore ServiceAccount must explicitly enable its token")
    account_name = _metadata(accounts[0]).get("name")
    role_name = _metadata(roles[0]).get("name")
    expected_role_ref = {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role_name,
    }
    expected_subjects = [{
        "kind": "ServiceAccount",
        "name": account_name,
        "namespace": NAMESPACE,
    }]
    if bindings[0].get("roleRef") != expected_role_ref:
        errors.append("restore RoleBinding does not reference the exact run-scoped Role")
    if bindings[0].get("subjects") != expected_subjects:
        errors.append("restore RoleBinding subjects are not the exact run-scoped ServiceAccount")


def _check_probe(documents: list[dict[str, Any]], run_id: str, errors: list[str]) -> None:
    configs = [document for document in documents if document.get("kind") == "ConfigMap"]
    jobs = [
        document
        for document in documents
        if document.get("kind") == "Job"
        and _labels(document).get(COMPONENT_LABEL) == "semantic-probe"
    ]
    if len(configs) != 1 or len(jobs) != 1:
        errors.append("probe manifest must contain exactly one ConfigMap and one Job")
        return
    expected_name = f"dd-semantic-{run_id}"
    config = configs[0]
    job = jobs[0]
    if _metadata(config).get("name") != expected_name or _metadata(job).get("name") != expected_name:
        errors.append("probe objects are not named for the exact run ID")
    if config.get("immutable") is not True:
        errors.append("probe ConfigMap must be immutable")
    source = config.get("data", {}).get("validate_diffdock.py")
    if not isinstance(source, str) or hashlib.sha256(source.encode("utf-8")).hexdigest() != VALIDATOR_SHA256:
        errors.append("probe ConfigMap does not contain the reviewed strict DiffDock validator")
    fixture = config.get("data", {}).get("1ubq-aspirin-request.json")
    if not isinstance(fixture, str) or hashlib.sha256(fixture.encode("utf-8")).hexdigest() != FIXTURE_SHA256:
        errors.append("probe ConfigMap does not contain the pinned 1UBQ/aspirin fixture")
    if _metadata(config).get("annotations", {}).get(
        "archvteams.nebius.ai/validator-sha256"
    ) != VALIDATOR_SHA256:
        errors.append("probe ConfigMap validator digest annotation is incorrect")
    if _metadata(config).get("annotations", {}).get(
        "archvteams.nebius.ai/fixture-sha256"
    ) != FIXTURE_SHA256:
        errors.append("probe ConfigMap fixture digest annotation is incorrect")

    annotations = _metadata(job).get("annotations", {})
    if not isinstance(annotations, dict):
        annotations = {}
    if not UUID.fullmatch(str(annotations.get("archvteams.nebius.ai/target-pod-uid", ""))):
        errors.append("probe Job is not bound to the target Pod UID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(annotations.get(POD_SPEC_HASH_KEY, ""))):
        errors.append("probe Job is not bound to the target PodSpec hash")
    if annotations.get("archvteams.nebius.ai/validator-sha256") != VALIDATOR_SHA256:
        errors.append("probe Job validator digest annotation is incorrect")
    if annotations.get("archvteams.nebius.ai/fixture-sha256") != FIXTURE_SHA256:
        errors.append("probe Job fixture digest annotation is incorrect")

    spec = _pod_spec(job)
    location = f"Job/{_metadata(job).get('name', '?')}"
    if spec is None:
        errors.append(f"{location} has no pod template spec")
        return
    if spec.get("automountServiceAccountToken") is not False:
        errors.append(f"{location} must be tokenless")
    if spec.get("enableServiceLinks") is not False:
        errors.append(f"{location} must disable service links")
    pod_security = spec.get("securityContext", {})
    if not isinstance(pod_security, dict) or pod_security.get("fsGroup") != 65534:
        errors.append(f"{location} must make evidence volumes writable by the non-root probe")
    for field in ("hostPID", "hostIPC", "hostNetwork"):
        if spec.get(field) is not False:
            errors.append(f"{location} must set {field}: false")
    hostnames = _hostname_values(spec)
    if hostnames != ["gpu-node-a.example.invalid"]:
        errors.append(f"{location} must select the stable hf93 hostname")
    init_containers = spec.get("initContainers")
    stager: dict[str, Any] = {}
    if not isinstance(init_containers, list) or len(init_containers) != 1:
        errors.append(f"{location} must contain exactly one validator staging init container")
    else:
        stager = init_containers[0]
        stager_args = stager.get("args", []) if isinstance(stager, dict) else []
        if (
            not isinstance(stager, dict)
            or stager.get("name") != "stage-validator"
            or not isinstance(stager.get("image"), str)
            or not PINNED_IMAGE.fullmatch(stager["image"])
            or stager.get("command") != [PROBE_EXECUTABLE]
            or not isinstance(stager_args, list)
            or len(stager_args) != 2
            or stager_args[0] != "-c"
            or not isinstance(stager_args[1], str)
            or hashlib.sha256(stager_args[1].encode("utf-8")).hexdigest()
            != STAGER_SHA256
        ):
            errors.append(f"{location} validator stager is not the exact reviewed implementation")
        elif set(_mount_signatures(stager, location, errors)) != {
            ("validator-source", "/validator-source", None, True),
            ("validator", "/validator", None, False),
        }:
            errors.append(f"{location} validator stager mounts are not exact")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        errors.append(f"{location} must contain exactly one CPU semantic probe")
        return
    container = containers[0]
    if container.get("name") != "semantic-probe" or container.get("image") != stager.get("image"):
        errors.append(f"{location} must use one exact pinned CPU probe image")
    resources = container.get("resources", {})
    for group in ("requests", "limits"):
        group_values = resources.get(group, {}) if isinstance(resources, dict) else {}
        if any("gpu" in str(key).lower() for key in group_values):
            errors.append(f"{location} semantic probe must not request a GPU")
    command = container.get("command")
    if (
        not isinstance(command, list)
        or len(command) != 1
        or not isinstance(command[0], str)
        or not command[0].startswith("/")
    ):
        errors.append(f"{location} must invoke one absolute Python executable")
    expected_args = [
        "/validator/validate_diffdock.py",
        "--base-url",
        f"http://dd-canary-{run_id}:8000",
        "--request-file",
        "/validator/1ubq-aspirin-request.json",
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
    ]
    if container.get("args") != expected_args:
        errors.append(f"{location} must execute exactly two fixed strict DiffDock probes")
    expected_mounts = {
        ("validator", "/validator", None, True),
        ("evidence", "/evidence", None, False),
        ("tmp", "/tmp", None, False),
    }
    if set(_mount_signatures(container, location, errors)) != expected_mounts:
        errors.append(f"{location} semantic-probe mounts are not exact")
    if set(_volume_names(spec, location, errors)) != {
        "validator-source",
        "validator",
        "evidence",
        "tmp",
    }:
        errors.append(f"{location} volume set is not exact")
    validator_source = _find_volume(spec, "validator-source") or {}
    if validator_source.get("configMap") != {
        "name": expected_name,
        "defaultMode": 0o444,
    }:
        errors.append(f"{location} validator source is not the exact ConfigMap projection")
    validator_volume = _find_volume(spec, "validator") or {}
    if validator_volume.get("emptyDir") != {"sizeLimit": "1Mi"}:
        errors.append(f"{location} staged validator inputs must use the exact emptyDir")
    job_spec = job.get("spec", {})
    if not isinstance(job_spec, dict) or any(
        job_spec.get(key) != value
        for key, value in {
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
        }.items()
    ):
        errors.append(f"{location} must be a single-attempt, single-completion Job")
    deadline = job_spec.get("activeDeadlineSeconds") if isinstance(job_spec, dict) else None
    if not isinstance(deadline, int) or deadline > 660:
        errors.append(f"{location} must have a bounded active deadline")


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_placeholder(key) or _contains_placeholder(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return isinstance(value, str) and ("@@" in value or "REPLACE_WITH" in value)


def lint_documents(raw_documents: Iterable[dict[str, Any]]) -> list[str]:
    documents = list(raw_documents)
    errors: list[str] = []
    if not documents:
        return ["manifest contains no documents"]
    run_ids: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            errors.append("manifest contains a non-object document")
            continue
        if _contains_placeholder(document):
            errors.append(f"{document.get('kind', '?')} contains an unresolved placeholder")
        metadata = _metadata(document)
        if document.get("kind") == "Secret":
            errors.append("manifest must never contain a Secret")
        if metadata.get("namespace") != NAMESPACE:
            errors.append(f"{document.get('kind', '?')}/{metadata.get('name', '?')} has wrong namespace")
        run_id = _labels(document).get(RUN_LABEL)
        if not isinstance(run_id, str) or not run_id:
            errors.append(f"{document.get('kind', '?')}/{metadata.get('name', '?')} lacks run-id label")
        else:
            run_ids.add(run_id)
        spec = _pod_spec(document)
        if spec is not None:
            _check_images_and_common_pod_safety(document, spec, errors)
    if len(run_ids) != 1:
        errors.append("all objects must carry one unique run-id label")
        run_id = ""
    else:
        run_id = next(iter(run_ids))

    targets = [
        document
        for document in documents
        if document.get("kind") == "Pod"
        and _labels(document).get(COMPONENT_LABEL) == "restore-target"
    ]
    workers = [
        document
        for document in documents
        if document.get("kind") == "Job"
        and _labels(document).get(COMPONENT_LABEL) == "restore-worker"
    ]
    probes = [
        document
        for document in documents
        if document.get("kind") == "Job"
        and _labels(document).get(COMPONENT_LABEL) == "semantic-probe"
    ]
    if targets:
        observed_kinds = sorted(document.get("kind") for document in documents)
        if observed_kinds != sorted(["Pod", "Service", "Service", "NetworkPolicy", "NetworkPolicy"]):
            errors.append("target manifest object set is not exactly Pod, two Services, and two NetworkPolicies")
        if len(targets) != 1:
            errors.append("target manifest must contain exactly one restore target")
        else:
            _check_target(targets[0], errors)
        _check_services(documents, run_id, errors)
        _check_network_policies(documents, run_id, errors)
    if workers:
        observed_kinds = sorted(document.get("kind") for document in documents)
        if observed_kinds != sorted(["ServiceAccount", "Role", "RoleBinding", "Job"]):
            errors.append("restore manifest object set is not exactly ServiceAccount, Role, RoleBinding, and Job")
        if len(workers) != 1:
            errors.append("restore manifest must contain exactly one restore Job")
        else:
            _check_worker(workers[0], errors)
            annotations = _metadata(workers[0]).get("annotations", {})
            target_name = annotations.get("archvteams.nebius.ai/target-pod", "")
            _check_rbac(documents, target_name, errors)
    if probes:
        observed_kinds = sorted(document.get("kind") for document in documents)
        if observed_kinds != ["ConfigMap", "Job"]:
            errors.append("probe manifest object set is not exactly ConfigMap and Job")
        _check_probe(documents, run_id, errors)
    if not targets and not workers and not probes:
        errors.append("manifest contains neither a restore target, restore worker, nor semantic probe")
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", default="-", help="YAML path or - for stdin")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.manifest == "-":
            documents = list(yaml.safe_load_all(sys.stdin))
        else:
            documents = list(yaml.safe_load_all(Path(args.manifest).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        print(f"lint-manifest: cannot read manifest: {exc}", file=sys.stderr)
        return 2
    errors = lint_documents(documents)
    if errors:
        for error in errors:
            print(f"lint-manifest: {error}", file=sys.stderr)
        return 1
    print(f"lint-manifest: ok ({len(documents)} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
