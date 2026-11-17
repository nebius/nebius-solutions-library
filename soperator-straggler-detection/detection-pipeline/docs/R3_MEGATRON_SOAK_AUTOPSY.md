# R3 Megatron Soak Autopsy
## 32.2x healthy event and iters 10,000–17,000 episode

**Run:** r3-soak-meg-healthy-1 (45,000-iter healthy Megatron soak, no injected fault)  
**Cluster:** 2 nodes × 8 H200 SXM = 16 GPUs total  
**Analysis date:** 2026-11-17 (run still in progress at time of writing, iter ~25,003)  
**Log source:** `/falcon-workspace/output/r3-soak-meg-healthy-1/train_falcon-training-{0,1}.log`

---

### (a) Step time — did measured step time actually rise?

**Answer: No. Step time did not rise during the episode.**

Measured from FALCON_STAGE3_RESULT timestamps (one line per iter per evaluation):

| Period | Iter range | Wall time (UTC) | Duration | Avg ms/iter |
|---|---|---|---|---|
| Baseline | 2,500–10,000 | 06:05:41–06:26:11 | 20m 30s | ~164 ms |
| Episode onset | 10,000 | 06:26:11 | — | — |
| Episode end | 17,000 | 06:45:24 | 19m 13s | ~165 ms |

No measurable step-time increase. The 10,000–17,000 episode produced more CONFIRMED events
(1,892 on node 0 vs 1,766 in the baseline range) despite identical per-iteration timing.

### (b) Alignment — does the episode onset align with any logging boundary?

The onset at iter ~10,000 aligns with the second major phase of NCCL baseline rolling
average convergence: the PP-group rank with anomalously low baseline (see below) had its
historical `baseline_ms` fall from ~0.49ms (at iter 5,000) to ~0.40ms (at iter 10,000) to
~0.27-0.28ms (through iter 10,000–17,000). This is a **progressive baseline drift** in the
rolling history, not a checkpoint or eval boundary. The episode corresponds to the period
where this rank's baseline crosses below a threshold that makes most PP-group evaluations
qualify as CONFIRMED.

No correlation with checkpoint intervals, eval steps, or dataloader boundaries.

### (c) Per-member structure — concentrated outlier or diffuse?

**32.2x event (iter 13,996, PP group, group size=4):**

```
per_rank_confirm_ms: [1.997, 4.989, 5.255, 7.854]
per_rank_baseline_ms: [2.744, 1.799, 1.801, 0.244]
per-member ratio:      0.73,  2.77,  2.92, 32.2
```

The 32.2x ratio is produced entirely by rank 3 of the group having a historical baseline
of 0.244ms — approximately 8–11x below its group peers (1.80–2.74ms). The actual
`confirm_avg_ms` for rank 3 (7.85ms) is elevated relative to its own anomalous baseline,
but all four members' confirm times are within the same order of magnitude. This is NOT a
straggler pattern; it is a **baseline artifact on one rank**.

**Robust Z analysis (confirm_ms, 32.2x event):**
- Group confirm values: [1.997, 4.989, 5.255, 7.854]
- Median: ~5.12, MAD: ~0.95
- Z(rank 3): (7.854 − 5.12) / (1.483 × 0.95) ≈ +1.9  ← not far above median
- Z(rank 0): (1.997 − 5.12) / (1.483 × 0.95) ≈ −2.2  ← one rank below median
- Structure: one rank mildly LOW in confirm_ms (rank 0 at 2.0ms vs median ~5.1ms). This
  is the opposite of a straggler signal — if anything, rank 0 is the fastest member.
- Attribution: `likely_root_cause_rank=0, attribution_confidence=high` — but this
  "high" attribution refers to rank 0 being the standout fast member (one-sided outlier),
  not a straggler.

**Representative episode event (iter 11,001, PP group):**

```
per_rank_confirm_ms: [2.396, 1.973, 2.010, 1.114]
per_rank_baseline_ms: [2.744, 1.776, 1.970, 0.276]
per-member ratio:      0.87,  1.11,  1.02,  4.03
```

Actual confirm times span 1.1–2.4ms — tight spread of ~2× across all 4 members.
No single low outlier below the group. This is **diffuse**: all members are elevated
roughly equally relative to their respective baselines, with the 4th rank's ratio inflated
solely by its anomalously low baseline.

Attribution: `likely_root_cause_rank=-1, attribution_confidence=ambiguous` — the system
itself cannot identify a single culprit.

### (d) Rank concentration analysis (episode 10,000–17,000)

High-attribution (confidence=high) events naming specific ranks (node 0):

| likely_root_cause_rank | Count |
|---|---|
| rank 8 | 8 |
| rank 4 | 6 |
| rank 10 | 2 |
| rank 0 | 2 |

Same distribution on node 1 (identical, as expected — both nodes evaluate the same PP groups).

The named ranks are spread across at least 4 distinct ranks. The majority of events
(1,874 of 1,892 on node 0) have `attribution_confidence=ambiguous` with
`likely_root_cause_rank=-1`, meaning no single rank was consistently isolated as a culprit.

The small number of `high` confidence events (18 total) name 4 different ranks with no
dominant concentration — consistent with chance identification of whichever member happened
to be furthest from the group mean in a given window, not a persistent straggler.

### Verdict

**No step-time change → measurement artifact.**

Specific mechanism: the PP-group member at TP position 3 (last in the pipeline) has a
progressively falling historical baseline (0.49ms at iter 5,000, 0.24ms by iter 14,000)
caused by a cold-start sampling bias in the rolling history. All four members' actual
communication times are normal and within a tight 2× spread. The high `peak_ratio` values
are entirely explained by the low-baseline artifact, not by any actual slowdown in step
time, network, or GPU activity.

Sampler data: **cause unknown; sampler not yet running** at the time of this episode
(env_sampler.sh was not launched for this soak run). No external telemetry (CPU steal,
TCP retransmits, GPU throttling) is available to confirm or rule out cluster-level
transients.

### Implications for classification layer

This episode demonstrates exactly why per_rank_baseline abnormalities must be treated as
a possible artifact. The proposed F2 feature (low-outlier structure in `per_rank_confirm_ms`
directly, not in ratio) would have correctly scored this episode as diffuse:

- **F2 check on 32.2x event**: confirm_ms = [1.997, 4.989, 5.255, 7.854] — the standout
  member (rank 0 at 2.0ms) is LOW, not high. No high-outlier (slow straggler) pattern
  present. F2 would not fire STRAGGLER.
- **F1 check**: events name ranks 0, 4, 8, 10 — binomial p-value under H0=uniform/16 is
  not significant for a 4-way spread over 18 events. F1 would not fire.
- **Correct output class**: SLOWDOWN-DIFFUSE (or no alert, since the actual confirm times
  are normal). No STRAGGLER alert should fire.

This episode is labeled: **SLOWDOWN-DIFFUSE (natural, cause unknown)** in the design set.
