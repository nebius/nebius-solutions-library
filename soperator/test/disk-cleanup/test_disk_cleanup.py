"""Exercise disk cleanup with a fake Nebius transport; never access cloud resources."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "modules/cleanup/scripts/disk_cleanup.py"
MODULE_SPEC = importlib.util.spec_from_file_location("disk_cleanup_under_test", SCRIPT)
cleanup = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = cleanup
MODULE_SPEC.loader.exec_module(cleanup)

PROJECT_ID = "project-cleanup-test"
CLUSTER_ID = "mk8scluster-cleanup-test"
FOREIGN_CLUSTER_ID = "mk8scluster-other-cluster"


def disk_item(disk_id: str, cluster_id: str = CLUSTER_ID) -> dict:
    return {
        "metadata": {
            "id": disk_id,
            "parent_id": PROJECT_ID,
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


class CleanupTest(unittest.IsolatedAsyncioTestCase):
    async def run_cleanup(self, items: dict, disk_ids: list, delete_failures=0, **env_overrides):
        self.commands = []
        observations = {
            disk_id: copy.deepcopy(item if isinstance(item, list) else [item])
            for disk_id, item in items.items()
        }

        async def fake_command(command):
            nonlocal delete_failures
            self.commands.append(command)
            action = command[1:4]
            if action == ["compute", "disk", "get"]:
                disk_id = command[command.index("--id") + 1]
                self.assertIn(disk_id, observations)
                values = observations[disk_id]
                item = values.pop(0) if len(values) > 1 else values[0]
                if item is None:
                    raise cleanup.CommandError(command, 1, "", "ResourceNotFound")
                return json.dumps(item)
            if action == ["compute", "disk", "delete"]:
                if delete_failures:
                    delete_failures -= 1
                    raise cleanup.CommandError(command, 1, "", "temporary API failure")
                disk_id = command[command.index("--id") + 1]
                return json.dumps({"id": "computeoperation-" + disk_id})
            if command[1:5] == ["compute", "disk", "operation", "wait"]:
                return ""
            self.fail("Unexpected CLI command (project-wide disk listing is forbidden): " + str(command))

        env = {
            "PARENT_ID": PROJECT_ID,
            "EXPECTED_CLUSTER_ID": CLUSTER_ID,
            "DISK_IDS": json.dumps(disk_ids),
        }
        env.update(env_overrides)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            cleanup, "run_command", side_effect=fake_command
        ):
            result = await cleanup.async_main()
        return result

    def deleted_ids(self):
        return [
            command[command.index("--id") + 1]
            for command in self.commands
            if command[1:4] == ["compute", "disk", "delete"]
        ]

    async def test_only_approved_disk_from_expected_cluster_is_deleted(self):
        owned = disk_item("computedisk-approved")
        foreign = disk_item("computedisk-foreign", FOREIGN_CLUSTER_ID)
        retained = disk_item("computedisk-retained")
        items = {
            item["metadata"]["id"]: item for item in [owned, foreign, retained]
        }

        result = await self.run_cleanup(
            items, [owned["metadata"]["id"], foreign["metadata"]["id"]]
        )

        self.assertEqual(result, 0)
        self.assertEqual(self.deleted_ids(), [owned["metadata"]["id"]])
        requested_ids = {
            command[command.index("--id") + 1]
            for command in self.commands
            if command[1:4] == ["compute", "disk", "get"]
        }
        self.assertNotIn(retained["metadata"]["id"], requested_ids)

    async def test_protected_attached_and_unverifiable_disks_are_preserved(self):
        changes = {
            "deletion-protected": lambda d: d["spec"].update(forbid_deletion=True),
            "read-write-attached": lambda d: d["status"].update(
                read_write_attachment="computeinstance-busy"
            ),
            "read-only-attached": lambda d: d["status"].update(
                read_only_attachments=["computeinstance-reader"]
            ),
            "missing-owner": lambda d: d["metadata"]["labels"].pop(
                "compute.csi.nebius.com/cluster-id"
            ),
            "conflicting-owner": lambda d: d["metadata"]["labels"].update(
                {"k8s.io/" + FOREIGN_CLUSTER_ID: "owned"}
            ),
            "wrong-project": lambda d: d["metadata"].update(parent_id="project-other"),
            "missing-project": lambda d: d["metadata"].pop("parent_id"),
            "unrelated-name": lambda d: d["metadata"].update(name="user-data"),
            "unrelated-namespace": lambda d: d["metadata"]["labels"].update(
                {"kubernetes.io/created-for/pvc/namespace": "other-workload"}
            ),
            "missing-status": lambda d: d.pop("status"),
            "missing-spec": lambda d: d.pop("spec"),
            "not-ready": lambda d: d["status"].update(state="CREATING"),
            "reconciling": lambda d: d["status"].update(reconciling=True),
            "managed-disk": lambda d: d["status"].update(managed_by="computeinstance-manager"),
            "locked-disk": lambda d: d["status"].update(lock_state={"locked": True}),
        }
        for scenario, change in changes.items():
            with self.subTest(scenario=scenario):
                item = disk_item("computedisk-" + scenario)
                change(item)
                disk_id = item["metadata"]["id"]
                result = await self.run_cleanup({disk_id: item}, [disk_id])
                self.assertEqual(result, 0)
                self.assertEqual(self.deleted_ids(), [])

    async def test_disk_is_revalidated_immediately_before_deletion(self):
        for change in [
            lambda d: d["metadata"]["labels"].update(
                {"compute.csi.nebius.com/cluster-id": FOREIGN_CLUSTER_ID}
            ),
            lambda d: d["status"].update(read_write_attachment="computeinstance-new-owner"),
            lambda d: d["status"].update(read_only_attachments=["computeinstance-reader"]),
            lambda d: d["spec"].update(forbid_deletion=True),
            lambda d: d["metadata"].update(parent_id="project-other"),
            lambda d: d["status"].update(state="CREATING"),
            lambda d: d["status"].update(reconciling=True),
            lambda d: d["status"].update(managed_by="computeinstance-manager"),
            lambda d: d["status"].update(lock_state={"locked": True}),
        ]:
            original = disk_item("computedisk-changed")
            changed = copy.deepcopy(original)
            change(changed)
            with self.subTest(changed=changed):
                disk_id = original["metadata"]["id"]
                result = await self.run_cleanup({disk_id: [original, changed]}, [disk_id])
                self.assertEqual(result, 0)
                self.assertEqual(self.deleted_ids(), [])
                self.assertGreaterEqual(len(self.commands), 2)

    async def test_retry_revalidates_disk_instead_of_reusing_stale_approval(self):
        original = disk_item("computedisk-retry")
        changed = copy.deepcopy(original)
        changed["status"]["read_write_attachment"] = "computeinstance-attached-during-retry"
        disk_id = original["metadata"]["id"]
        with mock.patch.object(cleanup, "requeue_delay", return_value=0):
            result = await self.run_cleanup(
                {disk_id: [original, original, changed]}, [disk_id], delete_failures=1
            )
        self.assertEqual(result, 0)
        self.assertEqual(self.deleted_ids(), [disk_id])
        get_count = sum(command[1:4] == ["compute", "disk", "get"] for command in self.commands)
        self.assertEqual(get_count, 3)

    async def test_wrong_get_response_identity_aborts_before_any_deletion(self):
        first = disk_item("computedisk-a-approved")
        unexpected = disk_item("computedisk-unexpected")
        with self.assertRaises(cleanup.CleanupError):
            await self.run_cleanup(
                {"computedisk-a-approved": first, "computedisk-b-approved": unexpected},
                ["computedisk-a-approved", "computedisk-b-approved"],
            )
        self.assertEqual(self.deleted_ids(), [])

    async def test_omitted_default_fields_and_empty_lock_state_allow_approved_disk(self):
        item = disk_item("computedisk-default-fields")
        item["spec"] = {}
        item["status"]["lock_state"] = {}
        result = await self.run_cleanup({item["metadata"]["id"]: item}, [item["metadata"]["id"]])
        self.assertEqual(result, 0)
        self.assertEqual(self.deleted_ids(), [item["metadata"]["id"]])

    async def test_already_absent_disk_is_a_safe_noop(self):
        result = await self.run_cleanup({"computedisk-absent": None}, ["computedisk-absent"])
        self.assertEqual(result, 0)
        self.assertEqual(self.deleted_ids(), [])

    async def test_legacy_or_empty_allowlist_does_not_call_nebius(self):
        for env in [
            {"PARENT_ID": PROJECT_ID},
            {"PARENT_ID": PROJECT_ID, "DISK_IDS": "[]"},
            {"PARENT_ID": PROJECT_ID, "EXPECTED_CLUSTER_ID": CLUSTER_ID, "DISK_IDS": "[]"},
        ]:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                cleanup, "run_command"
            ) as command:
                self.assertEqual(await cleanup.async_main(), 0)
                command.assert_not_called()

    async def test_invalid_allowlist_fails_before_any_cli_command(self):
        for value in [
            "not-json", "null", "{}", '"computedisk-string"',
            "[null]", "[true]", "[123]", '[""]', '["not-a-disk-id"]',
        ]:
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {"PARENT_ID": PROJECT_ID, "EXPECTED_CLUSTER_ID": CLUSTER_ID, "DISK_IDS": value},
                clear=True,
            ), mock.patch.object(cleanup, "run_command") as command:
                with self.assertRaises(cleanup.CleanupError):
                    await cleanup.async_main()
                command.assert_not_called()

    async def test_explicit_allowlist_requires_project_and_cluster_scope(self):
        for scope in [
            {"PARENT_ID": "", "EXPECTED_CLUSTER_ID": CLUSTER_ID},
            {"PARENT_ID": PROJECT_ID, "EXPECTED_CLUSTER_ID": ""},
            {"PARENT_ID": PROJECT_ID, "EXPECTED_CLUSTER_ID": "not-a-cluster-id"},
        ]:
            env = dict(scope, DISK_IDS='["computedisk-approved"]')
            with self.subTest(scope=scope), mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                cleanup, "run_command"
            ) as command:
                with self.assertRaises(cleanup.CleanupError):
                    await cleanup.async_main()
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
