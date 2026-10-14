# Scheduling: GPU classes, regions, cost and resume

*For architects and operators: how a run is admitted, where it is placed across GPU classes and regions, and how prices enter the decision.*

How a workload (endpoint pod or job) ends up on a GPU, and what a model
declares to steer that. Three layers, all existing projects, one small
dispatcher of our own (`services/dispatcher`: the ranking loop, the InfiniBand floor and a price feed).

| Layer | Decides | Project |
|---|---|---|
| In a cluster: which pool (GPU class, spot/reserved) | Kueue ResourceFlavors in ClusterQueue order, `flavorFungibility` | Kueue 0.20 |
| Across clusters: which region/cluster | Kueue MultiKueue with an external dispatcher that ranks clusters by preference, free quota and live price | Kueue MultiKueue + `services/dispatcher` |
| Nodes | node-group autoscaler per pool (`min_nodes`..`max_nodes` of the pool); InfiniBand pools: the dispatcher moves the pool's floor for multi-node runs (below) | Nebius mk8s + `services/dispatcher` |

## What a model declares

A model is defined through the API (`POST /v1/models`, docs/API.md "Models"); the catalog ships only the
generic run classes (`catalog/models`). What steers placement in a definition:

```json
{"id": "chat", "kind": "endpoint", "image": "vllm/vllm-openai:v0.11.0",
 "gpu": {"count": 1, "classes": ["h100", "l40s"]},            // preferred first
 "images": {"l40s": "vllm/vllm-openai:v0.11.0-l40s"},         // optional: one image per GPU class
 "regions": ["eu-north1"],                                     // default: every region with a pool of a class
 "deployments": {"eu-north1": {"variants": [{"name": "spot", "pool": "h100-spot-1x", "min": 0, "max": 4}]}},
 "nodes": 1, "interconnect": "none"}                           // multi-node endpoints: docs/API.md
```

- Endpoints are **pinned**: for every region of the model the API picks the pool at definition time
  (the pools of the model's classes, reserved before on-demand before spot; for `nodes > 1` a pool whose
  preset has `gpu.count` GPUs per node; never a tenant's dedicated pool) and renders the deployment with
  `nodeSelector: serverless2.nebius/pool: <pool>` and the image of that pool's class (`images.<class>`,
  else `image`); `deployments.<region>.variants` pin explicitly, one deployment per variant
  (docs/API.md "Variants"). Every setting and its default: `services/api/render_endpoint.py` DEFAULTS.
  The renderer can also hand endpoint pods to Kueue (`queue.name`: the `models` namespace is
  Kueue-managed, `serverless2.nebius/kueue: managed` in
  `charts/platform/templates/scheduling/namespaces.yaml`, so every replica is gated at creation, gets
  quota from the ClusterQueue behind the LocalQueue and is released with the pool's `nodeSelector` and
  tolerations injected); the API does not use that mode, so warm endpoints are the static
  `endpoint_floor_gpus` of their pool (docs/FLEET.md).
- Runs declare `gpu.classes` (preferred first): the API sets the
  queue label to `prefer-<first class>` and a `nodeAffinity` over the fleet
  pools of all listed classes (so a fallback never lands on an unsupported
  GPU); image references are the same in every region (`docs/IMAGES.md`).
  `docs/JOBS.md`.

Preference profiles are the ClusterQueues `charts/fleet` renders from the
fleet definition (`docs/FLEET.md`): `default` and one `prefer-<gpu_class>` per
class, on every worker (flavor order = the preference) and on the MultiKueue
manager (same names, with the MultiKueue admission check). A LocalQueue in
the tenant namespace points at one of them; the dispatcher reads
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
below), so for such a class the GPU class is chosen **at submission**: the API ranks the pools of the
fleet document (`fleet-prices`) the way the dispatcher ranks clusters (`services/api/placement.py` mirrors
`rank()` below: the class preference, reserved before on-demand before spot, then the cost of the run on
the node the pool would boot at its list price, so a 1-GPU run does not pick an 8-GPU pool; the model's
regions, the caller's region as a pin when given, a tenant's dedicated pools for that tenant only),
renders the Job with the best class's image, limits its node affinity to that class's pools and queues it
on that class's profile (`prefer-<class>`). The operation and every attempt record carry the class
(`gpu_class`, plus the image under `image`). The run may still move between regions of its class while
it waits (MultiKueue), and a resume keeps the class: the checkpoints were written by that image. What the
API does not see is free quota right now and the live spot quote; it does not ask the dispatcher for
them (a round trip, a Service and a timeout whose only gain would be that signal, while the queue waits
for the preferred class either way). A class without a pool in the fleet document is skipped; with no
pool at all the preferred class is taken and the queue waits for it.

The trade-off, by design: a run with per-class images waits for its class instead of switching class
while queued, unless it names `execution.placement.fallback_wait_s` ("Fallback to the next class"
below), which re-renders it for the next class after the wait. A class with one image keeps free class
switching at queue time.

## Cross-cluster dispatch (`services/dispatcher`)

Kueue MultiKueue in external-dispatcher mode (`multiKueue.dispatcherName:
serverless2.nebius/cost-dispatcher` in the manager's Kueue config,
`clusters/control/values/kueue.yaml`): Kueue creates the Workload on
the manager and waits for `status.nominatedClusterNames`; the dispatcher sets
it; Kueue then copies the job to the nominated worker(s), the first worker
that admits wins and `status.clusterName` is fixed.

Every `INTERVAL_S` (15 s) the dispatcher, for each Workload with quota reserved
and the MultiKueue admission check pending:

1. Candidates are `(region id, pool)` pairs from the `fleet-prices`
   ConfigMap (`pools.yaml`, rendered by `charts/fleet` from the fleet
   definition: class, capacity type, list price, project, platform, preset)
   whose GPU class the profile allows: `prefer-<class>` = that class first,
   then the other classes by their cheapest pool; `default` = any class. A
   `serverless2.nebius/region` label on the Workload (a region id, or the
   region name from the MultiKueueCluster label) restricts candidates to that
   cluster (resume, see below).
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

Kueue's own incremental dispatcher (`kueue.x-k8s.io/multikueue-dispatcher-incremental`, beta since
Kueue 0.19, nominating `MultiKueueConfig.spec.clusters` in configuration order, three clusters per
five-minute round by default, `multiKueue.incrementalDispatcherConfig.stepSize: 1` for one) was
evaluated against this dispatcher and is not used. The fleet's `MultiKueueConfig` already lists the
regions in list-price order per profile, so the mode is one Kueue configuration change away, but it
would not remove the dispatcher (the volume pass and the InfiniBand floors stay), and it does not
know the region pin: a same-volume resume (`serverless2.nebius/region`) would be nominated by the
static order, admitted elsewhere, and the volume pass would create an empty work volume there,
restarting the run from scratch without an error. It also replaces the live free-quota check and
the spot quotes with a static order. The dispatcher already nominates incrementally, one cluster per
`RENOMINATE_AFTER_S` round, at most `INTERVAL_S` after quota reservation, so the native mode gains
seconds of admission latency at best.

Nominations are written with SERVER-SIDE APPLY as field manager `kueue-admission`,
restating `status.admission` and `status.admissionChecks` as read. Both details are
load-bearing: Kueue admits a MultiKueue workload by setting `status.clusterName` and
clearing `nominatedClusterNames` in one server-side apply under that manager, and a
value written with a merge patch (an "Update" managedFields entry, even under the same
manager name) survives that apply, so the API server rejects Kueue's patch
("clusterName and nominatedClusterNames are mutually exclusive") and the manager
Workload stays Pending for its whole life; and the same manager name owns Kueue's quota
reservation and the check states, so an apply that names only `nominatedClusterNames`
releases them, the manager scheduler re-admits the workload, copies it to a cluster
that was not nominated, and after the resulting retry every next apply is rejected
("admissionChecks[0].state: Required value").

Prices: the `price-feed` CronJob (every 10 min) asks the Nebius price calculator for
each spot/on-demand pool of `fleet-prices` (`nebius billing v1alpha1
calculator estimate`; for spot a preemptible VM that follows the spot price:
`--resource-spec-compute-instance-spec-preemptible-on-preemption STOP
--resource-spec-compute-instance-spec-follows-spot-price`, the current quote,
which Nebius moves every 15 minutes) and writes `spot.json` (USD per GPU-hour,
keyed `<region id>/<pool>`) into the ConfigMap `kueue-system/spot-prices`, which it
creates on first run (runtime-owned, outside Terraform). The feed must not pass the
deprecated `--preemptible-priority`: the CLI rejects it (exit 4), the feed would write
an empty map and every spot pool would be ranked at its list price. Quotes for the
same preset differ by an order of magnitude between regions, which is what the
cross-region ranking is for. The calculator needs an SA key: Secret
`kueue-system/price-feed-nebius-sa` (optional mount; without it the feed logs
failures and list prices are used).

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
RBAC; the dispatcher image, `images.versions.dispatcher`, from
`services/dispatcher/Dockerfile`: the ops image's Nebius CLI on
`python:3.12-slim`), installed by the platform stage on the control cluster.
`services/dispatcher/tests` covers the `fleet-prices` parsing,
profile-from-queue-name, preference, fallback class, cheapest with live
price, nothing-free, region pin, multi-GPU quota, renomination timing and
the InfiniBand floor.

Decisions are logged (`kubectl -n kueue-system logs deploy/dispatcher`) as
`<ns>/<workload> profile=... pin=... gpus=N -> [clusters] best=(cluster,
flavor, price, free)`.

## InfiniBand pools grow on demand

The Nebius cluster autoscaler never adds an InfiniBand node: it simulates the new node without the DRA
attributes of its RDMA NICs, so a pod that claims them never triggers a scale-up, from zero or from one
(`cannot allocate all claims`, `CEL runtime error: no such key: ifName`; docs/JOBS.md "Multi-node runs").
Without help an operator would raise the pool's `min_nodes` in `terraform.tfvars` and apply the cloud stage
before every multi-node run. The dispatcher's elected replica does it instead
(`services/dispatcher/infiniband.py`, every reconcile cycle, 15 s), per worker cluster:

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
the source registry (`images.source`), NGC, GHCR, Docker Hub, Quay or
registry.k8s.io on demand and keeps the layers in the region. A Job therefore
contains nothing that depends on the region it lands in: every fleet run is
created unpinned and the dispatcher places it at queue time, re-nominating
the next cluster every `RENOMINATE_AFTER_S` while it waits. The region the
API reports is the cluster that ADMITTED the run (Workload
`status.clusterName`); a nomination is not a placement. The work volume is
created where the run is admitted, never at nomination (`services/dispatcher`).

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

## Per-node fit

Kueue admits a workload against a flavor's **aggregate** quota (`max_nodes` x the per-node budget of `charts/fleet`)
and never asks whether one node holds the pod. A GPU-free run that the CPU pool's quota refuses for the moment (its
ephemeral-storage or CPU borrowed by another run) falls through the flavor order to a GPU pool whose aggregate CPU
covers it while no single node does (an AlphaFold 3 pipeline at 16.1 CPU on 1-GPU nodes of 14 admittable CPU), and
its pod stays Pending for ever on an admitted workload that holds that flavor's quota. Three pieces make per-node
fit structural (api 0.11.80, fleet chart 0.1.12):

- `fit.yaml` of the fleet document (`fleet-prices`, `charts/fleet/templates/prices.yaml`): per cluster id and pool
  label, what ONE node admits (`cpu`, `memory_gib`, `ephemeral_gib`, `gpus`, `shared_fs`), from the same helpers that
  size the Kueue quotas with one node (`node_reserve` for GPU pools, `system_reserve` for the system and CPU pools,
  the ephemeral share of the boot disk); `tools/check.sh` asserts it agrees with the ClusterQueues.
- The render (`jobs.py`, `services/api/fit.py`): the pod's effective requests (main + uploader; an init container's
  own request bounds it) must fit one node of a candidate pool. Candidates are the class's explicit `pool:` (then it
  is a 400 when it cannot hold the pod: nothing is widened to another pool), else the GPU class pools of the run,
  else every pool, each intersected per (cluster, pool) with what the run requires besides size: the shared
  filesystem (`shared_fs`) and local NVMe (`pools.yaml`), so a big node without the mount is no candidate; the fit is
  computed on the final rendered pod (after the execution profile); the eligible clusters are the run's placement
  regions (or the pinned region's). The fitting pools
  become `serverless2.nebius/pool In [...]`, conjunctive with the class, `shared-fs` and `local-nvme` terms (Kueue
  assigns only a flavor whose `nodeLabels` satisfy the affinity), and the placement regions are narrowed to clusters
  with a fitting pool. No pool anywhere: **400 `impossible shape`** at submission, naming the requests and the largest
  candidate node; a shape that fits a pool whose quota is busy is **temporarily unavailable**: QUEUED, Kueue waits
  (never admitted where it cannot run, never spilled). Nothing is reduced or enlarged on the caller's behalf. Without
  `fit.yaml` (an older fleet chart) nothing changes.
- Recovery of a run admitted before this (or by a worker whose table lagged): the control API's reconcile loop
  (`reconciliation.unschedulable_switch`) looks at placed fleet runs whose pods are all Pending and unbound, whose
  newest pod the scheduler has reported `Unschedulable` for `UNSCHEDULABLE_WAIT_S` (600 s), and whose assigned pool
  (the pod's injected `serverless2.nebius/pool`) has no node that holds the pod's requests. Such a run is moved
  exactly as a fallback is (`jobs.switch_unschedulable`): `<root>-r<n>` is created from the same input, placement,
  class and key with the fit affinity, carrying `serverless2.nebius/recovery` ("unschedulable on north/h100-reserved-1x
  for 1200 s: the pod needs cpu 16.1, ... ; re-placed on the pools whose node does (cpu-batch-32)") and
  `recovered-from`; the original is fenced first (suspended, named after its successor: `SUPERSEDED`, its Workload
  deactivated so the worker's copy and the Pending pod go), the successor is admitted on the run's ledger and created, a
  failed create restores the original; the owner's handle, the root id and the result path stay the run's. Nothing of
  the original ran, so nothing is lost or re-accounted. A pod whose pool CAN hold it (a pool scaling from zero, a
  full pool) is left waiting; a started attempt is never switched; a shape no pool holds is annotated once
  (`serverless2.nebius/unschedulable`, the operator decides); at most two such switches per root. The assigned pool
  is judged on the admitting cluster only (the same label may be a bigger preset elsewhere). The window between the
  reconciler's snapshot and the fence is bounded from both sides: the pods are re-read immediately before the fence
  (a binding meanwhile stops the switch, nothing fenced) and once more after it (a binding in the last instants is
  recorded on the original, `serverless2.nebius/started-before-fence`, and in the successor's reason; the attempt's
  records stay accepted).

Not done here: Kueue's Topology-Aware Scheduling would make Kueue itself do per-node fit at admission; it changes the
admission substrate and is qualified separately.

## Automatic recovery after infrastructure loss

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
The superseded original's work volume is scratch nothing reads again (the successor restores the portable checkpoint
onto its own volume; a superseded handle's `:resume` and `:export` follow to the successor): the dispatcher never
(re)creates it and its sweeper deletes it once every manager Job that still references the claim was superseded,
leaving the handles, their attempt records and the chain history in place (docs/JOBS.md "Work volume"). A FAILED
link that still names the claim (a failed run whose same-volume resume was then recovered elsewhere) keeps it as
before, until that handle goes.

Supported fault injection for a drill, touching only the run itself (no shared node): deactivate the run's Kueue
Workload on the control cluster, `kubectl -n tenant-<t> patch workload $(kubectl -n tenant-<t> get workload -l
kueue.x-k8s.io/job-uid=<job uid> -o name) --type=merge -p '{"spec":{"active":false}}'`. Kueue evicts the worker's
copy, the pod gets SIGTERM, the uploader records `interrupted`, and because the Workload stays inactive no
same-worker replacement starts: the run is lost deterministically and recovers after `wait_s`. Deleting or evicting
the pod alone is not deterministic: the Job controller's same-worker replacement may start within `wait_s` and
the run then legitimately continues where it was.

## Not possible: swapping the image after Kueue picked the pool

Tried with a Kyverno mutating policy that sets the container image from
`serverless2.nebius/image.<pool>` annotations when the pool `nodeSelector`
appears: it works on CREATE (pinned pods), but on the UPDATE in which Kueue
injects the nodeSelector and removes the gate, Kueue's pod integration
compares the pod spec with the Workload's pod set, finds the image changed,
logs `No matching Workload; restoring pod templates according to existent
Workload` and deletes the pod (Kueue `jobframework.EquivalentToWorkload`
compares the pod template including images; Workload pod sets are immutable
once quota is reserved). The same holds for Jobs. Therefore per-GPU image
tags are decided before the Job exists: for endpoints by the pinned pool
(the image of its class, or the variant's), for runs by the class chosen at
submission ("Per-GPU images for runs" above). A Kueue-placed workload with
one image must run on every pool its profile allows. Kyverno is not part of
the solution.

## Tenants and single-cluster mode

`tenants.<name>.gpu_quota`, `fair_share_weight` and `dedicated_pools` render per-tenant ClusterQueues
in the cohort (nominal 0, `borrowingLimit = gpu_quota`, `fairSharing.weight`, dedicated pools as the
tenant's own nominal quota; `charts/fleet/templates/worker-queues.yaml`, docs/FLEET.md "Tenants").
Kueue's fair-share usage sums resource magnitudes without normalisation; set weights per GPU class,
not per CPU/memory. The MultiKueue manager cannot be its own worker: that is why
`control_plane.dedicated = false` is a single-cluster mode without MultiKueue and without the
dispatcher (runs are created and admitted in the one cluster).
