"""Exercise real Terraform state and destroy ordering with a fake Nebius CLI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[2] / "modules/cleanup"
TERRAFORM = shutil.which("terraform")

LEGACY_MAIN = """
resource "terraform_data" "disk_cleanup" {
  triggers_replace = {
    parent_id = var.iam_project_id
  }
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    environment = {
      PARENT_ID = self.triggers_replace.parent_id
    }
    command = "python3 ${path.module}/scripts/disk_cleanup.py"
  }
}
"""

ROOT_MAIN = """
variable "expected_cluster_id" {
  type    = string
  default = ""
}
variable "disk_ids" {
  type    = set(string)
  default = []
}
module "cleanup" {
  source              = "./cleanup"
  iam_project_id      = "project-test"
  expected_cluster_id = var.expected_cluster_id
  disk_ids            = var.disk_ids
}
# Model the installation's dependency so cleanup runs after the cluster.
resource "terraform_data" "cluster" {
  depends_on = [module.cleanup]
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["python3"]
    command     = "${path.module}/cluster_destroy.py"
  }
}
"""

FAKE_NEBIUS = """
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["NEBIUS_TEST_COMMAND_LOG"], "a") as log:
    log.write(json.dumps({"command": args}) + "\\n")
disks = json.loads(Path(os.environ["NEBIUS_TEST_DISKS"]).read_text())
if args[:3] == ["compute", "disk", "list"]:
    print(json.dumps({"items": disks}))
elif args[:3] == ["compute", "disk", "get"]:
    disk_id = args[args.index("--id") + 1]
    disk = next((item for item in disks if item["metadata"]["id"] == disk_id), None)
    if disk is None:
        sys.stderr.write("ResourceNotFound\\n")
        sys.exit(1)
    print(json.dumps(disk))
elif args[:3] == ["compute", "disk", "delete"]:
    print(json.dumps({"id": "computeoperation-test"}))
elif args[:4] == ["compute", "disk", "operation", "wait"]:
    pass
else:
    sys.stderr.write("Unexpected fake CLI command: " + repr(args))
    sys.exit(1)
"""


def disk(disk_id, cluster_id):
    return {
        "metadata": {
            "id": disk_id,
            "parent_id": "project-test",
            "name": "pvc-" + disk_id,
            "labels": {
                "compute.csi.nebius.com/cluster-id": cluster_id,
                "k8s.io/" + cluster_id: "owned",
                "kubernetes.io/created-for/pvc/namespace": "soperator",
            },
        },
        "spec": {"forbid_deletion": False},
        "status": {"state": "READY"},
    }


@unittest.skipUnless(TERRAFORM, "Terraform is required for lifecycle regression tests")
class TerraformLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.module = self.root / "cleanup"
        shutil.copytree(MODULE, self.module)
        self.current_main = (self.module / "main.tf").read_text()
        (self.root / "main.tf").write_text(ROOT_MAIN)
        self.log = self.root / "commands.jsonl"
        fixtures = self.root / "disks.json"
        fixtures.write_text(json.dumps([
            disk("computedisk-approved", "mk8scluster-owner"),
            disk("computedisk-retained", "mk8scluster-owner"),
            disk("computedisk-foreign", "mk8scluster-foreign"),
        ]))
        executable_directory = self.root / "bin"
        executable_directory.mkdir()
        executable = executable_directory / "nebius"
        executable.write_text("#!" + sys.executable + "\n" + FAKE_NEBIUS)
        executable.chmod(0o755)
        (self.root / "cluster_destroy.py").write_text(
            'import json, os\n'
            'with open(os.environ["NEBIUS_TEST_COMMAND_LOG"], "a") as log:\n'
            '    log.write(json.dumps({"event": "cluster_destroy"}) + "\\n")\n'
        )
        self.env = dict(
            os.environ,
            PATH=str(executable_directory) + os.pathsep + os.environ.get("PATH", ""),
            TF_IN_AUTOMATION="1",
            TF_INPUT="0",
            CHECKPOINT_DISABLE="1",
            NEBIUS_TEST_COMMAND_LOG=str(self.log),
            NEBIUS_TEST_DISKS=str(fixtures),
        )
        self.terraform("init", "-backend=false", "-input=false")

    def terraform(self, *args):
        result = subprocess.run(
            [TERRAFORM, *args, "-no-color"],
            cwd=self.root, env=self.env, text=True, capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def apply(self):
        self.terraform("apply", "-auto-approve", "-input=false")

    def configure(self, cluster_id, disk_ids):
        (self.root / "terraform.tfvars.json").write_text(json.dumps({
            "expected_cluster_id": cluster_id,
            "disk_ids": disk_ids,
        }))

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self):
        return [event["command"] for event in self.events() if "command" in event]

    def assert_in_place_update(self):
        self.terraform("plan", "-input=false", "-out=update.tfplan")
        plan = json.loads(self.terraform("show", "-json", "update.tfplan"))
        cleanup_change = next(
            change for change in plan["resource_changes"]
            if change["address"] == "module.cleanup.terraform_data.disk_cleanup"
        )
        self.assertEqual(cleanup_change["change"]["actions"], ["update"])
        self.apply()
        self.assertEqual(self.commands(), [])

    def test_destroy_legacy_state_without_applying_upgrade_makes_no_cli_calls(self):
        (self.module / "main.tf").write_text(LEGACY_MAIN)
        self.apply()
        (self.module / "main.tf").write_text(self.current_main)
        # A newly configured policy must not authorize old parent-only state.
        self.configure("mk8scluster-owner", ["computedisk-approved"])
        self.terraform("destroy", "-auto-approve", "-input=false")
        self.assertEqual(self.commands(), [])

    def test_upgrade_and_policy_changes_update_state_without_deletion(self):
        (self.module / "main.tf").write_text(LEGACY_MAIN)
        self.apply()
        (self.module / "main.tf").write_text(self.current_main)
        self.configure("mk8scluster-owner", ["computedisk-approved"])
        self.assert_in_place_update()
        self.configure("mk8scluster-foreign", ["computedisk-foreign"])
        self.assert_in_place_update()

    def test_destroy_uses_applied_scope_after_cluster_and_preserves_other_disks(self):
        self.configure("mk8scluster-owner", ["computedisk-approved"])
        self.apply()
        self.assertEqual(self.commands(), [])
        # These unapplied values must not expand the destroy-time policy.
        self.configure("mk8scluster-foreign", ["computedisk-foreign"])
        self.terraform("destroy", "-auto-approve", "-input=false")
        deleted = [
            args[args.index("--id") + 1] for args in self.commands()
            if args[:3] == ["compute", "disk", "delete"]
        ]
        self.assertEqual(deleted, ["computedisk-approved"])
        self.assertEqual(self.events()[0], {"event": "cluster_destroy"})


if __name__ == "__main__":
    unittest.main()
