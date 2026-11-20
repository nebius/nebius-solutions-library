"""Run cleanup failures in isolation with dummy keys and no cloud access."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / "scripts/grafana-observability-key.sh"


class GrafanaCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for tool in ("jq", "cat", "cmp", "grep", "rm"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        cli = self.bin / "nebius"
        cli.write_text('#!/bin/bash\nprintf "%s\\n" "$API_ERROR" >&2\nexit "$CLI_STATUS"\n')
        cli.chmod(0o700)

        self.env = {"PATH": str(self.bin)}
        self.files = {}
        for which in ("current", "previous"):
            for field, content in (("token", which), ("metadata", '{"id":"%s"}' % which)):
                path = root / f"{which}.{field}"
                path.write_text(content)
                self.files[path] = content
                self.env[f"{which}_{field}_file".upper()] = str(path)

    def cleanup(self, status=0, message=""):
        return subprocess.run(
            ["/bin/bash", HELPER, "cleanup"],
            env=dict(self.env, CLI_STATUS=str(status), API_ERROR=message),
            capture_output=True,
            text=True,
        )

    def assert_retained(self, result):
        self.assertNotEqual(result.returncode, 0)
        for path, expected in self.files.items():
            self.assertEqual(path.read_text(), expected)

    def test_missing_cli(self):
        (self.bin / "nebius").unlink()
        self.assert_retained(self.cleanup())

    def test_missing_jq(self):
        (self.bin / "jq").unlink()
        self.assert_retained(self.cleanup())

    def test_unconfirmed_failures(self):
        for status, message in (
            (127, "credential helper: command not found"),
            (1, "Error: rpc error: code = PermissionDenied desc = access denied"),
            (13, "profile not found"),
            (1, "Error: rpc error: code = NotFound desc = key absent"),
        ):
            with self.subTest(status=status, message=message):
                self.assert_retained(self.cleanup(status, message))

    def test_confirmed_not_found(self):
        result = self.cleanup(13, "Error: rpc error: code = NotFound desc = key absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(not path.exists() for path in self.files))

    def test_failed_issue_rollback_reports_key_lookup(self):
        for path in self.files:
            path.unlink()
        for tool in ("date", "mktemp"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        issued_name = self.bin.parent / "issued-name"
        calls = self.bin.parent / "calls"
        (self.bin / "nebius").write_text(
            '''#!/bin/bash
echo "$3" >> "$CALLS"
case "$3" in
  list) echo '[]' ;;
  issue)
    while [[ "$1" != "--name" ]]; do shift || exit 2; done
    echo "$2" > "$ISSUED_NAME"
    echo '{"token":"dummy-rollback-credential"}' ;;
  get-by-name) exit 17 ;;
  revoke) exit 19 ;;
  *) exit 2 ;;
esac
'''
        )
        project = "project-test"
        result = subprocess.run(
            ["/bin/bash", HELPER, "issue"],
            env=dict(self.env, PARENT_ID=project, SERVICE_ACCOUNT_ID="sa-test",
                     CLUSTER_ID="cluster-test", CALLS=str(calls), ISSUED_NAME=str(issued_name)),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["list", "issue", "get-by-name", "revoke"])
        name = issued_name.read_text().strip()
        self.assertIn(f"static-key get-by-name --parent-id {project} --name {name}", result.stderr)
        self.assertNotIn("dummy-rollback-credential", result.stdout + result.stderr)

    def test_retry_documented_finalization(self):
        readme = HELPER.parents[3] / "k8s-training/README.md"
        section = readme.read_text().split("After validation succeeds, invalidate the previous key", 1)[1]
        snippet = section.split("```bash\n", 1)[1].split("\n```", 1)[0]
        root = self.bin.parent
        current = root / "grafana_access_token.txt"
        current_metadata = root / "grafana_access_token.metadata.json"
        previous = root / "grafana_access_token.previous.txt"
        metadata = root / "grafana_access_token.previous.metadata.json"
        current.write_text("current-token")
        current_metadata.write_text('{"id":"current-id"}')
        (self.bin / "nebius").write_text(
            '''#!/bin/bash
case "$*" in
  "iam static-key delete --id previous-id") ;;
  "iam static-key revoke --file /dev/stdin") jq -e '.token == "previous-token"' >/dev/null || exit 2 ;;
  *) exit 2 ;;
esac
printf '%s\\n' "$API_ERROR" >&2
exit "$CLI_STATUS"
'''
        )
        for key_id in ("previous-id", None):
            for status, error, removed in (
                (0, "", True),
                (7, "Error: rpc error: code = PermissionDenied", False),
                (1, "connection timed out", False),
                (13, "profile not found", False),
                (1, "Error: rpc error: code = NotFound desc = key absent", False),
                (13, "Error: rpc error: code = NotFound desc = key absent", True),
            ):
                with self.subTest(key_id=key_id, status=status, error=error):
                    previous.write_text("previous-token\n")
                    metadata.write_text(json.dumps({"id": key_id}))
                    result = subprocess.run(
                        ["/bin/bash", "-c", snippet], cwd=root,
                        env=dict(self.env, CLI_STATUS=str(status), API_ERROR=error),
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0 if removed else status, result.stderr)
                    self.assertEqual(previous.exists(), not removed)
                    self.assertEqual(metadata.exists(), not removed)
                    self.assertEqual(current.read_text(), "current-token")
                    self.assertEqual(current_metadata.read_text(), '{"id":"current-id"}')
                    self.assertNotIn("previous-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
