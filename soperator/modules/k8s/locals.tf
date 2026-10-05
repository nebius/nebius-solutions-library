locals {
  node_ssh_access = {
    enabled = length(var.node_ssh_access_users) > 0
  }

  node_ssh_access_public_ip = {
    enabled = local.node_ssh_access.enabled && var.node_ssh_access_public_ip
  }

  node_cloud_init = {
    enabled = local.node_ssh_access.enabled || var.use_default_apparmor_profile
    cloud_init_data_no_nvidia = templatefile("${path.module}/templates/cloud_init.yaml.tftpl", {
      ssh_users                    = var.node_ssh_access_users
      use_default_apparmor_profile = var.use_default_apparmor_profile
      nvidia_config_lines          = []
    })
  }

  node_group_gpu_present = {
    worker = [
      for worker in var.node_group_workers :
      (module.resources.by_platform[worker.resource.platform][worker.resource.preset].gpus > 0 ? true : false)
    ]
  }

  node_group_gpu_cluster_compatible = {
    worker = [for worker in var.node_group_workers :
      module.resources.by_platform[worker.resource.platform][worker.resource.preset].gpu_cluster_compatible
    ]
  }

  node_group_workload_label = {
    worker = [for worker in local.node_group_gpu_present.worker :
      (worker ? module.labels.label_workload_gpu : module.labels.label_workload_cpu)
    ]
  }

  # V2 workers (for nodesets)
  node_group_gpu_present_v2 = {
    worker = [
      for worker in var.node_group_workers_v2 :
      (module.resources.by_platform[worker.resource.platform][worker.resource.preset].gpus > 0 ? true : false)
    ]
  }

  node_group_gpu_cluster_compatible_v2 = {
    worker = [for worker in var.node_group_workers_v2 :
      module.resources.by_platform[worker.resource.platform][worker.resource.preset].gpu_cluster_compatible
    ]
  }

  node_group_workload_label_v2 = {
    worker = [for worker in local.node_group_gpu_present_v2.worker :
      (worker ? module.labels.label_workload_gpu : module.labels.label_workload_cpu)
    ]
  }

  # Normalize optional NVLink IDs to strings. "" means the worker should not get
  # an nvlink block or NVLink label.
  node_group_nvl_instance_group_id_v2 = {
    worker = [
      for worker in var.node_group_workers_v2 :
      try(trimspace(worker.nvl_instance_group_id), "")
    ]
  }

  # Convert NVLink IDs into label maps that can be merged unconditionally.
  # Example: ["nvl-1", ""] becomes
  # [{ "nebius.com/nvlink-instance-group" = "nvl-1" }, {}].
  node_group_nvl_instance_group_label_v2 = {
    worker = [
      for nvl_instance_group_id in local.node_group_nvl_instance_group_id_v2.worker :
      nvl_instance_group_id != "" ? tomap({
        (module.labels.key_nebius_nvlink_instance_group) = nvl_instance_group_id
      }) : tomap({})
    ]
  }

  # Normalize optional placement-policy node lists. [] means the provider
  # placement_policy block should be omitted for that worker.
  node_group_placement_policy_nodes_v2 = {
    worker = [
      for worker in var.node_group_workers_v2 :
      coalesce(try(worker.placement_policy_nodes, null), [])
    ]
  }

  context_name = join(
    "-",
    [
      "nebius",
      replace(lower(var.company_name), " ", "-"),
      "slurm"
    ]
  )
}
