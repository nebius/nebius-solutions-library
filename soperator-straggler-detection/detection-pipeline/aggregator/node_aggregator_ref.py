#!/usr/bin/env python3
"""P21.5 -- communicator-generic rewrite of the P20d aggregator.

P21 (multi-communicator investigation) confirmed three real breaks, all
from the same root assumption -- exactly one communicator spans the whole
job, with a stable global rank numbering and workload-specific hardcoded
message-size/rank-count constants calibrated against it:

  1. handle_record() read header["rank"] as if it were a stable global
     identity and never looked at header["id"] (the real communicator
     identity) at all. Confirmed directly against real two-communicator
     data: the SAME physical GPU reported rank=0 under its TP
     communicator's records and rank=1 under its DP communicator's
     records, in the same dump file -- "rank" is communicator-LOCAL, not
     global, the moment more than one communicator exists.
  2. SCORED_BUCKETS filtered on two hardcoded exact byte counts
     (BUCKET_B/BUCKET_C) calibrated for one specific single-communicator
     workload. A different workload, or the same workload under a
     different parallelism strategy, produces different message sizes --
     confirmed directly: neither the TP job's TP-communicator sizes
     (25165824/12582912 bytes) nor its DP-communicator sizes (1179648/
     2362368/12306432/14668800 bytes) matched BUCKET_B/BUCKET_C at all,
     so every record from either communicator was silently dropped.
  3. (fixed in detection.py, not here -- see that file's own docstring)
     per_rank_series' hardcoded N_RANKS=16 full-coverage requirement.

This file's fix for (1) and (2):

  - Physical identity: metadata["pid"] -- present on every record, stable
    for the process's lifetime, and entirely independent of how many NCCL
    communicators that process happens to participate in (confirmed: it
    has nothing to do with communicator structure at all, unlike rank).
    State is now keyed by (comm_id, phys_id, bucket), not (rank, bucket).
  - Communicator/bucket structure is DISCOVERED, not assumed: a
    communicator's local membership on this node is "however many
    distinct phys_ids have been observed reporting under this comm_id so
    far" -- a set that only grows, never assumed complete or fixed at any
    specific count at startup, so a communicator that only activates
    partway through a job (or has any member count at all -- 2, 3, 5, 8,
    whatever) is handled the same way, generically.
  - Bucket calibration is per-communicator and data-driven: each newly-
    discovered comm_id accumulates a message-size histogram from its own
    real records; once enough samples exist (a generic "how much evidence
    before trusting the answer" threshold, not a workload-specific byte
    value), the set of "real, recurring" sizes for THAT communicator is
    derived as whichever sizes recur often enough to not be a one-off
    bootstrap/startup artifact -- zero hardcoded byte values anywhere.

Known, deliberately-not-carried-forward gap (see P21.5's own closeout
report for the full reasoning): the old EXCLUDE_ALWAYS/RANK0_PEERS
exclusions (rank 3's known permanent hardware defect, rank 0's known
master-process overhead) depended on "rank" being a stable, externally-
known global identity mapped to a specific physical GPU slot. Inspector's
own dump schema (header: id/rank/n_ranks/nnodes; metadata: hostname/pid/
timestamps) does not expose a "physical GPU slot index" field at all --
that mapping was always external knowledge (how the job was launched),
never something recoverable from the data alone. This fix does not
fabricate a shaky substitute for that mapping; the exclusion set is now
empty by default (EXCLUDE_ALWAYS/etc. below), a real, honestly-flagged
gap, not a silently-dropped feature.

Metric labels: `rank="{r}"` is replaced by two labels -- `comm="{comm_id}"`
and `member="{phys_id}"` -- since a bare rank number is no longer globally
meaningful once more than one communicator exists on this node.
"""
import argparse
import glob
import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import statistics as st
from collections import deque, defaultdict, Counter

sys.path.insert(0, "/root/P19a_metrics")
from promql_cv_verify import stat_cv as verified_stat_cv  # noqa: E402  (exact fidelity)

# P21.5 -- these two were imported from detection.py before (BUCKET_B/
# BUCKET_C, the exact hardcoded byte values this fix removes). Only the
# genuinely workload-independent window-size tuning constants remain
# sourced from there.
sys.path.insert(0, "/root/P18k_classifier")
from detection import CV_TRIM_EACH_SIDE, STAT_WINDOW_SIZE  # noqa: E402

CV_WINDOW = STAT_WINDOW_SIZE["cv"]
MEAN_WINDOW = STAT_WINDOW_SIZE["mean"]
TRIM = CV_TRIM_EACH_SIDE
PERSIST_WINDOW = 3
PERSIST_REQUIRED = 3
CV_Z_THRESH = 60.0
MEAN_Z_THRESH = 30.0
MEAN_MM_THRESH = 2.0
OUTLIER_COUNT_THRESH = 4
OUTLIER_COUNT_MM_THRESH = 2.0
OUTLIER_K = 3.0

# P21.5 -- known, honestly-flagged gap: see module docstring. Left as an
# empty, pluggable hook (not removed outright) so a real physical-GPU-slot
# mapping can be wired in later if one becomes available (e.g. a future
# Inspector schema version that exposes a device ordinal), without
# touching every call site again.
EXCLUDE_ALWAYS = frozenset()
EXCLUDE_CV_EXTRA = frozenset()
EXCLUDE_OUTLIER_EXTRA = frozenset()

JOB_ID_REFRESH_S = 60
DEFAULT_CLUSTER = "soperator"  # soperator-fluxcd values.yaml's own default clusterName

# P21.5 -- per-communicator bucket calibration. Generic tuning parameters
# (how much evidence before trusting the answer), not workload-specific
# byte values -- the same role STAT_WINDOW_SIZE already plays for window
# sizing. A message size must clear BOTH a minimum absolute occurrence
# count and a minimum fraction of the calibration sample to be treated as
# a real, recurring collective rather than a one-off bootstrap/startup
# artifact (a single early broadcast, a one-time bootstrap AllGather) --
# confirmed this exact pattern in real data (the TP job's own bootstrap
# AllGather appeared once, its two real recurring MLP-shard sizes
# thousands of times).
CALIB_MIN_TOTAL_SAMPLES = 200
CALIB_MIN_OCCURRENCES = 10
CALIB_MIN_FREQ_FRAC = 0.02

# P27-hotfix3 -- real, measured, time-based grace period after a bucket
# first becomes scored, before its mean-window fired decision is trusted
# (see bucket_scored_at_ts_us's own docstring at __init__ for the full
# finding). Real observed instability window, live-measured across 3
# independent healthy FSDP runs: fires occurred from t+80s to t+143s
# relative to job start, each tightly following that run's own bucket-
# scoring moment -- a real, reproducible ~63s window. 120s gives >1.9x
# real margin above that measured 63s ceiling, the same "real margin
# above a real measured ceiling" reasoning already used by this project's
# own HEARTBEAT_STALE_THRESH_S (>2x its own measured ~39s healthy
# ceiling) -- reused directly, not a new invented ratio.
BUCKET_MATURITY_GRACE_S = 120.0

# P22.1 (FSDP calibration-window fix) -- caps the TOTAL used in the
# frac-of-total term below (CALIB_MIN_FREQ_FRAC * effective_total), not a
# cap on counting or on how long a bucket can still qualify. Without
# this, a real but comparatively rarer collective type's own bar
# (0.02 * self.total) could keep rising forever as long as a dominant
# type keeps inflating self.total -- an ever-receding target it might
# never catch, even though it is genuinely real, recurring traffic.
# 5000 = 25x the existing CALIB_MIN_TOTAL_SAMPLES=200 floor -- reasoned
# from that existing anchor, not a new arbitrary number. At this
# ceiling the frac term maxes out at CALIB_MIN_FREQ_FRAC*5000=100
# occurrences; confirmed against this project's own real FSDP data
# (P22's fault-injection job) that even the rarest real collective type
# actually observed (AllReduce, ~5% of total traffic) would clear 100
# occurrences within roughly 2,000 total records -- comfortably inside
# this ceiling, and at the record cadences already measured (hundreds/
# sec), a matter of seconds, not an indefinite wait.
CALIB_MAX_TOTAL_SAMPLES = 5000

# P23 step 3 -- bucket-cardinality bound. Real, discovered live in the
# P23 MoE toy-layer session: msg_size_bytes was used as an EXACT bucket
# key everywhere, which is fine for DDP/TP/FSDP (structurally-fixed
# tensor/parameter shapes -> a small, fixed number of exact sizes) but
# breaks down for MoE's real, data-dependent AllToAll traffic, whose
# byte size is (routed token count) x (fixed per-token unit) -- a
# CONTINUOUSLY varying value with no natural ceiling on how many exact
# byte counts can occur. Confirmed directly: 7,062+ distinct (bucket,
# coll) pairs calibrated over a single ~40-minute real MoE run, still
# growing, driving real, unbounded-looking aggregator memory growth
# (159MB -> 414MB -> 549MB over 10/30/40 min) -- a genuinely new growth
# axis P22.5's all_vals bound never addressed (that fix bounded the
# SAMPLE COUNT per bucket, not the NUMBER of buckets).
#
# Fix: coarsen msg_size into log-scale bins (round to the nearest power
# of BUCKET_LOG_BASE) before using it as a bucket key anywhere, instead
# of capping/evicting tracked buckets. Chosen over an eviction/LRU
# policy because MoE's real sizes are close to unique-per-occurrence
# (driven by continuously-varying real token counts) -- an eviction cap
# would just churn: newly-appearing rare sizes would perpetually evict
# other rare ones before any of them accumulate enough occurrences to
# calibrate (CALIB_MIN_OCCURRENCES), permanently degrading detection
# fidelity for that whole traffic class. Log-scale coarsening instead
# collapses the continuously-varying REAL sizes that differ only by a
# few tokens' worth of bytes into the SAME bin, which is both what
# bounds cardinality and is the statistically correct thing to do (more
# real samples land in each bin, giving calibration and z-scores a
# larger, more robust sample instead of fragmenting it across thousands
# of near-identical one-off exact values).
#
# BUCKET_LOG_BASE = 2**(1/8): 8 bins per octave (~9.05% relative bin
# width), the same base-2-with-N-subdivisions-per-octave technique used
# by OpenTelemetry/DataDog exponential histograms for exactly this
# problem (bounded bin count over an unbounded dynamic range, fixed
# relative resolution) -- not invented for this project. Verified safe
# against every exact size this project has ever recorded for DDP/TP/
# FSDP (e.g. DDP's 28323840 vs 42980352, the closest real gap on record
# at ~1.52x) -- all comfortably wider than one bin, so none of those
# already-validated exact-size buckets merge under this scheme; see
# Step 1 of the P23 step-3 session for the direct verification. Over
# MoE's real observed range (0 up to ~25MB, smallest real per-token
# unit ~1536 bytes), this yields on the order of ~100 bins total,
# regardless of how many distinct exact byte values actually occur --
# log(25e6/1536) / log(2**(1/8)) ~= 102.
BUCKET_LOG_BASE = 2 ** (1.0 / 8)


def coarsen_msg_size(msg_size):
    """Maps a raw exact byte count to its log-scale bin's representative
    value. 0 (and, defensively, any non-positive value -- log is
    undefined there) maps to itself unchanged: a 0-byte transfer is real
    and common (e.g. an MoE peer routed zero tokens this round) and is
    already its own single, exact value -- no coarsening needed or
    possible. The bin's representative value (BUCKET_LOG_BASE **
    round(...)), not a bin index, is what's reported, so the exported
    "bucket" label stays a real, interpretable approximate byte count."""
    if msg_size <= 0:
        return msg_size
    exponent = round(math.log(msg_size, BUCKET_LOG_BASE))
    return int(round(BUCKET_LOG_BASE ** exponent))


# P23 (throughput-ratio self-calibration fix) -- real, structural
# limitation confirmed live across two sessions: agg_job_throughput_
# ratio_to_baseline (see maybe_check_job_throughput) establishes its
# "healthy" reference from THIS job's own early data. If a real fault is
# present from the job's very first collective (not something that
# starts mid-run), there is no within-job contrast to key off of at
# all -- the reference itself is contaminated, and the ratio reads ~1.0
# forever regardless of real severity. Confirmed directly: MoE's real
# job-wide NCCL_MAX_NCHANNELS=1 fault (2.5-3.4x real iteration-time
# slowdown, confirmed in train_worker-N.log) produced a ratio of
# 0.969-1.031 throughout. This is mathematically inherent to ANY purely
# within-job signal -- a rolling/trailing comparison has the exact same
# blind spot as a frozen one, since a constant-from-launch fault
# produces no internal contrast for either to detect. The only way to
# tell "this job's rate is abnormally low" is a reference EXTERNAL to
# the job's own data: history from other, different job runs.
#
# VictoriaMetrics (this pipeline's existing, already-persistent,
# already-cross-job store -- no new storage infrastructure needed) is
# used directly: each job, once its OWN bucket structure has stabilized,
# pushes its self-measured raw rate as a new agg_job_throughput_rate_ref
# sample, tagged with a coarse WORKLOAD SIGNATURE (see workload_
# signature() below). A later job of the same rough shape queries
# history for that signature and, if enough exists, uses the MEDIAN of
# multiple historical jobs' rates as its reference instead of its own
# (possibly-contaminated) self-measurement. A median across several
# independent historical jobs is deliberately robust to any ONE of them
# having itself been faulted-from-launch -- as long as faulted-from-
# launch runs aren't the majority of history for that signature, the
# median still reflects the real healthy rate. This is what closes the
# gap that a single frozen cross-job reference (contaminated once, wrong
# forever) would not.
#
# Cold start (a workload signature with no/insufficient history yet)
# necessarily falls back to self-calibration for THAT one job -- a real,
# disclosed, narrower residual gap (the very first job(s) of a brand-new
# workload shape), not the general case this fix targets.
THROUGHPUT_XJOB_MIN_HISTORY = 3   # same "3 consecutive/independent data points" precedent as PERSIST_WINDOW
THROUGHPUT_XJOB_LOOKBACK_S = 30 * 86400  # long enough to span realistic gaps between recurring runs

# P27.5 -- optional backstop only, not the primary mechanism (see
# maybe_check_job_throughput's own comment for the real fix, the
# relative-tolerance relaxation of the stability check itself). Scaled
# off PERSIST_WINDOW (already this file's own "how many checks is enough"
# constant) rather than a bare new literal: 10x the checks normally
# needed gives real, generous margin beyond what the relative-tolerance
# check above should ever need in practice, while still bounding the
# worst case for a workload whose bucket cardinality never settles even
# under relative tolerance.
THROUGHPUT_CEILING_CHECKS = 10 * PERSIST_WINDOW


def workload_signature(n_comms, coll_types, msg_size_bins=()):
    """Coarse, generic fingerprint for 'roughly this kind of workload on
    this host', derived purely from already-discovered structure -- the
    number of distinct communicators this job uses (n_comms -- this
    file's own existing len(self.comm_calib), already printed in its
    "done." summary), the real, discovered SET of collective types
    (coll_types, from comm_bucket_members' own bucket keys -- (msg_size,
    coll) tuples -- never a hardcoded strategy name), and (P26 fix) the
    real, discovered SET of coarse message sizes actually seen.

    Two real, live-confirmed properties drove the original choice of
    (n_comms, coll_types) alone (not msg-size-bucket-COUNT/denom, tried
    first):
      1. Composition alone is not enough -- DDP and TP are BOTH
         AllReduce-only, needing n_comms (DDP=1, TP=6) to separate them;
         separately, DDP and FSDP share a similar denom SCALE despite
         different real rates, needing composition (DDP: AllReduce
         only; FSDP: AllGather+ReduceScatter+AllReduce) to separate
         those instead. Neither input alone suffices; together every
         real shape tested in this project (DDP, TP, FSDP, MoE) is
         pairwise distinct: DDP=(1,{AllReduce}), FSDP=(1,{AllGather,
         AllReduce,ReduceScatter}), TP=(6,{AllReduce}), MoE=(1,
         {AllReduce,Recv,Send}).
      2. denom (live_denom, total active bucket-MEMBER series -- one
         entry per (comm,bucket,rank) once that bucket has been SCORED,
         i.e. cleared CALIB_MIN_OCCURRENCES) was tried first as the
         scale input and found live, in the MoE-network-fault scenario,
         to itself be fault-sensitive: a severe throughput fault slows
         the RATE new buckets reach SCORED status, so a job's OWN denom
         at the moment PERSIST_WINDOW-check stability is (correctly)
         detected can legitimately be much smaller under a severe fault
         than in a healthy run of the identical workload -- confirmed
         directly: the same real MoE+network-fault job's two nodes
         landed in DIFFERENT denom-based signature bins (2048 vs 512)
         despite being the identical workload, one correctly finding
         its cross-job history and one silently missing it.

    P26 fix -- (n_comms, coll_types) alone is provably too coarse: a
    genuinely new, second workload (ResNet) landed on the EXACT same
    signature as nanoGPT ("1:AllReduce" -- both single-comm, AllReduce-
    only DDP) despite having a real healthy throughput ~3.15x apart
    (873/sec vs 276/sec, confirmed live), so ResNet's cross-job lookup
    silently pulled nanoGPT's history and produced a false CONFIRMED/
    PAGE uniform_slowdown on a genuinely healthy run. Composition and
    comm-count don't capture SCALE at all.

    msg_size_bins closes this WITHOUT reintroducing denom's fault-
    sensitivity, because it is fundamentally a different kind of
    quantity: not a COUNT that accumulates over real volume/time (like
    denom), but the SET of distinct coarse message sizes ever seen --
    i.e. built from CommCalibration.counts.keys() (msg_size component),
    which is incremented unconditionally on every single record from
    the very FIRST occurrence of each (msg_size, coll) pair, with no
    10-occurrence gate at all (unlike calib.scored, which IS gated and
    IS what denom counted). A model's distinct real tensor/gradient
    sizes are a structural property of its architecture -- every one of
    them is produced within the model's own first forward+backward pass
    regardless of how slowly or quickly that pass executes, so a fault
    that slows EXEC TIME (clock-lock, network contention, etc.) does not
    change WHICH sizes get seen or delay when they're first seen -- only
    denom's SCORING of them takes real accumulated volume. By the time
    PERSIST_WINDOW stability on denom is reached (today's existing
    gating point for calling this function), every real message size
    has already been seen at least once, fault or no fault -- confirmed
    by construction, not merely assumed: counts[key] is incremented
    before any threshold check even runs (see CommCalibration.observe).
    Values are already coarsened (msg_size is coarsen_msg_size()'d
    before it ever reaches calib.observe -- see handle_record), so no
    re-coarsening is needed here."""
    composition = "+".join(sorted(set(coll_types)))
    sizes = "+".join(str(s) for s in sorted(set(msg_size_bins)))
    return f"{n_comms}:{composition}:{sizes}"


HEARTBEAT_INTERVAL_S = 10.0
THROUGHPUT_CHECK_INTERVAL_S = 10.0


def stat_outlier_count(vals, k=OUTLIER_K):
    if not vals:
        return 0
    med = st.median(vals)
    thresh = med / k
    return sum(1 for v in vals if v < thresh)


def maxmed(worst_val, peer_vals):
    med = st.median(peer_vals)
    return worst_val / med if med else float("inf")


class RankBucketState:
    def __init__(self):
        self.mean_unconsumed = []
        self.cv_unconsumed = []
        self.all_vals = []
        self.mean_history = None
        self.cv_history = deque(maxlen=PERSIST_WINDOW)
        self.persist_hist = deque(maxlen=PERSIST_WINDOW)
        self.rate_samples = []


class CommCalibration:
    """P21.5 -- one per discovered comm_id. Accumulates a histogram and
    decides which (msg_size, coll) buckets are "scored" (real, recurring)
    vs. still-unproven. Before a given bucket is scored, its records are
    counted here but not yet fed into the live windowed statistics -- a
    brief, per-bucket warm-up cost, consistent with this project's
    established practice of a calibration phase before real detection
    begins.

    P22-prereq (collective-type separation) -- the histogram key was
    msg_size alone, silently pooling any two collective types that
    happen to share a message size on the same communicator. Fixed by
    keying on (msg_size, coll) instead -- discovered from the data
    (coll_perf.coll, already present in every record), never a
    hardcoded collective-type name.

    P22.1 (FSDP calibration-window fix) -- that fix's own scoring was
    still a single, one-time freeze gated on self.total reaching
    CALIB_MIN_TOTAL_SAMPLES, regardless of which collective types had
    actually appeared by then. Confirmed via real FSDP fault-injection
    data (P22's own test) that this silently, PERMANENTLY excludes a
    real, substantial collective type if it simply hasn't occurred yet
    at that point -- forward's AllGathers always precede backward's
    ReduceScatters within a training step, so a fresh job's first 200
    records can be entirely AllGather, freezing ReduceScatter out
    forever and hiding a real, strong (~58% exec-time drop) fault signal
    completely.

    Fixed by making scoring fully per-bucket and open-ended, not a
    single global freeze: self.scored is now a set that only ever
    GROWS, checked and updated on every observe() call. Each (msg_size,
    coll) bucket is judged purely on ITS OWN occurrence count against
    the SAME threshold formula this file already used (reused exactly,
    not reinvented -- see CALIB_MAX_TOTAL_SAMPLES's own comment for the
    one new, explicitly-justified constant this required), independent
    of whether any other bucket has cleared it yet. A bucket that
    hasn't cleared it yet is honestly "not yet calibrated" -- counted,
    not fed into live stats -- never force-included with too little
    evidence, and never permanently excluded either, since counting and
    threshold-checking continue for the rest of the communicator's
    life, not just during a fixed opening window."""
    def __init__(self):
        self.counts = Counter()
        self.total = 0
        self.scored = set()  # (msg_size, coll) tuples; grows over time, never frozen, never reset

    def observe(self, msg_size, coll):
        """Returns the (msg_size, coll) key if it just newly qualified
        this call, else None -- lets the caller log a per-bucket
        graduation event, the per-bucket analog of the old one-time
        "calibrated comm=..." print."""
        key = (msg_size, coll)
        self.counts[key] += 1
        self.total += 1
        if key in self.scored:
            return None
        effective_total = min(self.total, CALIB_MAX_TOTAL_SAMPLES)
        thresh = max(CALIB_MIN_OCCURRENCES, CALIB_MIN_FREQ_FRAC * effective_total)
        if self.counts[key] >= thresh:
            self.scored.add(key)
            return key
        return None


def bucket_labels(bucket):
    """P22-prereq -- bucket is now (msg_size, coll); render as two
    separate PromQL labels (bucket=msg_size, coll=coll_type) rather than
    embedding coll inside the existing bucket label, so every existing
    query/dashboard that filters on bucket="<size>" keeps working
    unchanged -- coll is purely additive."""
    msg_size, coll = bucket
    return f'bucket="{msg_size}",coll="{coll}"'


class NodeAggregator:
    def __init__(self, dump_dir, hostname, vm_url, cluster=DEFAULT_CLUSTER, slurm_job_id=None):
        self.dump_dir = dump_dir
        self.hostname = hostname
        self.vm_url = vm_url
        self.cluster = cluster
        self._explicit_job_id = slurm_job_id
        self.slurm_job_id = slurm_job_id or ""
        self._job_id_checked_at = 0
        # P21.5 -- state keyed by (comm_id, phys_id, bucket) instead of
        # (rank, bucket). comm_id = header["id"], phys_id = metadata["pid"]
        # (see module docstring for why pid, not rank).
        self.state = defaultdict(RankBucketState)
        # Per-communicator calibration, discovered dynamically -- no
        # assumption anywhere about how many communicators exist.
        self.comm_calib = defaultdict(CommCalibration)
        # (comm_id, bucket) -> set of phys_ids observed so far. Grows only;
        # never assumed complete at any fixed count. A communicator's
        # "local coverage" for window-closing purposes is always "every
        # phys_id discovered so far for this (comm_id, bucket)", not a
        # pre-known number.
        self.comm_bucket_members = defaultdict(set)
        # P27-hotfix3 -- real scoring-moment timestamp per (comm,bucket),
        # real grace period for a bucket that JUST became scored (see
        # score_mean_window's own use of this, and BUCKET_MATURITY_GRACE_S's
        # own comment for the full finding). A freshly-scored bucket's
        # first real window(s) draw from very few real post-scoring
        # samples (calib.scored gates ALL prior samples out entirely --
        # see the "if bucket not in calib.scored: return" line above --
        # so a bucket's own real sample history starts at zero the moment
        # it becomes scored, regardless of how long the comm itself has
        # been running). Confirmed live this session, real and
        # reproducible: ~47x-over-documented-tolerance PROBABLE-tier noise
        # on FSDP, ALL of it concentrated in a real, measured ~63s window
        # immediately after bucket=4/bucket=32768 each crossed their own
        # real scoring threshold (real fires at t+80s to t+143s relative
        # to job start, across 3 independent healthy runs; zero fires
        # before or after that window in any of them) -- never genuine
        # steady-state noise. An occurrence-count-based grace (window-
        # close counter) was tried first and confirmed the mechanism (real,
        # monotonic 12->9->5 fires/run as the multiplier grew) but never
        # cleanly separated real time from real occurrence-count/window-
        # size (100 samples/window) without more blind tuning -- time
        # itself, from the real scoring-moment timestamp already recorded
        # here, is the more principled, self-explaining basis, since the
        # instability is itself a real-TIME phenomenon (early jitter
        # settling), not fundamentally a sample-count one.
        self.bucket_scored_at_ts_us = {}
        # P27-hotfix4 -- (comm_id, phys_id) -> (comm-local rank, n_ranks),
        # see handle_record's own comment for the real distinction from
        # P21.5's already-documented "rank is not a stable global
        # identity" finding -- used only as a same-comm-shape role label.
        self.phys_comm_role = {}
        self.phys_gpu_slot = {}  # P21.7 -- phys_id -> gpu_slot_index, re-pushed on the heartbeat cadence (see maybe_heartbeat)
        self.file_offsets = {}
        self.rate_logged = set()
        self.push_buf = []
        self.n_records_seen = 0
        self.n_pushes = 0
        self.n_mean_windows = 0
        self.n_cv_windows = 0
        self._throughput_last_check_wall = time.time()
        self._throughput_last_records = 0
        self._throughput_last_ts_us = None  # P22.5 -- dump-time anchor, see maybe_check_job_throughput
        self._throughput_rate_ref = None  # P23 step 3 -- this job's self-calibrated baseline raw rate, see maybe_check_job_throughput
        self._throughput_last_live_denom = None  # P23 step 3 -- previous check's live bucket-count, for stability detection
        self._throughput_denom_stable_checks = 0  # P23 step 3 -- consecutive identical-live-denom checks so far
        self._throughput_total_checks = 0  # P27.5 -- total checks since throughput-checking began, for the bounded-ceiling backstop below
        self.n_push_failures = 0
        self.consecutive_push_failures = 0
        self._last_heartbeat_wall = 0.0

    def base_labels(self):
        return f'hostname="{self.hostname}",cluster="{self.cluster}",slurm_job_id="{self.slurm_job_id}"'

    def query_throughput_history(self, sig):
        """P23 (throughput-ratio self-calibration fix) -- reads (not just
        writes) VM: real historical agg_job_throughput_rate_ref samples
        for this hostname+signature, from OTHER (different) slurm_job_ids,
        within THROUGHPUT_XJOB_LOOKBACK_S. last_over_time(...) rather than
        a plain instant query -- a plain instant query only sees samples
        within VM's ~5min default staleness window, which a job from
        hours/days ago will never satisfy; last_over_time with an
        explicit, long lookback range correctly finds each historical
        series' last real value regardless of how long ago it was
        pushed. Returns a list of real historical rate values (possibly
        empty); never raises -- a query failure here must not block this
        job's own detection, so it degrades to "no history" (self-
        calibration fallback), same as a real "no history yet" case.

        P27.6 -- exact-string sig matching, confirmed live, is too
        strict: the message-size-set component records EVERY distinct
        size seen by stabilization time, and a genuinely slower real run
        (a real fault, or just ordinary variance) can reach that point
        having observed one fewer rare size than a healthy reference run
        did -- confirmed directly via MoE netfault, whose own sig
        differed from 3 real healthy runs' sig by exactly one rare entry
        (message size 19951585), falling back to self-calibration (blind
        to a fault present since launch -- a separate, already-disclosed
        gap) purely because of this string-exactness, not because the
        workload was actually different.

        Fixed by decoupling the two roles workload_signature's sig
        string used to serve at once. n_comms and composition -- this
        file's own docstring already established these as the real
        STRUCTURAL discriminators (different comm counts or different
        collective-type sets mean a genuinely different communication
        pattern; confirmed live: DDP=(1,{AllReduce}), FSDP=(1,{AllGather,
        AllReduce,ReduceScatter}), TP=(6,{AllReduce}), MoE=(1,{AllReduce,
        Recv,Send}) are pairwise distinct on this alone) -- are still
        matched exactly, no tolerance, so these can never blur together
        regardless of what follows. The message-size SET (the SCALE
        discriminator, per that same docstring) is now compared by
        Jaccard distance (symmetric difference over union) instead of
        string equality, tolerating up to CALIB_MIN_FREQ_FRAC (this
        file's own existing "how large a deviation is significant"
        threshold, reused rather than a new invented number) of the
        union differing. Two genuinely different workloads' real tensor/
        gradient sizes are structurally almost entirely disjoint sets
        (confirmed by this file's own earlier ResNet-vs-nanoGPT
        finding), so their Jaccard distance sits far above this floor --
        the tolerance only ever absorbs a SAME workload's own natural
        run-to-run variability in which of its rarest sizes had appeared
        by stabilization time, never a real cross-workload collision.

        This requires broadening the VM query from an exact sig= label
        match to all of this hostname's historical entries, then
        filtering in Python -- the only way to evaluate a tolerant
        comparison at all, since VM's label matching is exact-string
        only."""
        try:
            base = self.vm_url.rsplit("/api/v1/", 1)[0]
            my_n_comms, my_composition, my_sizes_str = sig.split(":", 2)
            my_sizes = set(my_sizes_str.split("+")) if my_sizes_str else set()
            promql = (f'last_over_time(agg_job_throughput_rate_ref{{hostname="{self.hostname}"}}'
                      f'[{int(THROUGHPUT_XJOB_LOOKBACK_S)}s])')
            qs = urllib.parse.urlencode({"query": promql})
            req = urllib.request.Request(f"{base}/api/v1/query?{qs}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.load(resp)
            values = []
            for row in d.get("data", {}).get("result", []):
                m = row.get("metric", {})
                if m.get("slurm_job_id") == self.slurm_job_id:
                    continue  # exclude this same job's own (not-yet-pushed anyway, but defensive)
                cand_sig = m.get("sig", "")
                try:
                    cand_n_comms, cand_composition, cand_sizes_str = cand_sig.split(":", 2)
                except ValueError:
                    continue  # malformed/missing sig label -- skip, don't guess
                if cand_n_comms != my_n_comms or cand_composition != my_composition:
                    continue  # structural mismatch -- never tolerated, regardless of sizes
                cand_sizes = set(cand_sizes_str.split("+")) if cand_sizes_str else set()
                union = my_sizes | cand_sizes
                jaccard_dist = (len(my_sizes ^ cand_sizes) / len(union)) if union else 0.0
                if jaccard_dist > CALIB_MIN_FREQ_FRAC:
                    continue  # sizes differ by more than the tolerated fraction -- real mismatch
                try:
                    values.append(float(row["value"][1]))
                except Exception:
                    continue
            return values
        except Exception as e:
            print(f"[{self.hostname}] throughput history query failed (falling back to "
                  f"self-calibration): {e}", file=sys.stderr)
            return []

    def refresh_job_id(self):
        if self._explicit_job_id:
            return
        now = time.time()
        if now - self._job_id_checked_at < JOB_ID_REFRESH_S:
            return
        self._job_id_checked_at = now
        try:
            out = subprocess.run(
                ["squeue", "-w", self.hostname, "-h", "-o", "%i", "--states=R"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:
                print(f"[{self.hostname}] squeue exited {out.returncode}: {out.stderr.strip()}", file=sys.stderr)
                self.slurm_job_id = "unknown"
                return
            job_ids = [l.strip() for l in out.stdout.splitlines() if l.strip()]
            self.slurm_job_id = job_ids[0] if job_ids else ""
        except Exception as e:
            print(f"[{self.hostname}] squeue lookup failed: {e}", file=sys.stderr)
            self.slurm_job_id = "unknown"

    # P22.5 (aggregator scalability fix) -- how many lines to process
    # between deadline checks. Checking time.time() every single line
    # would be wasteful; checking only once per poll_files() call (the
    # old behavior) is exactly the bug -- a single call processing a
    # large backlog could run for minutes with no opportunity for run()'s
    # own duration check to ever see it. 2000 is a small, cheap interval
    # relative to real per-record processing cost, giving --duration a
    # bounded, small overshoot (worst case: the time to process ~2000
    # records) regardless of total backlog size.
    DEADLINE_CHECK_EVERY = 2000

    def poll_files(self, deadline=None):
        """P22.5 -- now accepts an optional wall-clock deadline and can
        return WITHOUT having drained every already-read byte, if that
        deadline is hit mid-file. This is the fix for the confirmed real
        bug where --duration stopped being honored: the old version read
        the ENTIRE new chunk of a file in one f.read() and processed every
        line before ever returning, so a single call facing a large
        backlog (see this file's other P22.5 comments for how the
        backlog itself formed) could run far longer than run()'s own
        `while time.time() < t_end` loop could ever notice -- that check
        only re-fires AFTER poll_files() returns.

        Opened in binary mode ('rb') specifically so a partial-progress
        offset can be computed and committed safely: text-mode file
        objects' tell()/seek() use an opaque, encoding-aware cookie that
        is only guaranteed valid at positions tell() itself returned, not
        at an arbitrary byte count reconstructed by summing consumed line
        lengths. In binary mode, byte offsets are unambiguous, so
        stopping partway through an already-read chunk and committing
        offset + (bytes actually consumed so far) is exactly correct --
        the remainder is simply picked up on the NEXT poll_files() call,
        not lost and not reprocessed."""
        self.refresh_job_id()
        for fp in sorted(glob.glob(f"{self.dump_dir}/*.log")):
            offset = self.file_offsets.get(fp, 0)
            try:
                with open(fp, "rb") as f:
                    f.seek(offset)
                    new_data = f.read()
            except FileNotFoundError:
                continue
            consumed = 0
            since_check = 0
            for raw_line in new_data.splitlines(keepends=True):
                consumed += len(raw_line)
                line = raw_line.strip()
                if line:
                    try:
                        d = json.loads(line)
                    except Exception:
                        d = None
                    if d is not None:
                        self.handle_record(d)
                since_check += 1
                if deadline is not None and since_check >= self.DEADLINE_CHECK_EVERY:
                    since_check = 0
                    if time.time() >= deadline:
                        break
            self.file_offsets[fp] = offset + consumed

    def handle_record(self, d):
        h = d["header"]
        cp = d["coll_perf"]
        m = d["metadata"]
        comm_id = h["id"]
        phys_id = m["pid"]
        # P27-hotfix4 -- real, comm-LOCAL role (h["rank"], 0..n_ranks-1),
        # used ONLY as a per-comm-shape role label for cross-JOB baseline
        # comparison (see agg_member_role_ref below) -- deliberately NOT
        # the same mistake P21.5's own docstring above warns against
        # (treating comm-local rank as a stable GLOBAL identity across
        # DIFFERENT comms on the same host). This is comparing the SAME
        # comm shape's role ACROSS separate job runs of the same
        # workload, a real, narrower, valid use distinct from that.
        self.phys_comm_role[(comm_id, phys_id)] = (h["rank"], h["n_ranks"])
        # P23 step 3 -- coarsened ONCE, here, at the single point the raw
        # exact byte count enters the pipeline; every downstream use of
        # "msg_size" (calibration, bucket keys, comm_bucket_members, the
        # exported bucket="..." label) sees only the coarsened value, so
        # cardinality is bounded everywhere a bucket key is created or
        # published, not just in one internal dict. See coarsen_msg_size's
        # own docstring/BUCKET_LOG_BASE comment for why and the safety
        # check against existing DDP/TP/FSDP exact sizes.
        msg_size = coarsen_msg_size(cp["coll_msg_size_bytes"])
        # P22-prereq -- already present in every record (Step 1 of the
        # collective-type-separation investigation confirmed this
        # directly); just never read anywhere in this file before now.
        coll = cp["coll"]

        # P21.7 -- gpu_slot_index (added to Inspector's own dump schema
        # this session) is a real physical-GPU identity, independent of
        # calibration/bucket status. Cached here on first sight of this
        # phys_id; the actual VM PUSH happens from maybe_heartbeat() on
        # its own periodic cadence, using CURRENT wall time -- NOT here,
        # using this record's own dump_timestamp_us. A real bug caught
        # directly this session: dump files accumulate from whenever the
        # job first started, and a freshly (re)started aggregator re-reads
        # a dump file from offset 0, so "the first record this process
        # ever sees" can be many minutes old by wall-clock time -- pushing
        # with THAT historical timestamp meant the sample was already
        # past VM's own staleness window by the time anything queried it,
        # even though the identity fact itself is still true. Re-pushing
        # on the heartbeat's own cadence (like agg_aggregator_heartbeat
        # itself) keeps it genuinely queryable at any point during a long
        # job, not just briefly near job start. -1 (capture failed at the
        # plugin level) is cached and re-pushed as-is, not silently
        # hidden, so a consumer can tell "unknown" apart from a real slot.
        if phys_id not in self.phys_gpu_slot:
            self.phys_gpu_slot[phys_id] = m.get("gpu_slot_index", -1)

        calib = self.comm_calib[comm_id]
        bucket = (msg_size, coll)
        # P22.1 -- always observe (per-bucket occurrence counting never
        # stops), then check THIS record's own bucket against the
        # current scored set -- no global "still calibrating" state for
        # the whole communicator any more, since that was exactly the
        # bug (one bucket's evidence gated every other bucket's fate).
        newly_scored = calib.observe(msg_size, coll)
        if newly_scored is not None:
            print(f"[{self.hostname}] calibrated new bucket for comm={comm_id}: "
                  f"bucket={newly_scored} after {calib.counts[newly_scored]} occurrences "
                  f"(comm total samples so far: {calib.total}, "
                  f"now-scored buckets: {sorted(calib.scored)}, "
                  f"full distribution: {dict(calib.counts)})", flush=True)
            # P27-hotfix3 -- real scoring-moment timestamp, real dump_timestamp_us
            # (not wall-clock query time), for score_mean_window's own grace-
            # period gate below. See BUCKET_MATURITY_GRACE_S's own comment for
            # the full finding this closes.
            self.bucket_scored_at_ts_us[(comm_id, newly_scored)] = m["dump_timestamp_us"]
        if bucket not in calib.scored:
            return

        self.n_records_seen += 1
        ts_us = m["dump_timestamp_us"]
        val = cp["coll_exec_time_us"]
        key = (comm_id, phys_id, bucket)
        s = self.state[key]
        s.mean_unconsumed.append(val)
        s.cv_unconsumed.append(val)
        s.all_vals.append(val)
        # P22.5 (aggregator scalability fix) -- all_vals was NEVER
        # trimmed before this fix, growing by one entry per record for
        # the entire lifetime of the process. Confirmed as the real root
        # cause of P22.4's failure under FSDP's much higher event volume:
        # (a) unbounded memory growth (~30MB -> 16.3GB RSS over ~50
        # minutes, one float per record, per (comm,phys_id,bucket) key,
        # forever), and (b) check_outlier_count's stat_outlier_count()
        # recomputes st.median(vals) over this SAME ever-growing list on
        # EVERY CV-window close -- a classic unbounded-accumulator +
        # O(n)-per-append pattern that compounds into O(n^2) total cost
        # as the job runs longer, making poll_files() calls progressively
        # slower, which is also why --duration stopped being honored (see
        # run()'s own comment). Bounded to the same 500-entry rolling
        # cap this file already uses for rate_samples (the exact existing
        # precedent, not a new invented number) -- more than enough
        # recent history for a meaningful outlier-rate estimate, and
        # caps both the memory and the per-call cost at a fixed size
        # regardless of how long the job runs.
        if len(s.all_vals) > 500:
            s.all_vals = s.all_vals[-500:]
        s.rate_samples.append(ts_us)
        if len(s.rate_samples) > 500:
            s.rate_samples = s.rate_samples[-500:]
        self.comm_bucket_members[(comm_id, bucket)].add(phys_id)
        self.maybe_log_rate(comm_id, bucket, s)

        self.push_buf.append((f'agg_samples_seen{{comm="{comm_id}",member="{phys_id}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               len(s.all_vals), ts_us // 1000))

        self.drain_windows(comm_id, bucket, ts_us)

    def maybe_log_rate(self, comm_id, bucket, s):
        key = (comm_id, bucket)
        if key in self.rate_logged or len(s.rate_samples) < 60:
            return
        span_s = (s.rate_samples[-1] - s.rate_samples[0]) / 1e6
        if span_s <= 0:
            return
        rate = (len(s.rate_samples) - 1) / span_s
        self.rate_logged.add(key)
        print(f"[{self.hostname}] measured cadence comm={comm_id} bucket={bucket[0]} coll={bucket[1]} rate={rate:.2f}/sec "
              f"(informational only -- window-closing is sample-count-driven, not timer-gated)",
              flush=True)

    def close_windows(self, comm_id, bucket, window_size, unconsumed_attr):
        # P21.5 -- "everyone discovered so far for this (comm,bucket)", not
        # a fixed a-priori set. A communicator with 2 members closes
        # windows once both have enough samples; one with 8 (or 3, or 47)
        # does the same, generically -- whatever's actually been observed.
        members = sorted(self.comm_bucket_members[(comm_id, bucket)])
        windows = []
        while True:
            min_count = min(len(getattr(self.state[(comm_id, p, bucket)], unconsumed_attr)) for p in members)
            if min_count < window_size:
                break
            w = {}
            for p in members:
                st_ = self.state[(comm_id, p, bucket)]
                q = getattr(st_, unconsumed_attr)
                w[p] = q[:window_size]
                setattr(st_, unconsumed_attr, q[window_size:])
            windows.append(w)
        return windows

    def drain_windows(self, comm_id, bucket, ts_us):
        for w in self.close_windows(comm_id, bucket, MEAN_WINDOW, "mean_unconsumed"):
            self.n_mean_windows += 1
            self.score_mean_window(comm_id, bucket, w, ts_us)
        for w in self.close_windows(comm_id, bucket, CV_WINDOW, "cv_unconsumed"):
            self.n_cv_windows += 1
            self.score_cv_window(comm_id, bucket, w, ts_us)
        self.push_buf.append((f'agg_records_seen_total{{{self.base_labels()}}}', self.n_records_seen, ts_us // 1000))
        self.push_buf.append((f'agg_mean_windows_total{{{self.base_labels()}}}', self.n_mean_windows, ts_us // 1000))
        self.push_buf.append((f'agg_cv_windows_total{{{self.base_labels()}}}', self.n_cv_windows, ts_us // 1000))
        self.push_buf.append((f'agg_pushes_total{{{self.base_labels()}}}', self.n_pushes, ts_us // 1000))
        self.maybe_check_job_throughput(ts_us)

    def maybe_check_job_throughput(self, ts_us):
        """P21.5 -- denominator is now the real, currently-discovered
        member count summed across every (comm,bucket) this node has ever
        scored, not a hardcoded "8 ranks * 2 buckets". Grows/shrinks
        automatically as communicators and their memberships are
        discovered, generically, for any communicator count or shape.

        P22.5 (aggregator scalability fix) -- the RATE's own time
        denominator changed from WALL-CLOCK elapsed time to DUMP-
        TIMESTAMP elapsed time (real training-time span covered by the
        records processed), not because of a units preference but
        because this is a real, confirmed correctness bug: with a
        wall-clock denominator, d_records/dt silently conflates "how fast
        is the JOB producing records" with "how fast can THIS AGGREGATOR
        consume them" -- indistinguishable the moment the aggregator
        itself falls behind (backlog, GC pause, a future volume spike),
        even though the job's real behavior hasn't changed at all.
        Confirmed exactly this in P22.4: this metric read low enough to
        fire 2 real, false CONFIRMED/PAGE "uniform_slowdown" alerts while
        the training loop's own per-iteration timing (train_worker-N.log)
        was provably steady the whole time -- the aggregator's wall-clock
        pace had degraded (from the now-fixed all_vals bug), the job's
        real pace had not. Using (ts_us - last_ts_us) instead measures
        real elapsed TRAINING time directly from the data's own
        timestamps, entirely decoupled from how long the aggregator
        itself took, in wall-clock terms, to get through it -- a real
        job slowdown still shows up correctly (dump timestamps are
        themselves real wall-clock captures at record-write time, so
        fewer real records over a real training-time span still means a
        real, lower rate), but an aggregator-side processing lag no
        longer can manufacture a false one. The wall-clock check below
        is now purely a "don't recompute this too often" throttle, not
        part of the rate's own math."""
        now = time.time()
        if now - self._throughput_last_check_wall < THROUGHPUT_CHECK_INTERVAL_S:
            return
        self._throughput_last_check_wall = now
        d_records = self.n_records_seen - self._throughput_last_records
        self._throughput_last_records = self.n_records_seen
        if self._throughput_last_ts_us is None:
            self._throughput_last_ts_us = ts_us
            return  # first call: no real dump-time span measured yet
        dt = (ts_us - self._throughput_last_ts_us) / 1e6
        self._throughput_last_ts_us = ts_us
        if dt <= 0:
            return
        raw_rate = d_records / dt
        live_denom = sum(len(members) for members in self.comm_bucket_members.values())
        # P23 step 3 -- three attempts were needed here; the first two are
        # kept in spirit in what follows because each fixed a real, but
        # incomplete, part of the problem, live-confirmed via the actual
        # VictoriaMetrics time series each time rather than assumed fixed:
        #
        # Attempt 1 (freeze this metric's own denom once total records
        # crossed CALIB_MAX_TOTAL_SAMPLES): wrong dimension -- MoE's high
        # raw event rate crosses any record-count threshold in under 2
        # real seconds, long before comm_bucket_members (which grows by
        # ~one entry per newly-discovered bucket, and MoE's real, data-
        # dependent routing keeps discovering new buckets over a real
        # stretch of wall-clock time, not instantly) has actually settled.
        # Volume and discovery-time are different dimensions.
        #
        # Attempt 2 (freeze the denom once it's been observed stable --
        # byte-for-byte unchanged -- across PERSIST_WINDOW consecutive
        # checks, and don't report anything before that): fixed the
        # timing of the freeze correctly, but real VM data after this fix
        # showed the metric, once it started reporting, was STUCK at a
        # low, non-recovering value indefinitely (~2.4-2.9/rank/bucket/sec,
        # flat for the entire observed window) -- not a transient startup
        # dip at all. The real, deeper problem: dividing the SAME raw
        # event rate across ~202 real buckets instead of DDP/TP/FSDP's
        # ~5-88 makes the per-bucket rate structurally, permanently lower
        # for a completely healthy job, for a purely arithmetic reason
        # (more buckets sharing the same total). CALIBRATED_RATE_PER_SEC
        # =50.0 (coverage_guard.py) was measured against small-bucket-
        # count shapes and is not a valid floor for ANY shape with
        # structurally more buckets, no matter how stable that bucket
        # count is once reached.
        #
        # Fixed properly (attempt 3) by abandoning the per-bucket
        # normalization for THIS check entirely and self-calibrating
        # instead: once the bucket count (still tracked via live_denom,
        # now used only as a readiness SIGNAL, not a divisor) has been
        # stable for PERSIST_WINDOW consecutive checks, capture THIS
        # job's own raw_rate at that moment as its baseline, then report
        # later checks as a plain RATIO to that self-established baseline
        # (1.0 = healthy, 0.5 = half of this job's own normal rate).
        # coverage_guard.ABSOLUTE_RATE_FLOOR_FRAC (already existing,
        # reused unchanged) becomes directly the ratio floor -- no
        # cross-job, cross-shape constant is needed at all for this
        # check, since the comparison is now entirely relative to the
        # SAME job's own measured healthy behavior. Provably inert for
        # DDP/TP/FSDP: their bucket count already stabilizes within the
        # first few checks (as it always did), so the baseline is
        # established just as early as this metric previously became
        # available, and a genuine 2x+ real slowdown is caught exactly
        # the same way (ratio crossing 0.5), just measured against this
        # job's own real rate instead of an unrelated historical constant.
        #
        # Attempt 3's real, disclosed gap (found across two later
        # sessions): self-calibration is mathematically blind to a fault
        # present since this job's OWN first collective -- there is no
        # within-job contrast to key off of, so the "baseline" IS the
        # fault. Confirmed live: MoE's real, job-wide NCCL_MAX_NCHANNELS=1
        # fault (present from launch, 2.5-3.4x real iteration-time
        # slowdown) produced a ratio pinned at 0.969-1.031 throughout.
        #
        # Fixed (attempt 4) by preferring a CROSS-JOB reference over
        # self-calibration whenever enough real history exists: see
        # workload_signature() and query_throughput_history() above, and
        # the branch immediately below. Self-calibration is now only the
        # cold-start fallback for a workload signature with no/
        # insufficient history yet -- not the default for every job.
        if self._throughput_rate_ref is None:
            self._throughput_total_checks += 1
            # P27.5 -- attempt 4's exact-equality stability check (live_denom
            # == previous check's live_denom, byte-for-byte) is structurally
            # unreachable for a workload whose real bucket cardinality keeps
            # slowly growing indefinitely -- confirmed live via MoE: a real
            # 700s seeding run (far past this metric's original ~30s design
            # target) still had NEW buckets crossing their occurrence
            # threshold at the 10-minute mark, so live_denom never held
            # byte-identical for 3 consecutive checks even once, and
            # workload_signature() was therefore never even called (confirmed
            # directly: zero agg_job_workload_sig_info samples in VM for all
            # three MoE runs this session, healthy/netfault/seeding alike).
            #
            # Fixed by relaxing exact equality to a small RELATIVE tolerance
            # -- reusing CALIB_MIN_FREQ_FRAC (this file's own existing "what
            # fraction of real activity is significant enough to count"
            # threshold, already used for per-bucket qualification) rather
            # than inventing a second, unrelated fuzziness constant. A large,
            # mostly-settled denominator absorbing a continued trickle of
            # rare new buckets changes by a tiny FRACTION of itself each
            # check and now counts as stable; a genuinely still-ramping-up
            # job (early in a run, most buckets not yet discovered at all)
            # still changes by a large fraction each check and is correctly
            # rejected exactly as before -- the discriminating signal this
            # check exists for (real ramp-up vs. real settling) is a
            # relative one, not an exact-byte-count one, and always was.
            prev = self._throughput_last_live_denom
            denom_stable = (live_denom > 0 and prev is not None and prev > 0
                             and abs(live_denom - prev) <= CALIB_MIN_FREQ_FRAC * prev)
            # P27.5 -- optional backstop (see THROUGHPUT_CEILING_CHECKS's own
            # comment): only engages if relative tolerance ALSO never
            # settles within a generous, real time budget -- never the
            # primary path, and inert for every workload (DDP/TP/FSDP, and
            # MoE once the relative-tolerance fix above is doing its job)
            # that already reaches denom_stable well before this ceiling.
            ceiling_reached = self._throughput_total_checks >= THROUGHPUT_CEILING_CHECKS
            if denom_stable or (ceiling_reached and live_denom > 0):
                self._throughput_denom_stable_checks += 1
                if self._throughput_denom_stable_checks >= PERSIST_WINDOW:
                    # P23 (self-calibration fix) -- stability is reached,
                    # but this job's OWN raw_rate at this exact moment is
                    # exactly the thing that can be contaminated if a
                    # fault has been present since launch (confirmed
                    # live, twice). Before trusting it, check for a
                    # cross-job reference: real history from OTHER job
                    # runs of the same coarse workload signature.
                    coll_types = {b[1] for (_cid, b) in self.comm_bucket_members.keys()}
                    # P26 fix -- msg_size_bins from calib.counts.keys(), NOT
                    # from comm_bucket_members/calib.scored -- see
                    # workload_signature's own docstring for exactly why
                    # this specific source is the fault-insensitive one.
                    msg_size_bins = {msg_size for calib in self.comm_calib.values()
                                      for (msg_size, _coll) in calib.counts.keys()}
                    sig = workload_signature(len(self.comm_calib), coll_types, msg_size_bins)
                    # Info-metric so a separate process (coverage_guard.py,
                    # running inside alert_engine.py) can discover THIS
                    # job's own sig without needing direct access to this
                    # aggregator's internal calib state -- pushed once,
                    # right here, using the exact same sig value everything
                    # else in this block uses, never recomputed differently.
                    self.push_buf.append(
                        (f'agg_job_workload_sig_info{{sig="{sig}",{self.base_labels()}}}', 1, ts_us // 1000))
                    history = self.query_throughput_history(sig)
                    if len(history) >= THROUGHPUT_XJOB_MIN_HISTORY:
                        history.sort()
                        mid = len(history) // 2
                        xjob_ref = (history[mid] if len(history) % 2 else
                                    (history[mid - 1] + history[mid]) / 2.0)
                        self._throughput_rate_ref = xjob_ref
                        print(f"[{self.hostname}] throughput reference: cross-job median "
                              f"{xjob_ref:.2f}/sec from {len(history)} historical run(s) "
                              f"(sig={sig}), this job's own raw_rate at stabilization was "
                              f"{raw_rate:.2f}/sec", flush=True)
                    elif raw_rate > 0:
                        # Cold start: no (or insufficient) cross-job history yet for
                        # this signature -- a real, disclosed, narrower residual gap
                        # (first-ever run(s) of a new workload shape), not the general
                        # case. Falls back to self-calibration, same as before.
                        self._throughput_rate_ref = raw_rate
                        print(f"[{self.hostname}] throughput reference: self-calibrated "
                              f"{raw_rate:.2f}/sec (only {len(history)} historical run(s) "
                              f"found for sig={sig}, need {THROUGHPUT_XJOB_MIN_HISTORY} -- "
                              f"cold start, not cross-job-verified)", flush=True)
                    if self._throughput_rate_ref is not None:
                        # Contribute this job's own measurement to history for
                        # FUTURE jobs, regardless of which reference THIS job used.
                        self.push_buf.append(
                            (f'agg_job_throughput_rate_ref{{sig="{sig}",{self.base_labels()}}}',
                             raw_rate, ts_us // 1000))
            else:
                self._throughput_denom_stable_checks = 0
            self._throughput_last_live_denom = live_denom
            return  # no reference baseline yet -- report nothing, rather than guess
        ratio = raw_rate / self._throughput_rate_ref if self._throughput_rate_ref else 0.0
        self.push_buf.append((f'agg_job_throughput_ratio_to_baseline{{{self.base_labels()}}}',
                               ratio, ts_us // 1000))

    def score_mean_window(self, comm_id, bucket, window_vals, ts_us):
        for p, vals in window_vals.items():
            mv = st.mean(vals)
            self.state[(comm_id, p, bucket)].mean_history = mv
            # P27-hotfix4 -- real role label (comm-local rank, n_ranks),
            # added to this ALREADY-pushed metric rather than inventing a
            # new one -- see phys_comm_role's own docstring at __init__.
            # role_rank/role_n defaults ("na") for the rare case a member
            # closes its first window before handle_record has captured
            # its role yet -- never blocks the existing push, just an
            # honestly-unlabeled cold-start row this session's own
            # cross-job query (see alert_engine.py's _member_role_
            # baseline) already knows to skip.
            role_rank, role_n = self.phys_comm_role.get((comm_id, p), ("na", "na"))
            self.push_buf.append((f'agg_mean_exec_time_us{{comm="{comm_id}",member="{p}",'
                                   f'role_rank="{role_rank}",role_n="{role_n}",{bucket_labels(bucket)},{self.base_labels()}}}',
                                   mv, ts_us // 1000))
        members_here = [p for p in window_vals if p not in EXCLUDE_ALWAYS]
        if len(members_here) < 3:
            return
        # P27-hotfix3 -- real, TIME-based grace period, see
        # bucket_scored_at_ts_us's own docstring at __init__ and
        # BUCKET_MATURITY_GRACE_S's own comment for the full finding
        # (an occurrence-count-based grace was tried first and confirmed
        # the mechanism -- real, monotonic 12->9->5 fires/run -- but never
        # cleanly separated real elapsed time from sample-count/window-
        # size without more blind tuning). Only fired's OWN decision is
        # gated here -- window computation and z/mm reporting still run
        # and push below regardless, real numbers, never hidden, same
        # "never suppress data" discipline this file already follows for
        # DCGM-down/self-calibration-cold-start.
        scored_at = self.bucket_scored_at_ts_us.get((comm_id, bucket))
        past_grace = scored_at is not None and (ts_us - scored_at) / 1e6 > BUCKET_MATURITY_GRACE_S
        # Coverage-floor follow-up (this session) -- past_grace is EXACTLY
        # "has this bucket had a fair chance to fire yet," independent of
        # whether a fault is actually present -- the same real question
        # "silence is ambiguous between healthy and never-checked" needs
        # answered honestly. Pushed as its own real metric here (the same
        # gate already computed above, not a second timing mechanism) so a
        # short job can be told apart from a genuinely-checked-and-healthy
        # one without re-deriving anything.
        self.push_buf.append((f'agg_detection_coverage_achieved{{comm="{comm_id}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               1 if past_grace else 0, ts_us // 1000))
        means = {p: self.state[(comm_id, p, bucket)].mean_history for p in members_here}
        sorted_vals = sorted(means.values())
        n = len(sorted_vals)
        median = sorted_vals[n // 2] if n % 2 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        worst = max(means, key=lambda p: abs(means[p] - median))
        peers = [means[p] for p in means if p != worst]
        mu = st.mean(peers)
        sd = st.stdev(peers) if len(peers) > 1 else 0
        z = abs(means[worst] - mu) / sd if sd > 0 else (float("inf") if means[worst] != mu else 0.0)
        peer_median = st.median(peers)
        lo, hi = min(means[worst], peer_median), max(means[worst], peer_median)
        mm = hi / lo if lo else float("inf")
        fired = past_grace and z > MEAN_Z_THRESH and mm > MEAN_MM_THRESH
        z_report = z if z != float("inf") else 1e6
        mm_report = mm if mm != float("inf") else 1e6
        self.push_buf.append((f'agg_mean_fired{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               1 if fired else 0, ts_us // 1000))
        self.push_buf.append((f'agg_mean_z_worst{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               z_report, ts_us // 1000))
        self.push_buf.append((f'agg_mean_mm_worst{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               mm_report, ts_us // 1000))

    def score_cv_window(self, comm_id, bucket, window_vals, ts_us):
        cv_exclude = EXCLUDE_ALWAYS | EXCLUDE_CV_EXTRA
        for p, vals in window_vals.items():
            cv = verified_stat_cv(vals, TRIM)
            self.state[(comm_id, p, bucket)].cv_history.append(cv)
            self.push_buf.append((f'agg_cv_exec_time{{comm="{comm_id}",member="{p}",{bucket_labels(bucket)},{self.base_labels()}}}',
                                   cv, ts_us // 1000))

        members_here = [p for p in window_vals if p not in cv_exclude]
        cvs = {p: self.state[(comm_id, p, bucket)].cv_history[-1] for p in members_here}
        # P21.5 -- discovered directly against real 2-member (TP-pair)
        # communicator data: with exactly 2 total members, "peers" (all
        # members except whichever is "worst") always has exactly 1
        # element, so st.stdev(peers) is undefined by construction (stdev
        # needs >=2 data points) and falls to the `else 0` branch below --
        # making z EITHER 0.0 or float("inf") every single window,
        # regardless of whether anything is actually wrong. Confirmed
        # directly: 30 of 32 alerts on a genuinely healthy TP job were
        # exactly this (z=1000000.0, the inf-report sentinel), all on
        # 2-member communicators, zero on the 4-member DP communicators
        # also present in the same run. This is not a bug in ingestion or
        # calibration (both correctly delivered real, comm-scoped data
        # here) -- it's a real, previously-unexercised gap in the CV
        # z-score formula itself, which needs a real peer DISTRIBUTION
        # (2+ peers, i.e. 3+ total members) to be meaningful. mean's own
        # window-scoring already guards on exactly this (`len(...) < 3`
        # above) -- CV's `< 2` was the one guard in this file that didn't
        # match that already-established convention. Fixed by matching
        # it, not by inventing a new number: any communicator of ANY size
        # is required to have 3+ members before CV is scored at all,
        # generically, not "at least 3 unless it's a TP pair".
        # check_outlier_count is unaffected by the same degeneracy (it's
        # median-based, not stdev-based -- a median of a single peer value
        # is perfectly well-defined) so it must not be skipped just
        # because CV's own gate didn't clear; only the CV-specific scoring
        # below is gated on len(cvs) < 3.
        if len(cvs) >= 3:
            worst = max(cvs, key=lambda p: cvs[p])
            peers = [cvs[p] for p in cvs if p != worst]
            mu = st.mean(peers)
            sd = st.stdev(peers) if len(peers) > 1 else 0
            z = (cvs[worst] - mu) / sd if sd > 0 else (float("inf") if cvs[worst] > mu else 0.0)
            whist = self.state[(comm_id, worst, bucket)].persist_hist
            whist.append(z > CV_Z_THRESH)
            fired = sum(whist) >= PERSIST_REQUIRED
            self.push_buf.append((f'agg_persistence_fired{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                                   1 if fired else 0, ts_us // 1000))
            z_report = z if z != float("inf") else 1e6
            self.push_buf.append((f'agg_cv_z_worst{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                                   z_report, ts_us // 1000))

        self.check_rank0(comm_id, bucket, ts_us)
        self.check_outlier_count(comm_id, bucket, ts_us)

    def check_rank0(self, comm_id, bucket, ts_us):
        """P21.5 -- 'rank0' had a specific meaning (the master process's
        known bookkeeping overhead) that depended on a stable global rank
        number; see module docstring's exclusion gap. Left as a no-op hook
        (never fires, since no phys_id is privileged as "rank 0" here)
        rather than silently guessing which physical GPU that was."""
        return

    def check_outlier_count(self, comm_id, bucket, ts_us):
        outlier_exclude = EXCLUDE_ALWAYS | EXCLUDE_OUTLIER_EXTRA
        members = sorted(self.comm_bucket_members[(comm_id, bucket)])
        members_here = [p for p in members if p not in outlier_exclude]
        counts = {}
        for p in members_here:
            vals = self.state[(comm_id, p, bucket)].all_vals
            if len(vals) >= 20:
                counts[p] = stat_outlier_count(vals)
        for p, c in counts.items():
            self.push_buf.append((f'agg_outlier_count{{comm="{comm_id}",member="{p}",{bucket_labels(bucket)},{self.base_labels()}}}',
                                   c, ts_us // 1000))
        if len(counts) < 2:
            return
        worst = max(counts, key=lambda p: counts[p])
        peers = [counts[p] for p in counts if p != worst]
        mm = maxmed(counts[worst], peers)
        fired = counts[worst] >= OUTLIER_COUNT_THRESH and mm > OUTLIER_COUNT_MM_THRESH
        mm_report = mm if mm != float("inf") else 1e6
        self.push_buf.append((f'agg_outlier_count_fired{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               1 if fired else 0, ts_us // 1000))
        self.push_buf.append((f'agg_outlier_count_mm_worst{{comm="{comm_id}",member="{worst}",{bucket_labels(bucket)},{self.base_labels()}}}',
                               mm_report, ts_us // 1000))

    def maybe_heartbeat(self):
        now = time.time()
        if now - self._last_heartbeat_wall < HEARTBEAT_INTERVAL_S:
            return
        self._last_heartbeat_wall = now
        now_ms = int(now * 1000)
        self.push_buf.append((f'agg_aggregator_heartbeat{{{self.base_labels()}}}', 1, now_ms))
        self.push_buf.append((f'agg_push_success_total{{{self.base_labels()}}}', self.n_pushes, now_ms))
        self.push_buf.append((f'agg_push_failures_total{{{self.base_labels()}}}', self.n_push_failures, now_ms))
        self.push_buf.append((f'agg_consecutive_push_failures{{{self.base_labels()}}}',
                               self.consecutive_push_failures, now_ms))
        # P21.7 -- re-pushed here, using CURRENT wall time, not the
        # historical dump-record timestamp from when each phys_id was
        # first seen (see handle_record's own comment for why that was a
        # real bug). Same cadence as the heartbeat itself.
        for phys_id, slot in self.phys_gpu_slot.items():
            self.push_buf.append((f'agg_member_gpu_slot_index{{member="{phys_id}",{self.base_labels()}}}',
                                   slot, now_ms))

    def flush(self):
        if not self.push_buf:
            return
        body = "\n".join(f"{m} {v} {t}" for m, v, t in self.push_buf) + "\n"
        req = urllib.request.Request(self.vm_url, data=body.encode(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.n_pushes += 1
            self.consecutive_push_failures = 0
        except Exception as e:
            self.n_push_failures += 1
            self.consecutive_push_failures += 1
            print(f"push error (consecutive_failures={self.consecutive_push_failures}, "
                  f"total_failures={self.n_push_failures}): {e}", file=sys.stderr)
        self.push_buf = []

    def run(self, duration_s, poll_interval=0.25):
        # P22.5 -- deadline now threaded into poll_files() itself (see its
        # own docstring), so a single call facing a large backlog can no
        # longer block this loop's own duration check for longer than
        # ~DEADLINE_CHECK_EVERY records' worth of processing time. The
        # explicit `if time.time() >= t_end: break` right after
        # poll_files() additionally skips the sleep() once the deadline
        # has already passed, rather than waiting out a full poll_interval
        # for no reason.
        t_end = time.time() + duration_s
        while time.time() < t_end:
            self.poll_files(deadline=t_end)
            self.maybe_heartbeat()
            self.flush()
            if time.time() >= t_end:
                break
            time.sleep(poll_interval)
        self.poll_files(deadline=t_end)
        self.maybe_heartbeat()
        self.flush()
        n_comms = len(self.comm_calib)
        print(f"[{self.hostname}] done. records_seen={self.n_records_seen} pushes={self.n_pushes} "
              f"push_failures={self.n_push_failures} mean_windows={self.n_mean_windows} "
              f"cv_windows={self.n_cv_windows} comms_discovered={n_comms} "
              f"cluster={self.cluster} slurm_job_id={self.slurm_job_id}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump_dir")
    ap.add_argument("hostname")
    ap.add_argument("vm_url")
    ap.add_argument("--node", choices=["A", "B"], default="A",
                     help="P21.5: no longer selects a rank set (communicator membership "
                          "is discovered from data) -- kept only so existing launch "
                          "scripts that pass --node keep working; unused otherwise.")
    ap.add_argument("--duration", type=float, default=90)
    ap.add_argument("--cluster", default=DEFAULT_CLUSTER)
    ap.add_argument("--slurm-job-id", default=None,
                     help="Override; if omitted, looked up live via squeue and refreshed every %ds" % JOB_ID_REFRESH_S)
    args = ap.parse_args()
    agg = NodeAggregator(args.dump_dir, args.hostname, args.vm_url,
                         cluster=args.cluster, slurm_job_id=args.slurm_job_id)
    agg.run(args.duration)
