#!/usr/bin/env bash
set -euo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly allowed_node="${TARGET_NODE:-gpu-node-b.example.invalid}"
readonly expected_validator_sha256="0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7"
readonly expected_fixture_sha256="053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d"
readonly expected_image="nvcr.io/nim/nvidia/molmim@sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
readonly namespace="nim-fast-start"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
lane_dir=$(dirname -- "$script_dir")
readonly lane_dir

run_id=""
evidence_root=""
node=""
kubeconfig=""
cleanup=0

usage() {
  cat >&2 <<'USAGE'
usage: run_cached_trial.sh \
  --run-id DNS_LABEL \
  --evidence-root ABSOLUTE_DIRECTORY \
  --node ALLOWED_H100_HOSTNAME \
  --kubeconfig ABSOLUTE_FILE [--cleanup]
USAGE
}

die_usage() {
  printf '%s\n' "$1" >&2
  usage
  exit 64
}

set_once() {
  local name=$1 current=$2 value=$3
  [[ -z $current ]] || die_usage "${name} may be supplied only once"
  [[ -n $value ]] || die_usage "${name} requires a nonempty value"
}

while (($# > 0)); do
  case "$1" in
    --run-id)
      (($# >= 2)) || die_usage "--run-id requires a value"
      set_once "$1" "$run_id" "$2"; run_id=$2; shift 2 ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once "$1" "$evidence_root" "$2"; evidence_root=$2; shift 2 ;;
    --node)
      (($# >= 2)) || die_usage "--node requires a value"
      set_once "$1" "$node" "$2"; node=$2; shift 2 ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once "$1" "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
    --cleanup)
      ((cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      cleanup=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      die_usage "unknown argument: $1" ;;
  esac
done

for required in run_id evidence_root node kubeconfig; do
  [[ -n ${!required} ]] || die_usage "--${required//_/-} is required"
done
if [[ ! $run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#run_id} -gt 28 ]]; then
  die_usage "--run-id must be a DNS label of at most 28 characters"
fi
[[ $node == "$allowed_node" ]] || die_usage "--node is not the configured qualification H100"
[[ $evidence_root == /* && -d $evidence_root/runs && ! -L $evidence_root/runs ]] || \
  die_usage "--evidence-root/runs must be an existing absolute non-symlink directory"
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || \
  die_usage "--kubeconfig must be an absolute regular non-symlink file"

for command in kubectl jq python3 sha256sum date mkdir install tail sleep; do
  command -v "$command" >/dev/null || { printf 'required command unavailable: %s\n' "$command" >&2; exit 69; }
done

actual=$(sha256sum "$lane_dir/validate_molmim.py"); actual=${actual%% *}
[[ $actual == "$expected_validator_sha256" ]] || { printf 'validator digest mismatch\n' >&2; exit 78; }
actual=$(sha256sum "$lane_dir/fixtures/request-cmaes-qed.json"); actual=${actual%% *}
[[ $actual == "$expected_fixture_sha256" ]] || { printf 'fixture digest mismatch\n' >&2; exit 78; }

server=$(kubectl --kubeconfig "$kubeconfig" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
[[ $server == "$allowed_server" ]] || { printf 'kubeconfig is not bound to the allowed cluster\n' >&2; exit 78; }

kc=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")
cache_holder=$("${kc[@]}" get pod molmim-native-f7-cache-holder-t12 -o json)
jq -e --arg node "$node" '
  .metadata.name == "molmim-native-f7-cache-holder-t12" and
  .spec.nodeName == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  all(.status.containerStatuses[]?; .ready == true) and
  any(.spec.volumes[]?; .name == "cache" and
    .persistentVolumeClaim.claimName == "molmim-native-f7-cache" and
    .persistentVolumeClaim.readOnly == true)
' <<<"$cache_holder" >/dev/null || {
  printf 'fully prewarmed MolMIM cache holder is not Ready\n' >&2
  exit 69
}
image_holder=$("${kc[@]}" get pod molmim-native-f7-image-holder-t12 -o json)
jq -e --arg node "$node" --arg image "$expected_image" '
  .metadata.name == "molmim-native-f7-image-holder-t12" and
  .spec.nodeName == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  (.status.containerStatuses | length) == 1 and
  .status.containerStatuses[0].ready == true and
  (.status.containerStatuses[0].imageID | sub("^docker-pullable://"; "")) == $image
' <<<"$image_holder" >/dev/null || {
  printf 'exact MolMIM image cache holder is not Ready\n' >&2
  exit 69
}

readonly run_dir="${evidence_root}/runs/${run_id}"
[[ ! -e $run_dir && ! -L $run_dir ]] || { printf 'run directory already exists\n' >&2; exit 73; }
mkdir -m 0700 -- "$run_dir"

printf '%s\n' "$cache_holder" > "$run_dir/prewarm-holder-pod.json"
printf '%s\n' "$image_holder" > "$run_dir/image-holder-pod.json"
"${kc[@]}" logs molmim-native-f7-cache-holder-t12 | tail -1 \
  > "$run_dir/prewarm-holder-receipt.json"
jq -e '
  .schema == "archvteams.nebius.ai/molmim-cache-holder-receipt/v1" and
  .status == "PASS" and .mode == "cache-full-read" and
  .regular_file_count == 2 and .regular_file_bytes == 284497920 and
  .unique_bytes == 284497920 and .prewarm_bytes == 284497920 and
  .tree_sha256 == "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c" and
  (.full_read_elapsed_seconds | type == "number" and isfinite and . > 0)
' "$run_dir/prewarm-holder-receipt.json" >/dev/null || {
  printf 'cache holder log did not prove a full read before T0\n' >&2
  exit 69
}
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/prewarm-captured-at.txt"

demand_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -n \
  --arg run_id "$run_id" \
  --arg demand_at "$demand_at" \
  --arg node "$node" \
  --arg image "$expected_image" \
  '{schema:"archvteams.nebius.ai/molmim-conventional-run/v1",run_id:$run_id,
    demand_at:$demand_at,node:$node,image:$image,mode:"conventional-cached"}' \
  > "$run_dir/run.json"

python3 "$script_dir/render.py" target --run-id "$run_id" --demand-at "$demand_at" \
  > "$run_dir/target.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/target-submit-at.txt"
"${kc[@]}" create -f "$run_dir/target.yaml"
target_name="molmim-cached-${run_id}"
"${kc[@]}" get pod "$target_name" -o json > "$run_dir/target-created.json"
target_uid=$(jq -er '.metadata.uid' "$run_dir/target-created.json")

# Submit the separate CPU probe immediately after the API has assigned the
# target UID. It polls the run-scoped ClusterIP while conventional startup runs.
python3 "$script_dir/render.py" probe \
  --run-id "$run_id" --demand-at "$demand_at" --target-uid "$target_uid" \
  > "$run_dir/probe.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/probe-submit-at.txt"
"${kc[@]}" create -f "$run_dir/probe.yaml"

probe_name="molmim-cached-probe-${run_id}"
wait_for_job() {
  local name=$1 output=$2 attempt
  for ((attempt=0; attempt<900; attempt++)); do
    "${kc[@]}" get job "$name" -o json > "$output"
    jq -e '(.status.succeeded // 0) == 1' "$output" >/dev/null && return 0
    jq -e '(.status.failed // 0) > 0' "$output" >/dev/null && return 1
    sleep 1
  done
  return 124
}
wait_for_job "$probe_name" "$run_dir/probe-job.json" || {
  "${kc[@]}" get pods -l "job-name=$probe_name" -o json > "$run_dir/probe-pods.failed.json" || true
  exit 1
}
"${kc[@]}" get pods -l "job-name=$probe_name" -o json > "$run_dir/probe-pods.json"
probe_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/probe-pods.json")
"${kc[@]}" get pod "$probe_pod" -o json > "$run_dir/probe-pod.json"
"${kc[@]}" logs "$probe_pod" > "$run_dir/probe.log"
tail -1 "$run_dir/probe.log" | jq -e -c \
  'select(.status=="PASS" and .passed_case_count==2 and (.cases|length)==2)' \
  > "$run_dir/semantic-summary.json"
"${kc[@]}" get pod "$target_name" -o json > "$run_dir/target-final.json"
"${kc[@]}" get events --field-selector "involvedObject.uid=$target_uid" -o json \
  > "$run_dir/target-events.json"

python3 "$script_dir/evidence.py" \
  --run "$run_dir/run.json" \
  --target "$run_dir/target-final.json" \
  --probe-job "$run_dir/probe-job.json" \
  --probe-pod "$run_dir/probe-pod.json" \
  --semantic-summary "$run_dir/semantic-summary.json" \
  --target-submit-at "$run_dir/target-submit-at.txt" \
  --prewarm-holder-pod "$run_dir/prewarm-holder-pod.json" \
  --prewarm-holder-receipt "$run_dir/prewarm-holder-receipt.json" \
  --prewarm-captured-at "$run_dir/prewarm-captured-at.txt" \
  --events "$run_dir/target-events.json" \
  > "$run_dir/conventional-evidence.json"
jq -e 'select(.status=="PASS" and .request_count==2 and .semantic_pass_count==2)' \
  "$run_dir/conventional-evidence.json" >/dev/null

if ((cleanup == 1)); then
  {
    "${kc[@]}" delete -f "$run_dir/probe.yaml" --ignore-not-found --wait=true --timeout=120s
    "${kc[@]}" delete -f "$run_dir/target.yaml" --ignore-not-found --wait=true --timeout=180s
  } >> "$run_dir/cleanup.log" 2>&1
  date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/cleanup-verified-at.txt"
fi

jq '{run_id,status,timings_seconds}' "$run_dir/conventional-evidence.json"
