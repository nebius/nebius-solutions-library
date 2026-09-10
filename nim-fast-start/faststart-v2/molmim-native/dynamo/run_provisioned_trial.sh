#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly expected_validator_sha256="0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7"
readonly expected_fixture_sha256="053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d"
readonly trial_namespace="nim-fast-start"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly contract_path="${script_dir}/restore-interface.live.json"
readonly validator_path="${script_dir}/../validate_molmim.py"
readonly fixture_path="${script_dir}/../fixtures/request-cmaes-qed.json"

trial_run_id=""
trial_evidence_root=""
trial_node=""
trial_kubeconfig=""
trial_holder=""
trial_checkpoint_id=""
trial_target_glibc_version=""
trial_image_io_mode=""
trial_artifact_manifest_sha256=""
trial_cleanup=0
trial_allow_performance_validation=0

usage() {
  cat >&2 <<'USAGE'
usage: run_provisioned_trial.sh \
  --run-id RUN_ID \
  --evidence-root ABSOLUTE_DIRECTORY \
  --node ALLOWED_H100_HOSTNAME \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD \
  --checkpoint-id molmim-native-f7-v1|molmim-native-f7-v2-buffered \
  --target-glibc-version MAJOR.MINOR \
  --image-io-mode direct|buffered \
  --artifact-manifest-sha256 CAPTURED_64_HEX_SHA256 \
  [--allow-performance-validation-worker] [--cleanup]
USAGE
}

die_usage() {
  printf '%s\n' "$1" >&2
  usage
  exit 64
}

set_once() {
  local option_name=$1
  local current_value=$2
  local new_value=$3
  if [[ -n $current_value ]]; then
    die_usage "${option_name} may be supplied only once"
  fi
  if [[ -z $new_value ]]; then
    die_usage "${option_name} requires a nonempty value"
  fi
}

while (($# > 0)); do
  case "$1" in
    --run-id)
      (($# >= 2)) || die_usage "--run-id requires a value"
      set_once "--run-id" "$trial_run_id" "$2"
      trial_run_id=$2
      shift 2
      ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once "--evidence-root" "$trial_evidence_root" "$2"
      trial_evidence_root=$2
      shift 2
      ;;
    --node)
      (($# >= 2)) || die_usage "--node requires a value"
      set_once "--node" "$trial_node" "$2"
      trial_node=$2
      shift 2
      ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once "--kubeconfig" "$trial_kubeconfig" "$2"
      trial_kubeconfig=$2
      shift 2
      ;;
    --artifact-holder)
      (($# >= 2)) || die_usage "--artifact-holder requires a value"
      set_once "--artifact-holder" "$trial_holder" "$2"
      trial_holder=$2
      shift 2
      ;;
    --checkpoint-id)
      (($# >= 2)) || die_usage "--checkpoint-id requires a value"
      set_once "--checkpoint-id" "$trial_checkpoint_id" "$2"
      trial_checkpoint_id=$2
      shift 2
      ;;
    --target-glibc-version)
      (($# >= 2)) || die_usage "--target-glibc-version requires a value"
      set_once "--target-glibc-version" "$trial_target_glibc_version" "$2"
      trial_target_glibc_version=$2
      shift 2
      ;;
    --image-io-mode)
      (($# >= 2)) || die_usage "--image-io-mode requires a value"
      set_once "--image-io-mode" "$trial_image_io_mode" "$2"
      trial_image_io_mode=$2
      shift 2
      ;;
    --artifact-manifest-sha256)
      (($# >= 2)) || die_usage "--artifact-manifest-sha256 requires a value"
      set_once "--artifact-manifest-sha256" "$trial_artifact_manifest_sha256" "$2"
      trial_artifact_manifest_sha256=$2
      shift 2
      ;;
    --cleanup)
      ((trial_cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      trial_cleanup=1
      shift
      ;;
    --allow-performance-validation-worker)
      ((trial_allow_performance_validation == 0)) || \
        die_usage "--allow-performance-validation-worker may be supplied only once"
      trial_allow_performance_validation=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done

[[ -n $trial_run_id ]] || die_usage "--run-id is required"
[[ -n $trial_evidence_root ]] || die_usage "--evidence-root is required"
[[ -n $trial_node ]] || die_usage "--node is required"
[[ -n $trial_kubeconfig ]] || die_usage "--kubeconfig is required"
[[ -n $trial_holder ]] || die_usage "--artifact-holder is required"
[[ -n $trial_checkpoint_id ]] || die_usage "--checkpoint-id is required"
[[ -n $trial_target_glibc_version ]] || die_usage "--target-glibc-version is required"
[[ -n $trial_image_io_mode ]] || die_usage "--image-io-mode is required"
[[ -n $trial_artifact_manifest_sha256 ]] || die_usage "--artifact-manifest-sha256 is required"

if [[ ! $trial_run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#trial_run_id} -gt 30 ]]; then
  die_usage "--run-id must be a DNS label of at most 30 characters"
fi
if [[ ! $trial_holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#trial_holder} -gt 63 ]]; then
  die_usage "--artifact-holder must be a DNS label of at most 63 characters"
fi
case "${trial_image_io_mode}:${trial_checkpoint_id}:${trial_holder}" in
  direct:molmim-native-f7-v1:molmim-native-f7-holder-t12)
    ;;
  buffered:molmim-native-f7-v2-buffered:molmim-native-f7-buffered-holder-t12)
    ;;
  *)
    die_usage "image I/O mode, checkpoint, and artifact holder are not an exact prepared tuple"
    ;;
esac
if [[ ! $trial_target_glibc_version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  die_usage "--target-glibc-version must be canonical MAJOR.MINOR"
fi
trial_glibc_major=${trial_target_glibc_version%%.*}
trial_glibc_minor=${trial_target_glibc_version#*.}
if ((trial_glibc_major < 2 || (trial_glibc_major == 2 && trial_glibc_minor < 35))); then
  die_usage "--target-glibc-version is older than the worker tool bundle requirement 2.35"
fi
case "$trial_node" in
  gpu-node-b.example.invalid)
    ;;
  *)
    die_usage "--node is not an allowlisted H100 hostname"
    ;;
esac
if [[ ! $trial_artifact_manifest_sha256 =~ ^[0-9a-f]{64}$ ]]; then
  die_usage "--artifact-manifest-sha256 must be 64 lowercase hexadecimal characters"
fi
[[ $trial_evidence_root == /* ]] || die_usage "--evidence-root must be absolute"
[[ -d $trial_evidence_root && ! -L $trial_evidence_root ]] || die_usage "--evidence-root must be an existing non-symlink directory"
[[ -d $trial_evidence_root/runs && ! -L $trial_evidence_root/runs ]] || die_usage "--evidence-root/runs must be an existing non-symlink directory"
[[ $trial_kubeconfig == /* ]] || die_usage "--kubeconfig must be absolute"
[[ -f $trial_kubeconfig && ! -L $trial_kubeconfig ]] || die_usage "--kubeconfig must be a regular non-symlink file"

for required_command in kubectl jq python3 sha256sum date install tail sleep; do
  command -v "$required_command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$required_command" >&2
    exit 69
  }
done

actual_validator_sha256=$(sha256sum "$validator_path")
actual_validator_sha256=${actual_validator_sha256%% *}
if [[ $actual_validator_sha256 != "$expected_validator_sha256" ]]; then
  printf 'semantic validator digest mismatch\n' >&2
  exit 78
fi
actual_fixture_sha256=$(sha256sum "$fixture_path")
actual_fixture_sha256=${actual_fixture_sha256%% *}
if [[ $actual_fixture_sha256 != "$expected_fixture_sha256" ]]; then
  printf 'semantic fixture digest mismatch\n' >&2
  exit 78
fi
if ((trial_allow_performance_validation == 1)); then
  contract_gate='.approved == true and .release_ready == false and (.release_blocker | type == "string" and length > 0) and .worker_classification == "performance-validation-only"'
else
  contract_gate='.approved == true and .release_ready == true and .release_blocker == "" and .worker_classification == "full-agent-compliance-release"'
fi
jq -e --arg validator "$expected_validator_sha256" --arg mode "$trial_image_io_mode" "
  ${contract_gate} and
  .validator_sha256 == \$validator and
  (.supported_image_io_modes | index(\$mode)) != null and
  .tool_bundle.maximum_required_glibc == \"2.35\" and
  (.worker_image | test(\"@sha256:[0-9a-f]{64}$\")) and
  (.probe_image | test(\"@sha256:[0-9a-f]{64}$\"))
" "$contract_path" >/dev/null || {
  printf 'immutable restore contract is not deployable\n' >&2
  exit 78
}

readonly trial_dir="${trial_evidence_root}/runs/${trial_run_id}"
readonly trial_target="molmim-target-${trial_run_id}"
readonly trial_worker="molmim-restore-${trial_run_id}"
readonly trial_probe="molmim-semantic-${trial_run_id}"
readonly trial_canary="molmim-canary-${trial_run_id}"

if [[ -e $trial_dir || -L $trial_dir ]]; then
  printf 'run directory already exists: %s\n' "$trial_dir" >&2
  exit 73
fi

trial_server=$(kubectl --kubeconfig "$trial_kubeconfig" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
if [[ $trial_server != "$allowed_server" ]]; then
  printf 'kubeconfig is not bound to the allowed cluster\n' >&2
  exit 78
fi

trial_kubectl=(kubectl --kubeconfig "$trial_kubeconfig" -n "$trial_namespace")
cache_holder_json=$("${trial_kubectl[@]}" get pod \
  molmim-native-f7-cache-holder-t12 -o json)
if ! jq -e --arg node "$trial_node" '
  .metadata.name == "molmim-native-f7-cache-holder-t12" and
  .spec.nodeName == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.containerStatuses // []) | length == 1) and
  all(.status.containerStatuses[]?; .ready == true) and
  any(.spec.volumes[]?;
    .name == "cache" and
    .persistentVolumeClaim.claimName == "molmim-native-f7-cache" and
    .persistentVolumeClaim.readOnly == true)
' <<<"$cache_holder_json" >/dev/null; then
  printf 'pinned MolMIM cache holder is not Ready and fully prewarmed on the requested node\n' >&2
  exit 69
fi
image_holder_json=$("${trial_kubectl[@]}" get pod \
  molmim-native-f7-image-holder-t12 -o json)
if ! jq -e --arg node "$trial_node" \
  --arg image 'nvcr.io/nim/nvidia/molmim@sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa' '
  .metadata.name == "molmim-native-f7-image-holder-t12" and
  .spec.nodeName == $node and
  (.spec.containers | length) == 1 and
  .spec.containers[0].name == "holder" and
  .spec.containers[0].image == $image and
  .spec.containers[0].imagePullPolicy == "IfNotPresent" and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.containerStatuses // []) | length == 1) and
  .status.containerStatuses[0].name == "holder" and
  .status.containerStatuses[0].ready == true
' <<<"$image_holder_json" >/dev/null; then
  printf 'exact MolMIM image holder is not Ready on the requested node\n' >&2
  exit 69
fi
holder_json=$("${trial_kubectl[@]}" get pod "$trial_holder" -o json)
if ! jq -e --arg node "$trial_node" '
  .spec.nodeName == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.containerStatuses // []) | length > 0) and
  all(.status.containerStatuses[]?; .ready == true) and
  any(.spec.volumes[]?;
    .name == "artifacts" and
    .persistentVolumeClaim.claimName == "molmim-native-f7-artifacts" and
    .persistentVolumeClaim.readOnly == true) and
  any(.spec.volumes[]?;
    .name == "nim-cache" and
    .persistentVolumeClaim.claimName == "molmim-native-f7-cache" and
    .persistentVolumeClaim.readOnly == true)
' <<<"$holder_json" >/dev/null; then
  printf 'artifact holder is not Ready with both pinned PVCs on the requested node\n' >&2
  exit 69
fi

mkdir -m 0700 -- "$trial_dir" || {
  printf 'could not create new run directory: %s\n' "$trial_dir" >&2
  exit 73
}
printf '%s\n' "$cache_holder_json" > "$trial_dir/cache-holder-pod.json"
printf '%s\n' "$image_holder_json" > "$trial_dir/image-holder-pod.json"
printf '%s\n' "$holder_json" > "$trial_dir/artifact-holder-pod.json"
"${trial_kubectl[@]}" logs molmim-native-f7-cache-holder-t12 | tail -1 \
  > "$trial_dir/cache-holder-receipt.json"
"${trial_kubectl[@]}" logs "$trial_holder" | tail -1 \
  > "$trial_dir/artifact-holder-receipt.json"
jq -e '
  .schema == "archvteams.nebius.ai/molmim-cache-holder-receipt/v1" and
  .status == "PASS" and .mode == "cache-full-read" and
  .unique_bytes == 284497920 and .prewarm_bytes == 284497920 and
  .tree_sha256 == "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c" and
  (.full_read_elapsed_seconds | type == "number" and isfinite and . > 0)
' "$trial_dir/cache-holder-receipt.json" >/dev/null || {
  printf 'cache holder log did not prove a full read before T0\n' >&2
  exit 69
}
jq -e --arg checkpoint "$trial_checkpoint_id" --arg mode "$trial_image_io_mode" \
  --arg manifest "$trial_artifact_manifest_sha256" '
  .schema == "archvteams.nebius.ai/molmim-native-artifact-receipt/v1" and
  .status == "PASS" and .checkpoint_id == $checkpoint and
  .artifact_version == "1" and .image_io_mode == $mode and
  .manifest_sha256 == $manifest and
  (.regular_file_bytes | type == "number" and . > 1000000000) and
  .unique_bytes == .regular_file_bytes and
  .prewarm_bytes == .regular_file_bytes and
  (.tree_sha256 | test("^[0-9a-f]{64}$")) and
  (.full_read_elapsed_seconds | type == "number" and isfinite and . > 0)
' "$trial_dir/artifact-holder-receipt.json" >/dev/null || {
  printf 'artifact holder log did not prove the selected full-read mode before T0\n' >&2
  exit 69
}
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/prewarm-captured-at.txt"
install -m 0600 -- "$contract_path" "$trial_dir/restore-interface.json"
(
  cd -- "$trial_dir"
  sha256sum restore-interface.json > restore-interface.sha256
)

trial_demand=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -n \
  --arg run_id "$trial_run_id" \
  --arg demand_at "$trial_demand" \
  --arg node "$trial_node" \
  --arg checkpoint_id "$trial_checkpoint_id" \
  --arg target_glibc_version "$trial_target_glibc_version" \
  --arg image_io_mode "$trial_image_io_mode" \
  --arg manifest "$trial_artifact_manifest_sha256" \
  '{
    schema:"archvteams.nebius.ai/molmim-faststart-run/v1",
    demand_at:$demand_at,
    run_id:$run_id,
    target_node:$node,
    target_glibc_version:$target_glibc_version,
    image_io_mode:$image_io_mode,
    checkpoint_id:$checkpoint_id,
    artifact_version:"1",
    artifact_manifest_sha256:$manifest,
    artifact_pvc:"molmim-native-f7-artifacts",
    cache_pvc:"molmim-native-f7-cache"
  }' > "$trial_dir/run.json"

python3 "$script_dir/render.py" target \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" > "$trial_dir/target.yaml"
python3 "$script_dir/lint_manifest.py" "$trial_dir/target.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/target-submit-at.txt"
"${trial_kubectl[@]}" create -f "$trial_dir/target.yaml"
"${trial_kubectl[@]}" wait \
  --for=jsonpath='{.status.containerStatuses[0].state.running}' \
  "pod/$trial_target" --timeout=300s
"${trial_kubectl[@]}" get pod "$trial_target" -o json \
  > "$trial_dir/target-before-binding.json"

python3 "$script_dir/bind_target.py" \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" \
  --pod-json "$trial_dir/target-before-binding.json" \
  --collected-at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  --binding-output "$trial_dir/binding.json" \
  --patch-output "$trial_dir/target-pod-spec.patch.json"
"${trial_kubectl[@]}" patch pod "$trial_target" \
  --type=json --patch-file="$trial_dir/target-pod-spec.patch.json" -o json \
  > "$trial_dir/target-patch-response.json"
"${trial_kubectl[@]}" get pod "$trial_target" -o json \
  > "$trial_dir/target-bound.json"

# Submit the CPU probe immediately after binding. It polls readiness itself,
# overlapping client scheduling with the native restore.
python3 "$script_dir/render.py" probe \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" \
  --binding "$trial_dir/binding.json" > "$trial_dir/semantic-probe.yaml"
python3 "$script_dir/lint_manifest.py" "$trial_dir/semantic-probe.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/probe-submit-at.txt"
"${trial_kubectl[@]}" create -f "$trial_dir/semantic-probe.yaml"

python3 "$script_dir/render.py" restore \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" \
  --binding "$trial_dir/binding.json" > "$trial_dir/restore-worker.yaml"
python3 "$script_dir/lint_manifest.py" "$trial_dir/restore-worker.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/worker-submit-at.txt"
"${trial_kubectl[@]}" create -f "$trial_dir/restore-worker.yaml"

wait_for_job() {
  local wait_name=$1
  local wait_output=$2
  local wait_attempt
  for ((wait_attempt=0; wait_attempt<900; wait_attempt++)); do
    "${trial_kubectl[@]}" get job "$wait_name" -o json > "$wait_output"
    if jq -e '(.status.succeeded // 0) == 1' "$wait_output" >/dev/null; then
      return 0
    fi
    if jq -e '(.status.failed // 0) > 0' "$wait_output" >/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 124
}

if ! wait_for_job "$trial_worker" "$trial_dir/worker-job.json"; then
  "${trial_kubectl[@]}" get pods -l "job-name=$trial_worker" -o json \
    > "$trial_dir/worker-pods.failed.json" || true
  exit 1
fi
"${trial_kubectl[@]}" get pods -l "job-name=$trial_worker" -o json \
  > "$trial_dir/worker-pods.json"
trial_worker_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' \
  "$trial_dir/worker-pods.json")
"${trial_kubectl[@]}" get pod "$trial_worker_pod" -o json \
  > "$trial_dir/worker-pod.json"
"${trial_kubectl[@]}" logs "$trial_worker_pod" > "$trial_dir/worker.log"
tail -1 "$trial_dir/worker.log" | jq -e -c 'select(.status=="succeeded")' \
  > "$trial_dir/worker-receipt.json"

"${trial_kubectl[@]}" wait --for=condition=Ready "pod/$trial_target" --timeout=300s
"${trial_kubectl[@]}" get pod "$trial_target" -o json \
  > "$trial_dir/target-ready.json"

if ! wait_for_job "$trial_probe" "$trial_dir/probe-job.json"; then
  "${trial_kubectl[@]}" get pods -l "job-name=$trial_probe" -o json \
    > "$trial_dir/probe-pods.failed.json" || true
  exit 1
fi
"${trial_kubectl[@]}" get pods -l "job-name=$trial_probe" -o json \
  > "$trial_dir/probe-pods.json"
trial_probe_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' \
  "$trial_dir/probe-pods.json")
"${trial_kubectl[@]}" get pod "$trial_probe_pod" -o json \
  > "$trial_dir/probe-pod.json"
"${trial_kubectl[@]}" logs "$trial_probe_pod" > "$trial_dir/semantic-probe.log"
tail -1 "$trial_dir/semantic-probe.log" | jq -e -c \
  'select(.status=="PASS" and .passed_case_count==2)' \
  > "$trial_dir/semantic-summary.json"

"${trial_kubectl[@]}" get pod "$trial_target" -o json \
  > "$trial_dir/target-final.json"
"${trial_kubectl[@]}" get service "$trial_canary" -o json \
  > "$trial_dir/canary-service.json"
"${trial_kubectl[@]}" get endpointslices.discovery.k8s.io \
  -l "kubernetes.io/service-name=$trial_canary" -o json \
  > "$trial_dir/canary-endpointslices.json"

python3 "$script_dir/evidence.py" \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" \
  --binding "$trial_dir/binding.json" \
  --target-pod "$trial_dir/target-final.json" \
  --service "$trial_dir/canary-service.json" \
  --endpoint-slices "$trial_dir/canary-endpointslices.json" \
  --worker-job "$trial_dir/worker-job.json" \
  --worker-pod "$trial_dir/worker-pod.json" \
  --worker-receipt "$trial_dir/worker-receipt.json" \
  --probe-job "$trial_dir/probe-job.json" \
  --probe-pod "$trial_dir/probe-pod.json" \
  --semantic-summary "$trial_dir/semantic-summary.json" \
  --target-submit-at "$trial_dir/target-submit-at.txt" \
  --cache-holder-pod "$trial_dir/cache-holder-pod.json" \
  --cache-holder-receipt "$trial_dir/cache-holder-receipt.json" \
  --artifact-holder-pod "$trial_dir/artifact-holder-pod.json" \
  --artifact-holder-receipt "$trial_dir/artifact-holder-receipt.json" \
  --prewarm-captured-at "$trial_dir/prewarm-captured-at.txt" \
  > "$trial_dir/canary-evidence.json"

jq -e 'select(.status=="PASS" and .request_count==2 and .semantic_pass_count==2)' \
  "$trial_dir/canary-evidence.json" >/dev/null

if ((trial_cleanup == 1)); then
  {
    "${trial_kubectl[@]}" delete -f "$trial_dir/semantic-probe.yaml" \
      --ignore-not-found --wait=true --timeout=120s
    "${trial_kubectl[@]}" delete -f "$trial_dir/restore-worker.yaml" \
      --ignore-not-found --wait=true --timeout=120s
    "${trial_kubectl[@]}" delete -f "$trial_dir/target.yaml" \
      --ignore-not-found --wait=true --timeout=180s
  } >> "$trial_dir/cleanup.log" 2>&1
  date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/cleanup-verified-at.txt"
fi

jq '{run_id,status,timings_seconds}' "$trial_dir/canary-evidence.json"
