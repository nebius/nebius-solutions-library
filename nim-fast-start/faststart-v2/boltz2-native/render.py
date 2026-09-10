#!/usr/bin/env python3
"""Render isolated Boltz2 target, one-shot restore, and semantic-probe YAML."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
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


base = _load_module("openfold2_dynamo_render_for_boltz2", BASE_DYNAMO / "render.py")
base_lint = _load_module("openfold2_dynamo_lint_for_boltz2", BASE_DYNAMO / "lint_manifest.py")

NAMESPACE = "nim-fast-start"
NIM_IMAGE = (
    "nvcr.io/nim/mit/boltz2@sha256:"
    "0788c95c8b5b6c1a73a62c656b298ecc353a8187dc22b794f496ae40672c4c98"
)
OPENFOLD_IMAGE = base.NIM_IMAGE
RUN_SCHEMA = "archvteams.nebius.ai/boltz2-faststart-run/v1"
BINDING_SCHEMA = "archvteams.nebius.ai/boltz2-target-binding/v1"
VALIDATOR_SHA256 = "4b4e04b62cd8aff2027844f75002012439d7cb5d44f91f4ac514a99abf2217c8"


class RenderError(ValueError):
    pass


def _target_name(run_id: str) -> str:
    return f"b2-target-{run_id}"


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read {label}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must be an object")
    return value


def validate_contract(value: dict[str, Any]) -> dict[str, Any]:
    original = base.VALIDATOR_SHA256
    try:
        base.VALIDATOR_SHA256 = VALIDATOR_SHA256
        return base.validate_contract(value)
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    finally:
        base.VALIDATOR_SHA256 = original


def validate_run(value: dict[str, Any]) -> dict[str, Any]:
    translated = copy.deepcopy(value)
    if translated.get("schema") != RUN_SCHEMA:
        raise RenderError("run config schema is not supported")
    translated["schema"] = base.RUN_SCHEMA
    try:
        base.validate_run(translated)
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    return value


def validate_binding(value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    translated = copy.deepcopy(value)
    if translated.get("schema") != BINDING_SCHEMA:
        raise RenderError("binding schema is not supported")
    translated["schema"] = base.BINDING_SCHEMA
    translated["pod_name"] = f"of2-target-{run['run_id']}"
    translated["container_name"] = "openfold2"
    translated["image_id"] = OPENFOLD_IMAGE
    translated_run = copy.deepcopy(run)
    translated_run["schema"] = base.RUN_SCHEMA
    try:
        base.validate_binding(translated, translated_run)
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    if value.get("pod_name") != _target_name(run["run_id"]):
        raise RenderError("binding pod_name does not match the rendered Boltz2 target")
    if value.get("container_name") != "boltz2":
        raise RenderError("binding container_name must be boltz2")
    if value.get("image_id") != NIM_IMAGE:
        raise RenderError("binding image_id is not the exact pinned Boltz2 digest")
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
        "@@CANARY_SERVICE@@": f"b2-canary-{run_id}",
        "@@QUALIFIED_SERVICE@@": f"b2-qualified-{run_id}",
        "@@TARGET_NETWORK_POLICY@@": f"b2-target-{run_id}",
        "@@PROBE_NETWORK_POLICY@@": f"b2-probe-{run_id}",
        "@@WORKER_NAME@@": f"b2-restore-{run_id}",
        "@@WORKER_IMAGE@@": contract["worker_image"],
        "@@WORKER_EXECUTABLE@@": contract["worker_executable"],
        "@@PROBE_NAME@@": f"b2-semantic-{run_id}",
        "@@PROBE_IMAGE@@": contract["probe_image"],
        "@@PROBE_EXECUTABLE@@": contract["probe_executable"],
        "@@PROBE_RUN_ID_1@@": f"{run_id}-semantic-a",
        "@@PROBE_RUN_ID_2@@": f"{run_id}-semantic-b",
        "@@DEMAND_AT@@": run["demand_at"],
    }


def _boltz_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {_boltz_structure(key): _boltz_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_boltz_structure(item) for item in value]
    if isinstance(value, str):
        if value == OPENFOLD_IMAGE:
            return NIM_IMAGE
        return value.replace("validate_openfold2", "validate_boltz2").replace(
            "OpenFold2", "Boltz2"
        ).replace("openfold2", "boltz2")
    return value


def _load_template(name: str) -> list[dict[str, Any]]:
    try:
        docs = base._load_template(name)
    except base.RenderError as exc:
        raise RenderError(str(exc)) from exc
    return _boltz_structure(docs)


def render_target(run: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    documents = base._replace(_load_template("target.yaml.tmpl"), _base_tokens(run, contract), None)
    base._assert_no_placeholders(documents)
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
    tokens.update({
        "@@TARGET_UID@@": binding["pod_uid"],
        "@@TARGET_CONTAINER_ID@@": binding["container_id"],
        "@@TARGET_CGROUP@@": binding["cgroup"],
        "@@TARGET_POD_IP@@": binding["pod_ip"],
        "@@TARGET_POD_SPEC_SHA256@@": binding["pod_spec_sha256"],
    })
    documents = base._replace(_load_template("restore-worker.yaml.tmpl"), tokens, args)
    base._assert_no_placeholders(documents)
    return documents


def render_probe(
    run: dict[str, Any], contract: dict[str, Any], binding: dict[str, Any]
) -> list[dict[str, Any]]:
    source = (HERE / "validate_boltz2.py").read_bytes()
    if hashlib.sha256(source).hexdigest() != VALIDATOR_SHA256:
        raise RenderError("strict Boltz2 validator source digest changed")
    tokens = _base_tokens(run, contract)
    tokens.update({
        "@@TARGET_UID@@": binding["pod_uid"],
        "@@TARGET_POD_SPEC_SHA256@@": binding["pod_spec_sha256"],
        "@@VALIDATOR_SHA256@@": VALIDATOR_SHA256,
        "@@VALIDATOR_SOURCE@@": source.decode("utf-8"),
    })
    documents = base._replace(_load_template("semantic-probe.yaml.tmpl"), tokens, None)
    jobs = [document for document in documents if document.get("kind") == "Job"]
    if len(jobs) != 1:
        raise RenderError("semantic probe template must contain exactly one Job")
    # Keep the external client as a separate Pod reached through the ClusterIP,
    # but constrain it to the same stable, explicitly approved node as the
    # target.  This prevents an otherwise CPU-only probe from being assigned to
    # transient preemptible capacity while preserving the service boundary.
    jobs[0]["spec"]["template"]["spec"]["affinity"] = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{
                    "matchExpressions": [{
                        "key": "kubernetes.io/hostname",
                        "operator": "In",
                        "values": [run["target_node"]],
                    }]
                }]
            }
        }
    }
    base._assert_no_placeholders(documents)
    return documents


def _normalize_for_openfold_lint(value: Any, *, source_value: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_for_openfold_lint(key)
            result[normalized_key] = _normalize_for_openfold_lint(
                item, source_value=source_value or key == "data"
            )
        return result
    if isinstance(value, list):
        return [_normalize_for_openfold_lint(item, source_value=source_value) for item in value]
    if isinstance(value, str) and not source_value:
        if value == NIM_IMAGE:
            return OPENFOLD_IMAGE
        return value.replace("validate_boltz2", "validate_openfold2").replace(
            "Boltz2", "OpenFold2"
        ).replace("boltz2", "openfold2").replace("b2-", "of2-")
    return value


def lint_documents(documents: list[dict[str, Any]]) -> list[str]:
    normalized = _normalize_for_openfold_lint(copy.deepcopy(documents))
    original = base_lint.VALIDATOR_SHA256
    try:
        base_lint.VALIDATOR_SHA256 = VALIDATOR_SHA256
        return base_lint.lint_documents(normalized)
    finally:
        base_lint.VALIDATOR_SHA256 = original


def dump_documents(documents: Iterable[dict[str, Any]]) -> None:
    yaml.safe_dump_all(
        list(documents), sys.stdout, explicit_start=True, default_flow_style=False, sort_keys=False
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
        errors = lint_documents(documents)
        if errors:
            raise RenderError("rendered manifest failed static checks: " + "; ".join(errors))
        dump_documents(documents)
    except (RenderError, OSError, UnicodeDecodeError) as exc:
        print(f"render-boltz2: refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
