#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

run_id=""
cleanup=0
checkpoint_id=${B2_CHECKPOINT_ID:-boltz2-native-f7-v1}
artifact_manifest_sha256=${B2_ARTIFACT_MANIFEST_SHA256:-6539b9f50a71c9f5fb6a3fbacd44f5d5ea41003539b6563682a38600d1492456}
readonly expected_checkpoint_id="boltz2-native-f7-v1"
readonly expected_artifact_manifest_sha256="6539b9f50a71c9f5fb6a3fbacd44f5d5ea41003539b6563682a38600d1492456"
readonly expected_contract_sha256="${B2_EXPECTED_CONTRACT_SHA256:-ca266cc317802971d6f767bf8c28008338bd88dc7470d0fc45ed7084f6845e9c}"
readonly namespace='nim-fast-start'
artifact_pvc=${B2_ARTIFACT_PVC:-boltz2-artifacts-example}
cache_pvc=${B2_CACHE_PVC:-boltz2-cache-example}
allowed_h100_nodes=${ALLOWED_H100_NODES:-gpu-node-a.example.invalid}
code_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly code_dir
readonly contract_path="${B2_CONTRACT_PATH:-${code_dir}/restore-interface.live.json}"
readonly qualification_collector="${code_dir}/../performance/qualification_receipt.py"
readonly manifest_splitter="${code_dir}/../performance/split_manifest.py"
readonly uid_cleanup_library="${code_dir}/../performance/uid_cleanup.sh"
readonly clock_sample_library="${code_dir}/../performance/clock_sample.sh"
readonly instrumentation_contract_builder="${code_dir}/../performance/instrumentation_contract.py"
# shellcheck source=../performance/uid_cleanup.sh
source "$uid_cleanup_library"
# shellcheck source=../performance/clock_sample.sh
source "$clock_sample_library"
kubeconfig=""
node=""
root=""
artifact_holder=""
cache_holder=""
cohort_id=""
attempt_index=""
attempt_ledger=""
instrumentation_contract_sha256=""

usage() {
  cat >&2 <<'USAGE'
usage: run_one_native_trial.sh \
  --run-id RUN_ID \
  --evidence-root ABSOLUTE_DIRECTORY \
  --node ALLOWED_H100_HOSTNAME \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD \
  --cache-holder READY_POD [--cleanup] \
  [--cohort-id DNS_LABEL --attempt-index POSITIVE_INTEGER \
   --attempt-ledger ABSOLUTE_APPEND_ONLY_NDJSON \
   --instrumentation-contract-sha256 LOWERCASE_SHA256]

Legacy compatibility: run_one_native_trial.sh RUN_ID [--cleanup] reads the
kubeconfig, node, evidence root, and optional holder names from the environment.
USAGE
}

die_usage() { printf '%s\n' "$1" >&2; usage; exit 64; }

set_once() {
  local option_name=$1 current_value=$2 new_value=$3
  [[ -z $current_value ]] || die_usage "${option_name} may be supplied only once"
  [[ -n $new_value ]] || die_usage "${option_name} requires a nonempty value"
}

if (($# >= 1)) && [[ $1 != --* ]]; then
  (($# <= 2)) || die_usage "legacy form accepts only RUN_ID [--cleanup]"
  run_id=$1
  [[ ${2:-} != --cleanup ]] || cleanup=1
  [[ -z ${2:-} || ${2:-} == --cleanup ]] || \
    die_usage "legacy second argument must be --cleanup"
  kubeconfig=${KUBECONFIG:?set KUBECONFIG to an absolute kubeconfig path}
  node=${TARGET_NODE:?set TARGET_NODE to the selected GPU node hostname}
  root=${BOLTZ2_EVIDENCE_ROOT:?set BOLTZ2_EVIDENCE_ROOT to an absolute evidence directory}
  artifact_holder=${B2_ARTIFACT_HOLDER:-boltz2-artifact-holder-example}
  cache_holder=${B2_CACHE_HOLDER:-boltz2-cache-holder-example}
else
  while (($# > 0)); do
    case "$1" in
      --run-id)
        (($# >= 2)) || die_usage "--run-id requires a value"
        set_once --run-id "$run_id" "$2"; run_id=$2; shift 2 ;;
      --evidence-root)
        (($# >= 2)) || die_usage "--evidence-root requires a value"
        set_once --evidence-root "$root" "$2"; root=$2; shift 2 ;;
      --node)
        (($# >= 2)) || die_usage "--node requires a value"
        set_once --node "$node" "$2"; node=$2; shift 2 ;;
      --kubeconfig)
        (($# >= 2)) || die_usage "--kubeconfig requires a value"
        set_once --kubeconfig "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
      --artifact-holder)
        (($# >= 2)) || die_usage "--artifact-holder requires a value"
        set_once --artifact-holder "$artifact_holder" "$2"; artifact_holder=$2; shift 2 ;;
      --cache-holder)
        (($# >= 2)) || die_usage "--cache-holder requires a value"
        set_once --cache-holder "$cache_holder" "$2"; cache_holder=$2; shift 2 ;;
      --cleanup)
        ((cleanup == 0)) || die_usage "--cleanup may be supplied only once"
        cleanup=1; shift ;;
      --cohort-id)
        (($# >= 2)) || die_usage "--cohort-id requires a value"
        set_once --cohort-id "$cohort_id" "$2"; cohort_id=$2; shift 2 ;;
      --attempt-index)
        (($# >= 2)) || die_usage "--attempt-index requires a value"
        set_once --attempt-index "$attempt_index" "$2"; attempt_index=$2; shift 2 ;;
      --attempt-ledger)
        (($# >= 2)) || die_usage "--attempt-ledger requires a value"
        set_once --attempt-ledger "$attempt_ledger" "$2"; attempt_ledger=$2; shift 2 ;;
      --instrumentation-contract-sha256)
        (($# >= 2)) || die_usage "--instrumentation-contract-sha256 requires a value"
        set_once --instrumentation-contract-sha256 \
          "$instrumentation_contract_sha256" "$2"
        instrumentation_contract_sha256=$2; shift 2 ;;
      --help|-h)
        usage; exit 0 ;;
      *)
        die_usage "unknown argument: $1" ;;
    esac
  done
fi

[[ -n $run_id && -n $root && -n $node && -n $kubeconfig ]] || \
  die_usage "run ID, evidence root, node, and kubeconfig are required"
[[ -n $artifact_holder && -n $cache_holder ]] || \
  die_usage "artifact and cache holders are required"
run_dir="$root/runs/$run_id"
target="b2-target-$run_id"
worker="b2-restore-$run_id"
probe="b2-semantic-$run_id"
canary="b2-canary-$run_id"

if [[ ! $run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#run_id} -gt 30 ]]; then
  echo "invalid run ID" >&2
  exit 64
fi
for holder in "$artifact_holder" "$cache_holder"; do
  [[ $holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#holder} -le 63 ]] || \
    die_usage "holder names must be DNS labels"
done
node_allowed=0
IFS=',' read -r -a allowed_node_list <<<"$allowed_h100_nodes"
for allowed_node in "${allowed_node_list[@]}"; do
  if [[ $node == "$allowed_node" ]]; then
    node_allowed=1
    break
  fi
done
((node_allowed == 1)) || die_usage "--node is not an allowlisted H100 hostname"
[[ $root == /* && -d $root && ! -L $root ]] || \
  die_usage "--evidence-root must be an existing absolute non-symlink directory"
[[ -d $root/runs && ! -L $root/runs ]] || \
  die_usage "--evidence-root/runs must be an existing non-symlink directory"
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || \
  die_usage "--kubeconfig must be an absolute regular non-symlink file"

cohort_option_count=0
[[ -z $cohort_id ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $attempt_index ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $attempt_ledger ]] || cohort_option_count=$((cohort_option_count + 1))
[[ -z $instrumentation_contract_sha256 ]] || \
  cohort_option_count=$((cohort_option_count + 1))
if ((cohort_option_count != 0 && cohort_option_count != 4)); then
  die_usage "cohort ID, attempt index, attempt ledger, and instrumentation contract must be supplied together"
fi
if ((cohort_option_count == 4)); then
  [[ $cohort_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#cohort_id} -le 40 ]] || \
    die_usage "--cohort-id must be a DNS label of at most 40 characters"
  [[ $attempt_index =~ ^[1-9][0-9]*$ ]] || \
    die_usage "--attempt-index must be a positive integer"
  [[ $attempt_ledger == /* && -f $attempt_ledger && ! -L $attempt_ledger ]] || \
    die_usage "--attempt-ledger must be an absolute regular non-symlink file"
  [[ $instrumentation_contract_sha256 =~ ^[0-9a-f]{64}$ ]] || \
    die_usage "--instrumentation-contract-sha256 must be one lowercase SHA-256"
  ((cleanup == 1)) || die_usage "cohort attempts require --cleanup"
  [[ $checkpoint_id == "$expected_checkpoint_id" && \
     $artifact_manifest_sha256 == "$expected_artifact_manifest_sha256" ]] || \
    die_usage "fresh cohorts forbid Boltz2 checkpoint/artifact environment overrides"
fi
if [[ -e $run_dir || -L $run_dir ]]; then
  echo "run directory already exists: $run_dir" >&2
  exit 73
fi

for required_command in kubectl jq python3 sha256sum date install tail sleep mv rm curl; do
  command -v "$required_command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$required_command" >&2
    exit 69
  }
done

actual_contract_sha256=$(sha256sum "$contract_path")
actual_contract_sha256=${actual_contract_sha256%% *}
if [[ $actual_contract_sha256 != "$expected_contract_sha256" ]]; then
  printf 'immutable Boltz2 restore contract digest mismatch\n' >&2
  exit 78
fi
instrumentation_contract_receipt=""
if ((cohort_option_count == 4)); then
  instrumentation_contract_receipt=$(
    python3 "$instrumentation_contract_builder" --model boltz2
  )
  actual_instrumentation_contract_sha256=$(jq -er \
    '.instrumentation_contract_sha256 | select(test("^[0-9a-f]{64}$"))' \
    <<<"$instrumentation_contract_receipt")
  if [[ $actual_instrumentation_contract_sha256 != \
        "$instrumentation_contract_sha256" ]]; then
    printf 'fresh-cohort instrumentation contract drifted before admission\n' >&2
    exit 78
  fi
fi

server=$(kubectl --kubeconfig "$kubeconfig" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
if [[ $server != "${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER to the exact Kubernetes API URL}" ]]; then
  echo "kubeconfig is not bound to the allowed cluster" >&2
  exit 78
fi
trial_kubectl=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")
artifact_holder_json=""
artifact_holder_checked_at=""
artifact_holder_mount_json=""
cache_holder_json=""
cache_holder_checked_at=""
cache_holder_mount_json=""
for holder in "$artifact_holder" "$cache_holder"; do
  holder_json=$("${trial_kubectl[@]}" get pod "$holder" -o json)
  if ! jq -e --arg node "$node" '
    .spec.nodeName == $node and
    any(.status.conditions[]?; .type == "Ready" and .status == "True") and
    ((.status.containerStatuses // []) | length > 0) and
    all(.status.containerStatuses[]?; .ready == true)
  ' <<<"$holder_json" >/dev/null; then
    echo "required holder is not Ready on the requested node: $holder" >&2
    exit 69
  fi
  holder_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  if [[ $holder == "$artifact_holder" ]]; then
    holder_claim=$artifact_pvc
  else
    holder_claim=$cache_pvc
  fi
  holder_mount=$(
    jq -er --arg claim "$holder_claim" '
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
    printf 'holder does not mount the reviewed PVC exactly once: %s\n' "$holder" >&2
    exit 69
  }
  IFS=$'\t' read -r holder_container holder_volume holder_mount_path <<<"$holder_mount"
  "${trial_kubectl[@]}" exec "pod/$holder" -c "$holder_container" -- \
    /bin/test -d "$holder_mount_path" || {
    printf 'holder PVC mount is not accessible: %s\n' "$holder" >&2
    exit 69
  }
  holder_mount_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  holder_mount_json=$(jq -nc --arg checked_at "$holder_mount_checked_at" \
    --arg claim "$holder_claim" \
    --arg container "$holder_container" --arg volume "$holder_volume" \
    --arg mount_path "$holder_mount_path" \
    '{checked_at:$checked_at,claim:$claim,container:$container,volume_name:$volume,
      mount_path:$mount_path,command:["/bin/test","-d",$mount_path],
      status:"PASS",exit_code:0}')
  if [[ $holder == "$artifact_holder" ]]; then
    artifact_holder_json=$holder_json
    artifact_holder_checked_at=$holder_checked_at
    artifact_holder_mount_json=$holder_mount_json
  else
    cache_holder_json=$holder_json
    cache_holder_checked_at=$holder_checked_at
    cache_holder_mount_json=$holder_mount_json
  fi
done
if ! capture_agent_list=$("${trial_kubectl[@]}" get daemonsets -o json); then
  echo "could not prove native capture agent absence" >&2
  exit 69
fi
if ((${#capture_agent_list} > 1048576)) || ! jq -e '
  (.kind == "DaemonSetList" or .kind == "List") and
  (.items | type == "array") and
  ([.items[]? | select(.metadata.name == "archvteams-2407-native-snapshot-agent")]
    | length) == 0
' <<<"$capture_agent_list" >/dev/null; then
  echo "native capture agent is present or its absence receipt is malformed" >&2
  exit 69
fi
capture_agent_checked_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)

target_create_attempted=0
target_support_create_attempted=0
probe_create_attempted=0
worker_create_attempted=0
trial_admitted=0

append_admission_event() {
  local admitted_at=$1 runner_sha256
  [[ -n $attempt_ledger ]] || return 0
  runner_sha256=$(sha256sum "${BASH_SOURCE[0]}")
  runner_sha256=${runner_sha256%% *}
  jq -nc \
    --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
    --arg event "admitted" \
    --arg cohort_id "$cohort_id" \
    --arg model "boltz2" \
    --arg run_id "$run_id" \
    --arg admitted_at "$admitted_at" \
    --arg trial_dir "$run_dir" \
    --arg runner_sha256 "$runner_sha256" \
    --arg instrumentation_contract_sha256 \
      "$instrumentation_contract_sha256" \
    --argjson attempt_index "$attempt_index" \
    '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
      attempt_index:$attempt_index,run_id:$run_id,admitted_at:$admitted_at,
      trial_dir:$trial_dir,runner_sha256:$runner_sha256,
      instrumentation_contract_sha256:$instrumentation_contract_sha256}' \
    >> "$attempt_ledger"
}

write_cleanup_receipt() {
  local original_exit_code=$1
  local cleanup_started_at cleanup_completed_at cleanup_status
  local cleanup_failure=0
  local resources_file="$run_dir/.cleanup-resources.ndjson"
  local receipt_partial="$run_dir/.cleanup-receipt.json.partial"
  cleanup_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  : > "$resources_file"
  : > "$run_dir/cleanup.log"

  if ((cleanup == 1)) && \
    ((target_support_create_attempted == 1 || target_create_attempted == 1 || \
      worker_create_attempted == 1 || probe_create_attempted == 1)); then
    uid_cleanup_start_proxy trial_kubectl "$run_dir" \
      "$run_dir/cleanup.log" || cleanup_failure=1
  fi

  uid_cleanup_group trial_kubectl "$namespace" "$cleanup" \
    semantic-probe "$run_dir/probe-create-response.json" \
    "$probe_create_attempted" 2 120s \
    "$run_dir/cleanup.log" "$resources_file" "$run_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$namespace" "$cleanup" \
    restore-worker "$run_dir/worker-create-response.json" \
    "$worker_create_attempted" 4 120s \
    "$run_dir/cleanup.log" "$resources_file" "$run_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$namespace" "$cleanup" \
    target "$run_dir/target-create-response.json" \
    "$target_create_attempted" 1 180s \
    "$run_dir/cleanup.log" "$resources_file" "$run_dir" || cleanup_failure=1
  uid_cleanup_group trial_kubectl "$namespace" "$cleanup" \
    target-support "$run_dir/target-support-create-response.json" \
    "$target_support_create_attempted" 4 120s \
    "$run_dir/cleanup.log" "$resources_file" "$run_dir" || cleanup_failure=1
  uid_cleanup_stop_proxy

  cleanup_completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  if ((cleanup == 0)); then
    cleanup_status="NOT_REQUESTED"
  elif ((cleanup_failure == 0)); then
    cleanup_status="PASS"
  else
    cleanup_status="FAIL"
  fi
  if jq -s \
    --arg schema "archvteams.nebius.ai/run-cleanup-receipt/v1" \
    --arg run_id "$run_id" \
    --arg status "$cleanup_status" \
    --arg started_at "$cleanup_started_at" \
    --arg completed_at "$cleanup_completed_at" \
    --argjson requested "$([[ $cleanup == 1 ]] && printf true || printf false)" \
    --argjson original_exit_code "$original_exit_code" \
    '{schema:$schema,run_id:$run_id,status:$status,requested:$requested,
      original_runner_exit_code:$original_exit_code,started_at:$started_at,
      completed_at:$completed_at,resources:.}' \
    "$resources_file" > "$receipt_partial"; then
    mv -- "$receipt_partial" "$run_dir/cleanup-receipt.json" || cleanup_failure=1
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
  cleanup_status=$(jq -r '.status' "$run_dir/cleanup-receipt.json" 2>/dev/null)
  [[ -n $cleanup_status && $cleanup_status != null ]] || cleanup_status="FAIL"
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  jq -n \
    --arg schema "archvteams.nebius.ai/runner-attempt-result/v1" \
    --arg run_id "$run_id" \
    --arg model "boltz2" \
    --arg completed_at "$completed_at" \
    --arg cleanup_status "$cleanup_status" \
    --argjson admitted "$([[ $trial_admitted == 1 ]] && printf true || printf false)" \
    --argjson original_exit_code "$original_exit_code" \
    --argjson final_exit_code "$final_exit_code" \
    '{schema:$schema,run_id:$run_id,model:$model,admitted:$admitted,
      completed_at:$completed_at,original_runner_exit_code:$original_exit_code,
      cleanup_status:$cleanup_status,final_exit_code:$final_exit_code}' \
    > "$run_dir/attempt-result.json" || final_exit_code=1
  if ((trial_admitted == 1)) && [[ -n $attempt_ledger ]]; then
    jq -nc \
      --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
      --arg event "completed" \
      --arg cohort_id "$cohort_id" \
      --arg model "boltz2" \
      --arg run_id "$run_id" \
      --arg completed_at "$completed_at" \
      --arg trial_dir "$run_dir" \
      --arg summary_path "$run_dir/trial-summary.json" \
      --arg cleanup_receipt_path "$run_dir/cleanup-receipt.json" \
      --arg cleanup_status "$cleanup_status" \
      --argjson attempt_index "$attempt_index" \
      --argjson runner_exit_code "$final_exit_code" \
      '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
        attempt_index:$attempt_index,run_id:$run_id,completed_at:$completed_at,
        trial_dir:$trial_dir,summary_path:$summary_path,
        cleanup_receipt_path:$cleanup_receipt_path,cleanup_status:$cleanup_status,
        runner_exit_code:$runner_exit_code}' \
      >> "$attempt_ledger" || final_exit_code=1
  fi
  exit "$final_exit_code"
}

mkdir -m 700 "$run_dir"
trap finalize_trial EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -n $instrumentation_contract_receipt ]]; then
  printf '%s\n' "$instrumentation_contract_receipt" \
    > "$run_dir/instrumentation-contract.json"
fi
jq -n --arg checked_at "$artifact_holder_checked_at" \
  --argjson pod "$artifact_holder_json" \
  --argjson mount_verification "$artifact_holder_mount_json" \
  '{schema:"archvteams.nebius.ai/warm-storage-holder-check/v1",
    checked_at:$checked_at,pod:$pod,mount_verifications:[$mount_verification]}' \
  > "$run_dir/artifact-holder.json"
jq -n --arg checked_at "$cache_holder_checked_at" \
  --argjson pod "$cache_holder_json" \
  --argjson mount_verification "$cache_holder_mount_json" \
  '{schema:"archvteams.nebius.ai/warm-storage-holder-check/v1",
    checked_at:$checked_at,pod:$pod,mount_verifications:[$mount_verification]}' \
  > "$run_dir/cache-holder.json"
jq -n --arg checked_at "$capture_agent_checked_at" \
  --argjson daemonset_list "$capture_agent_list" \
  '{schema:"archvteams.nebius.ai/capture-agent-absence/v1",
    checked_at:$checked_at,namespace:"nim-fast-start",
    forbidden_name:"archvteams-2407-native-snapshot-agent",
    daemonset_list:$daemonset_list,status:"PASS"}' \
  > "$run_dir/capture-agent-absence.json"
artifact_holder_uid=$(jq -er '.pod.metadata.uid' "$run_dir/artifact-holder.json")
cp "$contract_path" "$run_dir/restore-interface.json"
sha256sum "$run_dir/restore-interface.json" > "$run_dir/restore-interface.sha256"

demand=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -n \
  --arg run_id "$run_id" \
  --arg demand_at "$demand" \
  --arg node "$node" \
  --arg checkpoint_id "$checkpoint_id" \
  --arg artifact_manifest_sha256 "$artifact_manifest_sha256" \
  --arg artifact_pvc "$artifact_pvc" \
  --arg cache_pvc "$cache_pvc" \
  '{
    schema:"archvteams.nebius.ai/boltz2-faststart-run/v1",
    demand_at:$demand_at,
    run_id:$run_id,
    target_node:$node,
    checkpoint_id:$checkpoint_id,
    artifact_version:"1",
    artifact_manifest_sha256:$artifact_manifest_sha256,
    artifact_pvc:$artifact_pvc,
    cache_pvc:$cache_pvc
  }' > "$run_dir/run.json"

python3 "$code_dir/render.py" target \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" > "$run_dir/target.yaml"
python3 "$manifest_splitter" \
  --input "$run_dir/target.yaml" \
  --output-directory "$run_dir/target-bundle" \
  --bundle target > "$run_dir/target-bundle.json"
target_support_manifests=("$run_dir"/target-bundle/support/*.json)
[[ ${#target_support_manifests[@]} == 4 ]] || {
  printf 'target support bundle is incomplete\n' >&2
  exit 78
}
target_support_create_attempted=1
if ! uid_create_bundle trial_kubectl \
  "$run_dir/target-support-create-response.json" \
  "$run_dir/target-support-create.log" \
  "${target_support_manifests[@]}"; then
  printf 'target support creation failed\n' >&2
  exit 1
fi
capture_controller_clock_boundary cohort-admission \
  "$run_dir/admission-boundary.json"
trial_admitted_at=$(jq -er '.utc' "$run_dir/admission-boundary.json")
append_admission_event "$trial_admitted_at"
trial_admitted=1
# Primary T0 is conservative pre-dispatch.  The timestamp captured immediately
# after the JSON create response is a client-observed proxy, not server acceptance.
"${trial_kubectl[@]}" get pod "$artifact_holder" -o json \
  > "$run_dir/anchor-holder.json"
jq -e --arg uid "$artifact_holder_uid" --arg node "$node" \
  --arg image "$BOOT_TIME_ANCHOR_HOLDER_IMAGE" '
    .metadata.uid == $uid and .metadata.deletionTimestamp == null and
    .spec.nodeName == $node and .status.phase == "Running" and
    ([.status.conditions[] | select(.type == "Ready" and .status == "True")] | length) == 1 and
    ([.spec.containers[] | select(.name == "holder" and .image == $image)] | length) == 1 and
    ([.status.containerStatuses[] | select(.name == "holder" and .ready == true and
      .restartCount == 0 and .imageID == $image)] | length) == 1
  ' "$run_dir/anchor-holder.json" >/dev/null
capture_boot_time_anchor trial_kubectl "$artifact_holder" "$artifact_holder_uid" \
  "$node" holder "$run_dir/boot-time-anchor.json"
capture_controller_clock_boundary target-submit \
  "$run_dir/target-submit-clock.json"
jq -er '.utc' "$run_dir/target-submit-clock.json" \
  > "$run_dir/target-submit-at.txt"
target_create_attempted=1
"${trial_kubectl[@]}" create -f "$run_dir/target-bundle/primary.json" -o json \
  > "$run_dir/target-create-response.json" \
  2> "$run_dir/target-create.stderr"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/target-create-response-at.txt"
jq -e --arg name "$target" '
  select(.apiVersion == "v1" and .kind == "Pod")
  | select(.metadata.name == $name and .metadata.namespace == "nim-fast-start")
  | select(.metadata.uid | type == "string" and length > 0)
' "$run_dir/target-create-response.json" >/dev/null
kubectl --kubeconfig "$kubeconfig" -n "$namespace" wait \
  --for=jsonpath='{.status.containerStatuses[0].state.running}' \
  "pod/$target" --timeout=300s
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$target" -o json \
  > "$run_dir/target-before-binding.json"
target_uid=$(jq -er '.metadata.uid' "$run_dir/target-before-binding.json")

python3 "$code_dir/bind_target.py" \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --pod-json "$run_dir/target-before-binding.json" \
  --collected-at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  --binding-output "$run_dir/binding.json" \
  --patch-output "$run_dir/target-pod-spec.patch.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" patch pod "$target" \
  --type=json --patch-file="$run_dir/target-pod-spec.patch.json" -o json \
  > "$run_dir/target-patch-response.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$target" -o json \
  > "$run_dir/target-bound.json"

# Submit the external CPU client before the one-shot restore worker. It polls
# the run-scoped ClusterIP and records exactly two distinct semantic calls.
python3 "$code_dir/render.py" probe \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --binding "$run_dir/binding.json" > "$run_dir/semantic-probe.yaml"
python3 "$manifest_splitter" \
  --input "$run_dir/semantic-probe.yaml" \
  --output-directory "$run_dir/probe-bundle" \
  --bundle semantic-probe > "$run_dir/probe-bundle.json"
probe_support_manifests=("$run_dir"/probe-bundle/support/*.json)
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/probe-submit-at.txt"
probe_create_attempted=1
if ! uid_create_bundle trial_kubectl "$run_dir/probe-create-response.json" \
  "$run_dir/probe-create.log" "${probe_support_manifests[@]}" \
  "$run_dir/probe-bundle/primary.json"; then
  printf 'semantic probe creation failed\n' >&2
  exit 1
fi

python3 "$code_dir/render.py" restore \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --binding "$run_dir/binding.json" > "$run_dir/restore-worker.yaml"
python3 "$manifest_splitter" \
  --input "$run_dir/restore-worker.yaml" \
  --output-directory "$run_dir/worker-bundle" \
  --bundle restore-worker > "$run_dir/worker-bundle.json"
worker_support_manifests=("$run_dir"/worker-bundle/support/*.json)
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/worker-submit-at.txt"
worker_create_attempted=1
if ! uid_create_bundle trial_kubectl "$run_dir/worker-create-response.json" \
  "$run_dir/worker-create.log" "${worker_support_manifests[@]}" \
  "$run_dir/worker-bundle/primary.json"; then
  printf 'restore worker creation failed\n' >&2
  exit 1
fi

wait_for_job() {
  local name=$1
  local output=$2
  local attempt
  for ((attempt=0; attempt<900; attempt++)); do
    kubectl --kubeconfig "$kubeconfig" -n "$namespace" get job "$name" -o json > "$output"
    if jq -e '(.status.succeeded // 0) == 1' "$output" >/dev/null; then
      return 0
    fi
    if jq -e '(.status.failed // 0) > 0' "$output" >/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 124
}

if ! wait_for_job "$worker" "$run_dir/worker-job.json"; then
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pods -l "job-name=$worker" -o json \
    > "$run_dir/worker-pods.failed.json"
  exit 1
fi
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pods -l "job-name=$worker" -o json \
  > "$run_dir/worker-pods.json"
worker_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/worker-pods.json")
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$worker_pod" -o json \
  > "$run_dir/worker-pod.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" logs "$worker_pod" > "$run_dir/worker.log"
tail -1 "$run_dir/worker.log" | jq -e -c 'select(.status=="succeeded")' \
  > "$run_dir/worker-receipt.json"

kubectl --kubeconfig "$kubeconfig" -n "$namespace" wait \
  --for=condition=Ready "pod/$target" --timeout=300s
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$target" -o json \
  > "$run_dir/target-ready.json"

if ! wait_for_job "$probe" "$run_dir/probe-job.json"; then
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pods -l "job-name=$probe" -o json \
    > "$run_dir/probe-pods.failed.json"
  exit 1
fi
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pods -l "job-name=$probe" -o json \
  > "$run_dir/probe-pods.json"
probe_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/probe-pods.json")
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$probe_pod" -o json \
  > "$run_dir/probe-pod.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" logs "$probe_pod" > "$run_dir/semantic-probe.log"
tail -1 "$run_dir/semantic-probe.log" | jq -e -c \
  'select(.status=="PASS" and .ok==true and .passed_case_count==2 and .failed_case_count==0)' \
  > "$run_dir/semantic-summary.json"

kubectl --kubeconfig "$kubeconfig" -n "$namespace" get pod "$target" -o json \
  > "$run_dir/target-final.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get service "$canary" -o json \
  > "$run_dir/canary-service.json"
kubectl --kubeconfig "$kubeconfig" -n "$namespace" get endpointslices.discovery.k8s.io \
  -l "kubernetes.io/service-name=$canary" -o json > "$run_dir/canary-endpointslices.json"

target_uid=$(jq -er '.metadata.uid' "$run_dir/target-final.json")
target_image=$(jq -er '
  [.spec.containers[] | select(.name == "boltz2") | .image] |
  select(length == 1) | .[0]
' "$run_dir/target-final.json")
"${trial_kubectl[@]}" get events \
  --field-selector "involvedObject.uid=$target_uid" -o json \
  > "$run_dir/target-events.json"
"${trial_kubectl[@]}" exec "pod/$target" -c boltz2 -- \
  nvidia-smi -q -x > "$run_dir/target-nvidia-smi.xml" \
  2> "$run_dir/target-nvidia-smi.stderr"
python3 "$qualification_collector" \
  --model boltz2 \
  --run-id "$run_id" \
  --namespace "$namespace" \
  --target-name "$target" \
  --target-container boltz2 \
  --expected-image "$target_image" \
  --worker-container restore-worker \
  --probe-container semantic-probe \
  --target-submit-at "$run_dir/target-submit-at.txt" \
  --target-create-response-at "$run_dir/target-create-response-at.txt" \
  --target-create-response "$run_dir/target-create-response.json" \
  --target-pod "$run_dir/target-final.json" \
  --target-events "$run_dir/target-events.json" \
  --worker-pod "$run_dir/worker-pod.json" \
  --worker-receipt "$run_dir/worker-receipt.json" \
  --probe-pod "$run_dir/probe-pod.json" \
  --semantic-summary "$run_dir/semantic-summary.json" \
  --gpu-health-xml "$run_dir/target-nvidia-smi.xml" \
  --gpu-health-stderr "$run_dir/target-nvidia-smi.stderr" \
  --admission-boundary "$run_dir/admission-boundary.json" \
  --target-submit-clock "$run_dir/target-submit-clock.json" \
  --boot-time-anchor "$run_dir/boot-time-anchor.json" \
  --anchor-holder "$run_dir/anchor-holder.json" \
  --capture-agent-absence "$run_dir/capture-agent-absence.json" \
  > "$run_dir/qualification-receipt.json"

python3 - "$run_dir" "$code_dir/.." <<'PY' > "$run_dir/trial-summary.json"
import json
import sys
from pathlib import Path

directory = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from timing_evidence import build_timing_evidence

run = json.loads((directory / "run.json").read_text())
binding = json.loads((directory / "binding.json").read_text())
worker = json.loads((directory / "worker-receipt.json").read_text())
semantic = json.loads((directory / "semantic-summary.json").read_text())
target = json.loads((directory / "target-final.json").read_text())
qualification = json.loads((directory / "qualification-receipt.json").read_text())
target_submit_at = (directory / "target-submit-at.txt").read_text().strip()
target_create_response_at = (
    directory / "target-create-response-at.txt"
).read_text().strip()
timings = build_timing_evidence(
    run,
    semantic,
    target,
    target_submit_at=target_submit_at,
    target_create_response_at=target_create_response_at,
)
expected_qualification_target = {
    "namespace": binding["namespace"],
    "name": binding["pod_name"],
    "uid": binding["pod_uid"],
    "pod_spec_sha256": binding["pod_spec_sha256"],
    "image": target["spec"]["containers"][0]["image"],
}
if (
    qualification.get("schema")
    != "archvteams.nebius.ai/warm-instance-qualification/v3"
    or qualification.get("status") != "PASS"
    or qualification.get("model") != "boltz2"
    or qualification.get("run_id") != run["run_id"]
    or qualification.get("target") != expected_qualification_target
    or qualification.get("timing_boundaries", {})
    .get("acceptance_response_proxy", {})
    .get("is_exact_server_acceptance")
    is not False
):
    raise ValueError("qualification receipt does not match the Boltz2 trial")
conservative = qualification.get("boot_time_alignment", {}).get(
    "conservative_upper_bounds"
)
upper_sources = {
    "demand_to_http_ready_boottime_upper_seconds": "http_ready_complete_body",
    "demand_to_first_semantic_boottime_upper_seconds": (
        "first_semantic_response_complete_body"
    ),
    "demand_to_two_semantic_boottime_upper_seconds": (
        "two_semantic_responses_complete_body"
    ),
}
if not isinstance(conservative, dict):
    raise ValueError("qualification BOOTTIME upper bounds are missing")
upper_timings = {}
for output_key, source_key in upper_sources.items():
    item = conservative.get(source_key)
    value = item.get("upper_bound_seconds") if isinstance(item, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("qualification BOOTTIME upper bounds are malformed")
    upper_timings[output_key] = float(value)
result = {
    "schema": "archvteams.nebius.ai/boltz2-native-trial-summary/v1",
    "run_id": run["run_id"],
    "status": "PASS",
    "checkpoint_id": run["checkpoint_id"],
    "artifact_manifest_sha256": run["artifact_manifest_sha256"],
    "pod_uid": binding["pod_uid"],
    "pod_spec_sha256": binding["pod_spec_sha256"],
    "worker_receipt": worker,
    "semantic": semantic,
    "qualification": qualification,
    **upper_timings,
    **timings,
}
print(json.dumps(result, sort_keys=True, indent=2))
PY

jq '{run_id,status,demand_to_http_ready_seconds,demand_to_kubernetes_ready_seconds,
     semantic_request_1_seconds,semantic_request_2_seconds,demand_to_two_semantic_seconds,
     demand_to_http_ready_boottime_upper_seconds,
     demand_to_first_semantic_boottime_upper_seconds,
     demand_to_two_semantic_boottime_upper_seconds,
     target_create_api_round_trip_seconds,
     acceptance_response_proxy_to_two_semantic_seconds,
     restore_seconds:(.worker_receipt.duration_ms / 1000)}' \
  "$run_dir/trial-summary.json"
