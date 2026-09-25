#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
dynamo_dir=$(dirname -- "$test_dir")
readonly dynamo_dir
readonly runner="${dynamo_dir}/run_provisioned_trial.sh"
readonly fixture_bin="${test_dir}/provisioned-fixtures/bin"
test_tmp=$(mktemp -d /tmp/openfold2-provisioned-runner-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

export PATH="${fixture_bin}:${PATH}"
export FAKE_CALL_LOG="${test_tmp}/calls.log"
export FAKE_EXPECTED_KUBECONFIG="${test_tmp}/kubeconfig.yaml"
export FAKE_NODE="gpu-node-b.example.invalid"
export FAKE_HOLDER_NAME="of2-artifact-holder-t12"
export FAKE_SERVER="https://kubernetes-api.example.invalid:443"
export FAKE_HOLDER_READY=true
export OF2_ARTIFACT_PVC="openfold2-artifacts-example"
export OF2_CACHE_PVC="openfold2-cache-example"
export ALLOWED_H100_NODES="$FAKE_NODE"

printf '%s\n' 'apiVersion: v1' 'kind: Config' > "$FAKE_EXPECTED_KUBECONFIG"
chmod 0600 "$FAKE_EXPECTED_KUBECONFIG"
mkdir -m 0700 "${test_tmp}/evidence"
mkdir -m 0700 "${test_tmp}/evidence/runs"
: > "$FAKE_CALL_LOG"

pass_count=0
pass() {
  pass_count=$((pass_count + 1))
  printf 'ok %d - %s\n' "$pass_count" "$1"
}
fail() {
  printf 'not ok - %s\n' "$1" >&2
  exit 1
}

runner_args=(
  --evidence-root "${test_tmp}/evidence"
  --node "$FAKE_NODE"
  --kubeconfig "$FAKE_EXPECTED_KUBECONFIG"
  --artifact-holder "$FAKE_HOLDER_NAME"
)

if "$runner" > "${test_tmp}/missing.stdout" 2> "${test_tmp}/missing.stderr"; then
  fail "runner accepted missing required arguments"
fi
[[ ! -s $FAKE_CALL_LOG ]] || fail "missing arguments reached kubectl"
pass "required CLI arguments fail before any Kubernetes call"

: > "$FAKE_CALL_LOG"
export FAKE_SERVER="https://wrong.example.invalid:443"
if "$runner" --run-id wrong-server "${runner_args[@]}" \
  > "${test_tmp}/wrong-server.stdout" 2> "${test_tmp}/wrong-server.stderr"; then
  fail "runner accepted the wrong API server"
fi
[[ $(<"$FAKE_CALL_LOG") == "config-view" ]] || fail "wrong-server guard made extra calls"
[[ ! -e ${test_tmp}/evidence/runs/wrong-server ]] || fail "wrong-server guard created evidence"
pass "exact API-server guard runs before holder lookup or evidence creation"

: > "$FAKE_CALL_LOG"
export FAKE_SERVER="https://kubernetes-api.example.invalid:443"
export FAKE_HOLDER_READY=false
if "$runner" --run-id holder-not-ready "${runner_args[@]}" \
  > "${test_tmp}/holder.stdout" 2> "${test_tmp}/holder.stderr"; then
  fail "runner accepted a non-Ready artifact holder"
fi
[[ $(<"$FAKE_CALL_LOG") == $'config-view\nholder-get' ]] || fail "holder precondition made extra calls"
[[ ! -e ${test_tmp}/evidence/runs/holder-not-ready ]] || fail "holder failure created evidence"
pass "Ready same-node artifact holder is a pre-mutation condition"

for capture_mode in error present; do
  : > "$FAKE_CALL_LOG"
  if [[ $capture_mode == error ]]; then
    export FAKE_DAEMONSET_ERROR=true
  else
    export FAKE_CAPTURE_AGENT_PRESENT=true
  fi
  if "$runner" --run-id "capture-agent-${capture_mode}" "${runner_args[@]}" \
    > "${test_tmp}/capture-${capture_mode}.stdout" \
    2> "${test_tmp}/capture-${capture_mode}.stderr"; then
    fail "runner accepted capture-agent ${capture_mode} state"
  fi
  unset FAKE_DAEMONSET_ERROR FAKE_CAPTURE_AGENT_PRESENT
  [[ ! -e ${test_tmp}/evidence/runs/capture-agent-${capture_mode} ]] || \
    fail "capture-agent precondition created run evidence"
  if rg '^create:' "$FAKE_CALL_LOG" >/dev/null; then
    fail "capture-agent precondition reached object creation"
  fi
done
pass "capture-agent absence is fail-closed and proven before mutation"

: > "$FAKE_CALL_LOG"
export FAKE_HOLDER_READY=true
"$runner" --run-id fake-early-probe "${runner_args[@]}" --cleanup \
  > "${test_tmp}/success.stdout" 2> "${test_tmp}/success.stderr" || {
    sed -n '1,160p' "${test_tmp}/success.stderr" >&2
    fail "fake provisioned-node trial failed"
  }

run_dir="${test_tmp}/evidence/runs/fake-early-probe"
[[ $(/usr/bin/jq -r '.status' "$run_dir/canary-evidence.json") == "PASS" ]] || \
  fail "derived evidence is not PASS"
[[ $(/usr/bin/jq -r '.semantic_pass_count' "$run_dir/canary-evidence.json") == "2" ]] || \
  fail "derived evidence does not contain two semantic passes"
[[ -s $run_dir/target-create-response.json && -s $run_dir/target-create-response-at.txt ]] || \
  fail "client create-response receipt/timestamp is absent"
[[ $(/usr/bin/jq -r '.status' "$run_dir/qualification-receipt.json") == PASS ]] || \
  fail "warm-instance qualification receipt is not PASS"
[[ $(sha256sum "$run_dir/restore-interface.json" | cut -d' ' -f1) == \
   $(sha256sum "$dynamo_dir/restore-interface.live.json" | cut -d' ' -f1) ]] || \
  fail "run did not retain the exact immutable contract"

probe_create_line=$(rg -n '^create:semantic-probe.yaml$' "$FAKE_CALL_LOG" | cut -d: -f1)
worker_create_line=$(rg -n '^create:restore-worker.yaml$' "$FAKE_CALL_LOG" | cut -d: -f1)
ready_wait_line=$(rg -n '^wait-target-ready$' "$FAKE_CALL_LOG" | cut -d: -f1)
[[ -n $probe_create_line && -n $worker_create_line && -n $ready_wait_line ]] || \
  fail "fake call log lacks early-probe phases"
((probe_create_line < worker_create_line)) || fail "probe was not created before restore worker"
((probe_create_line < ready_wait_line)) || fail "probe was not created before target Ready wait"
[[ $(rg -c '^python-evidence$' "$FAKE_CALL_LOG") == "1" ]] || \
  fail "evidence was not generated exactly once"
pass "probe creation overlaps restore and evidence is generated once"

expected_cleanup=$'delete:semantic-probe.yaml\ndelete:support-ConfigMap\ndelete:restore-worker.yaml\ndelete:support-RoleBinding\ndelete:support-Role\ndelete:support-ServiceAccount\ndelete:target.yaml\ndelete:support-NetworkPolicy\ndelete:support-NetworkPolicy\ndelete:support-Service\ndelete:support-Service'
actual_cleanup=$(rg '^delete:' "$FAKE_CALL_LOG")
[[ $actual_cleanup == "$expected_cleanup" ]] || fail "cleanup was not exact and run-scoped"
[[ -s $run_dir/cleanup.log ]] || fail "cleanup output was not retained"
[[ $(/usr/bin/jq -r '.status' "$run_dir/cleanup-receipt.json") == PASS ]] || \
  fail "cleanup receipt is not PASS"
[[ $(/usr/bin/jq '[.resources[] | select(.delete_attempted == true and
  .uid_precondition_enforced == true and .expected_uid == .observed_uid_before_delete)]
  | length' "$run_dir/cleanup-receipt.json") == 11 ]] || \
  fail "cleanup did not UID-precondition every rendered object"
pass "cleanup trap UID-preconditions all eleven rendered objects and persists a receipt"

: > "$FAKE_CALL_LOG"
export FAKE_FAIL_WORKER=true
if "$runner" --run-id fake-worker-failure "${runner_args[@]}" --cleanup \
  > "${test_tmp}/worker-failure.stdout" 2> "${test_tmp}/worker-failure.stderr"; then
  fail "runner accepted a failed restore worker"
fi
unset FAKE_FAIL_WORKER
failed_run_dir="${test_tmp}/evidence/runs/fake-worker-failure"
[[ $(/usr/bin/jq -r '.status' "$failed_run_dir/cleanup-receipt.json") == PASS ]] || \
  fail "failure-path cleanup receipt is not PASS"
[[ $(/usr/bin/jq -r '.final_exit_code' "$failed_run_dir/attempt-result.json") != 0 ]] || \
  fail "failed runner attempt was recorded as success"
actual_failure_cleanup=$(rg '^delete:' "$FAKE_CALL_LOG")
[[ $actual_failure_cleanup == "$expected_cleanup" ]] || \
  fail "worker failure did not trigger exact run-scoped cleanup"
pass "worker failure still triggers fail-closed cleanup and a durable attempt result"

: > "$FAKE_CALL_LOG"
export FAKE_CREATE_FAIL_NUMBER=2
if "$runner" --run-id fake-partial-create "${runner_args[@]}" --cleanup \
  > "${test_tmp}/partial-create.stdout" 2> "${test_tmp}/partial-create.stderr"; then
  fail "runner accepted a partial support-bundle create"
fi
unset FAKE_CREATE_FAIL_NUMBER
partial_dir="${test_tmp}/evidence/runs/fake-partial-create"
[[ $(/usr/bin/jq -r '.status' "$partial_dir/cleanup-receipt.json") == FAIL ]] || \
  fail "partial create did not retain a FAIL cleanup receipt"
[[ $(/usr/bin/jq '[.resources[] | select(.status == "uid-receipt-incomplete")] | length' \
  "$partial_dir/cleanup-receipt.json") == 1 ]] || \
  fail "partial create did not retain its incomplete UID group receipt"
[[ $(/usr/bin/jq '[.resources[] | select(.status == "uid-precondition-deleted")] | length' \
  "$partial_dir/cleanup-receipt.json") == 2 ]] || \
  fail "partial create did not delete every successfully created object"
[[ $(/usr/bin/jq -r '.admitted' "$partial_dir/attempt-result.json") == false && \
   $(/usr/bin/jq -r '.final_exit_code' "$partial_dir/attempt-result.json") != 0 ]] || \
  fail "partial pre-admission create was not durably failed"
pass "partial bundle creation deletes every captured UID and fails closed"

: > "$FAKE_CALL_LOG"
export FAKE_CLEANUP_UID_MISMATCH=true
if "$runner" --run-id fake-uid-mismatch "${runner_args[@]}" --cleanup \
  > "${test_tmp}/uid-mismatch.stdout" 2> "${test_tmp}/uid-mismatch.stderr"; then
  fail "runner accepted a foreign target UID during cleanup"
fi
unset FAKE_CLEANUP_UID_MISMATCH
mismatch_dir="${test_tmp}/evidence/runs/fake-uid-mismatch"
[[ $(/usr/bin/jq -r '.status' "$mismatch_dir/cleanup-receipt.json") == FAIL ]] || \
  fail "UID mismatch did not fail cleanup"
[[ $(/usr/bin/jq '[.resources[] | select(.resource_kind == "pod" and
  .status == "uid-mismatch-preserved" and .delete_attempted == false)] | length' \
  "$mismatch_dir/cleanup-receipt.json") == 1 ]] || \
  fail "foreign UID was not explicitly preserved"
[[ $(rg -c '^delete:' "$FAKE_CALL_LOG") == 10 ]] || \
  fail "UID mismatch did not clean the ten unambiguous resources"
pass "foreign replacement UID is preserved and makes the final attempt fail"

: > "$FAKE_CALL_LOG"
export FAKE_DELETE_FAILURE_PATTERN='jobs/of2-semantic-'
if "$runner" --run-id fake-delete-failure "${runner_args[@]}" --cleanup \
  > "${test_tmp}/delete-failure.stdout" 2> "${test_tmp}/delete-failure.stderr"; then
  fail "runner accepted a UID-preconditioned API delete failure"
fi
unset FAKE_DELETE_FAILURE_PATTERN
delete_failure_dir="${test_tmp}/evidence/runs/fake-delete-failure"
[[ $(/usr/bin/jq -r '.status' "$delete_failure_dir/cleanup-receipt.json") == FAIL ]] || \
  fail "API delete failure did not persist a FAIL cleanup receipt"
[[ $(/usr/bin/jq '[.resources[] | select(.status == "uid-delete-failed" and
  .delete_attempted == true and .uid_precondition_enforced == true and
  .delete_exit_code != 0)] | length' "$delete_failure_dir/cleanup-receipt.json") == 1 ]] || \
  fail "API delete failure lost its server-side UID-precondition receipt"
[[ $(rg -c '^delete:' "$FAKE_CALL_LOG") == 10 && \
   $(rg -c '^delete-failed:' "$FAKE_CALL_LOG") == 1 ]] || \
  fail "API delete failure did not continue cleaning other resources"
pass "DELETE-body failure is retained and cleanup continues fail-closed"

[[ $(stat -c %a "$run_dir") == "700" ]] || fail "run directory is not private"
while IFS= read -r evidence_file; do
  [[ $(stat -c %a "$evidence_file") == "600" ]] || \
    fail "evidence file is not private: $evidence_file"
done < <(find "$run_dir" -type f -print)
pass "new evidence directory and files use private modes"

: > "$FAKE_CALL_LOG"
"$runner" --run-id fake-retained "${runner_args[@]}" \
  > "${test_tmp}/retained.stdout" 2> "${test_tmp}/retained.stderr" || {
    sed -n '1,160p' "${test_tmp}/retained.stderr" >&2
    fail "fake retained-resource trial failed"
  }
if rg '^delete:' "$FAKE_CALL_LOG" >/dev/null; then
  fail "runner cleaned resources without --cleanup"
fi
[[ -d ${test_tmp}/evidence/runs/fake-retained ]] || fail "retained run evidence is absent"
pass "omitting --cleanup retains the run without issuing deletes"

call_count_before=$(wc -l < "$FAKE_CALL_LOG")
if "$runner" --run-id fake-retained "${runner_args[@]}" \
  > "${test_tmp}/duplicate.stdout" 2> "${test_tmp}/duplicate.stderr"; then
  fail "runner reused an existing evidence directory"
fi
call_count_after=$(wc -l < "$FAKE_CALL_LOG")
[[ $call_count_after == "$call_count_before" ]] || fail "duplicate run reached Kubernetes"
pass "existing run directory is never reused or overwritten"

printf '1..%d\n' "$pass_count"
