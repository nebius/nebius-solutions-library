#!/usr/bin/env bash
set -euo pipefail

test_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly test_dir
dynamo_dir=$(dirname -- "$test_dir")
readonly dynamo_dir
readonly runner="${dynamo_dir}/run_provisioned_trial.sh"
readonly fixture_bin="${test_dir}/provisioned-fixtures/bin"
test_tmp=$(mktemp -d /tmp/proteinmpnn-provisioned-runner-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

export PATH="${fixture_bin}:${PATH}"
export FAKE_CALL_LOG="${test_tmp}/calls.log"
export FAKE_EXPECTED_KUBECONFIG="${test_tmp}/kubeconfig.yaml"
export FAKE_NODE="gpu-node-a.example.invalid"
export FAKE_HOLDER_NAME="proteinmpnn-artifact-holder-example"
export FAKE_SERVER="https://kubernetes-api.example.invalid:443"
export FAKE_HOLDER_READY=true

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
