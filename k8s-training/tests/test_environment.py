"""Exercise environment setup without credentials or cloud access.

Run with: python3 -m unittest discover -s k8s-training/tests -p 'test_*.py'
Requires Bash and jq. Every Nebius command is replaced by a local stub.
"""

import json
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
case "$*" in
  "vpc network list "*)
    printf '%s\\n' "$TEST_NETWORKS_JSON"
    exit "${TEST_NETWORKS_EXIT:-0}"
    ;;
  "vpc subnet list "*)
    printf '%s\\n' "$TEST_SUBNETS_JSON"
    exit "${TEST_SUBNETS_EXIT:-0}"
    ;;
esac
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
            "TEST_NETWORKS_JSON": '{"items":[]}',
            "TEST_SUBNETS_JSON": '{"items":[{"metadata":{"id":"mock-subnet"}}]}',
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

    @staticmethod
    def resource(resource_id, name, network_id=None):
        return {"metadata": {"id": resource_id, "name": name},
                "spec": {"network_id": network_id}}

    def vpc(self, networks, subnets):
        self.env["TEST_NETWORKS_JSON"] = json.dumps(networks)
        self.env["TEST_SUBNETS_JSON"] = json.dumps(subnets)

    def assert_selected_subnet(self, subnet_id, variables=None):
        result = self.source(
            variables,
            'source "$1" && printf "selected=%s\\n" "$TF_VAR_subnet_id"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"selected={subnet_id}\n", result.stdout)
        self.assertIn(f"subnet: {subnet_id}\n", result.stdout)

    def test_default_subnet_preferred_only_in_default_network(self):
        for name in ("default", "default-eu-north1", "default-subnet-a"):
            with self.subTest(name=name):
                self.vpc(
                    {"items": [self.resource("custom-net", "custom"),
                               self.resource("default-net", name)]},
                    {"items": [self.resource("wrong-network", "default", "custom-net"),
                               self.resource("custom-subnet", "custom", "default-net"),
                               self.resource("chosen", name, "default-net"),
                               self.resource("second-match", name, "default-net")]},
                )
                self.assert_selected_subnet("chosen")

    def test_custom_subnet_in_default_network_preferred_over_project_first(self):
        self.vpc(
            [self.resource("net", "default")],
            [self.resource("other", "default", "other-net"),
             self.resource("chosen", "training", "net")],
        )
        self.assert_selected_subnet("chosen")

    def test_project_first_subnet_used_without_matching_default_network(self):
        for networks in (
            [self.resource("net", "custom")],
            [self.resource("empty-net", "default")],
            [], {}, None, {"items": None},
        ):
            with self.subTest(networks=networks):
                self.vpc(networks, [self.resource("chosen", "custom", "net"),
                                    self.resource("later", "default", "net")])
                self.assert_selected_subnet("chosen")

    def test_default_subnet_in_later_default_network_is_preferred(self):
        self.vpc(
            [self.resource("net-a", "default-a"), self.resource("net-b", "default-b")],
            [self.resource("custom", "training", "net-a"),
             self.resource("chosen", "default", "net-b")],
        )
        self.assert_selected_subnet("chosen")

    def test_list_envelopes_and_arrays_and_pagination(self):
        networks = [self.resource("net", "default")]
        subnets = [self.resource("chosen", "default", "net")]
        for network_response in (networks, {"items": networks}):
            for subnet_response in (subnets, {"items": subnets}):
                with self.subTest(networks=network_response, subnets=subnet_response):
                    self.vpc(network_response, subnet_response)
                    self.assert_selected_subnet("chosen")
        calls = self.log.read_text().splitlines()
        vpc_calls = [call for call in calls if call.startswith("vpc ")]
        self.assertEqual(len(vpc_calls), 8)
        for call in vpc_calls:
            self.assertIn("--parent-id mock-project --all --format json", call)

    def test_explicit_subnet_skips_vpc_discovery_and_is_preserved(self):
        self.env.update(TEST_NETWORKS_EXIT="7", TEST_SUBNETS_EXIT="7")
        result = self.source(
            {"NEBIUS_VPC_SUBNET_ID": "explicit"},
            'source "$1" && source "$1" && printf "selected=%s\\n" "$TF_VAR_subnet_id"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selected=explicit\n", result.stdout)
        self.assertIn("Using configured subnet: explicit", result.stdout)
        self.assertNotIn("vpc ", self.log.read_text())

    def test_empty_override_and_missing_names_allow_discovery(self):
        self.vpc([{"metadata": {"id": "nameless-net"}}],
                 [{"metadata": {"id": "nameless-subnet"}}])
        self.assert_selected_subnet("nameless-subnet", {"NEBIUS_VPC_SUBNET_ID": ""})

    def test_no_subnets_stops_before_resource_creation(self):
        for response in ([], {}, None, {"items": []}, {"items": None}):
            with self.subTest(response=response):
                self.vpc([], response)
                result = self.source()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no subnet found in project mock-project", result.stderr)
                self.assertNotIn("storage ", self.log.read_text())
                self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_vpc_cli_failures_are_not_reported_as_missing_subnets(self):
        for resource in ("NETWORKS", "SUBNETS"):
            with self.subTest(resource=resource):
                self.log.unlink(missing_ok=True)
                result = self.source({f"TEST_{resource}_EXIT": "7"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"failed to list {resource.lower()}", result.stderr)
                self.assertNotIn("no subnet found", result.stderr)
                self.assertNotIn("storage ", self.log.read_text())
                if resource == "NETWORKS":
                    self.assertNotIn("vpc subnet list", self.log.read_text())

    def test_invalid_vpc_json_stops_before_resource_creation(self):
        subnet_response = json.dumps([self.resource("chosen", "custom", "net")])
        for resource in ("NETWORKS", "SUBNETS"):
            for response in ("not-json", "", " \n\t ", "[] []",
                             f"{subnet_response}\n{subnet_response}"):
                with self.subTest(resource=resource, response=response):
                    result = self.source({f"TEST_{resource}_JSON": response})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("failed to parse VPC network/subnet responses", result.stderr)
                    self.assertNotIn("no subnet found", result.stderr)
                    self.assertNotIn("storage ", self.log.read_text())
                    self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_invalid_cluster_names_stop_before_cloud_calls(self):
        for name in (" ", "bad name", 'bad"name', "bad\nname", "../cluster"):
            with self.subTest(name=name):
                result = self.source({"TF_VAR_cluster_name": name})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TF_VAR_cluster_name", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_explicit_name_without_tfvars(self):
        result = self.source({"TF_VAR_cluster_name": "training-a"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "terraform.tfvars").exists())
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())
        self.assertTrue(self.log.exists())

    def test_existing_state_key_is_preserved(self):
        for key in ("k8s-training.tfstate", "clusters/legacy.tfstate"):
            with self.subTest(key=key):
                result = self.source({"TF_VAR_cluster_name": "custom-name", "K8S_TF_STATE_KEY": key})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'key    = "{key}"',
                              (self.root / "terraform_backend_override.tf").read_text())

    def test_custom_name_without_explicit_key_warns(self):
        result = self.source({"TF_VAR_cluster_name": "training-b"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Warning: cluster name is 'training-b' but state key is the legacy "
            "default; set K8S_TF_STATE_KEY for an additional cluster.",
            result.stderr,
        )
        self.assertIn("Terraform state: s3://tfstate-k8s-training-mockhash/k8s-training.tfstate",
                      result.stdout)
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_default_name_or_explicit_key_does_not_warn(self):
        for variables in (
            {},
            {"TF_VAR_cluster_name": ""},
            {"TF_VAR_cluster_name": "k8s-training"},
            {"TF_VAR_cluster_name": "training-b", "K8S_TF_STATE_KEY": "k8s-training.tfstate"},
            {"TF_VAR_cluster_name": "training-b", "K8S_TF_STATE_KEY": "clusters/training-b.tfstate"},
        ):
            with self.subTest(variables=variables):
                result = self.source(variables)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Warning:", result.stderr)

    def test_invalid_keys_stop_before_cloud_calls(self):
        for key in ("", ".tfstate", "bad key.tfstate", 'bad"key.tfstate', "bad\nkey.tfstate", "state",
                    "a/../b.tfstate", "a/./b.tfstate", "a/b/../../c.tfstate"):
            with self.subTest(key=key):
                result = self.source({"TF_VAR_cluster_name": "training-a", "K8S_TF_STATE_KEY": key})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("K8S_TF_STATE_KEY", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "terraform_backend_override.tf").exists())

    def test_resourcing_with_new_cluster_name_keeps_legacy_key(self):
        result = self.source(
            {"TF_VAR_cluster_name": "training-a"},
            'source "$1" && export TF_VAR_cluster_name=training-b && source "$1"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_missing_or_empty_name_uses_terraform_default(self):
        for variables in ({}, {"TF_VAR_cluster_name": ""}):
            with self.subTest(variables=variables):
                result = self.source(variables, 'source "$1" && printf "cluster=%s\\n" "$TF_VAR_cluster_name"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("cluster=k8s-training", result.stdout)
                self.assertIn('key    = "k8s-training.tfstate"',
                              (self.root / "terraform_backend_override.tf").read_text())

    def test_custom_name_in_tfvars_keeps_legacy_key(self):
        (self.root / "terraform.tfvars").write_text('cluster_name = "existing-custom-cluster"\n')
        result = self.source()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_explicit_key_is_used_and_printed(self):
        result = self.source({"TF_VAR_cluster_name": "training-b", "K8S_TF_STATE_KEY": "clusters/training-b.tfstate"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Terraform state: s3://tfstate-k8s-training-mockhash/clusters/training-b.tfstate", result.stdout)
        self.assertIn('key    = "clusters/training-b.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_unsetting_override_restores_legacy_key(self):
        result = self.source(
            {"TF_VAR_cluster_name": "training-b", "K8S_TF_STATE_KEY": "training-b.tfstate"},
            'source "$1" && unset K8S_TF_STATE_KEY && source "$1"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "k8s-training.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())

    def test_dots_inside_path_components_remain_valid(self):
        result = self.source({"K8S_TF_STATE_KEY": "clusters/v1.2/cluster..backup.tfstate"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key    = "clusters/v1.2/cluster..backup.tfstate"',
                      (self.root / "terraform_backend_override.tf").read_text())


if __name__ == "__main__":
    unittest.main()
