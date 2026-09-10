#!/usr/bin/env python3
"""Render RFdiffusion target, one-shot restore worker, and early semantic probe."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

import yaml


HERE = Path(__file__).resolve().parent
BASE_DYNAMO = HERE.parent / "dynamo"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = _load_module("openfold2_dynamo_render_for_rfdiffusion", BASE_DYNAMO / "render.py")

NAMESPACE = "nim-fast-start"
RUN_SCHEMA = "archvteams.nebius.ai/rfdiffusion-faststart-run/v1"
BINDING_SCHEMA = "archvteams.nebius.ai/rfdiffusion-target-binding/v1"
VALIDATOR_SHA256 = "691ee7d60aaf22f32cb05bb7c67fa0552b656f0d549015f3297516e1dd7b2317"
OPENFOLD_IMAGE = base.NIM_IMAGE
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
WORKER_GATE = json.loads((HERE / "worker-gate.json").read_text(encoding="utf-8"))
NIM_IMAGE = PROFILE["model"]["image"]
TARGET_NODE = PROFILE["hardware"]["retained_capture_node"]
CONTAINER_NAME = PROFILE["model"]["container_name"]
FIXTURE_PATH = HERE / PROFILE["semantic_profile"]["fixture"]
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RenderError(ValueError):
    pass


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read {label}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RenderError(
            f"{label} keys differ; missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)}"
        )


def validate_contract(value: dict[str, Any]) -> dict[str, Any]:
    original = base.VALIDATOR_SHA256
    try:
        base.VALIDATOR_SHA256 = VALIDATOR_SHA256
        validated = base.validate_contract(value)
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    finally:
        base.VALIDATOR_SHA256 = original
    if validated["worker_image"] != WORKER_GATE["worker_image"]:
        raise RenderError("contract does not use the exact current performance worker")
    if validated["tool_bundle"]["content_sha256"] != WORKER_GATE[
        "tool_bundle_manifest_sha256"
    ]:
        raise RenderError("contract tool bundle does not match the worker gate")
    return validated


def validate_run(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema",
            "run_id",
            "target_node",
            "checkpoint_id",
            "artifact_version",
            "artifact_manifest_sha256",
            "artifact_pvc",
            "cache_pvc",
            "demand_at",
            "image_io_mode",
        },
        "run config",
    )
    if value.get("schema") != RUN_SCHEMA:
        raise RenderError("run config schema is not supported")
    try:
        base._timestamp(value.get("demand_at"), "demand_at")
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or len(run_id) > 30 or DNS_LABEL.fullmatch(run_id) is None:
        raise RenderError("run_id must be a lowercase DNS label of at most 30 characters")
    if value.get("target_node") != TARGET_NODE:
        raise RenderError("target_node is not the exact retained single-H100 node")
    mode = value.get("image_io_mode")
    if mode not in {"direct", "buffered"}:
        raise RenderError("image_io_mode must be direct or buffered")
    artifact = PROFILE["artifacts"][mode]
    if value.get("checkpoint_id") != artifact["checkpoint_id"]:
        raise RenderError("checkpoint_id does not match the selected image I/O mode")
    if value.get("artifact_version") != artifact["artifact_version"]:
        raise RenderError("artifact_version does not match the pinned profile")
    digest = value.get("artifact_manifest_sha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise RenderError("artifact_manifest_sha256 must be lowercase SHA-256")
    if digest != artifact.get("manifest_sha256"):
        raise RenderError(
            "artifact_manifest_sha256 does not match the selected pinned profile"
        )
    if value.get("artifact_pvc") != PROFILE["storage"]["artifact_pvc"]:
        raise RenderError("artifact_pvc does not match the profile")
    if value.get("cache_pvc") != PROFILE["storage"]["cache_pvc"]:
        raise RenderError("cache_pvc does not match the profile")
    return value


def _target_name(run_id: str) -> str:
    return f"rfd-target-{run_id}"


def validate_binding(value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema",
            "collected_at",
            "run_id",
            "namespace",
            "pod_name",
            "pod_uid",
            "container_name",
            "container_id",
            "cgroup",
            "pod_ip",
            "node",
            "image_id",
            "pod_spec_sha256",
        },
        "binding",
    )
    if value.get("schema") != BINDING_SCHEMA:
        raise RenderError("binding schema is not supported")
    try:
        base._timestamp(value.get("collected_at"), "binding.collected_at")
        parsed_uid = uuid.UUID(str(value.get("pod_uid")))
    except (base.RenderError, ValueError) as exc:
        raise RenderError("binding timestamp or Pod UID is invalid") from exc
    if str(parsed_uid) != value.get("pod_uid"):
        raise RenderError("binding Pod UID must be canonical lowercase UUID")
    expected = {
        "run_id": run["run_id"],
        "namespace": NAMESPACE,
        "pod_name": _target_name(run["run_id"]),
        "container_name": CONTAINER_NAME,
        "node": run["target_node"],
        "image_id": NIM_IMAGE,
    }
    for key, item in expected.items():
        if value.get(key) != item:
            raise RenderError(f"binding {key} does not match the exact RFdiffusion run")
    if base.CONTAINER_ID.fullmatch(str(value.get("container_id"))) is None:
        raise RenderError("binding container_id must be a full containerd ID")
    cgroup = value.get("cgroup")
    if not isinstance(cgroup, str) or not cgroup.startswith("/kubepods") or ".." in cgroup:
        raise RenderError("binding cgroup is not an exact kubepods path")
    try:
        base.ipaddress.ip_address(value.get("pod_ip"))
    except ValueError as exc:
        raise RenderError("binding pod_ip is not an IP address") from exc
    if not isinstance(value.get("pod_spec_sha256"), str) or SHA256.fullmatch(
        value["pod_spec_sha256"]
    ) is None:
        raise RenderError("binding PodSpec digest is invalid")
    return value


def _base_tokens(run: dict[str, Any], contract: dict[str, Any]) -> dict[str, str]:
    run_id = run["run_id"]
    return {
        "@@RUN_ID@@": run_id,
        "@@TARGET_NAME@@": _target_name(run_id),
        "@@TARGET_NODE@@": run["target_node"],
        "@@TARGET_IMAGE@@": NIM_IMAGE,
        "@@CHECKPOINT_ID@@": run["checkpoint_id"],
        "@@ARTIFACT_VERSION@@": run["artifact_version"],
        "@@ARTIFACT_MANIFEST_SHA256@@": run["artifact_manifest_sha256"],
        "@@ARTIFACT_PVC@@": run["artifact_pvc"],
        "@@CACHE_PVC@@": run["cache_pvc"],
        "@@TOOL_BUNDLE_SHA256@@": contract["tool_bundle"]["content_sha256"],
        "@@CANARY_SERVICE@@": f"rfd-canary-{run_id}",
        "@@QUALIFIED_SERVICE@@": f"rfd-qualified-{run_id}",
        "@@TARGET_NETWORK_POLICY@@": f"rfd-target-{run_id}",
        "@@PROBE_NETWORK_POLICY@@": f"rfd-probe-{run_id}",
        "@@WORKER_NAME@@": f"rfd-restore-{run_id}",
        "@@WORKER_IMAGE@@": contract["worker_image"],
        "@@WORKER_EXECUTABLE@@": contract["worker_executable"],
        "@@PROBE_NAME@@": f"rfd-semantic-{run_id}",
        "@@PROBE_IMAGE@@": contract["probe_image"],
        "@@PROBE_EXECUTABLE@@": contract["probe_executable"],
        "@@PROBE_RUN_ID_1@@": f"{run_id}-semantic-a",
        "@@PROBE_RUN_ID_2@@": f"{run_id}-semantic-b",
        "@@DEMAND_AT@@": run["demand_at"],
    }


def _rfdiffusion_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _rfdiffusion_structure(key): _rfdiffusion_structure(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rfdiffusion_structure(item) for item in value]
    if isinstance(value, str):
        if value == OPENFOLD_IMAGE:
            return NIM_IMAGE
        return (
            value.replace("validate_openfold2", "validate_rfdiffusion")
            .replace("OpenFold2", "RFdiffusion")
            .replace("openfold2", "rfdiffusion")
            .replace("of2-", "rfd-")
        )
    return value


def _load_template(name: str) -> list[dict[str, Any]]:
    try:
        return _rfdiffusion_structure(base._load_template(name))
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc


def _target_container(documents: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    pods = [item for item in documents if item.get("kind") == "Pod"]
    if len(pods) != 1:
        raise RenderError("target template must contain exactly one Pod")
    pod = pods[0]
    containers = pod.get("spec", {}).get("containers", [])
    if len(containers) != 1:
        raise RenderError("target Pod must contain exactly one container")
    return pod, containers[0]


def render_target(run: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    documents = base._replace(
        _load_template("target.yaml.tmpl"), _base_tokens(run, contract), None
    )
    pod, container = _target_container(documents)
    pod["spec"]["imagePullSecrets"] = [{"name": "nvcrio-cred"}]
    pod["spec"]["terminationGracePeriodSeconds"] = 1
    container["resources"] = copy.deepcopy(PROFILE["pod_profile"])
    container["resources"].pop("shared_memory", None)
    container["resources"].pop("qos_class", None)
    container["securityContext"]["runAsUser"] = 1000
    container["securityContext"]["runAsGroup"] = 1000
    pod["spec"]["securityContext"]["fsGroup"] = 1000
    pod["spec"]["securityContext"]["fsGroupChangePolicy"] = "OnRootMismatch"
    for mount in container["volumeMounts"]:
        if mount.get("name") == "nim-cache":
            mount["mountPath"] = PROFILE["model"]["cache_path"]
    container["volumeMounts"].append(
        {
            "name": "validator",
            "mountPath": PROFILE["runtime_topology"]["validator_mount_path"],
            "readOnly": True,
        }
    )
    pod["spec"]["volumes"].append(
        {
            "name": "validator",
            "emptyDir": {
                "sizeLimit": PROFILE["runtime_topology"]["validator_size_limit"],
            },
        }
    )
    for item in container.get("env", []):
        if item.get("name") == "NIM_CACHE_PATH":
            item["value"] = PROFILE["model"]["nim_cache_path"]
    for volume in pod["spec"]["volumes"]:
        if volume.get("name") == "dshm":
            volume["emptyDir"]["sizeLimit"] = PROFILE["pod_profile"]["shared_memory"]
    base._assert_no_placeholders(documents)
    errors = lint_documents(documents)
    if errors:
        raise RenderError("rendered target failed lint: " + "; ".join(errors))
    return documents


def render_restore(
    run: dict[str, Any], contract: dict[str, Any], binding: dict[str, Any]
) -> list[dict[str, Any]]:
    fields = {
        "target_namespace": binding["namespace"],
        "target_name": binding["pod_name"],
        "target_uid": binding["pod_uid"],
        "target_container": binding["container_name"],
        "target_container_id": binding["container_id"],
        "target_cgroup": binding["cgroup"],
        "target_pod_ip": binding["pod_ip"],
        "target_node": binding["node"],
        "target_pod_spec_sha256": binding["pod_spec_sha256"],
        "expected_image_id": binding["image_id"],
        "run_id": run["run_id"],
        "checkpoint_id": run["checkpoint_id"],
        "artifact_version": run["artifact_version"],
        "artifact_manifest_sha256": run["artifact_manifest_sha256"],
        "tool_bundle_sha256": contract["tool_bundle"]["content_sha256"],
    }
    args = [item.format_map(fields) for item in base.REQUIRED_ARGUMENT_TEMPLATE]
    tokens = _base_tokens(run, contract)
    tokens.update(
        {
            "@@TARGET_UID@@": binding["pod_uid"],
            "@@TARGET_CONTAINER_ID@@": binding["container_id"],
            "@@TARGET_CGROUP@@": binding["cgroup"],
            "@@TARGET_POD_IP@@": binding["pod_ip"],
            "@@TARGET_POD_SPEC_SHA256@@": binding["pod_spec_sha256"],
        }
    )
    documents = base._replace(_load_template("restore-worker.yaml.tmpl"), tokens, args)
    base._assert_no_placeholders(documents)
    errors = lint_documents(documents)
    if errors:
        raise RenderError("rendered restore worker failed lint: " + "; ".join(errors))
    return documents


def render_probe(
    run: dict[str, Any], contract: dict[str, Any], binding: dict[str, Any]
) -> list[dict[str, Any]]:
    source = (HERE / "validate_rfdiffusion.py").read_bytes()
    if hashlib.sha256(source).hexdigest() != VALIDATOR_SHA256:
        raise RenderError("strict RFdiffusion validator source digest changed")
    fixture = FIXTURE_PATH.read_bytes()
    if hashlib.sha256(fixture).hexdigest() != PROFILE["semantic_profile"][
        "fixture_sha256"
    ]:
        raise RenderError("strict 1UBQ fixture digest changed")
    tokens = _base_tokens(run, contract)
    tokens.update(
        {
            "@@TARGET_UID@@": binding["pod_uid"],
            "@@TARGET_POD_SPEC_SHA256@@": binding["pod_spec_sha256"],
            "@@VALIDATOR_SHA256@@": VALIDATOR_SHA256,
            "@@VALIDATOR_SOURCE@@": source.decode("utf-8"),
        }
    )
    documents = base._replace(_load_template("semantic-probe.yaml.tmpl"), tokens, None)
    config_maps = [item for item in documents if item.get("kind") == "ConfigMap"]
    if len(config_maps) != 1:
        raise RenderError("semantic probe template must contain exactly one ConfigMap")
    config_maps[0]["data"]["1UBQ.pdb"] = fixture.decode("ascii")
    jobs = [item for item in documents if item.get("kind") == "Job"]
    if len(jobs) != 1:
        raise RenderError("semantic probe template must contain exactly one Job")
    job = jobs[0]
    probe_container = job["spec"]["template"]["spec"]["containers"][0]
    probe_container["args"][1:1] = ["--fixture", "/validator/1UBQ.pdb"]
    job["spec"]["activeDeadlineSeconds"] = 900
    job["spec"]["template"]["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [run["target_node"]],
                            }
                        ]
                    }
                ]
            }
        }
    }
    base._assert_no_placeholders(documents)
    errors = lint_documents(documents)
    if errors:
        raise RenderError("rendered semantic probe failed lint: " + "; ".join(errors))
    return documents


def _contains_gpu(resources: Any) -> bool:
    if not isinstance(resources, dict):
        return False
    return any(
        isinstance(values, dict) and "nvidia.com/gpu" in values
        for values in resources.values()
    )


def _hostname_affinity(pod_spec: dict[str, Any]) -> list[str]:
    try:
        terms = pod_spec["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
        expressions = terms[0]["matchExpressions"]
        expression = next(item for item in expressions if item.get("key") == "kubernetes.io/hostname")
        if expression.get("operator") != "In":
            return []
        return expression.get("values", [])
    except (KeyError, IndexError, StopIteration, TypeError):
        return []


def lint_documents(documents: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    rendered = json.dumps(documents, sort_keys=True)
    if "@@" in rendered:
        errors.append("unresolved placeholder")
    if ":latest" in rendered:
        errors.append("mutable image tag")
    for document in documents:
        metadata = document.get("metadata", {})
        if metadata.get("namespace") != NAMESPACE:
            errors.append(f"{document.get('kind')} {metadata.get('name')} has wrong namespace")

    pods = [item for item in documents if item.get("kind") == "Pod"]
    if pods:
        if len(pods) != 1:
            errors.append("target render must contain one Pod")
        else:
            pod = pods[0]
            spec = pod.get("spec", {})
            for volume in spec.get("volumes", []):
                sources = set(volume) - {"name"}
                if sources == {"emptyDir"}:
                    if not volume["emptyDir"].get("sizeLimit"):
                        errors.append(f"target emptyDir {volume.get('name')} is unbounded")
                elif sources == {"persistentVolumeClaim"}:
                    if not volume["persistentVolumeClaim"].get("claimName"):
                        errors.append(f"target PVC {volume.get('name')} has no claim")
                else:
                    errors.append(
                        f"target volume {volume.get('name')} is not d5ce-compatible emptyDir/PVC"
                    )
            containers = spec.get("containers", [])
            if len(containers) != 1:
                errors.append("target must contain one container")
            else:
                container = containers[0]
                if container.get("name") != CONTAINER_NAME or container.get("image") != NIM_IMAGE:
                    errors.append("target container identity is not exact")
                if container.get("command") != ["/bin/sleep"] or container.get("args") != ["2147483647"]:
                    errors.append("target placeholder is not inert")
                if container.get("resources") != {
                    "requests": PROFILE["pod_profile"]["requests"],
                    "limits": PROFILE["pod_profile"]["limits"],
                }:
                    errors.append("target resources do not match the H100 profile")
                cache_mounts = [
                    item for item in container.get("volumeMounts", []) if item.get("name") == "nim-cache"
                ]
                if len(cache_mounts) != 1 or cache_mounts[0].get("mountPath") != PROFILE["model"]["cache_path"]:
                    errors.append("target cache mount does not match the captured rootfs")
                validator_mounts = [
                    item
                    for item in container.get("volumeMounts", [])
                    if item.get("name") == "validator"
                ]
                if validator_mounts != [
                    {
                        "name": "validator",
                        "mountPath": PROFILE["runtime_topology"]["validator_mount_path"],
                        "readOnly": True,
                    }
                ]:
                    errors.append("target validator mount does not match the captured topology")
                validator_volumes = [
                    item for item in spec.get("volumes", []) if item.get("name") == "validator"
                ]
                if validator_volumes != [
                    {
                        "name": "validator",
                        "emptyDir": {
                            "sizeLimit": PROFILE["runtime_topology"]["validator_size_limit"],
                        },
                    }
                ]:
                    errors.append("target validator volume does not match the captured topology")
            if "nodeName" in spec or _hostname_affinity(spec) != [TARGET_NODE]:
                errors.append("target must use scheduler affinity for the exact H100 node")
            if spec.get("automountServiceAccountToken") is not False:
                errors.append("target must be tokenless")
            if spec.get("imagePullSecrets") != [{"name": "nvcrio-cred"}]:
                errors.append("target pull secret is not exact")
            services = [item for item in documents if item.get("kind") == "Service"]
            if len(services) != 2 or any(item.get("spec", {}).get("type") != "ClusterIP" for item in services):
                errors.append("target must expose exactly two ClusterIP services")

    jobs = [item for item in documents if item.get("kind") == "Job"]
    config_maps = [item for item in documents if item.get("kind") == "ConfigMap"]
    if jobs:
        if len(jobs) != 1:
            errors.append("render must contain exactly one Job")
        else:
            job = jobs[0]
            pod_spec = job.get("spec", {}).get("template", {}).get("spec", {})
            containers = pod_spec.get("containers", [])
            if len(containers) != 1:
                errors.append("Job must contain exactly one container")
            else:
                container = containers[0]
                if _contains_gpu(container.get("resources")):
                    errors.append("worker/probe must be CPU-only")
                component = job.get("metadata", {}).get("labels", {}).get(
                    "app.kubernetes.io/component"
                )
                if component == "semantic-probe":
                    args = container.get("args", [])
                    if args.count("--run-id") != 2:
                        errors.append("semantic probe must pass exactly two run IDs")
                    if "--base-url" not in args or not any(
                        isinstance(item, str) and item.startswith("http://rfd-canary-")
                        for item in args
                    ):
                        errors.append("semantic probe must use the run-scoped ClusterIP")
                    if pod_spec.get("automountServiceAccountToken") is not False:
                        errors.append("semantic probe must be tokenless")
                    if _hostname_affinity(pod_spec) != [TARGET_NODE]:
                        errors.append("semantic probe must stay on the exact provisioned node")
                elif component == "restore-worker":
                    if container.get("image") != WORKER_GATE["worker_image"]:
                        errors.append("restore worker image is not current")
                    if container.get("securityContext", {}).get("privileged") is not True:
                        errors.append("restore worker lacks its required privilege")
                    if _hostname_affinity(pod_spec) != [TARGET_NODE]:
                        errors.append("restore worker is not node-bound to the target")
    if config_maps:
        if len(config_maps) != 1:
            errors.append("semantic render must contain one ConfigMap")
        else:
            config = config_maps[0]
            source = config.get("data", {}).get("validate_rfdiffusion.py")
            if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != VALIDATOR_SHA256:
                errors.append("embedded validator source digest differs")
            fixture = config.get("data", {}).get("1UBQ.pdb")
            expected_fixture = PROFILE["semantic_profile"]["fixture_sha256"]
            if not isinstance(fixture, str) or hashlib.sha256(fixture.encode("ascii")).hexdigest() != expected_fixture:
                errors.append("embedded 1UBQ fixture digest differs")
    return errors


def dump_documents(documents: Iterable[dict[str, Any]]) -> None:
    yaml.safe_dump_all(
        list(documents),
        sys.stdout,
        explicit_start=True,
        default_flow_style=False,
        sort_keys=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("target", "restore", "probe"):
        child = subparsers.add_parser(mode)
        child.add_argument("--contract", type=Path, required=True)
        child.add_argument("--run-config", type=Path, required=True)
        if mode != "target":
            child.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        contract = validate_contract(_read_json(args.contract, "contract"))
        run = validate_run(_read_json(args.run_config, "run config"))
        if args.mode == "target":
            documents = render_target(run, contract)
        else:
            binding = validate_binding(_read_json(args.binding, "binding"), run)
            documents = (
                render_restore(run, contract, binding)
                if args.mode == "restore"
                else render_probe(run, contract, binding)
            )
        dump_documents(documents)
        return 0
    except (RenderError, OSError, yaml.YAMLError) as exc:
        print(f"render-rfdiffusion: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
