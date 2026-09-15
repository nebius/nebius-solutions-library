resource "nebius_mk8s_v1_node_group" "system" {
  depends_on = [
    nebius_mk8s_v1_cluster.this,
    terraform_data.before_node_groups,
    terraform_data.check_resource_preset_sufficiency,
  ]

  parent_id = nebius_mk8s_v1_cluster.this.id

  version = var.k8s_version

  name = module.labels.name_nodeset_system
  labels = merge(
    module.labels.label_nodeset_system,
    module.labels.label_workload_cpu,
    module.labels.label_jail
  )

  autoscaling = {
    min_node_count = var.node_group_system.min_size
    max_node_count = var.node_group_system.max_size
  }

  template = {
    metadata = {
      labels = merge(
        module.labels.label_nodeset_system,
        module.labels.label_workload_cpu,
        module.labels.label_jail,
      )
    }

    resources = {
      platform = var.node_group_system.resource.platform
      preset   = var.node_group_system.resource.preset
    }

    boot_disk = {
      type             = var.node_group_system.boot_disk.type
      size_bytes       = provider::units::from_gib(var.node_group_system.boot_disk.size_gibibytes)
      block_size_bytes = provider::units::from_kib(var.node_group_system.boot_disk.block_size_kibibytes)
    }

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
