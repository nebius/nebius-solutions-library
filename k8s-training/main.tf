resource "nebius_mk8s_v1_cluster" "k8s-cluster" {
  parent_id = var.parent_id
  name      = var.cluster_name
  control_plane = {
    endpoints = {
      public_endpoint = var.mk8s_cluster_public_endpoint ? {} : null
    }
    etcd_cluster_size = var.etcd_cluster_size
    subnet_id         = var.subnet_id
    version           = var.k8s_version
  }
}

module "cilium-egress-gateway" {
  count  = var.enable_egress_gateway ? 1 : 0
  source = "../modules/cilium-egress-gateway"

  mk8s_cluster_id = resource.nebius_mk8s_v1_cluster.k8s-cluster.id
  mk8s_version    = var.k8s_version
  project_id      = var.parent_id
  ssh_user_name   = var.ssh_user_name
  ssh_public_key  = local.ssh_public_key
  subnet_id       = var.subnet_id

  depends_on = [
    resource.nebius_mk8s_v1_node_group.cpu-only
  ]
}

data "nebius_iam_v1_group" "editors" {
  count     = var.enable_k8s_node_group_sa ? 1 : 0
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "k8s_node_group_sa" {
  count     = var.enable_k8s_node_group_sa ? 1 : 0
  parent_id = var.parent_id
  name      = "${var.cluster_name}-k8s-node-group-sa"
}

resource "nebius_iam_v1_group_membership" "k8s_node_group_sa-admin" {
  count     = var.enable_k8s_node_group_sa ? 1 : 0
  parent_id = data.nebius_iam_v1_group.editors[0].id
  member_id = nebius_iam_v1_service_account.k8s_node_group_sa[count.index].id
}
################
# CPU NODE GROUP
################
resource "nebius_mk8s_v1_node_group" "cpu-only" {

  autoscaling = var.cpu_nodes_autoscaling.enabled ? {
    min_node_count = var.cpu_nodes_autoscaling.min_size == null ? var.cpu_nodes_autoscaling.max_size : var.cpu_nodes_autoscaling.min_size
    max_node_count = var.cpu_nodes_autoscaling.max_size
  } : null

  fixed_node_count = var.cpu_nodes_autoscaling.enabled ? null : var.cpu_nodes_fixed_count


  parent_id = nebius_mk8s_v1_cluster.k8s-cluster.id
  name      = "${var.cluster_name}-ng-cpu"
  labels = {
    "library-solution" : "k8s-training",
  }
  version = var.k8s_version
  template = {
    boot_disk = {
      size_gibibytes = var.cpu_disk_size
      type           = var.cpu_disk_type
    }

    service_account_id = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[0].id : null

    network_interfaces = [
      {
        public_ip_address = var.cpu_nodes_public_ips ? {} : null
        subnet_id         = var.subnet_id
      }
    ]
    resources = {
      platform = local.cpu_nodes_platform
      preset   = local.cpu_nodes_preset
    }
    preemptible = var.cpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 1
    } : null
    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id   = local.shared-filesystem.id
          size = local.shared-filesystem.size_gibibytes
        }

      }
    ] : null
    underlay_required = false
    preemptible = var.cpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 3
    } : null
    cloud_init_user_data = templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore     = var.enable_filestore ? "true" : "false",
      filestore_mount_path = local.filestore.mount_path,
      ssh_user_name        = var.ssh_user_name,
      ssh_public_key       = local.ssh_public_key
    })
  }
}
#################
# GPU NODE GROUPS
#################
resource "nebius_mk8s_v1_node_group" "gpu" {
  count = var.gpu_node_groups

  autoscaling = var.gpu_nodes_autoscaling.enabled ? {
    min_node_count = var.gpu_nodes_autoscaling.min_size == null ? var.gpu_nodes_autoscaling.max_size : var.gpu_nodes_autoscaling.min_size
    max_node_count = var.gpu_nodes_autoscaling.max_size
  } : null

  fixed_node_count = var.gpu_nodes_autoscaling.enabled ? null : var.gpu_nodes_fixed_count_per_group

  parent_id = nebius_mk8s_v1_cluster.k8s-cluster.id
  name      = "${var.cluster_name}-ng-gpu-${count.index}"
  labels = {
    "library-solution" : "k8s-training",
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

    service_account_id = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[0].id : null

    network_interfaces = [
      {
        subnet_id         = var.subnet_id
        public_ip_address = var.gpu_nodes_public_ips ? {} : null
      }
    ]
    resources = {
      platform = local.gpu_nodes_platform
      preset   = local.gpu_nodes_preset
    }
    preemptible = var.gpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 1
    } : null
    filesystems = var.enable_filestore ? [
      {
        attach_mode = "READ_WRITE"
        mount_tag   = "data"
        existing_filesystem = {
          id = local.shared-filesystem.id
        }
      }
    ] : null
    gpu_cluster  = var.enable_gpu_cluster ? nebius_compute_v1_gpu_cluster.fabric_2[0] : null
    gpu_settings = var.gpu_nodes_driverfull_image ? { drivers_preset = local.device_preset } : null
    preemptible = var.gpu_nodes_preemptible ? {
      on_preemption = "STOP"
      priority      = 3
    } : null

    local_disks = var.gpu_enable_local_disks ? {
      config = {
        kubelet_ephemeral = true
      }
      passthrough_group = {
        requested = true
      }
    } : null

    underlay_required = false
    cloud_init_user_data = templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore     = var.enable_filestore ? "true" : "false",
      filestore_mount_path = local.filestore.mount_path,
      ssh_user_name        = var.ssh_user_name,
      ssh_public_key       = local.ssh_public_key
    })
  }
}
