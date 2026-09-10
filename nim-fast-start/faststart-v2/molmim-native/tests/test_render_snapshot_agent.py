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
        init_container = document["spec"]["initContainers"][0]
        self.assertEqual(
            init_container["image"],
            "docker.io/library/busybox:1.36@sha256:"
            "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
        )
        init_script = init_container["command"][2]
        self.assertIn(
            "e1eeddafb76c80cf19b78dd17cf524da331d8d9a18df235108d58087ab6f9ccf",
            init_script,
        )
        self.assertIn(
            "ebbe5e221b6b331bb84efbdfea7adb88e9dddab62a2ea901598bad09fe7f76a0",
            init_script,
        )
        volumes = {item["name"]: item for item in document["spec"]["volumes"]}
        self.assertEqual(
            volumes["config-source"]["configMap"]["name"],
            "archvteams-2407-native-snapshot-config",
        )
        self.assertEqual(volumes["config"]["emptyDir"], {"sizeLimit": "1Mi"})
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
