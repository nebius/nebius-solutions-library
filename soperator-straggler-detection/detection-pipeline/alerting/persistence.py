"""Standalone, independently-testable persistence tracker -- the exact
same "3 consecutive windows above threshold" rule as node_aggregator.py's
internal implementation, re-implemented here from scratch (not imported
from node_aggregator.py) so a bug in the aggregator's own internal state
can't silently become the alerting ground truth unchecked.

P20c gap-closure fix: observe() now requires the underlying sample's OWN
timestamp and deduplicates on it, per key. The bug this closes (found live
during Gap 2 testing): the caller polls on a fixed interval (e.g. 2.5s)
but the underlying window-close cadence is irregular and can run slower
than that; a poll can legitimately see the SAME last-known sample it saw
on the previous poll (VM instant queries return "last known value"
regardless of poll timing). Without timestamp dedup, that one real sample
got appended to history again on every poll that re-observed it, so a
single genuinely-isolated elevated reading -- exactly the kind of noise
"3 consecutive windows" exists to filter out -- could satisfy the
persistence requirement purely from being polled 3+ times, never having
occurred 3 times.

Chosen over the alternative (tightening the poll-side freshness threshold
to <= poll_interval): timestamp dedup is exact and immune to poll-timing
jitter in either direction. A threshold tied to poll_interval must assume
poll cadence and window-close cadence stay in a specific numeric
relationship; when they drift (GC pause, network delay, a slightly slow
window), a tightened threshold either lets a stale sample back in (bug
recurs) or wrongly discards a genuinely new one (silently drops a real
window, weakening detection with no signal it happened). Deduping on the
sample's own timestamp encodes the actual invariant needed -- one
observation per distinct underlying sample -- directly, with no
dependency on timing assumptions holding.
"""
from collections import deque, defaultdict


class CVPersistenceTracker:
    def __init__(self, z_thresh, persist_window, persist_required):
        self.z_thresh = z_thresh
        self.persist_window = persist_window
        self.persist_required = persist_required
        self.hist = defaultdict(lambda: deque(maxlen=persist_window))
        self.was_firing = defaultdict(bool)
        self.last_ts = {}

    def observe(self, key, z, ts):
        """key: any hashable identifying (hostname, rank, bucket) or
        similar. ts: the underlying sample's own timestamp (e.g. the VM
        series' value timestamp) -- REQUIRED, not the poll time. A call
        whose ts is not strictly newer than the last ts already recorded
        for this key is a no-op (same underlying sample re-polled, not a
        new window) and does not touch history or firing state.

        Returns True exactly on the tick this key's condition transitions
        into 'fired' (not on every tick while it holds), same
        distinct-event semantics as tune_persistence.py's simulate()."""
        prev_ts = self.last_ts.get(key)
        if prev_ts is not None and ts <= prev_ts:
            return False
        self.last_ts[key] = ts
        h = self.hist[key]
        h.append(z > self.z_thresh)
        fired = len(h) >= self.persist_required and all(list(h)[-self.persist_required:])
        is_new_event = fired and not self.was_firing[key]
        self.was_firing[key] = fired
        return is_new_event
