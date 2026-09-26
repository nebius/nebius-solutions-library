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

# --- Structural exclusions (mechanism, not rank numbers) ---
# EXCLUDE_ALWAYS/EXCLUDE_CV_EXTRA/EXCLUDE_OUTLIER_EXTRA in node_aggregator.py
# already exclude rank 3 (known-degraded GPU, cross-referenced dynamically
# against the live TFLOPS health-check, not hardcoded -- see
# health_exclusions.py) and rank 0 (master-process/rendezvous-coordinator
# overhead, P18b finding, re-confirmed independently in the P20c-prerequisite
# RAS investigation) from the peer-relative CV/outlier_count statistics.
# The alert layer inherits these unchanged -- it does not re-derive or
# widen them.
