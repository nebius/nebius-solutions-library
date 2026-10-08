# Model catalog and endpoint onboarding

One file per model in `catalog/models/<id>.yaml`, one schema for endpoints and
run classes (the API reads this directory; `services/api/catalog.py`).
Endpoint entries (a `runtime` block) are deployed straight from this directory:
the models stage of the Terraform solution (`stack/models`), or the `models`
ApplicationSet of the reference fleet, renders `charts/endpoint` in catalog mode
with the entry as its values file, once per cluster named in its
`deployments` (and not `paused`). Nothing is rendered into the repository. Run-class
entries (`mode: run`, no runtime) carry a `job` block (image, command, GPUs,
CPU, memory, work volume size, `scratch: network | local-nvme`; `{{name}}` placeholders) that the API renders into
one Kubernetes Job per operation (`docs/JOBS.md`), list the `parameters` the UI
offers, and carry `deployments.<cluster>.parameters` (pool, image, tuning) plus
`price_per_gpu_hour` for billing. `container-run` is the generic class (any
image and command), `gromacs` the reference, `hello-run` the CPU smoke test.

## Catalog entry

```yaml
id: <dns-name>                 # InferenceService name, namespace `models`
displayName: ...
task: speech-to-text | text-generation | molecular-docking | ...
source: { repository, revision, license }
mode: sync | async | run       # sync = request/response or stream over the endpoint; async = endpoint-call Job; run = Kubernetes Job + Kueue (job block, docs/JOBS.md)
protocol: http | websocket | openai | grpc
port: 8000
endpoints: { ...: /path }      # what a client calls; documented, not enforced
gpu: { count: 1, memoryGiB: 8, classes: [h100, rtx-pro-6000] }   # run classes: classes the image runs on, preferred first (profile prefer-<first>)
concurrency: 1                 # sessions/requests one replica handles at once
coldStartClass: fast | medium | slow     # measured, see spikes/S10-endpoints/RESULT.md
price: { unit: call | audio-minute | 1k-tokens, usd: 0.05 }
scaleToZero: true
runtime:                       # charts/endpoint values (see charts/endpoint/values.yaml)
  image: <registry>/<repo>@sha256:...
  command: [...]; args: [...]; env: [...]
  resources: { gpu: 1, cpu: "4", memory: 16Gi }
  scaling: { minReplicas: 0, maxReplicas: 1, target: 1, containerConcurrency: 1, scaleToZeroRetention: 2m }
  timeout: 600                 # Knative revision timeout; cluster max 3600
  shm: { enabled: true, size: 1Gi }
  volumes: [{ name: cache, mountPath: /cache, sizeLimit: 8Gi }]
  weights:                     # optional: weights on the region's shared filesystem (PVC models/weights-shared, subPath <path>)
    sharedFilesystem: { enabled: true, path: <id>, mountPath: /weights }
    env: { HF_HOME: /weights } # or NIM_CACHE_PATH, whatever the runtime reads; charts/endpoint/README.md
                               # hub only until the eu-south1 filesystem quota (compute.filesystem.size.network-ssd)
                               # is granted: eu-south1 has no `weights-shared` claim, the pod would stay Pending
  readinessProbe: { path: /readyz }
deployments:                   # one key per cluster directory; values override `runtime`
  hub:       { pool: h100-spot-1x, priorityClassName: serverless2-interactive }
  eu-south1: { pool: rtx6000-spot-1x }   # same image everywhere: registry.serverless2.local/<alias>/<path> (docs/IMAGES.md)
```

## Steps

1. Find the exact image, port, protocol, health path, resource needs and the
   user the container runs as (`crane config <image>`). Private images: pull
   secret in namespace `models` (NGC: `ngc`, created from
   `~/.config/fs2/nvidia-api-key` with username `$oauthtoken`; not in git).
2. Images must live in a registry the region's nodes can pull from. Nodes only
   pull from registries in their own project: mirror into the regional registry
   (`nebius registry create --parent-id <project> --name serverless2-models`,
   `crane auth login cr.<region>.nebius.cloud -u iam -p "$(nebius iam get-access-token)"`,
   `regctl registry set cr.<region>.nebius.cloud --blob-chunk 67108864 --blob-max 268435456`,
   `regctl image copy --platform linux/amd64 <src>@sha256:... cr.<region>.nebius.cloud/<registry-id-without-prefix>/<repo>:<tag>`)
   and put the regional reference in `deployments.<cluster>.image`.
   `crane copy` fails on multi-GiB layers (monolithic PATCH, "unexpected EOF");
   regctl with 64 MiB chunks does ~47 MiB/s. The IAM token expires after
   about 40 minutes: log in again and rerun (pushed blobs are skipped).
   Set `deployments.<cluster>.paused: true` to prune a model from a cluster
   without deleting its entry.
3. Write the catalog entry; keep `maxReplicas` 1 to 2 (the GPU pools are shared)
   and `minReplicas: 0` unless the measured cold start is unacceptable for the
   class. Requests may only use `nvidia.com/gpu`, `cpu`, `memory`
   (the Kueue ClusterQueues cover nothing else).
4. Check the rendering locally: `helm template <id> charts/endpoint -f catalog/models/<id>.yaml
   --set cluster=<cluster> | kubectl apply --dry-run=server -f -`; re-render the
   pre-pull DaemonSets (`python3 spikes/S15-edge/render.py`), commit, `git pull
   --rebase`, push. Argo CD creates `model-<id>` and syncs within about 3 minutes.
5. Verify through the external gateway: host
   `<id>-predictor.models.<gateway-ip>.sslip.io` (the top-level `<id>.models...` host reported in `status.url`
   has no route in Knative + Gateway API mode). Send one real request from
   zero, one warm; record both in `spikes/S10-endpoints/RESULT.md`.
6. Register the endpoint with LiteLLM (lane D) and set the price.

## Pitfalls (measured)

- `minReplicas` must be in `spec.predictor`; the Knative annotation alone is
  overridden to 1. `initial-scale` is 1, so every new revision starts one pod.
- WebSocket scale-from-zero works: the activator holds the upgrade request
  until the pod is ready (hub: 195 s with a 9 GiB image pull); keep the
  client's open timeout above the cold start.
- vLLM and NeMo need `/dev/shm` as an emptyDir (`shm.enabled`).
- The `kserve/huggingfaceserver:v0.20.0` tag is CPU-only torch; use `-gpu`.
- Containers whose entrypoint is `bash -c script` ignore SIGTERM; Knative sets
  the grace period to the revision `timeout`, so such pods hold their GPU for
  the whole timeout after scale-to-zero (DiffDock NIM: run `start_server`
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
