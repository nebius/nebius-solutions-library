#!/bin/bash
# Automated pass/fail verification of the checkpointing installation.
#
# Run from this directory on a login node after ./bootstrap.sh. It submits the
# real example job (overridden to one GPU on each of two nodes, so cross-node
# sharding is still exercised without needing a full allocation) and checks:
#
#   1. commit   - the job writes a checkpoint and commits the `latest` marker;
#   2. kill     - after a hard SIGKILL (no graceful save possible), the marker
#                 still points at a complete checkpoint. The kill is sent as
#                 soon as the next upload is observed in flight; if none shows
#                 up within one save cadence the job is killed anyway, so this
#                 phase ATTEMPTS to interrupt an active upload but always
#                 verifies hard-kill recovery;
#   3. resume   - a new submission with the same prefix resumes from the
#                 committed step and commits further progress;
#   4. graceful - on SIGUSR1 the ranks commit the current step and exit.
#
# Deliberately NOT verified here: automatic Slurm requeue, real instance
# preemption, and node recovery. Those depend on scheduler events and operator
# action; the README's interruption section describes how to exercise them.
#
# Everything it writes lives under a unique verify-* prefix, which is deleted
# at the end. Exits 0 only if all checks pass. Typical duration is ~5-10
# minutes once the two nodes are allocated; queue time is extra.
set -euo pipefail

CHECKPOINTING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${CHECKPOINTING_DIR}"
export PATH="${CHECKPOINTING_DIR}/bin:${PATH}"

# Generous per-phase deadlines; queue time counts against the first one.
START_DEADLINE="${VERIFY_START_DEADLINE:-1800}"   # submit -> job running
COMMIT_DEADLINE="${VERIFY_COMMIT_DEADLINE:-300}"  # running -> first commit
RESUME_DEADLINE="${VERIFY_RESUME_DEADLINE:-300}"  # resume -> next commit

set -a
# shellcheck disable=SC1091 # rendered by the checkpoint access module
source /etc/nebius-checkpoints.env
set +a

S5CMD=(s5cmd --endpoint-url "${NEBIUS_OBJECT_STORAGE_ENDPOINT}")

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "PASS: $*"
}

JOB_IDS=()
PREFIX=""
cleanup() {
  for job in "${JOB_IDS[@]}"; do
    scancel "${job}" 2>/dev/null || true
  done
  if [ -n "${PREFIX}" ]; then
    "${S5CMD[@]}" rm "s3://${NEBIUS_CHECKPOINT_BUCKET}/${PREFIX}/*" >/dev/null 2>&1 || true
    # The hard-kill phase interrupts an active upload, which can orphan an
    # incomplete multipart upload that object listing does not show. Abort any
    # under the verification prefix with the bootstrapped boto3 environment.
    "${CHECKPOINTING_DIR}/venv/bin/python" - "${PREFIX}" <<'PYEOF' >/dev/null 2>&1 || true
import os
import sys

import boto3
from botocore.config import Config

client = boto3.client(
    "s3",
    endpoint_url=os.environ["NEBIUS_OBJECT_STORAGE_ENDPOINT"],
    region_name=os.environ["NEBIUS_OBJECT_STORAGE_REGION"],
    config=Config(s3={"addressing_style": "path"}),
)
bucket = os.environ["NEBIUS_CHECKPOINT_BUCKET"]
for page in client.get_paginator("list_multipart_uploads").paginate(
    Bucket=bucket, Prefix=sys.argv[1] + "/"
):
    for upload in page.get("Uploads") or []:
        client.abort_multipart_upload(
            Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
        )
PYEOF
  fi
}
trap cleanup EXIT

read_marker() {
  # Prints the committed step, or nothing if the marker does not exist yet.
  "${S5CMD[@]}" cat "s3://${NEBIUS_CHECKPOINT_BUCKET}/${PREFIX}/latest" 2>/dev/null | tr -d '[:space:]'
}

step_is_complete() {
  # A committed step must contain the DCP metadata object written last. The
  # trainer zero-pads step directories to eight digits (step-00000123/).
  "${S5CMD[@]}" ls \
    "s3://${NEBIUS_CHECKPOINT_BUCKET}/${PREFIX}/step-$(printf '%08d' "$1")/.metadata" \
    >/dev/null 2>&1
}

newest_step_dir() {
  # Prints the highest step number that has any objects, committed or not.
  "${S5CMD[@]}" ls "s3://${NEBIUS_CHECKPOINT_BUCKET}/${PREFIX}/step-*" 2>/dev/null \
    | grep -o 'step-[0-9]*' | sed 's/step-0*//;s/^$/0/' | sort -n | tail -n 1
}

job_state() {
  squeue --noheader --format=%T --job "$1" 2>/dev/null | head -n 1
}

submit() {
  # One GPU on each of two nodes: still cross-node and sharded, but does not
  # need the full allocation the demo header requests.
  TRAIN_ARGS="--prefix ${PREFIX} --save-every-seconds 20 --keep-last 2" \
    sbatch --parsable --job-name=checkpointing-verify \
    --gpus-per-node=1 --ntasks-per-node=1 checkpoint_train.sbatch
}

wait_running() { # jobid
  local waited=0
  while [ "$(job_state "$1")" != "RUNNING" ]; do
    [ -n "$(job_state "$1")" ] || fail "job $1 left the queue before running (see results/checkpointing-demo.$1.out)"
    [ "${waited}" -lt "${START_DEADLINE}" ] || fail "job $1 not running after ${START_DEADLINE}s (state: $(job_state "$1"))"
    sleep 10; waited=$((waited + 10))
  done
}

wait_gone() { # jobid deadline what
  local waited=0
  while [ -n "$(job_state "$1")" ]; do
    [ "${waited}" -lt "$2" ] || fail "job $1 still in the queue $2s after $3"
    sleep 5; waited=$((waited + 5))
  done
}

wait_commit() { # min_exclusive_step deadline
  local waited=0 marker
  while true; do
    marker="$(read_marker || true)"
    if [ -n "${marker}" ] && [ "${marker}" -gt "$1" ] 2>/dev/null; then
      echo "${marker}"
      return
    fi
    [ "${waited}" -lt "$2" ] || fail "no checkpoint committed beyond step $1 within $2s"
    sleep 5; waited=$((waited + 5))
  done
}

PREFIX="verify-$(date +%Y%m%d-%H%M%S)-$$"
echo "Verification prefix: s3://${NEBIUS_CHECKPOINT_BUCKET}/${PREFIX}/"

echo "[1/4] Submitting the example job and waiting for the first committed checkpoint..."
job1="$(submit)"
JOB_IDS+=("${job1}")
echo "  submitted job ${job1}"
wait_running "${job1}"
echo "  job ${job1} is running"
committed="$(wait_commit -1 "${COMMIT_DEADLINE}")"
step_is_complete "${committed}" || fail "marker names step ${committed} but its .metadata object is missing"
pass "commit - job ${job1} committed checkpoint step ${committed}"

echo "[2/4] Hard-killing the job (SIGKILL, no graceful save) and checking the marker..."
# Kill the moment objects of a NEWER step appear: the next upload is then in
# flight, so the SIGKILL lands mid-upload. If no new upload starts within the
# save cadence, kill anyway - the invariant must hold at any moment.
caught_in_flight=false
waited=0
while [ "${waited}" -lt 45 ]; do
  newest="$(newest_step_dir || true)"
  if [ -n "${newest}" ] && [ "${newest}" -gt "${committed}" ] 2>/dev/null; then
    echo "  upload of step ${newest} is in flight"
    caught_in_flight=true
    break
  fi
  sleep 2; waited=$((waited + 2))
done
[ "${caught_in_flight}" = "true" ] \
  || echo "  no new upload observed within 45s; killing anyway (hard-kill recovery is still verified)"
scancel --signal=KILL "${job1}"
wait_gone "${job1}" 120 "SIGKILL"
marker_after_kill="$(read_marker)" || fail "marker unreadable after SIGKILL"
[ -n "${marker_after_kill}" ] || fail "marker missing after SIGKILL"
[ "${marker_after_kill}" -ge "${committed}" ] 2>/dev/null \
  || fail "marker moved backwards after SIGKILL (${committed} -> ${marker_after_kill})"
step_is_complete "${marker_after_kill}" \
  || fail "marker names step ${marker_after_kill} but the checkpoint is incomplete after SIGKILL"
pass "kill - marker still points at complete checkpoint step ${marker_after_kill}"

echo "[3/4] Resubmitting with the same prefix and waiting for resume + new commit..."
job2="$(submit)"
JOB_IDS+=("${job2}")
echo "  submitted job ${job2}"
wait_running "${job2}"
echo "  job ${job2} is running"
resumed="$(wait_commit "${marker_after_kill}" "${RESUME_DEADLINE}")"
log2="results/checkpointing-demo.${job2}.out"
grep -q "resuming from checkpoint step ${marker_after_kill}" "${log2}" \
  || fail "job ${job2} did not log resuming from step ${marker_after_kill} (see ${log2})"
pass "resume - job ${job2} resumed from step ${marker_after_kill} and committed step ${resumed}"

echo "[4/4] Sending SIGUSR1 (graceful stop) and waiting for the final checkpoint..."
scancel --signal=USR1 "${job2}"
wait_gone "${job2}" 180 "SIGUSR1"
grep -q "exiting after signal at step" "${log2}" \
  || fail "job ${job2} did not log a final checkpoint after SIGUSR1 (see ${log2})"
final_marker="$(read_marker)" || fail "marker unreadable after graceful stop"
[ "${final_marker}" -ge "${resumed}" ] 2>/dev/null \
  || fail "marker moved backwards after graceful stop (${resumed} -> ${final_marker})"
step_is_complete "${final_marker}" \
  || fail "final checkpoint step ${final_marker} is incomplete"
pass "graceful - job ${job2} checkpointed step ${final_marker} on SIGUSR1 and exited"

echo
echo "All checks passed. Cleaning up the ${PREFIX} prefix."
