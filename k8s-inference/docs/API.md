# API

*For developers who call the platform: the customer API, its modes and its shape.*

The served contract is `services/api/openapi.yaml` (also at `/openapi.json`; a test keeps the two
in sync). Summary:

```
GET    /v1/models[/{model}]          the built-in classes (catalog/models) and the models defined through the API, with live endpoint status
POST   /v1/models                    define a model (admin key, fleet API only): a container plus a few knobs -> stored, rendered, registered
PUT    /v1/models/{model}            replace a definition (new version); DELETE /v1/models/{model} removes it (built-in classes: 409)
GET    /v1/models/{model}/history    every version of a definition: who changed what, when (admin key)
POST   /v1/models/{model}:invoke     body {mode, input, region, priority, timeout_s} -> operation (sync returns the result inline)
GET    /v1/operations[/{id}]         status, attempts, timings, region, gpu_class (per-class images), cost
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
by `:resume` after a failure or cancel. Endpoints are created, changed and deleted through the model
API ("Models" below), on the fleet API only. Admission limits are not in the API: Kueue bounds concurrent
runs (the fleet's profile queues), LiteLLM bounds sync/async calls per key (`rpm_limit`, `max_budget`), and an
Envoy Gateway global rate limit caps each key (distinct `Authorization` header, counted in Redis
across replicas): 600 requests per minute on the API route, 3000 per minute on the predictor hosts.

MCP (later): one tool per model operation generated from the model's OpenAPI spec; long calls use
the MCP Tasks extension with the same operation id.

## Models

A model is defined through the API, never in `terraform.tfvars`. `POST /v1/models` (admin key) takes the
shape of the console's "New model" form: a container plus a few knobs.

```json
{"id": "my-llm", "kind": "endpoint",
 "image": "vllm/vllm-openai:v0.11.0", "args": ["--model", "<org>/<model>", "--port", "8000"],
 "env": {"HF_HOME": "/weights"}, "port": 8000, "protocol": "openai", "served_model": "<org>/<model>",
 "gpu": {"count": 1, "classes": ["h100", "l40s"]}, "scaling": {"min": 0, "max": 2, "target": 4},
 "regions": ["eu-north1"], "weights": {"path": "my-llm", "mount_path": "/weights"}}
```

`protocol` is `openai | http | websocket | grpc`; `gpu.classes` lists the classes the image runs on,
preferred first; `regions` defaults to every region with a pool of one of the classes; `kind: job`
defines a run class instead (`cpu`, `memory`, `disk_gi`, `scratch`, `parameters`). The full field list is
the module docstring of `services/api/models.py`; the schema is `Model` in `services/api/openapi.yaml`.

What happens on a write: the definition goes into the fleet database (table `models`: id, kind, spec, the
rendered entry, version, `managed_by`, created/updated by and at) and every write into `models_history`;
the API renders the endpoint on every cluster of the model's regions with `charts/endpoint`, registers an
OpenAI endpoint as a LiteLLM model group whose `api_base` is the endpoint's gateway hostname in that region,
and writes a read-only ConfigMap copy `catalog-<id>` into the `api` namespace of every other region.
`PUT` replaces the definition (version + 1), `DELETE` removes the endpoint everywhere and keeps the last
version in the history, `GET /v1/models/{model}/history` lists the versions (admin key).

**Model groups and the platform-internal key.** A LiteLLM model group calls the endpoint over its public
gateway hostname, and the edge on those hostnames accepts only a valid LiteLLM key. The group therefore
calls as the platform's own key: alias `platform-internal` (metadata `tenant: platform, role: internal`,
no budget, no expiry, every model), created once per fleet by the platform stage (Terraform,
`stack/platform/litellm-internal.tf`) and kept in Secret
`api/litellm-internal`, which the control API reads as `LITELLM_INTERNAL_KEY`. The caller's own key is
checked first: LiteLLM applies its budget, rate limit and spend before it routes to the group, then the edge
sees the internal key, so the per-key rate limit of the edge (`SecurityPolicy model-key-check`) counts
those calls against the platform key, not the caller. Without the Secret the API deploys the endpoint and
registers no group (a warning in the write's response and in the log): the endpoint still answers through
`POST /v1/models/{model}:invoke` and its gateway hostname with any tenant key. The internal key is never
returned by the API or shown in the console.

Writes land on the fleet API only (the control cluster's `api.` host, the one with a database). A regional
API serves the copies and answers `POST`, `PUT`, `DELETE` and `/history` with 409 and the fleet API's URL.
The built-in classes (`hello-run`, `container-run`, `distributed-run`) are files and answer writes with
409 too. `images` (per GPU class), `deployments.<region>.variants` and `routing` are accepted and stored
but not acted on yet (`docs/DESIGN-REVIEW-2026-10-09.md`).

The acceptance probe of the models stage uses this path: with `acceptance.example_endpoint = true` (the
default) it creates `llm-example` with the first admin key, waits for it, calls it once and leaves it as
the first model of the console.

## Multi-node endpoints

An endpoint definition with `nodes` > 1 serves one model from that many whole nodes at once (tensor-parallel inside
a node, pipeline- or expert-parallel across them): the platform renders a LeaderWorkerSet (Kubernetes SIG) instead
of a KServe InferenceService. `gpu.count` is the GPUs per node and must be a whole-node preset of the class
(`8` on H100/H200/B200/B300); `command` runs on the leader, which serves `port`, `worker_command` on the other
nodes; every pod gets `NNODES`, `GPUS_PER_NODE`, `WORLD_SIZE`, `NODE_RANK`, `MASTER_ADDR` (the leader's hostname),
`MASTER_PORT` and `NCCL_SOCKET_IFNAME` next to the LeaderWorkerSet's own `LWS_LEADER_ADDRESS`, `LWS_GROUP_SIZE`
and `LWS_WORKER_INDEX`, so vLLM's multi-node recipe works unchanged:

```json
{"id": "kimi-k3", "kind": "endpoint", "image": "vllm/vllm-openai:v0.31.0", "protocol": "openai", "port": 8000,
 "nodes": 2, "gpu": {"count": 8, "classes": ["b200"]}, "resources": {"cpu": "120", "memory": "1500Gi"}, "shm_gib": 32,
 "command": "bash /vllm-workspace/examples/ray_serving/multi-node-serving.sh leader --ray_cluster_size=$LWS_GROUP_SIZE; vllm serve /weights/... --tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray",
 "worker_command": "bash /vllm-workspace/examples/ray_serving/multi-node-serving.sh worker --ray_address=$LWS_LEADER_ADDRESS",
 "health_path": "/health", "weights": {"path": "kimi-k3", "env": {"HF_HOME": "/weights"}},
 "scaling": {"min": 0, "idle_s": 1800}, "regions": ["us-central1"]}
```

Placement: the pool whose preset has exactly `gpu.count` GPUs per node, reserved before on-demand before spot;
`interconnect` `none` (default) prefers plain pools and runs NCCL over Ethernet, which is right for pipeline
parallelism and lets the pool scale from zero; `preferred` takes an InfiniBand pool when the region has one,
`required` only those, and the pods then claim every fabric NIC through DRA. The cluster autoscaler never adds
InfiniBand nodes, so an InfiniBand multi-node endpoint needs the pool's `min_nodes` set to its node count.

Start and stop, no autoscaling in between: `scaling.min` 1 starts the replica group (the nodes boot, the drivers
install, the weights load: 10-20 minutes for a trillion-parameter model), 0 stops it; the console's Start/Stop button
and `PATCH /v1/endpoints/{id} {"scaling": {"min": 1}}` do that. `scaling.idle_s` stops it after that many seconds
without a request, measured by the control API's reconcile loop from the later of the last authorized request (the
regional API stamps the LeaderWorkerSet) and the moment the leader became Ready; the stop is a normal definition
write (history entry by `idle-shutdown`). A call to a stopped or starting multi-node endpoint gets 503 with that
explanation. The Service and the gateway route of the leader carry a KServe predictor's name
(`<id>-predictor.models.<domain>`), so authorization, the certificate, LiteLLM and `:invoke` are the same as for
a one-pod endpoint.

Start-up time: every endpoint (both kinds) reports `startup_s`, `started_at` and `ready_at` of its newest pod
(`GET /v1/endpoints`): pod created to Ready, which includes a cold node's boot and driver install, the image pull
and the model load; the console shows it as "Last start-up", the Endpoints dashboard as a table by model.

## Internal substrate shape (documented, not served)

The internal shape mirrors public `nebius.ai.v1` Endpoint and Job plus the
extensions below. It is what the provider adapter writes into KServe objects and
Kubernetes Jobs, and it is the gap list for the Serverless and Token Factory teams.

Endpoint extensions: `scaling {buffer (ready replicas above demand, 0-8), min_replicas, max_replicas, target_concurrency,
scale_to_zero_after}`, `fast_start {policy}`, `capacity {pool_ref, placement:
RESERVED|SPOT|ANY, max_spot_price, region}`, `routing {hostname, private}`,
`status {replicas_ready, in_flight, last_cold_start, preemption_events}`.

Job extensions: `queue {priority_class, max_queue_seconds}`, `resources
{nodes, gpus_per_node}`, `retry {max_attempts, resume_from_checkpoint,
checkpoint_path}`, `capacity {placement, max_spot_price, region}`, `status
{QUEUED|ADMITTED|RUNNING|PREEMPTED|SUCCEEDED|FAILED, attempts[]}`.

Pool: `platform, preset, gpus_per_node, capacity_type RESERVED|SPOT,
min_nodes, max_nodes, reservation_ids[], pricing_policy_id, region`.
