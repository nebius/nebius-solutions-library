"""P20c Step 2 -- coverage guard.

Wires agg_samples_seen and the four *_total counters (added P20b:
agg_records_seen_total, agg_mean_windows_total, agg_cv_windows_total,
agg_pushes_total) into the alert path as a trust modifier, not a second
detector. A real fault detected during degraded coverage is less
trustworthy than one detected during full coverage -- this caps confidence
rather than firing or silently suppressing.

KNOWN BLIND SPOT (documented here, in the alert's own output, not just the
dashboard's): the peer-relative check (agg_samples_seen / max-among-node-
peers) is blind to sample loss that hits every rank on a node uniformly
(a node-wide aggregator issue, or a dedup problem affecting all 8 ranks
equally) -- all ranks would still agree and read 1.0 even if the true
expected rate (calibrated healthy baseline ~50/sec/rank for nanoGPT bucket
B/C) was higher than what's actually arriving. This module partially
closes that gap (the *_total counters give a second, non-peer-relative
signal: an absolute throughput floor), but a rank-by-rank comparison alone
never can -- see check_coverage()'s "absolute" component below.
"""
import urllib.request
import urllib.parse
import json
import time

ABSOLUTE_RATE_FLOOR_FRAC = 0.5  # below 50% of calibrated rate -> degraded
PEER_COVERAGE_FLOOR = 0.9       # below 90% of the node's max peer -> degraded
XJOB_LOOKBACK_S = 30 * 86400    # same precedent as node_aggregator_ref.py's THROUGHPUT_XJOB_LOOKBACK_S
XJOB_MIN_HISTORY = 3            # same "3 independent data points" precedent as PERSIST_WINDOW/THROUGHPUT_XJOB_MIN_HISTORY
SIG_LOOKUP_LOOKBACK_S = 3600    # a job's own sig-info sample only needs to survive within one job's lifetime


def _query_instant(vm_url, promql):
    qs = urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(f"{vm_url}/api/v1/query?{qs}", timeout=10) as resp:
        d = json.load(resp)
    return d.get("data", {}).get("result", [])


def _query_instant_real_ts(vm_url, promql):
    """P26.5-maintenance fix -- a plain instant query's value[0] is the
    QUERY's own evaluation time, not the underlying sample's real
    ingestion time (the same bug already found and fixed this project's
    own alert_engine.py/pipeline_health.py). Used here specifically so
    the absolute-rate check below can measure its two snapshots' REAL
    elapsed time from the samples themselves, instead of assuming the
    `time.sleep(2.0)` between the two calls was exactly 2.000s of real
    elapsed time (it wasn't necessarily -- HTTP round-trip/VM response
    jitter on either call skews the true gap, silently under/over-stating
    the computed rate). Same fix shape as alert_engine.py's own
    _query_instant_real_ts: issue a second query wrapping the same
    expression in PromQL's timestamp() function, correlate its real
    per-series timestamps back onto the original rows by label set --
    EXCLUDING __name__ from that correlation key on both sides.
    timestamp()'s own result vector drops __name__ (a real PromQL/VM
    behavior, not a guess -- alert_engine.py's own _query_instant_real_ts
    docstring records hitting this directly: its first version silently
    dropped every row by comparing label sets that included __name__ on
    only one side). Caught live this session by testing this new copy
    against a real pushed metric before trusting it -- confirmed the
    exact same mistake reproduced here, fixed to match."""
    qs = urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(f"{vm_url}/api/v1/query?{qs}", timeout=10) as resp:
        d = json.load(resp)
    rows = d.get("data", {}).get("result", [])
    ts_qs = urllib.parse.urlencode({"query": f"timestamp({promql})"})
    with urllib.request.urlopen(f"{vm_url}/api/v1/query?{ts_qs}", timeout=10) as resp:
        ts_d = json.load(resp)
    ts_rows = ts_d.get("data", {}).get("result", [])
    ts_by_labels = {}
    for r in ts_rows:
        key = tuple(sorted((k, v) for k, v in r["metric"].items() if k != "__name__"))
        ts_by_labels[key] = float(r["value"][1])
    out = []
    for r in rows:
        key = tuple(sorted((k, v) for k, v in r["metric"].items() if k != "__name__"))
        real_ts = ts_by_labels.get(key)
        if real_ts is not None:
            out.append({"metric": r["metric"], "value": [real_ts, r["value"][1]]})
        else:
            out.append(r)
    return out


def _lookup_workload_sig(vm_url, hostname, slurm_job_id):
    """P26 fix -- discovers THIS job's own workload_signature() value via
    the agg_job_workload_sig_info{sig=...} info-metric node_aggregator_
    ref.py pushes once it's computed one (same point/same value it uses
    for agg_job_throughput_rate_ref -- never recomputed independently
    here, since this process has no access to the internal calib state
    the signature is built from). last_over_time (not a plain instant
    query) because that push may be well outside VM's ~5min staleness
    window by the time this runs later in a long job. Returns None if
    no sig has been computed yet for this job (e.g. still mid-
    calibration) -- not an error, just "not established yet"."""
    if not slurm_job_id:
        return None
    try:
        promql = (f'last_over_time(agg_job_workload_sig_info{{hostname="{hostname}",'
                  f'slurm_job_id="{slurm_job_id}"}}[{SIG_LOOKUP_LOOKBACK_S}s])')
        result = _query_instant(vm_url, promql)
        if not result:
            return None
        return result[0]["metric"].get("sig")
    except Exception:
        return None


def _lookup_xjob_rate_reference(vm_url, hostname, sig, slurm_job_id):
    """P26 fix -- coverage_guard's absolute-rate floor, reusing the EXACT
    same cross-job-reference infrastructure P23 already built and
    validated for agg_job_throughput_ratio_to_baseline (query_throughput_
    history's own pattern), instead of the old CALIBRATED_RATE_PER_SEC=
    50.0 hardcoded constant (measured against nanoGPT only, confirmed
    live to make coverage_guard treat every genuinely-healthy ResNet run
    as DEGRADED -- ResNet's real per-node rate is ~3.15x lower than
    nanoGPT's).

    agg_job_throughput_rate_ref's stored raw_rate is already a total
    node-wide records/sec figure (d_records/dt across the WHOLE
    aggregator, not divided by member/bucket count) -- exactly the same
    units as this function's own `rate` variable (agg_records_seen_
    total's growth rate). No separate n_active normalization is needed:
    the historical reference already reflects a healthy run of a
    matching signature, at whatever member/bucket scale that signature
    implies.

    Returns (expected_rate, n_history) -- expected_rate is None if fewer
    than XJOB_MIN_HISTORY independent historical runs exist yet for this
    exact sig (a new/rare workload signature) -- report nothing rather
    than guess, same philosophy as node_aggregator_ref.py's own cold-
    start fallback for the sibling check."""
    if not sig:
        return None, 0
    try:
        promql = (f'last_over_time(agg_job_throughput_rate_ref{{hostname="{hostname}",'
                  f'sig="{sig}"}}[{XJOB_LOOKBACK_S}s])')
        result = _query_instant(vm_url, promql)
        values = [float(r["value"][1]) for r in result
                  if r.get("metric", {}).get("slurm_job_id") != slurm_job_id]
        if len(values) < XJOB_MIN_HISTORY:
            return None, len(values)
        values.sort()
        mid = len(values) // 2
        ref = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
        return ref, len(values)
    except Exception:
        return None, 0


def check_coverage(vm_url, hostname, comm, bucket, member, slurm_job_id=None):
    """Returns a dict: {degraded: bool, peer_fraction, absolute_rate_frac,
    reasons: [...]}. Two independent checks, either can flag degraded:

    1. Peer-relative (matches the dashboard's Coverage panel): this
       member's agg_samples_seen vs. the max among its node AND
       communicator peers, same bucket.
    2. Absolute-rate (the blind-spot mitigation): agg_records_seen_total's
       growth rate for this node over the last ~10s, vs. a calibrated
       floor scaled by however many (comm,bucket) member-slots are
       actually active on this node right now. Uniform node-wide loss
       shows up here even though it can't show up in (1).

    P21.5 -- two real fixes:
    - `comm` is now part of the query/grouping (both the label filter and
      the group_left/max-by dimensions) -- without it, two DIFFERENT
      communicators that happen to report the same bucket byte value
      (nothing prevents this; each communicator's message-size space is
      discovered independently) could get compared against each other as
      if they were peers, exactly the confirmed real bug this whole
      session's fix is about.
    - The old hardcoded `expected = CALIBRATED_RATE_PER_SEC * 8` assumed
      exactly 8 ranks/node, one specific communicator's worth of
      throughput. Replaced with a live count of however many distinct
      (comm,bucket) member series are actually reporting on this node
      right now (`count(agg_samples_seen{hostname="..."})`) -- correct for
      any number of communicators or members, discovered the same way
      node_aggregator_ref.py's own throughput-normalization denominator
      is (see that file's maybe_check_job_throughput).
    """
    reasons = []
    job_filter = f',slurm_job_id="{slurm_job_id}"' if slurm_job_id else ""

    peer_q = (f'agg_samples_seen{{comm="{comm}",member="{member}",bucket="{bucket}",hostname="{hostname}"{job_filter}}} '
              f'/ on(bucket,hostname,comm) group_left() '
              f'max by (bucket,hostname,comm) (agg_samples_seen{{comm="{comm}",bucket="{bucket}",hostname="{hostname}"{job_filter}}})')
    peer_result = _query_instant(vm_url, peer_q)
    if not peer_result:
        # A real gap found via this session's own live testing: an empty
        # result here does NOT mean "nothing to report" -- it means this
        # member/host has no sample recent enough to survive VM's own
        # staleness window (~5 min), which is itself the single MOST
        # degraded state possible (worse than a partial-rate drop) and
        # must not be silently treated as "coverage looks fine".
        reasons.append(f"no recent agg_samples_seen for comm={comm} member={member} bucket={bucket} "
                        f"hostname={hostname} -- either the aggregator is down or has "
                        f"not pushed within VM's staleness window")
        peer_fraction = None
    else:
        peer_fraction = float(peer_result[0]["value"][1])
        if peer_fraction < PEER_COVERAGE_FLOOR:
            reasons.append(f"peer coverage {peer_fraction:.2f} < floor {PEER_COVERAGE_FLOOR} "
                            f"(this member is falling behind its own communicator peers on this node)")

    now = time.time()
    # P26.5-maintenance fix -- dt used to be hardcoded 2.0, assuming the
    # elapsed wall-clock time between these two queries was exactly the
    # time.sleep(2.0) duration below. Real elapsed time can differ under
    # network/VM response jitter on either HTTP round-trip, silently
    # skewing the computed rate. Now uses each snapshot's own REAL sample
    # timestamp (via _query_instant_real_ts, same fix shape as this
    # project's other value[0]-is-query-time bug fixes) and measures dt
    # as their real difference -- not assumed, not hardcoded.
    r0 = _query_instant_real_ts(vm_url, f'agg_records_seen_total{{hostname="{hostname}"{job_filter}}}')
    active_members = _query_instant(vm_url, f'count(agg_samples_seen{{hostname="{hostname}"{job_filter}}})')
    time.sleep(2.0)
    r1 = _query_instant_real_ts(vm_url, f'agg_records_seen_total{{hostname="{hostname}"{job_filter}}}')
    absolute_rate_frac = None
    if not r0 or not r1:
        reasons.append(f"no recent agg_records_seen_total for hostname={hostname} -- "
                        f"either the aggregator is down or has not pushed within VM's "
                        f"staleness window")
    else:
        v0, v1 = float(r0[0]["value"][1]), float(r1[0]["value"][1])
        t0, t1 = float(r0[0]["value"][0]), float(r1[0]["value"][0])
        dt = t1 - t0
        if dt <= 0:
            reasons.append(f"real elapsed time between the two agg_records_seen_total snapshots for "
                            f"hostname={hostname} was {dt:.3f}s (non-positive) -- cannot compute a real rate "
                            f"this cycle")
            rate = None
        else:
            rate = (v1 - v0) / dt
        n_active = float(active_members[0]["value"][1]) if active_members else 0.0
        # P26 fix -- workload-generic floor, reusing P23's cross-job-
        # reference infrastructure (see _lookup_xjob_rate_reference's own
        # docstring) instead of the old CALIBRATED_RATE_PER_SEC=50.0
        # constant (nanoGPT-only, confirmed live to misfire on ResNet).
        sig = _lookup_workload_sig(vm_url, hostname, slurm_job_id)
        expected, n_hist = _lookup_xjob_rate_reference(vm_url, hostname, sig, slurm_job_id)
        absolute_rate_frac = (rate / expected) if (expected and rate is not None) else None
        if absolute_rate_frac is not None and absolute_rate_frac < ABSOLUTE_RATE_FLOOR_FRAC:
            reasons.append(f"node-wide absolute throughput {rate:.1f} rec/s is "
                            f"{absolute_rate_frac:.2f}x the calibrated floor "
                            f"({expected:.1f} rec/s expected, learned from {n_hist} historical "
                            f"healthy run(s) of this same workload signature sig={sig}, "
                            f"{n_active:.0f} currently-active member/bucket series on this node) "
                            f"-- this is the uniform-node-wide-loss check the peer-relative panel "
                            f"is blind to")
        elif expected is None:
            # Not treated as degraded and no reason appended -- a new or
            # rare workload signature with insufficient cross-job history
            # (sig={sig!r}, n_hist={n_hist}) has no basis for this check
            # yet. Report nothing rather than guess, same philosophy as
            # node_aggregator_ref.py's own self-calibration cold-start.
            pass

    return {
        "degraded": bool(reasons),
        "peer_fraction": peer_fraction,
        "absolute_rate_frac": absolute_rate_frac,
        "reasons": reasons,
        "blind_spot_note": (
            "Peer-relative coverage cannot detect sample loss affecting every "
            "member of this communicator on this node uniformly -- the "
            "absolute-rate check above is this alert's mitigation for that "
            "gap, not a complete fix."
        ),
    }
