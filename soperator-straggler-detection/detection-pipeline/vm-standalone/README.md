# Standalone VictoriaMetrics — the real, working detection backend

**Use this. Do not start with the Kubernetes VMSingle Helm chart**
(`../../straggler-vmsingle/` — see its `DEPRECATED.md`). That path is
confirmed RBAC-blocked (zero in-pod Kubernetes permissions) on **two
separate clusters in a row**. This standalone approach needs no
Kubernetes access at all — it runs as a plain binary, launched as an
ordinary Slurm job, exactly like a training job. This is what every real
test in this project's history actually ran against, and it's what
`observability/dashboards/provisioning/datasources/local.yaml` (see its
own hardcoded-`worker-0:8428` flag in the top-level README) points at.

## What this is

A single, official upstream VictoriaMetrics binary
(confirmed version: `victoria-metrics-20260814-123346-tags-v1.150.0`, via
`./victoria-metrics-prod --version` — download the matching or newer
release directly from
[VictoriaMetrics/VictoriaMetrics releases](https://github.com/VictoriaMetrics/VictoriaMetrics/releases);
**do not commit a prebuilt binary into this repo** — verify the release
you download starts cleanly on the target cluster's own kernel/libc
before wiring anything else to it, first with a bare `--version` smoke
test). No install, no daemon, no systemd unit required — it's launched
directly and reads/writes real files under `-storageDataPath`.

## Real, confirmed launch command

This is the exact command this pipeline's own tests were run against
(reconstructed from the live, running process — `ps aux` against the
actual job — not from memory):

```bash
srun --partition=main --nodelist=<a node NOT used for GPU compute tests> \
     --gres=gpu:1 --cpus-per-task=3 --mem=8G --time=7-00:00:00 \
     --job-name=vm_standalone --immediate=60 \
     /path/to/victoria-metrics-prod \
     -storageDataPath=/path/to/vm-standalone/data \
     -httpListenAddr=:8428 \
     -dedup.minScrapeInterval=0s \
     -retentionPeriod=100y
```

Every flag here is load-bearing, not left at a convenient default:

- **`-dedup.minScrapeInterval=0s` — the single most load-bearing setting
  in this whole pipeline's detection accuracy, not a minor tuning knob.**
  VictoriaMetrics's normal default dedup window (30s in most configs) is
  the SAME real problem that blocked using the platform's own shared
  Grafana/vmagent instance earlier in this project's history: it silently
  drops the vast majority of the high-frequency samples this pipeline
  depends on, because the CV detector's entire persistence window lives
  inside a single 30s dedup bucket. **Do not point this pipeline at any
  VictoriaMetrics/Prometheus instance without first confirming its own
  dedup interval is 0s (or short enough relative to this aggregator's
  ~2-6s emission cadence)** — this is exactly the mistake the now-
  deprecated `straggler-vmsingle/` Kubernetes chart's own README already
  documents almost happening with the platform's shared instance.
- **`-retentionPeriod=100y`** — this pipeline relies on real cross-job
  history (`agg_job_throughput_rate_ref`, keyed by `workload_signature`)
  persisting across many separate, independent Slurm jobs run hours or
  days apart. A short default retention would silently evict exactly the
  data the cross-job-reference mechanism (used for cold-start/self-
  calibration fallback — see `aggregator/node_aggregator_ref.py`) needs.
- **`--gres=gpu:1`** — VM itself doesn't use the GPU; this is just enough
  of a resource request to get a real Slurm allocation on a dedicated node
  without competing for a full node's GPUs. Pick a node **not** used for
  the actual training/GPU workloads under test, so VM never competes with
  training jobs for CPU or scheduling.
- **`--time=7-00:00:00`** — VM needs to run for the entire testing
  session/campaign, not one test's duration. Extend as needed with
  `scontrol update jobid=<id> TimeLimit=<new-limit>` (this project found
  plain `scontrol` returns "Access/permission denied" for a job's own
  owner on at least one cluster; `sudo -n scontrol update ...` was
  confirmed working there instead — a real, cluster-specific permission
  quirk, not universal).

## Before starting fresh on a new cluster

**Do not copy an existing `-storageDataPath` data directory to a new
cluster.** It will contain real, accumulated time-series data from every
prior test run — potentially hundreds of MB of stale metrics, plus a real
risk of old, dead communicator IDs and workload signatures still being
"discoverable" by VM's own label-index queries long after the jobs that
produced them ended (this project found and fixed a live bug in
`node_aggregator_ref.py`/`alert_engine.py`'s own comm/bucket discovery
caused by exactly this — see `../docs/codebase_reference.md`). Start a new
cluster's instance with an **empty** `-storageDataPath` directory.

## Verifying it's up

```bash
curl -s http://<vm-host>:8428/health   # expect "OK"
curl -s "http://<vm-host>:8428/api/v1/query?query=up"
```

Point `aggregator/node_aggregator_ref.py` and `alerting/alert_engine.py`
at `http://<vm-host>:8428` (both take this as a positional/first CLI
argument), and `observability/dashboards/provisioning/datasources/local.yaml`'s
`url:` field at the same host — see the top-level README's "known
limitations" section for why that file currently hardcodes
`worker-0:8428` specifically and will need to become dynamic in Stage 2.
