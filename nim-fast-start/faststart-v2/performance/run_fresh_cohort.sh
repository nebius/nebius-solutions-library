#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir

model=""
cohort_id=""
run_prefix=""
attempt_count=20
attempt_count_supplied=0
maximum_scheduled_attempts=""
evidence_root=""
kubeconfig=""
node=""
artifact_holder=""
cache_holder=""

usage() {
  cat >&2 <<'USAGE'
usage: run_fresh_cohort.sh \
  --model openfold2|boltz2 \
  --cohort-id DNS_LABEL \
  --run-prefix DNS_LABEL \
  --evidence-root ABSOLUTE_DIRECTORY \
  --kubeconfig ABSOLUTE_FILE \
  --node ALLOWED_H100_HOSTNAME \
  --artifact-holder READY_POD \
  [--cache-holder READY_POD] \
  [--attempt-count N_GE_20] [--maximum-scheduled-attempts N]

The driver always requests fail-closed run-scoped cleanup.  It never imports pre-existing
trial directories: only attempts admitted into its new append-only ledger are
eligible for aggregation.
USAGE
}

die_usage() { printf '%s\n' "$1" >&2; usage; exit 64; }
set_once() {
  local option_name=$1 current=$2 value=$3
  [[ -z $current ]] || die_usage "$option_name may be supplied only once"
  [[ -n $value ]] || die_usage "$option_name requires a value"
}

while (($# > 0)); do
  case "$1" in
    --model)
      (($# >= 2)) || die_usage "--model requires a value"
      set_once --model "$model" "$2"; model=$2; shift 2 ;;
    --cohort-id)
      (($# >= 2)) || die_usage "--cohort-id requires a value"
      set_once --cohort-id "$cohort_id" "$2"; cohort_id=$2; shift 2 ;;
    --run-prefix)
      (($# >= 2)) || die_usage "--run-prefix requires a value"
      set_once --run-prefix "$run_prefix" "$2"; run_prefix=$2; shift 2 ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once --evidence-root "$evidence_root" "$2"; evidence_root=$2; shift 2 ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once --kubeconfig "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
    --node)
      (($# >= 2)) || die_usage "--node requires a value"
      set_once --node "$node" "$2"; node=$2; shift 2 ;;
    --artifact-holder)
      (($# >= 2)) || die_usage "--artifact-holder requires a value"
      set_once --artifact-holder "$artifact_holder" "$2"; artifact_holder=$2; shift 2 ;;
    --cache-holder)
      (($# >= 2)) || die_usage "--cache-holder requires a value"
      set_once --cache-holder "$cache_holder" "$2"; cache_holder=$2; shift 2 ;;
    --attempt-count)
      (($# >= 2)) || die_usage "--attempt-count requires a value"
      ((attempt_count_supplied == 0)) || \
        die_usage "--attempt-count may be supplied only once"
      attempt_count_supplied=1
      attempt_count=$2; shift 2 ;;
    --maximum-scheduled-attempts)
      (($# >= 2)) || die_usage "--maximum-scheduled-attempts requires a value"
      set_once --maximum-scheduled-attempts "$maximum_scheduled_attempts" "$2"
      maximum_scheduled_attempts=$2; shift 2 ;;
    --help|-h)
      usage; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

[[ $model == openfold2 || $model == boltz2 ]] || \
  die_usage "--model must be openfold2 or boltz2"
[[ -n $cohort_id && -n $run_prefix && -n $evidence_root ]] || \
  die_usage "cohort ID, run prefix, and evidence root are required"
[[ -n $kubeconfig && -n $node && -n $artifact_holder ]] || \
  die_usage "kubeconfig, node, and artifact holder are required"
[[ $cohort_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#cohort_id} -le 40 ]] || \
  die_usage "--cohort-id must be a DNS label of at most 40 characters"
[[ $run_prefix =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#run_prefix} -le 25 ]] || \
  die_usage "--run-prefix must be a DNS label of at most 25 characters"
[[ $attempt_count =~ ^[0-9]+$ ]] || die_usage "--attempt-count must be an integer"
((attempt_count >= 20 && attempt_count <= 100)) || \
  die_usage "--attempt-count must be between 20 and 100"
if [[ -z $maximum_scheduled_attempts ]]; then
  maximum_scheduled_attempts=$((attempt_count + 10))
fi
[[ $maximum_scheduled_attempts =~ ^[0-9]+$ ]] || \
  die_usage "--maximum-scheduled-attempts must be an integer"
((maximum_scheduled_attempts >= attempt_count && maximum_scheduled_attempts <= 200)) || \
  die_usage "--maximum-scheduled-attempts must be >= attempt count and <= 200"
if [[ $model == boltz2 ]]; then
  [[ -n $cache_holder ]] || die_usage "Boltz2 requires --cache-holder"
elif [[ -n $cache_holder ]]; then
  die_usage "OpenFold2 does not accept --cache-holder"
fi
[[ $evidence_root == /* && -d $evidence_root && ! -L $evidence_root ]] || \
  die_usage "--evidence-root must be an existing absolute non-symlink directory"
[[ -d $evidence_root/runs && ! -L $evidence_root/runs ]] || \
  die_usage "--evidence-root/runs must be an existing non-symlink directory"
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || \
  die_usage "--kubeconfig must be an absolute regular non-symlink file"

for required_command in jq python3 sha256sum date install setsid kill; do
  command -v "$required_command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$required_command" >&2
    exit 69
  }
done

if [[ $model == openfold2 ]]; then
  runner="${script_dir}/../dynamo/run_provisioned_trial.sh"
else
  runner="${script_dir}/../boltz2-native/run_one_native_trial.sh"
fi
[[ -x $runner && ! -L $runner ]] || {
  printf 'model runner is not an executable regular file: %s\n' "$runner" >&2
  exit 69
}

cohort_root="$evidence_root/cohorts/$cohort_id"
if [[ -e $cohort_root || -L $cohort_root ]]; then
  printf 'cohort directory already exists: %s\n' "$cohort_root" >&2
  exit 73
fi
install -d -m 0700 -- "$evidence_root/cohorts"
mkdir -m 0700 -- "$cohort_root"
ledger="$cohort_root/attempts.ndjson"
install -m 0600 /dev/null "$ledger"
runner_sha256=$(sha256sum "$runner")
runner_sha256=${runner_sha256%% *}
instrumentation_receipt="$cohort_root/instrumentation-contract.json"
python3 "$script_dir/instrumentation_contract.py" --model "$model" \
  > "$instrumentation_receipt"
instrumentation_contract_sha256=$(jq -er \
  '.instrumentation_contract_sha256 | select(test("^[0-9a-f]{64}$"))' \
  "$instrumentation_receipt")
started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -nc \
  --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
  --arg event "cohort_started" \
  --arg cohort_id "$cohort_id" \
  --arg model "$model" \
  --arg run_prefix "$run_prefix" \
  --arg evidence_root "$evidence_root" \
  --arg started_at "$started_at" \
  --arg runner_sha256 "$runner_sha256" \
  --arg instrumentation_contract_sha256 "$instrumentation_contract_sha256" \
  --argjson requested_attempt_count "$attempt_count" \
  --argjson maximum_scheduled_attempts "$maximum_scheduled_attempts" \
  '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
    run_prefix:$run_prefix,evidence_root:$evidence_root,started_at:$started_at,
    runner_sha256:$runner_sha256,
    instrumentation_contract_sha256:$instrumentation_contract_sha256,
    requested_attempt_count:$requested_attempt_count,
    maximum_scheduled_attempts:$maximum_scheduled_attempts}' >> "$ledger"

admitted_count=0
scheduled_count=0
controller_abort=0
active_runner_pid=""
footer_written=0

append_controller_abort() {
  local reason=$1 observed_exit=${2:-255} run=${3:-controller}
  jq -nc \
    --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
    --arg event "controller_abort" \
    --arg cohort_id "$cohort_id" \
    --arg model "$model" \
    --arg run_id "$run" \
    --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
    --arg reason "$reason" \
    --argjson observed_runner_exit_code "$observed_exit" \
    '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
      run_id:$run_id,observed_at:$observed_at,reason:$reason,
      observed_runner_exit_code:$observed_runner_exit_code}' >> "$ledger"
}

write_footer() {
  ((footer_written == 0)) || return 0
  local finished_at
  finished_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
  admitted_count=$(jq -s '[.[] | select(.event == "admitted")] | length' "$ledger")
  jq -nc \
    --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
    --arg event "cohort_finished" \
    --arg cohort_id "$cohort_id" \
    --arg model "$model" \
    --arg finished_at "$finished_at" \
    --argjson requested_attempt_count "$attempt_count" \
    --argjson admitted_attempt_count "$admitted_count" \
    --argjson scheduled_attempt_count "$scheduled_count" \
    --argjson controller_abort "$([[ $controller_abort == 1 ]] && printf true || printf false)" \
    '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
      finished_at:$finished_at,requested_attempt_count:$requested_attempt_count,
      admitted_attempt_count:$admitted_attempt_count,
      scheduled_attempt_count:$scheduled_attempt_count,
      controller_abort:$controller_abort}' >> "$ledger"
  footer_written=1
}

controller_exit() {
  local exit_code=$?
  trap - EXIT
  trap '' INT TERM
  if [[ -n $active_runner_pid ]]; then
    kill -TERM -- "-$active_runner_pid" 2>/dev/null || true
    wait "$active_runner_pid" 2>/dev/null || true
    active_runner_pid=""
  fi
  if ((footer_written == 0)); then
    controller_abort=1
    append_controller_abort "controller exited before cohort footer" "$exit_code"
    write_footer || true
  fi
  exit "$exit_code"
}

controller_signal() {
  local signal_name=$1 exit_code=$2
  trap '' INT TERM
  controller_abort=1
  if [[ -n $active_runner_pid ]]; then
    kill -s "$signal_name" -- "-$active_runner_pid" 2>/dev/null || true
    wait "$active_runner_pid" 2>/dev/null || true
    active_runner_pid=""
  fi
  append_controller_abort "controller received ${signal_name}; active runner was signaled and reaped" "$exit_code"
  write_footer
  exit "$exit_code"
}

trap controller_exit EXIT
trap 'controller_signal INT 130' INT
trap 'controller_signal TERM 143' TERM

while ((admitted_count < attempt_count && scheduled_count < maximum_scheduled_attempts)); do
  scheduled_count=$((scheduled_count + 1))
  next_attempt_index=$((admitted_count + 1))
  printf -v run_id '%s-%03d' "$run_prefix" "$scheduled_count"
  common=(
    --run-id "$run_id"
    --evidence-root "$evidence_root"
    --node "$node"
    --kubeconfig "$kubeconfig"
    --artifact-holder "$artifact_holder"
    --cleanup
    --cohort-id "$cohort_id"
    --attempt-index "$next_attempt_index"
    --attempt-ledger "$ledger"
    --instrumentation-contract-sha256 "$instrumentation_contract_sha256"
  )
  if [[ $model == boltz2 ]]; then
    common+=(--cache-holder "$cache_holder")
  fi

  set +e
  setsid "$runner" "${common[@]}" \
    > "$cohort_root/${run_id}.stdout" \
    2> "$cohort_root/${run_id}.stderr" &
  active_runner_pid=$!
  wait "$active_runner_pid"
  runner_exit=$?
  active_runner_pid=""
  set -e
  if jq -s -e --arg run_id "$run_id" '
    any(.[]; .event == "admitted" and .run_id == $run_id)
  ' "$ledger" >/dev/null; then
    admitted_count=$((admitted_count + 1))
    admitted_trial_dir="$evidence_root/runs/$run_id"
    if ! jq -s -e --arg run_id "$run_id" --arg model "$model" \
      --arg trial_dir "$admitted_trial_dir" --argjson runner_exit "$runner_exit" '
        [.[] | select(.event == "completed" and .run_id == $run_id)] as $events |
        ($events | length) == 1 and
        $events[0].model == $model and
        $events[0].trial_dir == $trial_dir and
        $events[0].cleanup_receipt_path == ($trial_dir + "/cleanup-receipt.json") and
        $events[0].cleanup_status == "PASS" and
        $events[0].runner_exit_code == $runner_exit
      ' "$ledger" >/dev/null || \
      ! jq -e --arg run_id "$run_id" --arg model "$model" \
        --argjson runner_exit "$runner_exit" '
          .schema == "archvteams.nebius.ai/runner-attempt-result/v1" and
          .run_id == $run_id and .model == $model and .admitted == true and
          .cleanup_status == "PASS" and .final_exit_code == $runner_exit
        ' "$admitted_trial_dir/attempt-result.json" >/dev/null 2>&1 || \
      ! jq -e --arg run_id "$run_id" '
          .schema == "archvteams.nebius.ai/run-cleanup-receipt/v1" and
          .run_id == $run_id and .requested == true and .status == "PASS"
        ' "$admitted_trial_dir/cleanup-receipt.json" >/dev/null 2>&1; then
      append_controller_abort \
        "admitted runner returned without one internally consistent PASS cleanup" \
        "$runner_exit" "$run_id"
      controller_abort=1
      break
    fi
  else
    pre_admission_cleanup_safe=true
    rejected_trial_dir="$evidence_root/runs/$run_id"
    if [[ -e $rejected_trial_dir || -L $rejected_trial_dir ]]; then
      pre_admission_cleanup_safe=false
      if [[ -d $rejected_trial_dir && ! -L $rejected_trial_dir ]] && \
        jq -e --arg run_id "$run_id" --arg model "$model" \
          --argjson runner_exit "$runner_exit" '
            .schema == "archvteams.nebius.ai/runner-attempt-result/v1" and
            .run_id == $run_id and .model == $model and .admitted == false and
            .cleanup_status == "PASS" and .final_exit_code == $runner_exit
          ' "$rejected_trial_dir/attempt-result.json" >/dev/null 2>&1 && \
        jq -e --arg run_id "$run_id" '
            .schema == "archvteams.nebius.ai/run-cleanup-receipt/v1" and
            .run_id == $run_id and .requested == true and .status == "PASS"
          ' "$rejected_trial_dir/cleanup-receipt.json" >/dev/null 2>&1; then
        pre_admission_cleanup_safe=true
      fi
    fi
    jq -nc \
      --arg schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
      --arg event "pre_admission_rejection" \
      --arg cohort_id "$cohort_id" \
      --arg model "$model" \
      --arg run_id "$run_id" \
      --arg observed_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
      --argjson observed_runner_exit_code "$runner_exit" \
      '{schema:$schema,event:$event,cohort_id:$cohort_id,model:$model,
        run_id:$run_id,observed_at:$observed_at,
        observed_runner_exit_code:$observed_runner_exit_code}' >> "$ledger"
    if [[ $pre_admission_cleanup_safe != true ]]; then
      append_controller_abort "pre-admission attempt has no proven PASS cleanup" \
        "$runner_exit" "$run_id"
      controller_abort=1
      break
    fi
  fi
done

write_footer

if ((admitted_count < attempt_count || controller_abort == 1)); then
  printf 'fresh cohort stopped with %d/%d admitted attempts\n' \
    "$admitted_count" "$attempt_count" >&2
  exit 1
fi

python3 "$script_dir/aggregate_fresh_cohort.py" \
  --model "$model" \
  --ledger "$ledger" \
  --output "$cohort_root/aggregate.json"
