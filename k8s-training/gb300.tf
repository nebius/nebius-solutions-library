resource "terraform_data" "gb300_validation" {
  count = local.gb300_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.gb300_rack_count == 1 || local.enable_gpu_cluster
      error_message = "GB300 deployments with two or more racks require infiniband_fabric for cross-rack connectivity. A single rack may omit it."
    }

    precondition {
      condition     = var.node_group_strategy != null
      error_message = "GB300 requires an explicit node_group_strategy. Use max_surge = { count = 0 } and normally max_unavailable = { count = 1 }."
    }

    precondition {
      condition = (
        try(coalesce(var.node_group_strategy.max_surge.count, -1) == 0, false) ||
        try(coalesce(var.node_group_strategy.max_surge.percent, -1) == 0, false)
      )
      error_message = "GB300 rollout strategy must set max_surge to zero so an update never requests a nineteenth rack node."
    }

    precondition {
      condition     = !var.gpu_nodes_autoscaling.enabled
      error_message = "GB300 rack node groups use fixed sizing; gpu_nodes_autoscaling must be disabled."
    }

    precondition {
      condition     = !var.gpu_nodes_preemptible
      error_message = "Production GB300 rack nodes cannot be preemptible."
    }

    precondition {
      condition     = !var.gpu_nodes_public_ips
      error_message = "GB300 nodes must not receive public IP addresses."
    }

    precondition {
      condition     = !var.custom_driver
      error_message = "GB300 uses the MK8s driverfull CUDA 13 image path; custom_driver is not supported."
    }

    precondition {
      condition     = var.mig_strategy == null && var.mig_parted_config == null
      error_message = "MIG configuration is not supported by the GB300 rack path."
    }

    precondition {
      condition     = !var.enable_gpu_kubelet_numa && var.gpu_kubelet_numa_config == null && var.gpu_kubelet_numa_preset == null
      error_message = "The existing x86 GPU NUMA presets are not validated for GB300 Grace ARM nodes."
    }

    precondition {
      condition     = !var.test_mode
      error_message = "The bundled NCCL test currently targets 8-GPU HGX nodes and cannot be enabled for GB300."
    }

    precondition {
      condition     = !var.enable_kuberay_cluster && !var.enable_kuberay_service
      error_message = "The bundled KubeRay images and resource profiles are not yet validated for GB300 Grace ARM nodes."
    }
  }
}

resource "nebius_compute_v1_nvl_instance_group" "gb300" {
  for_each = local.gb300_racks

  parent_id = var.parent_id
  name      = "${var.cluster_name}-gb300-${each.key}"
  size      = each.value.node_count
  type      = "GB300"

  depends_on = [
    terraform_data.gb300_validation,
  ]
}

resource "nebius_mk8s_v1_node_group" "gb300" {
  for_each = local.gb300_racks

  parent_id        = nebius_mk8s_v1_cluster.k8s-cluster.id
  name             = "${var.cluster_name}-ng-gb300-${each.key}"
  version          = var.k8s_version
  fixed_node_count = each.value.node_count

  labels = {
    "library-solution"                 = "k8s-training"
    "nebius.com/nvlink-instance-group" = nebius_compute_v1_nvl_instance_group.gb300[each.key].id
  }

  strategy = var.node_group_strategy

  template = {
    metadata = {
      labels = {
        "nebius.com/nvlink-instance-group" = nebius_compute_v1_nvl_instance_group.gb300[each.key].id
      }
    }

    boot_disk = {
      size_gibibytes = var.gb300.boot_disk_size_gibibytes
      type           = var.gpu_disk_type
    }

    local_disks = var.gb300.local_nvme ? {
      passthrough_group = {
        requested = true
      }
      config = {
        kubelet_ephemeral = true
      }
    } : null

    service_account_id = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[0].id : null

    network_interfaces = [{
      subnet_id = var.subnet_id
    }]

    resources = {
      platform = local.gb300_platform
      preset   = local.gb300_preset
    }

    filesystems = var.enable_filestore ? [{
      attach_mode = "READ_WRITE"
      mount_tag   = "data"
      existing_filesystem = {
        id = local.shared-filesystem.id
      }
    }] : null

    gpu_cluster  = local.enable_gpu_cluster ? nebius_compute_v1_gpu_cluster.fabric_2[0] : null
    gpu_settings = { drivers_preset = local.device_preset }
    nvlink = {
      nvl_instance_group_id = nebius_compute_v1_nvl_instance_group.gb300[each.key].id
    }

    os                = "ubuntu24.04"
    underlay_required = false
    cloud_init_user_data = templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore         = var.enable_filestore ? "true" : "false",
      filestore_mount_path     = local.filestore.mount_path,
      ssh_user_name            = var.ssh_user_name,
      ssh_public_key           = local.ssh_public_key,
      kubelet_numa_config      = null,
      kubelet_numa_config_yaml = ""
    })
  }

  depends_on = [
    terraform_data.gb300_validation,
  ]
}
