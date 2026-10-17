"""The caller's readiness probes (services/jobs/bin/call.sh): frequent and short while the gateway holds a cold start,
deterministic pauses (2, 4, 8, 10, 10, ... s), bounded wall-clock budget, no effect on the inference POST's retry budget
and never a second POST. Driven with stub curl and sleep: no real time passes."""

import stat
from pathlib import Path

import test_call_sh as base

pytestmark = base.pytestmark

STUB = (
    base.STUB.replace(
        "    -D|--max-time|--connect-to|-w) ((i++));;",
        '    --max-time) echo "${args[$((i+1))]}" >> "${MAXTIMES:-/dev/null}"; ((i++));;\n'
        "    -w) fmt=${args[$((i+1))]}; ((i++));;\n"
        "    -D|--connect-to) ((i++));;",
    )
    # the real curl prints exactly the -w format: a readiness probe (-w '%{http_code}') gets the code alone, so a 4xx
    # probe answer is matched by call.sh's exact cases (the base stub always prints the POST's four fields)
    .replace(
        """printf '%s 0.5 1.5 %s' "$code" "${#body}\"""",
        """if [ "${fmt:-}" = '%{http_code}' ]; then printf '%s' "$code"; else printf '%s 0.5 1.5 %s' "$code" "${#body}"; fi""",
    )
)
assert STUB != base.STUB


def _run(tmp_path, script, env=None):
    """base._run with a sleep stub that records every pause instead of sleeping, and a curl stub that records --max-time."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    sleeps = tmp_path / "sleeps.txt"
    sleeps.write_text("")
    sl = bin_ / "sleep"
    sl.write_text(f'#!/bin/bash\necho "$1" >> "{sleeps}"\n')
    sl.chmod(sl.stat().st_mode | stat.S_IEXEC)
    maxtimes = tmp_path / "maxtimes.txt"
    maxtimes.write_text("")
    orig = base.STUB
    base.STUB = STUB
    try:
        res, _work, _calls = base._run(
            tmp_path, base.BINDING, script, env={"MAXTIMES": str(maxtimes), "BACKOFF_BASE": "0", **(env or {})}
        )
    finally:
        base.STUB = orig
    pauses = [int(float(x)) for x in sleeps.read_text().split()]
    return res, pauses, [int(x) for x in maxtimes.read_text().split()]


def _calls(tmp_path):
    return [line.split(" ")[:2] for line in (tmp_path / "calls.txt").read_text().splitlines()]


def test_a_cold_start_is_probed_often_with_short_timeouts_and_capped_pauses(tmp_path):
    script = ["GET https://ep.example/health 000"] * 6 + [
        "GET https://ep.example/health 200 ok",
        'POST https://ep.example/v1/chat 200 {"ok":true}',
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    res, pauses, maxtimes = _run(tmp_path, script)
    assert res.returncode == 0, res.stdout + res.stderr
    gets = [c for c in _calls(tmp_path) if c[0] == "GET"]
    posts = [c for c in _calls(tmp_path) if c[0] == "POST"]
    assert len(gets) == 7 and len(posts) == 1  # seven probes, then the one inference POST
    assert pauses[:6] == [2, 4, 8, 10, 10, 10]  # 2 s doubling, capped at 10 s: a ready replica is seen within ~40 s
    assert maxtimes[:7] == [30] * 7  # every probe ends within 30 s instead of 120 s
    assert "readiness probe 7 -> HTTP 200" in res.stdout


def test_pause_and_timeout_are_tunable_and_the_post_budget_is_untouched(tmp_path):
    script = [
        "GET https://ep.example/health 503",
        "GET https://ep.example/health 000",
        "GET https://ep.example/health 200 ok",
        "POST https://ep.example/v1/chat 429",
        "POST https://ep.example/v1/chat 200 {}",
        "PUT https://objects.example/put/response 200",
        "PUT https://objects.example/put/call 200",
    ]
    res, pauses, maxtimes = _run(
        tmp_path, script, env={"READY_BACKOFF_BASE": "1", "READY_BACKOFF_MAX": "3", "READY_TIMEOUT": "7"}
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert pauses[:2] == [1, 2] and maxtimes[:3] == [7, 7, 7]
    # the inference backoff is the POST's own (BACKOFF_BASE 0 here): the readiness probes consumed none of it, and the
    # 429 was asked again exactly once, the inference POST then made once
    assert [c for c in _calls(tmp_path) if c[0] == "POST"] == [["POST", "https://ep.example/v1/chat"]] * 2
    assert "readiness probe 1 -> HTTP 503" in res.stdout and "readiness probe 3 -> HTTP 200" in res.stdout


def test_the_readiness_budget_fails_explicitly_without_posting(tmp_path):
    res, pauses, _ = _run(tmp_path, ["GET https://ep.example/health 000"], env={"READY_MAX_WAIT": "0"})
    assert res.returncode == 5, res.stdout + res.stderr
    assert [c[0] for c in _calls(tmp_path)] == ["GET"]  # no POST, nothing delivered
    assert pauses == []  # gave up at the budget, no pause
    assert "did not become ready" in (tmp_path / "work" / "RESULT.json").read_text()
    assert "giving up before the inference" in res.stdout
    assert not (tmp_path / "work" / "posted.json").exists()


def test_a_refused_key_still_fails_at_once(tmp_path):
    res, pauses, _ = _run(tmp_path, ["GET https://ep.example/health 403 nope"])
    assert res.returncode == 2 and pauses == [] and [c[0] for c in _calls(tmp_path)] == ["GET"]


def test_default_budget_is_the_call_timeout():
    text = Path(base.CALL).read_text()
    assert "ready_max=${READY_MAX_WAIT:-${CALL_TIMEOUT:-3600}}" in text
    assert '--max-time "${READY_TIMEOUT:-30}"' in text and "backoff $n" in text  # the POST keeps its own backoff
