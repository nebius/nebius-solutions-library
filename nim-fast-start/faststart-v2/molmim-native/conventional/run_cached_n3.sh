#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir

run_prefix=""
evidence_root=""
node=""
kubeconfig=""
cleanup=0

usage() {
  cat >&2 <<'USAGE'
usage: run_cached_n3.sh --run-prefix DNS_LABEL --evidence-root ABSOLUTE_DIRECTORY \
  --node gpu-node-b.example.invalid --kubeconfig ABSOLUTE_FILE [--cleanup]
USAGE
}

die_usage() { printf '%s\n' "$1" >&2; usage; exit 64; }
set_once() {
  local name=$1 current=$2 value=$3
  [[ -z $current ]] || die_usage "${name} may be supplied only once"
  [[ -n $value ]] || die_usage "${name} requires a nonempty value"
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
    --cleanup)
      ((cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      cleanup=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done
for required in run_prefix evidence_root node kubeconfig; do
  [[ -n ${!required} ]] || die_usage "--${required//_/-} is required"
done
((cleanup == 1)) || die_usage "--cleanup is required between every counted n=3 trial"
if [[ ! $run_prefix =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#run_prefix} -gt 25 ]]; then
  die_usage "--run-prefix must be a DNS label of at most 25 characters"
fi
[[ -x $script_dir/run_cached_trial.sh ]] || { printf 'cached trial runner is not executable\n' >&2; exit 69; }
[[ -d $evidence_root/runs && ! -L $evidence_root/runs ]] || die_usage "evidence runs directory is absent"

summary="$evidence_root/n3-${run_prefix}-conventional-cached.json"
partial="${summary}.partial"
[[ ! -e $summary && ! -L $summary && ! -e $partial && ! -L $partial ]] || {
  printf 'n=3 conventional summary already exists\n' >&2
  exit 73
}

arguments=(--evidence-root "$evidence_root" --node "$node" --kubeconfig "$kubeconfig")
((cleanup == 0)) || arguments+=(--cleanup)
paths=()
for repetition in 1 2 3; do
  run_id="${run_prefix}-r${repetition}"
  "$script_dir/run_cached_trial.sh" --run-id "$run_id" "${arguments[@]}"
  [[ -s $evidence_root/runs/$run_id/cleanup-verified-at.txt ]] || {
    printf 'trial cleanup was not verified: %s\n' "$run_id" >&2
    exit 69
  }
  paths+=("$evidence_root/runs/$run_id/conventional-evidence.json")
done

python3 "$script_dir/aggregate.py" "${paths[@]}" > "$partial"
chmod 0600 "$partial"
mv -- "$partial" "$summary"
jq '{status,trial_count,request_count,statistics_seconds}' "$summary"
