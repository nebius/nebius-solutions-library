resource "nebius_mk8s_v1_node_group" "nfs" {
  count = var.node_group_nfs.enabled ? 1 : 0

  depends_on = [
    nebius_mk8s_v1_cluster.this,
    nebius_mk8s_v1_node_group.system.this,
    terraform_data.check_resource_preset_sufficiency,
  ]

  parent_id = nebius_mk8s_v1_cluster.this.id

  version = var.k8s_version

  name = module.labels.name_nodeset_nfs
  labels = merge(
    module.labels.label_nodeset_nfs,
    module.labels.label_workload_cpu,
  )

  fixed_node_count = var.node_group_nfs.spec.size

  template = {
    metadata = {
      labels = merge(
        module.labels.label_nodeset_nfs,
        module.labels.label_workload_cpu,
        module.labels.label_exclude_from_external_lb,
      )
    }
    taints = [{
      key    = module.labels.key_slurm_nodeset_name,
      value  = module.labels.name_nodeset_nfs
      effect = "NO_SCHEDULE"
    }]

    resources = {
      platform = var.node_group_nfs.spec.resource.platform
      preset   = var.node_group_nfs.spec.resource.preset
    }

    boot_disk = {
      type             = var.node_group_nfs.spec.boot_disk.type
      size_bytes       = provider::units::from_gib(var.node_group_nfs.spec.boot_disk.size_gibibytes)
      block_size_bytes = provider::units::from_kib(var.node_group_nfs.spec.boot_disk.block_size_kibibytes)
    }

    network_interfaces = [{
      public_ip_address = local.node_ssh_access_public_ip.enabled ? {} : null
      subnet_id         = var.vpc_subnet_id
    }]

    os = "ubuntu24.04"

    cloud_init_user_data = local.node_ssh_access.enabled ? local.node_cloud_init.cloud_init_data_no_nvidia : null
  }

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}
