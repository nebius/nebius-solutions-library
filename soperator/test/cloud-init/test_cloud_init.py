"""Run with Python 3 and Terraform; no providers or cloud credentials needed."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TEMPLATE = Path(__file__).resolve().parents[2] / "modules/k8s/templates/cloud_init.yaml.tftpl"


def render(apparmor, ssh=False, nvidia=False):
    variables = {
        "use_default_apparmor_profile": apparmor,
        "ssh_users": [{"name": "tester", "public_keys": ["ssh-ed25519 test"]}] if ssh else [],
        "nvidia_config_lines": ["options nvidia NVreg_EnableMSI=1"] if nvidia else [],
    }
    template = f"templatefile({json.dumps(str(TEMPLATE))}, {json.dumps(variables)})"
    expression = f"jsonencode(yamldecode({template}))" if any([apparmor, ssh, nvidia]) else f"jsonencode({template})"
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            ["terraform", "console", "-no-color"], input=expression + "\n",
            cwd=directory, text=True, capture_output=True, check=True,
        )
    if not any([apparmor, ssh, nvidia]):
        assert json.loads(json.loads(result.stdout)).strip() == "#cloud-config"
        return {}
    return json.loads(json.loads(result.stdout))


class CloudInitTest(unittest.TestCase):
    def test_config_combinations(self):
        for apparmor, ssh, nvidia in itertools.product([False, True], repeat=3):
            with self.subTest(apparmor=apparmor, ssh=ssh, nvidia=nvidia):
                config = render(apparmor, ssh, nvidia)
                self.assertEqual("bootcmd" in config, apparmor)
                self.assertEqual("users" in config, ssh)
                self.assertEqual("write_files" in config, nvidia)
                self.assertEqual("runcmd" in config, nvidia)
                if ssh:
                    self.assertEqual(config["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 test"])
                if nvidia:
                    self.assertEqual(config["write_files"][0]["content"].strip(), "options nvidia NVreg_EnableMSI=1")
                if apparmor:
                    self.assertEqual(len(config["bootcmd"]), 1)
                    subprocess.run(["sh", "-n"], input=config["bootcmd"][0], text=True, check=True)

    def test_loader_failures_and_repeated_boots(self):
        script = render(apparmor=True)["bootcmd"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = root / "profiles"
            loaded = root / "loaded"
            args = root / "args"
            parser = root / "apparmor_parser"
            parser.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ARGS"\ncat > "$LOADED"\nexit "$PARSER_EXIT"\n')
            parser.chmod(0o755)
            # Substitute only the kernel interface; run the actual rendered shell.
            script = script.replace("/sys/kernel/security/apparmor/profiles", str(profiles))
            env = dict(os.environ, PATH=directory + os.pathsep + os.defpath,
                       LOADED=str(loaded), ARGS=str(args), PARSER_EXIT="0")

            with self.subTest(scenario="kernel interface unavailable"):
                result = subprocess.run(["/bin/sh"], input=script, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("kernel interface is unavailable", result.stderr)
                self.assertFalse(loaded.exists())

            with self.subTest(scenario="parser fails"):
                profiles.write_text("soperator-default (enforce)\n")
                result = subprocess.run(["/bin/sh"], input=script, env=dict(env, PARSER_EXIT="1"), text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

            for scenario, content in [
                ("profile missing", ""),
                ("profile in complain mode", "soperator-default (complain)\n"),
                ("different profile loaded", "other (enforce)\n"),
            ]:
                with self.subTest(scenario=scenario):
                    profiles.write_text(content)
                    result = subprocess.run(["/bin/sh"], input=script, env=env, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("not in enforce mode", result.stderr)

            with self.subTest(scenario="profile reloaded on every boot"):
                profiles.write_text("soperator-default (enforce)\n")
                args.write_text("")
                for _ in range(2):
                    subprocess.run(["/bin/sh"], input=script, env=env, text=True, check=True)
                self.assertEqual(args.read_text().splitlines(), ["--replace", "--replace"])
                self.assertIn("profile soperator-default flags=", loaded.read_text())

            with self.subTest(scenario="parser unavailable"):
                # An empty PATH makes parser discovery fail even on an AppArmor host.
                result = subprocess.run(["/bin/sh"], input=script, env=dict(env, PATH=str(root / "missing")), text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("parser is unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
