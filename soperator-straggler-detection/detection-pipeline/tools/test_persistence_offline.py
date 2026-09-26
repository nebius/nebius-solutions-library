#!/usr/bin/env python3
"""Offline, no-GPU validation of the alert layer's OWN persistence
implementation (persistence.py), independent of node_aggregator.py's
internal state -- replayed against the same real captured sequences
already used to validate the classifier itself, so this is a genuine
second check with real data, not a synthetic sanity test.
"""
import json
import sys

sys.path.insert(0, "/root/P20c_alerting")
from persistence import CVPersistenceTracker
import thresholds as T

# P20a healthy long-run, bucket C, (ts_us, worst_rank, z) -- the same
# sequence tune_persistence.py used to validate the classifier's own
# threshold+persistence choice.
HEALTHY_SEQ = "/root/P20b_hardening/cv_zsequence.json"


def replay(seq_path, z_thresh, persist_window, persist_required):
    seq = json.load(open(seq_path))
    tracker = CVPersistenceTracker(z_thresh, persist_window, persist_required)
    events = []
    for ts, rank, z in seq:
        if tracker.observe(rank, z, ts):
            events.append((ts, rank, z))
    return events


def test_healthy_regression():
    """Step 2, case 1: the original regression check -- fix must not
    change this result. All entries in the real captured sequence have
    distinct timestamps already, so dedup is a no-op here; this proves
    the fix didn't accidentally change behavior on real, already-valid
    data."""
    events = replay(HEALTHY_SEQ, T.CV_Z_THRESH, T.PERSIST_WINDOW, T.PERSIST_REQUIRED)
    print(f"P20a healthy sequence (2690 bucket-C windows, ~117 min): {len(events)} alert-layer fires")
    assert len(events) == 0, "REGRESSION: alert layer's own persistence check fires on known-healthy data"
    print("PASS: zero fires, matching the pre-fix result and the classifier's own "
          "zero-FP result (Stage 2/3).\n")


def test_bug_reproduction():
    """Step 2, case 2: the actual bug-fix verification. Reproduces the
    live Gap-2 finding directly: ONE real elevated sample (z=90, above
    CV_Z_THRESH=60), polled repeatedly at the OLD 2.5s poll interval,
    within the OLD 10s staleness window that made this possible -- i.e.
    the exact same (ts, z) pair handed to observe() multiple times, the
    way a caller polling faster than the underlying window-close cadence
    would see it. Before the fix: observe() blindly appended on every
    call, so 3 calls = 3 history entries = fires. After the fix: repeat
    timestamps are deduped, so this must NOT fire."""
    tracker = CVPersistenceTracker(60.0, 3, 3)
    ts, z = 1_000_000.0, 90.0  # one real window: elevated, same sample
    fired_any = False
    for _ in range(4):  # 4 polls, same underlying sample every time
        if tracker.observe("rank4", z, ts):
            fired_any = True
    print(f"one real sample polled 4x at the same timestamp: fired={fired_any}")
    assert not fired_any, ("BUG STILL PRESENT: a single elevated sample polled "
                            "repeatedly satisfied the 3-consecutive-window requirement")
    print("PASS: repeated polls of the same underlying sample no longer count as "
          "separate windows -- the bug found live in Gap 2 is fixed.\n")


def test_true_positive_distinct_windows():
    """Step 2, case 3: sensitivity must be unchanged. Three GENUINELY
    DISTINCT underlying samples (different timestamps, matching a real
    ~2.5s window-close cadence), all elevated -- must still fire, exactly
    as the pre-fix implementation did. Confirms the fix only rejects
    duplicate timestamps, not legitimate consecutive distinct ones."""
    tracker = CVPersistenceTracker(60.0, 3, 3)
    samples = [(1_000_000.0, 90.0), (1_000_002.5, 88.0), (1_000_005.0, 95.0)]
    fired_at = None
    for ts, z in samples:
        if tracker.observe("rank4", z, ts):
            fired_at = ts
    print(f"three distinct elevated windows: fired_at={fired_at}")
    assert fired_at == samples[-1][0], ("REGRESSION: three genuinely distinct elevated "
                                         "windows no longer fire on the 3rd -- fix made "
                                         "persistence stricter than intended")
    print("PASS: three distinct elevated windows still fire on the 3rd, exactly as "
          "before the fix -- sensitivity unchanged.\n")


def main():
    print("=== Alert-layer persistence check (independent of node_aggregator.py) ===")
    print(f"Thresholds under test: CV_Z_THRESH={T.CV_Z_THRESH}, "
          f"PERSIST_WINDOW={T.PERSIST_WINDOW}, PERSIST_REQUIRED={T.PERSIST_REQUIRED}\n")

    test_healthy_regression()
    test_bug_reproduction()
    test_true_positive_distinct_windows()
    print("All persistence.py regression + bug-fix checks passed.")


if __name__ == "__main__":
    main()
