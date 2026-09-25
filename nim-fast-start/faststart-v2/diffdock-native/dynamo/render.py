#!/usr/bin/env python3
"""Render the offline DiffDock native-Dynamo scaffold.

This program performs no network, cloud, or Kubernetes calls. It only accepts
strict JSON receipts and writes YAML to stdout. The example interface contract
is unapproved, so a default render always fails closed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import posixpath
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment failure
    raise SystemExit("render: PyYAML is required") from exc


BASE_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = BASE_DIR / "manifests"
NAMESPACE = "nim-fast-start"
NIM_IMAGE = (
    "nvcr.io/nim/mit/diffdock@sha256:"
    "300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480"
)
NIM_DIGEST = NIM_IMAGE.rsplit("@sha256:", 1)[1]
CONTRACT_SCHEMA = "archvteams.nebius.ai/dynamo-restore-interface/v1"
RUN_SCHEMA = "archvteams.nebius.ai/diffdock-faststart-run/v1"
BINDING_SCHEMA = "archvteams.nebius.ai/diffdock-target-binding/v1"
TOOL_LAYOUT = "archvteams-public-dynamo-tools/v1"
SOURCE_REPOSITORY = "https://github.com/ai-dynamo/dynamo"
SOURCE_COMMIT = "f7f37be174d252590c4b56e25ff4262dd82466fd"
VALIDATOR_SHA256 = "245ae98a98db09c34924cd7a499b99da9eb35742667043aaee3e497c33268008"
FIXTURE_SHA256 = "f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977"
PROBE_EXECUTABLES = frozenset({"/usr/local/bin/python3", "/usr/bin/python3"})
ALLOWED_H100_NODES = frozenset(
    {
        "gpu-node-a.example.invalid",
    }
)

DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
DNS_SUBDOMAIN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(
    r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
)
CONTAINER_ID = re.compile(r"^containerd://[0-9a-f]{64}$")

# These binaries exist in the locally retained public Dynamo images, but none
# implements the bound, one-shot restore contract below.  Keep the denylist in
# the renderer so an approval receipt cannot accidentally turn a long-running
# informer or an unbound in-namespace helper into the restore worker.
KNOWN_NON_WORKER_EXECUTABLES = frozenset(
    {
        "/usr/local/bin/snapshot-agent",
        "/usr/local/bin/nsrestore",
        "/snapshot-binaries/nsrestore",
    }
)

REQUIRED_ARGUMENT_TEMPLATE = [
    "restore",
    "--target-namespace", "{target_namespace}",
    "--target-name", "{target_name}",
    "--target-uid", "{target_uid}",
    "--target-container", "{target_container}",
    "--target-container-id", "{target_container_id}",
    "--target-cgroup", "{target_cgroup}",
    "--target-pod-ip", "{target_pod_ip}",
    "--target-node", "{target_node}",
    "--target-pod-spec-sha256", "{target_pod_spec_sha256}",
    "--expected-image-id", "{expected_image_id}",
    "--run-id", "{run_id}",
    "--checkpoint-id", "{checkpoint_id}",
    "--artifact-version", "{artifact_version}",
    "--artifact-manifest-sha256", "{artifact_manifest_sha256}",
    "--tool-bundle-sha256", "{tool_bundle_sha256}",
    "--container-runtime-socket", "/run/containerd/containerd.sock",
    "--host-proc", "/host/proc",
    "--host-cgroup", "/sys/fs/cgroup",
    "--pod-resources-socket", "/var/lib/kubelet/pod-resources/kubelet.sock",
]


class RenderError(ValueError):
    """A fail-closed input or template validation error."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"cannot read {label}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RenderError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RenderError(
            f"{label} keys do not match schema; missing={missing}, extra={extra}"
        )


def _non_placeholder(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "REPLACE" in value:
        raise RenderError(f"{label} must be a non-placeholder string")
    if any(ord(char) < 32 for char in value):
        raise RenderError(f"{label} contains control characters")
    return value


def _digest(value: Any, label: str) -> str:
    value = _non_placeholder(value, label)
    if not SHA256.fullmatch(value):
        raise RenderError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _dns_label(value: Any, label: str, maximum: int = 63) -> str:
    value = _non_placeholder(value, label)
    if len(value) > maximum or not DNS_LABEL.fullmatch(value):
        raise RenderError(f"{label} must be a lowercase DNS label <= {maximum} chars")
    return value


def _dns_subdomain(value: Any, label: str) -> str:
    value = _non_placeholder(value, label)
    if len(value) > 253 or not DNS_SUBDOMAIN.fullmatch(value):
        raise RenderError(f"{label} must be a lowercase DNS subdomain")
    return value


def _timestamp(value: Any, label: str) -> str:
    value = _non_placeholder(value, label)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RenderError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RenderError(f"{label} must include a timezone")
    return value


def validate_contract(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema",
            "approved",
            "approval",
            "source",
            "worker_image",
            "worker_executable",
            "worker_executable_sha256",
            "argument_template",
            "probe_image",
            "probe_executable",
            "validator_sha256",
            "tool_bundle",
        },
        "contract",
    )
    if value["schema"] != CONTRACT_SCHEMA:
        raise RenderError("contract schema is not supported")
    if value["approved"] is not True:
        raise RenderError("restore interface is not explicitly approved")

    approval = value["approval"]
    if not isinstance(approval, dict):
        raise RenderError("contract.approval must be an object")
    _exact_keys(
        approval,
        {"reviewed_by", "reviewed_at", "evidence_sha256"},
        "contract.approval",
    )
    _non_placeholder(approval["reviewed_by"], "contract.approval.reviewed_by")
    _timestamp(approval["reviewed_at"], "contract.approval.reviewed_at")
    _digest(approval["evidence_sha256"], "contract.approval.evidence_sha256")

    source = value["source"]
    if not isinstance(source, dict):
        raise RenderError("contract.source must be an object")
    _exact_keys(source, {"repository", "commit", "patch_sha256"}, "contract.source")
    if source["repository"] != SOURCE_REPOSITORY:
        raise RenderError("contract source must be the public Dynamo repository")
    commit = _non_placeholder(source["commit"], "contract.source.commit")
    if commit != SOURCE_COMMIT:
        raise RenderError("contract.source.commit is not the pinned Phase 1 Dynamo commit")
    _digest(source["patch_sha256"], "contract.source.patch_sha256")

    worker_image = _non_placeholder(value["worker_image"], "contract.worker_image")
    if not IMAGE_DIGEST.fullmatch(worker_image):
        raise RenderError("contract.worker_image must be an immutable @sha256 image")
    executable = _non_placeholder(
        value["worker_executable"], "contract.worker_executable"
    )
    if (
        not executable.startswith("/")
        or posixpath.normpath(executable) != executable
        or executable in {"/bin/sh", "/bin/bash", "/usr/bin/env"}
        or any(char.isspace() for char in executable)
    ):
        raise RenderError("contract.worker_executable must be a normalized absolute binary path")
    if executable in KNOWN_NON_WORKER_EXECUTABLES:
        raise RenderError(
            "contract.worker_executable is a known Dynamo daemon or low-level "
            "helper, not the reviewed one-shot restore worker"
        )
    _digest(
        value["worker_executable_sha256"],
        "contract.worker_executable_sha256",
    )
    if value["argument_template"] != REQUIRED_ARGUMENT_TEMPLATE:
        raise RenderError("contract.argument_template is not the required bound interface")

    probe_image = _non_placeholder(value["probe_image"], "contract.probe_image")
    if not IMAGE_DIGEST.fullmatch(probe_image):
        raise RenderError("contract.probe_image must be an immutable @sha256 image")
    probe_executable = _non_placeholder(
        value["probe_executable"], "contract.probe_executable"
    )
    if probe_executable not in PROBE_EXECUTABLES:
        raise RenderError("contract.probe_executable must be an approved Python 3 path")
    if _digest(value["validator_sha256"], "contract.validator_sha256") != VALIDATOR_SHA256:
        raise RenderError("contract.validator_sha256 is not the reviewed DiffDock validator")

    tools = value["tool_bundle"]
    if not isinstance(tools, dict):
        raise RenderError("contract.tool_bundle must be an object")
    _exact_keys(tools, {"layout", "content_sha256"}, "contract.tool_bundle")
    if tools["layout"] != TOOL_LAYOUT:
        raise RenderError("contract tool bundle layout is not supported")
    _digest(tools["content_sha256"], "contract.tool_bundle.content_sha256")
    return value


def validate_run(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema",
            "run_id",
            "target_node",
            "target_glibc_version",
            "checkpoint_id",
            "artifact_version",
            "artifact_manifest_sha256",
            "artifact_pvc",
            "cache_pvc",
            "demand_at",
        },
        "run config",
    )
    if value["schema"] != RUN_SCHEMA:
        raise RenderError("run config schema is not supported")
    _timestamp(value["demand_at"], "demand_at")
    _dns_label(value["run_id"], "run_id", maximum=32)
    target_node = _dns_subdomain(value["target_node"], "target_node")
    if target_node not in ALLOWED_H100_NODES:
        raise RenderError("target_node is not one of the task's exact allowed H100 nodes")
    glibc_version = _non_placeholder(value["target_glibc_version"], "target_glibc_version")
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", glibc_version)
    if match is None:
        raise RenderError("target_glibc_version must be canonical MAJOR.MINOR")
    checkpoint_id = _dns_label(value["checkpoint_id"], "checkpoint_id")
    if checkpoint_id not in {
        "diffdock-native-f7-v1",
        "diffdock-native-f7-v2-rootfsless",
        "diffdock-native-f7-v3-buffered",
    }:
        raise RenderError("checkpoint_id is not an exact prepared DiffDock artifact")
    artifact_version = _non_placeholder(value["artifact_version"], "artifact_version")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", artifact_version):
        raise RenderError("artifact_version contains unsafe characters")
    _digest(value["artifact_manifest_sha256"], "artifact_manifest_sha256")
    _dns_subdomain(value["artifact_pvc"], "artifact_pvc")
    _dns_subdomain(value["cache_pvc"], "cache_pvc")
    if value["artifact_pvc"] == value["cache_pvc"]:
        raise RenderError("artifact_pvc and cache_pvc must be separate claims")
    return value


def _target_name(run_id: str) -> str:
    return f"dd-target-{run_id}"


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
    if value["schema"] != BINDING_SCHEMA:
        raise RenderError("binding schema is not supported")
    _timestamp(value["collected_at"], "binding.collected_at")
    if value["run_id"] != run["run_id"]:
        raise RenderError("binding run_id does not match run config")
    if value["namespace"] != NAMESPACE:
        raise RenderError("binding namespace does not match the fixed task namespace")
    if value["pod_name"] != _target_name(run["run_id"]):
        raise RenderError("binding pod_name does not match the rendered target")
    pod_uid = _non_placeholder(value["pod_uid"], "binding.pod_uid")
    try:
        parsed_uid = uuid.UUID(pod_uid)
    except ValueError as exc:
        raise RenderError("binding.pod_uid must be a UUID") from exc
    if str(parsed_uid) != pod_uid:
        raise RenderError("binding.pod_uid must use canonical lowercase UUID form")
    if value["container_name"] != "diffdock":
        raise RenderError("binding.container_name must be diffdock")
    container_id = _non_placeholder(value["container_id"], "binding.container_id")
    if not CONTAINER_ID.fullmatch(container_id):
        raise RenderError("binding.container_id must be a full containerd ID")
    cgroup = _non_placeholder(value["cgroup"], "binding.cgroup")
    if (
        len(cgroup) > 4096
        or not cgroup.startswith("/kubepods")
        or ".." in cgroup.split("/")
        or posixpath.normpath(cgroup) != cgroup
    ):
        raise RenderError("binding.cgroup must be an exact normalized kubepods path")
    pod_ip = _non_placeholder(value["pod_ip"], "binding.pod_ip")
    try:
        address = ipaddress.ip_address(pod_ip)
    except ValueError as exc:
        raise RenderError("binding.pod_ip must be an IP address") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise RenderError("binding.pod_ip is not a routable Pod address")
    if value["node"] != run["target_node"]:
        raise RenderError("binding node does not match run config")
    image_id = _non_placeholder(value["image_id"], "binding.image_id")
    normalized_image = image_id.removeprefix("docker-pullable://")
    if normalized_image != NIM_IMAGE:
        raise RenderError("binding.image_id is not the exact pinned DiffDock digest")
    _digest(value["pod_spec_sha256"], "binding.pod_spec_sha256")
    return value


def _load_template(name: str) -> list[dict[str, Any]]:
    path = MANIFEST_DIR / name
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RenderError(f"cannot load manifest template {name}: {exc}") from exc
    if not documents or any(not isinstance(document, dict) for document in documents):
        raise RenderError(f"manifest template {name} contains an invalid document")
    return documents


def _replace(value: Any, tokens: dict[str, Any], restore_args: list[str] | None) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, tokens, restore_args) for key, item in value.items()}
    if isinstance(value, list):
        if value == ["@@RESTORE_ARGS@@"]:
            if restore_args is None:
                raise RenderError("restore argument placeholder used without a binding")
            return list(restore_args)
        return [_replace(item, tokens, restore_args) for item in value]
    if isinstance(value, str):
        rendered = value
        for token, replacement in tokens.items():
            rendered = rendered.replace(token, str(replacement))
        return rendered
    return value


def _assert_no_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_placeholders(key)
            _assert_no_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_placeholders(item)
    elif isinstance(value, str) and "@@" in value:
        raise RenderError(f"unresolved manifest placeholder: {value}")


def _base_tokens(run: dict[str, Any], contract: dict[str, Any]) -> dict[str, str]:
    run_id = run["run_id"]
    return {
        "@@RUN_ID@@": run_id,
        "@@TARGET_NAME@@": _target_name(run_id),
        "@@TARGET_NODE@@": run["target_node"],
        "@@CHECKPOINT_ID@@": run["checkpoint_id"],
        "@@ARTIFACT_VERSION@@": run["artifact_version"],
        "@@ARTIFACT_MANIFEST_SHA256@@": run["artifact_manifest_sha256"],
        "@@ARTIFACT_PVC@@": run["artifact_pvc"],
        "@@CACHE_PVC@@": run["cache_pvc"],
        "@@TOOL_BUNDLE_SHA256@@": contract["tool_bundle"]["content_sha256"],
        "@@CANARY_SERVICE@@": f"dd-canary-{run_id}",
        "@@QUALIFIED_SERVICE@@": f"dd-qualified-{run_id}",
        "@@TARGET_NETWORK_POLICY@@": f"dd-target-{run_id}",
        "@@PROBE_NETWORK_POLICY@@": f"dd-probe-{run_id}",
        "@@WORKER_NAME@@": f"dd-restore-{run_id}",
        "@@WORKER_IMAGE@@": contract["worker_image"],
        "@@WORKER_EXECUTABLE@@": contract["worker_executable"],
        "@@WORKER_EXECUTABLE_SHA256@@": contract["worker_executable_sha256"],
        "@@PROBE_NAME@@": f"dd-semantic-{run_id}",
        "@@PROBE_IMAGE@@": contract["probe_image"],
        "@@PROBE_EXECUTABLE@@": contract["probe_executable"],
        "@@PROBE_RUN_ID_1@@": f"{run_id}-semantic-a",
        "@@PROBE_RUN_ID_2@@": f"{run_id}-semantic-b",
        "@@DEMAND_AT@@": run["demand_at"],
    }


def render_target(run: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    documents = _load_template("target.yaml.tmpl")
    rendered = _replace(documents, _base_tokens(run, contract), None)
    _assert_no_placeholders(rendered)
    return rendered


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
    try:
        restore_args = [argument.format_map(fields) for argument in REQUIRED_ARGUMENT_TEMPLATE]
    except KeyError as exc:  # pragma: no cover - guarded by constant fields
        raise RenderError(f"missing restore binding field: {exc}") from exc
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
    documents = _load_template("restore-worker.yaml.tmpl")
    rendered = _replace(documents, tokens, restore_args)
    _assert_no_placeholders(rendered)
    return rendered


def render_probe(
    run: dict[str, Any], contract: dict[str, Any], binding: dict[str, Any]
) -> list[dict[str, Any]]:
    validator_path = BASE_DIR.parent / "validate_diffdock.py"
    fixture_path = BASE_DIR.parent / "fixtures" / "1ubq-aspirin-request.json"
    try:
        validator_bytes = validator_path.read_bytes()
    except OSError as exc:
        raise RenderError(f"cannot read strict DiffDock validator: {exc}") from exc
    import hashlib

    if hashlib.sha256(validator_bytes).hexdigest() != VALIDATOR_SHA256:
        raise RenderError("strict DiffDock validator source digest changed")
    try:
        validator_source = validator_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - repository corruption
        raise RenderError("strict DiffDock validator is not UTF-8") from exc
    try:
        fixture_bytes = fixture_path.read_bytes()
        fixture_source = fixture_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RenderError(f"cannot read strict DiffDock request fixture: {exc}") from exc
    if hashlib.sha256(fixture_bytes).hexdigest() != FIXTURE_SHA256:
        raise RenderError("strict DiffDock request fixture digest changed")
    tokens = _base_tokens(run, contract)
    tokens.update(
        {
            "@@TARGET_UID@@": binding["pod_uid"],
            "@@TARGET_POD_SPEC_SHA256@@": binding["pod_spec_sha256"],
            "@@VALIDATOR_SHA256@@": VALIDATOR_SHA256,
            "@@FIXTURE_SHA256@@": FIXTURE_SHA256,
            "@@VALIDATOR_SOURCE@@": validator_source,
            "@@REQUEST_FIXTURE@@": fixture_source,
        }
    )
    documents = _load_template("semantic-probe.yaml.tmpl")
    rendered = _replace(documents, tokens, None)
    _assert_no_placeholders(rendered)
    return rendered


def dump_documents(documents: Iterable[dict[str, Any]]) -> None:
    yaml.safe_dump_all(
        list(documents),
        sys.stdout,
        explicit_start=True,
        default_flow_style=False,
        sort_keys=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("target", "restore", "probe"):
        child = subparsers.add_parser(mode)
        child.add_argument("--contract", type=Path, required=True)
        child.add_argument("--run-config", type=Path, required=True)
        if mode in {"restore", "probe"}:
            child.add_argument("--binding", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        contract = validate_contract(_read_json(args.contract, "contract"))
        run = validate_run(_read_json(args.run_config, "run config"))
        if args.mode == "target":
            documents = render_target(run, contract)
        else:
            binding = validate_binding(_read_json(args.binding, "binding"), run)
            if args.mode == "restore":
                documents = render_restore(run, contract, binding)
            else:
                documents = render_probe(run, contract, binding)
        try:
            from lint_manifest import lint_documents
        except ImportError:  # pragma: no cover - package import path
            from .lint_manifest import lint_documents
        errors = lint_documents(documents)
        if errors:
            raise RenderError("rendered manifest failed static checks: " + "; ".join(errors))
        dump_documents(documents)
    except RenderError as exc:
        print(f"render: refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
