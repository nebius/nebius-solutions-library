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
# (TRANSFER_ONLY=1: no inference, upload what is captured; nothing complete -> exit 3). A 502/503/504 with no
# response is retried like a saturated endpoint (the gateway admits the call once more under the same admission
# token); a response with any status is a capture: inference that may have run is never repeated silently.
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
[ -n "${CONNECT_TO:-}" ] && curl_auth+=(--connect-to "$CONNECT_TO")

backoff() { local e=$1; [ "$e" -gt 6 ] && e=6; local s=$(( 5 * (1 << e) )); [ $s -gt 300 ] && s=300; sleep $s; }

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

if [ "${TRANSFER_ONLY:-0}" = 1 ]; then
  # export retry: the capture of a previous attempt on this volume, complete only with its marker
  if [ -f call.json ] && [ -f response.bin ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
    CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
    if deliver; then finish succeeded; exit 0; fi
    finish failed "upload of the retained capture failed again"; exit 0
  fi
  echo "no complete capture on the work volume: nothing to export (inference is not repeated)"
  finish failed "no complete capture to export"; exit 3
fi

if [ -f call.json ] && [ "$(jq -r '.capture_complete // false' call.json)" = true ]; then
  echo "a complete capture exists already (a previous attempt of this operation): delivering it, not calling again"
  CAPTURE_ATTEMPT=$(jq -r '.attempt_id // empty' call.json)
  if deliver; then finish succeeded; exit 0; fi
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

n=0
while :; do
  posted=$(date -u +%FT%T.%3NZ)
  code=$(curl -sS -o response.bin -D headers.txt -w '%{http_code} %{time_starttransfer} %{time_total} %{size_download}' -X POST "$URL" \
         -H 'Content-Type: application/json' "${curl_auth[@]}" --data-binary @request.bin --max-time "${CALL_TIMEOUT:-3600}" 2>out/last_error.txt) || code="000 0 0 0"
  read -r http t_first t_total size <<< "$code"
  n=$((n + 1)); echo "$(date -u +%FT%TZ) attempt $n -> HTTP $http (${t_total}s, ${size} bytes)"
  case "$http" in
    000|502|503|504)
      # no response from the endpoint (not ready, saturated, or the edge gave up before the first byte): wait and ask
      # again; the endpoint saw the same admitted call, the operation's deadline bounds the waiting
      [ -s response.bin ] && [ "$http" != 000 ] && break;;
    408|429) ;;
    *) break;;
  esac
  backoff $n
done
ctype=$(grep -i '^content-type:' headers.txt | tail -1 | cut -d' ' -f2- | tr -d '\r')
sha=$(sha256sum response.bin | cut -d' ' -f1)
completed=$(date -u +%FT%T.%3NZ)
jq -cn --arg op "${OPERATION:-}" --arg a "${POD_NAME:-$(hostname)}" --argjson st "${http:-0}" --arg ct "${ctype:-application/octet-stream}" \
   --argjson sz "$(stat -c %s response.bin)" --arg sha "$sha" --arg p "$posted" --arg c "$completed" --argjson tf "${t_first:-0}" --argjson tt "${t_total:-0}" \
   '{operation_id:$op, attempt_id:$a, http_status:$st, content_type:$ct, size_bytes:$sz, sha256:$sha,
     timings:{posted_at:$p, first_byte_s:$tf, completed_at:$c, duration_s:$tt}, capture_complete:true}' > call.json.tmp && mv call.json.tmp call.json
CAPTURE_ATTEMPT="${POD_NAME:-$(hostname)}"
case "$http" in
  2*) ;;
  4*) echo "the endpoint answered $http (the caller's error): the response is captured and delivered as is";;
  *) echo "the endpoint answered $http: captured and delivered as is";;
esac
if deliver; then finish succeeded; [ "${http:0:1}" = 4 ] && exit 2; exit 0; fi
finish failed "upload of the capture failed; the capture is kept on the work volume for POST /v1/operations/<id>:export"
exit 0
