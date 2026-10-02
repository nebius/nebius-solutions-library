#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
boltz_dir=$(dirname -- "$test_dir")
readonly boltz_dir
readonly runner="${boltz_dir}/run_one_native_trial.sh"
readonly fixture_bin="${boltz_dir}/../dynamo/tests/provisioned-fixtures/bin"
test_tmp=$(mktemp -d /tmp/boltz2-provisioned-runner-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

export PATH="${fixture_bin}:${PATH}"
export FAKE_CALL_LOG="${test_tmp}/calls.log"
export FAKE_EXPECTED_KUBECONFIG="${test_tmp}/kubeconfig.yaml"
export FAKE_NODE="gpu-node-a.example.invalid"
export FAKE_HOLDER_NAMES="boltz2-artifact-holder-example,boltz2-cache-holder-example"
export FAKE_SERVER="https://kubernetes-api.example.invalid:443"
export FAKE_HOLDER_READY=true
export FAKE_MODEL=boltz2
export EXPECTED_API_SERVER="$FAKE_SERVER"
export ALLOWED_H100_NODES="$FAKE_NODE"
export B2_ARTIFACT_PVC="boltz2-artifacts-example"
export B2_CACHE_PVC="boltz2-cache-example"

printf '%s\n' 'apiVersion: v1' 'kind: Config' > "$FAKE_EXPECTED_KUBECONFIG"
chmod 0600 "$FAKE_EXPECTED_KUBECONFIG"
mkdir -m 0700 "${test_tmp}/evidence" "${test_tmp}/evidence/runs"
: > "$FAKE_CALL_LOG"

pass_count=0
pass() { pass_count=$((pass_count + 1)); printf 'ok %d - %s\n' "$pass_count" "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }

args=(
  --evidence-root "${test_tmp}/evidence"
  --node "$FAKE_NODE"
  --kubeconfig "$FAKE_EXPECTED_KUBECONFIG"
  --artifact-holder boltz2-artifact-holder-example
  --cache-holder boltz2-cache-holder-example
)

if "$runner" > "${test_tmp}/missing.stdout" 2> "${test_tmp}/missing.stderr"; then
  fail "runner accepted missing arguments"
fi
[[ ! -s $FAKE_CALL_LOG ]] || fail "missing arguments reached kubectl"
pass "required parameterized CLI fails before Kubernetes"

: > "$FAKE_CALL_LOG"
"$runner" --run-id b2-fake-pass "${args[@]}" --cleanup \
  > "${test_tmp}/pass.stdout" 2> "${test_tmp}/pass.stderr" || {
  sed -n '1,160p' "${test_tmp}/pass.stderr" >&2
  fail "fake Boltz2 trial failed"
}
run_dir="${test_tmp}/evidence/runs/b2-fake-pass"
[[ $(/usr/bin/jq -r '.status' "$run_dir/trial-summary.json") == PASS ]] || \
  fail "Boltz2 summary is not PASS"
[[ -s $run_dir/target-create-response.json && -s $run_dir/target-create-response-at.txt ]] || \
  fail "create-response evidence is missing"
[[ $(/usr/bin/jq -r '.status' "$run_dir/cleanup-receipt.json") == PASS ]] || \
  fail "cleanup receipt is not PASS"
[[ $(rg -c '^delete:' "$FAKE_CALL_LOG") == 11 ]] || \
  fail "success cleanup did not issue exactly eleven UID-preconditioned deletes"
pass "successful Boltz2 trial retains dual timing and cleanup receipts"

: > "$FAKE_CALL_LOG"
export FAKE_FAIL_WORKER=true
if "$runner" --run-id b2-fake-fail "${args[@]}" --cleanup \
  > "${test_tmp}/fail.stdout" 2> "${test_tmp}/fail.stderr"; then
  fail "runner accepted failed worker"
fi
unset FAKE_FAIL_WORKER
failed_dir="${test_tmp}/evidence/runs/b2-fake-fail"
[[ $(/usr/bin/jq -r '.status' "$failed_dir/cleanup-receipt.json") == PASS ]] || \
  fail "worker failure cleanup was not PASS"
[[ $(/usr/bin/jq -r '.final_exit_code' "$failed_dir/attempt-result.json") != 0 ]] || \
  fail "worker failure result is not nonzero"
[[ $(rg -c '^delete:' "$FAKE_CALL_LOG") == 11 ]] || \
  fail "worker failure did not issue exactly eleven UID-preconditioned deletes"
pass "Boltz2 worker failure is cleaned and durably failed"

: > "$FAKE_CALL_LOG"
ledger="${test_tmp}/attempts.ndjson"
: > "$ledger"
instrumentation_contract_sha256=$(
  /usr/bin/python3 "$boltz_dir/../performance/instrumentation_contract.py" \
    --model boltz2 | /usr/bin/jq -er '.instrumentation_contract_sha256'
)
"$runner" --run-id b2-cohort-001 "${args[@]}" --cleanup \
  --cohort-id b2-fresh --attempt-index 1 --attempt-ledger "$ledger" \
  --instrumentation-contract-sha256 "$instrumentation_contract_sha256" \
  > "${test_tmp}/cohort.stdout" 2> "${test_tmp}/cohort.stderr" || {
  sed -n '1,160p' "${test_tmp}/cohort.stderr" >&2
  fail "fake cohort attempt failed"
}
[[ $(/usr/bin/jq -s '[.[] | .event] | join(",")' "$ledger") == '"admitted,completed"' ]] || \
  fail "cohort ledger does not contain exactly admitted/completed events"
[[ $(/usr/bin/jq -s -r '.[1].cleanup_status' "$ledger") == PASS ]] || \
  fail "cohort completion does not retain cleanup status"
[[ $(/usr/bin/jq -s -r '.[0].instrumentation_contract_sha256' "$ledger") == \
   "$instrumentation_contract_sha256" && \
   $(/usr/bin/jq -r '.instrumentation_contract_sha256' \
     "${test_tmp}/evidence/runs/b2-cohort-001/instrumentation-contract.json") == \
   "$instrumentation_contract_sha256" ]] || \
  fail "cohort attempt did not retain its exact instrumentation contract"
pass "Boltz2 runner appends one admitted/completed ledger pair"

printf '1..%d\n' "$pass_count"
