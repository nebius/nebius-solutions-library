# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`, `ui`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/endpoint`, `charts/fleet`, `charts/tenant`;
`version` in each `Chart.yaml`). One line per version, newest first; the current versions are in `README.md`
"Versions". Dates are the day the version landed on the branch.

## 2026-10-11

- api 0.11.14: endpoints rendered natively (`services/api/render_endpoint.py`) instead of `helm template charts/endpoint` at runtime; helm and the chart leave the API image. `charts/endpoint` stays one release as the reference rendering, with golden tests (`tests/golden/endpoint`) holding the renderer and the chart equal.
- api 0.11.11: version bump of the image after the integration-gate changes below, no code change.
- api 0.11.10, jobs 0.1.14: integration gate: one continuation per `Idempotency-Key` on resume; a superseded handle stays readable and resolves to its successor, also after the Job expired; a completed run without a delivery record is `FINALIZING` then `EXPORT_FAILED`, never `SUCCEEDED`; the uploader keeps the work volume until the export (or `RESULT.json`) and the API's acknowledgement are durable; live checkpoint pointers (`CHECKPOINT.json`) recorded and fenced through the uploader and used by a resume without an explicit reference.
- api 0.11.9, ui 0.6.2: an API key's budget, allowed models and expiry can be edited (`PATCH /v1/keys/{alias}`, console).
- api 0.11.8: attempt records of a resumed or exported run join their root operation; token reviews skip the manager's own cluster.
- api 0.11.7: a finalizing pod counts as a running operation; a delivered export retry closes the original.
- jobs 0.1.13: uploader: the status survives the exclude expansion, no bucket calls in record mode; the gateway routes the attempt-records endpoint.
- api 0.11.6, jobs 0.1.12: attempt lifecycle and finalization: attempts in the fleet database written by the uploader with its pod-bound token, epoch fencing, lifecycle fields on operations; `SUCCEEDED` needs a verified export or the application's finalization record (export retry without recompute); fallback to the next GPU class after the wait bound; resume from a portable checkpoint on another class with its image; pinned model revision; queue reasons; per-class images for API-defined job classes.
- tenant chart 0.3.0: execution options for runs: workload identities (ServiceAccounts with projected tokens), execution profiles with their own egress policy, the fleet's API hosts reachable from every profile; the runner's Kubernetes token reaches the uploader only; tenants without a bucket.
- api 0.11.5, jobs 0.1.11, tenant chart 0.2.4: review round: non-root API image, verified Helm download, current Alpine base, leftovers removed (dead helpers, legacy runner identity, LocalQueue cut-over step, stale chart schema); a run class without deployments runs in every region of the fleet.
- api 0.11.4, dispatcher 0.2.7, endpoint chart 0.1.2, fleet chart 0.1.3, tenant chart 0.2.3: the fleet's lender queue admits the platform's endpoints; the inert `borrowWithinCohort` removed under fair sharing; multi-node endpoints reuse the InferenceService's container, probe and volume helpers, honour pod metadata and get a liveness probe; idle shutdown respects a concurrent write; one helper renders the tenant queues on workers and manager.
- fleet chart 0.1.2, tenant chart 0.2.2: tenants get their own Kueue queues with weighted fair sharing, a per-pool GPU cap and dedicated pools; shared data mount for runs (`job.data`); LiteLLM router settings for multi-region models.

## 2026-10-10

- api 0.11.3, ui 0.6.1: model definitions carry an example request (console "Test request" tab, `example` on `GET /v1/models` and `/v1/endpoints`).
- api 0.11.2: monitoring an operation whose region left the fleet answers 410 with the explanation instead of a connection error.
- api 0.11.1: multi-node endpoints: LeaderWorkerSet reads passed namespace and plural in the wrong order (Forbidden).
- api 0.11.0, ui 0.6.0: multi-node endpoints: one model served from N whole nodes (LeaderWorkerSet behind a predictor-named Service and route), started and stopped explicitly, idle shutdown; start-up time of every endpoint in the API, console and dashboard; endpoint placement takes the smallest fitting preset before on-demand.
- endpoint chart 0.1.1: administrator pod metadata (`podMetadata`) carried to the generated workloads.
- api 0.10.12, ui 0.5.4: Fleet page lists the clusters (region, project, roles, Kubernetes version, nodes, API and Grafana links).
- api 0.10.11: fleet CA rotation is self-healing: regional certificates re-issued, LiteLLM restarted, the API refreshes its trust file.
- api 0.10.10: the GPU regions' models certificates use the regions' issuer, not the control cluster's.
- api 0.10.9, ui 0.5.3: Fleet page in the console: pools per region with ready nodes and GPUs in use (`GET /v1/fleet?live=true`).
- api 0.10.8: GPU regions' gateways carry fleet-CA certificates; ACME stays on the control cluster.
- api 0.10.7: the console edits every endpoint defined through the API (the owner comes from the model record).
- api 0.10.6: operations list and lookup skip an unreachable region instead of failing every request.
- api 0.10.5, ui 0.5.2: metrics of a finished run: the console picks the range that covers it; the KServe drain tolerates absent kinds.
- api 0.10.4: metrics and logs of a completed run are served by the regional API after the worker's Job is gone.

## 2026-10-09

- dispatcher 0.2.6: scaling buffer: a floor is lowered only after demand has stayed low for a whole cooldown.
- api 0.10.3, dispatcher 0.2.5: scaling buffer reads from the model copy (no revision rolls); raw metrics call fixed for the Kubernetes client 36.
- dispatcher 0.2.4: the image ships `buffer.py`.
- api 0.10.2, dispatcher 0.2.3, ui 0.5.1: scaling buffer: ready replicas above demand (`scaling.buffer`), held by the dispatcher on the live revision.
- fleet chart 0.1.1: queue order by the cost of the run.
- dispatcher 0.2.2: a small run does not boot a whole node (smallest preset first).
- api 0.10.1, dispatcher 0.2.1, jobs 0.1.10, tenant chart 0.2.1: large models on spot: fixes from the 8-GPU endpoint and 16-GPU InfiniBand tests.
- api 0.10.0, dispatcher 0.2.0, jobs 0.1.9, ops 0.1.11: refactor around shared components and durable state.
- api 0.9.6, dispatcher 0.1.9, jobs 0.1.8, ui 0.5.0: review response: no hardcoded fleet literals, security notes in the solution.
- api 0.9.3: Helm 3.22.0 in the API image; the CPU-endpoint test checks the rendered object.
- api 0.9.2, dispatcher 0.1.8, jobs 0.1.7: model-agnostic platform: models are defined through the API and stored in a managed database.

## 2026-10-08

- api 0.8.2, dispatcher 0.1.7, ops 0.1.10, ui 0.2.3: base-image security updates; Terraform 1.11 evaluation of null checks.
- api 0.8.1, dispatcher 0.1.6, jobs 0.1.4, ops 0.1.9, ui 0.2.2, endpoint chart 0.1.0, fleet chart 0.1.0, tenant chart 0.2.0: first version of the solution in the library.
