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


def _root_cause_line(f):
    """Real gap found live: this project's own composed-incident-summary
    Grafana panel got a real root-cause-vs-downstream-effect label
    (incident_role), but that determination was never surfaced in the
    actual alert text/log at all -- invisible to anyone reading logs
    rather than Grafana, which is exactly what this alert text exists
    for. is_likely_root_cause is real, data-derived (this member's own
    real exec time is the minimum among every member of this collective
    at the real firing instant), not a guess.

    Real false-correlation-claim found live during FSDP validation: the
    original wording here, for the non-minimum case, asserted "some other
    member likely caused this one to wait" -- confirmed live this is
    sometimes simply WRONG, not just imprecise. A real FSDP test found
    role_rank=0 repeatedly firing its own incidents, completely unrelated
    in time/bucket/collective to an actual injected fault elsewhere,
    matching this project's own ALREADY-documented role_rank=0 structural
    misattribution bias (README known-limitations) -- a real, independent,
    non-fault artifact, not "downstream of" anything. This mechanism
    cannot distinguish "genuinely waiting on a real root cause" from
    "unrelated structural noise that merely isn't the minimum right now"
    -- it only knows this member wasn't the fastest. Reworded below to
    state exactly that narrower, honestly-supportable fact, rather than
    asserting a causal link the data doesn't actually establish."""
    irc = f.get("is_likely_root_cause")
    if irc is None:
        return None
    if irc:
        return ("  This member's own real exec time was the minimum among every member of this "
                "collective at the moment it fired -- consistent with being the real root cause "
                "(per this project's own documented 'the straggler arrives late and shows the "
                "shortest exec time' rationale), though a genuine tie or multiple simultaneous "
                "faults in the same collective can't be fully ruled out from this data alone.")
    return ("  This member's own real exec time was NOT the minimum among this collective's real "
            "members at the moment it fired. This does NOT necessarily mean it's downstream of a "
            "specific other finding -- it only means some other member read faster at that moment. "
            "Confirmed live this can also reflect an independent, non-fault structural artifact "
            "(e.g. a known position-correlated misattribution bias, see README known limitations) "
            "rather than a genuine correlated wait. Check whether another member for this same "
            "comm/bucket/coll around the same time is flagged root cause before assuming a link; "
            "tools/incident_correlator.py can help confirm either way.")


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
        root_cause_line = _root_cause_line(f)
        if root_cause_line:
            lines.append(root_cause_line)
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
        root_cause_line = _root_cause_line(f)
        if root_cause_line:
            lines.append(root_cause_line)
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
        root_cause_line = _root_cause_line(f)
        if root_cause_line:
            lines.append(root_cause_line)
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
