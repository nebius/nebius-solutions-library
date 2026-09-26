#!/usr/bin/env python3
"""Pre-flight test-duration check (this session, closing a recurring
blind spot: PP's forward-only false alarm, the checkpoint-fault's
original attempt, and RL's first training-phase attempt all showed
"zero alerts" purely because the test ran shorter than
BUCKET_MATURITY_GRACE_S, not because of a real detection gap).

Reuses this project's own real constants directly from
node_aggregator_ref.py -- CALIB_MIN_TOTAL_SAMPLES, BUCKET_MATURITY_
GRACE_S, STAT_WINDOW_SIZE (via node_aggregator_ref's own import) --
not a second, parallel, re-guessed timing mechanism.

Real formula, three real phases a test must clear:
  1. Calibration: the primary bucket needs CALIB_MIN_TOTAL_SAMPLES
     (200) real occurrences before it becomes "scored" at all.
  2. Grace: score_mean_window()'s own fired-gate requires real wall-
     clock time since that scoring moment to exceed
     BUCKET_MATURITY_GRACE_S (120s), REGARDLESS of z/mm magnitude --
     confirmed directly in node_aggregator_ref.py's own code.
  3. A window must actually CLOSE after grace clears: window-closing
     is sample-count-driven (not timer-gated), so in the worst case
     (a window happened to close right before grace ended) the very
     next one won't close until another full window's worth of real
     samples accumulate -- CV_WINDOW (125) is used here since it's
     the larger of the two real window sizes (mean=100, cv=125),
     giving a fair chance for BOTH checks, not just mean.

minimum_duration_s = (CALIB_MIN_TOTAL_SAMPLES / rate) + BUCKET_MATURITY_GRACE_S + (CV_WINDOW / rate)
"""
import os
import sys

# Stage 3 fix: this file was missed by Stage 2's item-4 sys.path sweep
# (it's a tools/ helper, not one of the core alerting/aggregator modules
# that sweep targeted) -- same real-location fix as every other file.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aggregator"))
from node_aggregator_ref import CALIB_MIN_TOTAL_SAMPLES, BUCKET_MATURITY_GRACE_S, MEAN_WINDOW, CV_WINDOW


def minimum_test_duration_s(real_cadence_per_sec):
    """real_cadence_per_sec: the primary bucket's own real, measured
    per-member collective rate (the exact number node_aggregator_ref.py
    already prints as "measured cadence ... rate=X.XX/sec" -- reuse
    that real, live-measured number directly, never a guess)."""
    if real_cadence_per_sec <= 0:
        raise ValueError("real_cadence_per_sec must be a real, positive, measured rate")
    t_calib = CALIB_MIN_TOTAL_SAMPLES / real_cadence_per_sec
    t_grace = BUCKET_MATURITY_GRACE_S
    t_window_buffer = CV_WINDOW / real_cadence_per_sec
    return t_calib + t_grace + t_window_buffer


def preflight_check(planned_duration_s, real_cadence_per_sec, label=""):
    required = minimum_test_duration_s(real_cadence_per_sec)
    sufficient = planned_duration_s >= required
    verdict = "SUFFICIENT" if sufficient else "TOO SHORT"
    msg = (f"[preflight{' ' + label if label else ''}] planned={planned_duration_s:.1f}s "
           f"required={required:.1f}s (calib={CALIB_MIN_TOTAL_SAMPLES/real_cadence_per_sec:.1f}s + "
           f"grace={BUCKET_MATURITY_GRACE_S:.1f}s + window_buffer={CV_WINDOW/real_cadence_per_sec:.1f}s "
           f"@ rate={real_cadence_per_sec:.2f}/sec) -> {verdict}")
    return sufficient, required, msg


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: preflight_duration_check.py <planned_duration_s> <real_cadence_per_sec> [label]")
        sys.exit(2)
    planned = float(sys.argv[1])
    rate = float(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else ""
    sufficient, required, msg = preflight_check(planned, rate, label)
    print(msg)
    sys.exit(0 if sufficient else 1)
