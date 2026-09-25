#!/usr/bin/env python3
"""Offline tests for the release-gated capture-agent renderer."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "render_snapshot_agent.py"
SPEC = importlib.util.spec_from_file_location("render_snapshot_agent", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class SnapshotAgentRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = module._contract(ROOT / "dynamo" / "restore-interface.live.json")

    def test_candidate_contract_is_deliberately_blocked_for_live_render(self) -> None:
        with self.assertRaisesRegex(module.RenderError, "release gate is closed"):
            module.render(self.contract)

    def test_approved_release_renders_exact_single_contract_image(self) -> None:
        contract = copy.deepcopy(self.contract)
        contract["release_ready"] = True
        contract["release_blocker"] = ""
        contract["worker_classification"] = "full-agent-compliance-release"
        document = module.render(contract)
        container = document["spec"]["containers"][0]
        self.assertEqual(container["name"], "agent")
        self.assertEqual(container["image"], contract["worker_image"])
        self.assertNotIn("@@", json.dumps(document))

    def test_explicit_performance_validation_render_is_honest(self) -> None:
        document = module.render(self.contract, require_release=False)
        self.assertEqual(
            document["spec"]["containers"][0]["image"],
            self.contract["worker_image"],
        )
        self.assertFalse(self.contract["release_ready"])
        self.assertEqual(
            self.contract["worker_classification"], "performance-validation-only"
        )

    def test_contract_without_direct_capture_support_is_rejected(self) -> None:
        contract = copy.deepcopy(self.contract)
        contract["release_ready"] = True
        contract["release_blocker"] = ""
        contract["worker_classification"] = "full-agent-compliance-release"
        contract["supported_image_io_modes"] = ["buffered"]
        with self.assertRaisesRegex(module.RenderError, "does not support direct"):
            module.render(contract)


if __name__ == "__main__":
    unittest.main()
