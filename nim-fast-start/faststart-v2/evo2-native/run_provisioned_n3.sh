#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir

run_prefix=""
image_io_mode=""
artifact_manifest_sha256=""
evidence_root=""
kubeconfig=""
artifact_holder=""
cleanup=0
allow_performance_worker=0

usage() {
  cat >&2 <<'USAGE'
usage: run_provisioned_n3.sh \
  --run-prefix DNS_LABEL \
  --image-io-mode direct|buffered \
  --artifact-manifest-sha256 SHA256 \
  --evidence-root ABSOLUTE_DIRECTORY \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD \
  --allow-performance-validation-worker [--cleanup]
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
    --run-prefix)
      (($# >= 2)) || die_usage "--run-prefix requires a value"
      set_once --run-prefix "$run_prefix" "$2"; run_prefix=$2; shift 2 ;;
    --image-io-mode)
      (($# >= 2)) || die_usage "--image-io-mode requires a value"
      set_once --image-io-mode "$image_io_mode" "$2"; image_io_mode=$2; shift 2 ;;
    --artifact-manifest-sha256)
      (($# >= 2)) || die_usage "--artifact-manifest-sha256 requires a value"
      set_once --artifact-manifest-sha256 "$artifact_manifest_sha256" "$2"
      artifact_manifest_sha256=$2; shift 2 ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once --evidence-root "$evidence_root" "$2"; evidence_root=$2; shift 2 ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once --kubeconfig "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
    --artifact-holder)
      (($# >= 2)) || die_usage "--artifact-holder requires a value"
      set_once --artifact-holder "$artifact_holder" "$2"; artifact_holder=$2; shift 2 ;;
    --allow-performance-validation-worker)
      ((allow_performance_worker == 0)) || die_usage "worker override supplied twice"
      allow_performance_worker=1; shift ;;
    --cleanup)
      ((cleanup == 0)) || die_usage "--cleanup supplied twice"
      cleanup=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      die_usage "unknown argument: $1" ;;
  esac
done

[[ -n $run_prefix && -n $image_io_mode && -n $artifact_manifest_sha256 ]] || die_usage "run prefix, mode, and manifest digest are required"
[[ -n $evidence_root && -n $kubeconfig && -n $artifact_holder ]] || die_usage "evidence root, kubeconfig, and holder are required"
[[ $run_prefix =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#run_prefix} -le 27 ]] || die_usage "run prefix must be a DNS label of at most 27 characters"
((allow_performance_worker == 1)) || die_usage "the current worker requires the explicit performance-validation override"

common=(
  --image-io-mode "$image_io_mode"
  --artifact-manifest-sha256 "$artifact_manifest_sha256"
  --evidence-root "$evidence_root"
  --kubeconfig "$kubeconfig"
  --artifact-holder "$artifact_holder"
  --allow-performance-validation-worker
)
((cleanup == 0)) || common+=(--cleanup)

summaries=()
for index in 1 2 3; do
  run_id="${run_prefix}-${index}"
  "$script_dir/run_one_provisioned_trial.sh" --run-id "$run_id" "${common[@]}"
  summary="$evidence_root/runs/$run_id/trial-summary.json"
  [[ -f $summary ]] || { printf 'missing passing summary: %s\n' "$summary" >&2; exit 1; }
  summaries+=("$summary")
done

install -d -m 0700 -- "$evidence_root/aggregates"
aggregate="$evidence_root/aggregates/${run_prefix}-${image_io_mode}-n3.json"
python3 "$script_dir/aggregate_results.py" \
  --image-io-mode "$image_io_mode" \
  --output "$aggregate" \
  "${summaries[@]}"
