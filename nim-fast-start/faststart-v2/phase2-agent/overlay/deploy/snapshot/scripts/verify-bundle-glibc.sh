#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

version_is_greater() {
  local candidate=$1
  local ceiling=$2
  local candidate_major candidate_minor candidate_patch
  local ceiling_major ceiling_minor ceiling_patch

  if [[ ! ${candidate} =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
    echo "invalid candidate version: ${candidate}" >&2
    return 2
  fi
  if [[ ! ${ceiling} =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
    echo "invalid ceiling version: ${ceiling}" >&2
    return 2
  fi

  IFS=. read -r candidate_major candidate_minor candidate_patch <<<"${candidate}"
  IFS=. read -r ceiling_major ceiling_minor ceiling_patch <<<"${ceiling}"
  candidate_patch=${candidate_patch:-0}
  ceiling_patch=${ceiling_patch:-0}

  ((10#${candidate_major} > 10#${ceiling_major})) && return 0
  ((10#${candidate_major} < 10#${ceiling_major})) && return 1
  ((10#${candidate_minor} > 10#${ceiling_minor})) && return 0
  ((10#${candidate_minor} < 10#${ceiling_minor})) && return 1
  ((10#${candidate_patch} > 10#${ceiling_patch}))
}

self_test() {
  if version_is_greater 2.35 2.35 \
    || version_is_greater 2.34 2.35 \
    || version_is_greater 2.2.5 2.35; then
    echo "version comparison accepted a value at or below the ceiling" >&2
    return 1
  fi
  if ! version_is_greater 2.35.1 2.35 \
    || ! version_is_greater 2.36 2.35 \
    || ! version_is_greater 3.0 2.35; then
    echo "version comparison rejected a value above the ceiling" >&2
    return 1
  fi
  comparison_status=0
  version_is_greater 2..35 2.35 >/dev/null 2>&1 || comparison_status=$?
  if ((comparison_status != 2)); then
    echo "version comparison did not reject a malformed value" >&2
    return 1
  fi
  echo "verify-bundle-glibc version comparison self-test passed"
}

if [[ ${1:-} == "--self-test" ]]; then
  if [[ $# -ne 1 ]]; then
    echo "usage: $0 --self-test" >&2
    exit 2
  fi
  self_test
  exit 0
fi

if [[ $# -ne 3 ]]; then
  echo "usage: $0 BUNDLE_DIR MAX_GLIBC_VERSION RECEIPT" >&2
  exit 2
fi

readonly bundle_dir=${1%/}
readonly max_glibc=$2
readonly receipt=$3

if [[ ! -d ${bundle_dir} ]]; then
  echo "bundle directory does not exist: ${bundle_dir}" >&2
  exit 1
fi
if [[ ! ${max_glibc} =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
  echo "invalid maximum GLIBC version: ${max_glibc}" >&2
  exit 2
fi
if ! command -v readelf >/dev/null 2>&1; then
  echo "readelf is required" >&2
  exit 1
fi
if find "${bundle_dir}" -type l -print -quit | grep -q .; then
  echo "bundle contains a symlink" >&2
  exit 1
fi

mapfile -d '' bundle_files < <(find "${bundle_dir}" -type f -print0 | sort -z)
if ((${#bundle_files[@]} == 0)); then
  echo "bundle contains no regular files" >&2
  exit 1
fi

elf_count=0
versioned_elf_count=0
audit_lines=()
for file_path in "${bundle_files[@]}"; do
  if ! readelf -h "${file_path}" >/dev/null 2>&1; then
    continue
  fi

  ((elf_count += 1))
  relative_path=${file_path#"${bundle_dir}"/}
  mapfile -t glibc_versions < <(
    readelf --version-info "${file_path}" 2>/dev/null \
      | sed -n 's/.*Name: GLIBC_\([0-9][0-9.]*\).*/\1/p' \
      | sort -Vu
  )

  max_required=none
  if ((${#glibc_versions[@]} > 0)); then
    ((versioned_elf_count += 1))
    max_required=${glibc_versions[-1]}
  fi
  for required in "${glibc_versions[@]}"; do
    comparison_status=0
    version_is_greater "${required}" "${max_glibc}" || comparison_status=$?
    if ((comparison_status == 0)); then
      echo "GLIBC_${required} exceeds GLIBC_${max_glibc}: ${relative_path}" >&2
      exit 1
    fi
    if ((comparison_status > 1)); then
      echo "could not compare GLIBC requirement for ${relative_path}: ${required}" >&2
      exit 1
    fi
  done
  audit_lines+=("${relative_path}"$'\t'"max_glibc=${max_required}")
done

if ((elf_count == 0)); then
  echo "bundle contains no ELF files" >&2
  exit 1
fi

receipt_tmp=${receipt}.tmp
{
  echo "schema_version=1"
  echo "max_allowed=GLIBC_${max_glibc}"
  echo "elf_files=${elf_count}"
  echo "versioned_elfs=${versioned_elf_count}"
  printf '%s\n' "${audit_lines[@]}"
} >"${receipt_tmp}"
mv -f -- "${receipt_tmp}" "${receipt}"
echo "PASS elf_files=${elf_count} versioned_elfs=${versioned_elf_count} max_allowed=GLIBC_${max_glibc}"
