# Model catalog and endpoint onboarding

One file per model in `catalog/models/<id>.yaml`, one schema for endpoints and
run classes (the API reads this directory; `services/api/catalog.py`).
Endpoints are defined through the API (`POST /v1/models`), which renders the endpoint's objects (`services/api/render_endpoint.py`) with the
entry as its values file, once per cluster named in its `deployments` (and not `paused`); this directory
holds the built-in run classes only. Nothing is rendered into the repository. Run-class
entries (`mode: run`, no runtime) carry a `job` block (image, or `images` with one image per GPU
class, command, GPUs, CPU, memory, work volume size, `scratch: network | local-nvme`; `{{name}}`
placeholders; `docs/SCHEDULING.md` "Per-GPU images for runs") that the API renders into
one Kubernetes Job per operation (`docs/JOBS.md`), list the `parameters` a client
offers, and carry `deployments.<cluster>.parameters` (pool, image, tuning) plus
`price_per_gpu_hour` for billing. `container-run` is the generic single-node class (any image and command),
`distributed-run` the multi-node one, `hello-run` the CPU smoke test. The platform ships only these;
models are runtime input through `POST /v1/models` (README.md "Day 2"; nothing about a model is in `terraform.tfvars`).
An application layer (a scientific-AI product, a chat product, a team's fine-tuning) brings its own models
through that API; none is part of the platform.

## Catalog entry

The entry below is the shape of the built-in run classes (the `job` block `services/api/jobs.py` renders), written
by hand in this directory. Endpoints have no entry: a client writes the definition of docs/API.md "Models" (its
`scaling` vocabulary is docs/API.md "Scaling"), the API stores that spec with the placement it resolves
(`services/api/models.py` `definition`) and `services/api/render_endpoint.py` renders the objects from the spec
itself; the endpoint fixtures of `services/api/tests/catalog` are such specs with their placement under `deployments`.

```yaml
id: <dns-name>
displayName: ...
description: ...
task: batch | smoke-test | ...
mode: run                      # Kubernetes Job + Kueue (job block, docs/JOBS.md)
protocol: kubernetes-job
gpu: { count: 1, classes: [h100, rtx-pro-6000] }   # classes the image runs on, preferred first (profile prefer-<first>)
price: { unit: gpu-hour | call, usd: 1.60 }        # display; billing uses deployments.<cluster>.price_per_gpu_hour
job:                           # docs/JOBS.md "Run class"
  image: <registry>/<repo>@sha256:...              # or `images: { default: ..., <class>: ... }` (docs/SCHEDULING.md)
  command: [...]               # `{{name}}` placeholders from `parameters`
  gpu: 1
  cpu: "12"
  memory: 32Gi
  pvcSizeGi: 50
  graceSeconds: 300
  scratch: network | local-nvme
parameters:
  - { name: nsteps, type: number, label: Steps, default: 1000 }
deployments:                   # one key per cluster id ("hub" = this API's region); absent = every region of the fleet
  hub:       { parameters: { threads: "16" }, price_per_gpu_hour: 2.15 }
  eu-south1: { parameters: { threads: "16" }, price_per_gpu_hour: 1.60, paused: false }
```

## Steps

1. Find the exact image, port, protocol, health path, resource needs and the
   user the container runs as (`crane config <image>`). Private images: pull
   secret in namespace `models` (NGC: `ngc`, created from your NGC API key
   with username `$oauthtoken`; the key stays outside git).
2. Reference the image through the fleet's logical registry host
   (`registry.serverless2.local/<alias>/<path>`, `docs/IMAGES.md`): every cluster's cache fetches it from the
   source registry or an upstream on first pull, nothing is mirrored per region.
   Set `deployments.<cluster>.paused: true` to prune a model from a cluster
   without deleting its entry.
3. Write the catalog entry; keep `maxReplicas` 1 to 2 (the GPU pools are shared)
   and `minReplicas: 0` unless the measured cold start is unacceptable for the
   class. Requests may only use `nvidia.com/gpu`, `cpu`, `memory`
   (the Kueue ClusterQueues cover nothing else).
4. Check rendering locally with `.venv-api/bin/python services/api/models.py render <cluster> < catalog/models/<id>.yaml` (the API's renderer, `services/api/render_endpoint.py`), then `make check`. The built-in classes are read at API start; a changed file reaches a fleet with the next API image.
5. Models are created with `POST /v1/models` (admin key). The API stores desired state and history before rendering the objects on each cluster and retries a failed cluster; there is no controller watching Git. Use `env_secrets` for credentials and `If-Match` for guarded updates.
6. Verify a cold and warm request through the predictor's external HTTPS host with a permitted key. OpenAI API-managed models register their LiteLLM group during reconciliation. See `docs/EDGE.md` and `docs/OPERATIONS.md`.

## Pitfalls (measured)

- `minReplicas` must be in `spec.predictor`; the Knative annotation alone is
  overridden to 1. `initial-scale` is 1, so every new revision starts one pod.
- WebSocket scale-from-zero works: the activator holds the upgrade request
  until the pod is ready (195 s with a 9 GiB image pull); keep the
  client's open timeout above the cold start.
- vLLM and NeMo need `/dev/shm` as an emptyDir (`shm.enabled`).
- The `kserve/huggingfaceserver:v0.20.0` tag is CPU-only torch; use `-gpu`.
- Containers whose entrypoint is `bash -c script` ignore SIGTERM; Knative sets
  the grace period to the revision `timeout`, so such pods hold their GPU for
  the whole timeout after scale-to-zero (NIM images: run their server
  as PID 1 via `command`). Keep `timeout` as low as the longest request allows.
- Every new revision starts one pod while the old one is still up; on 1-GPU
  spot nodes roll out spec changes while the model is at zero, and delete a
  first revision that never became ready (`kubectl delete revision`).
- Pods in `models` get `kueue.x-k8s.io/priority-class: customer-batch` and (hub)
  `priorityClassName: serverless2-interactive`, so an endpoint preempts
  bulk-backfill runs on a full pool.

## GPU placement and per-GPU images

`docs/SCHEDULING.md` has the full picture. In the `runtime` block:

- `queue.name: <profile>` (a LocalQueue in `models`, e.g. `prefer-h100`,
  `cheapest-any`): replicas are admitted by Kueue, which picks the first pool
  of the profile with free quota and injects its nodeSelector; `pool` is then
  not rendered. The `image` must run on every pool the profile allows.
- no `queue.name`: `pool` pins the replicas as before. `images: {<pool>:
  <image>}` then selects a pool-specific build at render time; `image` is the
  fallback for pools not listed.
- `images` has no effect on Kueue-placed endpoints: the pool is only known
  after admission and Kueue rejects a pod whose image changes afterwards
  (measured, see `docs/SCHEDULING.md`).
