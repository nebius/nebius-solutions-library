#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly source_dir
test_tmp=$(mktemp -d /tmp/fresh-cohort-driver-test.XXXXXX)
readonly test_tmp
trap 'rm -rf -- "$test_tmp"' EXIT

mkdir -p "$test_tmp/tree/performance" "$test_tmp/tree/dynamo" \
  "$test_tmp/tree/boltz2-native" "$test_tmp/evidence/runs"
cp "$source_dir/run_fresh_cohort.sh" "$test_tmp/tree/performance/run_fresh_cohort.sh"
chmod 0755 "$test_tmp/tree/performance/run_fresh_cohort.sh"
cat > "$test_tmp/tree/performance/instrumentation_contract.py" <<'PY'
#!/usr/bin/env python3
import argparse
import json
parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
args = parser.parse_args()
print(json.dumps({
    "schema": "archvteams.nebius.ai/fresh-cohort-instrumentation-contract/v1",
    "model": args.model,
    "source_count": 1,
    "sources": [{"path": "fake", "sha256": "b" * 64}],
    "instrumentation_contract_sha256": "c" * 64,
}, sort_keys=True))
PY

for runner_path in \
  "$test_tmp/tree/dynamo/run_provisioned_trial.sh" \
  "$test_tmp/tree/boltz2-native/run_one_native_trial.sh"; do
  cat > "$runner_path" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
run_id="" ledger="" cohort_id="" attempt_index="" evidence_root="" model=openfold2
instrumentation_contract_sha256=""
[[ $0 != *boltz2-native* ]] || model=boltz2
while (($# > 0)); do
  case "$1" in
    --run-id) run_id=$2; shift 2 ;;
    --attempt-ledger) ledger=$2; shift 2 ;;
    --cohort-id) cohort_id=$2; shift 2 ;;
    --attempt-index) attempt_index=$2; shift 2 ;;
    --evidence-root) evidence_root=$2; shift 2 ;;
    --instrumentation-contract-sha256)
      instrumentation_contract_sha256=$2; shift 2 ;;
    --node|--kubeconfig|--artifact-holder|--cache-holder) shift 2 ;;
    --cleanup) shift ;;
    *) exit 64 ;;
  esac
done
if [[ ${REJECT_FIRST:-false} == true && $run_id == *-001 ]]; then
  exit 69
fi
trial_dir="$evidence_root/runs/$run_id"
if [[ ${UNSAFE_REJECT_FIRST:-false} == true && $run_id == *-001 ]]; then
  mkdir -m 0700 "$trial_dir"
  jq -nc --arg run_id "$run_id" '{schema:"archvteams.nebius.ai/run-cleanup-receipt/v1",
    run_id:$run_id,status:"FAIL",requested:true}' > "$trial_dir/cleanup-receipt.json"
  jq -nc --arg run_id "$run_id" --arg model "$model" \
    '{schema:"archvteams.nebius.ai/runner-attempt-result/v1",run_id:$run_id,
      model:$model,admitted:false,cleanup_status:"FAIL",final_exit_code:69}' \
    > "$trial_dir/attempt-result.json"
  exit 69
fi
if [[ ${BLOCK_RUNNER:-false} == true ]]; then
  trap 'printf TERM > "$evidence_root/signal-received"; exit 143' TERM
  printf '%s\n' "$$" > "$evidence_root/active-runner.pid"
  while :; do sleep 1; done
fi
mkdir -m 0700 "$trial_dir"
cp "$evidence_root/cohorts/$cohort_id/instrumentation-contract.json" \
  "$trial_dir/instrumentation-contract.json"
now=2026-08-18T00:00:00Z
runner_sha256=$(sha256sum "$0")
runner_sha256=${runner_sha256%% *}
jq -nc --arg run_id "$run_id" --arg ledger_schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
  --arg cohort_id "$cohort_id" --arg model "$model" --arg trial_dir "$trial_dir" \
  --arg runner_sha256 "$runner_sha256" \
  --arg instrumentation_contract_sha256 "$instrumentation_contract_sha256" \
  --argjson attempt_index "$attempt_index" '{schema:$ledger_schema,event:"admitted",
  cohort_id:$cohort_id,model:$model,attempt_index:$attempt_index,run_id:$run_id,
  admitted_at:"2026-08-18T00:00:00Z",trial_dir:$trial_dir,
  runner_sha256:$runner_sha256,
  instrumentation_contract_sha256:$instrumentation_contract_sha256}' >> "$ledger"
cleanup_status=PASS
final_exit=0
if [[ ${ADMITTED_CLEANUP_FAIL:-false} == true ]]; then
  cleanup_status=FAIL
  final_exit=1
fi
jq -nc --arg run_id "$run_id" --arg status "$cleanup_status" \
  '{schema:"archvteams.nebius.ai/run-cleanup-receipt/v1",
    run_id:$run_id,status:$status,requested:true}' > "$trial_dir/cleanup-receipt.json"
jq -nc --arg run_id "$run_id" --arg model "$model" --arg status "$cleanup_status" \
  --argjson final_exit "$final_exit" \
  '{schema:"archvteams.nebius.ai/runner-attempt-result/v1",run_id:$run_id,
    model:$model,admitted:true,cleanup_status:$status,final_exit_code:$final_exit}' \
  > "$trial_dir/attempt-result.json"
jq -nc --arg run_id "$run_id" --arg ledger_schema "archvteams.nebius.ai/fresh-cohort-ledger-event/v1" \
  --arg cohort_id "$cohort_id" --arg model "$model" --arg trial_dir "$trial_dir" \
  --arg status "$cleanup_status" --argjson final_exit "$final_exit" \
  --argjson attempt_index "$attempt_index" '{schema:$ledger_schema,event:"completed",
  cohort_id:$cohort_id,model:$model,attempt_index:$attempt_index,run_id:$run_id,
  completed_at:"2026-08-18T00:00:01Z",trial_dir:$trial_dir,
  summary_path:($trial_dir+"/summary.json"),cleanup_receipt_path:($trial_dir+"/cleanup-receipt.json"),
  cleanup_status:$status,runner_exit_code:$final_exit}' >> "$ledger"
exit "$final_exit"
RUNNER
  chmod 0755 "$runner_path"
done

cat > "$test_tmp/tree/performance/aggregate_fresh_cohort.py" <<'PY'
import argparse
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--model")
parser.add_argument("--ledger", type=Path)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
events = [json.loads(line) for line in args.ledger.read_text().splitlines()]
admitted = sum(event.get("event") == "admitted" for event in events)
args.output.write_text(json.dumps({"status": "PASS", "attempt_count": admitted}))
print(args.output.read_text())
PY

printf '%s\n' 'apiVersion: v1' > "$test_tmp/kubeconfig"
chmod 0600 "$test_tmp/kubeconfig"
driver="$test_tmp/tree/performance/run_fresh_cohort.sh"

pass_count=0
pass() { pass_count=$((pass_count + 1)); printf 'ok %d - %s\n' "$pass_count" "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }

if "$driver" \
  --model openfold2 --cohort-id short-test --run-prefix short \
  --attempt-count 19 --evidence-root "$test_tmp/evidence" \
  --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid --artifact-holder holder \
  > "$test_tmp/short.out" 2> "$test_tmp/short.err"; then
  fail "driver accepted fewer than twenty attempts"
fi
pass "driver refuses an n<20 request before execution"

export REJECT_FIRST=true
"$driver" \
  --model openfold2 \
  --cohort-id fresh-test \
  --run-prefix ft \
  --attempt-count 20 \
  --maximum-scheduled-attempts 21 \
  --evidence-root "$test_tmp/evidence" \
  --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid \
  --artifact-holder holder \
  > "$test_tmp/driver.out" 2> "$test_tmp/driver.err" || {
  sed -n '1,160p' "$test_tmp/driver.err" >&2
  fail "driver did not recover from one pre-admission rejection"
}
unset REJECT_FIRST
ledger="$test_tmp/evidence/cohorts/fresh-test/attempts.ndjson"
[[ $(jq -s '[.[] | select(.event=="admitted")] | length' "$ledger") == 20 ]] || \
  fail "ledger does not contain twenty admissions"
[[ $(jq -s '[.[] | select(.event=="completed")] | length' "$ledger") == 20 ]] || \
  fail "ledger does not contain twenty completions"
[[ $(jq -s '[.[] | select(.event=="pre_admission_rejection")] | length' "$ledger") == 1 ]] || \
  fail "pre-admission rejection is not retained"
[[ $(jq -s '[.[] | select(.event=="cohort_started")][0].instrumentation_contract_sha256 as $hash |
  [.[] | select(.event=="admitted") |
    select(.instrumentation_contract_sha256 == $hash)] | length' "$ledger") == 20 ]] || \
  fail "admitted attempts do not share the frozen instrumentation contract"
[[ $(jq -r '.attempt_count' "$test_tmp/evidence/cohorts/fresh-test/aggregate.json") == 20 ]] || \
  fail "aggregate did not receive exactly the admitted cohort"
pass "driver runs until twenty fresh admitted attempts and aggregates only its ledger"

if "$driver" \
  --model openfold2 --cohort-id fresh-test --run-prefix ft2 \
  --evidence-root "$test_tmp/evidence" --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid --artifact-holder holder \
  > "$test_tmp/reuse.out" 2> "$test_tmp/reuse.err"; then
  fail "driver reused an existing cohort directory"
fi
pass "driver refuses to overwrite or mix an existing cohort"

export UNSAFE_REJECT_FIRST=true
if "$driver" \
  --model openfold2 --cohort-id unsafe-test --run-prefix unsafe \
  --attempt-count 20 --maximum-scheduled-attempts 21 \
  --evidence-root "$test_tmp/evidence" --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid --artifact-holder holder \
  > "$test_tmp/unsafe.out" 2> "$test_tmp/unsafe.err"; then
  fail "driver retried after an unproven pre-admission cleanup"
fi
unset UNSAFE_REJECT_FIRST
unsafe_ledger="$test_tmp/evidence/cohorts/unsafe-test/attempts.ndjson"
[[ $(jq -s '[.[] | select(.event=="controller_abort")] | length' "$unsafe_ledger") == 1 ]] || \
  fail "unsafe pre-admission cleanup did not abort the controller"
[[ $(jq -s -r 'last | select(.event=="cohort_finished") | .scheduled_attempt_count' \
  "$unsafe_ledger") == 1 ]] || fail "driver retried the unsafe rejected attempt"
pass "unproven pre-admission cleanup aborts without scheduling another attempt"

export ADMITTED_CLEANUP_FAIL=true
if "$driver" \
  --model openfold2 --cohort-id cleanup-fail-test --run-prefix cleanupfail \
  --attempt-count 20 --evidence-root "$test_tmp/evidence" \
  --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid --artifact-holder holder \
  > "$test_tmp/cleanup-fail.out" 2> "$test_tmp/cleanup-fail.err"; then
  fail "driver continued after an admitted cleanup failure"
fi
unset ADMITTED_CLEANUP_FAIL
cleanup_fail_ledger="$test_tmp/evidence/cohorts/cleanup-fail-test/attempts.ndjson"
[[ $(jq -s '[.[] | select(.event=="admitted")] | length' "$cleanup_fail_ledger") == 1 ]] || \
  fail "cleanup failure scheduled another admitted attempt"
[[ $(jq -s '[.[] | select(.event=="controller_abort")] | length' "$cleanup_fail_ledger") == 1 ]] || \
  fail "admitted cleanup failure did not abort the controller"
pass "an admitted cleanup failure stops acquisition before the next trial"

BLOCK_RUNNER=true "$driver" \
  --model openfold2 --cohort-id signal-test --run-prefix signal \
  --attempt-count 20 --evidence-root "$test_tmp/evidence" \
  --kubeconfig "$test_tmp/kubeconfig" \
  --node gpu-node-a.example.invalid --artifact-holder holder \
  > "$test_tmp/signal.out" 2> "$test_tmp/signal.err" &
driver_pid=$!
for ((attempt=0; attempt<100; attempt++)); do
  [[ -s $test_tmp/evidence/active-runner.pid ]] && break
  sleep 0.05
done
[[ -s $test_tmp/evidence/active-runner.pid ]] || fail "signal test runner did not start"
active_runner_pid=$(<"$test_tmp/evidence/active-runner.pid")
kill -TERM "$driver_pid"
set +e
wait "$driver_pid"
signal_exit=$?
set -e
[[ $signal_exit == 143 ]] || fail "TERM did not produce the controller signal exit"
[[ $(<"$test_tmp/evidence/signal-received") == TERM ]] || \
  fail "controller did not forward TERM to the active runner"
if kill -0 "$active_runner_pid" 2>/dev/null; then
  fail "controller did not reap the signaled runner"
fi
signal_ledger="$test_tmp/evidence/cohorts/signal-test/attempts.ndjson"
[[ $(jq -s '[.[] | select(.event=="controller_abort")] | length' "$signal_ledger") == 1 ]] || \
  fail "signaled cohort has no controller-abort receipt"
[[ $(jq -s -r 'last | select(.event=="cohort_finished") | .controller_abort' \
  "$signal_ledger") == true ]] || fail "signaled cohort has no fail-closed footer"
pass "TERM is forwarded, runner is reaped, and an abort/footer is persisted"

printf '1..%d\n' "$pass_count"
