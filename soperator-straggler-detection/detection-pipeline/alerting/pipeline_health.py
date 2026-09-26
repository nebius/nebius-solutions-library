"""P20d-closeout -- pipeline dead-man's-switch.

A genuinely different question from coverage_guard.py's peer-relative/
absolute-rate checks. Those run only as a trust-modifier AFTER a rank-level
alert has already fired (alert_engine._emit() -> check_coverage()), and
answer "how much do I trust this specific alert." This module answers a
prior, more basic question that must be checked UNCONDITIONALLY every poll
cycle, alert-or-not: "is data from this aggregator actually arriving in VM
at all, right now?"

This is exactly the demonstrated failure this closes: the run3 vm_url
incident. Both node_aggregator_ref.py instances pushed to a wrong URL for
3h10m -- every single push failed with HTTP 400. Because no fresh data
ever existed in VM, no rank-level alert ever fired (empty query results,
zero iterations in alert_engine._check_cv/_check_mean), so
coverage_guard.check_coverage() -- whose own staleness-detection logic
(lines ~56-66, ~78-81) is real and already correct for exactly this
situation -- never got invoked at all. The bug wasn't in that logic; it
was that its only call site is conditional on an alert already existing.

This module deliberately reuses coverage_guard's same query+staleness
pattern (for consistency, not novelty) against a decoupled heartbeat
metric the aggregator pushes on its own wall-clock timer (see
node_aggregator_ref.py's maybe_heartbeat()), independent of whether any
real training record has been seen -- so this stays meaningful during a
legitimately data-quiet stretch (idle/crashed job), which is a different,
also-real failure mode from "push channel is broken," and must not be
conflated with it or with a genuine healthy zero-alert read.
"""
import urllib.request
import urllib.parse
import json
import time

# Measured, not invented (same discipline as thresholds.py's CV/mean
# thresholds): this VM instance runs with its default -search.latencyOffset
# (30s, confirmed via vm.log/process args -- not explicitly set, so it's
# VM's stock default), which makes every instant query's freshest visible
# sample lag "now" by ~30s regardless of push health. Measured directly
# against a genuinely healthy, continuously-pushing aggregator (14 samples
# over ~110s): heartbeat age oscillates 29.6s-38.9s the entire time (VM's
# ~30s baseline lag plus the heartbeat's own 10s push-cadence sawtooth).
# The original guess of 30.0s sat INSIDE that normal healthy oscillation
# band -- it would have false-fired roughly half the time on a perfectly
# healthy system. 90s gives real margin above the measured ~39s healthy
# ceiling (>2x) while still catching a real outage in 90s instead of the
# 3h10m the original incident actually ran undetected.
HEARTBEAT_STALE_THRESH_S = 90.0
CONSEC_FAIL_THRESH = 3            # sustained failures, not one network blip


def _query_instant(vm_url, promql):
    qs = urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(f"{vm_url}/api/v1/query?{qs}", timeout=10) as resp:
        d = json.load(resp)
    return d.get("data", {}).get("result", [])


def _freshest_row(vm_url, promql):
    """Row-SELECTION analog of alert_engine.py's _query_instant_real_ts
    (a field-EXTRACTION fix for the same underlying value[0]-is-eval-time
    issue). Here the risk isn't a wrong field on a single row -- it's that
    promql can legitimately return MULTIPLE rows for the same hostname at
    once (e.g. two slurm_job_ids' heartbeat series coexisting right after
    a job restart/relaunch, old job's aggregator not yet scraped out of
    VM's lookback window). Blindly taking result[0] previously picked
    whatever row VM happened to return first (label-sort order, not
    recency) -- demonstrated directly: an old job's 162s-stale heartbeat
    was selected over a new job's genuinely fresh one for the same
    hostname, producing a false PIPELINE-DOWN. This queries promql AND
    timestamp(promql) (correlating by label set, excluding __name__ since
    timestamp() strips it -- same correlation trick as
    _query_instant_real_ts), and returns the single row with the most
    recent real sample time, plus that real timestamp. (None, None) if no
    rows at all."""
    val_res = _query_instant(vm_url, promql)
    if not val_res:
        return None, None
    ts_res = _query_instant(vm_url, f"timestamp({promql})")
    ts_by_labels = {}
    for row in ts_res:
        key = tuple(sorted((k, v) for k, v in row["metric"].items() if k != "__name__"))
        ts_by_labels[key] = float(row["value"][1])
    best_row, best_ts = None, float("-inf")
    for row in val_res:
        key = tuple(sorted((k, v) for k, v in row["metric"].items() if k != "__name__"))
        real_ts = ts_by_labels.get(key)
        if real_ts is not None and real_ts > best_ts:
            best_row, best_ts = row, real_ts
    return best_row, best_ts


def check_pipeline_health(vm_url, hostname):
    """Returns {down: bool, reasons: [...], last_heartbeat_age_s,
    consecutive_push_failures}. "down" means "cannot evaluate this host
    right now" -- structurally distinct from a real, healthy, zero-alert
    read, and must never be silently conflated with it by a caller."""
    reasons = []
    # _freshest_row (not a bare instant query, and not result[0]) for the
    # same reason timestamp(...) is deliberate here, not cosmetic: a plain
    # instant query's value[0] is the QUERY's own evaluation time, not the
    # underlying sample's real ingestion time, AND when multiple rows come
    # back for this hostname (multiple job ids), result[0] is not
    # necessarily the freshest one either -- both caught by real
    # reproduction tests (see _freshest_row's docstring).
    hb_row, ts = _freshest_row(vm_url, f'agg_aggregator_heartbeat{{hostname="{hostname}"}}')
    age = None
    if hb_row is None:
        reasons.append(
            f"no agg_aggregator_heartbeat sample at all for hostname={hostname} -- "
            f"aggregator process is down, unreachable, or has never pushed within "
            f"VM's staleness window"
        )
    else:
        age = time.time() - ts
        if age > HEARTBEAT_STALE_THRESH_S:
            reasons.append(
                f"last heartbeat for hostname={hostname} is {age:.1f}s old "
                f"(> {HEARTBEAT_STALE_THRESH_S:.0f}s threshold) -- pushes have stopped "
                f"landing in VM even though the aggregator may still be running and "
                f"computing locally (this is the exact run3 vm_url incident's signature)"
            )

    cf_row, _ = _freshest_row(vm_url, f'agg_consecutive_push_failures{{hostname="{hostname}"}}')
    consec = None
    if cf_row is not None:
        consec = float(cf_row["value"][1])
        if consec >= CONSEC_FAIL_THRESH:
            reasons.append(
                f"{consec:.0f} consecutive push failures self-reported by the "
                f"aggregator for hostname={hostname}"
            )

    return {
        "down": bool(reasons),
        "reasons": reasons,
        "last_heartbeat_age_s": age,
        "consecutive_push_failures": consec,
    }
