#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly trial_runner="${script_dir}/run_provisioned_trial.sh"

run_prefix=""
evidence_root=""
node=""
kubeconfig=""
artifact_holder=""
checkpoint_id=""
target_glibc_version=""
image_io_mode=""
artifact_manifest_sha256=""
cleanup=0
allow_performance_validation=0

usage() {
  cat >&2 <<'USAGE'
usage: run_n3.sh \
  --run-prefix DNS_LABEL \
  --evidence-root ABSOLUTE_DIRECTORY \
  --node ALLOWED_H100_HOSTNAME \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD \
  --checkpoint-id EXACT_CHECKPOINT_ID \
  --target-glibc-version MAJOR.MINOR \
  --image-io-mode direct|buffered \
  --artifact-manifest-sha256 CAPTURED_64_HEX_SHA256 \
  [--allow-performance-validation-worker] [--cleanup]
USAGE
}

die_usage() {
  printf '%s\n' "$1" >&2
  usage
  exit 64
}

set_once() {
  local option_name=$1 current_value=$2 new_value=$3
  [[ -z $current_value ]] || die_usage "${option_name} may be supplied only once"
  [[ -n $new_value ]] || die_usage "${option_name} requires a nonempty value"
}

while (($# > 0)); do
  case "$1" in
    --run-prefix)
      (($# >= 2)) || die_usage "--run-prefix requires a value"
      set_once "$1" "$run_prefix" "$2"; run_prefix=$2; shift 2 ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once "$1" "$evidence_root" "$2"; evidence_root=$2; shift 2 ;;
    --node)
      (($# >= 2)) || die_usage "--node requires a value"
      set_once "$1" "$node" "$2"; node=$2; shift 2 ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once "$1" "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
    --artifact-holder)
      (($# >= 2)) || die_usage "--artifact-holder requires a value"
      set_once "$1" "$artifact_holder" "$2"; artifact_holder=$2; shift 2 ;;
    --checkpoint-id)
      (($# >= 2)) || die_usage "--checkpoint-id requires a value"
      set_once "$1" "$checkpoint_id" "$2"; checkpoint_id=$2; shift 2 ;;
    --target-glibc-version)
      (($# >= 2)) || die_usage "--target-glibc-version requires a value"
      set_once "$1" "$target_glibc_version" "$2"; target_glibc_version=$2; shift 2 ;;
    --image-io-mode)
      (($# >= 2)) || die_usage "--image-io-mode requires a value"
      set_once "$1" "$image_io_mode" "$2"; image_io_mode=$2; shift 2 ;;
    --artifact-manifest-sha256)
      (($# >= 2)) || die_usage "--artifact-manifest-sha256 requires a value"
      set_once "$1" "$artifact_manifest_sha256" "$2"; artifact_manifest_sha256=$2; shift 2 ;;
    --cleanup)
      ((cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      cleanup=1; shift ;;
    --allow-performance-validation-worker)
      ((allow_performance_validation == 0)) || \
        die_usage "--allow-performance-validation-worker may be supplied only once"
      allow_performance_validation=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      die_usage "unknown argument: $1" ;;
  esac
done

for required_name in run_prefix evidence_root node kubeconfig artifact_holder \
  checkpoint_id target_glibc_version image_io_mode artifact_manifest_sha256; do
  [[ -n ${!required_name} ]] || die_usage "--${required_name//_/-} is required"
done
if [[ ! $run_prefix =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#run_prefix} -gt 25 ]]; then
  die_usage "--run-prefix must be a DNS label of at most 25 characters"
fi
[[ -x $trial_runner ]] || { printf 'trial runner is not executable\n' >&2; exit 69; }
[[ -d $evidence_root/runs && ! -L $evidence_root/runs ]] || \
  die_usage "--evidence-root/runs must be an existing non-symlink directory"

summary_path="${evidence_root}/n3-${run_prefix}-${image_io_mode}.json"
partial_path="${summary_path}.partial"
if [[ -e $summary_path || -L $summary_path || -e $partial_path || -L $partial_path ]]; then
  printf 'n=3 summary path already exists\n' >&2
  exit 73
fi

common_args=(
  --evidence-root "$evidence_root"
  --node "$node"
  --kubeconfig "$kubeconfig"
  --artifact-holder "$artifact_holder"
  --checkpoint-id "$checkpoint_id"
  --target-glibc-version "$target_glibc_version"
  --image-io-mode "$image_io_mode"
  --artifact-manifest-sha256 "$artifact_manifest_sha256"
)
if ((cleanup == 1)); then
  common_args+=(--cleanup)
fi
if ((allow_performance_validation == 1)); then
  common_args+=(--allow-performance-validation-worker)
fi

run_ids=()
for repetition in 1 2 3; do
  run_id="${run_prefix}-r${repetition}"
  run_ids+=("$run_id")
  "$trial_runner" --run-id "$run_id" "${common_args[@]}"
done

python3 - "$evidence_root" "$checkpoint_id" "$image_io_mode" \
  "${run_ids[@]}" <<'PY' > "$partial_path"
import json
import math
import statistics
import sys
from pathlib import Path
from datetime import datetime

root = Path(sys.argv[1])
checkpoint_id = sys.argv[2]
image_io_mode = sys.argv[3]
run_ids = sys.argv[4:]
if len(run_ids) != 3 or len(set(run_ids)) != 3:
    raise SystemExit("n=3 runner did not produce exactly three unique run IDs")
runs = []
demand = []
for run_id in run_ids:
    path = root / "runs" / run_id / "canary-evidence.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("status") != "PASS"
        or value.get("request_count") != 2
        or value.get("semantic_pass_count") != 2
        or value.get("response_timing_contract") != "request-dispatch-to-complete-http-body/v1"
        or value.get("t0_source") != "target-submit-at.txt"
        or value.get("t0_at") != value.get("demand_at")
    ):
        raise SystemExit(f"trial is not a strict two-call PASS: {run_id}")
    artifact = value.get("artifact", {})
    if artifact.get("checkpoint_id") != checkpoint_id or artifact.get("image_io_mode") != image_io_mode:
        raise SystemExit(f"trial artifact identity mismatch: {run_id}")
    boundary = value.get("evidence", {})
    t0_at = value.get("t0_at")
    response_received = boundary.get("second_response_received_at")
    validation_finished = boundary.get("validation_finished_at")
    try:
        t0_time = datetime.fromisoformat(t0_at.replace("Z", "+00:00"))
        response_time = datetime.fromisoformat(response_received.replace("Z", "+00:00"))
        validation_time = datetime.fromisoformat(validation_finished.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SystemExit(f"trial lacks response-boundary provenance: {run_id}") from exc
    if not t0_time <= response_time <= validation_time:
        raise SystemExit(f"trial response boundary is outside T0/validation: {run_id}")
    recomputed_demand = round((response_time - t0_time).total_seconds(), 6)
    reported_demand = value.get("timings_seconds", {}).get(
        "demand_to_two_semantic_responses"
    )
    if (
        isinstance(reported_demand, bool)
        or not isinstance(reported_demand, (int, float))
        or not math.isfinite(float(reported_demand))
        or round(float(reported_demand), 6) != recomputed_demand
    ):
        raise SystemExit(f"trial total does not match response boundary: {run_id}")
    demand.append(recomputed_demand)
    runs.append(value)

http_ready = [float(run["timings_seconds"]["demand_to_http_ready"]) for run in runs]
kubernetes_ready = [float(run["timings_seconds"]["demand_to_kubernetes_ready"]) for run in runs]
call_1 = [float(run["timings_seconds"]["semantic_request_1"]) for run in runs]
call_2 = [float(run["timings_seconds"]["semantic_request_2"]) for run in runs]
restore_receipt = [float(run["timings_seconds"]["demand_to_restore_receipt"]) for run in runs]
restore = [float(run["timings_seconds"]["worker_restore"]) for run in runs]
result = {
    "schema": "archvteams.nebius.ai/genmol-native-n3/v1",
    "status": "PASS",
    "t0_source": "target-submit-at.txt",
    "checkpoint_id": checkpoint_id,
    "image_io_mode": image_io_mode,
    "trial_count": 3,
    "request_count": 6,
    "semantic_pass_count": 6,
    "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
    "run_ids": run_ids,
    "demand_to_two_semantic_seconds": demand,
    "demand_to_http_ready_seconds": http_ready,
    "demand_to_kubernetes_ready_seconds": kubernetes_ready,
    "semantic_request_1_seconds": call_1,
    "semantic_request_2_seconds": call_2,
    "demand_to_restore_receipt_seconds": restore_receipt,
    "worker_restore_seconds": restore,
    "statistics_seconds": {
        "demand_to_two_semantic_min": min(demand),
        "demand_to_two_semantic_median": statistics.median(demand),
        "demand_to_two_semantic_max": max(demand),
        "demand_to_two_semantic_mean": statistics.mean(demand),
        "demand_to_http_ready_median": statistics.median(http_ready),
        "demand_to_kubernetes_ready_median": statistics.median(kubernetes_ready),
        "semantic_request_1_median": statistics.median(call_1),
        "semantic_request_2_median": statistics.median(call_2),
        "demand_to_restore_receipt_median": statistics.median(restore_receipt),
        "worker_restore_median": statistics.median(restore),
    },
}
print(json.dumps(result, sort_keys=True, indent=2))
PY
chmod 0600 "$partial_path"
mv -- "$partial_path" "$summary_path"
jq '{status,checkpoint_id,image_io_mode,trial_count,request_count,statistics_seconds}' \
  "$summary_path"
