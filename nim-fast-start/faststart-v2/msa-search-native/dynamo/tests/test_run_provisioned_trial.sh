#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
source_dynamo_dir=$(dirname -- "$test_dir")
readonly source_dynamo_dir
readonly source_runner="${source_dynamo_dir}/run_provisioned_trial.sh"
readonly fixture_bin="${test_dir}/provisioned-fixtures/bin"
test_tmp=$(mktemp -d /tmp/msa-search-provisioned-runner-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

install -d -m 0700 "${test_tmp}/lane/fixtures"
cp -a -- "$source_dynamo_dir" "${test_tmp}/lane/dynamo"
cp -- "${source_dynamo_dir}/../validate_msa_search.py" "${test_tmp}/lane/validate_msa_search.py"
cp -- "${source_dynamo_dir}/../verify_mmseqs_pipe.py" "${test_tmp}/lane/verify_mmseqs_pipe.py"
cp -- "${source_dynamo_dir}/../fixtures/request-pdb70.json" "${test_tmp}/lane/fixtures/request-pdb70.json"
/usr/bin/jq '
  .release_ready=true |
  .release_blocker="" |
  .worker_classification="full-agent-compliance-release"
' \
  "${source_dynamo_dir}/restore-interface.live.json" \
  > "${test_tmp}/lane/dynamo/restore-interface.live.json"
dynamo_dir="${test_tmp}/lane/dynamo"
readonly dynamo_dir
readonly runner="${dynamo_dir}/run_provisioned_trial.sh"

export PATH="${fixture_bin}:${PATH}"
export FAKE_CALL_LOG="${test_tmp}/calls.log"
export FAKE_EXPECTED_KUBECONFIG="${test_tmp}/kubeconfig.yaml"
export FAKE_NODE="gpu-node-a.example.invalid"
export FAKE_HOLDER_NAME="msa-search-native-f7-holder-root-hf93"
export FAKE_SERVER="https://kubernetes-api.example.invalid:443"
export FAKE_HOLDER_READY=true
export FAKE_PIPE_OK=true

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
  --checkpoint-id msa-search-native-f7-v1
  --target-glibc-version 2.39
  --image-io-mode direct
  --artifact-manifest-sha256 "$(printf 'a%.0s' {1..64})"
)

if "$runner" > "${test_tmp}/missing.stdout" 2> "${test_tmp}/missing.stderr"; then
  fail "runner accepted missing required arguments"
fi
[[ ! -s $FAKE_CALL_LOG ]] || fail "missing arguments reached kubectl"
pass "required CLI arguments fail before any Kubernetes call"

: > "$FAKE_CALL_LOG"
if "$source_runner" --run-id unreleased "${runner_args[@]}" \
  > "${test_tmp}/unreleased.stdout" 2> "${test_tmp}/unreleased.stderr"; then
  fail "shipped candidate contract passed its release gate"
fi
[[ ! -s $FAKE_CALL_LOG ]] || fail "unreleased contract reached kubectl"
rg -q 'not deployable' "${test_tmp}/unreleased.stderr" || \
  fail "release-gate refusal was not explicit"
pass "shipped performance-validation candidate is blocked before any Kubernetes call"

: > "$FAKE_CALL_LOG"
"$source_runner" --run-id acknowledged-performance "${runner_args[@]}" \
  --allow-performance-validation-worker --cleanup \
  > "${test_tmp}/acknowledged.stdout" 2> "${test_tmp}/acknowledged.stderr" || {
    sed -n '1,160p' "${test_tmp}/acknowledged.stderr" >&2
    fail "explicitly acknowledged performance-validation run failed"
  }
acknowledged_dir="${test_tmp}/evidence/runs/acknowledged-performance"
[[ $(/usr/bin/jq -r '.release_ready' "$acknowledged_dir/restore-interface.json") == "false" ]] || \
  fail "acknowledged run did not retain release_ready=false"
[[ $(/usr/bin/jq -r '.worker_classification' "$acknowledged_dir/restore-interface.json") == \
   "performance-validation-only" ]] || \
  fail "acknowledged run did not retain the performance-only classification"
pass "explicit acknowledgement runs only the pinned performance-validation contract"

bad_glibc_args=("${runner_args[@]}")
for ((argument_index = 0; argument_index < ${#bad_glibc_args[@]}; argument_index++)); do
  if [[ ${bad_glibc_args[$argument_index]} == "--target-glibc-version" ]]; then
    bad_glibc_args[argument_index + 1]=2.35.1
  fi
done
: > "$FAKE_CALL_LOG"
if "$runner" --run-id malformed-glibc "${bad_glibc_args[@]}" \
  > "${test_tmp}/bad-glibc.stdout" 2> "${test_tmp}/bad-glibc.stderr"; then
  fail "runner accepted a noncanonical glibc receipt"
fi
[[ ! -s $FAKE_CALL_LOG ]] || fail "glibc receipt guard reached kubectl"
rg -q 'canonical MAJOR.MINOR' "${test_tmp}/bad-glibc.stderr" || \
  fail "glibc receipt refusal was not explicit"
pass "malformed target glibc receipt fails before Kubernetes"

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

: > "$FAKE_CALL_LOG"
export FAKE_HOLDER_READY=true
export FAKE_PIPE_OK=false
if "$runner" --run-id pipe-disconnected "${runner_args[@]}" \
  > "${test_tmp}/pipe.stdout" 2> "${test_tmp}/pipe.stderr"; then
  fail "runner accepted a disconnected MMseqs/API pipe"
fi
[[ ! -e ${test_tmp}/evidence/runs/pipe-disconnected/canary-evidence.json ]] || \
  fail "pipe failure produced PASS evidence"
[[ $(/usr/bin/jq -r '.shared_pipe_verified' \
  "${test_tmp}/evidence/runs/pipe-disconnected/mmseqs-pipe.log") == "false" ]] || \
  fail "pipe failure receipt was not retained"
pass "disconnected MMseqs/API pipe blocks PASS evidence"

: > "$FAKE_CALL_LOG"
export FAKE_PIPE_OK=true
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
[[ $(/usr/bin/jq -r '.mmseqs_pipe.shared_pipe_verified' "$run_dir/canary-evidence.json") == "true" ]] || \
  fail "derived evidence does not contain the MMseqs pipe PASS"
[[ $(/usr/bin/jq -r '.mmseqs_fd' "$run_dir/mmseqs-pipe-receipt.json") == "1" ]] || \
  fail "MMseqs pipe receipt does not bind fd 1"
[[ $(/usr/bin/jq -r '.api_worker_fd' "$run_dir/mmseqs-pipe-receipt.json") == "24" ]] || \
  fail "MMseqs pipe receipt does not bind API worker fd 24"
[[ $(/usr/bin/jq -r '.checkpoint_id' "$run_dir/run.json") == "msa-search-native-f7-v1" ]] || \
  fail "runner did not retain the selected checkpoint identity"
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
[[ $(rg -c '^exec-pipe-check:msa-target-fake-early-probe$' "$FAKE_CALL_LOG") == "1" ]] || \
  fail "target namespace pipe check was not executed exactly once"
[[ $(rg -c '^python-evidence$' "$FAKE_CALL_LOG") == "1" ]] || \
  fail "evidence was not generated exactly once"
[[ -s $run_dir/target-submit-at.txt ]] || fail "submit-edge timestamp was not retained"
target_submit_line=$(rg -n '^create:target.yaml$' "$FAKE_CALL_LOG" | cut -d: -f1)
[[ -n $target_submit_line ]] || fail "target create was not logged"
rg -q -- '--target-submit-at' "$runner" || \
  fail "evidence collector does not receive the submit-edge timestamp"
source_submit_line=$(rg -n 'target-submit-at\.txt"$' "$source_runner" | cut -d: -f1)
source_create_line=$(rg -n '^"\$\{trial_kubectl\[@\]\}" create -f "\$trial_dir/target\.yaml"$' \
  "$source_runner" | cut -d: -f1)
[[ -n $source_submit_line && -n $source_create_line ]] || \
  fail "source runner lacks the submit-edge/create pair"
((source_create_line == source_submit_line + 1)) || \
  fail "submit-edge timestamp is not immediately adjacent to target creation"
pass "probe creation overlaps restore and evidence is generated once"

expected_cleanup=$'delete:semantic-probe.yaml\ndelete:restore-worker.yaml\ndelete:target.yaml'
actual_cleanup=$(rg '^delete:' "$FAKE_CALL_LOG")
[[ $actual_cleanup == "$expected_cleanup" ]] || fail "cleanup was not exact and run-scoped"
[[ -s $run_dir/cleanup.log ]] || fail "cleanup output was not retained"
pass "optional cleanup deletes only the three rendered run-scoped manifests"

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
