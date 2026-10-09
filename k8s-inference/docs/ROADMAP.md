# Features and roadmap

*Nebius Serverless AI (the managed service, as documented in October 2026) against this solution, and
what neither has yet. "Verified" means built and exercised on a test fleet (docs/VERIFICATION.md).*

## Feature list

| Feature | Serverless AI | This solution |
|---|---|---|
| Endpoints with scale to zero | yes | yes |
| Replicas per endpoint (min/max), warm floor | yes | yes, per region |
| Scaling buffer: ready replicas above demand | no | yes, `scaling.buffer` (the dispatcher holds Knative's min-scale at demand + N) |
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
2. **Request-based routing to variants of one model** (a header or request field picks reserved capacity first,
   spot as overflow): LiteLLM model groups with several deployments, written from the model's `routing` block
   (`docs/DESIGN-REVIEW-2026-10-09.md`).
3. **Re-render a waiting job for the next GPU class after a timeout** (option A+ of the design review): about
   150 lines in the dispatcher's reconcile loop plus tests.
4. **Read-only weights mount for jobs**, and job images rewritten to the cache automatically (endpoints have
   both; a multi-node run loads its weights into its own checkpoint path today).
5. **GPU sharing for many small runs** (MIG or MPS). MIG was measured on 2026-10-09: slices scale with their
   SM share, a fully partitioned GPU gives 1 to 5 percent (H100) or 7 to 10 percent (RTX PRO 6000) more
   aggregate throughput; a packing tool, not a speed-up. Test plain multi-process sharing or MPS before
   building either into the platform.
6. **InfiniBand pools that grow automatically.** The cluster autoscaler's simulated node has no DRA attributes
   and never adds an InfiniBand node; `min_nodes` is set by hand before a multi-node run.
7. **Per-tenant GPU quotas in the console, SSO for the operator UIs, a formal security review** before any
   customer launch (README "Security notes and limitations").

## The scaling buffer

`scaling.buffer = N` keeps N ready replicas above the measured demand while a model serves (docs/SCHEDULING.md
"Scaling buffer"). Knative has no additive setting, so the dispatcher holds the live revision's `min-scale` at
demand + N from Knative's own metrics, returns it to the model's minimum at zero demand (scale to zero stays,
the warm spare node covers the first start), and lowers it only after the cooldown. About 120 lines in the
dispatcher plus the API field and the console control. The other headroom knobs remain: `utilization_percent`
(proportional), `min` (fixed floor), `warm_nodes` (a node for the next replica of any model).
