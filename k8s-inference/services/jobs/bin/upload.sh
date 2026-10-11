#!/bin/bash
# Uploader container: waits until the main container of this pod has ended, then syncs /work (minus
# the inputs) to OUTPUT_PREFIX with a STATUS.json and an attempt record (attempts/<pod>.json: the
# operation's history and GPU-seconds survive the pod, which preemption deletes), and on success
# deletes the job's PVC (a failed attempt keeps it for a resume). Always exits 0: the pod's phase
# follows the main container.
#
# "Ended" is detected two ways: the pod status through the Kubernetes API (gives the exit code) and,
# because the pod shares its PID namespace, the absence of any process outside this container and
# the sandbox's pause (the kubelet does not refresh the status of a terminating pod, so after a
# SIGTERM the API alone would leave us waiting until the grace period's SIGKILL). On SIGTERM
# (preemption, cancel) it uploads what exists at once, then waits for main to finish its own
# SIGTERM handling (checkpoint) and uploads again.
set -uo pipefail
# UPLOAD_MODE=record (application-managed I/O, docs/JOBS.md "Execution options"): no bucket, no credentials; only the
# attempt record is kept under /work/attempts (the run moves its own data).
MODE="${UPLOAD_MODE:-sync}"; EXPORT=""
API="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
POD_URL="$API/api/v1/namespaces/$POD_NAMESPACE/pods/$POD_NAME"
MAIN="${MAIN_CONTAINER:-main}"; EXIT=""; POD="{}"; SEEN=0

pod() { POD=$(curl -sS --cacert "$CA" -H "Authorization: Bearer $TOKEN" "$POD_URL" 2>/dev/null || echo '{}'); }
field() { jq -r --arg m "$MAIN" "$1" <<<"$POD"; }

main_alive() {   # any process whose ancestry ends neither at this shell nor at pid 1 (pause) belongs to another container
  local d p root rest pp
  for d in /proc/[0-9]*; do
    p=${d#/proc/}; root=$p
    while :; do
      rest=$(cat "/proc/$root/stat" 2>/dev/null) || break
      rest=${rest##*) }; set -- $rest; pp=$2
      [ -z "${pp:-}" ] || [ "$pp" = 0 ] && break
      root=$pp
    done
    [ "$root" = "$$" ] && continue
    [ "$root" = 1 ] && continue
    [ -d "$d" ] && return 0
  done
  return 1
}

record() {   # $1 = status; the attempt as the uploader sees it now (main may still be running on SIGTERM)
  pod
  local started ended node pool
  started=$(field '.status.containerStatuses[]? | select(.name==$m) | (.state.terminated.startedAt // .state.running.startedAt // empty)')
  ended=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated.finishedAt // empty')
  node=$(field '.spec.nodeName // empty')
  pool=$(field '.spec.nodeSelector["serverless2.nebius/pool"] // empty')
  mkdir -p /work/attempts
  [ "$1" = started ] && ended="" || ended="${ended:-$(date -u +%FT%TZ)}"   # a start record has no end yet
  printf '{"operation":"%s","pod":"%s","node":"%s","pool":"%s","status":"%s","exit_code":%s,"gpus":%s,"gpu_class":"%s","started_at":"%s","ended_at":"%s"}\n' \
    "$OPERATION" "$POD_NAME" "$node" "$pool" "$1" "${EXIT:-null}" "${GPUS:-0}" "${GPU_CLASS:-}" "$started" "$ended" > "/work/attempts/$POD_NAME.json"
}

upload() {   # $1 = status
  cd /work || return
  record "$1"
  if [ "$MODE" = record ]; then echo "RECORD $(date -u +%FT%TZ) status=$1 (application-managed I/O: nothing exported)"; return; fi
  # multi-node runs (UPLOAD_SCOPE=rank0, docs/JOBS.md): every pod records its attempt, only rank 0 publishes the
  # results and STATUS.json (the ranks share nothing under /work; outputs are written by rank 0 by convention)
  if [ "${UPLOAD_SCOPE:-all}" = rank0 ] && [ "${NODE_RANK:-0}" != 0 ]; then
    echo "RECORD $(date -u +%FT%TZ) rank=$NODE_RANK status=$1 -> $OUTPUT_PREFIX/attempts/$POD_NAME.json"
    aws s3 cp "/work/attempts/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress 2>&1 | tail -n 2
    return
  fi
  echo "UPLOAD START $(date -u +%FT%TZ) status=$1 -> $OUTPUT_PREFIX"
  # UPLOAD_EXCLUDES is a string of aws filters ("--exclude checkpoint/*", quotes allowed): split it like a shell
  # line with globbing OFF. Unquoted $UPLOAD_EXCLUDES expanded `checkpoint/*` against /work into `checkpoint/hf`,
  # which excludes that one entry and uploads everything below it: a multi-node run with shared checkpoints
  # pushed its 470 GB weight cache to the bucket (test fleet, 2026-10-09, docs/VERIFICATION.md).
  set -f; eval "set -- ${UPLOAD_EXCLUDES:-}"
  aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --no-progress --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>&1 | tail -n 30
  local rc=${PIPESTATUS[0]}
  # verification: a second pass must find nothing left to copy (sizes), else the export is not durable (docs/JOBS.md)
  if [ "$rc" = 0 ] && [ -n "$(aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --dryrun --size-only --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>/dev/null | head -n 1)" ]; then rc=99; fi
  set +f
  if [ "$rc" = 0 ]; then EXPORT=succeeded; else EXPORT=failed; echo "UPLOAD FAILED $(date -u +%FT%TZ) rc=$rc: the volume is kept, retry the export (POST /v1/operations/$OPERATION:export)"; fi
  # STATUS.json is published last: its presence with export=succeeded is the durable-delivery marker
  printf '{"operation":"%s","attempt_pod":"%s","status":"%s","export":"%s","exit_code":%s,"finished_at":"%s"}\n' \
    "$OPERATION" "$POD_NAME" "$1" "$EXPORT" "${EXIT:-null}" "$(date -u +%FT%TZ)" > STATUS.json
  aws s3 cp STATUS.json "${OUTPUT_PREFIX%/}/STATUS.json" --no-progress 2>&1 | tail -n 1
  echo "UPLOAD END $(date -u +%FT%TZ) export=$EXPORT"
}
on_term() { echo "SIGTERM $(date -u +%FT%TZ): partial upload, then waiting for $MAIN to end"; EXIT=""; upload interrupted; }
trap on_term TERM

gone=0
while true; do
  pod
  EXIT=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated.exitCode // empty')
  [ -n "$EXIT" ] && break
  if main_alive; then
    if [ "$SEEN" = 0 ]; then
      # the attempt is on record from its first second: a node that dies without warning (a stopped spot VM
      # sends no SIGTERM, measured 2026-10-09 on a B300 spot node) leaves this "started" record, which the
      # replacement attempt's upload of /work/attempts carries to the bucket; the copy here is best-effort
      record started
      aws s3 cp "/work/attempts/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress > /dev/null 2>&1 || true
    fi
    SEEN=1; gone=0
  elif [ "$SEEN" = 1 ]; then
    gone=$((gone + 1))                        # main's processes are gone; the kubelet can take a while to post the exit code
    if [ "$gone" -ge "${EXIT_CODE_POLLS:-40}" ]; then
      echo "$MAIN ended, exit code not reported by the API after $gone polls; last status: $(field '.status.containerStatuses[]? | select(.name==$m) | .state' | tr -d '\n' | cut -c1-200)"
      EXIT=""; break
    fi
    sleep 3 & wait $!; continue
  fi
  sleep "${POLL_SECONDS:-10}" & wait $!      # interruptible by the trap
done
if [ "$EXIT" = "0" ]; then
  upload succeeded
  if [ -n "${PVC_NAME:-}" ] && [ "${KEEP_PVC:-false}" != "true" ] && [ "$EXPORT" != failed ]; then
    curl -sS -o /dev/null -w "pvc delete %{http_code}\n" --cacert "$CA" -H "Authorization: Bearer $TOKEN" -X DELETE \
      "$API/api/v1/namespaces/$POD_NAMESPACE/persistentvolumeclaims/$PVC_NAME"
  fi
elif [ -z "$EXIT" ]; then
  upload interrupted
  echo "$MAIN interrupted: outputs uploaded, PVC ${PVC_NAME:-?} kept (resumes automatically after a preemption; POST /v1/operations/$OPERATION:resume after a cancel)"
else
  upload failed
  echo "$MAIN exited $EXIT: outputs uploaded, PVC ${PVC_NAME:-?} kept for a resume (POST /v1/operations/$OPERATION:resume)"
fi
exit 0
