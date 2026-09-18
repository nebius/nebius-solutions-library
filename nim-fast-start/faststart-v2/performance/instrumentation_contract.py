#!/usr/bin/env python3
"""Build the homogeneous source contract for one fresh timing cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence


SCHEMA = "archvteams.nebius.ai/fresh-cohort-instrumentation-contract/v1"
ROOT = Path(__file__).resolve().parent.parent
COMMON_PATHS = (
    "dynamo/__init__.py",
    "dynamo/bind_target.py",
    "dynamo/evidence.py",
    "dynamo/lint_manifest.py",
    "dynamo/render.py",
    "performance/aggregate_fresh_cohort.py",
    "performance/clock_sample.sh",
    "performance/instrumentation_contract.py",
    "performance/qualification_receipt.py",
    "performance/run_fresh_cohort.sh",
    "performance/split_manifest.py",
    "performance/uid_cleanup.sh",
)
MODEL_PATHS = {
    "openfold2": (
        "dynamo/manifests/restore-worker.yaml.tmpl",
        "dynamo/manifests/semantic-probe.yaml.tmpl",
        "dynamo/manifests/target.yaml.tmpl",
        "dynamo/restore-interface.live.json",
        "dynamo/run_provisioned_trial.sh",
        "validate_openfold2.py",
    ),
    "boltz2": (
        "boltz2-native/bind_target.py",
        "boltz2-native/render.py",
        "boltz2-native/restore-interface.live.json",
        "boltz2-native/run_one_native_trial.sh",
        "boltz2-native/validate_boltz2.py",
        "dynamo/manifests/restore-worker.yaml.tmpl",
        "dynamo/manifests/semantic-probe.yaml.tmpl",
        "dynamo/manifests/target.yaml.tmpl",
        "timing_evidence.py",
    ),
}


class InstrumentationContractError(ValueError):
    """A contract source is missing or unsafe."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InstrumentationContractError(
            f"contract source must be a regular non-symlink file: {path}"
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise InstrumentationContractError(
            f"cannot hash contract source: {type(exc).__name__}"
        ) from exc
    return digest.hexdigest()


def build_contract(model: str, root: Path = ROOT) -> dict[str, Any]:
    if model not in MODEL_PATHS:
        raise InstrumentationContractError("model must be openfold2 or boltz2")
    resolved_root = root.resolve(strict=True)
    items = [
        {"path": relative, "sha256": _sha256(resolved_root / relative)}
        for relative in sorted((*COMMON_PATHS, *MODEL_PATHS[model]))
    ]
    payload = json.dumps(
        items,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return {
        "schema": SCHEMA,
        "model": model,
        "source_count": len(items),
        "sources": items,
        "instrumentation_contract_sha256": hashlib.sha256(payload).hexdigest(),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(MODEL_PATHS), required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        json.dumps(
            build_contract(args.model),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
