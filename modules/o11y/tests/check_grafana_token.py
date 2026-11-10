#!/usr/bin/env python3
"""Plan the production Grafana resource with mocked providers.

The temporary fixture uses the real grafana.tf, variables.tf, and provider
requirements. It proves that a token prepared before planning reaches the
Marketplace application on the first plan, without contacting Nebius.
Requires Terraform >= 1.11 on PATH (or set TERRAFORM_BIN).
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile


MODULE = Path(__file__).resolve().parents[1]


def check_production_wiring():
    source = (MODULE / "grafana.tf").read_text()
    module_variables = (MODULE / "variables.tf").read_text()
    root_variables = (MODULE.parents[1] / "k8s-training/variables.tf").read_text()
    required = (
        'var.parent_id,',
        'var.grafana_observability_key_id,',
        'random_password.grafana_password[0].result,',
        '"grafana.nebius.accessToken" = var.grafana_observability_access_token',
    )
    for expression in required:
        if expression not in source:
            raise RuntimeError(f"Missing production Grafana credential wiring: {expression}")
    forbidden = (
        'resource "terraform_data" "grafana_access_token"',
        "grafana_access_token.txt",
        "file(local.grafana_access_token_path)",
    )
    for expression in forbidden:
        if expression in source:
            raise RuntimeError(f"Obsolete apply-time token path remains: {expression}")
    for location, variables in (
        ("root", root_variables),
        ("o11y module", module_variables),
    ):
        token_block = variables.split(
            'variable "grafana_observability_access_token" {', 1
        )[1].split("}", 1)[0]
        if "sensitive   = true" not in token_block or "ephemeral   = true" not in token_block:
            raise RuntimeError(
                f"The {location} Grafana token variable must remain sensitive and ephemeral"
            )


def main():
    check_production_wiring()
    terraform = os.environ.get("TERRAFORM_BIN", "terraform")
    with tempfile.TemporaryDirectory(prefix="o11y-grafana-token-") as directory:
        fixture = Path(directory)
        for filename in ("grafana.tf", "variables.tf", "versions.tf"):
            shutil.copyfile(MODULE / filename, fixture / filename)
        tests = fixture / "tests"
        tests.mkdir()
        shutil.copyfile(
            MODULE / "tests/grafana-token/grafana_token.tftest.hcl",
            tests / "grafana_token.tftest.hcl",
        )
        subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=fixture,
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
            check=True,
        )


if __name__ == "__main__":
    main()
