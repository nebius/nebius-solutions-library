# Scheduling: GPU classes, regions, cost and resume

*For architects and operators: how a run is admitted, where it is placed across GPU classes and regions, and how prices enter the decision.*

How a workload (endpoint pod or job) ends up on a GPU, and what a model
declares to steer that. Three layers, all existing projects, one small
dispatcher of our own (`services/dispatcher`, about 260 lines plus a 65-line price feed).

| Layer | Decides | Project |
|---|---|---|
| In a cluster: which pool (GPU class, spot/reserved) | Kueue ResourceFlavors in ClusterQueue order, `flavorFungibility` | Kueue 0.20 |
| Across clusters: which region/cluster | Kueue MultiKueue with an external dispatcher that ranks clusters by preference, free quota and live price | Kueue MultiKueue + `services/dispatcher` |
| Nodes | node-group autoscaler per pool (0..max from `fleet.yaml`) | Nebius mk8s |

## What a model declares

Endpoint entries (`catalog/models/<id>.yaml`, `runtime` block, see
`charts/endpoint/values.yaml`):

```yaml
runtime:
  image: <registry>/<repo>:<tag>          # must run on every pool the queue may choose
  queue:
    name: prefer-h100                     # LocalQueue in `models` = preference profile; omit to pin
    priorityClass: customer-batch         # Kueue WorkloadPriorityClass
  pool: h100-spot-1x                      # pinned placement when queue.name is unset
  images:                                 # per-pool image, applied only when pinned (pool known at render time)
    h100-spot-1x: <registry>/<repo>:<tag>-h100
    rtx6000-spot-1x: <registry>/<repo>:<tag>-rtx6000
```

- `queue.name` set: predictor pods carry `kueue.x-k8s.io/queue-name` (KServe
  copies predictor labels to the Knative revision pods), the `models`
  namespace is Kueue-managed (`serverless2.nebius/kueue: managed`,
  `clusters/common/manifests/scheduling/namespaces.yaml`), so every replica is
  gated at creation, gets quota from the ClusterQueue behind the LocalQueue
  and is released with the pool's `nodeSelector` and tolerations injected.
  The chart renders no pool `nodeSelector` in this mode.
- `queue.name` unset: the chart pins `nodeSelector: serverless2.nebius/pool:
  <pool>` as before and, when `images.<pool>` exists, uses that image.
- Run classes declare `gpu.classes` (preferred first): the API sets the
  queue label to `prefer-<first class>` and a `nodeAffinity` over the fleet
  pools of all listed classes (so a fallback never lands on an unsupported
  GPU); image references are the same in every region (`docs/IMAGES.md`).
  `docs/JOBS.md`.

Preference profiles are the ClusterQueues `charts/fleet` renders from
`fleet.yaml` (`docs/FLEET.md`): `default` and one `prefer-<gpu_class>` per
class, on every worker (flavor order = the preference) and on the MultiKueue
manager (same names, with the MultiKueue admission check). A LocalQueue in
the tenant or `models` namespace points at one of them; the dispatcher reads
the profile from the manager ClusterQueue name. "If the GPU type is busy use
another one; wait if all are busy" is Kueue's flavor fungibility inside a
cluster and the dispatcher's ranking across clusters.

## Per-GPU images for runs

A run class may ship one image per GPU class, for builds that are tuned for a GPU:

```yaml
gpu: { count: 1, classes: [l40s, h100] }          # preferred first
job:
  images:                                          # one image per class; keys = `default` or a class above
    default: registry.serverless2.local/nebius/my-model:1.0
    h100:    registry.serverless2.local/nebius/my-model:1.0-h100
    l40s:    registry.serverless2.local/nebius/my-model:1.0-l40s
```

Kueue picks the pool at admission and a Job's image cannot change afterwards (see "Not possible"
below), so for such a class the GPU class is chosen **at submission** (option A of
`docs/DESIGN-REVIEW-2026-10-09.md`): the API asks the dispatcher's ranking (`GET /v1/rank`: the
class preference, reserved before on-demand before spot, price, free capacity now, the caller's
region as a pin when given), renders the Job with the best class's image, limits its node affinity
to that class's pools and queues it on that class's profile (`prefer-<class>`). The operation and
every attempt record carry the class (`gpu_class`, plus the image under `image`). The run may still
move between regions of its class while it waits (MultiKueue), and a resume keeps the class: the
checkpoints were written by that image. If the dispatcher is unreachable at submission, the
preferred class that has a pool is taken and a warning is logged.

The trade-off, by design: a run with per-class images waits for its class instead of switching
class while queued. A class with one image keeps today's behaviour (free class switching at queue
time). The documented follow-up "A+" re-renders a run that has not started after N minutes for the
next class in its preference list (cancel the gated Job, render again with the next class's image,
record the switch as an attempt, switch only downward in the list and at most a bounded number of
times); it is not built, because it adds a control loop with retry semantics for a situation that
has not hurt yet. `services/api/placement.py`, `services/dispatcher` (`rank_response`).

## Cross-cluster dispatch (`services/dispatcher`)

Kueue MultiKueue in external-dispatcher mode (`multiKueue.dispatcherName:
serverless2.nebius/cost-dispatcher` in the manager's Kueue config, commented
block in `clusters/common/values/kueue.yaml`): Kueue creates the Workload on
the manager and waits for `status.nominatedClusterNames`; the dispatcher sets
it; Kueue then copies the job to the nominated worker(s), the first worker
that admits wins and `status.clusterName` is fixed.

Every `INTERVAL_S` (15 s) the dispatcher, for each Workload with quota reserved
and the MultiKueue admission check pending:

1. Candidates are `(region id, pool)` pairs from the `fleet-prices`
   ConfigMap (`pools.yaml`, rendered by `charts/fleet` from `fleet.yaml`:
   class, capacity type, list price, project, platform, preset) whose GPU
   class the profile allows: `prefer-<class>` = that class first, then the
   other classes by their cheapest pool; `default` = any class. A
   `serverless2.nebius/region` label on the Workload (region id such as
   `eu-south1`, or the region name from the MultiKueueCluster label)
   restricts candidates to that cluster (resume, see below).
2. A candidate is free when the worker's `default` ClusterQueue has enough
   unused nominal `nvidia.com/gpu` quota in that pool's flavor (read through
   the MultiKueueCluster kubeconfig Secrets; nominal only, borrowing is
   ignored).
3. Order: free first; `prefer-<class>` = class preference, then reserved
   before on-demand before spot (`fleet.capacity_order`), then price;
   `default` = price only. Reserved pools cost their marginal price (0),
   spot pools the live price from `spot-prices` when present, else the list
   price from `fleet-prices`.
4. The best candidate's cluster is nominated. Nothing free: the best
   cluster is nominated anyway and the job queues there. After
   `RENOMINATE_AFTER_S` (300 s) without admission the next cluster is
   appended; nominations are never withdrawn (Kueue's external-dispatcher
   contract).

Nominations are written with SERVER-SIDE APPLY as field manager `kueue-admission`
(dispatcher 0.1.5; 0.1.4 shipped the call on a client that rejected the apply content type): Kueue admits a MultiKueue workload by setting `status.clusterName`
and clearing `nominatedClusterNames` in one server-side apply under that manager, and a
value written with a merge patch (an "Update" managedFields entry, even under the same
manager name) survives that apply, so the API server rejects Kueue's patch
("clusterName and nominatedClusterNames are mutually exclusive") and the manager
Workload stays Pending for its whole life (dispatcher 0.1.2/0.1.3, 2026-10-07). The apply
restates `status.admission` and `status.admissionChecks` as read (dispatcher 0.1.6): the same
manager name owns Kueue's quota reservation and the check states, and an apply that names
only `nominatedClusterNames` releases them, so the manager scheduler re-admitted the
workload, copied it to a cluster that was not nominated, and after the resulting retry the
next apply was rejected ("admissionChecks[0].state: Required value") for ever (measured
2026-10-08 on the fresh-deploy test fleet, docs/VERIFICATION.md). Prices:
the `price-feed` CronJob (every 10 min) asks the Nebius price calculator for
each spot/on-demand pool of `fleet-prices` (`nebius billing v1alpha1
calculator estimate`; for spot a preemptible VM that follows the spot price:
`--resource-spec-compute-instance-spec-preemptible-on-preemption STOP
--resource-spec-compute-instance-spec-follows-spot-price`, the current quote,
which Nebius moves every 15 minutes since dynamic spot pricing started on
2026-10-08) and writes `spot.json` (USD per GPU-hour, keyed `<region
id>/<pool>`) into the ConfigMap `kueue-system/spot-prices`, which it creates
on first run (runtime-owned, outside Terraform). Until
2026-10-09 the feed asked with `--preemptible-priority 1`, a field the API
deprecated on 2026-05-11; the CLI 0.12 rejects it (exit 4), so the feed wrote
an empty map and every spot pool was ranked at its list price (seen on the
test fleet s2pr2, dispatcher 0.1.8). Quotes seen on 2026-10-09 for the 8-GPU
B300 preset following the spot price: eu-west2 7.92 USD/h (0.99 per
GPU-hour), us-north1 28, uk-south1 75.92; regular 76 everywhere. Measured
2026-10-07: H100 1-GPU preset 4.5 on-demand, 2.15 spot. The calculator needs
an SA key: Secret `kueue-system/price-feed-nebius-sa` (optional mount;
without it the feed logs failures and list prices are used).

Spot pools in the ranking: a spot pool is a candidate like any other, with no
waiting period; inside one GPU class it sorts after the class's reserved and
on-demand pools (`capacity_order`, capacity-first), and among spot pools the
live quote decides. A pool with `max_price` is a capped pool (pricing policy:
its nodes stop above the cap and return below it); a pool without one follows
the spot price and is stopped only when Nebius reclaims the capacity. Either
way a run on a preempted node resumes as described below, from its network
checkpoint volume; local NVMe scratch (`scratch: local-nvme`) does not
survive a preemption (docs/FLEET.md "Spot capacity").

Packaging: `clusters/common/manifests/dispatcher` (Deployment, CronJob,
RBAC; image `serverless2/dispatcher:0.1.1` from `services/dispatcher/
Dockerfile`, the ops image's Nebius CLI on `python:3.12-slim`), overlay
`clusters/control/apps/overlays/dispatcher`, app spec staged in
`clusters/common/apps-staged/dispatcher.yaml` until the control cluster
exists (same cut-over as `fleet.yaml`).

Decisions are logged (`kubectl -n kueue-system logs deploy/dispatcher`) as
`<ns>/<workload> profile=... pin=... gpus=N -> [clusters] best=(cluster,
flavor, price, free)`.

## Images: one host, cached per region

Every image reference names the logical host `registry.serverless2.local`
(`docs/IMAGES.md`): on each node containerd resolves it to the node's Spegel
peers and then to the cluster's Zot pull-through cache, which fetches from
the source registry (fleet.yaml `images.source`, our builds), NGC, GHCR,
Docker Hub, Quay or registry.k8s.io on demand and keeps the layers in the
region. A Job therefore contains nothing that depends on the region it lands
in: every fleet run is created unpinned and the dispatcher places it at
queue time, re-nominating the next cluster every `RENOMINATE_AFTER_S` while it
waits (measured 2026-10-07: eu-south1 on Hold -> hub nominated after 300 s,
MultiKueue ran the Job there). The region the API reports is the cluster that
ADMITTED the run (Workload `status.clusterName`); a nomination is not a
placement. The work volume is created where the run is admitted, never at
nomination (`services/dispatcher`).

## Resume in the same region

A job that is retried after a spot eviction must restart where its data is.
`labelKeysToCopy` in the Kueue config copies `serverless2.nebius/region` and
`serverless2.nebius/profile` from the Job to its Workload. The API sets
`serverless2.nebius/region: <region>` on a resumed Job from the original's
Workload `status.clusterName` (and on every pinned run); the dispatcher then
only nominates that cluster. A new job without the label may go anywhere its
profile, `gpu-classes` and `regions` annotations allow.

## Reading the state

```sh
kubectl -n <ns> get workloads                                  # admitted, pending
kubectl -n <ns> get workload <name> -o jsonpath='{.status.nominatedClusterNames} {.status.clusterName}'
kubectl -n <ns> get workload <name> -o jsonpath='{.status.admission.podSetAssignments[0].flavors}'
kubectl get clusterqueue default -o jsonpath='{.status.flavorsUsage}'   # per-flavor usage vs nominal
kubectl -n kueue-system get cm spot-prices -o jsonpath='{.data.spot\.json}'
```

## Verified on the hub (2026-10-07)

- Pod in a managed namespace with `kueue.x-k8s.io/queue-name` and
  `nvidia.com/gpu: 1`, no nodeSelector: Workload admitted in flavor
  `h100-spot-1x`, pod released with `nodeSelector serverless2.nebius/pool:
  h100-spot-1x` and the `nvidia.com/gpu` toleration injected; after deleting
  the pod the Workload is gone and `flavorsUsage` is back to 0. The Knative
  path is the same pod integration (Kueue's `deployment` integration only
  propagates the label to the pod template); verify one catalog model with
  `queue.name` after merge.
- Ranking: `services/dispatcher/tests` (11 cases) covers the
  `fleet-prices` parsing, profile-from-queue-name, preference, fallback
  class, cheapest with live price, nothing-free, region pin, multi-GPU
  quota, renomination timing; a smoke run against the chart-rendered
  `pools.yaml` of today's fleet gives `prefer-h100` with H100 busy ->
  eu-south1 RTX PRO 6000 (cheapest free class), `prefer-l40s` -> hub L40S.
- Manual end-to-end test for lane F2 (needs the control cluster from F1):
  set `multiKueue.dispatcherName` on the manager's Kueue, create the
  MultiKueueCluster Secrets, move `fleet.yaml` and `dispatcher.yaml` from
  `clusters/common/apps-staged`, submit a Job to a LocalQueue on
  `prefer-rtx-pro-6000`, expect `nominatedClusterNames: [eu-south1]` within
  15 s and admission on eu-south1; then label a Job
  `serverless2.nebius/region: hub` and expect nomination of `hub` only.

## Not possible: swapping the image after Kueue picked the pool

Measured 2026-10-07 with Kyverno 3.9.1 on the hub: a mutating policy that
sets the container image from `serverless2.nebius/image.<pool>` annotations
when the pool `nodeSelector` appears works on CREATE (pinned pods), but on the
UPDATE in which Kueue injects the nodeSelector and removes the gate, Kueue's
pod integration compares the pod spec with the Workload's pod set, finds the
image changed, logs `No matching Workload; restoring pod templates according
to existent Workload` and deletes the pod (Kueue `jobframework.
EquivalentToWorkload` compares the pod template including images; Workload
pod sets are immutable once quota is reserved). The same holds for Jobs.
Therefore per-GPU image tags are decided before the Job exists: for endpoints
by the pinned pool (`images.<pool>`), for runs by the class chosen at submission
("Per-GPU images for runs" above). A Kueue-placed workload with one image must
run on every pool its profile allows. Kyverno was removed again; nothing of it
remains on the hub.

## Terraform solution (2026-10-08): tenant inputs

`tenants.<name>.gpu_quota`, `fair_share_weight` and `dedicated_pools` exist in the tfvars schema. The
charts render the shared profile queues for every tenant (all tenants share the fleet's `default` and
`prefer-<class>` ClusterQueues, which is the right side of Kueue's reclaim semantics: priority across
queues with separate floors starves the lower queue); per-tenant ClusterQueues in the cohort (nominal 0,
`borrowingLimit = gpu_quota`, `fairSharing.weight`, flavors restricted to `dedicated_pools`) are the
documented design for those inputs and are not rendered yet (the inputs are accepted, validated and
carried on the tenant release values). Kueue's fair-share usage sums resource magnitudes without
normalisation; set weights per GPU class, not per CPU/memory. The MultiKueue manager cannot be its own
worker: that is why `control_plane.dedicated = false` is a single-cluster mode without MultiKueue.
