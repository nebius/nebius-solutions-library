"""Offline tests: mocked disk tools only; never access or format block devices."""
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "modules/cloud-init/simple-setup-init.tftpl"
SCRIPT = ROOT / "modules/cloud-init/prepare-local-nvme.sh"


def render(mode="raid0", enabled=True):
    values = dict(
        region="eu-west2", enable_local_disks=enabled,
        local_disks_mount_mode=mode, local_disks_expected_count=6,
        local_nvme_drives_path="/scratch",
        local_disks_dependent_services=["training.service"], users=[],
        extra_disk_id="", extra_path="/extra", shared_filesystem_id="",
        shared_filesystem_mount="", aws_access_key_id="", aws_secret_access_key="",
        mount_bucket="", s3_mount_path="",
    )
    # Use Terraform's YAML decoder too: indentation errors must fail the tests.
    expression = (
        f"jsonencode(yamldecode(templatefile({json.dumps(str(TEMPLATE))}, "
        f"merge({json.dumps(values)}, "
        f"{{local_nvme_script = file({json.dumps(str(SCRIPT))})}}))))"
    )
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            ["terraform", "console"], input=expression, text=True,
            capture_output=True, cwd=cwd, check=True,
        )
    return json.loads(json.loads(result.stdout.strip()))


class TemplateTests(unittest.TestCase):
    def test_disabled(self):
        config = render(enabled=False)
        self.assertNotIn("write_files", config)
        self.assertNotIn("packages", config)
        self.assertNotIn("runcmd", config)

    def test_enabled_modes(self):
        for mode in ("raid0", "raw"):
            with self.subTest(mode=mode):
                config = render(mode)
                files = {entry["path"]: entry for entry in config["write_files"]}
                script = files["/usr/local/sbin/prepare-local-nvme.sh"]
                self.assertEqual(script["permissions"], "0755")
                self.assertEqual(script["content"], SCRIPT.read_text())
                subprocess.run(["bash", "-n"], input=script["content"], text=True, check=True)
                unit = files["/etc/systemd/system/local-nvme.service"]["content"]
                self.assertIn(f"ExecStart=/usr/local/sbin/prepare-local-nvme.sh {mode} /scratch 6", unit)
                dropin = files["/etc/systemd/system/training.service.d/local-nvme.conf"]["content"]
                self.assertIn("Requires=local-nvme.service", dropin)
                self.assertIn("After=local-nvme.service", dropin)
                self.assertEqual("mdadm" in config["packages"], mode == "raid0")
                self.assertIn("systemctl enable --now local-nvme.service", config["runcmd"])


# Every disk-inspection and mutation tool is replaced before calling a function.
# Unexpected mock invocations fail so no test can fall through to real disk tools.
MOCKS = r'''
realpath() { printf '%s\n' "$2"; }
mkdir() { :; }
udevadm() { :; }
sleep() { :; }
mountpoint() { return 32; }
lsblk() { echo "Unexpected lsblk: $*" >&2; return 99; }
findmnt() { echo "Unexpected findmnt: $*" >&2; return 99; }
wipefs() { return 0; }
blkid() { return 2; }
mdadm() { echo "Unexpected mdadm: $*" >&2; return 99; }
find() { echo "Unexpected find: $*" >&2; return 99; }
readlink() { echo /dev/md127; }
cat() { echo "Unexpected cat: $*" >&2; return 99; }
mkfs.ext4() { echo "ACTION mkfs $*" >&2; }
mount() { echo "ACTION mount $*" >&2; }
chown() { echo "ACTION chown $*" >&2; }
chmod() { echo "ACTION chmod $*" >&2; }
NVME_DISKS=(/dev/fake0)
EXPECTED_DISKS=1
MD_DEV=/dev/nonexistent-local-nvme-test
MOUNTPOINT=/mnt/test
'''


class DiskSafetyTests(unittest.TestCase):
    def run_script(self, overrides, call):
        return subprocess.run(
            ["bash"], input=f"source {shlex.quote(str(SCRIPT))}\n{MOCKS}\n{overrides}\n{call}\n",
            text=True, capture_output=True,
        )

    def assert_refused(self, overrides, call, message):
        result = self.run_script(overrides, call)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("ACTION", result.stderr)
        return result

    def test_unsafe_paths_and_arguments(self):
        for path in ("/", "/etc", "/mnt/../etc", "/scratch;echo bad"):
            with self.subTest(path=path):
                self.assert_refused("", f"main raw {shlex.quote(path)} 6", "Unsafe mount path")
        self.assert_refused("", "main invalid /scratch 6", "Unsupported local disk mode")
        self.assert_refused("", "main raw /scratch 0", "Invalid expected disk count")

    def test_signature_inspection_failure(self):
        self.assert_refused("wipefs() { return 1; }", "blank_device /dev/fake0", "Cannot inspect signatures")

    def test_mount_inspection_failures(self):
        cases = [
            ("mountpoint() { return 1; }", "Cannot inspect mountpoint"),
            ("lsblk() { return 1; }", "Cannot inspect device mounts"),
            ("lsblk() { :; }; blkid() { return 4; }", "Cannot inspect filesystem"),
            ("lsblk() { :; }; blkid() { return 8; }", "Cannot inspect filesystem"),
            ("lsblk() { :; }; blkid() { :; }", "Empty filesystem type"),
            ("lsblk() { :; }; wipefs() { return 1; }", "Cannot inspect signatures"),
            ("lsblk() { :; }; wipefs() { echo crypto_LUKS; }", "Unrecognized signature"),
            ("lsblk() { :; }; blkid() { echo xfs; }", "Unexpected filesystem"),
            ("lsblk() { echo /elsewhere; }", "Device already mounted elsewhere"),
            ("realpath() { echo /elsewhere; }", "Symlink"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message, overrides=overrides):
                self.assert_refused(overrides, "mount_device /dev/fake0 /mnt/test", message)

    def test_mounted_device_identity(self):
        for overrides, message in [
            ("findmnt() { return 1; }; lsblk() { return 1; }", "Cannot identify mount"),
            ("findmnt() { echo 8:1; }; lsblk() { return 1; }", "Cannot identify device"),
            ("findmnt() { :; }; lsblk() { :; }", "Wrong device mounted"),
            ("findmnt() { echo 8:1; }; lsblk() { echo 8:2; }", "Wrong device mounted"),
        ]:
            with self.subTest(message=message, overrides=overrides):
                self.assert_refused("mountpoint() { return 0; }; " + overrides,
                                    "mount_device /dev/fake0 /mnt/test", message)

    def test_mount_blank_existing_and_already_mounted(self):
        cases = [
            ("lsblk() { :; }", True, True),
            ("lsblk() { :; }; blkid() { echo ext4; }", False, True),
            ("mountpoint() { return 0; }; findmnt() { echo 8:1; }; lsblk() { echo ' 8:1 '; }", False, False),
        ]
        for overrides, formats, mounts in cases:
            with self.subTest(overrides=overrides):
                result = self.run_script(overrides, "mount_device /dev/fake0 /mnt/test")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("ACTION mkfs" in result.stderr, formats)
                self.assertEqual("ACTION mount" in result.stderr, mounts)
                self.assertIn("ACTION chmod 1777 /mnt/test", result.stderr)

    def test_root_and_partition_inspections(self):
        root = 'findmnt() { echo /dev/root; }; '
        cases = [
            ("findmnt() { return 1; }", "Cannot identify root mount"),
            ("findmnt() { :; }", "Empty root mount source"),
            (root + "lsblk() { return 1; }", "Cannot identify root backing devices"),
            (root + "lsblk() { :; }", "Empty root backing devices"),
            (root + "lsblk() { echo /dev/fake0; }", "Root backing disk selected"),
            (root + 'lsblk() { [[ "$1" != -srnpo ]] || { echo /dev/root; return; }; return 1; }', "Cannot inspect device type"),
            (root + 'lsblk() { case "$1" in -srnpo) echo /dev/root;; -dn) echo disk;; *) return 1;; esac; }', "Cannot inspect partitions"),
            (root + 'lsblk() { case "$1" in -srnpo) echo /dev/root;; -dn) echo disk;; *) printf "disk\\npart\\n";; esac; }', "Partitioned disk"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                self.assert_refused(overrides, "check_disks", message)

    def test_holder_inspection_failure(self):
        self.assert_refused("lsblk() { return 1; }", "check_no_holders /dev/fake0", "Cannot inspect holders")
        self.assert_refused("lsblk() { :; }", "check_no_holders /dev/fake0", "Device has holders")

    def test_raid_inspection_failures(self):
        cases = [
            ("wipefs() { return 1; }", "Cannot inspect signatures"),
            ("wipefs() { echo linux_raid_member; }; mdadm() { return 1; }", "Cannot inspect RAID member"),
            ("wipefs() { echo linux_raid_member; }; mdadm() { :; }", "Non-RAID signature"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                self.assert_refused(overrides, "prepare_raid", message)

    def test_mixed_and_partial_raid(self):
        base = 'NVME_DISKS=(/dev/fake0 /dev/fake1); EXPECTED_DISKS=2; '
        self.assert_refused(base + '''
            wipefs() { echo linux_raid_member; }
            mdadm() { echo "MD_UUID=${*: -1}"; }
        ''', "prepare_raid", "Mixed RAID arrays")
        self.assert_refused(base + '''
            wipefs() { [[ "${*: -1}" == /dev/fake0 ]] || echo linux_raid_member; }
            lsblk() { echo fake0; }
            mdadm() { echo MD_UUID=one; }
        ''', "prepare_raid", "Partial RAID signatures")

    def test_discovery_failure_and_count_mismatch(self):
        self.assert_refused("find() { return 1; }", "discover_disks", "Cannot enumerate NVMe devices")
        self.assert_refused("find() { echo /dev/fake0; }; EXPECTED_DISKS=2",
                            "discover_disks", "Expected 2 NVMe devices; found 1")
        result = self.run_script("find() { printf '/dev/fake10\\n/dev/fake2\\n'; }; EXPECTED_DISKS=2",
                                 'discover_disks; printf "%s\\n" "${NVME_DISKS[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["/dev/fake2", "/dev/fake10"])

    def test_raw_disk_mounts_and_parent_conflict(self):
        self.assert_refused("mountpoint() { return 0; }", "prepare_raw", "Raw-mode parent is already a mount")
        self.assert_refused("lsblk() { return 1; }", "prepare_raw", "Cannot inspect holders")
        result = self.run_script('''
            NVME_DISKS=(/dev/fake0 /dev/fake1)
            lsblk() { echo "${*: -1}"; }
            mount_device() { echo "ACTION mount_device $*" >&2; }
        ''', "prepare_raw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION mount_device /dev/fake0 /mnt/test/disk-0", result.stderr)
        self.assertIn("ACTION mount_device /dev/fake1 /mnt/test/disk-1", result.stderr)

    def test_existing_raid_assembly(self):
        result = self.run_script('''
            wipefs() { echo linux_raid_member; }
            mdadm() {
                case "$1" in
                    --examine) echo MD_UUID=one;;
                    --detail) echo MD_UUID=unrelated;;
                    --assemble) echo "ACTION assemble $*" >&2;;
                    *) return 99;;
                esac
            }
            cat() { case "$1" in */level) echo raid0;; */raid_disks) echo 1;; *) return 99;; esac; }
            find() { echo /dev/fake0; }
            mount_device() { echo "ACTION mount_device $*" >&2; }
        ''', "prepare_raid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION assemble", result.stderr)
        self.assertIn("ACTION mount_device", result.stderr)

    def test_blank_raid_creation(self):
        result = self.run_script('''
            lsblk() { echo fake0; }
            mdadm() { echo "ACTION mdadm $*" >&2; }
            cat() { case "$1" in */level) echo raid0;; */raid_disks) echo 1;; *) return 99;; esac; }
            find() { echo /dev/fake0; }
            mount_device() { echo "ACTION mount_device $*" >&2; }
        ''', "prepare_raid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION mdadm --create", result.stderr)
        self.assertIn("ACTION mount_device", result.stderr)


if __name__ == "__main__":
    unittest.main()
