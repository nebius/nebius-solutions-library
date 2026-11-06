#!/bin/bash
# Init container: sync the inputs prefix into /work/in once. On a resumed attempt (same PVC) the
# marker exists and the inputs and checkpoints on the volume are kept untouched.
set -euo pipefail
cd "${WORK_DIR:-/work}"
# out/ and checkpoint/ are created by this container (uid 10001) but written by `main`, which runs as the image's
# user with every capability dropped (no CAP_DAC_OVERRIDE even as root): world-writable
# with the setgid bit so whatever user writes there, the files stay group-readable for the uploader
mkdir -p out checkpoint
chmod 2777 out checkpoint 2>/dev/null || true
# the canonical work aliases of this attempt (docs/JOBS.md "Work mount aliases"): WORK_ALIAS_DIRS lists the directories
# main's alias mounts show (aliases/<slug>/<this pod>), expanded by the kubelet; created here before the
# resumed-attempt exit below (a new pod on the kept volume needs its own directory). Tenant-private: group-writable for
# the pod's fsGroup (every container of the pod carries it, whatever user the model image runs as), setgid so files
# stay group-owned for the uploader; not world-writable
for d in $(printf '%s' "${WORK_ALIAS_DIRS:-}" | tr ':' ' '); do
  case "$d" in aliases/*) mkdir -p "$d"; chmod 2770 "$d" 2>/dev/null || true ;; *) echo "WARN: alias directory $d ignored (not under aliases/)" ;; esac
done
if [ -f .inputs-fetched ]; then echo "FETCH SKIP $(date -u +%FT%TZ): resumed attempt, inputs already on the volume"; exit 0; fi
if [ -n "${INPUT_PREFIX:-}" ]; then
  echo "FETCH START $(date -u +%FT%TZ) $INPUT_PREFIX"
  aws s3 sync "${INPUT_PREFIX%/}/" /work/in/ --no-progress
  du -sh /work/in | sed 's/^/FETCHED /'
fi
date -u +%FT%TZ > .inputs-fetched
echo "FETCH END $(date -u +%FT%TZ)"
