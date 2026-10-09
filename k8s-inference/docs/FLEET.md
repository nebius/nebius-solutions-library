# The fleet: one definition of the inference cluster

*For operators and architects: how pools, capacity types, GPU classes and queues relate. The document uses the `fleet.yaml` form of the definition; in the Terraform solution the same keys live under `regions.*.pools` in `terraform.tfvars` (last section).*

One definition (`fleet.yaml`, or the `fleet` object of `terraform.tfvars`) describes the whole inference
cluster: the control-plane cluster, every region, every GPU pool (platform,
preset, GPU class, reserved / on-demand / spot capacity, min and max nodes,
warm-endpoint floor), the shared weights filesystem per region and the price
list. Two consumers read it and nothing else defines capacity:

| Consumer | Reads | Produces |
|---|---|---|
| the cloud stage (`stack/cloud`, module `stack/modules/cluster`) | control + regions | one Managed Kubernetes cluster per entry, system pool, GPU node groups with their reservation / spot policy, node identity, ops identity, registries, backups bucket, weights filesystem; one state per cluster |
| `charts/fleet` (Helm, rendered by the platform stage on every cluster) | regions, prices, `node_reserve`, `capacity_order` | worker: Kueue ResourceFlavor per pool, `default` + `prefer-<gpu-class>` ClusterQueues, pre-pull DaemonSet per pool, `fleet-prices` ConfigMap; control: MultiKueueCluster per region, MultiKueueConfig + AdmissionCheck per profile, fleet-wide flavors and profile queues |

Nothing else defines capacity: pools, quotas, pre-pull lists and prices all derive from it.

## fleet.yaml

```yaml
fleet:
  kubernetes_version: "1.35"
  node_reserve: { cpu: 2, memory_gib: 20 }     # per node, outside Kueue quota (daemonsets, kubelet)
  capacity_order: [reserved, on_demand, spot]  # inside one GPU class, in every preference queue
  control: { id: control, cluster: serverless2-control, region: eu-north1, project: ..., subnet: ..., system_pool: {...}, allowed_cidrs: [...] }
  regions:
    <region name>:
      id: <cluster id>            # the cluster's name in every stage (`./stack.sh apply platform <id>`)
      cluster: <mk8s name>
      project: <nebius project>   # one project = one region
      subnet: <vpc subnet>
      system_pool: { platform: cpu-d3, preset: 8vcpu-32gb, node_count: 2 }
      allowed_cidrs: [...]        # public API endpoint allow-list (docs/OPERATIONS.md)
      ops: { registries: [...], backups_bucket: ... }
      weights_filesystem: { enabled: true, size_gib: 2048 }
      pools:
        <pool key>:               # = node label serverless2.nebius/pool, Kueue flavor name, DaemonSet suffix
          platform: gpu-h100-sxm
          preset: 1gpu-16vcpu-200gb
          gpu_class: h100         # what models prefer (prefer-h100); Kueue label, NOT a node label
          capacity: { type: reserved, reservation_ids: [reservation-...] }   # or
          capacity: { type: on_demand }                                      # or
          capacity: { type: spot, max_price: "2.15" }                        # or spot {} = follow the price
          min_nodes: 0            # spare, always-warm nodes
          max_nodes: 8
          endpoint_floor_gpus: 0  # GPUs held by warm endpoints WITHOUT runtime.queue.name (Kueue does not see those)
          interconnect: none      # or infiniband: the pool's nodes join one Nebius GPU cluster on an InfiniBand fabric
                                  # (8-GPU presets only; multi-node runs with interconnect: required land here, docs/JOBS.md;
                                  # min_nodes = the node count of the largest multi-node run: the autoscaler never
                                  # adds InfiniBand nodes, not even from one ("DynamicResources" filter: CEL error
                                  # on the simulated node's devices, s2pr2 2026-10-09); the node group gets
                                  # gpu_settings.dra = true so Managed Kubernetes runs DraNet on its nodes)
          ib_devices_per_node: 8  # InfiniBand NICs a pod claims per node (default 8; 4 on gpu-gb300): Kueue quota
                                  # `nebius.ai/infiniband` = this x max_nodes (DRA, docs/JOBS.md "Multi-node runs")
          infiniband_fabric: fabric-7   # with interconnect: infiniband; defaults to the region's single fabric for the
                                  # platform (H200: eu-north1 fabric-7, eu-west1 fabric-5, eu-north2 eu-north2-a); H100
                                  # in eu-north1 has fabric-2/3/4/6 and must be set (docs.nebius.com/compute/clusters/gpu)
  prices: { <platform>: { on_demand: 4.50, spot: 0.79 }, ..., reserved_marginal: 0 }   # USD per GPU-hour
```

Rules the chart derives from it:

- **Flavor name = pool key**, so the objects adopt today's `h100-spot-1x` and
  `rtx6000-spot-1x` flavors and the `default` ClusterQueue without renames.
- **Quota per pool** = `max_nodes` x GPUs per node minus `endpoint_floor_gpus`;
  CPU and memory = (preset minus `node_reserve`) x the nodes left for jobs.
  Hub today: 8 GPUs / 112 vCPU / 1440Gi (unchanged); eu-south1: 3 / 66 /
  594Gi (was 600Gi by hand).
- **Price of a pool**: reserved = `reserved_marginal`, on-demand = list,
  spot = `max_price` cap or the spot list price.
- **Order inside a profile** `prefer-<class>`: pools of that class in
  `capacity_order` (reserved, on-demand, spot), cheapest first within a type;
  then the other classes by their cheapest pool. The `default` profile is
  pure price order. On the control cluster the same order runs across
  regions (flavor `<region id>-<pool>`), and the MultiKueueConfig of a
  profile lists the regions that have the class first (by their cheapest
  pool of it), the others after.

## Scheduling: `charts/fleet`

Rendered per cluster by the platform stage with the fleet document plus the
pre-pull image list and `--set cluster=<id>`. Local check (the reference
fleet's files; `make check` does the same with the example tfvars):

```
helm template fleet charts/fleet -f fleet.yaml -f clusters/hub/values/fleet.yaml --set cluster=hub
helm template fleet charts/fleet -f fleet.yaml -f clusters/control/values/fleet.yaml --set cluster=control
```

Worker cluster (every region):

- `ResourceFlavor <pool>`: node label `serverless2.nebius/pool`, GPU taint
  toleration; labels carry gpu-class and capacity type, annotations the
  platform and price.
- `ClusterQueue default`: nominal quota per pool in cohort `serverless2`
  (tenant queues from `charts/tenant` keep borrowing from it as today).
- `ClusterQueue prefer-<class>` per GPU class of the WHOLE fleet (a job
  dispatched to a region without its preferred class queues there by price;
  tenant LocalQueues carry the same names everywhere): nominal quota 0,
  `borrowingLimit` = pool capacity, flavors in preference order,
  `flavorFungibility.whenCanBorrow: MayStopSearch` (stop at the first flavor
  with free cohort capacity) and `whenCanPreempt: TryNextFlavor`. A job that
  prefers H100 lands on H100 when free, else on the next class, and waits
  only when every pool is full. A LocalQueue pointing at `prefer-<class>` is
  all a job needs (`kueue.x-k8s.io/queue-name`); the API maps a model's
  preferred class to it (lane F3/F4).
- `DaemonSet prepull-<pool>` in `models`, same shape as the hand-written
  ones, image list per cluster in `clusters/<id>/values/fleet.yaml`.
- `ConfigMap kueue-system/fleet-prices`: per-pool price, class, capacity,
  GPUs per node and max nodes (`pools.yaml`), the platform list
  (`list.yaml`) for the cost-aware dispatcher, the price feed and the API
  (class affinity). OpenCost
  keeps one GPU / spot price per cluster in
  `clusters/<id>/apps/overlays/observability`, set from the same numbers.
- DCGM exporter now follows any `serverless2.nebius/pool` label except
  `system`, so new pools need no observability change.

Control cluster (Kueue MultiKueue manager):

- `MultiKueueCluster <region id>` with kubeconfig Secret
  `kueue-system/multikueue-<region id>` (never in git; a `multikueue`
  service account on the worker with the Kueue-documented ClusterRole).
- `MultiKueueConfig <profile>` with the regions in price order for that
  profile, `AdmissionCheck multikueue-<profile>`.
- `ResourceFlavor <region>-<pool>` (logical, no node labels) and the same
  `default` / `prefer-<class>` ClusterQueues as the workers, each with its
  profile's admission check. A job submitted to the control cluster is
  admitted only after a worker in the profile's region order admitted it;
  the worker's queue of the same name does the final pool placement.
- Dispatch order vs. "all at once" is Kueue configuration
  (`multiKueue.dispatcherName`, incremental tries the listed regions in
  order); the cost-aware dispatcher (lane F4) replaces the static order with
  live prices from `fleet-prices` and `spot-prices` (`docs/SCHEDULING.md`).
- Resume after spot preemption: MultiKueue keeps a Workload on the worker
  that admitted it; the Job's own `backoffLimit` / JobSet restart and the
  checkpoint on the region's shared filesystem resume it in the same region
  (lane F3). A workload is only re-dispatched to another region when the
  worker evicts it entirely.

Endpoints: Knative scales them (a revision superseded by a newer one is collected after an hour; a model whose first revision never becomes Ready keeps its Pending pod and GPU request until the model is changed or deleted, Knative never collects the latest revision); an endpoint with `runtime.queue.name` set
(`docs/SCHEDULING.md`) is admitted by Kueue like a job (the `models`
namespace is Kueue-managed), so its warm replicas count in the quota and its
pool's `endpoint_floor_gpus` must be 0 for them. Endpoints without
`queue.name` stay outside Kueue and keep the static `endpoint_floor_gpus`
deduction.

## Tenants

`charts/tenant` renders, per cluster, the tenant namespace with one
LocalQueue per profile (`default`, `prefer-<class>` for every class of
`fleet.yaml`, the chart reads it) pointing at the fleet ClusterQueue of the
same name; on the control cluster (`clusters.control`, `manager: true`) the
namespace, those queues and the API binding only. Tenants share the fleet
queues; the per-tenant ClusterQueue caps of v1 are gone (fairness between
tenants is Kueue's priority ordering; Kueue's usage-based admission fair
sharing is the knob to turn on if one tenant starves others).
The models stage renders it on every cluster of the tenant's regions.

## Change recipes

- **Add a pool / GPU class**: one entry under `regions.<r>.pools`; `plan`
  shows one node group (and a pricing policy for a capped spot pool); Terraform
  adds the flavor, the `prefer-<class>` queue, the pre-pull DaemonSet and the
  price line. No other file.
- **Use a reservation**: `capacity: { type: reserved, reservation_ids: [...] }`
  on a pool whose `platform`/`preset` match the capacity block; the pool is
  `STRICT` and sorts first in its class.
- **Raise capacity**: `max_nodes` (plan = in-place node-group update), the
  quota follows on the next sync.
- **Warm endpoint added**: raise the pool's `endpoint_floor_gpus` with the
  catalog change (docs/OPERATIONS.md "Capacity for runs and endpoints").
- **Add a region**: one `regions.<name>` entry (project, subnet, pools),
  then `./stack.sh apply` (the cloud stage adds the cluster, the platform and
  models stages follow on it, and the control cluster's MultiKueue gains the
  worker). Verified on a live fleet on 2026-10-09 (a test fleet plus eu-west2
  with a B300 spot pool, docs/VERIFICATION.md).
- **Prices**: `prices` block (nebius.com/prices); flavors, `fleet-prices` and
  the profile order follow.

## Known limits

- The quota deduction for warm endpoints without `queue.name` is static
  (`endpoint_floor_gpus`); endpoints with `queue.name` are exact (Kueue).
- MultiKueue manager quotas mirror `fleet.yaml` (`quotaManagement: Manual`);
  Kueue's automated quota management needs its feature gate and was not
  enabled.
- Spot list prices are the "from" prices; the live spot price comes from the
  price feed (lane F4), not from this file.

GPU node groups carry a cloud-init fragment (`stack/modules/cluster/main.tf` `gpu_cloud_init`): the containerd registry drop-in of the image cache (docs/IMAGES.md) and, when `weights_filesystem` is enabled, the virtiofs mount of the shared weights filesystem. Changing it rolls the pools (surge 1).

## Terraform solution (2026-10-08): `terraform.tfvars` instead of `fleet.yaml`

In the library form the fleet definition is the `fleet` object of `terraform.tfvars` (schema and
validations: `stack/config/variables.tf`), the same structure as `fleet.yaml`: `control_plane`,
`regions.<region>.pools`, `images`, `prices`, plus `edge`, `observability`, `tenants`, `models`,
`acceptance`. `charts/fleet` receives the same document from the platform stage. New per-pool inputs:
`local_nvme` / `local_nvme_mode` (host NVMe as kubelet ephemeral storage, node label
`serverless2.nebius/local-nvme=true`; only presets that ship local disks accept it, see "Local NVMe"
below), `boot_disk_gib`, `labels`; `cpu_pools` (CPU-only pools, tainted
`serverless2.nebius/cpu-pool`); reserved pools roll with zero surge; the system pool raises
`fs.inotify.max_user_instances`. Boot-disk rule: a node scaled from zero advertises about 80% of its boot
disk minus 32 GiB as ephemeral storage, never host NVMe.

### Spot capacity: how `capacity` maps to the Nebius API (2026-10-09)

Nebius sells preemptible capacity under dynamic pricing since 2026-10-08 (the API field is still
`preemptible`): a preemptible VM or node group declares exactly one of `follows_spot_price` (pay the
current quote, never stopped for price), `spot_pricing_policy` (a pricing policy with a cap: stopped above
it, restarted below it) or `on_demand`. The pool input renders them, one and only one
(`stack/modules/cluster/main.tf`):

| `capacity` | Node-group template | Meaning |
|---|---|---|
| `{ type = "on_demand" }` (default) | no `preemptible` | regular VMs at the list price |
| `{ type = "spot" }` | `preemptible = {}`, `follows_spot_price = {}` | spot at the current quote; nodes stop only when Nebius reclaims capacity |
| `{ type = "spot", max_price = "2.15" }` | `preemptible = {}`, `spot_pricing_policy = <policy>` | spot capped at that USD per GPU-hour (one `nebius_billing_v1_pricing_policy` per pool); nodes stop above the cap and return below it |
| `{ type = "reserved", reservation_ids = [...] }` | `reservation_policy = STRICT` | the capacity block, used first |

The quotas that apply to spot pools are the preemptible ones (`compute.instance.preemptible.count`
per regional project, counted in VMs; `./stack.sh preflight` prints their usage and refuses a spot pool
whose platform is not `allowed_for_preemptibles` in that project). The GPU quotas
(`compute.instance.gpu.<platform>`) apply to on-demand pools. The standalone-VM fields of the Compute
API (`preemptible.on_preemption = STOP`, `recovery_policy = FAIL`, the deprecated `preemptible.priority`)
do not exist on node groups: Managed Kubernetes stops a preempted node's VM and replaces it when capacity
allows, so a spot pool can run below `min_nodes` for a while and scale-from-zero can wait. Capacity-first
policy: prefer reserved and on-demand pools, add spot as an extra pool that follows the price (no
discretionary cap); the scheduler orders reserved before on-demand before spot inside a class
(`capacity_order`), the dispatcher the same across regions with the live quote (docs/SCHEDULING.md).
What a preemption does to a run: the Job's replacement pod resumes from the network checkpoint volume in
the same region, a multi-node JobSet restarts all its pods (docs/JOBS.md); everything on local NVMe
(`scratch: local-nvme`, image layers) is gone with the node, so runs on NVMe must checkpoint to `/ckpt`.
Live spot quotes change every 15 minutes; the price feed keeps the dispatcher's view current.

### Warm spare nodes (2026-10-09)

`pools.<name>.warm_nodes = N` keeps N nodes of the pool running with no model on them. This is the
cluster autoscaler's "overprovisioning" pattern, installed as the `cluster-overprovisioner` chart
(Delivery Hero, `clusters/common/apps/overprovisioner.yaml`) on every worker: one Deployment of `pause`
pods per pool with spares (the platform stage computes the list from the pools), each pod requesting
every GPU of a node under the PriorityClass `serverless2-warm-spare` (value -1). Every real pod has priority 0 or more (endpoints) or the run priority classes (100 and up),
so the scheduler evicts a placeholder the moment a model needs the node and the model starts without
waiting for an instance to boot; the evicted placeholder goes Pending and the cluster autoscaler brings
the next spare up (its priority cutoff is -10, so the placeholder counts), within `max_nodes`. The pre-pull
DaemonSet has already warmed the platform images on the node; a model's own image is pulled from the cache.

What it costs and what it is not: a spare is a running node (spot or on-demand, the pool's capacity type)
billed whether a model uses it or not; `min_nodes` keeps a floor too, but the first model occupies it and
nothing refills the spare. A spare serves every model whose GPU class and count fit the pool's preset, in
that region. It is not an extra application replica (see `scaling.min` for that), and InfiniBand pools do
not take it (the autoscaler never adds InfiniBand nodes; keep `min_nodes` there).

### Local NVMe: which platform/preset combinations have it (2026-10-08)

Nebius exposes host NVMe to Managed Kubernetes nodes through the node-group template
`local_disks = { passthrough_group = { requested = true }, config = { kubelet_ephemeral = true | none = true } }`
(`stack/modules/cluster/main.tf`, pool input `local_nvme` / `local_nvme_mode`). The preset API does not
advertise local disks; the authoritative list is the Compute documentation ("Using local SSD disks",
docs.nebius.com/compute/storage/local-disks, "Availability"), which on 2026-10-08 reads: *Local SSD
disks are available only in the uk-south1, eu-west2 and us-north1 regions, only on the NVIDIA B300 NVLink
with Intel Granite Rapids platform (gpu-b300-sxm) with the eight-GPU preset 8gpu-192vcpu-2768gb* (6 x 3.84 TB).

| Platform | Preset | Local disks | Regions | Evidence |
|---|---|---|---|---|
| gpu-b300-sxm | 8gpu-192vcpu-2768gb | 6 x 3.84 TB NVMe | uk-south1, eu-west2, us-north1 | **verified 2026-10-09** on a spot node in eu-west2 (test fleet s2pr2, pool `b300-spot-8x`): six `MTFDKCC3T8TGP` 3.5 TiB NVMe in a RAID0 (`md127`, ext4, 21 TiB) under `/mnt/local-ephemeral` with `/var/lib/kubelet` and `/var/lib/containerd` on it; node capacity `ephemeral-storage` 22.4 TB (allocatable 20.65 TB); a `scratch: local-nvme` run wrote 100 GiB at 6.7 GB/s (one `dd`, direct, fsync) and read it at 1.6 GB/s single-stream / 5.7 GB/s with four readers; a spot pool, so the preemptible quota applies (`compute.instance.preemptible.count`), not the B300 GPU quota |
| gpu-b300-sxm | 1gpu-24vcpu-346gb | none | eu-west2 | node group with `local_disks` rejected on 2026-10-08 (test fleet s2lib, see docs/VERIFICATION.md) |
| gpu-h100-sxm | 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb | none | eu-north1 | rejected on 2026-10-08: `local_disks.passthrough_group.requested is invalid` |
| gpu-h200-sxm, gpu-b200-sxm, gpu-gb300, gpu-l40s-a/-d, gpu-rtx6000-a | all | none | - | not listed in the documentation (the H100 probe above is the behaviour on an unlisted preset) |

Consequences for a fleet definition: set `local_nvme = true` only on a `gpu-b300-sxm` /
`8gpu-192vcpu-2768gb` pool in one of the three regions; the apply fails early (node-group creation) on
anything else, nothing is left half-built. What the flag does when it is accepted: the node group formats
the six disks as the kubelet's ephemeral storage (`kubelet_ephemeral`), so every `emptyDir`, image layer
and `scratch: local-nvme` run volume (`/work`, docs/JOBS.md) lands on NVMe, and the node carries the label
`serverless2.nebius/local-nvme=true` that the API's node affinity for such runs selects;
`local_nvme_mode = raw` leaves the devices unformatted (`config.none`) for a workload that owns them. The
rendering (node-group template, node label, catalog `scratch`, pod affinity and emptyDir) is covered by
`terraform plan` and the API unit tests, and the whole path ran end to end on 2026-10-09 on a B300 spot node
(`docs/dev-fleet/VERIFICATION-DEV.md`, "Local NVMe on a B300 spot node"). Two things that test fixed: the
Kueue flavor of an NVMe pool now carries the `local-nvme` label (without it Kueue ignored the affinity key
and admitted such a run on any flavor), and a local-NVMe run is dispatched only to regions that have an
NVMe pool. The NVMe content does not survive a preemption: a stopped spot VM loses `/mnt/local-ephemeral`
(the kubelet re-formats it on the next boot), so a run on local NVMe keeps its checkpoints elsewhere
(the network work volume of `scratch: network`, or its own uploads).
