# GPU straggler detection for Soperator

This directory holds the deployable pieces of a GPU straggler/fail-slow
detection pipeline built for Soperator (Slurm-on-Kubernetes) clusters.

## What's here

- [`detection-pipeline/`](./detection-pipeline/README.md) -- the actual
  detection pipeline: the NCCL Inspector profiler plugin, the node-side
  aggregator, the classifier/corroboration logic, the alert engine, the
  Grafana dashboard, and all 15 validated fault-injection workload shapes
  used to test it. This is a **structural packaging pass** -- an
  exhaustive, code-verified inventory of every real dependency this system
  has, organized into one portable tree (see its own `INVENTORY.md`). It
  is not yet a one-command installer; `install.sh`/`run.sh` are a
  following stage.

- [`straggler-vmsingle/`](./straggler-vmsingle/README.md) -- **deprecated,
  kept for historical reference only** (see its own `DEPRECATED.md`). A
  Kubernetes VMSingle Helm chart for this pipeline's metrics storage,
  confirmed RBAC-blocked (zero in-pod Kubernetes permissions) on two
  separate clusters in a row. Superseded by `detection-pipeline`'s
  standalone-VictoriaMetrics-as-a-Slurm-job approach, which needs no
  Kubernetes access at all.

Start with `detection-pipeline/README.md`.
