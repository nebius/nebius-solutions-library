#!/bin/bash
# jq expressions intentionally use single quotes: jq, rather than the shell, expands their variables.
# shellcheck disable=SC2016
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
: "${POD_NAME:?POD_NAME must be set}"
# UPLOAD_MODE=record (application-managed I/O, docs/JOBS.md "Execution options"): no bucket, no credentials; only the
# attempt record is kept under /work/attempts (the run moves its own data).
MODE="${UPLOAD_MODE:-sync}"; EXPORT=""; OUTPUT_PREFIX="${OUTPUT_PREFIX:-}"; ACK=""; CKPT_SEEN=""
API="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
TOKEN_FILE=/var/run/secrets/kubernetes.io/serviceaccount/token; TOKEN=$(cat "$TOKEN_FILE"); CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
POD_URL="$API/api/v1/namespaces/$POD_NAMESPACE/pods/$POD_NAME"
MAIN="${MAIN_CONTAINER:-main}"; EXIT=""; POD="{}"; SEEN=0

pod() { TOKEN=$(cat "$TOKEN_FILE"); POD=$(curl -sS --cacert "$CA" -H "Authorization: Bearer $TOKEN" "$POD_URL" 2>/dev/null || echo '{}'); }
field() { jq -r --arg m "$MAIN" "$1" <<<"$POD"; }

main_alive() {   # any process whose ancestry ends neither at this shell nor at pid 1 (pause) belongs to another container
  local d p root rest pp
  local -a stat_fields
  for d in /proc/[0-9]*; do
    p=${d#/proc/}; root=$p
    while :; do
      rest=$(cat "/proc/$root/stat" 2>/dev/null) || break
      rest=${rest##*) }; read -r -a stat_fields <<< "$rest"; pp=${stat_fields[1]}
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
  local started ended node pool image digest result ckpt
  started=$(field '.status.containerStatuses[]? | select(.name==$m) | (.state.terminated.startedAt // .state.running.startedAt // empty)')
  ended=$(field '.status.containerStatuses[]? | select(.name==$m) | .state.terminated.finishedAt // empty')
  node=$(field '.spec.nodeName // empty')
  pool=$(field '.spec.nodeSelector["serverless2.nebius/pool"] // empty')
  image=$(field '.spec.containers[]? | select(.name==$m) | .image // empty')
  digest=$(field '.status.containerStatuses[]? | select(.name==$m) | .imageID // empty')
  # the application's finalization record (docs/JOBS.md "Attempts and finalization"): RESULT.json next to the
  # outputs names the committed result and checkpoint; without it an application-managed run is not delivered.
  # CHECKPOINT.json ({"checkpoint_ref": ..., "generation": N}) is the live pointer the application publishes while it
  # runs; the acknowledged pointer is written back to CHECKPOINT.ack.json
  local result="" ckpt="" gen=""
  if [ -f /work/RESULT.json ]; then
    result=$(jq -r '.result_ref // empty' /work/RESULT.json 2>/dev/null); ckpt=$(jq -r '.checkpoint_ref // empty' /work/RESULT.json 2>/dev/null)
  fi
  if [ -z "$ckpt" ] && [ -f /work/CHECKPOINT.json ]; then
    ckpt=$(jq -r '.checkpoint_ref // empty' /work/CHECKPOINT.json 2>/dev/null); gen=$(jq -r '.generation // empty' /work/CHECKPOINT.json 2>/dev/null)
  fi
  # the record goes next to the outputs; a volume this container cannot write (a misconfigured claim) must not stop
  # the record from reaching the API: it is kept under /tmp then and the upload step reports the volume
  REC=/work/attempts
  if ! mkdir -p "$REC" 2>/dev/null || ! [ -w "$REC" ]; then REC=/tmp/attempts; mkdir -p "$REC"; echo "WARN: /work is not writable by the uploader; records kept under $REC"; fi
  [ "$1" = started ] && ended="" || ended="${ended:-$(date -u +%FT%TZ)}"   # a start record has no end yet
  jq -cn --arg op "$OPERATION" --arg pod "$POD_NAME" --arg node "$node" --arg pool "$pool" --arg st "$1" --argjson exit "${EXIT:-null}" \
    --argjson gpus "${GPUS:-0}" --arg cls "${GPU_CLASS:-}" --arg s "$started" --arg e "$ended" --arg img "$image" --arg dg "$digest" \
    --arg exp "${EXPORT:-}" --arg res "$result" --arg ck "$ckpt" --arg gen "$gen" --arg mode "$([ "$MODE" = record ] && echo application || echo managed)" \
    --arg opmode "${OPERATION_MODE:-}" --arg model "${MODEL:-}" \
    '{operation:$op,pod:$pod,node:$node,pool:$pool,status:$st,exit_code:$exit,gpus:$gpus,gpu_class:$cls,started_at:$s,ended_at:$e,
      image:$img,image_digest:$dg,io_mode:$mode} + (if $exp != "" then {export:$exp} else {} end)
      + (if $opmode != "" then {mode:$opmode} else {} end) + (if $model != "" then {model:$model} else {} end)
      + (if $res != "" then {result_ref:$res} else {} end) + (if $ck != "" then {checkpoint_ref:$ck} else {} end)
      + (if $gen != "" then {checkpoint_generation:($gen|tonumber)} else {} end)' > "$REC/$POD_NAME.json"
  # the durable copy: the fleet API keeps attempts in its database (fenced by epoch; a stale pod gets 409). A terminal
  # record is retried; ACK holds the last HTTP status (200 = acknowledged; the work volume is released only then)
  ACK=""
  if [ -n "${API_URL:-}" ]; then
    local tries=1; [ "$1" = started ] || tries=4
    for i in $(seq 1 $tries); do
      ACK=$(curl -sS -m 20 -o "$REC/.ack.json" -w "%{http_code}" -X POST "$API_URL/internal/attempts" \
        -H "Authorization: Bearer $(cat "$TOKEN_FILE")" -H "Content-Type: application/json" --data-binary "@$REC/$POD_NAME.json" 2>/dev/null || echo 000)
      echo "attempt record $1 -> API $ACK"
      case "$ACK" in 200|409) break ;; esac
      [ "$i" -lt "$tries" ] && { sleep 20 & wait $!; }
    done
    if [ "$ACK" = 200 ] && [ -n "$gen" ]; then
      jq -c --arg g "$gen" '{generation:($g|tonumber), epoch:.epoch, checkpoint_ref:.checkpoint_ref, revision:.revision, acknowledged_at:(now|todate)}' \
        "$REC/.ack.json" > /work/CHECKPOINT.ack.json.tmp 2>/dev/null && mv /work/CHECKPOINT.ack.json.tmp /work/CHECKPOINT.ack.json
    fi
  fi
}

checkpoint_changed() {   # the application published a new checkpoint pointer (CHECKPOINT.json content changed)
  [ -f /work/CHECKPOINT.json ] || return 1
  local now; now=$(tr -d '\n' 2>/dev/null < /work/CHECKPOINT.json)
  [ "$now" != "$CKPT_SEEN" ] || return 1
  CKPT_SEEN="$now"; return 0
}

upload() {   # $1 = status
  local st="$1"                                 # the positional parameters are replaced below (UPLOAD_EXCLUDES)
  cd /work || return
  record "$st"
  if [ "$MODE" = record ]; then
    # application-managed I/O: the run (or the endpoint caller, docs/API.md "Endpoint calls") delivered its own output;
    # RESULT.json may say how that went (export: succeeded | failed), else the platform exported nothing (n/a)
    EXPORT=$(jq -r '.export // "n/a"' /work/RESULT.json 2>/dev/null || echo n/a); [ -n "$EXPORT" ] || EXPORT="n/a"
    record "$st"; echo "RECORD $(date -u +%FT%TZ) status=$st export=$EXPORT (application-managed I/O)"; return
  fi
  # multi-node runs (UPLOAD_SCOPE=rank0, docs/JOBS.md): every pod records its attempt, only rank 0 publishes the
  # results and STATUS.json (the ranks share nothing under /work; outputs are written by rank 0 by convention)
  if [ "${UPLOAD_SCOPE:-all}" = rank0 ] && [ "${NODE_RANK:-0}" != 0 ]; then
    echo "RECORD $(date -u +%FT%TZ) rank=$NODE_RANK status=$st -> $OUTPUT_PREFIX/attempts/$POD_NAME.json"
    aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress 2>&1 | tail -n 2
    return
  fi
  echo "UPLOAD START $(date -u +%FT%TZ) status=$st -> $OUTPUT_PREFIX"
  # UPLOAD_EXCLUDES is a string of aws filters ("--exclude checkpoint/*", quotes allowed): split it like a shell
  # line with globbing OFF. Unquoted $UPLOAD_EXCLUDES expanded `checkpoint/*` against /work into `checkpoint/hf`,
  # which excludes that one entry and uploads everything below it: a multi-node run with shared checkpoints
  # pushed its 470 GB weight cache to the bucket (docs/VERIFICATION.md).
  set -f; eval "set -- ${UPLOAD_EXCLUDES:-}"
  aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --no-progress --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>&1 | tail -n 30
  local rc=${PIPESTATUS[0]}
  # verification: a second pass must find nothing left to copy (sizes), else the export is not durable (docs/JOBS.md)
  if [ "$rc" = 0 ] && [ -n "$(aws s3 sync /work/ "${OUTPUT_PREFIX%/}/" --dryrun --size-only --exclude 'in/*' --exclude '.inputs-fetched' --exclude 'lost+found/*' --exclude 'STATUS.json' "$@" 2>/dev/null | head -n 1)" ]; then rc=99; fi
  set +f
  if [ "$rc" = 0 ]; then EXPORT=succeeded; else EXPORT=failed; echo "UPLOAD FAILED $(date -u +%FT%TZ) rc=$rc: the volume is kept, retry the export (POST /v1/operations/$OPERATION:export)"; fi
  record "$st"                                  # the record now says how the export went (and reaches the API)
  aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress 2>&1 | tail -n 1
  # STATUS.json is published last: its presence with export=succeeded is the durable-delivery marker
  printf '{"operation":"%s","attempt_pod":"%s","status":"%s","export":"%s","exit_code":%s,"finished_at":"%s"}\n' \
    "$OPERATION" "$POD_NAME" "$st" "$EXPORT" "${EXIT:-null}" "$(date -u +%FT%TZ)" > STATUS.json
  aws s3 cp STATUS.json "${OUTPUT_PREFIX%/}/STATUS.json" --no-progress 2>&1 | tail -n 1
  echo "UPLOAD END $(date -u +%FT%TZ) export=$EXPORT"
}
# shellcheck disable=SC2317,SC2329 # invoked by the TERM trap (diagnostic varies by ShellCheck version)
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
      # sends no SIGTERM, measured on a B300 spot node) leaves this "started" record, which the
      # replacement attempt's upload of /work/attempts carries to the bucket; the copy here is best-effort
      record started
      [ "$MODE" = record ] || aws s3 cp "${REC:-/work/attempts}/$POD_NAME.json" "${OUTPUT_PREFIX%/}/attempts/$POD_NAME.json" --no-progress > /dev/null 2>&1 || true
    fi
    SEEN=1; gone=0
    checkpoint_changed && record started     # the live pointer, fenced and acknowledged through the API
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
  # the work volume goes only once delivery is durable: the export verified (managed) or RESULT.json present
  # (application), and the API acknowledged the final record when there is an API (else the bucket record stands)
  delivered=false
  if [ "$MODE" = record ]; then [ -f /work/RESULT.json ] && delivered=true; else [ "$EXPORT" = succeeded ] && delivered=true; fi
  if [ -n "${API_URL:-}" ] && [ "$ACK" != 200 ]; then delivered=false; fi
  if [ -n "${PVC_NAME:-}" ] && [ "${KEEP_PVC:-false}" != "true" ] && [ "$delivered" = true ]; then
    curl -sS -o /dev/null -w "pvc delete %{http_code}\n" --cacert "$CA" -H "Authorization: Bearer $TOKEN" -X DELETE \
      "$API/api/v1/namespaces/$POD_NAMESPACE/persistentvolumeclaims/$PVC_NAME"
  elif [ -n "${PVC_NAME:-}" ]; then
    echo "volume $PVC_NAME kept: delivery not acknowledged (export=$EXPORT ack=$ACK result=$([ -f /work/RESULT.json ] && echo yes || echo no)); POST /v1/operations/$OPERATION:export retries the delivery"
  fi
elif [ -z "$EXIT" ]; then
  upload interrupted
  echo "$MAIN interrupted: outputs uploaded, PVC ${PVC_NAME:-?} kept (resumes automatically after a preemption; POST /v1/operations/$OPERATION:resume after a cancel)"
else
  upload failed
  echo "$MAIN exited $EXIT: outputs uploaded, PVC ${PVC_NAME:-?} kept for a resume (POST /v1/operations/$OPERATION:resume)"
fi
exit 0
