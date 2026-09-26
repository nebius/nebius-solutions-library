# MoE two-stage detector (P30) — found missing entirely, added in the
third completeness pass

MoE's own AllToAll traffic is architecturally unlocalizable by ordinary
peer-relative timing statistics (a synchronous collective's *waiting*
ranks show the elevated timing, not the delayed rank itself — see
`../docs/straggler_dectection_history.md` Phase 9). This is the real,
separate mechanism built to close that gap for a suspect rank once
job-wide detection has already flagged that *something* is wrong — a
distinct, 5-step diagnostic pipeline, not part of the always-on live
alert loop:

1. **`arrival_order.py`** — real "who arrived last" signal, built from
   Inspector's own per-record timestamps (`event_trace_ts.coll_start_ts`
   + kernel-event timestamps), not from `coll_exec_time_us` (already
   proven unreliable for this exact localization problem).
2. **`load_check.py`** — parses `[MOE-TOKEN-LOAD]` lines (already emitted
   by `../workloads/moe/model_moe.py`'s `MoEMLP.forward()` — confirmed
   present in the packaged copy) to compare the suspect rank's real
   assigned-token total against its peers' for the same round.
3. **`telemetry_check.py`** — wires the suspect rank into this project's
   *existing* DCGM/host-contention/NVLink checks (`classifier/cause_metrics.py`,
   already packaged) — no new telemetry-gathering, just correct targeting.
4. **`pairwise_sweep.py`** + **`run_pairwise_sweep.py`** — an isolated,
   brand-new `torch.distributed` process group between exactly the
   suspect and one healthy peer, sidestepping the barrier-smearing
   problem by construction (nothing here is a synchronous collective
   involving the whole training world).

**Hardcoded absolute paths found in this addition** (added to
`../STAGE2_HANDOFF.md`): `telemetry_check.py` inserts
`/root/P18k_classifier` into `sys.path`; `run_pairwise_sweep.py` inserts
`/root/P30_moe_detector` and hardcodes
`SCRIPT = "/root/P30_moe_detector/pairwise_sweep.py"`.
