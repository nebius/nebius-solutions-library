# Operations runbook (Serverless 2.0, phase 1)

*For operators: identities and secrets, recovery, backups, tenants, patching and the checks after any change. Sections that mention `fleet.yaml` or Argo CD describe the reference fleet; the Terraform solution has the same mechanisms behind `terraform.tfvars` and `./stack.sh` (see the last section).*

Everything here is delivered by the Argo CD Application `ops`
(`clusters/common/manifests/ops` + `clusters/<cluster>/apps/overlays/ops`) and the ops image
`services/ops` (Nebius CLI, crane, jq, curl). Numbers:
`spikes/S13-ops/RESULT.md`. Nothing pre-existing is touched; all identities
below are NEW and project-scoped.

## Identities and secrets

| What | Where | Notes |
|---|---|---|
| SA `<fleet>-ops` + group `<fleet>-ops` (per project) | one per project of the fleet (ids in the cloud stage's outputs: `./stack.sh output cloud`) | the group holds `editor` on its own project (an `editor` profile cannot grant service roles such as `compute.editor`: "No permission"); cross-project permits are refused ("parent of subject is not from resource's hierarchy"), so each region has its own SA |
| SA auth key (JWT, no expiry) | `infra/state/<cluster>/ops/serverless2-ops.sa.json` (gitignored); Secret `ops/nebius-sa` key `<region>.json`; hub also `ops/nebius-sa-eu-south1` | mounted at `/var/run/nebius/<region>.json`; `ops-login.sh` turns each file into a CLI profile `sa-<region>` and mints a token per call |
| S3 access key of the hub ops SA | `infra/state/hub/ops/serverless2-ops.s3.json`; Secret `data/backup-s3` (`ACCESS_KEY_ID`, `ACCESS_SECRET_KEY`) | bucket policy on `serverless2-backups-eu-north1`: group -> `storage.object-editor` |
| Registry push/pull | the SA token (`Username: iam`) through the docker credential helper `docker-credential-nebius-sa` inside the image | a fresh token per registry request: no 40-minute expiry (known issue 2) |

### Rotation (automatic since 2026-10-06)

| Credential | Rotated by | Cadence | Lifetime |
|---|---|---|---|
| api-agent kubeconfig per region (control cluster secret `api/region-kubeconfigs`, keys eu-north1 and eu-south1) | CronJob `ops/rotate-api-agent-token` (control cluster since 2026-10-07; `services/ops/bin/rotate-api-agent-token.sh`): mk8s cluster token of the region's ops SA -> TokenRequest for `api/api-agent` (48 h) -> Secret patch | daily 03:10 UTC | 48 h; the API rebuilds a region client when the mounted file changes (`services/api/kube.py`), no restart |
| fleet credentials for the workers: Argo CD cluster Secrets `argocd/hub`, `argocd/eu-south1` and MultiKueue kubeconfigs `kueue-system/multikueue-<cluster>` (control cluster) | CronJob `ops/rotate-fleet-tokens` (control cluster; `services/ops/bin/rotate-fleet-tokens.sh`): same path, TokenRequest for `kube-system/argocd-manager` and `kueue-system/multikueue` (48 h) -> Secret patches; first run by hand after `bootstrap.sh register` | daily 03:20 UTC | 48 h; Argo CD and Kueue re-read the Secrets |
| ops SA auth keys (`ops/nebius-sa`, control cluster and hub also `ops/nebius-sa-eu-south1`) | CronJob `ops/rotate-nebius-keys` on each cluster (`rotate-nebius-keys.sh`): new key with the current key, Secret patch, other keys of that SA older than 7 days deleted | Mondays 04:20 UTC | until retired (one rotation of overlap) |
| CNPG backup S3 key (`data/backup-s3`) | same CronJob on the control cluster (`BACKUP_S3_SECRET`): new access key, Secret patch, older keys deleted | weekly | CNPG re-reads the Secret at the next backup |
| local copies `infra/state/<cluster>/ops/*.sa.json` | not rotated: they are only needed to re-create the Secrets; after the first rotation they are stale and the live Secret is the source (`kubectl -n ops get secret nebius-sa -o jsonpath='{.data.<region>\.json}' | base64 -d`) | | |
| LiteLLM master key | manual: `kubectl -n litellm create secret generic litellm-master` with a new value, mirror it to `api/litellm-master`, restart `litellm` and `api`; tenant keys are unaffected | | |
| tenant S3 keys | Terraform (`infra/tenant`): `terraform taint 'module.<region>[0].nebius_iam_v2_access_key.tenant[0]'` then `python -m onboarding create-tenant <name>` rewrites the Secrets | on demand | |

The static `kubernetes.io/service-account-token` Secret `api/api-agent-token`
in each region is no longer used by anything (the rotation and `onboarding
bootstrap --region` both use the TokenRequest API) and can be removed from
`clusters/common/manifests/api-agent/api-agent.yaml`.

Manual fallback (same steps the CronJobs run):
```
nebius iam auth-public-key generate --service-account-id <sa-id> --output /tmp/key.json --output-format service-account-json
kubectl -n ops create secret generic nebius-sa --from-file=<region>.json=/tmp/key.json --dry-run=client -o yaml | kubectl apply -f -
nebius iam auth-public-key list-by-account --account-service-account-id <sa-id> --format json   # then delete the old key id
```

### API endpoint allow-list (`control_plane_allowed_cidrs`)

All three clusters' public Kubernetes API endpoints are restricted since
2026-10-06 (`allowed_cidrs` per cluster in `fleet.yaml`, applied with `infra/fleet/apply.sh`):

| Cluster | Allowed | Why |
|---|---|---|
| eu-south1 | `<hub NAT pool>/24` | hub NAT pool: the hub API pods (region kubeconfig) and the token-rotation CronJob |
| eu-south1 | `<admin host egress>/32` | the admin host: its route to the eu-south1 endpoint goes through a VPN subnet router, whose egress is this address |
| eu-south1 | `<own NAT pool>/25` | its own NAT pool (in-region callers) |
| hub | `<admin host egress>/32` | the admin host through another VPN router (a different egress per region) |
| hub | `<hub NAT pool>/24`, `<region NAT pool>/25` | hub and region NAT pools (in-cluster callers use the private endpoint; kept for completeness) |

The addresses were read from the kube-apiserver audit log, not guessed:
audit logging is on (`audit_logs = {}` in `infra/cluster/main.tf`) and the
entries land in the project's logging bucket `sp_mk8s_audit_logs`:

```
nebius --profile <sa profile> logging query --project-id <project> --bucket sp_mk8s_audit_logs \
  --since 20m --limit 500 '{userAgent=~"kubectl.*"}' --format json
# labels.sourceIPs_0 is what the allow-list sees; labels.user_username who it was
```

Guesses fail: an earlier attempt with the NAT addresses that `ifconfig.me`
and the gateway access log report locked out
both this host and the hub API within seconds (recovered in 32 s). Procedure
for a change: apply on eu-south1 first with an automatic check and revert in
the same shell (`kubectl --context <eu-south1> get nodes --request-timeout=20s`
three times, plus a `curl` through the customer API to an eu-south1
operation; on failure `terraform apply -var 'control_plane_allowed_cidrs=[]'`,
which needs only the Nebius API), then the hub the same way. If this host's
router or the clusters' NAT pools change, the symptom is a hanging `kubectl`;
revert first, then re-read the audit log.

## High availability of the control plane (readiness item 1, 2026-10-06)

Everything a request passes through runs with two replicas spread over the two
system nodes, with a PodDisruptionBudget (minAvailable 1) so a node drain or a
crash never takes the path down:

| Component | Where it is set | Mode |
|---|---|---|
| Envoy proxies `knative-external` / `knative-internal` (every request) | `clusters/common/manifests/gateway/envoyproxy-ha.yaml` (`envoyDeployment.replicas: 2`, `envoyPDB`) | active/active behind the LB / ClusterIP Service |
| Envoy Gateway controller | `clusters/common/values/envoy-gateway.yaml` | leader election |
| LiteLLM proxy (keys, sync/async pass-through) | `clusters/common/values/litellm.yaml` (`replicaCount: 2`) | active/active, state in CNPG |
| API | `clusters/hub/apps/manifests/api/api.yaml` (2 replicas, since 0.3) | active/active |
| Kueue controller | `clusters/common/values/kueue.yaml` (`controllerManager.replicas: 2`) | leader election |
| Knative Serving (activator, autoscaler, controller, webhook) | `clusters/common/manifests/knative/knative-serving.yaml` (`high-availability.replicas: 2`) | activator is in the scale-from-zero path |
| CloudNativePG `data/postgres` | 2 instances since S13 | primary + replica, automatic failover |
| edge-auth | 2 replicas since S15 | active/active |

Single replicas on purpose (not in the request path, reconcile-only; an outage
delays changes, not traffic): KServe controller (the chart has no replica
knob), JobSet, cert-manager, Knative operator, CNPG operator, Argo CD, Loki,
Prometheus/Grafana. Capacity: the two system nodes (8 vCPU / 32 GiB each) carry
the second replicas with the requests the charts set (all small); `kubectl
describe node` on both system nodes should stay below 70 % requested CPU.

Drill (after any HA change): delete one pod of each component while the smoke
runs (`/v1/models` with the demo key every 2 s, a hello-run, one endpoint
call); no request may fail.

## Recover a stuck pool (preempted spot VMs)

Symptom: a node of `h100-spot-1x` / `rtx6000-spot-1x` is `NotReady`, the node
group reports `ComputeInstanceStopped`, the pool is "full" but one GPU does
nothing, pods stay Pending. Cause: spot VMs are created with
`on_preemption: STOP` (the mk8s node-group template has no other option), and
neither the node group nor the autoscaler restarts or replaces a stopped VM.

Automated: CronJob `ops/recover-stopped-nodes` (every 2 min, both clusters)
lists the instances of the spot node groups (`NODE_GROUP_IDS`; with ops image
0.1.6+ leave it empty and set `NEBIUS_CLUSTER_ID`: every preemptible node group
of the cluster is covered, new pools need no manifest change) and runs `nebius
compute instance start` on every `STOPPED` one; the node comes back under the same name with
its cached images (measured: 2 min 52 s from STOPPED to Ready, of which 83 s
waited for the next tick). If the start is refused (no spot capacity), the
instance is deleted (`DELETE_ON_START_FAILURE=true`) and the node-group
controller provisions a replacement. Check it:

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

A node that is `NotReady` while its instance is `RUNNING` is a different
failure (kubelet/driver); `kubectl delete node` plus `instance delete` is the
manual path, not automated.

## Images (push, cache, switch the source registry)

Images are pushed once to the source registry (fleet.yaml `images.source`) and
referenced everywhere as `registry.serverless2.local/<alias>/<path>`; each
cluster's Zot cache fetches on demand and Spegel shares the layers between the
nodes. Nothing is mirrored per region any more. The runbooks (push, add an
upstream, switch the source registry, the credential Secret, sizing) are in
`docs/IMAGES.md`. Health: `kubectl -n registry get statefulset zot`,
`kubectl -n spegel get daemonset` (`spegel` and `containerd-registry-config`
must have one ready pod per node), Grafana dashboard "Spegel".

## Model weights

Each cluster has a shared filesystem for weights (`weights_filesystem` per
region in `fleet.yaml`; Terraform creates the Nebius Compute
shared filesystem, attaches it to every GPU pool and mounts it on the nodes at
`/mnt/weights` through cloud-init; `terraform output weights_filesystem`
shows id and mount path). Pods reach it through the static ReadWriteMany
PersistentVolume + claim `models/weights-shared`
(`clusters/<cluster>/apps/overlays/scheduling/weights-pv.yaml`; Knative
allows hostPath only read-only). A catalog entry opts in with
`runtime.weights.sharedFilesystem: {enabled: true, path: <id>, mountPath: ...}`
plus the env vars its runtime reads (`charts/endpoint/README.md`); the pod
then mounts the claim with subPath `<id>` (`/mnt/weights/<id>` on the node)
and keeps its cache there across scale-to-zero and node replacement. Nothing
is deleted automatically: a model's directory is removed by hand when the
entry goes. Both regions have the filesystem (hub since 2026-10-06, eu-south1
since 2026-10-07 after the quota `compute.filesystem.size.network-ssd` was
raised to 5 TiB; 2 TiB each). A new region needs the quota before
`weights_filesystem.enabled: true`; enabling it on an existing region is a
node-group template change that recreates every node of the GPU pools
(surge 1, drain 10 min): existing nodes never get the mount, only new ones.

Seed weights before the first start (optional; the first start seeds too), with
the Job template `clusters/common/manifests/ops/seed-weights-job.yaml` (not
applied by Argo CD; variables in its header):

```
# HuggingFace repo into the HF cache layout
SEED_PATH=qwen2-5-0-5b SEED_SOURCE=hf SEED_REF=Qwen/Qwen2.5-0.5B-Instruct SEED_REV=main SEED_IMAGE=python:3.12-slim SEED_POOL=h100-spot-1x SEED_UID=1000 \
  envsubst < clusters/common/manifests/ops/seed-weights-job.yaml | kubectl --context <hub> -n ops create -f -
# NIM image: runs the image's download-to-cache with NGC_API_KEY (Secret ops/ngc-api-key; pull Secret ops/ngc)
SEED_PATH=diffdock SEED_SOURCE=ngc SEED_REF= SEED_REV=main SEED_IMAGE=nvcr.io/nim/mit/diffdock:2.2.0 SEED_POOL=h100-spot-1x SEED_UID=1000 \
  envsubst < clusters/common/manifests/ops/seed-weights-job.yaml | kubectl --context <hub> -n ops create -f -
# a bucket prefix (SEED_SOURCE=s3 SEED_REF=s3://bucket/prefix SEED_IMAGE=amazon/aws-cli:2.22.35), or any command in any image (SEED_SOURCE=cmd)
# eu-south1: same template, the region's GPU pool and context
SEED_PATH=qwen2-5-0-5b SEED_SOURCE=hf SEED_REF=Qwen/Qwen2.5-0.5B-Instruct SEED_REV=main SEED_IMAGE=python:3.12-slim SEED_POOL=rtx6000-spot-1x SEED_UID=1000 \
  envsubst < clusters/common/manifests/ops/seed-weights-job.yaml | kubectl --context <eu-south1> -n ops create -f -
```

Measured on the hub (2026-10-06, H100 spot pool, through the API, first call
after scale-to-zero with the node present; "before" from S10 with the caches
on emptyDir, re-downloaded every start):

| Model | Cache on the shared filesystem | Cold start before | Cold start after | Warm |
|---|---|---|---|---|
| DiffDock (NIM, 2.7 GB cache) | `/mnt/weights/diffdock` | 250 s | **28.2 s, 27.4 s** | 1.8 s |
| Qwen2.5 0.5B (vLLM, 953 MB HF cache) | `/mnt/weights/qwen2-5-0-5b` | 152-262 s | **137.7 s** (553 s when the pool first had to add a node: ~5 min node + image, then vLLM init) | 0.3-0.9 s |

DiffDock's cold start is now the image start plus NIM init; Qwen's is
dominated by vLLM/torch initialisation, not the download. The first start
after enabling the block seeded both directories itself (no seed job was
run).

The job runs on the named GPU pool (the filesystem is mounted there), with no
GPU request; `uid` (default 1000, the NIM user) owns the files afterwards.
Private HF repos: Secret `ops/hf-token` (key `HF_TOKEN`). Changing
`weights_filesystem` (size, mount path) is a node-group template change and
rolls the GPU pools (max_surge 1); growing the filesystem is in place.

## Backups and restore

| Data | Mechanism | Where | Retention |
|---|---|---|---|
| CloudNativePG `data/postgres` (the one database: LiteLLM keys, spend, logs) | `spec.backup.barmanObjectStore` (WAL archiving, gzip) + `ScheduledBackup data/postgres-daily` at 03:15 UTC | `s3://serverless2-backups-eu-north1/postgres/postgres/{base,wals}/` | 14 d (`retentionPolicy`) |
| Operations (run and endpoint-call Jobs) | the Jobs themselves (`ttlSecondsAfterFinished` 90 d) plus `STATUS.json`, `attempts/` and outputs under `operations/<id>/` in the tenant bucket | tenant bucket (lifecycle rule `lifecycleDays`) | 90 d |
| Tenant buckets, model caches, PVCs | none (object storage is durable; caches are rebuilt) | | |

Take a backup now:

```
kubectl -n data apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: { name: postgres-manual, namespace: data }
spec: { cluster: { name: postgres }, method: barmanObjectStore }
EOF
```

Restore CNPG (latest or point-in-time) into a NEW cluster, then point LiteLLM
at it (`db.endpoint` in `clusters/common/values/litellm.yaml`):

```
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: { name: postgres-restored, namespace: data }
spec:
  instances: 2
  storage: { size: 50Gi }
  bootstrap:
    recovery:
      source: postgres
      # recoveryTarget: { targetTime: "2026-10-05 21:30:00+00" }   # optional PITR
  externalClusters:
    - name: postgres
      barmanObjectStore:
        destinationPath: s3://serverless2-backups-eu-north1/postgres
        endpointURL: https://storage.eu-north1.nebius.cloud
        s3Credentials:
          accessKeyId: { name: backup-s3, key: ACCESS_KEY_ID }
          secretAccessKey: { name: backup-s3, key: ACCESS_SECRET_KEY }
        wal: { compression: gzip }
```

### LiteLLM on CloudNativePG: one-time migration (2026-10 leanness review, item 3)

LiteLLM used the chart's standalone Bitnami Postgres (`litellm/litellm-postgresql`) next to an
unused CNPG cluster. The litellm Application now sets `db.useExisting: true`, `db.endpoint:
postgres-rw.data.svc.cluster.local`, `db.secret.name: litellm-db`, `db.deployStandalone: false`.
Order of operations after the change is merged (keys are unavailable for the minutes between
steps 3 and 5; the edge ext-auth and the API cache decisions for 30 s):

```
# 0. preconditions: the CNPG role/db exist (bootstrap.sh initdb: database litellm, owner litellm) and the
#    credentials Secret is in the litellm namespace (bootstrap.sh copies data/litellm-db -> litellm/litellm-db)
kubectl -n litellm get secret litellm-db || kubectl -n data get secret litellm-db -o json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); d["metadata"]={"name":"litellm-db","namespace":"litellm"}; print(json.dumps(d))' | kubectl apply -f -
# 1. pause Argo CD auto-sync of the litellm app (so the chart does not switch before the data is copied)
kubectl -n argocd patch app litellm --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
# 2. dump from the Bitnami Postgres and restore into CNPG (both reachable in-cluster; the postgres:16 image has both tools)
kubectl -n litellm run pgmigrate --rm -i --restart=Never --image=postgres:16 \
  --env=SRC_PW="$(kubectl -n litellm get secret litellm-dbcredentials -o jsonpath='{.data.password}' | base64 -d)" \
  --env=DST_PW="$(kubectl -n litellm get secret litellm-db -o jsonpath='{.data.password}' | base64 -d)" -- sh -c '
  PGPASSWORD=$SRC_PW pg_dump -Fc -h litellm-postgresql.litellm.svc -U litellm litellm > /tmp/l.dump &&
  PGPASSWORD=$DST_PW pg_restore --no-owner --role=litellm -h postgres-rw.data.svc -U litellm -d litellm /tmp/l.dump &&
  PGPASSWORD=$DST_PW psql -h postgres-rw.data.svc -U litellm -d litellm -c "select count(*) from \"LiteLLM_VerificationToken\""'
# 3. switch: sync the app (new Deployment env, no StatefulSet); the schema-migration Job runs against CNPG
kubectl -n argocd patch app litellm --type merge -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl -n litellm rollout status deploy/litellm
# 4. verify: every demo key still answers /key/info, the API and the edge auth still accept them
for k in infra/state/hub/tenants/*.litellm-key; do curl -sS -o /dev/null -w "$k %{http_code}\n" -H "Authorization: Bearer $(cat $k)" http://127.0.0.1:14000/key/info; done
# 5. clean up the Bitnami leftovers once step 4 passed (Argo CD prunes the StatefulSet; the PVC stays)
kubectl -n litellm delete pvc data-litellm-postgresql-0 secret litellm-postgresql litellm-dbcredentials backup-s3
```

Rollback (before step 5): restore the Bitnami values in `clusters/common/values/litellm.yaml` (drop the `db:` block),
push, sync; the old PVC still holds the data. A pre-migration `pg_dump` is in the
backups bucket under `litellm/` from the last run of the (now removed) `litellm-pgdump` CronJob.


### Restore drill (readiness gap 9): run after every change to the backup path

Repeatable procedure (about 3 minutes); the scratch cluster is deleted at the end.

```
# 1. fresh base backup (the drill restores "latest")
kubectl -n data apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: { name: postgres-drill-$(date -u +%Y%m%d), namespace: data }
spec: { cluster: { name: postgres }, method: barmanObjectStore }
EOF
kubectl -n data get backup -w        # phase: completed
# 2. scratch cluster recovering from the object store (1 instance, 20 Gi)
kubectl -n data apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: { name: postgres-restore-test, namespace: data, labels: { serverless2.nebius/drill: restore } }
spec:
  instances: 1
  storage: { size: 20Gi }
  bootstrap: { recovery: { source: postgres } }
  externalClusters:
    - name: postgres
      barmanObjectStore:
        destinationPath: s3://serverless2-backups-eu-north1/postgres
        endpointURL: https://storage.eu-north1.nebius.cloud
        s3Credentials:
          accessKeyId: { name: backup-s3, key: ACCESS_KEY_ID }
          secretAccessKey: { name: backup-s3, key: ACCESS_SECRET_KEY }
        wal: { compression: gzip }
        data: { compression: gzip }
EOF
kubectl -n data get cluster postgres-restore-test -w     # readyInstances 1
# 3. compare
Q='select (select count(*) from "LiteLLM_VerificationToken"), (select count(*) from "LiteLLM_SpendLogs"), (select count(*) from information_schema.tables where table_schema='"'"'public'"'"')'
kubectl -n data exec postgres-1 -c postgres -- psql -U postgres -d litellm -At -c "$Q"
kubectl -n data exec postgres-restore-test-1 -c postgres -- psql -U postgres -d litellm -At -c "$Q"
# 4. clean up
kubectl -n data delete cluster postgres-restore-test
```

Result 2026-10-06 15:04 UTC: backup `postgres-drill-20261006` (id
20261006T145956, WAL 0x19-0x1A) completed in 20 s; the scratch cluster was
ready 4 min 15 s after apply; live vs restored `7|28|0|91` (keys, spend logs,
users, tables) identical; `pg_is_in_recovery() = f` (promoted); scratch
cluster deleted.

Finding from the first attempt: the two base backups taken before 2026-10-06
15:05 UTC (`20261005T213026`, `20261006T031500`) are **not restorable**: they
need WAL segments 5-7 and the archive only holds segments from 7 onwards
(archiving to this path started 13:28 UTC; Postgres marks 1-6 `.done`, so an
earlier archive destination or a reconfiguration swallowed them). The retention
policy expires them in 14 days; every backup after the drill backup is covered
by a continuous WAL chain. The drill is the only thing that catches this, hence
"after every change to the backup path".

## Tenant isolation

`CiliumClusterwideNetworkPolicy tenant-isolation` (both clusters) selects
every pod in a namespace labeled `serverless2.nebius/tenant` and allows only:
ingress from the node (kubelet probes) and Prometheus; egress to CoreDNS, the
Kubernetes API (`kube-apiserver` entity: the uploader polls its own pod), the gateway
(`envoy-gateway-system`: endpoint calls from tenant Jobs use the endpoint's public
hostname with the caller's key, so key check, rate limit and accounting apply exactly
as for an external client; since 2026-10-08 the predictor Services in `models` and
the Knative activator are not reachable from tenant pods, docs/SECURITY-PREREVIEW.md
F2) and `world` (tenant bucket, NGC, the public gateway). Everything else in the
cluster (other tenants, `litellm`, `data`, `api`, `models`, `knative-serving`) is
unreachable in both directions. Measured in
`spikes/S13-ops/RESULT.md`. Cilium namespace labels are matched as
`io.cilium.k8s.namespace.labels.<key>` (not `io.kubernetes.pod.namespace.labels`:
that key validates but matches nothing).

Tenant namespaces carry Pod Security Admission labels (`charts/tenant`: `baseline`
enforced, `restricted` warned/audited) and every pod the API renders drops all
capabilities, forbids privilege escalation and runs under the runtime seccomp
profile (`services/api/jobs.py`; runner containers as uid 10001, the model
container as the image's user unless the job class sets `runAsNonRoot`).

A cluster-wide policy cannot express "my own namespace", so `charts/tenant`
renders a namespaced `CiliumNetworkPolicy tenant-local` (same-namespace ingress
and egress, needed for multi-pod runs) into each tenant namespace
(`charts/tenant/templates/network.yaml`, applied by `services/onboarding`).

Plain `networking.k8s.io/v1` equivalent (if Cilium policy must be avoided):
a per-namespace NetworkPolicy with `policyTypes: [Ingress, Egress]`, ingress
from `podSelector: {}`, egress to `podSelector: {}`, to `kube-system`
`k8s-app=coredns` :53, to the namespace `envoy-gateway-system`, and
`ipBlock 0.0.0.0/0 except [10.0.0.0/8]` plus the
API-server IPs as /32 entries (they change: that is why the Cilium entity is
used).

### API RBAC

`clusters/hub/apps/manifests/api/api.yaml` holds everything the `api`
ServiceAccount may do: ClusterRole `serverless2-api-tenant` (workflows, pods,
Secret `tenant-storage`; defined in `clusters/common/manifests/api-agent`)
bound per tenant namespace by the RoleBinding `charts/tenant` renders; Role
`serverless2-api-endpoints` in `models` (inferenceservices get/list/patch,
pods); read-only Roles `serverless2-api-templates` in the template namespaces
(`api`, `gromacs`); one ClusterRole to list tenant namespaces (billing).

### Tenants (`tenants/<name>.yaml`, `infra/tenant`, `charts/tenant`)

One file per tenant, one command. `tenants/<name>.yaml` holds the clusters the
tenant lives in (region, bucket endpoint; the `control` entry is the MultiKueue
manager) and `lifecycleDays` (retention of run outputs). `python -m onboarding
create-tenant <name> [--region r ...] [--budget $] [--lifecycle-days N]
[--skip-cloud]` (`--skip-cloud`: no Terraform, re-render the Kubernetes side
of an existing tenant from the state key files, e.g. after a fleet.yaml change
that adds a GPU class):

1. Terraform root `infra/tenant` (state key `tenants/<name>.tfstate` in the
   fleet's state bucket, `infra/backend.hcl`; `FLEET_BACKEND=local` for the
   pre-2026-10-07 files `infra/state/hub/tenants/<name>/`): per region a service account, a group with
   `storage.object-editor` on the tenant bucket through the bucket policy, the
   bucket `serverless2-<name>-<region>` with a lifecycle rule that expires
   `operations/` objects after `lifecycleDays` (default 90; aborts incomplete
   multipart uploads after 7 days), and the S3 access key (skipped for regions
   whose key file `infra/state/hub/tenants/<name>-<region>.s3.json` exists).
   Re-running is a no-op; adding a region or changing the retention is a
   values change plus a re-run.
2. `charts/tenant` rendered per cluster (with `fleet.yaml`) and applied
   server-side (field manager `onboarding`): namespace, run-pod identity
   (`job-runner`), API binding, one Kueue LocalQueue per scheduling profile
   (`default`, `prefer-<class>`), network policy; on the control cluster the
   namespace, queues and API binding. Pre-v2 objects (per-tenant ClusterQueue,
   Argo executor identity, artifact repository) are removed on a re-run.
3. Secrets in the tenant namespace (never in git): workers `tenant-storage`
   and `s3` (this region's bucket and key), `s3-fleet` (the hub region's bucket
   and key, used by fleet-placed runs), `ngc`; control `tenant-storage` (the
   hub region's bucket).
4. The LiteLLM key (budget, allowed models and pass-through routes).

Existing tenants were imported into their state (demo: 2026-10-06, 8 objects,
only the lifecycle rule and the SA description changed). Re-render the chart
by hand when needed:

```
helm template demo charts/tenant -f tenants/demo.yaml -f fleet.yaml --set cluster=eu-south1 | kubectl --context <ctx> apply --server-side --field-manager=onboarding -f -
```

Removing a tenant: delete its LiteLLM key, `terraform -chdir=infra/tenant
destroy` with its state (empties nothing: the bucket must be empty first),
delete the namespace in each cluster.

## Retire Argo Workflows (post-merge steps of lane F5)

The repository no longer has the `argo-workflows` component (2026-10-07). The fleet
ApplicationSets delete the `argo-workflows` (hub) and `argo-workflows-eu-south1`
Applications when the spec file disappears from `main`, but with
`preserveResourcesOnDeletion` the objects stay; remove them once, per worker:

```
for ctx in <hub> <eu-south1>; do
  kubectl --context $ctx delete ns argo --wait=false
  kubectl --context $ctx delete crd $(kubectl --context $ctx get crd -o name | grep argoproj.io | grep -v -E 'applications|applicationsets|appprojects' | sed 's#.*/##')
  kubectl --context $ctx delete clusterrole,clusterrolebinding -l app.kubernetes.io/instance=argo-workflows
  kubectl --context $ctx delete clusterrole,clusterrolebinding argo-ui-operator   # the old UI login (hub only)
  kubectl --context $ctx delete ns gromacs --wait=false                           # the hand-applied v1 run class
done
```

Then on the hub, whose database only held the Argo archive and the pre-cut-over LiteLLM
copy (LiteLLM runs on the control cluster since 2026-10-07):

```
kubectl --context <hub> -n data delete scheduledbackup postgres-daily
kubectl --context <hub> -n data delete cluster postgres     # CloudNativePG; the PVCs go with it
kubectl --context <hub> delete ns data litellm ui-app --wait=false
```

The hub's base backups under `s3://serverless2-backups-eu-north1/postgres/` (prefix
`postgres/`, the control cluster writes `postgres-control/`) can be deleted by hand; the
LiteLLM data they contain is older than the control cluster's live database.

## GitOps layout and the switch to ApplicationSets

Since 2026-10-06 each cluster's root Application syncs `clusters/<cluster>/apps/root/`,
two ApplicationSets: `platform` (one Application per `clusters/common/apps/*.yaml`;
Helm values in `clusters/common/values/`, manifests as kustomize overlays in
`clusters/<cluster>/apps/overlays/` over `clusters/common/manifests/`) and `models`
(one `model-<id>` Application per endpoint entry of `catalog/models/`, chart
`charts/endpoint` in catalog mode). Application names are unchanged from the
generated era, no Application carries a finalizer, and both ApplicationSets set
`preserveResourcesOnDeletion`, so no generator change ever deletes a resource.

Switch-over from the committed `applications/*.yaml` (one-time, per cluster,
hub first; each cluster has its own Argo CD):

1. Before merging: strip the Argo CD tracking annotation from the live
   `tenant-demo` network policy on both clusters so the `ops` app does not prune
   it when its git copy goes away (charts/tenant owns it now):
   `kubectl -n tenant-demo annotate ciliumnetworkpolicy tenant-local argocd.argoproj.io/tracking-id-`.
   Remove the post-delete finalizers of the `nvidia-device-plugin` Application
   (`kubectl -n argocd patch app nvidia-device-plugin --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]'`):
   they are the only Application finalizers, and they would run the chart's
   post-delete hook when the root app prunes the old Application object.
2. Merge and push. The root app still points at `clusters/<cluster>/apps/applications`,
   which no longer exists: it reports the path missing and changes nothing.
3. `kubectl apply -f clusters/<cluster>/apps/root-app.yaml` (new path `apps/root`).
   The root app prunes the 25 (hub) / 19 (eu-south1) old Application objects
   (no finalizers: their resources stay) and creates the two ApplicationSets,
   which create the same-named Applications within seconds; the resources'
   tracking annotations already carry those names, so they show Synced without
   any apply. `models` (old, single app) is pruned and `model-<id>` adopt its
   InferenceServices (tracking annotation rewritten on the first sync; the
   objects are identical, verified by rendering diff).
4. First syncs with real changes: jobset (cert-manager-issued webhook
   certificate, no Replace), scheduling (removes LocalQueue `models/default`,
   on eu-south1 also `tenant-template`, the `spikes` namespace and its
   LocalQueue), ops (the tenant-demo policy copy, kept alive by step 1),
   kube-prometheus-stack (Grafana dashboard provider `Upstream` + NVIDIA DCGM
   dashboard download; Grafana pod restarts), observability (dashboards without
   the GPU row). Check: `kubectl -n argocd get app` all Synced/Healthy,
   `kubectl -n tenant-demo get cnp tenant-local`, Grafana shows the Upstream
   folder, endpoints unchanged (`kubectl -n models get isvc`).
Lessons from the 2026-10-06 switch-over (both clusters):

- Stripping the tracking annotation from the live `tenant-local` policy did not
  stop the prune: app `ops` still had the object in its last cached comparison
  and pruned it on the first sync. Re-apply it from the chart afterwards
  (`helm template demo charts/tenant -f tenants/demo.yaml --set cluster=<c>`,
  only the CiliumNetworkPolicy document, `kubectl apply --server-side
  --field-manager=onboarding`), or re-run onboarding for every tenant.
- Any change of an Application's name re-creates every Knative revision it
  owns: KServe copies the InferenceService annotations, including
  `argocd.argoproj.io/tracking-id`, into the Knative revision template. Each new
  revision needs a GPU for its verification pod; with min-scale 1 endpoints and
  busy pools the pool must have a free GPU per endpoint, or the revision hits
  the Knative progress deadline (`Initial scale was never achieved`; delete the
  failed Revision object and Knative recreates it). The `model-<id>` names are
  therefore permanent.
- Argo CD re-adds the post-delete finalizer on `nvidia-device-plugin` (the chart
  has a post-delete hook); it only matters if that Application is deleted.

5. Rollback: `kubectl apply` the previous `root-app.yaml` (path `apps/applications`)
   from the pre-merge commit and `git revert` the merge; the ApplicationSets are
   pruned with `preserveResourcesOnDeletion`, the old Applications come back
   under the same names and adopt the resources again.

Day-to-day: add or bump a chart in `clusters/common/apps/<name>.yaml` (+ values
file), add a model in `catalog/models/` (Argo CD creates `model-<id>` on every
cluster that has a deployment), change a cluster value in its overlay.

## Edge: certificates, regional API, rate limits

- **Certificates** are ACME (Let's Encrypt) through the
  Gateway API HTTP-01 solver; see docs/EDGE.md. Status: `kubectl -n
  envoy-gateway-system get certificate,certificaterequest,order,challenge`.
  A failed order never touches the Secret the listener uses.
  New hostname (model or route): add it to the SAN list of
  `clusters/<cluster>/apps/overlays/gateway/tls.yaml`.
- **Regional API** (`api` app on every cluster): needs Secret
  `api/litellm-master` in that cluster (copy of the hub's
  `litellm/litellm-master`; `kubectl --context <region> -n api create secret
  generic litellm-master --from-literal=masterkey=...` or `python -m
  onboarding bootstrap --region <region>`), and the tenant RoleBindings that
  `charts/tenant` renders per cluster. It does not carry region kubeconfigs.
- **Rate limits** live in `clusters/common/manifests/api/api.yaml` (API route)
  and `clusters/common/manifests/gateway/ratelimit.yaml` (everything else),
  counted by the Envoy rate-limit service (`envoy-gateway-system`, deployed by
  the chart when `config.envoyGateway.rateLimit` is set) in
  `ratelimit-redis`. Redis is not persistent: a restart resets the windows;
  if it is unreachable Envoy fails open. Check: `kubectl -n
  envoy-gateway-system get deploy envoy-ratelimit ratelimit-redis`; test with
  two keys, a burst beyond 600/min on `/v1/models` returns 429 for that key only.

## Capacity for runs and endpoints (readiness gap 7)

Kueue admits run pods against the `default` ClusterQueue, but it does not see
endpoint pods, and a pool has a maximum (`max_nodes` per pool in
`fleet.yaml`; `endpoint_floor_gpus` there is the warm-endpoint deduction that
`charts/fleet` applies, docs/FLEET.md). Rule: **ClusterQueue nominal GPU quota =
pool maximum minus the GPUs held by endpoints with a warm floor
(`autoscaling.knative.dev/min-scale` >= 1 in the catalog)**. Today: hub
`h100-spot-1x` max 8, no warm endpoint, quota 8; eu-south1 `rtx6000-spot-1x`
max 4, one warm endpoint (nemotron-speech), quota 3. Both live in
`clusters/<cluster>/apps/overlays/scheduling/pool.yaml` and must change
together with the tfvars. Cold endpoints (min-scale 0) borrow a GPU only while
they serve; an admitted run can wait Pending for one scale-to-zero period
(60 s grace) in the worst case. Alerts `RunPodPendingLong` (admitted run pods
Pending > 10 min) and `KueueWorkloadsPendingLong` (quota exhausted > 15 min)
in `clusters/common/manifests/observability/rules-capacity.yaml`.

When a tenant needs more: raise `max_nodes` (Terraform apply), the quota, and
if it is a sustained need, a reserved (non-spot) pool as a second flavor
ahead of the spot one in the ClusterQueue (flavor order = preference).

## Add a region

1. Project + subnet in the new region; add the region to `fleet.yaml`
   (copy the `eu-south1` entry: set `allowed_cidrs` to the ops host and the
   hub egress IP, `ops.registries` to the registries the region needs, the
   GPU pools); `infra/fleet/apply.sh plan <region id>` then `apply` creates
   the cluster, system + GPU node groups, node SA, the ops SA/group/editor
   permit and the registries with its own state file (`docs/FLEET.md`,
   `infra/cluster/README.md`).
   Bootstrap Argo CD as in `spikes/S0-argocd`.
2. GitOps: copy `clusters/eu-south1/apps/{root,overlays,bootstrap.sh,root-app.yaml}`
   to `clusters/<region>/apps/` and set the dozen cluster values: `cluster:` in
   both ApplicationSets (and the `deployments.<region>` selector keys of the
   models one), the gateway hostname and certificate (`overlays/gateway`), the
   Knative domain (`overlays/knative-serving`), the pool flavor and quotas
   (`overlays/scheduling/pool.yaml`), project, node-group id and ops image
   registry (`overlays/ops`), the LiteLLM address (`overlays/edge`), the API
   region/hostnames (`overlays/api`) and the UI route hostnames (`overlays/ui`).
   Everything else comes from `clusters/common`.
3. Secrets only (not in Terraform): an auth key for the ops SA
   (`nebius iam auth-public-key create` for `terraform output
   ops_service_account_id`), Secrets `ops/nebius-sa` (`<region>.json`) in the
   new cluster and `ops/nebius-sa-<region>` on the hub; `ngc` pull secret in
   `models`; `api/litellm-master` for the regional API.
4. Mirror the ops image first (the region's CronJob needs it), then every
   catalog image for that region; add `deployments.<region>` to the catalog
   entries, render, push.
5. Tenants: add the region to `services/onboarding` `REGIONS` (project, kube
   context, flavor, limits); onboarding with `--region <region>` creates the
   bucket in that project and renders `charts/tenant` for that cluster; the
   tenant namespace label makes `tenant-isolation` apply.

## Patching (docs/SECURITY-PREREVIEW.md F3)

- `.github/workflows/ci.yml` runs on every push and pull request: `terraform fmt`/`validate`
  (infra/cluster, infra/tenant), `helm template` of the charts with the repo's values,
  `kustomize build` of every overlay, the API and dispatcher tests, and a `trivy` scan of the
  five images built from `services/*/Dockerfile` (fails on CRITICAL, unfixed ignored).
- Cadence: third-party chart versions (`clusters/common/apps/*.yaml`) and image bases are reviewed
  monthly; a critical CVE in our images or in an internet-facing component (Envoy Gateway,
  LiteLLM, the API) is patched within 7 days.
- Rolling an image: build and push a new tag (docs/IMAGES.md "Pushing an image"), change the tag
  in the manifest (`clusters/common/manifests/*`), the catalog, or `RUNNER_IMAGE`, commit; the
  caches fetch the new tag on first pull. Rolling a chart: bump `version:` in
  `clusters/common/apps/<name>.yaml`, render it locally (`helm template` with the values file),
  commit; check `kubectl -n argocd get app` and "Checks after any change".

## Checks after any change

```
kubectl -n argocd get app                                   # all Synced/Healthy
kubectl -n ops get cronjob; kubectl -n ops logs -l app=recover-stopped-nodes --tail=3
kubectl -n data get backup | tail -3; kubectl -n api get job | tail -3   # CNPG backups; billing CronJob runs
kubectl get ccnp tenant-isolation -o jsonpath='{.status.conditions[0].status}'
```

## Terraform solution (2026-10-08): rotation and take-over

There are no rotation CronJobs in the library form: every credential is a Terraform resource.
`./stack.sh tf cloud -- apply -replace=random_password.litellm_master` (then `apply platform` on every
cluster) rotates the LiteLLM master key; `-replace='nebius_iam_v1_auth_public_key.ops["<project>"]'` the
ops service-account key; on a worker `./stack.sh tf platform <id> -- apply
-replace='kubernetes_secret_v1.sa_token["multikueue"]'` (then `apply platform control`) the MultiKueue
token; the cache's registry static key: delete `stack/.secrets/<name>-registry-key.json`, re-apply
platform on every cluster, retire the old key with `nebius iam static-key delete`. Take-over by another
operator: the tfvars, a key for the state bucket (`stack/bootstrap/state-bucket.sh` with their profile),
`./stack.sh plan` must show no changes. `recover-stopped-nodes` still runs on every worker.
