# Operations runbook

*For operators of a fleet deployed from `terraform.tfvars` with `./stack.sh`: identities and secrets,
rotation, the API allow-list, high availability, recovery of spot pools, images, weights, the database,
tenants and their isolation, edge, capacity, patching and the checks after any change.*

Everything operational inside the clusters is rendered by the platform stage from
`clusters/common/manifests/ops` (the `ops` namespace, the spot-recovery CronJob, the tenant isolation
policy) and runs in the ops image `services/ops` (Nebius CLI, crane, jq, curl). Nothing pre-existing in
your projects is touched; every identity below is created by the cloud stage and scoped to one project.

## Identities and secrets

| What | Where | Notes |
|---|---|---|
| SA `<fleet>-ops` + group `<fleet>-ops` (per project) | one per project of the fleet (ids in the cloud stage's outputs: `./stack.sh output cloud`) | the group holds `editor` on its own project; the SA's key is the identity of every ops job |
| SA auth key (JWT, no expiry) | Secret `ops/nebius-sa`, key `<region>.json`, written by the platform stage (`stack/platform/secrets.tf`) | mounted at `/var/run/nebius/<region>.json`; `ops-login.sh` turns every such file into a CLI profile `sa-<region>` and a registry credential helper for that region (tokens minted per call, nothing expires) |
| S3 access key of the hub ops SA | Secret `monitoring/cost-export-s3` (`ACCESS_KEY_ID`, `ACCESS_SECRET_KEY`), the daily cost report | bucket policy on the fleet's backups bucket (`<fleet>-backups`): the ops group may write there |
| Image cache credentials | a static Container Registry key of the hub ops SA, issued once per operator by `stack/scripts/registry-static-key.sh` and cached under `stack/.secrets/` (gitignored); Secret `registry/zot-sync-credentials` | docs/IMAGES.md |
| Registry push/pull from the ops image | the SA token (`Username: iam`) through the docker credential helper `docker-credential-nebius-sa` inside the image | a fresh token per registry request: no 40-minute expiry |
| LiteLLM master key, Grafana admin password, the database password | `random_password` resources of the cloud stage, in the state bucket (sensitive outputs) and in the clusters' Secrets | `./stack.sh output platform <id>` prints them |

### Rotation and take-over

There are no rotation CronJobs in this form: every credential is a Terraform resource, so rotation is a
`-replace` plus a re-apply of the stage that consumes it.

| Credential | Rotate with |
|---|---|
| LiteLLM master key | `./stack.sh tf cloud -- apply -replace=random_password.litellm_master`, then `apply platform` on every cluster |
| ops service-account key of a project | `-replace='nebius_iam_v1_auth_public_key.ops["<project>"]'` on the cloud stage, then `apply platform` on the clusters of that project |
| the fleet database's password | `-replace=random_password.database` on the cloud stage (check the plan first: the managed cluster's bootstrap user must update in place, not be replaced), then `apply platform control` rewrites the `database` Secrets; restart `api` and `litellm` |
| MultiKueue token of a worker | on the worker `./stack.sh tf platform <id> -- apply -replace='kubernetes_secret_v1.sa_token["multikueue"]'`, then `apply platform control` |
| the image cache's registry static key | delete `stack/.secrets/<name>-registry-key.json`, re-apply platform on every cluster, retire the old key with `nebius iam static-key delete` |
| tenant S3 keys | `-replace` the tenant's `nebius_iam_v2_access_key` in the models stage of that region, then `apply models` there and on the control cluster |
| the platform-internal LiteLLM key (`api/litellm-internal`) | by hand, see "Database" below |

Take-over by another operator: the tfvars, a key for the state bucket (`stack/bootstrap/state-bucket.sh`
with their profile), `./stack.sh plan` must show no changes. `recover-stopped-nodes` keeps running on every
worker meanwhile; nothing depends on the operator's laptop.

Manual fallback for the ops key (what the `-replace` does for you):
```
nebius iam auth-public-key generate --service-account-id <sa-id> --output /tmp/key.json --output-format service-account-json
kubectl -n ops create secret generic nebius-sa --from-file=<region>.json=/tmp/key.json --dry-run=client -o yaml | kubectl apply -f -
nebius iam auth-public-key list-by-account --account-service-account-id <sa-id> --format json   # then delete the old key id
```

### API endpoint allow-list (`allowed_cidrs`)

Each cluster's public Kubernetes API endpoint is restricted to `allowed_cidrs` of its block in the tfvars
(`control_plane.allowed_cidrs`, `regions.<r>.allowed_cidrs`; an empty list leaves the endpoint open): the
operator's egress address, and on the workers also the control cluster's NAT egress, through which its API
pods and MultiKueue reach them. Read the addresses from the kube-apiserver audit log, not from guesses: audit
logging is on (`audit_logs = {}` in `stack/modules/cluster/main.tf`) and the entries land in the project's
logging bucket `sp_mk8s_audit_logs`:

```
nebius --profile <sa profile> logging query --project-id <project> --bucket sp_mk8s_audit_logs \
  --since 20m --limit 500 '{userAgent=~"kubectl.*"}' --format json
# labels.sourceIPs_0 is what the allow-list sees; labels.user_username who it was
```

Guesses fail: an allow-list built from the NAT addresses that `ifconfig.me` and the gateway access log
report locked the operator host out of a cluster within seconds (recovered in half a minute by applying an
empty list, which needs only the Nebius API). Procedure for a change: apply on one worker first with an
automatic check in the same shell (`kubectl --context <worker> get nodes --request-timeout=20s` three
times, plus a `curl` through the customer API to an operation there; on failure re-apply with the previous
list), then the other clusters. If the operator host's router or the clusters' NAT pools change, the
symptom is a hanging `kubectl`: revert first, then re-read the audit log.

## High availability of the control plane

Everything a request passes through runs with two replicas spread over the two system nodes, with a
PodDisruptionBudget (minAvailable 1), so a node drain or a crash never takes the path down:

| Component | Where it is set | Mode |
|---|---|---|
| Envoy proxies `knative-external` / `knative-internal` (every request) | `clusters/common/manifests/gateway/envoyproxy-ha.yaml` (`envoyDeployment.replicas: 2`, `envoyPDB`) | active/active behind the LB / ClusterIP Service |
| Envoy Gateway controller | `clusters/common/values/envoy-gateway.yaml` | leader election |
| LiteLLM proxy (keys, sync/async pass-through) | `clusters/common/values/litellm.yaml` (`replicaCount: 2`) | active/active, state in the managed PostgreSQL |
| API | `clusters/common/manifests/api/api.yaml` (2 replicas) | active/active |
| Kueue controller | `clusters/common/values/kueue.yaml` (`controllerManager.replicas: 2`) | leader election |
| Knative Serving (activator, autoscaler, controller, webhook) | `clusters/common/manifests/knative/knative-serving.yaml` (`high-availability.replicas: 2`) | activator is in the scale-from-zero path |
| Fleet database | Nebius Managed PostgreSQL (`control_plane.database.hosts`, 1 by default; the service's failover with 2) | the service's |

Single replicas on purpose (not in the request path, reconcile-only; an outage delays changes, not
traffic): KServe controller (the chart has no replica knob), JobSet, cert-manager, Knative operator, Loki, Prometheus/Grafana. Capacity: the two system nodes (8 vCPU / 32 GiB each) carry the
second replicas with the requests the charts set (all small); `kubectl describe node` on both system nodes
should stay below 70 % requested CPU.

Drill (after any HA change): delete one pod of each component while a smoke runs (`/v1/models` with a
tenant key every 2 s, a `hello-run`, one endpoint call); no request may fail.

## Recover a stuck pool (preempted spot VMs)

Symptom: a node of a spot pool is `NotReady`, the node group reports `ComputeInstanceStopped`, the pool is
"full" but one GPU does nothing, pods stay Pending. Cause: a preempted spot VM is stopped, and neither the
node group nor the autoscaler restarts or replaces a stopped VM on its own.

Automated: CronJob `ops/recover-stopped-nodes` (every 2 min, every worker; the platform stage sets the
project and cluster ids) lists the instances of every preemptible node group of the cluster (new pools
need no change) and runs `nebius compute instance start` on every `STOPPED` one; the node comes back under
the same name with its cached images (measured: 2 min 52 s from STOPPED to Ready, of which 83 s waited for
the next tick). If the start is refused (no spot capacity), the instance is deleted
(`DELETE_ON_START_FAILURE=true`) and the node-group controller provisions a replacement. Check it:

```
kubectl -n ops get jobs | tail -3
kubectl -n ops logs -l app=recover-stopped-nodes --tail=20     # "node groups ...: RUNNING=4" or "... is STOPPED: starting"
```

By hand (the same thing the CronJob does):

```
nebius compute instance list --parent-id <project> --format json | jq -r '.items[] | select(.status.state=="STOPPED") | .metadata.id + " " + .metadata.name'
nebius compute instance start --id <computeinstance-id>       # ~1 min to Ready
nebius compute instance delete --id <computeinstance-id>      # if start fails; the node group replaces it
```

A node that is `NotReady` while its instance is `RUNNING` is a different failure (kubelet/driver);
`kubectl delete node` plus `instance delete` is the manual path, not automated.

## Images (push, cache, switch the source registry)

Images are pushed once to the source registry (`images.source` in the tfvars, or the registry the cloud
stage creates) and referenced everywhere as `registry.serverless2.local/<alias>/<path>`; each cluster's
Zot cache fetches on demand and Spegel shares the layers between the nodes. Nothing is mirrored per
region. The runbooks (push, add an upstream, switch the source registry, the credential Secret, sizing)
are in `docs/IMAGES.md`. Health: `kubectl -n registry get statefulset zot`, `kubectl -n spegel get
daemonset` (`spegel` and `containerd-registry-config` must have one ready pod per node), Grafana dashboard
"Spegel".

## Model weights

Each region can have a shared filesystem for weights (`weights_filesystem` in its block of the tfvars; the
cloud stage creates the Nebius Compute shared filesystem, attaches it to every GPU pool and mounts it on the
nodes at `/mnt/weights` through cloud-init). Pods reach it through the static ReadWriteMany
PersistentVolume + claim `models/weights-shared` that the platform stage renders (Knative allows hostPath
only read-only). A model opts in with `weights.sharedFilesystem: {enabled: true, path: <id>, mountPath: ...}`
plus the env vars its runtime reads (`charts/endpoint/README.md`); the pod then mounts the claim with
subPath `<id>` (`/mnt/weights/<id>` on the node) and keeps its cache there across scale-to-zero and node
replacement. Nothing is deleted automatically: a model's directory is removed by hand when the model goes.
A region needs the quota `compute.filesystem.size.network-ssd` before `weights_filesystem` is enabled;
enabling it on a region that already has nodes is a node-group template change that recreates every node
of the GPU pools (surge 1, drain 10 min): existing nodes never get the mount, only new ones.

Seed weights before the first start (optional; the first start seeds too), with the Job template
`clusters/common/manifests/ops/seed-weights-job.yaml` (not applied by the stage; variables in its header):

```
# HuggingFace repo into the HF cache layout
SEED_PATH=llm-example SEED_SOURCE=hf SEED_REF=<org>/<model> SEED_REV=main SEED_IMAGE=python:3.12-slim SEED_POOL=<pool> SEED_UID=1000 \
  envsubst < clusters/common/manifests/ops/seed-weights-job.yaml | kubectl --context <region> -n ops create -f -
# NIM image: runs the image's download-to-cache with NGC_API_KEY (Secret ops/ngc-api-key; pull Secret ops/ngc)
SEED_PATH=<model> SEED_SOURCE=ngc SEED_REF= SEED_REV=main SEED_IMAGE=nvcr.io/<org>/<nim-image>:<tag> SEED_POOL=<pool> SEED_UID=1000 \
  envsubst < clusters/common/manifests/ops/seed-weights-job.yaml | kubectl --context <region> -n ops create -f -
# a bucket prefix (SEED_SOURCE=s3 SEED_REF=s3://bucket/prefix SEED_IMAGE=amazon/aws-cli:2.22.35), or any command in any image (SEED_SOURCE=cmd)
```

Measured on an H100 spot pool, through the API, first call after scale-to-zero with the node present
("before" with the caches on emptyDir, re-downloaded every start):

| Model | Cache on the shared filesystem | Cold start before | Cold start after | Warm |
|---|---|---|---|---|
| a NIM endpoint (2.7 GB cache) | `/mnt/weights/<model>` | 250 s | **28.2 s, 27.4 s** | 1.8 s |
| a 0.5B-parameter chat model (vLLM, 953 MB HF cache) | `/mnt/weights/<model>` | 152-262 s | **137.7 s** (553 s when the pool first had to add a node: ~5 min node + image, then vLLM init) | 0.3-0.9 s |

The NIM's cold start is then the image start plus its init; the chat model's is dominated by vLLM/torch
initialisation, not the download. The job runs on the named GPU pool (the filesystem is mounted there),
with no GPU request; `uid` (default 1000, the NIM user) owns the files afterwards. Private HF repos: Secret
`ops/hf-token` (key `HF_TOKEN`). Changing `weights_filesystem` (size, mount path) rolls the GPU pools
(max_surge 1); growing the filesystem is in place.

## Database

The fleet database is a Nebius Managed Service for PostgreSQL cluster, not something that runs in a
cluster: `<fleet>-db` in the control plane's project and region, created by the cloud stage
(`stack/cloud/database.tf`), PostgreSQL 16 with the session-mode pooler, one host of platform `cpu-e2`
preset `2vcpu-8gb` with 64 GiB network-ssd by default (`control_plane.database` in the tfvars: `platform`,
`preset`, `disk_gib`, `hosts`, `backup_retention`, `backup_window_start`). It has no public access; the
control cluster's nodes reach its private endpoint through the VPC of the control subnet. Cost: tens of
USD per month, see the Nebius price list.

Two databases on it:

| Database | Holds | Owner |
|---|---|---|
| `platform` (the bootstrap database) | table `models` (id, kind, spec, rendered entry, version, managed_by, created/updated by and at) and `models_history` (every write); `schema_version` | the control API, the only writer (`services/api/db.py`) |
| `litellm` | API keys, budgets, spend (LiteLLM's own schema) | LiteLLM (`db.useExisting` in the chart values); created once by the Job `litellm/database-init` of the platform stage |

Connection data lives in Kubernetes Secrets written by the platform stage (`stack/platform/database.tf`):
`database` in the namespaces `api` and `litellm` (keys `host`, `port`, `user`, `password`, `platform_url`,
`litellm_url`) and `litellm-db` in `litellm` (`username`, `password`, read by the LiteLLM chart). The
control API gets `DATABASE_URL` from `api/database` key `platform_url`; regional APIs have no database.
The password is `random_password.database` of the cloud stage (output `secrets.database_password`).

**The platform-internal LiteLLM key** (`api/litellm-internal`, alias `platform-internal`; docs/API.md "Model
groups"): the key the model groups of API-defined OpenAI endpoints call the gateway with. Created by the
platform stage (`stack/platform/litellm-internal.tf`: value in the Secret, registered in LiteLLM by the Job
`litellm/key-platform-internal`). Rotation, by hand: generate a new value, `POST /key/generate` it with the
master key (same alias is fine, aliases are not unique), replace the Secret, restart the API (`kubectl -n
api rollout restart deployment/api`), re-save every OpenAI model once (`PUT /v1/models/<id>` with its
current spec, which re-registers the group with the new key), then `POST /key/delete` the old key. A
compromised key only lets its holder call the endpoints through the edge; it has no budget of its own, so
delete and rotate it, do not just block it.

**Backups and point-in-time restore** are the service's: a daily backup in the window starting at
`backup_window_start` (03:00 UTC by default), kept for `backup_retention` (14 days by default), with
continuous WAL for point-in-time recovery inside that window. Nothing runs in the cluster for it.

```
nebius msp postgresql v1alpha1 backup list --parent-id <control project id>          # the backups of the project
nebius msp postgresql v1alpha1 cluster get-for-backup --id <cluster id>              # the configuration to restore with
nebius msp postgresql v1alpha1 cluster restore --parent-id <control project id> --network-id <vpc network id> \
  --backup-id <backup id> --recovery-time 2026-10-09T10:00:00Z --name <fleet>-db-restored ...   # a NEW cluster from a backup (PITR)
```

The Nebius console (Managed PostgreSQL, the cluster, "Backups") offers the same. A restore creates a new
cluster; to switch the fleet to it, point `platform_url`/`litellm_url` (the `database` Secrets) and the
LiteLLM values at the new endpoint, or import the restored cluster into the Terraform state in place of the
old one. The cluster id and endpoint are in `./stack.sh output cloud` (`database`).

**A psql shell** (one-off pod on the control cluster, image through the fleet's image cache host, environment
from Secret `api/database`):

```
IMAGES=<images.host of the tfvars>   # the fleet's logical registry host, registry.serverless2.local by default (docs/IMAGES.md)
kubectl -n api run psql --rm -it --restart=Never --image=$IMAGES/docker/library/postgres:16-alpine \
  --env="PLATFORM_URL=$(kubectl -n api get secret database -o jsonpath='{.data.platform_url}' | base64 -d)" \
  -- sh -c 'psql "$PLATFORM_URL"'
# \dt                                   tables: models, models_history, schema_version
# select id, kind, version, updated_by, updated_at from models order by id;
# select id, version, action, "by", at from models_history order by at desc limit 20;
```

Use `litellm_url` for the LiteLLM database. The managed endpoint requires TLS (`sslmode=require` is in the
URLs).

**Schema migrations** are applied by the control API at start (`services/api/db.py` `MIGRATIONS`, one SQL
block per version, recorded in `schema_version`; `migrate()` applies the ones that are missing, in a
transaction each). A new migration is a new list entry; a rollout of the API applies it; nothing to run by
hand. The API logs the schema version at start.

**Sizing and HA**: one host is enough for the platform's writes (model definitions change rarely; LiteLLM
writes a spend row per call). `control_plane.database.hosts = 2` adds a replica with the service's
failover; `disk_gib` grows in place. Changing `preset` restarts the host.

Other data:

| Data | Mechanism | Where | Retention |
|---|---|---|---|
| Operations (run and endpoint-call Jobs) | the Jobs themselves (`ttlSecondsAfterFinished` 90 d) plus `STATUS.json`, `attempts/` and outputs under `operations/<id>/` in the tenant bucket | tenant bucket (lifecycle rule `lifecycle_days`) | 90 d by default |
| Tenant buckets, model caches, PVCs | none (object storage is durable; caches are rebuilt) | | |

## Tenant isolation

`CiliumClusterwideNetworkPolicy tenant-isolation` (every cluster) selects every pod in a namespace labeled
`serverless2.nebius/tenant` and allows only: ingress from the node (kubelet probes) and Prometheus; egress
to CoreDNS, the Kubernetes API (`kube-apiserver` entity: the uploader polls its own pod), the gateway
(`envoy-gateway-system`: endpoint calls from tenant Jobs use the endpoint's public hostname with the
caller's key, so key check, rate limit and accounting apply exactly as for an external client; the
predictor Services in `models` and the Knative activator are not reachable from tenant pods, so no call
skips the key check) and `world` (tenant bucket, NGC, the public gateway). Everything else in the cluster
(other tenants, `litellm`, `api`, `models`, `knative-serving`) is unreachable in both directions. Cilium
namespace labels are matched as `io.cilium.k8s.namespace.labels.<key>` (not
`io.kubernetes.pod.namespace.labels`: that key validates but matches nothing).

Tenant namespaces carry Pod Security Admission labels (`charts/tenant`: `baseline` enforced, `restricted`
warned/audited) and every pod the API renders drops all capabilities, forbids privilege escalation and runs
under the runtime seccomp profile (`services/api/render.py`; runner containers as uid 10001, the model
container as the image's user unless the job class sets `runAsNonRoot`). What this does and does not
protect against is in README.md "Security notes and limitations".

A cluster-wide policy cannot express "my own namespace", so `charts/tenant` renders a namespaced
`CiliumNetworkPolicy tenant-local` (same-namespace ingress and egress, needed for multi-pod runs) into each
tenant namespace (`charts/tenant/templates/network.yaml`, rendered by the models stage).

Plain `networking.k8s.io/v1` equivalent (if Cilium policy must be avoided): a per-namespace NetworkPolicy
with `policyTypes: [Ingress, Egress]`, ingress from `podSelector: {}`, egress to `podSelector: {}`, to
`kube-system` `k8s-app=coredns` :53, to the namespace `envoy-gateway-system`, and `ipBlock 0.0.0.0/0 except
[10.0.0.0/8]` plus the API-server IPs as /32 entries (they change: that is why the Cilium entity is used).

### API RBAC

`clusters/common/manifests/api/api.yaml` holds everything the `api` ServiceAccount may do: ClusterRole
`serverless2-api-tenant` (jobs, pods, Secret `tenant-storage`; defined in `clusters/common/manifests/api-agent`)
bound per tenant namespace by the RoleBinding `charts/tenant` renders; Role `serverless2-api-endpoints` in
`models` (inferenceservices get/list/patch, pods); read-only Roles `serverless2-api-templates` in the
template namespaces (`api`); one ClusterRole to list tenant namespaces (billing).

### Tenants (`tenants.<name>` in the tfvars, `stack/models`, `charts/tenant`)

A tenant is one block of the tfvars (`budget_usd`, `lifecycle_days`, `allowed_images`, its keys) and one
`./stack.sh apply models <id>` per cluster, control last. Per region the models stage creates a service
account, a group with `storage.object-editor` on the tenant bucket through the bucket policy, the bucket
`<fleet>-<name>-<region>` with a lifecycle rule that expires `operations/` objects after `lifecycle_days`
(default 90; aborts incomplete multipart uploads after 7 days) and an S3 access key
(`stack/modules/tenant-region`); then `charts/tenant` renders the namespace `tenant-<name>`, the run-pod
identity (`job-runner`), the API binding, one Kueue LocalQueue per scheduling profile (`default`,
`prefer-<class>`), the network policy and, on the control cluster, the namespace, queues and API binding
only. Secrets in the tenant namespace (never in git): workers `tenant-storage` and `s3` (this region's
bucket and key), `s3-fleet` (the hub region's bucket and key, used by fleet-placed runs), `ngc`; control
`tenant-storage` (the hub region's bucket). The LiteLLM keys (budget, allowed models and pass-through
routes) are created on the control cluster: `./stack.sh output models control` prints them. Adding a region
to a tenant or changing the retention is a tfvars change plus a re-apply; removing a tenant is removing the
block (with `protect_data = true` the bucket stays and must be emptied by hand first).

## Edge: certificates, regional API, rate limits

- **Certificates** are ACME (Let's Encrypt) through the Gateway API HTTP-01 solver; see docs/EDGE.md.
  Status: `kubectl -n envoy-gateway-system get certificate,certificaterequest,order,challenge`. A failed
  order never touches the Secret the listener uses. The platform's own hostnames come from the platform
  stage (`edge` in the tfvars); the model endpoints' hostnames are kept on the Certificate `models` by the
  fleet API as models come and go.
- **Regional API** (`api` on every cluster): the platform stage gives it Secret `api/litellm-master` (a
  copy of the control cluster's) and the tenant RoleBindings that `charts/tenant` renders. It carries no
  region kubeconfigs; sync and async calls for its region are answered there, writes go to the fleet API.
- **Rate limits** live in `clusters/common/manifests/api/api.yaml` (API route) and
  `clusters/common/manifests/gateway/ratelimit.yaml` (everything else), counted by the Envoy rate-limit
  service (`envoy-gateway-system`, deployed by the chart when `config.envoyGateway.rateLimit` is set) in
  `ratelimit-redis`. Redis is not persistent: a restart resets the windows; if it is unreachable Envoy
  fails open. Check: `kubectl -n envoy-gateway-system get deploy envoy-ratelimit ratelimit-redis`; test
  with two keys, a burst beyond 600/min on `/v1/models` returns 429 for that key only.

## Capacity for runs and endpoints

Kueue admits run pods against the fleet's ClusterQueues, but it does not see endpoint pods that are outside
Kueue, and a pool has a maximum (`max_nodes` per pool in the tfvars; `endpoint_floor_gpus` there is the
warm-endpoint deduction that `charts/fleet` applies, docs/FLEET.md). Rule: **ClusterQueue nominal GPU quota =
pool maximum minus the GPUs held by endpoints with a warm floor (`scaling.min` >= 1) that are not Kueue
managed**; the chart computes it from the two numbers, so they change together in the tfvars. Cold endpoints
(min 0) borrow a GPU only while they serve; an admitted run can wait Pending for one scale-to-zero period
(60 s grace) in the worst case. Alerts `RunPodPendingLong` (admitted run pods Pending > 10 min) and
`KueueWorkloadsPendingLong` (quota exhausted > 15 min) in
`clusters/common/manifests/observability/rules-capacity.yaml`.

When a tenant needs more: raise `max_nodes` (`./stack.sh apply`), the quota follows, and if it is a
sustained need, a reserved (non-spot) pool as a second flavor ahead of the spot one (the preference queues
order reserved before on-demand before spot inside a class).

## Add a region

One `regions.<name>` block (project, subnet, allowed CIDRs, pools), then `./stack.sh apply`: the cloud stage
adds the cluster, the platform and models stages follow on it, and the control cluster's MultiKueue gains
the worker (docs/FLEET.md "Change recipes"). The platform images need no per-region copy: the new cluster's
cache fetches them from the source registry on first use.

## Patching

- CI (`.github/workflows/k8s-inference.yml` at the library's root) runs on every pull request and push
  that touches the solution: `terraform fmt`/`validate` of every stage, `helm template` of the charts with
  the example's values, every manifest rendered, the API and dispatcher tests, the console's tests and
  build, and a `trivy` scan of the five images built from `services/*/Dockerfile` and `ui/Dockerfile`
  (fails on CRITICAL, unfixed ignored). The ops image pins its tools by version and SHA-256
  (`services/ops/Dockerfile`).
- Cadence: third-party chart versions (`clusters/common/apps/*.yaml`) and image bases are reviewed
  monthly; a critical CVE in the platform's own images or in an internet-facing component (Envoy Gateway,
  LiteLLM, the API) is patched within 7 days. Images that tenants register through the API are not scanned
  by the platform (README.md "Security notes and limitations").
- Rolling a platform image: build and push a new tag (`tools/images.sh build`, docs/IMAGES.md "Pushing an
  image"), set it in `images.versions`, `./stack.sh apply platform` on every cluster; the caches fetch the
  new tag on first pull. Rolling a chart: bump `version:` in `clusters/common/apps/<name>.yaml`, render it
  locally (`make check`), `./stack.sh apply platform`; then "Checks after any change".

## Checks after any change

```
./stack.sh plan                                                   # no changes on any stage and cluster
make check                                                        # every stage validates, every chart and manifest renders, the tests pass
kubectl -n ops get cronjob; kubectl -n ops logs -l app=recover-stopped-nodes --tail=3      # on each worker
kubectl -n api get job | tail -3                                  # billing CronJob runs (control cluster)
nebius msp postgresql v1alpha1 backup list --parent-id <control project id> | head          # the managed database's backups
kubectl get ccnp tenant-isolation -o jsonpath='{.status.conditions[0].status}'
./stack.sh apply models control                                   # re-runs the acceptance probe (hello-run, the example endpoint)
```

## Reliability and upgrades

See [RELIABILITY.md](RELIABILITY.md) for durable model reconciliation, atomic GPU accounting, shared Redis, Object Storage logs, private TLS and the remaining production integration checks.
