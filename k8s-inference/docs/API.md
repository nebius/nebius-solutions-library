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
GET/POST /v1/keys, PATCH/DELETE /v1/keys/{alias}, GET /v1/keys/me   LiteLLM keys of the tenant (admin key for writes; PATCH changes budget, allowed models, expiry: the console's Edit button)
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
 "gpu": {"count": 1, "classes": ["h100", "l40s"]}, "scaling": {"min": 0, "max": 2, "container_concurrency": 4},
 "regions": ["eu-north1"], "weights": {"path": "my-llm", "mount_path": "/weights"}}
```

`protocol` is `openai | http | websocket | grpc`; `gpu.classes` lists the classes the image runs on,
preferred first; `regions` defaults to every region with a pool of one of the classes; `kind: job`
defines a run class instead (`cpu`, `memory`, `disk_gi`, `scratch`, `parameters`). The full field list is
the module docstring of `services/api/models.py`; the schema is `Model` in `services/api/openapi.yaml`.

### Scaling

`scaling` is one vocabulary (`ScalingSpec` in `openapi.yaml`), the same on a model definition, on
`GET /v1/endpoints` and on `PATCH /v1/endpoints/{id}`: `min` and `max` replicas per region;
`metric: concurrency_utilization` (the default) scales on `target` percent (1-100, default 100) of
`container_concurrency`, the hard per-replica request limit (1-1000, default 1); `metric: requests_per_second`
scales on `target` requests/s per replica (default 5) with no concurrency limit; `cooldown_s` delays scale-down,
`window_s` (6-300, default 30) is the metrics averaging window, `idle_s` keeps the last replica after the decision
to scale to zero. The API writes the complete
policy back, so a read never has to guess a default. Rendering onto Knative: `concurrency_utilization` is the
`concurrency` metric with `container_concurrency` as the scale target and `target` as its utilization percentage;
`requests_per_second` is the `rps` metric at 100 % with `containerConcurrency` 0. Definitions stored under the raw
Knative names the API accepted before 0.11.12 (`metric: concurrency | rps`, `utilization_percent`) are read in this
vocabulary (`services/api/models.py` `native_scaling`).

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
but not acted on yet (the design review of 2026-10-09).

The acceptance probe of the models stage uses this path: with `acceptance.example_endpoint = true` (the
default) it creates `llm-example` with the first admin key, waits for it, calls it once and leaves it as
the first model of the console.

Run classes may declare `data: {"path": "refdb/uniref", "mount_path": "/data", "readonly": true}`: a directory of
the tenant's area on the region's shared filesystem mounted into every run of the class (docs/JOBS.md "Shared data").

### Loading large weights

vLLM's default loader reads one safetensors shard at a time, about 170 MB/s from the shared filesystem; its
Run:ai streamer loads the same files with many streams at about 5 GB/s (806 GB in 2.6 min on a B300 node,
2026-10-10). Put `--load-format runai_streamer --model-loader-extra-config '{"concurrency": 32}'` into the command
of any vLLM endpoint whose weights live on the filesystem, and `VLLM_CACHE_ROOT=/weights/vllm-cache` in its env so
the torch.compile cache survives a restart (the first start of a trillion-parameter model spends about 25 min there).

## Variants

One endpoint may run as several deployments in a region behind its one name, in priority order: a warm floor on
reserved GPUs and scale-to-zero overflow on spot, a cheaper class for budget requests. In the model definition:

```json
{"id": "chat", "kind": "endpoint", "protocol": "openai", "image": "vllm/vllm-openai:v0.31.0",
 "images": {"l40s": "vllm/vllm-openai:v0.31.0-l40s"},
 "gpu": {"count": 1, "classes": ["h100", "l40s"]},
 "deployments": {"eu-north1": {"variants": [
     {"name": "reserved", "pool": "h100-reserved-1x", "min": 1, "max": 2},
     {"name": "spot",     "pool": "h100-spot-1x",     "min": 0, "max": 4},
     {"name": "cheap",    "pool": "l40s-spot-1x",     "min": 0, "max": 2}]}},
 "routing": {"field": "tier", "map": {"fast": "reserved", "budget": "cheap"}, "default": "priority"}}
```

Each variant is its own Knative service: the first keeps the model's name (`chat`), the others are
`<id>-<variant>` (`chat-spot`, `chat-cheap`), each with its pool, replica bounds and image (`variant.image`, else
`images.<class of the pool>`, else the model's image). All of them carry the model's labels, hostname certificate
and authorizer. A request through `:invoke` goes to the variant that `routing.field` selects (`map` maps the field's
value, dotted paths allowed), else to the `default` variant, else (`priority`) to the first variant with a ready
replica, else to the first one. Through LiteLLM the model group lists every variant of every cluster as a deployment
with its priority (`order`), so the OpenAI route tries the warm floor before the overflow. `GET /v1/endpoints` reports
`variants` (name, object id, pool, bounds, image, live state) and `routing`. `images` alone (no variants) gives a
plain deployment the image of its pool's GPU class. Regions named under `deployments` define where the endpoint
runs; a region without variants keeps the automatic pool choice.

## Bindings and execution options (runs)

`PUT /v1/bindings/{ref}` `{"data": {"<key>": "<value>", ...}}` (at most 64 KiB) stores a confidential input of the
tenant; `GET /v1/bindings` lists them (reference, bound operation, keys; never the data); `DELETE /v1/bindings/{ref}`
revokes one. A run names a binding as `execution.private_input_ref`; the other fields of the `execution` block
(`io_mode`, `profile_ref`, `workload_identity_ref`, `placement`, `recovery`) are described in docs/JOBS.md
"Execution options". `GET /v1/fleet` carries `execution` (capability discovery: io modes, the tenant's identities and
profiles, the regions' OIDC issuers). `POST /v1/operations/{id}:resume` takes an optional body
`{"checkpoint_ref": "...", "gpu_class": "..."}`. Operations report their resolved block as `execution` and their lifecycle
(`root_operation_id`, `attempt_id`, `epoch`, `revision`, `cluster`, `image_digest`, `export_state`, `result_ref`,
`checkpoint_ref`; docs/JOBS.md "Attempts and finalization"); `GET /v1/operations/{id}?since=<revision>` answers 304
while nothing changed; `POST /v1/operations/{id}:export` retries a failed export without rerunning the simulation;
`POST /v1/operations/{id}:resume` with an `Idempotency-Key` header replays one continuation; a handle that capacity
fallback superseded resolves to its successor (`requested_id` names the handle used).

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

The example above is vLLM's Ray-based pipeline-parallel recipe; the stock `vllm/vllm-openai` image ships no Ray,
so without a custom image use vLLM's Ray-free data-parallel mode instead (one full copy of the model per node,
tensor-parallel inside the node; the leader serves, the workers run `--headless`): leader
`vllm serve ... --tensor-parallel-size 8 --data-parallel-size 2 --data-parallel-size-local 1 --data-parallel-address $(hostname -i) --data-parallel-rpc-port 13345 --port 8000`,
workers `... --data-parallel-address $LWS_LEADER_ADDRESS --data-parallel-start-rank $LWS_WORKER_INDEX --headless`
(verified with Kimi K3 on two B300 nodes, 2026-10-10). Over Ethernet the pods get `NCCL_IB_DISABLE=1`: the nodes
carry Mellanox NICs the pod does not hold, and NCCL's InfiniBand transport fails with "unhandled system error" otherwise.

Placement: the pool whose preset has exactly `gpu.count` GPUs per node, reserved before on-demand before spot;
`interconnect` `none` (default) prefers plain pools and runs NCCL over Ethernet, which is right for pipeline
parallelism and lets the pool scale from zero; `preferred` takes an InfiniBand pool when the region has one,
`required` only those, and the pods then claim every fabric NIC through DRA. The cluster autoscaler never adds
InfiniBand nodes; the dispatcher raises the pool's floor to the endpoint's node count while it is started and
restores `min_nodes` after it is stopped (docs/SCHEDULING.md "InfiniBand pools grow on demand"), so the nodes
boot after `scaling.min` goes to 1 and a start takes the usual few minutes longer than on a pool with nodes up.

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
