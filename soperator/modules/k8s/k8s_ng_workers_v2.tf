locals {
  # GPU clusters for v2 worker nodes
  gpu_clusters_v2 = {
    for gpu_placement in distinct([for worker in var.node_group_workers_v2 :
      {
        fabric = try(trimspace(worker.gpu_cluster.infiniband_fabric), "")
      }
      if worker.gpu_cluster != null && try(trimspace(worker.gpu_cluster.id), "") == "" && try(trimspace(worker.gpu_cluster.infiniband_fabric), "") != ""
    ]) :
    gpu_placement.fabric => {
      fabric = gpu_placement.fabric
    }
  }

  gpu_clusters_by_nodegroup = {
    for ng in distinct([for worker in var.node_group_workers_v2 :
      {
        name   = worker.name
        fabric = try(trimspace(worker.gpu_cluster.infiniband_fabric), "")
      }
      if worker.gpu_cluster != null && try(trimspace(worker.gpu_cluster.id), "") == "" && try(trimspace(worker.gpu_cluster.infiniband_fabric), "") != ""
    ]) :
    ng.name => ng.fabric
  }

  node_group_gpu_cluster_id_v2 = {
    worker = [
      for worker in var.node_group_workers_v2 :
      try(trimspace(worker.gpu_cluster.id), "")
    ]
  }

  node_group_gpu_cluster_fabric_v2 = {
    worker = [
      for worker in var.node_group_workers_v2 :
      try(trimspace(worker.gpu_cluster.infiniband_fabric), "")
    ]
  }
}

resource "nebius_compute_v1_gpu_cluster" "this_v2" {
  for_each = local.gpu_clusters_v2

  parent_id = var.iam_project_id

  name = "${var.name}-${each.value.fabric}"

  infiniband_fabric = each.value.fabric

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}

resource "nebius_mk8s_v1_node_group" "worker_v2" {
  count = length(var.node_group_workers_v2)

  depends_on = [
    nebius_mk8s_v1_cluster.this,
    nebius_mk8s_v1_node_group.system.this,
    nebius_compute_v1_gpu_cluster.this_v2,
    terraform_data.check_resource_preset_sufficiency,
  ]

  parent_id = nebius_mk8s_v1_cluster.this.id

  version = "${var.k8s_version}-nebius-node.${var.node_group_version}"

  # Prefer the generated node_group_name from the installation layer. Fall back
  # to the historical <nodeset>-<subset> name for callers that do not provide it.
  name = coalesce(
    try(var.node_group_workers_v2[count.index].node_group_name, null),
    join("-", [
      var.node_group_workers_v2[count.index].name,
      var.node_group_workers_v2[count.index].subset_index,
    ])
  )
  labels = merge(
    tomap({
      (module.labels.key_slurm_nodeset_name) = var.node_group_workers_v2[count.index].name
    }),
    local.node_group_workload_label_v2.worker[count.index],
    local.node_group_nvl_instance_group_label_v2.worker[count.index],
    module.labels.label_jail,
  )

  autoscaling = var.node_group_workers_v2[count.index].autoscaling ? {
    min_node_count = var.node_group_workers_v2[count.index].min_size
    max_node_count = var.node_group_workers_v2[count.index].max_size
  } : null

  fixed_node_count = var.node_group_workers_v2[count.index].autoscaling ? null : var.node_group_workers_v2[count.index].size

  auto_repair = {
    conditions = [
      # Don't recreate the node if it's not ready for 5 minutes
      # to avoid races with Soperator, since it does the same
      {
        type     = "NodeReady"
        status   = "FALSE"
        disabled = true
      },
      # Don't restart nodes with not responding kubelet
      # to avoid races with Soperator, since it does the same
      {
        type     = "NodeReady"
        status   = "UNKNOWN"
        disabled = true
      },
      # Don't recreate nodes with broken boot disks
      # since it's covered by NodeReady=Unknown
      {
        type     = "NebiusBootDiskIOError"
        status   = "TRUE"
        disabled = true
      },
      # Don't set-unhealthy and restart nodes with failed Mk8s health checks
      # to avoid races with Soperator, since it has its own health checks
      {
        type     = "NebiusGPUError"
        status   = "TRUE"
        disabled = true
      },
      # Don't restart nodes with broken containerd
      # since it's covered by NodeReady=False
      {
        type     = "NebiusContainerRuntimeError"
        status   = "TRUE"
        disabled = true
      },
      # Set-unhealthy and recreate nodes marked as unhealthy by Soperator
      {
        type    = "HardwareIssuesSuspected"
        status  = "TRUE"
        timeout = "1s"
      },
    ]
  }

  strategy = {
    max_unavailable = {
      percent = 50
    }
    max_surge = {
      percent = 0
    }
    drain_timeout = null
  }

  template = {
    metadata = {
      labels = merge(
        var.node_group_workers_v2[count.index].extra_labels,
        module.labels.label_jail,
        module.labels.label_nodeset_worker,
        tomap({
          (module.labels.key_slurm_nodeset_name_name) = var.node_group_workers_v2[count.index].name
        }),
        local.node_group_workload_label_v2.worker[count.index],
        (local.node_group_gpu_present_v2.worker[count.index] ? module.labels.label_nebius_gpu : {}),
        local.node_group_nvl_instance_group_label_v2.worker[count.index],
        module.labels.label_exclude_from_external_lb,
      )
    }
    taints = local.node_group_gpu_present_v2.worker[count.index] ? [{
      key    = module.labels.key_nvidia_gpu,
      value  = module.resources.by_platform[var.node_group_workers_v2[count.index].resource.platform][var.node_group_workers_v2[count.index].resource.preset].gpus
      effect = "NO_SCHEDULE"
    }] : null

    resources = {
      platform = var.node_group_workers_v2[count.index].resource.platform
      preset   = var.node_group_workers_v2[count.index].resource.preset
    }
    gpu_cluster = (local.node_group_gpu_cluster_compatible_v2.worker[count.index]
      ? (var.node_group_workers_v2[count.index].gpu_cluster != null && (local.node_group_gpu_cluster_id_v2.worker[count.index] != "" || local.node_group_gpu_cluster_fabric_v2.worker[count.index] != "")
        ? {
          id = (local.node_group_gpu_cluster_id_v2.worker[count.index] != ""
            ? local.node_group_gpu_cluster_id_v2.worker[count.index]
            : nebius_compute_v1_gpu_cluster.this_v2[local.gpu_clusters_by_nodegroup[var.node_group_workers_v2[count.index].name]].id
          )
        }
        : null
      )
      : null
    )

    preemptible = var.node_group_workers_v2[count.index].preemptible

    reservation_policy = var.node_group_workers_v2[count.index].reservation_policy

    max_pods = var.node_group_workers_v2[count.index].max_pods

    gpu_settings = (var.use_preinstalled_gpu_drivers && local.node_group_gpu_present_v2.worker[count.index]) ? {
      drivers_preset = lookup(var.platform_driver_presets, var.node_group_workers_v2[count.index].resource.platform)
    } : null

    boot_disk = {
      type             = var.node_group_workers_v2[count.index].boot_disk.type
      size_bytes       = provider::units::from_gib(var.node_group_workers_v2[count.index].boot_disk.size_gibibytes)
      block_size_bytes = provider::units::from_kib(var.node_group_workers_v2[count.index].boot_disk.block_size_kibibytes)
    }

    local_disks = try(var.node_group_workers_v2[count.index].local_nvme.enabled, false) ? {
      config = {
        kubelet_ephemeral = true
      }
      passthrough_group = {
        requested = true
      }
    } : null

    filesystems = concat(
      [
        {
          attach_mode = "READ_WRITE"
          mount_tag   = var.filestores.jail.mount_tag
          existing_filesystem = {
            id = var.filestores.jail.id
          }
        }
      ],
      [
        for submount in var.filestores.jail_submounts :
        {
          attach_mode = "READ_WRITE"
          mount_tag   = submount.mount_tag
          existing_filesystem = {
            id = submount.id
          }
        }
      ]
    )

    # Omit optional provider blocks when the normalized sentinels are empty.
    # Example: nvl_instance_group_id = "" gives nvlink = null; placement nodes
    # ["node-a", "node-b"] gives placement_policy.nodes = ["node-a", "node-b"].
    nvlink = local.node_group_nvl_instance_group_id_v2.worker[count.index] != "" ? {
      nvl_instance_group_id = local.node_group_nvl_instance_group_id_v2.worker[count.index]
    } : null
    placement_policy = length(local.node_group_placement_policy_nodes_v2.worker[count.index]) > 0 ? {
      nodes = local.node_group_placement_policy_nodes_v2.worker[count.index]
    } : null

    network_interfaces = [{
      public_ip_address = local.node_ssh_access_public_ip.enabled ? {} : null
      subnet_id         = var.vpc_subnet_id
    }]

    os = "ubuntu24.04"

    cloud_init_user_data = (
      local.node_ssh_access.enabled ||
      (local.node_group_gpu_present_v2.worker[count.index] && length(var.nvidia_config_lines) > 0)
      ) ? templatefile("${path.module}/templates/cloud_init.yaml.tftpl", {
        ssh_users           = var.node_ssh_access_users
        nvidia_config_lines = local.node_group_gpu_present_v2.worker[count.index] ? var.nvidia_config_lines : []
    }) : null
  }

  lifecycle {
    ignore_changes = [
      labels,
    ]

    precondition {
      condition = (var.node_group_workers_v2[count.index].resource.platform == "cpu-e2"
        ? !contains(["2vcpu-8gb", "4vcpu-16gb"], var.node_group_workers_v2[count.index].resource.preset)
        : true
      )
      error_message = "Worker[${count.index}] resource preset '${var.node_group_workers_v2[count.index].resource.preset}' is insufficient."
    }
  }
}
