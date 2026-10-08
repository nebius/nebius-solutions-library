# services/api: the customer API

FastAPI service implementing `docs/API.md` (contract: `openapi.yaml`, also served at `/openapi.json`;
`test_openapi_paths_match_served_routes` keeps them equal). About 1000 lines: `app.py` (routes),
`auth.py` (LiteLLM key -> tenant), `catalog.py` (model catalog), `jobs.py` (operations as Kubernetes
Jobs: render, submit, status, cancel, resume; `docs/JOBS.md`), `kube.py` (per-region clients, KServe
reads), `artifacts.py` (presigned URLs and small reads in the tenant bucket), `endpoints.py` (read/scale
InferenceServices), `billing.py` (run-class spend, run as a CronJob), `resilience.py` (retries).
The runner image the Jobs use (fetch, upload, endpoint call) is `services/jobs`.

| Request | What happens |
|---|---|
| `Authorization: Bearer <key>` | `GET LiteLLM /key/info` with the master key (cached 30 s); `metadata.tenant` names the tenant, namespace `tenant-<name>`. Blocked, expired, budget-exhausted keys and model allow-lists are enforced |
| `POST /v1/models/{m}:invoke` mode `sync` | proxied to LiteLLM's pass-through route (`litellm_route`, spend + budget + `cost_per_request` recorded on the caller's key) or straight to the KServe predictor host; returns `{result, operation}` |
| mode `async` | a one-pod Job in the tenant namespace whose main container (runner image, `call.sh`) POSTs `input` to the same route with the caller's key from a per-operation Secret owned by the Job, retrying until the endpoint answers; the uploader puts the response in the bucket and `GET .../result` returns it inline |
| `region` (sync/async) | this API's region, else forwarded to that region's API when `REGION_API_URLS` names it (`<region>=https://api.<ip>.sslip.io,...`; the control cluster's API: same key, same body, answer returned as is), else 400. `HUB_REGION` (default `REGION`) is the region the catalog's `deployments.hub` means (the control cluster sets `eu-north1`) |
| `region` (run mode) | regional API: its own region (the Job is created in its cluster). Fleet API (`FLEET_MANAGER=true`, the control cluster): optional; the Job is created on the MultiKueue manager and placed by Kueue + the dispatcher among the model's regions (`deployments` keys) and GPU classes; a run whose images live in the per-region registries is placed before rendering through the dispatcher's `GET /v1/rank` (`DISPATCHER_URL`) and pinned (`serverless2.nebius/region`); inputs and outputs use the tenant's hub-region bucket. The worker's pods are read through the per-region kube clients (`REGION_KUBECONFIGS`, Secret `api/region-kubeconfigs` from the `api-agent` ServiceAccount tokens). `docs/JOBS.md` "Fleet placement" |
| mode `run` | the catalog entry's `job` block is rendered (`{{param}}`) into a Job in the tenant namespace: fetch init container, `main`, uploader, a per-run PVC for `/work`; run-pod SA (`job-runner`), Kueue labels (`queue-name: <profile>` = `prefer-<first gpu class>` or `default`, priority `customer-batch`, `bulk-backfill` for `priority: low`), a nodeAffinity over the fleet pools of the entry's GPU classes and the region's registry prefix on every image (both from `kueue-system/fleet-prices`), parameters from `input` (unknown or missing required names -> 400), `activeDeadlineSeconds` from `timeout_s`, `podFailurePolicy` so preemptions do not consume `backoffLimit` (`docs/JOBS.md`) |
| `Idempotency-Key` | operation id = `op-<sha256(tenant, model, key)[:16]>`; `AlreadyExists` -> 200 with the existing operation |
| `GET /v1/operations[/{id}]` | Job conditions + pods -> QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED; one attempt per pod (`DisruptionTarget` or exit 137/143 -> PREEMPTED); `logs_url` into Grafana Explore/Loki (`GRAFANA_URLS`); `resumed_from`, `resumable` |
| `GET /v1/operations/{id}/result` | 409 while running; async: `out/response.json` from the bucket; run: presigned links to `operations/<id>/...` in the tenant bucket |
| `POST /v1/operations/{id}:cancel` | queued: Job deleted (PVC and Secret follow by owner reference); running: `activeDeadlineSeconds: 1` + `cancelled` annotation, reported CANCELLED |
| `POST /v1/operations/{id}:resume` | FAILED/CANCELLED run with its PVC still present: Job `<id>-r<n>` with the original spec on the same volume in the same region; 409 otherwise |
| `GET /v1/endpoints[/{id}]`, `PATCH /v1/endpoints/{id}` | InferenceServices of catalog models in `models`; PATCH (admin keys) sets min/max replicas, target concurrency, scale-to-zero retention and is reverted by Argo CD for git-managed endpoints. Create/delete: `catalog/models` + `charts/endpoint` in the repo |
| `GET/POST /v1/keys`, `DELETE /v1/keys/{alias}` | LiteLLM keys of the caller's tenant (alias prefixed `<tenant>-`, pass-through routes copied from the admin key). Admin keys only (`metadata.role=admin`) |
| `POST /v1/artifacts/uploads`, `GET /v1/artifacts/{uri}` | presigned PUT to `s3://serverless2-<tenant>-<region>/uploads/<id>/<file>` / GET of a tenant object (credentials: secret `tenant-storage` in the tenant namespace) |

Catalog: `catalog/models/*.yaml`, one schema for endpoints and run classes (`catalog/README.md`):
`id, displayName, mode, protocol, port, endpoints{...}, gpu{count,classes}, price{unit,usd},
runtime{...}` for endpoints, `job{image,command,gpu,...}` + `parameters[]` for run classes,
`deployments{<cluster>: {pool, image, priorityClassName, parameters, price_per_gpu_hour}}` (`hub`
= this API's region). Loaded from `CATALOG_DIRS` (default `/app/catalog/models`, copied into the image).

Admission and limits are not in the API: Kueue bounds concurrent runs (profile ClusterQueues of the
fleet, `charts/fleet`; tenants share them through their LocalQueues), LiteLLM `rpm_limit`/`max_budget` bound sync and async calls per key, the Envoy
Gateway `BackendTrafficPolicy api/api-rate-limit` caps the route (local rate limit, 600 req/min per
gateway replica). Every Kubernetes call is retried (3 attempts, 0.2/0.6 s) with a 15 s timeout;
LiteLLM and S3 calls likewise (`resilience.py`).

Billing (`billing.py`): the CronJob `api/billing` (every 2 min, `python -m billing`, same image and
identity as the API, on the control cluster) bills every finished unbilled run once (a fleet-placed
run: its copy on the worker, with that region's price; the manager's Job is annotated too): GPU-seconds (main container running time
x GPUs, summed over the Job's pods, preempted attempts included) x `deployments.<region>.price_per_gpu_hour`,
added to the submitting key's spend via LiteLLM `/key/update` (the key's sha256 is on the Job, never
the key); the `billed` annotation makes the pass idempotent. LiteLLM has no spend-increment call, so `add_spend` is read-modify-write: a
pass-through call landing inside that round trip loses its increment (accepted).

RBAC: no cluster-wide API ClusterRole; per tenant namespace a RoleBinding to `serverless2-api-tenant`
(rendered by `charts/tenant`: Jobs, pods, PVCs, Secret creation, Kueue Workloads read), a scale-only Role
in `models`, a read of `kueue-system/fleet-prices`, one ClusterRole to list tenant namespaces (billing).

Deploy: image `<fleet.yaml images.source>/serverless2/api:<tag>` built from the repo root
(`docker buildx build --push -f services/api/Dockerfile .`), referenced as
`registry.serverless2.local/nebius/serverless2/api:<tag>` (docs/IMAGES.md); runner image
`serverless2/jobs:<tag>` from `services/jobs` the same way (`RUNNER_IMAGE`). Manifests in
`clusters/common/manifests/api` + per-cluster overlays (Argo CD app `api`: Deployment, CronJob,
HTTPRoute, rate limit, RBAC), host from `PUBLIC_API_URL`. Secret `api/litellm-master` comes from
`python -m onboarding bootstrap`.

Tests: `pip install -r services/api/requirements-dev.txt && pytest services/api/tests` (cluster and
LiteLLM faked). Tenants: `services/onboarding` (cloud identities + bucket with the Nebius CLI,
`charts/tenant` with `tenants/<name>.yaml` for the Kubernetes objects, Secrets by the script, LiteLLM
key); `clusters/common/manifests/api-agent` (one ApplicationSet-generated app per worker) installs the fleet API's identity in each region.

Not done: operation history beyond the Jobs' 90-day TTL (finished Jobs are the history; a longer
record would be a Loki/bucket export); authz is key -> tenant only.
