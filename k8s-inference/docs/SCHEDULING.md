# Scheduling: GPU classes, regions, cost and resume

*For architects and operators: how a run is admitted, where it is placed across GPU classes and regions, and how prices enter the decision.*

How a workload (endpoint pod or job) ends up on a GPU, and what a model
declares to steer that. Three layers, all existing projects, one small
dispatcher of our own (`services/dispatcher`, about 540 lines, plus the InfiniBand floor (230) and a 70-line price feed).

| Layer | Decides | Project |
|---|---|---|
| In a cluster: which pool (GPU class, spot/reserved) | Kueue ResourceFlavors in ClusterQueue order, `flavorFungibility` | Kueue 0.20 |
| Across clusters: which region/cluster | Kueue MultiKueue with an external dispatcher that ranks clusters by preference, free quota and live price | Kueue MultiKueue + `services/dispatcher` |
| Nodes | node-group autoscaler per pool (0..max from `fleet.yaml`); InfiniBand pools: the dispatcher moves the pool's floor for multi-node runs (below) | Nebius mk8s + `services/dispatcher` |

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
  `charts/platform/templates/scheduling/namespaces.yaml`), so every replica is
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
the design review of 2026-10-09): the API ranks the pools of the fleet document (`fleet-prices`) the way the
dispatcher ranks clusters (`services/api/placement.py` mirrors `rank()` below: the class preference,
reserved before on-demand before spot, then the cost of the run on the node the pool would boot at its list
price, so a 1-GPU run does not pick an 8-GPU pool; the model's regions, the caller's region as a pin when
given, a tenant's dedicated pools for that tenant only), renders the Job with the best class's image, limits
its node affinity to that class's pools and queues it on that class's profile (`prefer-<class>`). The
operation and every attempt record carry the class (`gpu_class`, plus the image under `image`). The run may
still move between regions of its class while it waits (MultiKueue), and a resume keeps the class: the
checkpoints were written by that image. What the API does not see is free quota right now and the live spot
quote; until api 0.11.13 it asked the dispatcher for them over HTTP (`GET /v1/rank`), a round trip, a
Service and a timeout whose only gain was that signal, while the queue waited for the preferred class
either way. A class without a pool in the fleet document is skipped; with no pool at all the preferred class
is taken and the queue waits for it.

The trade-off, by design: a run with per-class images waits for its class instead of switching
class while queued. A class with one image keeps today's behaviour (free class switching at queue
time). The documented follow-up "A+" re-renders a run that has not started after N minutes for the
next class in its preference list (cancel the gated Job, render again with the next class's image,
record the switch as an attempt, switch only downward in the list and at most a bounded number of
times); it is not built, because it adds a control loop with retry semantics for a situation that
has not hurt yet. `services/api/placement.py`.

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
a test fleet, dispatcher 0.1.8). Quotes seen on 2026-10-09 for the 8-GPU
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

Packaging: `charts/platform/templates/dispatcher` (Deployment, CronJob,
RBAC; image `serverless2/dispatcher:0.1.1` from `services/dispatcher/
Dockerfile`, the ops image's Nebius CLI on `python:3.12-slim`), overlay
`clusters/control/apps/overlays/dispatcher`, app spec staged in
`clusters/common/apps-staged/dispatcher.yaml` until the control cluster
exists (same cut-over as `fleet.yaml`).

Decisions are logged (`kubectl -n kueue-system logs deploy/dispatcher`) as
`<ns>/<workload> profile=... pin=... gpus=N -> [clusters] best=(cluster,
flavor, price, free)`.

## InfiniBand pools grow on demand (2026-10-11)

The Nebius cluster autoscaler never adds an InfiniBand node: it simulates the new node without the DRA
attributes of its RDMA NICs, so a pod that claims them never triggers a scale-up, from zero or from one
(`cannot allocate all claims`, `CEL runtime error: no such key: ifName`; docs/JOBS.md "Multi-node runs").
Until now an operator raised the pool's `min_nodes` in `terraform.tfvars` and applied the cloud stage before
every multi-node run. The dispatcher's elected replica does it now (`services/dispatcher/infiniband.py`, every
reconcile cycle, 15 s), per worker cluster:

1. **Demand** per InfiniBand pool = the largest node count any one multi-node run or endpoint needs there now.
   Runs: the worker's Kueue Workloads whose pod template requires the node label
   `serverless2.nebius/interconnect=infiniband` (what `interconnect: required` renders), pending or admitted and
   not finished; `nodes` = the pod set's count; the pool = Kueue's flavor assignment once quota is reserved, before
   that the InfiniBand pool of the template's `serverless2.nebius/pool` affinity that Kueue takes first (reserved,
   on-demand, spot, then price). A `preferred` run counts once Kueue has put it on an InfiniBand flavor. Endpoints:
   LeaderWorkerSets (docs/API.md "Multi-node endpoints") with `replicas > 0` whose pods pin an InfiniBand pool
   (`size x replicas` nodes). Every run holds whole nodes of its own and nothing else grows the pool, so the floor
   serves the biggest one; smaller ones queue behind it in Kueue as before.
2. **The floor** = max(the fleet's `min_nodes`, demand), never above `max_nodes`: a run that needs more nodes than
   the pool has can never fit there (logged once, nothing changed; a bigger `max_nodes` is a fleet change). A raise
   is applied at once: `nebius mk8s node-group update --id <node group> --patch --autoscaling-min-node-count N`
   as the region's ops identity (the platform stage mounts one `<region>.json` key per GPU region into the
   dispatcher, Secret `kueue-system/price-feed-nebius-sa`; `nebius_cli.py` turns each into the profile
   `sa-<region>` exactly as the ops image does). Managed Kubernetes then boots the nodes, DraNet publishes their
   NICs and Kueue's admitted pods bind; nothing in the run changes. A lower waits until demand has stayed below
   the floor for `RESTORE_AFTER_S` (600 s): a restart-all after a preemption finds its nodes still there, and a
   run that follows another reuses them instead of waiting for a boot.
3. **State**: what the dispatcher set is kept in the ConfigMap `kueue-system/infiniband-pools` (`state.json`,
   created on first use, runtime-owned), so a restarted dispatcher still restores the fleet's value when the pool
   goes idle; while every pool is at its fleet value the state is empty.

What it needs from the fleet document: `charts/fleet` renders `min_nodes` and the pool's `node_group_id` into
`fleet-prices` (the platform stage adds the ids from the cloud state; a pool the cloud stage has not applied yet
has none and is left alone), and the worker identity of the manager may list LeaderWorkerSets
(`charts/platform/templates/fleet-access/multikueue.yaml`). `INFINIBAND=0` on the Deployment switches the whole
step off; pools without `interconnect: infiniband` are never touched, whatever runs on them.

**Terraform drift, accepted by design.** While a run waits or runs, the node group's `autoscaling.min_node_count`
differs from `terraform.tfvars`: `terraform plan` of the cloud stage shows the pool's floor going back to
`min_nodes`, and an `apply` in that window lowers it (running pods keep their nodes; the autoscaler removes idle
ones, and the dispatcher raises the floor again on its next cycle if the run still waits). Once no multi-node
run has needed the pool for `RESTORE_AFTER_S`, the dispatcher restores the configured value and the plan is clean
again. Apply the cloud stage in a quiet hour on InfiniBand pools, or read the state ConfigMap first:

```sh
kubectl -n kueue-system get cm infiniband-pools -o jsonpath='{.data.state\.json}'     # {} = every pool at its fleet value
kubectl -n kueue-system logs deploy/dispatcher | grep min_nodes                        # every change, with its reason
```

Not covered: single-cluster mode (`control_plane.dedicated = false` runs no dispatcher: set `min_nodes` by hand
there), and the quota side (the pool's Kueue quota is `max_nodes` x GPUs already, so a run is admitted before
its nodes exist; that is what makes the floor move).

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
## Fallback to the next class

A run that names `execution.placement.gpu_preference` and `fallback_wait_s` (docs/JOBS.md "Execution options") is
rendered for the first class of its preference (that class's image, `prefer-<class>` queue, the class's pools as
affinity). The control API's reconcile loop (every 15 s) looks at fleet runs that are still unplaced (no worker
admitted their Workload) after the wait bound and moves each to the next class of its preference
(`jobs.switch_class`): `<root>-r<n>` is created for the next class with the remaining preference, carrying
`serverless2.nebius/fallback` ("rtx-pro-6000 -> l40s after 60 s without capacity"), which the operation reports as
`reason` and `resumed_from`; the original Job stays as a readable handle (status `SUPERSEDED`, its Workload
deactivated and the Job suspended so it never admits), names its `successor`, and `GET /v1/operations/<original>`
returns the successor's state with `requested_id`; the fleet database keeps the link past the Job's lifetime. The last class of the preference
keeps waiting (Kueue reports why: `reason` carries the Workload's own condition message while an operation is
QUEUED). Capacity that becomes free in a worse class is never taken silently: the preference order is the caller's.

A resume with `checkpoint_ref` (docs/JOBS.md "Attempts and finalization") goes through the same placement with an
optional `gpu_class`: the new attempt gets that class's image, a fresh volume where it lands, and the pinned model
revision (`serverless2.nebius/model-version`), never a newer definition.

- Manual end-to-end test of MultiKueue placement:
  set `multiKueue.dispatcherName` on the manager's Kueue, create the
  MultiKueueCluster Secrets, move `fleet.yaml` and `dispatcher.yaml` from
  `clusters/common/apps-staged`, submit a Job to a LocalQueue on
  `prefer-rtx-pro-6000`, expect `nominatedClusterNames: [eu-south1]` within
  15 s and admission on eu-south1; then label a Job
  `serverless2.nebius/region: hub` and expect nomination of `hub` only.

## Automatic recovery after infrastructure loss (2026-10-12)

Opt-in per run: `execution.recovery.on_infrastructure_loss: recover` (docs/JOBS.md "Execution options"; the default
`retry` and `fail` are unchanged). The Job keeps the `retry` policy, so a disruption first gets the Kubernetes Job
controller's replacement attempt on the same worker and volume. The control API's reconcile loop (every 15 s) then
watches the run: it is **lost** when a confirmed infrastructure loss is on record and no attempt is RUNNING: the newest
attempt that ran ended `PREEMPTED` because its pod carried `DisruptionTarget` or was deleted (preemption, drain, a
spot reclaim: the uploader's `interrupted` record), because its `started` record has no end and the pod no longer
exists on the worker (a node lost without warning; the worker's pod listing must have succeeded), or because Kueue
evicted the Workload after the run had started. Never a loss: an application failure (`main` exit code, including
`OOMKilled`, which reports `FAILED` and not `PREEMPTED`), `DeadlineExceeded`, a cancel, a finished run, a run that
never started, and a worker whose Kubernetes API is unreachable (the tick skips it).

The first tick that sees the loss stamps `serverless2.nebius/lost-at` on the manager Job (cleared when an attempt runs
again). Once the loss stands for `wait_s` (default 300), the chain has an acknowledged checkpoint pointer (the newest
`CHECKPOINT.json` the uploader recorded, docs/JOBS.md "Attempts and finalization"), fewer than `max_recoveries`
(1..3, default 1) automatic continuations exist and `deadline_s` (from the root's creation) has not passed, the API
(`jobs.recover`): delivers the private input for the successor (a revoked binding stops here, the original untouched);
fences the original with a successor mark in the fleet database (every further attempt record of it, pointers and
finals alike, is refused with 409), deactivates its Kueue Workload (the worker's copy and any pod, a pending
same-worker replacement included, go) and suspends the Job; creates the successor `<root>-r-<sha8>` with
`sha8 = sha256("<original>:recover:<lost epoch>")[:8]` (a deterministic name: competing API replicas and repeated
ticks converge on one Job, the create is ignore-exists) from the pointer (`CHECKPOINT_REF`) on the next class of
`placement.gpu_preference` after the lost attempt's class (the last class stays on itself) with that class's pinned
image, a fresh volume, the permitted clusters, the same input, key, identity, profile and private input, the
preference narrowed to the remaining classes, and the root's **remaining** deadline as its `activeDeadlineSeconds`
when `deadline_s` was set (never a fresh full budget); then annotates the original with `successor` and the successor
with `recovered-from`. A cancel that lands while the run is being moved withdraws the successor and marks the original
cancelled; a cancelled run is never recovered. Exhausted bounds, a missing pointer or a revoked binding leave the run
where it is (the same-worker retry continues) and the operation's `reason` says why.

Handles are those of fallback: the original is `SUPERSEDED`, `GET`, `:cancel`, `:result`, `:export`, metrics and
logs on it follow to the live successor (`requested_id`), `root_operation_id` stays the root; the successor reports
`recovered_from`, `resumed_from` (the root) and `reason` ("recovered from <original> (<loss>): rtx-pro-6000 -> l40s").

Supported fault injection for a drill, touching only the run itself (no shared node): deactivate the run's Kueue
Workload on the control cluster, `kubectl -n tenant-<t> patch workload $(kubectl -n tenant-<t> get workload -l
kueue.x-k8s.io/job-uid=<job uid> -o name) --type=merge -p '{"spec":{"active":false}}'`. Kueue evicts the worker's
copy, the pod gets SIGTERM, the uploader records `interrupted`, and because the Workload stays inactive no
same-worker replacement starts: the run is lost deterministically and recovers after `wait_s`. Deleting or evicting
the pod alone is not deterministic: the Job controller's same-worker replacement may start within `wait_s` and
the run then legitimately continues where it was.

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
