resource "nebius_mk8s_v1_node_group" "controller" {
  depends_on = [
    nebius_mk8s_v1_cluster.this,
    nebius_mk8s_v1_node_group.system.this,
    terraform_data.check_resource_preset_sufficiency,
  ]

  parent_id = nebius_mk8s_v1_cluster.this.id

  version = var.k8s_version

  name = module.labels.name_nodeset_controller
  labels = merge(
    module.labels.label_nodeset_controller,
    module.labels.label_workload_cpu,
    module.labels.label_jail,
  )

  fixed_node_count = var.node_group_controller.size

  template = {
    metadata = {
      labels = merge(
        module.labels.label_nodeset_controller,
        module.labels.label_workload_cpu,
        module.labels.label_jail,
        module.labels.label_exclude_from_external_lb,
      )
    }
    taints = [{
      key    = module.labels.key_slurm_nodeset_name,
      value  = module.labels.name_nodeset_controller
      effect = "NO_SCHEDULE"
    }]

    resources = {
      platform = var.node_group_controller.resource.platform
      preset   = var.node_group_controller.resource.preset
    }

    boot_disk = {
      type             = var.node_group_controller.boot_disk.type
      size_bytes       = provider::units::from_gib(var.node_group_controller.boot_disk.size_gibibytes)
      block_size_bytes = provider::units::from_kib(var.node_group_controller.boot_disk.block_size_kibibytes)
    }

    filesystems = concat(
      [
        {
          attach_mode = "READ_WRITE"
          mount_tag   = var.filestores.controller_spool.mount_tag
          existing_filesystem = {
            id = var.filestores.controller_spool.id
          }
        },
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
      ],
      var.filestores.accounting != null
      ? [
        {
          attach_mode = "READ_WRITE"
          mount_tag   = var.filestores.accounting.mount_tag
          existing_filesystem = {
            id = var.filestores.accounting.id
          }
        }
      ]
      : []
    )

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
