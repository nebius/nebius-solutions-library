# Module `cluster`

One Managed Kubernetes cluster of the fleet and everything that belongs to it in the cloud: the
cluster, its node identity, the system pool, the GPU and CPU pools (spot, on-demand or reserved
capacity), one Nebius GPU cluster per InfiniBand pool, the optional shared weights filesystem and
the static public IP of the gateway. Per-project resources (operations identity, registry, buckets)
live in `stack/cloud`.

Used once per cluster by `stack/cloud/main.tf`. Nothing in here touches Kubernetes itself; that is the
platform stage.

## Inputs

| Name | Description | Type | Default |
|---|---|---|---|
| `name` | Cluster name and prefix of its resources | `string` | required |
| `project_id` | Project the cluster lives in | `string` | required |
| `subnet_id` | Subnet for the control plane and the nodes | `string` | required |
| `kubernetes_version` | Kubernetes version | `string` | required |
| `labels` | Labels on every resource | `map(string)` | `{}` |
| `control_plane_allowed_cidrs` | Who may reach the Kubernetes API (empty = open) | `list(string)` | `[]` |
| `system_pool` | CPU node group for the add-ons | `object` | required |
| `gpu_pools` | GPU node groups (`regions.*.pools` of terraform.tfvars) | `map(object)` | `{}` |
| `cpu_pools` | CPU-only batch node groups | `map(object)` | `{}` |
| `weights_filesystem` | Shared filesystem for model weights | `object` | required |
| `protect_data` | `prevent_destroy` on the filesystem | `bool` | `true` |
| `image_cache` | Logical registry host and cache NodePort for containerd | `object` | required |
| `public_ip` | Static public IP for the gateway | `bool` | `true` |

## Outputs

| Name | Description |
|---|---|
| `cluster_id` | Managed Kubernetes cluster id |
| `endpoint` | Public Kubernetes API endpoint |
| `cluster_ca_certificate` | CA certificate of the API endpoint |
| `gateway_ip` | Static public IP of the gateway (null in internal mode) |
| `gateway_allocation_id` | Id of that allocation (annotated onto the gateway Service) |
| `weights_filesystem_id` | Filesystem id (null when disabled) |
| `nodepull_service_account_id` | Node identity (registry pull) |
| `gpu_node_group_ids` | Node group id per GPU pool |
| `gpu_cluster_ids` | Nebius GPU cluster id per InfiniBand pool |

## What the nodes get at boot

- GPU nodes: containerd reads the image cache's mirror configuration (`/etc/containerd/certs.d`) and
  points the logical registry host at the cache on the node itself, so the first pull of a fresh node
  already goes through the cache; the weights filesystem is mounted at `/mnt/weights` (virtiofs).
- System and CPU nodes: higher inotify limits (the add-ons exhaust Ubuntu's default over time).
- InfiniBand pools: the node group joins its GPU cluster and `gpu_settings.dra = true` lets Managed
  Kubernetes advertise the fabric NICs to pods (docs/JOBS.md "Multi-node runs").
- Reserved pools roll with zero surge (a full reservation cannot surge); every other pool with surge 1.
