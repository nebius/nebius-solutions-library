#!/usr/bin/env python3
"""
falcon_alert_layer.py — classification-based alert layer for Falcon straggler detection.

WHAT THIS DOES
--------------
Consumes FALCON_STAGE3_RESULT lines from one or more training pod logs (live-tail or
replay).  Applies a sliding-window classifier to distinguish:

  STRAGGLER        — one rank is persistently slow and identifiable
  SLOWDOWN-DIFFUSE — cluster-wide or diffuse slowdown; no single rank is the cause

Neither class is silently dropped.  STRAGGLER is actionable; SLOWDOWN-DIFFUSE is
informational (check network, co-tenants, GPU throttling).

DOES NOT CHANGE
---------------
- The Falcon Stage-1, Stage-2, or Stage-3 detectors themselves.
- Any training pod manifest, Megatron script, or Inspector plugin.
- Any VictoriaMetrics push logic.

Consumes ONLY the already-emitted FALCON_STAGE3_RESULT log lines.

FEATURES
--------
F1 — Concentration: fraction of high-confidence events in the window that name a single
     (rank, node) pair.  Tested against H0: uniform over all 16 ranks (binomial).

F2 — Low-outlier structure: in the per_rank_confirm_ms array for each implicated group,
     is exactly one member far BELOW the group median (robust Z ≤ −2) while all others
     are near or above?  This is the straggler signature (one slow rank makes peers wait,
     so the peers' recv times are elevated while the slow rank's own recv is LOW).  If all
     members are tightly clustered → diffuse.

F3 — Multi-group corroboration: the same rank appears high-confidence in ≥2 different
     group types (TP + PP, etc.) within the window.

F4 — Step-time effect (informational only, NOT used for classification): tracks whether
     per-iteration timestamps from FALCON_STAGE3_RESULT lines show increased iteration
     duration within the window.

CLASSIFICATION RULE
-------------------
  STRAGGLER    if F1 (binomial p < 0.01) AND F2 (low-outlier present) [AND optionally F3]
  SLOWDOWN-DIFFUSE  otherwise, when enough events exist to classify

The cross-node correlation gate (suppress if both nodes show equal event rates) is
implemented as an evidence test only via --test-cross-node-gate.  It is NOT used in
classification regardless of result.

USAGE
-----
  # Replay mode (from local log files):
  python3 falcon_alert_layer.py --logs node0.log node1.log --window 120 --min-events 5

  # Live-tail mode (kubectl exec):
  kubectl exec -n falcon-k8s falcon-training-0 -- tail -f /path/to/log.log | \\
    python3 falcon_alert_layer.py --stdin --window 120 --min-events 5

  # Cross-node gate evidence test on saved fault trials:
  python3 falcon_alert_layer.py --test-cross-node-gate --logs node0.log node1.log

DESIGN / HELD-OUT DISCIPLINE
-----------------------------
This rule was frozen on the DESIGN set (all R1–R3 sessions except the held-out episodes
listed in docs/K8S_DECISIONS_PENDING.md).  Do not adjust thresholds to fit held-out data.
See Point 5 of the R3 steering note for the full discipline.
"""

import sys
import re
import math
import time
import argparse
import datetime
import statistics
import threading
from collections import defaultdict, deque
from typing import List, Optional, Tuple, Dict

# ---------------------------------------------------------------------------
# Constants (adjustable via CLI flags, frozen after design-set calibration)
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_S = 120          # sliding window length (seconds)
DEFAULT_MIN_EVENTS = 5          # minimum CONFIRMED events in window to classify
DEFAULT_BINOMIAL_P_THRESH = 0.01  # F1: p-value threshold for concentration
DEFAULT_LOW_Z_THRESH = -1.5     # F2: robust Z for "low outlier" in confirm_ms array
N_TOTAL_RANKS = 16              # cluster total ranks (2 nodes × 8 GPUs)

# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

# FALCON_STAGE3_RESULT ts=... rank=N iter=N group=TP|PP|DP status=CONFIRMED|NOT_CONFIRMED ...
_STAGE3_RE = re.compile(
    r'FALCON_STAGE3_RESULT'
    r'\s+ts=(?P<ts>\S+)'
    r'\s+rank=(?P<rank>\d+)'
    r'\s+iter=(?P<iter>\d+)'
    r'\s+group=(?P<group>\S+)'
    r'\s+status=(?P<status>\S+)'
    r'.*?peak_ratio=(?P<peak_ratio>[0-9.]+)'
    r'.*?likely_root_cause_rank=(?P<lrcr>-?\d+)'
    r'.*?attribution_confidence=(?P<attr_conf>\S+)'
    r'.*?per_rank_confirm_ms=\[(?P<prconf>[^\]]+)\]'
    r'.*?per_rank_baseline_ms=\[(?P<prbas>[^\]]+)\]'
)

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def parse_ts(ts_str: str) -> float:
    """Parse ISO8601 UTC timestamp to POSIX seconds."""
    try:
        dt = datetime.datetime.strptime(ts_str, _TS_FMT)
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        # Fallback for varying sub-second precision
        try:
            dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return time.time()


def parse_float_list(s: str) -> List[float]:
    """Parse a bracketed comma-separated list of floats like ['1.5', '2.3', ...]."""
    return [float(x.strip().strip("'\"")) for x in s.split(",") if x.strip()]


class Stage3Event:
    __slots__ = (
        "ts", "rank", "iter_num", "group", "status",
        "peak_ratio", "lrcr", "attr_conf",
        "per_rank_confirm", "per_rank_baseline",
        "source_node",  # optional: which log file / pod this came from
    )

    def __init__(self, m: re.Match, source_node: str = ""):
        self.ts = parse_ts(m.group("ts"))
        self.rank = int(m.group("rank"))
        self.iter_num = int(m.group("iter"))
        self.group = m.group("group")
        self.status = m.group("status")
        self.peak_ratio = float(m.group("peak_ratio"))
        self.lrcr = int(m.group("lrcr"))
        self.attr_conf = m.group("attr_conf")
        self.per_rank_confirm = parse_float_list(m.group("prconf"))
        self.per_rank_baseline = parse_float_list(m.group("prbas"))
        self.source_node = source_node


def parse_line(line: str, source_node: str = "") -> Optional[Stage3Event]:
    """Return a Stage3Event if the line is a FALCON_STAGE3_RESULT, else None."""
    if "FALCON_STAGE3_RESULT" not in line:
        return None
    m = _STAGE3_RE.search(line)
    if m is None:
        return None
    return Stage3Event(m, source_node)


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------

def _robust_z_scores(values: List[float]) -> List[float]:
    """
    Robust Z-scores using median and MAD.
    Z_i = (x_i − median) / (1.4826 × MAD)
    Returns list of same length as values, or [] if fewer than 2 values.
    """
    if len(values) < 2:
        return []
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad < 1e-9:
        # Degenerate: all identical
        return [0.0] * len(values)
    scale = 1.4826 * mad
    return [(v - med) / scale for v in values]


def compute_f2(events: List[Stage3Event]) -> Tuple[bool, str]:
    """
    F2 — Straggler structural pattern in per_rank_confirm_ms.

    Two straggler signatures are recognized:

    (a) SINGLE-LOW-OUTLIER: exactly one member with robust Z ≤ low_z_thresh (far BELOW
        the group median) while remaining members are near or above the median.
        This occurs in large groups (DP ≥8 members) where the straggler's barrier-induced
        low recv-wait stands out against many elevated peers.

    (b) BIMODAL-SPLIT: the confirm_ms values separate into two clusters where the high
        cluster is ≥5× the low cluster's median, with members split roughly 1:n-1 or
        2:n-2.  This is the PP small-group straggler pattern: peers who waited for the
        straggler have high recv-wait; the straggler (and possibly its direct ring
        neighbor) has low recv-wait because it arrived late.

    A DIFFUSE pattern is: all members within a tight spread (max/min < 5×).

    Returns (is_straggler_pattern, description).
    """
    # Check a representative sample (most recent 20 events)
    sample = events[-20:] if len(events) > 20 else events

    straggler_count = 0
    diffuse_count = 0
    ambiguous_count = 0

    for ev in sample:
        vals = ev.per_rank_confirm
        if len(vals) < 2:
            ambiguous_count += 1
            continue

        sorted_vals = sorted(vals)
        max_v = sorted_vals[-1]
        min_v = sorted_vals[0]

        if max_v < 1e-6 or min_v < 0:
            ambiguous_count += 1
            continue

        ratio_max_min = max_v / max(min_v, 1e-6)

        if ratio_max_min < 5.0:
            # Tight spread — diffuse (all elevated equally, or all normal)
            diffuse_count += 1
            continue

        # Large spread: check for single-low-outlier or bimodal pattern
        zs = _robust_z_scores(vals)

        # (a) Single-low-outlier pattern
        n_low = sum(1 for z in zs if z <= DEFAULT_LOW_Z_THRESH)
        n_near_or_above = sum(1 for z in zs if z >= -0.5)
        if n_low == 1 and n_near_or_above >= len(vals) - 1:
            straggler_count += 1
            continue

        # (b) Bimodal-split pattern: find a gap separating low cluster from high cluster.
        # IMPORTANT: the high cluster must also be absolutely large (≥10ms) to rule out
        # the healthy-run artifact where one rank has an anomalously low baseline, making
        # confirm_ms look bimodal even on healthy runs (all values remain in the 0.2–8ms
        # range on a healthy cluster; genuine fault-induced wait times are 10ms+).
        gaps = []
        for i in range(len(sorted_vals) - 1):
            lo = sorted_vals[i]
            hi = sorted_vals[i + 1]
            gaps.append((hi / max(lo, 1e-6), i))
        if gaps:
            best_ratio, split_idx = max(gaps, key=lambda x: x[0])
            n_low_cluster = split_idx + 1
            n_high_cluster = len(sorted_vals) - n_low_cluster
            # Bimodal: gap ≥ 5× AND at least 1 in each cluster AND not degenerate
            if best_ratio >= 5.0 and n_low_cluster >= 1 and n_high_cluster >= 1:
                high_cluster = sorted_vals[split_idx + 1:]
                low_cluster = sorted_vals[:split_idx + 1]
                high_med = statistics.median(high_cluster)
                # Absolute floor: high cluster must be ≥10ms to qualify as genuine fault.
                # Healthy runs have per_rank_confirm in the 0.1–8ms range; network/barrier
                # faults push waiting ranks to 20ms+.
                if high_med >= 10.0 and high_med > 5.0 * max(statistics.median(low_cluster), 1e-6):
                    straggler_count += 1
                    continue

        # Could not assign clearly
        ambiguous_count += 1

    total_scored = straggler_count + diffuse_count + ambiguous_count
    if total_scored == 0:
        return False, "insufficient data"

    # Require clear majority (>60%) for a verdict
    if straggler_count > 0.6 * total_scored:
        return True, (
            f"straggler pattern in {straggler_count}/{total_scored} sampled events "
            f"(low-outlier or bimodal-split in per_rank_confirm_ms)"
        )
    elif diffuse_count >= 0.6 * total_scored:
        return False, (
            f"diffuse pattern in {diffuse_count}/{total_scored} sampled events "
            f"(all members within tight spread in confirm_ms)"
        )
    else:
        return False, (
            f"ambiguous in sampled events (straggler={straggler_count} diffuse={diffuse_count} "
            f"ambig={ambiguous_count})"
        )


def compute_f1(high_conf_events: List[Stage3Event],
               n_total_ranks: int = N_TOTAL_RANKS) -> Tuple[bool, float, Optional[Tuple[int, str]], str]:
    """
    F1 — Concentration: binomial test against H0 = uniform over n_total_ranks.

    Among high-attribution-confidence events (attr_conf=='high') that name a specific
    likely_root_cause_rank, what fraction name the same (rank, node) pair?

    Returns (is_concentrated, p_value, best_rank_node, description).
    """
    if not high_conf_events:
        return False, 1.0, None, "no high-confidence events"

    # Only events with a real rank (lrcr >= 0)
    named = [ev for ev in high_conf_events if ev.lrcr >= 0]
    if not named:
        return False, 1.0, None, "no high-conf events with named rank"

    # Count per (rank, node)
    counts: Dict[Tuple[int, str], int] = defaultdict(int)
    for ev in named:
        counts[(ev.lrcr, ev.source_node)] += 1

    n = len(named)
    best_key = max(counts, key=lambda k: counts[k])
    k = counts[best_key]

    # Binomial p-value under H0: P(X >= k | n trials, p=1/n_total_ranks)
    p0 = 1.0 / n_total_ranks
    # Compute P(X >= k) = 1 - P(X <= k-1) using CDF approximation
    # For small n, exact; for large n, use normal approximation
    p_val = _binomial_tail_p(n, k, p0)

    concentrated = p_val < DEFAULT_BINOMIAL_P_THRESH
    desc = (
        f"rank={best_key[0]} node={best_key[1]!r} named {k}/{n} high-conf events "
        f"(p={p_val:.4f}, threshold={DEFAULT_BINOMIAL_P_THRESH})"
    )
    return concentrated, p_val, best_key, desc


def _binomial_tail_p(n: int, k: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p).  Exact computation for small n; normal approx otherwise."""
    if n <= 200:
        # Exact using survival function via cumulative sum
        # P(X >= k) = sum_{i=k}^{n} C(n,i) * p^i * (1-p)^(n-i)
        log_p = math.log(p) if p > 0 else -1e18
        log_q = math.log(1 - p) if p < 1 else -1e18
        # Use log-space to avoid overflow
        total = 0.0
        log_binom = 0.0  # log C(n, 0)
        # Build up log C(n, i) iteratively
        # First compute log C(n,k) directly
        log_cnk = sum(math.log(n - j) - math.log(j + 1) for j in range(k))
        for i in range(k, n + 1):
            if i > k:
                log_cnk += math.log(n - i + 1) - math.log(i)
            term = math.exp(log_cnk + i * log_p + (n - i) * log_q)
            total += term
        return min(1.0, total)
    else:
        # Normal approximation
        mu = n * p
        sigma = math.sqrt(n * p * (1 - p))
        if sigma < 1e-9:
            return 0.0 if k > mu else 1.0
        z = (k - 0.5 - mu) / sigma  # continuity correction
        return _normal_sf(z)


def _normal_sf(z: float) -> float:
    """Survival function P(Z > z) for standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def compute_f3(high_conf_events: List[Stage3Event]) -> Tuple[bool, str]:
    """
    F3 — Multi-group corroboration: same rank appears in ≥2 different group types.
    """
    named = [ev for ev in high_conf_events if ev.lrcr >= 0]
    if not named:
        return False, "no named high-conf events"

    # Per rank: collect distinct group types
    rank_groups: Dict[int, set] = defaultdict(set)
    for ev in named:
        rank_groups[ev.lrcr].add(ev.group)

    for rank, groups in rank_groups.items():
        if len(groups) >= 2:
            return True, f"rank={rank} confirmed in groups: {sorted(groups)}"

    return False, (
        f"no rank confirmed in ≥2 group types (groups per rank: "
        + ", ".join(f"rank{r}:{sorted(g)}" for r, g in sorted(rank_groups.items()))
        + ")"
    )


def compute_f4_informational(events: List[Stage3Event]) -> str:
    """
    F4 — Step-time effect (informational only, not used for classification).

    Compares inter-event timestamp spacing in the first vs second half of the window
    as a rough proxy for iteration speed.  Logged but never read by the classifier.
    """
    if len(events) < 4:
        return "F4=insufficient_data"
    half = len(events) // 2
    first_half = events[:half]
    second_half = events[half:]

    def mean_gap(evs: List[Stage3Event]) -> float:
        if len(evs) < 2:
            return 0.0
        gaps = [evs[i + 1].ts - evs[i].ts for i in range(len(evs) - 1) if evs[i + 1].ts > evs[i].ts]
        return statistics.mean(gaps) if gaps else 0.0

    g1 = mean_gap(first_half)
    g2 = mean_gap(second_half)
    if g1 < 1e-6:
        return "F4=no_timing_data"
    pct_change = (g2 - g1) / g1 * 100
    return f"F4=step_time_change({pct_change:+.1f}%,first_half_gap={g1:.3f}s,second_half_gap={g2:.3f}s)"


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class AlertClassifier:
    """
    Sliding-window classifier over FALCON_STAGE3_RESULT CONFIRMED events.

    Call .add_event(event) for each parsed line, then .classify() on a schedule
    or after each add.  Emits STRAGGLER or SLOWDOWN-DIFFUSE to stdout.
    """

    def __init__(self, window_s: float = DEFAULT_WINDOW_S,
                 min_events: int = DEFAULT_MIN_EVENTS):
        self.window_s = window_s
        self.min_events = min_events
        self._window: deque = deque()   # Stage3Event, CONFIRMED only
        self._last_emit_class: Optional[str] = None
        self._last_emit_ts: float = 0.0
        self._emit_cooldown_s: float = 30.0  # don't re-emit same class within this window
        self._straggler_ranks_seen: set = set()  # track active STRAGGLER rank keys

    def add_event(self, ev: Stage3Event):
        if ev.status != "CONFIRMED":
            return
        self._window.append(ev)
        self._evict_old(ev.ts)

    def _evict_old(self, now_ts: float):
        cutoff = now_ts - self.window_s
        while self._window and self._window[0].ts < cutoff:
            self._window.popleft()

    def classify(self) -> Optional[str]:
        """
        Run classification on current window.  Returns the alert class string
        ("STRAGGLER: ...", "SLOWDOWN-DIFFUSE: ...", or None if not enough data).
        Side effect: prints the alert.
        """
        events = list(self._window)
        if len(events) < self.min_events:
            return None

        now_ts = events[-1].ts if events else time.time()
        self._evict_old(now_ts)
        events = list(self._window)
        if len(events) < self.min_events:
            return None

        high_conf = [ev for ev in events if ev.attr_conf == "high" and ev.lrcr >= 0]

        # F1: concentration
        f1_conc, f1_pval, f1_best, f1_desc = compute_f1(high_conf)

        # F2: low-outlier structure in confirm_ms
        f2_low_outlier, f2_desc = compute_f2(events)

        # F3: multi-group corroboration
        f3_multi, f3_desc = compute_f3(high_conf)

        # F4: step-time (informational)
        f4_desc = compute_f4_informational(events)

        # Classification decision
        n_events = len(events)
        n_hc = len(high_conf)
        window_start = datetime.datetime.fromtimestamp(events[0].ts, tz=datetime.timezone.utc).strftime("%H:%M:%SZ")
        window_end = datetime.datetime.fromtimestamp(events[-1].ts, tz=datetime.timezone.utc).strftime("%H:%M:%SZ")
        common_prefix = (
            f"window=[{window_start}..{window_end}] "
            f"events={n_events} high_conf={n_hc} "
            f"F1={f1_desc} F2={f2_desc} F3={f3_desc} {f4_desc}"
        )

        if f1_conc and f2_low_outlier:
            # STRAGGLER
            rank = f1_best[0] if f1_best else "?"
            node = f1_best[1] if f1_best else "?"
            class_str = (
                f"STRAGGLER rank={rank} node={node!r} "
                f"confidence={'HIGH' if f3_multi else 'MEDIUM'} "
                f"{common_prefix}"
            )
            self._emit(class_str, "STRAGGLER", rank_key=f1_best)
            return class_str
        else:
            class_str = (
                "SLOWDOWN-DIFFUSE — Cluster-wide slowdown detected — "
                "check network, co-tenants, GPU throttling. "
                + common_prefix
            )
            self._emit(class_str, "SLOWDOWN-DIFFUSE")
            return class_str

    def _emit(self, msg: str, alert_class: str, rank_key=None):
        """Print the alert, respecting cooldown to avoid flood."""
        now = time.time()
        # STRAGGLER always overrides a recent SLOWDOWN-DIFFUSE (class escalation allowed).
        # Cooldown only suppresses same-class repeats.
        if (self._last_emit_class == alert_class
                and now - self._last_emit_ts < self._emit_cooldown_s):
            # For STRAGGLER, only suppress if same rank
            if alert_class != "STRAGGLER":
                return
            if rank_key and rank_key in self._straggler_ranks_seen:
                return

        ts_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        print(f"[FALCON_ALERT {ts_str}] {msg}", flush=True)
        self._last_emit_class = alert_class
        self._last_emit_ts = now
        if alert_class == "STRAGGLER" and rank_key:
            self._straggler_ranks_seen.add(rank_key)


# ---------------------------------------------------------------------------
# Cross-node gate evidence test (not used in production classification)
# ---------------------------------------------------------------------------

def test_cross_node_gate(log_files: List[str], window_s: float = 120.0) -> None:
    """
    Evidence test only: test whether the cross-node correlation gate
    (suppress if both nodes show equal event rates) would have suppressed
    real straggler trials.  NOT adopted into the alert layer.

    Prints a report to stdout.
    """
    print("[CROSS-NODE-GATE-TEST] Loading events from all logs...", flush=True)
    all_events: List[Stage3Event] = []
    for lf in log_files:
        node_name = lf.rsplit("/", 1)[-1].replace(".log", "")
        try:
            with open(lf) as f:
                for line in f:
                    ev = parse_line(line, source_node=node_name)
                    if ev and ev.status == "CONFIRMED":
                        all_events.append(ev)
        except (OSError, IOError) as e:
            print(f"[CROSS-NODE-GATE-TEST] WARNING: could not read {lf}: {e}", flush=True)

    if not all_events:
        print("[CROSS-NODE-GATE-TEST] No events found.", flush=True)
        return

    all_events.sort(key=lambda e: e.ts)
    nodes = sorted({ev.source_node for ev in all_events})
    print(f"[CROSS-NODE-GATE-TEST] Loaded {len(all_events)} CONFIRMED events from {len(nodes)} node(s): {nodes}")

    # Slide a window and compute per-node rates
    suppressed = 0
    total_windows = 0
    t_start = all_events[0].ts
    t_end = all_events[-1].ts
    step = window_s / 2

    t = t_start + window_s
    while t <= t_end + step:
        window_events = [ev for ev in all_events if t - window_s <= ev.ts < t]
        if len(window_events) < 5:
            t += step
            continue
        total_windows += 1
        per_node: Dict[str, int] = defaultdict(int)
        for ev in window_events:
            per_node[ev.source_node] += 1

        if len(per_node) >= 2:
            rates = list(per_node.values())
            # Gate: both nodes within 20% of each other → suppress
            max_r, min_r = max(rates), min(rates)
            if min_r > 0 and max_r / min_r <= 1.2:
                suppressed += 1
        t += step

    print(f"[CROSS-NODE-GATE-TEST] Windows evaluated: {total_windows}")
    print(f"[CROSS-NODE-GATE-TEST] Windows where gate WOULD suppress: {suppressed}")
    print(f"[CROSS-NODE-GATE-TEST] Suppression rate: {suppressed/max(1,total_windows)*100:.1f}%")
    print("[CROSS-NODE-GATE-TEST] NOTE: This gate is NOT adopted. See docs/K8S_DECISIONS_PENDING.md.")
    print("[CROSS-NODE-GATE-TEST] Verdict: check whether these suppressed windows correspond to")
    print("[CROSS-NODE-GATE-TEST]   real straggler trials (would be false negatives) or healthy/")
    print("[CROSS-NODE-GATE-TEST]   diffuse windows (would be true suppressions).")


# ---------------------------------------------------------------------------
# Main — file replay and stdin modes
# ---------------------------------------------------------------------------

def stream_events_from_files(log_files: List[str]) -> "typing.Generator":
    """Yield Stage3Event in timestamp order from one or more log files."""
    all_events: List[Stage3Event] = []
    for lf in log_files:
        node_name = lf.rsplit("/", 1)[-1].replace(".log", "")
        try:
            with open(lf) as f:
                for line in f:
                    ev = parse_line(line, source_node=node_name)
                    if ev:
                        all_events.append(ev)
        except (OSError, IOError) as e:
            print(f"[falcon_alert_layer] WARNING: could not read {lf}: {e}", file=sys.stderr)

    all_events.sort(key=lambda e: e.ts)
    yield from all_events


def stream_events_from_stdin() -> "typing.Generator":
    """Yield Stage3Event from stdin (live-tail mode)."""
    for line in sys.stdin:
        ev = parse_line(line.rstrip(), source_node="stdin")
        if ev:
            yield ev


def main():
    import typing  # noqa — used above in generator return type hints

    parser = argparse.ArgumentParser(
        description="Falcon alert layer: classify FALCON_STAGE3_RESULT streams as STRAGGLER or SLOWDOWN-DIFFUSE."
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--logs", nargs="+", metavar="FILE",
        help="Replay mode: one or more log files to classify."
    )
    mode_group.add_argument(
        "--stdin", action="store_true",
        help="Live-tail mode: read FALCON_STAGE3_RESULT lines from stdin."
    )
    mode_group.add_argument(
        "--test-cross-node-gate", action="store_true",
        help="Evidence test only: measure how often the cross-node gate would suppress. "
             "Requires --logs."
    )
    parser.add_argument(
        "--window", type=float, default=DEFAULT_WINDOW_S,
        help=f"Sliding window length in seconds (default: {DEFAULT_WINDOW_S})."
    )
    parser.add_argument(
        "--min-events", type=int, default=DEFAULT_MIN_EVENTS,
        help=f"Minimum CONFIRMED events in window to classify (default: {DEFAULT_MIN_EVENTS})."
    )
    parser.add_argument(
        "--classify-every", type=int, default=10,
        help="In replay mode: run classifier every N events (default: 10)."
    )
    parser.add_argument(
        "--gate-logs", nargs="+", metavar="FILE",
        help="Log files for --test-cross-node-gate."
    )

    args = parser.parse_args()

    if args.test_cross_node_gate:
        gate_logs = args.gate_logs or args.logs or []
        if not gate_logs:
            parser.error("--test-cross-node-gate requires --gate-logs or --logs")
        test_cross_node_gate(gate_logs, window_s=args.window)
        return

    classifier = AlertClassifier(window_s=args.window, min_events=args.min_events)

    if args.stdin:
        source = stream_events_from_stdin()
    elif args.logs:
        source = stream_events_from_files(args.logs)
    else:
        parser.error("One of --logs, --stdin, or --test-cross-node-gate is required.")
        return

    n = 0
    for ev in source:
        classifier.add_event(ev)
        n += 1
        if n % args.classify_every == 0:
            classifier.classify()

    # Final classification at end of stream
    classifier.classify()


if __name__ == "__main__":
    main()
