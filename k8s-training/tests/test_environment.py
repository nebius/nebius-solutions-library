"""Exercise backend generation without credentials or cloud access.

Run with: python3 -m unittest discover -s k8s-training/tests -p 'test_*.py'
Requires Bash and jq. Every Nebius command is replaced by a local stub.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "environment.sh"


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("jq"):
            self.skipTest("jq is required by environment.sh")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        # Cover all response fields consumed by the setup script. No real CLI runs.
        self.stub("nebius", '''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_CALL_LOG"
printf '%s\n' '{"metadata":{"id":"mock-id"},"items":[{"metadata":{"id":"mock-subnet","name":"unused"}}],"memberships":[{"spec":{"member_id":"mock-id"}}],"status":{"aws_access_key_id":"mock-access","secret":"mock-secret"}}'
''')
        self.stub("md5sum", "#!/bin/sh\ncat >/dev/null\nprintf 'mockhash  -\\n'\n")
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.root),
            "TEST_CALL_LOG": str(self.log),
            "NEBIUS_TENANT_ID": "mock-tenant",
            "NEBIUS_PROJECT_ID": "mock-project",
            "NEBIUS_REGION": "eu-west2",
        }

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def source(self, variables=None, command='source "$1"'):
        return subprocess.run(
            ["bash", "-c", command, "test", str(SCRIPT)],
            cwd=self.root,
            env={**self.env, **(variables or {})},
            text=True,
            capture_output=True,
        )

    def test_invalid_cluster_names_stop_before_cloud_calls(self):
        for name in (None, "", " ", "bad name", 'bad"name', "bad\nname", "../cluster"):
            with self.subTest(name=name):
                result = self.source({} if name is None else {"TF_VAR_cluster_name": name})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TF_VAR_cluster_name", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_explicit_name_without_tfvars(self):
        result = self.source({"TF_VAR_cluster_name": "training-a"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "terraform.tfvars").exists())
        self.assertIn('key    = "training-a.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())
        self.assertTrue(self.log.exists())

    def test_existing_state_key_is_preserved(self):
        for key in ("k8s-training.tfstate", "clusters/legacy.tfstate"):
            with self.subTest(key=key):
                result = self.source({"TF_VAR_cluster_name": "custom-name", "K8S_TF_STATE_KEY": key})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'key    = "{key}"',
                              (self.root / "terraform_backend_override.tf").read_text())

    def test_invalid_keys_stop_before_cloud_calls(self):
        for key in ("", ".tfstate", "bad key.tfstate", 'bad"key.tfstate', "bad\nkey.tfstate", "state"):
            with self.subTest(key=key):
                result = self.source({"TF_VAR_cluster_name": "training-a", "K8S_TF_STATE_KEY": key})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("K8S_TF_STATE_KEY", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_resourcing_recomputes_default_key(self):
        result = self.source(
            {"TF_VAR_cluster_name": "training-a"},
            'source "$1" && export TF_VAR_cluster_name=training-b && source "$1"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "training-b.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_default_name_retains_legacy_key(self):
        result = self.source({"TF_VAR_cluster_name": "k8s-training"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())


if __name__ == "__main__":
    unittest.main()
