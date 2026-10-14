"""The endpoint caller (services/jobs/bin/call.sh) with application I/O, driven with a stub curl: readiness first, one
inference POST, the frozen call.json schema, unknown outcomes never re-posted, transfer-only retries, delivery."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

CALL = Path(__file__).resolve().parents[2] / "jobs" / "bin" / "call.sh"
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None, reason="bash and jq are needed"
)

STUB = r"""#!/bin/bash
# a scripted curl: SCRIPT lists "<method-or-GET> <url-substring> <code> [<body>]" lines consumed in order per matching call;
# every invocation is appended to CALLS
args=("$@"); method=GET; out=/dev/null; url=""; data=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method=${args[$((i+1))]}; ((i++));;
    -o) out=${args[$((i+1))]}; ((i++));;
    -H) [ -n "${HEADERS:-}" ] && echo "${args[$((i+1))]}" >> "$HEADERS"; ((i++));;
    -D|--max-time|--connect-to|-w) ((i++));;
    --data-binary) data=${args[$((i+1))]}; ((i++));;
    -sS|-s) ;;
    *) [[ "${args[$i]}" == http* ]] && url=${args[$i]};;
  esac
done
echo "$method $url ${data:0:160}" >> "$CALLS"
line=$(grep -n -m1 "^$method [^ ]*$(printf '%s' "$url" | sed 's/[^a-zA-Z0-9/:.?=_-]//g' | cut -c1-200)" "$SCRIPT" 2>/dev/null | head -1)
if [ -z "$line" ]; then line=$(grep -n -m1 "^$method \* " "$SCRIPT" | head -1); fi
[ -n "$line" ] || { echo "no script for $method $url" >&2; exit 7; }
n=${line%%:*}; spec=${line#*:}
sed -i "${n}d" "$SCRIPT"
code=$(echo "$spec" | awk '{print $3}'); body=$(echo "$spec" | cut -d' ' -f4-)
[ "$out" != /dev/null ] && printf '%s' "$body" > "$out"
[ "$code" = 000 ] && exit 7
printf '%s 0.5 1.5 %s' "$code" "${#body}"
exit 0
"""


SYNC_STUB = r"""#!/bin/bash
# a scripted sync: SYNC_FAIL=all fails every call; SYNC_FAIL=after:N fails from the (N+1)th call on; the count persists
n=0; [ -f "$SYNC_COUNT" ] && n=$(cat "$SYNC_COUNT"); n=$((n + 1)); echo "$n" > "$SYNC_COUNT"
case "${SYNC_FAIL:-}" in
  all) exit 1;;
  after:*) [ "$n" -gt "${SYNC_FAIL#after:}" ] && exit 1;;
esac
exit 0
"""


def _run(tmp_path, binding, script, env=None, work_files=None):
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    for name, content in (work_files or {}).items():
        (work / name).write_text(content)
    priv = tmp_path / "private"
    priv.mkdir(exist_ok=True)
    for k, v in binding.items():
        (priv / k).write_text(v)
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    stub = bin_ / "curl"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    sync = bin_ / "sync"  # every run sees the scripted sync; without SYNC_FAIL it always succeeds
    sync.write_text(SYNC_STUB)
    sync.chmod(sync.stat().st_mode | stat.S_IEXEC)
    jq = bin_ / "jq"  # JQ_FAIL=manifest|result: that one jq write emits a partial document and exits 1 (a short write)
    jq.write_text(
        "#!/bin/bash\nreal=$(command -v -p jq || echo /usr/bin/jq)\n"
        'case "${JQ_FAIL:-}" in\n'
        '  manifest) for a in "$@"; do [[ "$a" == *capture_complete:true* ]] && { printf \'{"operation_id":"op-1","attempt_id\'; exit 1; }; done;;\n'
        '  result) for a in "$@"; do [[ "$a" == *export:\\"succeeded\\"* ]] && { printf \'{"result_ref":"app://\'; exit 1; }; done;;\n'
        'esac\nexec "$real" "$@"\n'
    )
    jq.chmod(jq.stat().st_mode | stat.S_IEXEC)
    mv = bin_ / "mv"  # MV_FAIL=<basename>: the rename of exactly that file fails (a write/rename failure at a boundary)
    mv.write_text(
        '#!/bin/bash\nif [ -n "${MV_FAIL:-}" ] && [ "$(basename "${@: -1}")" = "$MV_FAIL" ]; then echo "mv: refused $MV_FAIL" >&2; exit 1; fi\nexec /bin/mv "$@"\n'
    )
    mv.chmod(mv.stat().st_mode | stat.S_IEXEC)
    calls, scr = tmp_path / "calls.txt", tmp_path / "script.txt"
    calls.write_text("")
    scr.write_text("\n".join(script) + "\n")
    e = {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "SCRIPT": str(scr),
        "PRIVATE_INPUT_DIR": str(priv),
        "IO_MODE": "application",
        "URL": "https://ep.example/v1/chat",
        "READY_URL": "https://ep.example/health",
        "OPERATION": "op-1",
        "POD_NAME": "op-1-abc",
        "AUTH_HEADER": "Bearer k",
        "ADMITTED": "t",
        "BACKOFF_BASE": "0",
        "SYNC_COUNT": str(tmp_path / "sync.count"),
        **(env or {}),
    }
    # call.sh does `cd /work`: run it in a shell that maps /work to the temp directory through a wrapper
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(
        f'#!/bin/bash\nsed "s#cd /work || exit 1#cd {work} || exit 1#" {CALL} > {tmp_path}/call.sh\nexec bash {tmp_path}/call.sh\n'
    )
    r = subprocess.run(["bash", str(wrapper)], env=e, capture_output=True, text=True, timeout=120)
    return r, work, calls.read_text().splitlines()


BINDING = {
    "request.json": '{"messages": []}',
    "response_url": "https://objects.example/put/response",
    "call_url": "https://objects.example/put/call",
    "result_ref": "app://results/1",
}


def test_readiness_then_one_post_then_delivery_with_the_frozen_schema(tmp_path):
    script = [
        "GET https://ep.example/health 503",
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 200 {"choices":[]}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(tmp_path, BINDING, script)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [c.split()[0] for c in calls] == ["GET", "GET", "POST", "PUT", "PUT"]
    call = json.loads((work / "call.json").read_text())
    assert call["capture_complete"] is True and call["http_status"] == 200 and call["attempt_id"] == "op-1-abc"
    assert set(call["timings"]) == {"posted_at", "first_byte_at", "completed_at", "duration_s"}
    assert call["timings"]["first_byte_at"].endswith("Z") and "T" in call["timings"]["posted_at"]
    assert (work / "response.bin").read_text() == '{"choices":[]}' and call["sha256"]
    assert json.loads((work / "RESULT.json").read_text()) == {
        "result_ref": "app://results/1",
        "export": "succeeded",
        "capture_attempt_id": "op-1-abc",
    }


def test_no_response_is_unknown_and_never_posted_again(tmp_path):
    script = ["GET https://ep.example/health 200 ok", "POST https://ep.example/v1/chat 000"]
    r, work, calls = _run(tmp_path, BINDING, script)
    assert r.returncode == 4 and [c.split()[0] for c in calls] == ["GET", "POST"]
    call = json.loads((work / "call.json").read_text())
    assert call["capture_complete"] is False and call["outcome"] == "unknown"
    assert json.loads((work / "RESULT.json").read_text())["export"] == "failed"
    # a retried pod of the same operation: the marker stands, nothing is posted
    script = ["GET https://ep.example/health 200 ok", 'POST https://ep.example/v1/chat 200 {"x":1}']
    r2, work2, calls2 = _run(tmp_path, BINDING, script, work_files={"posted.json": (work / "posted.json").read_text()})
    assert r2.returncode == 4 and calls2 == []
    assert "unknown" in r2.stdout


def test_empty_gateway_timeout_is_unknown_and_a_429_is_asked_again(tmp_path):
    script = ["GET https://ep.example/health 200 ok", "POST https://ep.example/v1/chat 504"]
    r, work, calls = _run(tmp_path, BINDING, script)
    assert r.returncode == 4 and len([c for c in calls if c.startswith("POST")]) == 1
    script = [
        "GET https://ep.example/health 200 ok",
        "POST https://ep.example/v1/chat 429",
        'POST https://ep.example/v1/chat 200 {"ok":1}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(tmp_path / "second", BINDING, script)
    assert r.returncode == 0 and len([c for c in calls if c.startswith("POST")]) == 2


def test_delivered_4xx_fails_the_call_and_a_failed_upload_keeps_the_capture(tmp_path):
    script = [
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 422 {"error":"bad input"}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(tmp_path, BINDING, script)
    assert r.returncode == 2 and json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"
    assert json.loads((work / "call.json").read_text())["http_status"] == 422
    script = [
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 200 {"ok":1}',
        "PUT https://objects.example/put/response 500",
        "PUT https://objects.example/put/response 500",
        "PUT https://objects.example/put/response 500",
        "PUT https://objects.example/put/response 500",
        "PUT https://objects.example/put/response 500",
        "PUT https://objects.example/put/response 500",
    ]
    r, work, calls = _run(tmp_path / "second", BINDING, script, env={"CALL_TIMEOUT": "10"})
    assert r.returncode == 0 and json.loads((work / "RESULT.json").read_text())["export"] == "failed"
    assert (work / "response.bin").exists() and json.loads((work / "call.json").read_text())["capture_complete"] is True


def test_transfer_only_delivers_a_complete_capture_and_refuses_an_incomplete_one(tmp_path):
    capture = {
        "operation_id": "op-1",
        "attempt_id": "op-1-old",
        "http_status": 200,
        "content_type": "application/json",
        "capture_complete": True,
    }
    script = ["PUT https://objects.example/put/response 200", "PUT https://objects.example/put/call 200"]
    r, work, calls = _run(
        tmp_path,
        BINDING,
        script,
        env={"TRANSFER_ONLY": "1"},
        work_files={"call.json": json.dumps(capture), "response.bin": "{}"},
    )
    assert r.returncode == 0 and [c.split()[0] for c in calls] == ["PUT", "PUT"]
    assert json.loads((work / "RESULT.json").read_text())["capture_attempt_id"] == "op-1-old"
    r, work, calls = _run(tmp_path / "second", BINDING, [], env={"TRANSFER_ONLY": "1"})
    assert r.returncode == 3 and calls == [] and json.loads((work / "RESULT.json").read_text())["export"] == "failed"


def test_refresh_supplies_the_urls_with_the_identity_header(tmp_path):
    binding = {**BINDING, "refresh_url": "https://app.example/refresh", "refresh_token": "rt"}
    del binding["response_url"]
    del binding["call_url"]
    ident = tmp_path / "token"
    ident.write_text("projected")
    script = [
        "GET https://ep.example/health 200 ok",
        'POST https://app.example/refresh 200 {"input_url":"","response_url":"https://objects.example/fresh/response","call_url":"https://objects.example/fresh/call"}',
        'POST https://ep.example/v1/chat 200 {"ok":1}',
        'POST https://app.example/refresh 200 {"response_url":"https://objects.example/fresh/response","call_url":"https://objects.example/fresh/call"}',
        "PUT https://objects.example/fresh/response 200",
        "PUT https://objects.example/fresh/call 200",
    ]
    r, work, calls = _run(tmp_path, binding, script, env={"IDENTITY_TOKEN_FILE": str(ident)})
    assert r.returncode == 0, r.stdout + r.stderr
    refreshes = [c for c in calls if "app.example/refresh" in c]
    assert len(refreshes) == 2 and '"phase":"input"' in refreshes[0] and '"phase":"output"' in refreshes[1]


def test_a_captured_endpoint_error_is_delivered_but_never_a_success(tmp_path):
    """Delivery is independent of the inference outcome: a 5xx capture ends with exit 5 (the endpoint's error), a
    retained 4xx re-delivered with TRANSFER_ONLY ends with exit 2, a retained 2xx with 0."""
    script = [
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 500 {"error":"cuda"}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, work, calls = _run(tmp_path, BINDING, script)
    assert r.returncode == 5 and json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"
    assert json.loads((work / "call.json").read_text())["http_status"] == 500
    for status, code in ((422, 2), (200, 0), (503, 5)):
        capture = {
            "operation_id": "op-1",
            "attempt_id": "op-1-old",
            "http_status": status,
            "content_type": "application/json",
            "capture_complete": True,
        }
        script = ["PUT https://objects.example/put/response 200", "PUT https://objects.example/put/call 200"]
        r, work, calls = _run(
            tmp_path / f"retry-{status}",
            BINDING,
            script,
            env={"TRANSFER_ONLY": "1"},
            work_files={"call.json": json.dumps(capture), "response.bin": "{}"},
        )
        assert r.returncode == code, (status, r.stdout)
        assert json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"


def test_caller_names_its_operation_and_attempt_next_to_the_admission(tmp_path):
    """With an admission token the caller sends x-serverless2-operation and x-serverless2-attempt on every gateway call
    (readiness and the POST): the hook verifies the token against them (docs/API.md "Endpoint calls")."""
    headers = tmp_path / "headers.txt"
    script = [
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 200 {"choices":[]}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    r, _work, calls = _run(tmp_path, BINDING, script, env={"ADMITTED": "tok", "HEADERS": str(headers)})
    assert r.returncode == 0, r.stdout + r.stderr
    sent = headers.read_text().splitlines()
    assert sent.count("x-serverless2-admitted: tok") >= 2  # the readiness GET and the POST
    assert sent.count("x-serverless2-operation: op-1") >= 2 and sent.count("x-serverless2-attempt: op-1-abc") >= 2
    # without an admission token nothing names an operation (a direct call stays a direct call)
    headers.write_text("")
    r, _work, _calls = _run(tmp_path / "plain", BINDING, script, env={"ADMITTED": "", "HEADERS": str(headers)})
    assert r.returncode == 0 and "x-serverless2-operation" not in headers.read_text()


QUICK = {"READY_BACKOFF_BASE": "0", "READY_MAX_WAIT": "5"}
FULL = [
    "GET https://ep.example/health 200 ok",
    'POST https://ep.example/v1/chat 200 {"choices":[]}',
    "PUT https://objects.example/put/response 200",
    "PUT https://objects.example/put/call 200",
]


def _posts(calls):
    return [c for c in calls if c.startswith("POST https://ep.example")]


def _puts(calls):
    return [c for c in calls if c.startswith("PUT ")]


def test_sync_failure_before_the_post_means_no_post_and_a_retryable_exit(tmp_path):
    """The one-POST marker could not be made durable: nothing is posted, the marker is taken back, exit 6 (retryable);
    the restart (sync working again) posts exactly once and delivers."""
    script = list(FULL)
    r, work, calls = _run(tmp_path, BINDING, script, env={**QUICK, "SYNC_FAIL": "all"})
    assert r.returncode == 6, r.stdout + r.stderr
    assert _posts(calls) == [] and _puts(calls) == []
    assert not (work / "posted.json").exists() and not (work / "call.json").exists()
    assert not (work / "RESULT.json").exists()  # the failure record itself could not be made durable: none is claimed
    # the restart on the same work directory, sync healthy: one POST, delivered
    (tmp_path / "sync.count").unlink(missing_ok=True)
    r2, work, calls2 = _run(tmp_path, BINDING, script, env=QUICK)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert len(_posts(calls2)) == 1 and len(_puts(calls2)) == 2
    assert json.loads((work / "call.json").read_text())["capture_complete"] is True


def test_sync_failure_after_the_post_keeps_the_outcome_unknown_and_never_reposts(tmp_path):
    """The response was captured but could not be made durable: outcome unknown for good (exit 4), nothing delivered,
    no manifest; the restart sees the marker and refuses to post again."""
    script = list(FULL)
    # the marker's sync (1st call) succeeds, the capture's sync (2nd) fails
    r, work, calls = _run(tmp_path, BINDING, script, env={**QUICK, "SYNC_FAIL": "after:1"})
    assert r.returncode == 4, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and _puts(calls) == []
    assert (work / "posted.json").exists() and not (work / "call.json").exists()
    assert not (work / "RESULT.json").exists()
    assert "could not be made durable" in r.stdout
    (tmp_path / "sync.count").unlink(missing_ok=True)
    r2, work, calls2 = _run(tmp_path, BINDING, script, env=QUICK)
    assert r2.returncode == 4 and _posts(calls2) == [] and _puts(calls2) == []  # never a second POST
    assert json.loads((work / "call.json").read_text())["outcome"] == "unknown"


def test_manifest_or_result_sync_failure_defers_without_a_false_success(tmp_path):
    """The bytes are durable but the manifest is not: nothing delivered, exit 6; the restart delivers the complete
    capture without posting. RESULT.json not durable after a delivery: removed, exit 6, no export success claimed."""
    script = list(FULL)
    r, work, calls = _run(
        tmp_path, BINDING, script, env={**QUICK, "SYNC_FAIL": "after:2"}
    )  # marker ok, capture ok, manifest fails
    assert r.returncode == 6, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and _puts(calls) == [] and not (work / "RESULT.json").exists()
    assert json.loads((work / "call.json").read_text())["capture_complete"] is True  # the bytes it names are durable
    (tmp_path / "sync.count").unlink(missing_ok=True)
    r2, work, calls2 = _run(tmp_path, BINDING, FULL[2:], env=QUICK)
    assert r2.returncode == 0 and _posts(calls2) == [] and len(_puts(calls2)) == 2
    assert json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"


def test_result_sync_failure_never_claims_export_success(tmp_path):
    """RESULT.json itself not durable after a delivery: removed, exit 6, nothing claims success; the capture stays
    complete for the next attempt."""
    r, work, calls = _run(
        tmp_path, BINDING, list(FULL), env={**QUICK, "SYNC_FAIL": "after:3"}
    )  # marker, capture, manifest ok
    assert r.returncode == 6, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and len(_puts(calls)) == 2 and not (work / "RESULT.json").exists()
    assert json.loads((work / "call.json").read_text())["capture_complete"] is True


def test_marker_write_or_rename_failure_means_no_post(tmp_path):
    """The one-POST marker could not be created (its rename fails): no POST, nothing claimed, exit 6; the restart on a
    healthy filesystem posts once."""
    r, work, calls = _run(tmp_path, BINDING, list(FULL), env={**QUICK, "MV_FAIL": "posted.json"})
    assert r.returncode == 6, r.stdout + r.stderr
    assert _posts(calls) == [] and _puts(calls) == []
    assert not (work / "posted.json").exists() and not (work / "posted.json.tmp").exists()
    r2, work, calls2 = _run(tmp_path, BINDING, list(FULL), env=QUICK)
    assert r2.returncode == 0 and len(_posts(calls2)) == 1


def test_manifest_rename_failure_keeps_the_marker_and_the_unknown_outcome(tmp_path):
    """call.json could not be renamed into place after the POST: outcome unknown (exit 4), posted.json stays, nothing
    delivered; the restart refuses to post again."""
    r, work, calls = _run(tmp_path, BINDING, list(FULL), env={**QUICK, "MV_FAIL": "call.json"})
    assert r.returncode == 4, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and _puts(calls) == []
    assert (work / "posted.json").exists() and not (work / "call.json").exists()
    assert not (work / "RESULT.json").exists() or json.loads((work / "RESULT.json").read_text())["export"] == "failed"
    r2, work, calls2 = _run(tmp_path, BINDING, list(FULL), env=QUICK)
    assert r2.returncode == 4 and _posts(calls2) == []


def test_result_rename_failure_never_claims_export_success(tmp_path):
    """RESULT.json could not be renamed into place after a delivery: no success claimed, exit 6; the capture stays
    complete and the restart delivers it again without posting."""
    r, work, calls = _run(tmp_path, BINDING, list(FULL), env={**QUICK, "MV_FAIL": "RESULT.json"})
    assert r.returncode == 6, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and len(_puts(calls)) == 2 and not (work / "RESULT.json").exists()
    assert json.loads((work / "call.json").read_text())["capture_complete"] is True
    r2, work, calls2 = _run(tmp_path, BINDING, FULL[2:], env=QUICK)
    assert (
        r2.returncode == 0
        and _posts(calls2) == []
        and json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"
    )


def test_partial_manifest_write_keeps_the_outcome_unknown(tmp_path):
    """jq writes a partial manifest and fails: the fragment is never renamed into place, the outcome is unknown
    (exit 4), posted.json stays, nothing is delivered; the restart refuses to post again."""
    r, work, calls = _run(tmp_path, BINDING, list(FULL), env={**QUICK, "JQ_FAIL": "manifest"})
    assert r.returncode == 4, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and _puts(calls) == []
    assert (
        (work / "posted.json").exists() and not (work / "call.json").exists() and not (work / "call.json.tmp").exists()
    )
    r2, work, calls2 = _run(tmp_path, BINDING, list(FULL), env=QUICK)
    assert r2.returncode == 4 and _posts(calls2) == []


def test_partial_result_write_never_claims_export_success(tmp_path):
    """jq writes a partial RESULT.json and fails after a delivery: nothing claims success, exit 6; the capture stays
    complete and the restart delivers it again without posting."""
    r, work, calls = _run(tmp_path, BINDING, list(FULL), env={**QUICK, "JQ_FAIL": "result"})
    assert r.returncode == 6, r.stdout + r.stderr
    assert len(_posts(calls)) == 1 and len(_puts(calls)) == 2
    assert not (work / "RESULT.json").exists() and not (work / "RESULT.json.tmp").exists()
    assert json.loads((work / "call.json").read_text())["capture_complete"] is True
    r2, work, calls2 = _run(tmp_path, BINDING, FULL[2:], env=QUICK)
    assert r2.returncode == 0 and _posts(calls2) == []
    assert json.loads((work / "RESULT.json").read_text())["export"] == "succeeded"
