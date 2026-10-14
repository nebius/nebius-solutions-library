# The fleet: one definition of the inference cluster

*For operators and architects: how pools, capacity types, GPU classes and queues relate. The definition is the `fleet` object of `terraform.tfvars` (schema and validations: `stack/config/variables.tf`; a commented example: `terraform.tfvars.example`).*

One definition describes the whole inference cluster: the control-plane
cluster, every region, every GPU pool (platform, preset, GPU class,
reserved / on-demand / spot capacity, min and max nodes, warm-endpoint
floor), the shared weights filesystem per region and the price list. The
config stage (`stack/config`) derives one document from it; two consumers
read that and nothing else defines capacity:

| Consumer | Reads | Produces |
|---|---|---|
| the cloud stage (`stack/cloud`, module `stack/modules/cluster`) | control plane + regions | one Managed Kubernetes cluster per entry, system pool, GPU node groups with their reservation / spot policy, node identity, ops identity, registries, backups bucket, weights filesystem; one state per cluster |
| `charts/fleet` (Helm, rendered by the platform stage on every cluster) | regions, prices, `node_reserve`, `capacity_order`, tenants | worker: Kueue ResourceFlavor per pool, `default` + `prefer-<gpu-class>` ClusterQueues (the fleet's and one set per tenant), pre-pull DaemonSet per pool, `fleet-prices` ConfigMap; control: MultiKueueCluster per region, MultiKueueConfig + AdmissionCheck per profile, fleet-wide flavors and profile queues |

Nothing else defines capacity: pools, quotas, pre-pull lists and prices all derive from it.

## The definition (`terraform.tfvars`)

```hcl
fleet = {
  name               = "inference"
  kubernetes_version = "1.35"
  node_reserve       = { cpu = 2, memory_gib = 20 }       # per node, outside Kueue quota (daemonsets, kubelet)
  capacity_order     = ["reserved", "on_demand", "spot"]  # inside one GPU class, in every preference queue
  control_plane = { dedicated = true, project_id = "...", region = "...", subnet_id = "...",
                    system_pool = { ... }, allowed_cidrs = [...] }
  regions = {
    <region name> = {
      id            = "<cluster id>"   # the cluster's name in every stage (`./stack.sh apply platform <id>`)
      project_id    = "..."            # one project = one region
      subnet_id     = "..."
      system_pool   = { platform = "cpu-d3", preset = "8vcpu-32gb", node_count = 2 }
      allowed_cidrs = [...]            # public API endpoint allow-list (docs/OPERATIONS.md)
      service_cidrs = [...]            # optional Kubernetes Service CIDRs (also on control_plane); unset = a /16 from the project's pool
      weights_filesystem = { enabled = true, size_gib = 2048 }
      pools = {
        <pool key> = {                 # = node label serverless2.nebius/pool, Kueue flavor name, DaemonSet suffix
          platform  = "gpu-h100-sxm"
          preset    = "1gpu-16vcpu-200gb"
          gpu_class = "h100"           # what models prefer (prefer-h100); Kueue label, NOT a node label
          capacity  = { type = "reserved", reservation_ids = ["capacityblockgroup-..."] }   # or
          #           { type = "on_demand" }                                                 # or
          #           { type = "spot", max_price = "2.15" }   # or { type = "spot" } = follow the price
          min_nodes = 0                # node floor (the first model occupies it)
          max_nodes = 8
          warm_nodes          = 0      # spare nodes with no model on them ("Warm spare nodes" below)
          endpoint_floor_gpus = 0      # GPUs held by warm endpoints (Kueue does not see those)
          interconnect        = "none" # or "infiniband": the pool's nodes join one Nebius GPU cluster on an
                                       # InfiniBand fabric (8-GPU presets only; multi-node runs with
                                       # interconnect: required land here, docs/JOBS.md; the pool's floor
                                       # follows them, docs/SCHEDULING.md "InfiniBand pools grow on demand";
                                       # the node group gets gpu_settings.dra = true so Managed Kubernetes
                                       # runs DraNet on its nodes)
          infiniband_fabric   = "..."  # with interconnect = "infiniband": the region's fabric for the platform
                                       # (docs.nebius.com/compute/clusters/gpu); must be set where a region
                                       # has several fabrics for the platform
          local_nvme          = false  # host NVMe as kubelet ephemeral storage ("Local NVMe" below)
          driver_preset       = "cuda13.0"   # the node image's driver; unused with gpu_operator ("GPU drivers")
        }
      }
      cpu_pools = { ... }              # optional CPU-only pools, tainted serverless2.nebius/cpu-pool
  assets = { <alias> = { kind = "file" | "tree", host_path = "/mnt/weights/...", gid = 65532, sha256 = "...", size = N } }  # restricted assets (docs/JOBS.md)
    }
  }
  prices = { "<platform>" = { on_demand = 4.50, spot = 0.79 }, ... }   # USD per GPU-hour (a default table
                                       # covers every current platform); reserved_marginal_price = 0
  images = { ... }                     # docs/IMAGES.md
  tenants = { ... }                    # "Tenants" below
}
```

Further per-pool inputs: `local_nvme_mode` (`kubelet-ephemeral` or `raw`), `boot_disk_gib`, `labels`.
Reserved pools roll with zero surge (a full reservation cannot surge); the system pool raises
`fs.inotify.max_user_instances`. Boot-disk rule: a node scaled from zero advertises about 80% of its
boot disk minus 32 GiB as ephemeral storage, never host NVMe. On an InfiniBand pool a pod claims every
RDMA NIC of its node (8, 4 on `gpu-gb300`); the pool's Kueue quota `nebius.ai/infiniband` is that times
`max_nodes` (DRA, docs/JOBS.md "Multi-node runs").

Rules the chart derives from it:

- **Flavor name = pool key**.
- **Quota per pool** = `max_nodes` x GPUs per node minus `endpoint_floor_gpus`;
  CPU and memory = (preset minus `node_reserve`) x the nodes left for jobs.
  The example's `h100-spot-1x` (8 x `1gpu-16vcpu-200gb`): 8 GPUs, 112 vCPU,
  1440 Gi.
- **Price of a pool**: reserved = `reserved_marginal_price`, on-demand = list,
  spot = `max_price` cap or the spot list price.
- **Order inside a profile** `prefer-<class>`: pools of that class in
  `capacity_order` (reserved, on-demand, spot), cheapest first within a type;
  then the other classes by their cheapest pool. The `default` profile is
  pure price order. On the control cluster the same order runs across
  regions (flavor `<region id>-<pool>`), and the MultiKueueConfig of a
  profile lists the regions that have the class first (by their cheapest
  pool of it), the others after.

## Scheduling: `charts/fleet`

Rendered per cluster by the platform stage with the derived fleet document
plus the pre-pull image list and `--set cluster=<id>`. Local check: `make
check` renders the chart for every cluster of the example tfvars and asserts
the flavor order and quotas (`tools/check.sh`).

Worker cluster (every region):

- `ResourceFlavor <pool>`: node label `serverless2.nebius/pool`, GPU taint
  toleration; labels carry gpu-class and capacity type, annotations the
  platform and price.
- `ClusterQueue default`: nominal quota per pool in cohort `serverless2`
  (the tenant queues borrow from it, "Tenants" below).
- `ClusterQueue prefer-<class>` per GPU class of the WHOLE fleet (a job
  dispatched to a region without its preferred class queues there by price;
  tenant LocalQueues carry the same names everywhere): nominal quota 0,
  `borrowingLimit` = pool capacity, flavors in preference order,
  `flavorFungibility.whenCanBorrow: MayStopSearch` (stop at the first flavor
  with free cohort capacity) and `whenCanPreempt: TryNextFlavor`. A job that
  prefers H100 lands on H100 when free, else on the next class, and waits
  only when every pool is full. A LocalQueue pointing at `prefer-<class>` is
  all a job needs (`kueue.x-k8s.io/queue-name`); the API maps a model's
  preferred class to it.
- `DaemonSet prepull-<pool>` in `models`; the image list per cluster is
  derived by the config stage from the bundled run classes (`catalog/models`).
- `ConfigMap kueue-system/fleet-prices`: per-pool price, class, capacity,
  GPUs per node, min and max nodes and node-group id (`pools.yaml`), the
  platform list (`list.yaml`) for the cost-aware dispatcher, the price feed
  and the API (class affinity). OpenCost keeps one GPU / spot price per
  cluster in the platform stage, set from the same numbers.
- DCGM exporter follows any `serverless2.nebius/pool` label except
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
  order); the cost-aware dispatcher replaces the static order with
  live prices from `fleet-prices` and `spot-prices` (`docs/SCHEDULING.md`).
- Resume after spot preemption: MultiKueue keeps a Workload on the worker
  that admitted it; the Job's own `backoffLimit` / JobSet restart and the
  checkpoint on the region's shared filesystem resume it in the same region.
  A workload is only re-dispatched to another region when the
  worker evicts it entirely.

Endpoints: Knative scales them (a revision superseded by a newer one is
collected after an hour; a model whose first revision never becomes Ready
keeps its Pending pod and GPU request until the model is changed or deleted,
Knative never collects the latest revision). A superseded revision that never
became Ready (a crash-looping container, no node for its GPUs) also keeps its
initial replica, and so its GPU request, until Knative's activation timeout:
the revision's progress deadline plus 30 s, measured from its PodAutoscaler's
`Active=Unknown` transition (`kubectl -n models get podautoscaler
<model>-predictor-0000N -o jsonpath='{.status.conditions}'`). The fleet's
deadline is `progress-deadline: 3600s` (`charts/platform/templates/knative-serving`,
sized for a trillion-parameter model on a cold node), so on a pool that cannot
add a node the new revision waits up to an hour for the old one's GPU;
on a pool with room the autoscaler adds a node and the new revision starts
beside it. Future knob, not implemented: Knative reads a per-revision
`serving.knative.dev/progress-deadline` annotation before the fleet default,
so the renderer could carry a per-model startup deadline (a small model's
never-ready revision would release its GPU in minutes while the giants keep
the hour). The API pins every endpoint
deployment to a pool (`docs/SCHEDULING.md` "What a model declares"), so
endpoints stay outside Kueue and their warm replicas are the static
`endpoint_floor_gpus` deduction of their pool. The renderer can also hand an
endpoint to Kueue (`queue.name`; the `models` namespace is Kueue-managed),
in which case its replicas count in the quota exactly and the floor must be
0 for them; the API does not use that mode.

## Ephemeral storage

A run class may request the kubelet's node-local scratch (`ephemeral_storage` / `ephemeralStorage`, docs/JOBS.md "Run
class"). Kueue admits a request only for a resource the ClusterQueue covers, so every queue covers `ephemeral-storage`
and every flavor budgets it from the pool's boot disk: per node `boot_disk_gib x node_reserve.ephemeral_fraction`
(0.85) `- node_reserve.ephemeral_gib` (4 Gi), times the pool's nodes (the same `max_nodes` minus the warm floor as
for CPU and memory); the `system` flavor from the system nodes' fixed 128 GiB disk. The budget stays under what the
kubelet reports allocatable (measured on this fleet: ~109.6 Gi of a 128 GiB disk, ~444.4 Gi of a 512 GiB one; the
budget is 104 and 431 Gi), which keeps the system reservation, the image cache and the containers' writable layers
their headroom. The quota is an admission bound, not a provisioning: a pod's actual scratch is still the node's disk,
and a `local-nvme` scratch class lands on NVMe pools as before. Tune `node_reserve.ephemeral_fraction` /
`ephemeral_gib` per fleet when a node image reserves more.

## Tenants

`charts/tenant` renders, per cluster, the tenant namespace with one LocalQueue per profile (`default`,
`prefer-<class>` for every class of the fleet) pointing at the tenant's own ClusterQueues `<tenant>-<profile>`
that `charts/fleet` renders on every cluster (`charts/fleet/templates/worker-queues.yaml`); on the control cluster
(`manager: true`) the namespace, those queues and the API binding only. The platform stage renders it on every
cluster of the tenant's regions (`stack/platform/tenants.tf`), after the fleet chart.

How capacity is shared (`terraform.tfvars` `tenants.<name>`):

| Field | Effect |
|---|---|
| (default) | the fleet's `default` ClusterQueue holds the nominal quota of every shared pool and lends it; a tenant's queues have nominal 0 on those pools and borrow up to the pool's capacity, in the profile's order (reserved, on-demand, spot; smallest preset first) |
| `fair_share_weight` (1) | Kueue weighted fair sharing between the tenants' queues (`fairSharing` in the Kueue configuration, strategies LessThanOrEqualToFinalShare then LessThanInitialShare): when the fleet is full, the tenant furthest above its weighted share is preempted first, so one tenant's sweep never starves another; priority classes still order work inside a queue |
| `gpu_quota` | caps what the tenant may borrow on each pool (at most that many GPUs of any one pool); unset = up to the pool |
| `storage` | `managed` (default): a bucket and an S3 key per region for the runs' inputs and outputs (cloud stage, `stack/cloud/tenants.tf`); `external`: none, the tenant's runs use application-managed I/O only (docs/JOBS.md "Execution options"). Do not flip an existing tenant to `external`: its buckets would be planned for destruction (`protect_data` refuses) |
| `identities` | workload identities a run may name (`workload_identity_ref`): `{ <ref> = { audience = "sts.amazonaws.com", env = { AWS_ROLE_ARN = "..." } } }` renders a ServiceAccount `identity-<ref>` whose projected token the run presents to a storage provider's OIDC federation |
| `execution_profiles` | execution profiles a run may name (`profile_ref`): `{ <name> = { egress_fqdns = [...], egress_cidrs = [...], shared_filesystem = false } }` renders the network policy its pods get instead of the tenant-wide "world" egress |
| (clients) | `GET /v1/fleet` reports the caller's tenant (quota, weight, dedicated pools) and, with `live=true`, the GPUs its queues hold per region |
| `dedicated_pools` | pool names (fleet-wide unique) that belong to this tenant: their quota is the tenant's own nominal quota, lent to nobody, listed in no other tenant's queue; the dispatcher never nominates them for another tenant's run and the API never pins an endpoint to them. The node isolation this gives is "no other tenant's run is ever scheduled there"; a node taint would add defence in depth, but the GPU and Network Operator DaemonSets would each need the toleration, so none is rendered |

The dispatcher reads the nominal quota from the lender and the usage from every queue of the cohort (a tenant's
borrowed GPUs are what empties the pool); its profile names accept the tenant prefix.

## Change recipes

- **Moving a tenant's queues**: a LocalQueue's target queue is immutable. When a change moves a tenant's queues
  to other ClusterQueues, delete the old LocalQueues in `tenant-<name>` first
  (`kubectl -n tenant-<name> delete localqueue --all`); waiting runs are re-queued by Kueue, running ones are not
  touched. The platform stage renders the fleet chart (the ClusterQueues) before the tenant releases.
- **Add a pool / GPU class**: one entry under `regions.<r>.pools`; `plan`
  shows one node group (and a pricing policy for a capped spot pool); Terraform
  adds the flavor, the `prefer-<class>` queue, the pre-pull DaemonSet and the
  price line. No other file.
- **Use a reservation**: `capacity = { type = "reserved", reservation_ids = [...] }`
  on a pool whose `platform`/`preset` match the capacity block; the pool is
  `STRICT` and sorts first in its class.
- **Raise capacity**: `max_nodes` (plan = in-place node-group update), the
  quota follows on the next sync.
- **Multi-node run on an InfiniBand pool**: nothing to do; the pool's floor follows the run
  (docs/SCHEDULING.md "InfiniBand pools grow on demand"). `max_nodes` bounds it: a run that needs more nodes
  than the pool has is a fleet change. While a floor is raised, `plan` of the cloud stage shows it going back
  (accepted drift; it is clean again once the pool is idle).
- **Warm endpoint added**: raise the pool's `endpoint_floor_gpus` with the
  model change (docs/OPERATIONS.md "Capacity for runs and endpoints").
- **Add a region**: one `regions.<name>` entry (project, subnet, pools; the preflight reports the tenant-wide
  `compute.disk.count` and preemptible-VM headroom of the region, a worker region needs about eight disks),
  then `./stack.sh apply` (the cloud stage adds the cluster and the tenants'
  buckets there, the platform stage follows on it, and the control cluster's
  MultiKueue gains the worker).
- **Prices**: `prices` block (nebius.com/prices); flavors, `fleet-prices` and
  the profile order follow.

## Known limits

- The quota deduction for warm endpoints is static (`endpoint_floor_gpus`);
  only a Kueue-managed endpoint (`queue.name`, not used by the API) would be
  exact.
- MultiKueue manager quotas mirror the definition (`quotaManagement: Manual`);
  Kueue's automated quota management needs its feature gate and is not
  enabled.
- Spot list prices are the "from" prices; the live spot price comes from the
  price feed, not from the definition.

GPU node groups carry a cloud-init fragment (`stack/modules/cluster/main.tf` `gpu_cloud_init`): the containerd registry drop-in of the image cache (docs/IMAGES.md; absent with `images.cache.enabled = false`), the memlock limit for RDMA and, when `weights_filesystem` is enabled, the virtiofs mount of the shared weights filesystem. Changing it rolls the pools (surge 1).

## Spot capacity: how `capacity` maps to the Nebius API

Nebius sells preemptible capacity under dynamic pricing (the API field is still `preemptible`): a
preemptible VM or node group declares exactly one of `follows_spot_price` (pay the current quote, never
stopped for price), `spot_pricing_policy` (a pricing policy with a cap: stopped above it, restarted below
it) or `on_demand`. The pool input renders them, one and only one (`stack/modules/cluster/main.tf`):

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

## Warm spare nodes

`pools.<name>.warm_nodes = N` keeps N nodes of the pool running with no model on them. This is the
cluster autoscaler's "overprovisioning" pattern, installed as the `cluster-overprovisioner` chart
(Delivery Hero, `clusters/common/apps/overprovisioner.yaml`) on every worker: one Deployment of `pause`
pods per pool with spares (the platform stage computes the list from the pools), each pod requesting
every GPU of a node under the PriorityClass `serverless2-warm-spare` (value -1). Every real pod has
priority 0 or more (endpoints) or the run priority classes (100 and up), so the scheduler evicts a
placeholder the moment a model needs the node and the model starts without waiting for an instance to
boot; the evicted placeholder goes Pending and the cluster autoscaler brings the next spare up (its
priority cutoff is -10, so the placeholder counts), within `max_nodes`. The pre-pull DaemonSet has
already warmed the platform images on the node; a model's own image is pulled from the cache.

What it costs and what it is not: a spare is a running node (spot or on-demand, the pool's capacity type)
billed whether a model uses it or not; `min_nodes` keeps a floor too, but the first model occupies it and
nothing refills the spare. A spare serves every model whose GPU class and count fit the pool's preset, in
that region. It is not an extra application replica (see `scaling.min` for that), and InfiniBand pools do
not take it (their floor follows the multi-node runs instead, docs/SCHEDULING.md "InfiniBand pools grow
on demand").

## CPU-only runs: the `system` flavor

Every ClusterQueue (the fleet's `default`, each tenant's `default` and `prefer-<class>`, on the workers and mirrored
on the manager) lists a `system` flavor first: the region's CPU system nodes (`serverless2.nebius/pool=system`, no GPU
toleration), with `nvidia.com/gpu: 0` and CPU/memory = `regions.<r>.system_pool` (`node_count` x preset) minus the
per-node `system_reserve` of `charts/fleet/values.yaml` (default 4 CPUs and 16 GiB for the platform's add-ons), the
tenants borrowing it like any pool. Kueue's flavor order makes the split exact: a workload that requests a GPU
cannot fit the `system` flavor and takes its pool flavor as before; a GPU-free workload (the application finalizer,
an export retry, a CPU run class) fits it first and is scheduled onto the system nodes of the cluster that admitted
it, with the pool node selector of a GPU flavor never injected. No node is added and no GPU quota changes; the
budget is what the system nodes already have. `tools/check.sh` asserts the rendered order and quotas.

## CPU pools

A run that needs more CPU or memory than the system nodes budget (an AlphaFold 3 data pipeline at 16 CPUs / 64 GiB
on 8-vCPU system nodes) gets a CPU pool: `regions.<r>.cpu_pools.<name> = { platform, preset = "<n>vcpu-<n>gb",
min_nodes (0), max_nodes, boot_disk_gib (128), shared_filesystem (true) }`. The cloud stage creates the node group
(label `serverless2.nebius/pool=<name>`, taint `serverless2.nebius/cpu-pool=present:NoSchedule`, autoscaled between
min and max) and, with `shared_filesystem`, attaches and mounts the region's weights filesystem at `/mnt/weights`
exactly like a GPU pool (a run class's `weights`/`data`, the shared checkpoints and the restricted assets need it on
the node; the system nodes have it only with `weights_filesystem.mount_on_system`). `charts/fleet` renders one
ResourceFlavor per CPU pool (its node label, its taint tolerated, no GPU toleration) and lists it in every ClusterQueue
**after `system`** with `nvidia.com/gpu: 0` and CPU/memory = `max_nodes` × preset minus the per-node `system_reserve`.
Kueue's flavor order does the routing: a GPU request skips the GPU-free flavors; a GPU-free workload that fits the
system nodes stays there (finalizers, export retries, small CPU classes); one that does not takes the CPU pool, is
admitted against the pool's quota, lands Pending with the pool's node selector and toleration (Kueue injects both),
the node group scales up, the pod runs, the group scales back down. The manager mirrors the flavors and quotas, so a
fleet-placed CPU run goes to a region whose CPU pool has room. No class setting is needed; a class may still pin
`pool: <name>`. `tools/check.sh` asserts the order (system, CPU pools, GPU pools) and the tolerations. The flavor
order alone is an aggregate decision: a GPU-free run that does not fit the CPU pool's quota for the moment would move
on to a GPU pool whose aggregate CPU covers it while no single node does; the API's per-node fit
(docs/SCHEDULING.md "Per-node fit", `fit.yaml` of the fleet document) keeps such a run on the pools whose node holds it.

## Filesystem-aware placement

The shared weights filesystem is a hostPath on the nodes that mount it (every GPU pool of a region that has it, CPU
pools with `shared_filesystem`, the system pool only with `weights_filesystem.mount_on_system`). A run that mounts it
(`weights`, `data`, `checkpoints: shared`, `assets`) must land on such a node, else the kubelet fails the mount for
ever. Three pieces make that structural: the cluster module labels every node group `serverless2.nebius/shared-fs`
`true` or `false` by its attachment; every worker ResourceFlavor carries the same label in `nodeLabels`; the API
renders a required node affinity `serverless2.nebius/shared-fs In [true]` on such runs (as it does `local-nvme` for
`scratch: local-nvme`). Kueue never assigns a flavor whose `nodeLabels` contradict a pod's required affinity, so such a
run skips the unmounted `system` flavor and takes a CPU pool or its GPU pool; a run that mounts nothing shared keeps
`system`. Existing Jobs do not gain the affinity by a rollout (docs/OPERATIONS.md "Repair a run admitted on an unmounted
flavor"); the static PVs keep no node affinity (immutable once created; the flavor label is the guard).

## GPU drivers: the node image or the NVIDIA GPU Operator

Two ways to get a driver onto a GPU node, chosen once per fleet in `terraform.tfvars`:

| | Managed Kubernetes node image (default) | NVIDIA GPU Operator (`gpu_operator.enabled = true`) |
|---|---|---|
| Node group | `gpu_settings.drivers_preset = pools.<name>.driver_preset` (`cuda13.0`) | no `drivers_preset` (`dra = true` stays on InfiniBand pools) |
| Driver, toolkit | baked into the Nebius image (`nebius.com/driverful=true`, `nebius.com/nvidia_driver_version`) | driver container (`nvidia-driver-daemonset`, one NVIDIADriver resource per platform from the Nebius chart), container toolkit sets the `nvidia` runtime |
| Device plugin, GFD, NFD | the fleet's `nvidia-device-plugin` release (`clusters/common/apps/nvidia-device-plugin.yaml`) | the operator's (`gpu-operator.yaml`, namespace `nvidia-gpu-operator`); the fleet release is not installed |
| RDMA / InfiniBand | OFED in the image, DraNet (Managed Kubernetes) advertises the NICs | NVIDIA Network Operator (`network-operator.yaml`, DOCA driver container on every node with a Mellanox NIC) installed first on every worker cluster: the Nebius driver profiles of the SXM platforms wait for it on such nodes, InfiniBand or not (a node without the fabric waits for it just the same); `gpu_operator.network_operator = false` leaves it out. DraNet unchanged |
| Driver version | whatever the preset ships | `gpu_operator.driver_version` (an image tag of the marketplace driver image: `crane ls cr.eu-north1.nebius.cloud/marketplace/nebius/nvidia-gpu-operator/image/driver`) |
| Kernel modules, GDS | the image's | `gpu_operator.kernel_module` (open, proprietary, auto) and `gpu_operator.gds` (GPUDirect Storage: the `nvidia-fs` module built next to the driver, needs the open modules; experimental: the module loads, but cuFile direct I/O on local NVMe was not achieved on the current node images and the shared filesystem never supports it) |
| MIG | no (the node device plugin has no MIG manager) | the operator's MIG manager (`nvidia.com/mig.config` node label) |
| DCGM metrics | the fleet's `dcgm-exporter` release | the same (the operator's exporter stays off) |

One NVIDIADriver per GPU platform: the chart's list lives in `clusters/common/values/gpu-operator.yaml` and the
platform stage appends a profile for every platform of the cluster's pools that the list lacks (a regional
variant such as `gpu-rtx6000-a` gets the manager settings of its longest-prefix sibling, `gpu-rtx6000`). A node whose
`node.kubernetes.io/instance-type` matches no profile never receives a driver: the toolkit waits for driver
validation and the node stays at zero allocatable GPUs. Check with `kubectl get nvidiadrivers` that every platform
of the region appears.

Why one would switch: a driver version other than the preset's, GPUDirect Storage, MIG profiles or any other
operator feature, and one driver stack that is the same on every platform. Why one would not: the node image is
ready at boot, the operator's driver container compiles and loads the module after the node joins (about
three to five minutes on a cold node, during which the node has no `nvidia.com/gpu` and the scale-from-zero of a
model or run takes that much longer); a warm spare (`warm_nodes`) absorbs that.

How a change rolls out: `./stack.sh apply platform <id>` installs the operators (the operands only touch nodes
without the Nebius driver image, so running nodes are unaffected) and removes the fleet's device-plugin release,
then `./stack.sh apply cloud` updates every GPU node group without a driver preset and Managed Kubernetes
recreates the nodes with its deployment strategy (one at a time, drained first); each new node is taken by the
operator. The order matters: with the operator installed second, the first driverless nodes would wait for it.
Nodes with `dra = true` (InfiniBand pools) lose their `nvidia.com/gpu` the moment the fleet's device plugin is
removed and get it back when they are recreated; plan the change for a quiet hour on those pools.

## Local NVMe: which platform/preset combinations have it

Nebius exposes host NVMe to Managed Kubernetes nodes through the node-group template
`local_disks = { passthrough_group = { requested = true }, config = { kubelet_ephemeral = true | none = true } }`
(`stack/modules/cluster/main.tf`, pool input `local_nvme` / `local_nvme_mode`). The preset API does not
advertise local disks; the authoritative list is the Compute documentation ("Using local SSD disks",
docs.nebius.com/compute/storage/local-disks, "Availability"), which lists: *Local SSD disks are available
only in the uk-south1, eu-west2 and us-north1 regions, only on the NVIDIA B300 NVLink with Intel Granite
Rapids platform (gpu-b300-sxm) with the eight-GPU preset 8gpu-192vcpu-2768gb* (6 x 3.84 TB).

| Platform | Preset | Local disks | Regions | Notes |
|---|---|---|---|---|
| gpu-b300-sxm | 8gpu-192vcpu-2768gb | 6 x 3.84 TB NVMe | uk-south1, eu-west2, us-north1 | verified on a spot node: six `MTFDKCC3T8TGP` 3.5 TiB NVMe in a RAID0 (`md127`, ext4, 21 TiB) under `/mnt/local-ephemeral` with `/var/lib/kubelet` and `/var/lib/containerd` on it; node capacity `ephemeral-storage` 22.4 TB (allocatable 20.65 TB); a `scratch: local-nvme` run wrote 100 GiB at 6.7 GB/s (one `dd`, direct, fsync) and read it at 1.6 GB/s single-stream / 5.7 GB/s with four readers. As a spot pool the preemptible quota applies (`compute.instance.preemptible.count`), not the B300 GPU quota |
| gpu-b300-sxm | 1gpu-24vcpu-346gb | none | eu-west2 | a node group with `local_disks` is rejected |
| gpu-h100-sxm | 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb | none | eu-north1 | rejected: `local_disks.passthrough_group.requested is invalid` |
| gpu-h200-sxm, gpu-b200-sxm, gpu-gb300, gpu-l40s-a/-d, gpu-rtx6000-a | all | none | - | not listed in the documentation (the H100 probe above is the behaviour on an unlisted preset) |

Consequences for a fleet definition: set `local_nvme = true` only on a `gpu-b300-sxm` /
`8gpu-192vcpu-2768gb` pool in one of the three regions; the apply fails early (node-group creation) on
anything else, nothing is left half-built. What the flag does when it is accepted: the node group formats
the six disks as the kubelet's ephemeral storage (`kubelet_ephemeral`), so every `emptyDir`, image layer
and `scratch: local-nvme` run volume (`/work`, docs/JOBS.md) lands on NVMe, and the node carries the label
`serverless2.nebius/local-nvme=true` that the API's node affinity for such runs selects;
`local_nvme_mode = raw` leaves the devices unformatted (`config.none`) for a workload that owns them. The
rendering (node-group template, node label, catalog `scratch`, pod affinity and emptyDir) is covered by
`terraform plan` and the API unit tests. Two details matter: the Kueue flavor of an NVMe pool carries the
`local-nvme` label (without it Kueue would ignore the affinity key and admit such a run on any flavor),
and a local-NVMe run is dispatched only to regions that have an NVMe pool. The NVMe content does not
survive a preemption: a stopped spot VM loses `/mnt/local-ephemeral` (the kubelet re-formats it on the
next boot), so a run on local NVMe keeps its checkpoints elsewhere (the network work volume of
`scratch: network`, or its own uploads).
