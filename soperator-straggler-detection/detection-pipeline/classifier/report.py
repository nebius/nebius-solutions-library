#!/usr/bin/env python3
"""Stage 4 output formatting: renders a classification finding as one of
the three tiers, including the full structured manual-review request for
UNCONFIRMED findings."""


def _real_exec_comparison_line(ev):
    """Shared by all three tiers below. Real bug found live: the first
    version of this always said "this rank was Xus LATER than its peers"
    using worst_val - peer_mean, unconditionally -- correct for an
    ordinary compute straggler (worst_val > peer_mean), but confirmed
    live to produce a real, genuinely confusing NEGATIVE number for a
    late-arriving-straggler signature (the flagged rank's own collective
    call completes FAST once it finally joins -- worst_val read 302.9us
    against peers' 1,007,928.5us in a real test -- the documented
    inversion already described elsewhere in this project: the real cost
    shows up as the PEERS waiting, not in the flagged rank's own number).
    Handles both real shapes honestly instead of asserting one
    direction unconditionally."""
    worst_val, peer_mean = ev.get("worst_val"), ev.get("peer_mean")
    if worst_val is None or peer_mean is None:
        return None
    if worst_val >= peer_mean:
        diff = worst_val - peer_mean
        return (f"  this rank's own real exec time: {worst_val:.1f}us vs. peer reference: "
                f"{peer_mean:.1f}us -- this rank was {diff:.1f}us slower to complete this "
                f"collective than its peers.")
    diff = peer_mean - worst_val
    return (f"  this rank's own real exec time: {worst_val:.1f}us vs. peer reference: "
            f"{peer_mean:.1f}us -- this rank's OWN reading is actually lower than its peers. "
            f"This is a real, expected signature for a late-arriving straggler: it sleeps/stalls "
            f"before joining the collective, so once it finally does, its own call completes "
            f"quickly -- the real cost shows up as its peers waiting {diff:.1f}us longer than "
            f"their own typical time, not in this rank's own number.")


def format_finding(f):
    tier = f["tier"]
    if "rank" in f:
        subject = f"rank {f['rank']}"
    else:
        subject = f["host"]

    if tier == "CONFIRMED":
        return _format_confirmed(f, subject)
    elif tier == "PROBABLE":
        return _format_probable(f, subject)
    else:
        return _format_unconfirmed(f, subject)


def _format_confirmed(f, subject):
    ev = f["evidence"]
    c1 = f["cause"]["class1"]
    lines = [f"{subject} · {f['type_candidate']} straggler · {f['timescale']} · CONFIRMED", ""]
    if "statistic" in ev:
        lines.append(f"Arrival lag {ev['mm']:.2f}x node peers ({ev['statistic']}, z={ev['z']:.1f}).")
        # Real gap closed: worst_val/peer_mean were already computed and
        # present in `ev` (same fields _format_unconfirmed below already
        # reads) but never shown here -- a reader only ever saw an
        # abstract ratio/z-score, never the actual real exec-time numbers
        # behind it. Same units convention as the worker0_mean/worker1_mean
        # line in _format_probable below (raw microseconds, this project's
        # own agg_*_exec_time_us metric convention).
        comparison_line = _real_exec_comparison_line(ev)
        if comparison_line:
            lines.append(comparison_line)
    for k, v in c1.items():
        lines.append(f"{k}: {v}")
    # P26.5-maintenance -- real eBPF storage evidence, shown whenever it's
    # what actually confirmed this finding (host/PID/window are all real,
    # not placeholders -- same convention as the class1 DCGM lines above).
    pc = f["cause"].get("path_c_storage")
    if pc:
        lines.append(f"storage (eBPF block-I/O-wait): pid={pc.get('pid')} on {pc.get('host')} -- "
                      f"{pc.get('target_iowait_us', 0)}us aggregated over "
                      f"[{pc.get('t_start', 0):.1f},{pc.get('t_end', 0):.1f}] "
                      f"({pc.get('target_count', 0)} block requests)")
        # Approved item 2 (item-4 Part C followup) -- informational
        # ONLY, never read by determine_storage_path/determine_confirmed_path
        # (see storage_evidence.query_iowait_window's own docstring for the
        # trace). Path C only: the only cause-path with a real, queryable
        # incident window right now (Path A/B are single-instant snapshots --
        # see README's known-limitations section).
        if "coverage_seconds_present" in pc:
            lines.append(f"  window-overlap strength (informational only, does NOT affect "
                         f"confidence tier): iowait evidence present for "
                         f"{pc['coverage_seconds_present']:.1f} of {pc['coverage_seconds_total']:.1f} "
                         f"real seconds in this incident's actual duration")
    return "\n".join(lines)


def _format_probable(f, subject):
    ev = f["evidence"]
    lines = [f"{subject} · {f['type_candidate']} straggler · {f['timescale']} · PROBABLE", ""]
    if "statistic" in ev:
        lines.append(f"Arrival lag {ev['mm']:.2f}x node peers ({ev['statistic']}, z={ev['z']:.1f}).")
        comparison_line = _real_exec_comparison_line(ev)
        if comparison_line:
            lines.append(comparison_line)
    else:
        lines.append(f"Node aggregate diff {ev['diff_sd_units']:.2f} SD units "
                      f"(worker0={ev['worker0_mean']:.1f}us, worker1={ev['worker1_mean']:.1f}us).")
    for k, v in f["cause"]["class1"].items():
        lines.append(f"{k}: {v} (available but ambiguous/incomplete)")
    return "\n".join(lines)


def _format_unconfirmed(f, subject, ruled_out=None, impossible=None, class2_flags=None, next_steps=None):
    ev = f["evidence"]
    lines = []
    lines.append(f"{subject} · straggler · {f.get('timescale','?')} · CAUSE UNCONFIRMED")
    lines.append("")
    lines.append("What was detected:")
    if "statistic" in ev:
        mm_str = f"{ev['mm']:.1f}x" if ev['mm'] != float("inf") else "far above (peer group showed none)"
        z_str = f"{ev['z']:.1f}" if ev['z'] != float("inf") else "undefined (zero peer variance)"
        lines.append(f"  {subject}'s exec-time {ev['statistic']} is {mm_str} its node peers "
                      f"({ev['statistic']}, z={z_str}).")
        # Same concrete real-number disclosure as _format_confirmed/
        # _format_probable above, polished to the same .1f/us convention
        # (this one previously rendered raw, unformatted Python values --
        # e.g. "Raw value: 1053.234, peer mean: 29.5017" -- harder to read
        # than necessary for the exact same real data).
        comparison_line = _real_exec_comparison_line(ev)
        if comparison_line:
            lines.append(comparison_line)
    else:
        lines.append(f"  Node aggregate diff {ev['diff_sd_units']:.2f} SD units, no within-node outlier.")
    lines.append("")
    lines.append("What was ruled out:")
    for item in (ruled_out or []):
        lines.append(f"  · {item}")
    if not ruled_out:
        lines.append("  · (none -- see 'what could not be checked' below)")
    lines.append("")
    lines.append("What could not be checked:")
    for item in (impossible or f["cause"].get("impossible", [])):
        lines.append(f"  · {item}")
    if class2_flags:
        lines.append("")
        lines.append("Also worth checking (supporting evidence only, not a cause):")
        for item in class2_flags:
            lines.append(f"  · {item}")
    lines.append("")
    lines.append("Suggested next steps, most likely first:")
    for i, step in enumerate(next_steps or [], 1):
        lines.append(f"  {i}. {step}")
    return "\n".join(lines)


def build_unconfirmed_report(finding, ruled_out, impossible, class2_flags, next_steps):
    """Full manual-review builder with explicit ruled-out/impossible/next-step
    lists, matching the Stage 4 spec's example format exactly."""
    subject = f"rank {finding['rank']}" if "rank" in finding else finding["host"]
    return _format_unconfirmed(finding, subject, ruled_out, impossible, class2_flags, next_steps)
