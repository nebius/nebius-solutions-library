# Falcon Alert Layer

A classification-based alert layer for the Falcon straggler detection system.

---

## What it does

The alert layer reads `FALCON_STAGE3_RESULT` lines emitted by the existing Falcon Stage-3
detector and classifies them into one of two output classes:

| Class | Meaning | Action |
|---|---|---|
| `STRAGGLER` | One rank is persistently slow; the per-group timing shows a concentrated, non-diffuse signal | Investigate the named rank immediately |
| `SLOWDOWN-DIFFUSE` | Cluster-wide or diffuse slowdown; no single rank is the cause | Check network, co-tenants, GPU throttling |

Both classes are always surfaced to stdout — neither is silently dropped.

---

## What it does NOT change

- The Falcon Stage-1, Stage-2, or Stage-3 detectors.
- Any training pod manifest, Megatron script, or Inspector plugin.
- Any VictoriaMetrics push or Grafana dashboard.

The alert layer is a pure **consumer** of existing log output. It can be run against
historical logs (replay mode) or live-tailed logs (stdin mode) without touching any
running process.

---

## Output classes

### STRAGGLER

```
[FALCON_ALERT 2026-11-17T06:38:00.001Z] STRAGGLER rank=8 node='falcon-training-0'
  confidence=HIGH window=[06:36:00Z..06:38:00Z] events=42 high_conf=38
  F1=rank=8 node='falcon-training-0' named 38/38 high-conf events (p=0.0000)
  F2=straggler pattern in 20/20 sampled events (bimodal-split in per_rank_confirm_ms)
  F3=rank=8 confirmed in groups: ['PP', 'TP']
  F4=step_time_change(+340%,...)
```

Fields:
- `rank=` — the most likely root-cause training rank (0-based, matches torchrun rank)
- `node=` — the pod name where the rank was identified
- `confidence=HIGH` if F3 (multi-group corroboration) also fires, else `MEDIUM`
- `events=` — total CONFIRMED Stage-3 events in the window
- `high_conf=` — events where Stage-3 itself said `attribution_confidence=high`
- `F1=` — concentration result (binomial test against uniform distribution)
- `F2=` — structural pattern in per_rank_confirm_ms
- `F3=` — multi-group corroboration
- `F4=` — step-time effect (informational only, never used for classification)

### SLOWDOWN-DIFFUSE

```
[FALCON_ALERT 2026-11-17T07:02:00.001Z] SLOWDOWN-DIFFUSE — Cluster-wide slowdown
  detected — check network, co-tenants, GPU throttling.
  window=[07:00:00Z..07:02:00Z] events=74 high_conf=4
  F1=rank=5 node='falcon-training-1' named 4/4 high-conf events (p=0.0012) [but F2 not met]
  F2=diffuse pattern in 20/20 sampled events (all members within tight spread)
  ...
```

No rank is named. Concentration may be present (F1) but the per-group structure (F2) does
not show a single slow member — the slowdown is shared across the group.

---

## Running in replay mode (from local log files)

```bash
python3 k8s/alert_layer/falcon_alert_layer.py \
    --logs /path/to/node0.log /path/to/node1.log \
    --window 120 \
    --min-events 5
```

Options:
- `--window N` — sliding window in seconds (default: 120)
- `--min-events N` — minimum CONFIRMED events in window before classifying (default: 5)
- `--classify-every N` — run classifier every N events in replay mode (default: 10)

The script reads all logs, sorts events by timestamp, and replays them through the
classifier.

---

## Running in live-tail mode (stdin)

```bash
# Tail from a running training pod:
kubectl exec -n falcon-k8s falcon-training-0 -- \
    tail -f /falcon-workspace/output/<session>/train_falcon-training-0.log | \
    python3 k8s/alert_layer/falcon_alert_layer.py --stdin --window 120 --min-events 5

# Or from a local log that is still being written:
tail -f /tmp/train.log | python3 k8s/alert_layer/falcon_alert_layer.py --stdin
```

---

## Running on Slurm logs (unchanged)

The alert layer works identically on Slurm logs.  Slurm training logs contain the same
`FALCON_STAGE3_RESULT` lines (the format is not K8s-specific).

```bash
python3 k8s/alert_layer/falcon_alert_layer.py \
    --logs /path/to/slurm_node0.log /path/to/slurm_node1.log
```

---

## Cross-node gate evidence test

The cross-node correlation gate (suppress if both nodes show equal event rates) was
evaluated as an evidence test.  It is NOT adopted in the alert layer.

**R3 evidence test results (2026-11-17):**

| Dataset | Windows evaluated | Gate would suppress | Interpretation |
|---|---|---|---|
| R3 Megatron healthy soak (45K iters, both nodes) | 118 | 91 (77.1%) | True suppressions — healthy symmetric pattern |
| Megatron fault trials (3 × 150ms, both nodes combined) | 13 | 0 (0%) | No false negatives — straggler creates asymmetry |
| DDP/FSDP fault trials | n/a | 0 | Fault asymmetry, gate would not suppress |

The gate is highly selective (77% suppression of healthy windows, 0% of fault windows),
which is a useful property — but it is NOT adopted because the classification approach
(F1+F2) already achieves 0 false STRAGGLER in healthy and 21/21 fault detection, making
the gate unnecessary.  Additionally, the steering note prohibits adoption regardless of
evidence (Decision 9 in `docs/K8S_DECISIONS_PENDING.md`).

To reproduce the test on saved fault trial logs:

```bash
python3 k8s/alert_layer/falcon_alert_layer.py \
    --test-cross-node-gate \
    --gate-logs node0.log node1.log \
    --window 120
```

See `docs/K8S_DECISIONS_PENDING.md` for the rationale.

---

## Classification features

| Feature | Test | Used for classification? |
|---|---|---|
| F1 | Binomial concentration of high-conf events on one (rank, node) pair | Yes |
| F2 | Straggler structural pattern in per_rank_confirm_ms (single low outlier or bimodal split with ≥10ms high cluster) | Yes |
| F3 | Same rank confirmed in ≥2 group types (TP + PP, etc.) | Yes — raises confidence to HIGH |
| F4 | Step-time increase in window | Informational only — logged, never read by classifier |

**STRAGGLER fires if and only if F1 AND F2 are both true.**

---

## Design / held-out discipline

The classification rule was **frozen on the design set** (all R1–R3 sessions except
explicitly held-out episodes).  The held-out set must not be inspected until the rule
is frozen.  See `docs/K8S_DECISIONS_PENDING.md` for details.

Split unit: by **whole session or whole episode**, never by individual event or by
time-halves of one soak.

Labelled episodes in the design set:
- R3 Megatron healthy soak, iters 10,000–17,000: `SLOWDOWN-DIFFUSE (natural, cause unknown)`
  (see `docs/R3_MEGATRON_SOAK_AUTOPSY.md` for full analysis)
- Injected diffuse episodes: `SLOWDOWN-DIFFUSE (injected)` (use `inject_diffuse_slowdown.sh`)

---

## Acceptance criteria (Decision 10 — pending Adam's confirmation)

| Criterion | Target |
|---|---|
| STRAGGLER alerts in healthy sessions | 0 (hard requirement) |
| Injected diffuse episodes classified SLOWDOWN-DIFFUSE | 3/3 |
| Injected faults 10/25/50/100% CPU classified STRAGGLER | 3/3 PRIMARY |
| Time to STRAGGLER alert for injected faults | ≤120 s (proposed) |

---

## Environment sampler

Run `env_sampler.sh` alongside all soaks and fault trials to collect host/GPU telemetry
aligned with FALCON_STAGE3_RESULT timestamps.  This enables post-hoc autopsy of
anomalous healthy events.

```bash
source detection-pipeline/cluster_k8s.env
nohup bash k8s/alert_layer/env_sampler.sh >> /tmp/env_sampler.log 2>&1 &
echo "Sampler PID: $!"
```
