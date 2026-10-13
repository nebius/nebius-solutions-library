#!/bin/bash
# Main container of an endpoint-call job (API mode `async`, docs/API.md "Endpoint calls").
#
# Managed I/O (no execution block): POST REQUEST_BODY to URL until it is accepted, with exponential backoff capped
# at 5 min; the uploader exports out/ to the platform bucket. 4xx other than 408/429 are the caller's error: exit 2,
# which the Job's podFailurePolicy turns into an immediate FailJob (no pod retries).
#
# Application I/O (IO_MODE=application, a private binding mounted at PRIVATE_INPUT_DIR): the request comes from the
# binding (request.json, or input_url fetched at transfer time), the capture goes to the application's own
# destination (response_url, call_url: presigned PUT URLs; with refresh_url + refresh_token the caller asks the
# application for fresh URLs at every transfer, with its projected identity token when one is mounted), the stable
# result_ref is handed to the uploader's attempt record through RESULT.json. The capture is written to the retained
# work volume first (response.bin, then call.json last: the completion marker) and uploaded after; a failed upload
# leaves export=failed for the record (EXPORT_FAILED) and the capture on the volume for a transfer-only retry
# (TRANSFER_ONLY=1: no inference, upload what is captured; nothing complete -> exit 3). Readiness is probed first
# with an idempotent GET (READY_URL) so the one inference POST meets an endpoint that answers; the POST is made
# exactly once per operation (posted.json is the marker): no response means an unknown outcome (exit 4, FAILED),
# never a second POST; a response with any status is a capture and is delivered. The exit code is the inference
# outcome (0 = 2xx, 2 = 4xx, 5 = another status), on the first delivery and on every re-delivery; the delivery
# itself is reported through RESULT.json (export succeeded | failed).
#
# The URL is the endpoint's gateway hostname (TLS verified, key in AUTH_HEADER, ADMITTED = the API's admission token
# so the gateway hook counts the call once); CONNECT_TO (host:443:svc:443) routes the connection to the gateway's
# in-cluster Service instead of the public IP, while the TLS name and Host stay the public hostname.
set -uo pipefail
cd /work || exit 1
mkdir -p out
PRIV="${PRIVATE_INPUT_DIR:-}"

curl_auth=()
[ -n "${AUTH_HEADER:-}" ] && curl_auth+=(-H "Authorization: $AUTH_HEADER")
[ -n "${ADMITTED:-}" ] && curl_auth+=(-H "x-serverless2-admitted: $ADMITTED")
# the admission is bound to this operation and attempt (docs/API.md "Endpoint calls"): the gateway's hook verifies the
# token against the operation and checks that the operation is active, owned by this key, and that this attempt is
# its live pod; the headers name them, the token proves them
[ -n "${ADMITTED:-}" ] && [ -n "${OPERATION:-}" ] && curl_auth+=(-H "x-serverless2-operation: $OPERATION")
[ -n "${ADMITTED:-}" ] && [ -n "${POD_NAME:-}" ] && curl_auth+=(-H "x-serverless2-attempt: $POD_NAME")
[ -n "${CONNECT_TO:-}" ] && curl_auth+=(--connect-to "$CONNECT_TO")

backoff() {   # exponential, BACKOFF_BASE (5 s) x 2^n, capped at BACKOFF_MAX (300 s)
  local e=$1; [ "$e" -gt 6 ] && e=6
  local s=$(( ${BACKOFF_BASE:-5} * (1 << e) )); [ "$s" -gt "${BACKOFF_MAX:-300}" ] && s=${BACKOFF_MAX:-300}
  sleep "$s"
}

binding() { [ -n "$PRIV" ] && [ -f "$PRIV/$1" ] && cat "$PRIV/$1"; }

refresh() {   # $1 = phase (input|output): fresh URLs from the application, or the static ones from the binding
  local url token body attempt_id
  url=$(binding refresh_url); token=$(binding refresh_token)
  if [ -z "$url" ]; then
    INPUT_URL=$(binding input_url); RESPONSE_URL=$(binding response_url); CALL_URL=$(binding call_url); return 0
  fi
  attempt_id="${POD_NAME:-$(hostname)}"
  body=$(jq -cn --arg op "${OPERATION:-}" --arg a "$attempt_id" --arg p "$1" '{operation_id:$op,attempt_id:$a,phase:$p}')
  local -a h=(-H "Content-Type: application/json" -H "Authorization: Bearer $token")
  [ -n "${IDENTITY_TOKEN_FILE:-}" ] && [ -r "$IDENTITY_TOKEN_FILE" ] && h+=(-H "X-Workload-Identity: $(cat "$IDENTITY_TOKEN_FILE")")
  local n=0 code
  while :; do
    code=$(curl -sS -o out/refresh.json -w '%{http_code}' -X POST "$url" "${h[@]}" --data-binary "$body" --max-time 60 2>out/refresh_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) refresh $1 attempt $n -> HTTP $code"
    case "$code" in
      2*) INPUT_URL=$(jq -r '.input_url // empty' out/refresh.json); RESPONSE_URL=$(jq -r '.response_url // empty' out/refresh.json)
          CALL_URL=$(jq -r '.call_url // empty' out/refresh.json); return 0;;
      401|403|404) echo "refresh refused ($code): $(head -c 300 out/refresh.json)"; return 1;;
    esac
    [ "$n" -ge 20 ] && return 1
    backoff $n
  done
}

put() {   # $1 = file, $2 = presigned URL, $3 = content type
  local n=0 code
  while :; do
    code=$(curl -sS -o out/put_error.txt -w '%{http_code}' -X PUT "$2" -H "Content-Type: $3" --data-binary "@$1" --max-time 900 2>&1) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) upload $1 attempt $n -> HTTP $code"
    case "$code" in 2*) return 0;; 403|404) return 1;; esac   # an expired or wrong URL: ask for a fresh one
    [ "$n" -ge 5 ] && return 1
    backoff $n
  done
}

deliver() {   # upload the capture; with refresh, fresh URLs first, and once more after a 403 (expired URL)
  local _round
  for _round in 1 2; do
    refresh output || return 1
    if [ -z "${RESPONSE_URL:-}" ] || [ -z "${CALL_URL:-}" ]; then echo "no response_url/call_url to deliver to"; return 1; fi
    put response.bin "$RESPONSE_URL" "$(jq -r '.content_type // "application/octet-stream"' call.json)" \
      && put call.json "$CALL_URL" "application/json" && return 0
  done
  return 1
}

finish() {   # $1 = export state; RESULT.json is what the uploader's attempt record carries (upload.sh record mode)
  local ref; ref=$(binding result_ref)
  if [ "$1" = succeeded ]; then
    jq -cn --arg r "$ref" --arg a "${CAPTURE_ATTEMPT:-${POD_NAME:-}}" '{result_ref:$r, export:"succeeded", capture_attempt_id:$a}' > RESULT.json.tmp
  else
    jq -cn --arg r "$ref" --arg e "${2:-}" '{export:"failed", error:$e} + (if $r != "" then {result_ref_pending:$r} else {} end)' > RESULT.json.tmp
  fi
  mv RESULT.json.tmp RESULT.json
}

if [ "${IO_MODE:-managed}" != application ]; then
  n=0
  while true; do
    code=$(curl -sS -o out/response.json -w '%{http_code}' -X POST "$URL" -H 'Content-Type: application/json' \
           "${curl_auth[@]}" --data-binary "$REQUEST_BODY" --max-time "${CALL_TIMEOUT:-3600}" 2>out/last_error.txt) || code=000
    n=$((n + 1))
    echo "$(date -u +%FT%TZ) attempt $n -> HTTP $code"
    case "$code" in
      2*) printf '{"http_status":%s,"attempts":%s}\n' "$code" "$n" > out/call.json; exit 0;;
      408|429|000|5*) ;;
      4*) printf '{"http_status":%s,"attempts":%s}\n' "$code" "$n" > out/call.json; cat out/response.json; exit 2;;   # caller's error: FailJob
    esac
    backoff $n
  done
fi

# ---- application I/O ------------------------------------------------------------------------------------------
if [ -z "$PRIV" ] || [ ! -d "$PRIV" ]; then echo "application I/O needs the private binding (PRIVATE_INPUT_DIR)"; exit 2; fi

outcome_exit() {   # the inference outcome of a capture, independent of its delivery: 0 = 2xx, 2 = 4xx (the caller's
  local st=$1       # error), 5 = anything else the endpoint answered (its error); delivery is tracked by RESULT.json
  case "$st" in 2*) return 0;; 4*) return 2;; *) return 5;; esac
}

done_with() {   # $1 = HTTP status of the capture: exit with its outcome after a successful delivery
  local st=$1 rc; outcome_exit "$st"; rc=$?
  [ "$rc" = 0 ] || echo "endpoint answered HTTP $st (delivered)" > /dev/termination-log 2>/dev/null
  exit "$rc"
}

if [ "${TRANSFER_ONLY:-0}" = 1 ]; then
  # export retry: the capture of a previous attempt on this volume, complete only with its marker; the retry ends with
  # the captured outcome, so a delivered error never becomes a successful inference
  if [ -f call.json ] && [ -f response.bin ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
    CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
    if deliver; then finish succeeded; done_with "$(jq -r '.http_status // 0' call.json)"; fi
    finish failed "upload of the retained capture failed again"; exit 0
  fi
  echo "no complete capture on the work volume: nothing to export (inference is not repeated)"
  finish failed "no complete capture to export"; exit 3
fi

if [ -f call.json ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
  echo "a complete capture exists already (a previous attempt of this operation): delivering it, not calling again"
  CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
  if deliver; then finish succeeded; done_with "$(jq -r '.http_status // 0' call.json)"; fi
  finish failed "upload failed"; exit 0
fi

# the request: inline in the binding, or fetched at transfer time (a queued call may outlive a URL minted at submission)
refresh input || { finish failed "refresh of the input URL refused"; exit 2; }
if [ -n "${INPUT_URL:-}" ]; then
  n=0
  while :; do
    code=$(curl -sS -o request.bin -w '%{http_code}' "$INPUT_URL" --max-time 900 2>out/input_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) input fetch attempt $n -> HTTP $code"
    case "$code" in 2*) break;; 403|404) refresh input || true;; esac
    [ "$n" -ge 10 ] && { finish failed "the input could not be fetched (HTTP $code)"; exit 2; }
    backoff $n
  done
elif [ -f "$PRIV/request.json" ]; then
  cp "$PRIV/request.json" request.bin
else
  finish failed "the binding has neither request.json nor input_url"; exit 2
fi

# exactly one inference POST per operation: a previous attempt of this pod's Job that posted and died leaves
# posted.json without call.json; its outcome is unknown and is never re-posted (exit 4: the operation FAILS with that
# error, nothing to export; the application decides whether to submit again)
if [ -f posted.json ]; then
  echo "a previous attempt posted the request ($(jq -r '.attempt_id' posted.json) at $(jq -r '.posted_at' posted.json)) and left no capture: the inference outcome is unknown; not posting again"
  jq -cn --arg op "${OPERATION:-}" --arg a "$(jq -r '.attempt_id' posted.json)" '{operation_id:$op, attempt_id:$a, http_status:0, capture_complete:false, outcome:"unknown"}' > call.json.tmp && mv call.json.tmp call.json
  finish failed "inference outcome unknown: the request was posted by a previous attempt and no response was captured"
  echo "inference outcome unknown (posted, no response captured)" > /dev/termination-log 2>/dev/null
  exit 4
fi

# readiness first, with an idempotent GET through the same gateway (a cold or scaled-to-zero endpoint is woken by it and
# answers when it serves); the inference POST goes to an endpoint that answers, so the edge never cuts a cold start
if [ -n "${READY_URL:-}" ]; then
  n=0
  while :; do
    code=$(curl -sS -o out/ready.txt -w '%{http_code}' "$READY_URL" "${curl_auth[@]}" --max-time 120 2>out/ready_error.txt) || code=000
    n=$((n + 1)); echo "$(date -u +%FT%TZ) readiness probe $n -> HTTP $code"
    case "$code" in 2*|405) break;; 401|403) echo "readiness probe refused ($code): $(head -c 200 out/ready.txt)"; finish failed "the endpoint refused the call's key ($code)"; exit 2;; esac
    backoff $n
  done
fi

n=0
while :; do
  posted_epoch=$(date -u +%s)
  jq -cn --arg a "${POD_NAME:-$(hostname)}" --argjson e "$posted_epoch" '{attempt_id:$a, posted_at:($e|todate)}' > posted.json.tmp && mv posted.json.tmp posted.json
  code=$(curl -sS -o response.bin -D headers.txt -w '%{http_code} %{time_starttransfer} %{time_total} %{size_download}' -X POST "$URL" \
         -H 'Content-Type: application/json' "${curl_auth[@]}" --data-binary @request.bin --max-time "${CALL_TIMEOUT:-3600}" 2>out/last_error.txt) || code="000 0 0 0"
  read -r http t_first t_total size <<< "$code"
  n=$((n + 1)); echo "$(date -u +%FT%TZ) inference POST $n -> HTTP $http (${t_total}s, ${size} bytes)"
  if [ "$http" = 429 ] && [ "$n" -lt 30 ]; then
    rm -f posted.json; backoff $n; continue   # rejected at the edge before the model (per-key rate window): safe to ask again
  fi
  break
done
if [ "$http" = 000 ] || { [ "$size" = 0 ] && [ "${http:0:1}" = 5 ]; }; then
  # no response: the connection broke or the edge gave up before the first byte; the model may have run: unknown
  jq -cn --arg op "${OPERATION:-}" --arg a "${POD_NAME:-$(hostname)}" --argjson st "${http:-0}" --argjson e "$posted_epoch" \
     '{operation_id:$op, attempt_id:$a, http_status:$st, capture_complete:false, outcome:"unknown", timings:{posted_at:($e|todate)}}' > call.json.tmp && mv call.json.tmp call.json
  finish failed "inference outcome unknown: no response was captured (HTTP $http, $(head -c 200 out/last_error.txt | tr '\n' ' '))"
  echo "inference outcome unknown (HTTP $http, no response captured)" > /dev/termination-log 2>/dev/null
  exit 4
fi
ctype=$(grep -i '^content-type:' headers.txt | tail -1 | cut -d' ' -f2- | tr -d '\r')
sha=$(sha256sum response.bin | cut -d' ' -f1)
completed_epoch=$(date -u +%s)
jq -cn --arg op "${OPERATION:-}" --arg a "${POD_NAME:-$(hostname)}" --argjson st "${http:-0}" --arg ct "${ctype:-application/octet-stream}" \
   --argjson sz "$(stat -c %s response.bin)" --arg sha "$sha" --argjson p "$posted_epoch" --argjson c "$completed_epoch" --argjson tf "${t_first:-0}" --argjson tt "${t_total:-0}" \
   '{operation_id:$op, attempt_id:$a, http_status:$st, content_type:$ct, size_bytes:$sz, sha256:$sha,
     timings:{posted_at:($p|todate), first_byte_at:(($p + ($tf|floor))|todate), completed_at:($c|todate), duration_s:$tt}, capture_complete:true}' > call.json.tmp && mv call.json.tmp call.json
CAPTURE_ATTEMPT="${POD_NAME:-$(hostname)}"
case "$http" in
  2*) ;;
  4*) echo "the endpoint answered $http (the caller's error): the response is captured and delivered as is";;
  *) echo "the endpoint answered $http (the endpoint's error): captured and delivered as is";;
esac
if deliver; then finish succeeded; done_with "$http"; fi
finish failed "upload of the capture failed; the capture is kept on the work volume for POST /v1/operations/<id>:export"
exit 0
