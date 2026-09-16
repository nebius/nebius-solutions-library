# GPU straggler detection for Soperator (MVP)

This directory holds the deployable pieces of a GPU straggler/fail-slow
detection pipeline built for Soperator (Slurm-on-Kubernetes) clusters. It
is an **MVP for review**, not a final production artifact -- the
implementation is expected to be optimized after initial review.

## What's here

- [`straggler-vmsingle/`](./straggler-vmsingle/README.md) -- a Helm chart
  that deploys a dedicated, single-tenant VictoriaMetrics `VMSingle`
  instance for this pipeline's own metrics. It exists because the
  platform's shared VictoriaMetrics instance runs a `dedup.minScrapeInterval`
  (30s) that was measured, live, to discard the large majority of this
  pipeline's real aggregate samples -- see that chart's own README for the
  full real evidence and the alternatives considered (pre-aggregation, and
  changing the shared instance's setting) and why they were rejected.

Start with `straggler-vmsingle/README.md` for the install steps, the
real validation performed, and the open items flagged for review.
