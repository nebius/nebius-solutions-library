#!/usr/bin/env bash
# Prepare ephemeral local NVMe storage. Source this file to test with mocked tools.
set -euo pipefail

fail() { echo "[local-nvme] $*" >&2; exit 1; }

# Inspection errors must never be interpreted as an empty/blank device.
inspect() {
  "$@" || fail "Inspection failed: $*"
}

blank_device() {
  local signatures
  signatures=$(inspect wipefs --noheadings --output TYPE "$1") || fail "Cannot inspect signatures: $1"
  [[ -z "$signatures" ]]
}

is_mounted() {
  local status
  if mountpoint -q "$1"; then
    return 0
  else
    status=$?
    # util-linux mountpoint returns 32 for a path that is not a mountpoint.
    [[ "$status" -eq 32 ]] || fail "Cannot inspect mountpoint: $1 (status $status)"
    return 1
  fi
}

check_no_holders() {
  local names
  names=$(inspect lsblk -nr -o NAME "$1") || fail "Cannot inspect holders: $1"
  [[ -n "$names" && "$names" != *$'\n'* ]] || fail "Device has holders: $1"
}

check_disks() {
  local root_source root_disks disk disk_type types
  root_source=$(inspect findmnt -n -o SOURCE /) || fail "Cannot identify root mount"
  [[ -n "$root_source" ]] || fail "Empty root mount source"
  root_disks=$(inspect lsblk -srnpo NAME "$root_source") || fail "Cannot identify root backing devices"
  [[ -n "$root_disks" ]] || fail "Empty root backing devices"
  for disk in "${NVME_DISKS[@]}"; do
    grep -Fxq "$disk" <<< "$root_disks" && fail "Root backing disk selected: $disk"
    disk_type=$(inspect lsblk -dn -o TYPE "$disk") || fail "Cannot inspect device type: $disk"
    [[ "$disk_type" == disk ]] || fail "Not a whole disk: $disk"
    types=$(inspect lsblk -nr -o TYPE "$disk") || fail "Cannot inspect partitions: $disk"
    [[ -n "$types" ]] || fail "Empty device types: $disk"
    if grep -Fxq part <<< "$types"; then
      fail "Partitioned disk: $disk"
    fi
  done
}

mount_device() {
  local device="$1" target="$2" fs status mounted_id device_id mounts canonical
  canonical=$(inspect realpath -m "$target") || fail "Cannot resolve $target"
  [[ "$canonical" == "$target" ]] || fail "Symlink in $target"
  mkdir -p "$target"
  if is_mounted "$target"; then
    mounted_id=$(inspect findmnt -n -o MAJ:MIN --mountpoint "$target") || fail "Cannot identify mount: $target"
    device_id=$(inspect lsblk -dn -o MAJ:MIN "$device") || fail "Cannot identify device: $device"
    device_id=$(xargs <<< "$device_id")
    [[ -n "$mounted_id" && -n "$device_id" && "$mounted_id" == "$device_id" ]] || fail "Wrong device mounted at $target"
  else
    mounts=$(inspect lsblk -nr -o MOUNTPOINTS "$device") || fail "Cannot inspect device mounts: $device"
    [[ -z "${mounts//[[:space:]]/}" ]] || fail "Device already mounted elsewhere: $device"
    if fs=$(blkid -p -s TYPE -o value "$device"); then
      [[ -n "$fs" ]] || fail "Empty filesystem type: $device"
    else
      status=$?
      # blkid uses 2 when no requested signature is found. Other errors fail closed.
      [[ "$status" -eq 2 && -z "$fs" ]] || fail "Cannot inspect filesystem: $device (status $status)"
    fi
    if [[ -z "$fs" ]]; then
      blank_device "$device" || fail "Unrecognized signature: $device"
      mkfs.ext4 "$device"
    elif [[ "$fs" != ext4 ]]; then
      fail "Unexpected filesystem on $device: $fs"
    fi
    mount -t ext4 -o defaults,relatime,rw "$device" "$target"
  fi
  chown nobody:nogroup "$target"
  chmod 1777 "$target"
}

# Discovery is best-effort for other arrays; selected-array checks remain fatal.
find_active_raid() {
  local uuid="$1" candidate device info active_uuid
  shift
  for candidate in "$@"; do
    [[ -e "$candidate" ]] || continue
    device="/dev/${candidate##*/}"
    if ! info=$(mdadm --detail --export "$device"); then
      echo "[local-nvme] Warning: cannot inspect RAID candidate $device; skipping" >&2
      continue
    fi
    active_uuid=$(awk -F= '$1 == "MD_UUID" {print $2}' <<< "$info")
    if [[ -z "$active_uuid" ]]; then
      echo "[local-nvme] Warning: missing UUID for RAID candidate $device; skipping" >&2
      continue
    fi
    if [[ "$active_uuid" == "$uuid" ]]; then
      printf '%s\n' "$device"
      return 0
    fi
  done
}

prepare_raid() {
  local uuid="" blank=0 disk signatures info member_uuid active
  local resolved md_name level raid_disks actual expected
  for disk in "${NVME_DISKS[@]}"; do
    signatures=$(inspect wipefs --noheadings --output TYPE "$disk") || fail "Cannot inspect signatures: $disk"
    if [[ -z "$signatures" ]]; then
      check_no_holders "$disk"
      blank=$((blank + 1))
    else
      # An existing signature requires a successful RAID inspection and UUID.
      info=$(inspect mdadm --examine --export "$disk") || fail "Cannot inspect RAID member: $disk"
      member_uuid=$(awk -F= '$1 == "MD_UUID" {print $2}' <<< "$info")
      [[ -n "$member_uuid" ]] || fail "Non-RAID signature on $disk"
      [[ -z "$uuid" || "$uuid" == "$member_uuid" ]] || fail "Mixed RAID arrays"
      uuid="$member_uuid"
    fi
  done
  if [[ "$blank" -eq "$EXPECTED_DISKS" ]]; then
    [[ ! -e "$MD_DEV" ]] || fail "RAID name already in use"
    mdadm --create "$MD_DEV" --name=local-nvme --metadata=1.2 --level=0 --raid-devices="$EXPECTED_DISKS" --run "${NVME_DISKS[@]}"
  else
    [[ "$blank" -eq 0 ]] || fail "Partial RAID signatures; refusing reconstruction"
    # Locate the array by UUID even when udev assembled it under another name.
    active=$(find_active_raid "$uuid" /sys/block/md*)
    if [[ -n "$active" ]]; then
      MD_DEV="$active"
    else
      [[ ! -e "$MD_DEV" ]] || fail "RAID name already in use"
      mdadm --assemble "$MD_DEV" --uuid="$uuid" "${NVME_DISKS[@]}"
    fi
  fi
  resolved=$(inspect readlink -f "$MD_DEV") || fail "Cannot resolve RAID device"
  [[ -n "$resolved" ]] || fail "Empty RAID device path"
  md_name=${resolved##*/}
  level=$(inspect cat "/sys/block/$md_name/md/level") || fail "Cannot inspect RAID level"
  [[ "$level" == raid0 ]] || fail "Unexpected RAID level"
  raid_disks=$(inspect cat "/sys/block/$md_name/md/raid_disks") || fail "Cannot inspect RAID size"
  [[ "$raid_disks" == "$EXPECTED_DISKS" ]] || fail "Unexpected RAID size"
  actual=$(inspect find "/sys/block/$md_name/slaves" -mindepth 1 -maxdepth 1 -printf '/dev/%f\n') || fail "Cannot inspect RAID members"
  actual=$(sort <<< "$actual")
  expected=$(printf '%s\n' "${NVME_DISKS[@]}" | sort)
  [[ "$actual" == "$expected" ]] || fail "Unexpected RAID members"
  mount_device "$MD_DEV" "$MOUNTPOINT"
}

discover_disks() {
  local enumeration attempt disk
  # Retry enumeration, but never guess how many disks constitute the allocation.
  for ((attempt = 0; attempt < 30; attempt++)); do
    udevadm settle --timeout=5 || true
    enumeration=$(inspect find /dev -maxdepth 1 -regextype posix-extended -regex '/dev/nvme[0-9]+n[0-9]+' -type b) || fail "Cannot enumerate NVMe devices"
    NVME_DISKS=()
    if [[ -n "$enumeration" ]]; then
      while IFS= read -r disk; do NVME_DISKS+=("$disk"); done < <(sort -V <<< "$enumeration")
    fi
    [[ "${#NVME_DISKS[@]}" -eq "$EXPECTED_DISKS" ]] && break
    sleep 2
  done
  [[ "${#NVME_DISKS[@]}" -eq "$EXPECTED_DISKS" ]] || fail "Expected $EXPECTED_DISKS NVMe devices; found ${#NVME_DISKS[@]}"
}

prepare_raw() {
  local index disk
  if is_mounted "$MOUNTPOINT"; then
    fail "Raw-mode parent is already a mount"
  fi
  index=0
  for disk in "${NVME_DISKS[@]}"; do
    check_no_holders "$disk"
    mount_device "$disk" "$MOUNTPOINT/disk-$index"
    index=$((index + 1))
  done
}

main() {
  [[ "$#" -eq 3 ]] || fail "Usage: $0 <raid0|raw> <mount-path> <disk-count>"
  local MODE="$1" MOUNTPOINT="$2" EXPECTED_DISKS="$3" MD_DEV=/dev/md/local-nvme
  local canonical
  local -a NVME_DISKS=()
  [[ "$MODE" == raid0 || "$MODE" == raw ]] || fail "Unsupported local disk mode"
  [[ "$EXPECTED_DISKS" =~ ^[1-9][0-9]*$ ]] || fail "Invalid expected disk count"
  # Only dedicated scratch locations are accepted; reject symlink traversal.
  [[ "$MOUNTPOINT" =~ ^/(scratch|mnt/[a-zA-Z0-9_-]+|srv/[a-zA-Z0-9_-]+)$ ]] || fail "Unsafe mount path"
  canonical=$(inspect realpath -m "$MOUNTPOINT") || fail "Cannot resolve mount path"
  [[ "$canonical" == "$MOUNTPOINT" ]] || fail "Symlink in mount path"
  mkdir -p "$MOUNTPOINT"
  exec 9>/run/local-nvme.lock
  flock -n 9 || fail "Preparation already running"

  discover_disks
  check_disks
  if [[ "$MODE" == raid0 ]]; then
    prepare_raid
  else
    prepare_raw
  fi
  # Mounts are recreated by this service on each boot; never persist ephemeral UUIDs.
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
