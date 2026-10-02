#!/usr/bin/env python3
"""Add only the two named image-pull ServiceAccount references for a fresh node."""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml


RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
RUN_LABEL = "archvteams.nebius.ai/run-id"
TARGET_SECRET = "archvteams-2407-registry-pull"
WORKER_SECRET = "archvteams-2407-registry-pull"


class OverlayError(ValueError):
    """The rendered manifest is not the exact expected pipeline output."""


def _read(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise OverlayError("input must be a regular non-symlink file")
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise OverlayError(f"cannot read YAML: {type(exc).__name__}") from exc
    if not documents or any(not isinstance(item, dict) for item in documents):
        raise OverlayError("input contains a malformed document")
    return documents


def _write(path: Path, documents: list[dict[str, Any]]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink() or os.path.lexists(path):
        raise OverlayError(f"output already exists: {path}")
    payload = yaml.safe_dump_all(
        documents,
        explicit_start=True,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent / path.name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        handle.write(payload)
        handle.flush()
        os.fsync(descriptor)


def _run_id(value: str) -> str:
    if len(value) > 30 or RUN_ID.fullmatch(value) is None:
        raise OverlayError("run ID must be a lowercase DNS label no longer than 30 characters")
    return value


def target_overlay(
    documents: list[dict[str, Any]], run_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target_name = f"of2-target-{run_id}"
    matches = [
        item
        for item in documents
        if item.get("kind") == "Pod"
        and item.get("metadata", {}).get("name") == target_name
        and item.get("metadata", {}).get("namespace") == "nim-fast-start"
        and item.get("metadata", {}).get("labels", {}).get(RUN_LABEL) == run_id
    ]
    if len(matches) != 1:
        raise OverlayError("target manifest has no unique run-bound target Pod")
    spec = matches[0].get("spec")
    if not isinstance(spec, dict):
        raise OverlayError("target Pod spec is malformed")
    if spec.get("imagePullSecrets") not in (None, []):
        raise OverlayError("target Pod already contains direct image-pull secrets")
    if spec.get("serviceAccountName") not in (None, "default"):
        raise OverlayError("target Pod already selects a non-default ServiceAccount")
    service_account_name = f"of2-target-pull-{run_id}"
    spec["serviceAccountName"] = service_account_name
    service_account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": service_account_name,
            "namespace": "nim-fast-start",
            "labels": {
                "app.kubernetes.io/name": "openfold2",
                "app.kubernetes.io/component": "target-image-pull",
                RUN_LABEL: run_id,
            },
        },
        "automountServiceAccountToken": False,
        "imagePullSecrets": [{"name": TARGET_SECRET}],
    }
    return documents, [service_account]


def restore_overlay(documents: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    expected_name = f"of2-restore-{run_id}"
    matches = [
        item
        for item in documents
        if item.get("kind") == "ServiceAccount"
        and item.get("metadata", {}).get("name") == expected_name
        and item.get("metadata", {}).get("namespace") == "nim-fast-start"
        and item.get("metadata", {}).get("labels", {}).get(RUN_LABEL) == run_id
    ]
    if len(matches) != 1:
        raise OverlayError("restore manifest has no unique run-bound ServiceAccount")
    account = matches[0]
    if account.get("imagePullSecrets") not in (None, []):
        raise OverlayError("worker ServiceAccount already contains image-pull secrets")
    account["imagePullSecrets"] = [{"name": WORKER_SECRET}]
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    target = subparsers.add_parser("target")
    target.add_argument("--run-id", required=True)
    target.add_argument("--input", type=Path, required=True)
    target.add_argument("--output", type=Path, required=True)
    target.add_argument("--service-account-output", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--run-id", required=True)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run_id = _run_id(args.run_id)
        documents = _read(args.input)
        if args.mode == "target":
            overlaid, account = target_overlay(documents, run_id)
            _write(args.output, overlaid)
            _write(args.service_account_output, account)
        else:
            _write(args.output, restore_overlay(documents, run_id))
    except OverlayError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
