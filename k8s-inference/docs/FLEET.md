# The fleet: one definition of the inference cluster

*For operators and architects: how pools, capacity types, GPU classes and queues relate. Written for the reference fleet's `fleet.yaml`; in the Terraform solution the same keys live under `regions.*.pools` in `terraform.tfvars`.*

`fleet.yaml` at the repo root defines the whole Serverless 2.0 inference
cluster: the control-plane cluster, every region, every GPU pool (platform,
preset, GPU class, reserved / on-demand / spot capacity, min and max nodes,
warm-endpoint floor), the shared weights filesystem per region and the price
list. Two consumers read it and nothing else defines capacity:

| Consumer | Reads | Produces |
|---|---|---|
| `infra/fleet` (Terraform wrapper over the `infra/cluster` module) | control + regions | one Managed Kubernetes cluster per entry, system pool, GPU node groups with their reservation / spot policy, node identity, ops identity, registries, backups bucket, weights filesystem; one state per cluster |
| `charts/fleet` (Helm, rendered by Argo CD on every cluster) | regions, prices, `node_reserve`, `capacity_order` | worker: Kueue ResourceFlavor per pool, `default` + `prefer-<gpu-class>` ClusterQueues, pre-pull DaemonSet per pool, `fleet-prices` ConfigMap; control: MultiKueueCluster per region, MultiKueueConfig + AdmissionCheck per profile, fleet-wide flavors and profile queues |

Everything that used to live in `clusters/<cluster>/cluster.tfvars` and in
`clusters/<cluster>/apps/overlays/scheduling/pool.yaml` + `overlays/edge/prepull*.yaml`
is derived from this one file (the overlays stay until the cut-over below).

## fleet.yaml

```yaml
fleet:
  kubernetes_version: "1.35"
  node_reserve: { cpu: 2, memory_gib: 20 }     # per node, outside Kueue quota (daemonsets, kubelet)
  capacity_order: [reserved, on_demand, spot]  # inside one GPU class, in every preference queue
  control: { id: control, cluster: serverless2-control, region: eu-north1, project: ..., subnet: ..., system_pool: {...}, allowed_cidrs: [...] }
  regions:
    <region name>:
      id: <cluster id>            # Argo CD directory clusters/<id>, Terraform state infra/state/<id>
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
                                  # needs min_nodes >= 1: DRA claims cannot scale a pool from zero; the node group
                                  # gets gpu_settings.dra = true so Managed Kubernetes runs DraNet on its nodes)
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

## Terraform: `infra/fleet`

```
infra/fleet/apply.sh plan all              # every cluster in fleet.yaml, read-only
infra/fleet/apply.sh plan hub              # must be "no changes" for an adopted cluster
infra/fleet/apply.sh apply control         # creates the control cluster (only on "go")
```

`render.py <id>` turns a cluster's entry into `infra/fleet/rendered/<id>.tfvars.json`
(gitignored); `apply.sh` initialises the module with that cluster's own
state (key `clusters/<id>.tfstate` in the fleet's Object Storage state bucket,
`infra/backend.hcl`, locked per command; `infra/fleet/backend.sh`) and runs
plan / apply / output / state. An empty state means "create everything":
`plan` prints a `!!` line, `apply` refuses unless `FLEET_NEW_CLUSTER=<id>`
names that one new cluster. Bucket credentials come from the environment or
`~/.config/serverless2/tfstate.env` (`infra/bootstrap/state-bucket.sh`); any
operator with them and a Nebius profile with `admin` on the projects can run
this (docs/BOOTSTRAP.md). `FLEET_BACKEND=local` keeps using the local files
`infra/state/<id>/terraform.tfstate` of before 2026-10-07 (from a git worktree
with `FLEET_STATE_DIR=/path/to/main/infra/state`).
The states of the hub and eu-south1 are the ones that existed before
`fleet.yaml`: adoption was proven with `plan` = no changes on eu-south1 and
only the new L40S pool on the hub (the `moved` blocks in `infra/cluster/main.tf`
keep the imported ops identities in place). Never run two applies at once.

Weights filesystem: `weights_filesystem: {enabled: true, size_gib: 2048}` on
the hub (2026-10-06) and on eu-south1 (2026-10-07, after the quota
`compute.filesystem.size.network-ssd` was raised to 5 TiB; apply = 1 added,
1 changed, the RTX pool rolled in 4 min 47 s with surge 1). Enabling it on a
region that already has nodes recreates the GPU nodes (the mount comes from
the node-group template); the control cluster never has one. The region's
`clusters/<cluster>/apps/overlays/scheduling/weights-pv.yaml` must exist
alongside (`docs/OPERATIONS.md` "Model weights").

Reservations: a pool with `capacity.type: reserved` gets
`reservation_policy = STRICT` with its `reservation_ids`, so its nodes come
only from those capacity blocks ("16 H100 reserved" = one pool with
`max_nodes: 16` and the reservation id); every other pool is `FORBID`. A
reserved pool and a spot pool of the same class are two pools; the
preference queue orders them reserved first. The control cluster is CPU-only
(`gpu_pools = {}`, `ops.enabled = false` because it shares the hub's project
and ops identity, no weights filesystem).

## Scheduling: `charts/fleet`

Rendered per cluster by Argo CD with `fleet.yaml` plus
`clusters/<id>/values/fleet.yaml` (the pre-pull image list) and
`--set cluster=<id>` (ApplicationSet kind `localhelm`, staged spec in
`clusters/common/apps-staged/fleet.yaml`). Local check:

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

Endpoints: Knative scales them; an endpoint with `runtime.queue.name` set
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
`python -m onboarding create-tenant <name>` applies it everywhere and
removes the v1 objects.

## Cut-over (lane F2, done 2026-10-07)

1. `infra/fleet/apply.sh apply control`; bootstrap Argo CD on it with a
   `clusters/control/apps` tree (ApplicationSet with `cluster: control`,
   `hubOnly` filtered out, the `localhelm` kind) and the Kueue chart with
   the MultiKueue feature enabled.
2. On each worker: service account + ClusterRole for MultiKueue, kubeconfig
   into `kueue-system/multikueue-<region id>` on the control cluster.
3. Move `clusters/common/apps-staged/fleet.yaml` to `clusters/common/apps/`
   and in the same commit remove `pool.yaml` from
   `clusters/<id>/apps/overlays/scheduling/kustomization.yaml` and
   `prepull*.yaml` from `overlays/edge/kustomization.yaml` (two Applications
   must not own one object; Argo CD applies server-side, so the objects are
   adopted, not recreated). Keep `weights-pv.yaml` and the rest.
4. Tenants: `charts/tenant` LocalQueues gain a `prefer-<class>` target per
   tenant (or the API creates them on demand) on the control cluster and on
   every worker with the same name.
5. Hub apply for the L40S pool only when the owner wants the second class
   live (`infra/fleet/apply.sh plan hub` shows exactly that one node group).

## Change recipes

- **Add a pool / GPU class**: one entry under `regions.<r>.pools`; `plan`
  shows one node group (and a pricing policy for a capped spot pool); Argo CD
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
  `apply.sh apply <id>`, Argo CD bootstrap, one `MultiKueueCluster` Secret;
  docs/OPERATIONS.md "Add a region" for the per-cluster values.
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

GPU node groups carry a cloud-init fragment (`infra/cluster/main.tf` `gpu_cloud_init`): the containerd registry drop-in of the image cache (docs/IMAGES.md) and, when `weights_filesystem` is enabled, the virtiofs mount of the shared weights filesystem. Changing it rolls the pools (surge 1).

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
| gpu-b300-sxm | 8gpu-192vcpu-2768gb | 6 x 3.84 TB NVMe | uk-south1, eu-west2, us-north1 | documentation; not creatable by this program (B300 quota of the eu-west2 project: 5 GPUs, the preset needs 8) |
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
`terraform plan` and the API unit tests; the owner's projects have no quota for the only NVMe preset, so an
end-to-end NVMe run is still to be done by the first fleet with an 8-GPU B300 reservation.
