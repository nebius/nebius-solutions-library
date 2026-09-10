#!/usr/bin/env bash
set -euo pipefail

# A partial/promisor checkout must fail if an object is absent; verification is
# intentionally offline and must never satisfy a read by fetching a blob.
export GIT_NO_LAZY_FETCH=1
export GIT_TERMINAL_PROMPT=0

readonly expected_dynamo_ref="f7f37be174d252590c4b56e25ff4262dd82466fd"
readonly expected_phase2_digest="sha256:c9df66930fbe31c2910752c6601ca4798f422c048f4df6d200df1624357729d9"
readonly expected_phase2_image_id="sha256:22501fa2418190f559777dc15875c0e6590bec78a4c39ae93b9b044987e5888f"
readonly expected_agent_sha="d479604484dde77bb18c0447d134f0668032fef6d7568c975c0585532679bc84"
readonly expected_nsrestore_sha="2e1a1fdd7454c3a34123c133d6c987217477ba0d8df0037309c7cd8c8a447b7a"
readonly expected_criu_sha="920b21bb333e1a4c8266e4724384aa3a53f4de7cd23dbf4aa75032cf6d5964b7"
readonly expected_cuda_sha="707fa7f54136824d6c1d6dd724b9b1717610f831033c00d06da474de363a06db"

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 DYNAMO_SOURCE_DIR [LOCAL_PHASE2_IMAGE]" >&2
  exit 2
fi

readonly source_dir=$1
readonly local_image=${2:-}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly script_dir
readonly patch_file="${script_dir}/public-aio-toolchain.patch"
readonly core_patch_file="${script_dir}/core-hardening.patch"
readonly closure_patch_file="${script_dir}/compliance-closure.patch"
readonly source_archive_patch_file="${script_dir}/source-archive-fix.patch"
readonly portable_toolchain_patch_file="${script_dir}/portable-glibc35-toolchain.patch"
readonly buffered_io_patch_file="${script_dir}/buffered-criu-io.patch"
readonly jammy_compliance_patch_file="${script_dir}/jammy-compliance-tomli.patch"
readonly ns_bind_mount_patch_file="${script_dir}/ns-bind-mount-glibc35.patch"
readonly overlay_dir="${script_dir}/overlay"

apply_checked_patch() {
  local patch_path=$1
  local patch_args=(
    --batch
    --forward
    --fuzz=0
    --no-backup-if-mismatch
    --reject-file=-
    -p1
    -d "${tmp_dir}"
  )

  patch "${patch_args[@]}" --dry-run <"${patch_path}" >/dev/null
  patch "${patch_args[@]}" <"${patch_path}" >/dev/null
}

if [[ ! -d "${source_dir}/.git" ]]; then
  echo "Dynamo source is not a Git checkout: ${source_dir}" >&2
  exit 1
fi

actual_ref=$(git -C "${source_dir}" rev-parse HEAD)
if [[ "${actual_ref}" != "${expected_dynamo_ref}" ]]; then
  echo "Dynamo source mismatch: expected ${expected_dynamo_ref}, got ${actual_ref}" >&2
  exit 1
fi

# Verify the raw-NIM support is present in the immutable source object, not in
# possibly dirty worktree files.
git -C "${source_dir}" show "${expected_dynamo_ref}:deploy/snapshot/internal/nsmount/mount.go" \
  | grep -F 'nsFd, err := os.Open(nsFdPath)' >/dev/null
git -C "${source_dir}" show "${expected_dynamo_ref}:deploy/snapshot/internal/executor/restore.go" \
  | grep -F 'fmt.Sprintf("--mount=/proc/self/fd/%d", nsFdChild)' >/dev/null
git -C "${source_dir}" show "${expected_dynamo_ref}:deploy/snapshot/internal/executor/restore.go" \
  | grep -F 'os.Open(filepath.Join(nsmount.SnapshotBinSrc, "nsrestore"))' >/dev/null
git -C "${source_dir}" show "${expected_dynamo_ref}:deploy/snapshot/internal/executor/nsrestore.go" \
  | grep -F 'failed to open cuda-checkpoint-helper before CRIU restore' >/dev/null
git -C "${source_dir}" show "${expected_dynamo_ref}:deploy/snapshot/Dockerfile" \
  | grep -F 'COPY --from=builder /nsrestore /snapshot-binaries/nsrestore' >/dev/null

# Apply the proposed build-only patch to a disposable archive of the exact Git
# object. This never writes to the caller's checkout.
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "${tmp_dir}"' EXIT
git -C "${source_dir}" archive "${expected_dynamo_ref}" \
  container/compliance deploy/operator deploy/snapshot \
  | tar -x -C "${tmp_dir}"
apply_checked_patch "${patch_file}"
apply_checked_patch "${core_patch_file}"
apply_checked_patch "${closure_patch_file}"
apply_checked_patch "${source_archive_patch_file}"
apply_checked_patch "${portable_toolchain_patch_file}"
apply_checked_patch "${buffered_io_patch_file}"
apply_checked_patch "${jammy_compliance_patch_file}"
apply_checked_patch "${ns_bind_mount_patch_file}"
cp -a "${overlay_dir}/." "${tmp_dir}/"

if find "${tmp_dir}" -type f \( -name '*.orig' -o -name '*.rej' \) -print -quit \
  | grep -q .; then
  echo "verified materialization contains a patch backup or reject file" >&2
  exit 1
fi

grep -F 'ARG CRIU_REF=91d552257809d0e5c7148190e9aa0372f13b76a0' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG GO_BUILD_IMAGE=golang:1.26.6@sha256:0d1d3a794be25f809dd2cb3160d8c73276c4056a9f8242a138e908ddeee7b6b6' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG CUDA_BUILD_IMAGE=nvidia/cuda:13.0.3-devel-ubuntu24.04@sha256:7d56ebe2b7cd864a60dca3c8b2d0a39f8fc110417e8253e32505c3387f59119c' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG CRIU_BUILD_IMAGE=ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG AGENT_BASE_IMAGE=nvidia/cuda:13.0.3-base-ubuntu22.04@sha256:73ab6dfb3814a5097cd456736e70650ef9dc72343be4117d0400de78168760fe' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG CUDA_CHECKPOINT_HELPER_SHA256=0e44e9067d71411c775cd3b21fa0df806613504c8c699bd1944e3e7c10989298' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
# Dockerfile variables must remain literal in both search expressions.
# shellcheck disable=SC2016
grep -F 'FROM ${CUDA_BUILD_IMAGE} AS cuda-helper-builder' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
# shellcheck disable=SC2016
grep -F 'FROM ${CRIU_BUILD_IMAGE} AS criu-builder' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
# Dockerfile variables must remain literal in the search expression.
# shellcheck disable=SC2016
grep -F 'FROM ${AGENT_BASE_IMAGE} AS agent_pre_unverified' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'FROM criu-builder AS bundle-glibc35-audit' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'FROM criu-builder AS ns-bind-mount-builder' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'COPY --from=ns-bind-mount-builder /ns-bind-mount /usr/local/sbin/ns-bind-mount' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'FROM agent_pre_unverified AS agent_pre' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F '/snapshot-binaries 2.35 /snapshot-binaries.glibc-compatibility' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
if grep -F 'RUN go get' "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null; then
  echo "dependency refresh still mutates go.mod/go.sum during the image build" >&2
  exit 1
fi
grep -F 'cyclonedx-gomod app -licenses -json -main ./cmd/restore-worker -output /sbom-restore-worker-go.cdx.json .' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'COPY --from=builder /sbom-restore-worker-go.cdx.json /tmp/sbom-restore-worker-go.cdx.json' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F -- '--go-sbom /tmp/sbom-restore-worker-go.cdx.json' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'ARG CUDA_CHECKPOINT_REF=00d5cce84c628088d6caa203fc4af40c1538b6f7' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'FROM agent_pre AS task-public-agent' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
if grep -F 'RUN git clone https://github.com/NVIDIA/cuda-checkpoint.git' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null; then
  echo "cuda-checkpoint remains unpinned after patch" >&2
  exit 1
fi

test -f "${tmp_dir}/deploy/snapshot/cmd/restore-worker/worker.go"
test -f "${tmp_dir}/deploy/snapshot/internal/executor/pinned_namespaces.go"
test -f "${tmp_dir}/deploy/snapshot/internal/executor/checkpoint_overlay_test.go"
test -x "${tmp_dir}/deploy/snapshot/scripts/verify-bundle-glibc.sh"
grep -F 'targetTar := filepath.Join(targetRoot, "bin", "tar")' \
  "${tmp_dir}/deploy/snapshot/internal/runtime/overlay.go" >/dev/null
grep -F 'cmd.Env = environmentWithout(os.Environ(), "LD_LIBRARY_PATH")' \
  "${tmp_dir}/deploy/snapshot/internal/runtime/overlay.go" >/dev/null
grep -F 'case "buffered":' \
  "${tmp_dir}/deploy/snapshot/internal/criu/util.go" >/dev/null
grep -F 'return nil, nil' \
  "${tmp_dir}/deploy/snapshot/internal/criu/util.go" >/dev/null
grep -F '"writeback", "direct", "buffered"' \
  "${tmp_dir}/deploy/snapshot/internal/types/config.go" >/dev/null
grep -F 'python3 python3-yaml python3-tomli' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'RequireCleanUnmount bool' \
  "${tmp_dir}/deploy/snapshot/internal/executor/restore.go" >/dev/null
grep -F 'openPinnedRestoreNamespaces(snapshotruntime.HostProcPath' \
  "${tmp_dir}/deploy/snapshot/internal/executor/restore.go" >/dev/null
grep -F 'return fmt.Errorf("rootfs diff capture failed: %w", err)' \
  "${tmp_dir}/deploy/snapshot/internal/executor/checkpoint.go" >/dev/null
grep -F 'return fmt.Errorf("deleted-file capture failed: %w", err)' \
  "${tmp_dir}/deploy/snapshot/internal/executor/checkpoint.go" >/dev/null
grep -F '"target-pod-spec-sha256"' \
  "${tmp_dir}/deploy/snapshot/cmd/restore-worker/options.go" >/dev/null
# Dockerfile variables must remain literal in the search expression.
# shellcheck disable=SC2016
grep -F 'RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -ldflags="-w -s" -o /restore-worker ./cmd/restore-worker' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'xargs -0 sha256sum > /snapshot-binaries.manifest' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.revision="f7f37be174d252590c4b56e25ff4262dd82466fd"' \
  "${tmp_dir}/deploy/snapshot/Dockerfile" >/dev/null
grep -F 'SPDX-License-Identifier: Apache-2.0' \
  "${tmp_dir}/deploy/snapshot/cmd/cuda-checkpoint-helper/main.c" >/dev/null
grep -F -- '  - name: ns-bind-mount' \
  "${tmp_dir}/container/compliance/native_packages.yaml" >/dev/null
grep -F -- '  - name: snapshot-inet-remap' \
  "${tmp_dir}/container/compliance/native_packages.yaml" >/dev/null
grep -F '"ns-bind-mount",' \
  "${tmp_dir}/container/compliance/collect_sources.py" >/dev/null
grep -F '"snapshot-inet-remap",' \
  "${tmp_dir}/container/compliance/collect_sources.py" >/dev/null
grep -F 'shutil.copytree(item, dest, symlinks=True)' \
  "${tmp_dir}/container/compliance/collect_sources.py" >/dev/null
test "$(sha256sum "${tmp_dir}/deploy/snapshot/go.mod" | cut -d' ' -f1)" = \
  '7e8580094f486fe9e0f5dc37b9cd8ecf1ed153cdd1a6a2293cc53bb3530cebc1'
test "$(sha256sum "${tmp_dir}/deploy/snapshot/go.sum" | cut -d' ' -f1)" = \
  'cc3ffd4efe77d70fc65b5c47689931a18080d65619b093250ea3011e20675822'

test "$(sha256sum "${core_patch_file}" | cut -d' ' -f1)" = \
  '260c1d9a7f192b8c0b25c924ab26b43a95ad599d38d3f367383e3e984aecfd11'
test "$(sha256sum "${patch_file}" | cut -d' ' -f1)" = \
  'de10a0609115589efb137186544c9d90a0fcc03616ef959376139c65ae9615bc'
test "$(sha256sum "${closure_patch_file}" | cut -d' ' -f1)" = \
  '32493e5da929a993976148124699f8abf99240c4ba486e6a01fda453c682ae68'
test "$(sha256sum "${source_archive_patch_file}" | cut -d' ' -f1)" = \
  '5dd45d97596bbdf068f33f0532fc71da1754c9eeb7d91f0abe547ac29f30bf0e'
test "$(sha256sum "${portable_toolchain_patch_file}" | cut -d' ' -f1)" = \
  '62d584ea83770c62d090bbbc15f265a6fadda064ec8c6e6a4fd0abe8328780b9'
test "$(sha256sum "${buffered_io_patch_file}" | cut -d' ' -f1)" = \
  '6a59dcb524abd0d9596697efd28afb3bd9d4e543b193f687178494452cecac15'
test "$(sha256sum "${jammy_compliance_patch_file}" | cut -d' ' -f1)" = \
  '6fa58bcdf97c54f8ecd75e2a685150bd1ae5cede71283eb8fc9aa88bacf87156'
test "$(sha256sum "${ns_bind_mount_patch_file}" | cut -d' ' -f1)" = \
  '4847d7d42aae570fc7f91351a8fbf3018f10dc6247d93c2c9696754861731366'
test "$(sha256sum "${script_dir}/Dockerfile.restore-worker" | cut -d' ' -f1)" = \
  'f0fb42c68ad7bf8dd39e27a5e070a1613953e7ec2cac0f19027cbea63a509570'
test "$(sha256sum "${overlay_dir}/deploy/snapshot/scripts/verify-bundle-glibc.sh" | cut -d' ' -f1)" = \
  '7849fea3931032dfa35c9d9cc0ecc577d94185160910ef37dc5b2573a08cc406'
overlay_tree_sha=$(
  find "${overlay_dir}" -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    | sed "s#${script_dir}/##" \
    | sha256sum \
    | cut -d' ' -f1
)
if [[ "${overlay_tree_sha}" != '5363391ab0faed88612308fa1d7a1a9664d464fc9ba440973e89e8bc4ca2a0ed' ]]; then
  echo "one-shot worker overlay tree hash mismatch: ${overlay_tree_sha}" >&2
  exit 1
fi

bash -n "${tmp_dir}/deploy/snapshot/scripts/verify-bundle-glibc.sh"
"${tmp_dir}/deploy/snapshot/scripts/verify-bundle-glibc.sh" --self-test >/dev/null

python3 -m json.tool "${script_dir}/provenance.json" >/dev/null

if [[ -n "${local_image}" ]]; then
  image_id=$(docker image inspect "${local_image}" --format '{{.Id}}')
  if [[ "${image_id}" != "${expected_phase2_image_id}" ]]; then
    echo "local Phase 2 image ID mismatch: expected ${expected_phase2_image_id}, got ${image_id}" >&2
    exit 1
  fi
  repo_digests=$(docker image inspect "${local_image}" --format '{{join .RepoDigests "\n"}}')
  if ! grep -F "@${expected_phase2_digest}" <<<"${repo_digests}" >/dev/null; then
    echo "local Phase 2 image lacks expected immutable digest ${expected_phase2_digest}" >&2
    exit 1
  fi
  image_revision=$(docker image inspect "${local_image}" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
  if [[ "${image_revision}" != "${expected_dynamo_ref}" ]]; then
    echo "local Phase 2 image revision mismatch: expected ${expected_dynamo_ref}, got ${image_revision}" >&2
    exit 1
  fi
  docker run --rm --network none --entrypoint /bin/sh "${local_image}" -c \
    "test \"\$(sha256sum /usr/local/bin/snapshot-agent | cut -d' ' -f1)\" = '${expected_agent_sha}'
     test \"\$(sha256sum /snapshot-binaries/nsrestore | cut -d' ' -f1)\" = '${expected_nsrestore_sha}'
     test \"\$(sha256sum /snapshot-binaries/criu | cut -d' ' -f1)\" = '${expected_criu_sha}'
     test \"\$(sha256sum /snapshot-binaries/cuda-checkpoint | cut -d' ' -f1)\" = '${expected_cuda_sha}'
     test \"\$(criu --version | sed -n 's/^GitID: //p')\" = 'b47c692'"
fi

echo "phase2-agent offline source, patch, and provenance verification passed"
