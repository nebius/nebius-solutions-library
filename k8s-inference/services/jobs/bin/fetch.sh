#!/bin/bash
# Init container: sync the inputs prefix into /work/in once. On a resumed attempt (same PVC) the
# marker exists and the inputs and checkpoints on the volume are kept untouched.
set -euo pipefail
cd /work
# out/ and checkpoint/ are created by this container (uid 10001) but written by `main`, which runs as the image's
# user with every capability dropped (no CAP_DAC_OVERRIDE even as root, docs/SECURITY-PREREVIEW.md F1): world-writable
# with the setgid bit so whatever user writes there, the files stay group-readable for the uploader
mkdir -p out checkpoint
chmod 2777 out checkpoint 2>/dev/null || true
if [ -f .inputs-fetched ]; then echo "FETCH SKIP $(date -u +%FT%TZ): resumed attempt, inputs already on the volume"; exit 0; fi
if [ -n "${INPUT_PREFIX:-}" ]; then
  echo "FETCH START $(date -u +%FT%TZ) $INPUT_PREFIX"
  aws s3 sync "${INPUT_PREFIX%/}/" /work/in/ --no-progress
  du -sh /work/in | sed 's/^/FETCHED /'
fi
date -u +%FT%TZ > .inputs-fetched
echo "FETCH END $(date -u +%FT%TZ)"
