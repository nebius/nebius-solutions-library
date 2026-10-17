"""The uploader (services/jobs/bin/upload.sh) after a successful main container: the work volume is released only
when delivery is durable. An application-managed run (UPLOAD_MODE=record) whose RESULT.json reports `export: failed`
(an endpoint caller whose own S3 delivery was refused, the capture kept for `:export`) keeps the volume, even though
the API acknowledged that record. Driven with a stub curl (pod status, attempt records, the PVC delete) and a stub aws;
the script hard-codes /work, so a copy with /work rebased into the test directory runs."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

UPLOAD = Path(__file__).resolve().parents[2] / "jobs" / "bin" / "upload.sh"
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None, reason="bash and jq are needed"
)

CURL = r"""#!/bin/bash
# GET <pod url> -> the pod's status (main terminated, exit 0); POST .../internal/attempts -> ACK_CODE (default 200) with
# an ack body; DELETE .../persistentvolumeclaims/<name> -> PVC_DELETE_CODE (default 200); every call is appended to CALLS
args=("$@"); method=GET; out=""; w=""; url=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -X) method=${args[$((i+1))]}; ((i++));;
    -o) out=${args[$((i+1))]}; ((i++));;
    -w) w=${args[$((i+1))]}; ((i++));;
    -H|--cacert|-m|--data-binary) ((i++));;
    *) [[ "${args[$i]}" == http* ]] && url=${args[$i]};;
  esac
done
echo "$method $url" >> "$CALLS"
case "$method $url" in
  "GET "*/pods/*) if [ -n "${POD_JSON_RUNNING:-}" ] && [ "$(grep -c '^GET' "$CALLS")" -le "${RUNNING_POLLS:-0}" ]; then printf '%s' "$POD_JSON_RUNNING"; else printf '%s' "$POD_JSON"; fi;;
  "POST "*/internal/attempts) code=${ACK_CODE:-200}; [ -n "$out" ] && printf '{"epoch":1}' > "$out";;
  "DELETE "*/persistentvolumeclaims/*) code=${PVC_DELETE_CODE:-200};;
  *) code=404;;
esac
[ -n "$w" ] && printf '%s' "${w//%\{http_code\}/$code}"
exit 0
"""

AWS = '#!/bin/bash\n# every s3 call succeeds unless AWS_FAIL=1 (sync mode export failure); prints nothing\n[ "${AWS_FAIL:-0}" = 1 ] && exit 1\nexit 0\n'


def _run(tmp_path, *, mode, result=None, env=None, alive_polls=None):
    work = tmp_path / "work"
    (work / "attempts").mkdir(parents=True)
    if result is not None:
        (work / "RESULT.json").write_text(json.dumps(result))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("curl", CURL), ("aws", AWS)):
        stub = bin_ / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = (
        tmp_path / "upload.sh"
    )  # /work rebased; the service-account token and CA paths may not exist (read errors are tolerated)
    text = UPLOAD.read_text().replace("/work", str(work))
    if alive_polls is not None:
        # the PID-namespace scan cannot be driven on a developer host: main counts as alive for the first GETs
        text = text.replace(
            "record() {", f'main_alive() {{ [ "$(grep -c \'^GET\' "$CALLS")" -lt {alive_polls} ]; }}\nrecord() {{', 1
        )
    script.write_text(text)
    pod = {
        "spec": {"nodeName": "n1", "containers": [{"name": "main", "image": "img:1"}]},
        "status": {
            "containerStatuses": [
                {
                    "name": "main",
                    "imageID": "img@sha256:abc",
                    "state": {
                        "terminated": {
                            "exitCode": 0,
                            "startedAt": "2026-10-15T00:00:00Z",
                            "finishedAt": "2026-10-15T00:01:00Z",
                        }
                    },
                }
            ]
        },
    }
    calls = tmp_path / "calls"
    calls.write_text("")
    environment = {
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "POD_NAME": "op-1-0",
        "POD_NAMESPACE": "tenant-a",
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        "KUBERNETES_SERVICE_PORT": "443",
        "OPERATION": "op-1",
        "UPLOAD_MODE": mode,
        "OUTPUT_PREFIX": "s3://bucket/op-1",
        "API_URL": "https://api.internal",
        "PVC_NAME": "op-1-work",
        "POD_JSON": json.dumps(pod),
        "POD_JSON_RUNNING": json.dumps(
            {
                **pod,
                "status": {
                    "containerStatuses": [
                        {
                            "name": "main",
                            "imageID": "img@sha256:abc",
                            "state": {"running": {"startedAt": "2026-10-15T00:00:00Z"}},
                        }
                    ]
                },
            }
        ),
        "CALLS": str(calls),
        **(env or {}),
    }
    done = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout, calls.read_text().splitlines()


def test_a_failed_application_export_keeps_the_work_volume_for_the_transfer_retry(tmp_path):
    """op-bb1275a5f615d662 (2026-10-15): inference succeeded, the caller's S3 PUT was refused (full bucket), RESULT.json
    said export=failed, the API acknowledged that record, and the uploader deleted the PVC that `:export` needed."""
    out, calls = _run(
        tmp_path,
        mode="record",
        result={"export": "failed", "error": "HTTP 403", "result_ref_pending": "s3://b/op-1/response.bin"},
    )
    assert "status=succeeded export=failed (application-managed I/O)" in out
    assert "attempt record succeeded -> API 200" in out
    assert not any(c.startswith("DELETE ") for c in calls), calls
    assert "volume op-1-work kept" in out and "POST /v1/operations/op-1:export" in out


def test_a_delivered_application_result_releases_the_work_volume(tmp_path):
    out, calls = _run(tmp_path, mode="record", result={"result_ref": "s3://b/op-1/response.bin", "export": "succeeded"})
    assert "export=succeeded" in out and "pvc delete 200" in out
    assert [c for c in calls if c.startswith("DELETE ")] == [
        "DELETE https://10.0.0.1:443/api/v1/namespaces/tenant-a/persistentvolumeclaims/op-1-work"
    ]


def test_an_application_result_without_an_export_state_is_the_applications_own_delivery(tmp_path):
    """A scientific run that moved its own data: RESULT.json names the result, the platform exported nothing (n/a)."""
    out, calls = _run(tmp_path, mode="record", result={"result_ref": "app://run/1", "checkpoint_ref": "app://ckpt/1"})
    assert "export=n/a" in out and any(c.startswith("DELETE ") for c in calls)


def test_no_finalization_record_keeps_the_work_volume(tmp_path):
    out, calls = _run(tmp_path, mode="record")
    assert not any(c.startswith("DELETE ") for c in calls)
    assert "kept: delivery not acknowledged (export=n/a ack=200 result=no)" in out


def test_an_unacknowledged_record_keeps_the_volume_even_when_delivered(tmp_path):
    out, calls = _run(
        tmp_path,
        mode="record",
        result={"result_ref": "s3://b/op-1/response.bin", "export": "succeeded"},
        env={"ACK_CODE": "409"},  # a stale epoch: the record is refused, never acknowledged
    )
    assert not any(c.startswith("DELETE ") for c in calls) and "ack=409" in out


@pytest.mark.parametrize("fail,deleted", [(0, True), (1, False)])
def test_managed_io_releases_the_volume_only_after_a_verified_export(tmp_path, fail, deleted):
    out, calls = _run(tmp_path, mode="sync", env={"AWS_FAIL": str(fail)})
    assert any(c.startswith("DELETE ") for c in calls) is deleted
    assert ("export=succeeded" in out) is deleted and ("UPLOAD FAILED" in out) is not deleted


def test_the_exit_code_is_waited_for_without_bound_once_main_is_gone(tmp_path):
    """op-e0f60cf93b5c9b42 (2026-10-15): main exited 0 and the kubelet reported the container as running for two more
    minutes; the uploader gave up after 40 polls and recorded the completed attempt as interrupted. Now, outside a
    termination, it keeps polling (a log line every 20 polls) until the API reports the exit code, and records what
    main did: the pod cannot end before this container does."""
    out, calls = _run(
        tmp_path,
        mode="record",
        result={"result_ref": "app://run/1"},
        env={"RUNNING_POLLS": "25", "POLL_SECONDS": "0"},
        alive_polls=3,  # the start record, then main's processes are gone while the API still says running
    )
    assert "attempt record started -> API 200" in out
    assert "main ended, waiting for the API to report its exit code (20 polls)" in out
    assert "attempt record succeeded -> API 200" in out and "interrupted" not in out
    assert sum(c.startswith("GET ") for c in calls) > 25 and any(c.startswith("DELETE ") for c in calls)
