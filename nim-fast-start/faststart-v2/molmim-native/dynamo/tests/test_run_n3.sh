#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
test_tmp=$(mktemp -d /tmp/molmim-n3-runner-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

install -d -m 0700 "$test_tmp/lane" "$test_tmp/evidence/runs"
cp -- "$test_dir/../run_n3.sh" "$test_tmp/lane/run_n3.sh"
cp -- "$test_dir/provisioned-fixtures/bin/fake_n3_trial_runner" \
  "$test_tmp/lane/run_provisioned_trial.sh"
chmod +x "$test_tmp/lane/run_n3.sh" "$test_tmp/lane/run_provisioned_trial.sh"
export FAKE_N3_LOG="$test_tmp/calls.log"
: > "$FAKE_N3_LOG"
printf 'apiVersion: v1\nkind: Config\n' > "$test_tmp/kubeconfig"

args=(
  --run-prefix molmim-ut
  --evidence-root "$test_tmp/evidence"
  --node gpu-node-b.example.invalid
  --kubeconfig "$test_tmp/kubeconfig"
  --artifact-holder molmim-native-f7-holder-t12
  --checkpoint-id molmim-native-f7-v1
  --target-glibc-version 2.35
  --image-io-mode direct
  --artifact-manifest-sha256 "$(printf 'a%.0s' {1..64})"
  --allow-performance-validation-worker
  --cleanup
)

args_without_cleanup=()
for argument in "${args[@]}"; do
  [[ $argument == --cleanup ]] || args_without_cleanup+=("$argument")
done
if "$test_tmp/lane/run_n3.sh" "${args_without_cleanup[@]}" \
  > /dev/null 2> "$test_tmp/missing-cleanup.stderr"; then
  printf 'n=3 runner accepted trials without cleanup\n' >&2
  exit 1
fi
rg -q -- '--cleanup is required between every counted n=3 trial' \
  "$test_tmp/missing-cleanup.stderr"
[[ $(wc -l < "$FAKE_N3_LOG") == 0 ]]

"$test_tmp/lane/run_n3.sh" "${args[@]}" > "$test_tmp/stdout"
summary="$test_tmp/evidence/n3-molmim-ut-direct.json"
[[ $(jq -r '.status' "$summary") == PASS ]]
[[ $(jq -r '.trial_count' "$summary") == 3 ]]
[[ $(jq -r '.request_count' "$summary") == 6 ]]
[[ $(jq -r '.statistics_seconds.demand_to_two_semantic_median' "$summary") == 20.0 ]]
[[ $(jq -r '.statistics_seconds.worker_restore_median' "$summary") == 4.0 ]]
[[ $(jq -r '.statistics_seconds.demand_to_http_ready_median' "$summary") == 15.0 ]]
[[ $(jq -r '.statistics_seconds.semantic_request_1_median' "$summary") == 3.0 ]]
[[ $(jq -r '.statistics_seconds.semantic_request_2_median' "$summary") == 2.0 ]]
[[ $(wc -l < "$FAKE_N3_LOG") == 3 ]]
[[ $(cut -d' ' -f1 "$FAKE_N3_LOG" | paste -sd, -) == molmim-ut-r1,molmim-ut-r2,molmim-ut-r3 ]]
[[ $(stat -c %a "$summary") == 600 ]]

calls_before=$(wc -l < "$FAKE_N3_LOG")
if "$test_tmp/lane/run_n3.sh" "${args[@]}" > /dev/null 2> "$test_tmp/duplicate.stderr"; then
  printf 'duplicate n=3 summary was accepted\n' >&2
  exit 1
fi
[[ $(wc -l < "$FAKE_N3_LOG") == "$calls_before" ]]
rg -q 'summary path already exists' "$test_tmp/duplicate.stderr"

run_failure_case() {
  local label=$1 prefix=$2 expected=$3
  local root="$test_tmp/${label}-evidence"
  local index
  local -a case_args=("${args[@]}")
  install -d -m 0700 "$root/runs"
  for ((index = 0; index < ${#case_args[@]}; index++)); do
    case "${case_args[$index]}" in
      --run-prefix) case_args[index + 1]=$prefix ;;
      --evidence-root) case_args[index + 1]=$root ;;
    esac
  done
  if "$test_tmp/lane/run_n3.sh" "${case_args[@]}" \
    > /dev/null 2> "$test_tmp/${label}.stderr"; then
    printf '%s n=3 evidence was accepted\n' "$label" >&2
    exit 1
  fi
  rg -q "$expected" "$test_tmp/${label}.stderr"
  [[ ! -e "$root/n3-${prefix}-direct.json" ]]
}

export FAKE_N3_RECEIPT_RUN_ID=molmim-replay-r1
run_failure_case replay molmim-replay 'identity-bound strict two-call PASS'
unset FAKE_N3_RECEIPT_RUN_ID

export FAKE_N3_NONFINITE_RUN_ID=molmim-nan-r2
run_failure_case nonfinite molmim-nan 'positive and finite'
unset FAKE_N3_NONFINITE_RUN_ID

printf 'n=3 runner: PASS\n'
