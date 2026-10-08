# API

*For developers who call the platform: the customer API, its modes and its shape.*

The served contract is `services/api/openapi.yaml` (also at `/openapi.json`; a test keeps the two
in sync). Summary:

```
GET    /v1/models[/{model}]          catalog (catalog/models/*.yaml) with live endpoint status
POST   /v1/models/{model}:invoke     body {mode, input, region, priority, timeout_s} -> operation (sync returns the result inline)
GET    /v1/operations[/{id}]         status, attempts, timings, region, cost
GET    /v1/operations/{id}/result    result (async) or presigned artifact links (run)
POST   /v1/operations/{id}:cancel
POST   /v1/operations/{id}:resume    FAILED/CANCELLED run -> new operation on the same checkpoint volume, same region
POST   /v1/artifacts/uploads         presigned upload URL scoped to the tenant prefix
GET    /v1/artifacts/{uri}           presigned download of a tenant object
GET    /v1/endpoints[/{id}]          InferenceServices of catalog models in this region; PATCH scaling (admin key)
GET/POST /v1/keys, DELETE /v1/keys/{alias}, GET /v1/keys/me   LiteLLM keys of the tenant (admin key for writes)
Idempotency-Key header on every mutating call
```

Entry points: `https://api.<control cluster host>` (the fleet API on the control cluster: runs,
operations, keys; sync/async calls are forwarded to the region's API) and `https://api.<region host>`
for in-region sync/async/streaming calls and runs pinned to that region. A host is `<gateway ip>.sslip.io`
or `<cluster>.<domain>` (`docs/EDGE.md`); `./stack.sh output models control` prints the URLs of a fleet. A run submitted to the fleet API is placed in the cheapest free
region among the model's deployments by its GPU preference (`docs/JOBS.md` "Fleet placement"); `region`
pins it. Keys are the same everywhere (one LiteLLM).

Modes: `sync`, `async`, `run`, declared per model; `async` may be requested per call. `async` and
`run` operations are Kubernetes Jobs in the tenant namespace of the region (`docs/JOBS.md`): a run
keeps its checkpoints on a per-operation volume, resumes on its own after a spot interruption and
by `:resume` after a failure or cancel. Endpoints are created and deleted in the repo
(`catalog/models` + `charts/endpoint` through Argo CD), never through the API. Admission limits are not in the API: Kueue bounds concurrent
runs (the fleet's profile queues), LiteLLM bounds sync/async calls per key (`rpm_limit`, `max_budget`), and an
Envoy Gateway global rate limit caps each key (distinct `Authorization` header, counted in Redis
across replicas): 600 requests per minute on the API route, 3000 per minute on the predictor hosts.

MCP (later): one tool per model operation generated from the model's OpenAPI spec; long calls use
the MCP Tasks extension with the same operation id.

## Internal substrate shape (documented, not served)

The internal shape mirrors public `nebius.ai.v1` Endpoint and Job plus the
extensions below. It is what the provider adapter writes into KServe objects and
Kubernetes Jobs, and it is the gap list for the Serverless and Token Factory teams.

Endpoint extensions: `scaling {min_replicas, max_replicas, target_concurrency,
scale_to_zero_after}`, `fast_start {policy}`, `capacity {pool_ref, placement:
RESERVED|SPOT|ANY, max_spot_price, region}`, `routing {hostname, private}`,
`status {replicas_ready, in_flight, last_cold_start, preemption_events}`.

Job extensions: `queue {priority_class, max_queue_seconds}`, `resources
{nodes, gpus_per_node}`, `retry {max_attempts, resume_from_checkpoint,
checkpoint_path}`, `capacity {placement, max_spot_price, region}`, `status
{QUEUED|ADMITTED|RUNNING|PREEMPTED|SUCCEEDED|FAILED, attempts[]}`.

Pool: `platform, preset, gpus_per_node, capacity_type RESERVED|SPOT,
min_nodes, max_nodes, reservation_ids[], pricing_policy_id, region`.
