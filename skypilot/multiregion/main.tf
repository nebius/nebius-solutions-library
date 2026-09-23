resource "nebius_mk8s_v1_cluster" "k8s-cluster" {
  for_each  = local.cluster_config
  parent_id = each.value.parent_id
  name      = each.value.name
  control_plane = {
    endpoints = {
      public_endpoint = var.mk8s_cluster_public_endpoint ? {} : null
    }
    etcd_cluster_size = var.etcd_cluster_size
    subnet_id         = each.value.subnet_id
    version           = var.k8s_version
  }
}

module "cilium-egress-gateway" {
  count  = var.enable_egress_gateway ? 1 : 0
  source = "../../modules/cilium-egress-gateway"

  mk8s_cluster_id = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].id
  mk8s_version    = var.k8s_version
  project_id      = local.primary_parent_id
  ssh_user_name   = var.ssh_user_name
  ssh_public_key  = local.ssh_public_key
  subnet_id       = local.primary_subnet_id

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only
  ]
}

data "nebius_iam_v1_group" "editors" {
  count     = var.enable_k8s_node_group_sa ? 1 : 0
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "k8s_node_group_sa" {
  for_each  = var.enable_k8s_node_group_sa ? local.cluster_config : {}
  parent_id = each.value.parent_id
  name      = local.multi_region ? "${var.cluster_name}-${each.value.region}-k8s-node-group-sa" : "${var.cluster_name}-k8s-node-group-sa"
}

resource "nebius_iam_v1_group_membership" "k8s_node_group_sa-admin" {
  for_each  = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa : {}
  parent_id = data.nebius_iam_v1_group.editors[0].id
  member_id = each.value.id
}

################
# CPU NODE GROUP
################
resource "nebius_mk8s_v1_node_group" "cpu-only" {
  for_each         = local.cluster_config
  fixed_node_count = var.cpu_nodes_count
  parent_id        = nebius_mk8s_v1_cluster.k8s-cluster[each.key].id
  name             = local.multi_region ? "${var.cluster_name}-${each.value.region}-ng-cpu" : "${var.cluster_name}-ng-cpu"
  labels = {
    "library-solution" : "infra",
  }
  version = var.k8s_version
  template = {
    boot_disk = {
      size_gibibytes = var.cpu_disk_size
      type           = var.cpu_disk_type
    }

    service_account_id = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[each.key].id : null

    network_interfaces = [
      {
        public_ip_address = var.cpu_nodes_public_ips ? {} : null
        subnet_id         = each.value.subnet_id
      }
    ]
    resources = {
      platform = each.value.cpu_nodes_platform
      preset   = each.value.cpu_nodes_preset
    }
    preemptible = var.cpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 3
    } : null
    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id   = local.shared-filesystem[each.key].id
          size = local.shared-filesystem[each.key].size_gibibytes
        }
      }
    ] : null
    underlay_required = false
    cloud_init_user_data = templatefile("${path.module}/../../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore         = var.enable_filestore ? "true" : "false",
      filestore_mount_path     = local.filestore.mount_path,
      ssh_user_name            = var.ssh_user_name,
      ssh_public_key           = local.ssh_public_key,
      kubelet_numa_config      = null,
      kubelet_numa_config_yaml = ""
    })
  }
}

#################
# GPU NODE GROUPS
#################
resource "nebius_mk8s_v1_node_group" "gpu" {
  for_each         = local.gpu_node_group_config
  fixed_node_count = var.gpu_nodes_count_per_group
  parent_id        = nebius_mk8s_v1_cluster.k8s-cluster[each.value.cluster_key].id
  name             = local.multi_region ? "${var.cluster_name}-${each.value.cluster.region}-ng-gpu-${each.value.group_index}" : "${var.cluster_name}-ng-gpu-${each.value.group_index}"
  labels = {
    "library-solution" : "infra",
  }
  version = var.k8s_version
  template = {
    metadata = {
      labels = var.mig_parted_config != null ? {
        "nvidia.com/mig.config" = var.mig_parted_config
      } : {}
    }

    boot_disk = {
      size_gibibytes = var.gpu_disk_size
      type           = var.gpu_disk_type
    }

    service_account_id = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[each.value.cluster_key].id : null

    network_interfaces = [
      {
        subnet_id         = each.value.cluster.subnet_id
        public_ip_address = var.gpu_nodes_public_ips ? {} : null
      }
    ]
    resources = {
      platform = each.value.cluster.gpu_nodes_platform
      preset   = each.value.cluster.gpu_nodes_preset
    }
    preemptible = var.gpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 3
    } : null
    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id = local.shared-filesystem[each.value.cluster_key].id
        }
      }
    ] : null
    gpu_cluster  = var.enable_gpu_cluster ? nebius_compute_v1_gpu_cluster.fabric_2[each.value.cluster_key] : null
    gpu_settings = var.gpu_nodes_driverfull_image ? { drivers_preset = lookup(local.platform_to_cuda, each.value.cluster.gpu_nodes_platform, "cuda13.0") } : null

    underlay_required = false
    cloud_init_user_data = templatefile("${path.module}/../../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore         = var.enable_filestore ? "true" : "false",
      filestore_mount_path     = local.filestore.mount_path,
      ssh_user_name            = var.ssh_user_name,
      ssh_public_key           = local.ssh_public_key,
      kubelet_numa_config      = null,
      kubelet_numa_config_yaml = ""
    })
  }
}
