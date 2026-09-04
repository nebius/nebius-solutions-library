#!/usr/bin/env python3
"""Split one reviewed multi-object manifest into support objects and one primary."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml


NAMESPACE = "nim-fast-start"
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
EXPECTED_KINDS = {
    "target": ["NetworkPolicy", "NetworkPolicy", "Pod", "Service", "Service"],
    "restore-worker": ["Job", "Role", "RoleBinding", "ServiceAccount"],
    "semantic-probe": ["ConfigMap", "Job"],
}
PRIMARY_KIND = {
    "target": "Pod",
    "restore-worker": "Job",
    "semantic-probe": "Job",
}


class SplitError(ValueError):
    """The rendered bundle is not the exact reviewed shape."""


def _load(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise SplitError("input must be a regular non-symlink file")
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SplitError(f"cannot parse input: {type(exc).__name__}") from exc
    if any(not isinstance(document, dict) for document in documents):
        raise SplitError("every manifest document must be an object")
    return documents


def split(input_path: Path, output_directory: Path, bundle: str) -> dict[str, Any]:
    documents = _load(input_path)
    kinds = sorted(str(document.get("kind", "")) for document in documents)
    if kinds != EXPECTED_KINDS[bundle]:
        raise SplitError(f"{bundle} manifest kind set changed")
    identities: set[tuple[str, str, str]] = set()
    for document in documents:
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            raise SplitError("manifest metadata is missing")
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        if (
            namespace != NAMESPACE
            or not isinstance(name, str)
            or DNS_LABEL.fullmatch(name) is None
            or len(name) > 63
            or "uid" in metadata
        ):
            raise SplitError("manifest identity is not a fresh namespaced object")
        identity = (str(document.get("apiVersion", "")), document["kind"], name)
        if not identity[0] or identity in identities:
            raise SplitError("manifest object identity is missing or duplicated")
        identities.add(identity)

    primary = [
        document for document in documents if document["kind"] == PRIMARY_KIND[bundle]
    ]
    if len(primary) != 1:
        raise SplitError(f"{bundle} must have exactly one primary object")
    support = [document for document in documents if document is not primary[0]]
    if output_directory.is_symlink() or os.path.lexists(output_directory):
        raise SplitError("output directory already exists")
    output_directory.mkdir(mode=0o700)
    support_directory = output_directory / "support"
    support_directory.mkdir(mode=0o700)

    def write_exclusive(path: Path, value: dict[str, Any]) -> None:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    write_exclusive(output_directory / "primary.json", primary[0])
    for index, document in enumerate(support, 1):
        write_exclusive(
            support_directory / f"{index:02d}-{document['kind'].lower()}.json",
            document,
        )
    return {
        "schema": "archvteams.nebius.ai/rendered-manifest-split/v1",
        "bundle": bundle,
        "primary": str(output_directory / "primary.json"),
        "primary_kind": PRIMARY_KIND[bundle],
        "support_count": len(support),
        "object_count": len(documents),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--bundle", choices=tuple(EXPECTED_KINDS), required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = split(args.input, args.output_directory, args.bundle)
    except (SplitError, OSError) as exc:
        print(f"split-manifest: refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
