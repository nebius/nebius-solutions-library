#!/usr/bin/env python3
"""Exercise the production Grafana token flow with mocked cloud providers.

The temporary fixture uses the real grafana.tf and a fake Nebius CLI. A clean
apply proves that terraform_data creates the token file before the ephemeral
local_command reads it and passes it to the application's write-only fields.
Subsequent applies exercise one-shot replacement and idempotency.
Requires Terraform >= 1.11.4 on PATH (or set TERRAFORM_BIN).
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


MODULE = Path(__file__).resolve().parents[1]


LEGACY_GENERATOR = """\
terraform {
  required_version = ">= 1.11.4"
}

resource "terraform_data" "grafana_access_token" {
  triggers_replace = {
    access_key_id      = "access-key"
    service_account_id = "service-account"
  }

  provisioner "local-exec" {
    command = "printf 'issued\\n' >> issue.log; printf 'legacy-token\\n' > token.txt"
  }
}
"""


UPGRADED_GENERATOR = """\
terraform {
  required_version = ">= 1.11.4"
}

resource "terraform_data" "grafana_access_token" {
  input = {
    token_path = "${path.root}/token.txt"
  }

  triggers_replace = {
    access_key_id      = "access-key"
    service_account_id = "service-account"
  }

  lifecycle {
    create_before_destroy = true
  }

  provisioner "local-exec" {
    command = "printf 'issued\\n' >> issue.log; printf 'replacement-token\\n' > token.txt"
  }
}

output "token_path" {
  value = terraform_data.grafana_access_token.output.token_path
}
"""


def check_production_wiring():
    grafana = (MODULE / "grafana.tf").read_text()
    output = (MODULE / "output.tf").read_text()
    versions = (MODULE / "versions.tf").read_text()

    required = (
        "triggers_replace = {",
        "create_before_destroy = true",
        "grafana_access_token.previous.txt",
        'ephemeral "local_command" "grafana_access_token"',
        "terraform_data.grafana_access_token[0].output.token_path",
        '"nebius.com/observability-token-revision"',
        "version = sha256(local.grafana_values)",
        "values  = local.grafana_values",
        'type = "Recreate"',
        'version = "~> 2.9"',
    )
    for expression in required:
        if expression not in grafana and expression not in versions:
            raise RuntimeError(f"Missing Grafana token wiring: {expression}")

    forbidden = (
        "try(trimspace(file(",
        "access_token      = local.grafana_access_token",
        "grafana_observability_key_generation",
        "rotation_generation",
    )
    combined = grafana + output
    for expression in forbidden:
        if expression in combined:
            raise RuntimeError(f"Obsolete Grafana token wiring remains: {expression}")


def check_apply_time_reader_upgrade(terraform):
    """Prove that adding the apply-time output does not replace a legacy generator."""

    with tempfile.TemporaryDirectory(prefix="o11y-grafana-upgrade-") as directory:
        fixture = Path(directory)
        configuration = fixture / "main.tf"
        configuration.write_text(LEGACY_GENERATOR)

        subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=fixture,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [terraform, "apply", "-auto-approve", "-input=false", "-no-color"],
            cwd=fixture,
            check=True,
            stdout=subprocess.DEVNULL,
        )

        configuration.write_text(UPGRADED_GENERATOR)
        plan = fixture / "upgrade.tfplan"
        subprocess.run(
            [terraform, "plan", f"-out={plan}", "-input=false", "-no-color"],
            cwd=fixture,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        plan_json = subprocess.run(
            [terraform, "show", "-json", plan],
            cwd=fixture,
            check=True,
            capture_output=True,
            text=True,
        )
        changes = json.loads(plan_json.stdout)["resource_changes"]
        generator = next(
            change
            for change in changes
            if change["address"] == "terraform_data.grafana_access_token"
        )
        if generator["change"]["actions"] != ["update"]:
            raise RuntimeError(
                "Adding the apply-time output must update the legacy generator in place; "
                f"got {generator['change']['actions']}"
            )

        subprocess.run(
            [terraform, "apply", "-input=false", "-no-color", plan],
            cwd=fixture,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if (fixture / "issue.log").read_text().splitlines() != ["issued"]:
            raise RuntimeError("The apply-time output unexpectedly reissued the legacy token")


def main():
    check_production_wiring()
    terraform = os.environ.get("TERRAFORM_BIN", "terraform")
    check_apply_time_reader_upgrade(terraform)

    with tempfile.TemporaryDirectory(prefix="o11y-grafana-token-") as directory:
        fixture = Path(directory)
        for filename in ("grafana.tf", "variables.tf", "versions.tf"):
            shutil.copyfile(MODULE / filename, fixture / filename)
        shutil.copytree(MODULE / "scripts", fixture / "scripts")

        tests = fixture / "tests"
        tests.mkdir()
        shutil.copyfile(
            MODULE / "tests/grafana-token/grafana_token.tftest.hcl",
            tests / "grafana_token.tftest.hcl",
        )

        binary_directory = fixture / "bin"
        binary_directory.mkdir()
        nebius_log = fixture / "nebius-calls.log"
        fake_nebius = binary_directory / "nebius"
        fake_nebius.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "operation=\"$*\"\n"
            "name=\"\"\n"
            "key_id=\"\"\n"
            "while (( $# )); do\n"
            "  case \"$1\" in\n"
            "    --name) name=\"$2\"; shift 2 ;;\n"
            "    --id) key_id=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "case \"$operation\" in\n"
            "  *\"static-key list\"*)\n"
            "    if [ -n \"$FAKE_ACTIVE_KEY_SERVICE_ACCOUNT_ID\" ]; then\n"
            "      printf '{\"items\":[{\"metadata\":{\"id\":\"static-key-existing\"},\"spec\":{\"service\":\"OBSERVABILITY\",\"account\":{\"service_account\":{\"id\":\"%s\"}}},\"status\":{\"active\":true}}]}\\n' \"$FAKE_ACTIVE_KEY_SERVICE_ACCOUNT_ID\"\n"
            "    else\n"
            "      printf '{\"items\":[]}\\n'\n"
            "    fi\n"
            "    ;;\n"
            "  *\"static-key issue\"*)\n"
            "    issue_number=$(grep -c '^issue:' \"$FAKE_NEBIUS_LOG\" 2>/dev/null || true)\n"
            "    issue_number=$((issue_number + 1))\n"
            "    printf 'issue:%s:%s\\n' \"$issue_number\" \"$name\" >> \"$FAKE_NEBIUS_LOG\"\n"
            "    printf '{\"token\":\"fixture-observability-token-%s\"}\\n' \"$issue_number\"\n"
            "    ;;\n"
            "  *\"static-key get-by-name\"*)\n"
            "    issue_number=$(awk -F: -v key_name=\"$name\" '$1 == \"issue\" && $3 == key_name {print $2}' \"$FAKE_NEBIUS_LOG\" | tail -1)\n"
            "    test -n \"$issue_number\"\n"
            "    printf '{\"metadata\":{\"id\":\"static-key-%s\"}}\\n' \"$issue_number\"\n"
            "    ;;\n"
            "  *\"static-key delete\"*)\n"
            "    if [ -n \"$FAKE_DELETE_FAIL_ONCE_ID\" ] && "
            "[ \"$key_id\" = \"$FAKE_DELETE_FAIL_ONCE_ID\" ] && "
            "[ ! -e \"$FAKE_DELETE_FAIL_MARKER\" ]; then\n"
            "      : > \"$FAKE_DELETE_FAIL_MARKER\"\n"
            "      printf 'simulated delete failure\\n' >&2\n"
            "      exit 1\n"
            "    fi\n"
            "    if [ \"$FAKE_DELETED_IDS_ARE_NOT_FOUND\" = true ] && "
            "grep -qx \"delete:$key_id\" \"$FAKE_NEBIUS_LOG\"; then\n"
            "      printf 'ResourceNotFound: key not found\\n' >&2\n"
            "      exit 1\n"
            "    fi\n"
            "    printf 'delete:%s\\n' \"$key_id\" >> \"$FAKE_NEBIUS_LOG\"\n"
            "    printf '{}\\n'\n"
            "    ;;\n"
            "  *\"static-key revoke\"*)\n"
            "    printf 'revoke\\n' >> \"$FAKE_NEBIUS_LOG\"\n"
            "    printf '{}\\n'\n"
            "    ;;\n"
            "  *)\n"
            "    printf 'Unexpected fake Nebius invocation: %s\\n' \"$operation\" >&2\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
        )
        fake_nebius.chmod(0o755)

        environment = os.environ.copy()
        environment["PATH"] = f"{binary_directory}:{environment['PATH']}"
        environment["FAKE_NEBIUS_LOG"] = str(nebius_log)
        environment["FAKE_DELETE_FAIL_ONCE_ID"] = ""
        environment["FAKE_DELETE_FAIL_MARKER"] = str(fixture / "unused-marker")
        environment["FAKE_DELETED_IDS_ARE_NOT_FOUND"] = "false"
        environment["FAKE_ACTIVE_KEY_SERVICE_ACCOUNT_ID"] = ""

        subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=fixture,
            env=environment,
            check=True,
        )

        subprocess.run(
            [
                terraform,
                "test",
                "-filter=tests/grafana_token.tftest.hcl",
                "-no-color",
            ],
            cwd=fixture,
            env=environment,
            check=True,
        )

        calls = nebius_log.read_text().splitlines()
        issue_calls = [call for call in calls if call.startswith("issue:")]
        issue_numbers = [call.split(":", 2)[1] for call in issue_calls]
        if issue_numbers != ["1", "2"]:
            raise RuntimeError(
                "Expected one initial issuance and one requested rotation; "
                f"got {issue_calls}"
            )

        delete_calls = [call for call in calls if call.startswith("delete:")]
        if delete_calls != ["delete:static-key-2", "delete:static-key-1"]:
            raise RuntimeError(
                "Expected destroy to delete the current and previous keys; "
                f"got {delete_calls}"
            )

        helper = fixture / "scripts/grafana-observability-key.sh"

        # A replacement on a fresh runner must fail before issuing a key when an
        # active remote key already exists; otherwise that key would be orphaned.
        missing_current = fixture / "missing-current"
        missing_current.mkdir()
        missing_log = missing_current / "nebius-calls.log"
        missing_environment = environment.copy()
        missing_environment.update(
            {
                "CURRENT_TOKEN_FILE": str(missing_current / "grafana_access_token.txt"),
                "CURRENT_METADATA_FILE": str(
                    missing_current / "grafana_access_token.metadata.json"
                ),
                "PREVIOUS_TOKEN_FILE": str(
                    missing_current / "grafana_access_token.previous.txt"
                ),
                "PREVIOUS_METADATA_FILE": str(
                    missing_current / "grafana_access_token.previous.metadata.json"
                ),
                "PARENT_ID": "project-test",
                "SERVICE_ACCOUNT_ID": "service-account-test",
                "FAKE_NEBIUS_LOG": str(missing_log),
                "FAKE_ACTIVE_KEY_SERVICE_ACCOUNT_ID": "service-account-test",
            }
        )
        missing_result = subprocess.run(
            ["/bin/bash", helper, "issue"],
            cwd=missing_current,
            env=missing_environment,
            capture_output=True,
            text=True,
        )
        if missing_result.returncode == 0:
            raise RuntimeError("Rotation without the current local credential must fail")
        if missing_log.exists() and "issue:" in missing_log.read_text():
            raise RuntimeError("Failed preflight unexpectedly issued a remote key")

        # If deletion of the previous key fails after the current key was
        # deleted, a retry must accept current-key NotFound and finish cleanup.
        cleanup_retry = fixture / "cleanup-retry"
        cleanup_retry.mkdir()
        current_token = cleanup_retry / "grafana_access_token.txt"
        current_metadata = cleanup_retry / "grafana_access_token.metadata.json"
        previous_token = cleanup_retry / "grafana_access_token.previous.txt"
        previous_metadata = (
            cleanup_retry / "grafana_access_token.previous.metadata.json"
        )
        current_token.write_text("fixture-observability-token-2\n")
        current_metadata.write_text('{"id":"static-key-2"}\n')
        previous_token.write_text("fixture-observability-token-1\n")
        previous_metadata.write_text('{"id":"static-key-1"}\n')
        for credential_file in (
            current_token,
            current_metadata,
            previous_token,
            previous_metadata,
        ):
            credential_file.chmod(0o600)

        retry_log = cleanup_retry / "nebius-calls.log"
        retry_marker = cleanup_retry / "previous-delete-failed"
        retry_environment = environment.copy()
        retry_environment.update(
            {
                "CURRENT_TOKEN_FILE": str(current_token),
                "CURRENT_METADATA_FILE": str(current_metadata),
                "PREVIOUS_TOKEN_FILE": str(previous_token),
                "PREVIOUS_METADATA_FILE": str(previous_metadata),
                "FAKE_NEBIUS_LOG": str(retry_log),
                "FAKE_DELETE_FAIL_ONCE_ID": "static-key-1",
                "FAKE_DELETE_FAIL_MARKER": str(retry_marker),
                "FAKE_DELETED_IDS_ARE_NOT_FOUND": "true",
            }
        )
        first_cleanup = subprocess.run(
            ["/bin/bash", helper, "cleanup"],
            cwd=cleanup_retry,
            env=retry_environment,
            capture_output=True,
            text=True,
        )
        if first_cleanup.returncode == 0:
            raise RuntimeError("The injected previous-key deletion failure was ignored")
        if not all(
            path.exists()
            for path in (
                current_token,
                current_metadata,
                previous_token,
                previous_metadata,
            )
        ):
            raise RuntimeError("A partial cleanup removed local recovery files")

        subprocess.run(
            ["/bin/bash", helper, "cleanup"],
            cwd=cleanup_retry,
            env=retry_environment,
            check=True,
        )
        if any(
            path.exists()
            for path in (
                current_token,
                current_metadata,
                previous_token,
                previous_metadata,
            )
        ):
            raise RuntimeError("Successful cleanup retry left local credentials behind")

        # Exercise two consecutive, finalized rotations. The first replacement
        # preserves key 1 while installing key 2. Simulate successful datasource
        # validation by deleting key 1 and its local backup, then rotate again to
        # key 3 and prove destroy cleans up keys 3 and 2.
        consecutive = fixture / "consecutive-rotations"
        consecutive.mkdir()
        consecutive_current_token = consecutive / "grafana_access_token.txt"
        consecutive_current_metadata = (
            consecutive / "grafana_access_token.metadata.json"
        )
        consecutive_previous_token = (
            consecutive / "grafana_access_token.previous.txt"
        )
        consecutive_previous_metadata = (
            consecutive / "grafana_access_token.previous.metadata.json"
        )
        consecutive_log = consecutive / "nebius-calls.log"
        consecutive_environment = environment.copy()
        consecutive_environment.update(
            {
                "CURRENT_TOKEN_FILE": str(consecutive_current_token),
                "CURRENT_METADATA_FILE": str(consecutive_current_metadata),
                "PREVIOUS_TOKEN_FILE": str(consecutive_previous_token),
                "PREVIOUS_METADATA_FILE": str(consecutive_previous_metadata),
                "PARENT_ID": "project-test",
                "SERVICE_ACCOUNT_ID": "service-account-test",
                "FAKE_NEBIUS_LOG": str(consecutive_log),
            }
        )

        # Clean first issue followed by the first requested rotation.
        for _ in range(2):
            subprocess.run(
                ["/bin/bash", helper, "issue"],
                cwd=consecutive,
                env=consecutive_environment,
                check=True,
            )

        if consecutive_current_token.read_text().strip() != (
            "fixture-observability-token-2"
        ):
            raise RuntimeError("The first rotation did not install key 2")
        if consecutive_previous_token.read_text().strip() != (
            "fixture-observability-token-1"
        ):
            raise RuntimeError("The first rotation did not preserve key 1")

        # Simulate successful datasource validation and operator finalization.
        first_previous_key_id = json.loads(
            consecutive_previous_metadata.read_text()
        )["id"]
        subprocess.run(
            [
                "nebius",
                "iam",
                "static-key",
                "delete",
                "--id",
                first_previous_key_id,
            ],
            cwd=consecutive,
            env=consecutive_environment,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        consecutive_previous_token.unlink()
        consecutive_previous_metadata.unlink()

        # A second requested rotation must now install key 3 and preserve key 2.
        subprocess.run(
            ["/bin/bash", helper, "issue"],
            cwd=consecutive,
            env=consecutive_environment,
            check=True,
        )
        if consecutive_current_token.read_text().strip() != (
            "fixture-observability-token-3"
        ):
            raise RuntimeError("The second rotation did not install key 3")
        if consecutive_previous_token.read_text().strip() != (
            "fixture-observability-token-2"
        ):
            raise RuntimeError("The second rotation did not preserve key 2")
        if json.loads(consecutive_current_metadata.read_text())["id"] != (
            "static-key-3"
        ):
            raise RuntimeError("The second rotation recorded the wrong current key ID")
        if json.loads(consecutive_previous_metadata.read_text())["id"] != (
            "static-key-2"
        ):
            raise RuntimeError("The second rotation recorded the wrong previous key ID")

        subprocess.run(
            ["/bin/bash", helper, "cleanup"],
            cwd=consecutive,
            env=consecutive_environment,
            check=True,
        )
        if any(
            path.exists()
            for path in (
                consecutive_current_token,
                consecutive_current_metadata,
                consecutive_previous_token,
                consecutive_previous_metadata,
            )
        ):
            raise RuntimeError("Two-rotation cleanup left local credentials behind")

        consecutive_calls = consecutive_log.read_text().splitlines()
        consecutive_issues = [
            call for call in consecutive_calls if call.startswith("issue:")
        ]
        if [call.split(":", 2)[1] for call in consecutive_issues] != [
            "1",
            "2",
            "3",
        ]:
            raise RuntimeError(
                "Expected an initial issue and two consecutive rotations; "
                f"got {consecutive_issues}"
            )
        consecutive_deletes = [
            call for call in consecutive_calls if call.startswith("delete:")
        ]
        if consecutive_deletes != [
            "delete:static-key-1",
            "delete:static-key-3",
            "delete:static-key-2",
        ]:
            raise RuntimeError(
                "Expected first-rotation finalization followed by cleanup of the "
                f"second rotation; got {consecutive_deletes}"
            )


if __name__ == "__main__":
    main()
