# =============================================================================
# Kubernetes Module
# =============================================================================

# -----------------------------------------------------------------------------
# Service Account for Node Groups
# -----------------------------------------------------------------------------
data "nebius_iam_v1_group" "editors" {
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "k8s_nodes" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-k8s-nodes-sa"
}

resource "nebius_iam_v1_group_membership" "k8s_nodes" {
  parent_id = data.nebius_iam_v1_group.editors.id
  member_id = nebius_iam_v1_service_account.k8s_nodes.id
}

# -----------------------------------------------------------------------------
# GPU Cluster (InfiniBand)
# -----------------------------------------------------------------------------
resource "nebius_compute_v1_gpu_cluster" "main" {
  count = var.enable_gpu_cluster && var.gpu_nodes_count_per_group > 0 ? 1 : 0

  parent_id         = var.parent_id
  name              = "${var.name_prefix}-gpu-cluster"
  infiniband_fabric = var.infiniband_fabric
}

# -----------------------------------------------------------------------------
# Managed Kubernetes Cluster
# -----------------------------------------------------------------------------
resource "nebius_mk8s_v1_cluster" "main" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-cluster"

  control_plane = {
    subnet_id         = var.subnet_id
    version           = var.k8s_version
    etcd_cluster_size = var.etcd_cluster_size

    endpoints = var.enable_public_endpoint ? {
      public_endpoint = {}
    } : {}
  }

  lifecycle {
    ignore_changes = [labels]
  }
}

# -----------------------------------------------------------------------------
# CPU Node Group
# -----------------------------------------------------------------------------
resource "nebius_mk8s_v1_node_group" "cpu" {
  parent_id        = nebius_mk8s_v1_cluster.main.id
  name             = "${var.name_prefix}-cpu-nodes"
  fixed_node_count = var.cpu_nodes_count
  version          = var.k8s_version

  labels = {
    "node-type" = "cpu"
  }

  template = {
    boot_disk = {
      size_gibibytes = var.cpu_disk_size_gib
      type           = var.cpu_disk_type
    }

    service_account_id = nebius_iam_v1_service_account.k8s_nodes.id

    network_interfaces = [
      {
        subnet_id         = var.subnet_id
        public_ip_address = var.cpu_nodes_assign_public_ip ? {} : null
      }
    ]

    resources = {
      platform = var.cpu_nodes_platform
      preset   = var.cpu_nodes_preset
    }

    filesystems = var.enable_filestore && var.filestore_id != null ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id = var.filestore_id
        }
      }
    ] : null

    cloud_init_user_data = templatefile("${path.module}/templates/cloud-init.yaml", {
      ssh_user_name    = var.ssh_user_name
      ssh_public_key   = var.ssh_public_key
      enable_filestore = var.enable_filestore
    })
  }
}

# -----------------------------------------------------------------------------
# GPU Node Groups
# -----------------------------------------------------------------------------
resource "nebius_mk8s_v1_node_group" "gpu" {
  count = var.gpu_nodes_count_per_group > 0 ? var.gpu_node_groups : 0

  parent_id        = nebius_mk8s_v1_cluster.main.id
  name             = "${var.name_prefix}-gpu-nodes-${count.index}"
  fixed_node_count = var.gpu_nodes_count_per_group
  version          = var.k8s_version

  labels = {
    "node-type" = "gpu"
  }

  template = {
    boot_disk = {
      size_gibibytes = var.gpu_disk_size_gib
      type           = var.gpu_disk_type
    }

    service_account_id = nebius_iam_v1_service_account.k8s_nodes.id

    network_interfaces = [
      {
        subnet_id         = var.subnet_id
        public_ip_address = var.gpu_nodes_assign_public_ip ? {} : null
      }
    ]

    resources = {
      platform = var.gpu_nodes_platform
      preset   = var.gpu_nodes_preset
    }

    # GPU cluster for InfiniBand
    gpu_cluster = var.enable_gpu_cluster ? nebius_compute_v1_gpu_cluster.main[0] : null

    # Driverfull images (pre-installed NVIDIA drivers, no GPU Operator driver needed)
    gpu_settings = var.gpu_nodes_driverfull_image ? { drivers_preset = var.gpu_drivers_preset } : null

    # Reservation policy for capacity block groups
    reservation_policy = length(var.gpu_reservation_ids) > 0 ? {
      policy          = "STRICT"
      reservation_ids = var.gpu_reservation_ids
    } : null

    # Preemptible configuration
    preemptible = var.gpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 3
    } : null

    # Taints for GPU nodes
    taints = var.enable_gpu_taints ? [
      {
        key    = "nvidia.com/gpu"
        value  = "true"
        effect = "NO_SCHEDULE"
      }
    ] : null

    filesystems = var.enable_filestore && var.filestore_id != null ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id = var.filestore_id
        }
      }
    ] : null

    cloud_init_user_data = templatefile("${path.module}/templates/cloud-init.yaml", {
      ssh_user_name    = var.ssh_user_name
      ssh_public_key   = var.ssh_public_key
      enable_filestore = var.enable_filestore
    })
  }
}
