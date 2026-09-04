#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
test_tmp=$(mktemp -d /tmp/openfold3-n3-runner-test.XXXXXX)
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
  --run-prefix of3-ut
  --evidence-root "$test_tmp/evidence"
  --node gpu-node-a.example.invalid
  --kubeconfig "$test_tmp/kubeconfig"
  --artifact-holder openfold3-native-f7-holder-hf93
  --checkpoint-id openfold3-native-f7-v1
  --target-glibc-version 2.35
  --image-io-mode direct
  --artifact-manifest-sha256 "$(printf 'a%.0s' {1..64})"
  --allow-performance-validation-worker
  --cleanup
)

"$test_tmp/lane/run_n3.sh" "${args[@]}" > "$test_tmp/stdout"
summary="$test_tmp/evidence/n3-of3-ut-direct.json"
[[ $(jq -r '.status' "$summary") == PASS ]]
[[ $(jq -r '.t0_source' "$summary") == target-submit-at.txt ]]
[[ $(jq -r '.trial_count' "$summary") == 3 ]]
[[ $(jq -r '.request_count' "$summary") == 6 ]]
[[ $(jq -r '.response_timing_contract' "$summary") == request-dispatch-to-complete-http-body/v1 ]]
[[ $(jq -r '.statistics_seconds.demand_to_two_semantic_median' "$summary") == 20.0 ]]
[[ $(jq -r '.statistics_seconds.demand_to_http_ready_median' "$summary") == 15.0 ]]
[[ $(jq -r '.statistics_seconds.demand_to_kubernetes_ready_median' "$summary") == 16.0 ]]
[[ $(jq -r '.statistics_seconds.semantic_request_1_median' "$summary") == 3.0 ]]
[[ $(jq -r '.statistics_seconds.semantic_request_2_median' "$summary") == 2.0 ]]
[[ $(jq -r '.statistics_seconds.worker_restore_median' "$summary") == 4.0 ]]
[[ $(wc -l < "$FAKE_N3_LOG") == 3 ]]
[[ $(cut -d' ' -f1 "$FAKE_N3_LOG" | paste -sd, -) == of3-ut-r1,of3-ut-r2,of3-ut-r3 ]]
[[ $(stat -c %a "$summary") == 600 ]]

calls_before=$(wc -l < "$FAKE_N3_LOG")
if "$test_tmp/lane/run_n3.sh" "${args[@]}" > /dev/null 2> "$test_tmp/duplicate.stderr"; then
  printf 'duplicate n=3 summary was accepted\n' >&2
  exit 1
fi
[[ $(wc -l < "$FAKE_N3_LOG") == "$calls_before" ]]
rg -q 'summary path already exists' "$test_tmp/duplicate.stderr"

stale_args=("${args[@]}")
stale_args[1]=of3-stale
if FAKE_N3_STALE_TOTAL=1 "$test_tmp/lane/run_n3.sh" "${stale_args[@]}" \
  > /dev/null 2> "$test_tmp/stale.stderr"; then
  printf 'stale validation-complete total was accepted\n' >&2
  exit 1
fi
rg -q 'trial total does not match response boundary' "$test_tmp/stale.stderr"
printf 'n=3 runner: PASS\n'
