#!/usr/bin/env python3
"""Bind one scheduled Evo2 placeholder to the generic one-shot worker."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import render


BASE_TRANSLATION_NODE = "gpu-node-b.example.invalid"


def _load_base_bind() -> Any:
    path = render.BASE_DYNAMO / "bind_target.py"
    spec = importlib.util.spec_from_file_location("openfold2_bind_for_evo2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["render"] = render.base
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base_bind = _load_base_bind()


def _translate_affinity(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("key") == "kubernetes.io/hostname" and value.get("operator") == "In":
            value["values"] = [BASE_TRANSLATION_NODE]
        for item in value.values():
            _translate_affinity(item)
    elif isinstance(value, list):
        for item in value:
            _translate_affinity(item)


def _translated_pod(pod: dict[str, Any], run_id: str) -> dict[str, Any]:
    translated = copy.deepcopy(pod)
    metadata = translated["metadata"]
    metadata["name"] = f"of2-target-{run_id}"
    metadata["labels"]["app.kubernetes.io/name"] = "openfold2"
    metadata["annotations"]["nvidia.com/snapshot-target-containers"] = "openfold2"
    spec = translated["spec"]
    spec["nodeName"] = BASE_TRANSLATION_NODE
    _translate_affinity(spec.get("affinity"))
    container = spec["containers"][0]
    container["name"] = "openfold2"
    container["image"] = render.OPENFOLD_IMAGE
    for status in translated["status"].get("containerStatuses", []):
        if status.get("name") == render.CONTAINER_NAME:
            status["name"] = "openfold2"
            status["image"] = render.OPENFOLD_IMAGE
            status["imageID"] = f"docker-pullable://{render.OPENFOLD_IMAGE}"
    return translated


def build_binding(
    pod: dict[str, Any],
    run: dict[str, Any],
    contract: dict[str, Any],
    collected_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if pod.get("metadata", {}).get("name") != render._target_name(run["run_id"]):
        raise base_bind.BindingError("live Pod name is not the Evo2 target")
    if pod.get("spec", {}).get("nodeName") != render.TARGET_NODE:
        raise base_bind.BindingError("live target is not scheduled on the pinned H200 node")
    containers = pod.get("spec", {}).get("containers", [])
    if len(containers) != 1:
        raise base_bind.BindingError("live Evo2 target must contain exactly one container")
    container = containers[0]
    if container.get("name") != render.CONTAINER_NAME or container.get("image") != render.NIM_IMAGE:
        raise base_bind.BindingError("live target container is not the pinned Evo2 image")
    statuses = pod.get("status", {}).get("containerStatuses", [])
    matches = [item for item in statuses if item.get("name") == render.CONTAINER_NAME]
    if len(matches) != 1:
        raise base_bind.BindingError("live Pod does not have one Evo2 container status")
    image_id = str(matches[0].get("imageID", "")).removeprefix("docker-pullable://")
    if image_id != render.NIM_IMAGE:
        raise base_bind.BindingError("live image ID is not the pinned Evo2 digest")

    base_run = copy.deepcopy(run)
    base_run.pop("image_io_mode")
    base_run["schema"] = render.base.RUN_SCHEMA
    base_run["target_node"] = BASE_TRANSLATION_NODE
    base_contract = copy.deepcopy(contract)
    base_contract["validator_sha256"] = render.base.VALIDATOR_SHA256
    binding, _ = base_bind.build_binding(
        _translated_pod(pod, run["run_id"]), base_run, base_contract, collected_at
    )
    digest = base_bind.pod_spec_sha256(pod["spec"])
    binding.update(
        {
            "schema": render.BINDING_SCHEMA,
            "pod_name": render._target_name(run["run_id"]),
            "container_name": render.CONTAINER_NAME,
            "image_id": render.NIM_IMAGE,
            "node": render.TARGET_NODE,
            "pod_spec_sha256": digest,
        }
    )
    render.validate_binding(binding, run)
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": binding["pod_uid"]},
        {
            "op": "add",
            "path": "/metadata/annotations/archvteams.nebius.ai~1target-pod-spec-sha256",
            "value": digest,
        },
    ]
    return binding, patch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--run-config", type=Path, required=True)
    parser.add_argument("--pod-json", type=Path, required=True)
    parser.add_argument("--collected-at", required=True)
    parser.add_argument("--binding-output", type=Path, required=True)
    parser.add_argument("--patch-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run = render.validate_run(render._read_json(args.run_config, "run config"))
        contract = render.validate_contract(render._read_json(args.contract, "contract"))
        pod = render._read_json(args.pod_json, "live Pod")
        binding, patch = build_binding(pod, run, contract, args.collected_at)
        base_bind._write_exclusive(args.binding_output, binding)
        base_bind._write_exclusive(args.patch_output, patch)
    except (render.RenderError, base_bind.BindingError, OSError, KeyError, IndexError) as exc:
        print(f"bind-evo2: refused: {exc}", file=sys.stderr)
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
