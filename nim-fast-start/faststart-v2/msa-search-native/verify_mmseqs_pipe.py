#!/usr/bin/env python3
"""Verify the retained MSA Search MMseqs-to-API pipe topology."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


PIPE = re.compile(r"^pipe:\[[0-9]+\]$")


class PipeError(ValueError):
    """The restored process tree does not retain the reviewed pipe topology."""


def _processes(proc_root: Path) -> list[tuple[int, str, Path]]:
    processes: list[tuple[int, str, Path]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise PipeError(f"cannot enumerate proc root: {type(exc).__name__}") from exc
    for entry in entries:
        if not entry.name.isdecimal() or not entry.is_dir():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        processes.append((int(entry.name), comm, entry))
    return sorted(processes)


def _pipe(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    return target if PIPE.fullmatch(target) else None


def inspect(proc_root: Path) -> dict[str, Any]:
    processes = _processes(proc_root)
    mmseqs = [(pid, root, _pipe(root / "fd" / "1")) for pid, comm, root in processes if comm == "mmseqs"]
    api_workers = [
        (pid, root, _pipe(root / "fd" / "24"))
        for pid, comm, root in processes
        if comm == "start_server"
    ]
    mmseqs = [(pid, root, pipe) for pid, root, pipe in mmseqs if pipe is not None]
    api_workers = [(pid, root, pipe) for pid, root, pipe in api_workers if pipe is not None]
    matches = [
        (mmseqs_pid, api_pid, pipe)
        for mmseqs_pid, _, pipe in mmseqs
        for api_pid, _, api_pipe in api_workers
        if pipe == api_pipe
    ]
    if len(matches) != 1:
        raise PipeError(
            "expected exactly one MMseqs fd 1 / API worker fd 24 shared pipe; "
            f"matches={len(matches)} mmseqs_fd1={len(mmseqs)} api_fd24={len(api_workers)}"
        )
    mmseqs_pid, api_pid, pipe = matches[0]
    return {
        "schema": "archvteams.nebius.ai/msa-search-mmseqs-pipe/v1",
        "status": "PASS",
        "shared_pipe_verified": True,
        "mmseqs_pid": mmseqs_pid,
        "mmseqs_fd": 1,
        "api_worker_pid": api_pid,
        "api_worker_fd": 24,
        "pipe": pipe,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args(argv)
    try:
        receipt = inspect(args.proc_root)
    except PipeError as exc:
        print(
            json.dumps(
                {
                    "schema": "archvteams.nebius.ai/msa-search-mmseqs-pipe/v1",
                    "status": "FAIL",
                    "shared_pipe_verified": False,
                    "error": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
