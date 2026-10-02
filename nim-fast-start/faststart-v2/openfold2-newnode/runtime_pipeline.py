#!/usr/bin/env python3
"""Run the frozen OpenFold2 pipeline with one evidence-backed new node admitted."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import sys
from pathlib import Path

from node_admission import AdmissionError, validate_admission


VALIDATOR_SHA256 = "4f3e70ef29ea9cd3113c09e6f63bd15b4d9826bf64d7d16972c6c3d0eef3090e"
MODULES = {"render", "lint", "bind", "evidence"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-root", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--node-json", type=Path, required=True)
    parser.add_argument("module", choices=sorted(MODULES))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    pipeline_root = args.pipeline_root.resolve(strict=True)
    if not pipeline_root.is_dir():
        parser.error("pipeline root is not a directory")
    validator = pipeline_root.parent / "validate_openfold2.py"
    if _sha256(validator) != VALIDATOR_SHA256:
        parser.error("frozen validator does not have the approved SHA-256")
    try:
        admission = validate_admission(args.admission, args.node_json)
    except AdmissionError as exc:
        parser.error(str(exc))
    node_name = admission["node"]["name"]

    sys.path.insert(0, str(pipeline_root))
    render = importlib.import_module("render")
    lint_manifest = importlib.import_module("lint_manifest")
    render.ALLOWED_H100_NODES = frozenset({node_name})
    lint_manifest.ALLOWED_H100_NODES = {node_name}

    forwarded = args.arguments
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if args.module == "render":
        return int(render.main(forwarded))
    if args.module == "lint":
        return int(lint_manifest.main(forwarded))
    if args.module == "bind":
        bind_target = importlib.import_module("bind_target")
        return int(bind_target.main(forwarded))
    evidence = importlib.import_module("evidence")
    return int(evidence.main(forwarded))


if __name__ == "__main__":
    raise SystemExit(main())
