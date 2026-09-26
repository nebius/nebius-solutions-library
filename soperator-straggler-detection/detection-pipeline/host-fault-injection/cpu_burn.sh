#!/bin/bash
# All-core CPU burner, alternating 5s on / 5s off, for a given duration.
# Runs directly on the host (outside any container) via SSH.
set -u
DURATION_S=$1
NCORES=$(nproc)
END=$(( $(date +%s) + DURATION_S ))
while [ "$(date +%s)" -lt "$END" ]; do
  for i in $(seq 1 "$NCORES"); do
    ( timeout 5 bash -c 'while true; do :; done' ) &
  done
  wait
  sleep 5
done
