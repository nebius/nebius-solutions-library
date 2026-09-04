#!/usr/bin/env bash
set -euo pipefail

retries=5
interval=2

while [[ $# -gt 0 && "$1" != "--" ]]; do
    case "$1" in
        -n) retries="$2"; shift 2 ;;
        -i) interval="$2"; shift 2 ;;
        *)  echo "Usage: retry.sh [-n retries] [-i interval] -- <command>" >&2; exit 1 ;;
    esac
done
[[ "${1:-}" == "--" ]] && shift

if [[ $# -eq 0 ]]; then
    echo "Usage: retry.sh [-n retries] [-i interval] -- <command>" >&2
    exit 1
fi

for i in $(seq 1 "$retries"); do
    if "$@"; then
        exit 0
    fi
    if [[ $i -lt $retries ]]; then
        echo "($i/$retries) Command failed, retrying in ${interval}s..." >&2
        sleep "$interval"
    fi
done

echo "Command failed after $retries attempts" >&2
exit 1
