# K8s Straggler Detection — Pending Decisions

This file tracks architectural decisions that require Adam's explicit sign-off before they can be
adopted in the alert layer. Each entry records the question, the evidence that motivates it, and
the current status.

---

## Decision 7 — Static severity-ratio threshold  **[WITHDRAWN]**

**Question (original):** Should the alert layer gate on a fixed severity-ratio threshold (e.g.
≥3.0×) to suppress low-confidence events?

**Status: WITHDRAWN — do not adopt.**

**Reason for withdrawal (R3 Megatron soak, 2026-11):**

1. A 32.2× severity-ratio event was observed in the R3 Megatron healthy soak.  No fault was
   injected; the run was confirmed clean.  A 3.0× gate would have fired a STRAGGLER alert on a
   healthy run.

2. The Round 2 characterization of the healthy event rate (26 events/hr above 3.0× in Megatron
   healthy soaks) demonstrates that no static threshold can achieve 0 false STRAGGLER alerts on
   this cluster.  Thresholds low enough to catch real faults are also low enough to fire on natural
   healthy variation; thresholds high enough to suppress all healthy variation miss real faults.

**Consequence:** Severity ratio is retained as a *display field* only (already emitted on every
`[STRAGGLER-INCIDENT]` line).  It MUST NOT be used as the sole gate for a STRAGGLER vs DIFFUSE
classification decision.  Classification must use structural features (see Decision 10 and the
alert-layer design in `k8s/alert_layer/falcon_alert_layer.py`).

---

## Decision 8 — K8s platform label  **[OPEN]**

**Question:** Should the K8s alert layer emit a platform tag distinguishing K8s alerts from
Slurm alerts so that ops dashboards can filter them separately?

**Status: OPEN.** No blocking evidence yet.  Recommended default: yes, include
`platform=k8s` in emitted lines.  Waiting for Adam's confirmation of the ops toolchain.

---

## Decision 9 — Sustained-count confirmation (≥K events in W seconds)  **[INSUFFICIENT ALONE]**

**Question (original):** Can a simple "≥K high-confidence events in W seconds" rule serve as
the primary confirmation gate for a STRAGGLER alert?

**Status: INSUFFICIENT ALONE — do not adopt as the sole gate.**

**Reason (R3 Megatron soak, 2026-11):**

The iteration-10,000–17,000 episode produced a sustained burst of high-confidence events across
the full observation window.  Both nodes showed identical event rates throughout the episode.
A ≥K/W rule (for any reasonable K and W) would have been satisfied and would have fired a
STRAGGLER alert — yet the episode was **cluster-wide** (both nodes identical), containing no
straggler.

The core failure mode: a sustained count rule cannot distinguish:
- A single slow rank making its group wait (straggler) — events concentrate on one rank/node
- A cluster-wide slowdown (network, co-tenants, thermal) — events spread uniformly across all
  ranks and both nodes

Sustained count is a **necessary** condition for a STRAGGLER alert (reduces noise), but it is
NOT sufficient.  The alert layer MUST additionally check concentration structure (F1) and
per-rank outlier pattern (F2) before emitting STRAGGLER.  See the classification design in
`k8s/alert_layer/falcon_alert_layer.py`.

---

## Decision 10 — Time-to-alert acceptance criterion  **[OPEN — Adam decides]**

**Question:** What is the acceptable time-to-alert for injected faults, and at what fault
magnitudes (CPU-side, as % slowdown)?

**Proposed acceptance criterion (from R3 steering):**

- Injected faults at 10% / 25% / 50% / 100% CPU-side slowdown:
  - PRIMARY classification rate: 3/3
  - Time-to-STRAGGLER alert: ≤120 seconds

**Status: OPEN.**  Adam must confirm whether ≤120 s is the right bound given this cluster's
calibration period (~35 s NCCL bootstrap) and the 30 s env-sampler collection interval.
If ≤120 s is confirmed, it becomes a hard acceptance criterion logged here and enforced in the
held-out validation.

---

## Standing rules (not under decision)

These are confirmed and no longer require a decision:

- **0 STRAGGLER alerts** in ALL healthy sessions (natural + injected diffuse episodes) is a
  hard requirement, not a target.  One false STRAGGLER in a healthy run is a blocking failure.

- **SLOWDOWN-DIFFUSE must surface** (not be silently suppressed).  Both output classes must
  be visible in the alert log.

- **Data discipline**: design/held-out split is by whole session or whole episode; never by
  individual event or by time-halves within one soak.  Frozen once training data is committed.

- **Statistics**: do not use Poisson CIs or rule-of-three bounds.  Report per-session event
  rates (events/hr) + spread (min, max, median, n sessions).  Require ≥4 sessions of ≥2 h at
  different times of day before computing cross-session statistics.
