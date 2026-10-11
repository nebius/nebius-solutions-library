# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`, `ui`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/endpoint`, `charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). One line per version, newest first; the current versions are in `README.md`
"Versions". Dates are the day the version landed on the branch.

## 2026-10-11

- api 0.11.19: the metrics and logs of another region (API server proxy) call the Kubernetes client's `update_params_for_auth` with its 36.x signature (the fleet answered 500 on every cross-region metrics and logs request).
- api 0.11.18: one API tier. The regional API deployment goes; the control cluster's API serves every region: the regions' gateways authorize endpoint calls against it through an Envoy Gateway `Backend` (its public hostname, `BackendTLSPolicy`), a sync `:invoke` for an endpoint in another region is proxied to the endpoint's gateway hostname with the caller's key, async calls run in the endpoint's region, metrics and logs of a region come through its API server proxy, the runtime catalog lives in the database only (no ConfigMap copies), the token review of an attempt record tries the GPU regions first, a multi-node endpoint's last-request stamp is written in every region of the model; `REGION_API_URLS`, `CONTROL_API_URL`, `/internal/keys/me` and the `:invoke` forwarding are removed (about 450 lines). Measured on the test fleet: the cross-cluster ext-auth adds ~5 ms per call (same region).
- platform chart 0.1.1: the `api` component is the control cluster's only (`clusters/common/apps/api.yaml` placement `control`: a GPU region loses its API Deployment, Service, HTTPRoute, hostname and `api/litellm-master` copy); `edge` renders the `Backend` api/api and its `BackendTLSPolicy` on the regions; the control cluster's https listener carries no hostname and the route `api-authorize` serves `/internal/authorize` under any host (Envoy keeps the original authority on ext-auth); the api-agent gets `services/proxy` on Prometheus and Loki; Envoy Gateway's `extensionApis.enableBackend` is on.
- endpoint chart (0.1.3, the reference rendering): value `apiBackend` renders the SecurityPolicy's ext-auth against the `Backend` api/api; the goldens of the non-local clusters follow.
- api 0.11.17: the control API stops consulting LiteLLM's `/key/info` once its one-time key import succeeded: LiteLLM answers from its cache for a key deleted minutes ago, so a revoked key stayed valid on the API (fleet drill 2026-10-11). The fallback remains for a fleet without the database.
- api 0.11.17: the image cache is an add-on (`images.cache.enabled`): with an empty `IMAGES_HOST` the API leaves image references as written and the weights-prepare image is Docker Hub's.
- platform chart 0.1.0: the platform's own manifests (`clusters/common/manifests` until now) as one chart with values in place of the platform stage's per-cluster JSON patches; the render is held equal to the objects the patches produced (`stack/platform/tests/golden`, `make check`), so a live cluster sees no change; the kustomization provider leaves the platform stage.
- tenant chart 0.3.2: rendered by the platform stage (the models stage is folded into it; tenant buckets are cloud-stage resources; the acceptance probe is `tools/verify.sh`); no template change.
- api 0.11.16: a model's own `env` entry replaces the platform default of the same name in a multi-node endpoint (kimi-k3 sets `NCCL_IB_DISABLE` itself; server-side apply refused the duplicate); a multi-node run's delivery is rank 0's record (the other ranks record no export).
- api 0.11.16: one auth layer: API keys, budgets, spend and the GPU charge ledger live in the fleet database (`keys`, `charges`, migration 5, `services/api/keys.py`); every API checks keys there (a regional API through the fleet API's `GET /internal/keys/me`), LiteLLM `/key/info` only as a read-only fallback until the one-time import at the control API's first start; billing writes the platform ledger, never LiteLLM's tables (the pinned-schema contract is gone). LiteLLM stays the OpenAI router: tenant keys are mirrored into it (same value), its model groups call the gateway with the fleet's router key (role `router`, Secret `api/router-key`, `stack/platform/router-key.tf`) instead of the platform-internal key and the second budget check; the models stage writes the tenants' keys with `python -m keys bootstrap` Jobs instead of LiteLLM key Jobs. Key management is on the fleet API.
- api 0.11.15, dispatcher 0.2.9, ui 0.6.5: the dispatcher reduced to its core: the ready-replica buffer (`scaling.buffer`, `buffer.py`, the console control, the Knative RBAC of the manager's worker identity) and the rank endpoint (`GET /v1/rank`, the dispatcher Service, `DISPATCHER_URL`) are gone; the API chooses a per-class-image run's GPU class from the fleet document in the dispatcher's order (`services/api/placement.py`), without the free-quota signal; a stored `scaling.buffer` is ignored on read.
- api 0.11.15: endpoints rendered natively (`services/api/render_endpoint.py`) instead of `helm template charts/endpoint` at runtime; helm and the chart leave the API image. `charts/endpoint` stays one release as the reference rendering, with golden tests (`tests/golden/endpoint`) holding the renderer and the chart equal.
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
