#!/usr/bin/env bash
set -euo pipefail

readonly retry_script="$(dirname "$0")/retry.sh"
readonly retries="${KUBECTL_APPLY_RETRIES:-10}"
readonly interval="${KUBECTL_APPLY_RETRY_INTERVAL:-5}"

umask 077
manifest_file="$(mktemp)"
readonly manifest_file
trap 'rm -f "$manifest_file"' EXIT

cat >"$manifest_file"
if [[ ! -s "$manifest_file" ]]; then
    echo "Manifest is empty" >&2
    exit 1
fi

"$retry_script" -n "$retries" -i "$interval" -- \
    kubectl apply "$@" -f "$manifest_file"
