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
((cleanup == 1)) || die_usage "--cleanup is required between every counted n=3 trial"
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
  [[ -s $evidence_root/runs/$run_id/cleanup-verified-at.txt ]] || {
    printf 'trial cleanup was not verified: %s\n' "$run_id" >&2
    exit 69
  }
done

python3 - "$evidence_root" "$checkpoint_id" "$image_io_mode" \
  "$artifact_manifest_sha256" "$target_glibc_version" \
  "${run_ids[@]}" <<'PY' > "$partial_path"
import json
import math
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
checkpoint_id = sys.argv[2]
image_io_mode = sys.argv[3]
artifact_manifest_sha256 = sys.argv[4]
target_glibc_version = sys.argv[5]
run_ids = sys.argv[6:]
if len(run_ids) != 3 or len(set(run_ids)) != 3:
    raise SystemExit("n=3 runner did not produce exactly three unique run IDs")

def positive_timing(value, label, run_id):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SystemExit(f"trial {label} is not numeric: {run_id}")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise SystemExit(f"trial {label} is not positive and finite: {run_id}")
    return result

runs = []
for run_id in run_ids:
    path = root / "runs" / run_id / "canary-evidence.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schema")
        != "archvteams.nebius.ai/molmim-production-canary-evidence/v1"
        or value.get("status") != "PASS"
        or value.get("run_id") != run_id
        or value.get("request_count") != 2
        or value.get("semantic_pass_count") != 2
    ):
        raise SystemExit(f"trial is not an identity-bound strict two-call PASS: {run_id}")
    artifact = value.get("artifact", {})
    if artifact != {
        "checkpoint_id": checkpoint_id,
        "version": "1",
        "manifest_sha256": artifact_manifest_sha256,
        "target_glibc_version": target_glibc_version,
        "image_io_mode": image_io_mode,
    }:
        raise SystemExit(f"trial artifact identity mismatch: {run_id}")
    timings = value.get("timings_seconds")
    if not isinstance(timings, dict):
        raise SystemExit(f"trial timings are malformed: {run_id}")
    prewarm = value.get("storage_prewarm")
    cache = prewarm.get("cache", {}) if isinstance(prewarm, dict) else {}
    artifact_prewarm = prewarm.get("artifact", {}) if isinstance(prewarm, dict) else {}
    if (
        cache.get("mode") != "cache-full-read"
        or cache.get("unique_bytes") != 284_497_920
        or cache.get("tree_sha256")
        != "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c"
        or artifact_prewarm.get("mode") != image_io_mode
        or artifact_prewarm.get("manifest_sha256") != artifact_manifest_sha256
        or not isinstance(artifact_prewarm.get("tree_sha256"), str)
        or len(artifact_prewarm["tree_sha256"]) != 64
    ):
        raise SystemExit(f"trial storage prewarm identity mismatch: {run_id}")
    runs.append(value)

demand = [
    positive_timing(
        run["timings_seconds"].get("demand_to_two_semantic_responses"),
        "demand-to-two-semantic timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
restore = [
    positive_timing(
        run["timings_seconds"].get("worker_restore"),
        "worker-restore timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
http_ready = [
    positive_timing(
        run["timings_seconds"].get("demand_to_http_ready"),
        "demand-to-direct-HTTP-ready timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
kubernetes_ready = [
    positive_timing(
        run["timings_seconds"].get("demand_to_kubernetes_ready"),
        "demand-to-Kubernetes-ready timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
call_1 = [
    positive_timing(
        run["timings_seconds"].get("semantic_request_1"),
        "first semantic request timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
call_2 = [
    positive_timing(
        run["timings_seconds"].get("semantic_request_2"),
        "second semantic request timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
validation_complete = [
    positive_timing(
        run["timings_seconds"].get("demand_to_validation_complete"),
        "demand-to-validation-complete timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
cache_prewarm_elapsed = [
    positive_timing(
        run["storage_prewarm"]["cache"].get("full_read_elapsed_seconds"),
        "cache full-read timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
artifact_prewarm_elapsed = [
    positive_timing(
        run["storage_prewarm"]["artifact"].get("full_read_elapsed_seconds"),
        "artifact full-read timing",
        run_id,
    )
    for run_id, run in zip(run_ids, runs, strict=True)
]
artifact_trees = {run["storage_prewarm"]["artifact"]["tree_sha256"] for run in runs}
artifact_unique_bytes = {
    run["storage_prewarm"]["artifact"].get("unique_bytes") for run in runs
}
if len(artifact_trees) != 1 or len(artifact_unique_bytes) != 1:
    raise SystemExit("n=3 trials did not use one immutable fully read artifact")
result = {
    "schema": "archvteams.nebius.ai/molmim-native-n3/v1",
    "status": "PASS",
    "checkpoint_id": checkpoint_id,
    "image_io_mode": image_io_mode,
    "trial_count": 3,
    "request_count": 6,
    "semantic_pass_count": 6,
    "run_ids": run_ids,
    "demand_to_two_semantic_seconds": demand,
    "worker_restore_seconds": restore,
    "demand_to_http_ready_seconds": http_ready,
    "demand_to_kubernetes_ready_seconds": kubernetes_ready,
    "semantic_request_1_seconds": call_1,
    "semantic_request_2_seconds": call_2,
    "demand_to_validation_complete_seconds": validation_complete,
    "storage_prewarm": {
        "captured_at": [run["storage_prewarm"]["captured_at"] for run in runs],
        "cache": {
            "mode": "cache-full-read",
            "unique_bytes": 284_497_920,
            "tree_sha256": "5ff815495b2b90ec6f4d9e5df24216b11a60d49f711e68999347036b0f43056c",
            "full_read_elapsed_seconds": cache_prewarm_elapsed,
            "full_read_elapsed_median_seconds": statistics.median(cache_prewarm_elapsed),
        },
        "artifact": {
            "mode": image_io_mode,
            "unique_bytes": next(iter(artifact_unique_bytes)),
            "tree_sha256": next(iter(artifact_trees)),
            "manifest_sha256": artifact_manifest_sha256,
            "full_read_elapsed_seconds": artifact_prewarm_elapsed,
            "full_read_elapsed_median_seconds": statistics.median(artifact_prewarm_elapsed),
        },
    },
    "statistics_seconds": {
        "demand_to_two_semantic_min": min(demand),
        "demand_to_two_semantic_median": statistics.median(demand),
        "demand_to_two_semantic_max": max(demand),
        "demand_to_two_semantic_mean": statistics.mean(demand),
        "worker_restore_median": statistics.median(restore),
        "worker_restore_min": min(restore),
        "worker_restore_max": max(restore),
        "demand_to_http_ready_median": statistics.median(http_ready),
        "demand_to_http_ready_min": min(http_ready),
        "demand_to_http_ready_max": max(http_ready),
        "demand_to_kubernetes_ready_median": statistics.median(kubernetes_ready),
        "demand_to_kubernetes_ready_min": min(kubernetes_ready),
        "demand_to_kubernetes_ready_max": max(kubernetes_ready),
        "semantic_request_1_median": statistics.median(call_1),
        "semantic_request_1_min": min(call_1),
        "semantic_request_1_max": max(call_1),
        "semantic_request_2_median": statistics.median(call_2),
        "semantic_request_2_min": min(call_2),
        "semantic_request_2_max": max(call_2),
        "demand_to_validation_complete_median": statistics.median(validation_complete),
        "demand_to_validation_complete_min": min(validation_complete),
        "demand_to_validation_complete_max": max(validation_complete),
    },
}
print(json.dumps(result, sort_keys=True, indent=2))
PY
chmod 0600 "$partial_path"
mv -- "$partial_path" "$summary_path"
jq '{status,checkpoint_id,image_io_mode,trial_count,request_count,statistics_seconds}' \
  "$summary_path"
