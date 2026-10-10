# One Managed Kubernetes cluster of the fleet: the cluster, its node identity, the system pool, the GPU and
# CPU pools, the optional shared weights filesystem and the static public IP of its gateway. Identities that
# are per project (ops service account, registry, buckets) live in stack/cloud/main.tf.
locals {
  labels = merge(var.labels, { "serverless2.nebius/cluster" = var.name })
}

data "nebius_vpc_v1_subnet" "target" { id = var.subnet_id }

# Node identity: Managed Kubernetes exchanges it for registry pull credentials; viewer at project scope.
resource "nebius_iam_v1_service_account" "nodepull" {
  parent_id   = var.project_id
  name        = "${var.name}-nodepull"
  description = "Node identity for ${var.name}"
  labels      = local.labels
}

resource "nebius_iam_v1_group" "nodepull_viewers" {
  parent_id = var.project_id
  name      = "${var.name}-nodepull-viewers"
}

resource "nebius_iam_v1_group_membership" "nodepull" {
  parent_id = nebius_iam_v1_group.nodepull_viewers.id
  member_id = nebius_iam_v1_service_account.nodepull.id
}

resource "nebius_iam_v1_access_permit" "nodepull_viewer" {
  parent_id   = nebius_iam_v1_group.nodepull_viewers.id
  resource_id = var.project_id
  role        = "viewer"
}

resource "nebius_mk8s_v1_cluster" "this" {
  parent_id = var.project_id
  name      = var.name
  labels    = local.labels
  control_plane = {
    subnet_id         = data.nebius_vpc_v1_subnet.target.id
    version           = var.kubernetes_version
    etcd_cluster_size = 3
    audit_logs        = {}
    endpoints = {
      public_endpoint = { allowed_cidrs = var.control_plane_allowed_cidrs }
    }
  }
}

# System pool: platform add-ons, caches, gateways. inotify instances raised (registry credential helpers and
# controllers exhaust Ubuntu's default on a shared node over weeks).
resource "nebius_mk8s_v1_node_group" "system" {
  parent_id        = nebius_mk8s_v1_cluster.this.id
  name             = "${var.name}-system"
  labels           = merge(local.labels, { pool = "system" })
  version          = var.kubernetes_version
  fixed_node_count = var.system_pool.node_count
  strategy = {
    max_surge       = { count = 1 }
    max_unavailable = { count = 0 }
    drain_timeout   = "300s"
  }
  template = {
    metadata             = { labels = merge({ "serverless2.nebius/pool" = "system" }, local.snapshot_store_labels) }
    boot_disk            = { size_gibibytes = 128, type = "NETWORK_SSD" }
    network_interfaces   = [{ subnet_id = data.nebius_vpc_v1_subnet.target.id }]
    os                   = "ubuntu24.04"
    cloud_init_user_data = local.snapshot_system_cloud_init
    filesystems          = var.weights_filesystem.mount_on_system ? local.weights_fs_attachment : null
    reservation_policy   = { policy = "FORBID" }
    resources            = { platform = var.system_pool.platform, preset = var.system_pool.preset }
    service_account_id   = nebius_iam_v1_service_account.nodepull.id
    underlay_required    = false
  }
  depends_on = [nebius_iam_v1_access_permit.nodepull_viewer]
}

# One pricing policy per spot pool with a cap (mutable only while unused: created before the pool).
resource "nebius_billing_v1_pricing_policy" "cap" {
  for_each              = { for k, p in var.gpu_pools : k => p if p.capacity.type == "spot" && p.capacity.max_price != null }
  parent_id             = var.project_id
  name                  = "${var.name}-${each.key}-cap"
  compute_instance_spec = { v1 = { platform = each.value.platform } }
  pricing               = { max_price_v1 = { max_price = each.value.capacity.max_price } }
}

# One Nebius GPU cluster per InfiniBand pool: every node of the pool joins it and the nodes talk InfiniBand
# over the pool's fabric (nodes of different GPU clusters cannot, even on the same fabric).
resource "nebius_compute_v1_gpu_cluster" "ib" {
  for_each          = { for pn, p in var.gpu_pools : pn => p if p.interconnect == "infiniband" }
  parent_id         = var.project_id
  name              = "${var.name}-${each.key}-ib"
  labels            = merge(local.labels, { pool = each.key })
  infiniband_fabric = each.value.infiniband_fabric
}

resource "nebius_mk8s_v1_node_group" "gpu" {
  for_each  = var.gpu_pools
  parent_id = nebius_mk8s_v1_cluster.this.id
  name      = "${var.name}-${each.key}"
  labels    = merge(local.labels, { pool = each.key })
  version   = var.kubernetes_version
  autoscaling = {
    min_node_count = each.value.min_nodes
    max_node_count = each.value.max_nodes
  }
  # A reservation that is exactly full cannot surge: reserved pools roll with zero surge.
  strategy = {
    max_surge       = { count = each.value.capacity.type == "reserved" ? 0 : 1 }
    max_unavailable = { count = each.value.capacity.type == "reserved" ? 1 : 0 }
    drain_timeout   = "600s"
  }
  template = {
    metadata = { labels = merge({
      "serverless2.nebius/pool"     = each.key
      "serverless2.nebius/capacity" = each.value.capacity.type == "spot" ? "spot" : (each.value.capacity.type == "reserved" ? "reserved" : "on-demand")
      }, each.value.local_nvme && each.value.local_nvme_mode == "kubelet-ephemeral" ? {
      "serverless2.nebius/local-nvme" = "true" # scratch: local-nvme runs and NVMe-preferring endpoints select it
      } : {}, each.value.interconnect == "infiniband" ? {
      "serverless2.nebius/interconnect" = "infiniband" # multi-node job classes require it (Kueue flavor label too)
    } : {}, each.value.labels) }
    # Only GPU workloads (with a matching toleration) land on GPU pools, so idle GPU nodes scale to zero.
    taints    = [{ key = "nvidia.com/gpu", value = "present", effect = "NO_SCHEDULE" }]
    boot_disk = { size_gibibytes = each.value.boot_disk_gib, type = "NETWORK_SSD" }
    # drivers_preset: the node image with the pool's driver preset; omitted when the GPU Operator installs the
    # drivers (var.gpu_operator, docs/FLEET.md "GPU drivers"). dra (InfiniBand pools): Managed Kubernetes advertises
    # the nodes' RDMA NICs through its DraNet DaemonSet (the `nebius.com/dranet-rdma-capable` label, ResourceSlices
    # for DeviceClass ib.networking.nebius.ai) and disables its own legacy NVIDIA device plugin (the fleet runs the
    # nvidia-device-plugin chart or the operator's); multi-node runs claim the NICs.
    gpu_settings = length(local.gpu_settings[each.key]) > 0 ? local.gpu_settings[each.key] : null
    gpu_cluster  = each.value.interconnect == "infiniband" ? { id = nebius_compute_v1_gpu_cluster.ib[each.key].id } : null
    # Host-local NVMe of the preset: passed through and, in kubelet-ephemeral mode, formatted by Managed
    # Kubernetes as the kubelet's ephemeral storage (emptyDir, image layers); raw = devices left untouched.
    # (the API requires passthrough_group.requested; a platform/preset without local disks rejects the node group
    # with "passthrough_group.requested is invalid": H100 1x/8x in eu-north1 did on 2026-10-08, B200/B300 presets ship NVMe)
    local_disks = each.value.local_nvme ? {
      passthrough_group = { requested = true }
      config            = each.value.local_nvme_mode == "kubelet-ephemeral" ? { kubelet_ephemeral = true } : { none = true }
    } : null
    network_interfaces   = [{ subnet_id = data.nebius_vpc_v1_subnet.target.id }]
    os                   = "ubuntu24.04"
    filesystems          = local.weights_fs_attachment
    cloud_init_user_data = local.gpu_cloud_init
    preemptible          = each.value.capacity.type == "spot" ? {} : null
    spot_pricing_policy = each.value.capacity.type == "spot" && each.value.capacity.max_price != null ? {
      id = nebius_billing_v1_pricing_policy.cap[each.key].id
    } : null
    follows_spot_price = each.value.capacity.type == "spot" && each.value.capacity.max_price == null ? {} : null
    # Reserved pools launch only from their capacity blocks (STRICT); every other pool never consumes one.
    reservation_policy = {
      policy          = each.value.capacity.type == "reserved" ? "STRICT" : "FORBID"
      reservation_ids = each.value.capacity.type == "reserved" ? each.value.capacity.reservation_ids : null
    }
    resources          = { platform = each.value.platform, preset = each.value.preset }
    service_account_id = nebius_iam_v1_service_account.nodepull.id
    underlay_required  = false
  }
  depends_on = [nebius_mk8s_v1_node_group.system]
}

resource "nebius_mk8s_v1_node_group" "cpu" {
  for_each  = var.cpu_pools
  parent_id = nebius_mk8s_v1_cluster.this.id
  name      = "${var.name}-${each.key}"
  labels    = merge(local.labels, { pool = each.key })
  version   = var.kubernetes_version
  autoscaling = {
    min_node_count = each.value.min_nodes
    max_node_count = each.value.max_nodes
  }
  strategy = {
    max_surge       = { count = 1 }
    max_unavailable = { count = 0 }
    drain_timeout   = "600s"
  }
  template = {
    metadata = { labels = merge({
      "serverless2.nebius/pool"     = each.key
      "serverless2.nebius/capacity" = "on-demand"
    }, each.value.labels) }
    taints               = [{ key = "serverless2.nebius/cpu-pool", value = "present", effect = "NO_SCHEDULE" }]
    boot_disk            = { size_gibibytes = each.value.boot_disk_gib, type = "NETWORK_SSD" }
    network_interfaces   = [{ subnet_id = data.nebius_vpc_v1_subnet.target.id }]
    os                   = "ubuntu24.04"
    cloud_init_user_data = local.system_cloud_init
    reservation_policy   = { policy = "FORBID" }
    resources            = { platform = each.value.platform, preset = each.value.preset }
    service_account_id   = nebius_iam_v1_service_account.nodepull.id
    underlay_required    = false
  }
  depends_on = [nebius_mk8s_v1_node_group.system]
}

# Static public IP of the cluster's gateway load balancer: the platform stage annotates the Envoy Service
# with nebius.com/load-balancer-allocation-id, so re-creating the Service keeps the address (and the DNS).
resource "nebius_vpc_v1_allocation" "gateway" {
  count       = var.public_ip ? 1 : 0
  parent_id   = var.project_id
  name        = "${var.name}-gateway"
  labels      = local.labels
  ipv4_public = { subnet_id = data.nebius_vpc_v1_subnet.target.id }
}

# ---------------------------------------------------------------------------
# Shared filesystem for model weights: attached to every GPU pool, mounted by cloud-init (virtiofs), used by
# pods through a static hostPath PersistentVolume (platform stage). Two resource blocks because
# prevent_destroy must be a literal: `protected` refuses `terraform destroy` (fleet.protect_data).
resource "nebius_compute_v1_filesystem" "weights_protected" {
  count            = var.weights_filesystem.enabled && var.protect_data ? 1 : 0
  parent_id        = var.project_id
  name             = "${var.name}-weights"
  labels           = local.labels
  type             = var.weights_filesystem.type
  size_gibibytes   = var.weights_filesystem.size_gib
  block_size_bytes = 4096
  lifecycle { prevent_destroy = true }
}

resource "nebius_compute_v1_filesystem" "weights" {
  count            = var.weights_filesystem.enabled && !var.protect_data ? 1 : 0
  parent_id        = var.project_id
  name             = "${var.name}-weights"
  labels           = local.labels
  type             = var.weights_filesystem.type
  size_gibibytes   = var.weights_filesystem.size_gib
  block_size_bytes = 4096
}

locals {
  snapshot_store_labels = var.weights_filesystem.enabled && var.weights_filesystem.mount_on_system ? {
    "serverless2.nebius/snapshot-store" = "true"
  } : {}
  # Create a dedicated root-owned directory only after a successful shared mount.
  snapshot_fs_runcmd = var.weights_filesystem.enabled && var.weights_filesystem.mount_on_system ? [
    " - [sh, -ec, 'mountpoint -q /mnt/weights && mkdir -p /mnt/weights/gpu-snapshot && chmod 0700 /mnt/weights/gpu-snapshot']",
  ] : []
  weights_fs_id = var.weights_filesystem.enabled ? (var.protect_data ? nebius_compute_v1_filesystem.weights_protected[0].id : nebius_compute_v1_filesystem.weights[0].id) : null
  weights_fs_attachment = var.weights_filesystem.enabled ? [{
    attach_mode         = "READ_WRITE"
    mount_tag           = "weights"
    existing_filesystem = { id = local.weights_fs_id }
  }] : null
  weights_fs_runcmd = var.weights_filesystem.enabled ? [
    " - mkdir -p /mnt/weights",
    " - mount -t virtiofs weights /mnt/weights",
    " - printf \"%s %s virtiofs defaults,nofail 0 2\\n\" weights /mnt/weights >> /etc/fstab",
  ] : []

  # System pool: raise inotify instances (registry credential helpers + controllers on a shared node).
  system_cloud_init = join("\n", [
    "#cloud-config",
    "write_files:",
    " - path: /etc/sysctl.d/90-serverless2.conf",
    "   content: |",
    "     fs.inotify.max_user_instances = 8192",
    "     fs.inotify.max_user_watches = 1048576",
    "runcmd:",
    " - sysctl --system",
  ])
  snapshot_system_cloud_init = join("\n", concat(
    [local.system_cloud_init],
    var.weights_filesystem.mount_on_system ? concat(local.weights_fs_runcmd, local.snapshot_fs_runcmd) : [],
  ))

  # GPU pools: containerd reads the image cache's mirror files (hosts.toml) only with `config_path` set, which
  # the GPU node image lacks, and the first pulls of a fresh node happen before Spegel has written them.
  # Both files are written at boot so the first pull already goes through the cache (docs/IMAGES.md).
  containerd_registry_dropin = join("\n", [
    "# serverless2 image cache (docs/IMAGES.md): per-registry mirror configuration (Spegel peers, Zot cache)",
    "version = 2",
    "[plugins.\"io.containerd.grpc.v1.cri\".registry]",
    "  config_path = \"/etc/containerd/certs.d\"",
    "[plugins.\"io.containerd.grpc.v1.cri\".containerd]",
    "  discard_unpacked_layers = false",
  ])
  containerd_hosts_toml = join("\n", [
    "server = \"http://127.0.0.1:${var.image_cache.node_port}\"",
    "",
    "[host.\"http://127.0.0.1:${var.image_cache.node_port}\"]",
    "  capabilities = [\"pull\", \"resolve\"]",
  ])
  gpu_settings = { for pn, p in var.gpu_pools : pn => merge(
    var.gpu_operator ? {} : { drivers_preset = p.driver_preset },
    p.interconnect == "infiniband" ? { dra = true } : {}
  ) }
  gpu_cloud_init = join("\n", concat(
    [
      "#cloud-config",
      "write_files:",
      " - path: /etc/containerd/conf.d/serverless2-registry.toml",
      "   content: |",
    ],
    [for l in split("\n", local.containerd_registry_dropin) : "     ${l}"],
    [
      " - path: /etc/containerd/certs.d/${var.image_cache.host}/hosts.toml",
      "   content: |",
    ],
    [for l in split("\n", local.containerd_hosts_toml) : "     ${l}"],
    [
      " - path: /etc/sysctl.d/90-serverless2.conf",
      "   content: |",
      "     fs.inotify.max_user_instances = 8192",
      "     fs.inotify.max_user_watches = 1048576",
      # Unlimited memlock on the container runtime: RDMA memory registration (NCCL over InfiniBand) is bounded by
      # RLIMIT_MEMLOCK (8 MiB on the node image, measured 2026-10-08); lifted on the containerd unit, every container
      # inherits it and multi-node runs need no CAP_IPC_LOCK (docs/JOBS.md "Multi-node runs").
      " - path: /etc/systemd/system/containerd.service.d/serverless2-memlock.conf",
      "   content: |",
      "     [Service]",
      "     LimitMEMLOCK=infinity",
      "runcmd:",
      " - sysctl --system",
      " - mkdir -p /etc/containerd/certs.d",
      " - grep -q '^imports' /etc/containerd/config.toml || sed -i '1a imports = [\"/etc/containerd/conf.d/*.toml\"]' /etc/containerd/config.toml",
      " - touch /etc/containerd/certs.d/.serverless2-containerd-restarted",
      " - systemctl daemon-reload",
      " - systemctl restart containerd",
    ],
    local.weights_fs_runcmd,
  ))
}
