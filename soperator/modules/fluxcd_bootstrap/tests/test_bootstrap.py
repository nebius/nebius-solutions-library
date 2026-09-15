import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1]
WAIT = MODULE.parent / "slurm/scripts/wait_for_bootstrap_releases.sh"
SEED = MODULE / "scripts/seed_values.sh"
INSTALL = MODULE / "scripts/install_foundation.sh"
RESUME = MODULE / "scripts/resume_umbrella.sh"

MOCK_KUBECTL = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
log = Path(os.environ["CALL_LOG"])
with log.open("a") as stream:
    stream.write(json.dumps(args) + "\n")
scenario = os.environ["SCENARIO"]
if "annotate" in args:
    Path(os.environ["ANNOTATED"]).touch()
elif "get" in args and "secrets" in args:
    if scenario == "complete":
        print("secret/sh.helm.release.v1.terraform-fluxcd-values.v1")
elif "get" in args and "helmrelease" in args and args[-1] == "name":
    if scenario == "repair":
        print("helmrelease/soperator-fluxcd")
elif "get" in args and "helmrelease" in args:
    name = args[args.index("helmrelease") + 1]
    stale = scenario in ("recover", "stale", "timeout") and name.endswith("cert-manager")
    ready = not stale or (Path(os.environ["ANNOTATED"]).exists() and scenario != "timeout")
    print(json.dumps({
        "metadata": {"generation": 2},
        "status": {
            "observedGeneration": 2 if ready else 1,
            "conditions": [{"type": "Ready", "status": "True" if ready or scenario == "stale" else "False"}]
        }
    }))
elif "get" in args and "configmap" in args:
    if scenario == "existing":
        print("configmap/terraform-fluxcd-values")
    elif scenario == "forbidden":
        sys.exit(1)
elif "create" in args and "namespace" in args:
    print(json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "storage-system"}}))
elif "apply" in args:
    sys.stdin.read()
elif "create" in args and "configmap" in args:
    values = next(value.split("=", 2)[2] for value in args if value.startswith("--from-literal="))
    print(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "terraform-fluxcd-values"}, "data": {"values.yaml": values}}))
elif "create" in args and "-f" in args:
    Path(os.environ["CREATED"]).write_text(sys.stdin.read())
'''


class BootstrapTests(unittest.TestCase):
    def run_script(self, script, scenario):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text(MOCK_KUBECTL)
            kubectl.chmod(0o755)
            helm = root / "helm"
            helm.write_text("#!/usr/bin/env python3\nimport os,json,sys\nif sys.argv[1]=='version':print('v3.17.3');sys.exit(0)\nwith open(os.environ['CALL_LOG'],'a') as f:f.write(json.dumps(['helm']+sys.argv[1:])+'\\n')\n")
            helm.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       K8S_CONTEXT="test-context", NAMESPACE="flux-system",
                       STORAGE_NAMESPACE="storage-system", CHART_REPOSITORY="oci://example.invalid/soperator", CHART_VERSION="5.0.0",
                       VALUES_YAML="soperator:\n  enabled: false\n",
                       SCENARIO=scenario, CALL_LOG=str(root / "calls"),
                       ANNOTATED=str(root / "annotated"), CREATED=str(root / "created"),
                       TIMEOUT_SECONDS="2", POLL_SECONDS="0.05")
            result = subprocess.run(["bash", str(script)], env=env,
                                    capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
            created = json.loads((root / "created").read_text()) if (root / "created").exists() else None
            for call in calls:
                self.assertIn("test-context", call)
                self.assertIn("flux-system", call)
            return result, calls, created

    def test_ready_releases_are_not_reconciled(self):
        result, calls, _ = self.run_script(WAIT, "ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("annotate" in call for call in calls))
        self.assertEqual(sum("helmrelease" in call for call in calls), 4)

    def test_failed_install_and_stale_ready_are_reconciled(self):
        for scenario in ("recover", "stale"):
            with self.subTest(scenario=scenario):
                result, calls, _ = self.run_script(WAIT, scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                annotations = [call for call in calls if "annotate" in call]
                self.assertEqual(len(annotations), 1)
                values = dict(value.split("=", 1) for value in annotations[0] if value.startswith("reconcile.fluxcd.io/"))
                self.assertEqual(values["reconcile.fluxcd.io/requestedAt"], values["reconcile.fluxcd.io/resetAt"])
                self.assertNotIn("reconcile.fluxcd.io/forceAt", values)

    def test_timeout_blocks_full_configuration(self):
        result, _, _ = self.run_script(WAIT, "timeout")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out", result.stderr)

    def test_existing_full_configmap_is_preserved(self):
        result, calls, created = self.run_script(SEED, "existing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(created)
        self.assertFalse(any("create" in call for call in calls))

    def test_read_error_does_not_overwrite_configmap(self):
        result, calls, _ = self.run_script(SEED, "forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("create" in call for call in calls))

    def test_new_seed_can_be_adopted_by_the_final_helm_release(self):
        result, _, created = self.run_script(SEED, "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(created["metadata"]["labels"]["app.kubernetes.io/managed-by"], "Helm")
        self.assertEqual(created["metadata"]["annotations"]["meta.helm.sh/release-name"], "terraform-fluxcd-values")
        self.assertEqual(created["metadata"]["annotations"]["meta.helm.sh/release-namespace"], "flux-system")
        self.assertEqual(created["data"]["values.yaml"], "soperator:\n  enabled: false\n")


    def test_completed_installation_is_never_filtered_again(self):
        result, calls, _ = self.run_script(INSTALL, "complete")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "helm" or "patch" in call for call in calls))

    def test_new_foundation_uses_same_release_name_as_flux(self):
        result, calls, _ = self.run_script(INSTALL, "new")
        self.assertEqual(result.returncode, 0, result.stderr)
        install = next(call for call in calls if call[0] == "helm")
        self.assertIn("flux-system-soperator-fluxcd", install)
        self.assertIn("--post-renderer", install)
        self.assertIn("--no-hooks", install)
        self.assertFalse(any(arg.startswith("--wait") for arg in install))
        self.assertFalse(any("patch" in call for call in calls))
        self.assertTrue(any("namespace" in call and "storage-system" in call for call in calls))

    def test_failed_old_bootstrap_is_suspended_before_repair(self):
        result, calls, _ = self.run_script(INSTALL, "repair")
        self.assertEqual(result.returncode, 0, result.stderr)
        patch_index = next(i for i, call in enumerate(calls) if "patch" in call)
        install_index = next(i for i, call in enumerate(calls) if call[0] == "helm")
        self.assertLess(patch_index, install_index)
        self.assertIn('{"spec":{"suspend":true}}', calls[patch_index])

    def test_takeover_resumes_and_reconciles_the_umbrella(self):
        result, calls, _ = self.run_script(RESUME, "repair")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('{"spec":{"suspend":false}}', calls[0])
        self.assertIn("annotate", calls[1])
        self.assertIn("soperator-fluxcd", calls[1])


if __name__ == "__main__":
    unittest.main()
