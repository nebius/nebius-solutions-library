"""P20c Step 1 -- alert-layer thresholds and persistence rule.

Every constant here is pulled from a real prior session's data, not
re-derived. Source cited on each line. These intentionally mirror
node_aggregator.py's own constants where the classifier already gates on
them (CV, mean, outlier_count) -- the alert layer re-implements the
threshold+persistence check independently against raw VM data rather than
trusting node_aggregator.py's internal fired flags, per this task's
explicit instruction. rank0_outlier_rate gets its OWN alert-layer gate
here since node_aggregator.py currently only emits the raw rate, with no
corresponding "_fired" flag.
"""

# --- CV persistence (the primary fail-slow signal) ---
# P20a.5 (P20b session): raised 20->60 specifically to drive the healthy
# false-positive rate to zero; re-confirmed zero across two independent
# healthy runs totalling ~177 minutes (P20b Stage 2 replay + Stage 3 fresh
# run). True-positive: P19f's rank-4 fault replayed under this setting
# still fires, z=94.5 (max observed in that fault window: 1179.4) -- see
# P20b final_report.md Stage 2. Re-confirmed again in the P20c-prerequisite
# session via a real live rank-1 fault injection (fired at t+1.3s, z up to
# ~1300 transient / 62-317 sustained).
CV_Z_THRESH = 60.0
PERSIST_WINDOW = 3
PERSIST_REQUIRED = 3  # "3 consecutive" -- maxlen=3 deque, all(last 3) True

# --- Mean (corroborating signal, never observed to fire on healthy data) ---
# node_aggregator.py / P20a Report Q2: zero agg_mean_fired events across the
# entire P20a 114-minute run (112 checkpoint events) and P20b's Stage 3
# 60-minute fresh run. Unmodified since P19f.
MEAN_Z_THRESH = 30.0
MEAN_MM_THRESH = 2.0

# --- outlier_count (corroborating signal ONLY -- known nonzero healthy rate) ---
# P19f: characterized as "medium/long-burst, naturally fires occasionally
# on real system noise". P20b Stage 2: 31 fires on the P20a healthy data,
# ranks 1 and 6 -- explicitly NOT zero, explicitly out of scope for the
# P20b threshold retune (task there was CV-specific). Because this
# statistic has a known nonzero false-fire rate on genuinely healthy data,
# the alert layer must NEVER let it alone drive a CONFIRMED verdict --
# it is corroborating evidence only (mirrors the P18k classifier's own
# class1/class2 tiering philosophy: a noisy signal never classifies alone).
OUTLIER_COUNT_THRESH = 4
OUTLIER_COUNT_MM_THRESH = 2.0

# --- rank0_outlier_rate (dedicated rank-0 check; replaces retired rank0_cv_z) ---
# P18f Stage 3 (carried unchanged through P18g-P18k): rank0_cv_z was
# RETIRED as a firing gate -- re-measured at n=20 healthy runs, healthy
# max (51.49) EXCEEDS the true-positive value (50.14); no threshold works
# for that statistic. rank0_outlier_rate is the validated replacement:
# n=19 healthy runs, healthy max rate 0.0052, true fault rate 0.1533.
# Threshold 0.03 sits ~5.8x above the healthy max and ~5.1x below the
# fault rate -- real margins from real measurements, not invented.
RANK0_OUTLIER_RATE_THRESH = 0.03

# --- agg_app_alltoall_ms (Option D, MoE AllToAll app-level timing, 2026-11) ---
# Independently calibrated per this task's own explicit instruction ("not
# borrowed from the existing z/mm thresholds without verification") -- NOT
# MEAN_Z_THRESH/MEAN_MM_THRESH above, which this new metric's real data
# never gets remotely close to (see below). Source: this project's own
# existing var/runs/item3_verbose_diag raw logs (MOE_FAULT_TARGET_RANK=4,
# 2s delay, re-analyzed directly from already-captured MOE-DISPATCH-TIMING
# lines before any of this round's code was written, then re-confirmed live
# once the real pipeline existed -- see DESIGN_NOTES Sec. 6.35 for both).
# Grand per-rank mean over the whole trial: z=31.24, mm=2.05x. Per-30-sample
# window (APPTIMING_WINDOW_SIZE, aggregator/node_aggregator_ref.py), the
# realistic per-poll granularity this check actually sees: 50/50 consecutive
# windows had z in [5.37, 25.19] (median 15.78) and mm in [1.62, 2.23]
# (median 2.09) -- i.e. MEAN_Z_THRESH=30.0 would almost NEVER fire on this
# signal (confirmed: the grand-mean z of 31.24 barely clears it once, no
# single window ever does), exactly the gap this task asked to check for
# rather than assume. Thresholds below sit with real margin BELOW the
# observed fault-trial minimum (not an upper-bound fit), to be confirmed
# safe against a real healthy-run noise floor in this round's own
# validation trials (Step 2) before being treated as final.
APPTIMING_Z_THRESH = 4.0
APPTIMING_MM_THRESH = 1.3

# --- MoE AllToAll proxy-thread peer-wait detection (S6.43, live) ---
# agg_proxy_peer_wait_mean_us (aggregator/node_aggregator_ref.py, the
# Inspector proxy-thread fix, DESIGN_NOTES Sec. 6.41) is a DIFFERENT
# signal from APPTIMING above (Option D/E's Python-level AllToAll call
# timing) -- its own noise floor was never going to match APPTIMING's,
# so it gets its own, independently-derived threshold rather than
# reusing APPTIMING_Z_THRESH/APPTIMING_MM_THRESH or MEAN_Z_THRESH/
# MEAN_MM_THRESH wholesale (same discipline that comment above already
# used). Grounded in this project's own real, already-measured numbers
# (staged validation + live-production-path re-validation, 2026-11):
# healthy noise band ~=1.1 peer-group z; 20ms/100ms fault z in [-8,-13]
# (clean separation); 1ms fault z~=-1.6 to -2.3 (marginal, explicitly
# NOT reliably detectable -- see README Known Limitations). The
# straggler shows up as the LOWEST recv-wait in its peer group (it
# posts its receive late, so the remote data is often already sitting
# in the network buffer by the time it looks -- the opposite direction
# from APPTIMING/MEAN's "worst = slowest"), so the check built on this
# compares peer_mean/worst in the inverted direction; PROXY_WAIT_MM_THRESH
# is a ratio >1 meaning "peers are this many times SLOWER than the
# outlier", not the other way around. 5.0/3.0 sit with real margin
# above the 1ms range's ~2.3 ceiling and the healthy ~1.1 noise floor,
# and well below the 20/100ms floor of ~8 -- deliberately NOT tuned to
# catch the already-disclosed-unreliable 1ms case.
# S6.44 K8s long-duration investigation: original 5.0 missed the training
# phase entirely on this cluster (z=4.44-4.49 during active training, below
# threshold). Root cause confirmed: peer_sd grows 1.74× as routing non-
# uniformity increases once training begins, diluting z from 6.35 to 4.44;
# the straggler is STILL anomalous (mm=22-25, ratio vs own early baseline
# 2.9x), but the z-score denominator grew faster than the numerator.
# 4.0 fires throughout all training phases (z=4.44-4.49 > 4.0) while
# maintaining 2.4-unit clearance above the healthy band max (z=1.59).
# Effect on Slurm: none — Slurm's strong-signal z≈10 >> 4.0 (still fires
# as before); Slurm's known late-job fade to z≈1.7 still doesn't fire
# regardless of this threshold (1.7 < 4.0), which is correct: a signal
# that has genuinely faded should not fire. Validated on K8s: 300-step
# fault fires throughout all training phases; healthy stays silent.
PROXY_WAIT_Z_THRESH = 4.0
PROXY_WAIT_MM_THRESH = 3.0
# Real bug found live (this session, during end-to-end validation): a
# bare instant query (one latest sample per series) is far noisier than
# the full-job weighted average moe_signal_check.py computes -- measured
# directly against the SAME live 100ms fault trial: z=1.10 off a bare
# instant snapshot vs z=8.87 off the full job. A lookback-windowed
# weighted average (via _query_export, same anti-miss tool _check_mean
# already uses) closes most of that gap without waiting for the whole
# job to finish. Measured directly across window sizes on that same
# live trial: 30s -> z=5.04 (barely clears PROXY_WAIT_Z_THRESH, no
# margin), 45s -> 5.29, 60s -> 5.96, 90s -> 7.04, 120s -> 7.49. 90s
# chosen: comfortable margin above both thresholds while still being
# genuinely live (a human running the offline script only ever sees
# this after the whole job ends).
PROXY_WAIT_LOOKBACK_S = 90.0

# --- Structural exclusions (mechanism, not rank numbers) ---
# EXCLUDE_ALWAYS/EXCLUDE_CV_EXTRA/EXCLUDE_OUTLIER_EXTRA in node_aggregator.py
# already exclude rank 3 (known-degraded GPU, cross-referenced dynamically
# against the live TFLOPS health-check, not hardcoded -- see
# health_exclusions.py) and rank 0 (master-process/rendezvous-coordinator
# overhead, P18b finding, re-confirmed independently in the P20c-prerequisite
# RAS investigation) from the peer-relative CV/outlier_count statistics.
# The alert layer inherits these unchanged -- it does not re-derive or
# widen them.
