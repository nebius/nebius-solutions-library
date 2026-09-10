#!/usr/bin/env python3
"""Render the capture agent from the single reviewed worker contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from dynamo.render import RenderError, validate_contract


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "snapshot-agent.yaml.tmpl"


def _contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read worker contract: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RenderError("worker contract must be an object")
    return validate_contract(value)


def render(contract: dict[str, Any], *, require_release: bool = True) -> dict[str, Any]:
    if require_release and contract["release_ready"] is not True:
        raise RenderError(f"worker release gate is closed: {contract['release_blocker']}")
    if "direct" not in contract["supported_image_io_modes"]:
        raise RenderError("capture worker contract does not support direct image I/O")
    try:
        raw = TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"cannot read snapshot-agent template: {exc}") from exc
    if raw.count("@@WORKER_IMAGE@@") != 1:
        raise RenderError("snapshot-agent template worker placeholder is not exact")
    rendered = raw.replace("@@WORKER_IMAGE@@", contract["worker_image"])
    if "@@" in rendered:
        raise RenderError("snapshot-agent template contains an unresolved placeholder")
    try:
        document = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise RenderError(f"rendered snapshot-agent manifest is invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise RenderError("rendered snapshot-agent manifest is not an object")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument(
        "--allow-performance-validation-worker",
        action="store_true",
        help="explicitly allow the pinned performance-validation-only worker",
    )
    args = parser.parse_args()
    try:
        contract = _contract(args.contract)
        if args.allow_performance_validation_worker:
            if (
                contract["release_ready"] is not False
                or contract["worker_classification"] != "performance-validation-only"
                or not contract["release_blocker"]
            ):
                raise RenderError(
                    "performance-validation acknowledgement does not match the contract"
                )
            document = render(contract, require_release=False)
        else:
            document = render(contract, require_release=True)
    except RenderError as exc:
        print(f"render_snapshot_agent: refused: {exc}", file=sys.stderr)
        return 2
    yaml.safe_dump(document, sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
