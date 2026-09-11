# --- Node service account ---
resource "nebius_iam_v1_service_account" "ncr_nodes" {
  parent_id   = var.project_id
  name        = "${var.cluster_name}-ncr-nodes"
  description = "Node-group SA for pulling from Nebius Container Registry"

  depends_on = [
    terraform_data.validate_platforms,
    terraform_data.validate_infiniband,
    terraform_data.validate_no_duplicates,
  ]
}

# --- Lookup the built-in 'viewers' group ---
data "nebius_iam_v1_group" "viewers" {
  id = var.viewers_group_id
}

# --- Add the SA to the viewers group ---
resource "nebius_iam_v1_group_membership" "sa_in_viewers" {
  parent_id = data.nebius_iam_v1_group.viewers.id
  member_id = nebius_iam_v1_service_account.ncr_nodes.id
}

############################
# Cluster
############################

resource "nebius_mk8s_v1_cluster" "saturn_cluster" {
  name      = var.cluster_name
  parent_id = var.project_id

  control_plane = {
    subnet_id = var.subnet_id
    endpoints = {
      public_endpoint = {}
    }
    etcd_cluster_size = 1
    version           = var.k8s_version
  }

  depends_on = [
    terraform_data.validate_platforms,
    terraform_data.validate_infiniband,
    terraform_data.validate_no_duplicates,
  ]
}

############################
# GPU Clusters with InfiniBand
############################

resource "nebius_compute_v1_gpu_cluster" "gpu_clusters" {
  for_each = local.infiniband_fabrics

  name              = "${var.cluster_name}-${each.key}"
  parent_id         = var.project_id
  infiniband_fabric = each.value.infiniband_fabric

  depends_on = [
    terraform_data.validate_platforms,
    terraform_data.validate_infiniband,
    terraform_data.validate_no_duplicates,
  ]
}

############################
# System node group (always created)
############################

resource "nebius_mk8s_v1_node_group" "system_nodes" {
  parent_id = nebius_mk8s_v1_cluster.saturn_cluster.id
  name      = "system_node"

  labels = {
    "k8s.io/cluster-autoscaler/node-template/label/node.saturncloud.io/role" = "system"
  }

  template = {
    service_account_id = nebius_iam_v1_service_account.ncr_nodes.id

    metadata = {
      labels = {
        "node.saturncloud.io/role" = "system"
      }
    }
    boot_disk = {
      size_gibibytes = 93
      type           = "NETWORK_SSD_NON_REPLICATED"
    }
    resources = {
      platform = "cpu-d3"
      preset   = "4vcpu-16gb"
    }
    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = local.filestore.mount_tag
        existing_filesystem = {
          id = local.shared_filesystem.id
        }
      }
    ] : null
    cloud_init_user_data = var.enable_filestore ? templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore     = "true"
      filestore_mount_path = local.filestore.mount_path
      ssh_user_name        = "ubuntu"
      ssh_public_key       = local.ssh_public_key
    }) : null
  }

  autoscaling = {
    min_node_count = 2
    max_node_count = 100
  }
}

############################
# Dynamic node groups from var.node_pools
############################

resource "nebius_mk8s_v1_node_group" "pool" {
  for_each = local.node_pool_keys

  parent_id = nebius_mk8s_v1_cluster.saturn_cluster.id
  name      = each.key

  # Scale-from-zero hints for the cluster autoscaler. Saturn schedules workloads against
  # Nebius-native labels (node.kubernetes.io/instance-type + nebius.com/resource-preset),
  # so the autoscaler must know a not-yet-created node of this group will carry them, or
  # pending pods won't trigger a scale-up.
  labels = {
    "k8s.io/cluster-autoscaler/node-template/label/node.kubernetes.io/instance-type" = each.value.platform
    "k8s.io/cluster-autoscaler/node-template/label/nebius.com/resource-preset"        = each.value.preset
  }

  template = {
    service_account_id = nebius_iam_v1_service_account.ncr_nodes.id

    metadata = {
      # node.kubernetes.io/instance-type and nebius.com/resource-preset are stamped by
      # Nebius natively; we only add the GPU count label Saturn relies on.
      labels = local.valid_presets["${each.value.platform}/${each.value.preset}"].gpus > 0 ? {
        "nvidia.com/gpu" = tostring(local.valid_presets["${each.value.platform}/${each.value.preset}"].gpus)
      } : {}
    }

    boot_disk = {
      size_gibibytes = each.value.boot_disk_gb
      type           = "NETWORK_SSD_NON_REPLICATED"
    }

    resources = {
      platform = each.value.platform
      preset   = each.value.preset
    }

    gpu_settings = local.valid_presets["${each.value.platform}/${each.value.preset}"].gpus > 0 ? {
      drivers_preset = coalesce(each.value.drivers_preset, local.default_cuda_preset)
    } : null

    gpu_cluster = each.value.infiniband_fabric != null ? {
      id = nebius_compute_v1_gpu_cluster.gpu_clusters["${each.value.platform}-${each.value.infiniband_fabric}"].id
    } : null

    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = local.filestore.mount_tag
        existing_filesystem = {
          id = local.shared_filesystem.id
        }
      }
    ] : null
    cloud_init_user_data = var.enable_filestore ? templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore     = "true"
      filestore_mount_path = local.filestore.mount_path
      ssh_user_name        = "ubuntu"
      ssh_public_key       = local.ssh_public_key
    }) : null
  }

  autoscaling = {
    min_node_count = each.value.min_nodes
    max_node_count = each.value.max_nodes
  }
}
