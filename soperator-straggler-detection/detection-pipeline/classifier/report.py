#!/usr/bin/env python3
"""Stage 4 output formatting: renders a classification finding as one of
the three tiers, including the full structured manual-review request for
UNCONFIRMED findings."""


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
    return "\n".join(lines)


def _format_probable(f, subject):
    ev = f["evidence"]
    lines = [f"{subject} · {f['type_candidate']} straggler · {f['timescale']} · PROBABLE", ""]
    if "statistic" in ev:
        lines.append(f"Arrival lag {ev['mm']:.2f}x node peers ({ev['statistic']}, z={ev['z']:.1f}).")
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
                      f"({ev['statistic']}, z={z_str}). Raw value: {ev.get('worst_val')}, peer mean: {ev.get('peer_mean')}.")
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
