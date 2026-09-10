#!/usr/bin/env python3
"""Offline tests for the restored MSA Search pipe qualification."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "verify_mmseqs_pipe.py"
SPEC = importlib.util.spec_from_file_location("verify_mmseqs_pipe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pipe_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pipe_check
SPEC.loader.exec_module(pipe_check)


def add_process(root: Path, pid: int, comm: str, fds: dict[int, str]) -> None:
    process = root / str(pid)
    (process / "fd").mkdir(parents=True)
    (process / "comm").write_text(comm + "\n", encoding="utf-8")
    for descriptor, target in fds.items():
        os.symlink(target, process / "fd" / str(descriptor))


class PipeTests(unittest.TestCase):
    def test_exact_retained_shared_pipe_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_process(root, 100, "start_server", {9: "pipe:[1]"})
            add_process(root, 101, "start_server", {24: "pipe:[4242]"})
            add_process(root, 102, "mmseqs", {1: "pipe:[4242]", 2: "pipe:[9]"})
            receipt = pipe_check.inspect(root)
            self.assertEqual(receipt["status"], "PASS")
            self.assertTrue(receipt["shared_pipe_verified"])
            self.assertEqual(receipt["mmseqs_pid"], 102)
            self.assertEqual(receipt["mmseqs_fd"], 1)
            self.assertEqual(receipt["api_worker_pid"], 101)
            self.assertEqual(receipt["api_worker_fd"], 24)
            self.assertEqual(receipt["pipe"], "pipe:[4242]")

    def test_disconnected_or_wrong_descriptor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_process(root, 101, "start_server", {24: "pipe:[1]"})
            add_process(root, 102, "mmseqs", {1: "pipe:[2]"})
            with self.assertRaisesRegex(pipe_check.PipeError, "exactly one"):
                pipe_check.inspect(root)

    def test_ambiguous_multiple_matches_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_process(root, 101, "start_server", {24: "pipe:[1]"})
            add_process(root, 102, "mmseqs", {1: "pipe:[1]"})
            add_process(root, 103, "mmseqs", {1: "pipe:[1]"})
            with self.assertRaisesRegex(pipe_check.PipeError, "matches=2"):
                pipe_check.inspect(root)


if __name__ == "__main__":
    unittest.main()
