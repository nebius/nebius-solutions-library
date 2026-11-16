"""Render the login values with Python 3 and Terraform; no providers needed."""
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

TEMPLATE = Path(__file__).resolve().parents[2] / "modules/slurm/templates/helm_values/terraform_fluxcd_values.yaml.tftpl"


def render_login(config_map_ref, root_public_keys):
    # Isolate the complete login block to avoid unrelated cluster prerequisites.
    template = TEMPLATE.read_text()
    start = template.index("              login:\n")
    end = template.index("              exporter:\n", start)
    login_template = textwrap.dedent(template[start:end])
    resources = {"cpu": 1, "memory": 2, "ephemeral_storage": 10}
    variables = {
        "apparmor_profile": "unconfined",
        "tailscale_enabled": False,
        "telemetry_enabled": False,
        "slurm_cluster": {
            "nodes": {
                "login": {
                    "size": 1,
                    "autoscaling": None,
                    "k8s_node_filter_name": "login",
                    "allocation_id": None,
                    "sshd_config_map_ref_name": config_map_ref,
                    "root_public_keys": root_public_keys,
                    "resources": resources,
                },
                "munge": {"resources": resources},
            },
            "jail_submounts": [],
            "nfs": {"enabled": False},
            "nfs_in_k8s": {"enabled": False},
        },
    }
    with tempfile.TemporaryDirectory() as directory:
        template_path = Path(directory) / "login.yaml.tftpl"
        template_path.write_text(login_template)
        expression = f"jsonencode(yamldecode(templatefile({json.dumps(str(template_path))}, {json.dumps(variables)})))"
        result = subprocess.run(
            ["terraform", "console", "-no-color"], input=expression + "\n",
            cwd=directory, text=True, capture_output=True, check=True,
        )
    return json.loads(json.loads(result.stdout))["login"]


class SSHDConfigTest(unittest.TestCase):
    def test_config_reference_is_independent_of_root_public_keys(self):
        for reference, keys in itertools.product(
            ["", "login-sshd-custom"], [[], ["ssh-ed25519 test-key"]],
        ):
            with self.subTest(config_map_ref=reference, root_public_keys=keys):
                login = render_login(reference, keys)
                self.assertEqual(login.get("sshdConfigMapRefName", ""), reference)
                self.assertEqual(login.get("sshRootPublicKeys", []), keys)


if __name__ == "__main__":
    unittest.main()
