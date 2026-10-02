#!/usr/bin/env bash
set -euo pipefail

export GIT_NO_LAZY_FETCH=1
export GIT_TERMINAL_PROMPT=0

readonly expected_ref="f7f37be174d252590c4b56e25ff4262dd82466fd"

if [[ $# -ne 2 ]]; then
  echo "usage: $0 CLEAN_DYNAMO_SOURCE EMPTY_DESTINATION" >&2
  exit 2
fi

readonly source_dir=$1
readonly destination=$2
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir

apply_patch_file() {
  patch \
    --batch \
    --forward \
    --fuzz=0 \
    --no-backup-if-mismatch \
    --reject-file=- \
    -p1 \
    -d "${destination}" \
    < "$1"
}

if [[ ! -d "${source_dir}/.git" ]]; then
  echo "source is not a Git checkout" >&2
  exit 1
fi
if [[ $(git -C "${source_dir}" rev-parse HEAD) != "${expected_ref}" ]]; then
  echo "source is not the pinned Phase 2 commit" >&2
  exit 1
fi
if [[ -e "${destination}" ]]; then
  echo "destination must not already exist" >&2
  exit 1
fi

# Archive the immutable Git object rather than copying the caller's worktree;
# dirty or untracked caller files can never enter the build context.
mkdir -m 0750 -- "${destination}"
git -C "${source_dir}" archive "${expected_ref}" | tar -x -C "${destination}"
apply_patch_file "${script_dir}/public-aio-toolchain.patch"
apply_patch_file "${script_dir}/core-hardening.patch"
apply_patch_file "${script_dir}/compliance-closure.patch"
apply_patch_file "${script_dir}/source-archive-fix.patch"
apply_patch_file "${script_dir}/portable-glibc35-toolchain.patch"
apply_patch_file "${script_dir}/buffered-criu-io.patch"
apply_patch_file "${script_dir}/jammy-compliance-tomli.patch"
apply_patch_file "${script_dir}/ns-bind-mount-glibc35.patch"
cp -a "${script_dir}/overlay/." "${destination}/"

if find "${destination}" -type f \( -name '*.orig' -o -name '*.rej' \) -print -quit \
  | grep -q .; then
  echo "materialized tree contains a patch backup or reject file" >&2
  exit 1
fi

echo "materialized pinned one-shot worker source at ${destination}"
