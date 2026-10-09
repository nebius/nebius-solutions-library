# Features and roadmap

*Nebius Serverless AI (the managed service, as documented in October 2026) against this solution, and
what neither has yet. "Verified" means built and exercised on a test fleet (docs/VERIFICATION.md).*

## Feature list

| Feature | Serverless AI | This solution |
|---|---|---|
| Endpoints with scale to zero | yes | yes |
| Replicas per endpoint (min/max), warm floor | yes | yes, per region |
| Scaling metrics | concurrency | concurrency, requests per second, utilization target, cooldown, idle time (Knative) |
| GPU selector | one platform and preset | GPU class with fallbacks; spot, on-demand and reserved pools |
| Cold start: image cache | no control | per-region pull-through cache plus peer-to-peer distribution, pre-pull on every pool (docs/IMAGES.md) |
| Cold start: warm spare node | no | yes, `warm_nodes` per pool (docs/FLEET.md; 68 s cold start measured against 3 to 6 min with a node boot) |
| Shared model weights | no | per-region filesystem, read-only in endpoints |
| Job queue | no | yes, Kueue with per-tenant quotas (docs/SCHEDULING.md) |
| Queue priorities | no | yes (low, normal, high) |
| Multi-node jobs over InfiniBand | no | yes, verified with 16 GPUs (docs/JOBS.md) |
| Multi-node inference (one endpoint over several nodes) | no | no; runs as a multi-node job today |
| Different image per GPU class | no | yes, the class is chosen at submission |
| Multi-region placement by price and free capacity | single region | yes, four regions on the test fleet |
| Preemptible capacity and pricing policy | no | yes: follow the spot price or a price cap per pool, checkpoint resume after preemption |
| Local NVMe scratch | no | yes (B300) |
| API keys | project keys | per-tenant keys with budgets, spend, expiry, rate limits and model allow-lists (LiteLLM) |
| OpenAI-compatible routing in front of every model | per endpoint | one LiteLLM model group per model |
| Console | yes | yes: models, endpoints, jobs with metrics and logs, keys |
| Observability and cost reports | basic | Grafana, Prometheus, Loki, OpenCost per key and per run |
| Private networking, own projects and buckets | no | yes |
| Operations | none | yours: Terraform, one `terraform.tfvars` |

## Roadmap: neither has it yet

1. **Multi-node inference endpoints.** A LeaderWorkerSet flavour of `charts/endpoint` reusing the multi-node
   job's pod pieces, Kueue's LeaderWorkerSet integration, the existing route and LiteLLM registration and the
   weights mount. About two to three days.
2. **Scaling buffer: extra ready replicas above the autoscaler's recommendation.** See the section below for
   why it is not a knob today and what exists instead.
3. **Request-based routing to variants of one model** (a header or request field picks reserved capacity first,
   spot as overflow): LiteLLM model groups with several deployments, written from the model's `routing` block
   (`docs/DESIGN-REVIEW-2026-10-09.md`).
4. **Re-render a waiting job for the next GPU class after a timeout** (option A+ of the design review): about
   150 lines in the dispatcher's reconcile loop plus tests.
5. **Read-only weights mount for jobs**, and job images rewritten to the cache automatically (endpoints have
   both; a multi-node run loads its weights into its own checkpoint path today).
6. **GPU sharing for many small runs** (MIG or MPS). MIG was measured on 2026-10-09: slices scale with their
   SM share, a fully partitioned GPU gives 1 to 5 percent (H100) or 7 to 10 percent (RTX PRO 6000) more
   aggregate throughput; a packing tool, not a speed-up. Test plain multi-process sharing or MPS before
   building either into the platform.
7. **InfiniBand pools that grow automatically.** The cluster autoscaler's simulated node has no DRA attributes
   and never adds an InfiniBand node; `min_nodes` is set by hand before a multi-node run.
8. **Per-tenant GPU quotas in the console, SSO for the operator UIs, a formal security review** before any
   customer launch (README "Security notes and limitations").

## Why the scaling buffer is not a knob yet

A scaling buffer means "keep N ready replicas more than the load needs", so a burst finds a replica
immediately. Knative's autoscaler, which runs every endpoint here, has no additive setting: it computes the
desired replica count as observed load divided by the target (concurrency or requests per second), and the
only ways to keep headroom are proportional or fixed:

- **Proportional headroom exists today**: `scaling.utilization_percent` (Knative's target utilization) makes
  the autoscaler add replicas at, say, 70 percent of the target instead of 100 percent. With a target of 4
  concurrent requests per replica and 50 percent utilization, every replica is held at 2 in-flight requests
  and the next replica starts before the current ones are full. This is the knob the console exposes.
- **A fixed floor exists today**: `scaling.min` keeps replicas up regardless of load.
- **A warm spare node exists today**: `warm_nodes` keeps a node ready for the next replica of any model.

An additive buffer ("current demand plus one replica") needs a small controller that watches every revision's
desired scale and patches its `min-scale` to desired plus N, with answers to three questions: what happens at
zero (a buffer of one at zero load means the model never scales to zero, so the buffer must switch off below
a floor), how fast the floor follows demand down (cooldown, otherwise the buffer thrashes), and what it costs
per region (N extra GPUs per endpoint per region, always). Neither Knative nor KEDA nor the Kubernetes HPA
offers that natively, so it is custom code (about a hundred lines plus tests) and it was deliberately left
out of this pull request; the console shows the control disabled with that note. Ask for it when proportional
headroom and the warm spare node are not enough for a workload.
