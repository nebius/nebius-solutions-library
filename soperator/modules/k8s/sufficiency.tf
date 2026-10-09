locals {
  ng_resources = concat(
    [
      {
        key       = module.labels.name_nodeset_system
        resource  = var.node_group_system.resource
        boot_disk = var.node_group_system.boot_disk
      },
      {
        key       = module.labels.name_nodeset_controller
        resource  = var.node_group_controller.resource
        boot_disk = var.node_group_controller.boot_disk
      }
    ],
    [
      for node_group in [var.node_group_login] : {
        key       = module.labels.name_nodeset_login
        resource  = node_group.resource
        boot_disk = node_group.boot_disk
      }
      if node_group.node_group_enabled
    ],
    [
      for worker in var.node_group_workers :
      {
        key       = module.labels.name_nodeset_worker
        resource  = worker.resource
        boot_disk = worker.boot_disk
      }
    ],
    var.node_group_accounting.enabled
    ? [{
      key       = module.labels.name_nodeset_accounting
      resource  = var.node_group_accounting.spec.resource
      boot_disk = var.node_group_accounting.spec.boot_disk
    }]
    : [],
    var.node_group_nfs.enabled
    ? [{
      key       = module.labels.name_nodeset_nfs
      resource  = var.node_group_nfs.spec.resource
      boot_disk = var.node_group_nfs.spec.boot_disk
    }]
    : []
  )
}

data "units_data_size" "boot_disk_minimal" {
  gibibytes = 128
}

resource "terraform_data" "check_resource_preset_sufficiency" {
  for_each = zipmap(range(length(local.ng_resources)), local.ng_resources)

  lifecycle {
    precondition {
      condition     = module.resources.by_platform[each.value.resource.platform][each.value.resource.preset].sufficient[each.value.key]
      error_message = "Insufficient resource preset `${each.value.resource.preset}` for `${each.value.key}` node group."
    }

    precondition {
      condition     = each.value.boot_disk.size_gibibytes >= data.units_data_size.boot_disk_minimal.gibibytes
      error_message = "Insufficient boot disk size `${each.value.boot_disk.size_gibibytes}` for `${each.value.key}` node group. It has to be at least `${data.units_data_size.boot_disk_minimal.gibibytes}` GiB."
    }
  }
}
