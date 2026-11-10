"""Exercise SkyPilot environment setup without credentials or cloud access."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "environment.sh"


class SkyPilotEnvironmentTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("jq"):
            self.skipTest("jq is required by environment.sh")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        self.stub("nebius", '''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_CALL_LOG"
case "$*" in
  "iam get-access-token")
    printf '%s\n' 'mock-iam-token'
    ;;
  "vpc subnet list "*)
    printf '%s\n' '{"items":[{"metadata":{"id":"mock-subnet"}}]}'
    ;;
  "storage bucket list "*)
    printf '%s\n' '{"items":[{"metadata":{"name":"skypilot-multiregionmockhash"},"status":{"region":"eu-west2"}}]}'
    ;;
  "iam service-account get-by-name "*)
    printf '%s\n' '{"metadata":{"id":"mock-infra-sa"}}'
    ;;
  "iam group get-by-name "*)
    printf '%s\n' '{"metadata":{"id":"mock-editors"}}'
    ;;
  "iam group-membership list-members "*)
    printf '%s\n' '{"memberships":[{"spec":{"member_id":"mock-infra-sa"}}]}'
    ;;
  "iam static-key issue "*)
    printf '%s\n' '{"metadata":{"id":"mock-grafana-key"},"token":"mock-grafana-token"}'
    ;;
  "iam static-key find "*)
    cat >/dev/null
    printf '%s\n' '{"staticKey":{"metadata":{"id":"mock-grafana-key"},"status":{"active":true}}}'
    ;;
  "iam v2 access-key create "*)
    printf '%s\n' '{"metadata":{"id":"mock-access-key"}}'
    ;;
  "iam v2 access-key get "*)
    printf '%s\n' '{"status":{"aws_access_key_id":"mock-aws-id","secret":"mock-aws-secret"}}'
    ;;
  *)
    printf '%s\n' '{}'
    ;;
esac
''')
        self.stub("md5sum", "#!/bin/sh\ncat >/dev/null\nprintf 'mockhash  -\\n'\n")
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.root),
            "TEST_CALL_LOG": str(self.log),
        }

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def source(self, command='source "$1"'):
        return subprocess.run(
            ["bash", "-c", command, "test", str(SCRIPT)],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def test_sourced_environment_creates_once_and_reuses_key(self):
        (self.root / "terraform.tfvars").write_text(
            'cluster_name = "sky-test"\nenable_grafana = true\n'
        )
        result = self.source(
            'source "$1" && source "$1" && '
            'test "$TF_VAR_grafana_observability_access_token" = mock-grafana-token && '
            'printf "key=%s\\n" "$TF_VAR_grafana_observability_key_id"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("key=mock-grafana-key", result.stdout)
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 1)
        key_file = self.root / ".grafana_observability_key.sky-test.json"
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("mock-grafana-token", result.stdout)

    def test_direct_execution_is_rejected_when_grafana_enabled(self):
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run 'source ./environment.sh'", result.stderr)
        self.assertFalse(self.log.exists())

    def test_disabled_grafana_skips_key_preparation(self):
        (self.root / "terraform.tfvars").write_text(
            'cluster_name = "sky-test"\nenable_grafana = false\n'
        )
        result = self.source(
            'source "$1" && '
            'test -z "${TF_VAR_grafana_observability_access_token:-}" && '
            'test -z "${TF_VAR_grafana_observability_key_id:-}"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Grafana is disabled; skipping Observability key preparation.", result.stdout)
        self.assertNotIn("iam static-key ", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
