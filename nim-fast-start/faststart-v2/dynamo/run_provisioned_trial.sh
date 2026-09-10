#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly expected_contract_sha256="${OF2_EXPECTED_CONTRACT_SHA256:-e9f6da8b0923a776ee305c3b5b351dcca40f1db8d914b76c64fc14aa9c03130b}"
readonly expected_validator_sha256="66c75652e7f17b35ff569a59cc6a21bc09416329d9fbf30178041aed9006a9ed"
readonly trial_namespace='nim-fast-start'
readonly trial_artifact_pvc="${OF2_ARTIFACT_PVC:-openfold2-artifacts-example}"
readonly trial_cache_pvc="${OF2_CACHE_PVC:-openfold2-cache-example}"
readonly allowed_h100_nodes="${ALLOWED_H100_NODES:-gpu-node-a.example.invalid}"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly contract_path="${OF2_CONTRACT_PATH:-${script_dir}/restore-interface.live.json}"
readonly validator_path="${script_dir}/../validate_openfold2.py"
readonly qualification_collector="${script_dir}/../performance/qualification_receipt.py"
readonly manifest_splitter="${script_dir}/../performance/split_manifest.py"
readonly uid_cleanup_library="${script_dir}/../performance/uid_cleanup.sh"
readonly clock_sample_library="${script_dir}/../performance/clock_sample.sh"
readonly instrumentation_contract_builder="${script_dir}/../performance/instrumentation_contract.py"
# shellcheck source=../performance/uid_cleanup.sh
source "$uid_cleanup_library"
# shellcheck source=../performance/clock_sample.sh
source "$clock_sample_library"

trial_run_id=""
trial_evidence_root=""
trial_node=""
trial_kubeconfig=""
trial_holder=""
trial_cleanup=0
trial_cohort_id=""
trial_attempt_index=""
trial_attempt_ledger=""
trial_instrumentation_contract_sha256=""

usage() {
  cat >&2 <<'USAGE'
usage: run_provisioned_trial.sh \
  --run-id RUN_ID \
  --evidence-root ABSOLUTE_DIRECTORY \
  --node ALLOWED_H100_HOSTNAME \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD [--cleanup] \
  [--cohort-id DNS_LABEL --attempt-index POSITIVE_INTEGER \
   --attempt-ledger ABSOLUTE_APPEND_ONLY_NDJSON \
   --instrumentation-contract-sha256 LOWERCASE_SHA256]
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
    --cleanup)
      ((trial_cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      trial_cleanup=1
      shift
      ;;
    --cohort-id)
      (($# >= 2)) || die_usage "--cohort-id requires a value"
      set_once "--cohort-id" "$trial_cohort_id" "$2"
      trial_cohort_id=$2
      shift 2
      ;;
    --attempt-index)
      (($# >= 2)) || die_usage "--attempt-index requires a value"
      set_once "--attempt-index" "$trial_attempt_index" "$2"
      trial_attempt_index=$2
      shift 2
      ;;
    --attempt-ledger)
      (($# >= 2)) || die_usage "--attempt-ledger requires a value"
      set_once "--attempt-ledger" "$trial_attempt_ledger" "$2"
      trial_attempt_ledger=$2
      shift 2
      ;;
    --instrumentation-contract-sha256)
      (($# >= 2)) || die_usage "--instrumentation-contract-sha256 requires a value"
      set_once "--instrumentation-contract-sha256" \
        "$trial_instrumentation_contract_sha256" "$2"
      trial_instrumentation_contract_sha256=$2
      shift 2
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

cohort_option_count=0
[[ -z $trial_cohort_id ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $trial_attempt_index ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $trial_attempt_ledger ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $trial_instrumentation_contract_sha256 ]] || \
  cohort_option_count=$((cohort_option_count + 1))
if ((cohort_option_count != 0 && cohort_option_count != 4)); then
  die_usage "cohort ID, attempt index, attempt ledger, and instrumentation contract must be supplied together"
fi

if [[ ! $trial_run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#trial_run_id} -gt 30 ]]; then
  die_usage "--run-id must be a DNS label of at most 30 characters"
fi
if [[ ! $trial_holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#trial_holder} -gt 63 ]]; then
  die_usage "--artifact-holder must be a DNS label of at most 63 characters"
fi
if ((cohort_option_count == 4)); then
  if [[ ! $trial_cohort_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#trial_cohort_id} -gt 40 ]]; then
    die_usage "--cohort-id must be a DNS label of at most 40 characters"
  fi
  [[ $trial_attempt_index =~ ^[1-9][0-9]*$ ]] || \
    die_usage "--attempt-index must be a positive integer"
  [[ $trial_attempt_ledger == /* ]] || \
    die_usage "--attempt-ledger must be absolute"
  [[ -f $trial_attempt_ledger && ! -L $trial_attempt_ledger ]] || \
    die_usage "--attempt-ledger must be an existing regular non-symlink file"
  [[ $trial_instrumentation_contract_sha256 =~ ^[0-9a-f]{64}$ ]] || \
    die_usage "--instrumentation-contract-sha256 must be one lowercase SHA-256"
  ((trial_cleanup == 1)) || \
    die_usage "cohort attempts require --cleanup"
fi
trial_node_allowed=0
IFS=',' read -r -a allowed_node_list <<<"$allowed_h100_nodes"
for allowed_node in "${allowed_node_list[@]}"; do
  if [[ $trial_node == "$allowed_node" ]]; then
    trial_node_allowed=1
    break
  fi
done
((trial_node_allowed == 1)) || die_usage "--node is not an allowlisted H100 hostname"
[[ $trial_evidence_root == /* ]] || die_usage "--evidence-root must be absolute"
[[ -d $trial_evidence_root && ! -L $trial_evidence_root ]] || die_usage "--evidence-root must be an existing non-symlink directory"
[[ -d $trial_evidence_root/runs && ! -L $trial_evidence_root/runs ]] || die_usage "--evidence-root/runs must be an existing non-symlink directory"
[[ $trial_kubeconfig == /* ]] || die_usage "--kubeconfig must be absolute"
[[ -f $trial_kubeconfig && ! -L $trial_kubeconfig ]] || die_usage "--kubeconfig must be a regular non-symlink file"

for required_command in kubectl jq python3 sha256sum date install tail sleep mv rm curl; do
  command -v "$required_command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$required_command" >&2
    exit 69
  }
done

actual_contract_sha256=$(sha256sum "$contract_path")
actual_contract_sha256=${actual_contract_sha256%% *}
if [[ $actual_contract_sha256 != "$expected_contract_sha256" ]]; then
  printf 'immutable restore contract digest mismatch\n' >&2
  exit 78
fi
actual_validator_sha256=$(sha256sum "$validator_path")
actual_validator_sha256=${actual_validator_sha256%% *}
if [[ $actual_validator_sha256 != "$expected_validator_sha256" ]]; then
  printf 'semantic validator digest mismatch\n' >&2
  exit 78
fi
jq -e --arg validator "$expected_validator_sha256" '
  .approved == true and
  .validator_sha256 == $validator and
  (.worker_image | test("@sha256:[0-9a-f]{64}$")) and
  (.probe_image | test("@sha256:[0-9a-f]{64}$"))
' "$contract_path" >/dev/null || {
  printf 'immutable restore contract is not deployable\n' >&2
  exit 78
}
trial_instrumentation_contract_receipt=""
if ((cohort_option_count == 4)); then
  trial_instrumentation_contract_receipt=$(
    python3 "$instrumentation_contract_builder" --model openfold2
  )
  actual_instrumentation_contract_sha256=$(jq -er \
    '.instrumentation_contract_sha256 | select(test("^[0-9a-f]{64}$"))' \
    <<<"$trial_instrumentation_contract_receipt")
  if [[ $actual_instrumentation_contract_sha256 != \
        "$trial_instrumentation_contract_sha256" ]]; then
    printf 'fresh-cohort instrumentation contract drifted before admission\n' >&2
    exit 78
  fi
fi

readonly trial_dir="${trial_evidence_root}/runs/${trial_run_id}"
readonly trial_target="of2-target-${trial_run_id}"
readonly trial_worker="of2-restore-${trial_run_id}"
readonly trial_probe="of2-semantic-${trial_run_id}"
readonly trial_canary="of2-canary-${trial_run_id}"

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
holder_json=$("${trial_kubectl[@]}" get pod "$trial_holder" -o json)
if ! jq -e --arg node "$trial_node" '
  .spec.nodeName == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.containerStatuses // []) | length > 0) and
  all(.status.containerStatuses[]?; .ready == true)
' <<<"$holder_json" >/dev/null; then
  printf 'artifact holder is not Ready on the requested node\n' >&2
  exit 69
fi
trial_holder_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
trial_holder_mount_verifications='[]'
for trial_holder_claim in "$trial_artifact_pvc" "$trial_cache_pvc"; do
  trial_holder_mount=$(
    jq -er --arg claim "$trial_holder_claim" '
      [.spec.volumes[]? |
        select(.persistentVolumeClaim.claimName == $claim) | .name] as $names |
      select(($names | length) == 1) |
      [.spec.containers[]? as $container |
        $container.volumeMounts[]? |
        select(.name == $names[0]) |
        [$container.name, $names[0], .mountPath]] |
      select(length == 1) | .[0] | @tsv
    ' <<<"$holder_json"
  ) || {
    printf 'artifact holder does not mount reviewed PVC exactly once: %s\n' \
      "$trial_holder_claim" >&2
    exit 69
  }
  IFS=$'\t' read -r trial_holder_container trial_holder_volume \
    trial_holder_mount_path <<<"$trial_holder_mount"
  "${trial_kubectl[@]}" exec "pod/$trial_holder" -c "$trial_holder_container" -- \
    /bin/test -d "$trial_holder_mount_path" || {
    printf 'artifact holder PVC mount is not accessible: %s\n' \
      "$trial_holder_claim" >&2
    exit 69
  }
  trial_holder_mount_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  trial_holder_mount_verification=$(jq -nc \
    --arg checked_at "$trial_holder_mount_checked_at" \
    --arg claim "$trial_holder_claim" --arg container "$trial_holder_container" \
    --arg volume "$trial_holder_volume" --arg mount_path "$trial_holder_mount_path" \
    '{checked_at:$checked_at,claim:$claim,container:$container,
      volume_name:$volume,mount_path:$mount_path,
      command:["/bin/test","-d",$mount_path],status:"PASS",exit_code:0}')
  trial_holder_mount_verifications=$(jq -nc \
    --argjson values "$trial_holder_mount_verifications" \
    --argjson item "$trial_holder_mount_verification" '$values + [$item]')
done
if ! trial_capture_agent_list=$("${trial_kubectl[@]}" get daemonsets -o json); then
  printf 'could not prove native capture agent absence\n' >&2
  exit 69
fi
if ((${#trial_capture_agent_list} > 1048576)) || ! jq -e '
  (.kind == "DaemonSetList" or .kind == "List") and
  (.items | type == "array") and
  ([.items[]? | select(.metadata.name == "archvteams-2407-native-snapshot-agent")]
    | length) == 0
' <<<"$trial_capture_agent_list" >/dev/null; then
  printf 'native capture agent is present or its absence receipt is malformed\n' >&2
  exit 69
fi
trial_capture_agent_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)

target_create_attempted=0
target_support_create_attempted=0
probe_create_attempted=0
worker_create_attempted=0
trial_admitted=0

append_admission_event() {
  local admitted_at=$1
  local runner_sha256
  [[ -n $trial_attempt_ledger ]] || return 0
  runner_sha256=$(sha256sum "${BASH_SOURCE[0]}")
  runner_sha256=${runner_sha256%% *}
  jq -nc \
    --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
    --arg event "admitted" \
    --arg cohort_id "$trial_cohort_id" \
    --arg model "openfold2" \
    --arg run_id "$trial_run_id" \
    --arg admitted_at "$admitted_at" \
    --arg trial_dir "$trial_dir" \
    --arg runner_sha256 "$runner_sha256" \
    --arg instrumentation_contract_sha256 \
      "$trial_instrumentation_contract_sha256" \
    --argjson attempt_index "$trial_attempt_index" \
    '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
      attempt_index:$attempt_index,run_id:$run_id,admitted_at:$admitted_at,
      trial_dir:$trial_dir,runner_sha256:$runner_sha256,
      instrumentation_contract_sha256:$instrumentation_contract_sha256}' \
    >> "$trial_attempt_ledger"
}

write_cleanup_receipt() {
  local original_exit_code=$1
  local cleanup_started_at cleanup_completed_at cleanup_status
  local cleanup_failure=0
  local resources_file="$trial_dir/.cleanup-resources.ndjson"
  local receipt_partial="$trial_dir/.cleanup-receipt.json.partial"
  cleanup_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  : > "$resources_file"
  : > "$trial_dir/cleanup.log"

  if ((trial_cleanup == 1)) && \
    ((target_support_create_attempted == 1 || target_create_attempted == 1 || \
      worker_create_attempted == 1 || probe_create_attempted == 1)); then
    uid_cleanup_start_proxy trial_kubectl "$trial_dir" \
      "$trial_dir/cleanup.log" || cleanup_failure=1
  fi

  uid_cleanup_group trial_kubectl "$trial_namespace" "$trial_cleanup" \
    semantic-probe "$trial_dir/probe-create-response.json" \
    "$probe_create_attempted" 2 120s \
    "$trial_dir/cleanup.log" "$resources_file" "$trial_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$trial_namespace" "$trial_cleanup" \
    restore-worker "$trial_dir/worker-create-response.json" \
    "$worker_create_attempted" 4 120s \
    "$trial_dir/cleanup.log" "$resources_file" "$trial_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$trial_namespace" "$trial_cleanup" \
    target "$trial_dir/target-create-response.json" \
    "$target_create_attempted" 1 180s \
    "$trial_dir/cleanup.log" "$resources_file" "$trial_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$trial_namespace" "$trial_cleanup" \
    target-support "$trial_dir/target-support-create-response.json" \
    "$target_support_create_attempted" 4 120s \
    "$trial_dir/cleanup.log" "$resources_file" "$trial_dir" || cleanup_failure=1
  uid_cleanup_stop_proxy

  cleanup_completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  if ((trial_cleanup == 0)); then
    cleanup_status="NOT_REQUESTED"
  elif ((cleanup_failure == 0)); then
    cleanup_status="PASS"
  else
    cleanup_status="FAIL"
  fi
  if jq -s \
    --arg schema "archvteams.nebius.ai/run-cleanup-receipt/v1" \
    --arg run_id "$trial_run_id" \
    --arg status "$cleanup_status" \
    --arg started_at "$cleanup_started_at" \
    --arg completed_at "$cleanup_completed_at" \
    --argjson requested "$([[ $trial_cleanup == 1 ]] && printf true || printf false)" \
    --argjson original_exit_code "$original_exit_code" \
    '{schema:$schema,run_id:$run_id,status:$status,requested:$requested,
      original_runner_exit_code:$original_exit_code,started_at:$started_at,
      completed_at:$completed_at,resources:.}' \
    "$resources_file" > "$receipt_partial"; then
    mv -- "$receipt_partial" "$trial_dir/cleanup-receipt.json" || cleanup_failure=1
  else
    cleanup_failure=1
  fi
  rm -f -- "$resources_file" "$receipt_partial"
  return "$cleanup_failure"
}

finalize_trial() {
  local original_exit_code=$?
  local cleanup_exit_code=0 final_exit_code cleanup_status completed_at
  trap - EXIT
  # Cleanup and receipt publication are the fail-closed critical section.  A
  # second signal must not interrupt UID-preconditioned deletion halfway.
  trap ':' INT TERM
  set +e
  write_cleanup_receipt "$original_exit_code"
  cleanup_exit_code=$?
  final_exit_code=$original_exit_code
  if ((final_exit_code == 0 && cleanup_exit_code != 0)); then
    final_exit_code=1
  fi
  cleanup_status=$(jq -r '.status' "$trial_dir/cleanup-receipt.json" 2>/dev/null)
  [[ -n $cleanup_status && $cleanup_status != null ]] || cleanup_status="FAIL"
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  jq -n \
    --arg schema "archvteams.nebius.ai/runner-attempt-result/v1" \
    --arg run_id "$trial_run_id" \
    --arg model "openfold2" \
    --arg completed_at "$completed_at" \
    --arg cleanup_status "$cleanup_status" \
    --argjson admitted "$([[ $trial_admitted == 1 ]] && printf true || printf false)" \
    --argjson original_exit_code "$original_exit_code" \
    --argjson final_exit_code "$final_exit_code" \
    '{schema:$schema,run_id:$run_id,model:$model,admitted:$admitted,
      completed_at:$completed_at,original_runner_exit_code:$original_exit_code,
      cleanup_status:$cleanup_status,final_exit_code:$final_exit_code}' \
    > "$trial_dir/attempt-result.json" || final_exit_code=1
  if ((trial_admitted == 1)) && [[ -n $trial_attempt_ledger ]]; then
    jq -nc \
      --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
      --arg event "completed" \
      --arg cohort_id "$trial_cohort_id" \
      --arg model "openfold2" \
      --arg run_id "$trial_run_id" \
      --arg completed_at "$completed_at" \
      --arg trial_dir "$trial_dir" \
      --arg summary_path "$trial_dir/canary-evidence.json" \
      --arg cleanup_receipt_path "$trial_dir/cleanup-receipt.json" \
      --arg cleanup_status "$cleanup_status" \
      --argjson attempt_index "$trial_attempt_index" \
      --argjson runner_exit_code "$final_exit_code" \
      '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
        attempt_index:$attempt_index,run_id:$run_id,completed_at:$completed_at,
        trial_dir:$trial_dir,summary_path:$summary_path,
        cleanup_receipt_path:$cleanup_receipt_path,cleanup_status:$cleanup_status,
        runner_exit_code:$runner_exit_code}' \
      >> "$trial_attempt_ledger" || final_exit_code=1
  fi
  exit "$final_exit_code"
}

mkdir -m 0700 -- "$trial_dir" || {
  printf 'could not create new run directory: %s\n' "$trial_dir" >&2
  exit 73
}
trap finalize_trial EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -n $trial_instrumentation_contract_receipt ]]; then
  printf '%s\n' "$trial_instrumentation_contract_receipt" \
    > "$trial_dir/instrumentation-contract.json"
fi
jq -n --arg checked_at "$trial_holder_checked_at" --argjson pod "$holder_json" \
  --argjson mount_verifications "$trial_holder_mount_verifications" \
  '{schema:"archvteams.nebius.ai/warm-storage-holder-check/v1",
    checked_at:$checked_at,pod:$pod,mount_verifications:$mount_verifications}' \
  > "$trial_dir/artifact-holder.json"
jq -n --arg checked_at "$trial_capture_agent_checked_at" \
  --argjson daemonset_list "$trial_capture_agent_list" \
  '{schema:"archvteams.nebius.ai/capture-agent-absence/v1",
    checked_at:$checked_at,namespace:"nim-fast-start",
    forbidden_name:"archvteams-2407-native-snapshot-agent",
    daemonset_list:$daemonset_list,status:"PASS"}' \
  > "$trial_dir/capture-agent-absence.json"
trial_holder_uid=$(jq -er '.pod.metadata.uid' "$trial_dir/artifact-holder.json")
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
  --arg artifact_pvc "$trial_artifact_pvc" \
  --arg cache_pvc "$trial_cache_pvc" \
  '{
    schema:"archvteams.nebius.ai/openfold2-faststart-run/v1",
    demand_at:$demand_at,
    run_id:$run_id,
    target_node:$node,
    checkpoint_id:"openfold2-native-f7-v1",
    artifact_version:"1",
    artifact_manifest_sha256:"78368af3e6f143d7dc681632c4150b29f6354717103638b56e776244d9631b04",
    artifact_pvc:$artifact_pvc,
    cache_pvc:$cache_pvc
  }' > "$trial_dir/run.json"

python3 "$script_dir/render.py" target \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" > "$trial_dir/target.yaml"
python3 "$script_dir/lint_manifest.py" "$trial_dir/target.yaml"
python3 "$manifest_splitter" \
  --input "$trial_dir/target.yaml" \
  --output-directory "$trial_dir/target-bundle" \
  --bundle target > "$trial_dir/target-bundle.json"
trial_target_support_manifests=("$trial_dir"/target-bundle/support/*.json)
[[ ${#trial_target_support_manifests[@]} == 4 ]] || {
  printf 'target support bundle is incomplete\n' >&2
  exit 78
}
target_support_create_attempted=1
if ! uid_create_bundle trial_kubectl \
  "$trial_dir/target-support-create-response.json" \
  "$trial_dir/target-support-create.log" \
  "${trial_target_support_manifests[@]}"; then
  printf 'target support creation failed\n' >&2
  exit 1
fi
# Cohort admission is durably appended before T0.  The primary clock remains
# the conservative pre-dispatch boundary; the post-response timestamp below is
# an explicitly labeled client-observed proxy, not exact server acceptance.
capture_controller_clock_boundary cohort-admission \
  "$trial_dir/admission-boundary.json"
trial_admitted_at=$(jq -er '.utc' "$trial_dir/admission-boundary.json")
append_admission_event "$trial_admitted_at"
trial_admitted=1
"${trial_kubectl[@]}" get pod "$trial_holder" -o json \
  > "$trial_dir/anchor-holder.json"
jq -e --arg uid "$trial_holder_uid" --arg node "$trial_node" \
  --arg image "$BOOT_TIME_ANCHOR_HOLDER_IMAGE" '
    .metadata.uid == $uid and .metadata.deletionTimestamp == null and
    .spec.nodeName == $node and .status.phase == "Running" and
    ([.status.conditions[] | select(.type == "Ready" and .status == "True")] | length) == 1 and
    ([.spec.containers[] | select(.name == "holder" and .image == $image)] | length) == 1 and
    ([.status.containerStatuses[] | select(.name == "holder" and .ready == true and
      .restartCount == 0 and .imageID == $image)] | length) == 1
  ' "$trial_dir/anchor-holder.json" >/dev/null
capture_boot_time_anchor trial_kubectl "$trial_holder" "$trial_holder_uid" \
  "$trial_node" holder "$trial_dir/boot-time-anchor.json"
capture_controller_clock_boundary target-submit \
  "$trial_dir/target-submit-clock.json"
jq -er '.utc' "$trial_dir/target-submit-clock.json" \
  > "$trial_dir/target-submit-at.txt"
target_create_attempted=1
"${trial_kubectl[@]}" create -f "$trial_dir/target-bundle/primary.json" -o json \
  > "$trial_dir/target-create-response.json" \
  2> "$trial_dir/target-create.stderr"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/target-create-response-at.txt"
jq -e --arg name "$trial_target" '
  select(.apiVersion == "v1" and .kind == "Pod")
  | select(.metadata.name == $name and .metadata.namespace == "nim-fast-start")
  | select(.metadata.uid | type == "string" and length > 0)
' "$trial_dir/target-create-response.json" >/dev/null
"${trial_kubectl[@]}" wait \
  --for=jsonpath='{.status.containerStatuses[0].state.running}' \
  "pod/$trial_target" --timeout=300s
"${trial_kubectl[@]}" get pod "$trial_target" -o json \
  > "$trial_dir/target-before-binding.json"
trial_target_uid=$(jq -er '.metadata.uid' "$trial_dir/target-before-binding.json")

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
python3 "$manifest_splitter" \
  --input "$trial_dir/semantic-probe.yaml" \
  --output-directory "$trial_dir/probe-bundle" \
  --bundle semantic-probe > "$trial_dir/probe-bundle.json"
trial_probe_support_manifests=("$trial_dir"/probe-bundle/support/*.json)
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/probe-submit-at.txt"
probe_create_attempted=1
if ! uid_create_bundle trial_kubectl "$trial_dir/probe-create-response.json" \
  "$trial_dir/probe-create.log" "${trial_probe_support_manifests[@]}" \
  "$trial_dir/probe-bundle/primary.json"; then
  printf 'semantic probe creation failed\n' >&2
  exit 1
fi

python3 "$script_dir/render.py" restore \
  --contract "$trial_dir/restore-interface.json" \
  --run-config "$trial_dir/run.json" \
  --binding "$trial_dir/binding.json" > "$trial_dir/restore-worker.yaml"
python3 "$script_dir/lint_manifest.py" "$trial_dir/restore-worker.yaml"
python3 "$manifest_splitter" \
  --input "$trial_dir/restore-worker.yaml" \
  --output-directory "$trial_dir/worker-bundle" \
  --bundle restore-worker > "$trial_dir/worker-bundle.json"
trial_worker_support_manifests=("$trial_dir"/worker-bundle/support/*.json)
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$trial_dir/worker-submit-at.txt"
worker_create_attempted=1
if ! uid_create_bundle trial_kubectl "$trial_dir/worker-create-response.json" \
  "$trial_dir/worker-create.log" "${trial_worker_support_manifests[@]}" \
  "$trial_dir/worker-bundle/primary.json"; then
  printf 'restore worker creation failed\n' >&2
  exit 1
fi

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

trial_target_uid=$(jq -er '.metadata.uid' "$trial_dir/target-final.json")
trial_target_image=$(jq -er '
  [.spec.containers[] | select(.name == "openfold2") | .image] |
  select(length == 1) | .[0]
' "$trial_dir/target-final.json")
"${trial_kubectl[@]}" get events \
  --field-selector "involvedObject.uid=$trial_target_uid" -o json \
  > "$trial_dir/target-events.json"
"${trial_kubectl[@]}" exec "pod/$trial_target" -c openfold2 -- \
  nvidia-smi -q -x > "$trial_dir/target-nvidia-smi.xml" \
  2> "$trial_dir/target-nvidia-smi.stderr"
python3 "$qualification_collector" \
  --model openfold2 \
  --run-id "$trial_run_id" \
  --namespace "$trial_namespace" \
  --target-name "$trial_target" \
  --target-container openfold2 \
  --expected-image "$trial_target_image" \
  --worker-container restore-worker \
  --probe-container semantic-probe \
  --target-submit-at "$trial_dir/target-submit-at.txt" \
  --target-create-response-at "$trial_dir/target-create-response-at.txt" \
  --target-create-response "$trial_dir/target-create-response.json" \
  --target-pod "$trial_dir/target-final.json" \
  --target-events "$trial_dir/target-events.json" \
  --worker-pod "$trial_dir/worker-pod.json" \
  --worker-receipt "$trial_dir/worker-receipt.json" \
  --probe-pod "$trial_dir/probe-pod.json" \
  --semantic-summary "$trial_dir/semantic-summary.json" \
  --gpu-health-xml "$trial_dir/target-nvidia-smi.xml" \
  --gpu-health-stderr "$trial_dir/target-nvidia-smi.stderr" \
  --admission-boundary "$trial_dir/admission-boundary.json" \
  --target-submit-clock "$trial_dir/target-submit-clock.json" \
  --boot-time-anchor "$trial_dir/boot-time-anchor.json" \
  --anchor-holder "$trial_dir/anchor-holder.json" \
  --capture-agent-absence "$trial_dir/capture-agent-absence.json" \
  > "$trial_dir/qualification-receipt.json"

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
  --target-create-response-at "$trial_dir/target-create-response-at.txt" \
  --qualification-receipt "$trial_dir/qualification-receipt.json" \
  > "$trial_dir/canary-evidence.json"

jq -e 'select(.status=="PASS" and .request_count==2 and .semantic_pass_count==2)' \
  "$trial_dir/canary-evidence.json" >/dev/null

jq '{run_id,status,timings_seconds}' "$trial_dir/canary-evidence.json"
