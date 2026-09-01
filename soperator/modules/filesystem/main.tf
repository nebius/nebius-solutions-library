resource "nebius_compute_v1_filesystem" "controller_spool" {
  count = var.controller_spool.spec != null ? 1 : 0

  parent_id = var.iam_project_id

  name = local.name.filesystem.controller_spool

  type             = module.resources.shared_filesystem_types.network_ssd
  size_bytes       = provider::units::from_gib(var.controller_spool.spec.size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.controller_spool.spec.block_size_kibibytes)
  forbid_deletion  = var.controller_spool.spec.forbid_deletion

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}
data "nebius_compute_v1_filesystem" "controller_spool" {
  count = var.controller_spool.existing != null ? 1 : 0

  id = var.controller_spool.existing.id
}
locals {
  controller_spool = {
    id = try(
      one(nebius_compute_v1_filesystem.controller_spool).id,
      one(data.nebius_compute_v1_filesystem.controller_spool).id,
    )
    size_gibibytes = floor(provider::units::to_gib(try(
      one(nebius_compute_v1_filesystem.controller_spool).status.size_bytes,
      one(data.nebius_compute_v1_filesystem.controller_spool).status.size_bytes,
    )))
    mount_tag = local.const.filesystem.controller_spool
  }
}

resource "nebius_compute_v1_filesystem" "jail" {
  count = var.jail.spec != null ? 1 : 0

  depends_on = [
    terraform_data.check_weka_count,
  ]

  parent_id = var.iam_project_id

  name = local.name.filesystem.jail

  type             = var.jail.spec.type
  size_bytes       = provider::units::from_gib(var.jail.spec.size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.jail.spec.block_size_kibibytes)
  forbid_deletion  = var.jail.spec.forbid_deletion

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}
data "nebius_compute_v1_filesystem" "jail" {
  count = var.jail.existing != null ? 1 : 0

  id = var.jail.existing.id
}
locals {
  jail = {
    id = try(
      one(nebius_compute_v1_filesystem.jail).id,
      one(data.nebius_compute_v1_filesystem.jail).id,
    )
    size_gibibytes = floor(provider::units::to_gib(try(
      one(nebius_compute_v1_filesystem.jail).status.size_bytes,
      one(data.nebius_compute_v1_filesystem.jail).status.size_bytes,
    )))
    mount_tag = local.const.filesystem.jail
    backend = try(
      one(nebius_compute_v1_filesystem.jail).type,
      one(data.nebius_compute_v1_filesystem.jail).type,
    )
  }
}

resource "nebius_compute_v1_filesystem" "jail_submount" {
  for_each = tomap({ for submount in var.jail_submounts :
    submount.name => {
      name            = local.name.jail_submount[submount.name]
      type            = submount.spec.type
      storage         = provider::units::from_gib(submount.spec.size_gibibytes)
      block           = provider::units::from_kib(submount.spec.block_size_kibibytes)
      forbid_deletion = submount.spec.forbid_deletion
    }
    if submount.spec != null
  })

  depends_on = [
    terraform_data.check_weka_count,
  ]

  parent_id = var.iam_project_id

  name = each.value.name

  type             = each.value.type
  size_bytes       = each.value.storage
  block_size_bytes = each.value.block
  forbid_deletion  = each.value.forbid_deletion

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}
data "nebius_compute_v1_filesystem" "jail_submount" {
  for_each = tomap({ for submount in var.jail_submounts :
    submount.name => submount.existing.id
    if submount.existing != null
  })

  id = each.value
}
locals {
  jail_submount = merge(
    tomap({ for submount in var.jail_submounts :
      submount.name => {
        id             = nebius_compute_v1_filesystem.jail_submount[submount.name].id
        size_gibibytes = floor(provider::units::to_gib(nebius_compute_v1_filesystem.jail_submount[submount.name].status.size_bytes))
        mount_tag      = "${local.const.filesystem.jail_submount_prefix}-${submount.name}"
      }
      if submount.spec != null
    }),
    tomap({ for submount in var.jail_submounts :
      submount.name => {
        id             = data.nebius_compute_v1_filesystem.jail_submount[submount.name].id
        size_gibibytes = floor(provider::units::to_gib(data.nebius_compute_v1_filesystem.jail_submount[submount.name].status.size_bytes))
        mount_tag      = "${local.const.filesystem.jail_submount_prefix}-${submount.name}"
      }
      if submount.existing != null
    }),
  )
}

locals {
  accounting_needed = var.accounting != null
}
resource "nebius_compute_v1_filesystem" "accounting" {
  count = local.accounting_needed ? (var.accounting.spec != null ? 1 : 0) : 0

  parent_id = var.iam_project_id

  name = local.name.filesystem.accounting

  type             = module.resources.shared_filesystem_types.network_ssd
  size_bytes       = provider::units::from_gib(var.accounting.spec.size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.accounting.spec.block_size_kibibytes)
  forbid_deletion  = var.accounting.spec.forbid_deletion
}
data "nebius_compute_v1_filesystem" "accounting" {
  count = local.accounting_needed ? (var.accounting.existing != null ? 1 : 0) : 0

  id = var.accounting.existing.id
}
locals {
  accounting = local.accounting_needed ? {
    id = try(
      one(nebius_compute_v1_filesystem.accounting).id,
      one(data.nebius_compute_v1_filesystem.accounting).id,
    )
    size_gibibytes = floor(provider::units::to_gib(try(
      one(nebius_compute_v1_filesystem.accounting).status.size_bytes,
      one(data.nebius_compute_v1_filesystem.accounting).status.size_bytes,
    )))
    mount_tag = local.const.filesystem.accounting
  } : {}
}

resource "terraform_data" "check_weka_count" {
  depends_on = [
    data.nebius_compute_v1_filesystem.jail,
    data.nebius_compute_v1_filesystem.jail_submount,
  ]

  lifecycle {
    precondition {
      condition = sum(
        concat(
          [(try(
            var.jail.spec.type,
            one(data.nebius_compute_v1_filesystem.jail).type
            ) == module.resources.shared_filesystem_types.weka
            ? 1
            : 0
          )],
          [for sm in var.jail_submounts : (
            try(
              sm.spec.type,
              data.nebius_compute_v1_filesystem.jail_submount[sm.name].type
            ) == module.resources.shared_filesystem_types.weka
            ? 1
            : 0
          )]
        )
      ) < 2
      error_message = "Total amount of WEKA filesystems couldn't be more than 1 for now."
    }
  }
}
