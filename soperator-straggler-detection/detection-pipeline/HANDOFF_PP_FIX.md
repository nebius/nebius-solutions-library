# Handoff: PP (Plain Pipeline-Parallel) fault-detection fix

You are starting fresh. This document is the complete context you need —
do not re-derive anything below from scratch; confirm/extend, don't
re-discover.

## Confirmed root cause (already traced to the exact code, do not re-trace)

PP's below-floor timing-asymmetry fallback (`_timing_asymmetry_fallback_
evaluate` in `alerting/alert_engine.py`) never fires on a real, strongly
signaled PP fault. This has been root-caused precisely, live, by directly
invoking the function against a real running PP job (not by reading code
alone):

1. **Member discovery is NOT the problem.** `_comm_cross_node_members`
   (also in `alerting/alert_engine.py`) was confirmed, live, to correctly
   return both real cross-node members (e.g.
   `[('worker-0', '<pid>'), ('worker-1', '<pid>')]`) for PP's real
   Send/Recv comm. The earlier P27-hotfix4 cross-node-discovery fix
   already in this file works exactly as intended. `len(members_with_
   host) != 2` does NOT trigger — don't waste time re-checking this.

2. **The function returns `None` at the baseline step**, inside the
   per-member loop that calls `_member_role_baseline` then
   `_cross_comm_peer_median`. This was confirmed by manually stepping
   through the function's logic line-by-line against live data (see
   "How to reproduce this diagnosis" below).

3. **The real reason**: `_member_role_baseline`'s cross-job history pool
   for BOTH of PP's roles (`role_rank=0,role_n=2` and `role_rank=1,
   role_n=2`) is **100% contaminated**. Every single historical
   `agg_mean_exec_time_us` entry ever recorded for these two roles,
   across this cluster's entire history, reflects the same repeated
   target-rank/200ms fault this project's own testing convention has
   always used for PP (STRAGGLER_TARGET_RANKS=1, STRAGGLER_SLEEP_MS=200,
   every single time, every session). When queried directly, role0's
   historical values cluster around ~204,000-206,000us and role1's
   around ~2,200-7,300us — both already reflecting the sleeping-target/
   waiting-partner signature of the SAME fault, never a genuinely
   healthy baseline.

4. **Why the existing anti-poisoning safeguard never caught this**:
   `alerting/alert_engine.py` already has a real mechanism for exactly
   this — `_excluded_role_pool_members` (fed by `_push_role_baseline_
   exclusion`) drops any (comm, member) that a role pool has ever seen
   flagged as anomalous. But it only engages **after a successful
   fire**. PP's fallback has never once fired — so the safeguard has
   never had a single opportunity to exclude anything. This is a
   self-reinforcing trap: the absence of detection is precisely what
   keeps the absence of detection permanent.

5. **On the "36x successful signal" this project's docs previously
   cited for PP**: that measurement almost certainly predates this
   contamination (a clean or empty history pool at the time it was
   taken). It does not generalize once this project's own repeated,
   parameter-identical fault testing accumulates in the pool. This is a
   real, reproducible, self-inflicted regression — not a code change,
   and not a member-discovery bug.

### How to reproduce this diagnosis yourself (if you want to re-verify before trusting it)

```python
import sys
sys.path.insert(0, 'classifier')
sys.path.insert(0, 'alerting')
import alert_engine as ae

vm_url = 'http://worker-0:8428'
comm = '<PP's real Send/Recv comm id, from a live run>'
eng = ae.AlertEngine(vm_url)

members = ae._comm_cross_node_members(vm_url, comm)  # will show 2 real members
result = eng._timing_asymmetry_fallback_evaluate('worker-0', comm)  # returns None

# To see the actual per-member data / where it degrades to None, step through
# _member_role_baseline directly for each (hostname, member, bucket='4194304',
# coll='Recv', role_rank, role_n='2') and inspect its returned (median, mad).
```

## CRITICAL: do not validate any fix against PP's existing historical data

**PP's entire historical `agg_mean_exec_time_us` role-baseline data is
confirmed contaminated.** Do not treat "the fix makes PP's role-baseline
lookup return a reasonable-looking number against existing history" as
success — the existing history is exactly the wrong thing to succeed
against. **Your first real task is figuring out how to obtain or
generate genuinely clean (unfaulted, or at least fault-varied) data to
validate any fix against** — options include (but are not limited to):
running several genuinely healthy PP baselines before attempting any
fix validation, or deliberately varying fault parameters (see Direction
2 below) as part of establishing a trustworthy validation baseline in
the first place.

## Two candidate fix directions (identified, neither attempted yet)

1. **A fire-independent role-pool sanity check.** Instead of relying
   solely on `_push_role_baseline_exclusion` (which requires a prior
   successful fire), add an independent check on the role pool's own
   internal consistency — e.g., flag a pool as suspect if it shows
   near-zero natural variance across many different jobs (a real,
   healthy PP pair's own structural ~2.2x stage asymmetry would still
   show real job-to-job variance; a uniformly-repeated identical fault
   would not). Not designed or prototyped yet.

2. **Varied fault-testing parameters.** If this project's own PP test
   convention deliberately varied the injected target rank and/or sleep
   duration run-to-run (instead of always STRAGGLER_TARGET_RANKS=1,
   STRAGGLER_SLEEP_MS=200), a genuinely healthy baseline would have real
   chances to enter the pool between faulted runs, and the existing
   exclusion safeguard would have real data to work with sooner. Not
   attempted yet; would also require deciding how many/which historical
   entries (if any) already in the pool should be considered
   recoverable vs. permanently discarded.

Neither direction has been implemented, prototyped, or chosen over the
other. That decision, and the actual implementation, is this session's
work.

## Real file/function locations (already traced — start here, don't re-search)

- `alerting/alert_engine.py`:
  - `_timing_asymmetry_fallback_evaluate` (~line 1922) — the function
    that returns `None`. Read its own docstring in full first; it
    documents the P27.2.x history in detail.
  - `_comm_cross_node_members` (~line 518) — confirmed working
    correctly, not the problem.
  - `_member_role_baseline` (~line 1704) — where the contaminated pool
    lives and where a fix would most likely need to change something.
  - `_cross_comm_peer_median` (~line 1799) — the fallback
    `_member_role_baseline` degrades to when it returns `(None, None)`;
    also relevant to Hybrid's fix (see `HANDOFF_HYBRID_FIX.md`) — be
    aware a change here could affect both investigations; coordinate or
    check the other session's status before touching this function.
  - `_excluded_role_pool_members` / `_push_role_baseline_exclusion` —
    the existing anti-poisoning safeguard, fed only by successful fires.
  - `ROLE_BASELINE_MAX_RELATIVE_MAD` (~module-level constant) — the
    existing "is this role pool too noisy to trust" gate; related to,
    but not the same check as, Direction 1 above (that gate catches high
    variance, not low/zero variance from repeated identical faults).

- The real PP launch command (per-node script, no separate dispatcher):
  ```bash
  srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=1 -w worker-0,worker-1 \
    --container-image="nvcr.io#nvidia/pytorch:25.01-py3" \
    --container-mounts="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp" \
    bash "$PKG/workloads/pp/run_pp_node.sh" <STEPS> "$PKG/var/<outdir>" <PORT> "$PKG/var/dump"
  ```
  Set `STRAGGLER_SLEEP_MS=200` and `STRAGGLER_TARGET_RANKS=1` (or vary
  per Direction 2) before launching. **Always use absolute paths** for
  both the script and the dump-dir base — a real, already-fixed bug in
  this exact script (relative `DUMPDIR="$DUMPBASE/dump_w$RANK"`) has
  already been corrected to `DUMPDIR="$DUMPBASE/$HN"` in the most recent
  commit on this branch; don't reintroduce a relative-path invocation.

## Shared-process safety rule (read before touching any live process)

**Before restarting `alert_engine.py` or any `node_aggregator_ref.py`,
confirm no other session (Hybrid's fix, DLRM's re-test) has a test
actively in flight.** These sessions may run in parallel against the
same shared cluster. Check `squeue -u "$USER" -h` and the aggregator/
alert_engine supervisor process states before restarting anything, and
coordinate if another session's job is running. A restart you trigger
will affect every other session's in-flight test too.

## Standing project rules (carry forward, do not relax)

- **n>=5 independent validation** for any new detection signal or
  threshold before trusting it.
- **Explicit rank/PID-match verification** before trusting any
  detection result — establish real ground truth (via each dump file's
  own `header.rank`/`metadata.pid`, read directly, before checking any
  alert) before looking at what fired.
- **Health precheck before/after every live step**: VM 200
  (`curl http://worker-0:8428/-/healthy`), alert engine supervisor alive
  (`ps -ef | grep alert_engine.py`), both aggregators alive, Grafana 401
  on unauthenticated `/api/org` (proves auth still enforced),
  `CHECK-FAILED` count unchanged in `var/alert_engine_supervised.log`.
- **Work only in `add/straggler-detection-v1-beta`** — never touch
  `main`, never modify existing repo content outside this contribution.
- Restart `alert_engine.py`/aggregators via clean `SIGTERM` to the leaf
  process (letting the supervisor wrapper auto-relaunch), never
  `kill -9` — and always confirm the new process's start time postdates
  your source-file edit's mtime before trusting a restart picked up
  your change.
- Do not force an incomplete fix if the real investigation shows this
  needs larger design work — a precise, scoped proposal is a complete
  and acceptable outcome.
