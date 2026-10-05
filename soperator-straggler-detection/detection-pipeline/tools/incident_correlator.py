#!/usr/bin/env python3
"""Read-only, on-demand evidence-assembly tool across this project's three
real detection mechanisms ([ALERT], [WAIT-INDUCED-ALERT],
[ROLE-BASELINE-ALERT]) for ONE human-identified real event.

HARD DESIGN CONSTRAINT (explicit, approved): this tool ASSEMBLES and
PRESENTS existing evidence more completely -- it never computes a new,
higher-confidence verdict, never overrides or re-scores the existing
CONFIRMED/PROBABLE/UNCONFIRMED tiering, and never states a comparative
lean ("more consistent with X than Y"). Every number in its output is
copied verbatim from a real metric value or a real reference-table note
written by a human after a real investigation -- nothing here is computed
FOR the human; it is handed to them, with its own source, to read and
judge themselves. If a future need for a comparative-lean mode arises,
that is a separate, explicit decision -- not something to default into
by extending this file.

Deliberately read-only: every function here only ever queries
VictoriaMetrics (GET) or reads a local log file -- no writes, no pushes,
no side effects on the live pipeline, safe to run at any time.

Three real human starting points this tool's CLI is shaped around (see
--help, and README's "investigating an incident" walkthrough):
  (a) a human tailing var/alert_summary.log/var/alert_engine_supervised.log
      sees a fresh alert line -- paste it with --log-line, no manual
      timestamp lookup needed (none exists to look up -- see below).
  (b) a human looking at a Grafana panel (trajectory/peer-timing/
      composed-incident-summary/the new wait-induced/role-baseline
      panels) has hostname/comm/bucket visible on screen -- pass those
      directly with --host/--comm/--bucket.
  (c) a human only has a rough time window and a hostname in mind --
      --host plus --from/--to, no comm required; every comm with real
      correlator-visible activity on that host in that window is found
      and reported automatically.

Real, disclosed limitation this tool's output always states explicitly
where it applies: raw [ALERT]/[WAIT-INDUCED-ALERT]/[ROLE-BASELINE-ALERT]
log lines carry NO timestamp (confirmed directly against the real log
format -- only this project's own supervisor bookkeeping lines do). A
real timestamp is only available for a finding when it ALSO pushed a
VictoriaMetrics metric (every [WAIT-INDUCED-ALERT]/[ROLE-BASELINE-ALERT]
finding does, via this project's own _push_visibility_metric; a plain
[ALERT] only does when it additionally crosses the sustained+impactful
straggler_incident_detected gate). Anywhere this tool shows a finding
that could only be matched by identity (hostname/comm/bucket/coll), not
confirmed against the requested time window by a real timestamp, it says
so visibly on that specific line -- never presented the same way as a
timestamp-confirmed one.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG_ROOT, "alerting"))
sys.path.insert(0, os.path.join(PKG_ROOT, "observability"))
import alert_engine as ae  # noqa: E402  (reuses real, already-validated query helpers)
import reliability_reference  # noqa: E402  (shared with alert_engine.py's own UNVALIDATED gate)

REFERENCE_PATH = reliability_reference.DEFAULT_PATH
DEFAULT_LOG_PATH = os.path.join(PKG_ROOT, "var", "alert_engine_supervised.log")
DEFAULT_WINDOW_S = 180.0  # +/- padding around a resolved real anchor timestamp
HISTORICAL_LOOKBACK_S = 90 * 86400  # matches this project's own ROLE_XJOB_LOOKBACK_S order of magnitude
LOG_FALLBACK_MAX_LINES = 10  # cap when no real timestamp exists at all for an identity


def _default_vm_url():
    """Real bug fixed: this used to default straight to a hardcoded
    "http://worker-0:8428" whenever VM_URL wasn't exported in the shell --
    which happens to be this project's own 2-node dev cluster's real VM
    address, so it silently worked during every validation run here and
    was never caught until run on a real cluster where VM lives somewhere
    else (confirmed live: a real 6-node cluster's own install.sh-discovered
    VM_URL was "http://login-0:8428" -- worker-0:8428 isn't reachable there
    at all, producing a raw ConnectionRefusedError traceback instead of a
    useful result). Every other script in this project gets VM_URL from
    cluster.env (install.sh's own real, live-discovered value) -- this is
    the one standalone CLI tool meant to be run ad hoc, with no wrapper
    script to source cluster.env first, so it now reads that file directly
    as a fallback instead of guessing a dev-cluster-specific address.
    Precedence: explicit VM_URL env var (highest -- an explicit override
    always wins) > cluster.env's own real value > the old hardcoded guess
    (last resort only, kept so this never hard-fails before --vm-url is
    even parsed)."""
    env_val = os.environ.get("VM_URL")
    if env_val:
        return env_val
    cluster_env_path = os.path.join(PKG_ROOT, "cluster.env")
    try:
        with open(cluster_env_path) as f:
            for line in f:
                m = re.match(r'^\s*VM_URL\s*=\s*"?([^"\s]+)"?\s*$', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "http://worker-0:8428"

HEADER_RE = re.compile(
    r"rank=(?P<member>\S+)\s+gpu_slot=(?P<gpu_slot>\S+)\s+role_rank=(?P<role_rank>\S+)\s+"
    r"role_n=(?P<role_n>\S+)\s+comm=(?P<comm>\S+)\s+node=(?P<hostname>\S+)"
)
BODY_BUCKET_RE = re.compile(r"bucket=(?P<bucket>\S+)\s+bytes,\s*coll=(?P<coll>\S+)")
CHECK_HEADER_TAGS = ("[ALERT]", "[WAIT-INDUCED-ALERT]", "[ROLE-BASELINE-ALERT]")


# ---------------------------------------------------------------------------
# Entry-point (a): parsing a pasted real log line
# ---------------------------------------------------------------------------

def parse_log_line(text):
    """Pulls (hostname, comm, member, bucket, coll, role_rank, role_n) out
    of a real, pasted [ALERT]/[WAIT-INDUCED-ALERT]/[ROLE-BASELINE-ALERT]
    header line -- all three share the identical header field layout, so
    one regex covers all three real check types. bucket/coll live in the
    BODY text, one or more lines below the header in the real multi-line
    alert text this project emits -- if the human only pasted the header
    line, bucket/coll come back None (never guessed), and the caller
    re-discovers them live from VM instead. Returns None if the pasted
    text doesn't match any real header this project emits -- an honest
    "couldn't parse this," never a partial guess."""
    m = HEADER_RE.search(text)
    if not m:
        return None
    out = m.groupdict()
    bm = BODY_BUCKET_RE.search(text)
    out["bucket"] = bm.group("bucket") if bm else None
    out["coll"] = bm.group("coll") if bm else None
    return out


# ---------------------------------------------------------------------------
# Entry-point (c) / --from --to: flexible, honest time parsing
# ---------------------------------------------------------------------------

def parse_time_arg(s, today_ref=None):
    """Accepts a real epoch seconds value, a full ISO-8601 UTC timestamp
    (this project's own "%Y-%m-%dT%H:%M:%SZ" convention, used everywhere
    else in this file), or a bare "HH:MM"/"HH:MM:SS" (combined with
    today's real UTC date -- what a human glancing at a clock actually
    has in mind). Raises ValueError with a clear message on anything else
    -- never silently guesses a time."""
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.datetime.strptime(s, fmt).time()
            base = today_ref or datetime.datetime.now(datetime.timezone.utc)
            dt = datetime.datetime.combine(base.date(), t, tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"could not parse time {s!r} -- use epoch seconds, "
                      f"YYYY-MM-DDTHH:MM:SSZ, or HH:MM (today, UTC)")


def parse_window_arg(s):
    """'5m'/'3h'/'90s'/a bare number of seconds -- this project's own
    convention (its env/CLI surfaces elsewhere use plain seconds; this
    adds the small number of human-friendly suffixes a time WINDOW is
    actually typed in, nothing more)."""
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    if s and s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


# ---------------------------------------------------------------------------
# Historical (non-live-only) VM lookups -- this tool investigates the PAST,
# so it deliberately does NOT reuse alert_engine.py's own _comm_cross_node_
# members/_discover_buckets/_comm_slurm_job_id (all three require _is_fresh_
# row -- real, currently-live data, correct for the live alerting path,
# wrong for a tool whose whole job is looking at a comm that may be long
# finished). Same query shape (agg_samples_seen{comm=...}), same discipline,
# just without the "must still be live" gate -- exactly how
# _member_role_baseline already does historical lookups elsewhere in this
# project.
# ---------------------------------------------------------------------------

def _query_instant_safe(vm_url, selector):
    """Same fail-safe discipline as export_metric() just below -- a real bug
    found live: these three lookups called ae._query_instant directly, with
    no protection, while export_metric (serving the identical purpose a few
    lines down) already wrapped the equivalent call. VM being unreachable
    (wrong --vm-url, VM down, a real network issue) crashed this tool with a
    raw traceback instead of degrading to 'no historical data found' --
    confirmed live: ConnectionRefusedError propagating all the way out of
    historical_comm_buckets(). Fixed by giving these three the same
    graceful-degradation behavior export_metric already has, rather than
    adding three separate one-off try/excepts."""
    try:
        return ae._query_instant(vm_url, selector)
    except Exception:
        return []


def historical_comm_members(vm_url, comm, lookback_s=HISTORICAL_LOOKBACK_S):
    rows = _query_instant_safe(vm_url, f'last_over_time(agg_samples_seen{{comm="{comm}"}}[{int(lookback_s)}s])')
    return sorted({(r["metric"].get("hostname"), r["metric"].get("member")) for r in rows
                   if r["metric"].get("hostname") and r["metric"].get("member")})


def historical_comm_buckets(vm_url, hostname, comm, lookback_s=HISTORICAL_LOOKBACK_S):
    rows = _query_instant_safe(
        vm_url, f'last_over_time(agg_samples_seen{{hostname="{hostname}",comm="{comm}"}}[{int(lookback_s)}s])')
    return sorted({(r["metric"].get("bucket"), r["metric"].get("coll")) for r in rows
                   if r["metric"].get("bucket") and r["metric"].get("coll")})


def historical_job_id_for_comm(vm_url, comm, lookback_s=HISTORICAL_LOOKBACK_S):
    rows = _query_instant_safe(vm_url, f'last_over_time(agg_samples_seen{{comm="{comm}"}}[{int(lookback_s)}s])')
    ids = {r["metric"].get("slurm_job_id") for r in rows} - {None, ""}
    return next(iter(ids)) if len(ids) == 1 else None


def resolve_anchor_timestamp(vm_url, hostname, comm):
    """The single most-recent REAL sample timestamp found for this
    identity across every metric that could plausibly carry one -- used
    to center a search window when the human gave identity but not a
    time (entry points a/b). Returns None (never a guess) if nothing
    real is found at all, e.g. the comm's data has aged out of VM
    entirely -- the caller must fall back to the disclosed, unordered
    log-only path.

    Uses /api/v1/export (_query_export, real per-sample timestamps),
    NOT _query_instant_real_ts wrapped in last_over_time() -- confirmed
    live this would otherwise be wrong: timestamp(last_over_time(x[w]))
    returns the QUERY EVALUATION time, not the real underlying sample's
    time, because last_over_time() itself produces a synthetic point at
    eval time. Every real call site of _query_instant_real_ts elsewhere
    in alert_engine.py passes a plain selector for exactly this reason
    -- never wraps it in a range function."""
    now = time.time()
    selectors = [
        f'agg_straggler_incident_detected{{hostname="{hostname}",comm="{comm}"}}',
        f'agg_wait_induced_detected{{hostname="{hostname}",comm="{comm}"}}',
        f'agg_wait_induced_peer_ratio{{hostname="{hostname}",comm="{comm}"}}',
        f'agg_role_baseline_detected{{hostname="{hostname}",comm="{comm}"}}',
    ]
    best = None
    for sel in selectors:
        for series in export_metric(vm_url, sel, now - HISTORICAL_LOOKBACK_S, now):
            for ts_ms in series.get("timestamps", []):
                ts = ts_ms / 1000.0
                if best is None or ts > best:
                    best = ts
    return best


# ---------------------------------------------------------------------------
# Reference table (observability/workload_reliability_reference.yaml) --
# load_reference/lookup_reference now live in observability/
# reliability_reference.py, the one shared module both this tool and the
# live alert_engine.py role-baseline UNVALIDATED gate import, so the two
# never drift into two different lookup implementations.
# ---------------------------------------------------------------------------

load_reference = reliability_reference.load_reference
lookup_reference = reliability_reference.lookup_reference


# ---------------------------------------------------------------------------
# Real evidence collection -- VM metrics (time-confirmed) + log (identity
# match, time-confirmed if a VM row exists for the same identity+window,
# explicitly disclosed as identity-only otherwise)
# ---------------------------------------------------------------------------

def export_metric(vm_url, selector, start_ts, end_ts):
    try:
        return ae._query_export(vm_url, selector, start_ts, end_ts)
    except Exception:
        return []


def collect_vm_findings(vm_url, hostname, comm, start_ts, end_ts):
    """Real, time-confirmed findings for every one of the three checks'
    OWN pushed metrics, within [start_ts, end_ts]. Each returned row keeps
    every real label/value exactly as pushed -- nothing recomputed."""
    out = {"straggler_incident": [], "wait_induced": [], "role_baseline": []}
    base = f'hostname="{hostname}",comm="{comm}"'

    for series in export_metric(vm_url, f"agg_straggler_incident_detected{{{base}}}", start_ts, end_ts):
        out["straggler_incident"].append(series)
    for series in export_metric(vm_url, f"agg_wait_induced_detected{{{base}}}", start_ts, end_ts):
        out["wait_induced"].append(series)
    for series in export_metric(vm_url, f"agg_wait_induced_peer_ratio{{{base}}}", start_ts, end_ts):
        out["wait_induced"].append(series)
    for series in export_metric(vm_url, f"agg_role_baseline_detected{{{base}}}", start_ts, end_ts):
        out["role_baseline"].append(series)
    return out


def discover_comms_in_window(vm_url, hostname, start_ts, end_ts):
    """Entry point (c): no comm given, only a host + time range. Finds
    every comm with real correlator-visible activity (any of the three
    checks' own pushed metrics) on this host in this window -- never
    invents a comm, only reports what real data shows."""
    comms = set()
    for metric in ("agg_straggler_incident_detected", "agg_wait_induced_detected",
                   "agg_wait_induced_peer_ratio", "agg_role_baseline_detected"):
        for series in export_metric(vm_url, f'{metric}{{hostname="{hostname}"}}', start_ts, end_ts):
            c = series.get("metric", {}).get("comm")
            if c:
                comms.add(c)
    return sorted(comms)


def grep_plain_alerts(log_path, hostname, comm):
    """Every real plain-text line for the three check headers matching
    this (hostname, comm), read straight from the log, tagged with WHICH
    of the three real headers it is -- this is the ONLY way to see an
    ordinary PROBABLE/LOG-ONLY [ALERT] that never crossed the
    sustained+impactful straggler_incident gate (and so never pushed a VM
    metric at all). Returns every match, grouped by check tag -- the
    caller decides per-check, using that check's OWN real VM findings,
    which of these are time-confirmed vs. identity-only (the log itself
    carries no timestamp, so this function cannot and does not decide
    that on its own)."""
    by_check = {tag: [] for tag in CHECK_HEADER_TAGS}
    if not os.path.exists(log_path):
        return by_check
    needle_comm = f"comm={comm}"
    needle_host = f"node={hostname}"
    with open(log_path, errors="replace") as f:
        for line in f:
            check = next((tag for tag in CHECK_HEADER_TAGS if tag in line), None)
            if check is None:
                continue
            if needle_comm not in line or needle_host not in line:
                continue
            parsed = parse_log_line(line)
            if parsed is None:
                continue
            by_check[check].append({"line": line.rstrip("\n"), "parsed": parsed})
    return by_check


# ---------------------------------------------------------------------------
# Report rendering -- facts only, per the approved pure-assembly design.
# No synthesized conclusion, no comparative language, anywhere below.
# ---------------------------------------------------------------------------

def render_report(vm_url, hostname, comm, start_ts, end_ts, ref, log_path):
    lines = []
    lines.append("=" * 78)
    lines.append(f"INCIDENT-CORRELATOR EVIDENCE BRIEF -- read-only, assembled, not a verdict")
    lines.append(f"hostname={hostname} comm={comm}")
    window_disp = (f"{datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc).isoformat()} .. "
                   f"{datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc).isoformat()}"
                   if start_ts is not None and end_ts is not None else "UNRESOLVED (no real timestamp found)")
    lines.append(f"window: {window_disp}")
    lines.append("=" * 78)

    buckets = historical_comm_buckets(vm_url, hostname, comm)
    if buckets:
        bucket, coll = max(buckets, key=lambda bc: int(bc[0]) if str(bc[0]).isdigit() else -1)
        lines.append(f"largest real bucket/coll for this comm: bucket={bucket} coll={coll} "
                      f"({len(buckets)} real bucket/coll pair(s) seen total)")
    else:
        bucket, coll = None, None
        lines.append("no real bucket/coll data found for this comm (aged out, or never existed)")

    job_id = historical_job_id_for_comm(vm_url, comm)
    engine = ae.AlertEngine(vm_url=vm_url, hostnames=None)
    sig = engine._job_workload_sig(hostname, job_id) if job_id else None
    ref_entry = lookup_reference(ref, sig)
    lines.append(f"resolved workload shape: {ref_entry.get('human_name')} "
                 f"(workload_sig={'(none resolvable)' if sig is None else sig}, slurm_job_id={job_id or '(unknown)'})")
    lines.append("")

    vm_findings = {} if start_ts is None else collect_vm_findings(vm_url, hostname, comm, start_ts, end_ts)
    log_by_check = grep_plain_alerts(log_path, hostname, comm)

    members = historical_comm_members(vm_url, comm)
    lines.append(f"real members of this comm ({len(members)}): "
                 + ", ".join(f"{h}/{m}" for h, m in members) if members else "no real members found")
    lines.append("")

    def render_check_section(title, check_key, log_tag, ref_key, vm_line_fn):
        lines.append(f"-- {title} --")
        vm_rows = vm_findings.get(check_key, [])
        confirmed_members = {s["metric"].get("member") for s in vm_rows}
        for s in vm_rows:
            lines.append(f"  {vm_line_fn(s['metric'], s)}")
        log_matches = log_by_check.get(log_tag, [])
        if start_ts is None:
            # No real timestamp resolvable at all for this identity -- every
            # log match is identity-only by construction; cap to the most
            # recent N so a long-cold comm doesn't dump its whole history.
            shown = log_matches[-LOG_FALLBACK_MAX_LINES:]
            for a in shown:
                lines.append(f"  IDENTITY-ONLY MATCH -- not time-confirmed (no real timestamp resolvable "
                             f"for this identity at all): member={a['parsed']['member']} "
                             f"role_rank={a['parsed']['role_rank']}")
                lines.append(f"    raw: {a['line']}")
        else:
            confirmed_shown = [a for a in log_matches if a["parsed"]["member"] in confirmed_members]
            unconfirmed = [a for a in log_matches if a["parsed"]["member"] not in confirmed_members]
            for a in confirmed_shown:
                lines.append(f"  (raw log text for the time-confirmed finding above, member={a['parsed']['member']}):")
                lines.append(f"    raw: {a['line']}")
            for a in unconfirmed[-LOG_FALLBACK_MAX_LINES:]:
                lines.append(f"  IDENTITY-ONLY MATCH -- not time-confirmed (no VM-pushed metric for this "
                             f"member within the requested window; log line itself carries no timestamp): "
                             f"member={a['parsed']['member']} role_rank={a['parsed']['role_rank']}")
                lines.append(f"    raw: {a['line']}")
        if not vm_rows and not log_matches:
            lines.append(f"  SILENT -- no {log_tag} evidence found for this identity")
        ref_block = ref_entry["checks"].get(ref_key, {})
        lines.append(f"  reference-table note for this shape (status={ref_block.get('status', 'UNVALIDATED')}, "
                     f"source={ref_block.get('evidence_ref') or 'n/a'}):")
        lines.append(f"    {ref_block.get('note', '(no note)').strip()}")
        lines.append("")

    render_check_section(
        "Check 1: [ALERT] (positive-deviation, mean/CV path)", "straggler_incident", "[ALERT]",
        "alert_positive_deviation",
        lambda m, s: (f"FIRED (time-confirmed): member={m.get('member')} role_rank={m.get('role_rank')} "
                      f"role_n={m.get('role_n')} gpu_slot={m.get('gpu_slot')} bucket={m.get('bucket')} "
                      f"coll={m.get('coll')} -- {len(s.get('values', []))} real sample(s) in window"))
    render_check_section(
        "Check 2: [WAIT-INDUCED-ALERT]", "wait_induced", "[WAIT-INDUCED-ALERT]", "wait_induced",
        lambda m, s: (f"FIRED (time-confirmed): member={m.get('member')} role_rank={m.get('role_rank')} "
                      f"role_n={m.get('role_n')} role_in_event={m.get('role_in_event', '?')} "
                      f"baseline_source={m.get('baseline_source', 'n/a')} "
                      f"value={s.get('values', [None])[-1]}"))
    render_check_section(
        "Check 3: [ROLE-BASELINE-ALERT]", "role_baseline", "[ROLE-BASELINE-ALERT]", "role_baseline",
        lambda m, s: (f"FIRED (time-confirmed): member={m.get('member')} role_rank={m.get('role_rank')} "
                      f"role_n={m.get('role_n')} baseline_source={m.get('baseline_source', 'n/a')} "
                      f"volatile={m.get('volatile', 'n/a')} value={s.get('values', [None])[-1]}"))
    lines.append("=" * 78)
    lines.append("End of evidence brief. This tool draws no conclusion -- read the three")
    lines.append("check results and the reference notes above and judge for yourself.")
    lines.append("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="incident_correlator.py",
        description="Read-only evidence-assembly across [ALERT]/[WAIT-INDUCED-ALERT]/"
                    "[ROLE-BASELINE-ALERT] for one real event. Never computes a new "
                    "verdict -- see this file's own module docstring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Three real ways to invoke this, matching where you're actually looking:

  (a) You're tailing the log and saw a fresh alert line -- paste it:
      tools/incident_correlator.py --log-line '[WAIT-INDUCED-ALERT] rank=2692421 gpu_slot=3 role_rank=3 role_n=4 comm=0x38bd68767bcecb node=worker-0 ...'

  (b) You're looking at a Grafana panel with hostname/comm/bucket visible:
      tools/incident_correlator.py --host worker-0 --comm 0x38bd68767bcecb --bucket 3234251 --coll AllReduce

  (c) You only have a rough time window and a host in mind:
      tools/incident_correlator.py --host worker-1 --from 14:25 --to 14:35
""")
    p.add_argument("--log-line", help="paste an exact alert header line; identity is parsed out automatically")
    p.add_argument("--comm", help="communicator id (from a log line or a Grafana panel's Comm variable)")
    p.add_argument("--host", "--hostname", dest="host", help="hostname, e.g. worker-0")
    p.add_argument("--bucket", help="message-size bucket (optional, informational -- largest real bucket is auto-discovered)")
    p.add_argument("--coll", help="collective type, e.g. AllReduce (optional)")
    p.add_argument("--window", default="3m", help="+/- padding around a resolved real timestamp (default 3m)")
    p.add_argument("--from", dest="time_from", help="coarse window start: epoch seconds, ISO timestamp, or HH:MM (today, UTC)")
    p.add_argument("--to", dest="time_to", help="coarse window end (same formats as --from)")
    p.add_argument("--vm-url", default=_default_vm_url())
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH)
    p.add_argument("--reference", default=REFERENCE_PATH)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    hostname, comm, bucket, coll = args.host, args.comm, args.bucket, args.coll
    if args.log_line:
        parsed = parse_log_line(args.log_line)
        if parsed is None:
            print("FATAL: --log-line did not match any real [ALERT]/[WAIT-INDUCED-ALERT]/"
                  "[ROLE-BASELINE-ALERT] header this project emits.", file=sys.stderr)
            return 1
        hostname = hostname or parsed["hostname"]
        comm = comm or parsed["comm"]
        bucket = bucket or parsed["bucket"]
        coll = coll or parsed["coll"]

    if not hostname:
        print("FATAL: --host is required (directly, or via --log-line).", file=sys.stderr)
        return 1

    ref = load_reference(args.reference)
    window_s = parse_window_arg(args.window)

    start_ts = end_ts = None
    if args.time_from or args.time_to:
        if not (args.time_from and args.time_to):
            print("FATAL: --from and --to must be given together.", file=sys.stderr)
            return 1
        start_ts = parse_time_arg(args.time_from)
        end_ts = parse_time_arg(args.time_to)

    if comm:
        if start_ts is None:
            anchor = resolve_anchor_timestamp(args.vm_url, hostname, comm)
            if anchor is not None:
                start_ts, end_ts = anchor - window_s, anchor + window_s
            else:
                print(f"NOTE: no real timestamp could be resolved for hostname={hostname} comm={comm} "
                      f"(data aged out of VM, or never existed). Falling back to an unordered, "
                      f"NOT time-filtered log search (most recent matches only, clearly marked below).",
                      file=sys.stderr)
        print(render_report(args.vm_url, hostname, comm, start_ts, end_ts, ref, args.log_path))
        return 0

    # Entry point (c): host + time range, no comm -- discover every real
    # comm with correlator-visible activity in that window.
    if start_ts is None:
        print("FATAL: with no --comm, --from/--to is required (entry point c).", file=sys.stderr)
        return 1
    comms = discover_comms_in_window(args.vm_url, hostname, start_ts, end_ts)
    if not comms:
        print(f"No real correlator-visible activity (straggler-incident/wait-induced/role-baseline) "
              f"found for hostname={hostname} in the requested window. This does not rule out an "
              f"ordinary PROBABLE [ALERT] that never reached the sustained+impactful gate -- "
              f"re-run with an explicit --comm if you have one, to also search the raw log.")
        return 0
    print(f"{len(comms)} real comm(s) with correlator-visible activity on {hostname} in this window:\n")
    for c in comms:
        print(render_report(args.vm_url, hostname, c, start_ts, end_ts, ref, args.log_path))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
