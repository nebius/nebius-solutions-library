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
  "iam service-account get-by-name "*)
    printf '%s\\n' 'mock-id'
    exit 0
    ;;
  "iam static-key issue "*)
    if [ "${TEST_STATIC_KEY_ISSUE_NO_ID:-0}" -eq 1 ]; then
      printf '%s\\n' '{"token":"mock-grafana-token","operation":{"id":"mock-operation"}}'
    else
      printf '%s\\n' '{"metadata":{"id":"mock-grafana-key"},"token":"mock-grafana-token"}'
    fi
    exit 0
    ;;
  "iam static-key get-by-name "*)
    printf '%s\\n' '{"metadata":{"id":"mock-grafana-key"}}'
    exit 0
    ;;
  "iam static-key delete "*|"iam static-key revoke "*)
    cat >/dev/null 2>&1 || true
    exit 0
    ;;
  "iam static-key find "*)
    cat >/dev/null
    if [ "${TEST_STATIC_KEY_FIND_EXIT:-0}" -ne 0 ]; then
      exit "${TEST_STATIC_KEY_FIND_EXIT}"
    fi
    printf '%s\\n' "{\\"staticKey\\":{\\"metadata\\":{\\"id\\":\\"${TEST_STATIC_KEY_FIND_ID:-mock-grafana-key}\\"},\\"status\\":{\\"active\\":${TEST_STATIC_KEY_FIND_ACTIVE:-true}}}}"
    exit 0
    ;;
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

    def test_grafana_observability_key_is_created_once_and_reused(self):
        helper = SCRIPT.parents[1] / "scripts/prepare-grafana-observability-key.sh"
        self.assertNotIn("--arg token", helper.read_text())
        legacy_file = self.root / "grafana_access_token.txt"
        legacy_file.write_text("legacy-token")
        result = self.source(
            command=(
                'source "$1" && source "$1" && '
                'test "$TF_VAR_grafana_observability_access_token" = mock-grafana-token && '
                'printf "key=%s expires=%s\\n" '
                '"$TF_VAR_grafana_observability_key_id" '
                '"$GRAFANA_OBSERVABILITY_KEY_EXPIRES_AT"'
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("key=mock-grafana-key expires=", result.stdout)
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 1)
        find_call = next(call for call in calls if call.startswith("iam static-key find "))
        self.assertIn("--file /dev/stdin", find_call)
        self.assertNotIn("mock-grafana-token", find_call)
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("mock-grafana-token", result.stdout)
        self.assertFalse(legacy_file.exists())

    def test_issued_key_id_is_resolved_by_name_when_issue_response_omits_it(self):
        result = self.source({"TEST_STATIC_KEY_ISSUE_NO_ID": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        self.assertEqual(json.loads(key_file.read_text())["key_id"], "mock-grafana-key")
        calls = self.log.read_text().splitlines()
        lookup_call = next(
            call for call in calls if call.startswith("iam static-key get-by-name ")
        )
        self.assertIn("--parent-id mock-id", lookup_call)
        self.assertIn("--name k8s-training.grafana-observability-", lookup_call)

    def test_new_key_is_deleted_when_local_cache_cannot_be_written(self):
        result = self.source({
            "GRAFANA_OBSERVABILITY_KEY_FILE": str(
                self.root / "missing-parent" / "grafana-key.json"
            )
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Removed newly issued Grafana Observability key mock-grafana-key "
            "after credential preparation failed.",
            result.stderr,
        )
        calls = self.log.read_text().splitlines()
        self.assertIn("iam static-key delete --id mock-grafana-key", calls)
        self.assertFalse((self.root / "missing-parent" / "grafana-key.json").exists())

    def test_reused_cache_permissions_are_repaired(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text(json.dumps({
            "project_id": "mock-project",
            "service_account_id": "mock-id",
            "key_id": "mock-grafana-key",
            "token": "mock-grafana-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }))
        key_file.chmod(0o644)
        result = self.source()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 0)

    def test_remotely_invalid_cached_key_is_replaced(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text(json.dumps({
            "project_id": "mock-project",
            "service_account_id": "mock-id",
            "key_id": "revoked-key",
            "token": "revoked-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }))
        result = self.source({"TEST_STATIC_KEY_FIND_EXIT": "13"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no longer exists; issuing a replacement", result.stdout)
        self.assertEqual(json.loads(key_file.read_text())["key_id"], "mock-grafana-key")
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 1)

    def test_inactive_cached_key_is_replaced(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text(json.dumps({
            "project_id": "mock-project",
            "service_account_id": "mock-id",
            "key_id": "mock-grafana-key",
            "token": "inactive-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }))
        result = self.source({"TEST_STATIC_KEY_FIND_ACTIVE": "false"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is inactive; issuing a replacement", result.stdout)
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 1)

    def test_cached_key_validation_error_does_not_rotate(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        original = {
            "project_id": "mock-project",
            "service_account_id": "mock-id",
            "key_id": "mock-grafana-key",
            "token": "cached-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        key_file.write_text(json.dumps(original))
        result = self.source({"TEST_STATIC_KEY_FIND_EXIT": "7"})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("failed to validate cached Grafana Observability key", result.stderr)
        self.assertEqual(json.loads(key_file.read_text()), original)
        self.assertNotIn("iam static-key issue ", self.log.read_text())

    def test_malformed_cached_key_fails_without_rotation(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text('{"project_id":"mock-project","token":')
        result = self.source()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("key cache is malformed", result.stderr)
        self.assertNotIn("iam static-key find ", self.log.read_text())
        self.assertNotIn("iam static-key issue ", self.log.read_text())

    def test_cache_from_another_context_is_replaced_explicitly(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text(json.dumps({
            "project_id": "another-project",
            "service_account_id": "another-service-account",
            "key_id": "other-context-key",
            "token": "other-context-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }))
        result = self.source()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("belongs to another project or service account", result.stdout)
        self.assertEqual(json.loads(key_file.read_text())["key_id"], "mock-grafana-key")
        calls = self.log.read_text().splitlines()
        self.assertEqual(sum(call.startswith("iam static-key find ") for call in calls), 0)
        self.assertEqual(sum(call.startswith("iam static-key issue ") for call in calls), 1)

    def test_near_expiry_grafana_key_is_replaced(self):
        key_file = self.root / ".grafana_observability_key.k8s-training.json"
        key_file.write_text(json.dumps({
            "project_id": "mock-project",
            "service_account_id": "mock-id",
            "key_id": "expiring-key",
            "token": "expiring-token",
            "expires_at": "2000-01-01T00:00:00Z",
        }))
        result = self.source(command='source "$1" && printf "key=%s\\n" "$TF_VAR_grafana_observability_key_id"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("key=mock-grafana-key", result.stdout)
        self.assertEqual(json.loads(key_file.read_text())["key_id"], "mock-grafana-key")
        self.assertEqual(
            sum(call.startswith("iam static-key issue ") for call in self.log.read_text().splitlines()),
            1,
        )

    def test_grafana_disabled_in_tfvars_skips_key_preparation(self):
        (self.root / "terraform.tfvars").write_text(
            "enable_grafana = false # do not deploy managed Grafana\n"
        )
        result = self.source(
            command=(
                'source "$1" && '
                'test -z "${TF_VAR_grafana_observability_key_id:-}" && '
                'test -z "${TF_VAR_grafana_observability_access_token:-}"'
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Grafana is disabled; skipping Observability key preparation.", result.stdout)
        self.assertNotIn("iam static-key issue ", self.log.read_text())
        self.assertFalse(
            (self.root / ".grafana_observability_key.k8s-training.json").exists()
        )

    def test_grafana_tfvars_parser_rejects_nonliteral_or_duplicate_values(self):
        for contents in (
            'enable_grafana = "false"\n',
            "enable_grafana = false\nenable_grafana = true\n",
            'enable_grafana = false\nenable_grafana = "true"\n',
        ):
            with self.subTest(contents=contents):
                self.log.unlink(missing_ok=True)
                (self.root / "terraform.tfvars").write_text(contents)
                result = self.source()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("literal enable_grafana", result.stderr)
                self.assertFalse(self.log.exists())

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
