#!/usr/bin/env python3
"""Plan the production access-key definitions with mocked providers.

Terraform 1.11 cannot mock the module's ephemeral MysteryBox resources. Build a
temporary fixture containing the actual access-key blocks and IAM dependencies,
without secret retrieval, application resources, or local-exec provisioners.
Requires Terraform >= 1.11 on PATH (or set TERRAFORM_BIN).
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


MODULE = Path(__file__).resolve().parents[1]
BLOCKS = {
    "loki.tf": (
        ('data', 'nebius_iam_v1_group', 'loki_editors'),
        ('resource', 'nebius_iam_v1_service_account', 'loki_s3_sa'),
        ('resource', 'nebius_iam_v1_group_membership', 'loki_sa_storage_editor'),
        ('resource', 'nebius_iam_v2_access_key', 'loki_s3_key'),
    ),
    "grafana.tf": (('resource', 'nebius_iam_v2_access_key', 'grafana_key'),),
}


def main():
    terraform = os.environ.get("TERRAFORM_BIN", "terraform")
    with tempfile.TemporaryDirectory(prefix="o11y-access-keys-") as directory:
        fixture = Path(directory)
        for filename in ("variables.tf", "versions.tf"):
            shutil.copyfile(MODULE / filename, fixture / filename)
        for filename, blocks in BLOCKS.items():
            source = (MODULE / filename).read_text()
            selected = []
            for kind, resource_type, name in blocks:
                # In terraform-fmt output, only the top-level closing brace is
                # unindented. Copy complete blocks, including nested account fields.
                header = f'{kind} "{resource_type}" "{name}"'
                matches = re.findall(
                    rf'^{re.escape(header)}\s*\{{.*?^\}}', source, re.MULTILINE | re.DOTALL
                )
                if len(matches) != 1:
                    raise RuntimeError(f"Expected one formatted {header} block in {filename}")
                selected.append(matches[0])
            (fixture / filename).write_text("\n\n".join(selected) + "\n")
        tests = fixture / "tests"
        tests.mkdir()
        shutil.copyfile(
            MODULE / "tests/access-keys/access_keys.tftest.hcl",
            tests / "access_keys.tftest.hcl",
        )
        subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-no-color"],
            cwd=fixture, check=True,
        )
        subprocess.run(
            [terraform, "test", "-filter=tests/access_keys.tftest.hcl", "-no-color"],
            cwd=fixture, check=True,
        )


if __name__ == "__main__":
    main()
